"""Physical/logical link status for all interfaces on all OpenWrt nodes."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .sensor import host_device


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    seen = set()

    @callback
    def add_new():
        if not coordinator.data:
            return
        new = []
        for host, sys in coordinator.data["systems"].items():
            for ifname in sys["interfaces"]:
                key = (host, ifname)
                if key not in seen:
                    new.append(InterfaceLink(coordinator, host, ifname))
                    seen.add(key)
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class InterfaceLink(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:ethernet"

    def __init__(self, coordinator, host, ifname):
        super().__init__(coordinator)
        self.host, self.ifname = host, ifname
        self._attr_name = f"{ifname} link"
        self._attr_unique_id = f"{DOMAIN}_{host}_{ifname}_link"
        self._attr_device_info = host_device(coordinator, host)

    @property
    def is_on(self):
        data = self.coordinator.data["systems"].get(self.host, {}).get("interfaces", {}).get(self.ifname)
        if data is None:
            return None
        state = data.get("operstate")
        if state in ("UP", "DOWN", "LOWERLAYERDOWN", "DORMANT"):
            return state == "UP"
        return data.get("up")  # administrative state for virtual bridges/VLANs

    @property
    def extra_state_attributes(self):
        return dict(self.coordinator.data["systems"].get(self.host, {}).get("interfaces", {}).get(self.ifname, {}))
