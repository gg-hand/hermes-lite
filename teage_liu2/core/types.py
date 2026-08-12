"""消息模型与事件类型(流是基建,类型最先定死)。

事件是主干产出的最小单元:
- 外壳(server/)将其编码为 SSE 帧
- 非流式调用方收集事件拼字符串
- 枝干与主干均只依赖此处定义的事件类型,不得自造事件

事件类型清单(M1):
- step_start   : 一次 LLM 调用的开始(循环形态下每轮一个 step)
- text_delta   : LLM 输出文本增量
- reasoning_delta: LLM 推理增量(可选,透传给前端思考区)
- step_end     : 一次 LLM 调用的结束(携带 content_blocks / stop_reason / usage)
- tool_use     : LLM 请求调用工具(循环形态发出)
- tool_result  : 工具执行结果回传
- done         : 整个对话结束(携带最终文本与终止原因)
- error        : 对话失败(不产生 done)
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

# ---------------------------------------------------------------------------
# 事件类型常量
# ---------------------------------------------------------------------------
EV_STEP_START = "step_start"
EV_TEXT_DELTA = "text_delta"
EV_REASONING_DELTA = "reasoning_delta"
EV_STEP_END = "step_end"
EV_TOOL_USE = "tool_use"
EV_TOOL_RESULT = "tool_result"
EV_DONE = "done"
EV_ERROR = "error"

EventType = Literal[
    "step_start",
    "text_delta",
    "reasoning_delta",
    "step_end",
    "tool_use",
    "tool_result",
    "done",
    "error",
]

# 终止原因(与 done 事件配套)
TERMINATION_NORMAL = "normal"          # end_turn 自然完成
TERMINATION_MAX_LOOPS = "max_loops"    # 达到循环上限
TERMINATION_USER_CANCEL = "user_cancel"  # 用户取消
TERMINATION_NO_TOOL_EXECUTOR = "no_tool_executor"  # LLM 想调工具但无枝干执行
TERMINATION_LLM_ERROR = "llm_error"    # LLM 调用失败
TERMINATION_INTERCEPTED = "intercepted"  # 枝干 before 钩子置 ctx.stop 拦截

TerminationReason = str

# ---------------------------------------------------------------------------
# 消息模型(Anthropic 风格 content block)
# ---------------------------------------------------------------------------
# 消息: {"role": "user"|"assistant"|"system"|"tool", "content": str | list[block]}
# content block: {"type": "text"|"tool_use"|"tool_result"|"thinking", ...}
Message = Dict[str, Any]
Block = Dict[str, Any]

# 事件: {"type": EventType, "session_id": str, ...payload}
Event = Dict[str, Any]


def text_block(text: str) -> Block:
    """构造文本 content block。"""
    return {"type": "text", "text": text}


def tool_use_block(block_id: str, name: str, tool_input: dict) -> Block:
    """构造 tool_use content block。"""
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


def tool_result_block(tool_use_id: str, content: str, is_error: bool = False) -> Block:
    """构造 tool_result content block。"""
    block: Block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def extract_text(content: Union[str, list, None]) -> str:
    """从消息 content(str 或 block 列表)提取纯文本。

    用于 token 粗估 / 归档 / 非流式收集。tool_use 只取 name,tool_result 递归提取。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
            elif btype == "tool_use":
                name = block.get("name", "")
                if isinstance(name, str):
                    parts.append(name)
            elif btype == "tool_result":
                parts.append(extract_text(block.get("content")))
        return "".join(parts)
    return str(content) if content is not None else ""


def split_content_blocks(content_blocks: List[Block]) -> tuple[List[str], List[Block], List[Block]]:
    """按类型拆分 content_blocks,返回 (text_parts, tool_use_blocks, thinking_blocks)。"""
    text_parts: List[str] = []
    tool_use_blocks: List[Block] = []
    thinking_blocks: List[Block] = []
    for block in content_blocks:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)
        elif btype == "tool_use":
            tool_use_blocks.append(block)
        elif btype == "thinking":
            thinking_blocks.append(block)
    return text_parts, tool_use_blocks, thinking_blocks


def normalize_history(messages: List[Message]) -> List[Message]:
    """将历史消息规整为 {role, content} 形式(剔除附加字段,符合 LLM API 规范)。

    丢弃 content 为空的系统消息等异常项。
    """
    clean: List[Message] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role is None or content is None:
            continue
        clean.append({"role": role, "content": content})
    return clean
