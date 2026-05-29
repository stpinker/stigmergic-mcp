"""SQLite persistence layer — schema init and all reads/writes."""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────
HALF_LIFE_DAYS = 21
FLOOR = 0.25
EPSILON = 0.1
MAX_GRADIENTS = 100
MAX_BOUNDARIES = 100


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
        _migrate_legacy_schema(con)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS gradients (
                gradient_id INTEGER PRIMARY KEY,
                endpoint_a TEXT NOT NULL,
                endpoint_b TEXT NOT NULL,
                UNIQUE(endpoint_a, endpoint_b)
            );

            CREATE TABLE IF NOT EXISTS boundaries (
                boundary_id INTEGER PRIMARY KEY,
                endpoint_a TEXT NOT NULL,
                endpoint_b TEXT NOT NULL,
                UNIQUE(endpoint_a, endpoint_b)
            );

            CREATE TABLE IF NOT EXISTS hits (
                gradient_id INTEGER NOT NULL REFERENCES gradients(gradient_id) ON DELETE CASCADE,
                conv_id   TEXT    NOT NULL,
                ts        REAL    NOT NULL,
                PRIMARY KEY (gradient_id, conv_id)
            );

            CREATE TABLE IF NOT EXISTS boundary_hits (
                boundary_id INTEGER NOT NULL REFERENCES boundaries(boundary_id) ON DELETE CASCADE,
                conv_id     TEXT    NOT NULL,
                ts          REAL    NOT NULL,
                PRIMARY KEY (boundary_id, conv_id)
            );

            CREATE INDEX IF NOT EXISTS idx_hits_gradient ON hits(gradient_id);
            CREATE INDEX IF NOT EXISTS idx_hits_ts   ON hits(ts);
            CREATE INDEX IF NOT EXISTS idx_boundary_hits_boundary ON boundary_hits(boundary_id);
            CREATE INDEX IF NOT EXISTS idx_boundary_hits_ts   ON boundary_hits(ts);
        """)


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(con: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(r["name"] == column_name for r in rows)


def _migrate_legacy_schema(con: sqlite3.Connection) -> None:
    """Rename legacy edge/edges SQL schema to gradient/gradients on startup."""
    if _table_exists(con, "edges") and not _table_exists(con, "gradients"):
        con.execute("ALTER TABLE edges RENAME TO gradients")

    if _table_exists(con, "gradients") and _column_exists(con, "gradients", "edge_id"):
        con.execute("ALTER TABLE gradients RENAME COLUMN edge_id TO gradient_id")

    if _table_exists(con, "hits") and _column_exists(con, "hits", "edge_id"):
        con.execute("ALTER TABLE hits RENAME COLUMN edge_id TO gradient_id")

    if _table_exists(con, "hits"):
        con.execute("DROP INDEX IF EXISTS idx_hits_edge")

    # If an older boundary schema used singular names, migrate it.
    if _table_exists(con, "boundary") and not _table_exists(con, "boundaries"):
        con.execute("ALTER TABLE boundary RENAME TO boundaries")

    if _table_exists(con, "boundary_hits") and _column_exists(con, "boundary_hits", "boundary_edge_id"):
        con.execute("ALTER TABLE boundary_hits RENAME COLUMN boundary_edge_id TO boundary_id")

    if _table_exists(con, "boundaries") and _column_exists(con, "boundaries", "boundary_edge_id"):
        con.execute("ALTER TABLE boundaries RENAME COLUMN boundary_edge_id TO boundary_id")


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
    """SQL fragment: strength of a single gradient given its hits rows."""
    return f"SUM(POWER(0.5, ({now} - ts) / (86400.0 * {HALF_LIFE_DAYS})))"


def _weight_expr(now: float, temperature: float) -> str:
    exponent = 1.0 / max(temperature, 1e-6)
    return f"POWER({_strength_expr(now)}, {exponent})"


# ── Pruning ───────────────────────────────────────────────────────────────────

def prune(con: sqlite3.Connection) -> None:
    """Remove epsilon-faded hits, then dead gradients. Called at the start of sense."""
    import math
    now = time.time()
    # 0.5^(age/HALF_LIFE) < EPSILON  =>  age > HALF_LIFE * log2(1/EPSILON)
    cutoff_ts = now - HALF_LIFE_DAYS * 86400.0 * math.log2(1.0 / EPSILON)
    con.execute("DELETE FROM hits WHERE ts < ?", (cutoff_ts,))
    con.execute("DELETE FROM boundary_hits WHERE ts < ?", (cutoff_ts,))

    # Drop dead gradients: strength < FLOOR
    # We need per-gradient strength after the epsilon prune above.
    strength_sql = f"""
        DELETE FROM gradients
        WHERE gradient_id IN (
            SELECT gradient_id FROM (
                SELECT gradient_id,
                       {_strength_expr(now)} AS s
                FROM hits
                GROUP BY gradient_id
            )
            WHERE s < {FLOOR}
        )
    """
    con.execute(strength_sql)

    boundary_strength_sql = f"""
        DELETE FROM boundaries
        WHERE boundary_id IN (
            SELECT boundary_id FROM (
                SELECT boundary_id,
                       {_strength_expr(now)} AS s
                FROM boundary_hits
                GROUP BY boundary_id
            )
            WHERE s < {FLOOR}
        )
    """
    con.execute(boundary_strength_sql)
    # Also remove gradients that have zero hits remaining (all hits were epsilon-pruned)
    con.execute("""
        DELETE FROM gradients
        WHERE gradient_id NOT IN (SELECT DISTINCT gradient_id FROM hits)
    """)
    con.execute("""
        DELETE FROM boundaries
        WHERE boundary_id NOT IN (SELECT DISTINCT boundary_id FROM boundary_hits)
    """)


# ── sense ─────────────────────────────────────────────────────────────────────

def sense_data(con: sqlite3.Connection, temperature: float) -> dict:
    now = time.time()
    half = HALF_LIFE_DAYS * 86400.0

    # vocabulary: all endpoints on live gradients or boundaries
    vocab_rows = con.execute("""
        SELECT DISTINCT endpoint_a AS ep FROM gradients
        UNION
        SELECT DISTINCT endpoint_b AS ep FROM gradients
        UNION
        SELECT DISTINCT endpoint_a AS ep FROM boundaries
        UNION
        SELECT DISTINCT endpoint_b AS ep FROM boundaries
        ORDER BY ep
    """).fetchall()
    vocabulary = [r["ep"] for r in vocab_rows]

    # per-gradient: strength, weight, trend
    recent_start = now - half
    prior_start = now - 2 * half

    gradient_rows = con.execute(f"""
        SELECT
            g.gradient_id,
            g.endpoint_a AS a,
            g.endpoint_b AS b,
            {_strength_expr(now)} AS strength,
            POWER({_strength_expr(now)}, {1.0 / max(temperature, 1e-6)}) AS weight,
            SUM(CASE WHEN h.ts >= {recent_start} THEN 1 ELSE 0 END) AS recent_convs,
            SUM(CASE WHEN h.ts >= {prior_start} AND h.ts < {recent_start} THEN 1 ELSE 0 END) AS prior_convs
        FROM gradients g
        JOIN hits h ON h.gradient_id = g.gradient_id
        GROUP BY g.gradient_id
    """).fetchall()

    boundary_rows = con.execute(f"""
        SELECT
            b.boundary_id,
            b.endpoint_a AS a,
            b.endpoint_b AS b,
            {_strength_expr(now)} AS strength,
            POWER({_strength_expr(now)}, {1.0 / max(temperature, 1e-6)}) AS weight,
            SUM(CASE WHEN bh.ts >= {recent_start} THEN 1 ELSE 0 END) AS recent_convs,
            SUM(CASE WHEN bh.ts >= {prior_start} AND bh.ts < {recent_start} THEN 1 ELSE 0 END) AS prior_convs
        FROM boundaries b
        JOIN boundary_hits bh ON bh.boundary_id = b.boundary_id
        GROUP BY b.boundary_id
    """).fetchall()

    if not gradient_rows and not boundary_rows:
        return {"vocabulary": vocabulary, "gradients": [], "boundaries": []}

    gradients = []
    for r in gradient_rows:
        recent = r["recent_convs"]
        prior = r["prior_convs"]
        trend = "↑" if recent > prior else ("↓" if recent < prior else "→")
        gradients.append({
            "a": r["a"],
            "b": r["b"],
            "strength": round(r["strength"], 4),
            "weight": r["weight"],
            "trend": trend,
        })

    boundaries = []
    for r in boundary_rows:
        recent = r["recent_convs"]
        prior = r["prior_convs"]
        trend = "↑" if recent > prior else ("↓" if recent < prior else "→")
        boundaries.append({
            "a": r["a"],
            "b": r["b"],
            "strength": round(r["strength"], 4),
            "weight": r["weight"],
            "trend": trend,
        })

    # weighted random sample of top 10 (without replacement)
    import random
    k = min(10, len(gradients))
    weights = [g["weight"] for g in gradients]
    if k == 0 or not gradients:
        sampled = []
    elif sum(weights) <= 0:
        sampled = gradients[:k]
    else:
        sampled = random.choices(gradients, weights=weights, k=k)
    # deduplicate preserving order (random.choices can repeat)
    seen = set()
    top_gradients = []
    for g in sampled:
        key = (g["a"], g["b"])
        if key not in seen:
            seen.add(key)
            top_gradients.append({"a": g["a"], "b": g["b"], "strength": g["strength"], "trend": g["trend"]})
    # if dedup dropped some, fill from remaining sorted by weight
    if len(top_gradients) < k:
        remaining = sorted(
            [g for g in gradients if (g["a"], g["b"]) not in seen],
            key=lambda g: g["weight"],
            reverse=True,
        )
        for g in remaining:
            if len(top_gradients) >= k:
                break
            top_gradients.append({"a": g["a"], "b": g["b"], "strength": g["strength"], "trend": g["trend"]})

    k = min(10, len(boundaries))
    boundary_weights = [g["weight"] for g in boundaries]
    if k == 0 or not boundaries:
        sampled_boundaries = []
    elif sum(boundary_weights) <= 0:
        sampled_boundaries = boundaries[:k]
    else:
        sampled_boundaries = random.choices(boundaries, weights=boundary_weights, k=k)
    seen = set()
    top_boundaries = []
    for g in sampled_boundaries:
        key = (g["a"], g["b"])
        if key not in seen:
            seen.add(key)
            top_boundaries.append({"a": g["a"], "b": g["b"], "strength": g["strength"], "trend": g["trend"]})
    if len(top_boundaries) < k:
        remaining = sorted(
            [g for g in boundaries if (g["a"], g["b"]) not in seen],
            key=lambda g: g["weight"],
            reverse=True,
        )
        for g in remaining:
            if len(top_boundaries) >= k:
                break
            top_boundaries.append({"a": g["a"], "b": g["b"], "strength": g["strength"], "trend": g["trend"]})

    return {"vocabulary": vocabulary, "gradients": top_gradients, "boundaries": top_boundaries}


# ── add_gradient ──────────────────────────────────────────────────────────────

def add_gradient(a: str, b: str, conv_id: str) -> dict:
    a, b = sorted([a.strip(), b.strip()])
    now = time.time()
    with _connect() as con:
        # get-or-create gradient
        con.execute(
            "INSERT OR IGNORE INTO gradients (endpoint_a, endpoint_b) VALUES (?, ?)",
            (a, b),
        )
        row = con.execute(
            "SELECT gradient_id FROM gradients WHERE endpoint_a = ? AND endpoint_b = ?",
            (a, b),
        ).fetchone()
        gradient_id = row["gradient_id"]

        # record hit — PK violation means same-conv re-hit
        try:
            con.execute(
                "INSERT INTO hits (gradient_id, conv_id, ts) VALUES (?, ?, ?)",
                (gradient_id, conv_id, now),
            )
        except sqlite3.IntegrityError:
            return {"error": "link already recorded this conversation"}

        # evict only if this was a brand-new gradient and we're over MAX_GRADIENTS
        gradient_count = con.execute("SELECT COUNT(*) AS c FROM gradients").fetchone()["c"]
        if gradient_count > MAX_GRADIENTS:
            _evict_weakest(con, now)

    return {"status": "recorded", "gradient": {"a": a, "b": b}}

def add_boundary(a: str, b: str, conv_id: str) -> dict:
    a, b = sorted([a.strip(), b.strip()])
    now = time.time()
    with _connect() as con:
        # get-or-create boundary
        con.execute(
            "INSERT OR IGNORE INTO boundaries (endpoint_a, endpoint_b) VALUES (?, ?)",
            (a, b),
        )
        row = con.execute(
            "SELECT boundary_id FROM boundaries WHERE endpoint_a = ? AND endpoint_b = ?",
            (a, b),
        ).fetchone()
        boundary_id = row["boundary_id"]

        # record hit — PK violation means same-conv re-hit
        try:
            con.execute(
                "INSERT INTO boundary_hits (boundary_id, conv_id, ts) VALUES (?, ?, ?)",
                (boundary_id, conv_id, now),
            )
        except sqlite3.IntegrityError:
            return {"error": "boundary already recorded this conversation"}

        boundary_count = con.execute("SELECT COUNT(*) AS c FROM boundaries").fetchone()["c"]
        if boundary_count > MAX_BOUNDARIES:
            _evict_weakest_boundary(con, now)

    return {"status": "recorded", "boundary": {"a": a, "b": b}}

def _evict_weakest(con: sqlite3.Connection, now: float) -> None:
    row = con.execute(f"""
        SELECT g.gradient_id, {_strength_expr(now)} AS s
        FROM gradients g
        JOIN hits h ON h.gradient_id = g.gradient_id
        GROUP BY g.gradient_id
        ORDER BY s ASC
        LIMIT 1
    """).fetchone()
    if row:
        con.execute("DELETE FROM gradients WHERE gradient_id = ?", (row["gradient_id"],))


def _evict_weakest_boundary(con: sqlite3.Connection, now: float) -> None:
    row = con.execute(f"""
        SELECT b.boundary_id, {_strength_expr(now)} AS s
        FROM boundaries b
        JOIN boundary_hits h ON h.boundary_id = b.boundary_id
        GROUP BY b.boundary_id
        ORDER BY s ASC
        LIMIT 1
    """).fetchone()
    if row:
        con.execute("DELETE FROM boundaries WHERE boundary_id = ?", (row["boundary_id"],))
