# falai-mcp

Hosted image generation and editing via [fal.ai](https://fal.ai), defaulting
to Seedream 5.0 Pro. Replaces `draw-things-mcp`, which needed the Draw Things
app running locally and stopped working when it was uninstalled.

## Tools

| Tool | Default model | Use it for |
|---|---|---|
| `generate_image` | `bytedance/seedream/v5/pro/text-to-image` | Text prompt → new image, any size |
| `edit_image` | `bytedance/seedream/v5/pro/edit` | Instruction-driven edits, up to 4 reference images |
| `remove_object` | `fal-ai/object-removal` | Deleting something from a photo |

## Choosing a model

Every tool takes `model`. For `generate_image` and `edit_image` the choices are
`seedream` (default), `gpt-image`, and `flux`.

**The default is perishable.** This server hardcoded `flux-2-pro` until
2026-08-06, which was the right call when it was written on 2026-07-26 and last
of six eleven days later. Measured on a prompt requiring eight distinct gilt
titles across eight book spines:

| Model | Titles correct |
|---|---|
| Seedream 5.0 Pro | **8/8** |
| GPT Image 2 | **8/8** |
| Nano Banana Pro | 7/8 |
| FLUX.2 [pro] | 3/8 — *"MOBY THE BEAGLE"*, *"MOBY DI DICK"* |
| Ideogram v3 | 3/8, and ignored the composition constraints |
| Qwen-Image | 2/8 — *"SPEGIES"*, *"SHESEHOLD MANAGEMENT"* |

Nothing about FLUX got worse; the field moved underneath it. Re-measure before
trusting the default, and note that a constant which looks deliberate tells you
nothing about its age.

**Seeds.** Only `flux` accepts one. Seedream and GPT Image have no seed
parameter, so passing `seed` to them returns a warning saying the image cannot
be reproduced, rather than silently dropping it.

`nano-banana-pro` is not wired up: it takes `aspect_ratio` and `resolution`
instead of `image_size`, so adding it without translating those would silently
ignore every size argument.

## Low-balance warning

Every result carries a `warnings` list. When the fal.ai balance falls below
**$5**, a warning is added telling the caller to mention it. Override with
`FALAI_LOW_BALANCE`.

The check runs *after* the image has been fetched and swallows every failure,
so a slow or changed billing endpoint can cost you a warning but never the
picture you already paid for.

### Which editing tool?

`remove_object` segments and erases the named object, leaving everything else
pixel-identical. `edit_image` re-renders the entire image, so untouched areas
drift — the composition shifts, foliage rearranges. For "take the dog out of
this photo", `remove_object` is the right answer; `edit_image` is for changes
that genuinely alter the scene.

## Setup

The API key is read from the macOS keychain, matching
`blog-content/review-pipeline/falai_batch.py`:

```bash
security add-generic-password -s "fal.ai" -a "api-key" -w <YOUR_KEY>
```

`FAL_KEY` overrides it if set.

Editing needs the staging bucket from [`../infra`](../infra/) deployed, and AWS
credentials that can read/write it (`AWS_PROFILE=nakom.is-sandbox`).
`generate_image` needs neither — it has no input image.

## How input images reach fal

fal's editing endpoints take image **URLs**, not uploads, so a local file has
to be reachable over HTTPS for the length of one call. The flow is:

1. Upload to `nak-sandbox-falai-uploads` under a random UUID key
2. Generate a presigned GET valid for 15 minutes
3. Call fal with that URL
4. Delete the object in a `finally` block

The bucket's 24-hour lifecycle rule is the backstop for step 4 failing — a
crash, a dropped connection, a killed process. Nothing is meant to persist.

Base64 data URIs also work and were the first implementation, but they cap out
on request size and would have meant downscaling phone photos before upload.
S3 keeps full resolution. Pass `max_dimension` to downscale deliberately.

### Expired SSO credentials

Local AWS credentials lapse often. Every flavour of that failure — missing
token cache, expired token, refused refresh, or a `ClientError` arriving
mid-call because the session died between constructing the client and using it
— is caught and re-raised as `AwsLoginRequired`, carrying an instruction to
stop and ask the user to run:

```bash
aws sso login
```

No `--profile` flag: every profile shares one IAM Identity Center session. The
message tells the calling model explicitly *not* to run the command itself,
since it opens a browser and blocks on interactive sign-in, and notes that
`generate_image` still works meanwhile.

### The regional endpoint gotcha

The presigned URL **must** use `s3.<region>.amazonaws.com`, not the global
`s3.amazonaws.com`. The global host answers with a 307 redirect to the
regional one, and fal's fetcher doesn't follow redirects — the call comes back
as an opaque `500 Internal Server Error` with nothing pointing at the cause.
`_staged_urls` pins the endpoint explicitly.

## Object removal: what it actually does

Tested on a generated garden scene with a golden retriever:

- The dog is removed cleanly and the rest of the photo is untouched.
- **Its shadow is not.** No setting removes it.
- `mask_expansion` above ~25 degrades badly. 15 (the default) was best;
  raising it smeared more without capturing the shadow.
- Describing the shadow makes it worse — "the dog and its shadow" confused the
  segmenter into mangling the lawn and leaving the dog in place.

Use plain noun phrases: "the dog", "the person on the left", "the bin". If a
leftover shadow matters, follow up with `edit_image` and ask for it explicitly,
accepting a full re-render.

## Prompt phrasing for `edit_image`

The model ignores soft requests. "Remove the dog from the lawn" changed
nothing. "Delete the golden retriever completely. The lawn must be entirely
empty grass where the dog was" worked. Be explicit and state the required end
state, not the action.

## Structured output

Every tool returns an `ImageResult`, not a string, so MCP emits a real
`outputSchema` and callers can inspect fields rather than parse prose:

```json
{
  "path": "/Users/nakomis/Pictures/falai-mcp/20260726_173221_a_tiny_grey_mouse.png",
  "width": 256, "height": 256, "size_kb": 118, "seed": 798931316,
  "requested_width": 128, "requested_height": 128,
  "size_honoured": false,
  "warnings": ["Requested 128x128 but fal.ai returned 256x256. ..."]
}
```

`warnings` is empty on the happy path. It exists because **fal silently clamps
sizes below 256px** and snaps to its own grid — no error, no warning, just a
different number in the response. Ask for 128×128 and you get 256×256 back as
though nothing happened. `size_honoured` is the boolean to branch on;
downscale locally when an exact size matters.

## Output

Generated images land in `~/Pictures/falai-mcp/`. Edits and removals are
written next to their source image, since that's where they belong. Both
return the absolute path — read it to view the result. Input files are never
modified.
