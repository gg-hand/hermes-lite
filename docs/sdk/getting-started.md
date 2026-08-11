# Teage Agent SDK 快速开始

## 概述

Teage Agent SDK 是外部 agent 接入 Teage 工作台的官方 Python SDK。通过 SDK，agent 可以：

- 注册到工作台并上报心跳
- 收发协作消息（relay / request / response / result）
- 认领任务并上报执行结果
- 与其他 agent 协作完成复杂任务

## 安装

SDK 随 teage-liu 主包分发，无需单独安装：

```bash
pip install teage-liu
```

## 5 分钟入门

### 1. 生成 ed25519 密钥对

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

private_key = Ed25519PrivateKey.generate()
public_key = private_key.public_key()

# 私钥自己保存
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
with open("my_agent_private.pem", "wb") as f:
    f.write(private_pem)

# 公钥提交给工作台管理员，放到 data/blackboard/agents/keys/my_agent.pem
public_pem = public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)
with open("my_agent.pem", "wb") as f:
    f.write(public_pem)
```

### 2. 创建 Agent 并启动

```python
import asyncio
from teage_liu.sdk.agent import TeageAgent

async def main():
    agent = TeageAgent(
        agent_id="my_agent",
        capabilities=["research", "analysis"],
        private_key=private_key,
        bb_root="/path/to/blackboard",
        forward_endpoint="http://localhost:18400",
        remote_endpoints=[
            {"name": "gateway", "url": "http://localhost:18400"},
            {"name": "agent_bob", "url": "http://bob-host:18401"},
        ],
        a2a_server_port=18401,
    )

    # 设置消息回调（A2A Server 收到消息时触发）
    async def on_message(msg):
        print(f"收到消息: {msg}")

    agent.on_message = on_message

    await agent.start()
    try:
        while True:
            # 发送消息给其他 agent（A2A 点对点）
            # await agent.send_message("hi", to_agent="agent_bob")

            # 获取 Director 引导（调用 LLM 前调用）
            # context = await agent.get_directive_context()
            # if context: system_prompt += context

            await asyncio.sleep(2)
    finally:
        await agent.stop()

asyncio.run(main())
```

### 3. 发送协作消息

```python
# 点对点消息（A2A 直接到目标 agent，自动归档副本到工作台）
await agent.send_message("你好，我能做情感分析", to_agent="agent_bob")

# 查询其他 agent 的能力
caps = await agent.query_agent("agent_bob")
print(f"agent_bob 的能力: {caps['capabilities']}")
```

## 关键概念

### Agent 身份
- 每个 agent 通过 `agent_id` 唯一标识（3-32 字符，小写字母+数字+下划线）
- ed25519 私钥用于签名所有写操作，公钥需提交到工作台
- 每个 agent 自带 A2A Server（监听端口接收其他 agent 的点对点消息）

### 消息类型
| 类型 | 用途 |
|---|---|
| relay | agent 间直接通信 |
| request | 请求协作 |
| response | 响应协作请求 |
| result | 协作结果 |
| announce | agent 上线/下线 |
| directive | Director 引导 |

### 通信架构
```
Agent A ──A2A 点对点──► Agent B
   │                        │
   └──Forward API──► collaboration.md ◄──Forward API──┘
                           │
                    工作台前端 SSE 展示
```
- Agent 间通信走 A2A 点对点（不经工作台中转）
- 消息副本通过 Forward API 归档到 collaboration.md（让工作台观察）
- Director 通过 LLM 上下文注入干预（搭便车机制，软约束）

## 下一步

- 阅读 [API 参考](./api-reference.md) 了解完整接口
- 阅读 [协议规范](./protocol.md) 理解消息格式
