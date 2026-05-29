"""Stigmergic Goal-Field MCP server."""

import argparse
import time
from pathlib import Path
from uuid import uuid4

import fastmcp

from . import db

mcp = fastmcp.FastMCP("stigmergic")

DEFAULT_TEMPERATURE = 1.0


@mcp.tool()
def new_session() -> dict:
    """Call this **exactly once**, at the very start of a conversation. It returns your `conv_id`.
    Reuse that same `conv_id` for every `add_gradient` call this conversation.
    **Do not call this again** to get a fresh look — use `sense` for that.
    Calling it twice makes you look like two different people to the field and corrupts the signal."""
    conv_id = str(uuid4())
    with db._connect() as con:
        db.prune(con)
        field = db.sense_data(con, DEFAULT_TEMPERATURE)
    return {"conv_id": conv_id, "sense": field}


@mcp.tool()
def sense(conv_id: str, temperature: float = DEFAULT_TEMPERATURE) -> dict:
    """Look at the field. Call it whenever useful.
    `vocabulary` is every word currently in play — **before you write a new endpoint, check this list
    and reuse an existing word if one fits**, so you don't split one relation across two spellings.
    `gradients` is the top relations right now, hottest first."""
    with db._connect() as con:
        db.prune(con)
        return db.sense_data(con, temperature)


@mcp.tool()
def add_gradient(a: str, b: str, conv_id: str) -> dict:
    """Record a relation between two ideas.
    Only assert a link you'd expect **a different, unrelated conversation to also independently reach.**
    Don't log passing mentions — log relations a stranger would re-find.
    If you get "already recorded this conversation," it's counted; move on."""
    a_s, b_s = sorted([a.strip(), b.strip()])
    return db.add_gradient(a_s, b_s, conv_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stigmergic Goal-Field MCP server")
    parser.add_argument("--db-path", required=True, type=Path, help="Path to the SQLite database file")
    parser.add_argument("--clear", action="store_true", help="Wipe the database on startup before serving")
    parser.add_argument("--http", action="store_true", help="Serve using HTTP transport instead of stdio")
    # fastmcp passes unknown args through; parse only ours
    args, _ = parser.parse_known_args()

    db.init(args.db_path, clear=args.clear)
    mcp.run(transport="http" if args.http else "stdio")


if __name__ == "__main__":
    main()
