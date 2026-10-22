# ScreenEye

An iPhone (or iPad) pointed at a screen, so Claude can read it — a console with no SSH, a
BIOS setup page, a TV, a printer's touchscreen.

- [`ios/`](ios/) — the app. It runs the back camera (4K where supported) and
  serves it over HTTP on port 8080, advertised over Bonjour as `_screeneye._tcp`.
  Generate the Xcode project with `xcodegen` (`project.yml`), then build to the
  phone.
- [`macos/`](macos/) — a Mac app to watch the phone's camera live and take
  pictures with it (it doesn't use the Mac's own camera).
- [`mcp/`](mcp/) — the MCP server that drives it.
- [`host/`](host/) — the always-on host on Mace: receives frames the app
  pushes, and runs the MCP.

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=mcps)

## The app's HTTP API

| Endpoint | Does |
|---|---|
| `GET /snapshot.jpg` | The picture: the visible region (the viewport) of the latest frame at full resolution, raw. Headers `X-ScreenEye-Orientation`, `X-ScreenEye-Rotation` and `X-ScreenEye-Viewport` (`x,y,w,h`, raw fractions of the lens's frame) |
| `GET /snapshot.jpg?upright=1` | The same picture turned upright (`X-ScreenEye-Rotation` is then `0`) |
| `GET /snapshot.jpg?full=1` | The lens's whole frame rather than the viewport (combines with `upright=1`) |
| `GET /snapshot.jpg?max=1600` | Scaled down so its longer side is at most this many pixels, for a live preview (combines with the others) |
| `GET /set?zoom=4&pan=0.3,0.6&focus=0.5,0.5&lock=1&ev=-1` | Zoom (multiple of the 1x lens), viewport centre (fractions of the lens's frame), focus/exposure point (fractions of the picture), focus lock, exposure bias. Any subset. Add `upright=1` to give `pan` and `focus` in the upright frame |
| `GET /status` | JSON: `zoom`, `minZoom`, `maxZoom`, `lensSteps`, `opticalZoom`, `lens`, `focusMode`, `lensPosition`, `ev`, `orientation`, `rotation`, `pan`, `viewport`, `coordinates`. `?upright=1` gives `pan` and `viewport` in the upright frame (`coordinates` says which) |

### Orientation

`/snapshot.jpg` is in the sensor's native landscape whatever way up the phone
is, for compatibility: upright only when the phone is in landscape with its top
to the left. The app follows gravity with CoreMotion, which works with the
screen's rotation locked, and reports:

- `orientation`: `portrait`, `portraitUpsideDown`, `landscapeLeft` (top to the
  left), `landscapeRight`, `faceUp`, `faceDown`, or `unknown` before the first
  reading (and on the simulator).
- `rotation`: how far to turn a raw frame **clockwise** to make it upright: `0`
  (`landscapeLeft`), `90` (`portrait`), `180` (`landscapeRight`), `270`
  (`portraitUpsideDown`).

Lying flat (within about 30° of face up or face down), gravity can't say which
way is up in the picture, so `rotation` **keeps its last value**: a phone
tipped down over a desk or a print bed was usually upright a moment before.
It's `0` if the phone has been flat since the app started. Near a 45°
diagonal, the orientation only changes once the phone is 10° past it, so it
doesn't flicker.

### Zoom and pan

Zoom is made of the lens's own zoom, at the nearest lens step at or below
(the lenses the device has, and the zooms a lens crops its sensor to at full
resolution, such as the 48 MP main camera's 2x), and a digital crop of that
lens's frame. The crop is the **viewport**, and it can sit anywhere in the
frame: that's panning. `pan` is the viewport's centre, kept inside the frame;
at a lens step there's no crop, so nothing to pan.

Coordinates are fractions, (0,0) top-left. On the app's API they're raw
(the sensor's landscape) unless `upright=1` is given. `pan` and `viewport` are
of the lens's whole frame; `focus` is a point in the picture (the viewport).
The MCP works upright by default, so its `pan`, `focus_x`, `focus_y`, `crop`
and `viewport` all agree with the picture it returns.

## On the phone

- **Zoom:** pinch the preview, tap a step (the device's lenses, such as 0.5×,
  1×, 2×, 4×; on a one-lens iPad, 1× and digital 2× and 5×) or drag the slider up to the device's maximum. It's the same zoom
  as `/set?zoom=`: either moves the other, and `/status` reports it.
- **Pan:** zoomed past a lens, drag the preview to move around; an overview in
  the corner shows the whole frame with the visible region outlined. Double
  tap recentres. It's the same viewport as `/set?pan=`.
- **Save to Mac:** pushes what's on screen at full resolution (the same image as
  `/snapshot.jpg`), with the camera state and an optional note, to the ScreenEye
  host. The host is found over Bonjour (`_screeneye-host._tcp`, TXT
  `path=/api/frames`); if none is found, the address typed under Settings (the
  gear) is used. The app shows progress, then the saved file name or the error.

The push is `POST {host}/api/frames`, `multipart/form-data`:

| Field | Contents |
|---|---|
| `image` | `image/jpeg`, filename `frame.jpg` |
| `meta` | JSON: `{"zoom": n, "lens": "…", "ev": n, "focus": [x, y] \| null, "lensPosition": n, "note": "…" \| null, "device": "…", "taken": "<ISO 8601 with offset>", "orientation": "…", "rotation": 0 \| 90 \| 180 \| 270, "opticalZoom": n, "frame": [w, h], "crop": [x, y, w, h]}` |

The image is raw; the host turns it upright using `rotation`. `frame` is the
lens's whole frame in pixels and `crop` the part of it in the image (raw
fractions); `zoom` is `opticalZoom` times the digital crop.

`focus` is the last focus point set, as raw fractions of the lens's whole frame, or `null`. The host replies
`201` with `{"name": "HHMMSS-note.jpg", "path": "screeneye/YYYY-MM-DD/HHMMSS-note.jpg"}`,
or `{"error": "…"}` with a 4xx/5xx.

### Building the app

```sh
cd ios && xcodegen generate
xcodebuild -project ScreenEye.xcodeproj -scheme ScreenEye \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test   # unit tests
```

## The Mac app

[`macos/`](macos/) is a native viewer for the phone: it finds it over Bonjour
(`_screeneye._tcp`; pick one in the toolbar if there are several, or type an
address in Settings), shows the camera live and takes pictures on the Mac. It
never touches the Mac's own camera.

- **Live view:** polls `/snapshot.jpg?upright=1&max=…` back to back, one
  request at a time, so it runs as fast as the phone and Wi-Fi allow. The frame
  size is in Settings (smaller is smoother). A phone app from before EYE-5
  ignores `max` and sends full-size frames: it still works, just slower.
- **Zoom:** the lens steps, the slider (logarithmic, like the phone's), a
  trackpad pinch, or ⌘= / ⌘- / ⌘1.
- **Pan:** zoomed past a lens, drag the picture; an overview in the corner
  shows where you are. Double-click or ⌘0 recentres.
- **Focus:** click the picture to focus and expose there; *Lock focus* (⌘L)
  holds it. The EV slider sets the exposure bias.
- **Take Picture** (⌘S, or Return in the note field): the full-resolution
  upright picture, exactly as the phone encoded it, saved to
  `~/Pictures/ScreenEye/YYYY-MM-DD/HHMMSS[-note].jpg` with a `.json` sidecar
  (the phone's `/status`, the note, the time and the phone), named as the host
  names them and never overwriting. ⌥⌘S takes the lens's whole frame. The
  folder, and copying each picture to the clipboard too, are in Settings.
  ⇧⌘C copies the live frame.
- **Open in Claude** (⇧⌘O): takes the same picture (note `parcel` if empty) and hands its path
  to [parcel-finder](https://github.com/nakomis/parcel-finder), a LaunchAgent on the Mac that
  finds the Claude Code conversation where the item was bought and wakes it with the photo
  (`POST http://127.0.0.1:8775/photo`, `{"path": …}`). If parcel-finder isn't running, the
  picture is still saved and the app says so.

Being a signed app with `NSLocalNetworkUsageDescription` and
`NSBonjourServices`, it gets macOS's Local Network prompt (allow it) rather
than being silently blocked as a process meta-mcp spawns is.

```sh
cd macos && xcodegen generate
xcodebuild -project ScreenEyeMac.xcodeproj -scheme ScreenEyeMac -destination 'platform=macOS' test
xcodebuild -project ScreenEyeMac.xcodeproj -scheme ScreenEyeMac -configuration Release \
  -derivedDataPath build/dd -allowProvisioningUpdates build
cp -R build/dd/Build/Products/Release/ScreenEye.app /Applications/
```

## The MCP server

| Tool | Does |
|---|---|
| `snapshot` | Optionally adjusts the camera (zoom, `pan`, focus, lock, ev), waits `settle_ms` for focus, takes the picture, turns it upright, optionally crops (`[x, y, w, h]` fractions of the upright picture) and scales (`max_dim`, default 2560), saves a JPEG and returns its **path**, with `orientation`, `rotated_deg` and `viewport` (what the picture covers of the lens's frame). `full_frame=True` takes the lens's whole frame, to see where to pan; `upright=False` keeps the raw sensor frame and coordinates |
| `set_camera` | Zoom, `pan` (`[x, y]`, the view's centre), focus point (in the picture), focus lock, exposure bias; upright coordinates unless `upright=False` |
| `status` | Camera state (with `orientation`, `rotation`, `opticalZoom`, `pan` and `viewport`, upright by default) and the phone's URL |

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
| `POST /api/frames` | `multipart/form-data`: `image` (`image/jpeg`) and `meta` (a JSON object: zoom, lens, ev, focus, lensPosition, note, device, taken, orientation, rotation). Turns the frame upright by `rotation` (clockwise; missing means 0, anything but 0/90/180/270 is a 400), keeping the JPEG's quality tables. Writes `screeneye/YYYY-MM-DD/HHMMSS[-note].jpg` and a `.json` sidecar (the meta, plus `received`, `client`, `bytes` (of the file written), `rotated_deg`, `name`; the meta's `rotation` is kept as the phone sent it, and the saved image has already been turned by `rotated_deg`), never overwriting (`-2`, `-3`, ...). Replies `201 {"name", "path"}` or `{"error"}` with 4xx/5xx; non-JPEGs get 415, anything over 30 MiB 413 |
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
