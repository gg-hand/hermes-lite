"""Intent Classifier 单元测试 — 验证意图分类、低置信度回退、LLM 失败降级。

运行方式:
    python -m unittest tests.test_intent_classifier -v
    python tests/test_intent_classifier.py

mock 策略:
- LLMClient 用 MagicMock + AsyncMock 替代 chat_consolidation 方法。
- 不依赖真实 chromadb / sentence_transformers / numpy（install_mocks 已注入）。
- 测试覆盖四类意图分类、低置信度回退、LLM 失败降级、非流式模式兼容、
  路由分发（IntentType 正确解析）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from teage_liu.agent.intent_classifier import (  # noqa: E402
    IntentClassificationResult,
    IntentType,
    _MAX_INTENT_TOKENS,
    _MIN_CONFIDENCE,
    _parse_intent_output,
    classify_intent,
)


def _make_llm_response(text: str) -> MagicMock:
    """构造 mock LLM 响应对象，.content 为含单个 text block 的列表。"""
    response = MagicMock()
    response.content = [{"type": "text", "text": text}]
    return response


def _make_llm_client(response_text: str = "") -> MagicMock:
    """构造 mock LLMClient，chat_consolidation 返回含 response_text 的响应。"""
    client = MagicMock()
    client.chat_consolidation = AsyncMock(
        return_value=_make_llm_response(response_text)
    )
    return client


class TestIntentTypeEnum(unittest.TestCase):
    """验证 IntentType 枚举完整性。"""

    def test_four_intent_types_exist(self) -> None:
        """四类意图枚举值均存在。"""
        self.assertTrue(hasattr(IntentType, "SIMPLE_QA"))
        self.assertTrue(hasattr(IntentType, "KNOWLEDGE_LOOKUP"))
        self.assertTrue(hasattr(IntentType, "MULTI_STEP_TASK"))
        self.assertTrue(hasattr(IntentType, "OUT_OF_SCOPE"))

    def test_enum_values_are_strings(self) -> None:
        """枚举值是字符串（便于 JSON 序列化与 LLM 输出匹配）。"""
        self.assertEqual(IntentType.SIMPLE_QA.value, "simple_qa")
        self.assertEqual(IntentType.KNOWLEDGE_LOOKUP.value, "knowledge_lookup")
        self.assertEqual(IntentType.MULTI_STEP_TASK.value, "multi_step_task")
        self.assertEqual(IntentType.OUT_OF_SCOPE.value, "out_of_scope")

    def test_four_distinct_values(self) -> None:
        """四个枚举值互不相同。"""
        values = {
            IntentType.SIMPLE_QA,
            IntentType.KNOWLEDGE_LOOKUP,
            IntentType.MULTI_STEP_TASK,
            IntentType.OUT_OF_SCOPE,
        }
        self.assertEqual(len(values), 4)


class TestParseIntentOutput(unittest.TestCase):
    """验证 _parse_intent_output 对各种 LLM 输出格式的解析。"""

    def test_strict_json_simple_qa(self) -> None:
        """严格 JSON 格式的 simple_qa 解析。"""
        result = _parse_intent_output(
            '{"intent": "simple_qa", "confidence": 0.9}'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)
        self.assertAlmostEqual(result.confidence, 0.9)

    def test_strict_json_knowledge_lookup(self) -> None:
        """严格 JSON 格式的 knowledge_lookup 解析。"""
        result = _parse_intent_output(
            '{"intent": "knowledge_lookup", "confidence": 0.85}'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.KNOWLEDGE_LOOKUP)

    def test_strict_json_multi_step_task(self) -> None:
        """严格 JSON 格式的 multi_step_task 解析。"""
        result = _parse_intent_output(
            '{"intent": "multi_step_task", "confidence": 0.95}'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.MULTI_STEP_TASK)

    def test_strict_json_out_of_scope(self) -> None:
        """严格 JSON 格式的 out_of_scope 解析。"""
        result = _parse_intent_output(
            '{"intent": "out_of_scope", "confidence": 0.8}'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.OUT_OF_SCOPE)

    def test_json_in_markdown_code_block(self) -> None:
        """LLM 输出包裹在 markdown 代码块中时仍能解析。"""
        result = _parse_intent_output(
            '```json\n{"intent": "simple_qa", "confidence": 0.9}\n```'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)

    def test_json_with_surrounding_text(self) -> None:
        """LLM 输出含前后多余文本时仍能提取 JSON。"""
        result = _parse_intent_output(
            '根据分析，分类结果如下：\n'
            '{"intent": "multi_step_task", "confidence": 0.88}\n'
            '以上是分类结果。'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.MULTI_STEP_TASK)
        self.assertAlmostEqual(result.confidence, 0.88)

    def test_alias_simple(self) -> None:
        """LLM 输出简写别名 'simple' 时仍能解析为 SIMPLE_QA。"""
        result = _parse_intent_output('{"intent": "simple", "confidence": 0.7}')
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)

    def test_alias_multistep(self) -> None:
        """LLM 输出简写别名 'multistep' 时仍能解析为 MULTI_STEP_TASK。"""
        result = _parse_intent_output('{"intent": "multistep", "confidence": 0.7}')
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.MULTI_STEP_TASK)

    def test_alias_oos(self) -> None:
        """LLM 输出简写别名 'oos' 时仍能解析为 OUT_OF_SCOPE。"""
        result = _parse_intent_output('{"intent": "oos", "confidence": 0.7}')
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.OUT_OF_SCOPE)

    def test_non_json_alias_match(self) -> None:
        """非 JSON 输出但含 intent 别名时降级解析（低置信度）。"""
        result = _parse_intent_output("这个看起来是 simple_qa 类型")
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)
        # 别名匹配降级路径置信度低
        self.assertAlmostEqual(result.confidence, 0.5)

    def test_empty_input_returns_none(self) -> None:
        """空输入返回 None。"""
        self.assertIsNone(_parse_intent_output(""))
        self.assertIsNone(_parse_intent_output("   "))
        self.assertIsNone(_parse_intent_output(None))

    def test_invalid_json_returns_none(self) -> None:
        """无效 JSON 返回 None。"""
        self.assertIsNone(_parse_intent_output("{invalid json}"))
        self.assertIsNone(_parse_intent_output("not a json at all"))

    def test_unknown_intent_returns_none(self) -> None:
        """JSON 含未知 intent 值时返回 None。"""
        result = _parse_intent_output(
            '{"intent": "unknown_type", "confidence": 0.9}'
        )
        self.assertIsNone(result)

    def test_missing_confidence_defaults_to_1(self) -> None:
        """JSON 缺失 confidence 字段时默认为 1.0。"""
        result = _parse_intent_output('{"intent": "simple_qa"}')
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.confidence, 1.0)

    def test_invalid_confidence_falls_back_to_1(self) -> None:
        """confidence 字段类型错误时回退为 1.0。"""
        result = _parse_intent_output(
            '{"intent": "simple_qa", "confidence": "high"}'
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.confidence, 1.0)

    def test_confidence_clamped_to_range(self) -> None:
        """confidence 超出 [0,1] 范围时被裁剪。"""
        result = _parse_intent_output(
            '{"intent": "simple_qa", "confidence": 1.5}'
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.confidence, 1.0)

        result = _parse_intent_output(
            '{"intent": "simple_qa", "confidence": -0.3}'
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.confidence, 0.0)

    def test_raw_output_preserved(self) -> None:
        """raw_output 字段保留 LLM 原始输出文本。"""
        raw = '```json\n{"intent": "simple_qa", "confidence": 0.9}\n```'
        result = _parse_intent_output(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result.raw_output, raw)


class TestClassifyIntentAsync(unittest.IsolatedAsyncioTestCase):
    """验证 classify_intent async 函数的行为。"""

    async def test_simple_qa_classification(self) -> None:
        """LLM 返回 simple_qa + 高置信度时，分类结果为 SIMPLE_QA。"""
        client = _make_llm_client(
            '{"intent": "simple_qa", "confidence": 0.95}'
        )
        result = await classify_intent(
            llm_client=client,
            user_input="你好，今天天气怎么样？",
        )
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)
        self.assertGreaterEqual(result.confidence, _MIN_CONFIDENCE)
        # 验证调用了 chat_consolidation
        client.chat_consolidation.assert_awaited_once()

    async def test_knowledge_lookup_classification(self) -> None:
        """LLM 返回 knowledge_lookup + 高置信度时，分类结果为 KNOWLEDGE_LOOKUP。"""
        client = _make_llm_client(
            '{"intent": "knowledge_lookup", "confidence": 0.88}'
        )
        result = await classify_intent(
            llm_client=client,
            user_input="之前我们讨论过什么？",
        )
        self.assertEqual(result.intent, IntentType.KNOWLEDGE_LOOKUP)

    async def test_multi_step_task_classification(self) -> None:
        """LLM 返回 multi_step_task + 高置信度时，分类结果为 MULTI_STEP_TASK。"""
        client = _make_llm_client(
            '{"intent": "multi_step_task", "confidence": 0.92}'
        )
        result = await classify_intent(
            llm_client=client,
            user_input="读取文件并分析数据后生成报告",
        )
        self.assertEqual(result.intent, IntentType.MULTI_STEP_TASK)

    async def test_out_of_scope_classification(self) -> None:
        """LLM 返回 out_of_scope + 高置信度时，分类结果为 OUT_OF_SCOPE。"""
        client = _make_llm_client(
            '{"intent": "out_of_scope", "confidence": 0.85}'
        )
        result = await classify_intent(
            llm_client=client,
            user_input="请修改 src/orchestrator.py 的源码",
        )
        self.assertEqual(result.intent, IntentType.OUT_OF_SCOPE)

    async def test_low_confidence_falls_back_to_multi_step(self) -> None:
        """低置信度（<0.6）回退到 MULTI_STEP_TASK（走完整 ReactLoop，最保守）。"""
        client = _make_llm_client(
            '{"intent": "simple_qa", "confidence": 0.4}'
        )
        result = await classify_intent(
            llm_client=client,
            user_input="随便问点什么",
        )
        # 低置信度回退：即使 LLM 说 simple_qa，confidence 低时改为 MULTI_STEP_TASK
        self.assertEqual(result.intent, IntentType.MULTI_STEP_TASK)
        self.assertLess(result.confidence, _MIN_CONFIDENCE)

    async def test_llm_failure_falls_back_to_simple_qa(self) -> None:
        """LLM 调用失败时降级为 SIMPLE_QA（不阻塞主流程）。"""
        client = MagicMock()
        client.chat_consolidation = AsyncMock(
            side_effect=RuntimeError("LLM service down")
        )
        result = await classify_intent(
            llm_client=client,
            user_input="任何问题",
        )
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)
        self.assertAlmostEqual(result.confidence, 0.0)

    async def test_llm_timeout_falls_back_to_simple_qa(self) -> None:
        """LLM 调用超时（asyncio.TimeoutError）时降级为 SIMPLE_QA。"""
        client = MagicMock()
        client.chat_consolidation = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )
        result = await classify_intent(
            llm_client=client,
            user_input="任何问题",
        )
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)
        self.assertAlmostEqual(result.confidence, 0.0)

    async def test_parse_failure_falls_back_to_simple_qa(self) -> None:
        """LLM 输出无法解析时降级为 SIMPLE_QA。"""
        client = _make_llm_response_client("this is not valid json or intent")
        result = await classify_intent(
            llm_client=client,
            user_input="test",
        )
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)
        self.assertAlmostEqual(result.confidence, 0.0)

    async def test_empty_user_input_returns_simple_qa(self) -> None:
        """空用户输入直接返回 SIMPLE_QA，不调用 LLM。"""
        client = _make_llm_client('{"intent": "multi_step_task", "confidence": 0.9}')
        result = await classify_intent(
            llm_client=client,
            user_input="",
        )
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)
        self.assertAlmostEqual(result.confidence, 0.0)
        # 空输入不应调用 LLM
        client.chat_consolidation.assert_not_awaited()

    async def test_whitespace_user_input_returns_simple_qa(self) -> None:
        """纯空白用户输入直接返回 SIMPLE_QA。"""
        client = _make_llm_client('{"intent": "multi_step_task", "confidence": 0.9}')
        result = await classify_intent(
            llm_client=client,
            user_input="   \n\t  ",
        )
        self.assertEqual(result.intent, IntentType.SIMPLE_QA)
        client.chat_consolidation.assert_not_awaited()

    async def test_uses_consolidation_model_with_max_200_tokens(self) -> None:
        """验证调用 chat_consolidation 时 max_tokens=200。"""
        client = _make_llm_client(
            '{"intent": "simple_qa", "confidence": 0.9}'
        )
        await classify_intent(
            llm_client=client,
            user_input="test",
        )
        call_kwargs = client.chat_consolidation.call_args.kwargs
        self.assertEqual(call_kwargs.get("max_tokens"), _MAX_INTENT_TOKENS)
        self.assertEqual(_MAX_INTENT_TOKENS, 200)

    async def test_history_passed_to_llm(self) -> None:
        """history 参数被传递给 LLM（取最近 2 轮）。"""
        client = _make_llm_client(
            '{"intent": "simple_qa", "confidence": 0.9}'
        )
        history = [
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "之前的回答"},
            {"role": "user", "content": "更早的问题"},
            {"role": "assistant", "content": "更早的回答"},
            {"role": "user", "content": "最老的问题"},
            {"role": "assistant", "content": "最老的回答"},
        ]
        await classify_intent(
            llm_client=client,
            user_input="新问题",
            history=history,
        )
        call_kwargs = client.chat_consolidation.call_args.kwargs
        messages = call_kwargs.get("messages", [])
        # 验证 messages 含历史（最多 4 条）+ 当前 user_input（1 条）= 最多 5 条
        self.assertLessEqual(len(messages), 5)
        # 最后一条应为当前 user_input
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("新问题", messages[-1]["content"])

    async def test_long_history_truncated_to_2_rounds(self) -> None:
        """长历史被截断为最近 2 轮（4 条消息）。"""
        client = _make_llm_client(
            '{"intent": "simple_qa", "confidence": 0.9}'
        )
        history = [
            {"role": "user", "content": f"问题{i}"} for i in range(20)
        ]
        await classify_intent(
            llm_client=client,
            user_input="新问题",
            history=history,
        )
        call_kwargs = client.chat_consolidation.call_args.kwargs
        messages = call_kwargs.get("messages", [])
        # 最多 4 条历史 + 1 条当前 user_input = 5 条
        self.assertLessEqual(len(messages), 5)

    async def test_system_prompt_passed_to_llm(self) -> None:
        """system prompt 被传递给 LLM。"""
        client = _make_llm_client(
            '{"intent": "simple_qa", "confidence": 0.9}'
        )
        await classify_intent(
            llm_client=client,
            user_input="test",
        )
        call_kwargs = client.chat_consolidation.call_args.kwargs
        system = call_kwargs.get("system", "")
        self.assertIsInstance(system, str)
        self.assertTrue(system.strip(), "system prompt 不应为空")
        self.assertIn("意图分类器", system)

    async def test_raw_output_preserved_in_result(self) -> None:
        """result.raw_output 保留 LLM 原始输出文本。"""
        raw = '{"intent": "simple_qa", "confidence": 0.9}'
        client = _make_llm_client(raw)
        result = await classify_intent(
            llm_client=client,
            user_input="test",
        )
        self.assertEqual(result.raw_output, raw)

    async def test_result_repr(self) -> None:
        """IntentClassificationResult 的 __repr__ 含 intent 和 confidence。"""
        result = IntentClassificationResult(
            intent=IntentType.SIMPLE_QA,
            confidence=0.85,
        )
        repr_str = repr(result)
        self.assertIn("simple_qa", repr_str)
        self.assertIn("0.85", repr_str)

    async def test_no_stream_manager_dependency(self) -> None:
        """classify_intent 不依赖 stream_manager（验证非流式模式兼容）。

        设计验证：classify_intent 签名不含 stream_manager 参数，
        chat() 非流式入口与 chat_stream() 流式入口共用同一函数。
        """
        client = _make_llm_client(
            '{"intent": "simple_qa", "confidence": 0.9}'
        )
        # 非流式模式：不传 stream_manager
        result_non_stream = await classify_intent(
            llm_client=client,
            user_input="test",
        )
        # 流式模式：同样不传 stream_manager（stream_manager 由 caller 持有）
        result_stream = await classify_intent(
            llm_client=client,
            user_input="test",
        )
        # 两次调用结果应一致（除 raw_output 外）
        self.assertEqual(result_non_stream.intent, result_stream.intent)


def _make_llm_response_client(response_text: str) -> MagicMock:
    """构造 mock LLMClient（与 _make_llm_client 别名，便于测试命名清晰）。"""
    return _make_llm_client(response_text)


class TestIntentClassificationResult(unittest.TestCase):
    """验证 IntentClassificationResult 数据类。"""

    def test_confidence_clamped_to_range(self) -> None:
        """confidence 超出 [0,1] 范围时被裁剪。"""
        result = IntentClassificationResult(
            intent=IntentType.SIMPLE_QA,
            confidence=1.5,
        )
        self.assertAlmostEqual(result.confidence, 1.0)

        result = IntentClassificationResult(
            intent=IntentType.SIMPLE_QA,
            confidence=-0.5,
        )
        self.assertAlmostEqual(result.confidence, 0.0)

    def test_default_confidence_is_1(self) -> None:
        """未传 confidence 时默认为 1.0。"""
        result = IntentClassificationResult(intent=IntentType.SIMPLE_QA)
        self.assertAlmostEqual(result.confidence, 1.0)

    def test_default_raw_output_is_empty(self) -> None:
        """未传 raw_output 时默认为空字符串。"""
        result = IntentClassificationResult(intent=IntentType.SIMPLE_QA)
        self.assertEqual(result.raw_output, "")


class TestIntegrationWithOrchestrator(unittest.IsolatedAsyncioTestCase):
    """验证 intent_classifier 与 Orchestrator 的集成点存在。

    这些测试不调用真实 Orchestrator.chat()（成本太高，需 mock 全链路），
    只验证 Orchestrator 模块导入了 classify_intent / IntentType 等符号，
    且 chat / chat_stream 方法体引用了这些符号（通过源码检查）。
    """

    def test_orchestrator_imports_intent_classifier(self) -> None:
        """Orchestrator 模块应导入 intent_classifier 符号。"""
        from teage_liu import orchestrator

        # 验证导入符号存在（None 也算，因为 import 失败时降级为 None）
        self.assertTrue(hasattr(orchestrator, "IntentType"))
        self.assertTrue(hasattr(orchestrator, "IntentClassificationResult"))
        self.assertTrue(hasattr(orchestrator, "classify_intent"))

    def test_orchestrator_intent_classifier_not_none(self) -> None:
        """正常环境下 intent_classifier 符号不为 None（导入成功）。"""
        from teage_liu import orchestrator

        self.assertIsNotNone(orchestrator.IntentType)
        self.assertIsNotNone(orchestrator.IntentClassificationResult)
        self.assertIsNotNone(orchestrator.classify_intent)

    def test_chat_method_references_intent_classifier(self) -> None:
        """chat() 方法源码应引用 classify_intent（验证集成点存在）。"""
        import inspect

        from teage_liu.orchestrator.chat_handler import ChatHandler

        chat_source = inspect.getsource(ChatHandler.chat)
        self.assertIn("classify_intent", chat_source)
        self.assertIn("intent_result", chat_source)

    def test_chat_stream_method_references_intent_classifier(self) -> None:
        """chat_stream() 方法源码应引用 classify_intent（验证集成点存在）。"""
        import inspect

        from teage_liu.orchestrator.chat_handler import ChatHandler

        chat_stream_source = inspect.getsource(ChatHandler.chat_stream)
        self.assertIn("classify_intent", chat_stream_source)
        self.assertIn("intent_result", chat_stream_source)

    def test_orchestrator_has_current_intent_result_attr(self) -> None:
        """P1 修复：Orchestrator 实例应有 _current_intent_result 属性，默认 None。"""
        from teage_liu.orchestrator import Orchestrator

        # 检查类定义中包含 _current_intent_result 初始化
        import inspect
        init_source = inspect.getsource(Orchestrator.__init__)
        self.assertIn("_current_intent_result", init_source)

    def test_chat_method_persists_intent_result(self) -> None:
        """P1 修复：chat() 方法应将 intent_result 保存到 self._current_intent_result。"""
        import inspect

        from teage_liu.orchestrator.chat_handler import ChatHandler

        chat_source = inspect.getsource(ChatHandler.chat)
        self.assertIn("orch._current_intent_result", chat_source)

    def test_chat_stream_method_persists_intent_result(self) -> None:
        """P1 修复：chat_stream() 方法应将 intent_result 保存到 self._current_intent_result。"""
        import inspect

        from teage_liu.orchestrator.chat_handler import ChatHandler

        chat_stream_source = inspect.getsource(ChatHandler.chat_stream)
        self.assertIn("orch._current_intent_result", chat_stream_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
