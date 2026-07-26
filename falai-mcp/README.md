# falai-mcp

Hosted image generation and editing via [fal.ai](https://fal.ai), using
FLUX.2 [pro]. Replaces `draw-things-mcp`, which needed the Draw Things app
running locally and stopped working when it was uninstalled.

## Tools

| Tool | Model | Use it for |
|---|---|---|
| `generate_image` | `fal-ai/flux-2-pro` | Text prompt → new image, any size |
| `edit_image` | `fal-ai/flux-2-pro/edit` | Instruction-driven edits, up to 4 reference images |
| `remove_object` | `fal-ai/object-removal` | Deleting something from a photo |

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

## Output

Generated images land in `~/Pictures/falai-mcp/`. Edits and removals are
written next to their source image, since that's where they belong. Both
return the absolute path — read it to view the result. Input files are never
modified.
