# alexa-mcp — design

Date: 10 October 2026
Status: draft, awaiting review

## Purpose

Let Claude make Alexa announcements on Martin's Echos: the real thing, as
"Alexa, announce …" does it, with the chime, all target devices in sync, and
arbitrary text. Also offer plain chime-less speech.

Success: Claude calls `announce("…")` and the default Echos play it as a proper
announcement, with no Amazon credentials stored outside AWS.

## Background: why this shape

Amazon offers no official API for announcements. Third-party skills cannot make
them, and the Voice Monkey-style workaround (smart-home trigger → routine →
custom skill) gives per-device speech with fixed-text announcements only.

[alexapy](https://gitlab.com/keatontaylor/alexapy) (Apache-2.0, the library behind
Home Assistant's Alexa Media Player) instead registers itself as an Alexa app on
the account and calls the private API the app uses:
`POST https://alexa.amazon.co.uk/api/behaviors/preview` with an
`AlexaAnnouncement` sequence. It is unofficial and will break when Amazon changes
things; that risk is accepted.

### Spike results (10 Oct 2026, throwaway, since deleted)

- A one-off browser login through alexapy's local auth proxy produced a device
  registration and refresh token. Password and 2FA were typed into Amazon's own
  page; the script never handled them.
- A Lambda in the sandbox account, given **only** the refresh token, logged in,
  listed all 16 devices, and made an announcement on three chosen Echos.
  Amazon returned 200 and all three played it.
- The refresh token was unchanged by use, so nothing needs to be written back.
  This removes any need for a table or for limiting the Lambda's concurrency.
- An "all online speakers" filter is unsafe. It caught the parents' Fire TV, the
  Echo Auto, TVs and the Alexa app itself. Targets must be named explicitly.

## Architecture

```
Claude ──▶ alexa-mcp (local, meta-mcp) ──boto3 lambda.invoke (SigV4, SSO profile)──▶
    alexa-announce Lambda ──▶ Secrets Manager (session)
                          └──▶ alexa.amazon.co.uk /api/behaviors/preview
```

There is no API Gateway: invoking the Lambda already requires IAM authorisation. Any
IAM principal with `lambda:InvokeFunction` on the function can call it (the Mac's SSO
profile, Luke/Leia via IAM Roles Anywhere, EventBridge). A Lambda Function URL with
`AWS_IAM` auth can be added later if a curl-able endpoint is ever wanted.

## Environments

Sandbox **and** prod, following the `home-servers/infra/cdk` pattern:

- `infra/bin/mcps.ts` reads `NPM_ENVIRONMENT` (`sandbox` | `prod`, anything else throws)
  and selects account `975050268859` (sandbox) or `637423226886` (prod), region `eu-west-2`.
- Scripts: `synth-sandbox` / `deploy-sandbox` (`nakom.is-sandbox`), `synth-prod` /
  `deploy-prod` (`nakom.is-admin`), `destroy-sandbox`. These are deployed by hand, with no CI.
- `McpsFalaiUploadsStack` stays **sandbox-only**: it is instantiated only when
  `deployEnv === 'sandbox'`. A prod deploy creates only the Alexa stack.
- Each account has its **own** device registration and secret, so either can be revoked
  without affecting the other.
- The MCP defaults to **prod**. All profiles share one IAM Identity Center session, so a
  single `aws sso login` covers both.

## Components

### `infra/lib/alexa-announce-stack.ts`

- **Secret** `alexa-announce/session` (Secrets Manager). CDK creates it with a
  placeholder value (`{}`), and `alexa-mcp-login` fills it in. If the Lambda finds the
  value can't be parsed or has no `refresh_token`, it returns `session_expired`. The value is JSON:
  `{email, url, refresh_token, mac_dms, uuid}`.
  Removal policy `RETAIN`, so a stack teardown does not destroy the registration
  silently.
- **Lambda** `alexa-announce`: Python 3.12, x86_64, 512 MB, 30 s timeout.
  - Its role has `secretsmanager:GetSecretValue` on that one secret, plus basic execution.
  - It has a log group with 30-day retention.
  - Bundling: `Code.fromAsset` with **local** bundling that runs
    `uv pip install --target <out> --python-platform x86_64-manylinux_2_28 --python-version 3.12 --only-binary :all:`
    plus the handler source, so Docker is not needed. This is the command proven in the spike.
- Outputs: the function name and ARN, and the secret ARN.

### `alexa-mcp/lambda/` (handler)

- Input: `{"action": "announce" | "speak", "text": str, "devices": [str, …]}`
- Output, on success: `{"ok": true, "action": …, "devices": [names]}`
- Output, on failure: `{"ok": false, "error": "session_expired" | "unknown_devices" | "bad_request" | "amazon_error", "detail": …}`
  - `unknown_devices`: `detail` holds `{unknown: [...], valid: [...]}`, where `valid` lists
    names of devices with the `VOLUME_SETTING` capability, so a typo can be corrected
    from the error alone.
  - `amazon_error`: `detail` holds the HTTP status and up to 300 characters of the body.
- Session handling:
  - The alexapy login is kept in a module-level variable and reused while the Lambda
    stays warm.
  - On a cold start, or after a failed call, it reads the secret and logs in from the
    refresh token. Login failure gives `session_expired`.
  - If an Amazon call fails with 401 or 403 on a warm session, it discards the session,
    logs in again once and retries once.
- Device resolution:
  - It fetches the device list (once per warm session) and matches **exact**
    `accountName`s.
  - Any unmatched name fails the whole call. Nothing is sent.
  - An empty list gives `bad_request`, never "all devices".
- Sequences:
  - `announce`: one `AlexaAnnouncement` node with `target.devices` = all resolved
    devices, locale `en-GB`, `expireAfter: PT5S`. This is the payload the spike proved.
  - `speak`: one `Alexa.Speak` node per device (`textToSpeak`, `deviceType`,
    `deviceSerialNumber`, `locale`, `customerId`), wrapped in a `ParallelNode`.
- The text must be 1–1000 characters, otherwise `bad_request`.
- Logging: action, device names, outcome and timing. **Never** tokens, cookies or message
  text. alexapy's own logger is set to WARNING.
- The code is split so that device resolution and payload building are pure functions,
  kept apart from the I/O that talks to Amazon and Secrets Manager.

### `alexa-mcp/alexa_mcp/server.py` (MCP)

- FastMCP, structured like `falai-mcp`. There are two tools:
  - `announce(text: str, devices: list[str] | None = None)`
  - `speak(text: str, devices: list[str] | None = None)`
- With no `devices`, it uses `ALEXA_DEFAULT_DEVICES`. These are comma-separated names
  (they contain apostrophes and spaces but no commas). Example:
  `Martin's Kitchen Echo Dot,Martin's Echo Show,Bedroom Echo`.
  If this is unset and no devices are given, the tool returns an error.
- It calls the Lambda with `boto3.Session(profile_name=ALEXA_AWS_PROFILE).client("lambda", region_name=ALEXA_REGION).invoke(...)`
  (RequestResponse).
- Environment variables (defaults point at prod):

  | Variable | Default |
  |---|---|
  | `ALEXA_AWS_PROFILE` | `nakom.is` |
  | `ALEXA_REGION` | `eu-west-2` |
  | `ALEXA_FUNCTION` | `alexa-announce` |
  | `ALEXA_DEFAULT_DEVICES` | none |

- Errors are turned into readable tool results:
  - An expired SSO session or missing token says to run `aws sso login` (the same
    wording approach as `falai-mcp`).
  - `session_expired` says the Alexa session has expired and to run
    `alexa-mcp-login --profile <profile>`.
  - `unknown_devices` lists the unknown names and the valid ones.
  - A Lambda `FunctionError` is passed on with its message.

### `alexa-mcp/alexa_mcp/login.py` (`alexa-mcp-login`)

- Usage: `alexa-mcp-login <amazon-email> [--profile nakom.is] [--region eu-west-2] [--secret alexa-announce/session] [--port 8765]`
- Runs the spike's proxy flow: start `AlexaProxy` on `127.0.0.1:<port>`, print the URL,
  wait for the authorisation code, then `test_loggedin()` to register the device and fetch
  tokens.
- It writes the session JSON **directly** to Secrets Manager with `put_secret_value`.
  Nothing is written to disk, apart from alexapy's cookie files, which go to a temporary
  directory that is deleted on exit.
- It prints the names of the speakers it found, to help set `ALEXA_DEFAULT_DEVICES`.
  It never prints token values.
- alexapy is needed on the Mac only for this command. It goes into an optional
  dependency group, `alexa-mcp[login]`, so the MCP server itself depends only on
  `mcp` and `boto3`.

### meta-mcp config

The example config gets an `alexa` entry:

```toml
[[mcps]]
name = "alexa"
description = "Alexa announcements and speech on the Echos"
command = ["uv", "--directory", "/path/to/mcps/alexa-mcp", "run", "alexa-mcp"]
env = {ALEXA_DEFAULT_DEVICES = "Martin's Kitchen Echo Dot,Martin's Echo Show,Bedroom Echo"}
```

The real `config.toml` (gitignored) is updated the same way.

## Rollout

1. Deploy the sandbox stack. Run `alexa-mcp-login --profile nakom.is-sandbox`, which
   creates a new registration, and test with `ALEXA_AWS_PROFILE=nakom.is-sandbox`.
2. Deploy the prod stack. Seed the prod secret **once** from the spike's kept
   `state.json` with `put-secret-value`, then delete `state.json`.
3. Add the MCP to meta-mcp and make one real `announce` call.

## Error handling summary

| Failure | What the caller sees |
|---|---|
| SSO session expired | "Run `aws sso login`" |
| Secret empty or refresh rejected | `session_expired` → "Run `alexa-mcp-login --profile …`" |
| Device name typo | `unknown_devices` with the valid names; nothing is sent |
| Amazon returns non-2xx | `amazon_error` with the status and a short body excerpt |
| Lambda crash or timeout | The `FunctionError` message is passed on |

## Testing

- pytest, in `alexa-mcp/tests/`:
  - device resolution: exact matching, unknown names, empty list
  - payload building: announcement, speak, ParallelNode shape
  - text validation
  - turning MCP error envelopes into readable results, with a stubbed Lambda client
- Manual end-to-end tests: a sandbox announce, then a prod announce, then an
  unknown-device call.

## Security notes

- The Amazon session is as powerful as the Alexa app on your phone. It never leaves AWS
  apart from in transit during the one-off `alexa-mcp-login`.
- Revoking: remove the "Alexa app" registration under Amazon → Your Devices and
  Content. Each account's registration can be revoked separately.
- Callers hold only IAM permission to invoke the Lambda. No Amazon credentials are
  distributed.

## Out of scope

- Sending my "blocked" audible alerts through this. That's a small follow-up once it exists.
- A `list_devices` tool. The `unknown_devices` error covers discovery.
- Email or SNS alerts when the session dies.
- A Function URL or API Gateway.
- Question/answer interactions.
