"""函数级埋点插入器测试（DESIGN §8.3）。"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.instrument_editor import insert_instrumentation  # noqa: E402


def test_insert_into_plain_function():
    src = "import os\n\ndef checkin():\n    x = redis.get('k')\n    return x\n"
    out = insert_instrumentation(src, "checkin", 'logger.info("checkin")', "import logging")
    assert out is not None
    assert '\n    logger.info("checkin")' in out    # 缩进 4
    assert "import logging" in out
    ast.parse(out)                                    # 仍能解析


def test_insert_after_docstring_in_method():
    src = 'class S:\n    def m(self):\n        """doc."""\n        return redis.get("k")\n'
    out = insert_instrumentation(src, "S.m", 'logger.info("m")')
    assert out is not None
    lines = out.splitlines()
    di = next(i for i, l in enumerate(lines) if "doc." in l)
    li = next(i for i, l in enumerate(lines) if "logger.info" in l)
    assert li == di + 1                               # 插在 docstring 之后
    assert lines[li].startswith("        ")           # 缩进 8
    ast.parse(out)


def test_idempotent():
    src = "def f():\n    return 1\n"
    out = insert_instrumentation(src, "f", 'logger.info("f")')
    assert out is not None
    assert insert_instrumentation(out, "f", 'logger.info("f")') is None   # 已插过 → None


def test_unknown_function_returns_none():
    src = "def f():\n    return 1\n"
    assert insert_instrumentation(src, "nope", 'logger.info("x")') is None


def test_import_not_duplicated():
    src = "import logging\n\ndef f():\n    return redis.get('k')\n"
    out = insert_instrumentation(src, "f", 'logger.info("f")', "import logging")
    assert out is not None
    assert out.count("import logging") == 1           # 已有则不重复
    ast.parse(out)
