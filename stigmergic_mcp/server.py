"""Stigmergic Goal-Field MCP server."""

import argparse
import time
from pathlib import Path
from typing import Literal
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
    Calling it twice makes you look like two different people to the field and corrupts the signal.    
    The initial sense snapshot uses `scope=["GLOBAL"]`."""
    conv_id = str(uuid4())
    with db._connect() as con:
        db._register_session(con, conv_id)
        db.prune(con)
        field = db.sense_data(
            con,
            conv_id,
            DEFAULT_TEMPERATURE,
            include=["trend", "age"],
            global_power=1.0,
            turn_power=0.0,
            persistence_power=0.0,
        )
    return {"conv_id": conv_id, "sense": field}


@mcp.tool()
def sense(
    conv_id: str,
    temperature: float | None = DEFAULT_TEMPERATURE, 
    include: list[Literal["trend", "age", "support", "trace", "lens", "cross"]] | None = None,
    global_power: float | None = None,
    turn_power: float | None = None,
    persistence_power: float | None = None,
    scope: list[Literal["GLOBAL", "TURN"]] | None = None,
) -> dict:
    """View the current state of the field. Call it whenever useful. v3
    ## Args
    `temperature` controls how sharply strength affects sampling.
    `global_power` controls wall-clock recency pressure.
    `turn_power` controls local same-conversation tick recency pressure.
    `persistence_power` controls how much bounded same-conversation recurrence affects visibility.
    Pass `include=["trend","age","support","trace","lens","cross"]` for optional markers.
    Legacy `scope=["GLOBAL"]`, `scope=["TURN"]`, or both remains a shorthand for global/turn powers.
    ## Response
    `vocabulary` is every word currently in play — **before you write a new endpoint, check this list
    and reuse an existing word if one fits**, so you don't split one relation across two spellings.
    `gradients` and `boundaries` are the top relations right now, hottest first.
    """
    with db._connect() as con:
        db.prune(con)
        return db.sense_data(
            con,
            conv_id,
            temperature,
            include=include,
            scope=scope,
            global_power=global_power,
            turn_power=turn_power,
            persistence_power=persistence_power,
        )


@mcp.tool()
def add_gradient(a: str, b: str, conv_id: str) -> dict:
    """Record a relation between two ideas.
    Gradients are the "rungs" of the field — they connect two endpoints and show a direction of pull.    
    Only assert a gradient you'd expect **a different, unrelated conversation to also independently reach.**
    Don't log passing mentions — log relations a stranger would re-find.
    Re-hits in the same conversation refresh that conversation's hit and advance its tick."""
    a_s, b_s = sorted([a.strip(), b.strip()])
    return db.add_gradient(a_s, b_s, conv_id)

@mcp.tool()
def add_boundary(a: str, b: str, conv_id: str) -> dict:
    """Record a boundary between two ideas.
    Boundaries are the "walls" of the field — they connect two endpoints and show a direction of push.
    Only assert a boundary you'd expect **a different, unrelated conversation to also independently reach.**
    Don't log passing mentions — log boundaries a stranger would re-find.
    Re-hits in the same conversation refresh that conversation's hit and advance its tick."""
    a_s, b_s = sorted([a.strip(), b.strip()])
    return db.add_boundary(a_s, b_s, conv_id)

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
