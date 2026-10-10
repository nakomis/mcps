"""Lambda entry point for alexa-announce.

The alexapy session is cached between warm invocations. Its aiohttp client is
bound to the event loop that created it, so every invocation runs on one
module-level loop. asyncio.run() per call would close that loop and break the
cached session.
"""

import asyncio
import json
import logging
import os
import time

from alexa_announce import core
from alexa_announce.session import AlexaSession, AmazonAuthError, AmazonError, SessionExpired

log = logging.getLogger("alexa_announce")
log.setLevel(logging.INFO)
logging.getLogger("alexapy").setLevel(logging.WARNING)

SECRET_ID = os.environ.get("SECRET_ID", "alexa-announce/session")
WORKDIR = "/tmp/alexapy"


class Provider:
    def __init__(self, load_state, opener):
        self._load_state = load_state
        self._open = opener
        self._session = None

    async def get(self):
        if self._session is None:
            self._session = await self._open(self._load_state())
        return self._session

    async def reset(self):
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception:  # closing a dead session must not mask the real error
                log.warning("closing stale session failed", exc_info=True)


def _load_state_from_secret() -> dict:
    import boto3

    raw = boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)["SecretString"]
    try:
        state = json.loads(raw)
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


_provider = Provider(_load_state_from_secret, lambda state: AlexaSession.open(state, WORKDIR))
_loop = asyncio.new_event_loop()


async def _send(provider: Provider, action: str, text: str, names: list[str]) -> list[str]:
    session = await provider.get()
    devices = core.resolve_devices(names, await session.devices())
    build = core.announce_node if action == "announce" else core.speak_node
    await session.run(build(text, devices, session.customer_id))
    return [d["accountName"] for d in devices]


async def handle(event: dict, provider: Provider) -> dict:
    started = time.monotonic()
    try:
        action, text, names = core.validate(event)
        try:
            sent = await _send(provider, action, text, names)
        except AmazonAuthError:
            await provider.reset()
            sent = await _send(provider, action, text, names)
    except core.RequestError as e:
        result = {"ok": False, "error": e.error, "detail": e.detail}
    except (SessionExpired, AmazonAuthError) as e:
        await provider.reset()
        result = {"ok": False, "error": "session_expired", "detail": str(e)}
    except AmazonError as e:
        result = {"ok": False, "error": "amazon_error",
                  "detail": {"status": e.status, "body": e.body[:300]}}
    else:
        result = {"ok": True, "action": action, "devices": sent}

    # Never log the text: it can be anything Claude chose to say out loud.
    log.info(json.dumps({
        "action": event.get("action"),
        "devices": event.get("devices"),
        "ok": result["ok"],
        "error": result.get("error"),
        "ms": int((time.monotonic() - started) * 1000),
    }))
    return result


def handler(event, context):
    return _loop.run_until_complete(handle(event, _provider))
