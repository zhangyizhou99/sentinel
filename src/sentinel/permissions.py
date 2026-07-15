"""权限中枢（DESIGN §14 权限与隔离）。

Sentinel 是「读懂他人代码」的 Agent，读别人代码是敏感操作，必须有边界与同意：
  - 边界（scope）：只能在 workspace_root 内活动，越界一律拒绝，且不可自我提权。
  - 同意（grant）：读取某目录代码前需显式授权（human-in-the-loop）；已授权集合按会话保存。

权限分两级：
  - 浏览目录名（find_repo）：低风险 → 在 scope 内免授权。
  - 读取代码内容（scan）：高风险 → 必须先 grant。
"""
from __future__ import annotations

import os
from typing import List


class PermissionBroker:
    """一次会话的权限状态：范围边界 + 已授权路径集合。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self._granted: set = set()

    def within_scope(self, path: str) -> bool:
        """path 是否落在允许的根目录内（含根本身）。"""
        ap = os.path.abspath(path)
        return ap == self.root or ap.startswith(self.root + os.sep)

    def is_granted(self, path: str) -> bool:
        """path 是否已被授权读取（被任一已授权目录覆盖即可）。"""
        ap = os.path.abspath(path)
        return any(ap == g or ap.startswith(g + os.sep) for g in self._granted)

    def grant(self, path: str) -> str:
        """授予对某路径的读取许可；越界拒绝（不可提权到 scope 之外）。返回规范化路径。"""
        ap = os.path.abspath(os.path.expanduser(path))
        if not self.within_scope(ap):
            raise PermissionError(f"越界，拒绝授权 | out of scope: {ap}")
        self._granted.add(ap)
        return ap

    @property
    def granted(self) -> List[str]:
        return sorted(self._granted)
