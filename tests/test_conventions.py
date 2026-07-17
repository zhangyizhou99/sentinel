"""埋点约定学习器测试（语义记忆 · DESIGN §8.1）。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.model.code_unit import CodeUnit  # noqa: E402
from sentinel.engines.conventions import (  # noqa: E402
    learn_convention,
    store_convention,
    CONVENTION_TAG,
    CONVENTION_AUTHOR,
)
from sentinel.memory.notes import NoteStore  # noqa: E402


def _u(qual, calls, instr, lang="python"):
    return CodeUnit(file="a.py", qualname=qual, kind="function", signature="()",
                    calls=calls, has_instrumentation=instr, language=lang)


def test_learn_convention_logging():
    units = [
        _u("f1", ["logger.info", "redis.get"], True),
        _u("f2", ["logger.error", "db.execute"], True),
        _u("f3", ["requests.get"], False),          # 未埋点，不计入
    ]
    conv = learn_convention("/tmp/repo", units)
    assert conv.found
    assert conv.style == "logging"
    assert "logger.info" in conv.top_calls
    assert conv.sample_count == 2                    # 只数已埋点的


def test_learn_convention_structlog():
    conv = learn_convention("/tmp/repo", [_u("f1", ["structlog.get_logger", "log.info"], True)])
    assert conv.style == "structlog"


def test_learn_convention_none_when_no_samples():
    conv = learn_convention("/tmp/repo", [_u("f1", ["requests.get"], False)])
    assert not conv.found and conv.style == "none"


def test_store_convention_idempotent():
    d = tempfile.mkdtemp()
    notes = NoteStore(db_path=os.path.join(d, "n.db"))
    conv = learn_convention("/tmp/repo", [_u("f1", ["logger.info", "redis.get"], True)])
    assert store_convention(notes, conv) > 0
    store_convention(notes, conv)                    # 再存一次
    autos = [n for n in notes.list_notes(repo="/tmp/repo", limit=100)
             if n.author == CONVENTION_AUTHOR and CONVENTION_TAG in n.tags]
    assert len(autos) == 1                           # 幂等：不重复堆积
    assert "logging" in autos[0].text
