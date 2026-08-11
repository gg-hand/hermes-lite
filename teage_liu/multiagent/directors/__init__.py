"""Director 实现包（Task 5）。

三种 Director 实现：
- AgentDirector: LLM 驱动，agent 自主判断何时注入 directive
- UserDirector: 用户手动坐镇，通过工作台 UI 注入 directive
- ScriptDirector: 脚本/规则引擎，基于条件自动触发（超时/死锁/冲突）
"""
from teage_liu.multiagent.directors.agent_director import AgentDirector
from teage_liu.multiagent.directors.script_director import ScriptDirector
from teage_liu.multiagent.directors.user_director import UserDirector

__all__ = ["AgentDirector", "ScriptDirector", "UserDirector"]
