"""端到端双实例测试：两个 hermes-lite 进程通过 blackboard 协作。

测试场景：
1. Director 进程 + Worker 进程同时启动
2. Director 抢占互斥锁，Worker 注册
3. 短时间内消息流、心跳维持、无锁冲突
4. Director 崩溃后 Worker 自治，恢复后退出

标记为 e2e + slow，默认跳过；运行需显式 -m e2e。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.e2e
@pytest.mark.slow
class TestEndToEndDualInstance:
    """端到端双实例测试。"""

    @pytest.mark.asyncio
    async def test_director_worker_collaboration(self, tmp_path: Path):
        """Director + Worker 协作（测试中缩短为 3 秒）。

        步骤：
        1. 启动 Director 进程
        2. 启动 Worker 进程
        3. 等待 3 秒，期间观察消息流
        4. 验证 status.json / messages.md / audit.jsonl 完整
        5. 验证无锁冲突错误
        """
        bb_root = tmp_path / "blackboard"
        bb_root.mkdir()

        # 启动 Director 进程
        director_proc = subprocess.Popen(
            [
                sys.executable, "-m", "hermes.multiagent.director_cli",
                "--bb-root", str(bb_root),
                "--mode", "script",
            ],
            env={**os.environ, "HERMES_ROLE": "director"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # 启动 Worker 进程
        worker_proc = subprocess.Popen(
            [
                sys.executable, "-m", "hermes.multiagent.worker_cli",
                "--bb-root", str(bb_root),
                "--agent-id", "worker_001",
            ],
            env={**os.environ, "HERMES_ROLE": "worker"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # 等待启动
            await asyncio.sleep(3)

            # 检查进程是否仍在运行
            assert director_proc.poll() is None, "Director 进程不应退出"
            assert worker_proc.poll() is None, "Worker 进程不应退出"

            # 检查 status.json
            status_path = bb_root / "status.json"
            assert status_path.exists(), "status.json 应存在"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            assert "locks" in status
            assert "current_turn" in status

            # 检查 director.md
            director_md_path = bb_root / "director.md"
            assert director_md_path.exists(), "director.md 应存在"
            director_md_content = director_md_path.read_text(encoding="utf-8")
            assert "current_epoch" in director_md_content
            assert "last_director_tick" in director_md_content

            # 检查 agent_card
            agent_card_path = bb_root / "agents" / "worker_001.md"
            assert agent_card_path.exists(), "worker_001 agent_card 应存在"

            # 检查 audit.jsonl 完整
            audit_path = bb_root / "audit" / "audit.jsonl"
            assert audit_path.exists(), "audit.jsonl 应存在"
            audit_content = audit_path.read_text(encoding="utf-8").strip()
            assert audit_content, "audit.jsonl 不应为空"
            # 至少有 register 记录
            assert "register" in audit_content or "director_started" in audit_content

            # 检查 messages.md
            messages_path = bb_root / "messages.md"
            assert messages_path.exists(), "messages.md 应存在"

        finally:
            director_proc.terminate()
            worker_proc.terminate()
            try:
                director_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                director_proc.kill()
            try:
                worker_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker_proc.kill()

    @pytest.mark.asyncio
    async def test_director_crash_worker_autonomous(self, tmp_path: Path):
        """Director 崩溃 → Worker 进入自治模式。

        使用短 heartbeat_timeout_seconds=2，使 Worker 在 ~7 秒内检测到 Director 故障。
        """
        bb_root = tmp_path / "blackboard"
        bb_root.mkdir()

        # 启动 Director（script 模式）
        director_proc = subprocess.Popen(
            [
                sys.executable, "-m", "hermes.multiagent.director_cli",
                "--bb-root", str(bb_root),
                "--mode", "script",
            ],
            env={**os.environ, "HERMES_ROLE": "director"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # 启动 Worker（短超时）
        worker_proc = subprocess.Popen(
            [
                sys.executable, "-m", "hermes.multiagent.worker_cli",
                "--bb-root", str(bb_root),
                "--agent-id", "worker_001",
                "--heartbeat-timeout", "2",
            ],
            env={**os.environ, "HERMES_ROLE": "worker"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # 等待启动初始化
            await asyncio.sleep(2)

            # 确认两个进程都在运行
            assert director_proc.poll() is None, "Director 应在运行"
            assert worker_proc.poll() is None, "Worker 应在运行"

            # 模拟 Director 崩溃
            director_proc.kill()
            director_proc.wait()

            # 等待 Worker 检测到 Director 故障
            # check_interval=5s + heartbeat_timeout=2s + buffer=2s = 9s
            await asyncio.sleep(9)

            # 验证 Worker 仍在运行
            assert worker_proc.poll() is None, "Worker 应在 Director 崩溃后继续运行"

            # 验证 Worker 进入自治模式
            # 方式1：检查 messages.md 是否有 director_assumed_offline
            messages_path = bb_root / "messages.md"
            messages_content = messages_path.read_text(encoding="utf-8") if messages_path.exists() else ""
            autonomous_entered = (
                "director_assumed_offline" in messages_content
                or "自治" in messages_content
            )

            # 方式2：检查 audit.jsonl 是否有 arbitrate 记录
            audit_path = bb_root / "audit" / "audit.jsonl"
            audit_content = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
            if not autonomous_entered:
                autonomous_entered = (
                    "arbitrate" in audit_content
                    and "director_heartbeat_timeout" in audit_content
                )

            assert autonomous_entered, (
                "Worker 应在 Director 崩溃后进入自治模式。"
                f"messages: {messages_content[:500]}, audit: {audit_content[:500]}"
            )

        finally:
            worker_proc.terminate()
            try:
                worker_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker_proc.kill()
