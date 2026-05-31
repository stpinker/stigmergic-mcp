"""SQLite persistence layer — schema init and all reads/writes."""

import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────
HALF_LIFE_SECONDS = 86400 * 2 # 2 days by default, but configurable at runtime and per-experiment
HALF_LIFE_TICKS = 2
FLOOR = 0.25
EPSILON = 0.1
MAX_GRADIENTS = 100
MAX_BOUNDARIES = 100
PERSISTENCE_ALPHA = 0.15
PERSISTENCE_CAP = 1.5

TREND_TAG = "trend"
TREND_ALPHA = 0.5
TREND_EPSILON = 0.2
TREND_WINDOW_SECONDS = 3600 # 1 hour
TREND_WINDOW_LABEL_RECENT = f"<{TREND_WINDOW_SECONDS // 60}min"
TREND_WINDOW_LABEL_PRIOR = f"<{(2 * TREND_WINDOW_SECONDS) // 60}min"

AGE_TAG = "age"
SUPPORT_TAG = "support"
TRACE_TAG = "trace"
LENS_TAG = "lens"
CROSS_TAG = "cross"
GLOBAL_WEIGHT_TAG = "GLOBAL"
TURN_WEIGHT_TAG = "TURN"

DEFAULT_GLOBAL_POWER = 1.0
DEFAULT_TURN_POWER = 1.0
DEFAULT_PERSISTENCE_POWER = 0.0
MAX_PUBLIC_POWER = 4.0

_CONFIG_KEYS = (
    "half_life_seconds",
    "half_life_ticks",
    "floor",
    "epsilon",
    "max_gradients",
    "max_boundaries",
    "persistence_alpha",
    "persistence_cap",
)

_config = {
    "half_life_seconds": HALF_LIFE_SECONDS,
    "half_life_ticks": HALF_LIFE_TICKS,
    "floor": FLOOR,
    "epsilon": EPSILON,
    "max_gradients": MAX_GRADIENTS,
    "max_boundaries": MAX_BOUNDARIES,
    "persistence_alpha": PERSISTENCE_ALPHA,
    "persistence_cap": PERSISTENCE_CAP,
}

_SUPPORTED_INCLUDE_TAGS = {
    TREND_TAG,
    AGE_TAG,
    SUPPORT_TAG,
    TRACE_TAG,
    LENS_TAG,
    CROSS_TAG,
}
_SUPPORTED_WEIGHT_TAGS = {GLOBAL_WEIGHT_TAG, TURN_WEIGHT_TAG}


def _validate_config(config: dict) -> None:
    if config["half_life_seconds"] <= 0:
        raise ValueError("half_life_seconds must be greater than 0")
    if config["half_life_ticks"] <= 0:
        raise ValueError("half_life_ticks must be greater than 0")
    if config["floor"] < 0:
        raise ValueError("floor must be greater than or equal to 0")
    if not 0 < config["epsilon"] < 1:
        raise ValueError("epsilon must be greater than 0 and less than 1")
    if config["max_gradients"] < 1:
        raise ValueError("max_gradients must be greater than or equal to 1")
    if config["max_boundaries"] < 1:
        raise ValueError("max_boundaries must be greater than or equal to 1")
    if config["persistence_alpha"] < 0:
        raise ValueError("persistence_alpha must be greater than or equal to 0")
    if config["persistence_cap"] < 1:
        raise ValueError("persistence_cap must be greater than or equal to 1")


def configure(
    *,
    half_life_seconds: float | None = None,
    half_life_ticks: float | None = None,
    floor: float | None = None,
    epsilon: float | None = None,
    max_gradients: int | None = None,
    max_boundaries: int | None = None,
    persistence_alpha: float | None = None,
    persistence_cap: float | None = None,
) -> dict:
    """Apply runtime field settings and return the resolved config."""
    global _config
    next_config = dict(_config)
    if half_life_seconds is not None:
        next_config["half_life_seconds"] = float(half_life_seconds)
    if half_life_ticks is not None:
        next_config["half_life_ticks"] = float(half_life_ticks)
    if floor is not None:
        next_config["floor"] = float(floor)
    if epsilon is not None:
        next_config["epsilon"] = float(epsilon)
    if max_gradients is not None:
        next_config["max_gradients"] = int(max_gradients)
    if max_boundaries is not None:
        next_config["max_boundaries"] = int(max_boundaries)
    if persistence_alpha is not None:
        next_config["persistence_alpha"] = float(persistence_alpha)
    if persistence_cap is not None:
        next_config["persistence_cap"] = float(persistence_cap)
    _validate_config(next_config)
    _config = next_config
    return get_config()


def get_config() -> dict:
    """Return the active field settings."""
    return dict(_config)


def _create_settings_table(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)


def _create_sessions_table(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            conv_id TEXT PRIMARY KEY,
            created_ts REAL NOT NULL
        )
    """)


def _store_config(con: sqlite3.Connection) -> None:
    _create_settings_table(con)
    for key, value in get_config().items():
        con.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )


def read_stored_config(path: Path) -> dict:
    """Read persisted field settings from a SQLite experiment database."""
    if not path.exists():
        return {}
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
        if row is None:
            return {}
        rows = con.execute("SELECT key, value FROM settings").fetchall()
        config = {}
        legacy_half_life_seconds = None
        for r in rows:
            key = r["key"]
            if key in _CONFIG_KEYS:
                config[key] = _coerce_config_value(key, r["value"])
            elif key == "half_life_days":
                legacy_half_life_seconds = float(r["value"]) * 86400.0
        if "half_life_seconds" not in config and legacy_half_life_seconds is not None:
            config["half_life_seconds"] = legacy_half_life_seconds
        return config
    finally:
        con.close()


def _coerce_config_value(key: str, value: str) -> float | int | str:
    if key in {"max_gradients", "max_boundaries"}:
        return int(value)
    if key in {
        "half_life_seconds",
        "half_life_ticks",
        "floor",
        "epsilon",
        "persistence_alpha",
        "persistence_cap",
    }:
        return float(value)
    return value


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
            CREATE TABLE IF NOT EXISTS sessions (
                conv_id TEXT PRIMARY KEY,
                created_ts REAL NOT NULL
            );

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
                conv_id     TEXT    NOT NULL,
                first_time  REAL    NOT NULL,
                last_time   REAL    NOT NULL,
                first_tick  INTEGER NOT NULL,
                last_tick   INTEGER NOT NULL,
                hits        INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (gradient_id, conv_id),
                CHECK (hits >= 1),
                CHECK (last_time >= first_time),
                CHECK (last_tick >= first_tick)
            );

            CREATE TABLE IF NOT EXISTS boundary_hits (
                boundary_id INTEGER NOT NULL REFERENCES boundaries(boundary_id) ON DELETE CASCADE,
                conv_id     TEXT    NOT NULL,
                first_time  REAL    NOT NULL,
                last_time   REAL    NOT NULL,
                first_tick  INTEGER NOT NULL,
                last_tick   INTEGER NOT NULL,
                hits        INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (boundary_id, conv_id),
                CHECK (hits >= 1),
                CHECK (last_time >= first_time),
                CHECK (last_tick >= first_tick)
            );

            CREATE INDEX IF NOT EXISTS idx_hits_gradient ON hits(gradient_id);
            CREATE INDEX IF NOT EXISTS idx_hits_last_time ON hits(last_time);
            CREATE INDEX IF NOT EXISTS idx_boundary_hits_boundary ON boundary_hits(boundary_id);
            CREATE INDEX IF NOT EXISTS idx_boundary_hits_last_time ON boundary_hits(last_time);
        """)
        _store_config(con)


def register_session(conv_id: str, created_ts: float | None = None) -> dict:
    """Record a conversation id minted by new_session."""
    with _connect() as con:
        _register_session(con, conv_id, created_ts)
    return {"status": "registered", "conv_id": conv_id}


def _register_session(
    con: sqlite3.Connection,
    conv_id: str,
    created_ts: float | None = None,
) -> None:
    _create_sessions_table(con)
    con.execute(
        """
        INSERT OR IGNORE INTO sessions (conv_id, created_ts) VALUES (?, ?)
        """,
        (conv_id, time.time() if created_ts is None else created_ts),
    )


def require_session(con: sqlite3.Connection, conv_id: str) -> None:
    """Require a conv_id previously minted by new_session."""
    _create_sessions_table(con)
    row = con.execute(
        "SELECT 1 FROM sessions WHERE conv_id = ?",
        (conv_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown conv_id; call new_session first")


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(con: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(r["name"] == column_name for r in rows)


def _migrate_hit_trace_columns(
    con: sqlite3.Connection,
    table_name: str,
) -> None:
    if not _table_exists(con, table_name):
        return
    if not _column_exists(con, table_name, "tick"):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN tick INTEGER NOT NULL DEFAULT 1")
    if not _column_exists(con, table_name, "first_time"):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN first_time REAL")
    if not _column_exists(con, table_name, "last_time"):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN last_time REAL")
    if not _column_exists(con, table_name, "first_tick"):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN first_tick INTEGER")
    if not _column_exists(con, table_name, "last_tick"):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN last_tick INTEGER")
    if not _column_exists(con, table_name, "hits"):
        con.execute(f"ALTER TABLE {table_name} ADD COLUMN hits INTEGER NOT NULL DEFAULT 1")

    if _column_exists(con, table_name, "ts"):
        con.execute(f"""
            UPDATE {table_name}
            SET first_time = COALESCE(first_time, ts),
                last_time = COALESCE(last_time, ts),
                first_tick = COALESCE(first_tick, tick),
                last_tick = COALESCE(last_tick, tick),
                hits = COALESCE(hits, 1)
            WHERE first_time IS NULL
               OR last_time IS NULL
               OR first_tick IS NULL
               OR last_tick IS NULL
        """)
    else:
        con.execute(f"""
            UPDATE {table_name}
            SET first_time = COALESCE(first_time, 0),
                last_time = COALESCE(last_time, first_time, 0),
                first_tick = COALESCE(first_tick, 1),
                last_tick = COALESCE(last_tick, first_tick, 1),
                hits = COALESCE(hits, 1)
            WHERE first_time IS NULL
               OR last_time IS NULL
               OR first_tick IS NULL
               OR last_tick IS NULL
        """)


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

    _migrate_hit_trace_columns(con, "hits")
    _migrate_hit_trace_columns(con, "boundary_hits")


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

def _global_decay_expr(now: float, alias: str) -> str:
    """SQL fragment: wall-clock decay of one hit row."""
    return f"""
        (
            POWER(0.5, ({now} - {alias}.last_time) / {_config['half_life_seconds']})
        )
    """


def _turn_decay_expr(table_name: str, alias: str) -> str:
    """SQL fragment: same-conversation turn decay of one hit row."""
    return f"""
        (
            POWER(
                0.5,
                (
                    (
                        SELECT MAX(latest.last_tick)
                        FROM {table_name} latest
                        WHERE latest.conv_id = {alias}.conv_id
                    ) - {alias}.last_tick
                ) / {_config['half_life_ticks']}
            )
        )
    """





def _persistence_boost_expr(alias: str) -> str:
    return f"""
        MIN(
            1.0 + {_config['persistence_alpha']} * LN(1.0 + ({alias}.hits - 1)),
            {_config['persistence_cap']}
        )
    """


def _validate_temperature(temperature: float | None) -> float:
    value = DEFAULT_GLOBAL_POWER if temperature is None else float(temperature)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("temperature must be positive")
    return value


def _validate_power(name: str, value: float, max_value: float = MAX_PUBLIC_POWER) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and greater than or equal to 0")
    if value > max_value:
        raise ValueError(f"{name} must be less than or equal to {max_value}")
    return value


def _resolve_lens(
    *,
    scope: list[str] | None = None,
    global_power: float | None = None,
    turn_power: float | None = None,
    persistence_power: float | None = None,
    temperature: float | None = None,
) -> dict:
    scope_tags = _normalize_scope(scope)
    resolved_global = 1.0 if GLOBAL_WEIGHT_TAG in scope_tags else 0.0
    resolved_turn = 1.0 if TURN_WEIGHT_TAG in scope_tags else 0.0
    if global_power is not None:
        resolved_global = global_power
    if turn_power is not None:
        resolved_turn = turn_power
    return {
        "global_power": _validate_power("global_power", resolved_global),
        "turn_power": _validate_power("turn_power", resolved_turn),
        "persistence_power": _validate_power(
            "persistence_power",
            DEFAULT_PERSISTENCE_POWER if persistence_power is None else persistence_power,
        ),
        "temperature": _validate_temperature(temperature),
    }


def _selected_decay_expr(
    now: float,
    table_name: str,
    alias: str,
    lens: dict | None = None,
) -> str:
    if lens is None:
        lens = {
            "global_power": DEFAULT_GLOBAL_POWER,
            "turn_power": DEFAULT_TURN_POWER,
            "persistence_power": DEFAULT_PERSISTENCE_POWER,
        }
    factors = []
    if lens["global_power"] > 0:
        factors.append(f"POWER({_global_decay_expr(now, alias)}, {lens['global_power']})")
    if lens["turn_power"] > 0:
        factors.append(f"POWER({_turn_decay_expr(table_name, alias)}, {lens['turn_power']})")
    if lens["persistence_power"] > 0:
        factors.append(f"POWER({_persistence_boost_expr(alias)}, {lens['persistence_power']})")
    return " * ".join(factors) if factors else "1.0"


def _strength_expr(
    now: float,
    table_name: str = "hits",
    alias: str = "h",
    lens: dict | None = None,
) -> str:
    """SQL fragment: strength of a relation given its hit rows."""
    return f"SUM({_selected_decay_expr(now, table_name, alias, lens)})"


def _weight_expr(
    now: float,
    temperature: float,
    table_name: str = "hits",
    alias: str = "h",
    lens: dict | None = None,
) -> str:
    exponent = 1.0 / max(temperature, 1e-6)
    return f"POWER({_strength_expr(now, table_name, alias, lens)}, {exponent})"


# ── Pruning ───────────────────────────────────────────────────────────────────

def prune(con: sqlite3.Connection) -> None:
    """Remove epsilon-faded hits, then dead gradients. Called at the start of sense."""
    import math
    now = time.time()
    # 0.5^(age/HALF_LIFE) < EPSILON  =>  age > HALF_LIFE * log2(1/EPSILON)
    cutoff_ts = now - _config["half_life_seconds"] * math.log2(1.0 / _config["epsilon"])
    con.execute(f"""
        DELETE FROM hits
        WHERE rowid IN (
            SELECT h.rowid
            FROM hits h
            WHERE h.last_time < ?
               OR {_selected_decay_expr(now, "hits", "h", None)} < ?
        )
    """, (cutoff_ts, _config["epsilon"]))
    con.execute(f"""
        DELETE FROM boundary_hits
        WHERE rowid IN (
            SELECT bh.rowid
            FROM boundary_hits bh
            WHERE bh.last_time < ?
               OR {_selected_decay_expr(now, "boundary_hits", "bh", None)} < ?
        )
    """, (cutoff_ts, _config["epsilon"]))

    # Drop dead gradients: strength < FLOOR
    # We need per-gradient strength after the epsilon prune above.
    strength_sql = f"""
        DELETE FROM gradients
        WHERE gradient_id IN (
            SELECT gradient_id FROM (
                SELECT gradient_id,
                       {_strength_expr(now, "hits", "h", None)} AS s
                FROM hits h
                GROUP BY gradient_id
            )
            WHERE s < {_config["floor"]}
        )
    """
    con.execute(strength_sql)

    boundary_strength_sql = f"""
        DELETE FROM boundaries
        WHERE boundary_id IN (
            SELECT boundary_id FROM (
                SELECT boundary_id,
                       {_strength_expr(now, "boundary_hits", "bh", None)} AS s
                FROM boundary_hits bh
                GROUP BY boundary_id
            )
            WHERE s < {_config["floor"]}
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

def _normalize_include(include: list[str] | None) -> set[str]:
    if include is None:
        return set()
    if isinstance(include, str):
        raise ValueError("include must be an array")
    tags = set()
    for tag in include:
        if not isinstance(tag, str):
            raise ValueError(f"include token must be a string: {tag}")
        normalized = tag.strip().lower()
        if not normalized:
            raise ValueError("include contains an empty token")
        if normalized not in _SUPPORTED_INCLUDE_TAGS:
            raise ValueError(f"unknown include token: {tag}")
        tags.add(normalized)
    return tags


def _normalize_scope(scope: list[str] | None) -> set[str]:
    if not scope:
        return {GLOBAL_WEIGHT_TAG, TURN_WEIGHT_TAG}
    if isinstance(scope, str):
        raise ValueError("scope must be an array")
    tags = set()
    for tag in scope:
        if not isinstance(tag, str):
            raise ValueError(f"scope token must be a string: {tag}")
        normalized = tag.strip().upper()
        if not normalized:
            raise ValueError("scope contains an empty token")
        if normalized not in _SUPPORTED_WEIGHT_TAGS:
            raise ValueError(f"unknown scope token: {tag}")
        tags.add(normalized)
    return tags

def _trend_from_row(row: sqlite3.Row) -> str:
    #recent = row["recent_convs"]
    #prior = row["prior_convs"]
    #return "\u2191" if recent > prior else ("\u2193" if recent < prior else "\u2192")
    recent = row["recent_convs"]
    prior = row["prior_convs"]

    raw = math.log((recent + TREND_ALPHA) / (prior + TREND_ALPHA))

    support = recent + prior
    confidence = 1.0 - math.exp(-support / 4.0)

    score = raw * confidence

    if score > TREND_EPSILON:
        return "↑" + f" ({TREND_WINDOW_LABEL_RECENT}: {recent}, {TREND_WINDOW_LABEL_PRIOR}: {prior}, Score: {score:.4f})"
    if score < -TREND_EPSILON:
        return "↓" + f" ({TREND_WINDOW_LABEL_RECENT}: {recent}, {TREND_WINDOW_LABEL_PRIOR}: {prior}, Score: {score:.4f})"
    return "→" + f" ({TREND_WINDOW_LABEL_RECENT}: {recent}, {TREND_WINDOW_LABEL_PRIOR}: {prior}, Score: {score:.4f})"

def _age_from_row(row: sqlite3.Row) -> str:
    age = time.time() - row["last_time"]
    return f"{age:.2f} seconds since last hit"


def _support_from_row(row: sqlite3.Row) -> dict:
    conv_count = row["conv_count"]
    total_hits = row["total_hits"]
    return {
        "conv_count": conv_count,
        "total_hits": total_hits,
        "avg_hits_per_conv": round(total_hits / conv_count, 4) if conv_count else 0,
        "max_hits_by_one_conv": row["max_hits_by_one_conv"],
    }


def _trace_from_row(row: sqlite3.Row) -> dict:
    now = time.time()
    return {
        "first_age_seconds": round(now - row["first_time"], 4),
        "last_age_seconds": round(now - row["last_time"], 4),
        "min_first_tick": row["min_first_tick"],
        "max_last_tick": row["max_last_tick"],
        "max_local_span": row["max_local_span"],
    }


def _relation_from_row(row: sqlite3.Row, include: set[str], lens: dict) -> dict:
    relation = {
        "a": row["a"],
        "b": row["b"],
        "first_time": row["first_time"],
        "last_time": row["last_time"],
        "strength": round(row["strength"], 4),
        "weight": row["weight"],
    }
    if TREND_TAG in include:
        relation[TREND_TAG] = _trend_from_row(row)
    if AGE_TAG in include:
        relation[AGE_TAG] = _age_from_row(row)
    if SUPPORT_TAG in include:
        relation[SUPPORT_TAG] = _support_from_row(row)
    if TRACE_TAG in include:
        relation[TRACE_TAG] = _trace_from_row(row)
    if LENS_TAG in include:
        relation[LENS_TAG] = {
            "global_power": lens["global_power"],
            "turn_power": lens["turn_power"],
            "persistence_power": lens["persistence_power"],
            "persistence_alpha": _config["persistence_alpha"],
            "persistence_cap": _config["persistence_cap"],
            "temperature": lens["temperature"],
        }
    return relation


def _public_relation(relation: dict, include: set[str]) -> dict:
    public = {
        "a": relation["a"],
        "b": relation["b"],
        "strength": relation["strength"],
    }
    for tag in (TREND_TAG, AGE_TAG, SUPPORT_TAG, TRACE_TAG, LENS_TAG, CROSS_TAG):
        if tag in include and tag in relation:
            public[tag] = relation[tag]
    return public


def _weighted_sample_without_replacement(items: list[dict], k: int) -> list[dict]:
    import random

    if k <= 0 or not items:
        return []

    k = min(k, len(items))
    if sum(max(item["weight"], 0) for item in items) <= 0:
        return sorted(items, key=lambda item: item["strength"], reverse=True)[:k]

    pool = list(items)
    sample = []
    while pool and len(sample) < k:
        total = sum(max(item["weight"], 0) for item in pool)
        if total <= 0:
            sample.extend(
                sorted(pool, key=lambda item: item["strength"], reverse=True)[: k - len(sample)]
            )
            break

        threshold = random.uniform(0, total)
        upto = 0.0
        for index, item in enumerate(pool):
            upto += max(item["weight"], 0)
            if upto >= threshold:
                sample.append(pool.pop(index))
                break

    return sample


def _attach_cross_reports(
    con: sqlite3.Connection,
    relations: list[dict],
    opposite_kind: str,
    now: float,
    lens: dict,
) -> None:
    for relation in relations:
        relation[CROSS_TAG] = None
    if not relations:
        return

    if opposite_kind == "boundary":
        relation_table = "boundaries"
        hit_table = "boundary_hits"
        id_column = "boundary_id"
    elif opposite_kind == "gradient":
        relation_table = "gradients"
        hit_table = "hits"
        id_column = "gradient_id"
    else:
        raise ValueError(f"unknown cross kind: {opposite_kind}")

    pairs = sorted({(relation["a"], relation["b"]) for relation in relations})
    where_clause = " OR ".join(
        f"(r.endpoint_a = ? AND r.endpoint_b = ?)" for _ in pairs
    )
    params = [endpoint for pair in pairs for endpoint in pair]
    rows = con.execute(
        f"""
        SELECT
            r.endpoint_a AS a,
            r.endpoint_b AS b,
            MAX(h.last_time) AS last_time,
            COUNT(DISTINCT h.conv_id) AS conv_count,
            SUM(h.hits) AS total_hits,
            MAX(h.hits) AS max_hits_by_one_conv,
            {_strength_expr(now, hit_table, "h", lens)} AS strength
        FROM {relation_table} r
        JOIN {hit_table} h ON h.{id_column} = r.{id_column}
        WHERE {where_clause}
        GROUP BY r.{id_column}
        """,
        params,
    ).fetchall()

    reports = {
        (row["a"], row["b"]): {
            "kind": opposite_kind,
            "strength": round(row["strength"], 4),
            "conv_count": row["conv_count"],
            "total_hits": row["total_hits"],
            "max_hits_by_one_conv": row["max_hits_by_one_conv"],
            "last_time": row["last_time"],
        }
        for row in rows
    }
    for relation in relations:
        relation[CROSS_TAG] = reports.get((relation["a"], relation["b"]))


def sense_data(
    con: sqlite3.Connection,
    conv_id: str,
    temperature: float,
    include: list[str] | None = None,
    scope: list[str] | None = None,
    global_power: float | None = None,
    turn_power: float | None = None,
    persistence_power: float | None = None,
) -> dict:
    require_session(con, conv_id)
    now = time.time()
    include_tags = _normalize_include(include)
    lens = _resolve_lens(
        scope=scope,
        global_power=global_power,
        turn_power=turn_power,
        persistence_power=persistence_power,
        temperature=temperature,
    )
    
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

    # Strength and weight are always needed; optional tags add their own columns.
    include_columns = ""
    if TREND_TAG in include_tags:        
        recent_start = now - TREND_WINDOW_SECONDS
        prior_start = now - 2 * TREND_WINDOW_SECONDS
        include_columns = f""",
            SUM(CASE WHEN {{hit_alias}}.last_time >= {recent_start} THEN 1 ELSE 0 END) AS recent_convs,
            SUM(CASE WHEN {{hit_alias}}.last_time >= {prior_start} AND {{hit_alias}}.last_time < {recent_start} THEN 1 ELSE 0 END) AS prior_convs"""
    gradient_rows = con.execute(f"""
        SELECT
            g.gradient_id,
            g.endpoint_a AS a,
            g.endpoint_b AS b,
            MIN(h.first_time) AS first_time,
            MAX(h.last_time) AS last_time,
            MIN(h.first_tick) AS min_first_tick,
            MAX(h.last_tick) AS max_last_tick,
            MAX(h.last_tick - h.first_tick) AS max_local_span,
            COUNT(DISTINCT h.conv_id) AS conv_count,
            SUM(h.hits) AS total_hits,
            MAX(h.hits) AS max_hits_by_one_conv,
            {_strength_expr(now, "hits", "h", lens)} AS strength,
            {_weight_expr(now, lens["temperature"], "hits", "h", lens)} AS weight
            {include_columns.format(hit_alias="h")}
        FROM gradients g
        JOIN hits h ON h.gradient_id = g.gradient_id
        GROUP BY g.gradient_id
    """).fetchall()

    boundary_rows = con.execute(f"""
        SELECT
            b.boundary_id,
            b.endpoint_a AS a,
            b.endpoint_b AS b,
            MIN(bh.first_time) AS first_time,
            MAX(bh.last_time) AS last_time,
            MIN(bh.first_tick) AS min_first_tick,
            MAX(bh.last_tick) AS max_last_tick,
            MAX(bh.last_tick - bh.first_tick) AS max_local_span,
            COUNT(DISTINCT bh.conv_id) AS conv_count,
            SUM(bh.hits) AS total_hits,
            MAX(bh.hits) AS max_hits_by_one_conv,
            {_strength_expr(now, "boundary_hits", "bh", lens)} AS strength,
            {_weight_expr(now, lens["temperature"], "boundary_hits", "bh", lens)} AS weight
            {include_columns.format(hit_alias="bh")}
        FROM boundaries b
        JOIN boundary_hits bh ON bh.boundary_id = b.boundary_id
        GROUP BY b.boundary_id
    """).fetchall()

    if not gradient_rows and not boundary_rows:
        return {"vocabulary": vocabulary, "gradients": [], "boundaries": []}

    gradients = [_relation_from_row(r, include_tags, lens) for r in gradient_rows]
    boundaries = [_relation_from_row(r, include_tags, lens) for r in boundary_rows]

    # Temperature shapes a weighted sample without replacement, biased by decayed strength.
    sampled_gradients = _weighted_sample_without_replacement(gradients, 10)
    sampled_boundaries = _weighted_sample_without_replacement(boundaries, 10)
    if CROSS_TAG in include_tags:
        _attach_cross_reports(con, sampled_gradients, "boundary", now, lens)
        _attach_cross_reports(con, sampled_boundaries, "gradient", now, lens)

    top_gradients = [
        _public_relation(g, include_tags)
        for g in sampled_gradients
    ]
    top_boundaries = [
        _public_relation(g, include_tags)
        for g in sampled_boundaries
    ]

    return {"vocabulary": vocabulary, "gradients": top_gradients, "boundaries": top_boundaries}


# ── add_gradient ──────────────────────────────────────────────────────────────

def _next_tick(con: sqlite3.Connection, table_name: str, conv_id: str) -> int:
    row = con.execute(
        f"SELECT COALESCE(MAX(last_tick), 0) + 1 AS next_tick FROM {table_name} WHERE conv_id = ?",
        (conv_id,),
    ).fetchone()
    return int(row["next_tick"])


def _hit_table_has_legacy_time_columns(con: sqlite3.Connection, table_name: str) -> bool:
    return _column_exists(con, table_name, "ts") and _column_exists(con, table_name, "tick")


def add_gradient(a: str, b: str, conv_id: str) -> dict:
    a, b = sorted([a.strip(), b.strip()])
    now = time.time()
    with _connect() as con:
        require_session(con, conv_id)
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

        tick = _next_tick(con, "hits", conv_id)
        if _hit_table_has_legacy_time_columns(con, "hits"):
            con.execute(
                """
                INSERT INTO hits (
                    gradient_id,
                    conv_id,
                    ts,
                    tick,
                    first_time,
                    last_time,
                    first_tick,
                    last_tick,
                    hits
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(gradient_id, conv_id) DO UPDATE SET
                    ts = excluded.ts,
                    tick = excluded.tick,
                    last_time = excluded.last_time,
                    last_tick = excluded.last_tick,
                    hits = hits.hits + 1
                """,
                (gradient_id, conv_id, now, tick, now, now, tick, tick),
            )
        else:
            con.execute(
                """
                INSERT INTO hits (
                    gradient_id,
                    conv_id,
                    first_time,
                    last_time,
                    first_tick,
                    last_tick,
                    hits
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(gradient_id, conv_id) DO UPDATE SET
                    last_time = excluded.last_time,
                    last_tick = excluded.last_tick,
                    hits = hits.hits + 1
                """,
                (gradient_id, conv_id, now, now, tick, tick),
            )

        # evict only if this was a brand-new gradient and we're over max_gradients
        gradient_count = con.execute("SELECT COUNT(*) AS c FROM gradients").fetchone()["c"]
        if gradient_count > _config["max_gradients"]:
            _evict_weakest(con, now)

    return {"status": "recorded", "gradient": {"a": a, "b": b}}

def add_boundary(a: str, b: str, conv_id: str) -> dict:
    a, b = sorted([a.strip(), b.strip()])
    now = time.time()
    with _connect() as con:
        require_session(con, conv_id)
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

        tick = _next_tick(con, "boundary_hits", conv_id)
        if _hit_table_has_legacy_time_columns(con, "boundary_hits"):
            con.execute(
                """
                INSERT INTO boundary_hits (
                    boundary_id,
                    conv_id,
                    ts,
                    tick,
                    first_time,
                    last_time,
                    first_tick,
                    last_tick,
                    hits
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(boundary_id, conv_id) DO UPDATE SET
                    ts = excluded.ts,
                    tick = excluded.tick,
                    last_time = excluded.last_time,
                    last_tick = excluded.last_tick,
                    hits = boundary_hits.hits + 1
                """,
                (boundary_id, conv_id, now, tick, now, now, tick, tick),
            )
        else:
            con.execute(
                """
                INSERT INTO boundary_hits (
                    boundary_id,
                    conv_id,
                    first_time,
                    last_time,
                    first_tick,
                    last_tick,
                    hits
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(boundary_id, conv_id) DO UPDATE SET
                    last_time = excluded.last_time,
                    last_tick = excluded.last_tick,
                    hits = boundary_hits.hits + 1
                """,
                (boundary_id, conv_id, now, now, tick, tick),
            )

        boundary_count = con.execute("SELECT COUNT(*) AS c FROM boundaries").fetchone()["c"]
        if boundary_count > _config["max_boundaries"]:
            _evict_weakest_boundary(con, now)

    return {"status": "recorded", "boundary": {"a": a, "b": b}}

def _evict_weakest(con: sqlite3.Connection, now: float) -> None:
    row = con.execute(f"""
        SELECT g.gradient_id, {_strength_expr(now, "hits", "h")} AS s
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
        SELECT b.boundary_id, {_strength_expr(now, "boundary_hits", "h")} AS s
        FROM boundaries b
        JOIN boundary_hits h ON h.boundary_id = b.boundary_id
        GROUP BY b.boundary_id
        ORDER BY s ASC
        LIMIT 1
    """).fetchone()
    if row:
        con.execute("DELETE FROM boundaries WHERE boundary_id = ?", (row["boundary_id"],))
