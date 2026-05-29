# Stigmergic Continuity MCP

Experimental MCP server for cross-conversation continuity via a stigmergic salience field of relations.

This project stores weak conceptual links that separate conversations independently rediscover. It is not a factual memory system, vector database, knowledge graph, or source of truth. A strong relation means only this:

```text
This relation keeps reappearing.
```

The server exposes a small Model Context Protocol (MCP) surface backed by SQLite. Agents call `new_session` once, inspect the current field with `sense`, and optionally reinforce relation pairs with `add_gradient`.

## Status

This is an early research prototype.

Implemented:

- MCP server using FastMCP
- SQLite persistence
- unordered relation keys
- same-conversation duplicate protection
- half-life decay and pruning
- top-gradient sampling with temperature
- console script entrypoint

Not currently included:

- tests
- packaged example clients
- report/query tooling
- authentication or multi-tenant isolation
- production deployment hardening

## Requirements

- Python 3.11 or newer
- SQLite, via Python's standard `sqlite3` module
- `fastmcp>=3.3.1`

The Python dependency is declared in [pyproject.toml](pyproject.toml).

## Installation

From the repository root:

```powershell
python -m pip install -e .
```

This installs the `stigmergic-mcp` console command from:

```text
stigmergic_mcp.server:main
```

## How To Run

Run the MCP server over stdio:

```powershell
stigmergic-mcp --db-path .\stigmergic.db
```

The `--db-path` argument is required. It points to the SQLite database file used to store the field.

Useful flags:

```powershell
stigmergic-mcp --db-path .\stigmergic.db --clear
stigmergic-mcp --db-path .\stigmergic.db --http
```

- `--clear` deletes the database file on startup before recreating the schema.
- `--http` runs FastMCP with HTTP transport instead of stdio.

## MCP Client Configuration

Example MCP configuration:

```json
{
  "mcpServers": {
    "stigmergic": {
      "command": "stigmergic-mcp",
      "args": [
        "--db-path",
        "ABSOLUTE_PATH to database file"
      ]
    }
  }
}
```

Adjust the database path for your machine. 

## Tools

### `new_session()`

Call once at the start of a conversation.

Returns a `conv_id` and the current field:

```json
{
  "conv_id": "uuid",
  "sense": {
    "vocabulary": [],
    "gradients": []
  }
}
```

Reuse the returned `conv_id` for every `add_gradient` call in that conversation. Calling `new_session` repeatedly makes one conversation look like several independent conversations, which corrupts the signal.

### `sense(conv_id, temperature = 1.0)`

Reads the field.

```json
{
  "vocabulary": ["conversation", "decay", "recurrence"],
  "gradients": [
    {
      "a": "conversation",
      "b": "recurrence",
      "strength": 1.0,
      "trend": "..."
    }
  ]
}
```

In the current code, `conv_id` is accepted as part of the MCP tool contract but is not used by the database query.

Fields:

- `vocabulary`: every endpoint currently present on live edges.
- `gradients`: up to 10 live relations sampled by strength and temperature.
- `strength`: decayed relation strength, rounded to four decimal places.
- `trend`: server-provided marker for recent activity compared with the previous half-life window.

Temperature changes the gradient view:

- lower values make stronger edges more likely to dominate
- higher values make the sample broader and more exploratory

### `add_gradient(a, b, conv_id)`

Records a relation between two endpoint strings.

```json
{
  "status": "recorded",
  "edge": {
    "a": "conversation",
    "b": "recurrence"
  }
}
```

The relation key is unordered. `A/B` and `B/A` refer to the same edge.

If the same conversation records the same edge twice, the server returns:

```json
{
  "error": "link already recorded this conversation"
}
```

That behavior is intentional. A relation should grow because different conversations rediscover it, not because one conversation repeats itself.

## Data Model

The SQLite schema is created automatically on startup.

```sql
CREATE TABLE IF NOT EXISTS edges (
    edge_id    INTEGER PRIMARY KEY,
    endpoint_a TEXT NOT NULL,
    endpoint_b TEXT NOT NULL,
    UNIQUE(endpoint_a, endpoint_b)
);

CREATE TABLE IF NOT EXISTS hits (
    edge_id   INTEGER NOT NULL REFERENCES edges(edge_id) ON DELETE CASCADE,
    conv_id   TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    PRIMARY KEY (edge_id, conv_id)
);
```

`edges` stores unique unordered endpoint pairs. `hits` stores one timestamped hit per edge per conversation.

## Decay And Pruning

The field is designed to forget by neglect.

Current constants are defined in [stigmergic_mcp/db.py](stigmergic_mcp/db.py):

- `HALF_LIFE_DAYS = 21`
- `FLOOR = 0.25`
- `EPSILON = 0.01`
- `MAX_EDGES = 5000`

On `new_session` and `sense`, the server prunes hits that have decayed below epsilon and removes dead edges. If the edge count exceeds `MAX_EDGES`, the weakest edge is evicted after a new gradient is added.

## Repository Layout

```text
stigmergic-mcp/
|-- stigmergic_mcp/
|   |-- __init__.py
|   |-- db.py
|   `-- server.py
|-- docs/
|-- mcps.json
|-- pyproject.toml
|-- README.md
`-- SKILL.md
```

Key files:

- [stigmergic_mcp/server.py](stigmergic_mcp/server.py): FastMCP tools and CLI argument parsing.
- [stigmergic_mcp/db.py](stigmergic_mcp/db.py): SQLite schema, persistence, decay, pruning, and sampling.
- [pyproject.toml](pyproject.toml): package metadata, dependencies, and console script.
- [mcps.json](mcps.json): example MCP client configuration.

## Development

Install the package in editable mode:

```powershell
python -m pip install -e .
```

Run a syntax check:

```powershell
python -m compileall stigmergic_mcp
```

Run with a disposable database:

```powershell
stigmergic-mcp --db-path .\scratch.db --clear
```

There is no test suite yet. If you add one, a small set of database-level tests around duplicate hits, unordered keys, decay, pruning, and sampling would be the highest-value starting point.

## Conceptual Model

The system stores relations, not standalone concepts.

```text
conversation A: relational-time <-> stigmergy
conversation B: relational-time <-> stigmergy
conversation C: relational-time <-> stigmergy
```

The field interprets this as a recurring attractor. It does not claim that the relation is true.

Good use cases:

- cross-LLM continuity
- multi-agent research flow
- creative or theoretical project direction
- lightweight behavioral memory
- repeated conceptual orientation across otherwise separate chats

Poor use cases:

- factual verification
- audit trails
- legal, medical, or financial decisions
- formal proof systems
- security-critical autonomy
- source-of-truth storage

## Write Discipline

Before writing a gradient:

1. Call `sense`.
2. Check `vocabulary`.
3. Reuse existing endpoint strings when they fit.
4. Add only relations that another unrelated conversation might independently rediscover.

Do not log passing mentions, one-off associations, or facts that need a source of truth.

## License

AGPL-3.0-or-later. 
