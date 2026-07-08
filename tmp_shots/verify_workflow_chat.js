const { chromium } = require('playwright');

(async () => {
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    // 降级到系统 Chrome
    browser = await chromium.launch({ headless: true, channel: 'chrome' });
  }
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });

  // 测试用 workflow spec
  const spec = {
    name: 'test-news-workflow',
    steps: [
      { id: 'fetch', name: '抓取新闻', type: 'tool', config: { tool: 'web_fetch' } },
      { id: 'summarize', name: '总结简报', type: 'llm', config: { prompt: '总结新闻' }, depends_on: ['fetch'] }
    ]
  };
  const json = JSON.stringify(spec);
  const b64 = Buffer.from(json, 'utf8').toString('base64');
  const encoded = encodeURIComponent(b64);

  console.log('=== 场景 1: /workflow?import= 一键导入 ===');
  const page1 = await context.newPage();
  await page1.goto(`http://127.0.0.1:8000/workflow?import=${encoded}`, { waitUntil: 'networkidle' });
  await page1.waitForTimeout(1500);

  // 检查编辑器是否加载了 spec
  const importResult = await page1.evaluate(() => {
    const validateBox = document.querySelector('.workflow-validate-result');
    const validateText = validateBox ? validateBox.textContent : '(无验证结果)';
    // 检查 step 是否加载
    const stepItems = document.querySelectorAll('.editor-step-item, .step-card, [data-step-idx]');
    const yamlArea = document.querySelector('#yamlArea, .yaml-input, textarea');
    const yamlValue = yamlArea ? yamlArea.value.substring(0, 200) : '(无 YAML)';
    return {
      validateText,
      stepCount: stepItems.length,
      yamlPreview: yamlValue
    };
  });
  console.log('验证结果:', importResult.validateText);
  console.log('step 数量:', importResult.stepCount);
  console.log('YAML 预览:', importResult.yamlPreview);
  await page1.screenshot({ path: 'tmp_shots/workflow-import.png', fullPage: true });

  console.log('\n=== 场景 2: /scheduler?new_workflow= 打开新建弹窗预填 ===');
  const page2 = await context.newPage();
  await page2.goto(`http://127.0.0.1:8000/scheduler?new_workflow=${encoded}`, { waitUntil: 'networkidle' });
  await page2.waitForTimeout(2000); // 等待 setTimeout 100ms + 弹窗动画

  const modalResult = await page2.evaluate(() => {
    const modal = document.getElementById('scheduleModal');
    const isVisible = modal ? (modal.classList.contains('show') || modal.style.display !== 'none') : false;
    // 检查 workflow 多步模式是否激活
    const multiRadio = document.querySelector('input[name="schedWfMode"][value="multi"]');
    const multiChecked = multiRadio ? multiRadio.checked : false;
    // 检查 step 是否预填
    const steps = document.querySelectorAll('.sched-wf-step');
    const stepData = Array.from(steps).map(s => {
      const idInput = s.querySelector('.sched-wf-step-id-input');
      const nameInput = s.querySelector('.sched-wf-step-name-input');
      const typeSelect = s.querySelector('.sched-wf-step-type-select');
      return {
        id: idInput ? idInput.value : '',
        name: nameInput ? nameInput.value : '',
        type: typeSelect ? typeSelect.value : ''
      };
    });
    // 检查 schedName 是否预填
    const schedName = document.getElementById('schedName');
    return {
      modalVisible: isVisible,
      multiModeActive: multiChecked,
      stepCount: steps.length,
      stepData,
      schedName: schedName ? schedName.value : ''
    };
  });
  console.log('弹窗可见:', modalResult.modalVisible);
  console.log('多步模式激活:', modalResult.multiModeActive);
  console.log('预填 step 数量:', modalResult.stepCount);
  console.log('step 数据:', JSON.stringify(modalResult.stepData, null, 2));
  console.log('schedule 名称:', modalResult.schedName);
  await page2.screenshot({ path: 'tmp_shots/scheduler-new-workflow.png', fullPage: true });

  console.log('\n=== 场景 3: /scheduler?proposal_id=xxx 自动展开（测试无 proposal 场景）===');
  const page3 = await context.newPage();
  await page3.goto('http://127.0.0.1:8000/scheduler?proposal_id=nonexistent_id', { waitUntil: 'networkidle' });
  await page2.waitForTimeout(2000);
  const proposalResult = await page3.evaluate(() => {
    // 检查是否显示了"未找到提议"的 toast
    const toasts = document.querySelectorAll('.toast, .toast-message');
    const toastTexts = Array.from(toasts).map(t => t.textContent);
    return { toastCount: toasts.length, toastTexts };
  });
  console.log('toast 数量:', proposalResult.toastCount);
  console.log('toast 内容:', proposalResult.toastTexts);

  await browser.close();
  console.log('\n=== 验证完成 ===');
})();
