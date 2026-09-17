"""Client tests. HMAC math is the highest-risk piece — one bad sign call
locks every model out of the API, so we verify the signature is
reproducible from the emitted headers."""

import base64
import hashlib
import hmac

import httpx
import pytest
import respx

from switchbot_module.client import BASE_URL, SwitchBotClient, SwitchBotError


def test_headers_include_required_fields():
    client = SwitchBotClient("mytoken", "mysecret")
    h = client._headers()
    assert h["Authorization"] == "mytoken"
    assert h["Content-Type"] == "application/json"
    assert h["t"].isdigit() and len(h["t"]) == 13  # ms timestamp
    assert len(h["nonce"]) == 36  # uuid4


def test_sign_reproducible_from_headers():
    client = SwitchBotClient("mytoken", "mysecret")
    h = client._headers()
    expected = base64.b64encode(
        hmac.new(
            b"mysecret",
            f"mytoken{h['t']}{h['nonce']}".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert h["sign"] == expected


@pytest.mark.asyncio
async def test_get_status_returns_body():
    client = SwitchBotClient("t", "s")
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/devices/ABC/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "statusCode": 100,
                    "body": {"temperature": 22.5, "humidity": 50},
                    "message": "success",
                },
            )
        )
        result = await client.get_status("ABC")
    assert result == {"temperature": 22.5, "humidity": 50}
    await client.close()


@pytest.mark.asyncio
async def test_send_command_error_raises():
    client = SwitchBotClient("t", "s")
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/devices/ABC/commands").mock(
            return_value=httpx.Response(
                200,
                json={"statusCode": 190, "body": {}, "message": "device offline"},
            )
        )
        with pytest.raises(SwitchBotError, match="device offline"):
            await client.send_command("ABC", "turnOn")
    await client.close()


@pytest.mark.asyncio
async def test_send_command_posts_expected_body():
    client = SwitchBotClient("t", "s")
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/devices/ABC/commands").mock(
            return_value=httpx.Response(
                200, json={"statusCode": 100, "body": {}, "message": "success"}
            )
        )
        await client.send_command("ABC", "setPosition", parameter="0,ff,50")
    import json as _json
    sent = _json.loads(route.calls.last.request.content)
    assert sent == {"command": "setPosition", "parameter": "0,ff,50", "commandType": "command"}
    await client.close()
