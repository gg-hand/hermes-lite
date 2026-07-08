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

const SESSION_STORAGE_KEY = 'hermes_lite_current_session';

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
 * 用 croniter 计算 cron 表达式接下来的 N 次运行时间。
 * @param {string} expr - 5 字段 cron 表达式
 * @param {number} count - 计算次数，默认 5
 * @returns {string} - 形如 "7/8 14:30 · 7/8 15:00 · ..."；表达式非法返回 '表达式无效'
 */
function previewCronNext(expr, count) {
  const n = count || 5;
  if (!expr || typeof expr !== 'string' || !expr.trim()) return '—';
  const trimmed = expr.trim();
  // 优先使用 croniter 全局（CDN 引入）
  if (typeof window.croniter !== 'undefined') {
    try {
      const it = window.croniter.parse(trimmed, new Date());
      const arr = [];
      let d = it.next();
      if (!d || isNaN(d.getTime())) throw new Error('parse fail');
      arr.push(formatCronSlot(d));
      for (let i = 1; i < n; i++) {
        d = it.next();
        if (!d) break;
        arr.push(formatCronSlot(d));
      }
      return arr.join(' · ');
    } catch (e) {
      return '表达式无效';
    }
  }
  // 降级：仅做 5 段格式校验
  const parts = trimmed.split(/\s+/);
  if (parts.length !== 5) return '表达式无效';
  return '（需联网载入 croniter 才能预览）';
}

function formatCronSlot(d) {
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// 暴露给其他模块（ES module 模式不采用，保持全局函数风格与原代码一致）
window.HermesUtils = {
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
