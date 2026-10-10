# mcps infra

CDK app for AWS resources backing the MCP servers in this repo.

| Stack | Environments | For |
|---|---|---|
| `McpsFalaiUploadsStack` | sandbox only | staging bucket for [`falai-mcp`](../falai-mcp/) |
| `McpsAlexaAnnounceStack` | sandbox + prod | the `alexa-announce` Lambda behind [`alexa-mcp`](../alexa-mcp/) |

`NPM_ENVIRONMENT` (`sandbox` | `prod`) picks the account: sandbox `975050268859`,
prod `637423226886`, both `eu-west-2`. No CI; deployed by hand.

## Deploying

```bash
pnpm install
pnpm run synth-sandbox    # or synth-prod
pnpm run deploy-sandbox   # nakom.is-sandbox
pnpm run deploy-prod      # nakom.is-admin
```

Lambda bundling uses `uv` on the host (no Docker); it falls back to the
Python 3.12 bundling image if local bundling is unavailable.

## Alexa announce stack

- `alexa-announce`: Python 3.12 Lambda, alexapy, 30 s timeout, 30-day logs.
- `alexa-announce/session`: the Amazon device registration. CDK creates a
  placeholder; fill it with `alexa-mcp-login <email> --profile <profile>`. It
  is retained on stack deletion. **Don't change the secret's generator settings**:
  CloudFormation would regenerate the value and wipe the registration.
  If a failed first deploy or a destroy leaves the retained secret orphaned,
  remove it before redeploying (this deletes the registration; log in again
  afterwards):
  `aws secretsmanager delete-secret --secret-id alexa-announce/session --force-delete-without-recovery --profile <profile> --region eu-west-2`
- Callers need only `lambda:InvokeFunction` on the function.

## falai uploads bucket (sandbox only)

fal.ai's image-editing endpoints accept image **URLs**, not uploads, so
[`falai-mcp`](../falai-mcp/) needs somewhere to put a local file for the length
of a single API call. `nak-sandbox-falai-uploads` is that somewhere.

The MCP deletes each object as soon as the call returns. The bucket's 24-hour
lifecycle rule catches whatever escapes that. The stack outputs the bucket name;
`falai-mcp` defaults to it, so set `FALAI_BUCKET` in `meta-mcp/config.toml` only
if you rename it.

## The bucket

| | |
|---|---|
| Name | `nak-sandbox-falai-uploads` |
| Region | `eu-west-2` |
| Public access | Blocked entirely — presigned URLs carry their own auth |
| Encryption | S3-managed, TLS enforced |
| Lifecycle | Objects expire after 1 day; incomplete multipart uploads too |
| Removal policy | `DESTROY` with `autoDeleteObjects` |

S3 evaluates lifecycle rules once a day, so "24 hours" is a floor rather than a
guarantee — an object may live up to ~48 hours if it lands just after a sweep.
That's fine for a backstop; the MCP's own delete is what normally applies.

## Tearing it down

```bash
pnpm run destroy-sandbox
```

`autoDeleteObjects` empties the bucket first, so this succeeds even with
objects in flight. `generate_image` keeps working without it; the editing
tools do not.
