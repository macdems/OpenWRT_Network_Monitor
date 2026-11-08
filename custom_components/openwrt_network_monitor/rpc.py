"""Small authenticated JSON-RPC2 client for OpenWrt's /ubus endpoint."""
from __future__ import annotations

import asyncio
import logging
import re

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)
_ZERO_SESSION = "0" * 32


class RpcError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UbusClient:
    def __init__(self, hass, host: str, port: int, https: bool, username: str, password: str, fingerprint: str | None = None):
        scheme = "https" if https else "http"
        url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self.url = f"{scheme}://{url_host}:{port}/ubus"
        self.ssl = None
        if fingerprint:
            fingerprint = fingerprint.replace(":", "").strip()
            if not https or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint):
                raise ValueError("A SHA-256 TLS fingerprint requires HTTPS and 64 hex characters")
            self.ssl = aiohttp.Fingerprint(bytes.fromhex(fingerprint))
        self.host = host
        self.username = username
        self.password = password
        self.session = async_get_clientsession(hass)
        self.token: str | None = None
        self._missing_optional: set[tuple[str, str]] = set()

    async def _request(self, token: str, obj: str, method: str, args: dict | None = None):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "call",
                   "params": [token, obj, method, args or {}]}
        try:
            async with self.session.post(
                self.url, json=payload, ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=7)
            ) as resp:
                resp.raise_for_status()
                reply = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise RpcError(f"{self.host}: connection/JSON error: {exc}") from exc
        result = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(result, list) or not result or not isinstance(result[0], int):
            raise RpcError(f"{self.host}: malformed ubus JSON-RPC result")
        if result[0] != 0:
            raise RpcError(f"{self.host}: {obj}.{method}: ubus code {result[0]}", result[0])
        if len(result) < 2 or not isinstance(result[1], dict):
            raise RpcError(f"{self.host}: {obj}.{method}: no data object")
        return result[1]

    async def list_objects(self, pattern: str) -> dict:
        """Return matching ubus object names and their method definitions."""
        if self.token is None:
            await self.login()
        payload = {"jsonrpc": "2.0", "id": 2, "method": "list",
                   "params": [self.token, pattern, {}]}
        try:
            async with self.session.post(
                self.url, json=payload, ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=7)
            ) as resp:
                resp.raise_for_status()
                reply = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise RpcError(f"{self.host}: ubus list error: {exc}") from exc
        result = reply.get("result") if isinstance(reply, dict) else None
        if not isinstance(result, dict):
            raise RpcError(f"{self.host}: ubus list denied or invalid")
        return result

    async def login(self) -> None:
        result = await self._request(_ZERO_SESSION, "session", "login", {
            "username": self.username, "password": self.password, "timeout": 3600,
        })
        token = result.get("ubus_rpc_session")
        if not isinstance(token, str) or len(token) != 32:
            raise RpcError(f"{self.host}: login returned no session")
        self.token = token

    async def call(self, obj: str, method: str, args: dict | None = None) -> dict:
        if self.token is None:
            await self.login()
        try:
            return await self._request(self.token, obj, method, args)
        except RpcError as exc:
            # A 6 means missing/expired permission. Refresh once, not forever.
            if exc.status != 6:
                raise
            self.token = None
            await self.login()
            return await self._request(self.token, obj, method, args)

    async def optional_call(self, obj: str, method: str, args: dict | None = None) -> dict | None:
        key = obj, method
        if key in self._missing_optional:
            return None
        try:
            return await self.call(obj, method, args)
        except RpcError as exc:
            # Missing objects, methods and ACL denial will not change until
            # the router's RPC configuration changes. Avoid retrying each poll.
            if exc.status in (3, 4, 5, 6):
                self._missing_optional.add(key)
            _LOGGER.debug("Optional %s.%s on %s unavailable: %s", obj, method, self.host, exc)
            return None
