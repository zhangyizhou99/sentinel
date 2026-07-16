"""情节记忆（Episodic Memory）—— Agent 的「经历」与「反馈」落盘。

用一份 SQLite 存两张表：
- runs     ：每次扫描/运行的流水（时间/仓库/盲区数/摘要），供回看历史、做趋势。
- feedback ：用户对**具体函数**的裁决（ignore=不用埋点 / instrument=该埋点），
             按 (仓库, unit_id) 唯一，重复提交则覆盖（最新裁决为准）。

反馈学习（Agentic-RL 的最小闭环）：下次扫描时，把用户标了 ignore 的函数从盲区里
**抑制掉**——「被拒的就别再烦你」。这不是 LLM 训练，而是确定性的经验记忆：
简单、可解释、可撤销（改回 instrument 即恢复）。

键约定：unit_id = "相对文件路径::限定名"（与 CodeUnit.unit_id 一致）；仓库用绝对路径。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Set

from sentinel.config import episodic_db_path

IGNORE = "ignore"
INSTRUMENT = "instrument"


@dataclass
class FeedbackRow:
    repo: str
    unit_id: str
    decision: str
    note: str
    ts: str


@dataclass
class RunRow:
    id: int
    ts: str
    repo: str
    goal: str
    blind_spot_count: int
    suppressed_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_repo(repo: str) -> str:
    return os.path.abspath(os.path.expanduser(repo or ""))


class EpisodicMemory:
    """SQLite 情节记忆。默认落在 config.cache_dir()/episodic.db。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or episodic_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        # check_same_thread=False：Web(Gradio) 会从不同工作线程调用同一单例；
        # SQLite 自身有锁，单用户本地场景够用。
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        # 记住最近扫描的仓库，方便对话里「把刚才那个函数标为忽略」时省去重复报路径。
        self.last_repo: Optional[str] = None

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                repo TEXT NOT NULL,
                goal TEXT,
                blind_spot_count INTEGER NOT NULL DEFAULT 0,
                suppressed_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS feedback (
                repo TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                note TEXT,
                ts TEXT NOT NULL,
                PRIMARY KEY (repo, unit_id)
            );
            """
        )
        self._conn.commit()

    # ---- 运行流水 -----------------------------------------------------------

    def record_run(self, repo: str, goal: str = "",
                   blind_spot_count: int = 0, suppressed_count: int = 0) -> int:
        repo = _norm_repo(repo)
        self.last_repo = repo
        cur = self._conn.execute(
            "INSERT INTO runs (ts, repo, goal, blind_spot_count, suppressed_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), repo, goal or "", int(blind_spot_count), int(suppressed_count)),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_runs(self, repo: Optional[str] = None, limit: int = 20) -> List[RunRow]:
        if repo:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE repo = ? ORDER BY id DESC LIMIT ?",
                (_norm_repo(repo), limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [RunRow(r["id"], r["ts"], r["repo"], r["goal"] or "",
                       r["blind_spot_count"], r["suppressed_count"]) for r in rows]

    # ---- 反馈（学习闭环）----------------------------------------------------

    def record_feedback(self, repo: str, unit_id: str,
                        decision: str, note: str = "") -> None:
        if decision not in (IGNORE, INSTRUMENT):
            raise ValueError(f"decision 必须是 {IGNORE}/{INSTRUMENT}，收到 {decision!r}")
        self._conn.execute(
            "INSERT INTO feedback (repo, unit_id, decision, note, ts) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(repo, unit_id) DO UPDATE SET "
            "decision=excluded.decision, note=excluded.note, ts=excluded.ts",
            (_norm_repo(repo), unit_id, decision, note or "", _now()),
        )
        self._conn.commit()

    def ignored_units(self, repo: str) -> Set[str]:
        """某仓库里被用户标为 ignore 的 unit_id 集合（下次扫描要抑制）。"""
        rows = self._conn.execute(
            "SELECT unit_id FROM feedback WHERE repo = ? AND decision = ?",
            (_norm_repo(repo), IGNORE),
        ).fetchall()
        return {r["unit_id"] for r in rows}

    def is_ignored(self, repo: str, unit_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM feedback WHERE repo = ? AND unit_id = ? AND decision = ?",
            (_norm_repo(repo), unit_id, IGNORE),
        ).fetchone()
        return row is not None

    def list_feedback(self, repo: Optional[str] = None) -> List[FeedbackRow]:
        if repo:
            rows = self._conn.execute(
                "SELECT * FROM feedback WHERE repo = ? ORDER BY ts DESC",
                (_norm_repo(repo),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM feedback ORDER BY ts DESC"
            ).fetchall()
        return [FeedbackRow(r["repo"], r["unit_id"], r["decision"],
                            r["note"] or "", r["ts"]) for r in rows]

    def close(self) -> None:
        self._conn.close()
