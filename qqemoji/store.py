"""索数据库（SQLite）：保存扫描结果并提供查询。"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

DB_PATH = config.INDEX_DIR / "index.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    account      TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL,
    variant      TEXT NOT NULL DEFAULT 'origin',
    path         TEXT NOT NULL,
    offset       INTEGER,
    length       INTEGER,
    obfuscated   INTEGER NOT NULL DEFAULT 0,
    ext          TEXT NOT NULL DEFAULT '',
    mime         TEXT NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        REAL NOT NULL DEFAULT 0,
    pack_id      TEXT NOT NULL DEFAULT '',
    pack_name    TEXT NOT NULL DEFAULT '',
    pack_author  TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL DEFAULT '',
    raw_name     TEXT NOT NULL DEFAULT '',
    keywords     TEXT NOT NULL DEFAULT '',
    width        INTEGER NOT NULL DEFAULT 0,
    height       INTEGER NOT NULL DEFAULT 0,
    frames       INTEGER NOT NULL DEFAULT 1,
    animated     INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    added        REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_source  ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_account ON items(account);
CREATE INDEX IF NOT EXISTS idx_items_pack    ON items(pack_id);
CREATE INDEX IF NOT EXISTS idx_items_ext     ON items(ext);
CREATE INDEX IF NOT EXISTS idx_items_mtime   ON items(mtime);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

COLUMNS = (
    "id", "account", "source", "variant", "path", "offset", "length", "obfuscated",
    "ext", "mime", "size", "mtime", "pack_id", "pack_name", "pack_author", "name",
    "raw_name", "keywords", "width", "height", "frames", "animated", "content_hash", "added",
)

SORT_MODES = {
    "time_desc": "mtime DESC",
    "time_asc": "mtime ASC",
    "size_desc": "size DESC",
    "size_asc": "size ASC",
    "name_asc": "name ASC, id ASC",
    "pack_asc": "pack_name ASC, name ASC, id ASC",
    "random": "RANDOM()",
}


class Index:
    """线程安全的索引封装（每线程一个连接，写操作加锁）。"""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_schema()

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """老版本的索引库缺少新列时自动补上（无需重新扫描）。"""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        for column, ddl in (("raw_name", "TEXT NOT NULL DEFAULT ''"),):
            if column not in existing:
                conn.execute(f"ALTER TABLE items ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def clear_account(self, account: str) -> int:
        with self._write_lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM items WHERE account = ?", (account,))
            conn.commit()
            return cur.rowcount or 0

    def clear_all(self) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute("DELETE FROM items")
            conn.execute("DELETE FROM meta")
            conn.commit()

    def upsert_many(self, records: Iterable[dict]) -> int:
        """批量写入（同一 id 覆盖）。"""
        rows = []
        now = time.time()
        for rec in records:
            data = {col: rec.get(col) for col in COLUMNS}
            data.setdefault("added", now)
            data["obfuscated"] = 1 if data.get("obfuscated") else 0
            data["animated"] = 1 if data.get("animated") else 0
            for key in ("offset", "length"):
                if data.get(key) is not None:
                    data[key] = int(data[key])
            for key in ("size", "width", "height", "frames"):
                data[key] = int(data.get(key) or 0)
            data["mtime"] = float(data.get("mtime") or 0)
            data["added"] = float(data.get("added") or now)
            rows.append(tuple(data.get(col) for col in COLUMNS))
        if not rows:
            return 0
        placeholders = ",".join("?" for _ in COLUMNS)
        sql = f"INSERT OR REPLACE INTO items ({','.join(COLUMNS)}) VALUES ({placeholders})"
        with self._write_lock:
            conn = self._conn()
            conn.executemany(sql, rows)
            conn.commit()
        return len(rows)

    def update_probe(self, item_id: str, width: int, height: int, frames: int, animated: bool) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "UPDATE items SET width=?, height=?, frames=?, animated=? WHERE id=?",
                (int(width), int(height), int(frames), 1 if animated else 0, item_id),
            )
            conn.commit()

    def set_content_hash(self, item_id: str, digest: str) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute("UPDATE items SET content_hash=? WHERE id=?", (digest, item_id))
            conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, str(value)))
            conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def _where(self, filters: dict) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        def in_clause(column: str, values: Sequence[str] | None) -> None:
            if not values:
                return
            marks = ",".join("?" for _ in values)
            clauses.append(f"{column} IN ({marks})")
            params.extend(values)

        in_clause("source", filters.get("sources"))
        in_clause("ext", filters.get("exts"))
        in_clause("account", filters.get("accounts"))

        if filters.get("packs"):
            in_clause("pack_id", filters["packs"])

        if filters.get("ids"):
            in_clause("id", filters["ids"])

        animated = filters.get("animated")
        if animated is True:
            clauses.append("animated = 1")
        elif animated is False:
            clauses.append("animated = 0")

        query = (filters.get("q") or "").strip()
        if query:
            like = f"%{query}%"
            clauses.append(
                "(name LIKE ? OR raw_name LIKE ? OR keywords LIKE ? OR pack_name LIKE ?"
                " OR pack_author LIKE ? OR id LIKE ? OR pack_id LIKE ?)"
            )
            params.extend([like] * 7)

        min_width = int(filters.get("min_width") or 0)
        if min_width > 0:
            clauses.append("MIN(width, height) >= ?")
            params.append(min_width)

        min_size = int(filters.get("min_size") or 0)
        if min_size > 0:
            clauses.append("size >= ?")
            params.append(min_size)

        if filters.get("exclude_ids"):
            marks = ",".join("?" for _ in filters["exclude_ids"])
            clauses.append(f"id NOT IN ({marks})")
            params.extend(filters["exclude_ids"])

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def query(self, filters: dict | None = None, page: int = 1, page_size: int = 60,
              with_total: bool = True) -> dict:
        """分页查询。

        :param with_total: 是否统计总条数。翻后续页时传 ``False`` 可以省掉一次
                           ``COUNT(*)``（深分页时开销明显）。
        """
        filters = filters or {}
        where, params = self._where(filters)
        conn = self._conn()
        total = None
        if with_total:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM items{where}", params).fetchone()["c"]

        order = SORT_MODES.get(filters.get("sort", "time_desc"), SORT_MODES["time_desc"])
        page = max(1, int(page))
        page_size = max(1, min(500, int(page_size)))
        offset = (page - 1) * page_size

        if order == "RANDOM()":
            sql = f"SELECT * FROM items{where} ORDER BY RANDOM() LIMIT ?"
            rows = conn.execute(sql, [*params, page_size]).fetchall()
        else:
            sql = f"SELECT * FROM items{where} ORDER BY {order} LIMIT ? OFFSET ?"
            rows = conn.execute(sql, [*params, page_size, offset]).fetchall()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (max(1, (total + page_size - 1) // page_size) if total is not None else None),
            "items": [dict(r) for r in rows],
        }

    def get(self, item_id: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def get_many(self, ids: Sequence[str]) -> list[dict]:
        out: list[dict] = []
        conn = self._conn()
        chunk = 500
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            marks = ",".join("?" for _ in part)
            rows = conn.execute(f"SELECT * FROM items WHERE id IN ({marks})", part).fetchall()
            out.extend(dict(r) for r in rows)
        return out

    def all_matching(self, filters: dict | None = None, limit: int = 200000) -> list[dict]:
        filters = dict(filters or {})
        filters.pop("sort", None)
        where, params = self._where(filters)
        order = "added ASC"
        rows = self._conn().execute(
            f"SELECT * FROM items{where} ORDER BY {order} LIMIT ?", [*params, limit]
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM items").fetchone()
        by_source = [
            dict(r) for r in conn.execute(
                "SELECT source, COUNT(*) AS count, COALESCE(SUM(size),0) AS size,"
                " SUM(animated) AS animated FROM items GROUP BY source"
            ).fetchall()
        ]
        by_ext = [
            dict(r) for r in conn.execute(
                "SELECT ext, COUNT(*) AS count FROM items GROUP BY ext ORDER BY count DESC"
            ).fetchall()
        ]
        by_account = [
            dict(r) for r in conn.execute(
                "SELECT account, COUNT(*) AS count FROM items GROUP BY account"
            ).fetchall()
        ]
        packs = conn.execute("SELECT COUNT(DISTINCT pack_id) c FROM items WHERE pack_id <> ''").fetchone()
        return {
            "total": total["c"],
            "size": total["s"],
            "animated": conn.execute("SELECT COUNT(*) c FROM items WHERE animated=1").fetchone()["c"],
            "packs": packs["c"],
            "by_source": by_source,
            "by_ext": by_ext,
            "by_account": by_account,
            "scanned_at": self.get_meta("scanned_at", ""),
            "last_scan_report": self.get_meta("last_scan_report", ""),
        }

    def packs(self, source: str = "marketface", limit: int = 3000) -> list[dict]:
        rows = self._conn().execute(
            """
            SELECT pack_id, MAX(pack_name) AS pack_name, MAX(pack_author) AS pack_author,
                   COUNT(*) AS count, SUM(animated) AS animated
            FROM items WHERE source=? AND pack_id <> ''
            GROUP BY pack_id ORDER BY count DESC LIMIT ?
            """,
            (source, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def ext_options(self) -> list[dict]:
        return [
            dict(r) for r in self._conn().execute(
                "SELECT ext, COUNT(*) AS count FROM items GROUP BY ext ORDER BY count DESC"
            ).fetchall()
        ]

    def account_options(self) -> list[dict]:
        return [
            dict(r) for r in self._conn().execute(
                "SELECT account, COUNT(*) AS count FROM items GROUP BY account ORDER BY count DESC"
            ).fetchall()
        ]


_index: Index | None = None
_index_lock = threading.Lock()


def get_index() -> Index:
    """进程内单例。"""
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = Index()
    return _index
