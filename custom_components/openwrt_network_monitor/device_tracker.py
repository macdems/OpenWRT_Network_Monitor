"""One ScannerEntity per client, regardless of Wi-Fi or wired VLAN."""
from __future__ import annotations

import logging

from homeassistant.components.device_tracker import ScannerEntity, CONF_ASSOCIATED_ZONE
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ZONE, DEFAULT_ZONE, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    added = set()

    @callback
    def add_new():
        if not coordinator.data:
            return
        new = []
        for mac in coordinator.data["clients"]:
            if mac not in added:
                new.append(OpenWrtNetworkClientTracker(coordinator, mac, entry))
                added.add(mac)
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class OpenWrtNetworkClientTracker(CoordinatorEntity, ScannerEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_entity_registry_enabled_default = True
    _attr_entity_category = None

    def __init__(self, coordinator, mac, entry):
        super().__init__(coordinator)
        self._mac = mac
        self._entry = entry
        self._attr_mac_address = mac
        hostname = coordinator.data["clients"][mac].get("hostname")
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, mac)},
            name=hostname or mac,
        )

    @property
    def hostname(self):
        return self.coordinator.data["clients"].get(self._mac, {}).get("hostname")

    @property
    def ip_address(self):
        return self.coordinator.data["clients"].get(self._mac, {}).get("ip")

    @property
    def is_connected(self):
        return bool(self.coordinator.data["clients"].get(self._mac, {}).get("connected"))

    @property
    def extra_state_attributes(self):
        rec = self.coordinator.data["clients"].get(self._mac, {})
        return {
            "connection_type": rec.get("connection_type"),
            "name_source": rec.get("name_source"),
            "network": rec.get("network"),
            "vlan": rec.get("vlan"),
            "subnet": rec.get("subnet"),
            "port": rec.get("port"),
            "bridge": rec.get("bridge"),
            "evidence": rec.get("evidence"),
            "ssid": rec.get("ssid"),
            "band": rec.get("band"),
            "signal": rec.get("signal"),  # dBm
            "ap_host": rec.get("ap_host"),
            "ap_name": rec.get("ap_name"),
            "attached_to": rec.get("attached_to"),
            "downstream_unmanaged_switch": rec.get("downstream_unmanaged_switch"),
            "edge_verified": rec.get("edge_verified"),
            "ap_mac": rec.get("bssid"),  # for network-flow-card / UniFi-like UI
            "bssid": rec.get("bssid"),
            "interface": rec.get("interface"),
            "node_key": rec.get("node_key"),
            "frequency": rec.get("frequency"),
            "last_seen": rec.get("last_seen"),
        }

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # ScannerEntity reads associated_zone from entity registry options;
        # apply the zone selected in config flow (default zone.home).
        zone = self._entry.options.get(CONF_ZONE, self._entry.data.get(CONF_ZONE, DEFAULT_ZONE))
        registry = er.async_get(self.hass)
        reg_entry = registry.async_get(self.entity_id)
        if reg_entry and reg_entry.options.get("device_tracker", {}).get(CONF_ASSOCIATED_ZONE) != zone:
            opts = dict(reg_entry.options)
            opts["device_tracker"] = {**opts.get("device_tracker", {}), CONF_ASSOCIATED_ZONE: zone}
            registry.async_update_entity(self.entity_id, options=opts)

    @callback
    def _handle_coordinator_update(self):
        hostname = self.hostname
        device = self.device_entry
        if hostname and device and not device.name_by_user and device.name != hostname:
            dr.async_get(self.hass).async_update_device(device.id, name=hostname)
        super()._handle_coordinator_update()
