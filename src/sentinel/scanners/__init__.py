"""语言解析器包。导入此包即注册所有内置后端。"""
from sentinel.scanners.base import (  # noqa: F401
    LanguageScanner,
    get_scanner_for,
    register,
    supported_extensions,
)
from sentinel.scanners import python_scanner  # noqa: F401  导入即触发 PythonScanner 注册

# 其它语言（JS/TS/Go/...）走 tree-sitter，按需动态补齐：
#   sentinel.scanners.treesitter_scanner.install_language_support(lang)
# 会在人审同意后装 language-pack、取查询（内置/缓存/LLM 现写+编译校验）、注册解析器。
# 所以这里**不**在导入期注册它们——用到才装，保持轻量。
