"""Director 身份签名验证（ed25519 + 软约束 + 阈值阻断）。

设计原则：
- 全链路异步：verify_director_write 为 async 方法
- 软约束：无签名字段时返回 degraded（兼容未实现签名的 Director）
- 阈值阻断：连续 3 次失败返回 distrust（触发自治模式）
- 失败计数：按 writer_agent_id 维度独立计数，成功后重置
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from teage_liu.multiagent.blackboard import append_audit
from teage_liu.multiagent.exceptions import VerifyResult

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """当前 UTC 时间 ISO 格式。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class SignatureVerifier:
    """Director 签名验证器（ed25519 + 三级 VerifyResult）。

    所有 Director 写操作必须携带 director_signature 字段。
    - 验证通过：重置失败计数，返回 VerifyResult.ok
    - 单次失败：audit + 返回 VerifyResult.degraded（failure_count += 1）
    - 连续 3 次失败：返回 VerifyResult.distrust（触发自治模式）
    - 签名字段缺失：软约束，返回 VerifyResult.degraded（reason=signature_missing，不计数）
    """

    def __init__(self, bb_root: Path, public_key_pem: str | None = None) -> None:
        """初始化签名验证器。

        Args:
            bb_root: 黑板根目录（用于写 audit 记录）
            public_key_pem: Director 公钥 PEM 字符串；为 None 或无效时不验证签名
        """
        self._bb_root = bb_root
        self._public_key: Ed25519PublicKey | None = None
        if public_key_pem:
            try:
                loaded = serialization.load_pem_public_key(public_key_pem.encode())
                if isinstance(loaded, Ed25519PublicKey):
                    self._public_key = loaded
                else:
                    logger.warning(
                        "Director 公钥非 ed25519 类型: %s", type(loaded).__name__
                    )
            except Exception as e:
                logger.warning("加载 Director ed25519 公钥失败: %s", e)
                self._public_key = None
        self._failure_counts: dict[str, int] = {}
        self._threshold = 3

    async def verify_director_write(
        self, status: dict, writer_agent_id: str
    ) -> VerifyResult:
        """验证 Director 写操作的签名。

        Args:
            status: 待验证的 status 字典（含 director_signature 字段时验证签名）
            writer_agent_id: 写入者 agent_id（用于失败计数维度）

        Returns:
            VerifyResult: ok / degraded / distrust
        """
        signature = status.get("director_signature", "")
        if not signature:
            # 无签名字段：软约束（兼容未实现签名的 Director）
            await append_audit(
                self._bb_root,
                {
                    "ts": _now_iso(),
                    "actor": writer_agent_id,
                    "action": "arbitrate",
                    "target": "status.json",
                    "op_id": str(uuid.uuid4()),
                    "epoch": status.get("epoch", 0),
                    "details": {"reason": "signature_missing"},
                    "prev_hash": "",
                    "hash": "",
                    "signature": "",
                },
            )
            return VerifyResult(
                level="degraded",
                reason="signature_missing",
                failure_count=0,
            )

        if not self._public_key:
            # 公钥未加载：视为失败（计数 +1）
            self._failure_counts[writer_agent_id] = (
                self._failure_counts.get(writer_agent_id, 0) + 1
            )
            count = self._failure_counts[writer_agent_id]
            return self._build_failure_result(writer_agent_id, count)

        # 验证签名：移除 director_signature 字段后序列化
        status_copy = {k: v for k, v in status.items() if k != "director_signature"}
        status_str = json.dumps(status_copy, sort_keys=True)
        try:
            self._public_key.verify(
                bytes.fromhex(signature),
                status_str.encode(),
            )
            # 验证通过：重置失败计数
            self._failure_counts.pop(writer_agent_id, None)
            return VerifyResult(level="ok", reason="", failure_count=0)
        except (InvalidSignature, ValueError) as e:
            self._failure_counts[writer_agent_id] = (
                self._failure_counts.get(writer_agent_id, 0) + 1
            )
            count = self._failure_counts[writer_agent_id]
            logger.debug(
                "Director 签名验证失败 (writer=%s, count=%d/%d): %s",
                writer_agent_id,
                count,
                self._threshold,
                e,
            )
            return self._build_failure_result(writer_agent_id, count)

    def _build_failure_result(self, agent_id: str, count: int) -> VerifyResult:
        """构造失败结果（根据计数返回 degraded / distrust）。"""
        if count >= self._threshold:
            return VerifyResult(
                level="distrust",
                reason=f"signature_failed_{count}_times",
                failure_count=count,
            )
        return VerifyResult(
            level="degraded",
            reason=f"signature_failed_count_{count}",
            failure_count=count,
        )

    def reset_failure_count(self, agent_id: str) -> None:
        """重置指定 agent 的失败计数（用于测试或自治模式退出后恢复）。"""
        self._failure_counts.pop(agent_id, None)
