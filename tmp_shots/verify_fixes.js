// 验证修复后的 4 个场景：safeParseJSON 复用 / on_failure 填充 / base64 降级 / CSS margin
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8000';

async function main() {
  let browser;
  try {
    try {
      browser = await chromium.launch({ headless: true });
    } catch (e) {
      browser = await chromium.launch({ headless: true, channel: 'chrome' });
    }

    // ========== 场景 1：/workflow?import= 正常工作（验证 safeParseJSON 修改无回归） ==========
    {
      const page = await browser.newPage();
      const spec = { name: '修复验证', steps: [{ id: 's1', name: 'step1', type: 'llm', config: { prompt: 'hi' } }] };
      const json = JSON.stringify(spec);
      const encoded = encodeURIComponent(btoa(unescape(encodeURIComponent(json))));
      await page.goto(`${BASE}/workflow?import=${encoded}`);
      await page.waitForTimeout(1000);
      const resultText = await page.textContent('#workflowValidateResult');
      const stepCount = await page.locator('.step-card').count();
      console.log(`场景1 import=: result="${resultText}", steps=${stepCount}`);
      if (!resultText || !resultText.includes('已导入')) throw new Error('场景1 失败：导入未成功');
      if (stepCount !== 1) throw new Error(`场景1 失败：step 数量 ${stepCount} != 1`);
      console.log('场景1 ✅');
      await page.close();
    }

    // ========== 场景 2：/workflow?import_key= localStorage 降级（验证降级路径） ==========
    {
      const page = await browser.newPage();
      const spec = { name: '降级测试', steps: [
        { id: 's1', name: 'fetch', type: 'tool', config: { tool: 'web_search' } },
        { id: 's2', name: 'summarize', type: 'llm', config: { prompt: '总结' }, depends_on: ['s1'] }
      ]};
      const json = JSON.stringify(spec);
      const storageKey = 'wf_import_test123';
      await page.goto(`${BASE}/workflow`);
      await page.evaluate(({ key, data }) => localStorage.setItem(key, data), { key: storageKey, data: json });
      await page.goto(`${BASE}/workflow?import_key=${storageKey}`);
      await page.waitForTimeout(1000);
      const resultText = await page.textContent('#workflowValidateResult');
      const stepCount = await page.locator('.step-card').count();
      console.log(`场景2 import_key=: result="${resultText}", steps=${stepCount}`);
      if (!resultText || !resultText.includes('已导入')) throw new Error('场景2 失败：降级导入未成功');
      if (stepCount !== 2) throw new Error(`场景2 失败：step 数量 ${stepCount} != 2`);
      // 验证 localStorage 被清理
      const remaining = await page.evaluate((key) => localStorage.getItem(key), storageKey);
      if (remaining !== null) throw new Error('场景2 失败：localStorage 未被清理');
      console.log('场景2 ✅');
      await page.close();
    }

    // ========== 场景 3：/scheduler?new_workflow= 含 on_failure 字段（验证 on_failure 填充） ==========
    {
      const page = await browser.newPage();
      const spec = {
        name: 'on_failure 测试',
        steps: [{
          id: 's1', name: 'retry step', type: 'tool',
          config: { tool: 'web_search' },
          on_failure: { action: 'retry' }
        }]
      };
      const json = JSON.stringify(spec);
      const encoded = encodeURIComponent(btoa(unescape(encodeURIComponent(json))));
      await page.goto(`${BASE}/scheduler?new_workflow=${encoded}`);
      await page.waitForTimeout(1500);
      const modalVisible = await page.isVisible('#scheduleModal');
      if (!modalVisible) throw new Error('场景3 失败：弹窗未打开');
      // 检查 multi radio 是否选中
      const multiChecked = await page.isChecked('input[name="schedWfMode"][value="multi"]');
      if (!multiChecked) throw new Error('场景3 失败：multi 模式未激活');
      // 检查 on_failure select 值
      const onFailureValue = await page.inputValue('.sched-wf-step-onfailure-select');
      console.log(`场景3 on_failure=: "${onFailureValue}"`);
      if (onFailureValue !== 'retry') throw new Error(`场景3 失败：on_failure=${onFailureValue} != retry`);
      console.log('场景3 ✅');
      await page.close();
    }

    // ========== 场景 4：/scheduler?new_workflow_key= localStorage 降级 ==========
    {
      const page = await browser.newPage();
      const spec = { name: 'scheduler 降级', steps: [{ id: 's1', name: 'step', type: 'llm', config: {} }] };
      const json = JSON.stringify(spec);
      const storageKey = 'wf_new_test456';
      await page.goto(`${BASE}/scheduler`);
      await page.evaluate(({ key, data }) => localStorage.setItem(key, data), { key: storageKey, data: json });
      await page.goto(`${BASE}/scheduler?new_workflow_key=${storageKey}`);
      await page.waitForTimeout(1500);
      const modalVisible = await page.isVisible('#scheduleModal');
      if (!modalVisible) throw new Error('场景4 失败：弹窗未打开');
      const multiChecked = await page.isChecked('input[name="schedWfMode"][value="multi"]');
      if (!multiChecked) throw new Error('场景4 失败：multi 模式未激活');
      const stepCount = await page.locator('.sched-wf-step').count();
      if (stepCount !== 1) throw new Error(`场景4 失败：step 数量 ${stepCount} != 1`);
      console.log('场景4 ✅');
      await page.close();
    }

    // ========== 场景 5：chat 卡片渲染（验证 safeParseJSON 修改无回归） ==========
    {
      const page = await browser.newPage();
      await page.goto(`${BASE}/chat`);
      await page.waitForTimeout(2000);
      // 注入 mock cron_propose 工具结果
      const mockResult = JSON.stringify({
        proposal_id: 'fix-test-001',
        status: 'pending_confirm',
        schedule_id: null
      });
      const mockInput = {
        schedule_config: {
          name: '修复验证调度',
          cron: '0 9 * * *',
          task: '测试',
          workflow: {
            name: '修复验证',
            steps: [
              { id: 'fetch', name: '抓取', type: 'tool', config: { tool: 'web_search' } },
              { id: 'summarize', name: '总结', type: 'llm', config: { prompt: '总结' } }
            ]
          }
        },
        llm_explanation: '修复后的验证测试',
        requested_tools: ['web_search']
      };
      const cardHtml = await page.evaluate(({ result, input }) => {
        const msgEl = document.createElement('div');
        const toolEvent = { name: 'cron_propose', input, result, tool_use_id: 'test-fix-001' };
        if (typeof appendToolCard === 'function') {
          appendToolCard(msgEl, toolEvent);
          return msgEl.innerHTML;
        }
        return null;
      }, { result: mockResult, input: mockInput });

      if (!cardHtml) throw new Error('场景5 失败：appendToolCard 未执行');
      if (!cardHtml.includes('workflow-propose-card')) throw new Error('场景5 失败：卡片未渲染');
      if (!cardHtml.includes('fix-test-001')) throw new Error('场景5 失败：proposal_id 未显示');
      if (!cardHtml.includes('提议调度 0 9 * * *')) throw new Error('场景5 失败：cron 未显示');
      if (!cardHtml.includes('在编辑器打开')) throw new Error('场景5 失败：编辑器按钮缺失');
      console.log('场景5 ✅ safeParseJSON 复用无回归');
      await page.close();
    }

    // ========== 场景 6：CSS margin-top 验证 ==========
    {
      const page = await browser.newPage();
      await page.goto(`${BASE}/chat`);
      await page.waitForTimeout(1000);
      const marginTop = await page.evaluate(() => {
        const el = document.createElement('div');
        el.className = 'workflow-propose-card-actions';
        document.body.appendChild(el);
        const style = window.getComputedStyle(el);
        const mt = style.marginTop;
        el.remove();
        return mt;
      });
      console.log(`场景6 margin-top="${marginTop}"`);
      if (marginTop === '0px') throw new Error('场景6 失败：margin-top 未生效');
      console.log('场景6 ✅');
      await page.close();
    }

    console.log('\n===== 全部 6 个场景通过 =====');
  } catch (e) {
    console.error('验证失败:', e.message);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
