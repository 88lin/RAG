"""Repository 层：对象与数据库行之间的翻译。

契约见 `base.py` —— 三条硬约束是不 commit、需要 id 时 flush、
查集合关系必须预加载。

按**聚合**而非按表划分：`RunRepository` 同时管 runs / run_steps /
tool_calls，因为后两者不能脱离 run 独立存在。
"""

from .base import BaseRepository
from .evidence import EvidenceRepository
from .ingest import IngestTaskRepository
from .runs import RunRepository
from .sessions import SessionRepository

__all__ = [
    "BaseRepository",
    "EvidenceRepository",
    "IngestTaskRepository",
    "RunRepository",
    "SessionRepository",
]
