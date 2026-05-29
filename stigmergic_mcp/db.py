"""SQLite persistence layer — schema init and all reads/writes."""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────
HALF_LIFE_DAYS = 21
FLOOR = 0.25
EPSILON = 0.01
MAX_EDGES = 5000


def _db_path_holder():
    """Module-level mutable so server.py can set it before first use."""
    pass


_path: Path | None = None


def init(path: Path, clear: bool = False) -> None:
    global _path
    _path = path
    if clear and path.exists():
        path.unlink()
    with _connect() as con:
        con.executescript("""
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

            CREATE INDEX IF NOT EXISTS idx_hits_edge ON hits(edge_id);
            CREATE INDEX IF NOT EXISTS idx_hits_ts   ON hits(ts);
        """)


@contextmanager
def _connect():
    con = sqlite3.connect(_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Decay helpers ─────────────────────────────────────────────────────────────

def _strength_expr(now: float) -> str:
    """SQL fragment: strength of a single edge given its hits rows."""
    return f"SUM(POWER(0.5, ({now} - ts) / (86400.0 * {HALF_LIFE_DAYS})))"


def _weight_expr(now: float, temperature: float) -> str:
    exponent = 1.0 / max(temperature, 1e-6)
    return f"POWER({_strength_expr(now)}, {exponent})"


# ── Pruning ───────────────────────────────────────────────────────────────────

def prune(con: sqlite3.Connection) -> None:
    """Remove epsilon-faded hits, then dead edges. Called at the start of sense."""
    import math
    now = time.time()
    # 0.5^(age/HALF_LIFE) < EPSILON  =>  age > HALF_LIFE * log2(1/EPSILON)
    cutoff_ts = now - HALF_LIFE_DAYS * 86400.0 * math.log2(1.0 / EPSILON)
    con.execute("DELETE FROM hits WHERE ts < ?", (cutoff_ts,))

    # Drop dead edges: strength < FLOOR
    # We need per-edge strength after the epsilon prune above.
    strength_sql = f"""
        DELETE FROM edges
        WHERE edge_id IN (
            SELECT edge_id FROM (
                SELECT edge_id,
                       {_strength_expr(now)} AS s
                FROM hits
                GROUP BY edge_id
            )
            WHERE s < {FLOOR}
        )
    """
    con.execute(strength_sql)
    # Also remove edges that have zero hits remaining (all hits were epsilon-pruned)
    con.execute("""
        DELETE FROM edges
        WHERE edge_id NOT IN (SELECT DISTINCT edge_id FROM hits)
    """)


# ── sense ─────────────────────────────────────────────────────────────────────

def sense_data(con: sqlite3.Connection, temperature: float) -> dict:
    now = time.time()
    half = HALF_LIFE_DAYS * 86400.0

    # vocabulary: all endpoints on live edges
    vocab_rows = con.execute("""
        SELECT DISTINCT endpoint_a AS ep FROM edges
        UNION
        SELECT DISTINCT endpoint_b AS ep FROM edges
        ORDER BY ep
    """).fetchall()
    vocabulary = [r["ep"] for r in vocab_rows]

    # per-edge: strength, weight, trend
    recent_start = now - half
    prior_start = now - 2 * half

    edge_rows = con.execute(f"""
        SELECT
            e.edge_id,
            e.endpoint_a AS a,
            e.endpoint_b AS b,
            {_strength_expr(now)} AS strength,
            POWER({_strength_expr(now)}, {1.0 / max(temperature, 1e-6)}) AS weight,
            SUM(CASE WHEN h.ts >= {recent_start} THEN 1 ELSE 0 END) AS recent_convs,
            SUM(CASE WHEN h.ts >= {prior_start} AND h.ts < {recent_start} THEN 1 ELSE 0 END) AS prior_convs
        FROM edges e
        JOIN hits h ON h.edge_id = e.edge_id
        GROUP BY e.edge_id
    """).fetchall()

    if not edge_rows:
        return {"vocabulary": vocabulary, "gradients": []}

    edges = []
    for r in edge_rows:
        recent = r["recent_convs"]
        prior = r["prior_convs"]
        trend = "↑" if recent > prior else ("↓" if recent < prior else "→")
        edges.append({
            "a": r["a"],
            "b": r["b"],
            "strength": round(r["strength"], 4),
            "weight": r["weight"],
            "trend": trend,
        })

    # weighted random sample of top 10 (without replacement)
    import random
    k = min(10, len(edges))
    weights = [e["weight"] for e in edges]
    sampled = random.choices(edges, weights=weights, k=k)
    # deduplicate preserving order (random.choices can repeat)
    seen = set()
    gradients = []
    for e in sampled:
        key = (e["a"], e["b"])
        if key not in seen:
            seen.add(key)
            gradients.append({"a": e["a"], "b": e["b"], "strength": e["strength"], "trend": e["trend"]})
    # if dedup dropped some, fill from remaining sorted by weight
    if len(gradients) < k:
        remaining = sorted(
            [e for e in edges if (e["a"], e["b"]) not in seen],
            key=lambda e: e["weight"],
            reverse=True,
        )
        for e in remaining:
            if len(gradients) >= k:
                break
            gradients.append({"a": e["a"], "b": e["b"], "strength": e["strength"], "trend": e["trend"]})

    return {"vocabulary": vocabulary, "gradients": gradients}


# ── add_gradient ──────────────────────────────────────────────────────────────

def add_gradient(a: str, b: str, conv_id: str) -> dict:
    a, b = sorted([a.strip(), b.strip()])
    now = time.time()
    with _connect() as con:
        # get-or-create edge
        con.execute(
            "INSERT OR IGNORE INTO edges (endpoint_a, endpoint_b) VALUES (?, ?)",
            (a, b),
        )
        row = con.execute(
            "SELECT edge_id FROM edges WHERE endpoint_a = ? AND endpoint_b = ?",
            (a, b),
        ).fetchone()
        edge_id = row["edge_id"]

        # record hit — PK violation means same-conv re-hit
        try:
            con.execute(
                "INSERT INTO hits (edge_id, conv_id, ts) VALUES (?, ?, ?)",
                (edge_id, conv_id, now),
            )
        except sqlite3.IntegrityError:
            return {"error": "link already recorded this conversation"}

        # evict only if this was a brand-new edge and we're over MAX_EDGES
        edge_count = con.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]
        if edge_count > MAX_EDGES:
            _evict_weakest(con, now)

    return {"status": "recorded", "edge": {"a": a, "b": b}}


def _evict_weakest(con: sqlite3.Connection, now: float) -> None:
    row = con.execute(f"""
        SELECT e.edge_id, {_strength_expr(now)} AS s
        FROM edges e
        JOIN hits h ON h.edge_id = e.edge_id
        GROUP BY e.edge_id
        ORDER BY s ASC
        LIMIT 1
    """).fetchone()
    if row:
        con.execute("DELETE FROM edges WHERE edge_id = ?", (row["edge_id"],))
