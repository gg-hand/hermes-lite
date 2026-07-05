"""验证 chat 页面滚动 bug 修复（min-height:0 + scrollMessagesToBottom force 参数）。

复现用户报告的场景：
  1. 长会话中展开工具卡片详情 → 之前"页面变短、滑轮划不动"
  2. 会话结束/流式更新时 → 之前滚动条被强制拉到底部打断浏览

验证点：
  A. .messages computed min-height === '0px'（flex 滚动容器修复）
  B. .message 有 flex-shrink: 0（防御性收缩）
  C. scrollTop 可正常被设置（滚动功能未失效）
  D. 内容高度超出视口时 .messages 是真正的滚动容器（scrollHeight > clientHeight）
  E. 工具卡片 <details> 展开后 scrollTop 仍可自由滚动
  F. 用户向上滚动浏览历史时，调用 scrollMessagesToBottom() 不应强制拉回底部
  G. scrollMessagesToBottom(true) 强制模式应拉到底部
"""
import json
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8000/chat"

# 注入大量消息 + 工具卡片，模拟长会话
INJECT_SCRIPT = r"""
() => {
  const msgs = document.getElementById('messages');
  if (!msgs) return { error: 'no #messages' };
  // 清空欢迎屏
  const welcome = msgs.querySelector('.welcome-screen');
  if (welcome) welcome.remove();
  // 注入 40 条长消息
  for (let i = 1; i <= 40; i++) {
    const m = document.createElement('div');
    m.className = 'message ' + (i % 2 ? 'user' : 'assistant');
    const role = (i % 2 ? 'user' : 'assistant');
    m.innerHTML = `
      <div class="message-role ${role}">${role}</div>
      <div class="message-bubble markdown">
        <p>消息 #${i}：这是一段用于测试滚动的较长内容。${'内容填充 '.repeat(8)}</p>
      </div>`;
    msgs.appendChild(m);
  }
  // 注入一个工具卡片（可展开的 details）
  const tm = document.createElement('div');
  tm.className = 'message assistant';
  tm.innerHTML = `
    <div class="message-role assistant">Assistant</div>
    <details class="tool-card" data-tool-name="file_read">
      <summary class="tool-card-header">
        <span class="tool-card-name">🔧 file_read</span>
        <span class="tool-card-action">读取 config.yaml</span>
        <span class="tool-card-status is-done">✓ 完成</span>
      </summary>
      <div class="tool-card-body">
        <div class="tool-card-section">
          <div class="tool-card-label">结果</div>
          <pre class="tool-card-content">${'line of file content\n'.repeat(30)}</pre>
        </div>
      </div>
    </details>`;
  msgs.appendChild(tm);
  return { injected: true, childCount: msgs.children.length };
}
"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#messages", timeout=10000)

        results = {}

        # A. .messages computed min-height
        mh = page.evaluate(
            "() => { const el = document.getElementById('messages');"
            " return window.getComputedStyle(el).minHeight; }"
        )
        results["A.min_height"] = mh
        assert mh == "0px", f"FAIL A: .messages min-height 应为 0px，实际 {mh}"

        # B. .message flex-shrink
        fs = page.evaluate(
            "() => { const el = document.querySelector('.message');"
            " return el ? window.getComputedStyle(el).flexShrink : 'no .message'; }"
        )
        results["B.flex_shrink"] = fs
        # 注入前页面可能无消息，先注入
        if fs == "0":
            pass
        else:
            # 先注入再测
            page.evaluate(INJECT_SCRIPT)
            fs = page.evaluate(
                "() => { const el = document.querySelector('.message');"
                " return el ? window.getComputedStyle(el).flexShrink : 'none'; }"
            )
            results["B.flex_shrink_after_inject"] = fs
        assert fs == "0", f"FAIL B: .message flex-shrink 应为 0，实际 {fs}"

        # 注入大量消息（如尚未注入）
        if not page.evaluate("() => document.querySelectorAll('#messages .message').length > 0"):
            inj = page.evaluate(INJECT_SCRIPT)
            results["inject"] = inj
        else:
            results["inject"] = "already"

        # D. 滚动容器：内容应超出视口
        dims = page.evaluate(
            "() => { const el = document.getElementById('messages');"
            " return { sh: el.scrollHeight, ch: el.clientHeight, st: el.scrollTop }; }"
        )
        results["D.dims"] = dims
        assert dims["sh"] > dims["ch"], (
            f"FAIL D: 内容应超出视口（scrollHeight {dims['sh']} > clientHeight {dims['ch']}）"
        )

        # C. scrollTop 可被设置到顶部
        page.evaluate("() => document.getElementById('messages').scrollTop = 0")
        st_top = page.evaluate("() => document.getElementById('messages').scrollTop")
        results["C.scroll_to_top"] = st_top
        assert st_top == 0, f"FAIL C: scrollTop 设为 0 后实际 {st_top}（滚动失效）"

        # 滚回底部
        page.evaluate(
            "() => { const el = document.getElementById('messages'); el.scrollTop = el.scrollHeight; }"
        )
        st_bot = page.evaluate("() => document.getElementById('messages').scrollTop")
        results["C.scroll_to_bottom"] = st_bot
        assert st_bot > 0, "FAIL C: 无法滚动到底部"

        # E. 工具卡片展开后，scrollTop 仍可自由滚动
        # 先滚到中间
        page.evaluate(
            "() => { const el = document.getElementById('messages');"
            " el.scrollTop = Math.floor(el.scrollHeight / 2); }"
        )
        mid_before = page.evaluate("() => document.getElementById('messages').scrollTop")
        # 展开工具卡片
        page.evaluate(
            "() => { const d = document.querySelector('#messages details.tool-card');"
            " if (d) d.open = true; }"
        )
        page.wait_for_timeout(300)
        # 尝试再次设置 scrollTop 到顶部
        page.evaluate("() => document.getElementById('messages').scrollTop = 0")
        st_after_expand = page.evaluate("() => document.getElementById('messages').scrollTop")
        results["E.after_expand_scroll_to_top"] = st_after_expand
        results["E.mid_before_expand"] = mid_before
        assert st_after_expand == 0, (
            f"FAIL E: 展开工具卡片后 scrollTop 无法回到顶部（实际 {st_after_expand}），"
            "这正是用户报告的'展开工具详情后滑轮划不动'bug"
        )

        # F. 用户向上浏览历史时，非强制 scrollMessagesToBottom() 不应拉回底部
        # 滚到中间位置模拟用户在看历史
        page.evaluate(
            "() => { const el = document.getElementById('messages');"
            " el.scrollTop = Math.floor(el.scrollHeight * 0.3); }"
        )
        user_pos = page.evaluate("() => document.getElementById('messages').scrollTop")
        # 调用非强制滚动（模拟流式更新/工具卡片创建触发的内部调用）
        page.evaluate("() => window.HermesUtils.scrollMessagesToBottom(false)")
        page.wait_for_timeout(300)
        after_non_force = page.evaluate("() => document.getElementById('messages').scrollTop")
        results["F.user_pos"] = user_pos
        results["F.after_non_force"] = after_non_force
        # 用户位置应保持（允许少量偏差，因为 80px 阈值内会跟随）
        # user_pos 在 30% 位置，距底部应该 > 80px，所以不应被拉回
        scroll_height = page.evaluate("() => document.getElementById('messages').scrollHeight")
        client_height = page.evaluate("() => document.getElementById('messages').clientHeight")
        dist_to_bottom = scroll_height - user_pos - client_height
        results["F.dist_to_bottom"] = dist_to_bottom
        if dist_to_bottom > 80:
            assert after_non_force == user_pos, (
                f"FAIL F: 用户在看历史（距底部 {dist_to_bottom}px > 80px），"
                f"非强制滚动不应拉回底部（用户位置 {user_pos}，调用后 {after_non_force}）"
            )

        # G. 强制模式应拉到底部
        page.evaluate("() => window.HermesUtils.scrollMessagesToBottom(true)")
        page.wait_for_timeout(300)
        after_force = page.evaluate("() => document.getElementById('messages').scrollTop")
        sh = page.evaluate("() => document.getElementById('messages').scrollHeight")
        ch = page.evaluate("() => document.getElementById('messages').clientHeight")
        results["G.after_force"] = after_force
        results["G.expected_bottom"] = sh - ch
        assert after_force == sh - ch, (
            f"FAIL G: 强制滚动应到底部（期望 {sh - ch}，实际 {after_force}）"
        )

        # H. isMessagesNearBottom 工具函数应正确判断
        # 在底部附近时应返回 true
        near_bot = page.evaluate("() => window.HermesUtils.isMessagesNearBottom()")
        results["H.near_bottom_true"] = near_bot
        assert near_bot is True, f"FAIL H: 在底部附近时 isMessagesNearBottom 应返回 true，实际 {near_bot}"
        # 滚到中间后应返回 false
        page.evaluate(
            "() => { const el = document.getElementById('messages');"
            " el.scrollTop = Math.floor(el.scrollHeight * 0.3); }"
        )
        near_mid = page.evaluate("() => window.HermesUtils.isMessagesNearBottom()")
        results["H.near_bottom_false"] = near_mid
        assert near_mid is False, f"FAIL H: 在中间位置时 isMessagesNearBottom 应返回 false，实际 {near_mid}"

        browser.close()
        print("\n=== 验证结果 ===")
        for k, v in results.items():
            print(f"  {k}: {v}")
        print("\n[PASS] 所有验证点通过")


if __name__ == "__main__":
    main()
