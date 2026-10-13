"""
meta-mcp: lazy-loading proxy MCP server.

Exposes three tools to Claude Code:
  list_mcps()              — list configured sub-MCPs and their one-line descriptions
  describe_mcp(name)       — return the tool schemas for a named sub-MCP (spawning it if needed)
  call_mcp(name, tool, args) — call a tool on a named sub-MCP and return the result
"""

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

import tomli
from mcp.server.fastmcp import FastMCP

from .proxy import SubMcp

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.toml"

# Results longer than INLINE_LIMIT characters are written to a file under
# SPILL_DIR and replaced by a short note with the path and a preview, so one big
# sub-MCP reply (a command catalogue, a base64 image) can't flood the caller's
# context. The caller reads or greps the file for the part it needs.
INLINE_LIMIT = int(os.environ.get("META_MCP_INLINE_LIMIT", "20000"))
PREVIEW_CHARS = 2000
SPILL_DIR = Path(os.environ.get("META_MCP_SPILL_DIR", Path(tempfile.gettempdir()) / "meta-mcp"))

mcp = FastMCP("meta-mcp")

_registry: dict[str, SubMcp] = {}
_descriptions: dict[str, str] = {}


def _load_config() -> None:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.toml not found at {CONFIG_PATH}. "
            "Copy config.toml.example to config.toml and edit it."
        )
    with open(CONFIG_PATH, "rb") as f:
        config = tomli.load(f)
    for entry in config.get("mcps", []):
        name = entry["name"]
        _descriptions[name] = entry["description"]
        _registry[name] = SubMcp(
            name=name,
            command=entry["command"],
            env=entry.get("env", {}),
        )


def _spill_if_large(text: str, name: str, tool: str) -> str:
    if len(text) <= INLINE_LIMIT:
        return text
    SPILL_DIR.mkdir(parents=True, exist_ok=True)
    safe_tool = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool)
    path = SPILL_DIR / f"{name}-{safe_tool}-{time.strftime('%Y%m%d-%H%M%S')}-{time.monotonic_ns()}.json"
    path.write_text(text)
    return (
        f"Result too large to return inline ({len(text):,} characters). "
        f"Full result written to {path} — read or grep that file for what you need.\n"
        f"First {PREVIEW_CHARS:,} characters:\n{text[:PREVIEW_CHARS]}"
    )


async def _shutdown_all() -> None:
    await asyncio.gather(*[sub.shutdown() for sub in _registry.values()])


def _install_shutdown_handler() -> None:
    loop = asyncio.get_event_loop()

    def _handle(sig):
        logger.info("Received %s, shutting down sub-MCPs", sig.name)
        loop.create_task(_shutdown_all())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: _handle(s))


@mcp.tool()
def list_mcps() -> str:
    """List all configured sub-MCPs with their one-line descriptions."""
    if not _registry:
        return "No sub-MCPs configured."
    lines = [f"{name}: {desc}" for name, desc in _descriptions.items()]
    return "\n".join(lines)


@mcp.tool()
async def describe_mcp(name: str) -> str:
    """
    Return the available tools and their JSON schemas for a named sub-MCP.
    Spawns the sub-MCP process if it is not already running.
    Pass the returned schema to call_mcp as the args argument.
    """
    sub = _registry.get(name)
    if sub is None:
        available = ", ".join(_registry.keys()) or "none"
        return f"Unknown sub-MCP '{name}'. Available: {available}"
    try:
        tools = await sub.list_tools()
    except Exception as exc:
        return f"Failed to connect to sub-MCP '{name}': {exc}"
    if not tools:
        return f"Sub-MCP '{name}' reports no tools."
    return _spill_if_large(json.dumps(tools, indent=2), name, "describe_mcp")


@mcp.tool()
async def call_mcp(name: str, tool: str, args: dict) -> str:
    """
    Call a tool on a named sub-MCP.
    Use describe_mcp first to discover available tools and their required args.
    """
    sub = _registry.get(name)
    if sub is None:
        available = ", ".join(_registry.keys()) or "none"
        return f"Unknown sub-MCP '{name}'. Available: {available}"
    try:
        result = await sub.call_tool(tool, args)
    except Exception as exc:
        return f"Error calling '{tool}' on sub-MCP '{name}': {exc}"
    if result is None:
        return "(no result)"
    text = json.dumps(result) if not isinstance(result, str) else result
    return _spill_if_large(text, name, tool)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    _load_config()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.call_soon(lambda: _install_shutdown_handler())
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
