"""Pure data parsers for usteer and DHCP (doesn't depend on Home Assistant)."""
from __future__ import annotations

import re
from typing import Any

MAC_RE = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def norm_mac(value: Any) -> str | None:
    """Return a normalized MAC address, or None for invalid keys."""
    if isinstance(value, str) and MAC_RE.fullmatch(value):
        return value.lower()
    return None


def band_from_freq(freq: Any) -> str | None:
    try:
        freq = int(freq)
    except (TypeError, ValueError):
        return None
    if 2400 <= freq <= 2500:
        return "2.4G"
    if 4900 <= freq < 5925:
        return "5G"
    if 5925 <= freq <= 7125:
        return "6G"
    return None


def normalize_nodes(local: dict, remote: dict, local_host: str) -> dict[str, dict]:
    """Map usteer node keys to Wi-Fi metadata."""
    result = {}
    for source, is_remote in ((local, False), (remote, True)):
        if not isinstance(source, dict):
            raise ValueError("usteer node listing is not an object")
        for node_key, raw in source.items():
            if not isinstance(raw, dict):
                continue
            host, _, _ = node_key.partition("#") if is_remote else (local_host, "", node_key)
            rrm = raw.get("rrm_nr") or []
            info = raw.get("node_info") or {}
            bssid = norm_mac(raw.get("bssid"))
            result[node_key] = {
                "ap_host": host,
                "ap_name": info.get("name") if isinstance(info, dict) else None,
                "interface": node_key.split("#")[-1],
                "ssid": raw.get("ssid") or (rrm[1] if len(rrm) > 1 else None),
                "bssid": bssid,
                "band": band_from_freq(raw.get("freq")),
                "frequency": raw.get("freq"),
            }
    return result


def _connected(mac: str, node_key: str, data: dict, nodes: dict) -> dict | None:
    mac = norm_mac(mac)
    if not mac:
        return None
    if node_key not in nodes:
        # A disappearing AP can leave a stale client record in usteer.
        # Do not count it as an authoritative association.
        return None
    signal = data.get("signal")
    return {"mac": mac, "connected": True, "node_key": node_key, **nodes[node_key],
            "signal": signal if isinstance(signal, (int, float)) else None}


def parse_aggregated(clients: dict, nodes: dict) -> dict[str, dict]:
    """Parse get_clients: ONLY connected=true means actually connected."""
    if not isinstance(clients, dict):
        raise ValueError("usteer get_clients did not return an object")
    found = {}
    for mac, associations in clients.items():
        if not isinstance(associations, dict):
            continue
        for key, details in associations.items():
            if not isinstance(details, dict) or details.get("connected") is not True:
                continue
            record = _connected(mac, key, details, nodes)
            if record:
                # During roaming there can briefly be two true entries;
                # prefer local and then stronger signal, neither is infallible.
                score = ("#" not in key, record["signal"] or -200)
                if mac.lower() not in found or score > found[mac.lower()][0]:
                    found[mac.lower()] = (score, record)
    return {mac: rec for mac, (_, rec) in found.items()}


def parse_local_clients(clients: dict, nodes: dict, local_host: str) -> dict[str, dict]:
    """Parse connected_clients from an individual AP (authoritative)."""
    if not isinstance(clients, dict):
        raise ValueError("usteer connected_clients did not return an object")
    found = {}
    for key, stations in clients.items():
        if not isinstance(stations, dict):
            continue
        for mac, details in stations.items():
            if not isinstance(details, dict):
                continue
            rec = _connected(mac, key, details, nodes)
            if rec:
                found[rec["mac"]] = rec
    return found


def parse_dhcp_object(data: Any) -> dict[str, dict]:
    """Recursively extract DHCPv4 leases from different odhcpd layouts."""
    found: dict[str, dict] = {}

    def visit(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                visit(item)
        elif isinstance(obj, dict):
            mac = norm_mac(obj.get("mac") or obj.get("macaddr") or obj.get("mac_address"))
            if mac:
                ip = obj.get("ip") or obj.get("ipaddr") or obj.get("ipv4")
                name = obj.get("hostname") or obj.get("name")
                found[mac] = {"hostname": name if name not in ("*", "") else None,
                              "ip": ip}
            else:
                for value in obj.values():
                    visit(value)
    visit(data)
    return found


def parse_dnsmasq_leases(text: str) -> dict[str, dict]:
    """Parse /tmp/dhcp.leases: expiry MAC IPv4 hostname client-id."""
    found = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and (mac := norm_mac(parts[1])):
            found[mac] = {"ip": parts[2],
                          "hostname": parts[3] if parts[3] != "*" else None}
    return found


def parse_hostapd_clients(obj: dict, node_key: str, nodes: dict) -> dict[str, dict]:
    """Parse hostapd.BSS.get_clients; ignore unassociated stations."""
    if not isinstance(obj, dict) or not isinstance(obj.get("clients"), dict):
        raise ValueError("hostapd get_clients did not return a clients mapping")
    found = {}
    for mac, detail in obj["clients"].items():
        if not isinstance(detail, dict):
            continue
        # Some hostapd responses include pre-auth or stale entries.
        if detail.get("assoc", True) is False or detail.get("authorized", True) is False:
            continue
        if record := _connected(mac, node_key, detail, nodes):
            found[record["mac"]] = record
    return found


def connected_during_grace(was_connected: bool, first_missed_at: float, now: float, grace: float) -> bool:
    """A missing station stays home only during a continuous successful-miss grace period."""
    return bool(was_connected) and (now - first_missed_at) < grace
