"""The alexapy side: log in from a stored refresh token, list devices, run a sequence.

alexapy is imported lazily so the pure logic and handler can be tested without it.
"""

import json
import os

from alexa_announce.core import behaviour_body


class SessionExpired(Exception):
    """The stored registration no longer works. Someone must run alexa-mcp-login."""


class AmazonAuthError(Exception):
    """Amazon refused a warm session (401/403). Worth one fresh login."""


class AmazonError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Amazon returned HTTP {status}")
        self.status = status
        self.body = body


class AlexaSession:
    def __init__(self, login):
        self._login = login
        self._devices = None

    @classmethod
    async def open(cls, state: dict, workdir: str) -> "AlexaSession":
        if not state.get("refresh_token") or not state.get("email"):
            raise SessionExpired("the session secret has no refresh token; run alexa-mcp-login")
        from alexapy import AlexaLogin

        os.makedirs(os.path.join(workdir, ".storage"), exist_ok=True)
        login = AlexaLogin(
            state.get("url", "amazon.co.uk"),
            state["email"],
            "",  # no password: only the refresh token is ever available here
            lambda p: os.path.join(workdir, p),
            oauth={k: state[k] for k in ("refresh_token", "mac_dms") if state.get(k)},
            uuid=state.get("uuid"),
        )
        await login.login()
        if not (login.status or {}).get("login_successful"):
            await login.close()
            raise SessionExpired("Amazon rejected the stored refresh token")
        return cls(login)

    @property
    def customer_id(self) -> str:
        return self._login.customer_id

    async def devices(self) -> list[dict]:
        if self._devices is None:
            from alexapy import AlexaAPI

            devices = await AlexaAPI.get_devices(self._login)
            if not devices:
                # alexapy swallows errors and returns None; an account always has
                # devices, so treat an empty list as a dead session.
                raise AmazonAuthError("device list came back empty")
            self._devices = devices
        return self._devices

    async def run(self, start_node: dict) -> None:
        login = self._login
        csrf = login._get_cookies_from_session().get("csrf")
        headers = {
            **login._headers,
            "csrf": csrf.value if csrf else "",
            "Referer": f"https://alexa.{login.url}/spa/index.html",
            "Content-Type": "application/json; charset=UTF-8",
        }
        resp = await login.session.post(
            f"https://alexa.{login.url}/api/behaviors/preview",
            data=behaviour_body(start_node),
            headers=headers,
        )
        if resp.status in (401, 403):
            raise AmazonAuthError(f"HTTP {resp.status}")
        if not 200 <= resp.status < 300:
            raise AmazonError(resp.status, await resp.text())

    async def close(self) -> None:
        await self._login.close()
