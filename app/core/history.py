# -*- coding: utf-8 -*-
"""动作历史: 撤销/重放所需的动作记录与撤销重做栈。

策略(妥协内存与正确性):
- 每个成功修改画布的动作记录受影响 bbox 及其 before/after 像素补丁(zlib 压缩)。
- undo 恢复 before 补丁, redo 恢复 after 补丁, O(bbox) 操作。
- 不保存整画布快照; 补丁用 zlib 压缩(纯色/简单图形压缩率极高)。
- undo 栈限制 max_steps; 超限时最老动作失去撤销资格, 但其 after 补丁保留
  (供"删除笔画"时的区域重放使用)。
"""
import zlib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class ActionRecord:
    action_id: str
    action_type: str                 # draw / erase_area / erase_stroke / bucket_fill
    stroke_id: Optional[str] = None  # draw: 产生的笔画; erase_stroke: 被删除的笔画
    bbox: Optional[tuple] = None     # (x0, y0, x1, y1)
    before: Optional[bytes] = None   # zlib 压缩的 before 补丁
    after: Optional[bytes] = None    # zlib 压缩的 after 补丁
    params_summary: str = ""
    created_index: int = 0           # 提交序号(笔画对象与动作共享同一计数)
    undoable: bool = True            # 超限丢弃后 False

    @property
    def undone(self) -> bool:
        return self._undone

    _undone: bool = field(default=False, repr=False)


def compress_patch(arr: np.ndarray) -> bytes:
    return zlib.compress(np.ascontiguousarray(arr).tobytes(), 6)


def decompress_patch(data: bytes, shape) -> np.ndarray:
    buf = zlib.decompress(data)
    arr = np.frombuffer(buf, dtype=np.uint8)
    return arr.reshape(shape).copy()


class HistoryManager:
    """撤销/重做栈 + 全量动作序列(供重放)。"""

    def __init__(self, max_steps: int = 100):
        self.max_steps = max(1, int(max_steps))
        self.undo_stack = []      # 已生效动作(可撤销), 栈顶为最新
        self.redo_stack = []      # 已撤销动作(可重做), 栈顶为最近撤销
        self.all_actions = []     # 按提交序的全部动作(含失去撤销资格的)
        self.total_count = 0      # 累计成功动作数

    def commit(self, record: ActionRecord) -> None:
        record.created_index = self.total_count
        self.total_count += 1
        self.all_actions.append(record)
        self.undo_stack.append(record)
        self.redo_stack.clear()
        # 超限: 最老动作失去撤销资格(保留 after 供重放)
        while len(self.undo_stack) > self.max_steps:
            old = self.undo_stack.pop(0)
            old.undoable = False
            old.before = None  # 释放 before 内存

    def mark_undone(self, record: ActionRecord) -> None:
        record._undone = True

    def mark_effective(self, record: ActionRecord) -> None:
        record._undone = False

    def pop_undo(self) -> Optional[ActionRecord]:
        while self.undo_stack:
            rec = self.undo_stack.pop()
            if rec.undoable:
                self.redo_stack.append(rec)
                return rec
        return None

    def pop_redo(self) -> Optional[ActionRecord]:
        if self.redo_stack:
            return self.redo_stack.pop()
        return None

    def push_back_undo(self, record: ActionRecord) -> None:
        """redo 栈被清空时把误弹的记录放回(内部使用)。"""
        self.undo_stack.append(record)

    @property
    def can_undo(self) -> bool:
        return len(self.undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self.redo_stack) > 0

    def is_undone(self, action_id: str) -> bool:
        for rec in self.redo_stack:
            if rec.action_id == action_id:
                return True
        return False
