"""SessionStore 专项测试(E7,计划 §4.3)。

会话级状态容器(session_id → dict 懒加载);枝干共享实例但可变状态
只能放 ctx.extra(对话态)/ SessionStore(会话态);关闭时清空。
"""

from __future__ import annotations

from teage_liu2.core.session import SessionStore


def test_lazy_get_and_shared_dict():
    """验收:懒加载 —— 首次 get 创建空 dict,同一会话返回同一 dict。"""
    ss = SessionStore()
    assert ss.get("s1") == {}
    d = ss.get("s1")
    d["k"] = 1
    assert ss.get("s1")["k"] == 1  # 同一 dict


def test_drop_and_clear():
    """验收:drop 删除会话;clear 清空全部。"""
    ss = SessionStore()
    ss.get("s1")["k"] = 1
    ss.get("s2")["k"] = 2

    ss.drop("s1")
    assert ss.get("s1") == {}  # 重建为空
    assert ss.get("s2")["k"] == 2

    ss.clear()
    assert ss.get("s2") == {}  # 清空后重建为空(无残留)


def test_sessions_isolated():
    """验收:两会话互不干扰(并发契约的容器侧保证)。"""
    ss = SessionStore()
    ss.get("sA")["x"] = "A"
    ss.get("sB")["x"] = "B"
    assert ss.get("sA")["x"] == "A"
    assert ss.get("sB")["x"] == "B"
