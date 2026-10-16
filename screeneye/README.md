# ScreenEye

An iPhone pointed at a screen, so Claude can read it — a console with no SSH, a
BIOS setup page, a TV, a printer's touchscreen.

- [`ios/`](ios/) — the app. It runs the back camera (4K where supported) and
  serves it over HTTP on port 8080, advertised over Bonjour as `_screeneye._tcp`.
  Generate the Xcode project with `xcodegen` (`project.yml`), then build to the
  phone.
- [`mcp/`](mcp/) — the MCP server that drives it.
- [`host/`](host/) — the always-on host on Mace: receives frames the app
  pushes, and runs the MCP.

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=mcps)

## The app's HTTP API

| Endpoint | Does |
|---|---|
| `GET /snapshot.jpg` | Latest frame |
| `GET /set?zoom=4&focus=0.5,0.5&lock=1&ev=-1` | Zoom (multiple of the 1x lens), focus/exposure point, focus lock, exposure bias. Any subset |
| `GET /status` | JSON: `zoom`, `maxZoom`, `lens`, `focusMode`, `lensPosition`, `ev` |

Snapshots are in the sensor's native landscape orientation whatever way up the
phone is.

## The MCP server

| Tool | Does |
|---|---|
| `snapshot` | Optionally adjusts the camera, waits `settle_ms` for focus, takes a frame, optionally crops (`[x, y, w, h]` fractions) and scales (`max_dim`, default 2560), saves a JPEG and returns its **path** |
| `set_camera` | Zoom, focus point, focus lock, exposure bias |
| `status` | Camera state and the phone's URL |

`snapshot` returns a path rather than image content because `meta-mcp`
flattens results to text. Claude reads the file to see it. To read small text,
crop rather than zoom: a crop keeps full sensor resolution.

The phone is found over Bonjour, cached, and rediscovered once if the cached
address stops answering. Set `SCREENEYE_URL` (e.g. `http://172.29.0.40:8080`)
to skip discovery. `SCREENEYE_DIR` sets where snapshots go (default
`/tmp/screeneye`), in a folder per day. `SCREENEYE_PATH_MAP`
(`local=reader`, several separated by commas) rewrites the returned path for a
caller on another machine; `host_path` then gives the path where it was written.

### meta-mcp

On Mace (the usual set-up): the MCP runs inside the ScreenEye host container,
over SSH. That also gets round macOS's Local Network privacy, which stops the
process meta-mcp spawns on a Mac from reaching the phone.

```toml
[[mcps]]
name = "screeneye"
description = "iPhone camera pointed at a screen: snapshot (saved to a file, Read the path), crop, zoom, focus, exposure"
command = ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mace", "docker", "exec", "-i", "screeneye-host", "screeneye-mcp"]
```

Snapshots land on Luke's print-recordings share in
`screeneye/snapshots/YYYY-MM-DD/`, and the path comes back as
`/Volumes/print-recordings/...` for the Mac to read.

Locally, on a Mac:

```toml
[[mcps]]
name = "screeneye"
description = "iPhone camera pointed at a screen: snapshot, zoom, focus"
command = ["/opt/homebrew/bin/uv", "--directory", "/Users/nakomis/repos/nakomis/mcps/screeneye/mcp", "run", "screeneye-mcp"]
```

## The host

[`host/`](host/) is the always-on side, deployed to Mace by the home-infra
Ansible playbook as the `screeneye-host` container. It receives the frames the
app's Save button pushes and files them on the share:

| Endpoint | Does |
|---|---|
| `POST /api/frames` | `multipart/form-data`: `image` (`image/jpeg`) and `meta` (a JSON object: zoom, lens, ev, focus, lensPosition, note, device, taken). Writes `screeneye/YYYY-MM-DD/HHMMSS[-note].jpg` and a `.json` sidecar (the meta, plus `received`, `client`, `bytes`, `name`), never overwriting (`-2`, `-3`, ...). Replies `201 {"name", "path"}` or `{"error"}` with 4xx/5xx; non-JPEGs get 415, anything over 30 MiB 413 |
| `GET /health` | `{"ok": true, "root", "writable"}` |

It listens on port 8090 and is advertised by the host's Avahi as
`_screeneye-host._tcp`, TXT `path=/api/frames`
([`host/screeneye-host.service`](host/screeneye-host.service)). Settings:
`SCREENEYE_ROOT` (the share, default `/mnt/print-recordings`),
`SCREENEYE_HOST_PORT`, `SCREENEYE_MAX_BYTES`. It has no authentication: it is
for the home LAN only, and nothing publishes it beyond that.

The image holds both packages. Build it from this directory:

```sh
docker build -f host/Dockerfile -t screeneye-host .
```

### Tests

```sh
cd mcp && uv run pytest
cd host && uv run pytest
```

The MCP's run against a fake phone (a local HTTP server advertised over
Bonjour); the host's against Starlette's test client.

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=mcps)
