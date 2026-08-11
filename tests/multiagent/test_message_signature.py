"""消息级 ed25519 签名校验测试。"""
from __future__ import annotations

import pytest
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from teage_liu.multiagent.message_signature import (
    MessageSignatureVerifier,
    sign_message,
    load_public_keys_from_dir,
)


@pytest.fixture
def key_pair():
    """生成 ed25519 密钥对。"""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture
def verifier(tmp_path: Path, key_pair):
    """带公钥目录的签名校验器。"""
    _, public_key = key_pair
    # 写入公钥到 agents/keys/
    keys_dir = tmp_path / "agents" / "keys"
    keys_dir.mkdir(parents=True)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (keys_dir / "agent_alice.pem").write_bytes(pem)
    return MessageSignatureVerifier(tmp_path)


def test_sign_and_verify_roundtrip(verifier, key_pair):
    """签名后验签通过。"""
    private_key, _ = key_pair
    message = {"from": "agent_alice", "type": "chat", "content": "hello", "seq": 1}
    signature = sign_message(message, private_key)
    assert verifier.verify_message(message, signature, "agent_alice") is True


def test_verify_rejects_tampered_message(verifier, key_pair):
    """篡改消息后验签失败。"""
    private_key, _ = key_pair
    message = {"from": "agent_alice", "type": "chat", "content": "hello", "seq": 1}
    signature = sign_message(message, private_key)
    tampered = {**message, "content": "hacked"}
    assert verifier.verify_message(tampered, signature, "agent_alice") is False


def test_verify_rejects_missing_public_key(verifier, key_pair):
    """signer_id 无对应公钥时验签失败。"""
    private_key, _ = key_pair
    message = {"from": "agent_bob", "type": "chat", "content": "hello", "seq": 1}
    signature = sign_message(message, private_key)
    assert verifier.verify_message(message, signature, "agent_bob") is False


def test_verify_rejects_forged_signer(verifier, key_pair):
    """冒充其他 agent 的签名被拒绝。"""
    private_key, _ = key_pair
    # 用 alice 的私钥签名，但声明 signer_id 为 bob
    message = {"from": "agent_bob", "type": "chat", "content": "hello", "seq": 1}
    signature = sign_message(message, private_key)
    assert verifier.verify_message(message, signature, "agent_bob") is False


def test_load_public_keys_from_dir(tmp_path: Path, key_pair):
    """从目录加载公钥字典。"""
    _, public_key = key_pair
    keys_dir = tmp_path / "agents" / "keys"
    keys_dir.mkdir(parents=True)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (keys_dir / "agent_alice.pem").write_bytes(pem)
    keys = load_public_keys_from_dir(tmp_path)
    assert "agent_alice" in keys
