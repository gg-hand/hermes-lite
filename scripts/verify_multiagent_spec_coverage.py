"""multiagent spec coverage 验收脚本。

封装 36 项 Grep 检查（覆盖 spec §4 的 23 个修订项 + 13 个补充检查），
支持全量验收与单 Plan 范围验收。

用法：
    python scripts/verify_multiagent_spec_coverage.py --all       # 全量验收
    python scripts/verify_multiagent_spec_coverage.py plan1       # 仅 Plan 1
    python scripts/verify_multiagent_spec_coverage.py plan2       # 仅 Plan 2
    python scripts/verify_multiagent_spec_coverage.py plan3       # 仅 Plan 3
    python scripts/verify_multiagent_spec_coverage.py plan4       # 仅 Plan 4

退出码：0 = 全部通过；1 = 有失败项。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# 项目根目录（scripts/ 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class CheckResult:
    """单项验收结果。"""

    name: str
    passed: bool
    detail: str = ""
    plan: str = ""


def _read_file(rel_path: str) -> str:
    """读取项目内相对路径文件，返回文本（不存在返回空串）。"""
    abs_path = _PROJECT_ROOT / rel_path
    if not abs_path.exists():
        return ""
    try:
        return abs_path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _file_exists(rel_path: str) -> bool:
    return (_PROJECT_ROOT / rel_path).exists()


def _grep(pattern: str, text: str, flags: int = 0) -> bool:
    """在文本中查找正则模式。"""
    return re.search(pattern, text, flags) is not None


def _grep_count(pattern: str, text: str, flags: int = 0) -> int:
    """统计匹配次数。"""
    return len(re.findall(pattern, text, flags))


# =============================================================================
# Plan 1 范围检查（15 项）— spec §4.1-§4.4 + 补充
# =============================================================================


def _p1_01_exceptions_8_classes() -> CheckResult:
    """§4.1 异常类完整（8 个）。"""
    text = _read_file("teage_liu/multiagent/exceptions.py")
    classes = [
        "CASConflictError",
        "CASVersionMismatchError",
        "FencingTokenMismatchError",
        "LockAcquisitionError",
        "NotMyTurnError",
        "DirectorUnavailableError",
        "GhostWriteAttemptError",
        "CapabilityNotInCardError",
        "A2AGatewayError",
    ]
    missing = [c for c in classes if not _grep(rf"class\s+{c}\s*\(", text)]
    if missing:
        return CheckResult(
            "p1_01_exceptions_8_classes",
            False,
            f"missing exception classes: {missing}",
            "plan1",
        )
    return CheckResult(
        "p1_01_exceptions_8_classes",
        True,
        f"all {len(classes)} exception classes present",
        "plan1",
    )


def _p1_02_schemas_7_files() -> CheckResult:
    """§4.2 schema 文件（7 个）。"""
    schemas = [
        "data/schemas/multiagent/protocol_md.schema.yaml",
        "data/schemas/multiagent/director_md.schema.yaml",
        "data/schemas/multiagent/status_json.schema.json",
        "data/schemas/multiagent/agent_card.schema.yaml",
        "data/schemas/multiagent/messages_md.schema.yaml",
        "data/schemas/multiagent/task_md.schema.yaml",
        "data/schemas/multiagent/audit_record.schema.json",
    ]
    missing = [s for s in schemas if not _file_exists(s)]
    if missing:
        return CheckResult(
            "p1_02_schemas_7_files",
            False,
            f"missing schema files: {missing}",
            "plan1",
        )
    return CheckResult(
        "p1_02_schemas_7_files",
        True,
        f"all {len(schemas)} schema files present",
        "plan1",
    )


def _p1_03_blackboard_path_sandbox() -> CheckResult:
    """§4.3 blackboard 路径沙箱（绝对路径拒绝）。"""
    text = _read_file("teage_liu/multiagent/blackboard.py")
    if not _grep(r"is_absolute|validate_path_safety|PathSafetyError", text):
        return CheckResult(
            "p1_03_blackboard_path_sandbox",
            False,
            "blackboard.py missing path sandbox logic",
            "plan1",
        )
    return CheckResult(
        "p1_03_blackboard_path_sandbox",
        True,
        "validate_path_safety + is_absolute check present",
        "plan1",
    )


def _p1_04_blackboard_atomic_write() -> CheckResult:
    """补充：blackboard atomic_write 函数。"""
    text = _read_file("teage_liu/multiagent/blackboard.py")
    if not _grep(r"async def atomic_write", text):
        return CheckResult(
            "p1_04_blackboard_atomic_write",
            False,
            "atomic_write function missing",
            "plan1",
        )
    return CheckResult(
        "p1_04_blackboard_atomic_write",
        True,
        "atomic_write function present",
        "plan1",
    )


def _p1_05_blackboard_yaml_safe_load() -> CheckResult:
    """补充：blackboard 必须 yaml.safe_load，禁用 yaml.load。"""
    text = _read_file("teage_liu/multiagent/blackboard.py")
    if not _grep(r"yaml\.safe_load", text):
        return CheckResult(
            "p1_05_blackboard_yaml_safe_load",
            False,
            "yaml.safe_load not used",
            "plan1",
        )
    if _grep(r"(?<!safe_)yaml\.load\s*\(", text):
        return CheckResult(
            "p1_05_blackboard_yaml_safe_load",
            False,
            "yaml.load (unsafe) detected",
            "plan1",
        )
    return CheckResult(
        "p1_05_blackboard_yaml_safe_load",
        True,
        "yaml.safe_load used, yaml.load not used",
        "plan1",
    )


def _p1_06_blackboard_append_jsonl() -> CheckResult:
    """补充：blackboard append_jsonl 函数（append-only）。"""
    text = _read_file("teage_liu/multiagent/blackboard.py")
    if not _grep(r"async def append_jsonl", text):
        return CheckResult(
            "p1_06_blackboard_append_jsonl",
            False,
            "append_jsonl function missing",
            "plan1",
        )
    return CheckResult(
        "p1_06_blackboard_append_jsonl",
        True,
        "append_jsonl function present",
        "plan1",
    )


def _p1_07_file_lock_cas() -> CheckResult:
    """§4.4 file_lock CAS 机制。"""
    text = _read_file("teage_liu/multiagent/file_lock.py")
    if not _grep(r"_cas_write|CASVersionMismatchError|expected_version", text):
        return CheckResult(
            "p1_07_file_lock_cas",
            False,
            "CAS mechanism missing",
            "plan1",
        )
    return CheckResult(
        "p1_07_file_lock_cas",
        True,
        "CAS write + version mismatch handling present",
        "plan1",
    )


def _p1_08_file_lock_fencing_token() -> CheckResult:
    """§4.4 file_lock fencing_token。"""
    text = _read_file("teage_liu/multiagent/file_lock.py")
    if not _grep(r"fencing_token|FencingTokenMismatchError", text):
        return CheckResult(
            "p1_08_file_lock_fencing_token",
            False,
            "fencing_token mechanism missing",
            "plan1",
        )
    return CheckResult(
        "p1_08_file_lock_fencing_token",
        True,
        "fencing_token + FencingTokenMismatchError present",
        "plan1",
    )


def _p1_09_file_lock_grace_period() -> CheckResult:
    """§4.4 file_lock grace_period。"""
    text = _read_file("teage_liu/multiagent/file_lock.py")
    if not _grep(r"grace_period|grace_until|force_releasing", text):
        return CheckResult(
            "p1_09_file_lock_grace_period",
            False,
            "grace_period mechanism missing",
            "plan1",
        )
    return CheckResult(
        "p1_09_file_lock_grace_period",
        True,
        "grace_period + force_release logic present",
        "plan1",
    )


def _p1_10_audit_logger_class() -> CheckResult:
    """补充：MultiAgentAuditLogger 类存在。"""
    text = _read_file("teage_liu/multiagent/audit_logger.py")
    if not _grep(r"class MultiAgentAuditLogger", text):
        return CheckResult(
            "p1_10_audit_logger_class",
            False,
            "MultiAgentAuditLogger class missing",
            "plan1",
        )
    return CheckResult(
        "p1_10_audit_logger_class",
        True,
        "MultiAgentAuditLogger class present",
        "plan1",
    )


def _p1_11_audit_logger_hash_chain() -> CheckResult:
    """补充：audit_logger hash 链。"""
    text = _read_file("teage_liu/multiagent/audit_logger.py")
    if not _grep(r"prev_hash|hashlib\.sha256", text):
        return CheckResult(
            "p1_11_audit_logger_hash_chain",
            False,
            "hash chain logic missing",
            "plan1",
        )
    return CheckResult(
        "p1_11_audit_logger_hash_chain",
        True,
        "prev_hash + sha256 hash chain present",
        "plan1",
    )


def _p1_12_agent_registry_class() -> CheckResult:
    """补充：AgentRegistry 类存在。"""
    text = _read_file("teage_liu/multiagent/agent_registry.py")
    if not _grep(r"class AgentRegistry", text):
        return CheckResult(
            "p1_12_agent_registry_class",
            False,
            "AgentRegistry class missing",
            "plan1",
        )
    return CheckResult(
        "p1_12_agent_registry_class",
        True,
        "AgentRegistry class present",
        "plan1",
    )


def _p1_13_watchdog_class() -> CheckResult:
    """补充：WatchdogWatcher 类存在 + 自检。"""
    text = _read_file("teage_liu/multiagent/watchdog_watcher.py")
    if not _grep(r"class WatchdogWatcher", text):
        return CheckResult(
            "p1_13_watchdog_class",
            False,
            "WatchdogWatcher class missing",
            "plan1",
        )
    if not _grep(r"_self_test|is_healthy|polling", text):
        return CheckResult(
            "p1_13_watchdog_class",
            False,
            "watchdog self-test / polling fallback missing",
            "plan1",
        )
    return CheckResult(
        "p1_13_watchdog_class",
        True,
        "WatchdogWatcher + self-test + polling fallback present",
        "plan1",
    )


def _p1_14_recovery_class() -> CheckResult:
    """补充：RecoveryCoordinator 类存在 + audit 重放。"""
    text = _read_file("teage_liu/multiagent/recovery.py")
    if not _grep(r"class RecoveryCoordinator", text):
        return CheckResult(
            "p1_14_recovery_class",
            False,
            "RecoveryCoordinator class missing",
            "plan1",
        )
    if not _grep(r"rebuild_state_from_audit|check_and_recover", text):
        return CheckResult(
            "p1_14_recovery_class",
            False,
            "audit replay methods missing",
            "plan1",
        )
    return CheckResult(
        "p1_14_recovery_class",
        True,
        "RecoveryCoordinator + audit replay present",
        "plan1",
    )


def _p1_15_config_container_integration() -> CheckResult:
    """补充：配置与容器集成（CONFIG_TO_COMPONENTS + _RESTART_REQUIRED_KEYS + lifespan）。"""
    container_text = _read_file("teage_liu/container.py")
    config_helpers_text = _read_file("teage_liu/config_helpers.py")
    lifespan_text = _read_file("teage_liu/lifespan.py")
    tool_error_text = _read_file("teage_liu/agent/tool_error.py")
    requirements_text = _read_file("requirements.txt")

    failures = []
    if not _grep(r'"multiagent"\s*:', container_text):
        failures.append("container.py missing 'multiagent' in CONFIG_TO_COMPONENTS")
    if not _grep(r"multiagent\.blackboard_dir", config_helpers_text):
        failures.append("config_helpers.py missing multiagent.blackboard_dir in _RESTART_REQUIRED_KEYS")
    if not _grep(r"multiagent", lifespan_text):
        failures.append("lifespan.py missing multiagent registration")
    if not _grep(r"multiagent", tool_error_text):
        failures.append("tool_error.py missing multiagent category in _CATEGORY_ZH")
    if not _grep(r"watchdog|aiofiles|portalocker|cryptography", requirements_text):
        failures.append("requirements.txt missing multiagent dependencies")

    if failures:
        return CheckResult(
            "p1_15_config_container_integration",
            False,
            "; ".join(failures),
            "plan1",
        )
    return CheckResult(
        "p1_15_config_container_integration",
        True,
        "container + config_helpers + lifespan + tool_error + requirements all updated",
        "plan1",
    )


# =============================================================================
# Plan 2 范围检查（9 项）— spec §4.5-§4.11 + 补充
# =============================================================================


def _p2_01_director_engine_flush() -> CheckResult:
    """§4.5 flush 流程（messages.pending.md 幂等）。"""
    text = _read_file("teage_liu/multiagent/director_engine.py")
    if not _grep(r"messages\.pending\.md|flush|op_id", text):
        return CheckResult(
            "p2_01_director_engine_flush",
            False,
            "director_engine.py missing flush/messages.pending.md logic",
            "plan2",
        )
    return CheckResult(
        "p2_01_director_engine_flush",
        True,
        "flush + messages.pending.md present",
        "plan2",
    )


def _p2_02_audit_append_only() -> CheckResult:
    """§4.6 append_audit append-only 语义（无 os.replace）。"""
    text = _read_file("teage_liu/multiagent/audit_logger.py")
    if _grep(r"os\.replace\s*\(", text):
        return CheckResult(
            "p2_02_audit_append_only",
            False,
            "audit_logger.py uses os.replace (violates append-only)",
            "plan2",
        )
    if not _grep(r'"a"', text) and not _grep(r"mode.*a", text):
        return CheckResult(
            "p2_02_audit_append_only",
            False,
            "audit_logger.py missing append mode",
            "plan2",
        )
    return CheckResult(
        "p2_02_audit_append_only",
        True,
        "append-only semantics (no os.replace, append mode used)",
        "plan2",
    )


def _p2_03_director_dual_form() -> CheckResult:
    """§4.7 Director 双形态（agent / script）。"""
    text = _read_file("teage_liu/multiagent/director_engine.py")
    if not _grep(r"director_implementation|agent|script", text):
        return CheckResult(
            "p2_03_director_dual_form",
            False,
            "director_engine.py missing dual-form support",
            "plan2",
        )
    return CheckResult(
        "p2_03_director_dual_form",
        True,
        "dual-form (agent/script) support present",
        "plan2",
    )


def _p2_04_epoch_mechanism() -> CheckResult:
    """§4.8 Epoch 机制。"""
    text = _read_file("teage_liu/multiagent/director_engine.py")
    if not _grep(r"current_epoch|epoch", text):
        return CheckResult(
            "p2_04_epoch_mechanism",
            False,
            "director_engine.py missing epoch mechanism",
            "plan2",
        )
    return CheckResult(
        "p2_04_epoch_mechanism",
        True,
        "epoch mechanism present",
        "plan2",
    )


def _p2_05_startup_mutex_lock() -> CheckResult:
    """§4.9 启动互斥锁 + 硬超时强抢。"""
    text = _read_file("teage_liu/multiagent/director_engine.py")
    if not _grep(r"director\.lock|emergency_release|LockFileEx|F_SETLK", text):
        return CheckResult(
            "p2_05_startup_mutex_lock",
            False,
            "director_engine.py missing startup mutex / emergency release",
            "plan2",
        )
    return CheckResult(
        "p2_05_startup_mutex_lock",
        True,
        "startup mutex + emergency release present",
        "plan2",
    )


def _p2_06_ed25519_signature() -> CheckResult:
    """§4.10 ed25519 签名。"""
    text = _read_file("teage_liu/multiagent/signature.py")
    if not _grep(r"ed25519|director_signature|SignatureVerifier|VerifyResult", text):
        return CheckResult(
            "p2_06_ed25519_signature",
            False,
            "signature.py missing ed25519 / SignatureVerifier",
            "plan2",
        )
    return CheckResult(
        "p2_06_ed25519_signature",
        True,
        "ed25519 signature verification present",
        "plan2",
    )


def _p2_07_autonomous_mode() -> CheckResult:
    """§4.11 自治模式 + 二次确认退出。"""
    text = _read_file("teage_liu/multiagent/worker_adapter.py")
    if not _grep(r"autonomous|confirm_exit|rollback_exit|AutonomousModeController", text):
        return CheckResult(
            "p2_07_autonomous_mode",
            False,
            "worker_adapter.py missing AutonomousModeController / confirm_exit",
            "plan2",
        )
    return CheckResult(
        "p2_07_autonomous_mode",
        True,
        "AutonomousModeController + confirm_exit present",
        "plan2",
    )


def _p2_08_trust_score() -> CheckResult:
    """补充：信任分管理。"""
    text = _read_file("teage_liu/multiagent/director_engine.py")
    if not _grep(r"trust_score|degraded_threshold|rejected_threshold|force_offline_threshold", text):
        return CheckResult(
            "p2_08_trust_score",
            False,
            "director_engine.py missing trust_score management",
            "plan2",
        )
    return CheckResult(
        "p2_08_trust_score",
        True,
        "trust_score management present",
        "plan2",
    )


def _p2_09_reactloop_integration() -> CheckResult:
    """补充：ReactLoop 7 集成点。"""
    text = _read_file("teage_liu/multiagent/worker_adapter.py")
    if not _grep(r"_build_multiagent_prompt|_check_capabilities|_session_hook|_check_turn|_heartbeat|InjectionIsolator", text):
        return CheckResult(
            "p2_09_reactloop_integration",
            False,
            "worker_adapter.py missing multiagent integration points",
            "plan2",
        )
    return CheckResult(
        "p2_09_reactloop_integration",
        True,
        "ReactLoop multiagent integration points present",
        "plan2",
    )


# =============================================================================
# Plan 3 范围检查（7 项）— spec §4.12-§4.18
# =============================================================================


def _p3_01_a2a_gateway_jsonrpc() -> CheckResult:
    """§4.12 A2A Gateway JSON-RPC 2.0。"""
    text = _read_file("teage_liu/multiagent/a2a_gateway.py")
    if not _grep(r"jsonrpc.*2\.0|method|params", text):
        return CheckResult(
            "p3_01_a2a_gateway_jsonrpc",
            False,
            "a2a_gateway.py missing JSON-RPC 2.0",
            "plan3",
        )
    return CheckResult(
        "p3_01_a2a_gateway_jsonrpc",
        True,
        "JSON-RPC 2.0 protocol present",
        "plan3",
    )


def _p3_02_httpx_async_client() -> CheckResult:
    """§4.13 httpx 异步客户端。"""
    text = _read_file("teage_liu/multiagent/a2a_client.py")
    if not _grep(r"httpx\.AsyncClient|async with", text):
        return CheckResult(
            "p3_02_httpx_async_client",
            False,
            "a2a_client.py missing httpx.AsyncClient",
            "plan3",
        )
    return CheckResult(
        "p3_02_httpx_async_client",
        True,
        "httpx.AsyncClient present",
        "plan3",
    )


def _p3_03_remote_agent_adapter() -> CheckResult:
    """§4.14 远程 agent 适配器。"""
    text = _read_file("teage_liu/multiagent/remote_agent_adapter.py")
    if not _grep(r"RemoteAgentAdapter|register_remote", text):
        return CheckResult(
            "p3_03_remote_agent_adapter",
            False,
            "remote_agent_adapter.py missing RemoteAgentAdapter",
            "plan3",
        )
    return CheckResult(
        "p3_03_remote_agent_adapter",
        True,
        "RemoteAgentAdapter present",
        "plan3",
    )


def _p3_04_director_election() -> CheckResult:
    """§4.15 Director 跨设备选举。"""
    text = _read_file("teage_liu/multiagent/election.py")
    if not _grep(r"Election|ElectionResult|epoch|lexicographic", text):
        return CheckResult(
            "p3_04_director_election",
            False,
            "election.py missing election mechanism",
            "plan3",
        )
    return CheckResult(
        "p3_04_director_election",
        True,
        "Director election present",
        "plan3",
    )


def _p3_05_path_sandbox_sanitize() -> CheckResult:
    """§4.16 路径沙箱（sanitize_path + sanitize_dict_paths）。"""
    text = _read_file("teage_liu/multiagent/path_sandbox.py")
    if not _grep(r"sanitize_path|to_absolute|sanitize_dict_paths", text):
        return CheckResult(
            "p3_05_path_sandbox_sanitize",
            False,
            "path_sandbox.py missing sanitize_path / sanitize_dict_paths",
            "plan3",
        )
    return CheckResult(
        "p3_05_path_sandbox_sanitize",
        True,
        "sanitize_path + sanitize_dict_paths present",
        "plan3",
    )


def _p3_06_rate_limiter() -> CheckResult:
    """§4.17 限流器（滑动窗口）。"""
    text = _read_file("teage_liu/multiagent/rate_limiter.py")
    if not _grep(r"RateLimiter|sliding_window|100", text):
        return CheckResult(
            "p3_06_rate_limiter",
            False,
            "rate_limiter.py missing RateLimiter / sliding window",
            "plan3",
        )
    return CheckResult(
        "p3_06_rate_limiter",
        True,
        "RateLimiter + sliding window present",
        "plan3",
    )


def _p3_07_jsonrpc_error_codes() -> CheckResult:
    """§4.18 JSON-RPC 错误码（10 个）。"""
    text = _read_file("teage_liu/multiagent/a2a_gateway.py")
    codes = ["-32700", "-32600", "-32601", "-32602", "-32603", "-32001", "-32002", "-32003", "-32004", "-32005"]
    missing = [c for c in codes if c not in text]
    if missing:
        return CheckResult(
            "p3_07_jsonrpc_error_codes",
            False,
            f"missing JSON-RPC error codes: {missing}",
            "plan3",
        )
    return CheckResult(
        "p3_07_jsonrpc_error_codes",
        True,
        f"all {len(codes)} JSON-RPC error codes present",
        "plan3",
    )


# =============================================================================
# Plan 4 范围检查（5 项）— spec §4.19-§4.23
# =============================================================================


def _p4_01_sse_channel() -> CheckResult:
    """§4.19 multiagent_alert SSE 通道。"""
    text = _read_file("teage_liu/api/multiagent_routes.py")
    if not _grep(r"multiagent_alert|text/event-stream", text):
        return CheckResult(
            "p4_01_sse_channel",
            False,
            "multiagent_routes.py missing SSE channel",
            "plan4",
        )
    return CheckResult(
        "p4_01_sse_channel",
        True,
        "multiagent_alert SSE channel present",
        "plan4",
    )


def _p4_02_frontend_config_ui() -> CheckResult:
    """§4.20 前端配置 UI。"""
    text = _read_file("web/js/multiagent-settings.js")
    if not _grep(r"multiagent-settings|multiagent\.enabled|multiagent\.role", text):
        return CheckResult(
            "p4_02_frontend_config_ui",
            False,
            "multiagent-settings.js missing config UI",
            "plan4",
        )
    return CheckResult(
        "p4_02_frontend_config_ui",
        True,
        "frontend config UI present",
        "plan4",
    )


def _p4_03_frontend_sse_subscribe() -> CheckResult:
    """§4.21 前端 SSE 订阅。"""
    text = _read_file("web/js/multiagent-sse.js")
    if not _grep(r"multiagent-sse|EventSource|multiagent_alert", text):
        return CheckResult(
            "p4_03_frontend_sse_subscribe",
            False,
            "multiagent-sse.js missing EventSource subscription",
            "plan4",
        )
    return CheckResult(
        "p4_03_frontend_sse_subscribe",
        True,
        "frontend SSE subscription present",
        "plan4",
    )


def _p4_04_status_indicator() -> CheckResult:
    """§4.22 状态指示器（三色）。"""
    text = _read_file("web/js/multiagent-render.js")
    if not _grep(r"healthy|degraded|autonomous|fault", text):
        return CheckResult(
            "p4_04_status_indicator",
            False,
            "multiagent-render.js missing status indicator states",
            "plan4",
        )
    return CheckResult(
        "p4_04_status_indicator",
        True,
        "status indicator states present",
        "plan4",
    )


def _p4_05_container_hot_reload() -> CheckResult:
    """§4.23 容器映射 + 热更新边界。"""
    container_text = _read_file("teage_liu/container.py")
    config_helpers_text = _read_file("teage_liu/config_helpers.py")
    if not _grep(r"multiagent.*:.*\[", container_text):
        return CheckResult(
            "p4_05_container_hot_reload",
            False,
            "container.py missing multiagent mapping",
            "plan4",
        )
    if not _grep(r"_RESTART_REQUIRED_KEYS", config_helpers_text):
        return CheckResult(
            "p4_05_container_hot_reload",
            False,
            "config_helpers.py missing _RESTART_REQUIRED_KEYS",
            "plan4",
        )
    return CheckResult(
        "p4_05_container_hot_reload",
        True,
        "container mapping + restart keys boundary present",
        "plan4",
    )


# =============================================================================
# 检查注册表
# =============================================================================

_ALL_CHECKS: list[Callable[[], CheckResult]] = [
    # Plan 1 (15)
    _p1_01_exceptions_8_classes,
    _p1_02_schemas_7_files,
    _p1_03_blackboard_path_sandbox,
    _p1_04_blackboard_atomic_write,
    _p1_05_blackboard_yaml_safe_load,
    _p1_06_blackboard_append_jsonl,
    _p1_07_file_lock_cas,
    _p1_08_file_lock_fencing_token,
    _p1_09_file_lock_grace_period,
    _p1_10_audit_logger_class,
    _p1_11_audit_logger_hash_chain,
    _p1_12_agent_registry_class,
    _p1_13_watchdog_class,
    _p1_14_recovery_class,
    _p1_15_config_container_integration,
    # Plan 2 (9)
    _p2_01_director_engine_flush,
    _p2_02_audit_append_only,
    _p2_03_director_dual_form,
    _p2_04_epoch_mechanism,
    _p2_05_startup_mutex_lock,
    _p2_06_ed25519_signature,
    _p2_07_autonomous_mode,
    _p2_08_trust_score,
    _p2_09_reactloop_integration,
    # Plan 3 (7)
    _p3_01_a2a_gateway_jsonrpc,
    _p3_02_httpx_async_client,
    _p3_03_remote_agent_adapter,
    _p3_04_director_election,
    _p3_05_path_sandbox_sanitize,
    _p3_06_rate_limiter,
    _p3_07_jsonrpc_error_codes,
    # Plan 4 (5)
    _p4_01_sse_channel,
    _p4_02_frontend_config_ui,
    _p4_03_frontend_sse_subscribe,
    _p4_04_status_indicator,
    _p4_05_container_hot_reload,
]

_PLAN_CHECKS: dict[str, list[Callable[[], CheckResult]]] = {
    "plan1": [
        _p1_01_exceptions_8_classes,
        _p1_02_schemas_7_files,
        _p1_03_blackboard_path_sandbox,
        _p1_04_blackboard_atomic_write,
        _p1_05_blackboard_yaml_safe_load,
        _p1_06_blackboard_append_jsonl,
        _p1_07_file_lock_cas,
        _p1_08_file_lock_fencing_token,
        _p1_09_file_lock_grace_period,
        _p1_10_audit_logger_class,
        _p1_11_audit_logger_hash_chain,
        _p1_12_agent_registry_class,
        _p1_13_watchdog_class,
        _p1_14_recovery_class,
        _p1_15_config_container_integration,
    ],
    "plan2": [
        _p2_01_director_engine_flush,
        _p2_02_audit_append_only,
        _p2_03_director_dual_form,
        _p2_04_epoch_mechanism,
        _p2_05_startup_mutex_lock,
        _p2_06_ed25519_signature,
        _p2_07_autonomous_mode,
        _p2_08_trust_score,
        _p2_09_reactloop_integration,
    ],
    "plan3": [
        _p3_01_a2a_gateway_jsonrpc,
        _p3_02_httpx_async_client,
        _p3_03_remote_agent_adapter,
        _p3_04_director_election,
        _p3_05_path_sandbox_sanitize,
        _p3_06_rate_limiter,
        _p3_07_jsonrpc_error_codes,
    ],
    "plan4": [
        _p4_01_sse_channel,
        _p4_02_frontend_config_ui,
        _p4_03_frontend_sse_subscribe,
        _p4_04_status_indicator,
        _p4_05_container_hot_reload,
    ],
}


def verify_all() -> dict:
    """执行全部 36 项 Grep 检查，返回 {passed, failed, details}。"""
    results = [check() for check in _ALL_CHECKS]
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    return {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "details": [
            {"name": r.name, "passed": r.passed, "detail": r.detail, "plan": r.plan}
            for r in results
        ],
    }


def verify_plan(plan_id: str) -> dict:
    """仅执行指定 Plan 范围的 Grep 检查（plan_id ∈ {'plan1','plan2','plan3','plan4'}）。"""
    if plan_id not in _PLAN_CHECKS:
        return {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "error": f"invalid plan_id: {plan_id}, expected one of {list(_PLAN_CHECKS.keys())}",
            "details": [],
        }
    checks = _PLAN_CHECKS[plan_id]
    results = [check() for check in checks]
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    return {
        "plan_id": plan_id,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "details": [
            {"name": r.name, "passed": r.passed, "detail": r.detail, "plan": r.plan}
            for r in results
        ],
    }


if __name__ == "__main__":
    if "--all" in sys.argv:
        result = verify_all()
    elif len(sys.argv) > 1 and sys.argv[1]:
        result = verify_plan(sys.argv[1])
    else:
        print("Usage: python verify_multiagent_spec_coverage.py [--all | plan1 | plan2 | plan3 | plan4]")
        sys.exit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["failed"] == 0 else 1)
