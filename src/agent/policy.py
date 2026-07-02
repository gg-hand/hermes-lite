"""轻量策略层（Layer 2），v2 升级为参数感知 + 会话级文件追踪 + 命令分类器。

本模块提供 ``PolicyEngine`` 与 ``CommandClassifier``，用于在工具调用执行前
进行策略评估，决定该次调用应当直接放行（``allow``）、需用户确认
（``confirm``）还是拒绝（``deny``）。

设计要点：
- v2 参数感知：``check`` 新增 ``session_id`` 参数，对 ``write_file`` /
  ``delete_file`` 工具，先查询 ``FileOperationRegistry`` 按文件状态决策。
- v2 命令分类器：``execute_command`` 工具调用 ``CommandClassifier.classify``
  按命令语义决策（读取类 allow / 删除类 confirm / 其他默认 allow + 审计标注）。
  策略为"黑名单为主 + 白名单加速 + 未知 allow"：高危命令（删除/强制推送/
  重置/重定向）走 confirm，读取类白名单直接放行，未知命令默认放行并在 reason
  中标注"未知命令默认放行"以便审计追踪。**此步独立于 ``file_registry``**，
  确保所有场景下读取类命令都能豁免。
- v1 工具名匹配：精确匹配优先，``tool_pattern`` 正则匹配次之。
- 对元工具 ``call_tool`` 做内省：先按内层工具名查规则，未命中再按
  ``call_tool`` 自身查。
- 不配置规则时使用 ``DEFAULT_RULES`` 安全默认值（仅拦截修改/执行/扩展类工具，
  读取与访问类工具默认放行）。
- ``file_registry`` 为可选注入：未注入时退化到 v1 工具名匹配行为（向后兼容）。

Phase 7 Task 3 记忆管理工具权限策略：
- ``delete_memory`` / ``update_memory`` 属于高危操作（直接修改向量库长期
  记忆），在 ``DEFAULT_RULES`` 中设为 ``confirm``，流式模式下推送 confirm
  事件等待用户确认，非流式模式下自动 deny（符合项目硬约束「非流式模式下
  确认操作自动拒绝」）。
- ``search_memory`` 属于读取类操作，不在 ``DEFAULT_RULES`` 中（默认放行），
  与 ``read_file`` / ``list_directory`` 等读取类工具一致。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional

if TYPE_CHECKING:  # 仅用于类型检查，运行时不导入以避免循环依赖
    from .file_registry import FileOperationRegistry

logger = logging.getLogger(__name__)

# Phase 8 Task 4.3: 硬禁止工具清单（统一引用 cron_tools.HARD_DISABLED_TOOLS）。
# 硬约束：硬禁止工具绝不预授权，即使 granted_tools 中含硬禁止项，
# PolicyEngine cron 路径也会拒绝（defense in depth）。
# 优先从 cron_tools 导入以保持单一数据源；导入失败时（如循环依赖边缘场景）
# 回退到本地副本，保证 PolicyEngine 可独立加载。
try:
    from .cron_tools import HARD_DISABLED_TOOLS as _HARD_DISABLED_TOOLS
except ImportError:  # pragma: no cover - 循环依赖边缘场景回退
    _HARD_DISABLED_TOOLS = {"memory_delete", "bash_exec", "tool_call"}

# 合法的 risk 取值
_VALID_RISKS = ("allow", "confirm", "deny")

# risk → risk_level 映射：allow 视为 low，confirm 与 deny 均视为 high
_RISK_TO_RISK_LEVEL = {
    "allow": "low",
    "confirm": "high",
    "deny": "high",
}


@dataclass
class Decision:
    """策略评估结果。

    Attributes:
        action: 决策动作，allow / confirm / deny 三选一。
        reason: 决策原因（人类可读，会展示给用户）。
        risk_level: 风险等级，low / medium / high 三选一。
        decision_source: Phase 8 Task 4。决策来源，用于审计记录。
            取值：``"default_rule"``（默认规则）/ ``"schedule_grant"``
            （cron 调度项预授权放行）/ ``"user_confirm"``（用户确认）。
            默认 ``"default_rule"``，向后兼容旧调用方。
    """

    action: Literal["allow", "confirm", "deny"]
    reason: str
    risk_level: Literal["low", "medium", "high"]
    decision_source: str = "default_rule"


DEFAULT_RULES = [
    {"tool": "file_write", "risk": "confirm", "reason": "写入文件会修改或覆盖磁盘内容"},
    {"tool": "bash_exec", "risk": "confirm", "reason": "执行 shell 命令可能修改、删除系统资源"},
    {"tool": "tool_call", "risk": "confirm", "reason": "调用扩展工具（Skill/MCP）可能产生副作用"},
    {"tool": "profile_update", "risk": "confirm", "reason": "修改用户画像属高危操作，需确认"},
    # Phase 7 Task 3: 记忆管理工具（高危截停）
    # delete_memory / update_memory 直接修改向量库长期记忆，需 confirm。
    # 流式模式下推送 confirm 事件等待用户确认；非流式模式下自动 deny
    # （符合项目硬约束「非流式模式下确认操作自动拒绝」）。
    {"tool": "memory_delete", "risk": "confirm", "reason": "删除长期记忆属高危操作，需确认"},
    {"tool": "memory_update", "risk": "confirm", "reason": "修改长期记忆属高危操作，需确认"},
    # Phase 8 Task 5.2: Skill 管理工具（高危截停）
    # propose_skill / reload_skill / toggle_skill 会写磁盘文件或修改注册中心，
    # 需 confirm 防止误操作。
    {"tool": "skill_propose", "risk": "confirm", "reason": "新增 Skill 会写入磁盘文件，需确认"},
    {"tool": "skill_reload", "risk": "confirm", "reason": "重新加载 Skill 会替换注册中心工具，需确认"},
    {"tool": "skill_toggle", "risk": "confirm", "reason": "启用/禁用 Skill 会修改注册中心状态，需确认"},
    {"tool": "file_edit", "risk": "confirm", "reason": "修改文件内容，需确认"},
    # 注：search_memory 为读取类操作，不在 DEFAULT_RULES 中（默认放行）。
]


class CommandClassifier:
    """execute_command 命令分类器（v2 参数感知，黑名单为主策略）。

    按命令语义分类决策（黑名单为主 + 白名单加速 + 未知 allow）：
    - 读取类命令（``dir`` / ``ls`` / ``cat`` / ``git status`` / ``echo`` 等）→ ``read``
    - 删除/高危类命令（``del`` / ``rm`` / ``rmdir`` / ``Remove-Item`` /
      ``git clean`` / ``git push --force`` / ``git reset --hard`` /
      含 ``>`` / ``>>`` 重定向等）→ ``delete``
    - 其他命令 → ``other``（由 ``_check_execute_command`` 默认放行 allow，
      reason 标注"未知命令默认放行"以便审计追踪）

    命令注入防御：含 ``&`` / ``|`` / ``;`` / ``&&`` / ``||`` 分隔符时拆分为
    多个子命令，任一子命令匹配删除/高危黑名单则整体归 ``delete``；
    无副作用命令（``cd`` / ``echo`` / ``pwd``）跳过不拖累整体；
    其余"有副作用"子命令均在读取白名单才整体归 ``read``；否则归 ``other``。

    匹配方式：命令前缀匹配（非子字符串匹配）。``command.startswith(prefix)``
    后检查下一个字符是空白或字符串结束，避免 ``dir`` 误匹配 ``dirxyz``。
    大小写不敏感（Windows 上 ``dir`` 与 ``DIR`` 等价）。命令前可能含前导空白，
    先 ``strip``。
    """

    # 读取类命令白名单（前缀匹配，大小写不敏感）
    # 含目录/文件查看类（pwd / cd / echo / type / cat / ls / dir）、
    # 命令查找类（where / whereis / which）、系统信息类（hostname / whoami /
    # ipconfig / ifconfig / netstat / systeminfo / uname / env / set）、
    # git 读取类（status / log / diff / show / branch）、版本查询类
    # （python / node / java / go / rustc --version）等。
    READ_WHITELIST: tuple = (
        # 目录与文件查看类（无副作用）
        "dir",
        "type",
        "cat",
        "ls",
        "pwd",
        "cd",
        "echo",
        # 命令查找类
        "where",
        "whereis",
        "which",
        # PowerShell 读取类
        "Get-Content",
        "Get-ChildItem",
        # 系统信息类
        "hostname",
        "whoami",
        "ipconfig",
        "ifconfig",
        "netstat",
        "systeminfo",
        "uname",
        "env",
        "set",
        # git 读取类
        "git status",
        "git log",
        "git diff",
        "git show",
        "git branch",
        # 版本查询类
        "python -m py_compile",
        "python --version",
        "node --version",
        "java --version",
        "go version",
        "rustc --version",
    )

    # 无副作用命令清单（前缀匹配，大小写不敏感）
    # cd / echo / pwd 既在 READ_WHITELIST 中也在此列出：在 classify 复合命令
    # 拆分时，无副作用子命令直接跳过，不拖累整体分类（例如 ``cd E:\proj && dir``
    # 中 cd 被跳过，仅 dir 决定整体归 read）。与 READ_WHITELIST 重复是有意为之，
    # 语义上区分"纯无副作用"与"读取类有返回值"两类。
    NO_SIDE_EFFECT: tuple = (
        "cd",
        "echo",
        "pwd",
    )

    # 删除/高危类命令黑名单（前缀匹配，大小写不敏感）
    # 含删除类（del / rm / rmdir / Remove-Item / rd / unlink / git clean）与
    # 高危写入类（git push --force / -f / git reset --hard）。文件重定向
    # 操作符 ``>`` / ``>>`` 在 ``_is_delete_command`` 中特殊检测。
    DELETE_BLACKLIST: tuple = (
        "del",
        "rm",
        "rmdir",
        "Remove-Item",
        "rd",
        "unlink",
        "git clean",
        "git push --force",
        "git push -f",
        "git reset --hard",
    )

    # 命令分隔符正则：先匹配多字符分隔符（``&&`` / ``||``），再匹配单字符（``&`` / ``|`` / ``;``）
    _SEPARATOR_RE = re.compile(r"&&|\|\||[&|;]")

    @classmethod
    def _matches_prefix(cls, command: str, prefix: str) -> bool:
        """检查 ``command`` 是否以 ``prefix`` 开头（前缀匹配）。

        前缀匹配规则：``command`` 去除前导空白后，以 ``prefix`` 开头，且紧随其后的
        字符必须是空白或字符串结束（避免 ``dir`` 误匹配 ``dirxyz``）。
        大小写不敏感。

        参数:
            command: 待检查的命令字符串（调用方负责 strip，本方法只做大小写归一）。
            prefix: 白名单/黑名单中的前缀。

        返回:
            是否前缀匹配。
        """
        cmd_lower = command.lower()
        prefix_lower = prefix.lower()
        if not cmd_lower.startswith(prefix_lower):
            return False
        # 前缀刚好等于命令本身（无参数情形）
        if len(cmd_lower) == len(prefix_lower):
            return True
        # 下一个字符必须是空白
        return cmd_lower[len(prefix_lower)].isspace()

    @classmethod
    def _is_read_command(cls, command: str) -> bool:
        """检查 ``command`` 是否匹配读取类白名单任一前缀。"""
        for prefix in cls.READ_WHITELIST:
            if cls._matches_prefix(command, prefix):
                return True
        return False

    @classmethod
    def _is_no_side_effect(cls, command: str) -> bool:
        """检查 ``command`` 是否为无副作用命令（``cd`` / ``echo`` / ``pwd``）。

        无副作用命令在 ``classify`` 复合命令拆分时直接跳过，不参与"是否读取类"
        判定，从而避免拖累整体分类。例如 ``cd E:\\proj && dir /b`` 中 ``cd``
        被跳过，仅由 ``dir`` 决定整体归 ``read``。
        """
        for prefix in cls.NO_SIDE_EFFECT:
            if cls._matches_prefix(command, prefix):
                return True
        return False

    @classmethod
    def _has_redirect_operator(cls, command: str) -> bool:
        """检查 ``command`` 是否含文件重定向操作符 ``>`` / ``>>``。

        重定向会覆盖或追加文件内容，属高危写入操作，归为 ``delete`` 类。

        检测规则（与 spec 一致，保守检测带空格的形式）：
        - 命令以 ``>`` 或 ``>>`` 开头（如 ``> file.txt`` 截断文件）
        - 命令中含 `` > `` 或 `` >> ``（前后带空格，如 ``echo hello > file.txt``）

        注：``echo hello>file.txt``（无空格）不被检测，这是已知的保守边界，
        与任务 spec 一致。
        """
        cmd = command.strip()
        if not cmd:
            return False
        # 命令以 > 或 >> 开头（>> 以 > 开头，统一判断 startswith(">")）
        if cmd.startswith(">"):
            return True
        # 命令中含 " > " 或 " >> "（前后带空格）
        if " > " in cmd or " >> " in cmd:
            return True
        return False

    @classmethod
    def _is_delete_command(cls, command: str) -> bool:
        """检查 ``command`` 是否匹配删除/高危黑名单前缀，或含文件重定向操作符。

        匹配顺序：
        1. 前缀匹配 ``DELETE_BLACKLIST``（del / rm / git push --force 等）
        2. 检测文件重定向操作符 ``>`` / ``>>``（覆盖/追加文件，高危写入）
        """
        for prefix in cls.DELETE_BLACKLIST:
            if cls._matches_prefix(command, prefix):
                return True
        if cls._has_redirect_operator(command):
            return True
        return False

    @classmethod
    def classify(cls, command: str) -> str:
        """分类命令语义（黑名单为主 + 白名单加速 + 未知 allow）。

        参数:
            command: 待分类的命令字符串。可能含前导空白或复合命令分隔符。

        返回:
            ``"read"`` / ``"delete"`` / ``"other"`` 三选一：
            - ``"read"``：单条读取类命令，或复合命令所有"有副作用"子命令
              均在读取白名单（``NO_SIDE_EFFECT`` 子命令跳过不拖累整体）
            - ``"delete"``：单条删除/高危命令，或复合命令任一子命令为
              删除/高危类（含重定向 ``>`` / ``>>``）
            - ``"other"``：不在白/黑名单，或复合命令含未知副作用子命令
              （由 ``_check_execute_command`` 默认放行 allow + 审计标注）
            - 空命令或纯分隔符 → ``"other"``
        """
        if not command:
            return "other"

        cmd = command.strip()
        if not cmd:
            return "other"

        # 拆分子命令（按 && / || / & / | / ; 分隔）
        parts = cls._SEPARATOR_RE.split(cmd)
        subcommands = [p.strip() for p in parts if p.strip()]
        if not subcommands:
            return "other"

        any_delete = False
        has_side_effect_unsafe = False
        for sub in subcommands:
            # 1. 任一子命令是 delete（含重定向）→ 标记 any_delete，跳过后续判定
            if cls._is_delete_command(sub):
                any_delete = True
                continue
            # 2. 无副作用命令（cd / echo / pwd）跳过，不拖累整体
            if cls._is_no_side_effect(sub):
                continue
            # 3. 非 read 的"有副作用"子命令 → 标记整体不安全
            if not cls._is_read_command(sub):
                has_side_effect_unsafe = True

        # 任一子命令匹配删除/高危黑名单 → delete（高优先级，安全优先）
        if any_delete:
            return "delete"
        # 所有"有副作用"子命令都在读取白名单（无副作用子命令已跳过）→ read
        if not has_side_effect_unsafe:
            return "read"
        # 否则归 other（含混合：读取 + 未知；由 _check_execute_command 默认 allow）
        return "other"


class PolicyEngine:
    """轻量策略评估器。

    根据工具名与规则列表评估一次工具调用应如何处置。规则匹配顺序：
    先遍历所有规则做 ``tool`` 精确匹配，无命中后再遍历所有规则做
    ``tool_pattern`` 正则匹配，从而保证精确匹配优先于正则。

    规则校验：构造时遍历每条规则，``risk`` 字段必须为
    ``("allow", "confirm", "deny")`` 之一，且 ``tool`` 与 ``tool_pattern``
    至少存在一个，否则跳过该规则并记录 warning。
    """

    def __init__(
        self,
        enabled: bool = True,
        rules: list = None,
        file_registry: Optional["FileOperationRegistry"] = None,
        cron_scheduler: Optional[Any] = None,
        workspace_root: Optional[str] = None,
    ) -> None:
        """初始化策略评估器。

        参数:
            enabled: 是否启用策略评估。``False`` 时 ``check`` 一律放行。
            rules: 规则列表，``None`` 时使用 ``DEFAULT_RULES``。每条规则
                为 dict，可含字段：``tool``（精确匹配）/ ``tool_pattern``
                （正则匹配）/ ``risk`` / ``reason``。构造时会校验每条规则，
                非法规则将被跳过并记录 warning。
            file_registry: v2 可选注入 ``FileOperationRegistry`` 实例。
                注入后 ``check`` 对 ``write_file`` / ``delete_file`` 工具按
                文件状态决策；为 ``None`` 时退化到 v1 工具名匹配（向后兼容）。
            cron_scheduler: Phase 8 Task 4.2 可选注入 ``CronScheduler`` 实例。
                注入后 ``check`` 对 ``session_id`` 以 ``cron:`` 开头的会话走
                预授权三层检查；为 ``None`` 时 cron 会话退化到默认规则。
            workspace_root: 工作空间根目录路径。为 ``None`` 或空时不做边界检查。
                非空时 ``_check_write_file`` 会检查目标路径是否在此目录内，
                跨出工作空间写文件将触发 ``confirm``。
        """
        self._enabled = enabled
        self._file_registry = file_registry
        self._cron_scheduler = cron_scheduler
        # 解析工作空间根目录为绝对路径
        if workspace_root:
            try:
                self._workspace_root = Path(workspace_root).resolve()
            except Exception:
                logger.warning("workspace_root 解析失败: %s，禁用边界检查", workspace_root)
                self._workspace_root = None
        else:
            self._workspace_root = None
        if rules is None:
            rules = DEFAULT_RULES

        validated: List[Dict[str, Any]] = []
        for rule in rules:
            risk = rule.get("risk")
            if risk not in _VALID_RISKS:
                logger.warning("跳过非法规则: %s", rule)
                continue
            # tool 与 tool_pattern 至少有一个必须存在
            if "tool" not in rule and "tool_pattern" not in rule:
                logger.warning("跳过非法规则: %s", rule)
                continue
            validated.append(rule)
        self._rules = validated

    def set_cron_scheduler(self, cron_scheduler: Optional[Any]) -> None:
        """注入 cron_scheduler 引用（Phase 8 Task 4.2）。

        由于 ``CronScheduler`` 在 ``server.py`` 中晚于 ``Orchestrator`` 创建，
        构造 ``PolicyEngine`` 时 ``cron_scheduler`` 通常为 ``None``。此方法
        供 ``server.py`` 在 ``CronScheduler`` 创建后注入引用，启用 cron 路径
        预授权三层检查。

        参数:
            cron_scheduler: ``CronScheduler`` 实例，或 ``None`` 禁用 cron 路径。
        """
        self._cron_scheduler = cron_scheduler

    @classmethod
    def from_config(
        cls,
        security_cfg: dict,
        file_registry: Optional["FileOperationRegistry"] = None,
        cron_scheduler: Optional[Any] = None,
    ) -> "PolicyEngine":
        """从 config 的 security 段构造 ``PolicyEngine``。

        参数:
            security_cfg: config 中 ``security`` 段的 dict，可含字段：
                ``enabled``（默认 ``True``）/ ``rules``（``None`` 或空 list
                时使用 ``DEFAULT_RULES``，这是设计意图，让用户不配置时获得
                安全默认值）。
            file_registry: v2 可选注入，透传给 ``__init__``。
            cron_scheduler: Phase 8 Task 4.2 可选注入，透传给 ``__init__``。

        返回:
            构造好的 ``PolicyEngine`` 实例。规则校验由 ``__init__`` 完成。
        """
        enabled = security_cfg.get("enabled", True)
        rules = security_cfg.get("rules")
        # None 或空 list 均回退到 DEFAULT_RULES
        if not rules:
            rules = DEFAULT_RULES
        return cls(
            enabled=enabled,
            rules=rules,
            file_registry=file_registry,
            cron_scheduler=cron_scheduler,
            workspace_root=security_cfg.get("workspace_root"),
        )

    def _match_rules(self, name: str) -> Optional[Dict[str, Any]]:
        """按规则列表匹配单个工具名。

        匹配顺序：先遍历所有规则做 ``tool`` 精确匹配，无命中后再遍历
        所有规则做 ``tool_pattern`` 正则匹配，保证精确匹配优先于正则。

        参数:
            name: 待匹配的工具名。

        返回:
            命中的规则 dict，未命中返回 ``None``。
        """
        # 第一遍：tool 精确匹配
        for rule in self._rules:
            if rule.get("tool") == name:
                return rule
        # 第二遍：tool_pattern 正则匹配
        for rule in self._rules:
            pattern = rule.get("tool_pattern")
            if pattern and re.search(pattern, name):
                return rule
        return None

    def _rule_to_decision(self, rule: Dict[str, Any]) -> Decision:
        """将命中的规则转换为 ``Decision``。

        参数:
            rule: 命中的规则 dict。

        返回:
            对应的 ``Decision``：``risk`` 透传为 ``action``，``reason``
            透传，``risk_level`` 由 ``risk`` 映射（allow→low /
            confirm→high / deny→high）。
        """
        risk = rule["risk"]
        reason = rule.get("reason", "")
        # risk → action：allow→allow / confirm→confirm / deny→deny（同名字符串）
        action = risk
        # risk_level：allow→low / confirm→high / deny→high
        risk_level = _RISK_TO_RISK_LEVEL[risk]
        return Decision(action=action, reason=reason, risk_level=risk_level)

    def check(
        self,
        tool_name: str,
        tool_input: dict,
        session_id: Optional[str] = None,
    ) -> Decision:
        """评估一次工具调用的处置决策。

        评估流程：
        1. ``enabled=False`` → 一律放行 ``Decision("allow", "", "low")``。
        2. v2 命令分类器：``execute_command`` 调用 ``_check_execute_command``
           按命令语义决策（读取类 allow / 删除类 confirm / 其他默认 allow +
           审计标注"未知命令默认放行"）。**此步独立于 ``file_registry``**，
           无论是否注入都执行，确保 ``execute_command`` 在所有场景下都享有
           命令分类豁免。
        3. v2 参数感知：若注入了 ``file_registry`` 且 ``session_id`` 非空，
           对 ``write_file`` / ``delete_file`` 工具按文件状态决策表返回
           （详见 ``_check_write_file`` / ``_check_delete_file``）。
        4. 元工具内省：若 ``tool_name == "tool_call"``，从
           ``tool_input.get("name")`` 提取内层工具名，先按内层名查规则，
           命中则返回；未命中再按 ``call_tool`` 自身查。
        5. 按规则列表顺序匹配：``tool`` 字段精确匹配优先，
           ``tool_pattern`` 字段正则匹配次之。
        6. 命中则返回对应 ``Decision``。
        7. 未命中任何规则 → 返回 ``Decision("allow", "", "low")``。

        参数:
            tool_name: 待评估的工具名。
            tool_input: 工具输入参数 dict。
            session_id: v2 可选会话 ID，用于查询 ``FileOperationRegistry``
                做参数感知决策。为 ``None`` 时退化到 v1 工具名匹配（向后兼容）。
                注意：``execute_command`` 命令分类器不依赖 ``session_id``，
                即使 ``session_id`` 为 ``None`` 也会执行分类决策。

        返回:
            评估得到的 ``Decision``。
        """
        # 1. 未启用 → 一律放行
        if not self._enabled:
            return Decision("allow", "", "low")

        # 1.5 Phase 8 Task 4.2: cron 会话预授权三层检查
        # 检测 session_id 以 "cron:" 开头且注入了 cron_scheduler 时，走预授权路径。
        # 三层检查通过 → allow + decision_source="schedule_grant"
        # 未通过 → 回退默认规则（不直接 deny，保持向后兼容与默认安全策略）
        cron_decision = self._check_cron_grant(tool_name, tool_input, session_id)
        if cron_decision is not None:
            return cron_decision

        # 2. v2 命令分类器：execute_command 独立决策（不依赖 file_registry / session_id）
        #    先于 v2 文件状态路径执行，确保所有场景下读取类命令都能豁免
        if tool_name == "bash_exec":
            return self._check_execute_command(tool_input)

        # 3. v2 参数感知：write_file / delete_file 走文件状态决策表
        if self._file_registry is not None and session_id:
            if tool_name == "file_write":
                return self._check_write_file(session_id, tool_input)
            if tool_name == "file_delete":
                return self._check_delete_file(session_id, tool_input)

        # 4. 元工具内省：call_tool 先按内层工具名查规则
        if tool_name == "tool_call":
            inner_name = tool_input.get("name") if tool_input else None
            if inner_name:
                rule = self._match_rules(inner_name)
                if rule is not None:
                    return self._rule_to_decision(rule)
            # 内层未命中则继续按 call_tool 自身查（落入下方通用流程）

        # 5 & 6. 按 tool_name 通用匹配
        rule = self._match_rules(tool_name)
        if rule is not None:
            return self._rule_to_decision(rule)

        # 7. 未命中任何规则 → 放行
        return Decision("allow", "", "low")

    # ------------------------------------------------------------------
    # Phase 8 Task 4.2: cron 会话预授权三层检查
    # ------------------------------------------------------------------
    def _check_cron_grant(
        self,
        tool_name: str,
        tool_input: dict,
        session_id: Optional[str],
    ) -> Optional[Decision]:
        """cron 会话预授权三层检查（Phase 8 Task 4.2）。

        检测 ``session_id`` 以 ``cron:`` 开头且注入了 ``cron_scheduler`` 时，
        从调度项的 ``granted_tools`` 进行三层检查：

        1. **工具是否预授权**：``tool_name`` 在 ``granted_tools`` 列表中。
           硬禁止工具（``delete_memory`` / ``execute_command`` / ``call_tool``）
           绝不预授权（defense in depth，即使误入 granted_tools 也拒绝）。
        2. **路径前缀匹配**：若该项 ``scope="path_prefix"``，检查
           ``tool_input`` 中的路径字段（``path`` / ``file_path``）是否在
           ``allowed_paths`` 任一前缀内。``scope="all"`` 时跳过此层。
        3. **通过则放行**：返回 ``Decision("allow", "schedule_grant", "low",
           decision_source="schedule_grant")``。

        未通过任一层时返回 ``None``，由 ``check`` 回退到默认规则（不直接
        deny，保持默认安全策略：未预授权的工具走 DEFAULT_RULES）。

        参数:
            tool_name: 待评估的工具名。
            tool_input: 工具输入参数 dict。
            session_id: 会话 ID，``cron:`` 前缀触发预授权路径。

        返回:
            预授权通过返回 ``Decision``（allow + schedule_grant）；未通过或
            非 cron 会话返回 ``None``（回退默认规则）。
        """
        if not session_id or not session_id.startswith("cron:"):
            return None
        if self._cron_scheduler is None:
            return None

        # 提取 schedule_id 并查询调度项
        schedule_id = session_id[5:]
        try:
            schedule = self._cron_scheduler._find_schedule(schedule_id)
        except Exception:
            logger.warning(
                "cron 预授权检查：查询调度项 %s 失败，回退默认规则",
                schedule_id,
                exc_info=True,
            )
            return None
        if schedule is None:
            # 调度项不存在（可能已删除），回退默认规则
            return None

        granted_tools = getattr(schedule, "granted_tools", None)
        if not granted_tools:
            # 无预授权配置，回退默认规则
            return None

        # 第一层：工具是否在预授权列表中
        grant_entry: Optional[Dict[str, Any]] = None
        for entry in granted_tools:
            if not isinstance(entry, dict):
                continue
            if entry.get("tool") == tool_name:
                grant_entry = entry
                break
        if grant_entry is None:
            # 工具未预授权，回退默认规则
            return None

        # 硬禁止工具绝不预授权（defense in depth）
        if tool_name in _HARD_DISABLED_TOOLS:
            logger.warning(
                "cron 预授权检查：工具 %s 在硬禁止清单中，拒绝预授权，回退默认规则",
                tool_name,
            )
            return None

        # 第二层：路径前缀匹配（scope="path_prefix" 时）
        scope = grant_entry.get("scope", "all")
        if scope == "path_prefix":
            allowed_paths = grant_entry.get("allowed_paths", []) or []
            if not self._is_path_allowed(tool_input, allowed_paths):
                # 路径越界，回退默认规则（不直接 deny，让默认规则决定 confirm/deny）
                logger.info(
                    "cron 预授权检查：工具 %s 路径越界（不在 allowed_paths %s），"
                    "回退默认规则",
                    tool_name,
                    allowed_paths,
                )
                return None

        # 第三层：通过 → allow + schedule_grant
        return Decision(
            action="allow",
            reason="schedule_grant",
            risk_level="low",
            decision_source="schedule_grant",
        )

    @staticmethod
    def _is_path_allowed(
        tool_input: dict, allowed_paths: List[str]
    ) -> bool:
        """检查 ``tool_input`` 中的路径是否在 ``allowed_paths`` 前缀内。

        从 ``tool_input`` 中提取路径字段（依次尝试 ``path`` / ``file_path``
        / ``target_path`` / ``destination``），检查是否以 ``allowed_paths``
        中任一项为前缀。``allowed_paths`` 为空时返回 ``False``（无允许路径）。

        路径比较前会 ``os.path.normpath`` 归一化，避免 ``/data/./x`` 与
        ``/data/x`` 不匹配的问题。前缀匹配要求路径分隔符边界：``/data``
        匹配 ``/data/x`` 与 ``/data``，但不匹配 ``/datax``。

        参数:
            tool_input: 工具输入参数 dict。
            allowed_paths: 允许的路径前缀列表。

        返回:
            路径在任一前缀内返回 ``True``，否则 ``False``。
        """
        if not allowed_paths:
            return False

        # 从 tool_input 提取路径（兼容不同工具的路径字段名）
        path_str: Optional[str] = None
        for key in ("path", "file_path", "target_path", "destination"):
            val = tool_input.get(key) if tool_input else None
            if isinstance(val, str) and val:
                path_str = val
                break

        if not path_str:
            # 工具无路径字段（如 read_file 无 path 时不该发生，但防御），
            # 视为越界（保守拒绝）
            return False

        import os

        norm_path = os.path.normpath(path_str)
        for prefix in allowed_paths:
            if not isinstance(prefix, str) or not prefix:
                continue
            norm_prefix = os.path.normpath(prefix)
            # 前缀匹配：路径等于前缀，或路径以前缀 + 分隔符开头
            if norm_path == norm_prefix:
                return True
            if norm_path.startswith(norm_prefix + os.sep):
                return True
        return False

    # ------------------------------------------------------------------
    # v2 参数感知决策表
    # ------------------------------------------------------------------
    def _check_write_file(
        self, session_id: str, tool_input: dict
    ) -> Decision:
        """file_write 决策表（v2 参数感知）。

        决策规则：
        - path.is_symlink() → ``deny("拒绝写入符号链接")``
        - 文件不存在 → ``allow("新建文件")``（handler 后续记录到 created）
        - file_registry.is_created → ``allow("修改会话内创建的文件")``
        - file_registry.is_modified → ``allow("已确认过的修改")``
        - 文件存在且不在 registry → ``confirm("首次修改用户文件")``
        - tool_input 无 path 字段 → 降级到工具名匹配（防御）

        参数:
            session_id: 会话 ID。
            tool_input: 工具输入参数 dict，需含 ``path`` 字段。

        返回:
            对应的 ``Decision``。
        """
        path_str = tool_input.get("path") if tool_input else None
        if not path_str:
            # 防御：无 path 字段，降级到工具名匹配
            rule = self._match_rules("file_write")
            if rule is not None:
                return self._rule_to_decision(rule)
            return Decision("allow", "", "low")

        p = Path(path_str)
        # symlink 防御
        try:
            if p.is_symlink():
                return Decision("deny", "拒绝写入符号链接", "high")
        except OSError:
            # 路径不存在时 is_symlink 返回 False，但某些异常情况降级处理
            pass

        # 工作空间边界检查：若目标路径不在 workspace_root 内则触发 HIL
        if self._workspace_root is not None:
            try:
                target_resolved = p.resolve()
                if not str(target_resolved).startswith(str(self._workspace_root)):
                    return Decision(
                        "confirm",
                        f"写入路径不在工作空间内（{self._workspace_root}），需确认",
                        "high",
                    )
            except (OSError, ValueError):
                # resolve 失败时保守处理：触发 confirm
                return Decision("confirm", "无法解析目标路径，需确认", "high")

        # 文件不存在 → 新建放行
        try:
            exists = p.exists()
        except OSError:
            exists = False
        if not exists:
            return Decision("allow", "新建文件", "low")

        # 会话内创建 → 放行
        if self._file_registry.is_created(session_id, p):
            return Decision("allow", "修改会话内创建的文件", "low")

        # 会话内已修改过 → 放行（首次已确认）
        if self._file_registry.is_modified(session_id, p):
            return Decision("allow", "已确认过的修改", "low")

        # 用户已有文件首次修改 → 截停
        return Decision("confirm", "首次修改用户文件", "high")

    def _check_delete_file(
        self, session_id: str, tool_input: dict
    ) -> Decision:
        """file_delete 决策表（v2 参数感知）。

        决策规则：
        - path.is_symlink() → ``deny("拒绝删除符号链接")``
        - file_registry.is_created → ``allow("删除会话内创建的文件")``
        - file_registry.is_modified → ``confirm("删除会话内修改过的文件")``
        - 不在任何集合 → ``confirm("删除用户文件")``
        - tool_input 无 path 字段 → 降级到 allow（防御）

        参数:
            session_id: 会话 ID。
            tool_input: 工具输入参数 dict，需含 ``path`` 字段。

        返回:
            对应的 ``Decision``。
        """
        path_str = tool_input.get("path") if tool_input else None
        if not path_str:
            # 防御：无 path 字段
            return Decision("allow", "", "low")

        p = Path(path_str)
        # symlink 防御
        try:
            if p.is_symlink():
                return Decision("deny", "拒绝删除符号链接", "high")
        except OSError:
            pass

        # 会话内创建 → 放行（用户要求：会话内 create_file 享有删除权限）
        if self._file_registry.is_created(session_id, p):
            return Decision("allow", "删除会话内创建的文件", "low")

        # 会话内修改过 → 截停
        if self._file_registry.is_modified(session_id, p):
            return Decision("confirm", "删除会话内修改过的文件", "high")

        # 用户文件 → 截停
        return Decision("confirm", "删除用户文件", "high")

    def _check_execute_command(self, tool_input: dict) -> Decision:
        """execute_command 决策表（v2 命令分类器，黑名单为主策略）。

        调用 ``CommandClassifier.classify`` 按命令语义分类决策：

        - ``read``（读取类，含所有"有副作用"子命令均在白名单的复合命令）→
          ``Decision("allow", "读取类命令", "low")``
        - ``delete``（删除/高危类，含任一子命令为删除/高危类或重定向）→
          ``Decision("confirm", "删除类命令", "high")``
        - ``other``（不在白/黑名单，或混合读取+未知子命令）→
          ``Decision("allow", "未知命令默认放行", "low")``。
          黑名单为主策略下，未知命令默认放行以便审计追踪：reason 字段明确
          标注"未知命令默认放行"，便于后续审计日志筛选与回溯。

        防御：``tool_input`` 无 ``command`` 字段时降级到 ``DEFAULT_RULES`` 的
        ``execute_command`` 规则匹配（与 v1 行为一致）。

        **此方法独立于 ``file_registry`` / ``session_id``**：命令分类不需要文件
        状态，因此 ``check`` 方法在 v2 文件状态路径之前调用本方法，确保
        ``execute_command`` 在所有场景下都享有命令分类豁免。

        参数:
            tool_input: 工具输入参数 dict，需含 ``command`` 字段（str）。

        返回:
            对应的 ``Decision``。
        """
        command = tool_input.get("command") if tool_input else None
        if not command:
            # 防御：无 command 字段，降级到 DEFAULT_RULES 工具名匹配
            rule = self._match_rules("bash_exec")
            if rule is not None:
                return self._rule_to_decision(rule)
            return Decision("allow", "", "low")

        category = CommandClassifier.classify(command)
        if category == "read":
            return Decision("allow", "读取类命令", "low")
        if category == "delete":
            return Decision("confirm", "删除类命令", "high")

        # 其他命令 → 默认放行（黑名单为主策略：未知命令 allow + 审计标注）
        # reason 字段标注"未知命令默认放行"，便于审计日志筛选与回溯
        return Decision("allow", "未知命令默认放行", "low")
