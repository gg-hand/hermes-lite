"""Phase 8 Task 3.2-3.5: cron 工具集（Core Tier）+ 提议-确认协议入口。

本模块提供 4 个 Core Tier cron 工具，注册到全局 ``ToolRegistry`` 供**用户
会话**（非 cron 执行会话）使用：

- ``list_schedules``：列出所有调度项（精简字段，不含 granted_tools 等敏感
  配置）。默认放行（read-only）。
- ``propose_schedule``：提议新调度项。检测硬禁止工具（``memory_delete`` /
  ``bash_exec`` / ``tool_call``），通过检测则生成 ``proposal_id``
  存入 ``ProposalStore``，不实际创建调度。默认放行。
- ``create_schedule``：根据已确认的 proposal 创建调度项。校验 proposal
  status=confirmed/modified，调用 ``cron_scheduler.add_schedule``，创建时
 锁定 ``active_tools_snapshot``。**需用户确认**（高危）。
- ``update_schedule``：更新已有调度项。工具集变更时重新锁定
  ``active_tools_snapshot``。**需用户确认**（高危）。

设计要点：
- **硬禁止清单**：``HARD_DISABLED_TOOLS`` 中的工具不可预授权，
  ``propose_schedule`` 入口检测到 ``requested_tools`` 含硬禁止项时直接
  返回错误，不生成 proposal。Task 4 会实装 ``PolicyEngine`` 的完整三层
  检查，本模块只在 propose 入口做检测。
- **active_tools_snapshot 锁定**：``create_schedule`` 从 proposal 的
  ``requested_tools`` 解析工具名列表（含版本，格式 ``name@version``，
  当前版本占位为 ``name``），写入调度项的 ``active_tools_snapshot`` 字段。
  缓存约束 2：调度项创建时锁定工具快照，后续触发时基于快照请求级过滤。
- **confirm 权限**：``create_schedule`` / ``update_schedule`` 在工具
  description 中标注 ``[需确认]``。实际 confirm 行为通过提议-确认协议
  实现——``create_schedule`` 校验 proposal 已 confirmed/modified，未确认
  的 proposal 调用直接返回错误。
- **请求级隔离**：这 4 个工具是用户会话工具，注册到全局 ToolRegistry
  是正确的。它们不涉及 cron 执行会话的 tools schema，不污染 cron 缓存
  （缓存约束 1）。

模块依赖：
- ``ProposalStore``（本 Task 3.1）
- ``CronScheduler``（Task 1 已增字段支持，本模块只调用现有接口）
- ``ToolRegistry``（Phase 5 已存在）

运行时依赖通过 ``register_cron_tools`` 参数注入，便于测试 mock。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List

from .cron_proposals import ProposalStore

if TYPE_CHECKING:  # 仅用于类型检查，运行时不导入以避免循环依赖
    from ..tasks.scheduler import CronScheduler
    from .tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 硬禁止工具清单（安全边界硬约束）
# ---------------------------------------------------------------------------

# 这三个工具不可预授权，propose_schedule 检测到 requested_tools 含其中任一
# 项时返回错误。Task 4 会实装 PolicyEngine 的完整三层检查，此处仅做入口检测。
HARD_DISABLED_TOOLS = {"memory_delete", "bash_exec", "tool_call"}

# list_schedules 返回的精简字段（不含 granted_tools / active_tools_snapshot
# 等敏感配置，避免泄露权限快照）
_SCHEDULE_PUBLIC_FIELDS = (
    "id",
    "name",
    "cron",
    "task",
    "enabled",
    "last_run",
    "next_run",
)


# ---------------------------------------------------------------------------
# 工具集注册
# ---------------------------------------------------------------------------


def register_cron_tools(
    registry: "ToolRegistry",
    cron_scheduler: "CronScheduler",
    proposal_store: ProposalStore,
) -> None:
    """注册 4 个 cron 工具到 ``ToolRegistry`` 的 Core Tier（用户会话可用）。

    所有工具通过 ``register_deferred`` 注册为 Deferred Tier（低频调度工具，按需加载，不占缓存 key）。
    工具 handler 通过 closure 捕获 ``cron_scheduler`` 与 ``proposal_store``
    实例。

    参数:
        registry: ToolRegistry 实例。
        cron_scheduler: CronScheduler 实例，提供 list_schedules /
            add_schedule / update_schedule 接口。
        proposal_store: ProposalStore 实例，管理提议生命周期。
    """
    _register_list_schedules(registry, cron_scheduler)
    _register_propose_schedule(registry, proposal_store)
    _register_create_schedule(registry, cron_scheduler, proposal_store)
    _register_update_schedule(registry, cron_scheduler)


# ---------------------------------------------------------------------------
# 工具 1: list_schedules（read-only，默认放行）
# ---------------------------------------------------------------------------


def _register_list_schedules(
    registry: "ToolRegistry",
    cron_scheduler: "CronScheduler",
) -> None:
    """注册 ``list_schedules`` 工具（Core Tier，read-only）。"""

    def _list_schedules() -> str:
        """list_schedules 工具 handler（closure 捕获 cron_scheduler）。

        调用 ``cron_scheduler.list_schedules()`` 取全量调度项，过滤为精简
        字段（不含 granted_tools / active_tools_snapshot 等敏感配置）。

        返回:
            JSON 字符串，形如 ``{"schedules": [{id, name, cron, task,
            enabled, last_run, next_run}], "total": N}``。
        """
        try:
            all_schedules = cron_scheduler.list_schedules()
            public = [
                {k: s.get(k) for k in _SCHEDULE_PUBLIC_FIELDS}
                for s in all_schedules
            ]
            return json.dumps(
                {"schedules": public, "total": len(public)},
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            return f"list_schedules 执行出错: {e}"

    registry.register_deferred(
        name="cron_list",
        description=(
            "列出当前所有 cron 调度项（精简视图）。返回每个调度项的 id / name "
            "/ cron / task / enabled / last_run / next_run 字段，不含权限配置。"
            "用于查看已有调度项状态。只读操作，无需确认。"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_list_schedules,
    )


# ---------------------------------------------------------------------------
# 工具 2: propose_schedule（默认放行，硬禁止检测）
# ---------------------------------------------------------------------------


def _register_propose_schedule(
    registry: "ToolRegistry",
    proposal_store: ProposalStore,
) -> None:
    """注册 ``propose_schedule`` 工具（Core Tier，默认放行）。"""

    def _propose_schedule(
        schedule_config: dict,
        requested_tools: list,
        llm_explanation: str,
    ) -> str:
        """propose_schedule 工具 handler（closure 捕获 proposal_store）。

        流程：
        1. 检测 ``requested_tools`` 是否含硬禁止工具
           （``memory_delete`` / ``bash_exec`` / ``tool_call``）。
           含硬禁止项则返回错误，不生成 proposal。
        2. 通过检测则调 ``proposal_store.create`` 生成 ``proposal_id``。
        3. 返回 ``proposal_id``，提示用户需在前端确认卡片确认后才创建调度。

        参数:
            schedule_config: 调度配置 dict，含 ``name`` / ``cron`` / ``task``
                / 可选 ``enabled`` / ``workflow`` / ``generate_llm_summary``。
            requested_tools: 请求预授权的工具列表，每项形如
                ``{"tool": str, "scope": "all"|"path_prefix", "allowed_paths": [...]}``。
            llm_explanation: LLM 提议说明（人类可读），展示在确认卡片上。

        返回:
            成功返回 JSON ``{"proposal_id": "...", "status": "pending_confirm",
            "message": "..."}``；硬禁止工具返回错误信息字符串。
        """
        try:
            # 1. 硬禁止工具检测
            tools_list = requested_tools or []
            hard_disabled_found = []
            for tool_entry in tools_list:
                if not isinstance(tool_entry, dict):
                    continue
                tool_name = tool_entry.get("tool", "")
                if tool_name in HARD_DISABLED_TOOLS:
                    hard_disabled_found.append(tool_name)
            if hard_disabled_found:
                return (
                    f"错误：以下工具不可预授权（安全边界硬约束）: "
                    f"{', '.join(sorted(set(hard_disabled_found)))}。"
                    f"请从 requested_tools 中移除后重新提议。"
                )

            # 2. 基本校验：schedule_config 含必填字段
            if not schedule_config or not isinstance(schedule_config, dict):
                return "错误：schedule_config 不能为空"
            cron_expr = schedule_config.get("cron", "")
            task_text = schedule_config.get("task", "")
            if not cron_expr or not str(cron_expr).strip():
                return "错误：schedule_config.cron 必填且不能为空"
            if not task_text or not str(task_text).strip():
                return "错误：schedule_config.task 必填且不能为空"

            # 3. 创建 proposal
            proposal_id = proposal_store.create(
                schedule_config=schedule_config,
                requested_tools=tools_list,
                llm_explanation=llm_explanation or "",
            )
            return json.dumps(
                {
                    "proposal_id": proposal_id,
                    "status": "pending_confirm",
                    "message": (
                        "提议已创建，等待用户在确认卡片上确认。"
                        "用户确认后才会创建调度项。"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            return f"propose_schedule 执行出错: {e}"

    registry.register_deferred(
        name="cron_propose",
        description=(
            "提议一个新的 cron 调度项，返回 proposal_id 等待用户确认。"
            "不会立即创建调度项——用户需在前端确认卡片上点击「确认」/"
            "「修改并确认」后才会实际创建。"
            "requested_tools 中不可包含硬禁止工具（memory_delete / "
            "bash_exec / tool_call），否则返回错误。"
            "参数 schedule_config 含 name/cron/task 及可选 enabled/workflow/"
            "generate_llm_summary；requested_tools 为预授权工具列表，每项含 "
            "tool/scope/allowed_paths；llm_explanation 为向用户说明的提议理由。"
            "只读操作（仅创建提议），无需确认。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "schedule_config": {
                    "type": "object",
                    "description": (
                        "调度配置。必填字段：cron（5 字段 cron 表达式）、"
                        "task（触发时执行的任务描述）。可选：name（调度名称）、"
                        "enabled（默认 true）、workflow（工作流模板配置）、"
                        "generate_llm_summary（默认 false）。"
                    ),
                    "properties": {
                        "name": {"type": "string", "description": "调度项名称"},
                        "cron": {
                            "type": "string",
                            "description": "5 字段 cron 表达式，如 '0 9 * * *'",
                        },
                        "task": {
                            "type": "string",
                            "description": "触发时执行的任务描述",
                        },
                        "enabled": {
                            "type": "boolean",
                            "description": "是否启用，默认 true",
                            "default": True,
                        },
                        "workflow": {
                            "type": "object",
                            "description": "可选工作流模板配置",
                        },
                        "generate_llm_summary": {
                            "type": "boolean",
                            "description": "是否生成 LLM 摘要，默认 false",
                            "default": False,
                        },
                    },
                    "required": ["cron", "task"],
                },
                "requested_tools": {
                    "type": "array",
                    "description": (
                        "请求预授权的工具列表。每项含 tool（工具名）、"
                        "scope（'all' 或 'path_prefix'）、allowed_paths（路径前缀"
                        "列表，scope=path_prefix 时必填）。不可包含硬禁止工具"
                        "（memory_delete / bash_exec / tool_call）。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string", "description": "工具名"},
                            "scope": {
                                "type": "string",
                                "enum": ["all", "path_prefix"],
                                "description": "授权范围",
                            },
                            "allowed_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "允许的路径前缀列表",
                            },
                        },
                        "required": ["tool", "scope"],
                    },
                },
                "llm_explanation": {
                    "type": "string",
                    "description": "向用户说明的提议理由（展示在确认卡片上）",
                },
            },
            "required": ["schedule_config", "requested_tools", "llm_explanation"],
        },
        handler=_propose_schedule,
    )


# ---------------------------------------------------------------------------
# 工具 3: create_schedule（confirm，从 proposal 创建调度项）
# ---------------------------------------------------------------------------


def _register_create_schedule(
    registry: "ToolRegistry",
    cron_scheduler: "CronScheduler",
    proposal_store: ProposalStore,
) -> None:
    """注册 ``create_schedule`` 工具（Core Tier，需确认）。"""

    def _create_schedule(proposal_id: str) -> str:
        """create_schedule 工具 handler（closure 捕获 scheduler + store）。

        流程：
        1. 按 ``proposal_id`` 取 proposal，校验 status 为 ``confirmed`` 或
           ``modified``。其他状态（``pending_confirm`` / ``rejected`` /
           ``schedule_active``）返回错误。
        2. 从 proposal 的 ``schedule_config`` + ``requested_tools`` 构造
           ``add_schedule`` 入参，含 ``granted_tools``（= requested_tools）
           与 ``active_tools_snapshot``（从 requested_tools 解析工具名列表）。
        3. 调用 ``cron_scheduler.add_schedule`` 创建调度项。
        4. 调用 ``proposal_store.mark_schedule_active`` 标记 proposal 终态。
        5. 返回 ``schedule_id``。

        参数:
            proposal_id: 已确认的提议 ID。

        返回:
            成功返回 JSON ``{"schedule_id": "...", "proposal_id": "...",
            "status": "schedule_active"}``；校验失败返回错误信息字符串。
        """
        try:
            # 1. 取 proposal 并校验状态
            proposal = proposal_store.get(proposal_id)
            if proposal is None:
                return f"错误：proposal {proposal_id} 不存在"
            if proposal.status not in ("confirmed", "modified"):
                return (
                    f"错误：proposal {proposal_id} 当前状态为 {proposal.status}，"
                    f"需用户确认（confirmed/modified）后才能创建调度项。"
                )

            # 2. 构造调度项入参
            sched_config = dict(proposal.schedule_config)
            requested_tools = list(proposal.requested_tools or [])
            # granted_tools = requested_tools（用户确认后的权限快照）
            sched_config["granted_tools"] = requested_tools
            # active_tools_snapshot: 从 requested_tools 解析工具名列表
            sched_config["active_tools_snapshot"] = _extract_tool_snapshot(
                requested_tools
            )

            # 3. 创建调度项
            try:
                schedule_id = cron_scheduler.add_schedule(sched_config)
            except ValueError as e:
                return f"错误：创建调度项失败（cron 表达式非法？）: {e}"

            # 4. 标记 proposal 终态
            proposal_store.mark_schedule_active(proposal_id, schedule_id)

            return json.dumps(
                {
                    "schedule_id": schedule_id,
                    "proposal_id": proposal_id,
                    "status": "schedule_active",
                    "active_tools_snapshot": sched_config[
                        "active_tools_snapshot"
                    ],
                    "message": "调度项已创建，工具快照已锁定",
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            return f"create_schedule 执行出错: {e}"

    registry.register_deferred(
        name="cron_create",
        description=(
            "[需确认] 根据已确认的 proposal 创建 cron 调度项。"
            "调用前用户必须在前端确认卡片上点击「确认」或「修改并确认」，"
            "使 proposal 状态转为 confirmed/modified。"
            "创建时锁定 active_tools_snapshot（从 proposal 的 requested_tools "
            "解析工具名列表），保证后续触发时工具集字节级稳定（缓存约束 2）。"
            "高危操作——未确认的 proposal 调用此工具会返回错误。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "proposal_id": {
                    "type": "string",
                    "description": "已确认的提议 ID（用户在确认卡片确认后获得）",
                },
            },
            "required": ["proposal_id"],
        },
        handler=_create_schedule,
    )


# ---------------------------------------------------------------------------
# 工具 4: update_schedule（confirm，更新调度项 + 重新锁定快照）
# ---------------------------------------------------------------------------


def _register_update_schedule(
    registry: "ToolRegistry",
    cron_scheduler: "CronScheduler",
) -> None:
    """注册 ``update_schedule`` 工具（Core Tier，需确认）。"""

    def _update_schedule(
        schedule_id: str,
        fields: dict,
    ) -> str:
        """update_schedule 工具 handler（closure 捕获 cron_scheduler）。

        流程：
        1. 接收 ``schedule_id`` + ``fields``（修改字段 dict）。
        2. 若 ``fields`` 含 ``granted_tools``，则同步重新计算并写入
           ``active_tools_snapshot``（从新的 granted_tools 解析工具名列表），
           保证工具集变更时快照重新锁定。
        3. 调用 ``cron_scheduler.update_schedule`` 应用更新。
        4. 返回更新结果。

        参数:
            schedule_id: 待更新的调度项 ID。
            fields: 待更新字段 dict，支持 ``name`` / ``cron`` / ``task`` /
                ``enabled`` / ``granted_tools`` / ``active_tools_snapshot`` /
                ``workflow`` / ``generate_llm_summary``。其中 ``granted_tools``
                变更时会自动重新锁定 ``active_tools_snapshot``。

        返回:
            成功返回 JSON ``{"schedule_id": "...", "updated": true,
            "snapshot_relocked": bool}``；调度项不存在返回错误。
        """
        try:
            if not schedule_id:
                return "错误：schedule_id 不能为空"
            if not fields or not isinstance(fields, dict):
                return "错误：fields 不能为空"

            # 工具集变更时重新锁定 active_tools_snapshot
            snapshot_relocked = False
            update_fields = dict(fields)
            if "granted_tools" in update_fields:
                new_granted = update_fields["granted_tools"] or []
                update_fields["active_tools_snapshot"] = _extract_tool_snapshot(
                    new_granted
                )
                snapshot_relocked = True

            # 应用更新
            try:
                ok = cron_scheduler.update_schedule(schedule_id, update_fields)
            except ValueError as e:
                return f"错误：更新调度项失败（cron 表达式非法？）: {e}"
            if not ok:
                return f"错误：调度项 {schedule_id} 不存在"

            return json.dumps(
                {
                    "schedule_id": schedule_id,
                    "updated": True,
                    "snapshot_relocked": snapshot_relocked,
                    "message": (
                        "调度项已更新，工具快照已重新锁定"
                        if snapshot_relocked
                        else "调度项已更新"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            return f"update_schedule 执行出错: {e}"

    registry.register_deferred(
        name="cron_update",
        description=(
            "[需确认] 更新已有 cron 调度项的字段。支持更新 name / cron / task "
            "/ enabled / granted_tools / workflow / generate_llm_summary。"
            "当 granted_tools 变更时，自动重新锁定 active_tools_snapshot"
            "（从新的 granted_tools 解析工具名列表），保证缓存约束 2。"
            "高危操作——会修改调度项配置与权限快照。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "string",
                    "description": "待更新的调度项 ID",
                },
                "fields": {
                    "type": "object",
                    "description": (
                        "待更新字段。可含 name / cron / task / enabled / "
                        "granted_tools / workflow / generate_llm_summary。"
                        "granted_tools 变更时自动重新锁定 active_tools_snapshot。"
                    ),
                    "properties": {
                        "name": {"type": "string"},
                        "cron": {"type": "string"},
                        "task": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "granted_tools": {
                            "type": "array",
                            "description": "新的预授权工具列表",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tool": {"type": "string"},
                                    "scope": {
                                        "type": "string",
                                        "enum": ["all", "path_prefix"],
                                    },
                                    "allowed_paths": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["tool", "scope"],
                            },
                        },
                        "workflow": {"type": "object"},
                        "generate_llm_summary": {"type": "boolean"},
                    },
                },
            },
            "required": ["schedule_id", "fields"],
        },
        handler=_update_schedule,
    )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _extract_tool_snapshot(
    granted_tools: List[Dict[str, Any]],
) -> List[str]:
    """从 granted_tools 列表解析 active_tools_snapshot（工具名列表）。

    每项 granted_tools 形如 ``{"tool": "file_write", "scope": ...,
    "allowed_paths": [...]}``，提取 ``tool`` 字段作为快照项。

    当前版本工具未引入版本号，快照项即工具名。Task 5 引入 cron_tool 后，
    快照项格式扩展为 ``name@version``（如 ``send_email@1.0.0``），本函数
    届时需扩展解析逻辑。

    参数:
        granted_tools: granted_tools 列表。

    返回:
        工具名列表（去重保序）。
    """
    seen = set()
    snapshot: List[str] = []
    for entry in granted_tools or []:
        if not isinstance(entry, dict):
            continue
        tool_name = entry.get("tool", "")
        if not tool_name or tool_name in seen:
            continue
        seen.add(tool_name)
        snapshot.append(tool_name)
    return snapshot


def is_hard_disabled(tool_name: str) -> bool:
    """检查工具名是否在硬禁止清单中。

    参数:
        tool_name: 工具名。

    返回:
        在硬禁止清单中返回 ``True``，否则 ``False``。
    """
    return tool_name in HARD_DISABLED_TOOLS
