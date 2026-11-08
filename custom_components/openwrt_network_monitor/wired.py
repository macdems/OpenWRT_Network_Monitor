"""Pure network and VLAN parsing, independent of SSID conventions."""
from __future__ import annotations

from ipaddress import ip_address, ip_interface, ip_network
import re

VLAN_SUFFIX = re.compile(r"\.(\d{1,4})$")


def parse_vlan_mapping(value: str) -> dict[str, int]:
    """Optional overrides: logical interface=VID, one per comma or line."""
    result = {}
    for token in re.split(r"[,\n;]+", value or ""):
        if not token.strip():
            continue
        if "=" not in token:
            raise ValueError(f"Expected interface=VID: {token!r}")
        name, number = (p.strip() for p in token.split("=", 1))
        if not name or not number.isdecimal() or not 1 <= int(number) <= 4094:
            raise ValueError(f"Invalid VLAN mapping: {token!r}")
        result[name] = int(number)
    return result


def parse_networks(data: dict, vlans: dict[str, int]) -> list[dict]:
    """Obtain L3 IPv4 networks from netifd and optional user VLAN overrides."""
    rows = data.get("interface", [])
    if not isinstance(rows, list):
        raise ValueError("Invalid network.interface dump")
    result = []
    for row in rows:
        if not isinstance(row, dict) or row.get("up") is False:
            continue
        name = row.get("interface")
        if not isinstance(name, str) or name in ("loopback", "wan", "wan6"):
            continue
        dev = row.get("l3_device") or row.get("device")
        if not isinstance(dev, str):
            continue
        suffix = VLAN_SUFFIX.search(dev)
        vlan = vlans.get(name)
        if vlan is None and suffix:
            vlan = int(suffix.group(1))
        for address in row.get("ipv4-address", []):
            if not isinstance(address, dict):
                continue
            try:
                cidr = ip_interface(f"{address['address']}/{address['mask']}").network
            except (TypeError, ValueError, KeyError):
                continue
            result.append({"network": name, "cidr": str(cidr),
                           "device": dev, "vlan": vlan,
                           "router_ip": address["address"]})
    return result


def network_for_ip(ip: str | None, networks: list[dict]) -> dict | None:
    if not ip:
        return None
    try:
        addr = ip_address(ip)
    except ValueError:
        return None
    matches = [n for n in networks
               if addr in ip_network(n["cidr"]) and ip != n.get("router_ip")]
    return max(matches, key=lambda n: ip_network(n["cidr"]).prefixlen, default=None)
