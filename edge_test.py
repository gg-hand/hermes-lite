"""hermes-lite 综合边缘测试脚本。

测试维度：
1. HTTP API 端点基本功能 + 边缘输入
2. 记忆系统端到端（真实 chromadb ONNX embedding）
3. 全链路 chat（mock LLM：单轮/多轮/工具调用/consolidation）
4. 错误处理（无效输入 / 非法 session / 配置校验）

运行方式：先启动服务器，再运行本脚本。
    python edge_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import shutil
from typing import Any, Dict, List, Optional

# 项目根目录
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)

import httpx

BASE_URL = "http://127.0.0.1:8000"

# 测试结果统计
_results: List[Dict[str, Any]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    _results.append({"name": name, "passed": passed, "detail": detail})
    mark = "[OK]" if passed else "[FAIL]"
    print(f"  {mark} {name}" + (f" -- {detail}" if detail and not passed else ""))


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ===========================================================================
# 1. HTTP API 端点测试
# ===========================================================================

def test_http_endpoints() -> None:
    section("1. HTTP API 端点基本功能")
    client = httpx.Client(base_url=BASE_URL, timeout=30.0)

    # 1.1 /health
    try:
        r = client.get("/health")
        data = r.json()
        # 接受 "ok" 或 "healthy"
        ok = r.status_code == 200 and data.get("status") in ("ok", "healthy")
        record("/health 返回 200 + status=ok/healthy", ok, str(data))
    except Exception as e:
        record("/health", False, str(e))

    # 1.2 /sessions（空列表）
    try:
        r = client.get("/sessions")
        data = r.json()
        # 接受 {"sessions": [...]} 或 [...] 两种格式
        sessions_list = data.get("sessions") if isinstance(data, dict) else data
        ok = r.status_code == 200 and isinstance(sessions_list, list)
        record("/sessions 返回列表", ok, str(data)[:80])
    except Exception as e:
        record("/sessions", False, str(e))

    # 1.3 /sessions/{id}/messages — 不存在的 session
    try:
        r = client.get("/sessions/nonexistent-session-999/messages")
        ok = r.status_code in (200, 404)
        record("GET 不存在 session 的 messages 不崩溃", ok, f"status={r.status_code}")
    except Exception as e:
        record("GET 不存在 session", False, str(e))

    # 1.4 /chat — 空输入
    try:
        r = client.post("/chat", json={"session_id": "edge-test", "message": ""})
        ok = r.status_code in (200, 400, 422)
        record("POST /chat 空消息不崩溃", ok, f"status={r.status_code}")
    except Exception as e:
        record("POST /chat 空消息", False, str(e))

    # 1.5 /chat — 缺少字段
    try:
        r = client.post("/chat", json={"session_id": "edge-test"})
        ok = r.status_code in (400, 422)
        record("POST /chat 缺少 message 字段返回 4xx", ok, f"status={r.status_code}")
    except Exception as e:
        record("POST /chat 缺字段", False, str(e))

    # 1.6 /chat — 超长输入（10KB）
    try:
        long_msg = "A" * 10000
        r = client.post("/chat", json={"session_id": "edge-test-long", "message": long_msg})
        # dummy API key 会导致 LLM 调用失败，但服务不应崩溃
        ok = r.status_code in (200, 500)
        record("POST /chat 超长输入(10KB) 不崩溃", ok, f"status={r.status_code}")
    except Exception as e:
        record("POST /chat 超长输入", False, str(e))

    # 1.7 /chat — 特殊字符
    try:
        special_msg = '测试\n\t<>&"\'{}[] emoji: 🎉🚀 中文特殊字符'
        r = client.post("/chat", json={"session_id": "edge-test-special", "message": special_msg})
        ok = r.status_code in (200, 500)
        record("POST /chat 特殊字符+emoji 不崩溃", ok, f"status={r.status_code}")
    except Exception as e:
        record("POST /chat 特殊字符", False, str(e))

    # 1.8 /chat — 无效 JSON
    try:
        r = client.post("/chat", content="not json", headers={"Content-Type": "application/json"})
        ok = r.status_code in (400, 422)
        record("POST /chat 无效 JSON 返回 4xx", ok, f"status={r.status_code}")
    except Exception as e:
        record("POST /chat 无效 JSON", False, str(e))

    # 1.9 DELETE /sessions/{id} — 不存在的 session
    try:
        r = client.delete("/sessions/nonexistent-delete-999")
        ok = r.status_code in (200, 404)
        record("DELETE 不存在 session 不崩溃", ok, f"status={r.status_code}")
    except Exception as e:
        record("DELETE 不存在 session", False, str(e))

    client.close()


# ===========================================================================
# 2. 记忆系统端到端（真实 chromadb）
# ===========================================================================

def test_memory_system() -> None:
    section("2. 记忆系统端到端（真实 chromadb ONNX）")

    # 使用真实 chromadb（不 mock）
    # 重新导入以绕过测试 mock
    import importlib
    # 保存可能被 mock 的模块
    saved_chromadb = sys.modules.pop("chromadb", None)
    saved_chroma_utils = sys.modules.pop("chromadb.utils", None)
    saved_chroma_ef = sys.modules.pop("chromadb.utils.embedding_functions", None)

    try:
        import chromadb
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        import numpy as np

        tmpdir = tempfile.mkdtemp(prefix="hermes_edge_test_")

        try:
            # 重新导入 chroma_store（使用真实 chromadb）
            if "src.storage.chroma_store" in sys.modules:
                del sys.modules["src.storage.chroma_store"]
            from src.storage.chroma_store import ChromaMemoryStore

            store = ChromaMemoryStore(persist_path=tmpdir)
            record("ChromaMemoryStore 初始化（真实 chromadb）", True)

            # 2.1 空查询
            results = store.query_memory("anything", top_k=5)
            record("空集合查询返回空列表", results == [], str(results))

            # 2.2 添加记忆
            id1 = store.add_memory("用户喜欢 Python 编程语言", {"type": "preference", "importance": 0.9})
            id2 = store.add_memory("用户是一名后端工程师", {"type": "fact"})
            id3 = store.add_memory("用户喜欢深色模式界面", {"type": "preference"})
            id4 = store.add_memory("今天天气很好，适合户外运动", {"type": "event"})
            record("添加 4 条记忆", store.collection.count() == 4, f"count={store.collection.count()}")

            # 2.3 语义检索 — 相关查询
            results = store.query_memory("用户喜欢什么编程语言", top_k=2)
            top_content = results[0]["content"] if results else ""
            ok = len(results) == 2 and "Python" in top_content
            record("语义检索：编程语言查询命中 Python", ok, f"top={top_content[:40]}, sim={results[0]['similarity']:.4f}" if results else "empty")

            # 2.4 语义检索 — 无关查询
            results = store.query_memory("量子力学原理", top_k=2)
            # 无关查询也应返回结果（只是相似度低），验证不崩溃
            record("语义检索：无关查询不崩溃", isinstance(results, list) and len(results) <= 2, f"got {len(results)} results")

            # 2.5 去重检测 — 相似内容
            dups = store.find_duplicates("用户喜欢用 Python 写代码", threshold=0.6)
            ok = any("Python" in d["content"] for d in dups)
            record("去重检测：相似内容被检出", ok, f"{len(dups)} dups, top_sim={dups[0]['similarity']:.4f}" if dups else "no dups")

            # 2.6 去重检测 — 不相似内容
            dups = store.find_duplicates("明天会议安排在下午三点", threshold=0.85)
            record("去重检测：不相似内容未被检出", len(dups) == 0, f"{len(dups)} dups")

            # 2.7 top_k=0
            results = store.query_memory("test", top_k=0)
            record("top_k=0 返回空列表", results == [], str(results))

            # 2.8 top_k 超过集合大小
            results = store.query_memory("test", top_k=100)
            record("top_k > 集合大小时自动截断", len(results) == 4, f"got {len(results)}")

            # 2.9 删除记忆
            store.delete_memory(id4)
            record("删除记忆后 count 减少", store.collection.count() == 3, f"count={store.collection.count()}")

            # 2.10 更新记忆
            store.update_memory(id1, "用户喜欢 Rust 编程语言", {"type": "preference", "importance": 0.9})
            all_mems = store.get_all_memories()
            updated = [m for m in all_mems if m["id"] == id1]
            ok = len(updated) == 1 and "Rust" in updated[0]["content"]
            record("更新记忆内容成功", ok, updated[0]["content"][:40] if updated else "not found")

            # 2.11 get_all_memories
            all_mems = store.get_all_memories()
            record("get_all_memories 返回全部", len(all_mems) == 3, f"got {len(all_mems)}")

            store.close()
        finally:
            # 清理（Windows 下 chromadb 文件可能有锁，ignore_errors）
            shutil.rmtree(tmpdir, ignore_errors=True)

    except Exception as e:
        record("记忆系统测试异常", False, str(e))
    finally:
        # 恢复 mock（如果之前有）
        if saved_chromadb is not None:
            sys.modules["chromadb"] = saved_chromadb
        if saved_chroma_utils is not None:
            sys.modules["chromadb.utils"] = saved_chroma_utils
        if saved_chroma_ef is not None:
            sys.modules["chromadb.utils.embedding_functions"] = saved_chroma_ef


# ===========================================================================
# 3. 全链路 chat 测试（mock LLM）
# ===========================================================================

class MockLLMResponse:
    """模拟 LLMResponse，兼容 anthropic.types.Message 接口。"""
    def __init__(self, content: List[Dict], stop_reason: str = "end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = {"input_tokens": 100, "output_tokens": 50}


class MockLLMClient:
    """Mock LLM 客户端，按预设规则返回响应。

    - 包含 "工具" 或 "tool" 关键词时返回 tool_use
    - 否则返回文本回复
    - consolidation 调用返回结构化 JSON 事实
    """
    def __init__(self):
        self.main_provider = "mock"
        self.main_model = "mock-model"
        self.consolidation_provider = "mock"
        self.consolidation_model = "mock-model"
        self.call_count = 0

    def chat_main(self, messages, tools=None, system=None, max_tokens=None):
        self.call_count += 1
        last_msg = messages[-1] if messages else {}
        user_text = ""
        if isinstance(last_msg.get("content"), str):
            user_text = last_msg["content"]
        elif isinstance(last_msg.get("content"), list):
            for b in last_msg["content"]:
                if isinstance(b, dict) and b.get("type") == "text":
                    user_text += b.get("text", "")
                elif isinstance(b, dict) and b.get("type") == "tool_result":
                    user_text += "[工具结果: " + str(b.get("content", ""))[:50] + "]"

        # 如果上一条是 tool_result（工具结果回传），返回最终文本
        if isinstance(last_msg.get("content"), list):
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in last_msg["content"]
            )
            if has_tool_result:
                return MockLLMResponse(
                    content=[{"type": "text", "text": f"根据工具结果，这是最终回复。"}],
                    stop_reason="end_turn",
                )

        # 如果用户消息包含 "工具"，触发 tool_use
        if "工具" in user_text or "tool" in user_text.lower():
            if tools:
                # 找第一个工具调用
                tool = tools[0]
                tool_name = tool.get("name", "unknown")
                return MockLLMResponse(
                    content=[
                        {"type": "text", "text": f"我来使用 {tool_name} 工具帮你处理。"},
                        {
                            "type": "tool_use",
                            "id": "toolu_mock_001",
                            "name": tool_name,
                            "input": {"query": user_text[:100]},
                        },
                    ],
                    stop_reason="tool_use",
                )

        # 普通文本回复
        return MockLLMResponse(
            content=[{"type": "text", "text": f"这是对「{user_text[:50]}」的回复。"}],
            stop_reason="end_turn",
        )

    def chat_consolidation(self, messages, system=None, max_tokens=None):
        # 从 system prompt 中提取对话文本，识别 user 消息内容作为事实
        # system 格式：CONSOLIDATION_PROMPT.format(conversation=conversation_text)
        # conversation_text 形如 "[user]: ...\n[assistant]: ..."
        facts = []
        if isinstance(system, str):
            for line in system.split("\n"):
                if line.startswith("[user]:"):
                    user_content = line[len("[user]:"):].strip()
                    if user_content:
                        # 简单识别 user_profile 类事实（含 "喜欢"/"住在"/"是" 等关键词）
                        fact_type = "user_profile" if any(
                            kw in user_content for kw in ["喜欢", "住在", "我是", "工作是"]
                        ) else "fact"
                        facts.append({
                            "content": user_content[:80],
                            "type": fact_type,
                            "importance": 0.7,
                        })
        return MockLLMResponse(
            content=[{"type": "text", "text": json.dumps(facts[:5], ensure_ascii=False)}],
            stop_reason="end_turn",
        )

    def count_tokens(self, text):
        return len(text) // 3 if text else 0

    def count_messages_tokens(self, messages):
        total = 0
        for m in messages:
            c = m.get("content", "")
            if isinstance(c, str):
                total += len(c) // 3
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict):
                        total += len(str(b.get("text", b.get("content", "")))) // 3
        return total


def test_chat_flow_mock_llm() -> None:
    section("3. 全链路 chat 测试（mock LLM）")

    # 清除可能被 mock 的模块，使用真实模块
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("src."):
            pass  # 保留已加载的 src 模块

    tmpdir = tempfile.mkdtemp(prefix="hermes_chat_test_")

    try:
        from src.config import load_config
        from src.orchestrator import Orchestrator
        from src.llm.prompts import SYSTEM_PROMPT

        # 加载配置并覆盖存储路径到临时目录
        config = load_config("config.yaml")
        config["storage"]["sqlite_path"] = os.path.join(tmpdir, "sessions.db")
        config["memory"]["chroma_path"] = os.path.join(tmpdir, "chroma")
        config["memory"]["memory_md_path"] = os.path.join(tmpdir, "memory.md")
        config["memory"]["consolidation_threshold"] = 4  # 降低阈值便于测试

        # 创建 Orchestrator 但替换 LLM 客户端
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.config = config

        memory_config = config.get("memory", {})
        storage_config = config.get("storage", {})
        tools_config = config.get("tools", {})

        # 用 mock LLM
        mock_llm = MockLLMClient()
        orchestrator.llm_client = mock_llm

        # 工具注册
        from src.agent.tool_registry import ToolRegistry
        from src.agent.builtin_tools import register_builtin_tools
        from src.agent.react_loop import ReactLoop
        from src.storage.history_buffer import HistoryBuffer
        from src.storage.chroma_store import ChromaMemoryStore
        from src.storage.sqlite_log import SessionLogger
        from src.memory.memory_md import MemoryMdManager
        from src.memory.retrieval import MemoryRetriever
        from src.memory.context_manager import ContextManager
        from src.memory.consolidation import ConsolidationEngine

        orchestrator.tool_registry = ToolRegistry(
            defer_loading_threshold=int(tools_config.get("defer_loading_threshold", 20))
        )
        register_builtin_tools(orchestrator.tool_registry)

        orchestrator.react_loop = ReactLoop(
            llm_client=mock_llm,
            tool_registry=orchestrator.tool_registry,
            max_loops=int(tools_config.get("max_react_loops", 50)),
        )

        orchestrator.history_buffer = HistoryBuffer(
            max_turns=int(memory_config.get("history_max_turns", 20)),
        )

        orchestrator.chroma_store = ChromaMemoryStore(
            persist_path=config["memory"]["chroma_path"]
        )

        orchestrator.session_logger = SessionLogger(
            db_path=config["storage"]["sqlite_path"]
        )

        orchestrator.memory_md_manager = MemoryMdManager(
            file_path=config["memory"]["memory_md_path"]
        )

        orchestrator.memory_retriever = MemoryRetriever(
            chroma_store=orchestrator.chroma_store,
            memory_md_manager=orchestrator.memory_md_manager,
            llm_client=mock_llm,
            top_k=int(memory_config.get("retrieval_top_k", 5)),
        )

        orchestrator.context_manager = ContextManager(
            tool_registry=orchestrator.tool_registry,
            memory_md_manager=orchestrator.memory_md_manager,
            memory_retriever=orchestrator.memory_retriever,
            history_buffer=orchestrator.history_buffer,
        )

        memory_md_writer = orchestrator.memory_md_manager.async_write
        orchestrator.consolidation_engine = ConsolidationEngine(
            llm_client=mock_llm,
            chroma_store=orchestrator.chroma_store,
            memory_md_writer=memory_md_writer,
            threshold=int(memory_config.get("consolidation_threshold", 4)),
            dedup_threshold=float(memory_config.get("dedup_similarity_threshold", 0.85)),
        )

        print("  Mock Orchestrator 装配完成")

        # 3.1 单轮对话
        try:
            resp = orchestrator.chat("session-1", "你好，介绍一下自己")
            ok = isinstance(resp, str) and len(resp) > 0
            record("单轮对话返回非空文本", ok, resp[:60])
        except Exception as e:
            record("单轮对话", False, str(e))

        # 3.2 多轮对话（历史保持）
        try:
            resp1 = orchestrator.chat("session-2", "我喜欢 Python")
            resp2 = orchestrator.chat("session-2", "我刚才说我喜欢什么？")
            ok = isinstance(resp2, str) and len(resp2) > 0
            record("多轮对话第二轮正常返回", ok, resp2[:60])
        except Exception as e:
            record("多轮对话", False, str(e))

        # 3.3 工具调用
        try:
            resp = orchestrator.chat("session-3", "请使用工具帮我查询信息")
            ok = isinstance(resp, str) and len(resp) > 0
            record("工具调用对话正常完成", ok, resp[:60])
        except Exception as e:
            record("工具调用对话", False, str(e))

        # 3.4 consolidation 触发（阈值=4，每轮 2 条信息，2 轮后触发）
        try:
            orchestrator.chat("session-4", "用户喜欢咖啡")
            orchestrator.chat("session-4", "用户住在上海")
            # 4 条信息 >= 阈值 4，应触发 consolidation
            chroma_count = orchestrator.chroma_store.collection.count()
            ok = chroma_count > 0
            record("consolidation 触发后 ChromaDB 有记忆", ok, f"chroma count={chroma_count}")
        except Exception as e:
            record("consolidation 触发", False, str(e))

        # 3.5 跨会话记忆检索
        try:
            results = orchestrator.chroma_store.query_memory("用户喜欢什么饮品", top_k=3)
            ok = len(results) > 0 and any("咖啡" in r["content"] for r in results)
            record("跨会话记忆检索命中", ok, f"top={results[0]['content'][:40]}" if results else "empty")
        except Exception as e:
            record("跨会话记忆检索", False, str(e))

        # 3.6 session 日志持久化
        try:
            sessions = orchestrator.session_logger.list_sessions()
            ok = len(sessions) >= 4
            record("Session 日志记录了多个会话", ok, f"{len(sessions)} sessions")
        except Exception as e:
            record("Session 日志", False, str(e))

        # 3.7 memory.md 写入（consolidation 异步写入，需等待守护线程完成）
        try:
            time.sleep(0.6)  # 等待 async_write 守护线程完成
            md_path = config["memory"]["memory_md_path"]
            if os.path.exists(md_path):
                with open(md_path, "r", encoding="utf-8") as f:
                    md_content = f.read()
                ok = len(md_content) > 0
                record("memory.md 有内容写入", ok, md_content[:60])
            else:
                record("memory.md 有内容写入", False, "文件不存在")
        except Exception as e:
            record("memory.md 写入", False, str(e))

        # 清理
        try:
            if orchestrator.chroma_store:
                orchestrator.chroma_store.close()
        except:
            pass

    except Exception as e:
        record("chat 流测试装配失败", False, str(e))
        import traceback
        traceback.print_exc()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# 4. 错误处理与配置校验
# ===========================================================================

def test_error_handling() -> None:
    section("4. 错误处理与配置校验")

    # 4.1 无效 provider
    try:
        from src.llm.client import _create_backend
        try:
            _create_backend("invalid_provider", "model", "key")
            record("无效 provider 抛出 ValueError", False, "未抛异常")
        except ValueError:
            record("无效 provider 抛出 ValueError", True)
    except Exception as e:
        record("无效 provider 测试", False, str(e))

    # 4.2 空 API Key
    try:
        from src.llm.client import LLMClient
        cfg = {
            "llm": {
                "main_provider": "anthropic",
                "main_model": "claude-test",
                "main_api_key": "",
                "consolidation_provider": "anthropic",
                "consolidation_model": "claude-test",
                "consolidation_api_key": "",
            }
        }
        try:
            LLMClient(config=cfg)
            record("空 API Key 抛出 ValueError", False, "未抛异常")
        except ValueError:
            record("空 API Key 抛出 ValueError", True)
    except Exception as e:
        record("空 API Key 测试", False, str(e))

    # 4.3 缺少 llm 配置段
    try:
        from src.llm.client import LLMClient
        try:
            LLMClient(config={})
            record("缺少 llm 配置段抛出 ValueError", False, "未抛异常")
        except ValueError:
            record("缺少 llm 配置段抛出 ValueError", True)
    except Exception as e:
        record("缺少 llm 配置段", False, str(e))

    # 4.4 DeepSeek 配置正确路由
    try:
        from src.llm.client import LLMClient, OpenAICompatBackend
        cfg = {
            "llm": {
                "main_provider": "deepseek",
                "main_model": "deepseek-chat",
                "main_api_key": "sk-test",
                "consolidation_provider": "deepseek",
                "consolidation_model": "deepseek-chat",
                "consolidation_api_key": "sk-test",
            }
        }
        c = LLMClient(config=cfg)
        ok = isinstance(c._main_backend, OpenAICompatBackend)
        record("DeepSeek 配置路由到 OpenAICompatBackend", ok, type(c._main_backend).__name__)
    except Exception as e:
        record("DeepSeek 配置路由", False, str(e))

    # 4.5 Qwen 默认 base_url
    try:
        from src.llm.client import LLMClient
        cfg = {
            "llm": {
                "main_provider": "qwen",
                "main_model": "qwen-plus",
                "main_api_key": "sk-test",
                "consolidation_provider": "qwen",
                "consolidation_model": "qwen-turbo",
                "consolidation_api_key": "sk-test",
            }
        }
        c = LLMClient(config=cfg)
        ok = "dashscope" in (c._main_backend.base_url or "")
        record("Qwen 默认 base_url 正确", ok, c._main_backend.base_url)
    except Exception as e:
        record("Qwen base_url", False, str(e))

    # 4.6 HistoryBuffer 溢出降级
    try:
        from src.storage.history_buffer import HistoryBuffer
        buf = HistoryBuffer(max_turns=5)
        sid = "overflow-test"
        for i in range(10):
            buf.add_message(sid, "user", f"消息 {i}")
            buf.add_message(sid, "assistant", f"回复 {i}")
        history = buf.get_history(sid)
        ok = len(history) <= 10  # max_turns=5 → 最多 10 条消息
        record("HistoryBuffer 溢出后裁剪到 max_turns", ok, f"got {len(history)} msgs")
    except Exception as e:
        record("HistoryBuffer 溢出", False, str(e))

    # 4.7 ToolRegistry defer_loading
    try:
        from src.agent.tool_registry import ToolRegistry
        reg = ToolRegistry(defer_loading_threshold=5)
        for i in range(10):
            reg.register(
                name=f"tool_{i}",
                description=f"工具 {i}",
                input_schema={"type": "object", "properties": {}},
                handler=lambda **kw: "ok",
            )
        schemas = reg.get_tools_schema()
        # 超过阈值应返回 stub（描述为 "Use tool_search to load full schema"）+ tool_search
        # 验证：1) 数量 = 工具数 + 1（tool_search）；2) 前面 10 个为 stub；3) 最后一个是 tool_search
        has_tool_search = any(s.get("name") == "tool_search" for s in schemas)
        stubs = [s for s in schemas if s.get("name") != "tool_search"]
        all_stubs = all(
            s.get("description") == "Use tool_search to load full schema" for s in stubs
        )
        ok = has_tool_search and all_stubs and len(stubs) == 10
        record(
            "ToolRegistry defer_loading 超阈值返回 stub + tool_search",
            ok,
            f"got {len(schemas)} schemas (10 stubs + 1 tool_search expected)",
        )
    except Exception as e:
        record("ToolRegistry defer_loading", False, str(e))

    # 4.8 ContextManager 分层
    try:
        from src.agent.tool_registry import ToolRegistry
        from src.memory.context_manager import ContextManager

        reg = ToolRegistry(defer_loading_threshold=20)
        cm = ContextManager(
            tool_registry=reg,
            memory_md_manager=None,
            memory_retriever=None,
            history_buffer=None,
        )
        # 验证 build_prompt 返回结构（正确方法名是 build_prompt，不是 build_context）
        ctx = cm.build_prompt(
            session_id="test",
            user_input="hello",
        )
        ok = "system" in ctx and "tools" in ctx and "messages" in ctx
        record("ContextManager 返回 system/tools/messages 三层", ok, str(list(ctx.keys())))
    except Exception as e:
        record("ContextManager 分层", False, str(e))


# ===========================================================================
# 汇总
# ===========================================================================

def print_summary() -> None:
    section("测试汇总")
    total = len(_results)
    passed = sum(1 for r in _results if r["passed"])
    failed = total - passed
    print(f"\n  总计: {total}  通过: {passed}  失败: {failed}")
    print(f"  通过率: {passed/total*100:.1f}%" if total > 0 else "  无测试")

    if failed > 0:
        print("\n  失败项:")
        for r in _results:
            if not r["passed"]:
                print(f"    [FAIL] {r['name']}: {r['detail']}")

    print()
    return failed == 0


if __name__ == "__main__":
    test_http_endpoints()
    test_memory_system()
    test_chat_flow_mock_llm()
    test_error_handling()
    all_passed = print_summary()
    sys.exit(0 if all_passed else 1)
