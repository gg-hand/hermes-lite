"""行为套件 runner 的断言能力与落盘记录契约。

来源：2026-09-10 深度审计 F-1（runner 曾静默忽略 `persisted` / `isolation` 断言，
形成"假绿"）。本文件锁定修复后的行为：未实现的断言键必须显式失败、
落盘调用必须可观测（双档可区分）、persisted / isolation 断言必须真实生效。
"""
from __future__ import annotations

import importlib.util
import pathlib

_RUNNER = (
    pathlib.Path(__file__).resolve().parents[1] / "PROTOCOL" / "behavior-suite" / "runner.py"
)
_spec = importlib.util.spec_from_file_location("bs_runner_under_test", _RUNNER)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


# ---------------------------------------------------------------------------
# 断言能力守卫(T0.2)
# ---------------------------------------------------------------------------
def test_unknown_final_key_fails_fast():
    problems = runner._check_supported({"final": {"persisted_x": {}}})
    assert problems and "persisted_x" in problems[0]


def test_unknown_invocation_key_fails_fast():
    problems = runner._check_supported(
        {"invocations": [{"hook": "before", "extension": "e", "isolation_x": {}}]}
    )
    assert problems and "isolation_x" in problems[0]


def test_unknown_top_and_event_key_fail_fast():
    assert runner._check_supported({"nope": 1})
    assert runner._check_supported({"event_stream": [{"type": "done", "payload_x": 1}]})


def test_known_keys_pass():
    assert runner._check_supported({
        "final": {"termination_reason": "normal", "is_complete": True, "persisted": {}},
        "invocations": [
            {"hook": "before", "extension": "e", "order": 1, "isolation": {"error": True}}
        ],
        "event_stream": [{"type": "done", "absent": True}],
        "error_codes": {"must_include": ["HOOK_EXCEPTION"]},
    }) == []


# ---------------------------------------------------------------------------
# 落盘记录(T1.1)
# ---------------------------------------------------------------------------
def test_ephemeral_records_flush_and_background_channels():
    store = runner._EphemeralStorage()
    try:
        store.ensure_session("s-x")
        store.log_message("s-x", "user", "输入")
        store.log_message_buffered("s-x", "assistant", "回复", message_type="assistant")
    finally:
        store.close()
    assert store.sessions == ["s-x"]
    assert [r["channel"] for r in store.records] == ["direct", "buffered"]
    assert [r["role"] for r in store.records] == ["user", "assistant"]
    assert store.records[1]["message_type"] == "assistant"


# ---------------------------------------------------------------------------
# persisted 断言(T1.2)
# ---------------------------------------------------------------------------
def test_persisted_view_and_assertion():
    store = runner._EphemeralStorage()
    try:
        store.log_message("s", "user", "hi")
        store.log_message_buffered("s", "assistant", "ok", message_type="assistant")
        view = runner._persisted_view(store.records)
    finally:
        store.close()
    assert view["roles"] == ["user", "assistant"]
    assert view["channels"] == ["direct", "buffered"]
    assert view["count"] == 2
    assert view["text_blob"] == "hi\nok"

    errs = runner._assert_final({"persisted": {"roles": ["user"]}}, {"type": "done"}, [], view)
    assert errs and "persisted" in errs[0]
    assert runner._assert_final(
        {"persisted": {"roles": ["user", "assistant"]}}, {"type": "done"}, [], view
    ) == []


def test_persisted_assertion_without_view_fails():
    errs = runner._assert_final({"persisted": {"count": 1}}, {"type": "done"}, [], None)
    assert errs and "不支持" in errs[0]


# ---------------------------------------------------------------------------
# isolation 断言(T1.4)
# ---------------------------------------------------------------------------
def test_isolation_assertion_requires_raised_extension():
    expected = [{"hook": "before", "extension": "bad", "isolation": {"error": True}}]
    assert runner._assert_invocations(expected, [])          # 未抛错 → 失败
    assert runner._assert_invocations(expected, [], ["bad"]) == []


# ---------------------------------------------------------------------------
# 错误码断言(T2.1)
# ---------------------------------------------------------------------------
def test_extract_codes_and_error_codes_assertion():
    assert runner._extract_codes("HOOK_TIMEOUT: 枝干 x 超时") == ["HOOK_TIMEOUT"]
    assert runner._extract_codes("普通日志没有任何码") == []

    errs = runner._assert_error_codes({"must_include": ["HOOK_EXCEPTION"]}, ["TOOL_NO_EXECUTOR"], [])
    assert errs and "HOOK_EXCEPTION" in errs[0]
    assert runner._assert_error_codes({"must_include": ["TOOL_NO_EXECUTOR"]}, [], ["TOOL_NO_EXECUTOR"]) == []
    assert runner._assert_error_codes({"must_exclude": ["HOOK_EXCEPTION"]}, [], ["HOOK_EXCEPTION"])
