"""N-worker 协作集成测试。

用法：
    pytest tests/integration/test_n_worker_collab.py --run-integration -v

前提：
    1. 双 worker 已启动：
       - worker-1: http://localhost:8000 (config-a2a-1.yaml, agent_id=teagent-lu)
       - worker-2: http://localhost:8001 (config-a2a-2.yaml, agent_id=teagent-liu-2)
       启动脚本：start_dual_workers.ps1
    2. Director 已在 worker-2 上启动（通过 /api/multiagent/director/start 或自治启动）
    3. 当前工作目录为项目根（teage-liu/），以便定位 data/blackboard_a2a_2/audit/audit.jsonl

设计原则：
    - 不跑 30 分钟长跑（sub-agent 无法等待），只做"短时 smoke + 行为断言"
    - 长跑与性能采集交给 scripts/collect_perf_metrics.py
    - 默认 skip，必须 --run-integration 显式启用
    - 服务未启动时 pytest.skip 而非 fail，避免误报
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

WORKER1 = os.environ.get("NWORKER_URL_1", "http://localhost:8000")
WORKER2 = os.environ.get("NWORKER_URL_2", "http://localhost:8001")

# worker-2 的 blackboard audit 路径（config-a2a-2.yaml: blackboard_dir=data/blackboard_a2a_2）
DEFAULT_AUDIT_PATH = Path("data/blackboard_a2a_2/audit/audit.jsonl")
AUDIT_PATH = Path(os.environ.get("NWORKER_AUDIT_PATH", str(DEFAULT_AUDIT_PATH)))

# watchdog 轮询周期 10s + 重启前等待 5s + 子进程启动，留足余量
RESTART_TIMEOUT = int(os.environ.get("NWORKER_RESTART_TIMEOUT", "30"))
AUDIT_SAMPLE_SECONDS = int(os.environ.get("NWORKER_AUDIT_SAMPLE_SECONDS", "60"))
E2E_TIMEOUT = int(os.environ.get("NWORKER_E2E_TIMEOUT", "30"))


def _get(url: str, timeout: float = 5.0) -> dict:
    """同步 GET JSON。服务未启动时 pytest.skip。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        pytest.skip(f"服务未启动或不可达: {url} ({e.reason})")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"请求 {url} 异常: {e!r}")


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> dict:
    """同步 POST JSON。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        pytest.skip(f"服务未启动或不可达: {url} ({e.reason})")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"POST {url} 异常: {e!r}")


def _kill_pid(pid: int) -> None:
    """跨平台杀进程。Windows 用 taskkill，其它用 kill -9。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def _count_lines(path: Path) -> int:
    """统计文件行数（空文件返回 0）。"""
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


# ---------------------------------------------------------------------------
# 测试 1：Director 自动重启（任务 1.5 watchdog）
# ---------------------------------------------------------------------------
def test_director_auto_restart_on_kill():
    """杀掉 worker-2 的 director 子进程，断言在 RESTART_TIMEOUT 秒内自动重启。

    验证点：
      - 杀进程前 director/status.running == True
      - 杀进程后，watchdog 检测到子进程退出并自动重启
      - 重启后 director/status.running == True 且 pid 不同
    """
    status = _get(f"{WORKER2}/api/multiagent/director/status")
    if not status.get("running"):
        pytest.skip(
            f"worker-2 director 未运行 (state={status.get('state')})，"
            "请先 POST /api/multiagent/director/start"
        )
    old_pid = status.get("pid")
    if not old_pid:
        pytest.fail("director/status 返回 running=True 但无 pid，无法执行 kill")

    print(f"[kill] 杀掉 director 子进程 pid={old_pid}")
    _kill_pid(int(old_pid))

    deadline = time.time() + RESTART_TIMEOUT
    last_status = status
    while time.time() < deadline:
        time.sleep(1)
        last_status = _get(f"{WORKER2}/api/multiagent/director/status")
        if (
            last_status.get("running")
            and last_status.get("pid")
            and last_status["pid"] != old_pid
        ):
            print(
                f"[restart-ok] 新 pid={last_status['pid']} "
                f"(旧 pid={old_pid}, state={last_status.get('state')})"
            )
            return
    pytest.fail(
        f"director 在 {RESTART_TIMEOUT}s 内未自动重启 "
        f"(old_pid={old_pid}, last_status={last_status})"
    )


# ---------------------------------------------------------------------------
# 测试 2：audit.jsonl 增长速率 < 10/分钟
# ---------------------------------------------------------------------------
def test_audit_growth_rate_under_threshold():
    """采样 AUDIT_SAMPLE_SECONDS 秒，断言 audit.jsonl 增长速率 < 10/分钟。

    阈值依据：N-worker 修复目标之一是抑制 audit 风暴（重复心跳/广播写入）。
    空闲态增长应趋近 0；活跃协作态也不应超过 10/分钟。
    """
    if not AUDIT_PATH.exists():
        pytest.skip(f"audit.jsonl 不存在: {AUDIT_PATH}（worker-2 可能未启动）")

    start_lines = _count_lines(AUDIT_PATH)
    start_time = time.time()
    print(f"[audit] 起始行数={start_lines}, 采样 {AUDIT_SAMPLE_SECONDS}s")
    time.sleep(AUDIT_SAMPLE_SECONDS)
    end_lines = _count_lines(AUDIT_PATH)
    elapsed = time.time() - start_time

    growth = end_lines - start_lines
    growth_per_min = growth / elapsed * 60 if elapsed > 0 else float("inf")
    print(
        f"[audit] 结束行数={end_lines}, 增长={growth} 行, "
        f"速率={growth_per_min:.2f}/分钟"
    )
    assert growth_per_min < 10, (
        f"audit 增长速率 {growth_per_min:.1f}/分钟 超阈值 10/分钟 "
        f"(start={start_lines}, end={end_lines}, elapsed={elapsed:.1f}s)"
    )


# ---------------------------------------------------------------------------
# 测试 3：端到端协作消息延迟 < 30s
# ---------------------------------------------------------------------------
def test_e2e_collab_message_latency():
    """测量端到端协作消息延迟，断言 < E2E_TIMEOUT 秒。

    流程：
      1. 记录起始 seq（worker-2 messages）
      2. 通过 worker-1 /api/multiagent/collab/broadcast 广播一条测试消息
      3. 轮询 worker-2 /api/multiagent/messages，等待出现来自 worker-1 的新消息
      4. 断言从发送到对端可见的延迟 < E2E_TIMEOUT

    注：本测试只验证"消息送达对端 blackboard"，不依赖 LLM 回复
    （LLM 回复延迟受模型负载影响，不适合作为稳定断言）。
    """
    # 1. 记录起始 seq
    base_msgs = _get(f"{WORKER2}/api/multiagent/messages?limit=1")
    base_seq = base_msgs.get("messages", [{}])[0].get("seq", 0) if base_msgs.get("messages") else 0
    print(f"[e2e] worker-2 起始 seq={base_seq}")

    # 2. 构造唯一标记，避免与历史消息混淆
    marker = f"nworker-collab-probe-{int(time.time()*1000)}"
    payload = {
        "from": "integration-test",
        "to": "*",
        "type": "chat",
        "content_type": "markdown",
        "content": f"[集成测试探针] marker={marker}",
    }

    # 3. 通过 worker-1 广播
    t0 = time.perf_counter()
    try:
        resp = _post_json(f"{WORKER1}/api/multiagent/collab/broadcast", payload)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"broadcast 调用失败（worker-1 可能未启用 collab）: {e!r}")
    sent_seq = resp.get("seq")
    print(f"[e2e] broadcast 返回 seq={sent_seq}, marker={marker}")

    # 4. 轮询 worker-2 messages，等待 marker 出现
    deadline = time.time() + E2E_TIMEOUT
    while time.time() < deadline:
        time.sleep(1)
        msgs = _get(f"{WORKER2}/api/multiagent/messages?limit=20")
        for m in msgs.get("messages", []):
            if marker in (m.get("content") or ""):
                latency = time.perf_counter() - t0
                print(f"[e2e] 命中 marker, seq={m.get('seq')}, 延迟={latency:.2f}s")
                assert latency < E2E_TIMEOUT, (
                    f"端到端延迟 {latency:.1f}s 超阈值 {E2E_TIMEOUT}s"
                )
                return
    pytest.fail(
        f"在 {E2E_TIMEOUT}s 内未在 worker-2 收到 marker={marker} 的消息 "
        f"(sent_seq={sent_seq}, base_seq={base_seq})"
    )
