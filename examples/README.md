# Network Flow Card — example dashboard

These generic examples target **Network Flow Card 4.x** and entities from **OpenWrt Network Monitor v0.4.0**. They are illustrative placeholders, **not an automatically discovered topology**. Adjust the node hierarchy, interface names, entity IDs, and clients to your own setup. Neither SNMP nor `card-mod` is required.

Polish versions of the same files live in [`pl/`](pl/README.md).

## Files

- `network-flow-card.yaml` — a **single card** to paste via Dashboard → Edit → Add Card → Manual.
- `network-dashboard.yaml` — a **complete Lovelace dashboard** including topology, WAN speeds, CPU gauges, AP client counts, historical throughput, and an Ethernet-by-network summary. Paste it into a dashboard's **raw configuration editor**, not into a single-card editor.
- `template-helpers.yaml` — **optional template sensors** to sum per-BSS Wi-Fi client counts across all SSIDs on each AP. Place the `template:` section in `configuration.yaml` or an HA package; merge it with any existing `template:` section instead of duplicating the key.

## Card installation

Install [Network Flow Card](https://github.com/benmac7/network-flow-card) through HACS (custom Dashboard repository if necessary). Confirm that the card's JS resource is loaded. Then paste `network-flow-card.yaml` as one card, or create/edit a dashboard using `network-dashboard.yaml`.

## Customize before use

1. **Entity IDs:** The files use placeholder IDs `sensor.router_*`, `sensor.ap_alpha_*`, and `sensor.ap_beta_*`. Home Assistant creates IDs from your configured node and interface names. In Developer Tools → States, search for `cpu_usage`, `load_1m`, `rx_bandwidth`, `wifi_clients`, `link`, and `ethernet_clients`. Choose **one WAN interface** for throughput; do not sum bridge, VLAN, and physical uplink counters (they overlap).
2. **Links and switches:** `binary_sensor.router_lan_port_link` is a placeholder for a router port used as an unmanaged switch uplink. It **does not** independently measure switch health. Remove the optional switch node if it does not describe a suitable uplink. A managed OpenWrt switch can expose its own sensors instead.
3. **Wi-Fi BSS counts:** Each BSS/SSID can have a separate sensor; populate `template-helpers.yaml` with all real BSS sensors per AP. Duplicate SSID names may cause HA to append suffixes to entity IDs. The two SSIDs in this example are illustrative and should be renamed or removed as appropriate.
4. **Clients:** Replace `device_tracker.phone`, `device_tracker.workstation`, and `device_tracker.printer` with actual trackers. This card does **not** natively discover the custom `openwrt_network_monitor` integration; add clients manually or select them in its editor.
5. **Ethernet clients:** Replace `sensor.ethernet_clients_all` if the entity ID differs. Its `by_network` attribute is a mapping of logical network names to connected counts; the dashboard does not need static VLAN IDs.

## Hyperbolic vs. Flat and automatic AP attachment

The sample defaults to `hyperbolic_client_layout: grouped` because automatic parent matching is not guaranteed. You may try `attached` after confirming that your Network Flow Card version resolves the entities and their parent nodes correctly. The integration exposes `ap_mac` (BSSID), `ssid`, `vlan`, `ap_name`, `attached_to`, and `connection_type`, but the card does not advertise native support for `openwrt_network_monitor`; **attributes alone do not guarantee correct mapping**. If devices appear under Unknown, use `grouped` or add explicit support in the card.

To switch from the hyperbolic graph to a standard topology view, change `view_mode: hyperbolic` to `view_mode: flat`. Group clients using Clients → Group By → VLAN or SSID in the card editor, where available.

## Metrics

- `*_rx_bandwidth` and `*_tx_bandwidth` measure live **Mbit/s** traffic, not the interface's negotiated speed; where available, `*_negotiated_link_speed` provides the negotiated Ethernet rate.
- `*_cpu_usage` is CPU utilization in percent. `*_load_1m` is **load average**, not a percentage; the examples use it as a convenient node-health proxy because the sensor becomes unavailable when telemetry fails.
- `*_wifi_clients` counts the clients of a **single BSS**. Per-AP totals in this directory are optional template helpers.
- Historical CPU, memory and throughput graphs use HA's recorder; no SNMP setup is necessary.

References: [Network Flow Card README](https://github.com/benmac7/network-flow-card/blob/main/README.md) and [HOWTO](https://github.com/benmac7/network-flow-card/blob/main/HOWTO.md). The card may evolve separately from this integration.
