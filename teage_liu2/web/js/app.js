/* ============================================================
   app.js — 澜语对话页（teage_liu2 前端）
   移植自老系统 web/static/js/{utils,chat-core,chat-session,chat-main}.js
   适配 teage_liu2 事件协议（§3.1）：
   step_start / text_delta / reasoning_delta / step_end
   tool_use{name,input} / tool_result{name,tool_use_id,result,is_error}
   done{response,termination_reason,usage} / error{message}
   差异：liu2 无会话列表/历史回放 API → 会话列表存 localStorage，
        切换会话不回放历史；tool_use 事件无 tool_use_id →
        工具卡按 name FIFO 配对 tool_result。
   ============================================================ */

// ========== 通用工具（utils.js 移植） ==========
function escapeHtml(text) {
  if (text == null) return '';
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

function renderMarkdown(text) {
  // fail-closed:markdown 渲染器或净化器任一缺失/异常 → 一律回退纯文本转义,
  // 绝不直出未净化 HTML(2026-09-10 评审 P-16 修复)。
  if (!window.marked) return escapeHtml(text);
  try {
    const raw = marked.parse(text || '');
    return window.DOMPurify ? DOMPurify.sanitize(raw) : escapeHtml(text);
  } catch (e) {
    return escapeHtml(text);
  }
}

function safeParseJSON(str) {
  if (!str || typeof str !== 'string') return null;
  try { return JSON.parse(str); } catch { return null; }
}

function stringifyValue(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function highlightJSON(jsonStr) {
  if (!jsonStr) return '';
  const escaped = escapeHtml(jsonStr);
  return escaped
    .replace(/("(?:\\.|[^"\\])*")(\s*:)/g, '<span class="json-key">$1</span>$2')
    .replace(/:\s*("(?:\\.|[^"\\])*")/g, ': <span class="json-str">$1</span>')
    .replace(/:\s*(-?\d+\.?\d*)/g, ': <span class="json-num">$1</span>')
    .replace(/:\s*(true|false|null)/g, ': <span class="json-bool">$1</span>');
}

function renderToolValue(value) {
  if (value == null || value === '') return '<span class="text-muted">（空）</span>';
  let str = typeof value === 'string' ? value : stringifyValue(value);
  const parsed = safeParseJSON(str);
  if (parsed != null && typeof parsed === 'object') {
    return highlightJSON(JSON.stringify(parsed, null, 2));
  }
  return escapeHtml(str);
}

function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => { el.className = 'toast'; }, 2800);
}

// ========== DOM 引用 ==========
const messagesEl = document.getElementById('messages');
const sessionListEl = document.getElementById('sessionList');
const welcomeScreenEl = document.getElementById('welcomeScreen');
const messageInputEl = document.getElementById('messageInput');
const sendBtnEl = document.getElementById('sendBtn');
const sessionTitleEl = document.getElementById('sessionTitle');
const terminationBadgeEl = document.getElementById('terminationBadge');
const connectionDotEl = document.getElementById('connectionDot');

// ========== 会话存储（localStorage 版，liu2 无会话 API） ==========
const SESSIONS_KEY = 'liu2_sessions';
const CURRENT_KEY = 'liu2_current_session';

function loadSessions() {
  try { return JSON.parse(localStorage.getItem(SESSIONS_KEY)) || []; }
  catch { return []; }
}
function saveSessions(list) {
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(list));
}
function loadCurrentSession() {
  return localStorage.getItem(CURRENT_KEY) || null;
}
function persistCurrentSession() {
  if (currentSessionId) localStorage.setItem(CURRENT_KEY, currentSessionId);
}

let currentSessionId = loadCurrentSession();
let sessions = loadSessions();
if (currentSessionId && !sessions.find(s => s.id === currentSessionId)) {
  currentSessionId = null;
}

function newSessionId() {
  return 'user_' + Math.random().toString(16).slice(2, 14);
}
function ensureSessionEntry(firstMessage) {
  let entry = sessions.find(s => s.id === currentSessionId);
  if (!entry) {
    entry = { id: currentSessionId, title: (firstMessage || '').slice(0, 20), created: Date.now(), updated: Date.now() };
    sessions.unshift(entry);
  } else {
    entry.updated = Date.now();
    if (firstMessage && (!entry.title || entry.title === '新会话')) {
      entry.title = firstMessage.slice(0, 20);
    }
  }
  saveSessions(sessions);
  renderSessionList();
}
function renderSessionList() {
  if (!sessions.length) {
    sessionListEl.innerHTML =
      '<div class="empty-state"><div class="empty-state-icon">≈</div><div class="empty-state-text">暂无会话<br>点击上方 + 新建</div></div>';
    return;
  }
  sessionListEl.innerHTML = sessions.map(s => `
    <div class="session-item${s.id === currentSessionId ? ' active' : ''}" data-sid="${escapeHtml(s.id)}">
      <span class="s-icon">≈</span>
      <span class="s-title" title="${escapeHtml(s.id)}">${escapeHtml(s.title || s.id)}</span>
      <button class="s-del" title="删除会话">×</button>
    </div>`).join('');
}
sessionListEl.addEventListener('click', (e) => {
  const item = e.target.closest('.session-item');
  if (!item) return;
  const sid = item.dataset.sid;
  if (e.target.classList.contains('s-del')) {
    e.stopPropagation();
    if (!confirm('删除该会话记录？（仅移除列表项，不影响服务端历史）')) return;
    sessions = sessions.filter(s => s.id !== sid);
    saveSessions(sessions);
    if (currentSessionId === sid) {
      currentSessionId = null;
      localStorage.removeItem(CURRENT_KEY);
      sessionTitleEl.textContent = '新会话';
      clearMessages();
    }
    renderSessionList();
    return;
  }
  switchSession(sid);
});

function switchSession(sid) {
  if (sid === currentSessionId) return;
  if (streamState === StreamState.STREAMING) {
    showToast('当前有消息正在生成，请稍后再切换会话', '');
    return;
  }
  currentSessionId = sid;
  persistCurrentSession();
  const entry = sessions.find(s => s.id === sid);
  sessionTitleEl.textContent = entry ? (entry.title || sid) : sid;
  terminationBadgeEl.hidden = true;
  clearMessages();
  renderSessionList();
  loadHistory(sid, null);
}

function newSession() {
  currentSessionId = newSessionId();
  persistCurrentSession();
  sessionTitleEl.textContent = '新会话';
  terminationBadgeEl.hidden = true;
  clearMessages();
}
document.getElementById('btnNewSession').addEventListener('click', newSession);

function clearMessages() {
  messagesEl.innerHTML = '';
  hist = null;
  messagesEl.appendChild(welcomeScreenEl);
  welcomeScreenEl.style.display = '';
}

// ========== 滚动 ==========
const SCROLL_NEAR_BOTTOM_THRESHOLD = 80;
function isMessagesNearBottom() {
  const { scrollTop, scrollHeight, clientHeight } = messagesEl;
  return scrollHeight - scrollTop - clientHeight <= SCROLL_NEAR_BOTTOM_THRESHOLD;
}
function scrollMessagesToBottom(force = false) {
  if (!force && !isMessagesNearBottom()) return;
  messagesEl.scrollTop = messagesEl.scrollHeight;
  setTimeout(() => { messagesEl.scrollTop = messagesEl.scrollHeight; }, 60);
}

// ========== 代码块复制按钮 ==========
function enhanceCodeBlocks(container) {
  if (!container) return;
  container.querySelectorAll('pre').forEach(pre => {
    if (pre.querySelector('.copy-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = '复制';
    btn.type = 'button';
    pre.appendChild(btn);
  });
}
messagesEl.addEventListener('click', (e) => {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return;
  const codeEl = btn.parentElement && btn.parentElement.querySelector('code');
  if (!codeEl) return;
  navigator.clipboard.writeText(codeEl.textContent).then(() => {
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = '复制'; }, 1500);
  }).catch(() => {
    btn.textContent = '失败';
    setTimeout(() => { btn.textContent = '复制'; }, 1500);
  });
});

// ========== 消息渲染（chat-core.js 移植精简版） ==========
function appendMessage(role, content) {
  welcomeScreenEl.style.display = 'none';
  const msg = document.createElement('div');
  msg.className = 'message ' + (role === 'user' ? 'user' : 'assistant');
  const roleLabel = role === 'user' ? 'You' : 'Assistant';
  msg.innerHTML =
    `<div class="message-role ${role}">${roleLabel}</div>` +
    `<div class="message-bubble">${role === 'user' ? escapeHtml(content || '') : renderMarkdown(content || '')}</div>`;
  if (role === 'assistant') enhanceCodeBlocks(msg.querySelector('.message-bubble'));
  messagesEl.appendChild(msg);
  scrollMessagesToBottom();
  return msg;
}

// 思考块（默认折叠；思考中为 thinking 态——无底色纯文字行，完成后才呈块状）
function _buildReasoningBlock() {
  const block = document.createElement('div');
  block.className = 'reasoning-block thinking collapsed';
  const header = document.createElement('div');
  header.className = 'reasoning-header';
  header.innerHTML = '<span>💡</span><span class="reasoning-title">思考中...</span><span class="reasoning-toggle">▸</span>';
  const content = document.createElement('div');
  content.className = 'reasoning-content';
  content.style.display = 'none';
  block.appendChild(header);
  block.appendChild(content);
  header.addEventListener('click', () => {
    const isCollapsed = block.classList.toggle('collapsed');
    content.style.display = isCollapsed ? 'none' : '';
    header.querySelector('.reasoning-toggle').textContent = isCollapsed ? '▸' : '▾';
  });
  return block;
}

// 流式气泡（加载提示行在气泡外、气泡上方；引用挂在 bubble._statusEl。
// 气泡初始隐藏：首字到达前只显示状态行，不渲染空气泡壳）
function _buildStreamBubble() {
  const status = document.createElement('div');
  status.className = 'stream-status';
  status.innerHTML = '<span class="streaming-dots"><i></i><i></i><i></i></span><span>正在思考…</span>';
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble markdown is-streaming';
  bubble.style.display = 'none';
  const content = document.createElement('div');
  content.className = 'bubble-content';
  bubble.appendChild(content);
  bubble._statusEl = status;
  return bubble;
}

function updateStreamBubble(bubble, fullText) {
  if (bubble.style.display === 'none') bubble.style.display = '';
  const content = bubble.querySelector('.bubble-content') || bubble;
  content.innerHTML = renderMarkdown(fullText);
  enhanceCodeBlocks(bubble);
  scrollMessagesToBottom();
}

function _cleanupStreamRounds(rounds, streamMsg) {
  rounds.forEach(r => {
    if (!r.el) return;
    r.el.classList.remove('is-streaming');
    if (r.el._statusEl) { r.el._statusEl.remove(); delete r.el._statusEl; }
  });
  rounds.forEach(r => {
    if (!r.text.trim() && !(r.reasoning && r.reasoning.trim()) && r.el && r.el.parentNode === streamMsg) {
      r.el.remove();
    }
  });
  const hasText = rounds.some(r => r.text.trim());
  const hasReasoning = rounds.some(r => r.reasoning && r.reasoning.trim());
  const hasCards = streamMsg && streamMsg.querySelector('.tool-card');
  if (!hasText && !hasReasoning && !hasCards && streamMsg && streamMsg.parentNode) streamMsg.remove();
}

// 工具卡（tool_use 无 tool_use_id → 按 name FIFO 配对 tool_result）
const _pendingToolCards = {}; // name -> [card, ...]
function appendToolCard(msgEl, name, input) {
  const details = document.createElement('details');
  details.className = 'tool-card';
  details.dataset.toolName = name;
  details.open = true;
  details.innerHTML = `
    <summary class="tool-card-header">
      <span class="tool-card-name">🔧 ${escapeHtml(name)}</span>
      <span class="tool-card-action">${escapeHtml(String(stringifyValue(input)).slice(0, 60))}</span>
      <span class="tool-card-status is-running">⏳ 运行中</span>
    </summary>
    <div class="tool-card-body">
      <div class="tool-card-section">
        <div class="tool-card-label">输入</div>
        <pre class="tool-card-content">${renderToolValue(input)}</pre>
      </div>
    </div>`;
  msgEl.appendChild(details);
  (_pendingToolCards[name] = _pendingToolCards[name] || []).push(details);
  scrollMessagesToBottom();
}

function completeToolCard(name, result, isError) {
  const queue = _pendingToolCards[name];
  const card = queue && queue.length ? queue.shift() : null;
  if (!card) return;
  const statusSpan = card.querySelector('.tool-card-status');
  if (statusSpan) {
    statusSpan.className = 'tool-card-status ' + (isError ? 'is-error' : 'is-done');
    statusSpan.textContent = isError ? '✗ 失败' : '✓ 完成';
  }
  const bodyDiv = card.querySelector('.tool-card-body');
  if (bodyDiv) {
    const section = document.createElement('div');
    section.className = 'tool-card-section';
    section.innerHTML =
      `<div class="tool-card-label">结果</div>` +
      `<pre class="tool-card-content">${renderToolValue(result)}</pre>`;
    bodyDiv.appendChild(section);
  }
  if (isError) card.classList.add('is-error');
  setTimeout(() => { card.open = false; }, 3000);
  scrollMessagesToBottom();
}

// ========== 历史回放（分页拉取，避免一次性渲染全部卡顿） ==========
const HISTORY_PAGE_SIZE = 30;
let hist = null; // { sid, oldestId, hasMore, loading, btn }

function _parseBlocks(m) {
  const raw = m.content_blocks;
  if (!raw) return null;
  const arr = typeof raw === 'string' ? safeParseJSON(raw) : raw;
  return Array.isArray(arr) ? arr : null;
}

function _newAssistantShell() {
  const m = document.createElement('div');
  m.className = 'message assistant';
  const role = document.createElement('div');
  role.className = 'message-role assistant';
  role.textContent = 'Assistant';
  m.appendChild(role);
  return m;
}

// 历史工具卡：直接呈完成态（输入 + 结果一次渲染，closed 减少刷屏）
function _buildHistoryToolCard(name, input, result, isError) {
  const details = document.createElement('details');
  details.className = 'tool-card' + (isError ? ' is-error' : '');
  details.dataset.toolName = name;
  details.innerHTML = `
    <summary class="tool-card-header">
      <span class="tool-card-name">🔧 ${escapeHtml(name)}</span>
      <span class="tool-card-action">${escapeHtml(String(stringifyValue(input)).slice(0, 60))}</span>
      <span class="tool-card-status ${isError ? 'is-error' : 'is-done'}">${isError ? '✗ 失败' : '✓ 完成'}</span>
    </summary>
    <div class="tool-card-body">
      <div class="tool-card-section">
        <div class="tool-card-label">输入</div>
        <pre class="tool-card-content">${renderToolValue(input)}</pre>
      </div>
      <div class="tool-card-section">
        <div class="tool-card-label">结果</div>
        <pre class="tool-card-content">${renderToolValue(result)}</pre>
      </div>
    </div>`;
  return details;
}

function _buildDoneReasoningBlock(reasoning) {
  const block = _buildReasoningBlock();
  block.classList.remove('thinking');
  const t = block.querySelector('.reasoning-title');
  if (t) t.textContent = '思考完成';
  const content = block.querySelector('.reasoning-content');
  if (content) content.innerHTML = renderMarkdown(reasoning || '');
  return block;
}

function _appendHistoryBubble(shell, mdText) {
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble markdown';
  bubble.innerHTML = renderMarkdown(mdText);
  enhanceCodeBlocks(bubble);
  shell.appendChild(bubble);
}

function _renderHistoryPage(msgs, prepend) {
  if (!hist || !msgs.length) return;
  welcomeScreenEl.style.display = 'none';
  // 先收集本页(含已加载页) tool_results：按 tool_use_id 配对工具卡结果
  const results = {};
  for (const m of msgs) {
    if (m.role === 'user' && m.message_type === 'tool_results') {
      for (const b of _parseBlocks(m) || []) {
        if (b && b.type === 'tool_result' && b.tool_use_id) {
          results[b.tool_use_id] = { content: b.content, is_error: !!b.is_error };
        }
      }
    }
  }

  const frag = document.createDocumentFragment();
  for (const m of msgs) {
    if (m.role === 'user') {
      // tool_results 聚合消息不单独渲染（结果已并入工具卡）
      if (m.message_type === 'tool_results') continue;
      if (!String(m.content || '').trim()) continue;
      const um = document.createElement('div');
      um.className = 'message user';
      um.innerHTML =
        '<div class="message-role user">You</div>' +
        `<div class="message-bubble">${escapeHtml(m.content)}</div>`;
      frag.appendChild(um);
      continue;
    }
    // assistant：reasoning 折叠块 + content_blocks(文本/工具卡)回放
    const shell = _newAssistantShell();
    let rendered = false;
    if (m.reasoning && String(m.reasoning).trim()) {
      shell.appendChild(_buildDoneReasoningBlock(m.reasoning));
      rendered = true;
    }
    const blocks = _parseBlocks(m);
    if (blocks && blocks.length) {
      for (const b of blocks) {
        if (!b || typeof b !== 'object') continue;
        if (b.type === 'text' && String(b.text || '').trim()) {
          _appendHistoryBubble(shell, b.text);
          rendered = true;
        } else if (b.type === 'tool_use') {
          const r = results[b.id] || { content: '', is_error: false };
          shell.appendChild(_buildHistoryToolCard(b.name || 'tool', b.input || {}, r.content, r.is_error));
          rendered = true;
        }
      }
    }
    if (!rendered && String(m.content || '').trim()) {
      // 旧行无 content_blocks → 回退纯文本
      _appendHistoryBubble(shell, m.content);
      rendered = true;
    }
    if (rendered) frag.appendChild(shell);
  }
  if (!frag.childNodes.length) return;

  if (prepend) {
    const first = messagesEl.querySelector('.message');
    const prevTop = messagesEl.scrollTop;
    const prevHeight = messagesEl.scrollHeight;
    if (first) messagesEl.insertBefore(frag, first);
    else messagesEl.appendChild(frag);
    // 保持视口锚定在原内容上
    messagesEl.scrollTop = messagesEl.scrollHeight - prevHeight + prevTop;
  } else {
    messagesEl.appendChild(frag);
    scrollMessagesToBottom(true);
  }
}

function _updateLoadMoreBtn() {
  if (!hist) return;
  if (hist.hasMore && !hist.btn) {
    const btn = document.createElement('button');
    btn.className = 'load-more-btn';
    btn.type = 'button';
    btn.textContent = '加载更早消息';
    btn.addEventListener('click', () => {
      if (hist && hist.oldestId != null) loadHistory(hist.sid, hist.oldestId);
    });
    const first = messagesEl.querySelector('.message');
    if (first) messagesEl.insertBefore(btn, first);
    else messagesEl.appendChild(btn);
    hist.btn = btn;
  } else if (!hist.hasMore && hist.btn) {
    hist.btn.remove();
    hist.btn = null;
  }
}

async function loadHistory(sid, beforeId) {
  const isPrepend = beforeId != null;
  if (!isPrepend) {
    hist = { sid, oldestId: null, hasMore: false, loading: false, btn: null };
  }
  if (!hist || hist.loading || hist.sid !== sid) return;
  hist.loading = true;
  try {
    let url = `/sessions/${encodeURIComponent(sid)}/messages?limit=${HISTORY_PAGE_SIZE}`;
    if (beforeId != null) url += `&before_id=${beforeId}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!hist || hist.sid !== sid) return; // 等待期间已切换会话
    const msgs = data.messages || [];
    _renderHistoryPage(msgs, isPrepend);
    hist.hasMore = !!data.has_more;
    const ids = msgs.map(m => m.id).filter(x => x != null);
    if (ids.length) {
      hist.oldestId = hist.oldestId == null ? Math.min(...ids) : Math.min(hist.oldestId, ...ids);
    }
    _updateLoadMoreBtn();
  } catch (e) {
    showToast('历史加载失败: ' + e.message, 'error');
  } finally {
    if (hist) hist.loading = false;
  }
}

// ========== SSE 解析（utils.js readSSE 移植） ==========
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
      let dataStr = '';
      for (const line of rawEvent.split('\n')) {
        if (line.startsWith('data:')) dataStr += line.slice(5).trim();
      }
      if (!dataStr) continue;
      try {
        onEvent(JSON.parse(dataStr));
      } catch (e) {
        console.warn('SSE 事件解析失败:', dataStr, e);
      }
    }
  }
}

// ========== 流状态 ==========
const StreamState = { IDLE: 'idle', STREAMING: 'streaming' };
let streamState = StreamState.IDLE;
let abortController = null;

function updateSendBtnToStopBtn() {
  sendBtnEl.textContent = '■ 停止';
  sendBtnEl.className = 'send-btn send-btn-stop';
}
function updateSendBtnToSend() {
  sendBtnEl.textContent = '发送';
  sendBtnEl.className = 'send-btn';
}

// ========== 主发送函数（liu2 /chat/stream 事件适配） ==========
async function sendMessage() {
  const text = messageInputEl.value.trim();
  if (!text) return;
  if (streamState === StreamState.STREAMING) {
    // liu2 无 /chat/cancel，流式中新消息直接拒绝
    showToast('当前有消息正在生成，请先停止', '');
    return;
  }

  if (!currentSessionId) {
    currentSessionId = newSessionId();
  }
  ensureSessionEntry(text);
  appendMessage('user', text);
  scrollMessagesToBottom(true);
  messageInputEl.value = '';
  autoResize();
  terminationBadgeEl.hidden = true;

  abortController = new AbortController();
  streamState = StreamState.STREAMING;
  updateSendBtnToStopBtn();

  // 流式消息容器 + 第一轮气泡
  welcomeScreenEl.style.display = 'none';
  const streamMsg = document.createElement('div');
  streamMsg.className = 'message assistant';
  const roleEl = document.createElement('div');
  roleEl.className = 'message-role assistant';
  roleEl.textContent = 'Assistant';
  streamMsg.appendChild(roleEl);
  messagesEl.appendChild(streamMsg);
  let bubble = _buildStreamBubble();
  streamMsg.appendChild(bubble._statusEl);
  streamMsg.appendChild(bubble);
  scrollMessagesToBottom();

  const rounds = [{ el: bubble, text: '', reasoning: '', reasoning_el: null }];
  let roundIdx = 0;
  let stepCount = 0;

  try {
    const res = await fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: currentSessionId, user_input: text }),
      signal: abortController.signal,
    });
    if (!res.ok || !res.body) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.error || `HTTP ${res.status}`);
    }

    await readSSE(res, (evt) => {
      switch (evt.type) {
        case 'step_start':
          // 第一步沿用已建的气泡；后续 step（loop 轮次）→ 分隔条 + 新气泡
          stepCount++;
          if (stepCount > 1) {
            const prev = rounds[rounds.length - 1];
            if (prev && prev.el) prev.el.classList.remove('is-streaming');
            if (prev && prev.text.trim()) {
              const sep = document.createElement('div');
              sep.className = 'divider-round';
              sep.textContent = `第 ${stepCount} 轮`;
              streamMsg.appendChild(sep);
              const nb = _buildStreamBubble();
              streamMsg.appendChild(nb._statusEl);
              streamMsg.appendChild(nb);
              rounds.push({ el: nb, text: '', reasoning: '', reasoning_el: null });
              roundIdx = rounds.length - 1;
            }
          }
          break;
        case 'text_delta': {
          const r = rounds[roundIdx];
          r.text += evt.text || '';
          if (!r.text.trim()) break; // 纯空白 delta（如首包 \n\n）不显示空气泡壳
          r.el.classList.add('has-text');
          const st = r.el._statusEl;
          if (st) st.classList.add('hidden');
          updateStreamBubble(r.el, r.text);
          break;
        }
        case 'reasoning_delta': {
          const r = rounds[roundIdx];
          r.reasoning = (r.reasoning || '') + (evt.text || '');
          if (!r.reasoning_el) {
            r.reasoning_el = _buildReasoningBlock();
            if (r.el && r.el.parentNode) {
              const anchor = r.el._statusEl || r.el;
              anchor.parentNode.insertBefore(r.reasoning_el, anchor);
            } else {
              streamMsg.appendChild(r.reasoning_el);
            }
          }
          const content = r.reasoning_el.querySelector('.reasoning-content');
          if (content) content.innerHTML = renderMarkdown(r.reasoning);
          const title = r.reasoning_el.querySelector('.reasoning-title');
          if (title) title.textContent = '思考中...';
          scrollMessagesToBottom();
          break;
        }
        case 'tool_use':
          rounds[roundIdx].el.classList.remove('is-streaming');
          if (!rounds[roundIdx].text.trim() && rounds[roundIdx].el.parentNode) {
            rounds[roundIdx].el.style.display = 'none';
          }
          appendToolCard(streamMsg, evt.name || 'tool', evt.effective_input || evt.input || {});
          break;
        case 'tool_result':
          completeToolCard(evt.name || 'tool', evt.result || '', !!evt.is_error);
          break;
        case 'done': {
          _cleanupStreamRounds(rounds, streamMsg);
          // 思考区标记完成
          rounds.forEach(r => {
            if (r.reasoning_el) {
              r.reasoning_el.classList.remove('thinking');
              const t = r.reasoning_el.querySelector('.reasoning-title');
              if (t) t.textContent = '思考完成';
            }
          });
          // done.response：max_loops 总结等未流式传输的新内容
          if (evt.response) {
            const norm = (s) => (s || '').trim().replace(/\s+/g, ' ');
            const last = rounds[rounds.length - 1];
            const lastNorm = last ? norm(last.text) : '';
            const respNorm = norm(evt.response);
            const alreadyStreamed = lastNorm.length > 0 && (
              lastNorm === respNorm || lastNorm.endsWith(respNorm) || respNorm.endsWith(lastNorm)
            );
            if (!alreadyStreamed) {
              if (!last || !last.el || !last.el.parentNode) {
                appendMessage('assistant', evt.response);
              } else {
                last.text = (last.text ? last.text + '\n\n' : '') + evt.response;
                updateStreamBubble(last.el, last.text);
              }
            }
          }
          // 终止原因徽章
          if (evt.termination_reason) {
            terminationBadgeEl.textContent = '终止: ' + evt.termination_reason;
            terminationBadgeEl.hidden = false;
          }
          break;
        }
        case 'error': {
          _cleanupStreamRounds(rounds, streamMsg);
          const er = rounds[roundIdx];
          if (er && er.el) {
            updateStreamBubble(er.el, er.text + '\n\n> **[错误]** ' + escapeHtml(evt.message || '未知错误'));
          } else {
            appendMessage('assistant', '**[错误]** ' + (evt.message || '未知错误'));
          }
          showToast('流式错误: ' + (evt.message || ''), 'error');
          break;
        }
      }
    });

    _cleanupStreamRounds(rounds, streamMsg);
  } catch (e) {
    _cleanupStreamRounds(rounds, streamMsg);
    const er = rounds[roundIdx];
    if (e.name === 'AbortError') {
      if (er && er.el && er.el.parentNode) {
        updateStreamBubble(er.el, (er.text || '') + '\n\n> —— 回复已中断 ——');
      }
    } else {
      if (er && er.el && er.el.parentNode) {
        if (er.text) {
          updateStreamBubble(er.el, er.text + '\n\n> **[错误]** ' + escapeHtml(e.message));
        } else {
          er.el.innerHTML = renderMarkdown('> **[错误]** ' + escapeHtml(e.message));
        }
      } else {
        appendMessage('assistant', '**[错误]** ' + e.message);
      }
      showToast('发送失败: ' + e.message, 'error');
    }
  } finally {
    streamState = StreamState.IDLE;
    updateSendBtnToSend();
    messageInputEl.focus();
  }
}

// ========== 输入区交互 ==========
function autoResize() {
  messageInputEl.style.height = 'auto';
  messageInputEl.style.height = Math.min(messageInputEl.scrollHeight, 140) + 'px';
}
messageInputEl.addEventListener('input', autoResize);
messageInputEl.addEventListener('keydown', (e) => {
  // 沿用老系统 Ctrl+Enter 发送；Enter 也发送，Shift+Enter 换行
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
sendBtnEl.addEventListener('click', () => {
  if (streamState === StreamState.STREAMING) {
    if (abortController) abortController.abort();
    return;
  }
  sendMessage();
});

// ========== 连接状态探活 ==========
async function checkHealth() {
  try {
    const res = await fetch('/health');
    connectionDotEl.classList.toggle('off', !res.ok);
  } catch {
    connectionDotEl.classList.add('off');
  }
}
checkHealth();
setInterval(checkHealth, 15000);

// ========== 移动端侧栏 ==========
document.getElementById('menuToggle').addEventListener('click', () => {
  document.getElementById('sidebar').classList.toggle('open');
});
messagesEl.addEventListener('click', () => {
  document.getElementById('sidebar').classList.remove('open');
});

// ========== 初始化 ==========
renderSessionList();
if (currentSessionId) {
  const entry = sessions.find(s => s.id === currentSessionId);
  if (entry) sessionTitleEl.textContent = entry.title || currentSessionId;
  // 刷新后恢复当前会话的消息历史（服务端仍保留，仅前端未回放）
  loadHistory(currentSessionId, null);
}
messageInputEl.focus();
