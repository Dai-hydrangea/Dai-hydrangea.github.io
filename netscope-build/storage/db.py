"""
NetScope SQLite ストレージ

contents (記事生データ) / clusters (Phase 1B で使う) / snapshots (Phase 1B) を管理。
Phase 1A は contents だけ稼働。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS contents (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_name TEXT,
    category TEXT,
    title TEXT NOT NULL,
    body TEXT,
    summary TEXT,
    url TEXT,
    published_at TEXT,
    score INTEGER DEFAULT 0,
    fetched_at TEXT,
    embedding BLOB,
    cluster_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_contents_category ON contents (category);
CREATE INDEX IF NOT EXISTS idx_contents_fetched ON contents (fetched_at);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    created_at TEXT,
    source_counts TEXT,
    trend_signal TEXT,
    bias_matrix TEXT
);

CREATE TABLE IF NOT EXISTS clusters (
    snapshot_id TEXT,
    cluster_id INTEGER,
    category TEXT,
    stance TEXT,
    label TEXT,
    size INTEGER,
    centroid BLOB,
    representative_ids TEXT,
    PRIMARY KEY (snapshot_id, cluster_id)
);
"""


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self) -> None:
        """contents テーブルに後付けカラムを安全に追加 (idempotent)"""
        cur = self.conn.execute("PRAGMA table_info(contents)")
        cols = {row["name"] for row in cur.fetchall()}
        if "title_ja" not in cols:
            self.conn.execute("ALTER TABLE contents ADD COLUMN title_ja TEXT")
        if "lang" not in cols:
            self.conn.execute("ALTER TABLE contents ADD COLUMN lang TEXT")

    def upsert_contents(self, items: Iterable[dict]) -> int:
        """重複は ID で吸収、 既存は更新 (score / fetched_at 等の最新化)"""
        cur = self.conn.cursor()
        n = 0
        for it in items:
            cur.execute(
                """
                INSERT INTO contents (id, source, source_name, category, title, body,
                                       url, published_at, score, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    score = excluded.score,
                    fetched_at = excluded.fetched_at
                """,
                (
                    it["id"], it["source"], it.get("source_name"), it.get("category"),
                    it["title"], it.get("body", ""), it.get("url", ""),
                    it.get("published_at", ""), it.get("score", 0), it.get("fetched_at", ""),
                ),
            )
            n += 1
        self.conn.commit()
        return n

    def get_untranslated(self, limit: int = 200) -> list[dict]:
        """title_ja が未設定の記事を取得"""
        cur = self.conn.execute(
            "SELECT id, title FROM contents WHERE title_ja IS NULL OR title_ja = '' LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]

    def update_translations(self, id_to_ja: dict[str, tuple[str, str]]) -> int:
        """id → (title_ja, lang) で一括更新"""
        cur = self.conn.cursor()
        for cid, (ja, lang) in id_to_ja.items():
            cur.execute(
                "UPDATE contents SET title_ja = ?, lang = ? WHERE id = ?",
                (ja, lang, cid),
            )
        self.conn.commit()
        return len(id_to_ja)

    def get_recent(self, days: int = 30, category: str | None = None) -> list[dict]:
        """ retain_days 以内の記事を取得 (export 用)"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if category:
            cur = self.conn.execute(
                "SELECT * FROM contents WHERE fetched_at >= ? AND category = ? ORDER BY fetched_at DESC",
                (cutoff, category),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM contents WHERE fetched_at >= ? ORDER BY fetched_at DESC",
                (cutoff,),
            )
        return [dict(row) for row in cur.fetchall()]

    def cleanup(self, retain_days: int = 30) -> int:
        """retain_days より古い記事を削除"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
        cur = self.conn.execute("DELETE FROM contents WHERE fetched_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def close(self):
        self.conn.close()
