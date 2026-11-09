"""Inventory representation and validation, independent of Home Assistant UI.

Only explicitly chosen OpenWrt nodes are queried. Virtual switches are inert
placeholders for topology. Nothing in this module accesses a router.
"""
from __future__ import annotations

import json

try:
    from .telemetry import parse_nodes_json
except ImportError:
    from telemetry import parse_nodes_json


def decode_inventory(value: str | list | None) -> list[dict]:
    """Decode the stored inventory; keep the existing on-disk format stable."""
    if value is None or value == "":
        return []
    try:
        rows = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid inventory JSON") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Inventory must contain device records")
    return [row.copy() for row in rows]


def encode_inventory(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def ports_from_text(value: str) -> list[str]:
    return [part for part in value.replace(",", " ").split() if part]


def ports_as_text(value: list[str] | str | None) -> str:
    if isinstance(value, str):
        return value
    return ", ".join(value or [])


def validate_inventory(rows: list[dict], primary: dict) -> None:
    """Cross-check host uniqueness, uplinks, topology cycles and port names."""
    if len(rows) > 32:
        raise ValueError("Maximum 32 additional devices")
    parse_nodes_json(encode_inventory(rows), primary)


def apply_node(rows: list[dict], new_node: dict, old_host: str | None = None) -> list[dict]:
    """Stage an edit and fix children's uplink references after a host change."""
    out = [item.copy() for item in rows]
    if old_host is None:
        out.append(new_node.copy())
        return out
    index = next((i for i, item in enumerate(out) if item["host"] == old_host), None)
    if index is None:
        raise ValueError("Unknown node")
    out[index] = new_node.copy()
    if new_node["host"] != old_host:
        for item in out:
            if item.get("upstream") == old_host:
                item["upstream"] = new_node["host"]
    return out


def remove_node(rows: list[dict], host: str) -> list[dict]:
    """Remove a node only when no other device has it as an upstream."""
    if any(item.get("upstream") == host for item in rows):
        raise ValueError("has_children")
    if not any(item.get("host") == host for item in rows):
        raise ValueError("Unknown node")
    return [item.copy() for item in rows if item.get("host") != host]
