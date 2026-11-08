"""Site-independent OpenWrt network monitoring: all configured APs and VLANs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time

from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOST, CONF_PORT, CONF_HTTPS, CONF_PASSWORD, CONF_USERNAME,
    CONF_NODES, CONF_ZONE, CONF_INTERVAL, CONF_GRACE,
    CONF_VLAN_MAP, CONF_WIRED_ENABLED, CONF_CENTRAL,
    DEFAULT_INTERVAL, DEFAULT_GRACE, DOMAIN,
)
from .parser import (
    parse_dhcp_object, parse_hostapd_clients, normalize_nodes, parse_aggregated,
    connected_during_grace,
)
from .identity import decode_static_hosts, merge_identity_sources, hostname_for_ip
from .rpc import UbusClient, RpcError
from .telemetry import (
    parse_nodes_json, decode_leases, interfaces_from_snapshot,
    cpu_percent, system_stats, select_ethernet, wifi_network_name, parse_neigh_json,
)
from .wired import parse_vlan_mapping, parse_networks, network_for_ip

_LOGGER = logging.getLogger(__name__)


class OpenWrtNetworkMonitorCoordinator(DataUpdateCoordinator):
    """Single transactional update: never infer absence from partial AP data."""

    def __init__(self, hass, entry):
        self.entry = entry
        opts = entry.options
        interval = int(opts.get(CONF_INTERVAL, DEFAULT_INTERVAL))
        super().__init__(
            hass, _LOGGER, name=f"OpenWrt network {entry.data[CONF_HOST]}",
            update_interval=timedelta(seconds=interval),
        )
        self.grace = int(opts.get(CONF_GRACE, DEFAULT_GRACE))
        self.wired_enabled = bool(opts.get(CONF_WIRED_ENABLED, True))
        self.central = bool(opts.get(CONF_CENTRAL, False))
        self.vlan_map = parse_vlan_mapping(opts.get(CONF_VLAN_MAP, ""))
        primary = {
            "host": entry.data[CONF_HOST],
            "name": opts.get("primary_name") or entry.data[CONF_HOST],
            "port": entry.data.get(CONF_PORT, 80),
            "https": entry.data.get(CONF_HTTPS, False),
            "username": entry.data[CONF_USERNAME],
            "password": entry.data[CONF_PASSWORD],
            "tls_fingerprint": entry.data.get("tls_fingerprint", ""),
            "uplink_ports": opts.get("primary_uplink_ports", "").replace(",", " ").split(),
            "edge_ports": opts.get("primary_edge_ports", "").replace(",", " ").split(),
        }
        rows = opts.get(CONF_NODES, entry.data.get(CONF_NODES, "[]"))
        self.all_nodes = parse_nodes_json(rows, primary)
        self.nodes = [n for n in self.all_nodes if n["role"] != "unmanaged_switch"]
        self.rpcs = {
            row["host"]: UbusClient(
                hass, row["host"], row["port"], row["https"],
                row["username"], row["password"], row.get("tls_fingerprint"),
            ) for row in self.nodes
        }
        self.primary = self.nodes[0]["host"]
        self.known: dict[str, dict] = {}
        self.missing_since: dict[str, float] = {}
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._previous_interfaces = {}
        self._previous_cpu = {}
        self._previous_mono = {}

    async def async_load_known(self):
        content = await self.store.async_load()
        if isinstance(content, dict) and isinstance(content.get("clients"), dict):
            self.known = content["clients"]
            # Unknown historical classifications must never become wired implicitly.
            for record in self.known.values():
                record.setdefault("ever_wifi", True)

    def _save_known(self):
        return {"clients": self.known}

    async def _read_node(self, node):
        host = node["host"]
        rpc = self.rpcs[host]
        # Snapshot must succeed on every included node, otherwise absence is
        # unknown; do NOT silently reclassify an AP outage as departure.
        snapshot, system, board, interfaces_dump, objects = await asyncio.gather(
            rpc.call("ha_netmon", "snapshot"), rpc.call("system", "info"),
            rpc.call("system", "board"), rpc.call("network.interface", "dump"),
            rpc.list_objects("hostapd.*"),
        )
        if not all(isinstance(i, dict) for i in (snapshot, system, board, interfaces_dump, objects)):
            raise ValueError(f"{host}: incomplete ubus snapshot")
        bsses = [key for key in objects if key.startswith("hostapd.")]
        usteer_local = await rpc.optional_call("usteer", "local_info")
        wireless_status = await rpc.optional_call("network.wireless", "status")
        wifi, wifi_nodes, wifi_ifaces = {}, {}, set()
        if node.get("wifi", node["role"] != "switch"):
            async def read_bss(key):
                status, clients = await asyncio.gather(
                    rpc.call(key, "get_status"), rpc.call(key, "get_clients")
                )
                metadata = (usteer_local or {}).get(key) or {}
                raw = {
                    **metadata,
                    "ssid": status.get("ssid") or metadata.get("ssid"),
                    "bssid": status.get("bssid") or metadata.get("bssid"),
                    "freq": status.get("freq") or status.get("frequency") or metadata.get("freq"),
                    "node_info": {"name": node["name"]},
                }
                node_key = f"{host}#{key}"
                parsed = normalize_nodes({}, {node_key: raw}, host)
                if node_key in parsed:
                    parsed[node_key]["network"] = wifi_network_name(
                        wireless_status, key.removeprefix("hostapd."), raw.get("ssid")
                    )
                return key, parsed, parse_hostapd_clients(clients, node_key, parsed)
            records = await asyncio.gather(*(read_bss(k) for k in bsses))
            for key, parsed, clients in records:
                wifi_nodes.update(parsed)
                wifi.update(clients)
                wifi_ifaces.add(key.removeprefix("hostapd."))
        return {
            "host": host, "snapshot": snapshot, "system": system, "board": board,
            "dump": interfaces_dump, "wifi": wifi, "wifi_nodes": wifi_nodes,
            "wireless_ifaces": wifi_ifaces, "wireless_status": wireless_status,
        }

    async def _async_update_data(self):
        try:
            rows = await asyncio.gather(*(self._read_node(node) for node in self.nodes))
            snapshots = {r["host"]: r["snapshot"] for r in rows}
            nodes, wifi = {}, {}
            wireless_ifaces = {}
            dhcp_sources_by_host = {}
            static_names_by_host = {}
            networks = []
            systems = {}
            now_mono = time.monotonic()
            for row in rows:
                host = row["host"]
                wireless_ifaces[host] = row["wireless_ifaces"]
                nodes.update(row["wifi_nodes"])
                for mac, rec in row["wifi"].items():
                    old = wifi.get(mac)
                    if old is None or (rec.get("signal") or -200) > (old.get("signal") or -200):
                        wifi[mac] = rec
                # Query each node, but give names configured on the primary
                # DHCP router precedence over entries on secondary nodes.
                reservations, static_names_by_ip = decode_static_hosts(row["snapshot"])
                dynamic = decode_leases(row["snapshot"])
                odhcpd = await self.rpcs[host].optional_call("dhcp", "ipv4leases")
                dhcp_sources_by_host[host] = (
                    ("dhcp_reservation", reservations),
                    ("dnsmasq_lease", dynamic),
                    ("odhcpd", parse_dhcp_object(odhcpd) if odhcpd else {}),
                )
                static_names_by_host[host] = static_names_by_ip
                networks.extend(parse_networks(row["dump"], self.vlan_map))
                dt = (now_mono - self._previous_mono[host]) if host in self._previous_mono else None
                stats = interfaces_from_snapshot(
                    row["snapshot"], self._previous_interfaces.get(host), dt
                )
                systems[host] = {
                    "name": next(n["name"] for n in self.nodes if n["host"] == host),
                    "board": row["board"],
                    "stats": system_stats(
                        row["system"],
                        cpu_percent(self._previous_cpu.get(host), row["snapshot"].get("cpu_ticks")),
                    ),
                    "interfaces": stats,
                }
            host_order = [self.primary] + [n["host"] for n in self.nodes if n["host"] != self.primary]
            leases = merge_identity_sources(
                source for host in host_order for source in dhcp_sources_by_host[host]
            )
            names_by_ip = [static_names_by_host[host] for host in host_order]
            # Explicit opt-in only: remote usteer aggregation is not sufficiently
            # authoritative for departing Wi-Fi users under normal operation.
            if self.central:
                root = self.rpcs[self.primary]
                local, remote, connected = await asyncio.gather(
                    root.call("usteer", "local_info"),
                    root.call("usteer", "remote_info"),
                    root.call("usteer", "get_clients"),
                )
                aggregate_nodes = normalize_nodes(local, remote, self.primary)
                for mac, rec in parse_aggregated(connected, aggregate_nodes).items():
                    if mac not in wifi:
                        wifi[mac] = rec
                nodes.update(aggregate_nodes)
            wired = select_ethernet(
                snapshots, self.all_nodes, wifi, leases, self.known,
                networks, wireless_ifaces,
            ) if self.wired_enabled else {}
            # Only commit baselines on a *successful complete* sample.
            for row in rows:
                host = row["host"]
                self._previous_mono[host] = now_mono
                self._previous_cpu[host] = row["snapshot"].get("cpu_ticks")
                self._previous_interfaces[host] = systems[host]["interfaces"]
        except (RpcError, ValueError, TypeError, KeyError, IndexError) as exc:
            # Keep monotonic missing timestamps: outage time is not evidence of
            # absence, but a successful new sample can restart that clock.
            self.missing_since.clear()
            raise UpdateFailed(str(exc)) from exc

        # A static-IP Wi-Fi client may not have a DHCP lease. A neighbour
        # entry is identity metadata only; the hostapd association remains
        # the sole evidence for Wi-Fi presence.
        neighbours = {}
        for snap in snapshots.values():
            for neighbour in parse_neigh_json(snap):
                neighbours.setdefault(neighbour["mac"], []).append(neighbour)
        for mac, rec in wifi.items():
            lease = leases.get(mac, {})
            neighbour = next((n for n in neighbours.get(mac, [])
                              if n["state"] in ("REACHABLE", "DELAY", "PROBE")), None)
            neighbour = neighbour or next(iter(neighbours.get(mac, [])), None)
            ip = lease.get("ip") or (neighbour.get("ip") if neighbour else None) or self.known.get(mac, {}).get("ip")
            net = network_for_ip(ip, networks)
            if net is None and (network_name := rec.get("network")):
                candidates = [n for n in networks if n.get("network") == network_name]
                if len(candidates) == 1:
                    net = candidates[0]
            static_ip_name = hostname_for_ip(ip, names_by_ip)
            rec.update({
                "hostname": lease.get("hostname") or static_ip_name,
                "name_source": lease.get("name_source") or ("dhcp_domain" if static_ip_name else None),
                "connection_type": "wifi", "ip": ip,
                "network": net.get("network") if net else None,
                "subnet": net.get("cidr") if net else None,
                "vlan": net.get("vlan") if net else None,
                "port": None, "bridge": None,
                "attached_to": rec.get("ap_name") or rec["ap_host"],
                "evidence": "hostapd" if rec.get("node_key") in nodes else "usteer",
            })
        present = {**wired, **wifi}
        timestamp = datetime.now(timezone.utc).isoformat()
        changed = False
        for mac, rec in present.items():
            old = self.known.get(mac, {})
            lease = leases.get(mac, {})
            ip = rec.get("ip") or lease.get("ip") or old.get("ip")
            ip_hostname = hostname_for_ip(ip, names_by_ip)
            hostname = lease.get("hostname") or ip_hostname or rec.get("hostname") or old.get("hostname")
            metadata = {
                "hostname": hostname,
                "name_source": (
                    lease.get("name_source") if lease.get("hostname") else
                    "dhcp_domain" if ip_hostname else
                    rec.get("name_source") or old.get("name_source")
                ),
                "ip": ip,
                "ever_wifi": bool(old.get("ever_wifi") or mac in wifi),
                "last_connection_type": rec["connection_type"],
            }
            if old != metadata:
                self.known[mac] = metadata
                changed = True
            self.missing_since.pop(mac, None)
        if changed:
            self.store.async_delay_save(self._save_known, 5)
        previous = self.data.get("clients", {}) if self.data else {}
        states = {}
        for mac, info in self.known.items():
            if mac in present:
                states[mac] = {**present[mac], **info, "connected": True,
                               "last_seen": timestamp}
            else:
                missed = self.missing_since.setdefault(mac, now_mono)
                prior = previous.get(mac, {})
                still = connected_during_grace(
                    bool(prior.get("connected")), missed, now_mono, self.grace
                )
                states[mac] = (
                    {**prior, **info, "connected": True}
                    if still else {
                        **info, "mac": mac, "connected": False,
                        "connection_type": info.get("last_connection_type"),
                        "ssid": None, "band": None, "signal": None,
                        "bssid": None, "ap_host": None, "ap_name": None,
                        "attached_to": None, "interface": None,
                        "frequency": None, "node_key": None,
                        "port": None, "bridge": None, "evidence": None,
                        "network": prior.get("network"),
                        "subnet": prior.get("subnet"), "vlan": prior.get("vlan"),
                        "last_seen": prior.get("last_seen"),
                    }
                )
        counts = {name: 0 for name in nodes}
        for rec in wifi.values():
            key = rec.get("node_key")
            if key in counts:
                counts[key] += 1
        wired_counts = {net["network"]: 0 for net in networks}
        for rec in wired.values():
            name = rec.get("network")
            if name:
                wired_counts[name] = wired_counts.get(name, 0) + 1
        return {
            "clients": states, "nodes": nodes, "counts": counts,
            "networks": networks, "wired_counts": wired_counts,
            "wired_present": wired, "systems": systems,
        }
