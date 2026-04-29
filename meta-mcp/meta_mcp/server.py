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
import signal
import sys
from pathlib import Path

import tomli
from mcp.server.fastmcp import FastMCP

from .proxy import SubMcp

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.toml"

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
        _registry[name] = SubMcp(name=name, command=entry["command"])


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
    return json.dumps(tools, indent=2)


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
    return json.dumps(result) if not isinstance(result, str) else result


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    _load_config()

    async def _run():
        _install_shutdown_handler()
        await mcp.run_async()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
