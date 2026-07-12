"""后台定时清理循环。

从 server.py 提取：cleanup_loop / file_cleanup_loop / metrics_persist_loop。
通过 sys.modules 访问 server 模块的全局组件变量，兼容 src.server 和 server 两种导入路径。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("hermes.server")

try:
    from config import load_config
except ImportError:
    try:
        from .config import load_config  # type: ignore
    except ImportError:
        load_config = None  # type: ignore

try:
    from monitoring.metrics_store import compute_delta
except ImportError:
    try:
        from .monitoring.metrics_store import compute_delta  # type: ignore
    except ImportError:
        compute_delta = None  # type: ignore

CONFIG_PATH = os.environ.get("HERMES_CONFIG", "config.yaml")


def _get_server_globals():
    """获取 server 模块的全局组件变量。

    兼容 src.server 和 server 两种导入路径。
    """
    server_mod = sys.modules.get("src.server") or sys.modules.get("server")
    if server_mod is None:
        return {}
    return {
        "orchestrator": getattr(server_mod, "orchestrator", None),
        "session_logger": getattr(server_mod, "session_logger", None),
        "metrics_collector": getattr(server_mod, "metrics_collector", None),
        "metrics_store": getattr(server_mod, "metrics_store", None),
        "upload_manager": getattr(server_mod, "upload_manager", None),
    }


async def cleanup_loop():
    """定时清理古早会话的后台任务。"""
    try:
        config = load_config(CONFIG_PATH)
    except Exception as e:
        logger.warning("cleanup_loop 读取配置失败，跳过清理: %s", e)
        return
    storage_cfg = config.get("storage", {})
    ttl_days = storage_cfg.get("session_ttl_days")
    interval_hours = storage_cfg.get("cleanup_interval_hours", 24)
    if not ttl_days:
        return

    while True:
        try:
            sg = _get_server_globals()
            session_logger = sg["session_logger"]
            metrics_store = sg["metrics_store"]
            orchestrator = sg["orchestrator"]

            if session_logger is not None:
                deleted = session_logger.delete_old_sessions(ttl_days)
                if deleted:
                    logger.info(
                        "定时清理完成: 删除了 %d 个旧会话（超过 %d 天）",
                        deleted, ttl_days,
                    )
            if metrics_store is not None:
                deleted_metrics = metrics_store.delete_old_metrics(ttl_days)
                if deleted_metrics:
                    logger.info("清理了 %d 条过期监控历史记录", deleted_metrics)
            if (
                orchestrator is not None
                and getattr(orchestrator, "history_buffer", None) is not None
                and orchestrator.history_buffer.persistence_dir
            ):
                persist_dir = Path(orchestrator.history_buffer.persistence_dir)
                if persist_dir.exists():
                    existing_sessions = {
                        s["id"] for s in session_logger.list_sessions()
                    }
                    for f in persist_dir.glob("*.jsonl"):
                        stem = f.stem
                        candidates = {stem, stem.replace("_", ":")}
                        if not (candidates & existing_sessions):
                            try:
                                f.unlink()
                                logger.info(
                                    "清理过期 session 历史文件: %s", f.name
                                )
                            except OSError as e:
                                logger.warning(
                                    "清理历史文件失败 %s: %s", f.name, e
                                )
                    todo_dir = persist_dir / "todo"
                    if todo_dir.exists():
                        for f in todo_dir.glob("*.json"):
                            stem = f.stem
                            candidates = {stem, stem.replace("_", ":")}
                            if not (candidates & existing_sessions):
                                try:
                                    f.unlink()
                                    logger.info(
                                        "清理过期 session todo 文件: %s",
                                        f.name,
                                    )
                                except OSError as e:
                                    logger.warning(
                                        "清理 todo 文件失败 %s: %s",
                                        f.name, e,
                                    )
        except Exception as e:
            logger.error("定时清理会话失败: %s", e)
        await asyncio.sleep(interval_hours * 3600)


async def file_cleanup_loop() -> None:
    """定时清理过期文件磁盘的后台任务。"""
    try:
        config = load_config(CONFIG_PATH)
    except Exception as e:
        logger.warning("file_cleanup_loop 读取配置失败，跳过清理: %s", e)
        return

    storage_cfg = config.get("storage", {})
    files_cfg = config.get("files", {})
    ttl_days = storage_cfg.get("session_ttl_days")
    interval_hours = files_cfg.get("cleanup_interval_hours", 24)
    if not ttl_days:
        return

    while True:
        try:
            sg = _get_server_globals()
            upload_manager = sg["upload_manager"]

            if upload_manager is not None:
                expired_ids = upload_manager.get_expired(ttl_days)
                for file_id in expired_ids:
                    try:
                        meta = upload_manager.get_metadata(file_id)
                        if meta is None:
                            continue
                        if meta.get("etl_status") == "processing":
                            continue
                        saved_path = meta.get("saved_path")
                        if saved_path:
                            try:
                                os.remove(saved_path)
                                logger.info("清理过期磁盘文件: %s", saved_path)
                            except OSError as e:
                                logger.warning("清理磁盘文件失败: %s -> %s", saved_path, e)
                        cache_dir = files_cfg.get("upload_dir", "data/uploads")
                        cache_path = os.path.join(cache_dir, f"{file_id}.parsed")
                        try:
                            os.remove(cache_path)
                        except OSError:
                            pass
                        upload_manager.mark_disk_expired(file_id)
                    except Exception as e:
                        logger.warning("清理文件 %s 失败: %s", file_id, e)
                if expired_ids:
                    logger.info("文件清理完成: 清理了 %d 个过期文件", len(expired_ids))
        except Exception as e:
            logger.error("定时清理文件失败: %s", e)
        await asyncio.sleep(interval_hours * 3600)


async def metrics_persist_loop() -> None:
    """定时将监控指标增量持久化到 SQLite 的后台任务。"""
    sg = _get_server_globals()
    metrics_collector = sg["metrics_collector"]
    metrics_store = sg["metrics_store"]

    if metrics_collector is None or metrics_store is None:
        return

    server_mod = sys.modules.get("src.server") or sys.modules.get("server")

    INITIAL_FLUSH_DELAY = 10

    baseline = metrics_collector.snapshot()
    current_date = datetime.now().date()
    is_first_flush = True

    while True:
        try:
            try:
                config = load_config(CONFIG_PATH)
                monitoring_cfg = config.get("monitoring", {})
                flush_interval = int(monitoring_cfg.get("flush_interval_minutes", 60))
            except Exception as e:
                logger.warning("metrics_persist_loop 读取配置失败，使用默认 60min: %s", e)
                flush_interval = 60
            if flush_interval <= 0:
                flush_interval = 60

            now = datetime.now()
            if is_first_flush:
                sleep_seconds = INITIAL_FLUSH_DELAY
            else:
                next_flush = now + timedelta(minutes=flush_interval)
                next_midnight = datetime(now.year, now.month, now.day) + timedelta(days=1)
                sleep_until = min(next_flush, next_midnight)
                sleep_seconds = (sleep_until - now).total_seconds()
            logger.info("metrics_persist_loop: 准备 sleep %.1f 秒 (is_first=%s)", sleep_seconds, is_first_flush)
            await asyncio.sleep(sleep_seconds)
            logger.info("metrics_persist_loop: sleep 返回，开始执行 flush")

            import state as _state_check
            if _state_check.metrics_baseline_reset:
                baseline = metrics_collector.snapshot()
                _state_check.metrics_baseline_reset = False
                if server_mod is not None:
                    server_mod._metrics_baseline_reset = False
                logger.info("metrics baseline 已重置（reset 接口触发）")

            current_snap = metrics_collector.snapshot()
            delta = compute_delta(current_snap, baseline)
            today = datetime.now().date()

            if today != current_date:
                target_date = current_date.isoformat()
                current_date = today
            else:
                target_date = current_date.isoformat()

            await asyncio.to_thread(metrics_store.upsert_daily, target_date, delta)
            baseline = current_snap
            logger.info("metrics flush 完成: date=%s, delta_llm_calls=%d, is_first=%s",
                        target_date, delta.get("llm_calls_total", 0), is_first_flush)
            is_first_flush = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("监控指标持久化失败: %s", e)
            is_first_flush = False
