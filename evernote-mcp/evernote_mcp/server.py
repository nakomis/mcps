#!/usr/bin/env python3
"""Evernote MCP Server — read-only access to notes via exported .enex files."""

import os
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from lxml import etree
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("evernote-mcp")

# ── Config ────────────────────────────────────────────────────────────────────

def _enex_dir() -> Path:
    raw = os.environ.get("EVERNOTE_ENEX_DIR", str(Path.home() / "Downloads"))
    return Path(raw).expanduser()


# ── ENEX parsing ──────────────────────────────────────────────────────────────

def _parse_enex(path: Path) -> list[dict]:
    """Parse an .enex file and return a list of note dicts."""
    # lxml with recover=True handles CDATA quirks and malformed content gracefully.
    # no_network + load_dtd=False prevents fetching the external Evernote DTD.
    parser = etree.XMLParser(recover=True, load_dtd=False, no_network=True)
    raw = path.read_bytes()
    root = etree.fromstring(raw, parser=parser)
    notes = []
    for note_el in root.findall("note"):
        def txt(tag):
            el = note_el.find(tag)
            return (el.text or "").strip() if el is not None else ""
        tags = [t.text.strip() for t in note_el.findall("tag") if t.text]
        notes.append({
            "title": txt("title"),
            "created": txt("created"),
            "updated": txt("updated"),
            "tags": tags,
            "content_enml": txt("content"),
        })
    return notes


def _enml_to_text(enml: str) -> str:
    """Convert ENML content to readable plain text."""
    if not enml:
        return ""
    soup = BeautifulSoup(enml, "lxml-xml")
    for tag in soup.find_all("en-media"):
        tag.replace_with(f"[attachment: {tag.get('type', 'file')}]")
    # Preserve some structure: div/p → newlines
    for tag in soup.find_all(["div", "p", "br"]):
        tag.insert_before("\n")
    return soup.get_text(separator="", strip=False).strip()


def _available_notebooks() -> dict[str, Path]:
    """Return {notebook_name: path} for all .enex files in ENEX_DIR."""
    d = _enex_dir()
    if not d.exists():
        return {}
    return {p.stem: p for p in sorted(d.glob("*.enex"))}


def _load_notebook(name: str) -> tuple[list[dict], str] | tuple[None, str]:
    """Load notes from a named notebook. Returns (notes, error_message)."""
    notebooks = _available_notebooks()
    # Case-insensitive match
    match = next((p for n, p in notebooks.items() if n.lower() == name.lower()), None)
    if match is None:
        available = ", ".join(notebooks.keys()) or "(none found)"
        return None, f"Notebook '{name}' not found. Available: {available}"
    return _parse_enex(match), ""


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_notebooks() -> str:
    """List all available Evernote notebooks (exported .enex files)."""
    notebooks = _available_notebooks()
    if not notebooks:
        return f"No .enex files found in {_enex_dir()}"
    lines = []
    for name, path in notebooks.items():
        notes = _parse_enex(path)
        lines.append(f"- {name}  ({len(notes)} notes)")
    return f"{len(notebooks)} notebooks in {_enex_dir()}:\n" + "\n".join(lines)


@mcp.tool()
def list_notes(notebook: str) -> str:
    """List all notes in a notebook.

    Args:
        notebook: Notebook name, e.g. 'BBC' (case-insensitive, without .enex).
    """
    notes, err = _load_notebook(notebook)
    if notes is None:
        return err
    if not notes:
        return f"No notes in '{notebook}'."
    lines = [f"- {n['title']}" + (f"  [tags: {', '.join(n['tags'])}]" if n['tags'] else "")
             for n in notes]
    return f"{len(notes)} notes in '{notebook}':\n" + "\n".join(lines)


@mcp.tool()
def search_notes(query: str, notebook: Optional[str] = None) -> str:
    """Full-text search across notes. Searches titles and content.

    Args:
        query: Text to search for (case-insensitive).
        notebook: Restrict to this notebook (optional). Searches all notebooks if omitted.
    """
    q = query.lower()
    notebooks = _available_notebooks()
    if not notebooks:
        return f"No .enex files found in {_enex_dir()}"

    if notebook:
        # Restrict to one notebook
        match_name = next((n for n in notebooks if n.lower() == notebook.lower()), None)
        if match_name is None:
            available = ", ".join(notebooks.keys())
            return f"Notebook '{notebook}' not found. Available: {available}"
        search_in = {match_name: notebooks[match_name]}
    else:
        search_in = notebooks

    results = []
    for nb_name, path in search_in.items():
        for note in _parse_enex(path):
            text = _enml_to_text(note["content_enml"]).lower()
            if q in note["title"].lower() or q in text:
                results.append(f"- [{nb_name}] {note['title']}")

    if not results:
        scope = f"'{notebook}'" if notebook else "all notebooks"
        return f"No notes matching '{query}' in {scope}."
    return f"{len(results)} results for '{query}':\n" + "\n".join(results)


@mcp.tool()
def get_note(notebook: str, title: str) -> str:
    """Get the full text content of a note.

    Args:
        notebook: Notebook name (case-insensitive, without .enex).
        title: Exact or partial note title (case-insensitive).
    """
    notes, err = _load_notebook(notebook)
    if notes is None:
        return err

    t = title.lower()
    # Prefer exact match, fall back to partial
    exact = [n for n in notes if n["title"].lower() == t]
    partial = [n for n in notes if t in n["title"].lower()]
    matches = exact or partial

    if not matches:
        available = ", ".join(f"'{n['title']}'" for n in notes[:10])
        return f"No note matching '{title}' in '{notebook}'. First notes: {available}"
    if len(matches) > 1 and not exact:
        titles = ", ".join(f"'{n['title']}'" for n in matches)
        return f"Multiple matches for '{title}': {titles}. Please be more specific."

    note = matches[0]
    text = _enml_to_text(note["content_enml"])
    header = f"# {note['title']}"
    if note["tags"]:
        header += f"  [tags: {', '.join(note['tags'])}]"
    header += f"\n*Updated: {note['updated'][:8]}*\n"
    return f"{header}\n{text}"


@mcp.tool()
def get_all_notes(notebook: str) -> str:
    """Get the full text content of every note in a notebook concatenated.

    Useful for getting a complete picture of a notebook in one call.

    Args:
        notebook: Notebook name (case-insensitive, without .enex).
    """
    notes, err = _load_notebook(notebook)
    if notes is None:
        return err
    if not notes:
        return f"No notes in '{notebook}'."

    parts = []
    for note in notes:
        text = _enml_to_text(note["content_enml"])
        header = f"## {note['title']}"
        if note["tags"]:
            header += f"  [tags: {', '.join(note['tags'])}]"
        parts.append(f"{header}\n\n{text}")

    return f"# {notebook} — all {len(notes)} notes\n\n" + "\n\n---\n\n".join(parts)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
