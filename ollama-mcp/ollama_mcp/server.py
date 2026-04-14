#!/usr/bin/env python3
"""Ollama MCP Server — embeddings and local LLM generation via Ollama."""

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ollama-mcp")

OLLAMA_BASE = "http://localhost:11434"


def _client() -> httpx.Client:
    return httpx.Client(base_url=OLLAMA_BASE, timeout=60)


# ── Embeddings ────────────────────────────────────────────────────────────────

@mcp.tool()
def embed(text: str, model: str = "nomic-embed-text") -> list[float]:
    """
    Generate an embedding vector for the given text using a local Ollama model.

    Args:
        text:  The text to embed.
        model: Ollama model to use (default: nomic-embed-text).

    Returns:
        A list of floats representing the embedding vector.
    """
    with _client() as client:
        r = client.post("/api/embeddings", json={"model": model, "prompt": text})
        r.raise_for_status()
        return r.json()["embedding"]


@mcp.tool()
def embed_batch(texts: list[str], model: str = "nomic-embed-text") -> list[list[float]]:
    """
    Generate embedding vectors for a list of texts.

    Args:
        texts: List of strings to embed.
        model: Ollama model to use (default: nomic-embed-text).

    Returns:
        A list of embedding vectors, one per input text.
    """
    with _client() as client:
        embeddings = []
        for text in texts:
            r = client.post("/api/embeddings", json={"model": model, "prompt": text})
            r.raise_for_status()
            embeddings.append(r.json()["embedding"])
        return embeddings


# ── Generation ────────────────────────────────────────────────────────────────

@mcp.tool()
def generate(prompt: str, model: str = "gemma3:4b", system: str = "") -> str:
    """
    Generate a text response from a local Ollama LLM.

    Args:
        prompt: The prompt to send to the model.
        model:  Ollama model to use (default: gemma3:4b).
        system: Optional system prompt.

    Returns:
        The generated text response.
    """
    payload: dict = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    with _client() as client:
        r = client.post("/api/generate", json=payload)
        r.raise_for_status()
        return r.json()["response"]


# ── Models ────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_models() -> list[dict]:
    """
    List all locally available Ollama models.

    Returns:
        A list of dicts with name, size, and modified date.
    """
    with _client() as client:
        r = client.get("/api/tags")
        r.raise_for_status()
        return [
            {
                "name": m["name"],
                "size_gb": round(m["size"] / 1e9, 2),
                "modified": m["modified_at"],
            }
            for m in r.json().get("models", [])
        ]


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
