#!/usr/bin/env python3
"""Alexa MCP server: announcements and speech on Martin's Echos.

Amazon has no public API for this. The work happens in the alexa-announce Lambda
(see ../infra), which holds an alexapy device registration in Secrets Manager
and calls the private API the Alexa app uses. This server only chooses devices
and invokes that Lambda with the caller's AWS SSO profile, so no Amazon
credentials ever reach this machine after the one-off alexa-mcp-login.

Defaults point at prod. Set ALEXA_AWS_PROFILE=nakom.is-sandbox to use sandbox.
"""

import json
import os

import boto3
from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    ProfileNotFound,
    SSOError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("alexa-mcp")

PROFILE = os.environ.get("ALEXA_AWS_PROFILE", "nakom.is")
REGION = os.environ.get("ALEXA_REGION", "eu-west-2")
FUNCTION = os.environ.get("ALEXA_FUNCTION", "alexa-announce")

_CREDENTIAL_EXCEPTIONS = (
    NoCredentialsError,
    CredentialRetrievalError,
    ProfileNotFound,
    SSOError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
_EXPIRED_ERROR_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "RequestExpired",
    "UnrecognizedClientException",
    "InvalidAccessKeyId",
}

# Both messages are addressed to the calling model: it must stop and relay them,
# because each fix opens a browser for interactive sign-in.
_SSO_MESSAGE = """\
AWS SSO credentials for profile {profile!r} have expired or are missing.

STOP and tell the user to run this in their terminal, then retry:

    aws sso login

Do not run it yourself: it opens a browser and will hang. Every profile shares
one IAM Identity Center session, so no --profile flag is needed.

({exc})"""

_SESSION_MESSAGE = """\
The Alexa session behind profile {profile!r} has expired or was never set up \
({detail}).

STOP and tell the user to run this in their terminal, then retry:

    uv --directory ~/repos/nakomis/mcps/alexa-mcp run --extra login \\
        alexa-mcp-login <amazon-email> --profile {profile}

Do not run it yourself: it needs a browser sign-in to Amazon."""


class AlexaError(RuntimeError):
    pass


def default_devices(raw: str | None) -> list[str]:
    names = []
    for name in (raw or "").split(","):
        name = name.strip()
        if name and name not in names:
            names.append(name)
    return names


def _client():
    return boto3.Session(profile_name=PROFILE).client("lambda", region_name=REGION)


def _invoke(payload: dict, client) -> dict:
    try:
        client = client or _client()
        resp = client.invoke(FunctionName=FUNCTION, Payload=json.dumps(payload).encode())
    except _CREDENTIAL_EXCEPTIONS as e:
        raise AlexaError(_SSO_MESSAGE.format(profile=PROFILE, exc=e)) from e
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") in _EXPIRED_ERROR_CODES:
            raise AlexaError(_SSO_MESSAGE.format(profile=PROFILE, exc=e)) from e
        raise
    body = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise AlexaError(f"The {FUNCTION} Lambda failed: {body.get('errorMessage', body)}")
    return body


def _interpret(result: dict) -> str:
    if result.get("ok"):
        verb = "Announced" if result["action"] == "announce" else "Spoke"
        return f"{verb} on: {', '.join(result['devices'])}"
    error, detail = result.get("error"), result.get("detail")
    if error == "session_expired":
        raise AlexaError(_SESSION_MESSAGE.format(profile=PROFILE, detail=detail))
    if error == "unknown_devices":
        raise AlexaError(
            f"Unknown device(s): {', '.join(detail['unknown'])}. Nothing was sent. "
            f"Valid names: {', '.join(detail['valid'])}"
        )
    raise AlexaError(f"{error}: {detail}")


def send(action: str, text: str, devices: list[str] | None, client=None) -> str:
    names = devices or default_devices(os.environ.get("ALEXA_DEFAULT_DEVICES"))
    if not names:
        raise AlexaError("No devices given and ALEXA_DEFAULT_DEVICES is not set.")
    return _interpret(_invoke({"action": action, "text": text, "devices": names}, client))


@mcp.tool()
def announce(text: str, devices: list[str] | None = None) -> str:
    """Make an Alexa announcement: a chime, then the text, in sync on every device.

    Args:
        text: What to say (1-1000 characters). Also shown on Echo Show screens.
        devices: Echo names, e.g. ["Bedroom Echo"]. Omit to use the configured
            default set. Matching ignores case. Any unknown name sends nothing
            and the error lists the valid names.
    """
    return send("announce", text, devices)


@mcp.tool()
def speak(text: str, devices: list[str] | None = None) -> str:
    """Have Alexa say the text on each device, with no announcement chime.

    Args:
        text: What to say (1-1000 characters).
        devices: Echo names; omit to use the configured default set.
    """
    return send("speak", text, devices)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
