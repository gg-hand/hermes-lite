"""Intent Classifier 前置路由（spec: agent-metacognition-uplift Task 5）。

在 orchestrator.chat() / chat_stream() 入口、``_build_enhanced_context()``
之后、``ReactLoop.run()`` / ``run_stream()`` 之前调用，对用户输入做轻量
意图分类，输出 ``IntentType`` 枚举供后续 Planning Phase / style_policy /
ToolRegistry 使用。

设计要点：

- **全链路异步**：``classify_intent`` 是 ``async def``，复用
  :meth:`LLMClient.chat_consolidation`（轻量 consolidation 模型，
  max 200 tokens），不阻塞事件循环。
- **不依赖 stream_manager**：``chat()`` 非流式无 stream_manager；
  ``chat_stream()`` 的 stream_manager 由 caller 传入，intent_classifier
  不读取 stream_manager。``session_id`` / ``cancel_event`` 在 chat 入口
  已可用，但 intent_classifier 也不依赖。
- **失败降级**：LLM 调用失败时降级为 ``SIMPLE_QA``（不阻塞主流程），
  低置信度回退到 ``MULTI_STEP_TASK``（走完整 ReactLoop，最保守）。
- **OUT_OF_SCOPE abstain**：返回 ``OUT_OF_SCOPE`` 时由 caller 决定是否
  直接 abstain（本模块只做分类，不做拦截）。
- **复用 consolidation 模型**：避免引入新 LLM 客户端，consolidation
  强制关闭 reasoning（spec 要求避免成本浪费）。
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    try:
        from ..llm.client import LLMClient
    except ImportError:  # pragma: no cover
        from llm.client import LLMClient  # type: ignore

logger = logging.getLogger(__name__)


# IntentClassifier 使用的 LLM 最大输出 token 数（spec: max 200 tokens）
_MAX_INTENT_TOKENS = 200

# 低置信度阈值：LLM 输出未显式声明置信度或低于此值时回退到 MULTI_STEP_TASK
# （最保守路径，走完整 ReactLoop）。
_MIN_CONFIDENCE = 0.6


class IntentType(enum.Enum):
    """用户意图分类枚举。

    每个值对应一种处理路径：

    - ``SIMPLE_QA``：通用概念、闲聊 → 不传 tools，纯对话
    - ``KNOWLEDGE_LOOKUP``：查记忆/文件 → 注入对应子集 tools
    - ``MULTI_STEP_TASK``：触发 Planning Phase（Task 6）
    - ``OUT_OF_SCOPE``：直接 abstain（拒答是能力不是失败）
    """

    SIMPLE_QA = "simple_qa"
    KNOWLEDGE_LOOKUP = "knowledge_lookup"
    MULTI_STEP_TASK = "multi_step_task"
    OUT_OF_SCOPE = "out_of_scope"


# IntentType 字符串别名 → 枚举映射（用于解析 LLM 输出）
_INTENT_ALIASES: Dict[str, IntentType] = {
    "simple_qa": IntentType.SIMPLE_QA,
    "knowledge_lookup": IntentType.KNOWLEDGE_LOOKUP,
    "multi_step_task": IntentType.MULTI_STEP_TASK,
    "out_of_scope": IntentType.OUT_OF_SCOPE,
    # 容错别名（LLM 可能输出的简写形式）
    "simple": IntentType.SIMPLE_QA,
    "knowledge": IntentType.KNOWLEDGE_LOOKUP,
    "multi_step": IntentType.MULTI_STEP_TASK,
    "multistep": IntentType.MULTI_STEP_TASK,
    "out_of_scope_qa": IntentType.OUT_OF_SCOPE,
    "outofscope": IntentType.OUT_OF_SCOPE,
    "oos": IntentType.OUT_OF_SCOPE,
}


# intent classifier 使用的 system prompt（轻量分类指令）
# 设计原则：输出严格 JSON，便于解析；不使用 reasoning（consolidation 强制关闭）。
_INTENT_SYSTEM_PROMPT = """你是意图分类器。根据用户输入和最近对话历史，输出一个 JSON 对象，包含 `intent` 和 `confidence` 两个字段。

intent 取值（仅限以下四个之一）：
- `simple_qa`：通用概念问答、闲聊、寒暄、单轮知识查询（不需要工具或单次工具调用即可回答）
- `knowledge_lookup`：需要检索记忆/文件/向量库的查询（如"之前说过什么"、"文件里有没有X"）
- `multi_step_task`：需要多步工具调用、规划、代码修改、复杂分析的复杂任务
- `out_of_scope`：超出系统能力范围（如请求修改源码、配置文件、需要外部 API 权限等无法完成的任务）

confidence：0.0-1.0 浮点数，表示分类置信度。

输出格式（严格 JSON，不要 markdown 代码块）：
{"intent": "simple_qa", "confidence": 0.9}

判断要点：
- 短问候、感谢、确认词（"好的"、"谢谢"、"嗯"）→ simple_qa
- 含"之前"、"上次"、"历史"、"记得"、"文件里"等检索线索 → knowledge_lookup
- 含多个动作动词（"读取并分析"、"修改并测试"）、或代码修改请求 → multi_step_task
- 请求修改 src/、config.yaml、.env 等受限资源 → out_of_scope
"""


class IntentClassificationResult:
    """意图分类结果。

    封装 IntentType 与置信度，便于 caller 判断是否需要回退到完整 ReactLoop。

    Attributes:
        intent: 分类结果枚举。
        confidence: 分类置信度（0.0-1.0）。
        raw_output: LLM 原始输出文本（用于调试 / 审计）。
        fallback_reason: 降级原因，None 表示正常分类成功。取值：
            - "timeout": LLM 调用超时
            - "llm_failure": LLM 调用抛异常
            - "parse_failure": LLM 输出解析失败
            - "low_confidence": 置信度 < 0.6 触发回退
    """

    def __init__(
        self,
        intent: IntentType,
        confidence: float = 1.0,
        raw_output: str = "",
        fallback_reason: Optional[str] = None,
    ) -> None:
        self.intent: IntentType = intent
        self.confidence: float = max(0.0, min(1.0, float(confidence)))
        self.raw_output: str = raw_output
        self.fallback_reason: Optional[str] = fallback_reason

    def __repr__(self) -> str:
        fb = f", fallback={self.fallback_reason!r}" if self.fallback_reason else ""
        return (
            f"IntentClassificationResult(intent={self.intent.value!r}, "
            f"confidence={self.confidence:.2f}{fb})"
        )


def _parse_intent_output(raw_text: str) -> Optional[IntentClassificationResult]:
    """解析 LLM 输出为 IntentClassificationResult。

    支持严格 JSON 与松散 JSON（含 markdown 代码块、字段缺失等）。
    解析失败返回 None，由 caller 决定降级策略。

    参数:
        raw_text: LLM 输出的原始文本。

    返回:
        IntentClassificationResult 或 None（解析失败时）。
    """
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()

    # 剥离 markdown 代码块（```json ... ``` 或 ``` ... ```）
    code_block_match = re.search(
        r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE
    )
    if code_block_match:
        text = code_block_match.group(1).strip()

    # 尝试提取 JSON 对象（容忍前后多余文本）
    json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not json_match:
        # 非 JSON 输出：尝试直接匹配 intent 别名
        lowered = text.lower()
        for alias, intent_type in _INTENT_ALIASES.items():
            if alias in lowered:
                return IntentClassificationResult(
                    intent=intent_type,
                    confidence=0.5,  # 低置信度，触发回退
                    raw_output=raw_text,
                )
        return None

    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    intent_str = str(data.get("intent", "")).lower().strip()
    intent_type = _INTENT_ALIASES.get(intent_str)
    if intent_type is None:
        return None

    confidence_raw = data.get("confidence", 1.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 1.0

    return IntentClassificationResult(
        intent=intent_type,
        confidence=confidence,
        raw_output=raw_text,
    )


async def classify_intent(
    llm_client: "LLMClient",
    user_input: str,
    history: Optional[List[Dict[str, Any]]] = None,
    cancel_event: Optional[Any] = None,
) -> IntentClassificationResult:
    """对用户输入做意图分类（async，不阻塞事件循环）。

    流程：
        1. 构造轻量 LLM 请求（system + 1-2 轮历史 + 当前 user_input）；
        2. 调用 ``chat_consolidation``（max 200 tokens，强制关闭 reasoning）；
        3. 解析输出为 ``IntentClassificationResult``；
        4. LLM 失败 / 解析失败 → 降级 ``SIMPLE_QA``（confidence=0.0）；
        5. 低置信度（<0.6）→ 回退 ``MULTI_STEP_TASK``（走完整 ReactLoop）。

    参数:
        llm_client: LLM 客户端，需有 ``chat_consolidation`` async 方法。
        user_input: 用户输入文本。
        history: 可选的最近对话历史（最多取最后 2 轮避免 token 膨胀）。
        cancel_event: 可选的取消事件（``threading.Event``），当前实现不主动
                      检查（consolidation LLM 调用本身有超时保护），
                      保留参数为 caller 未来扩展使用。

    返回:
        IntentClassificationResult：始终返回有效结果，不抛异常（fail-safe）。
        - LLM 失败/解析失败 → ``SIMPLE_QA`` + confidence=0.0
        - 低置信度 → ``MULTI_STEP_TASK`` + 原 confidence
        - 正常 → 解析出的 IntentType + LLM 声明的 confidence
    """
    if not user_input or not user_input.strip():
        # 空输入兜底：SIMPLE_QA
        return IntentClassificationResult(
            intent=IntentType.SIMPLE_QA,
            confidence=0.0,
            raw_output="",
        )

    # 构造 messages：system + 最近 2 轮历史 + 当前 user_input
    messages: List[Dict[str, Any]] = []

    # 取最近 2 轮历史（每轮 user + assistant），避免 token 膨胀
    if history:
        recent = history[-4:]  # 最多 4 条消息（2 轮）
        for msg in recent:
            role = msg.get("role")
            content = msg.get("content")
            if role in ("user", "assistant") and content:
                # 截断过长的历史消息（单条最多 500 字符）
                content_str = str(content)[:500]
                messages.append({"role": role, "content": content_str})

    messages.append(
        {
            "role": "user",
            "content": f"请对以下用户输入做意图分类：\n\n{user_input}",
        }
    )

    try:
        response = await llm_client.chat_consolidation(
            messages=messages,
            system=_INTENT_SYSTEM_PROMPT,
            max_tokens=_MAX_INTENT_TOKENS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "intent_classifier LLM 调用超时，降级为 SIMPLE_QA: input=%r",
            user_input[:80],
        )
        return IntentClassificationResult(
            intent=IntentType.SIMPLE_QA,
            confidence=0.0,
            raw_output="",
            fallback_reason="timeout",
        )
    except Exception as e:
        logger.warning(
            "intent_classifier LLM 调用失败，降级为 SIMPLE_QA: %s; input=%r",
            e,
            user_input[:80],
        )
        return IntentClassificationResult(
            intent=IntentType.SIMPLE_QA,
            confidence=0.0,
            raw_output="",
            fallback_reason="llm_failure",
        )

    # 提取 LLM 输出文本
    raw_output = ""
    try:
        if response and response.content:
            for block in response.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    raw_output = block.get("text", "")
                    break
    except Exception as e:
        logger.warning(
            "intent_classifier 提取 LLM 输出失败: %s; raw=%r",
            e,
            getattr(response, "content", None),
        )

    parsed = _parse_intent_output(raw_output)
    if parsed is None:
        # 解析失败：降级 SIMPLE_QA（低置信度，可触发回退但默认 SIMPLE_QA 不回退）
        logger.info(
            "intent_classifier 解析 LLM 输出失败，降级为 SIMPLE_QA: raw=%r",
            raw_output[:200],
        )
        return IntentClassificationResult(
            intent=IntentType.SIMPLE_QA,
            confidence=0.0,
            raw_output=raw_output,
            fallback_reason="parse_failure",
        )

    # 低置信度回退：MULTI_STEP_TASK 是最保守路径（走完整 ReactLoop）
    if parsed.confidence < _MIN_CONFIDENCE:
        logger.info(
            "intent_classifier 置信度低 (%.2f < %.2f)，回退到 MULTI_STEP_TASK: "
            "parsed=%s, input=%r",
            parsed.confidence,
            _MIN_CONFIDENCE,
            parsed.intent.value,
            user_input[:80],
        )
        return IntentClassificationResult(
            intent=IntentType.MULTI_STEP_TASK,
            confidence=parsed.confidence,
            raw_output=raw_output,
            fallback_reason="low_confidence",
        )

    logger.info(
        "intent_classifier 分类结果: %s (confidence=%.2f), input=%r",
        parsed.intent.value,
        parsed.confidence,
        user_input[:80],
    )
    return parsed

