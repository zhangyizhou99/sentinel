"""本地协作记忆：用户、工作区、任务进度与带权限范围的记忆。

这是可替换的本地实现：默认复用 Sentinel 的 SQLite 状态库，便于在不部署
云服务的情况下验证多人数据模型。每个读取接口显式传入 viewer_id，避免把
“当前机器的所有数据”误当成“当前用户都可见”。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from sentinel.config import episodic_db_path

PRIVATE = "private"
TEAM = "team"
REPO = "repo"
TASK = "task"
MEMORY_SCOPES = {PRIVATE, TEAM, REPO, TASK}
TASK_STATUSES = {"todo", "in_progress", "blocked", "done"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(values: Optional[Iterable[str]]) -> str:
    return json.dumps(list(values or []), ensure_ascii=False)


def _load(raw: str) -> List[str]:
    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


@dataclass(frozen=True)
class User:
    id: str
    display_name: str


@dataclass(frozen=True)
class TaskRecord:
    id: str
    workspace_id: str
    repo_id: str
    title: str
    status: str
    owner_id: str
    branch: str
    updated_at: str


@dataclass(frozen=True)
class Checkpoint:
    id: int
    task_id: str
    author_id: str
    summary: str
    completed: List[str]
    next_step: str
    artifacts: List[str]
    created_at: str


@dataclass(frozen=True)
class SharedMemory:
    id: int
    workspace_id: str
    repo_id: str
    task_id: str
    scope: str
    author_id: str
    content: str
    tags: List[str]
    created_at: str


class CollaborationStore:
    """本地 SQLite 协作存储，后续可由 PostgreSQL 实现替换。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or episodic_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_members (
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                PRIMARY KEY (workspace_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                repo_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_id TEXT NOT NULL DEFAULT '',
                branch TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                completed_json TEXT NOT NULL DEFAULT '[]',
                next_step TEXT NOT NULL DEFAULT '',
                artifacts_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collaboration_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id TEXT NOT NULL,
                repo_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL,
                author_id TEXT NOT NULL,
                content TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks(workspace_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON task_checkpoints(task_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_collab_memory_scope
                ON collaboration_memories(workspace_id, repo_id, task_id, scope);
            """
        )
        self._conn.commit()

    def ensure_user(self, user_id: str, display_name: str) -> User:
        user_id, display_name = user_id.strip(), display_name.strip()
        if not user_id or not display_name:
            raise ValueError("user_id 和 display_name 不能为空")
        now = _now()
        self._conn.execute(
            "INSERT INTO users (id, display_name, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at",
            (user_id, display_name, now, now),
        )
        self._conn.commit()
        return User(user_id, display_name)

    def ensure_workspace(self, workspace_id: str, name: str, user_id: str,
                         role: str = "owner") -> None:
        workspace_id, name = workspace_id.strip(), name.strip()
        if not workspace_id or not name:
            raise ValueError("workspace_id 和 name 不能为空")
        self._require_user(user_id)
        self._conn.execute(
            "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (workspace_id, name, _now()),
        )
        self._conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, ?) "
            "ON CONFLICT(workspace_id, user_id) DO UPDATE SET role=excluded.role",
            (workspace_id, user_id, role),
        )
        self._conn.commit()

    def add_member(self, workspace_id: str, user_id: str, role: str = "member") -> None:
        self._require_workspace(workspace_id)
        self._require_user(user_id)
        self._conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, ?) "
            "ON CONFLICT(workspace_id, user_id) DO UPDATE SET role=excluded.role",
            (workspace_id, user_id, role),
        )
        self._conn.commit()

    def create_task(self, workspace_id: str, title: str, owner_id: str, repo_id: str = "",
                    branch: str = "", task_id: Optional[str] = None) -> TaskRecord:
        self._require_member(workspace_id, owner_id)
        title = title.strip()
        if not title:
            raise ValueError("任务标题不能为空")
        task_id = task_id or str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            "INSERT INTO tasks (id, workspace_id, repo_id, title, status, owner_id, branch, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'todo', ?, ?, ?, ?)",
            (task_id, workspace_id, repo_id, title, owner_id, branch, now, now),
        )
        self._conn.commit()
        return self.get_task(task_id, owner_id)

    def get_task(self, task_id: str, viewer_id: str) -> TaskRecord:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        self._require_member(row["workspace_id"], viewer_id)
        return self._task(row)

    def list_tasks(self, workspace_id: str, viewer_id: str) -> List[TaskRecord]:
        self._require_member(workspace_id, viewer_id)
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE workspace_id = ? ORDER BY updated_at DESC", (workspace_id,)
        ).fetchall()
        return [self._task(row) for row in rows]

    def add_checkpoint(self, task_id: str, author_id: str, summary: str,
                       completed: Optional[Iterable[str]] = None, next_step: str = "",
                       artifacts: Optional[Iterable[str]] = None,
                       status: Optional[str] = None) -> Checkpoint:
        task = self.get_task(task_id, author_id)
        if status is not None and status not in TASK_STATUSES:
            raise ValueError(f"不支持的任务状态: {status}")
        summary = summary.strip()
        if not summary:
            raise ValueError("checkpoint 摘要不能为空")
        now = _now()
        cur = self._conn.execute(
            "INSERT INTO task_checkpoints "
            "(task_id, author_id, summary, completed_json, next_step, artifacts_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, author_id, summary, _json(completed), next_step.strip(), _json(artifacts), now),
        )
        new_status = status or task.status
        self._conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                           (new_status, now, task_id))
        self._conn.commit()
        return Checkpoint(int(cur.lastrowid), task_id, author_id, summary, list(completed or []),
                          next_step.strip(), list(artifacts or []), now)

    def list_checkpoints(self, task_id: str, viewer_id: str, limit: int = 20) -> List[Checkpoint]:
        self.get_task(task_id, viewer_id)
        rows = self._conn.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [Checkpoint(row["id"], row["task_id"], row["author_id"], row["summary"],
                           _load(row["completed_json"]), row["next_step"],
                           _load(row["artifacts_json"]), row["created_at"]) for row in rows]

    def add_memory(self, workspace_id: str, author_id: str, content: str, scope: str,
                   repo_id: str = "", task_id: str = "", tags: Optional[Iterable[str]] = None) -> SharedMemory:
        if scope not in MEMORY_SCOPES:
            raise ValueError(f"不支持的记忆范围: {scope}")
        self._require_member(workspace_id, author_id)
        content = content.strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        if scope == REPO and not repo_id:
            raise ValueError("repo 范围记忆需要 repo_id")
        if scope == TASK:
            if not task_id:
                raise ValueError("task 范围记忆需要 task_id")
            task = self.get_task(task_id, author_id)
            if task.workspace_id != workspace_id:
                raise ValueError("任务不属于该工作区")
        now = _now()
        cur = self._conn.execute(
            "INSERT INTO collaboration_memories "
            "(workspace_id, repo_id, task_id, scope, author_id, content, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, repo_id, task_id, scope, author_id, content, _json(tags), now),
        )
        self._conn.commit()
        return SharedMemory(int(cur.lastrowid), workspace_id, repo_id, task_id, scope,
                            author_id, content, list(tags or []), now)

    def recall_memories(self, workspace_id: str, viewer_id: str, repo_id: str = "",
                        task_id: str = "", limit: int = 20) -> List[SharedMemory]:
        self._require_member(workspace_id, viewer_id)
        rows = self._conn.execute(
            "SELECT * FROM collaboration_memories WHERE workspace_id = ? "
            "AND (scope = ? AND author_id = ? OR scope = ? OR scope = ? AND repo_id = ? "
            "OR scope = ? AND task_id = ?) ORDER BY id DESC LIMIT ?",
            (workspace_id, PRIVATE, viewer_id, TEAM, REPO, repo_id, TASK, task_id, limit),
        ).fetchall()
        return [SharedMemory(row["id"], row["workspace_id"], row["repo_id"], row["task_id"],
                             row["scope"], row["author_id"], row["content"],
                             _load(row["tags_json"]), row["created_at"]) for row in rows]

    def close(self) -> None:
        self._conn.close()

    def _require_user(self, user_id: str) -> None:
        if self._conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise KeyError(f"用户不存在: {user_id}")

    def _require_workspace(self, workspace_id: str) -> None:
        if self._conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None:
            raise KeyError(f"工作区不存在: {workspace_id}")

    def _require_member(self, workspace_id: str, user_id: str) -> None:
        self._require_workspace(workspace_id)
        row = self._conn.execute(
            "SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        ).fetchone()
        if row is None:
            raise PermissionError("当前用户不是该工作区成员")

    @staticmethod
    def _task(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(row["id"], row["workspace_id"], row["repo_id"], row["title"],
                          row["status"], row["owner_id"], row["branch"], row["updated_at"])