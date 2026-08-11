"""配置文件工具函数。

从 server.py 提取，包含配置合并、备份、原子写入、校验、重启检测与运行时应用。
_apply_runtime_config 通过 DI 容器 / app.dependency_overrides 获取运行时组件，
不再反射 server 模块全局变量。
"""
from __future__ import annotations

import copy
import logging
import os
import shutil
import threading
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger("teage_liu.server")

from teage_liu.tasks.cron_expr import CronExpr
_RESTART_REQUIRED_KEYS = {
    "server",
    "storage.sqlite_path",
    "memory.chroma_path",
    "memory.memory_md_path",
    "history.persistence_dir",
    "security.rules",
    "schedules",
    "multiagent.blackboard_dir",
    "multiagent.main_session_collab",
    "a2a.listen_host",
    "a2a.listen_port",
    # 标准 A2A 面：叶子级精确匹配（card 身份/mode 变更需重启）
    "a2a.standard.enabled",
    "a2a.standard.mode",
    "a2a.standard.base_url",
    "a2a.standard.endpoint_url",
    "a2a.standard.task_store_path",
}

_MISSING = object()

_config_write_lock = threading.Lock()

_RUNTIME_HOTUPDATE_MAP = {
    "memory.consolidation_threshold": ("consolidation_engine.threshold", int),
    "memory.dedup_similarity_threshold": ("consolidation_engine.dedup_threshold", float),
    "memory.retrieval_top_k": ("memory_retriever.top_k", int),
    "memory.history_max_turns": ("history_buffer.max_turns", int),
    "tools.max_react_loops": ("react_loop.max_loops", int),
    "tools.defer_loading_threshold": ("tool_registry.defer_loading_threshold", int),
    "security.approval_timeout_seconds": ("approval_manager.timeout", float),
    "memory.surprise_gate_enabled": ("consolidation_engine.surprise_gate_enabled", bool),
    "memory.surprise_similarity_threshold": ("consolidation_engine.surprise_similarity_threshold", float),
    "memory.surprise_skip_threshold": ("consolidation_engine.surprise_skip_threshold", float),
    "memory.decay_rate": ("decay.decay_rate", float),
    "memory.frequency_weight": ("decay.frequency_weight", float),
    "security.enabled": ("policy_engine.enabled", bool),
    "guardrails.input_scan.enabled": ("guardrail_engine.input_scan_enabled", bool),
    "guardrails.sanitizer.enabled": ("guardrail_engine.sanitizer_enabled", bool),
    "guardrails.output_filter.enabled": ("guardrail_engine.output_filter_enabled", bool),
    "reasoning.main.enabled": ("llm_client.main_reasoning_enabled", bool),
    "reasoning.main.effort": ("llm_client.main_reasoning_effort", str),
    "reasoning.main.budget_tokens": ("llm_client.main_reasoning_budget_tokens", int),
    "reasoning.cron.enabled": ("llm_client.cron_reasoning_enabled", bool),
    "reasoning.cron.effort": ("llm_client.cron_reasoning_effort", str),
    "reasoning.persist_thinking": ("history_buffer.persist_thinking", bool),
    "cron.inject_history": ("cron_inject_history_enabled", bool),
}


def _deep_merge_config(old: dict, new: dict) -> dict:
    """深度合并两个配置字典，返回新字典（不修改入参）。

    合并规则：
    - dict + dict → 递归合并；
    - dict + 非 dict → 用新值覆盖；
    - list + list → 用新值覆盖（不拼接）；
    - 标量 + 标量 → 用新值覆盖；
    - 旧 key 未在新配置中出现 → 保留旧值。
    """
    merged: dict = {}
    for k, v in old.items():
        merged[k] = copy.deepcopy(v)
    for k, new_v in new.items():
        old_v = merged.get(k, _MISSING)
        if old_v is not _MISSING and isinstance(old_v, dict) and isinstance(new_v, dict):
            merged[k] = _deep_merge_config(old_v, new_v)
        else:
            merged[k] = copy.deepcopy(new_v)
    return merged


def _backup_config(config_path: str) -> None:
    """备份配置文件到 ``config_path + ".bak"``。"""
    if not os.path.exists(config_path):
        logger.debug("配置文件不存在，跳过备份: %s", config_path)
        return
    try:
        shutil.copy2(config_path, config_path + ".bak")
        logger.debug("已备份配置文件: %s -> %s.bak", config_path, config_path)
    except Exception as e:
        logger.warning("备份配置失败: %s", e)


def _atomic_write_config(config_path: str, data: dict) -> None:
    """原子写入配置文件，避免写入中途崩溃导致配置损坏。"""
    tmp_path = config_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


def _validate_config_schema(config: dict) -> None:
    """校验配置字典的结构与关键字段类型。"""
    if not isinstance(config, dict):
        raise ValueError("配置校验失败: 配置根节点必须是字典")
    for seg in ("llm", "memory", "server", "storage", "skills", "monitoring", "tools", "security", "history", "multiagent"):
        if seg in config and not isinstance(config[seg], dict):
            raise ValueError(f"配置校验失败: {seg} 必须是字典")
    llm = config.get("llm")
    if isinstance(llm, dict):
        main_model = llm.get("main_model")
        if not isinstance(main_model, str) or not main_model.strip():
            raise ValueError("配置校验失败: llm.main_model 必填且不能为空")
        consolidation_model = llm.get("consolidation_model")
        if not isinstance(consolidation_model, str) or not consolidation_model.strip():
            raise ValueError("配置校验失败: llm.consolidation_model 必填且不能为空")
    server = config.get("server")
    if isinstance(server, dict) and "port" in server:
        if not isinstance(server["port"], int) or isinstance(server["port"], bool):
            raise ValueError("配置校验失败: server.port 必须为整数")
    memory = config.get("memory")
    if isinstance(memory, dict):
        if "consolidation_threshold" in memory:
            v = memory["consolidation_threshold"]
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("配置校验失败: memory.consolidation_threshold 必须为整数")
        if "retrieval_top_k" in memory:
            v = memory["retrieval_top_k"]
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("配置校验失败: memory.retrieval_top_k 必须为整数")
        for field in ("decay_rate", "frequency_weight"):
            if field in memory:
                v = memory[field]
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise ValueError(
                        f"配置校验失败: memory.{field} 必须为数值"
                    )
        condenser = memory.get("condenser")
        if condenser is not None:
            if not isinstance(condenser, dict):
                raise ValueError("配置校验失败: memory.condenser 必须是字典")
            if "enabled" in condenser and not isinstance(condenser["enabled"], bool):
                raise ValueError("配置校验失败: memory.condenser.enabled 必须为布尔值")
            if "strategy" in condenser:
                v = condenser["strategy"]
                if not isinstance(v, str) or v not in ("masking", "llm_summary"):
                    raise ValueError(
                        "配置校验失败: memory.condenser.strategy 必须为 'masking' 或 'llm_summary'"
                    )
            for field in ("keep_recent_n", "keep_first", "llm_summary_threshold"):
                if field in condenser:
                    v = condenser[field]
                    if not isinstance(v, int) or isinstance(v, bool):
                        raise ValueError(
                            f"配置校验失败: memory.condenser.{field} 必须为整数"
                        )
    security = config.get("security")
    if isinstance(security, dict):
        if "enabled" in security and not isinstance(security["enabled"], bool):
            raise ValueError("配置校验失败: security.enabled 必须为布尔值")
        if "approval_timeout_seconds" in security:
            v = security["approval_timeout_seconds"]
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError("配置校验失败: security.approval_timeout_seconds 必须为数值")
        if "rules" in security and not isinstance(security["rules"], list):
            raise ValueError("配置校验失败: security.rules 必须是列表")

    tasks = config.get("tasks")
    if tasks is not None and not isinstance(tasks, dict):
        raise ValueError("配置校验失败: tasks 必须是字典")

    schedules = config.get("schedules")
    if schedules is not None:
        if not isinstance(schedules, list):
            raise ValueError("配置校验失败: schedules 必须是列表")
        for i, item in enumerate(schedules):
            if not isinstance(item, dict):
                raise ValueError(f"配置校验失败: schedules[{i}] 必须是字典")
            cron = item.get("cron")
            if not isinstance(cron, str) or not cron.strip():
                raise ValueError(f"配置校验失败: schedules[{i}].cron 必填且为非空字符串")
            task = item.get("task")
            if not isinstance(task, str) or not task.strip():
                raise ValueError(f"配置校验失败: schedules[{i}].task 必填且为非空字符串")
            if "enabled" in item and not isinstance(item["enabled"], bool):
                raise ValueError(f"配置校验失败: schedules[{i}].enabled 必须为布尔值")
            if "id" in item and not isinstance(item["id"], str):
                raise ValueError(f"配置校验失败: schedules[{i}].id 必须为字符串")
            if "name" in item and not isinstance(item["name"], str):
                raise ValueError(f"配置校验失败: schedules[{i}].name 必须为字符串")
            if CronExpr is not None:
                try:
                    CronExpr(cron)
                except ValueError as e:
                    raise ValueError(f"配置校验失败: schedules[{i}].cron 非法: {e}")


def _check_needs_restart(old_config: dict, new_config: dict) -> bool:
    """检查配置变更是否需要重启才能生效。"""
    for key in _RESTART_REQUIRED_KEYS:
        parts = key.split(".")
        old_val = old_config
        new_val = new_config
        for p in parts:
            if not isinstance(old_val, dict):
                break
            if not isinstance(new_val, dict):
                break
            old_val = old_val.get(p, _MISSING)
            new_val = new_val.get(p, _MISSING)
        if old_val != new_val:
            return True
    return False


def _resolve_component(getter_func, container_key: str):
    """解析组件实例，优先尊重 app.dependency_overrides（供测试注入 mock）。

    解析顺序：
    1. app.dependency_overrides 中是否注册了 getter_func 的 override（测试场景）；
    2. 全局 DI 容器（生产场景，由 lifespan 初始化）；
    3. 返回 None。
    """
    try:
        from teage_liu.app import app
        if getter_func in app.dependency_overrides:
            return app.dependency_overrides[getter_func]()
    except Exception:
        pass
    try:
        from teage_liu.app import get_container
        container = get_container()
        if container is None:
            return None
        return container.get(container_key)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LLMClient 精准热重载（核心修复）
# ---------------------------------------------------------------------------
# orchestrator 注册为 hot_reloadable=False（持有大量运行时状态，整体重建会丢会话），
# 因此 llm 段变更时 container.reload 不会重建 orchestrator，LLMClient 也不会更新。
# 本节实现"精准热更新"：检测 llm 段关键字段变更，单独重建 LLMClient 并替换到
# orchestrator 及其子组件的所有引用点，保持运行时状态（history_buffer/chroma_store 等）
# 不变。失败时回滚到旧 LLMClient，避免半成功状态。

# 影响 LLMClient 实例的配置字段（变更需重建 LLMClient）
_LLM_RELOAD_KEYS = {
    "main_api_key", "consolidation_api_key",
    "main_provider", "consolidation_provider",
    "main_model", "consolidation_model",
    "main_base_url", "consolidation_base_url",
}


def _llm_config_changed(old_config: dict, new_config: dict) -> bool:
    """检测 llm 段中影响 LLMClient 的字段是否变更。

    对比 ``_LLM_RELOAD_KEYS`` 中列出的字段，任一不同即返回 True。
    其余 llm 字段（如 activity_timeout / max_context_tokens）走原有
    热更新路径或不影响 LLMClient 实例。
    """
    old_llm = (old_config or {}).get("llm", {}) or {}
    new_llm = (new_config or {}).get("llm", {}) or {}
    for key in _LLM_RELOAD_KEYS:
        if old_llm.get(key) != new_llm.get(key):
            return True
    return False


def _reload_llm_client(orchestrator: Any, new_config: dict) -> bool:
    """重建 LLMClient 并替换到 orchestrator 及其子组件的所有引用点。

    原子性策略：先创建新 LLMClient，成功后才批量替换所有引用点；
    失败则保留旧 LLMClient 不变，返回 False。

    引用点清单（基于 Orchestrator.__init__ 装配链路）：
    - ``orchestrator.llm_client``（顶层引用）
    - ``orchestrator.react_loop.llm_client``
    - ``orchestrator.consolidation_engine.llm_client``
    - ``orchestrator.memory_retriever.llm_client``
    - ``orchestrator.condenser.llm_client`` + ``condenser._token_counter``
      （仅 LLMSummaryCondenser 持有 llm_client，MaskingCondenser 无）
    - ``orchestrator.session_mgr._llm_client``（私有属性）

    参数:
        orchestrator: Orchestrator 实例。
        new_config: 已解析的新配置字典（含真实 API Key，非占位符）。

    返回:
        True 表示重建并替换成功；False 表示创建失败（已回滚）。
    """
    from teage_liu.orchestrator.factories import create_llm_client

    metrics = getattr(orchestrator, "metrics", None)
    old_llm_client = getattr(orchestrator, "llm_client", None)

    try:
        new_llm_client = create_llm_client(new_config, metrics=metrics)
    except Exception as e:
        logger.warning("LLMClient 重建失败（保留旧实例）: %s", e)
        return False

    if new_llm_client is None:
        # create_llm_client 内部已 try/except 并返回 None（如 API Key 缺失），
        # 保留旧实例避免把可用 LLMClient 替换成 None。
        logger.warning(
            "LLMClient 重建返回 None（保留旧实例）。请检查 llm 段配置完整性。"
        )
        return False

    # 批量替换所有引用点（getattr 守护兼容旧版本/未装配组件）
    orchestrator.llm_client = new_llm_client

    react_loop = getattr(orchestrator, "react_loop", None)
    if react_loop is not None:
        react_loop.llm_client = new_llm_client

    consolidation_engine = getattr(orchestrator, "consolidation_engine", None)
    if consolidation_engine is not None:
        consolidation_engine.llm_client = new_llm_client

    memory_retriever = getattr(orchestrator, "memory_retriever", None)
    if memory_retriever is not None:
        memory_retriever.llm_client = new_llm_client

    # condenser: 仅 LLMSummaryCondenser 持有 llm_client；
    # MaskingCondenser 无此属性，getattr 守护跳过
    condenser = getattr(orchestrator, "condenser", None)
    if condenser is not None and hasattr(condenser, "llm_client"):
        condenser.llm_client = new_llm_client
        # token_counter 与 llm_client 绑定（factories.py 中 token_counter 来自 llm_client）
        new_token_counter = getattr(new_llm_client, "count_messages_tokens", None)
        if hasattr(condenser, "_token_counter"):
            condenser._token_counter = new_token_counter

    # session_mgr 用私有属性 _llm_client
    session_mgr = getattr(orchestrator, "session_mgr", None)
    if session_mgr is not None and hasattr(session_mgr, "_llm_client"):
        session_mgr._llm_client = new_llm_client

    logger.info(
        "LLMClient 热重载成功: main=%s/%s, consolidation=%s/%s",
        new_config.get("llm", {}).get("main_provider", "?"),
        new_config.get("llm", {}).get("main_model", "?"),
        new_config.get("llm", {}).get("consolidation_provider", "?"),
        new_config.get("llm", {}).get("consolidation_model", "?"),
    )
    return True


def _apply_runtime_config(
    new_config: dict, old_config: Optional[dict] = None
) -> Dict[str, bool]:
    """将可热更新的运行时配置即时应用到内存中的 orchestrator 组件。

    重构后：通过 DI 容器 / app.dependency_overrides 获取组件，不再反射 server 模块
    全局变量。生产环境由 lifespan 初始化容器；测试环境通过
    ``app.dependency_overrides[get_orchestrator] = lambda: mock_orch`` 注入。

    参数:
        new_config: 已解析的新配置字典。
        old_config: 可选的旧配置字典，传入时用于检测 llm 段变更并触发
                    LLMClient 精准重建（绕过 orchestrator hot_reloadable=False 限制）。
                    默认 None 时不触发 LLM 重建（向后兼容旧调用方）。
    """
    from teage_liu.app import (
        get_orchestrator,
        get_approval_manager,
        get_audit_logger,
        get_etl_engine,
    )

    orchestrator = _resolve_component(get_orchestrator, "orchestrator")
    approval_manager = _resolve_component(get_approval_manager, "approval_manager")
    audit_logger = _resolve_component(get_audit_logger, "audit_logger")
    etl_engine = _resolve_component(get_etl_engine, "etl_engine")

    applied: Dict[str, bool] = {}
    if orchestrator is None:
        return applied

    for cfg_path, (attr_chain, conv) in _RUNTIME_HOTUPDATE_MAP.items():
        parts = cfg_path.split(".")
        val: Any = new_config
        for p in parts:
            if val is _MISSING or not isinstance(val, dict):
                break
            val = val.get(p, _MISSING)
        if val is _MISSING or val is None:
            continue

        obj: Any = orchestrator
        attr_parts = attr_chain.split(".")
        try:
            for ap in attr_parts[:-1]:
                obj = getattr(obj, ap)
            if obj is None:
                applied[cfg_path] = False
                continue
            setattr(obj, attr_parts[-1], conv(val))
            applied[cfg_path] = True
            logger.info("热更新 %s = %s", cfg_path, conv(val))
        except Exception as e:
            logger.warning("热更新 %s 失败: %s", cfg_path, e)
            applied[cfg_path] = False

    # Condenser 热更新
    memory_cfg = new_config.get("memory")
    if isinstance(memory_cfg, dict) and "condenser" in memory_cfg:
        condenser_cfg = memory_cfg.get("condenser") or {}
        try:
            orchestrator.apply_condenser_config(condenser_cfg)
            applied["memory.condenser"] = True
            logger.info("热更新 memory.condenser: %s", condenser_cfg)
        except Exception as e:
            logger.warning("热更新 memory.condenser 失败: %s", e)
            applied["memory.condenser"] = False

    # read_paths 专项热更新
    sec_cfg_rp = new_config.get("security") or {}
    rp_cfg = sec_cfg_rp.get("read_paths")
    if isinstance(rp_cfg, dict) and orchestrator is not None:
        try:
            orchestrator.policy_engine.set_read_paths(
                mode=rp_cfg.get("mode", "deny_first"),
                deny=rp_cfg.get("deny", []),
                allow=rp_cfg.get("allow", []),
                workspace_dirs=rp_cfg.get("workspace_dirs", []),
            )
            applied["security.read_paths"] = True
            logger.info("热更新 security.read_paths")
        except Exception as e:
            logger.warning("热更新 security.read_paths 失败: %s", e)
            applied["security.read_paths"] = False

    # 防护开关专项：关闭 HIL 时批量 deny pending 审批
    sec_cfg = new_config.get("security")
    if (
        isinstance(sec_cfg, dict)
        and "security.enabled" in applied
        and applied["security.enabled"]
        and not bool(sec_cfg.get("enabled", True))
    ):
        if approval_manager is not None:
            try:
                n = approval_manager.resolve_all("deny", "HIL 已关闭，审批自动拒绝")
                if n > 0:
                    logger.warning("HIL 关闭，自动 deny %d 条 pending 审批", n)
                    if audit_logger is not None:
                        audit_logger.log_guardrail_decision(
                            layer="policy_switch",
                            action="disable",
                            reason=f"HIL 关闭，自动 deny {n} 条 pending 审批",
                            session_id="system",
                            risk_level="high",
                        )
            except Exception as e:
                logger.warning("HIL 关闭专项处理失败: %s", e)

    # OCR 配置热更新专项
    ocr_cfg = new_config.get("files", {}).get("ocr", {}) or {}
    if etl_engine is not None and hasattr(etl_engine, "parser"):
        parser_obj = etl_engine.parser
        try:
            t_cfg = ocr_cfg.get("tesseract", {}) or {}
            if "lang" in t_cfg:
                parser_obj.ocr_tesseract_lang = t_cfg["lang"]
                applied["files.ocr.tesseract.lang"] = True
            if "preprocess" in t_cfg:
                parser_obj.ocr_tesseract_preprocess = bool(t_cfg["preprocess"])
                applied["files.ocr.tesseract.preprocess"] = True
            p_cfg = ocr_cfg.get("paddle", {}) or {}
            if "min_confidence" in p_cfg:
                parser_obj.ocr_paddle_min_confidence = float(p_cfg["min_confidence"])
                applied["files.ocr.paddle.min_confidence"] = True
            if "infer_timeout" in p_cfg:
                parser_obj.ocr_paddle_infer_timeout = int(p_cfg["infer_timeout"])
                applied["files.ocr.paddle.infer_timeout"] = True
            v_cfg = ocr_cfg.get("vision_llm", {}) or {}
            if "enabled" in v_cfg:
                parser_obj.ocr_vision_llm_enabled = bool(v_cfg["enabled"])
                applied["files.ocr.vision_llm.enabled"] = True
            ocr_applied = {k: v for k, v in applied.items() if k.startswith("files.ocr")}
            if ocr_applied:
                logger.info("热更新 files.ocr: %s", ocr_applied)
        except Exception as e:
            logger.warning("热更新 files.ocr 失败: %s", e)

    # LLMClient 精准热重载（核心修复）：
    # orchestrator 注册为 hot_reloadable=False，container.reload 不会重建它，
    # 因此 llm 段变更（API Key / model / provider / base_url）时需在此单独重建
    # LLMClient 并替换到所有引用点。仅当显式传入 old_config 时触发，保持向后兼容。
    if old_config is not None and _llm_config_changed(old_config, new_config):
        if _reload_llm_client(orchestrator, new_config):
            applied["llm.reload"] = True
            logger.info("热更新 llm.reload: LLMClient 已重建并替换所有引用点")
            # 同步更新容器中独立组件持有的 llm_client 引用（etl_engine / parser）。
            # 这些组件在 lifespan 创建时从 orchestrator.llm_client 拿到引用并缓存，
            # 不参与 _reload_llm_client 的批量替换，需在此单独更新。
            # 影响：files 段 llm_fallback 开启时，文件解析的 vision LLM OCR 路径
            # 会用到 llm_client；不更新会导致 API Key 变更后 OCR 仍用旧 Key 报错。
            try:
                from teage_liu.app import get_etl_engine
                etl_engine = _resolve_component(get_etl_engine, "etl_engine")
                if etl_engine is not None:
                    new_llm = getattr(orchestrator, "llm_client", None)
                    if new_llm is not None:
                        if hasattr(etl_engine, "llm_client"):
                            etl_engine.llm_client = new_llm
                        # parser 是 etl_engine 的内部组件，也持有 llm_client
                        parser_obj = getattr(etl_engine, "parser", None)
                        if parser_obj is not None and hasattr(parser_obj, "llm_client"):
                            parser_obj.llm_client = new_llm
                        logger.info("已同步 etl_engine/parser 的 llm_client 引用")
            except Exception as e:
                logger.warning("同步 etl_engine llm_client 引用失败: %s", e)
        else:
            applied["llm.reload"] = False
            logger.warning(
                "llm 段配置变更但 LLMClient 重建失败，仍使用旧实例。"
                "请检查 llm 段配置完整性（main_api_key / main_model 等）"
            )

    return applied
