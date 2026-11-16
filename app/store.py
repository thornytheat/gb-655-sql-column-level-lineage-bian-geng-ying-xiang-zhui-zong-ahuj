"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    item_id       TEXT,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS amendments (
    amendment_id      TEXT PRIMARY KEY,
    amendment_key     TEXT NOT NULL UNIQUE,
    root_schema_id    TEXT NOT NULL,
    base_schema_id    TEXT NOT NULL,
    base_revision_seq INTEGER NOT NULL,
    revision_seq      INTEGER,
    source_path       TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    applied_at        TEXT,
    result_schema_id  TEXT,
    error             TEXT,
    items_json        TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    UNIQUE(root_schema_id, revision_seq)
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
CREATE INDEX IF NOT EXISTS idx_amendments_base ON amendments(root_schema_id, revision_seq);
"""


def db_path() -> Path:
    raw = os.environ.get("LINEAGEGUARD_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "amendments", "kv"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        _insert_snapshot(conn, snapshot)


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_snapshots_for_source(source_path: str) -> list[dict]:
    """同一份文件被采信出的所有版本（含补正修订版），按建立时间排序。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM snapshots WHERE source_path=?
               ORDER BY created_at, schema_id""",
            (source_path,),
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, item_id, line, table_name, column_name,
                   reason, blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, row.get("item_id") or "", int(row.get("line") or 0),
                    row.get("table") or "", row.get("column") or "", row["reason"],
                    int(bool(row.get("blocking"))), row.get("detail") or "",
                    row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT item_id, line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "item_id": row["item_id"],
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
                int(run["version_seq"]), run["input_sha256"], run["ruleset"],
                run["created_at"], run["effective_from"], run["effective_to"],
                _dumps(run["summary"]), _dumps(run),
            ),
        )


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


# ---------- 补正 ----------

def save_amendment(amendment: dict) -> None:
    """补正单落库；同一 amendment_key 重放只更新状态，不新增行。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO amendments (amendment_id, amendment_key, root_schema_id,
                   base_schema_id, base_revision_seq, revision_seq, source_path, status,
                   created_at, updated_at, applied_at, result_schema_id, error,
                   items_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(amendment_key) DO UPDATE SET
                   status=excluded.status,
                   updated_at=excluded.updated_at,
                   applied_at=excluded.applied_at,
                   result_schema_id=excluded.result_schema_id,
                   error=excluded.error,
                   payload_json=excluded.payload_json""",
            (
                amendment["amendment_id"], amendment["amendment_key"],
                amendment["root_schema_id"], amendment["base_schema_id"],
                int(amendment["base_revision_seq"]), amendment.get("revision_seq"),
                amendment["source_path"], amendment["status"],
                amendment["created_at"], amendment["updated_at"],
                amendment.get("applied_at"), amendment.get("result_schema_id"),
                amendment.get("error"),
                _dumps(amendment["items"]), _dumps(amendment),
            ),
        )


def load_amendment(amendment_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM amendments WHERE amendment_id=?", (amendment_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def find_amendment_by_key(amendment_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM amendments WHERE amendment_key=?", (amendment_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_amendments(limit: int = 50) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM amendments
               ORDER BY created_at DESC, amendment_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def amendments_for_snapshot(schema_id: str) -> list[dict]:
    """以某快照为基线、或由某快照修订出来的补正单，按修订序号升序。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM amendments
               WHERE root_schema_id=? OR base_schema_id=?
               ORDER BY COALESCE(revision_seq, 1 << 30), created_at""",
            (schema_id, schema_id),
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def latest_revision(source_path: str) -> dict | None:
    """同一份文件的最新采信结果：修订序号最大的快照，没有修订就是原快照。"""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT payload_json FROM snapshots WHERE source_path=?
               ORDER BY COALESCE(json_extract(payload_json, '$.revision_seq'), 0) DESC,
                        created_at DESC, schema_id DESC LIMIT 1""",
            (source_path,),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_revision_map() -> dict[str, dict]:
    """全部文件的最新采信结果，按 source_path 索引，供页面标注「有新修订版」。"""
    out: dict[str, dict] = {}
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM snapshots
               ORDER BY COALESCE(json_extract(payload_json, '$.revision_seq'), 0) DESC,
                        created_at DESC, schema_id DESC"""
        ).fetchall()
    for row in rows:
        snapshot = _loads(row["payload_json"])
        out.setdefault(snapshot["source_path"], snapshot)
    return out


def _insert_snapshot(conn: sqlite3.Connection, snapshot: dict) -> None:
    conn.execute(
        """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
               encoding, bom, field_count, blocked_count, created_at, payload_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(schema_id) DO UPDATE SET
               version_label=excluded.version_label,
               source_path=excluded.source_path,
               source_sha256=excluded.source_sha256,
               encoding=excluded.encoding,
               bom=excluded.bom,
               field_count=excluded.field_count,
               blocked_count=excluded.blocked_count,
               created_at=excluded.created_at,
               payload_json=excluded.payload_json""",
        (
            snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
            snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
            int(snapshot["field_count"]), int(snapshot["blocked_count"]),
            snapshot["created_at"], _dumps(snapshot),
        ),
    )


class RevisionConflict(Exception):
    """提交补正时基线已经变了：另一份修订先落库。"""

    def __init__(self, current_schema_id: str | None):
        super().__init__("补正基线已过期")
        self.current_schema_id = current_schema_id


def commit_amendment(amendment: dict, snapshot: dict | None,
                     base_schema_id: str) -> dict:
    """一个事务里落修订快照、隔离区和补正单；基线过期则整体回滚。

    修订序号在库里现取，避免两个并发补正拿到同一个序号。
    """
    with _LOCK, _connect() as conn:
        conn.isolation_level = None  # 事务边界自己管，显式 BEGIN IMMEDIATE
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT payload_json FROM snapshots WHERE source_path=?
                   ORDER BY COALESCE(json_extract(payload_json, '$.revision_seq'), 0) DESC,
                            created_at DESC, schema_id DESC LIMIT 1""",
                (amendment["source_path"],),
            ).fetchone()
            current = _loads(row["payload_json"]) if row else None
            if current is None or current["schema_id"] != base_schema_id:
                raise RevisionConflict(
                    current["schema_id"] if current else None
                )
            base_revision = int(current.get("revision_seq") or 0)
            revision_seq = base_revision + 1
            amendment = dict(amendment)
            if snapshot is not None:
                amendment["revision_seq"] = revision_seq
                snapshot = dict(snapshot)
                snapshot["revision_seq"] = revision_seq
                snapshot["amendment_id"] = amendment["amendment_id"]
                _insert_snapshot(conn, snapshot)
                conn.execute("DELETE FROM quarantine WHERE schema_id=?",
                             (snapshot["schema_id"],))
                conn.executemany(
                    """INSERT INTO quarantine (schema_id, item_id, line, table_name,
                           column_name, reason, blocking, detail, raw)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            snapshot["schema_id"], item.get("item_id") or "",
                            int(item.get("line") or 0), item.get("table") or "",
                            item.get("column") or "", item["reason"],
                            int(bool(item.get("blocking"))), item.get("detail") or "",
                            item.get("raw") or "",
                        )
                        for item in snapshot["diagnostics"]
                    ],
                )
                amendment["status"] = amendment.get("status") or "applied"
                amendment["result_schema_id"] = snapshot["schema_id"]
                amendment["applied_at"] = amendment["updated_at"]
                amendment["error"] = None
            else:
                summary = amendment.get("summary") or {}
                amendment["status"] = "skipped" if (
                    summary.get("skipped") and not summary.get("rejected")
                    and not summary.get("pending")
                ) else "rejected"
                amendment["error"] = "没有可采纳的补正项"
                amendment["applied_at"] = amendment["updated_at"]
            conn.execute(
                """INSERT INTO amendments (amendment_id, amendment_key, root_schema_id,
                       base_schema_id, base_revision_seq, revision_seq, source_path,
                       status, created_at, updated_at, applied_at, result_schema_id,
                       error, items_json, payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(amendment_key) DO UPDATE SET
                       status=excluded.status,
                       updated_at=excluded.updated_at,
                       applied_at=excluded.applied_at,
                       result_schema_id=excluded.result_schema_id,
                       error=excluded.error,
                       payload_json=excluded.payload_json""",
                (
                    amendment["amendment_id"], amendment["amendment_key"],
                    amendment["root_schema_id"], amendment["base_schema_id"],
                    int(amendment["base_revision_seq"]), amendment.get("revision_seq"),
                    amendment["source_path"], amendment["status"],
                    amendment["created_at"], amendment["updated_at"],
                    amendment.get("applied_at"), amendment.get("result_schema_id"),
                    amendment.get("error"), _dumps(amendment["items"]),
                    _dumps(amendment),
                ),
            )
            conn.commit()
        except RevisionConflict:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - 落库失败要留下可续跑的单
            conn.rollback()
            failed = dict(amendment)
            failed["status"] = "failed"
            failed["error"] = f"{type(exc).__name__}: {exc}"
            failed["updated_at"] = config.now_utc().isoformat()
            try:
                save_amendment(failed)
            except Exception:  # noqa: BLE001 - 连失败单都写不进时只能抛出
                pass
            raise
    return amendment


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default
