"""程序性记忆·修复技能（Procedural memory）—— 见 DESIGN §8.2。

只做一件实事：**成功的补法 → 存成「修复技能」→ 复用**（别搞花架子）。
  key   = (语言, 框架, 信号类型)     如 (python, "", http)
  value = 操作模板：snippet_template（含 {qualname}/{signal} 占位）+ import_stmt

下次遇到同 key 盲区，直接套模板生成，跳过重新推理。与情节记忆同库不同表（skills）。
学习信号先用「apply 成功即记录」；接受/拒绝的加权降权接 Agentic-RL（后续）。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from sentinel.config import episodic_db_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Skill:
    language: str
    framework: str
    signal: str
    snippet_template: str
    import_stmt: str
    uses: int
    ts: str


class ProceduralMemory:
    """SQLite skills 表：(语言, 框架, 信号) → 补埋点操作模板。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or episodic_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS skills (
                language TEXT NOT NULL,
                framework TEXT NOT NULL DEFAULT '',
                signal TEXT NOT NULL,
                snippet_template TEXT NOT NULL,
                import_stmt TEXT NOT NULL DEFAULT '',
                uses INTEGER NOT NULL DEFAULT 1,
                ts TEXT NOT NULL,
                PRIMARY KEY (language, framework, signal)
            );
            """
        )
        self._conn.commit()

    def record_skill(self, language: str, signal: str, snippet_template: str,
                     import_stmt: str = "", framework: str = "") -> None:
        """记住一条修复技能；已存在则更新模板并把 uses +1（成功越多越可信）。"""
        self._conn.execute(
            """
            INSERT INTO skills (language, framework, signal, snippet_template, import_stmt, uses, ts)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(language, framework, signal) DO UPDATE SET
                snippet_template = excluded.snippet_template,
                import_stmt = excluded.import_stmt,
                uses = uses + 1,
                ts = excluded.ts
            """,
            (language, framework, signal, snippet_template, import_stmt, _now()),
        )
        self._conn.commit()

    def get_skill(self, language: str, signal: str, framework: str = "") -> Optional[Skill]:
        r = self._conn.execute(
            "SELECT * FROM skills WHERE language = ? AND framework = ? AND signal = ?",
            (language, framework, signal),
        ).fetchone()
        return self._row(r) if r else None

    def list_skills(self) -> List[Skill]:
        rows = self._conn.execute("SELECT * FROM skills ORDER BY uses DESC").fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r: sqlite3.Row) -> Skill:
        return Skill(r["language"], r["framework"], r["signal"],
                     r["snippet_template"], r["import_stmt"], r["uses"], r["ts"])

    def close(self) -> None:
        self._conn.close()
