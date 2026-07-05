"""WorkflowValidator 静态校验器（Task 4）。

在 ``cron_propose`` 与 ``cron_update`` 阶段对 workflow 配置做静态校验，
拦截非法配置进入运行时。

校验项：
1. step id 唯一性
2. depends_on 无环（DFS）
3. step.type 合法（ALLOWED_STEP_TYPES）
4. 模板名存在（deterministic 类型，BUILTIN_TEMPLATES）
5. 工具白名单项存在（tool 类型，tool_registry 提供）
6. 写操作工具 path_prefix 约束（file_write / file_edit / file_delete /
   memory_delete / memory_update 等）
7. 硬禁止工具检测（针对 tool 类型 step 的 ``config.tool`` 字段，
   硬禁止清单 ``{"memory_delete", "bash_exec", "tool_call"}``）

校验结果分为 errors（阻断）与 warnings（非阻断），errors 非空时
``valid=False``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .spec import (
    ALLOWED_STEP_TYPES,
    OnFailure,
    RetryPolicy,
    StepSpec,
    WorkflowSpec,
)

# 硬禁止工具清单（与 cron_tools.HARD_DISABLED_TOOLS 保持一致，
# 此处独立声明避免循环导入）
HARD_DISABLED_TOOLS: Set[str] = {"memory_delete", "bash_exec", "tool_call"}

# 写操作工具清单（需 path_prefix 约束）。
# 来源：policy.DEFAULT_RULES 中 risk=confirm 的写类工具，
# 不含纯读取类（file_read / file_query / search_memory 等）。
WRITE_OPERATION_TOOLS: Set[str] = {
    "file_write",
    "file_edit",
    "file_delete",
    "memory_delete",
    "memory_update",
    "profile_update",
}

# 简易模式裸 dict 兼容字段：当 workflow 仅含 template + 模板配置字段时，
# 不需要 steps 校验，直接放行。
_SIMPLE_MODE_KEYS = {"template", "template_config", "name", "version",
                     "on_failure", "timeout_seconds", "metadata"}


@dataclass
class ValidationError:
    """单个校验错误 / 警告。

    属性:
        step_id: 相关 step 的 id（workflow 级错误为空字符串）。
        field: 出错字段路径（如 ``config.tool`` / ``depends_on``）。
        message: 错误描述（一行精炼）。
        severity: ``error`` 或 ``warning``。
    """

    step_id: str = ""
    field: str = ""
    message: str = ""
    severity: str = "error"

    def to_dict(self) -> Dict[str, str]:
        return {
            "step_id": self.step_id,
            "field": self.field,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class ValidationResult:
    """校验结果。

    属性:
        valid: 是否通过（errors 为空时 True）。
        errors: 错误列表（阻断，severity=error）。
        warnings: 警告列表（非阻断，severity=warning）。
    """

    valid: bool = True
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)

    def add_error(self, step_id: str, field_name: str, message: str) -> None:
        self.errors.append(ValidationError(
            step_id=step_id, field=field_name, message=message, severity="error"
        ))
        self.valid = False

    def add_warning(self, step_id: str, field_name: str, message: str) -> None:
        self.warnings.append(ValidationError(
            step_id=step_id, field=field_name, message=message, severity="warning"
        ))

    def merge(self, other: "ValidationResult") -> "ValidationResult":
        """合并另一校验结果，返回 self。"""
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        if other.errors:
            self.valid = False
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
        }


class WorkflowValidator:
    """workflow 静态校验器。

    用法::

        validator = WorkflowValidator()
        result = validator.validate(spec, tool_registry=registry)
        if not result.valid:
            for err in result.errors:
                print(f"[{err.step_id}] {err.field}: {err.message}")
    """

    def __init__(
        self,
        builtin_templates: Optional[Dict[str, Any]] = None,
    ) -> None:
        """初始化校验器。

        参数:
            builtin_templates: 内置模板注册表（name → class）。
                为 ``None`` 时延迟导入 ``BUILTIN_TEMPLATES``。
        """
        self._builtin_templates = builtin_templates

    def _get_builtin_templates(self) -> Dict[str, Any]:
        """延迟加载内置模板注册表（避免循环导入）。"""
        if self._builtin_templates is None:
            try:
                from . import BUILTIN_TEMPLATES  # type: ignore[import]
                self._builtin_templates = BUILTIN_TEMPLATES
            except ImportError:
                self._builtin_templates = {}
        return self._builtin_templates

    def validate(
        self,
        spec: WorkflowSpec,
        tool_registry: Any = None,
    ) -> ValidationResult:
        """校验 WorkflowSpec。

        参数:
            spec: 待校验的 WorkflowSpec 实例。
            tool_registry: 可选 ToolRegistry 实例，用于工具白名单校验。
                为 ``None`` 时跳过工具存在性校验（仅做硬禁止 + 写操作约束）。

        返回:
            :class:`ValidationResult`。
        """
        result = ValidationResult()

        # 简易模式：仅校验 template 存在性
        if spec.is_simple_mode():
            self._validate_simple_mode(spec, result)
            return result

        # 多步模式：完整校验
        if not spec.steps:
            # 无 steps 且无 template，空 workflow
            result.add_error("", "steps", "workflow 既无 template 也无 steps")
            return result

        # 1. step id 唯一性
        self._check_step_id_uniqueness(spec, result)

        # 2. depends_on 无环
        self._check_depends_on_no_cycle(spec, result)

        # 3. 逐 step 校验
        registered_tool_names = self._get_registered_tool_names(tool_registry)
        builtin_templates = self._get_builtin_templates()
        for step in spec.steps:
            self._validate_step(
                step, result, registered_tool_names, builtin_templates
            )

        # 4. workflow 级 on_failure / timeout 校验（非阻断警告）
        self._validate_workflow_level(spec, result)

        return result

    # ------------------------------------------------------------------
    # 简易模式校验
    # ------------------------------------------------------------------
    def _validate_simple_mode(
        self, spec: WorkflowSpec, result: ValidationResult
    ) -> None:
        """简易模式：仅校验 template 名存在。"""
        if not spec.template:
            result.add_error("", "template", "简易模式 template 不能为空")
            return
        builtin_templates = self._get_builtin_templates()
        if spec.template not in builtin_templates:
            result.add_error(
                "",
                "template",
                f"简易模式模板 '{spec.template}' 不存在，"
                f"可用模板: {sorted(builtin_templates.keys())}",
            )

    # ------------------------------------------------------------------
    # 多步模式校验
    # ------------------------------------------------------------------
    def _check_step_id_uniqueness(
        self, spec: WorkflowSpec, result: ValidationResult
    ) -> None:
        """校验 step id 唯一性。"""
        seen: Dict[str, int] = {}
        for step in spec.steps:
            if step.id in seen:
                result.add_error(
                    step.id,
                    "id",
                    f"step id '{step.id}' 重复（第 {seen[step.id]} 与第 {seen[step.id] + 1} 个）",
                )
            seen[step.id] = len(seen)

    def _check_depends_on_no_cycle(
        self, spec: WorkflowSpec, result: ValidationResult
    ) -> None:
        """校验 depends_on 无环（DFS）。

        检测到环时记录错误，不抛异常。
        """
        # 构建 step_id → step 映射
        step_map: Dict[str, StepSpec] = {s.id: s for s in spec.steps}

        # 校验 depends_on 引用的 step id 存在
        for step in spec.steps:
            for dep in step.depends_on:
                if dep not in step_map:
                    result.add_error(
                        step.id,
                        "depends_on",
                        f"depends_on 引用了不存在的 step '{dep}'",
                    )

        # DFS 检测环
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {s.id: WHITE for s in spec.steps}

        def dfs(node_id: str, path: List[str]) -> bool:
            """返回 True 表示检测到环。"""
            color[node_id] = GRAY
            path.append(node_id)
            for dep in step_map.get(node_id, StepSpec(id=node_id)).depends_on:
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    # 环：找到 dep 在 path 中的位置
                    cycle_start = path.index(dep) if dep in path else 0
                    cycle = path[cycle_start:] + [dep]
                    result.add_error(
                        node_id,
                        "depends_on",
                        f"depends_on 存在环: {' → '.join(cycle)}",
                    )
                    return True
                if color[dep] == WHITE:
                    if dfs(dep, path):
                        return True
            path.pop()
            color[node_id] = BLACK
            return False

        for step_id in list(color.keys()):
            if color[step_id] == WHITE:
                dfs(step_id, [])

    def _validate_step(
        self,
        step: StepSpec,
        result: ValidationResult,
        registered_tool_names: Optional[Set[str]],
        builtin_templates: Dict[str, Any],
    ) -> None:
        """逐 step 校验。"""
        # 3a. type 合法
        if step.type not in ALLOWED_STEP_TYPES:
            result.add_error(
                step.id,
                "type",
                f"step type '{step.type}' 非法，"
                f"允许: {sorted(ALLOWED_STEP_TYPES)}",
            )

        # 3b. deterministic 类型校验模板名存在
        if step.type == "deterministic":
            template_name = step.config.get("template", "")
            if not template_name:
                result.add_error(
                    step.id,
                    "config.template",
                    "deterministic step 缺少 config.template 字段",
                )
            elif template_name not in builtin_templates:
                result.add_error(
                    step.id,
                    "config.template",
                    f"模板 '{template_name}' 不存在，"
                    f"可用: {sorted(builtin_templates.keys())}",
                )

        # 3c. tool 类型校验
        if step.type == "tool":
            self._validate_tool_step(step, result, registered_tool_names)

        # 3d. on_failure.action 合法性
        if step.on_failure.action not in {"retry", "fallback", "skip", "abort"}:
            result.add_error(
                step.id,
                "on_failure.action",
                f"on_failure.action '{step.on_failure.action}' 非法，"
                f"允许: retry / fallback / skip / abort",
            )

        # 3e. timeout_seconds 非负
        if step.timeout_seconds is not None and step.timeout_seconds < 0:
            result.add_error(
                step.id,
                "timeout_seconds",
                f"timeout_seconds 不能为负，实际: {step.timeout_seconds}",
            )

    def _validate_tool_step(
        self,
        step: StepSpec,
        result: ValidationResult,
        registered_tool_names: Optional[Set[str]],
    ) -> None:
        """校验 tool 类型 step 的 config.tool 字段。

        校验项：
        - config.tool 必填
        - 硬禁止工具检测（HARD_DISABLED_TOOLS）
        - 工具存在性（tool_registry 提供时）
        - 写操作工具 path_prefix 约束
        """
        tool_name = step.config.get("tool", "")
        if not tool_name:
            result.add_error(
                step.id,
                "config.tool",
                "tool step 缺少 config.tool 字段",
            )
            return

        # 硬禁止工具检测
        if tool_name in HARD_DISABLED_TOOLS:
            result.add_error(
                step.id,
                "config.tool",
                f"工具 '{tool_name}' 在硬禁止清单，"
                f"不可在 workflow 中调用（硬禁止: {sorted(HARD_DISABLED_TOOLS)}）",
            )
            return  # 硬禁止后续校验无意义

        # 工具存在性（tool_registry 提供时）
        if registered_tool_names is not None:
            if tool_name not in registered_tool_names:
                result.add_error(
                    step.id,
                    "config.tool",
                    f"工具 '{tool_name}' 未在 tool_registry 注册",
                )

        # 写操作工具 path_prefix 约束
        if tool_name in WRITE_OPERATION_TOOLS:
            # 写操作工具必须声明 path_prefix（限制写入范围）
            path_prefix = step.config.get("path_prefix")
            allowed_paths = step.config.get("allowed_paths")
            if not path_prefix and not allowed_paths:
                result.add_error(
                    step.id,
                    "config.path_prefix",
                    f"写操作工具 '{tool_name}' 必须声明 path_prefix 或 "
                    f"allowed_paths 约束写入范围",
                )

    def _validate_workflow_level(
        self, spec: WorkflowSpec, result: ValidationResult
    ) -> None:
        """workflow 级别校验（非阻断警告）。"""
        # workflow timeout 与 step timeout 关系
        if spec.timeout_seconds is not None:
            for step in spec.steps:
                if step.timeout_seconds and step.timeout_seconds > spec.timeout_seconds:
                    result.add_warning(
                        step.id,
                        "timeout_seconds",
                        f"step timeout ({step.timeout_seconds}s) 超过 "
                        f"workflow timeout ({spec.timeout_seconds}s)，"
                        f"step 可能被 workflow 级超时强制终止",
                    )

        # 步骤数过多警告（性能提示）
        if len(spec.steps) > 20:
            result.add_warning(
                "",
                "steps",
                f"workflow 含 {len(spec.steps)} 个 step，"
                f"建议拆分为多个 subworkflow 以降低单次执行复杂度",
            )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _get_registered_tool_names(self, tool_registry: Any) -> Optional[Set[str]]:
        """从 ToolRegistry 获取已注册工具名集合。

        返回 None 表示跳过工具存在性校验。
        """
        if tool_registry is None:
            return None
        try:
            schemas = tool_registry.get_tools_schema()
            return {s.get("name", "") for s in schemas if s.get("name")}
        except Exception:
            return None


def validate_workflow_dict(
    workflow_dict: Dict[str, Any],
    tool_registry: Any = None,
) -> ValidationResult:
    """便捷函数：从 dict 校验 workflow。

    参数:
        workflow_dict: workflow 配置 dict（YAML 解析后）。
        tool_registry: 可选 ToolRegistry 实例。

    返回:
        :class:`ValidationResult`。
    """
    try:
        spec = WorkflowSpec.from_dict(workflow_dict)
    except ValueError as e:
        result = ValidationResult()
        result.add_error("", "workflow", f"WorkflowSpec 解析失败: {e}")
        return result
    validator = WorkflowValidator()
    return validator.validate(spec, tool_registry=tool_registry)


__all__ = [
    "HARD_DISABLED_TOOLS",
    "WRITE_OPERATION_TOOLS",
    "ValidationError",
    "ValidationResult",
    "WorkflowValidator",
    "validate_workflow_dict",
]
