/* ============================================================
   chat-core.js — SSE 流式 + 消息渲染 + 工具/Todo/审批卡片 + 中断
   含 4 项前后端一致性修复：
   1. plan 工具名 plan_task→plan_create, update_todo→plan_update_step
   2. done.response 渲染（max_loops 总结 / stuck 消息）
   3. error.reason 展示（deny 拦截原因）
   4. 工具卡输入/输出 JSON 美化 + 语法高亮
   ============================================================ */

// ========== 工具值渲染（JSON 美化 + 高亮） ==========
// 修复点 4：替代原 escapeHtml 纯文本，工具输入/输出现在有语法高亮
function renderToolValue(value) {
  if (value == null || value === '') return '<span class="text-muted">（空）</span>';
  let str = typeof value === 'string' ? value : stringifyValue(value);
  const parsed = safeParseJSON(str);
  if (parsed != null && typeof parsed === 'object') {
    str = JSON.stringify(parsed, null, 2);
    return highlightJSON(str);
  }
  return escapeHtml(str);
}

// ========== 消息渲染 ==========
function appendMessage(role, content, attachments) {
  welcomeScreenEl.style.display = 'none';
  const msg = document.createElement('div');
  msg.className = 'message ' + (role === 'user' ? 'user' : 'assistant');
  const roleLabel = role === 'user' ? 'You' : 'Assistant';
  if (role === 'user') {
    let bubbleContent = escapeHtml(content || '');
    if (attachments) {
      bubbleContent += renderAttachments(attachments);
    }
    msg.innerHTML = `
      <div class="message-role ${role}">${roleLabel}</div>
      <div class="message-bubble">${bubbleContent}</div>
    `;
  } else {
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble markdown';
    bubble.innerHTML = renderMarkdown(content);
    msg.innerHTML = `<div class="message-role ${role}">${roleLabel}</div>`;
    msg.appendChild(bubble);
    enhanceCodeBlocks(bubble);
  }
  messagesEl.appendChild(msg);
  scrollMessagesToBottom();
  return msg;
}

// ========== 附件渲染（图片缩略图 / 文档卡片） ==========
function renderAttachments(attachments) {
  if (!attachments) return '';
  let atts = attachments;
  if (typeof atts === 'string') atts = safeParseJSON(atts) || [];
  if (!Array.isArray(atts) || !atts.length) return '';
  let html = '<div class="message-attachments">';
  for (const a of atts) {
    const fid = escapeHtml(a.file_id || '');
    const name = escapeHtml(a.name || '');
    const sizeLabel = a.size > 1048576
      ? (a.size / 1048576).toFixed(1) + 'MB'
      : (a.size / 1024).toFixed(1) + 'KB';
    if (a.category === 'image') {
      html += `<div class="attachment-image">` +
        `<img src="/files/${fid}/raw" alt="${name}" loading="lazy" ` +
        `class="attachment-thumb" data-file-id="${fid}" data-name="${name}">` +
        `</div>`;
    } else {
      const iconMap = { '.pdf': 'PDF', '.docx': 'DOC', '.txt': 'TXT', '.md': 'MD' };
      const icon = iconMap[a.type] || 'FILE';
      html += `<div class="attachment-file">` +
        `<span class="attachment-icon">${icon}</span>` +
        `<span class="attachment-name">${name}</span>` +
        `<span class="attachment-size">${sizeLabel}</span>` +
        `</div>`;
    }
  }
  html += '</div>';
  return html;
}

// ========== 图片放大弹窗 ==========
function openImageModal(src, caption) {
  const modal = document.getElementById('imageModal');
  const img = document.getElementById('imageModalImg');
  const cap = document.getElementById('imageModalCaption');
  if (modal && img) {
    img.src = src;
    if (cap) cap.textContent = caption || '';
    modal.classList.add('show');
  }
}

// ========== 工具动作描述 ==========
// 工具名与后端 src/agent/builtin_tools.py 注册名一致
function describeToolAction(toolName, toolInput) {
  const input = toolInput || {};
  const maxLen = 60;
  let desc = '';
  switch (toolName) {
    // 文件类
    case 'file_read':
      desc = `读取 ${input.path || ''}`;
      break;
    case 'file_write':
      desc = `写入 ${input.path || ''}`;
      break;
    case 'file_edit':
      desc = `编辑 ${input.path || ''}`;
      break;
    case 'file_delete':
      desc = `删除 ${input.path || ''}`;
      break;
    case 'file_listdir':
      desc = `列目录 ${input.path || ''}`;
      break;
    case 'file_glob':
      desc = `匹配 ${input.pattern || ''}`;
      break;
    case 'file_grep':
      desc = `搜索 "${input.pattern || ''}"`;
      break;
    // 执行 / 网络
    case 'bash_exec':
      desc = `执行 ${input.command || ''}`;
      break;
    case 'web_fetch':
      desc = `${input.method || 'GET'} ${input.url || ''}`;
      break;
    // 计划
    case 'plan_create':
      desc = `创建计划"${input.goal || ''}"`;
      break;
    case 'plan_update_step':
      desc = `步骤 #${input.step_id != null ? input.step_id : ''} ${input.status || ''}`;
      break;
    // 记忆 / 画像
    case 'memory_search':
      desc = `检索记忆 "${input.query || ''}"`;
      break;
    case 'memory_delete':
      desc = `删除记忆 ${input.memory_id || ''}`;
      break;
    case 'memory_update':
      desc = `更新记忆 ${input.memory_id || ''}`;
      break;
    case 'profile_update':
      desc = `更新画像 (${input.section || input.action || ''})`;
      break;
    // 工具元操作
    case 'tool_list':
      desc = `列出工具 "${input.query || ''}"`;
      break;
    case 'tool_call':
      desc = `调用 ${input.name || ''}`;
      break;
    // 上传文件
    case 'file_query':
      desc = `查询文件 ${input.file_id || ''}`;
      break;
    case 'file_read_uploaded':
      desc = `读取上传文件 ${input.file_id || ''}`;
      break;
    case 'file_list_uploads':
      desc = '列出上传文件';
      break;
    default:
      desc = toolName || 'tool';
  }
  if (desc.length > maxLen) desc = desc.slice(0, maxLen) + '...';
  return desc;
}

// ========== 流式消息管理 ==========
// 构建流式气泡：.bubble-status（状态文案，空气泡时显示）+ .bubble-content（markdown 目标）+ .streaming-dots（三点）
// 状态行与 content/dots 平级；有正文后加 .has-text 隐藏状态行，工具阶段移除 .is-streaming 全部静默
function _buildStreamBubble() {
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble markdown is-streaming';
  const status = document.createElement('div');
  status.className = 'bubble-status';
  status.textContent = '正在思考…';
  const content = document.createElement('div');
  content.className = 'bubble-content';
  const dots = document.createElement('span');
  dots.className = 'streaming-dots';
  dots.innerHTML = '<i></i><i></i><i></i>';
  bubble.appendChild(status);
  bubble.appendChild(content);
  bubble.appendChild(dots);
  return bubble;
}

// 暂停当前轮气泡的三点指示（进入工具/Todo/审批阶段时调用）
function _pauseDots(rounds, roundIdx) {
  const cur = rounds[roundIdx];
  if (cur && cur.el) cur.el.classList.remove('is-streaming');
}

function createStreamMessage() {
  welcomeScreenEl.style.display = 'none';
  const msg = document.createElement('div');
  msg.className = 'message assistant';
  const roleEl = document.createElement('div');
  roleEl.className = 'message-role assistant';
  roleEl.textContent = 'Assistant';
  const bubble = _buildStreamBubble();
  msg.appendChild(roleEl);
  msg.appendChild(bubble);
  messagesEl.appendChild(msg);
  scrollMessagesToBottom();
  return { msg, bubble };
}

function _cleanupStreamRounds(rounds, streamMsg) {
  rounds.forEach(r => {
    if (!r.el) return;
    r.el.classList.remove('is-streaming');
    r.el.querySelectorAll('.streaming-dots, .bubble-status').forEach(d => d.remove());
  });
  rounds.forEach(r => {
    if (!r.text.trim() && r.el && r.el.parentNode === streamMsg) {
      const prevEl = r.el.previousElementSibling;
      if (prevEl && prevEl.classList.contains('round-separator')) prevEl.remove();
      r.el.remove();
    }
  });
  const hasText = rounds.some(r => r.text.trim());
  const hasCards = streamMsg && streamMsg.querySelector('.tool-card, .todo-card, .approval-card');
  if (!hasText && !hasCards && streamMsg && streamMsg.parentNode) streamMsg.remove();
}

function _hideEmptyRoundBubble(rounds, roundIdx) {
  const cur = rounds[roundIdx];
  if (cur && cur.el && !cur.text.trim() && cur.el.parentNode) {
    cur.el.classList.remove('is-streaming');
    cur.el.style.display = 'none';
  }
}

function updateStreamBubble(bubble, fullText) {
  if (bubble.style.display === 'none') bubble.style.display = '';
  const content = bubble.querySelector('.bubble-content') || bubble;
  content.innerHTML = renderMarkdown(fullText);
  enhanceCodeBlocks(bubble);
  scrollMessagesToBottom();
}

// ========== 工具卡片（流式 + 历史） ==========
// 修复点 1：plan_create/plan_update_step 由 todo 卡片系统展示，不创建工具卡
function appendToolCard(msgEl, toolEvent) {
  const toolName = toolEvent.name || 'tool';
  const toolInput = toolEvent.input || {};
  const toolResult = toolEvent.result || '';
  const isError = toolEvent.is_error;
  const toolUseId = toolEvent.tool_use_id || '';

  if (toolName === 'plan_create' || toolName === 'plan_update_step') return;

  // 两阶段更新：tool_use_id 匹配已有卡片则更新状态
  if (toolUseId && toolResult) {
    const existing = msgEl.querySelector(`[data-tool-call-id="${CSS.escape(toolUseId)}"]`);
    if (existing) {
      const statusSpan = existing.querySelector('.tool-card-status');
      const bodyDiv = existing.querySelector('.tool-card-body');
      if (statusSpan) {
        statusSpan.className = 'tool-card-status ' + (isError ? 'is-error' : 'is-done');
        statusSpan.textContent = isError ? '✗ 失败' : '✓ 完成';
      }
      if (bodyDiv && !bodyDiv.querySelector('.tool-card-result')) {
        const section = document.createElement('div');
        section.className = 'tool-card-section tool-card-result';
        section.innerHTML = `<div class="tool-card-label">结果</div><pre class="tool-card-content">${renderToolValue(toolResult)}</pre>`;
        bodyDiv.appendChild(section);
      }
      if (isError) existing.classList.add('is-error');
      setTimeout(() => { existing.open = false; }, 3000);
      scrollMessagesToBottom();
      return;
    }
  }

  // 首次调用：创建新卡片
  const details = document.createElement('details');
  details.className = 'tool-card' + (isError ? ' is-error' : '');
  details.dataset.toolName = toolName;
  if (toolUseId) details.dataset.toolCallId = toolUseId;
  details.open = true;

  const actionDesc = describeToolAction(toolName, toolInput);
  const resultLabel = toolResult ? (isError ? '✗ 失败' : '✓ 完成') : '⏳ 运行中';
  const resultClass = toolResult ? (isError ? 'is-error' : 'is-done') : 'is-running';

  details.innerHTML = `
    <summary class="tool-card-header">
      <span class="tool-card-name">🔧 ${escapeHtml(toolName)}</span>
      <span class="tool-card-action">${escapeHtml(actionDesc)}</span>
      <span class="tool-card-status ${resultClass}">${resultLabel}</span>
    </summary>
    <div class="tool-card-body">
      <div class="tool-card-section">
        <div class="tool-card-label">输入</div>
        <pre class="tool-card-content">${renderToolValue(toolInput)}</pre>
      </div>
      ${toolResult ? `<div class="tool-card-section tool-card-result"><div class="tool-card-label">结果</div><pre class="tool-card-content">${renderToolValue(toolResult)}</pre></div>` : ''}
    </div>
  `;

  msgEl.appendChild(details);
  if (toolResult) setTimeout(() => { details.open = false; }, 3000);
  scrollMessagesToBottom();
}

// 历史合并卡片：tool_use + tool_result 配对
function appendMergedToolCard(toolName, toolUseContent, toolResultContent, isError) {
  const msg = document.createElement('div');
  msg.className = 'message assistant';
  const roleEl = document.createElement('div');
  roleEl.className = 'message-role assistant';
  roleEl.textContent = 'Assistant';
  msg.appendChild(roleEl);

  const details = document.createElement('details');
  details.className = 'tool-card';
  details.dataset.toolName = toolName;

  // 从 tool_use content 提取输入 JSON（格式："调用工具 X: {json}"）
  let toolInput = {};
  const match = String(toolUseContent).match(/^调用工具\s+.+?\s*:\s*([\s\S]*)$/);
  if (match) {
    try { toolInput = JSON.parse(match[1]); } catch (e) { /* 降级 */ }
  }

  const actionDesc = describeToolAction(toolName, toolInput);
  const resultStr = toolResultContent || '';
  const finalIsError = (isError === null || isError === undefined)
    ? /(\[失败\]|\[拦截\]|已拦截|error|失败|permission denied|forbidden)/i.test(resultStr || '')
    : !!isError;

  details.innerHTML = `
    <summary class="tool-card-header">
      <span class="tool-card-name">🔧 ${escapeHtml(toolName)}</span>
      <span class="tool-card-action">${escapeHtml(actionDesc)}</span>
      <span class="tool-card-status ${finalIsError ? 'is-error' : 'is-done'}">${finalIsError ? '✗ 失败' : '✓ 完成'}</span>
    </summary>
    <div class="tool-card-body">
      <div class="tool-card-section">
        <div class="tool-card-label">输入</div>
        <pre class="tool-card-content">${renderToolValue(toolInput)}</pre>
      </div>
      ${resultStr ? `<div class="tool-card-section"><div class="tool-card-label">结果</div><pre class="tool-card-content">${renderToolValue(resultStr)}</pre></div>` : ''}
    </div>
  `;

  msg.appendChild(details);
  messagesEl.appendChild(msg);
  scrollMessagesToBottom();
}

// 历史孤立工具调用（无配对结果）
function appendToolCallCard(toolName, content) {
  const msg = document.createElement('div');
  msg.className = 'message assistant';
  const roleEl = document.createElement('div');
  roleEl.className = 'message-role assistant';
  roleEl.textContent = 'Assistant';
  msg.appendChild(roleEl);

  const details = document.createElement('details');
  details.className = 'tool-card';
  details.dataset.toolName = toolName;

  let toolInput = {};
  const match = String(content).match(/^调用工具\s+.+?\s*:\s*([\s\S]*)$/);
  if (match) {
    try { toolInput = JSON.parse(match[1]); } catch (e) { /* 降级 */ }
  }
  const actionDesc = describeToolAction(toolName, toolInput);

  details.innerHTML = `
    <summary class="tool-card-header">
      <span class="tool-card-name">🔧 ${escapeHtml(toolName)}</span>
      <span class="tool-card-action">${escapeHtml(actionDesc)}</span>
      <span class="tool-card-status is-running">调用中...</span>
    </summary>
    <div class="tool-card-body">
      <div class="tool-card-section">
        <div class="tool-card-label">输入</div>
        <pre class="tool-card-content">${renderToolValue(toolInput)}</pre>
      </div>
    </div>
  `;

  msg.appendChild(details);
  messagesEl.appendChild(msg);
  scrollMessagesToBottom();
  return msg;
}

// 历史孤立工具结果
function appendToolResultCard(toolName, content, isError) {
  const msg = document.createElement('div');
  msg.className = 'message assistant';
  const roleEl = document.createElement('div');
  roleEl.className = 'message-role assistant';
  roleEl.textContent = 'Assistant';
  msg.appendChild(roleEl);

  const details = document.createElement('details');
  details.className = 'tool-card';
  details.dataset.toolName = toolName;

  const resultStr = content || '';
  const finalIsError = (isError === null || isError === undefined)
    ? /(\[失败\]|\[拦截\]|已拦截|error|失败|permission denied|forbidden)/i.test(resultStr || '')
    : !!isError;

  details.innerHTML = `
    <summary class="tool-card-header">
      <span class="tool-card-name">🔧 ${escapeHtml(toolName || 'tool')}</span>
      <span class="tool-card-status ${finalIsError ? 'is-error' : 'is-done'}">${finalIsError ? '✗ 失败' : '✓ 完成'}</span>
    </summary>
    <div class="tool-card-body">
      <div class="tool-card-section">
        <div class="tool-card-label">结果</div>
        <pre class="tool-card-content">${renderToolValue(resultStr)}</pre>
      </div>
    </div>
  `;

  msg.appendChild(details);
  messagesEl.appendChild(msg);
  scrollMessagesToBottom();
  return msg;
}

// ========== Todo 卡片 ==========
const TODO_STEP_ICONS = {
  pending: '⏳',
  in_progress: '🔄',
  completed: '✅',
  failed: '❌'
};

function appendTodoCard(msgEl, todoData) {
  // 作用域查找：只在当前消息节点内查找 .todo-card，避免多消息/多会话串扰
  let card = msgEl.querySelector('.todo-card');
  if (!card) {
    card = document.createElement('div');
    card.className = 'todo-card';
    msgEl.appendChild(card);
  }
  updateTodoCard(card, todoData);
}

function updateTodoCard(cardOrMsgEl, todoData) {
  const card = cardOrMsgEl.classList && cardOrMsgEl.classList.contains('todo-card')
    ? cardOrMsgEl
    : cardOrMsgEl.querySelector('.todo-card');
  if (!card) return;

  const goal = escapeHtml(todoData.goal || '');
  const steps = todoData.steps || [];
  const isComplete = todoData.completed === true;

  const newStatuses = {};
  steps.forEach(s => { newStatuses[s.id] = s.status || 'pending'; });
  const changedSteps = {};
  for (const [id, newStatus] of Object.entries(newStatuses)) {
    const oldStatus = _prevTodoStepStatuses[id] || 'pending';
    if (oldStatus !== newStatus) changedSteps[id] = newStatus;
  }
  _prevTodoStepStatuses = newStatuses;

  if (isComplete) card.classList.add('is-complete');
  else card.classList.remove('is-complete');

  const stepsHtml = steps.map(step => {
    const status = step.status || 'pending';
    const icon = TODO_STEP_ICONS[status] || '⏳';
    const stateClass = `is-${status}`;
    const animClass = changedSteps[step.id]
      ? (changedSteps[step.id] === 'completed' ? 'just-completed'
        : changedSteps[step.id] === 'failed' ? 'just-failed'
        : changedSteps[step.id] === 'in_progress' ? 'just-started'
        : '')
      : '';
    const result = step.result ? `<div class="todo-step-result">${escapeHtml(step.result)}</div>` : '';
    return `
      <div class="todo-step ${stateClass} ${animClass}" data-step-id="${step.id}">
        <span class="todo-step-icon">${icon}</span>
        <span class="todo-step-id">#${step.id}</span>
        <span class="todo-step-content">${escapeHtml(step.content || '')}</span>
      </div>
      ${result}
    `;
  }).join('');

  const completedCount = steps.filter(s => s.status === 'completed').length;
  card.innerHTML = `
    <div class="todo-header">
      <span class="todo-goal">${goal}</span>
      <span class="todo-progress">${completedCount}/${steps.length}</span>
    </div>
    <div class="todo-steps">${stepsHtml}</div>
  `;

  const hasChanges = Object.keys(changedSteps).length > 0;
  if (hasChanges) {
    card.classList.add('pulse');
    const removePulse = () => {
      card.classList.remove('pulse');
      card.removeEventListener('animationend', removePulse);
    };
    card.addEventListener('animationend', removePulse);
    setTimeout(() => card.classList.remove('pulse'), 700);
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  card.querySelectorAll('.just-completed, .just-failed, .just-started').forEach(el => {
    const cleanup = () => {
      el.classList.remove('just-completed', 'just-failed', 'just-started');
      el.removeEventListener('animationend', cleanup);
    };
    el.addEventListener('animationend', cleanup);
    setTimeout(() => el.classList.remove('just-completed', 'just-failed', 'just-started'), 900);
  });
}

// ========== 审批卡片 ==========
function renderGenericApproval(evt) {
  const riskLevel = (evt.risk_level || 'medium').toLowerCase();
  const safeRisk = ['high', 'medium', 'low'].includes(riskLevel) ? riskLevel : 'medium';
  const riskLabel = safeRisk === 'high' ? '高风险' : safeRisk === 'low' ? '低风险' : '中风险';
  const inputJson = evt.tool_input ? JSON.stringify(evt.tool_input, null, 2) : '{}';
  return `
    <div class="approval-header">
      <span class="approval-icon">⚠</span>
      <span class="approval-title">需要审批</span>
      <span class="approval-risk tag tag-${safeRisk === 'high' ? 'danger' : safeRisk === 'low' ? 'success' : 'warning'}">${riskLabel}</span>
    </div>
    <div class="approval-body">
      <div class="approval-reason">${escapeHtml(evt.reason || '')}</div>
      <div class="approval-tool-name">${escapeHtml(evt.tool_name || 'tool')}</div>
      <pre class="tool-card-content">${highlightJSON(inputJson)}</pre>
    </div>
    <div class="approval-actions">
      <button class="btn btn-primary btn-sm" data-approval-action="approve" data-approval-id="${escapeHtml(evt.approval_id)}">批准</button>
      <button class="btn btn-danger btn-sm" data-approval-action="deny" data-approval-id="${escapeHtml(evt.approval_id)}">拒绝</button>
    </div>
  `;
}

function renderFileApproval(evt) {
  const path = (evt.tool_input && evt.tool_input.path) || '(未指定路径)';
  const opMap = {
    file_write: '写入/新建',
    file_edit: '修改',
    file_delete: '删除',
    file_read: '读取',
  };
  const op = opMap[evt.tool_name] || '文件操作';
  const inputJson = evt.tool_input ? JSON.stringify(evt.tool_input, null, 2) : '{}';
  return `
    <div class="approval-header">
      <span class="approval-icon">📄</span>
      <span class="approval-title">文件操作审批</span>
      <span class="approval-risk tag tag-warning">${op}</span>
    </div>
    <div class="approval-body">
      <div class="approval-reason">${escapeHtml(evt.reason || '')}</div>
      <div class="approval-file-path">
        <span class="approval-label">路径:</span>
        <code>${escapeHtml(path)}</code>
      </div>
      <pre class="tool-card-content">${highlightJSON(inputJson)}</pre>
    </div>
    <div class="approval-actions">
      <button class="btn btn-primary btn-sm" data-approval-action="approve" data-approval-id="${escapeHtml(evt.approval_id)}">批准</button>
      <button class="btn btn-danger btn-sm" data-approval-action="deny" data-approval-id="${escapeHtml(evt.approval_id)}">拒绝</button>
    </div>
  `;
}

function renderShellApproval(evt) {
  const cmd = (evt.tool_input && evt.tool_input.command) || '(空命令)';
  return `
    <div class="approval-header">
      <span class="approval-icon">⚡</span>
      <span class="approval-title">Shell 命令审批</span>
      <span class="approval-risk tag tag-danger">高危</span>
    </div>
    <div class="approval-body">
      <div class="approval-reason">${escapeHtml(evt.reason || '')}</div>
      <div class="approval-shell-cmd">
        <span class="approval-label">命令:</span>
        <pre class="tool-card-content"><code>${escapeHtml(cmd)}</code></pre>
      </div>
    </div>
    <div class="approval-actions">
      <button class="btn btn-primary btn-sm" data-approval-action="approve" data-approval-id="${escapeHtml(evt.approval_id)}">批准</button>
      <button class="btn btn-danger btn-sm" data-approval-action="deny" data-approval-id="${escapeHtml(evt.approval_id)}">拒绝</button>
    </div>
  `;
}

function renderSkillApproval(evt) {
  // skill__{name} 或 skill__{name}__{tool}
  const parts = (evt.tool_name || '').split('__').filter(Boolean);
  const skillName = parts[1] || '(未知)';
  const subTool = parts.length > 2 ? parts.slice(2).join('__') : '';
  const inputJson = evt.tool_input ? JSON.stringify(evt.tool_input, null, 2) : '{}';
  return `
    <div class="approval-header">
      <span class="approval-icon">🧩</span>
      <span class="approval-title">Skill 操作审批</span>
      <span class="approval-risk tag tag-info">${escapeHtml(skillName)}</span>
    </div>
    <div class="approval-body">
      <div class="approval-reason">${escapeHtml(evt.reason || '')}</div>
      <div class="approval-skill-name">
        <span class="approval-label">Skill:</span>
        <code>${escapeHtml(skillName)}</code>
        ${subTool ? `<span class="approval-sub-tool">→ ${escapeHtml(subTool)}</span>` : ''}
      </div>
      <pre class="tool-card-content">${highlightJSON(inputJson)}</pre>
    </div>
    <div class="approval-actions">
      <button class="btn btn-primary btn-sm" data-approval-action="approve" data-approval-id="${escapeHtml(evt.approval_id)}">批准</button>
      <button class="btn btn-danger btn-sm" data-approval-action="deny" data-approval-id="${escapeHtml(evt.approval_id)}">拒绝</button>
    </div>
  `;
}

function renderMcpApproval(evt) {
  // mcp__{server}__{tool}
  const parts = (evt.tool_name || '').split('__').filter(Boolean);
  const serverName = parts[1] || '(未知)';
  const toolName = parts.slice(2).join('__') || '(未知)';
  const inputJson = evt.tool_input ? JSON.stringify(evt.tool_input, null, 2) : '{}';
  return `
    <div class="approval-header">
      <span class="approval-icon">🔌</span>
      <span class="approval-title">MCP 工具审批</span>
      <span class="approval-risk tag tag-purple">${escapeHtml(serverName)}</span>
    </div>
    <div class="approval-body">
      <div class="approval-reason">${escapeHtml(evt.reason || '')}</div>
      <div class="approval-mcp-info">
        <span class="approval-label">Server:</span>
        <code>${escapeHtml(serverName)}</code>
        <span class="approval-label" style="margin-left:12px">Tool:</span>
        <code>${escapeHtml(toolName)}</code>
      </div>
      <pre class="tool-card-content">${highlightJSON(inputJson)}</pre>
    </div>
    <div class="approval-actions">
      <button class="btn btn-primary btn-sm" data-approval-action="approve" data-approval-id="${escapeHtml(evt.approval_id)}">批准</button>
      <button class="btn btn-danger btn-sm" data-approval-action="deny" data-approval-id="${escapeHtml(evt.approval_id)}">拒绝</button>
    </div>
  `;
}

function renderMemoryApproval(evt) {
  const opMap = {
    memory_delete: '删除记忆',
    memory_update: '修改记忆',
    profile_update: '修改用户画像',
    memory_search: '搜索记忆',
  };
  const op = opMap[evt.tool_name] || '记忆操作';
  const inputJson = evt.tool_input ? JSON.stringify(evt.tool_input, null, 2) : '{}';
  return `
    <div class="approval-header">
      <span class="approval-icon">🧠</span>
      <span class="approval-title">记忆操作审批</span>
      <span class="approval-risk tag tag-danger">高危</span>
    </div>
    <div class="approval-memory-warning">
      ⚠ 此操作将直接修改长期记忆/用户画像,可能不可恢复,请谨慎确认。
    </div>
    <div class="approval-body">
      <div class="approval-reason">${escapeHtml(evt.reason || '')}</div>
      <div class="approval-memory-op">
        <span class="approval-label">操作:</span>
        <code>${escapeHtml(op)}</code>
      </div>
      <pre class="tool-card-content">${highlightJSON(inputJson)}</pre>
    </div>
    <div class="approval-actions">
      <button class="btn btn-primary btn-sm" data-approval-action="approve" data-approval-id="${escapeHtml(evt.approval_id)}">批准</button>
      <button class="btn btn-danger btn-sm" data-approval-action="deny" data-approval-id="${escapeHtml(evt.approval_id)}">拒绝</button>
    </div>
  `;
}

function appendApprovalCard(msgEl, evt) {
  const kind = evt.tool_kind || "generic";
  const renderers = {
    file: renderFileApproval,
    shell: renderShellApproval,
    skill: renderSkillApproval,
    mcp: renderMcpApproval,
    memory: renderMemoryApproval,
    generic: renderGenericApproval,
  };
  const renderer = renderers[kind] || renderers.generic;
  const card = document.createElement('div');
  card.className = `approval-card tool-kind-${kind}`;
  card.dataset.approvalId = evt.approval_id;
  card.dataset.resolved = 'false';
  card.innerHTML = renderer(evt);

  msgEl.appendChild(card);

  card.querySelector('[data-approval-action="approve"]')?.addEventListener('click', () => resolveApproval(evt.approval_id, 'approve'));
  card.querySelector('[data-approval-action="deny"]')?.addEventListener('click', () => showDenyReasonPanel(evt.approval_id));

  scrollMessagesToBottom();
  pendingApprovalCount++;
  updateSendBtnState();
}

const DENY_QUICK_REASONS = ['操作风险过高', '不需要此操作', '改用其他方式'];

function showDenyReasonPanel(approvalId) {
  const card = findApprovalCard(approvalId);
  if (!card || card.dataset.resolved === 'true') return;
  const actions = card.querySelector('.approval-actions');
  if (!actions) return;

  const chips = DENY_QUICK_REASONS.map((r, i) =>
    `<button type="button" class="deny-chip" data-chip-idx="${i}">${escapeHtml(r)}</button>`
  ).join('');

  actions.innerHTML = `
    <div class="deny-reason-panel">
      <div class="deny-chips">${chips}</div>
      <textarea class="deny-reason-input" maxlength="200" placeholder="补充说明（可选）" rows="2"></textarea>
      <div class="deny-reason-actions">
        <button type="button" class="btn btn-danger btn-sm" data-deny-action="submit">提交拒绝</button>
        <button type="button" class="btn btn-secondary btn-sm" data-deny-action="cancel">取消</button>
      </div>
    </div>
  `;

  let selectedReason = '';
  actions.querySelectorAll('.deny-chip').forEach((chip, idx) => {
    chip.addEventListener('click', () => {
      if (chip.classList.contains('is-selected')) {
        chip.classList.remove('is-selected');
        selectedReason = '';
      } else {
        actions.querySelectorAll('.deny-chip').forEach(c => c.classList.remove('is-selected'));
        chip.classList.add('is-selected');
        selectedReason = DENY_QUICK_REASONS[idx];
      }
    });
  });

  actions.querySelector('[data-deny-action="submit"]')?.addEventListener('click', () => {
    const text = (actions.querySelector('.deny-reason-input')?.value || '').trim();
    const reason = [selectedReason, text].filter(Boolean).join('：') || '';
    resolveApproval(approvalId, 'deny', reason);
  });
  actions.querySelector('[data-deny-action="cancel"]')?.addEventListener('click', () => {
    restoreApprovalActions(card);
  });
}

function restoreApprovalActions(card) {
  const actions = card.querySelector('.approval-actions');
  if (!actions) return;
  const approvalId = card.dataset.approvalId;
  actions.innerHTML = `
    <button class="btn btn-primary btn-sm" data-approval-action="approve" data-approval-id="${escapeHtml(approvalId)}">批准</button>
    <button class="btn btn-danger btn-sm" data-approval-action="deny" data-approval-id="${escapeHtml(approvalId)}">拒绝</button>
  `;
  card.querySelector('[data-approval-action="approve"]')?.addEventListener('click', () => resolveApproval(approvalId, 'approve'));
  card.querySelector('[data-approval-action="deny"]')?.addEventListener('click', () => showDenyReasonPanel(approvalId));
}

async function resolveApproval(approvalId, decision, reason = null) {
  try {
    const body = { decision: decision };
    if (reason !== null && reason !== '') body.reason = reason;
    await api(`/approvals/${encodeURIComponent(approvalId)}/resolve`, {
      method: 'POST',
      body: body
    });
    updateApprovalCardStatus(approvalId, decision, reason || '');
  } catch (e) {
    showToast('审批提交失败: ' + e.message, 'error');
  }
}

function findApprovalCard(approvalId) {
  const cards = document.querySelectorAll('.approval-card');
  for (const c of cards) {
    if (c.dataset.approvalId === approvalId) return c;
  }
  return null;
}

function updateApprovalCardStatus(approvalId, decision, reason) {
  const card = findApprovalCard(approvalId);
  if (!card) return;
  if (card.dataset.resolved === 'true') return;
  card.dataset.resolved = 'true';

  card.classList.add('is-resolved');
  const actions = card.querySelector('.approval-actions');
  if (actions) {
    let label, cls;
    if (decision === 'approve') { label = '已批准'; cls = 'approved'; }
    else if (decision === 'deny') { label = '已拒绝'; cls = 'denied'; }
    else if (decision === 'expired' || decision === 'timeout') { label = '已超时'; cls = 'denied'; }
    else { label = '已结束'; cls = 'denied'; }
    actions.innerHTML = `<div class="approval-resolved-tag ${cls}">${reason ? label + '（' + escapeHtml(reason) + '）' : label}</div>`;
  }

  if (pendingApprovalCount > 0) pendingApprovalCount--;
  updateSendBtnState();
}

// ========== 打字指示器 ==========
function appendTypingIndicator() {
  welcomeScreenEl.style.display = 'none';
  const msg = document.createElement('div');
  msg.className = 'message assistant';
  msg.id = 'typingMsg';
  msg.innerHTML = `
    <div class="message-role assistant">Assistant</div>
    <div class="message-bubble"><div class="typing-indicator"><span></span><span></span><span></span></div></div>
  `;
  messagesEl.appendChild(msg);
  scrollMessagesToBottom();
}

function removeTypingIndicator() {
  const el = document.getElementById('typingMsg');
  if (el) el.remove();
}

// ========== SSE 解析 ==========
async function readSSE(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sepIdx;
    while ((sepIdx = buffer.indexOf('\n\n')) !== -1) {
      const rawEvent = buffer.slice(0, sepIdx);
      buffer = buffer.slice(sepIdx + 2);

      const lines = rawEvent.split('\n');
      let dataStr = '';
      for (const line of lines) {
        if (line.startsWith('data:')) dataStr += line.slice(5).trim();
      }
      if (!dataStr) continue;

      try {
        const evt = JSON.parse(dataStr);
        onEvent(evt);
      } catch (e) {
        console.warn('SSE 事件解析失败:', dataStr, e);
      }
    }
  }
}

// ========== 中断控制 ==========
function immediateCancel() {
  if (streamState !== StreamState.STREAMING) return;
  streamState = StreamState.STOPPING;
  updateSendBtnToStopBtn(true);

  if (abortController) abortController.abort();

  fetch(API_BASE + '/chat/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: currentSessionId, mode: 'immediate' }),
  }).catch(() => {});
}

function gracefulInterrupt(newMessage) {
  if (streamState !== StreamState.STREAMING) return;
  streamState = StreamState.GRACEFUL;
  updateSendBtnToStopBtn(true);

  fetch(API_BASE + '/chat/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: currentSessionId,
      mode: 'graceful',
      new_message: newMessage,
    }),
  }).catch(() => {});

  pendingGracefulMessage = newMessage;
  showToast('等待回复断点后自动发送...', '');
}

// ========== 按钮状态管理 ==========
function updateSendBtnToStopBtn(disabled) {
  sendBtnEl.textContent = disabled ? '■ …' : '■ 停止';
  sendBtnEl.className = 'send-btn send-btn-stop';
  sendBtnEl.disabled = disabled;
  messageInputEl.placeholder = '输入新消息 (Ctrl+Enter 自动中断)';
}

function updateSendBtnToSend() {
  sendBtnEl.textContent = '发送';
  sendBtnEl.className = 'send-btn';
  sendBtnEl.disabled = isSending || pendingApprovalCount > 0;
  messageInputEl.placeholder = '输入消息... (Ctrl+Enter 发送)';
}

function updateSendBtnState() {
  if (streamState === StreamState.STREAMING) {
    updateSendBtnToStopBtn(false);
  } else if (streamState === StreamState.STOPPING || streamState === StreamState.GRACEFUL) {
    updateSendBtnToStopBtn(true);
  } else {
    updateSendBtnToSend();
  }
}

// ========== 主发送函数 ==========
async function sendMessage(textOverride) {
  const text = textOverride || messageInputEl.value.trim();
  if (!text) return;

  if (streamState === StreamState.STREAMING) {
    gracefulInterrupt(text);
    return;
  }
  if (streamState === StreamState.STOPPING || streamState === StreamState.GRACEFUL) {
    showToast('正在中断中，请稍候...', '');
    return;
  }

  if (!currentSessionId) sessionTitleEl.textContent = '新会话';

  appendMessage('user', text);
  // 用户主动发消息：强制滚动到底部，确保看到自己的消息和接下来的回应
  scrollMessagesToBottom(true);
  _prevTodoStepStatuses = {};
  messageInputEl.value = '';
  autoResize();
  isSending = true;

  abortController = new AbortController();
  streamState = StreamState.STREAMING;
  updateSendBtnToStopBtn(false);

  let { msg: streamMsg, bubble: streamBubble } = createStreamMessage();
  let rounds = [{ el: streamBubble, text: '' }];
  let roundIdx = 0;

  try {
    const res = await fetch(API_BASE + '/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: currentSessionId, message: text }),
      signal: abortController.signal,
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || `HTTP ${res.status}`);
    }

    await readSSE(res, (evt) => {
      switch (evt.type) {
        case 'session':
          currentSessionId = evt.session_id;
          persistCurrentSession();
          // 标题占位：用首条消息前 20 字（比 session id 更有意义），存入全局供列表渲染复用
          if (typeof text === 'string' && text) {
            window._firstMessagePreview = window._firstMessagePreview || {};
            if (!window._firstMessagePreview[currentSessionId]) {
              window._firstMessagePreview[currentSessionId] = text.slice(0, 20);
            }
            sessionTitleEl.textContent = text.slice(0, 20) + '...';
          } else {
            sessionTitleEl.textContent = currentSessionId.slice(0, 20) + '...';
          }
          break;
        case 'status': {
          // 状态文案注入当前活跃气泡的 .bubble-status（取代输入框上方独立状态条）
          const cur = rounds[roundIdx];
          if (cur && cur.el) {
            const stEl = cur.el.querySelector('.bubble-status');
            if (stEl) stEl.textContent = evt.message || evt.status || '正在思考…';
          }
          break;
        }
        case 'round_start':
          if (evt.loop_idx === 0) break;
          {
            const prev = rounds[rounds.length - 1];
            if (prev && prev.el) prev.el.classList.remove('is-streaming');
            if (prev && prev.text.trim()) {
              const sep = document.createElement('div');
              sep.className = 'divider-round';
              sep.textContent = `第 ${evt.loop_idx + 1} 步`;
              streamMsg.appendChild(sep);
            }
            const nb = _buildStreamBubble();
            streamMsg.appendChild(nb);
            rounds.push({ el: nb, text: '' });
            roundIdx = rounds.length - 1;
          }
          break;
        case 'text':
          rounds[roundIdx].text += evt.text;
          if (rounds[roundIdx].el) {
            rounds[roundIdx].el.classList.add('is-streaming');
            rounds[roundIdx].el.classList.add('has-text');
          }
          updateStreamBubble(rounds[roundIdx].el, rounds[roundIdx].text);
          break;
        case 'tool_start':
          _hideEmptyRoundBubble(rounds, roundIdx);
          _pauseDots(rounds, roundIdx);
          appendToolCard(streamMsg, evt);
          break;
        case 'tool':
          _hideEmptyRoundBubble(rounds, roundIdx);
          _pauseDots(rounds, roundIdx);
          appendToolCard(streamMsg, evt);
          break;
        case 'todo_init':
          _hideEmptyRoundBubble(rounds, roundIdx);
          _pauseDots(rounds, roundIdx);
          appendTodoCard(streamMsg, evt.todo);
          break;
        case 'todo_update':
          _hideEmptyRoundBubble(rounds, roundIdx);
          _pauseDots(rounds, roundIdx);
          appendTodoCard(streamMsg, evt.todo);
          break;
        case 'todo_complete':
          _hideEmptyRoundBubble(rounds, roundIdx);
          _pauseDots(rounds, roundIdx);
          appendTodoCard(streamMsg, evt.todo);
          break;
        case 'approval_request':
          _hideEmptyRoundBubble(rounds, roundIdx);
          _pauseDots(rounds, roundIdx);
          appendApprovalCard(streamMsg, evt);
          break;
        case 'approval_resolved':
          updateApprovalCardStatus(evt.approval_id, evt.decision, evt.reason || '');
          break;
        case 'interrupt':
          _cleanupStreamRounds(rounds, streamMsg);
          {
            const last = rounds[rounds.length - 1];
            if (last && last.el) updateStreamBubble(last.el, last.text + '\n\n> —— 回复已中断 ——');
          }
          break;
        case 'done':
          _cleanupStreamRounds(rounds, streamMsg);
          // 渲染 done.response —— 仅当它是"未流式传输过的新内容"时才追加
          // 正常 end_turn：text 事件已渲染全文，done.response 与之相同，追加会重复，需跳过
          // max_loops 总结 / stuck 终止 / 异常兜底：response 是未流式的新内容，需渲染
          if (evt.response) {
            let target = rounds[rounds.length - 1];
            if (!target || !target.el || !target.el.parentNode) {
              const nb = document.createElement('div');
              nb.className = 'message-bubble markdown';
              streamMsg.appendChild(nb);
              rounds.push({ el: nb, text: '' });
              target = rounds[rounds.length - 1];
            }
            const norm = (s) => (s || '').trim().replace(/\s+/g, ' ');
            const targetNorm = norm(target.text);
            const respNorm = norm(evt.response);
            const alreadyStreamed = targetNorm.length > 0 && (
              targetNorm === respNorm ||
              targetNorm.endsWith(respNorm) ||
              respNorm.endsWith(targetNorm)
            );
            if (!alreadyStreamed) {
              target.text = (target.text ? target.text + '\n\n' : '') + evt.response;
              updateStreamBubble(target.el, target.text);
            }
          }
          break;
        case 'output_filtered': {
          const filtered = evt.filtered_response;
          if (typeof filtered !== 'string') break;
          let target = null;
          for (let i = rounds.length - 1; i >= 0; i--) {
            if (rounds[i] && rounds[i].text.trim() && rounds[i].el && rounds[i].el.parentNode) {
              target = rounds[i];
              break;
            }
          }
          if (target) {
            target.text = filtered;
            updateStreamBubble(target.el, filtered);
            const bubbleEl = target.el;
            bubbleEl.classList.remove('output-filtered-flash');
            void bubbleEl.offsetWidth;
            bubbleEl.classList.add('output-filtered-flash');
            setTimeout(() => bubbleEl.classList.remove('output-filtered-flash'), 650);
          }
          break;
        }
        case 'error':
          _cleanupStreamRounds(rounds, streamMsg);
          // 修复点 3：追加显示 error.reason（deny 拦截原因）
          {
            const er = rounds[roundIdx];
            const reasonSuffix = evt.reason ? `\n\n> **原因：** ${escapeHtml(evt.reason)}` : '';
            if (er && er.el) {
              updateStreamBubble(er.el, er.text + '\n\n> **[错误]** ' + escapeHtml(evt.message) + reasonSuffix);
            }
            showToast('流式错误: ' + evt.message + (evt.reason ? '（' + evt.reason + '）' : ''), 'error');
          }
          break;
      }
    });

    _cleanupStreamRounds(rounds, streamMsg);
    loadSessions();
    // 标题由后端异步 LLM 生成（fire-and-forget，~2-15s）。退避轮询
    // 直到标题不再是占位符（首条消息前 20 字）或达到 5 次尝试。
    // 同时把首条消息前 20 字存入 window._firstMessagePreview 供列表占位。
    if (window._titlePollTimer) clearTimeout(window._titlePollTimer);
    if (typeof text === 'string' && text && currentSessionId) {
      window._firstMessagePreview = window._firstMessagePreview || {};
      if (!window._firstMessagePreview[currentSessionId]) {
        window._firstMessagePreview[currentSessionId] = text.slice(0, 20);
      }
    }
    const _titlePlaceholder = (typeof text === 'string' && text) ? text.slice(0, 20) : '';
    let _titleAttempt = 0;
    const _titleDelays = [2000, 4000, 8000, 15000, 30000];
    function _pollTitle() {
      if (_titleAttempt >= _titleDelays.length) return;
      window._titlePollTimer = setTimeout(() => {
        if (window.HermesChatSession && typeof window.HermesChatSession.loadSessions === 'function') {
          window.HermesChatSession.loadSessions().then(() => {
            const cur = window.HermesChatSession._currentSessionData;
            if (cur && (!cur.title || !cur.title.trim()) && _titlePlaceholder &&
                _titleAttempt + 1 < _titleDelays.length) {
              _titleAttempt++;
              _pollTitle();
            }
          }).catch(() => {});
        }
      }, _titleDelays[_titleAttempt]);
    }
    if (_titlePlaceholder) {
      _pollTitle();
    } else {
      window._titlePollTimer = setTimeout(() => {
        if (window.HermesChatSession) window.HermesChatSession.loadSessions();
      }, 3000);
    }
  } catch (e) {
    _cleanupStreamRounds(rounds, streamMsg);
    const er2 = rounds[roundIdx];
    if (e.name === 'AbortError') {
      if (er2 && er2.el && er2.el.parentNode) {
        updateStreamBubble(er2.el, (er2.text || '') + '\n\n> —— 回复已中断 ——');
      }
    } else {
      if (er2 && er2.el && er2.el.parentNode) {
        if (er2.text) {
          updateStreamBubble(er2.el, er2.text + '\n\n> **[错误]** ' + escapeHtml(e.message));
        } else {
          er2.el.innerHTML = renderMarkdown('> **[错误]** ' + e.message);
        }
      }
      showToast('发送失败: ' + e.message, 'error');
    }
  } finally {
    isSending = false;
    streamState = StreamState.IDLE;
    updateSendBtnToSend();
    messageInputEl.focus();

    if (pendingGracefulMessage) {
      const msg = pendingGracefulMessage;
      pendingGracefulMessage = null;
      sendMessage(msg);
    }
  }
}

// 暴露给其他模块
window.HermesChatCore = {
  appendMessage,
  describeToolAction,
  createStreamMessage,
  updateStreamBubble,
  appendToolCard,
  appendMergedToolCard,
  appendToolCallCard,
  appendToolResultCard,
  appendTodoCard,
  updateTodoCard,
  appendApprovalCard,
  resolveApproval,
  updateApprovalCardStatus,
  appendTypingIndicator,
  removeTypingIndicator,
  readSSE,
  immediateCancel,
  gracefulInterrupt,
  updateSendBtnToStopBtn,
  updateSendBtnToSend,
  updateSendBtnState,
  sendMessage,
  renderToolValue,
};
