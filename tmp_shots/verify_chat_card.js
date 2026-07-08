const { chromium } = require('playwright');

(async () => {
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    browser = await chromium.launch({ headless: true, channel: 'chrome' });
  }
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });

  console.log('=== 场景 4: chat 卡片渲染 cron_propose 结果 ===');
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:8000/chat', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // 在页面中模拟 cron_propose 工具结果
  const cardResult = await page.evaluate(() => {
    // 找到 messages 容器
    const messagesEl = document.getElementById('messages') || document.querySelector('.messages');
    if (!messagesEl) return { error: '未找到 messages 容器' };

    // 创建一个 assistant 消息容器
    const msgEl = document.createElement('div');
    msgEl.className = 'message assistant';
    messagesEl.appendChild(msgEl);

    // 构造 mock cron_propose toolEvent
    const workflowSpec = {
      name: 'daily-news',
      steps: [
        { id: 'fetch', name: '抓取新闻', type: 'tool', config: { tool: 'web_fetch' } },
        { id: 'summarize', name: '总结简报', type: 'llm', config: { prompt: '总结新闻' }, depends_on: ['fetch'] }
      ]
    };
    const toolEvent = {
      name: 'cron_propose',
      input: {
        schedule_config: {
          cron: '0 9 * * *',
          task: '每天 9 点发送新闻简报',
          workflow: workflowSpec
        },
        requested_tools: ['web_fetch', 'file_write'],
        llm_explanation: '根据您的需求，我提议创建一个每天早上 9 点执行的新闻简报 workflow：先抓取新闻，再用 LLM 总结成简报。'
      },
      result: JSON.stringify({
        proposal_id: 'prop_test_001',
        status: 'pending_confirm'
      }),
      is_error: false,
      tool_use_id: 'test_tool_use_001'
    };

    // 调用 appendToolCard
    if (typeof appendToolCard !== 'function') {
      return { error: 'appendToolCard 函数不可用' };
    }
    appendToolCard(msgEl, toolEvent);

    // 检查卡片渲染
    const card = msgEl.querySelector('.workflow-propose-card');
    if (!card) return { error: '未渲染 .workflow-propose-card' };

    const header = card.querySelector('.workflow-propose-card-header');
    const headerText = header ? header.textContent : '';
    const statusBadge = card.querySelector('.workflow-propose-card-status');
    const statusText = statusBadge ? statusBadge.textContent : '';
    const statusCls = statusBadge ? statusBadge.className : '';
    const explanation = card.querySelector('.workflow-propose-card-explanation');
    const explanationText = explanation ? explanation.textContent.trim() : '';
    const steps = card.querySelectorAll('.preview-step');
    const stepNames = Array.from(steps).map(s => {
      const name = s.querySelector('.preview-step-name');
      const meta = s.querySelector('.preview-step-meta');
      return { name: name ? name.textContent : '', type: meta ? meta.textContent : '' };
    });
    const arrows = card.querySelectorAll('.preview-arrow');
    const proposalIdEl = card.querySelector('code');
    const proposalId = proposalIdEl ? proposalIdEl.textContent : '';
    const actions = card.querySelector('.workflow-propose-card-actions');
    const confirmLink = actions ? actions.querySelector('a[href*="proposal_id"]') : null;
    const confirmHref = confirmLink ? confirmLink.getAttribute('href') : '';
    const editorLink = actions ? actions.querySelector('a[href*="import"]') : null;
    const editorHref = editorLink ? editorLink.getAttribute('href') : '';

    return {
      success: true,
      headerText: headerText.trim(),
      statusText,
      statusCls,
      explanationPreview: explanationText.substring(0, 80),
      stepCount: steps.length,
      stepNames,
      arrowCount: arrows.length,
      proposalId,
      confirmHref,
      editorHref,
      editorHrefStartsWith: editorHref ? editorHref.substring(0, 30) : ''
    };
  });

  console.log('卡片渲染结果:', JSON.stringify(cardResult, null, 2));
  await page.screenshot({ path: 'tmp_shots/chat-workflow-card.png', fullPage: true });

  // 验证 describeToolAction
  console.log('\n=== 场景 5: describeToolAction cron_* 分支 ===');
  const descResult = await page.evaluate(() => {
    if (typeof describeToolAction !== 'function') return { error: 'describeToolAction 不可用' };
    return {
      cron_list: describeToolAction('cron_list', {}),
      cron_propose: describeToolAction('cron_propose', { schedule_config: { cron: '0 9 * * *' } }),
      cron_create: describeToolAction('cron_create', { proposal_id: 'prop_123' }),
      cron_update: describeToolAction('cron_update', { schedule_id: 'sched_456' }),
      cron_tool_create: describeToolAction('cron_tool_create', {})
    };
  });
  console.log('describeToolAction 结果:', JSON.stringify(descResult, null, 2));

  await browser.close();
  console.log('\n=== 验证完成 ===');
})();
