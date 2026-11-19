from contextlib import contextmanager
import sqlite3
import time
from typing import Any, Dict, Iterator, List, Optional


class StickerDB:
    """Local SQLite storage for user slicing preferences and created sticker packs."""

    def __init__(self, db_path: str, default_emoji: str = "🧩") -> None:
        self.db_path = db_path
        self.default_emoji = default_emoji
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_prefs (
                    user_id INTEGER PRIMARY KEY,
                    slice_mode TEXT NOT NULL DEFAULT 'grid',
                    cols INTEGER NOT NULL DEFAULT 0,
                    rows INTEGER NOT NULL DEFAULT 0,
                    fit_mode TEXT NOT NULL DEFAULT 'crop',
                    tile_style TEXT NOT NULL DEFAULT 'seamless',
                    pack_type TEXT NOT NULL DEFAULT 'regular',
                    emoji TEXT NOT NULL DEFAULT '🧩',
                    custom_title TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_packs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    pack_name TEXT NOT NULL UNIQUE,
                    pack_title TEXT NOT NULL,
                    pack_type TEXT NOT NULL DEFAULT 'regular',
                    stickers_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_user_packs_uid
                ON user_packs(user_id, created_at DESC);
                """
            )

    def get_prefs(self, user_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_prefs WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row:
                return dict(row)
            now = int(time.time())
            conn.execute(
                """
                INSERT INTO user_prefs (
                    user_id, slice_mode, cols, rows, fit_mode,
                    tile_style, pack_type, emoji, custom_title, updated_at
                ) VALUES (?, 'grid', 0, 0, 'crop', 'seamless', 'regular', ?, '', ?)
                """,
                (user_id, self.default_emoji, now),
            )
            return {
                "user_id": user_id,
                "slice_mode": "grid",
                "cols": 0,
                "rows": 0,
                "fit_mode": "crop",
                "tile_style": "seamless",
                "pack_type": "regular",
                "emoji": self.default_emoji,
                "custom_title": "",
                "updated_at": now,
            }

    def update_prefs(self, user_id: int, **kwargs: Any) -> Dict[str, Any]:
        allowed = {
            "slice_mode",
            "cols",
            "rows",
            "fit_mode",
            "tile_style",
            "pack_type",
            "emoji",
            "custom_title",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_prefs(user_id)

        _ = self.get_prefs(user_id)
        updates["updated_at"] = int(time.time())
        cols_sql = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [user_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE user_prefs SET {cols_sql} WHERE user_id = ?", values
            )
        return self.get_prefs(user_id)

    def add_pack(
        self,
        user_id: int,
        pack_name: str,
        pack_title: str,
        pack_type: str,
        stickers_count: int,
    ) -> int:
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO user_packs (user_id, pack_name, pack_title, pack_type, stickers_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(pack_name) DO UPDATE SET
                    pack_title = excluded.pack_title,
                    stickers_count = excluded.stickers_count
                """,
                (user_id, pack_name, pack_title, pack_type, stickers_count, now),
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute(
                "SELECT id FROM user_packs WHERE pack_name = ?", (pack_name,)
            ).fetchone()
            return int(row["id"]) if row else 0

    def list_packs(
        self, user_id: int, pack_type: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if pack_type:
                rows = conn.execute(
                    """
                    SELECT * FROM user_packs
                    WHERE user_id = ? AND pack_type = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, pack_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM user_packs
                    WHERE user_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_pack(self, pack_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_packs WHERE id = ? AND user_id = ?",
                (pack_id, user_id),
            ).fetchone()
            return dict(row) if row else None

    def delete_pack_record(self, pack_id: int, user_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM user_packs WHERE id = ? AND user_id = ?",
                (pack_id, user_id),
            )
            return cur.rowcount > 0
