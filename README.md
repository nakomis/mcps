# nakomis/mcps

MCP servers for use with Claude Code. All code is CC0 — no rights reserved.

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=mcps)

## Servers

| Server | Description |
|---|---|
| [evernote-mcp](evernote-mcp/) | Read-only access to Evernote notes via exported .enex files |
| [trello-mcp](trello-mcp/) | Read/write access to Trello boards, lists, and cards |
| [falai-mcp](falai-mcp/) | Image generation, editing, and object removal via fal.ai FLUX.2 |

## Infrastructure

[`infra/`](infra/) is a CDK app for the AWS resources some servers need — currently
just the staging bucket `falai-mcp` uses. Sandbox only, deployed by hand.

## Installation

Each server is a self-contained Python package managed with `uv`. See the individual server's README for setup instructions.

Add servers to `~/.claude/mcp.json` — see each server's README for the exact snippet.
