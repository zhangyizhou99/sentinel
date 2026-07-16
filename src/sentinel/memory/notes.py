"""笔记库（Note Store）—— Agent / 用户可写可查的持久笔记。

笔记是「上下文工程」的一等公民：团队约定、某函数为何不埋点、某依赖的埋点口径……
写下来后由 ContextBuilder 在判定时自动召回、注入证据，让 Sentinel「越用越懂这个团队」。

作用域三档（scope 由字段推导，越具体越优先）：
  - unit  ：绑定到某个函数（unit_id 非空）——最相关。
  - repo  ：绑定到某个仓库（repo 非空、unit_id 空）——团队/仓库级约定。
  - global：不绑（repo 空）——跨仓库的通用经验。

存在与情节记忆同一个 SQLite 文件里（另一张表），键与 CodeUnit.unit_id 对齐。
搜索是确定性的（作用域 + 标签重叠 + 关键词子串打分），无需向量；将来可升级为向量召回。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sentinel.config import episodic_db_path

GLOBAL = "global"
REPO = "repo"
UNIT = "unit"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_repo(repo: Optional[str]) -> str:
    return os.path.abspath(os.path.expanduser(repo)) if repo else ""


def _split_tags(raw: str) -> List[str]:
    return [t for t in (raw or "").split(",") if t]


@dataclass
class Note:
    id: int
    ts: str
    repo: str
    unit_id: str
    tags: List[str] = field(default_factory=list)
    text: str = ""
    author: str = "agent"

    @property
    def scope(self) -> str:
        if self.unit_id:
            return UNIT
        if self.repo:
            return REPO
        return GLOBAL

    def to_dict(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "scope": self.scope, "repo": self.repo,
            "unit_id": self.unit_id, "tags": self.tags, "text": self.text,
            "author": self.author,
        }


@dataclass
class ScoredNote:
    note: Note
    score: float


class NoteStore:
    """笔记的增删查。默认与情节记忆同库（episodic.db 的 notes 表）。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or episodic_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                repo TEXT NOT NULL DEFAULT '',
                unit_id TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'agent'
            );
            CREATE INDEX IF NOT EXISTS idx_notes_repo ON notes(repo);
            """
        )
        self._conn.commit()

    # ---- 写 -----------------------------------------------------------------

    def add_note(self, text: str, repo: Optional[str] = None, unit_id: str = "",
                 tags: Optional[List[str]] = None, author: str = "agent") -> int:
        text = (text or "").strip()
        if not text:
            raise ValueError("笔记内容不能为空 | note text required")
        cur = self._conn.execute(
            "INSERT INTO notes (ts, repo, unit_id, tags, text, author) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), _norm_repo(repo), unit_id or "", ",".join(tags or []), text, author),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def delete_note(self, note_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # ---- 查 -----------------------------------------------------------------

    def _row_to_note(self, r: sqlite3.Row) -> Note:
        return Note(r["id"], r["ts"], r["repo"], r["unit_id"],
                    _split_tags(r["tags"]), r["text"], r["author"])

    def list_notes(self, repo: Optional[str] = None, limit: int = 50) -> List[Note]:
        if repo:
            rows = self._conn.execute(
                "SELECT * FROM notes WHERE repo = ? OR repo = '' ORDER BY id DESC LIMIT ?",
                (_norm_repo(repo), limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_note(r) for r in rows]

    def search_notes(self, repo: Optional[str] = None, unit_id: str = "",
                     tags: Optional[List[str]] = None, query: str = "",
                     limit: int = 5) -> List[ScoredNote]:
        """按相关性召回笔记：作用域(unit>repo>global) + 标签重叠 + 关键词子串。

        只在「本仓库 + 全局」范围内找（不串仓库）。返回带分数、已排序、截断到 limit。
        """
        repo_n = _norm_repo(repo)
        rows = self._conn.execute(
            "SELECT * FROM notes WHERE repo = ? OR repo = ''",
            (repo_n,),
        ).fetchall()
        want_tags = set(tags or [])
        q = (query or "").strip().lower()

        scored: List[ScoredNote] = []
        for r in rows:
            note = self._row_to_note(r)
            score = 0.0
            if unit_id and note.unit_id == unit_id:
                score += 100.0                      # 正是这个函数的笔记
            elif note.unit_id and unit_id and note.unit_id != unit_id:
                continue                            # 别的函数的专属笔记，跳过
            if note.repo and note.repo == repo_n:
                score += 40.0                       # 本仓库约定
            if want_tags:
                score += 12.0 * len(want_tags & set(note.tags))
            if q and q in note.text.lower():
                score += 8.0
            if score <= 0 and (want_tags or q or unit_id):
                continue                            # 有过滤条件却零命中 → 不相关
            scored.append(ScoredNote(note, score))

        scored.sort(key=lambda s: (s.score, s.note.id), reverse=True)
        return scored[:limit]

    def close(self) -> None:
        self._conn.close()
