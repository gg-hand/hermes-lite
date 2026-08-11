/* ============================================================
   utils.js — 通用工具 + 全局状态 + Markdown 渲染
   对话页所有模块共享的基础设施
   ============================================================ */

// ========== 常量 ==========
const API_BASE = '';

const StreamState = {
  IDLE: 'idle',
  STREAMING: 'streaming',
  CANCELLING: 'cancelling',
  STOPPING: 'stopping',
  GRACEFUL: 'graceful',
};

const SESSION_STORAGE_KEY = 'teage_liu_current_session';

// ========== 全局状态（DOM 元素在 chat-main.js 初始化时赋值） ==========
let currentSessionId = null;
let currentConfig = null;
let isSending = false;
let streamState = StreamState.IDLE;
let abortController = null;
let pendingGracefulMessage = null;
let pendingApprovalCount = 0;
let _prevTodoStepStatuses = {};

// DOM 引用（chat-main.js 中 initGlobals() 赋值）
let messagesEl = null;
let sessionListEl = null;
let memoryPanelEl = null;
let filePanelEl = null;
let welcomeScreenEl = null;
let messageInputEl = null;
let sendBtnEl = null;
let toastEl = null;
let sessionTitleEl = null;
let statusDotEl = null;
let schedulePanelEl = null;
let sidebarEl = null;
let memorySearchBtnEl = null;
let memorySearchInputEl = null;
let memoryTypeFilterEl = null;
let memoryListEl = null;
// Task 3: 顶栏上下文元素引用
let topbarModelEl = null;
let topbarModelValueEl = null;
let topbarSessionNameEl = null;
let topbarSessionTextEl = null;
let topbarConnectionEl = null;

// ========== Markdown 渲染 ==========
function renderMarkdown(text) {
  if (!window.marked) return escapeHtml(text);
  try {
    const raw = marked.parse(text || '');
    return window.DOMPurify ? DOMPurify.sanitize(raw) : raw;
  } catch (e) {
    return escapeHtml(text);
  }
}

// ========== HTTP 封装 ==========
async function api(path, options = {}) {
  const opts = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (opts.body && typeof opts.body === 'object') opts.body = JSON.stringify(opts.body);
  const res = await fetch(API_BASE + path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// ========== 通知与状态 ==========
function showToast(msg, type = '') {
  if (!toastEl) return;
  toastEl.textContent = msg;
  toastEl.className = 'toast show ' + type;
  setTimeout(() => { toastEl.className = 'toast'; }, 2800);
}

// ========== 文本工具 ==========
function escapeHtml(text) {
  if (text == null) return '';
  const div = document.createElement('div');
  div.textContent = String(text);
  return div.innerHTML;
}

function truncateText(text, maxLen = 200) {
  if (!text) return '';
  text = String(text);
  return text.length > maxLen ? text.slice(0, maxLen) + '…' : text;
}

function formatTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  } catch {
    return String(iso).slice(0, 16);
  }
}

/** 将任意值格式化为可读字符串：对象/数组 JSON 美化，字符串原样 */
function stringifyValue(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** 安全解析 JSON 字符串，失败返回 null */
function safeParseJSON(str) {
  if (!str || typeof str !== 'string') return null;
  try {
    return JSON.parse(str);
  } catch {
    return null;
  }
}

/** 对 JSON 字符串做语法高亮（用于工具卡输入/输出） */
function highlightJSON(jsonStr) {
  if (!jsonStr) return '';
  const escaped = escapeHtml(jsonStr);
  // 简易正则高亮：key / string / number / boolean / null
  return escaped
    .replace(/("(?:\\.|[^"\\])*")(\s*:)/g, '<span class="json-key">$1</span>$2')
    .replace(/:\s*("(?:\\.|[^"\\])*")/g, ': <span class="json-str">$1</span>')
    .replace(/:\s*(-?\d+\.?\d*)/g, ': <span class="json-num">$1</span>')
    .replace(/:\s*(true|false|null)/g, ': <span class="json-bool">$1</span>');
}

// ========== 滚动 ==========
// 距底部阈值（px）：用户向上浏览超过此距离则视为“在看历史”，不自动跟随
const SCROLL_NEAR_BOTTOM_THRESHOLD = 80;

/** 判断用户当前是否接近底部（即“在看最新内容”） */
function isMessagesNearBottom() {
  if (!messagesEl) return true;
  const { scrollTop, scrollHeight, clientHeight } = messagesEl;
  return scrollHeight - scrollTop - clientHeight <= SCROLL_NEAR_BOTTOM_THRESHOLD;
}

/**
 * 滚动到消息区底部。
 * @param {boolean} force - true 强制滚动到底部；false（默认）仅在用户已在底部附近时跟随，
 *                          避免用户向上浏览历史时被强制拉回打断
 */
function scrollMessagesToBottom(force = false) {
  if (!messagesEl) return;
  if (!force && !isMessagesNearBottom()) return;
  messagesEl.scrollTop = messagesEl.scrollHeight;
  requestAnimationFrame(() => {
    if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
  });
  setTimeout(() => {
    if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
  }, 50);
  setTimeout(() => {
    if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
  }, 200);
}

// ========== 代码块增强 ==========
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

/** 代码块复制按钮事件委托（在 chat-main.js 绑定到 messagesEl） */
function handleCodeCopyClick(e) {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return false;
  const codeEl = btn.parentElement && btn.parentElement.querySelector('code');
  if (!codeEl) return false;
  navigator.clipboard.writeText(codeEl.textContent).then(() => {
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = '复制'; }, 1500);
  }).catch(() => {
    btn.textContent = '失败';
    setTimeout(() => { btn.textContent = '复制'; }, 1500);
  });
  return true;
}

// ========== 模态弹窗 ==========
function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.add('show');
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.remove('show');
}

// ========== Cron 表达式下次运行预览 ==========
/**
 * 用内置轻量 cron 解析器计算 cron 表达式接下来的 N 次运行时间。
 * 纯 JS 实现，无外部 CDN 依赖（原 croniter 包已从 npm 下架）。
 * 支持 5 字段标准 cron（分 时 日 月 周），语法：* 数字 , - /
 * @param {string} expr - 5 字段 cron 表达式
 * @param {number} count - 计算次数，默认 5
 * @returns {string} - 形如 "7/8 14:30 · 7/8 15:00 · ..."；表达式非法返回 '表达式无效'
 */
function previewCronNext(expr, count) {
  const n = count || 5;
  if (!expr || typeof expr !== 'string' || !expr.trim()) return '—';
  const trimmed = expr.trim();
  const fields = trimmed.split(/\s+/);
  if (fields.length !== 5) return '表达式无效';

  // 解析每个字段为有效值集合
  const minuteSet = _parseCronField(fields[0], 0, 59);
  const hourSet = _parseCronField(fields[1], 0, 23);
  const domSet = _parseCronField(fields[2], 1, 31);
  const monthSet = _parseCronField(fields[3], 1, 12);
  const dowSet = _parseCronField(fields[4], 0, 6); // 0=Sunday
  if (minuteSet === null || hourSet === null || domSet === null
      || monthSet === null || dowSet === null) {
    return '表达式无效';
  }

  // 从当前时间下一分钟开始逐分钟扫描，最多扫描 366 天避免死循环
  const results = [];
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(),
                         now.getHours(), now.getMinutes() + 1, 0, 0);
  const limit = new Date(start.getTime() + 366 * 24 * 60 * 60 * 1000);
  const cursor = new Date(start);
  while (results.length < n && cursor <= limit) {
    const dow = cursor.getDay(); // 0=Sunday
    if (minuteSet.has(cursor.getMinutes())
        && hourSet.has(cursor.getHours())
        && domSet.has(cursor.getDate())
        && monthSet.has(cursor.getMonth() + 1)
        && dowSet.has(dow)) {
      results.push(formatCronSlot(cursor));
    }
    cursor.setMinutes(cursor.getMinutes() + 1);
  }
  return results.length > 0 ? results.join(' · ') : '一年内无匹配时刻';
}

/**
 * 解析 cron 单字段为有效值集合。
 * 支持语法: 通配符、数字、区间(a-b)、列表(a,b,c)、步长(通配符/n 或 a-b/n)
 * @returns {Set<number>|null} null 表示语法非法
 */
function _parseCronField(field, min, max) {
  if (field === '*') {
    const s = new Set();
    for (let i = min; i <= max; i++) s.add(i);
    return s;
  }
  const s = new Set();
  for (const part of field.split(',')) {
    // 处理 step: a-b/n 或 */n
    const stepMatch = part.match(/^(.*)\/(\d+)$/);
    let range = part;
    let step = 1;
    if (stepMatch) {
      range = stepMatch[1];
      step = parseInt(stepMatch[2], 10);
      if (isNaN(step) || step < 1) return null;
    }
    let lo, hi;
    if (range === '*') {
      lo = min; hi = max;
    } else if (range.includes('-')) {
      const segs = range.split('-');
      if (segs.length !== 2) return null;
      lo = parseInt(segs[0], 10);
      hi = parseInt(segs[1], 10);
    } else {
      const v = parseInt(range, 10);
      if (isNaN(v)) return null;
      lo = v; hi = stepMatch ? max : v; // 单值带 step 时视为 v-max/step
    }
    if (isNaN(lo) || isNaN(hi) || lo < min || hi > max || lo > hi) return null;
    for (let i = lo; i <= hi; i += step) s.add(i);
  }
  return s.size > 0 ? s : null;
}

function formatCronSlot(d) {
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// 暴露给其他模块（ES module 模式不采用，保持全局函数风格与原代码一致）
window.TeageUtils = {
  API_BASE,
  StreamState,
  SESSION_STORAGE_KEY,
  renderMarkdown,
  api,
  showToast,
  escapeHtml,
  truncateText,
  formatTime,
  stringifyValue,
  safeParseJSON,
  highlightJSON,
  scrollMessagesToBottom,
  isMessagesNearBottom,
  enhanceCodeBlocks,
  handleCodeCopyClick,
  openModal,
  closeModal,
  previewCronNext,
};
