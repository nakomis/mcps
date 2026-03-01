#!/usr/bin/env python3
"""Trello MCP Server — read/write access to Trello boards, lists, and cards."""

import os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("trello-mcp")

# ── Config ────────────────────────────────────────────────────────────────────

BASE = "https://api.trello.com/1"


def _auth() -> dict:
    key = os.environ.get("TRELLO_API_KEY", "")
    token = os.environ.get("TRELLO_TOKEN", "")
    if not key or not token:
        raise RuntimeError("TRELLO_API_KEY and TRELLO_TOKEN environment variables must be set")
    return {"key": key, "token": token}


def _get(path: str, **params) -> dict | list:
    r = httpx.get(f"{BASE}{path}", params={**_auth(), **params}, timeout=10)
    r.raise_for_status()
    return r.json()


def _post(path: str, **data) -> dict:
    r = httpx.post(f"{BASE}{path}", params=_auth(), json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _put(path: str, **data) -> dict:
    r = httpx.put(f"{BASE}{path}", params=_auth(), json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _delete(path: str) -> dict:
    r = httpx.delete(f"{BASE}{path}", params=_auth(), timeout=10)
    r.raise_for_status()
    return r.json()


# ── Boards ────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_boards() -> list[dict]:
    """List all Trello boards accessible to the authenticated user."""
    boards = _get("/members/me/boards", fields="id,name,url,closed")
    return [{"id": b["id"], "name": b["name"], "url": b["url"], "closed": b["closed"]}
            for b in boards]


@mcp.tool()
def get_board(board_id: str) -> dict:
    """Get details of a specific board by ID or short URL ID."""
    return _get(f"/boards/{board_id}", fields="id,name,desc,url,closed")


# ── Lists ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_lists(board_id: str) -> list[dict]:
    """Get all lists on a board."""
    lists = _get(f"/boards/{board_id}/lists", fields="id,name,pos,closed")
    return [{"id": l["id"], "name": l["name"], "pos": l["pos"]} for l in lists
            if not l.get("closed")]


@mcp.tool()
def create_list(board_id: str, name: str, pos: str = "bottom") -> dict:
    """Create a new list on a board. pos can be 'top', 'bottom', or a number."""
    result = _post(f"/boards/{board_id}/lists", name=name, pos=pos)
    return {"id": result["id"], "name": result["name"]}


# ── Cards ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_cards(list_id: str) -> list[dict]:
    """Get all cards in a list."""
    cards = _get(f"/lists/{list_id}/cards",
                 fields="id,name,desc,pos,url,labels,due,closed")
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "desc": c["desc"],
            "pos": c["pos"],
            "url": c["url"],
            "labels": [lb["name"] for lb in c.get("labels", [])],
            "due": c.get("due"),
        }
        for c in cards if not c.get("closed")
    ]


@mcp.tool()
def get_card(card_id: str) -> dict:
    """Get full details of a card."""
    c = _get(f"/cards/{card_id}", fields="id,name,desc,pos,url,labels,due,idList,idBoard")
    return {
        "id": c["id"],
        "name": c["name"],
        "desc": c["desc"],
        "url": c["url"],
        "list_id": c["idList"],
        "board_id": c["idBoard"],
        "labels": [lb["name"] for lb in c.get("labels", [])],
        "due": c.get("due"),
    }


@mcp.tool()
def create_card(list_id: str, name: str, desc: str = "", pos: str = "bottom") -> dict:
    """Create a card in a list. pos can be 'top', 'bottom', or a number."""
    result = _post("/cards", idList=list_id, name=name, desc=desc, pos=pos)
    return {"id": result["id"], "name": result["name"], "url": result["url"]}


@mcp.tool()
def update_card(card_id: str, name: str = None, desc: str = None,
                pos: str = None, list_id: str = None, due: str = None) -> dict:
    """
    Update a card's name, description, position, list (move), or due date.
    Only fields you pass will be changed. pos can be 'top', 'bottom', or a number.
    due should be an ISO 8601 datetime string, or null to clear it.
    """
    data = {}
    if name is not None:
        data["name"] = name
    if desc is not None:
        data["desc"] = desc
    if pos is not None:
        data["pos"] = pos
    if list_id is not None:
        data["idList"] = list_id
    if due is not None:
        data["due"] = due
    result = _put(f"/cards/{card_id}", **data)
    return {"id": result["id"], "name": result["name"], "url": result["url"]}


@mcp.tool()
def move_card_to_top(card_id: str) -> dict:
    """Move a card to the top of its current list."""
    return update_card(card_id, pos="top")


@mcp.tool()
def archive_card(card_id: str) -> dict:
    """Archive (close) a card."""
    result = _put(f"/cards/{card_id}", closed=True)
    return {"id": result["id"], "name": result["name"], "archived": True}


@mcp.tool()
def search_cards(query: str, board_id: str = None) -> list[dict]:
    """
    Search for cards by text. Optionally restrict to a specific board_id.
    Returns matching cards with id, name, url, and board name.
    """
    params = {"query": query, "modelTypes": "cards", "cards_limit": 20,
              "card_fields": "id,name,url,idBoard,idList"}
    if board_id:
        params["idBoards"] = board_id
    results = _get("/search", **params)
    return [
        {"id": c["id"], "name": c["name"], "url": c["url"]}
        for c in results.get("cards", [])
    ]


# ── Labels ────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_labels(board_id: str) -> list[dict]:
    """Get all labels defined on a board."""
    labels = _get(f"/boards/{board_id}/labels", fields="id,name,color")
    return [{"id": lb["id"], "name": lb["name"], "color": lb["color"]}
            for lb in labels if lb.get("name")]


@mcp.tool()
def add_label_to_card(card_id: str, label_id: str) -> dict:
    """Add a label to a card."""
    return _post(f"/cards/{card_id}/idLabels", value=label_id)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
