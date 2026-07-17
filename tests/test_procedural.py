"""程序性记忆·修复技能测试（DESIGN §8.2）。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.memory.procedural import ProceduralMemory  # noqa: E402


def test_record_get_and_reuse_count():
    pm = ProceduralMemory(os.path.join(tempfile.mkdtemp(), "p.db"))
    assert pm.get_skill("python", "http") is None
    pm.record_skill("python", "http", 'logger.info("{qualname}")', "import logging")
    s = pm.get_skill("python", "http")
    assert s and s.uses == 1 and "logger.info" in s.snippet_template
    pm.record_skill("python", "http", 'logger.info("{qualname}")', "import logging")
    assert pm.get_skill("python", "http").uses == 2       # 复用即加权
    assert len(pm.list_skills()) == 1


def test_skills_keyed_by_language_and_signal():
    pm = ProceduralMemory(os.path.join(tempfile.mkdtemp(), "p.db"))
    pm.record_skill("python", "http", "a", "")
    pm.record_skill("python", "cache", "b", "")
    assert pm.get_skill("python", "http").snippet_template == "a"
    assert pm.get_skill("python", "cache").snippet_template == "b"
    assert pm.get_skill("typescript", "http") is None     # 语言不匹配
