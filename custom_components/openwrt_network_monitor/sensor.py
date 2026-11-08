"""Dynamic per-host system, interface, Wi-Fi and Ethernet sensors."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

STATS = {
    "cpu_percent": ("CPU usage", "%", "mdi:cpu-64-bit"),
    "memory_percent": ("Memory usage", "%", "mdi:memory"),
    "memory_available": ("Available memory", "B", "mdi:memory"),
    "load_1": ("Load 1m", None, "mdi:speedometer"),
    "load_5": ("Load 5m", None, "mdi:speedometer"),
    "load_15": ("Load 15m", None, "mdi:speedometer"),
    "uptime": ("Uptime", "s", "mdi:clock-outline"),
}
IF_METRICS = {
    "rx_mbps": ("RX bandwidth", "Mbit/s"),
    "tx_mbps": ("TX bandwidth", "Mbit/s"),
    "speed_mbps": ("Negotiated link speed", "Mbit/s"),
    "rx_bytes": ("RX bytes", "B"),
    "tx_bytes": ("TX bytes", "B"),
    "rx_errors": ("RX errors", None),
    "tx_errors": ("TX errors", None),
    "rx_dropped": ("RX dropped", None),
    "tx_dropped": ("TX dropped", None),
}


def host_device(coordinator, host):
    config = next(n for n in coordinator.nodes if n["host"] == host)
    upstream = config.get("upstream")
    return DeviceInfo(
        identifiers={(DOMAIN, f"node_{host}")},
        name=config.get("name") or host,
        manufacturer="OpenWrt",
        via_device=(DOMAIN, f"node_{upstream}") if upstream else None,
    )


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    seen = set()

    @callback
    def add_new():
        if not coordinator.data:
            return
        new = []
        data = coordinator.data
        for host, system in data["systems"].items():
            for metric in STATS:
                key = ("system", host, metric)
                if key not in seen:
                    seen.add(key)
                    new.append(SystemSensor(coordinator, host, metric))
            for name in system["interfaces"]:
                for metric in IF_METRICS:
                    key = ("interface", host, name, metric)
                    if key not in seen:
                        seen.add(key)
                        new.append(InterfaceSensor(coordinator, host, name, metric))
        for node_key, node in data["nodes"].items():
            key = ("bss", node_key)
            if key not in seen:
                seen.add(key)
                new.append(BssClientsSensor(coordinator, node_key))
        for network in set(data["wired_counts"]):
            key = ("wired", network)
            if key not in seen:
                seen.add(key)
                new.append(WiredSensor(coordinator, network))
        if ("wired-total",) not in seen:
            seen.add(("wired-total",))
            new.append(WiredSensor(coordinator, None))
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class SystemSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, host, metric):
        super().__init__(coordinator)
        self.host, self.metric = host, metric
        self._attr_unique_id = f"{DOMAIN}_{host}_sys_{metric}"
        self._attr_name, self._attr_native_unit_of_measurement, self._attr_icon = STATS[metric]
        self._attr_device_info = host_device(coordinator, host)
        if metric == "uptime":
            self._attr_state_class = None

    @property
    def native_value(self):
        return self.coordinator.data["systems"].get(self.host, {}).get("stats", {}).get(self.metric)

    @property
    def extra_state_attributes(self):
        if self.metric == "uptime":
            return self.coordinator.data["systems"].get(self.host, {}).get("board", {})
        return {}


class InterfaceSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, host, ifname, metric):
        super().__init__(coordinator)
        self.host, self.ifname, self.metric = host, ifname, metric
        label, unit = IF_METRICS[metric]
        self._attr_name = f"{ifname} {label}"
        self._attr_unique_id = f"{DOMAIN}_{host}_{ifname}_{metric}"
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = "mdi:network"
        self._attr_device_info = host_device(coordinator, host)
        if metric.endswith("_bytes") or metric.endswith("_errors") or metric.endswith("_dropped"):
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            self._attr_entity_registry_enabled_default = False
        elif metric == "speed_mbps":
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self):
        return self.coordinator.data["systems"].get(self.host, {}).get("interfaces", {}).get(self.ifname, {}).get(self.metric)

    @property
    def extra_state_attributes(self):
        ifdata = self.coordinator.data["systems"].get(self.host, {}).get("interfaces", {}).get(self.ifname, {})
        return {k: ifdata.get(k) for k in (
            "mac", "mtu", "kind", "operstate", "up", "vlan", "link_type",
            "speed_mbps", "rx_bytes", "tx_bytes", "rx_packets", "tx_packets",
            "rx_errors", "tx_errors", "rx_dropped", "tx_dropped",
        ) if k != self.metric}


class BssClientsSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "clients"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator, key):
        super().__init__(coordinator)
        self.key = key
        node = coordinator.data["nodes"][key]
        self._attr_name = f"{node.get('ssid') or key} Wi-Fi clients"
        self._attr_unique_id = f"{DOMAIN}_{key}_clients"
        self._attr_device_info = host_device(coordinator, node["ap_host"])

    @property
    def native_value(self):
        return self.coordinator.data["counts"].get(self.key, 0)

    @property
    def extra_state_attributes(self):
        return dict(self.coordinator.data["nodes"].get(self.key, {}))


class WiredSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "clients"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ethernet"

    def __init__(self, coordinator, network):
        super().__init__(coordinator)
        self.network = network
        suffix = network or "all"
        self._attr_unique_id = f"{DOMAIN}_{coordinator.entry.entry_id}_wired_{suffix}"
        self._attr_name = f"Ethernet clients {suffix}"

    @property
    def native_value(self):
        if self.network is None:
            return len(self.coordinator.data["wired_present"])
        return self.coordinator.data["wired_counts"].get(self.network, 0)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if self.network is None:
            return {"by_network": data["wired_counts"]}
        clients = data["wired_present"].values()
        return {"clients": [
            {k: rec.get(k) for k in ("mac", "hostname", "ip", "ap_host", "port", "vlan")}
            for rec in clients if rec.get("network") == self.network
        ]}
