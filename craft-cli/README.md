# craft-cli

Keeps the MCP servers for ArtCraft's [Crafting Apps](https://getartcraft.com/apps/) (PhotoCraft, EffectCraft, PdfCraft, CADCraft and the rest, from [storytold](https://github.com/storytold)) in step with the desktop apps.

Each app's MCP server is its CLI (`photocraft-cli mcp`, `cadcraft-cli mcp`, …), which is a separate release asset, `<app>-cli-<ver>-macos-universal.zip`. It isn't in the app bundle. The apps update themselves, but their CLIs don't.

## `craft-cli-update`

```sh
craft-cli-update            # update every CLI to match its installed app
craft-cli-update --dry-run  # just say what would change
```

For every `/Applications/*Craft.app` signed as `ai.storyteller.*`, the script:

1. reads the app's version and downloads the matching CLI from the app's GitHub release;
2. requires the zip to hold exactly one `<app>-cli-<ver>-macos-universal/` folder containing the binary;
3. checks the binary is signed by the apps' team (`codesign --verify` against `DJ6XS33FX8`, Learning Machines LLC) and notarised (`spctl`), and refuses to install it otherwise;
4. moves it to `~/.local/share/craft-cli/<app>/<app>-cli-<ver>-macos-universal/` and atomically repoints `~/.local/bin/<app>-cli` at it;
5. keeps the version it replaced, since a running sub-MCP may still be using it, and deletes anything older.

`~/.local/share/craft-cli/<app>/` belongs to the script: anything else in it is deleted. A failure for one app (no download, unexpected zip layout, wrong signer) is reported. That app's existing install and link are left as they were, and the script moves on to the next app. It exits non-zero if any app failed. It also notes any app that has no `[[mcps]]` entry in `meta-mcp/config.toml` yet.

Afterwards, run `/mcp` in Claude Code so meta-mcp respawns the sub-MCPs on the new binaries.

## Install

From the repo root (`~/.local/bin` must be on your `PATH`):

```sh
ln -s "$PWD/craft-cli/craft-cli-update" ~/.local/bin/craft-cli-update
```

## meta-mcp entries

These are the four installed here. Every app's CLI runs headless over stdio. PhotoCraft, EffectCraft and PdfCraft can confine file access to a directory. CADCraft has no such option.

```toml
[[mcps]]
name = "photocraft"
description = "PhotoCraft (Photoshop-style image editor), headless; files under ~"
command = ["/Users/you/.local/bin/photocraft-cli", "mcp", "--automation-read-root", "/Users/you", "--automation-write-root", "/Users/you"]

[[mcps]]
name = "effectcraft"
description = "EffectCraft (After Effects-style motion graphics/VFX), headless; files under ~"
command = ["/Users/you/.local/bin/effectcraft-cli", "mcp", "--automation-read-root", "/Users/you", "--automation-write-root", "/Users/you"]

[[mcps]]
name = "pdfcraft"
description = "PdfCraft (Acrobat-style PDF reader/editor), headless; files under ~"
command = ["/Users/you/.local/bin/pdfcraft-cli", "mcp", "--root", "/Users/you"]

[[mcps]]
name = "cadcraft"
description = "CADCraft (AutoCAD-style 2D drafting, DWG/DXF), headless"
command = ["/Users/you/.local/bin/cadcraft-cli", "mcp"]
```

To drive the open app instead of a headless session (bridge mode), see each app's `docs/mcp.md`.
