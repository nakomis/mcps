# mcps infra

CDK app for AWS resources backing the MCP servers in this repo. Currently one
stack, holding one bucket.

## Why it exists

fal.ai's image-editing endpoints accept image **URLs**, not uploads, so
[`falai-mcp`](../falai-mcp/) needs somewhere to put a local file for the length
of a single API call. `nak-sandbox-falai-uploads` is that somewhere.

The MCP deletes each object as soon as the call returns. The bucket's 24-hour
lifecycle rule catches whatever escapes that — a crash, a dropped connection,
a killed process.

## Sandbox only

No prod stage, no CI. This is one throwaway bucket in the sandbox account
(`975050268859`, `eu-west-2`); the sandbox/prod split the other projects carry
would be pure ceremony. It is deployed by hand, rarely.

## Deploying

```bash
pnpm install
pnpm run synth-sandbox     # inspect the template
pnpm run deploy-sandbox    # apply
```

Uses the `nakom.is-sandbox` profile.

The stack outputs the bucket name. `falai-mcp` defaults to it, so nothing needs
wiring unless you rename it — in which case set `FALAI_BUCKET` in
`meta-mcp/config.toml`.

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
