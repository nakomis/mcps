"""
JSON-RPC client for communicating with a sub-MCP server over stdio.

Handles the MCP initialise handshake, tools/list, and tools/call.
Keeps the subprocess alive between calls.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_next_id = 0


def _id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


@dataclass
class SubMcp:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    process: asyncio.subprocess.Process | None = None
    initialised: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def _send(self, method: str, params: dict | None = None) -> Any:
        msg = {"jsonrpc": "2.0", "id": _id(), "method": method}
        if params:
            msg["params"] = params
        line = json.dumps(msg) + "\n"
        self.process.stdin.write(line.encode())
        await self.process.stdin.drain()

        while True:
            raw = await self.process.stdout.readline()
            if not raw:
                raise RuntimeError(f"sub-MCP '{self.name}' closed stdout unexpectedly")
            try:
                response = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if response.get("id") == msg["id"]:
                if "error" in response:
                    raise RuntimeError(f"sub-MCP error: {response['error']}")
                return response.get("result")

    async def _notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        line = json.dumps(msg) + "\n"
        self.process.stdin.write(line.encode())
        await self.process.stdin.drain()

    async def ensure_started(self) -> None:
        async with self._lock:
            if self.process is None or self.process.returncode is not None:
                await self._spawn()
            if not self.initialised:
                await self._initialise()

    async def _spawn(self) -> None:
        logger.info("Spawning sub-MCP '%s': %s", self.name, self.command)
        import os
        env = {**os.environ, **self.env} if self.env else None
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self.initialised = False

    async def _initialise(self) -> None:
        await self._send(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "meta-mcp", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized")
        self.initialised = True

    async def list_tools(self) -> list[dict]:
        await self.ensure_started()
        result = await self._send("tools/list")
        return result.get("tools", []) if result else []

    async def call_tool(self, tool: str, args: dict) -> Any:
        await self.ensure_started()
        result = await self._send("tools/call", {"name": tool, "arguments": args})
        return result

    async def shutdown(self) -> None:
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
