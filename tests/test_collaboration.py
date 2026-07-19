"""本地协作存储：用户边界、共享范围与任务 checkpoint。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.memory import CollaborationStore, PRIVATE, REPO, TASK, TEAM  # noqa: E402


def _store() -> CollaborationStore:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return CollaborationStore(db_path=path)


def _workspace(store: CollaborationStore) -> None:
    store.ensure_user("alice", "Alice")
    store.ensure_user("bob", "Bob")
    store.ensure_workspace("team-a", "Team A", "alice")
    store.add_member("team-a", "bob")


def test_private_memory_is_not_visible_to_another_member():
    store = _store()
    _workspace(store)
    store.add_memory("team-a", "alice", "Alice 的私有偏好", PRIVATE)
    store.add_memory("team-a", "alice", "团队约定", TEAM)

    visible = store.recall_memories("team-a", "bob")
    assert [memory.content for memory in visible] == ["团队约定"]
    store.close()


def test_repo_and_task_memory_require_matching_context():
    store = _store()
    _workspace(store)
    task = store.create_task("team-a", "补齐订单监控", "alice", repo_id="github:acme/orders")
    store.add_memory("team-a", "alice", "订单仓库约定", REPO, repo_id="github:acme/orders")
    store.add_memory("team-a", "alice", "任务决定", TASK, task_id=task.id)

    visible = store.recall_memories("team-a", "bob", repo_id="github:acme/orders", task_id=task.id)
    assert {memory.content for memory in visible} == {"订单仓库约定", "任务决定"}
    assert store.recall_memories("team-a", "bob", repo_id="github:acme/other", task_id="") == []
    store.close()


def test_checkpoint_updates_task_status_and_is_shared_with_members():
    store = _store()
    _workspace(store)
    task = store.create_task("team-a", "扫描后端", "alice", branch="obs/backend")
    checkpoint = store.add_checkpoint(
        task.id, "alice", "已经完成首轮扫描", completed=["扫描"],
        next_step="复核盲区", artifacts=["obs/backend"], status="in_progress",
    )

    assert checkpoint.next_step == "复核盲区"
    assert store.get_task(task.id, "bob").status == "in_progress"
    assert store.list_checkpoints(task.id, "bob")[0].completed == ["扫描"]
    store.close()