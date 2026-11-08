# AGENTS.md — OpenWrt Network Monitor

## Project mission

This repository provides a local-only, read-only Home Assistant custom integration for monitoring OpenWrt infrastructure. It discovers Wi-Fi clients on access points, wired clients learned through Linux bridge forwarding tables, logical networks and VLANs, and system/interface telemetry. It exposes device trackers, sensors, and binary sensors. It does not control routers, change network settings, steer clients, reboot devices, or open firewall rules.

The integration domain is **`openwrt_network_monitor`**. Installable Python code belongs under `custom_components/openwrt_network_monitor/`. The OpenWrt executable `openwrt/ha_netmon` is a limited `rpcd` plugin exposing only `ha_netmon.snapshot`. `openwrt/ha-netmon-acl.json` defines the restricted read-only RPC role; `tools/audit_acl.py` checks the account's effective permissions. Keep the installation documentation accurate for a fresh installation.

## General-purpose design

- Never hardcode any deployment-specific address, hostname, MAC, AP inventory, interface name, SSID, VLAN ID, credentials, network layout, or zone other than the default `zone.home`.
- Permit a configurable number of OpenWrt routers, APs, and switches, optional unmanaged-switch placeholders, upstream relationships, uplink ports, and verified edge ports. Physical topology is incomplete if hardware cannot expose a downstream port; don't fabricate information.
- VLANs and subnets are determined from OpenWrt network/interface data, with user-configured overrides only where necessary. A network may have zero, one, or many SSIDs. SSID is not a reliable VLAN identifier.
- Use public documentation ranges (e.g. `192.0.2.0/24`) and invented generic names (AP Alpha, AP Beta) in documentation, tests, and examples. No private network topology or user-specific details should appear anywhere in distributed files.

## Presence correctness

- Prefer direct `hostapd.*.get_clients` association data for Wi-Fi presence and use `usteer` for supplementary information, not as an authoritative cache of active connections.
- Create a stable device tracker per MAC. Resolve hostnames from primary-router dnsmasq DHCP leases, static DHCP `config host` reservations, and `config domain` IP entries. A blank or `*` value must not override a valid hostname. Keep a stable MAC-based tracker unique ID and never rename a user-customized device. Do not use DHCP metadata as proof of presence. Expose SSID, BSSID, radio band, RSSI, AP, port, logical network/VLAN and connection type when grounded in the source data.
- A DHCP lease or IP neighbour record alone is **not proof of presence**. FDB is a time-limited learning cache, not proof of current physical Ethernet attachment; bridge uplinks can also expose Wi-Fi clients' MACs. Different interfaces may share one MAC or an endpoint may randomize it.
- Apply a configurable departure grace period. **Never mark clients `not_home` due to a partial poll, failed AP query, or unreachable node**: preserve uncertainty and mark the impacted tracker unavailable until a complete reliable observation can be made.
- Keep multi-node observations transactional; reconcile roaming before deciding someone has departed. Persist previously discovered clients across restarts without promoting stale stored entries to connected.
- Use Home Assistant `ScannerEntity` semantics and `associated_zone` configuration, with `zone.home` as the default.

## Security invariants

- Access OpenWrt using JSON-RPC `/ubus` with a dedicated **read-only** account. Grant only explicit required methods in `openwrt/ha-netmon-acl.json`; never grant `file.exec`, `file.write`, unrestricted `file.read`, arbitrary shell/command execution, `uci`, restart/reboot, steering modifications, or wildcard administrator permissions.
- The `ha_netmon` RPC plugin exposes only a fixed read-only `snapshot` method. It may locally read dnsmasq lease files and filtered UCI host/name fields from a fixed command; it must not grant remote callers `uci.*` or arbitrary file-reading privileges. It must not execute user-provided commands, use parameters to construct paths or shell commands, or read arbitrary files. Keep its installation path root-owned and not writable by the RPC user.
- Verify that `rpcd` login roles are additive: the restricted ACL does not remove rights from other assigned roles. Keep an effective-permissions audit that checks dangerous methods with `session.access` **without calling them**.
- Prefer verified TLS or certificate fingerprint pinning; never silently disable TLS validation. Do not log credentials, bearer/session tokens, lease data unnecessarily, or reveal private topology in sample output.
- Do not expose `/ubus` publicly. Advise firewall source restrictions and least privilege.

## Telemetry and dashboard contracts

- Report interface RX/TX throughput in Mbit/s from deltas between monotonic successive byte counters; distinguish traffic rates from a negotiated Ethernet PHY/link speed. Do not add counters across bridge, VLAN, and physical parent interfaces because of double counting.
- Collect CPU utilization, load averages, RAM, uptime, interface status, counters and errors per configured OpenWrt node. Handle missing sysfs speeds or counter resets explicitly.
- `examples/` contains **English** Network Flow Card 4.x and Lovelace examples; `examples/pl/` contains **Polish** counterparts with the same filenames. Every displayed ID is a placeholder until configured in HA.
- The `network-flow-card` third-party integration has no guaranteed native discovery or automatic client-parent matching for this integration. Default to `hyperbolic_client_layout: grouped` unless attachment has been verified.

## Contributor workflow

1. Update source, translations (`strings.json`, `translations/en.json`, `translations/pl.json`), examples and documentation consistently when adding or changing exposed entities or config flow fields.
2. Run `python3 -m compileall -q custom_components tests`, `python3 -m unittest discover -s tests -v`, `sh -n openwrt/ha_netmon`, and `openwrt/ha_netmon call snapshot | python3 -m json.tool` where platform tools exist.
3. Verify `openwrt/ha-netmon-acl.json` remains read-only and the executable cannot accept arbitrary commands or input paths. On a test router, run `tools/audit_acl.py` against the restricted account.
4. Check YAML syntax and that `examples/` and `examples/pl/` are parallel. Examine generated distribution archives for secrets and deployment-specific topology before publishing.
5. Be explicit about limitations. Unit tests and static audits do not prove end-to-end Home Assistant/OpenWrt runtime compatibility; real-hardware testing is required.
