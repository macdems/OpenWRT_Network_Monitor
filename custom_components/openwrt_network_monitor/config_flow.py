"""Config flow and multi-step UI for arbitrary OpenWrt infrastructure.

The administrator can stage multiple topology changes, then commit once.
The UI never prints or logs passwords, and a new RPC endpoint must pass a
read-only connectivity probe before it is committed.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

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
from .inventory import (
    apply_node, decode_inventory, encode_inventory, ports_as_text,
    ports_from_text, remove_node, validate_inventory,
)
from .rpc import RpcError, UbusClient
from .wired import parse_vlan_mapping

_LOGGER = logging.getLogger(__name__)


async def _check_rpc(hass, node: dict) -> None:
    """Verify the fixed read-only API required by the actual collector."""
    rpc = UbusClient(
        hass, node["host"], node["port"], node["https"],
        node["username"], node["password"], node.get("tls_fingerprint"),
    )
    snapshot, info, board, networks, bsses = await asyncio.gather(
        rpc.call("ha_netmon", "snapshot"),
        rpc.call("system", "info"),
        rpc.call("system", "board"),
        rpc.call("network.interface", "dump"),
        rpc.list_objects("hostapd.*"),
    )
    if (
        not isinstance(snapshot.get("fdb"), list)
        or not isinstance(snapshot.get("links"), list)
        or not isinstance(info, dict)
        or not isinstance(board, dict)
        or not isinstance(networks, dict)
        or not isinstance(bsses, dict)
    ):
        raise ValueError("Incomplete read-only monitoring API")


def connection_schema():
    return vol.Schema({
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME, default="hass"): str,
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
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
                primary = {
                    "host": user_input[CONF_HOST], "port": user_input[CONF_PORT],
                    "https": user_input[CONF_HTTPS],
                    "username": user_input[CONF_USERNAME], "password": user_input[CONF_PASSWORD],
                }
                validate_inventory([], primary)
                await _check_rpc(self.hass, {**primary, "tls_fingerprint": user_input.get("tls_fingerprint")})
            except (RpcError, ValueError, TypeError, asyncio.TimeoutError, OSError) as exc:
                _LOGGER.warning("Read-only OpenWrt connection validation failed: %s", type(exc).__name__)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_HOST].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"OpenWrt {user_input[CONF_HOST]}", data=user_input)
        return self.async_show_form(step_id="user", data_schema=connection_schema(), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OpenWrtNetworkMonitorOptionsFlow()


class OpenWrtNetworkMonitorOptionsFlow(OptionsFlow):
    """Edit many nodes through forms and one final atomic options save."""

    def _prepare(self) -> None:
        if hasattr(self, "_draft"):
            return
        self._draft = dict(self.config_entry.options)
        self._devices = decode_inventory(
            self._draft.get(CONF_NODES, self.config_entry.data.get(CONF_NODES, "[]"))
        )
        self._editing_host: str | None = None
        self._pending: dict | None = None
        self._original: dict | None = None

    def _primary(self) -> dict:
        data = self.config_entry.data
        return {
            "host": data[CONF_HOST],
            "name": self._draft.get("primary_name") or data[CONF_HOST],
            "port": data[CONF_PORT],
            "https": data[CONF_HTTPS],
            "username": data[CONF_USERNAME],
            "password": data[CONF_PASSWORD],
            "tls_fingerprint": data.get("tls_fingerprint", ""),
            "uplink_ports": ports_from_text(self._draft.get("primary_uplink_ports", "")),
            "edge_ports": ports_from_text(self._draft.get("primary_edge_ports", "")),
        }

    def _options_list(self) -> list[str]:
        items = ["settings", "add_node", "add_virtual"]
        if self._devices:
            items.extend(("edit_select", "remove_select"))
        return [*items, "finish"]

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        self._prepare()
        # A menu prevents saving half-edited credentials or topology.
        summary = ", ".join(str(n.get("name") or n["host"]) for n in self._devices)
        return self.async_show_menu(
            step_id="init", menu_options=self._options_list(),
            description_placeholders={"count": str(len(self._devices)),
                                      "devices": summary or "—"},
        )

    async def async_step_settings(self, user_input=None) -> ConfigFlowResult:
        self._prepare()
        errors = {}
        if user_input is not None:
            proposed = {**self._draft, **user_input}
            try:
                parse_vlan_mapping(proposed.get(CONF_VLAN_MAP, ""))
                before = self._draft
                self._draft = proposed
                try:
                    validate_inventory(self._devices, self._primary())
                except ValueError:
                    self._draft = before
                    raise
            except ValueError:
                errors["base"] = "invalid_configuration"
            else:
                return await self.async_step_init()
        current = self._draft
        data = self.config_entry.data
        schema = vol.Schema({
            vol.Required(CONF_ZONE, default=current.get(CONF_ZONE, data.get(CONF_ZONE, DEFAULT_ZONE))):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="zone")),
            vol.Required(CONF_INTERVAL, default=current.get(CONF_INTERVAL, DEFAULT_INTERVAL)):
                vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
            vol.Required(CONF_GRACE, default=current.get(CONF_GRACE, DEFAULT_GRACE)):
                vol.All(vol.Coerce(int), vol.Range(min=0, max=1800)),
            vol.Required(CONF_WIRED_ENABLED, default=current.get(CONF_WIRED_ENABLED, True)): bool,
            vol.Required(CONF_CENTRAL, default=current.get(CONF_CENTRAL, False)): bool,
            vol.Optional("primary_name", default=current.get("primary_name", data[CONF_HOST])): str,
            vol.Optional("primary_uplink_ports", default=current.get("primary_uplink_ports", "")): str,
            vol.Optional("primary_edge_ports", default=current.get("primary_edge_ports", "")): str,
            vol.Optional(CONF_VLAN_MAP, default=current.get(CONF_VLAN_MAP, "")):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="settings", data_schema=schema, errors=errors)

    def _upstream_selector(self, omit_host: str | None = None, allow_none: bool = True):
        choices = ([{"value": "__none__", "label": "No upstream / top-level"}]
                   if allow_none else [])
        choices.append({"value": self.config_entry.data[CONF_HOST], "label": self._primary()["name"]})
        choices.extend({"value": n["host"], "label": str(n.get("name") or n["host"])}
                       for n in self._devices if n["host"] != omit_host)
        return selector.SelectSelector(
            selector.SelectSelectorConfig(options=choices, mode=selector.SelectSelectorMode.DROPDOWN)
        )

    def _node_schema(self, row: dict):
        primary = self._primary()
        inherited = not ("username" in row or "password" in row)
        return vol.Schema({
            vol.Required("host", default=row.get("host", "")): str,
            vol.Optional("name", default=row.get("name", "")): str,
            vol.Required("role", default=row.get("role", "ap")):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=["ap", "router", "switch", "auto"],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
            vol.Required("upstream", default=row.get("upstream") or "__none__"): self._upstream_selector(self._editing_host),
            vol.Optional("uplink_ports", default=ports_as_text(row.get("uplink_ports"))): str,
            vol.Optional("edge_ports", default=ports_as_text(row.get("edge_ports"))): str,
            vol.Required("port", default=row.get("port", primary["port"])):
                vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required("https", default=row.get("https", primary["https"])): bool,
            vol.Optional("tls_fingerprint", default=row.get("tls_fingerprint", "")): str,
            vol.Required("use_primary_credentials", default=inherited): bool,
            vol.Required("wifi", default=row.get("wifi", row.get("role") != "switch")): bool,
        })

    def _current(self) -> dict:
        if self._editing_host is None:
            return {}
        return next((n.copy() for n in self._devices if n["host"] == self._editing_host), {})

    async def _form_node(self, step: str, user_input=None) -> ConfigFlowResult:
        row = self._current()
        errors = {}
        if user_input is not None:
            proposed = {**row, **user_input}
            proposed["host"] = proposed["host"].strip()
            proposed["name"] = (proposed.get("name") or proposed["host"]).strip()
            proposed["uplink_ports"] = ports_from_text(proposed.get("uplink_ports", ""))
            proposed["edge_ports"] = ports_from_text(proposed.get("edge_ports", ""))
            if proposed.get("upstream") in ("", "__none__"):
                proposed.pop("upstream", None)
            # Empty fingerprint means normal TLS CA validation for this node.
            # Do not silently inherit a different device's certificate pin.
            proposed["tls_fingerprint"] = proposed.get("tls_fingerprint", "")
            inherit = proposed.pop("use_primary_credentials", True)
            if inherit:
                proposed.pop("username", None)
                proposed.pop("password", None)
            else:
                # Do not expose stored passwords when editing credentials.
                proposed["username"] = row.get("username") or self._primary()["username"]
            self._pending = proposed
            if not inherit:
                return await self.async_step_credentials()
            return await self._commit_node(step)
        return self.async_show_form(
            step_id=step, data_schema=self._node_schema(row), errors=errors
        )

    async def async_step_add_node(self, user_input=None) -> ConfigFlowResult:
        self._editing_host = None
        return await self._form_node("add_node", user_input)

    async def async_step_edit_node(self, user_input=None) -> ConfigFlowResult:
        return await self._form_node("edit_node", user_input)

    async def async_step_credentials(self, user_input=None) -> ConfigFlowResult:
        """Separate password screen: never return current password to frontend."""
        if self._pending is None:
            return await self.async_step_init()
        errors = {}
        current = self._current()
        if user_input is not None:
            password = user_input.get("password") or current.get("password")
            if not password:
                errors["base"] = "password_required"
            else:
                self._pending["username"] = user_input["username"].strip()
                self._pending["password"] = password
                return await self._commit_node("credentials")
        schema = vol.Schema({
            vol.Required("username", default=current.get("username") or self._primary()["username"]): str,
            vol.Optional("password"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        })
        return self.async_show_form(step_id="credentials", data_schema=schema, errors=errors)

    async def _commit_node(self, step: str) -> ConfigFlowResult:
        assert self._pending is not None
        proposed = self._pending
        errors = {}
        try:
            rows = apply_node(self._devices, proposed, self._editing_host)
            validate_inventory(rows, self._primary())
            # Only actual managed OpenWrt endpoints are contacted.
            effective = {**self._primary(), **proposed}
            if proposed.get("role") != "unmanaged_switch":
                await _check_rpc(self.hass, effective)
        except (ValueError, RpcError, TypeError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.warning("Cannot validate OpenWrt node: %s", type(exc).__name__)
            errors["base"] = "invalid_configuration" if isinstance(exc, ValueError) else "cannot_connect"
        else:
            self._devices = rows
            self._pending = None
            self._editing_host = None
            return await self.async_step_init()
        # For an error, preserve user input via the form's suggested defaults.
        if step == "credentials":
            # Keep staged node details; user can retry the credentials step.
            schema = vol.Schema({
                vol.Required("username", default=proposed.get("username", self._primary()["username"])): str,
                vol.Optional("password"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            })
            return self.async_show_form(step_id=step, data_schema=schema, errors=errors)
        row = {**self._current(), **proposed}
        return self.async_show_form(step_id=step, data_schema=self._node_schema(row), errors=errors)

    async def async_step_add_virtual(self, user_input=None) -> ConfigFlowResult:
        self._editing_host = None
        return await self._form_virtual("add_virtual", user_input)

    async def async_step_edit_virtual(self, user_input=None) -> ConfigFlowResult:
        return await self._form_virtual("edit_virtual", user_input)

    async def _form_virtual(self, step: str, user_input=None) -> ConfigFlowResult:
        row = self._current()
        errors = {}
        if user_input is not None:
            proposed = {**row, "role": "unmanaged_switch"}
            proposed.update({"host": user_input["host"].strip(),
                             "name": (user_input.get("name") or user_input["host"]).strip(),
                             "upstream": user_input["upstream"],
                             "parent_port": user_input["parent_port"].strip()})
            try:
                rows = apply_node(self._devices, proposed, self._editing_host)
                validate_inventory(rows, self._primary())
            except (ValueError, TypeError):
                errors["base"] = "invalid_configuration"
            else:
                self._devices = rows
                self._editing_host = None
                return await self.async_step_init()
        schema = vol.Schema({
            vol.Required("host", default=row.get("host", "")): str,
            vol.Optional("name", default=row.get("name", "")): str,
            vol.Required("upstream", default=row.get("upstream") or self.config_entry.data[CONF_HOST]):
                self._upstream_selector(self._editing_host, allow_none=False),
            vol.Required("parent_port", default=row.get("parent_port", "")): str,
        })
        return self.async_show_form(step_id=step, data_schema=schema, errors=errors)

    def _select_schema(self):
        options = [{"value": node["host"], "label": str(node.get("name") or node["host"])}
                   for node in self._devices]
        return vol.Schema({
            vol.Required("device"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
            )
        })

    async def async_step_edit_select(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            host = user_input["device"]
            if any(n["host"] == host for n in self._devices):
                self._editing_host = host
                if self._current().get("role") == "unmanaged_switch":
                    return await self.async_step_edit_virtual()
                return await self.async_step_edit_node()
            errors["base"] = "invalid_configuration"
        return self.async_show_form(step_id="edit_select", data_schema=self._select_schema(), errors=errors)

    async def async_step_remove_select(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            host = user_input["device"]
            try:
                remove_node(self._devices, host)
            except ValueError as exc:
                errors["base"] = "has_children" if str(exc) == "has_children" else "invalid_configuration"
            else:
                self._editing_host = host
                return await self.async_step_remove_confirm()
        return self.async_show_form(step_id="remove_select", data_schema=self._select_schema(), errors=errors)

    async def async_step_remove_confirm(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input.get("confirm"):
                self._devices = remove_node(self._devices, self._editing_host)
                self._editing_host = None
            return await self.async_step_init()
        return self.async_show_form(
            step_id="remove_confirm", data_schema=vol.Schema({
                vol.Required("confirm", default=False): bool,
            }),
            description_placeholders={"device": str(self._current().get("name") or self._editing_host)},
        )

    async def async_step_finish(self, user_input=None) -> ConfigFlowResult:
        self._prepare()
        try:
            validate_inventory(self._devices, self._primary())
            parse_vlan_mapping(self._draft.get(CONF_VLAN_MAP, ""))
        except ValueError:
            return self.async_show_form(step_id="finish", data_schema=vol.Schema({}),
                                        errors={"base": "invalid_configuration"})
        return self.async_create_entry(title="", data={
            **self._draft, CONF_NODES: encode_inventory(self._devices)
        })
