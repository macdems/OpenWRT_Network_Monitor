"""UI configuration of an arbitrary OpenWrt inventory."""
from __future__ import annotations

import asyncio
import logging
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN, CONF_HOST, CONF_PORT, CONF_HTTPS, CONF_NODES, CONF_ZONE,
    CONF_INTERVAL, CONF_GRACE, CONF_WIRED_ENABLED, CONF_VLAN_MAP, CONF_CENTRAL,
    DEFAULT_PORT, DEFAULT_ZONE, DEFAULT_INTERVAL, DEFAULT_GRACE,
)
from .rpc import UbusClient, RpcError
from .telemetry import parse_nodes_json
from .wired import parse_vlan_mapping

_LOGGER = logging.getLogger(__name__)


def connection_schema():
    return vol.Schema({
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME, default="hass"): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        vol.Required(CONF_HTTPS, default=True): bool,
        vol.Optional("tls_fingerprint", default=""): str,
        vol.Required(CONF_ZONE, default=DEFAULT_ZONE): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="zone")
        ),
    })


class OpenWrtNetworkMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            try:
                parse_nodes_json("[]", {
                    "host": user_input[CONF_HOST], "port": user_input[CONF_PORT],
                    "https": user_input[CONF_HTTPS],
                    "username": user_input[CONF_USERNAME], "password": user_input[CONF_PASSWORD],
                })
                rpc = UbusClient(self.hass, user_input[CONF_HOST], user_input[CONF_PORT],
                                 user_input[CONF_HTTPS], user_input[CONF_USERNAME], user_input[CONF_PASSWORD],
                                 user_input.get("tls_fingerprint"))
                reply, info = await asyncio.gather(
                    rpc.call("ha_netmon", "snapshot"), rpc.call("system", "info")
                )
                if not isinstance(reply.get("fdb"), list) or not isinstance(info, dict):
                    raise ValueError("Incomplete OpenWrt read-only helper response")
            except (RpcError, ValueError, TypeError, asyncio.TimeoutError) as exc:
                _LOGGER.warning("Cannot validate read-only ubus connection: %s", exc)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_HOST].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"OpenWrt {user_input[CONF_HOST]}", data=user_input
                )
        return self.async_show_form(step_id="user", data_schema=connection_schema(), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OpenWrtNetworkMonitorOptionsFlow()


class OpenWrtNetworkMonitorOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        entry = self.config_entry
        opts = entry.options
        if user_input is not None:
            primary = {
                "host": entry.data[CONF_HOST], "name": user_input.get("primary_name") or entry.data[CONF_HOST],
                "port": entry.data[CONF_PORT], "https": entry.data[CONF_HTTPS],
                "username": entry.data[CONF_USERNAME], "password": entry.data[CONF_PASSWORD],
                "tls_fingerprint": entry.data.get("tls_fingerprint", ""),
                "uplink_ports": user_input.get("primary_uplink_ports", "").replace(",", " ").split(),
                "edge_ports": user_input.get("primary_edge_ports", "").replace(",", " ").split(),
            }
            try:
                parse_nodes_json(user_input.get(CONF_NODES, "[]"), primary)
                parse_vlan_mapping(user_input.get(CONF_VLAN_MAP, ""))
            except ValueError as exc:
                _LOGGER.warning("Invalid network inventory settings: %s", exc)
                errors["base"] = "invalid_configuration"
            else:
                return self.async_create_entry(title="", data=user_input)
        schema = vol.Schema({
            vol.Required(CONF_ZONE, default=opts.get(CONF_ZONE, entry.data.get(CONF_ZONE, DEFAULT_ZONE))):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="zone")),
            vol.Required(CONF_INTERVAL, default=opts.get(CONF_INTERVAL, DEFAULT_INTERVAL)):
                vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
            vol.Required(CONF_GRACE, default=opts.get(CONF_GRACE, DEFAULT_GRACE)):
                vol.All(vol.Coerce(int), vol.Range(min=0, max=1800)),
            vol.Required(CONF_WIRED_ENABLED, default=opts.get(CONF_WIRED_ENABLED, True)): bool,
            vol.Required(CONF_CENTRAL, default=opts.get(CONF_CENTRAL, False)): bool,
            vol.Optional("primary_name", default=opts.get("primary_name", entry.data[CONF_HOST])): str,
            vol.Optional("primary_uplink_ports", default=opts.get("primary_uplink_ports", "")): str,
            vol.Optional("primary_edge_ports", default=opts.get("primary_edge_ports", "")): str,
            vol.Optional(CONF_NODES, default=opts.get(CONF_NODES, entry.data.get(CONF_NODES, "[]"))):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(CONF_VLAN_MAP, default=opts.get(CONF_VLAN_MAP, "")):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
