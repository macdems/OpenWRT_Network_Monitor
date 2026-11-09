# OpenWrt Network Monitor

**Custom integration for Home Assistant 2026.11+**, using OpenWrt JSON-RPC `/ubus` and a tightly scoped **read-only `rpcd` executable plugin**. No SNMP, SSH account, shell over RPC, or router configuration permissions are required. All network names, VLANs, AP addresses, hierarchy and trusted wired ports are configurable. Does **not** require `usteer` in direct AP mode: `hostapd` provides local BSS/association data.

**Not yet validated end-to-end on a physical OpenWrt 25.12 router and Home Assistant runtime.** Pure parsers, static security policy and local helper execution have been tested. Review permissions and deploy to a test router before production.

## Features

- **Wi-Fi clients:** automatic device trackers with hostname resolved from dnsmasq on the primary router (including static reservations), MAC, IP, actual SSID, BSSID (`ap_mac` alias), radio frequency, band (2.4G/5G/6G), RSSI in dBm, AP name/host, logical network and VLAN when resolvable.
- **Ethernet clients:** bridge FDB from **every configured OpenWrt AP/router/switch**, not just the main router. Identify endpoint by MAC, name, IP, VLAN, bridge, edge AP and port where learned. Handles VLANs without Wi-Fi; **DHCP leases alone never establish presence**.
- **Configurable topology:** arbitrary OpenWrt nodes (`router`, `ap`, `switch`, `auto`) with optional upstream relationship and uplink ports; non-queryable `unmanaged_switch` placeholders with a parent node/port. The physical port beyond a dumb switch cannot be discovered.
- **Presence:** one `device_tracker` per client MAC. Uses latest *successful full set of observations*, not cached DHCP data. Configurable zone (`zone.home` default), poll interval (20s default), consecutive-miss grace window (45s default). An AP outage makes trackers **unavailable**, not `not_home`. Records of previously seen clients persist across HA restart.
- **Router/AP metrics:** CPU utilisation sampled from `/proc/stat` (after two polls), load averages 1/5/15, memory consumption/available, uptime.
- **All Linux network interfaces** on every queried node: RX/TX current throughput in Mbit/s calculated from byte-counter deltas, byte/packet/error/drop counters, link state, MTU, address, VLAN interface metadata, negotiated Ethernet link speed (when sysfs reports it). One connectivity binary sensor per interface. Large counter/error sensor sets are created but disabled by default; key attributes remain accessible.
- **Per-BSS Wi-Fi client counts** and per logical network Ethernet client counts, with optional client lists.

### Limits and non-goals

- FDB is a *learning cache*, not instantaneous presence. An unplugged Ethernet device may remain visible until FDB ageing. When the known physical port is down, the helper suppresses old entries immediately; this does **not** work for individual devices behind an active unmanaged switch uplink. Silent clients may disappear before disconnection. For strong wired presence, combine switch port status or independent connectivity probes.
- Router-side FDB includes Wi-Fi client MACs traversing wired AP uplinks. A MAC with a history of Wi-Fi is **not** reclassified as wired on ambiguous FDB. For intentional Wi-Fi→Ethernet transitions, list **verified client-facing ports** in `edge_ports`; do not list trunks/AP uplinks there.
- Bridge FDB alone cannot find unmanaged switch internals, a client behind NAT, or an exact switch port on hardware that does not provide it.
- A single MAC is a tracker. A laptop using different Wi-Fi/Ethernet MACs will yield separate entities; randomised mobile Wi-Fi MACs also appear separately.
- Not all Wi-Fi radios or virtual interfaces have a negotiated physical link speed. `rx_mbps/tx_mbps` are traffic **throughput**, not radio PHY rate. Initial measurement is unknown until the second valid snapshot.
- No traffic classification per client and no wired active ARP scanning in this version. VLAN ID might be unknown on separate bridges unless set explicitly.
- An arbitrary device tracker entity can be used in Lovelace. *Automatic* client positioning in third-party `network-flow-card` depends on that card's supported attribute conventions. `ap_mac`, `attached_to`, `port` and `vlan` are exposed for further integration.

## A. Install on each OpenWrt device (AP/router/managed OpenWrt switch)

1. Install RPC and read-only telemetry prerequisites:

   ```sh
   apk update
   apk add uhttpd-mod-ubus ip-full ip-bridge
   # ensure rpcd, uhttpd and hostapd are running as appropriate
   ```

   For older OpenWrt systems using `opkg`, replace `apk` commands with `opkg update` and `opkg install ...`. `ip-full` and `ip-bridge` provide JSON `ip -j` and `bridge -j` output. `base64`, `awk`, `tr`, and `/proc/stat` are normally provided by BusyBox/the kernel.

1. Copy **`openwrt/ha_netmon`** from this archive to `/usr/libexec/rpcd/ha_netmon`:

   ```sh
   install -o root -g root -m 0755 ha_netmon /usr/libexec/rpcd/ha_netmon
   # run a local test, not over HA JSON-RPC:
   /usr/libexec/rpcd/ha_netmon list
   /usr/libexec/rpcd/ha_netmon call snapshot | jsonfilter -e '@.links'
   ```

   **Method**: `ha_netmon.snapshot {}`. The executable has a fixed `list/call snapshot` dispatch, **does not parse request JSON**, and reads only `bridge -j fdb show`, `ip -j -4 neigh show`, `ip -j -s link show`, `/proc/stat`, link-speed sysfs files and dnsmasq leasefiles (as Base64), plus a filtered set of DHCP host/domain names from UCI. It has **no remote command, file path, arbitrary argument, edit, restart or control method**. It runs as part of `rpcd` (often root), so keep it `root:root`, non-writable by other users, and review source before installing.

1. Copy `openwrt/ha-netmon-acl.json` to `/usr/share/rpcd/acl.d/ha-netmon.json`.

1. Create a dedicated RPC login `hass` (not root/admin, no interactive shell). Use the [Home Assistant OpenWrt ubus documentation](https://www.home-assistant.io/integrations/ubus/) for supported user creation. Use a long, random password. In `/etc/config/rpcd` configure its **only** privileges:

   ```text
   config login
      option username 'hass'
      option password '$p$hass'
      list read 'ha_netmon_read'
      list read 'unauthenticated'
   ```

   Do **not** add `list write`, `luci`, `root`, `*`, `uci`, or another broad administrative role to this login. The ACL has only `read.ubus` for these methods:

   | ubus object         | Allowed methods                            |
   |---------------------|--------------------------------------------|
   | `ha_netmon`         | `snapshot`                                 |
   | `system`            | `info`, `board`                            |
   | `network.interface` | `dump`                                     |
   | `network.device`    | `status`                                   |
   | `network.wireless`  | `status`                                   |
   | `hostapd.*`         | `get_clients`, `get_status`                |
   | `usteer`            | `local_info`, `remote_info`, `get_clients` |
   | `dhcp`              | `ipv4leases`                               |

   **`file.exec`, `file.read`, `file.write`, `uci.*`, `network.restart`, Wi-Fi steering modifications, reboot and other administrative methods are not granted.** Review other roles already assigned to `hass`: RPC permissions are **additive**. A strict role cannot revoke privileges from another role. Do not expose `/ubus` on WAN or guest networks, and restrict access through the firewall to the HA management address.

1. Reload RPC and verify the object:

   ```sh
   /etc/init.d/rpcd restart
   ubus -v list ha_netmon
   ubus call ha_netmon snapshot
   ubus call network.interface dump
   ```

   **Important:** local `ubus` commands on an SSH root session bypass the HTTP/RPC login role, so their success is not proof that HA's read-only ACL works. The archive includes `tools/audit_acl.py`, which authenticates via **HTTPS** as your restricted user and calls `session.access` to verify permitted reads and that dangerous functions are denied. It does **not actually execute** any write method. Run it using a certificate your workstation trusts:

   ```sh
   python3 tools/audit_acl.py https://router.example
   ```

   On devices with `usteer` but no Wi-Fi/BSS, the plugin works and exposes CPU/interfaces; AP associations need `hostapd`. Each OpenWrt node must expose the plugin and appropriate read-only methods.

1. Preserve after sysupgrade if necessary (OpenWrt normally does **not** retain arbitrary files in `/usr/libexec` unless specified):

   ```text
   # /etc/sysupgrade.conf
   /usr/libexec/rpcd/ha_netmon
   /usr/share/rpcd/acl.d/ha-netmon.json
   ```

   Remember to back up the RPC login's configuration and user account details if they are custom.

### Secure transport

Prefer HTTPS with a certificate trusted by HA. For self-signed HTTPS certificates, you may enter the **SHA-256 leaf certificate fingerprint** in the config flow (certificate pinning, not disabled validation). Read it from a trusted channel, e.g. from the router itself:

```sh
openssl x509 -in /etc/uhttpd.crt -noout -fingerprint -sha256
```

Remove colons before pasting, or leave them (both formats are accepted). A changed certificate fingerprint causes a connection failure until re-pinned. HTTP is available for deliberately isolated management networks but **passes RPC credentials without TLS encryption**, so it is less secure. Never expose the RPC endpoint publicly.

## DHCP hostname resolution

Client names are obtained **primarily from the router configured as the primary node**. The read-only helper provides two identity-only sources in `ha_netmon.snapshot`:

- `dhcp_static_hosts_b64`: only `name`, `mac`, and `ip` fields from `config host` and `config domain` sections of `/etc/config/dhcp`; other UCI settings are never exported. Static host names bind by MAC, domain entries by IP.
- `dhcp_leases_b64`: current dnsmasq DHCPv4 lease entries from the default `/tmp/dhcp.leases` and additional lease files explicitly configured with dnsmasq's `leasefile` UCI option. For safety, only regular files resolving inside `/tmp` are read (on OpenWrt `/var` normally resolves to `/tmp`).

The primary node's static names take precedence over its dynamic lease hostnames, then odhcpd names. Other OpenWrt nodes are fallbacks and cannot overwrite a valid primary-router name with a missing one. The names are cached to keep devices recognizable while offline. A lease containing `*` has **no hostname**, so a reservation or another source is required. A device with a manually configured IP but no DHCP/DNS registration may still display its MAC.

These data are strictly **identity metadata**. Lease existence or a configured static reservation does not mark the device `home`; active association/FDB logic is unchanged.

To troubleshoot on the **router** (output contains local client details; do not post publicly without redaction):

```sh
ubus call ha_netmon snapshot | jsonfilter -e '@.dhcp_leases_b64' | base64 -d
ubus call ha_netmon snapshot | jsonfilter -e '@.dhcp_static_hosts_b64' | base64 -d
uci -q show dhcp | grep -E '(leasefile|\.name=|\.mac=|\.ip=)'
```

Once HA receives a hostname, it updates the Home Assistant **device name** unless the user has customized that name. Home Assistant does not automatically rename existing `device_tracker.*` entity IDs (which could be used in automations); you can rename a previously MAC-based entity ID in the entity settings. The tracker's `name_source` attribute identifies how a name was resolved.

## B. Install the Home Assistant custom integration

Copy `custom_components/openwrt_network_monitor/` into HA at `/config/custom_components/openwrt_network_monitor/`, restart HA, then add **OpenWrt Network Monitor** under **Settings → Devices & services → Add integration**. Enter the primary OpenWrt host, RPC login, HTTPS port (443 by default, configurable), optional SHA-256 certificate fingerprint, and connected zone (defaults `zone.home`). The setup flow validates `ha_netmon.snapshot` and `system.info` on the primary node.

## Manage network devices in Home Assistant

Go to **Settings → Devices & services → OpenWrt Network Monitor → Configure**.
The options menu lets you:

1. **General settings** — polling interval, departure grace, connected zone, VLAN overrides, and the main router’s name/uplink/verified edge ports.
2. **Add OpenWrt router, AP or switch** — choose its role, name, IP/host, upstream device, uplink interfaces, trusted client-facing ports, HTTPS/HTTP port, and TLS fingerprint. Enable hostapd collection on devices with Wi-Fi.
3. **Add unmanaged switch** — create a topology-only placeholder using its unique identifier, upstream node and the port on that upstream node. An unmanaged switch is not contacted by RPC.
4. **Edit existing device / Remove existing device** — select a node from a dropdown. A node with downstream children must be detached from them before removal.
5. **Save and finish** — apply all staged changes together and reload the integration once. Leaving the options flow without saving discards the staged changes.

For a managed OpenWrt node, **Use primary router credentials** is enabled by default. Turn it off to enter an independent RPC username and password in a separate password-masked form. When editing a device, leave the password blank to retain its current secret; turn on inheritance to stop using per-device credentials. Each node may independently configure its HTTPS/HTTP port, protocol and optional SHA-256 TLS certificate fingerprint. The form checks connectivity and the required **read-only** RPC methods before accepting a node. It does not execute any configuration changes on OpenWrt.

### Generic topology example (not a required network configuration)

For an arbitrary network, a possible sequence of UI actions is:

1. Add an AP named **Access Point A** with host `192.0.2.2`, role `ap`, upstream `192.0.2.1`, and uplink port `lan1`.
2. Add a second AP named **Access Point B** with host `192.0.2.3`, the same upstream, and the uplink port used on that AP.
3. Optionally add an unmanaged switch named **Switch A** with identifier `unmanaged-switch-a`, upstream `192.0.2.2`, and parent port `lan2`.
4. Select **Save and finish**. The primary router address in this example (`192.0.2.1`) is only illustrative; choose your actual node from the upstream dropdown.

You may register up to 32 additional devices. OpenWrt-managed roles are `router`, `ap`, `switch`, and `auto`. A virtual unmanaged switch has no RPC connection and exposes no internal port information. Host changes in the edit form automatically update references from downstream nodes. Users cannot accidentally remove a parent that is still referenced by another node.

**Security and access:** Every OpenWrt endpoint must run the fixed `ha_netmon.snapshot` helper and have the restricted ACL installed. The HA user requires only read methods; never give it the `file.exec`, `uci`, or administrative roles. Per-device credentials are stored by Home Assistant in its configuration storage, which must be protected along with backups. RPC credentials are never shown in the configuration editor once saved. HTTPS with a valid certificate or pinned SHA-256 fingerprint is strongly recommended.

**Port fields matter:** `uplink_ports` are ports towards a higher-level node; exclude their FDB observations to avoid double counting and misplacing clients. `edge_ports` are a narrower, explicitly trusted list of ports where *wired endpoint traffic* is observed (direct host or unmanaged switch). Only those can be used to reclassify a MAC previously detected over Wi-Fi. Don't add AP trunk ports to `edge_ports`.

### VLANs / logical networks and SSIDs

All L3 IPv4 networks are read from `network.interface.dump` on configured OpenWrt nodes. Wi-Fi BSSes and SSIDs are read from `hostapd.*`, with `network.wireless.status` providing an optional BSS→logical network mapping. DHCP/neighbor IP and subnet matching are used first; SSID alone is **never** assumed to equal a VLAN. Networks without SSIDs are handled equally.

For OpenWrt configurations with dot-VLAN interfaces (`br-lan.20`, etc.), VID can be derived automatically. If the L3 device is a separate bridge (`br-private`), its VID is not inherently knowable from its name. You may enter manual overrides like:

```text
alpha=101
beta=202
gamma=303
```

These are **examples only**; no specific VLAN IDs or SSIDs are hardcoded. If an IP/MAC is visible but no reliable VLAN association exists, the tracker is retained with an unknown VLAN rather than invented metadata.

### Presence and metrics

- Default polling is every **20 s**, confirmed absence grace **45 s**. The first full sample after restart repopulates known MACs; the first interface rate and CPU utilisation are unknown until another successful sample.
- When a phone leaves all directly queried APs, it becomes `not_home` after it is absent from successful hostapd polls for the grace period. Some APs themselves retain associations until a disconnect timeout. A failed poll returns *unavailable*, **not** a false departure.
- An Ethernet endpoint is present when its **dynamic bridge FDB** exists on a usable non-wireless port. An entry on a physically DOWN port is rejected. DHCP lease or a `STALE` neighbour alone never counts as presence.
- For all interfaces, `rx_mbps` and `tx_mbps` measure the change in cumulative byte counters over wall time. They do not represent Wi-Fi negotiated bit rate. Link speed is separately exposed when available from `/sys/class/net/<dev>/speed`.
- Avoid sum-of-all-interfaces bandwidth as a WAN estimate: bridge, VLAN, Wi-Fi BSS and underlying physical interfaces often count the *same traffic multiple times*.

## C. Example Network Flow Card dashboard

A ready-to-edit **Network Flow Card 4.x** sample and a full Lovelace dashboard are included in `examples/`.

- `examples/network-flow-card.yaml`: paste into a single Manual card.
- `examples/network-dashboard.yaml`: complete dashboard with a Hyperbolic topology, WAN RX/TX, CPU gauges, AP client counts, and a wired-by-network breakdown.
- `examples/template-helpers.yaml`: optional per-AP Wi-Fi client totals across multiple SSIDs/BSSes.
- `examples/README.md`: English setup guide, placeholder entity mapping and known limitations. The corresponding Polish files are in `examples/pl/`.

**You must replace example entity IDs** with the actual IDs in HA. The card does not have native `openwrt_network_monitor` auto-discovery; Hyperbolic grouping is the safe default. An unmanaged switch only exposes upstream link state, not its own health.

## D. Diagnostics and tests

Enable debug logging with:

```yaml
logger:
  logs:
    custom_components.openwrt_network_monitor: debug
```

Use **Developer tools → States** for `device_tracker.*`, `sensor.*_rx_bandwidth`, `sensor.*_cpu_usage`, and `binary_sensor.*_link`. The tracked data changes on each coordinator poll. For a VLAN mismatch check `ubus call network.interface dump`, `bridge -j fdb show`, and `ip -j -4 neigh show` (locally via SSH). For unknown Wi-Fi SSID check `ubus call hostapd.<BSS> get_status`.

Run offline parser/security tests on a workstation:

```sh
python3 -m compileall -q custom_components tests
python3 -m unittest discover -s tests -v
sh -n openwrt/ha_netmon
openwrt/ha_netmon call snapshot | python3 -m json.tool >/dev/null
```

Read-only ACL tests exercise the static ACL and helper; the on-router `tools/audit_acl.py` test is required to validate **actual effective permissions**, because extra roles from other OpenWrt packages can widen them.
