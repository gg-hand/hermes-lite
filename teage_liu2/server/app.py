"""外壳:FastAPI 应用装配(只依赖 core,不感知 branches)。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..core.config import load_config
from ..core.history import SQLiteHistoryStore
from ..core.hooks import HookChain
from ..core.llm import LLMClient
from ..core.pipeline import ChatPipeline, MODE_LOOP
from .routes import router

logger = logging.getLogger(__name__)


def create_app(
    config_path: str = "config.yaml",
    config: Optional[Dict[str, Any]] = None,
) -> FastAPI:
    """装配应用。

    主干契约:配置开了但初始化失败(LLM API Key 缺失 / 存储打不开)→ 抛错,
    启动失败并给出可读错误 —— 不静默降级。
    """
    cfg = config if config is not None else load_config(config_path)
    config_path = cfg.get("_config_path", config_path)

    # 1. LLM 客户端(失败抛错 = 启动失败)
    llm_client = LLMClient(config_path=config_path, config=cfg)

    # 2. 历史存储(SQLite 默认;M1 独立库文件,避免与老库格式冲突)
    storage_cfg = cfg.get("storage", {}) or {}
    sqlite_path = storage_cfg.get("sqlite_path", "data2/sessions.db")
    history_store = SQLiteHistoryStore(sqlite_path)

    # 3. 钩子链:M1 零枝干(枝干由配置注册,M2 起)
    hooks = HookChain()

    # 4. 主干
    core_cfg = cfg.get("core", {}) or {}
    pipeline = ChatPipeline(
        llm_client=llm_client,
        history_store=history_store,
        hooks=hooks,
        mode=core_cfg.get("mode", MODE_LOOP),
        max_loops=int(core_cfg.get("max_loops", 50)),
        base_system_prompt=core_cfg.get("system_prompt"),
    )

    # 5. FastAPI 实例
    app = FastAPI(
        title="Teage Liu 2",
        description="主干-枝干架构的个人 AI Agent(M1 纯对话内核)",
        version="0.1.0",
    )
    cors_origins = (cfg.get("server", {}) or {}).get(
        "cors_origins", ["http://localhost:3000"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if isinstance(cors_origins, list) else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 6. 挂载路由 + 状态
    app.include_router(router)
    app.state.pipeline = pipeline
    app.state.llm_client = llm_client
    app.state.history_store = history_store

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        try:
            llm_client.close()
        except Exception as e:
            logger.warning("关闭 LLM 客户端失败: %s", e)
        try:
            history_store.close()
        except Exception as e:
            logger.warning("关闭历史存储失败: %s", e)

    return app
