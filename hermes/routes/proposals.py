"""proposals 路由：提议列表、详情、确认、修改、拒绝。

Task: 从 server.py 迁移 5 个端点。
- GET  /proposals
- GET  /proposals/{proposal_id}
- POST /proposals/{proposal_id}/confirm
- POST /proposals/{proposal_id}/modify
- POST /proposals/{proposal_id}/reject
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from hermes.app import get_proposal_store, get_orchestrator
from hermes.schemas.proposals import ProposalModifyRequest

logger = logging.getLogger("hermes.server")

router = APIRouter()


@router.get("/proposals")
def list_proposals(proposal_store=Depends(get_proposal_store)):
    """列出所有提议（按创建顺序）。

    Phase 8 Task 3.8。返回 ``proposal_store`` 中所有提议的 dict 列表，
    含 proposal_id / schedule_config / requested_tools / llm_explanation /
    status / created_at / schedule_id 字段。

    proposal_store 未初始化时返回 503。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")
    proposals = proposal_store.list()
    return {"proposals": [p.to_dict() for p in proposals], "total": len(proposals)}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str,
                 proposal_store=Depends(get_proposal_store)):
    """获取指定提议详情。

    Phase 8 Task 3.8。提议不存在时返回 404。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")
    proposal = proposal_store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")
    return proposal.to_dict()


@router.post("/proposals/{proposal_id}/confirm")
def confirm_proposal(proposal_id: str,
                     proposal_store=Depends(get_proposal_store),
                     orchestrator=Depends(get_orchestrator)):
    """确认提议：``pending_confirm → confirmed``，随后创建调度项。

    Phase 8 Task 3.8。流程：
    1. 调 ``proposal_store.confirm`` 将状态转为 ``confirmed``。
    2. 调用 ``create_schedule`` 工具逻辑（通过 tool_registry.execute_tool）
       创建调度项，锁定 active_tools_snapshot。
    3. 返回创建结果（含 schedule_id）。

    提议不存在或状态不允许转换时返回 404；调度项创建失败时返回 500。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    proposal = proposal_store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")

    # 1. 确认提议（pending_confirm → confirmed）
    # 幂等处理：若 proposal 已是 confirmed/modified（如之前创建调度失败留下的
    # 中间态），跳过状态转换直接创建调度项，避免用户被 409 卡住无法重试。
    if proposal.status == "pending_confirm":
        ok = proposal_store.confirm(proposal_id)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=f"提议 {proposal_id} 当前状态为 {proposal.status}，无法确认",
            )
    elif proposal.status not in ("confirmed", "modified"):
        raise HTTPException(
            status_code=409,
            detail=f"提议 {proposal_id} 当前状态为 {proposal.status}，无法确认",
        )

    # 2. 调用 create_schedule 工具创建调度项
    # cron_create 是 Deferred Tier 工具，execute_tool 不查找 _deferred_tools，
    # 需用 get_handler 直接获取 handler 调用（后端 API 非 LLM 工具调用流程）
    handler = orchestrator.tool_registry.get_handler("cron_create")
    if handler is None:
        raise HTTPException(status_code=500, detail="cron_create 工具未注册")
    try:
        result_str = handler(proposal_id=proposal_id)
    except Exception as e:
        logger.exception("confirm_proposal 调用 cron_create 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"创建调度项失败: {e}")
    # create_schedule 返回 JSON 字符串，解析判断成功/失败
    try:
        result = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        result = {"raw": result_str}

    if "schedule_id" not in result:
        # 创建失败，返回错误详情
        raise HTTPException(
            status_code=500,
            detail=f"创建调度项失败: {result.get('raw', result_str)}",
        )
    logger.info(
        "提议 %s 已确认并创建调度项 %s", proposal_id, result["schedule_id"]
    )
    return {
        "status": "schedule_active",
        "proposal_id": proposal_id,
        "schedule_id": result["schedule_id"],
        "active_tools_snapshot": result.get("active_tools_snapshot", []),
        "message": "提议已确认，调度项已创建",
    }


@router.post("/proposals/{proposal_id}/modify")
def modify_proposal(proposal_id: str,
                    req: ProposalModifyRequest,
                    proposal_store=Depends(get_proposal_store),
                    orchestrator=Depends(get_orchestrator)):
    """修改并确认提议：``pending_confirm → modified``，随后创建调度项。

    Phase 8 Task 3.8。流程：
    1. 调 ``proposal_store.modify`` 应用修改并将状态转为 ``modified``。
    2. 调用 ``create_schedule`` 工具逻辑创建调度项（用修改后的配置）。
    3. 返回创建结果。

    提议不存在或状态不允许转换时返回 404。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")
    if orchestrator is None or orchestrator.tool_registry is None:
        raise HTTPException(status_code=503, detail="ToolRegistry 尚未初始化")

    proposal = proposal_store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")

    # 1. 修改并确认（pending_confirm → modified）
    ok = proposal_store.modify(
        proposal_id,
        schedule_config_updates=req.schedule_config_updates,
        requested_tools=req.requested_tools,
    )
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"提议 {proposal_id} 当前状态为 {proposal.status}，无法修改",
        )

    # 2. 调用 create_schedule 工具创建调度项（用修改后的配置）
    # cron_create 是 Deferred Tier 工具，需用 get_handler 直接调用
    handler = orchestrator.tool_registry.get_handler("cron_create")
    if handler is None:
        raise HTTPException(status_code=500, detail="cron_create 工具未注册")
    try:
        result_str = handler(proposal_id=proposal_id)
    except Exception as e:
        logger.exception("modify_proposal 调用 cron_create 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"创建调度项失败: {e}")
    try:
        result = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        result = {"raw": result_str}

    if "schedule_id" not in result:
        raise HTTPException(
            status_code=500,
            detail=f"创建调度项失败: {result.get('raw', result_str)}",
        )
    logger.info(
        "提议 %s 已修改并创建调度项 %s", proposal_id, result["schedule_id"]
    )
    return {
        "status": "schedule_active",
        "proposal_id": proposal_id,
        "schedule_id": result["schedule_id"],
        "active_tools_snapshot": result.get("active_tools_snapshot", []),
        "message": "提议已修改并确认，调度项已创建",
    }


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: str,
                    proposal_store=Depends(get_proposal_store)):
    """拒绝提议：``pending_confirm → rejected``，不创建调度项。

    Phase 8 Task 3.8。提议不存在或状态不允许转换时返回 404/409。
    """
    if proposal_store is None:
        raise HTTPException(status_code=503, detail="ProposalStore 尚未初始化")

    proposal = proposal_store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提议 {proposal_id} 不存在")

    ok = proposal_store.reject(proposal_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"提议 {proposal_id} 当前状态为 {proposal.status}，无法拒绝",
        )
    logger.info("提议 %s 已被拒绝", proposal_id)
    return {
        "status": "rejected",
        "proposal_id": proposal_id,
        "message": "提议已拒绝，不会创建调度项",
    }
