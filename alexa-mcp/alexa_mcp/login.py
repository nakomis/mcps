"""alexa-mcp-login: register an Alexa app session and store it in Secrets Manager.

Starts alexapy's local auth proxy. You sign in to Amazon in your own browser
through it, typing your password and 2FA into Amazon's page. The resulting
device registration goes straight into the account's alexa-announce/session
secret. Nothing is kept on disk: alexapy's cookie files live in a temporary
directory that is deleted on exit.

Needs the optional extra:  uv run --extra login alexa-mcp-login <email>
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile

URL = "amazon.co.uk"


def session_state(email: str, url: str, refresh_token: str, mac_dms, uuid: str) -> dict:
    if not refresh_token:
        raise ValueError("login finished without a refresh token")
    return {"email": email, "url": url, "refresh_token": refresh_token,
            "mac_dms": mac_dms, "uuid": uuid}


def speaker_names(devices: list[dict] | None) -> list[str]:
    return sorted(
        d["accountName"] for d in devices or []
        if "VOLUME_SETTING" in d.get("capabilities", [])
    )


async def _login(args) -> None:
    import boto3
    from alexapy import AlexaAPI, AlexaLogin, AlexaProxy

    secrets = boto3.Session(profile_name=args.profile).client(
        "secretsmanager", region_name=args.region)
    # Fail before the browser dance if the secret or credentials are wrong.
    secrets.describe_secret(SecretId=args.secret)

    with tempfile.TemporaryDirectory(prefix="alexa-login-") as tmp:
        os.makedirs(os.path.join(tmp, ".storage"))
        login = AlexaLogin(URL, args.email, "", lambda p: os.path.join(tmp, p))
        login._create_session()

        proxy = AlexaProxy(login, f"http://127.0.0.1:{args.port}")
        await proxy.start_proxy(host="127.0.0.1")
        login.proxy_url = proxy.access_url()
        print(f"\nOpen this in your browser and sign in to Amazon:\n\n    {proxy.access_url()}\n",
              flush=True)

        while not login.authorization_code:
            await asyncio.sleep(1)
        await proxy.stop_proxy()
        print("Signed in; registering the device and fetching tokens...", flush=True)

        if not await login.test_loggedin():
            await login.close()
            sys.exit("Amazon did not complete the login. Run alexa-mcp-login again.")

        state = session_state(args.email, URL, login.refresh_token, login.mac_dms, login.uuid)
        secrets.put_secret_value(SecretId=args.secret, SecretString=json.dumps(state))
        names = speaker_names(await AlexaAPI.get_devices(login))
        await login.close()

    print(f"Stored the session in {args.secret} ({args.profile}). Speakers on this account:")
    for name in names:
        print(f"  - {name}")


def main():
    parser = argparse.ArgumentParser(prog="alexa-mcp-login", description=__doc__.splitlines()[0])
    parser.add_argument("email", help="the Amazon account's email address")
    parser.add_argument("--profile", default="nakom.is", help="AWS profile for the target account")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--secret", default="alexa-announce/session")
    parser.add_argument("--port", type=int, default=8765, help="local port for the login proxy")
    asyncio.run(_login(parser.parse_args()))


if __name__ == "__main__":
    main()
