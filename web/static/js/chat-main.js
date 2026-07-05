/* ============================================================
   chat-main.js — 初始化 + 事件绑定 + 健康检查
   最后加载的入口模块，负责串联所有模块
   ============================================================ */

// ========== 全局 DOM 引用赋值 ==========
function initGlobals() {
  messagesEl = document.getElementById('messages');
  sessionListEl = document.getElementById('sessionList');
  memoryPanelEl = document.getElementById('memoryPanel');
  filePanelEl = document.getElementById('filePanel');
  welcomeScreenEl = document.getElementById('welcomeScreen');
  messageInputEl = document.getElementById('messageInput');
  sendBtnEl = document.getElementById('sendBtn');
  toastEl = document.getElementById('toast');
  sessionTitleEl = document.getElementById('sessionTitle');
  statusDotEl = document.getElementById('statusDot');
  sidebarEl = document.getElementById('sidebar');
  memorySearchBtnEl = document.getElementById('memorySearchBtn');
  memorySearchInputEl = document.getElementById('memorySearchInput');
  memoryTypeFilterEl = document.getElementById('memoryTypeFilter');
  memoryListEl = document.getElementById('memoryList');
}

// ========== 输入框自动调整 ==========
function autoResize() {
  if (!messageInputEl) return;
  messageInputEl.style.height = 'auto';
  messageInputEl.style.height = Math.min(messageInputEl.scrollHeight, 150) + 'px';
}

// ========== 健康检查 ==========
async function checkHealth() {
  try {
    await api('/health');
    if (statusDotEl) statusDotEl.classList.remove('offline');
  } catch {
    if (statusDotEl) statusDotEl.classList.add('offline');
  }
}

// ========== 事件绑定 ==========
function bindEvents() {
  // 发送 / 停止按钮
  if (sendBtnEl) {
    sendBtnEl.addEventListener('click', () => {
      if (streamState === StreamState.STREAMING) {
        immediateCancel();
      } else {
        sendMessage();
      }
    });
  }

  // 输入框
  if (messageInputEl) {
    messageInputEl.addEventListener('input', autoResize);
    messageInputEl.addEventListener('keydown', (e) => {
      if (e.ctrlKey && e.key === 'Enter') {
        e.preventDefault();
        sendMessage();
      }
    });
  }

  // 侧边栏折叠 + 移动端遮罩
  const menuToggle = document.getElementById('menuToggle');
  const sidebarOverlay = document.getElementById('sidebarOverlay');
  if (menuToggle && sidebarEl) {
    // HTML 中 sidebar 已带 collapsed 类，双端默认收起
    // 移动端遮罩初始隐藏
    if (sidebarOverlay) sidebarOverlay.classList.remove('show');

    const toggleSidebar = () => {
      const willCollapse = !sidebarEl.classList.contains('collapsed');
      sidebarEl.classList.toggle('collapsed');
      // 移动端：展开时显示遮罩，收起时隐藏遮罩
      if (sidebarOverlay) {
        if (willCollapse) {
          sidebarOverlay.classList.remove('show');
        } else {
          sidebarOverlay.classList.add('show');
        }
      }
    };
    menuToggle.addEventListener('click', toggleSidebar);
    // 点击遮罩关闭侧栏
    if (sidebarOverlay) {
      sidebarOverlay.addEventListener('click', () => {
        sidebarEl.classList.add('collapsed');
        sidebarOverlay.classList.remove('show');
      });
    }
  }

  // 新建会话 / 设置
  const btnNewSession = document.getElementById('btnNewSession');
  if (btnNewSession) btnNewSession.addEventListener('click', newSession);

  const btnSettings = document.getElementById('btnSettings');
  if (btnSettings) btnSettings.addEventListener('click', openSettings);

  const saveConfigBtn = document.getElementById('saveConfigBtn');
  if (saveConfigBtn) saveConfigBtn.addEventListener('click', saveConfig);

  const restartServerBtn = document.getElementById('restartServerBtn');
  if (restartServerBtn) restartServerBtn.addEventListener('click', restartServer);

  // 侧边栏 Tab 切换
  document.querySelectorAll('.sidebar-tab').forEach(btn => {
    btn.addEventListener('click', () => switchSidebarTab(btn.dataset.tab));
  });

  // 记忆面板
  if (memorySearchBtnEl) {
    memorySearchBtnEl.addEventListener('click', () => {
      memoryPanelMode = 'search';
      loadMemories();
    });
  }
  if (memorySearchInputEl) {
    memorySearchInputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        memoryPanelMode = 'search';
        loadMemories();
      }
    });
  }
  if (memoryTypeFilterEl) {
    memoryTypeFilterEl.addEventListener('change', () => loadMemories());
  }
  const btnUserProfile = document.getElementById('btnUserProfile');
  if (btnUserProfile) btnUserProfile.addEventListener('click', loadUserProfile);
  if (memoryListEl) {
    memoryListEl.addEventListener('click', (e) => {
      const btn = e.target.closest('.memory-delete-btn');
      if (!btn) return;
      const itemEl = btn.closest('.memory-item');
      if (!itemEl) return;
      const memoryId = itemEl.dataset.id;
      if (memoryId) deleteMemory(memoryId, itemEl);
    });
  }

  // 弹窗遮罩点击关闭
  document.querySelectorAll('.modal-overlay').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target === el) el.classList.remove('show');
    });
  });

  // 代码块复制（事件委托）
  if (messagesEl) {
    messagesEl.addEventListener('click', (e) => {
      handleCodeCopyClick(e);
    });
  }
}

// ========== 启动 ==========
function start() {
  initGlobals();
  bindEvents();
  checkHealth();
  setInterval(checkHealth, 30000);

  (async () => {
    await loadSessions();
    const persisted = loadPersistedSession();
    if (persisted) {
      const exists = sessionListEl && sessionListEl.querySelector(`.session-item[data-id="${CSS.escape(persisted)}"]`);
      if (exists) {
        await selectSession(persisted);
      } else {
        localStorage.removeItem(SESSION_STORAGE_KEY);
      }
    }
  })();

  if (messageInputEl) messageInputEl.focus();
}

// DOM 就绪后启动
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start);
} else {
  start();
}

window.HermesChatMain = { start, initGlobals, bindEvents, autoResize, checkHealth };
