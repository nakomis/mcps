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
2. checks the CLI is notarised and signed by the same team as the apps (`DJ6XS33FX8`, Learning Machines LLC), and refuses to install it otherwise;
3. unpacks it to `~/.local/share/craft-cli/<app>/<app>-cli-<ver>-macos-universal/`, points `~/.local/bin/<app>-cli` at it and removes older versions.

It exits non-zero if any app's CLI couldn't be fetched or verified. It also notes any app that has no `[[mcps]]` entry in `meta-mcp/config.toml` yet. Afterwards, run `/mcp` in Claude Code so meta-mcp respawns the sub-MCPs on the new binaries.

## Install

```sh
ln -s "$PWD/craft-cli-update" ~/.local/bin/craft-cli-update
```

## meta-mcp entries

All four run headless over stdio. PhotoCraft, EffectCraft and PdfCraft can confine file access to a directory. CADCraft has no such option.

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
