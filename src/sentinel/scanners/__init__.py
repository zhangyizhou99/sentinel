"""语言解析器包。导入此包即注册所有内置后端。"""
from sentinel.scanners.base import (  # noqa: F401
    LanguageScanner,
    get_scanner_for,
    register,
    supported_extensions,
)
from sentinel.scanners import python_scanner  # noqa: F401  导入即触发 PythonScanner 注册

# 未来：from sentinel.scanners import js_scanner, go_scanner  # tree-sitter
