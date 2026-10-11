# ScreenEye

An iPhone pointed at a screen, so Claude can read it — a console with no SSH, a
BIOS setup page, a TV, a printer's touchscreen.

- [`ios/`](ios/) — the app. It runs the back camera (4K where supported) and
  serves it over HTTP on port 8080, advertised over Bonjour as `_screeneye._tcp`.
  Generate the Xcode project with `xcodegen` (`project.yml`), then build to the
  phone.
- [`mcp/`](mcp/) — the MCP server that drives it.

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
`/tmp/screeneye`).

### meta-mcp

```toml
[[mcps]]
name = "screeneye"
description = "iPhone camera pointed at a screen: snapshot, zoom, focus"
command = ["/opt/homebrew/bin/uv", "--directory", "/Users/nakomis/repos/nakomis/mcps/screeneye/mcp", "run", "screeneye-mcp"]
```

### Tests

```sh
cd mcp && uv run pytest
```

They run against a fake phone (a local HTTP server advertised over Bonjour).

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=mcps)
