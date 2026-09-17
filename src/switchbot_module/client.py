"""SwitchBot OpenAPI v1.1 client.

Every request carries an HMAC-SHA256 signature over `token + t + nonce`
keyed by `secret`. The signature is base64-encoded — SwitchBot's own docs
have conflicting examples about upper-casing it; base64 without upper is
what the official Python sample and working community clients use.
"""

import base64
import hashlib
import hmac
import time
import uuid
from collections.abc import Mapping
from typing import Any

import httpx

BASE_URL = "https://api.switch-bot.com/v1.1"


class SwitchBotError(Exception):
    """Raised when the SwitchBot API returns a non-100 statusCode."""


class SwitchBotClient:
    def __init__(self, token: str, secret: str, timeout: float = 10.0) -> None:
        self._token = token
        self._secret = secret.encode()
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        t = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        payload = f"{self._token}{t}{nonce}".encode()
        sign = base64.b64encode(hmac.new(self._secret, payload, hashlib.sha256).digest()).decode()
        return {
            "Authorization": self._token,
            "sign": sign,
            "t": t,
            "nonce": nonce,
            "Content-Type": "application/json",
        }

    async def get_status(self, device_id: str) -> Mapping[str, Any]:
        r = await self._client.get(f"/devices/{device_id}/status", headers=self._headers())
        r.raise_for_status()
        body = r.json()
        if body.get("statusCode") != 100:
            raise SwitchBotError(body.get("message", "unknown error"))
        return body.get("body", {})

    async def send_command(
        self,
        device_id: str,
        command: str,
        parameter: str = "default",
        command_type: str = "command",
    ) -> Mapping[str, Any]:
        r = await self._client.post(
            f"/devices/{device_id}/commands",
            headers=self._headers(),
            json={"command": command, "parameter": parameter, "commandType": command_type},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("statusCode") != 100:
            raise SwitchBotError(body.get("message", "unknown error"))
        return body.get("body", {})

    async def close(self) -> None:
        await self._client.aclose()
