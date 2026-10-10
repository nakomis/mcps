# alexa-mcp

Real Alexa announcements (chime, all Echos in sync, any text) and plain
speech, as MCP tools. CC0.

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=mcps)

## How it works

Amazon has no public API for announcements. [alexapy](https://gitlab.com/keatontaylor/alexapy)
(the library behind Home Assistant's Alexa Media Player) registers itself as an
Alexa app on your account and calls the same private API the app uses.

That runs in the `alexa-announce` Lambda (see [`../infra`](../infra/)), which keeps
the registration in Secrets Manager (`alexa-announce/session`). This MCP only
invokes the Lambda with your AWS SSO profile, so the Amazon session never
leaves AWS.

It is unofficial and will break whenever Amazon changes things.

## Tools

| Tool | Does |
|---|---|
| `announce(text, devices?)` | Chime, then the text, in sync on every device. Shown on Echo Show screens too. |
| `speak(text, devices?)` | Says the text on each device, with no chime. |

Omit `devices` to use `ALEXA_DEFAULT_DEVICES`. Names are matched ignoring case;
an unknown name sends nothing and the error lists the valid ones.

## Configuration

| Variable | Default |
|---|---|
| `ALEXA_AWS_PROFILE` | `nakom.is` (prod) |
| `ALEXA_REGION` | `eu-west-2` |
| `ALEXA_FUNCTION` | `alexa-announce` |
| `ALEXA_DEFAULT_DEVICES` | none; comma-separated device names |

See `../meta-mcp/config.toml.example` for the meta-mcp entry.

## Signing in (once per account)

```bash
uv --directory alexa-mcp run --extra login alexa-mcp-login <amazon-email> --profile nakom.is
```

Open the printed `http://127.0.0.1:8765` URL and sign in to Amazon as usual.
The registration is written straight into that account's secret. Use
`--profile nakom.is-sandbox` for sandbox; each account has its own registration.

The registration has no expiry. To revoke it, remove the Alexa app entry under
Amazon → Your Account → Your Devices and Content → Devices. To replace it, run
`alexa-mcp-login` again.

## Tests

```bash
cd alexa-mcp && uv run pytest
```
