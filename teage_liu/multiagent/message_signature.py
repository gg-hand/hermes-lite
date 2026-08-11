"""消息级 ed25519 签名校验。

与 SignatureVerifier（Director 专用）的区别：
- 验证任意 agent 的消息签名，不仅限 Director
- 公钥从 agents/keys/{agent_id}.pem 加载
- 不维护失败计数（消息级无状态，失败即拒绝）
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)


def sign_message(message: dict, private_key: Ed25519PrivateKey) -> str:
    """对消息签名（ed25519）。

    Args:
        message: 待签名消息（dict，去除 signature 字段后序列化）
        private_key: ed25519 私钥

    Returns:
        签名 hex 字符串
    """
    # 移除 signature 字段（如果存在），保证签名内容稳定
    msg_copy = {k: v for k, v in message.items() if k != "signature"}
    canonical = json.dumps(msg_copy, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return private_key.sign(canonical).hex()


def load_public_keys_from_dir(bb_root: Path) -> dict[str, Ed25519PublicKey]:
    """从 agents/keys/ 目录加载所有 agent 的公钥。

    Args:
        bb_root: 黑板根目录

    Returns:
        {agent_id: Ed25519PublicKey} 字典
    """
    keys: dict[str, Ed25519PublicKey] = {}
    keys_dir = bb_root / "agents" / "keys"
    if not keys_dir.exists():
        return keys

    for pem_file in keys_dir.glob("*.pem"):
        agent_id = pem_file.stem  # 文件名（不含 .pem）
        try:
            with open(pem_file, "rb") as f:
                loaded = serialization.load_pem_public_key(f.read())
            if isinstance(loaded, Ed25519PublicKey):
                keys[agent_id] = loaded
            else:
                logger.warning("公钥 %s 非 ed25519 类型", pem_file)
        except Exception as e:
            logger.warning("加载公钥 %s 失败: %s", pem_file, e)
    return keys


class MessageSignatureVerifier:
    """消息级签名校验器。

    公钥从 {bb_root}/agents/keys/{agent_id}.pem 加载。
    采用 TTL 缓存（默认 5 秒）避免高频验签时频繁磁盘 IO，
    同时支持动态注册（TTL 过期后自动 reload）。
    """

    _RELOAD_TTL_SECONDS: float = 5.0

    def __init__(self, bb_root: Path) -> None:
        self._bb_root = bb_root
        self._public_keys: dict[str, Ed25519PublicKey] = {}
        self._last_reload_ts: float = 0.0
        self._reload_if_stale()

    def _reload_if_stale(self) -> None:
        """TTL 过期则重新加载公钥目录，否则复用缓存。"""
        now = time.monotonic()
        if now - self._last_reload_ts < self._RELOAD_TTL_SECONDS:
            return
        self._public_keys = load_public_keys_from_dir(self._bb_root)
        self._last_reload_ts = now

    def verify_message(
        self, message: dict, signature: str, signer_id: str
    ) -> bool:
        """验证消息签名。

        Args:
            message: 原始消息 dict
            signature: 签名 hex 字符串
            signer_id: 声明的签名者 agent_id

        Returns:
            True 表示验签通过；False 表示验签失败（公钥缺失/签名无效）
        """
        if not signature or not signer_id:
            return False

        # TTL 缓存 reload（支持动态注册，避免高频 IO）
        self._reload_if_stale()
        public_key = self._public_keys.get(signer_id)
        if public_key is None:
            logger.debug("公钥未找到: agent_id=%s", signer_id)
            return False

        # 移除 signature 字段后序列化（与 sign_message 保持一致）
        msg_copy = {k: v for k, v in message.items() if k != "signature"}
        canonical = json.dumps(msg_copy, sort_keys=True, ensure_ascii=False).encode("utf-8")

        try:
            public_key.verify(bytes.fromhex(signature), canonical)
            return True
        except (InvalidSignature, ValueError) as e:
            logger.debug("签名验证失败 (signer=%s): %s", signer_id, e)
            return False

    def has_public_key(self, agent_id: str) -> bool:
        """检查指定 agent 是否注册了公钥。"""
        self._reload_if_stale()
        return agent_id in self._public_keys
