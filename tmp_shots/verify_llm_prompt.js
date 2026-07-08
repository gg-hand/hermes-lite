const { chromium } = require('playwright');

(async () => {
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    browser = await chromium.launch({ headless: true, channel: 'chrome' });
  }
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });

  console.log('=== LLM 引导测试：用户说"每天早上 9 点给我发今日新闻简报" ===');
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:8000/chat', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // 直接使用已知的输入框和发送按钮
  const inputSelector = 'messageInput';
  const sendBtn = 'sendBtn';
  console.log('使用输入框 ID:', inputSelector, '发送按钮 ID:', sendBtn);

  if (inputSelector) {
    // 输入消息
    await page.fill(`#${inputSelector}`, '每天早上 9 点给我发今日新闻简报');
    await page.waitForTimeout(300);

    if (sendBtn) {
      console.log('点击发送按钮:', sendBtn);
      await page.click(`#${sendBtn}`);

      // 等待 LLM 响应（最多 45 秒）
      console.log('等待 LLM 响应...');
      try {
        await page.waitForSelector('.workflow-propose-card, .tool-card[data-tool-name="cron_propose"]', { timeout: 45000 });
        console.log('✅ 检测到 cron_propose 响应！');
        // 再等 10 秒让 toolResult 返回和专属卡片渲染
        await page.waitForTimeout(10000);

        // 检查所有卡片状态
        const cardInfo = await page.evaluate(() => {
          const wfCard = document.querySelector('.workflow-propose-card');
          const toolCards = document.querySelectorAll('.tool-card[data-tool-name="cron_propose"]');
          const allToolCards = document.querySelectorAll('.tool-card');
          return {
            hasWorkflowCard: !!wfCard,
            workflowCardHeader: wfCard ? wfCard.querySelector('.workflow-propose-card-header')?.textContent?.trim() : null,
            cronProposeToolCardCount: toolCards.length,
            allToolCardCount: allToolCards.length,
            allToolNames: Array.from(allToolCards).map(c => c.dataset.toolName),
            cronProposeCardStatus: toolCards.length > 0 ? toolCards[0].querySelector('.tool-card-status')?.textContent?.trim() : null,
            cronProposeCardResult: toolCards.length > 0 ? toolCards[0].querySelector('.tool-card-result pre')?.textContent?.substring(0, 300) : null
          };
        });
        console.log('卡片信息:', JSON.stringify(cardInfo, null, 2));
      } catch (e) {
        console.log('⚠️ 45 秒内未检测到 cron_propose 卡片');
        const responseInfo = await page.evaluate(() => {
          const messages = document.querySelectorAll('.message');
          const lastMsg = messages[messages.length - 1];
          if (!lastMsg) return { msgCount: messages.length, lastMsg: '(无)' };
          const bubble = lastMsg.querySelector('.message-bubble, .bubble-content');
          const text = bubble ? bubble.textContent.trim().substring(0, 200) : '(无 bubble)';
          const toolCards = lastMsg.querySelectorAll('.tool-card');
          const toolNames = Array.from(toolCards).map(c => c.dataset.toolName);
          return { msgCount: messages.length, lastMsgText: text, toolNames };
        });
        console.log('响应信息:', JSON.stringify(responseInfo, null, 2));
      }

      await page.screenshot({ path: 'tmp_shots/llm-prompt-test.png', fullPage: true });
    } else {
      console.log('❌ 未找到发送按钮');
    }
  } else {
    console.log('❌ 未找到输入框');
  }

  await browser.close();
  console.log('\n=== 测试完成 ===');
})();
