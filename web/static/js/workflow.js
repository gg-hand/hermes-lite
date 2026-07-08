/* ============================================================
   workflow.js — Workflow 编排页面交互
   三栏：模板库 / 编辑器 / step 预览
   设计原则：易上手平民化，结构化表单优先，YAML 高级模式按需切换
   ============================================================ */

(function () {
  'use strict';

  // ---------- 模板预设：来自共享的 workflow-presets.js ----------
  const WORKFLOW_PRESETS = window.WORKFLOW_PRESETS || [];
  const STEP_TYPES = window.WORKFLOW_STEP_TYPES || ['deterministic', 'llm', 'tool', 'react', 'subworkflow'];

  // ---------- 状态 ----------
  const state = {
    presets: WORKFLOW_PRESETS,
    currentSpec: null,        // { name, timeout_seconds, steps: [...] }
    selectedStepIdx: null,
    advancedMode: false,
    searchQuery: ''
  };

  // ---------- 工具：DOM ----------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    });
    (Array.isArray(children) ? children : [children]).forEach((c) => {
      if (c == null) return;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return node;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ---------- 渲染：模板库 ----------
  function renderTemplates() {
    const groups = { beginner: [], intermediate: [], advanced: [] };
    const q = state.searchQuery.trim().toLowerCase();
    state.presets.forEach((p) => {
      if (!groups[p.difficulty]) return;
      if (q && !(`${p.name} ${p.description} ${p.tags.join(' ')}`.toLowerCase().includes(q))) return;
      groups[p.difficulty].push(p);
    });

    ['beginner', 'intermediate', 'advanced'].forEach((diff) => {
      const container = $(`#templateCards${diff.charAt(0).toUpperCase()}${diff.slice(1)}`);
      const countEl = $(`#${diff}Count`);
      if (!container) return;
      container.innerHTML = '';
      if (countEl) countEl.textContent = groups[diff].length;
      if (groups[diff].length === 0) {
        container.appendChild(el('div', { class: 'template-card-empty text-muted', text: q ? '无匹配模板' : '暂无模板' }));
        return;
      }
      groups[diff].forEach((p) => {
        const card = el('button', {
          class: 'template-card',
          type: 'button',
          onclick: () => loadPreset(p)
        }, [
          el('div', { class: 'template-card-title', text: p.name }),
          el('div', { class: 'template-card-desc', text: p.description }),
          el('div', { class: 'template-card-meta' },
            p.tags.map((t) => el('span', { class: 'template-tag', text: t })))
        ]);
        container.appendChild(card);
      });
    });
  }

  // ---------- 渲染：编辑器 ----------
  function renderEditor() {
    const empty = $('#workflowEmptyState');
    const body = $('#workflowEditorBody');
    if (!state.currentSpec) {
      empty.hidden = false;
      body.hidden = true;
      return;
    }
    empty.hidden = true;
    body.hidden = false;

    if (state.advancedMode) {
      $('#workflowYamlTextarea').hidden = false;
      $('#workflowFormContainer').hidden = true;
      $('#workflowYamlTextarea').value = specToYaml(state.currentSpec);
    } else {
      $('#workflowYamlTextarea').hidden = true;
      $('#workflowFormContainer').hidden = false;
      renderForm();
    }
    renderPreview();
  }

  function renderForm() {
    $('#workflowName').value = state.currentSpec.name || '';
    $('#workflowTimeout').value = state.currentSpec.timeout_seconds || '';
    renderSteps();
  }

  function renderSteps() {
    const container = $('#workflowStepsContainer');
    container.innerHTML = '';
    const steps = state.currentSpec.steps || [];
    if (steps.length === 0) {
      container.appendChild(el('div', { class: 'preview-empty', text: '点击"+ 添加 step"开始配置步骤' }));
      return;
    }
    steps.forEach((step, idx) => {
      container.appendChild(renderStepCard(step, idx));
    });
  }

  function renderStepCard(step, idx) {
    const isSelected = state.selectedStepIdx === idx;
    const card = el('div', { class: `step-card${isSelected ? ' is-selected' : ''}` }, [
      el('div', { class: 'step-card-header', onclick: () => toggleStepSelect(idx) }, [
        el('div', { class: 'step-card-index', text: String(idx + 1) }),
        el('div', { class: 'step-card-title', text: step.name || step.id }),
        el('span', { class: `step-type-badge step-type-${step.type}` }, [step.type]),
        el('div', { class: 'step-card-actions' }, [
          el('button', { class: 'step-icon-btn', title: '上移', onclick: (e) => { e.stopPropagation(); moveStep(idx, -1); } }, ['↑']),
          el('button', { class: 'step-icon-btn', title: '下移', onclick: (e) => { e.stopPropagation(); moveStep(idx, 1); } }, ['↓']),
          el('button', { class: 'step-icon-btn danger', title: '删除', onclick: (e) => { e.stopPropagation(); deleteStep(idx); } }, ['×'])
        ])
      ]),
      isSelected ? renderStepBody(step, idx) : null
    ].filter(Boolean));
    return card;
  }

  function renderStepBody(step, idx) {
    const body = el('div', { class: 'step-card-body' });
    body.appendChild(el('div', { class: 'step-form-grid' }, [
      renderField('step ID', step.id, (v) => { step.id = v; }, { placeholder: 's1', mono: true }),
      renderField('名称', step.name, (v) => { step.name = v; }, { placeholder: '可读名称' }),
      renderSelectField('类型', step.type, STEP_TYPES, (v) => { step.type = v; renderSteps(); }),
      renderField('depends_on（逗号分隔）', (step.depends_on || []).join(','),
        (v) => { step.depends_on = v.split(',').map((s) => s.trim()).filter(Boolean); },
        { placeholder: 's1,s2', mono: true }),
      renderField('condition', step.condition || '',
        (v) => { step.condition = v; },
        { placeholder: 'steps.s1.outputs.count > 0', mono: true, hint: '条件表达式，引用 <code>steps.&lt;id&gt;.outputs.&lt;key&gt;</code>' }),
      renderNumberField('timeout（秒）', step.timeout_seconds,
        (v) => { step.timeout_seconds = v; }, { placeholder: '3600' })
    ]));

    // config 区域：根据 type 渲染不同字段
    body.appendChild(renderStepConfig(step));
    return body;
  }

  function renderStepConfig(step) {
    const wrap = el('div', { class: 'step-form-grid' });
    const cfg = step.config || {};
    if (step.type === 'deterministic') {
      wrap.appendChild(renderField('template', cfg.template || '', (v) => { cfg.template = v; }, { placeholder: 'directory_watch', mono: true, hint: '内置模板名' }));
    } else if (step.type === 'llm') {
      wrap.appendChild(renderTextareaField('prompt', cfg.prompt || '', (v) => { cfg.prompt = v; }, { placeholder: '请生成今日新闻简报...' }));
      wrap.appendChild(renderField('system（可选）', cfg.system || '', (v) => { cfg.system = v; }, { placeholder: '留空使用默认' }));
    } else if (step.type === 'tool') {
      wrap.appendChild(renderField('tool', cfg.tool || '', (v) => { cfg.tool = v; }, { placeholder: 'web_search', mono: true }));
      wrap.appendChild(renderTextareaField('input (JSON)', JSON.stringify(cfg.input || {}, null, 2), (v) => {
        try { cfg.input = JSON.parse(v || '{}'); } catch (e) { /* 容错：保留原值 */ }
      }, { mono: true, placeholder: '{"query": "..."}' }));
    } else if (step.type === 'react') {
      wrap.appendChild(renderTextareaField('task', cfg.task || '', (v) => { cfg.task = v; }, { placeholder: '审查最近一次 git diff' }));
      wrap.appendChild(renderNumberField('max_loops', cfg.max_loops || 3, (v) => { cfg.max_loops = v; }));
      wrap.appendChild(renderField('tool_whitelist（逗号分隔）', (cfg.tool_whitelist || []).join(','),
        (v) => { cfg.tool_whitelist = v.split(',').map((s) => s.trim()).filter(Boolean); },
        { placeholder: 'shell,file_read', mono: true, hint: '为空时使用默认工具集' }));
    } else if (step.type === 'subworkflow') {
      wrap.appendChild(renderField('workflow_ref', cfg.workflow_ref || '', (v) => { cfg.workflow_ref = v; }, { placeholder: '另一个 workflow 的 name', mono: true, hint: 'P2 stub，暂未实装' }));
    }
    step.config = cfg;
    return wrap;
  }

  function renderField(label, value, onChange, opts = {}) {
    const input = el('input', {
      class: 'step-form-input',
      type: opts.type || 'text',
      value: value == null ? '' : String(value),
      placeholder: opts.placeholder || '',
      oninput: (e) => onChange(e.target.value)
    });
    if (opts.mono) input.style.fontFamily = 'var(--font-mono)';
    const children = [el('label', { class: 'step-form-label', text: label }), input];
    if (opts.hint) children.push(el('div', { class: 'step-form-hint', html: opts.hint }));
    return el('div', { class: `step-form-field${opts.span2 ? ' col-span-2' : ''}` }, children);
  }

  function renderNumberField(label, value, onChange, opts = {}) {
    const input = el('input', {
      class: 'step-form-input',
      type: 'number',
      value: value == null ? '' : String(value),
      placeholder: opts.placeholder || '',
      oninput: (e) => { const v = e.target.value; onChange(v === '' ? null : Number(v)); }
    });
    return el('div', { class: 'step-form-field' }, [
      el('label', { class: 'step-form-label', text: label }), input
    ]);
  }

  function renderSelectField(label, value, options, onChange) {
    const select = el('select', { class: 'step-form-select', onchange: (e) => onChange(e.target.value) },
      options.map((o) => el('option', { value: o, selected: o === value ? 'selected' : null }, [o]))
    );
    return el('div', { class: 'step-form-field' }, [
      el('label', { class: 'step-form-label', text: label }), select
    ]);
  }

  function renderTextareaField(label, value, onChange, opts = {}) {
    const ta = el('textarea', {
      class: 'step-form-textarea',
      placeholder: opts.placeholder || '',
      oninput: (e) => onChange(e.target.value)
    }, [value == null ? '' : String(value)]);
    if (opts.mono) ta.style.fontFamily = 'var(--font-mono)';
    return el('div', { class: 'step-form-field col-span-2' }, [
      el('label', { class: 'step-form-label', text: label }), ta
    ]);
  }

  // ---------- 渲染：预览 ----------
  function renderPreview() {
    const body = $('#workflowPreviewBody');
    const countEl = $('#previewStepCount');
    body.innerHTML = '';
    const steps = state.currentSpec ? (state.currentSpec.steps || []) : [];
    if (countEl) countEl.textContent = `${steps.length} step${steps.length === 1 ? '' : 's'}`;
    if (steps.length === 0) {
      body.appendChild(el('div', { class: 'preview-empty', text: '加载模板或添加 step 后，这里会显示 step 卡片与依赖关系箭头' }));
      return;
    }
    steps.forEach((step, idx) => {
      body.appendChild(el('div', { class: 'preview-step' }, [
        el('div', { class: 'preview-step-node', text: String(idx + 1) }),
        el('div', { class: 'preview-step-info' }, [
          el('div', { class: 'preview-step-name', text: step.name || step.id }),
          el('div', { class: 'preview-step-meta' }, [`${step.type}${step.depends_on && step.depends_on.length ? ' ← ' + step.depends_on.join(',') : ''}`])
        ])
      ]));
      if (idx < steps.length - 1) {
        body.appendChild(el('div', { class: 'preview-arrow', text: '↓' }));
      }
    });
  }

  // ---------- 行为 ----------
  function loadPreset(preset) {
    state.currentSpec = JSON.parse(JSON.stringify(preset.spec));
    state.selectedStepIdx = null;
    state.advancedMode = false;
    syncFormFromSpec();
    renderEditor();
    // 滚动到编辑器顶部
    $('#workflowEditor').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function newWorkflow() {
    state.currentSpec = { name: '', timeout_seconds: null, steps: [] };
    state.selectedStepIdx = null;
    state.advancedMode = false;
    syncFormFromSpec();
    renderEditor();
  }

  function syncFormFromSpec() {
    // 触发 form 字段渲染（renderForm 内部读 state.currentSpec）
  }

  function toggleAdvancedMode() {
    if (!state.currentSpec) {
      newWorkflow();
    }
    if (state.advancedMode) {
      // 从 YAML 解析回 spec
      try {
        const yaml = $('#workflowYamlTextarea').value;
        const parsed = parseSimpleYaml(yaml);
        state.currentSpec = parsed;
      } catch (e) {
        showValidateResult(false, `YAML 解析失败: ${e.message}`);
        return;
      }
    }
    state.advancedMode = !state.advancedMode;
    renderEditor();
  }

  function addStep() {
    if (!state.currentSpec) newWorkflow();
    const idx = (state.currentSpec.steps || []).length + 1;
    state.currentSpec.steps = state.currentSpec.steps || [];
    state.currentSpec.steps.push({
      id: `s${idx}`,
      name: `步骤 ${idx}`,
      type: 'llm',
      config: {},
      depends_on: [],
      condition: ''
    });
    state.selectedStepIdx = state.currentSpec.steps.length - 1;
    renderSteps();
    renderPreview();
  }

  function toggleStepSelect(idx) {
    state.selectedStepIdx = state.selectedStepIdx === idx ? null : idx;
    renderSteps();
  }

  function deleteStep(idx) {
    if (!state.currentSpec || !state.currentSpec.steps) return;
    const removedId = state.currentSpec.steps[idx].id;
    state.currentSpec.steps.splice(idx, 1);
    // 清理依赖
    state.currentSpec.steps.forEach((s) => {
      if (s.depends_on) s.depends_on = s.depends_on.filter((d) => d !== removedId);
    });
    if (state.selectedStepIdx === idx) state.selectedStepIdx = null;
    else if (state.selectedStepIdx !== null && state.selectedStepIdx > idx) state.selectedStepIdx -= 1;
    renderSteps();
    renderPreview();
  }

  function moveStep(idx, dir) {
    const steps = state.currentSpec.steps;
    const newIdx = idx + dir;
    if (newIdx < 0 || newIdx >= steps.length) return;
    [steps[idx], steps[newIdx]] = [steps[newIdx], steps[idx]];
    if (state.selectedStepIdx === idx) state.selectedStepIdx = newIdx;
    else if (state.selectedStepIdx === newIdx) state.selectedStepIdx = idx;
    renderSteps();
    renderPreview();
  }

  function validateSpec() {
    if (!state.currentSpec) {
      showValidateResult(false, '请先加载模板或新建 workflow');
      return;
    }
    const errors = [];
    if (!state.currentSpec.name) errors.push('workflow 名称不能为空');
    const steps = state.currentSpec.steps || [];
    if (steps.length === 0) errors.push('至少需要一个 step');
    const ids = new Set();
    steps.forEach((s, i) => {
      if (!s.id) errors.push(`step[${i}] 缺少 id`);
      if (ids.has(s.id)) errors.push(`step[${i}] id 重复: ${s.id}`);
      ids.add(s.id);
      if (!STEP_TYPES.includes(s.type)) errors.push(`step[${i}] 类型非法: ${s.type}`);
      (s.depends_on || []).forEach((d) => {
        if (!ids.has(d) && !steps.some((x) => x.id === d)) errors.push(`step[${i}] depends_on 引用不存在的 id: ${d}`);
      });
    });
    if (errors.length > 0) {
      showValidateResult(false, errors.join('\n'));
    } else {
      showValidateResult(true, `校验通过：${steps.length} step，类型分布 ${countTypes(steps).join(', ')}`);
    }
  }

  function countTypes(steps) {
    const m = {};
    steps.forEach((s) => { m[s.type] = (m[s.type] || 0) + 1; });
    return Object.entries(m).map(([k, v]) => `${k}=${v}`);
  }

  function showValidateResult(ok, msg) {
    const box = $('#workflowValidateResult');
    box.hidden = false;
    box.className = `workflow-validate-result${ok ? ' is-ok' : ' is-error'}`;
    box.textContent = msg;
  }

  // ---------- 保存为调度（内嵌 mini dialog，无跳转） ----------
  function saveAsSchedule() {
    if (!state.currentSpec) {
      showValidateResult(false, '请先加载模板或新建 workflow');
      return;
    }
    openSaveAsScheduleDialog();
  }

  function openSaveAsScheduleDialog() {
    const dialog = $('#saveAsScheduleDialog');
    if (!dialog) return;
    // 预填名称（默认 = workflow name）
    const nameInput = $('#miniSchedName');
    if (nameInput) nameInput.value = state.currentSpec.name || '';
    // 预填任务描述（取第一个 llm step 的 prompt）
    const taskInput = $('#miniSchedTask');
    if (taskInput) {
      const firstLlm = (state.currentSpec.steps || []).find((s) => s.type === 'llm');
      taskInput.value = (firstLlm && firstLlm.config && firstLlm.config.prompt) || '';
    }
    // 默认 cron（用户可改）
    const cronInput = $('#miniSchedCron');
    if (cronInput && !cronInput.value) cronInput.value = '0 9 * * *';
    // step 计数
    const countEl = $('#miniDialogStepsCount');
    if (countEl) countEl.textContent = String((state.currentSpec.steps || []).length);
    // 触发 cron 预览
    refreshMiniCronPreview();
    dialog.hidden = false;
    // 聚焦名称输入
    setTimeout(() => { if (nameInput) nameInput.focus(); }, 50);
  }

  function closeSaveAsScheduleDialog() {
    const dialog = $('#saveAsScheduleDialog');
    if (dialog) dialog.hidden = true;
  }

  function refreshMiniCronPreview() {
    const cronInput = $('#miniSchedCron');
    const preview = $('#miniCronNextPreview');
    if (!cronInput || !preview) return;
    const fn = (window.HermesUtils && window.HermesUtils.previewCronNext)
      || (typeof previewCronNext === 'function' ? previewCronNext : null);
    preview.textContent = fn ? fn(cronInput.value, 5) : '—';
  }

  async function confirmSaveAsSchedule() {
    const name = (($('#miniSchedName') || {}).value || '').trim();
    const cron = (($('#miniSchedCron') || {}).value || '').trim();
    const task = (($('#miniSchedTask') || {}).value || '').trim();
    if (!name) { showValidateResult(false, '请填写调度名称'); return; }
    if (!cron) { showValidateResult(false, '请填写 cron 表达式'); return; }
    const payload = {
      name,
      cron,
      task: task || `执行 workflow: ${name}`,
      enabled: true,
      workflow: state.currentSpec,
    };
    const confirmBtn = $('#miniDialogConfirm');
    if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = '创建中...'; }
    try {
      const data = await api('/schedules', { method: 'POST', body: payload });
      showValidateResult(true, `✓ 调度已创建：${data.schedule_id || data.id || name}`);
      closeSaveAsScheduleDialog();
    } catch (e) {
      showValidateResult(false, `创建失败: ${e.message}`);
    } finally {
      if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = '创建调度'; }
    }
  }

  // ---------- 简易 YAML 序列化/反序列化（避免引入外部库） ----------
  function specToYaml(spec) {
    const lines = [];
    lines.push(`name: ${spec.name || ''}`);
    if (spec.timeout_seconds) lines.push(`timeout_seconds: ${spec.timeout_seconds}`);
    lines.push(`steps:`);
    (spec.steps || []).forEach((s) => {
      lines.push(`  - id: ${s.id}`);
      if (s.name) lines.push(`    name: ${s.name}`);
      lines.push(`    type: ${s.type}`);
      if (s.depends_on && s.depends_on.length) lines.push(`    depends_on: [${s.depends_on.map((d) => d).join(', ')}]`);
      if (s.condition) lines.push(`    condition: "${s.condition}"`);
      if (s.timeout_seconds) lines.push(`    timeout_seconds: ${s.timeout_seconds}`);
      lines.push(`    config:`);
      Object.entries(s.config || {}).forEach(([k, v]) => {
        if (v == null) return;
        if (typeof v === 'string') {
          lines.push(`      ${k}: ${/[:\n#]/.test(v) || v.length > 60 ? JSON.stringify(v) : v}`);
        } else if (typeof v === 'object') {
          lines.push(`      ${k}:`);
          Object.entries(v).forEach(([k2, v2]) => {
            lines.push(`        ${k2}: ${typeof v2 === 'string' ? v2 : JSON.stringify(v2)}`);
          });
        } else {
          lines.push(`      ${k}: ${v}`);
        }
      });
    });
    return lines.join('\n');
  }

  function parseSimpleYaml(yaml) {
    // 简易解析：仅支持本页面生成的格式，复杂场景提示用高级模式手工编辑
    // 这里降级为最小可用解析：用 JS 对象字面量 fallback
    // 真正的 YAML 解析由后端 validate API 处理
    const spec = { name: '', steps: [], timeout_seconds: null };
    let currentStep = null;
    let currentConfigKey = null;
    yaml.split('\n').forEach((line) => {
      if (!line.trim() || line.trim().startsWith('#')) return;
      if (line.startsWith('name:')) spec.name = line.slice(5).trim();
      else if (line.startsWith('timeout_seconds:')) spec.timeout_seconds = Number(line.slice(16).trim()) || null;
      else if (line.startsWith('steps:')) return;
      else if (line.startsWith('  - id:')) {
        currentStep = { id: line.slice(7).trim(), name: '', type: 'llm', config: {}, depends_on: [], condition: '' };
        spec.steps.push(currentStep);
        currentConfigKey = null;
      } else if (currentStep && line.startsWith('    name:')) currentStep.name = line.slice(9).trim();
      else if (currentStep && line.startsWith('    type:')) currentStep.type = line.slice(9).trim();
      else if (currentStep && line.startsWith('    depends_on:')) {
        const m = line.slice(15).trim().match(/^\[(.*)\]$/);
        if (m) currentStep.depends_on = m[1].split(',').map((s) => s.trim()).filter(Boolean);
      } else if (currentStep && line.startsWith('    condition:')) {
        const m = line.slice(13).trim().match(/^"(.*)"$/);
        currentStep.condition = m ? m[1] : line.slice(13).trim();
      } else if (currentStep && line.startsWith('    timeout_seconds:')) {
        currentStep.timeout_seconds = Number(line.slice(21).trim()) || null;
      } else if (currentStep && line.startsWith('    config:')) {
        currentConfigKey = null;
      } else if (currentStep && line.startsWith('      ')) {
        const m = line.trim().match(/^([a-z_]+):\s*(.*)$/);
        if (m) {
          currentStep.config[m[1]] = coerceYamlValue(m[2]);
          currentConfigKey = m[1];
        }
      }
    });
    return spec;
  }

  function coerceYamlValue(v) {
    if (v == null) return null;
    v = v.trim();
    if (v === '') return '';
    if (/^-?\d+$/.test(v)) return Number(v);
    if (v === 'true') return true;
    if (v === 'false') return false;
    if (/^".*"$/.test(v) || /^'.*'$/.test(v)) return v.slice(1, -1);
    return v;
  }

  // ---------- 事件绑定 ----------
  function bindEvents() {
    $('#btnNewWorkflow').addEventListener('click', newWorkflow);
    $('#btnFromTemplate').addEventListener('click', () => {
      $('#workflowTemplates').scrollIntoView({ behavior: 'smooth' });
      $('#templateSearch').focus();
    });
    $('#btnSaveSchedule').addEventListener('click', saveAsSchedule);
    $('#btnAdvancedMode').addEventListener('click', toggleAdvancedMode);
    $('#btnAddStep').addEventListener('click', addStep);
    $('#btnValidate').addEventListener('click', validateSpec);
    $('#btnStartFromTemplate').addEventListener('click', () => {
      $('#templateSearch').focus();
    });

    $('#templateSearch').addEventListener('input', (e) => {
      state.searchQuery = e.target.value;
      renderTemplates();
    });

    // form 字段：name / timeout
    document.addEventListener('input', (e) => {
      if (!state.currentSpec) return;
      if (e.target.id === 'workflowName') state.currentSpec.name = e.target.value;
      else if (e.target.id === 'workflowTimeout') {
        state.currentSpec.timeout_seconds = e.target.value === '' ? null : Number(e.target.value);
      }
    });

    // 帮助弹窗
    const helpOverlay = $('#helpOverlay');
    const btnHelp = $('#btnHelp');
    const btnCloseHelp = $('#btnCloseHelp');
    if (btnHelp && helpOverlay) {
      btnHelp.addEventListener('click', () => { helpOverlay.hidden = false; });
    }
    if (btnCloseHelp && helpOverlay) {
      btnCloseHelp.addEventListener('click', () => { helpOverlay.hidden = true; });
    }
    if (helpOverlay) {
      helpOverlay.addEventListener('click', (e) => {
        if (e.target === helpOverlay) helpOverlay.hidden = true;
      });
    }
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && helpOverlay && !helpOverlay.hidden) {
        helpOverlay.hidden = true;
      }
    });

    // 保存为调度 mini dialog 事件
    const miniDialog = $('#saveAsScheduleDialog');
    const miniClose = $('#miniDialogClose');
    const miniCancel = $('#miniDialogCancel');
    const miniConfirm = $('#miniDialogConfirm');
    const miniCronInput = $('#miniSchedCron');
    if (miniClose) miniClose.addEventListener('click', closeSaveAsScheduleDialog);
    if (miniCancel) miniCancel.addEventListener('click', closeSaveAsScheduleDialog);
    if (miniConfirm) miniConfirm.addEventListener('click', confirmSaveAsSchedule);
    if (miniDialog) {
      miniDialog.addEventListener('click', (e) => {
        if (e.target === miniDialog) closeSaveAsScheduleDialog();
      });
    }
    if (miniCronInput) miniCronInput.addEventListener('input', refreshMiniCronPreview);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && miniDialog && !miniDialog.hidden) {
        closeSaveAsScheduleDialog();
      }
    });
  }

  // ---------- 初始化 ----------
  function init() {
    renderTemplates();
    bindEvents();
    renderEditor();
    const params = new URLSearchParams(window.location.search);
    // ?import_key=<localStorage key> 降级模式（超长 spec）优先于 ?import=
    const importKey = params.get('import_key');
    const importB64 = params.get('import');
    if (importKey || importB64) {
      try {
        let json;
        if (importKey) {
          json = localStorage.getItem(importKey);
          if (json) localStorage.removeItem(importKey);
          if (!json) throw new Error('导入数据已过期或不存在');
        } else {
          json = decodeURIComponent(escape(atob(importB64)));
        }
        const spec = JSON.parse(json);
        state.currentSpec = spec;
        state.selectedStepIdx = null;
        state.advancedMode = false;
        renderEditor();
        showValidateResult(true, `已导入 workflow：${(spec.steps || []).length} step`);
      } catch (e) {
        showValidateResult(false, `导入失败：${e.message}`);
      }
      return;
    }
    // URL 参数 ?preset=xxx 自动加载
    const presetId = params.get('preset');
    if (presetId) {
      const p = state.presets.find((x) => x.id === presetId);
      if (p) loadPreset(p);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
