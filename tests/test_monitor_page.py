from playwright.sync_api import sync_playwright
import json

errors = []
console_msgs = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()

    page.on("console", lambda msg: console_msgs.append(f"[{msg.type}] {msg.text}"))
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.goto("http://127.0.0.1:8000/monitor", wait_until="networkidle", timeout=15000)
    page.wait_for_timeout(2000)

    screenshot_path = "e:/Java/webser/web_app/webme/teage-liu/tests/monitor_screenshot.png"
    page.screenshot(path=screenshot_path, full_page=True)

    health_cells = page.locator(".health-cell").count()
    metric_cards = page.locator(".metric-card").count()
    canvases = page.locator("canvas").count()
    audit_entries = page.locator(".audit-entry").count()
    run_entries = page.locator(".run-entry").count()

    overall_status = page.locator("#overallStatus").inner_text()
    health_ok = page.locator("#healthOk").inner_text()
    health_warn = page.locator("#healthWarn").inner_text()
    health_crit = page.locator("#healthCrit").inner_text()
    health_total = page.locator("#healthTotal").inner_text()
    llm_calls = page.locator("#llmCallsTotal").inner_text()
    last_update = page.locator("#lastUpdate").inner_text()

    print("=== 监控页面验证结果 ===")
    print(f"健康组件格子数: {health_cells}")
    print(f"性能指标卡片数: {metric_cards}")
    print(f"Canvas 数: {canvases}")
    print(f"审计日志条数: {audit_entries}")
    print(f"调度运行条数: {run_entries}")
    print(f"总体状态: {overall_status}")
    print(f"健康: ok={health_ok} warn={health_warn} crit={health_crit} total={health_total}")
    print(f"LLM 调用次数: {llm_calls}")
    print(f"最后更新: {last_update}")
    print(f"\n控制台消息数: {len(console_msgs)}")
    for m in console_msgs[:10]:
        print(f"  {m}")
    print(f"\nJS 错误数: {len(errors)}")
    for e in errors[:5]:
        print(f"  {e}")

    page.screenshot(path="e:/Java/webser/web_app/webme/teage-liu/tests/monitor_light.png", full_page=False)

    browser.close()

print("\n截图已保存到 tests/monitor_screenshot.png")
