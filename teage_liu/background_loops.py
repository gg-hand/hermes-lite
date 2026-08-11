"""后台定时清理循环。

从 server.py 提取：cleanup_loop / file_cleanup_loop / metrics_persist_loop。
参数注入：循环通过参数接收组件引用，不再依赖 server 模块全局反射或 state 模块。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("teage_liu.server")

from teage_liu.config import load_config
from teage_liu.monitoring.metrics_store import compute_delta
CONFIG_PATH = os.environ.get("TEAGE_CONFIG", "config.yaml")


async def cleanup_loop(session_logger=None, metrics_store=None, orchestrator=None):
    """定时清理古早会话的后台任务。

    参数注入：通过参数接收组件引用，不再反射 server 模块全局。
    """
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

    # chroma TTL 清理配置（可选，默认 90 天）
    memory_cfg = config.get("memory", {})
    chroma_ttl_days = memory_cfg.get("chroma_ttl_days", 90)

    while True:
        try:
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
                if persist_dir.exists() and session_logger is not None:
                    existing_sessions = {
                        s["id"] for s in session_logger.list_sessions()
                    }
                    for f in persist_dir.glob("*.jsonl"):
                        stem = f.stem
                        candidates = {stem, stem.replace("_", ":")}
                        if not (candidates & existing_sessions):
                            try:
                                f.unlink()
                                logger.info("清理过期 session 历史文件: %s", f.name)
                            except OSError as e:
                                logger.warning("清理历史文件失败 %s: %s", f.name, e)
                    todo_dir = persist_dir / "todo"
                    if todo_dir.exists():
                        for f in todo_dir.glob("*.json"):
                            stem = f.stem
                            candidates = {stem, stem.replace("_", ":")}
                            if not (candidates & existing_sessions):
                                try:
                                    f.unlink()
                                    logger.info("清理过期 session todo 文件: %s", f.name)
                                except OSError as e:
                                    logger.warning("清理 todo 文件失败 %s: %s", f.name, e)
            # chroma 向量库 TTL 清理
            if (
                chroma_ttl_days > 0
                and orchestrator is not None
                and getattr(orchestrator, "chroma_store", None) is not None
            ):
                try:
                    # P2-6 修复：all_collections=True 覆盖所有已创建的 collection
                    deleted_chroma = orchestrator.chroma_store.delete_old_entries(
                        days=chroma_ttl_days, all_collections=True
                    )
                    if deleted_chroma:
                        logger.info(
                            "清理了 %d 条过期 chroma 记忆（超过 %d 天）",
                            deleted_chroma, chroma_ttl_days,
                        )
                except Exception as e:
                    logger.warning("chroma 清理失败: %s", e)
        except Exception as e:
            logger.error("定时清理会话失败: %s", e)
        await asyncio.sleep(interval_hours * 3600)


async def file_cleanup_loop(upload_manager=None) -> None:
    """定时清理过期文件磁盘的后台任务。

    参数注入：通过参数接收 upload_manager，不再反射 server 模块全局。
    """
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


async def metrics_persist_loop(metrics_collector=None, metrics_store=None,
                                reset_event: asyncio.Event = None) -> None:
    """定时将监控指标增量持久化到 SQLite 的后台任务。

    参数注入：通过参数接收组件引用 + asyncio.Event 替代 state.metrics_baseline_reset。
    """
    if metrics_collector is None or metrics_store is None:
        return

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
            logger.debug("metrics_persist_loop: 准备 sleep %.1f 秒 (is_first=%s)",
                         sleep_seconds, is_first_flush)
            await asyncio.sleep(sleep_seconds)
            logger.debug("metrics_persist_loop: sleep 返回，开始执行 flush")

            if reset_event is not None and reset_event.is_set():
                baseline = metrics_collector.snapshot()
                reset_event.clear()
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
            delta_llm = delta.get("llm_calls_total", 0)
            # 有增量数据或首次 flush 才记 INFO，否则降为 DEBUG 避免刷屏
            if delta_llm > 0 or is_first_flush:
                logger.info("metrics flush 完成: date=%s, delta_llm_calls=%d, is_first=%s",
                            target_date, delta_llm, is_first_flush)
            else:
                logger.debug("metrics flush 完成（无增量）: date=%s", target_date)
            is_first_flush = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("监控指标持久化失败: %s", e)
            is_first_flush = False
