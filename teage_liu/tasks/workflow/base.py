"""WorkflowTemplate 抽象基类 + WorkflowContext + WorkflowResult。

定义工作流模板系统的核心数据结构与执行契约（Phase 8 Task 2.1）。

核心契约：
- ``WorkflowTemplate.execute(config, context) -> WorkflowResult``
- 确定性步骤产出 ``metrics_for_injection``（dict，结构由模板自定义）
- LLM 步骤通过 ``context.llm_client.chat_main`` 单轮调用（``max_loops=1``）
- 时间变量通过 ``render_time_variables`` 在模板入口替换 ``config`` 与
  ``task`` 文本中的 ``{now}`` / ``{today}`` / ``{this_week_start}`` /
  ``{last_run_time}`` 占位符

缓存约束（5 条硬约束）：
- 模板的 system prompt **禁含动态变量**：模板实现需保证 ``build_system_prompt``
  返回值不依赖 ``current_time`` / ``last_run_time`` 等动态值
- 时间变量只放 messages[0]：通过 ``render_time_variables`` 在用户输入层替换
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# 时间变量替换（SubTask 2.7 第一层：模板变量层）
# ---------------------------------------------------------------------------

# 支持的时间变量占位符
_TIME_PLACEHOLDERS = ("{now}", "{today}", "{this_week_start}", "{last_run_time}")


def render_time_variables(
    text: Optional[str],
    current_time: Optional[datetime] = None,
    last_run_time: Optional[datetime] = None,
) -> str:
    """将时间变量占位符替换为实际值（SubTask 2.7 第一层）。

    支持的占位符（在 ``text`` 中出现即替换，未出现则跳过）：
    - ``{now}``：当前时间，ISO 格式（``%Y-%m-%d %H:%M:%S``）
    - ``{today}``：当前日期（``%Y-%m-%d``）
    - ``{this_week_start}``：本周一日期（``%Y-%m-%d``，周一为一周起点）
    - ``{last_run_time}``：上次执行时间，ISO 格式；为 ``None`` 时替换为
      字符串 ``"首次执行"``（空值容错）

    参数:
        text: 待替换的文本。为 ``None`` 或空字符串时原样返回。
        current_time: 当前时间。为 ``None`` 时取 ``datetime.now()``。
        last_run_time: 上次执行时间。为 ``None`` 时 ``{last_run_time}``
            替换为 ``"首次执行"``。

    返回:
        替换后的文本。无占位符时原样返回。
    """
    if not text:
        return text or ""

    now = current_time or datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    # 周一为一周起点（weekday() 周一=0 ... 周日=6）
    this_week_start = now - timedelta(days=now.weekday())
    this_week_start_str = this_week_start.strftime("%Y-%m-%d")
    last_run_str = (
        last_run_time.strftime("%Y-%m-%d %H:%M:%S")
        if last_run_time is not None
        else "首次执行"
    )

    return (
        text.replace("{now}", now.strftime("%Y-%m-%d %H:%M:%S"))
        .replace("{today}", today_str)
        .replace("{this_week_start}", this_week_start_str)
        .replace("{last_run_time}", last_run_str)
    )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class WorkflowContext:
    """工作流执行上下文。

    封装模板执行所需的全部运行时依赖：LLM 客户端、向量库、报告目录、
    当前时间、上次执行时间、环境变量取值回调等。由 CronScheduler 在
    触发调度项时构造并传入 ``WorkflowTemplate.execute``。

    属性:
        session_id: cron 会话 ID（形如 ``cron:<cron_id>``）。
        schedule_id: 调度项 ID（= ``cron_id``）。
        llm_client: LLM 客户端实例，用于 LLM 步骤调用。为 ``None`` 时
            LLM 步骤降级跳过（仅执行确定性步骤）。
        chroma_store: ChromaMemoryStore 实例，用于查询/写入记忆。为 ``None``
            时记忆相关步骤降级跳过。
        report_dir: 报告输出目录路径。模板将生成的报告文件写入此目录。
        current_time: 当前时间。模板用此值生成文件名与时间变量替换。
        last_run_time: 上次执行时间。用于 ``{last_run_time}`` 替换与
            ``days_since_last`` 计算。为 ``None`` 表示首次执行。
        get_env: 环境变量取值回调，签名 ``(key: str, default: str = "") -> str``。
            用于模板获取 SMTP 配置等环境变量。为 ``None`` 时回退到
            ``os.environ.get``。
        error_channel: LLM 调用等步骤的异常记录通道（D5 修复）。
            ``append_error`` 追加错误信息，``WorkflowEngine.execute`` 末尾
            合并到 ``WorkflowResult.errors``。为空列表时无异常。
        step_outputs: 已完成 step 的产出字典，key 为 step_id，value 为
            该 step 的 ``outputs`` dict。供后续 step（如 LLM 步骤）通过
            ``depends_on`` 引用前置步骤的输出结果。
    """

    session_id: str
    schedule_id: str
    llm_client: Optional[Any] = None
    chroma_store: Optional[Any] = None
    report_dir: str = "data/reports"
    current_time: datetime = field(default_factory=datetime.now)
    last_run_time: Optional[datetime] = None
    get_env: Optional[Callable[[str, str], str]] = None
    error_channel: List[str] = field(default_factory=list)
    step_outputs: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # === Q2 新增：hook 间传递的状态字段（5 个，全部带默认值，向后兼容）===
    schedule: Optional[Any] = None          # 调度项引用（hooks 需要）
    retry_count: int = 0                     # 当前重试次数
    retry_max: int = 0                       # 最大重试次数（HookRegistry.get_retry_max() 写入）
    last_error: Optional[Exception] = None   # 最近一次异常
    validation_errors: list = field(default_factory=list)  # 校验错误列表

    def get_env_value(self, key: str, default: str = "") -> str:
        """获取环境变量值（兼容 ``get_env=None`` 场景）。"""
        if self.get_env is not None:
            try:
                return self.get_env(key, default)
            except Exception:
                return default
        return os.environ.get(key, default)

    def append_error(self, message: str) -> None:
        """向 error_channel 追加错误信息（D5 修复）。

        供 ``_call_llm_single_turn`` 等步骤在异常时调用，将错误信息
        收集到 ``error_channel``，由 ``WorkflowEngine.execute`` 末尾
        合并到 ``WorkflowResult.errors``，确保 LLM 异常被上层感知。

        参数:
            message: 一行精炼错误描述（如 ``"LLM 调用失败: timeout"``）。
        """
        if message:
            self.error_channel.append(message)

    def render(self, text: Optional[str]) -> str:
        """用当前上下文的时间变量替换 ``text`` 中的占位符。

        便捷方法，等价于 ``render_time_variables(text, current_time,
        last_run_time)``。
        """
        return render_time_variables(
            text, current_time=self.current_time, last_run_time=self.last_run_time
        )

    def ensure_report_dir(self) -> str:
        """确保报告目录存在，返回路径。"""
        os.makedirs(self.report_dir, exist_ok=True)
        return self.report_dir


@dataclass
class WorkflowResult:
    """工作流执行结果。

    封装模板执行后的产出：LLM 回复、工具调用列表、文件输出、错误信息、
    以及待注入到下一轮 cron 上下文的工作流数据。

    属性:
        success: 是否执行成功（无致命错误）。
        assistant_response: LLM 步骤的回复文本。纯确定性模板（如
            ``email_notify``）此字段为空字符串。
        tool_calls: 工具调用列表，每项形如 ``{"name": str, "input": dict,
            "result": str, "is_error": bool}``。
        outputs: 文件输出列表，每项形如 ``{"path": str, "type": str}``，
            ``type`` 如 ``"report"`` / ``"snapshot"`` 等。
        errors: 错误信息列表（非致命错误也记录）。
        metrics_for_injection: 待注入到下一轮 cron 上下文 messages[0] 的
            工作流数据。结构由模板自定义，调用方（CronScheduler）将其
            序列化为 markdown 段拼接。为空 dict 时不注入。
        step_traces: step 执行轨迹列表（Task 8）。每项为
            :class:`StepTrace` 实例。旧路径无 step_traces 时为空列表。
            通过 ``add_step_trace`` 追加。
        run_id: 执行批次 ID（可选），关联该次触发的所有工具调用。
            非 cron 会话为 ``None``。
        workflow_name: workflow 名称（用于报告展示）。为 ``None`` 时
            调用方可用 schedule.name 替代。
    """

    success: bool = True
    assistant_response: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    outputs: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    metrics_for_injection: Dict[str, Any] = field(default_factory=dict)
    step_traces: List[Any] = field(default_factory=list)
    run_id: Optional[str] = None
    workflow_name: Optional[str] = None

    def add_error(self, msg: str) -> None:
        """记录非致命错误并标记 ``success=False``。"""
        self.errors.append(msg)
        self.success = False

    def add_step_trace(self, trace: Any) -> None:
        """追加一条 step 执行轨迹（Task 8.1）。

        参数:
            trace: :class:`StepTrace` 实例（避免循环导入，类型注解为 Any）。
        """
        if trace is not None:
            self.step_traces.append(trace)

    def to_injection_text(self) -> str:
        """将 ``metrics_for_injection`` 序列化为 markdown 段。

        供 ContextManager 拼接到 cron messages[0]。空 dict 时返回空字符串。

        返回:
            markdown 格式文本，形如::

                ## 工作流数据
                - key1: value1
                - key2: value2
        """
        if not self.metrics_for_injection:
            return ""
        lines = ["## 工作流数据"]
        for key, value in self.metrics_for_injection.items():
            lines.append(f"- {key}: {value}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class WorkflowTemplate(abc.ABC):
    """工作流模板抽象基类。

    子类必须实现 :meth:`execute`，封装「确定性步骤 + LLM 步骤」的两阶段
    执行逻辑。模板实现需遵守缓存约束：

    - :meth:`build_system_prompt` 返回值必须**不含动态变量**（缓存约束 5）
    - 时间变量只能通过 :meth:`WorkflowContext.render` 在用户输入层替换
      （缓存约束 3）
    - LLM 步骤单轮调用（``max_loops=1``），除 ``research`` 可配置

    属性:
        name: 模板名称（如 ``"directory_watch"``），用于调度项配置匹配。
    """

    #: 模板名称，子类必须覆盖
    name: str = "abstract"

    @abc.abstractmethod
    def execute(
        self, config: Dict[str, Any], context: WorkflowContext
    ) -> WorkflowResult:
        """执行工作流模板。

        参数:
            config: 模板配置 dict，结构由子类定义（如 ``directory_watch``
                含 ``watch_path`` 字段）。
            context: 工作流执行上下文，提供 LLM 客户端、向量库、报告目录等。

        返回:
            :class:`WorkflowResult` 实例。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 共享辅助方法
    # ------------------------------------------------------------------
    def build_system_prompt(self) -> str:
        """返回模板固定的 system prompt（不含动态变量，缓存约束 5）。

        子类可覆盖以提供自己的固定 prompt。默认返回空字符串（由调用方
        使用全局 SYSTEM_PROMPT）。
        """
        return ""

    def _call_llm_single_turn(
        self,
        context: WorkflowContext,
        user_input: str,
        system: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple:
        """单轮 LLM 调用（``max_loops=1`` 语义）。

        便捷方法，子类用于执行 LLM 步骤。调用 ``context.llm_client.chat_main``
        一次，返回 ``(response_text, tool_calls)``：

        - ``response_text``：LLM 输出的文本（拼接所有 text block）
        - ``tool_calls``：工具调用列表，每项 ``{"name", "input", "result",
          "is_error"}``；本方法不实际执行工具（``tools`` 仅作为 schema 传入
          LLM），故 ``result`` 字段为空字符串、``is_error=False``。
          实际工具执行由 ``research`` 模板通过 ``ReactLoop`` 完成。

        LLM 客户端为 ``None`` 或调用失败时返回 ``("", [])`` 并记录到
        ``context``（无 error 通道时静默）。

        参数:
            context: 工作流上下文。
            user_input: 用户输入文本（已替换时间变量）。
            system: 可选 system prompt。
            tools: 可选工具 schema 列表。

        返回:
            ``(response_text, tool_calls)`` 元组。
        """
        if context.llm_client is None:
            return "", []

        messages = [{"role": "user", "content": user_input}]
        try:
            response = context.llm_client.chat_main_sync(
                messages=messages,
                tools=tools,
                system=system,
            )
        except Exception as e:
            # D5 修复：LLM 调用失败时写入 context.error_channel
            # （由 WorkflowEngine.execute 末尾合并到 WorkflowResult.errors）
            try:
                context.append_error(f"LLM 调用失败: {type(e).__name__}: {e}")
            except Exception:
                pass
            return "", []

        response_text = ""
        tool_calls: List[Dict[str, Any]] = []
        content_blocks = getattr(response, "content", []) or []
        for block in content_blocks:
            bdict = block if isinstance(block, dict) else _block_to_dict(block)
            btype = bdict.get("type")
            if btype == "text":
                response_text += bdict.get("text", "")
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "name": bdict.get("name", ""),
                        "input": bdict.get("input", {}) or {},
                        "result": "",
                        "is_error": False,
                    }
                )
        return response_text, tool_calls


def _block_to_dict(block: Any) -> Dict[str, Any]:
    """将 Anthropic content block 对象转为 dict（兼容 mock 与真实 SDK）。"""
    if isinstance(block, dict):
        return block
    # 真实 anthropic SDK 的 TextBlock / ToolUseBlock 等支持 .model_dump()
    if hasattr(block, "model_dump"):
        try:
            return block.model_dump()
        except Exception:
            pass
    # 退化：按属性读取
    btype = getattr(block, "type", "")
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}) or {},
        }
    return {"type": btype}
