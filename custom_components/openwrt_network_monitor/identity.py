"""Resolve client identity from read-only dnsmasq / UCI metadata.

Name lookups are metadata only. Neither a lease nor a reservation is proof
that an endpoint is present on the network.
"""
from __future__ import annotations

import base64
import binascii
from ipaddress import IPv4Address
import shlex
from typing import Any, Iterable

try:
    from .parser import norm_mac
except ImportError:  # Unit tests run the pure Python modules without Home Assistant.
    from parser import norm_mac

MAX_BASE64 = 2_000_000


def valid_hostname(name: Any) -> str | None:
    """Discard empty/unassigned placeholders and control characters."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or name in {"*", "-", "?", "(unknown)", "unknown"}:
        return None
    if len(name) > 253 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        return None
    return name


def valid_ipv4(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(IPv4Address(value))
    except ValueError:
        return None


def _decode_b64(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_BASE64:
        return ""
    try:
        return base64.b64decode(value, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def parse_static_dhcp_hosts(text: str) -> tuple[dict[str, dict], dict[str, str]]:
    """Parse a *filtered* `uci -X show dhcp` with host/domain fields only.

    The OpenWrt helper excludes all other DHCP/UCI configuration, including
    unrelated options. Handles quoted hostnames and multi-MAC host sections.
    """
    section_types: dict[str, str] = {}
    section_fields: dict[str, dict[str, list[str]]] = {}
    for line in text.splitlines():
        key, sep, val = line.strip().partition("=")
        if not sep or not key.startswith("dhcp."):
            continue
        # Section keys have one dot; options have at least two.
        if key.count(".") == 1:
            section_types[key] = val.strip(" \t\"'")
            continue
        section, _, option = key.rpartition(".")
        if option not in {"mac", "name", "ip"}:
            continue
        try:
            tokens = shlex.split(val)
        except ValueError:
            continue
        if tokens:
            section_fields.setdefault(section, {}).setdefault(option, []).extend(tokens)

    by_mac: dict[str, dict] = {}
    by_ip: dict[str, str] = {}
    for section, fields in section_fields.items():
        if section_types.get(section) not in {"host", "domain"}:
            continue
        names = fields.get("name", [])
        hostname = valid_hostname(" ".join(names)) if names else None
        if not hostname:
            continue
        ip = next((v for v in (valid_ipv4(raw) for raw in fields.get("ip", [])) if v), None)
        if ip:
            by_ip.setdefault(ip, hostname)
        if section_types[section] != "host":
            continue
        for raw in fields.get("mac", []):
            for item in raw.replace(",", " ").split():
                if mac := norm_mac(item):
                    by_mac[mac] = {"hostname": hostname, "ip": ip, "name_source": "dhcp_reservation"}
    return by_mac, by_ip


def decode_static_hosts(snapshot: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """Handle optional static DHCP mappings from ha_netmon.snapshot."""
    return parse_static_dhcp_hosts(_decode_b64(snapshot.get("dhcp_static_hosts_b64")))


def merge_identity_sources(sources: Iterable[tuple[str, dict[str, dict]]]) -> dict[str, dict]:
    """Merge priority-ordered sources field by field without losing good names.

    A later source with hostname=null or hostname='*' must never erase a name
    resolved from the primary router. Sources are ordered by coordinator.
    """
    out: dict[str, dict] = {}
    for source, records in sources:
        for raw_mac, data in records.items():
            mac = norm_mac(raw_mac)
            if not mac or not isinstance(data, dict):
                continue
            target = out.setdefault(mac, {"hostname": None, "ip": None, "name_source": None})
            if not target["hostname"] and (hostname := valid_hostname(data.get("hostname"))):
                target["hostname"] = hostname
                target["name_source"] = data.get("name_source") or source
            if not target["ip"] and (ip := valid_ipv4(data.get("ip"))):
                target["ip"] = ip
    return out


def hostname_for_ip(ip: str | None, sources: Iterable[dict[str, str]]) -> str | None:
    """Fallback for dnsmasq `config domain` entries without MAC bindings."""
    if not ip:
        return None
    for mapping in sources:
        if (name := valid_hostname(mapping.get(ip))) is not None:
            return name
    return None
