"""标准 A2A v1.0 协议层（手写实现，不依赖官方 a2a-sdk）。

依据 a2a-protocol.org v1.0 规范：
- models.py        数据模型（AgentCard / Message / Part / Task / 事件）
- exceptions.py    A2A 标准错误码异常
- jsonrpc.py       JSON-RPC 2.0 信封与分发器
- task_store.py    Task 持久化 + 事件集线器
- task_manager.py  Task 生命周期管理
- agent_card.py    Agent Card 构建与发布
- handlers.py      标准方法 handlers
- engine_adapter.py 标准面 ↔ 内部引擎适配层
- sse.py           SSE 帧构造
- router.py        FastAPI 路由（/.well-known/agent-card.json + /a2a/std/jsonrpc）
- client.py        标准 A2A 客户端
"""
