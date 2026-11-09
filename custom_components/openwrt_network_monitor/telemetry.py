"""Pure functions: read-only OpenWrt telemetry and FDB-to-client topology.

No HA imports. Only a *current* hostapd association is authoritative for Wi-Fi.
Bridge FDB has a finite ageing delay and is explicitly treated as weaker evidence.
"""
from __future__ import annotations

from ipaddress import ip_address
import base64
import binascii
import json
import re
from typing import Any

try:
    from .parser import norm_mac, parse_dnsmasq_leases
    from .wired import network_for_ip
except ImportError:
    from parser import norm_mac, parse_dnsmasq_leases
    from wired import network_for_ip

IFNAME_RE = re.compile(r"^[a-zA-Z0-9_.:@-]{1,32}$")


def parse_nodes_json(raw: str, primary: dict) -> list[dict]:
    """Validate list of arbitrary AP/router/switch nodes and their uplinks.

    No user-supplied string reaches a shell command. All HTTP requests are
    constructed from strict hostnames/IPs and fixed /ubus endpoints.
    """
    try:
        additional = json.loads(raw or "[]")
    except (ValueError, TypeError) as exc:
        raise ValueError("Nodes must be a JSON list") from exc
    if not isinstance(additional, list) or len(additional) > 32:
        raise ValueError("Nodes must be a JSON list with <= 32 entries")
    result = [{**primary, "role": "router", "upstream": None, "uplink_ports": primary.get("uplink_ports", []), "edge_ports": primary.get("edge_ports", []), "name": primary.get("name") or primary["host"]}]
    seen = {primary["host"]}
    for row in additional:
        if not isinstance(row, dict):
            raise ValueError("Each node must be a JSON object")
        host = str(row.get("host", "")).strip()
        # Restrict host to IPv4/IPv6/domain literals (no slash, URL, userinfo).
        if not host or len(host) > 253 or re.search(r"[^a-zA-Z0-9.:-]", host):
            raise ValueError(f"Invalid node host: {host!r}")
        if host in seen:
            raise ValueError(f"Duplicate host: {host}")
        seen.add(host)
        role = row.get("role", "ap")
        if role not in ("router", "ap", "switch", "auto", "unmanaged_switch"):
            raise ValueError(f"Invalid role for {host}")
        ports = row.get("uplink_ports", [])
        edges = row.get("edge_ports", [])
        if not isinstance(ports, list) or any(not isinstance(p, str) or not IFNAME_RE.fullmatch(p) for p in ports):
            raise ValueError(f"Invalid uplink_ports on {host}")
        if not isinstance(edges, list) or any(not isinstance(p, str) or not IFNAME_RE.fullmatch(p) for p in edges):
            raise ValueError(f"Invalid edge_ports on {host}")
        if set(ports) & set(edges):
            raise ValueError(f"A port cannot be both uplink and edge on {host}")
        obj = {**primary, **row, "host": host, "role": role,
               "name": str(row.get("name") or host), "uplink_ports": ports,
               "edge_ports": edges}
        if not isinstance(obj.get("port"), int) or not (1 <= obj["port"] <= 65535):
            raise ValueError(f"Invalid port on {host}")
        if not isinstance(obj.get("https"), bool):
            raise ValueError(f"Invalid https on {host}")
        if role == "unmanaged_switch":
            parent_port = obj.get("parent_port")
            if not isinstance(parent_port, str) or not IFNAME_RE.fullmatch(parent_port):
                raise ValueError(f"Unmanaged switch {host} needs parent_port")
            if not obj.get("upstream"):
                raise ValueError(f"Unmanaged switch {host} needs upstream")
        result.append(obj)
    for row in result[1:]:
        if row.get("upstream") and row["upstream"] not in seen:
            raise ValueError(f"Unknown upstream node for {row['host']}")
        cur, visited = row, set()
        while cur.get("upstream"):
            if cur["host"] in visited:
                raise ValueError("Topology cycle")
            visited.add(cur["host"])
            cur = next(r for r in result if r["host"] == cur["upstream"])
    return result


def decode_leases(snapshot: dict) -> dict[str, dict]:
    value = snapshot.get("dhcp_leases_b64", "")
    if not isinstance(value, str) or len(value) > 2_000_000:
        return {}
    try:
        return parse_dnsmasq_leases(base64.b64decode(value, validate=True).decode("utf-8", "replace"))
    except (binascii.Error, ValueError):
        return {}


def parse_fdb_json(snapshot: dict, host: str, excluded_ports: set[str]) -> list[dict]:
    entries = snapshot.get("fdb")
    if not isinstance(entries, list):
        raise ValueError(f"{host} returned malformed FDB")
    found = []
    for item in entries:
        if not isinstance(item, dict) or not (mac := norm_mac(item.get("mac"))):
            continue
        if int(mac[:2], 16) & 1:
            continue
        port = item.get("ifname")
        if not isinstance(port, str) or port in excluded_ports:
            continue
        flags = item.get("flags", [])
        flags = {str(f).lower() for f in (flags if isinstance(flags, list) else [flags])}
        for flag in ("state", "type"):
            if isinstance(item.get(flag), str):
                flags.add(item[flag].lower())
        if {"self", "permanent", "static", "local"} & flags:
            continue
        if item.get("master") is None and "master" not in flags:
            # Some bridge builds omit master entirely; still accept the
            # dynamic FDB entry if the port belongs to the bridge.
            pass
        vlan = item.get("vlan")
        try:
            vlan = int(vlan) if vlan is not None else None
        except (TypeError, ValueError):
            vlan = None
        found.append({"mac": mac, "host": host, "port": port,
                      "vlan": vlan, "bridge": item.get("master") if isinstance(item.get("master"), str) else None})
    return found


def parse_neigh_json(snapshot: dict) -> list[dict]:
    rows = snapshot.get("neighbours")
    if not isinstance(rows, list):
        raise ValueError("Malformed neighbour table")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mac = norm_mac(row.get("lladdr"))
        dest = row.get("dst")
        if not mac or not isinstance(dest, str):
            continue
        try:
            ip_address(dest)
        except ValueError:
            continue
        state = row.get("state", "")
        if isinstance(state, list):
            state = state[0] if state else ""
        if state in ("FAILED", "INCOMPLETE", "NOARP", "NONE"):
            continue
        result.append({"mac": mac, "ip": dest, "state": state, "device": row.get("dev")})
    return result


def cpu_percent(previous: list | None, current: list | None) -> float | None:
    """Linux /proc/stat utilisation between successful samples (not load average)."""
    if not isinstance(previous, list) or not isinstance(current, list) or len(current) < 4 or len(previous) < 4:
        return None
    try:
        elapsed = sum(int(c) - int(p) for p, c in zip(previous, current))
        idle = (int(current[3]) - int(previous[3]))
        if len(current) > 4 and len(previous) > 4:
            idle += int(current[4]) - int(previous[4])
        if elapsed <= 0 or idle < 0 or idle > elapsed:
            return None
        return round(100 * (elapsed - idle) / elapsed, 1)
    except (TypeError, ValueError):
        return None


def system_stats(info: dict, cpu: float | None) -> dict:
    """Extract system.info; procd load averages are 16.16 fixed point."""
    load = info.get("load", [])
    memory = info.get("memory", {})
    values = {}
    for i, name in enumerate(("load_1", "load_5", "load_15")):
        value = load[i] if isinstance(load, list) and len(load) > i else None
        values[name] = round(value / 65536, 3) if isinstance(value, (int, float)) else None
    if not isinstance(memory, dict):
        memory = {}
    total = memory.get("total")
    available = memory.get("available")
    if not isinstance(available, (float, int)):
        available = sum(memory.get(k, 0) for k in ("free", "buffered", "cached"))
    values.update({"uptime": info.get("uptime"), "cpu_percent": cpu,
                   "memory_total": total,
                   "memory_available":  round(available / 1048576, 3) if isinstance(available, (int, float)) else None,
                   "memory_percent": round(100 * (total - available) / total, 1)
                   if isinstance(total, (int, float)) and total > 0 else None})
    return values


def _parse_counters(link: dict) -> dict:
    stats = link.get("stats64") or link.get("stats") or {}
    if not isinstance(stats, dict):
        stats = {}
    return {
        f"{direction}_{field}": v
        for direction in ("rx", "tx")
        for field in ("bytes", "packets", "errors", "dropped")
        if isinstance((v := (stats.get(direction) or {}).get(field)), (int, float))
    }


def interfaces_from_snapshot(snapshot: dict, last: dict | None, dt: float | None) -> dict[str, dict]:
    """Per interface stats and real traffic Mbps from 64-bit kernel counters."""
    links = snapshot.get("links")
    if not isinstance(links, list):
        raise ValueError("Malformed ip -j -s link output")
    speeds = snapshot.get("speeds_mbps") or {}
    result = {}
    for link in links:
        if not isinstance(link, dict) or not (name := link.get("ifname")) or not isinstance(name, str):
            continue
        linkinfo = link.get("linkinfo") or {}
        info_data = linkinfo.get("info_data") or {}
        stats = _parse_counters(link)
        item = {"name": name, "up": "UP" in link.get("flags", []),
                "operstate": link.get("operstate"), "mtu": link.get("mtu"),
                "mac": link.get("address"), "link_type": link.get("link_type"),
                "kind": linkinfo.get("info_kind"),
                "vlan": info_data.get("id") if linkinfo.get("info_kind") == "vlan" else None,
                "speed_mbps": speeds.get(name), **stats}
        prior = (last or {}).get(name, {})
        for direction in ("rx", "tx"):
            cur = item.get(f"{direction}_bytes")
            old = prior.get(f"{direction}_bytes")
            item[f"{direction}_mbps"] = (
                round(8 * (cur - old) / dt / 1_000_000, 3)
                if isinstance(cur, (int, float)) and isinstance(old, (int, float))
                and dt and dt > 0 and cur >= old else None
            )
        result[name] = item
    return result


def select_ethernet(
    snapshots: dict[str, dict], node_configs: list[dict], wifi: dict,
    leases: dict, known: dict, networks: list[dict], wireless_ifaces: dict[str, set[str]],
) -> dict[str, dict]:
    """Choose deepest FDB sighting; suppress ambiguous stale Wi-Fi on trunks.

    A previously Wi-Fi-seen MAC must have explicit *edge* evidence, i.e.
    FDB on an explicitly configured edge_ports entry. Otherwise do not
    reclassify cached Wi-Fi MACs: the MAC may be stale on an AP uplink.
    """
    by_host = {n["host"]: n for n in node_configs}
    def depth(host):
        seen, count = set(), 0
        while host in by_host and by_host[host].get("upstream") and host not in seen:
            seen.add(host)
            count += 1
            host = by_host[host]["upstream"]
        return count
    candidates: dict[str, list[tuple[int, bool, dict]]] = {}
    for host, snapshot in snapshots.items():
        node = by_host[host]
        skip = wireless_ifaces.get(host, set()) | set(node.get("uplink_ports", []))
        try:
            rows = parse_fdb_json(snapshot, host, skip)
        except ValueError:
            raise
        down_ports = {
            link.get("ifname")
            for link in snapshot.get("links", [])
            if isinstance(link, dict) and link.get("operstate") in ("DOWN", "LOWERLAYERDOWN")
        }
        for fdb in rows:
            if fdb["port"] in down_ports:
                continue  # FDB may remain until ageing, but port is down
            mac = fdb["mac"]
            if mac in wifi:
                continue
            edge = fdb["port"] in node.get("edge_ports", [])
            if known.get(mac, {}).get("ever_wifi") and not edge:
                continue
            candidates.setdefault(mac, []).append((depth(host), edge, fdb))
    neighs = [n for host, payload in snapshots.items() for n in parse_neigh_json(payload)]
    neigh_by_mac: dict[str, list] = {}
    for n in neighs:
        neigh_by_mac.setdefault(n["mac"], []).append(n)
    virtual_switches = [n for n in node_configs if n["role"] == "unmanaged_switch"]
    found = {}
    for mac, rows in candidates.items():
        _, edge, entry = max(rows, key=lambda p: (p[0], p[1]))
        neighbours = neigh_by_mac.get(mac, [])
        neighbour = next((n for n in neighbours if n["state"] in ("REACHABLE", "DELAY", "PROBE")), None)
        neighbour = neighbour or next(iter(neighbours), None)
        lease = leases.get(mac, {})
        ip = neighbour.get("ip") if neighbour else lease.get("ip")
        network = network_for_ip(ip, networks)
        if network is None and entry["vlan"] is not None:
            matches = [n for n in networks if n.get("vlan") == entry["vlan"]]
            if len(matches) == 1:
                network = matches[0]
        if network is None and entry["bridge"]:
            matches = [n for n in networks if n.get("device") == entry["bridge"]]
            if len(matches) == 1:
                network = matches[0]
        virtual = next((n for n in virtual_switches
                        if n.get("upstream") == entry["host"]
                        and n.get("parent_port") == entry["port"]), None)
        found[mac] = {
            "mac": mac, "ip": ip,
            "hostname": lease.get("hostname"), "connected": True,
            "connection_type": "ethernet", "ap_host": entry["host"],
            "ap_name": by_host[entry["host"]]["name"], "attached_to": (virtual["name"] if virtual else by_host[entry["host"]]["name"]),
            "downstream_unmanaged_switch": (virtual["name"] if virtual else None),
            "port": entry["port"], "bridge": entry["bridge"],
            "vlan": entry["vlan"] if entry["vlan"] is not None else (network.get("vlan") if network else None),
            "network": network.get("network") if network else None,
            "subnet": network.get("cidr") if network else None,
            "evidence": "bridge_fdb",
            "edge_verified": edge,
        }
    return found


def wifi_network_name(wireless_status: dict | None, ifname: str, ssid: str | None) -> str | None:
    """Resolve AP BSS -> configured OpenWrt logical network when available.

    SSID is not a VLAN, and multiple BSSes may share a name. Prefer ifname.
    """
    if not isinstance(wireless_status, dict):
        return None
    matches = []
    for radio in wireless_status.values():
        if not isinstance(radio, dict):
            continue
        for item in radio.get("interfaces", []):
            if not isinstance(item, dict):
                continue
            config = item.get("config") or {}
            network = config.get("network")
            if isinstance(network, list):
                network = next((n for n in network if isinstance(n, str)), None)
            if not isinstance(network, str):
                continue
            if item.get("ifname") == ifname:
                return network
            if config.get("ssid") == ssid and ssid:
                matches.append(network)
    return matches[0] if len(matches) == 1 else None
