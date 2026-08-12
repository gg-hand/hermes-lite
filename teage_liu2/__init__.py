"""teage_liu2 — 主干-枝干架构的个人 AI Agent(重构版)。

分层:
- core/: 内核,零枝干可跑(对话动作 + 钩子扩展机制)
- branches/: 枝干,可插拔子系统(记忆/工具/护栏/意图/调度/协作)
- server/: 外壳(FastAPI),只依赖 core,不感知 branches
"""

__version__ = "0.1.0"
