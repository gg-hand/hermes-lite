"""Agent Card 构建与发布（/.well-known/agent-card.json）。

URL 推导优先级：endpoint_url 配置 > base_url 配置 > 开发态从 TEAGE_HOST/PORT 推导。
securitySchemes：security.api_key 非空时自动声明 bearer。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi.responses import JSONResponse

from teage_liu.multiagent.a2a_std.models import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    SecurityScheme,
)

logger = logging.getLogger(__name__)

_DEFAULT_JSONRPC_PATH = "/a2a/std/jsonrpc"


class AgentCardBuilder:
    """Agent Card 构建器。"""

    def __init__(self, config: dict, bb_root: Path) -> None:
        self._config = config
        self._bb_root = bb_root
        a2a_cfg = config.get("a2a", {}) or {}
        self._std_cfg = a2a_cfg.get("standard", {}) or {}

    def build(self) -> dict:
        """构建 camelCase Agent Card dict。"""
        std = self._std_cfg
        multiagent_cfg = self._config.get("multiagent", {}) or {}
        worker_cfg = multiagent_cfg.get("worker", {}) or {}
        agent_id = worker_cfg.get("agent_id", "worker_001")

        capabilities_cfg = std.get("capabilities", {}) or {}
        capabilities = AgentCapabilities(
            streaming=bool(capabilities_cfg.get("streaming", True)),
            push_notifications=bool(capabilities_cfg.get("push_notifications", False)),
        )

        skills = [
            AgentSkill.model_validate(s) for s in (std.get("skills") or [])
            if isinstance(s, dict) and s.get("id") and s.get("name")
        ]
        # 合规工具（@a2a-compliance/cli）要求 skills 至少 1 项；未配置时给默认技能
        if not skills:
            skills = [AgentSkill(
                id="chat",
                name="Chat",
                description="Conversational interaction with the agent",
                tags=["chat"],
            )]

        security_schemes: list[SecurityScheme] = []
        configured = std.get("security_schemes") or []
        if configured:
            for s in configured:
                if isinstance(s, dict) and s.get("scheme"):
                    security_schemes.append(SecurityScheme.model_validate(s))
        else:
            # 自动：security.api_key 非空 → bearer
            sec_cfg = self._config.get("security", {}) or {}
            if sec_cfg.get("api_key"):
                security_schemes.append(SecurityScheme(scheme="bearer"))

        card = AgentCard(
            protocol_version="1.0",
            name=str(std.get("name") or agent_id),
            description=str(std.get("description") or "Teage Liu personal AI agent"),
            url=self.endpoint_url(),
            version=str(std.get("version") or "0.1.0"),
            preferred_transport="JSONRPC",
            capabilities=capabilities,
            default_input_modes=std.get("default_input_modes") or ["text", "text/plain"],
            default_output_modes=std.get("default_output_modes") or ["text", "text/plain"],
            skills=skills,
            security_schemes=security_schemes,
            extensions=list(std.get("extensions") or []),
            supports_authenticated_extended_card=False,
            supported_interfaces=[AgentInterface(
                url=self.endpoint_url(),
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )],
        )
        card_dict = card.model_dump(by_alias=True, exclude_none=True)
        # securitySchemes 线格式：官方 SDK(v1.0 protobuf) 用 map（scheme名 → scheme），
        # 而非规范早期 JSON schema 的 list——以官方 v1.0 实现为准；
        # 空时省略字段（map 空值对解析器不友好）。
        if card.security_schemes:
            schemes_map = {}
            for s in card.security_schemes:
                entry = {"scheme": s.scheme}
                if s.description:
                    entry["description"] = s.description
                schemes_map[s.scheme] = entry
            card_dict["securitySchemes"] = schemes_map
        else:
            card_dict.pop("securitySchemes", None)
        return card_dict

    def endpoint_url(self) -> str:
        """标准 JSON-RPC 端点 URL（Agent Card url 字段）。"""
        std = self._std_cfg
        endpoint = str(std.get("endpoint_url") or "").strip()
        if endpoint:
            return endpoint.rstrip("/")
        base = str(std.get("base_url") or "").strip()
        if not base:
            base = self._derive_base_url()
        return base.rstrip("/") + _DEFAULT_JSONRPC_PATH

    def _derive_base_url(self) -> str:
        """开发态推导：http://{host}:{port}。"""
        import os
        host = os.environ.get("TEAGE_HOST") or "localhost"
        if host in ("0.0.0.0", "::"):
            host = "localhost"
        port = os.environ.get("TEAGE_PORT") or (
            self._config.get("server", {}) or {}
        ).get("port", 8000)
        return f"http://{host}:{port}"

    def card_route(self) -> JSONResponse:
        """发布 Agent Card 的 JSON 响应。"""
        return JSONResponse(
            status_code=200,
            content=self.build(),
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
            },
        )
