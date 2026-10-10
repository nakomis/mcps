"""The alexapy side: log in from a stored refresh token, list devices, run a sequence.

alexapy is imported lazily so the pure logic and handler can be tested without it.
"""

import asyncio
import os

from alexa_announce.core import behaviour_body


# Amazon answers 429 ("Rate exceeded") when sequences arrive back to back.
RATE_LIMIT_BACKOFF_S = 2.0


class SessionExpired(Exception):
    """The stored registration no longer works. Someone must run alexa-mcp-login."""


class SessionUnusable(Exception):
    """Common base: this session cannot be trusted any more and must be dropped."""


class AmazonAuthError(SessionUnusable):
    """Amazon refused a warm session (401/403). Worth one fresh login."""


class AmazonTransportError(SessionUnusable):
    """Network/transport failure talking to Amazon (timeout, reset, DNS, TLS)."""


class AmazonSendUncertain(Exception):
    """The request may already have reached Amazon, so it must NOT be retried.

    Deliberately not a SessionUnusable: the handler's retry would announce twice.
    """


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
        # Never login.login(): when the refresh chain fails it resets and falls
        # back to a credentials sign-in form POST (email, empty password) against
        # the user's Amazon account. From AWS, only the refresh token is ever
        # used; never a password.
        try:
            login._create_session()
            ok = (
                await login.refresh_access_token()
                and await login.exchange_token_for_cookies()
                and await login.get_csrf()
                and await login.test_loggedin(rebuild_session=False)
            )
        except Exception as e:
            await _close_quietly(login)
            raise AmazonTransportError(f"login failed: {e}") from e
        if not ok:
            await _close_quietly(login)
            raise SessionExpired("Amazon rejected the stored refresh token")
        return cls(login)

    @property
    def customer_id(self) -> str:
        return self._login.customer_id

    async def devices(self) -> list[dict]:
        if self._devices is None:
            from alexapy import AlexaAPI
            from alexapy.errors import (
                AlexapyConnectionError,
                AlexapyLoginError,
                AlexapyTooManyRequestsError,
            )

            try:
                devices = await AlexaAPI.get_devices(self._login)
            except AlexapyLoginError as e:
                raise AmazonAuthError(f"device list refused: {e}") from e
            except (AlexapyConnectionError, AlexapyTooManyRequestsError) as e:
                raise AmazonTransportError(f"device list failed: {e}") from e
            except _transport_errors() as e:
                raise AmazonTransportError(f"device list failed: {e}") from e
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
        import aiohttp

        url = f"https://alexa.{login.url}/api/behaviors/preview"
        body = behaviour_body(start_node)
        try:
            try:
                await self._post_preview(url, body, headers)
            except AmazonError as e:
                if e.status != 429:
                    raise
                # Amazon refused the request with 429, so nothing played and
                # resending it once is safe.
                await asyncio.sleep(RATE_LIMIT_BACKOFF_S)
                await self._post_preview(url, body, headers)
        except aiohttp.ClientConnectorError as e:
            # Never got a connection, so nothing was sent: safe to retry.
            raise AmazonTransportError(f"connection failed: {e}") from e
        except _transport_errors() as e:
            # The request may have been accepted before the failure; retrying
            # could announce twice.
            raise AmazonSendUncertain(f"request failed after send: {e}") from e

    async def _post_preview(self, url: str, body, headers: dict) -> None:
        """One POST attempt. The context manager releases the response, 2xx included."""
        async with self._login.session.post(url, data=body, headers=headers) as resp:
            if resp.status in (401, 403):
                raise AmazonAuthError(f"HTTP {resp.status}")
            if not 200 <= resp.status < 300:
                raise AmazonError(resp.status, await resp.text())

    async def close(self) -> None:
        await self._login.close()


async def _close_quietly(login) -> None:
    try:
        await login.close()
    except Exception:  # a failed close must not mask the error that got us here
        pass


def _transport_errors() -> tuple:
    """Exceptions that mean the network failed, not that Amazon answered.

    aiohttp is imported lazily, like alexapy, so the tests need neither.
    """
    import aiohttp

    return (asyncio.TimeoutError, OSError, aiohttp.ClientError)
