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
  schedulePanelEl = document.getElementById('schedulePanel');
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

  // 侧边栏折叠
  const menuToggle = document.getElementById('menuToggle');
  if (menuToggle && sidebarEl) {
    menuToggle.addEventListener('click', () => sidebarEl.classList.toggle('collapsed'));
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

  // 调度管理绑定
  const createScheduleBtn = document.getElementById('createScheduleBtn');
  if (createScheduleBtn) createScheduleBtn.addEventListener('click', createScheduleUI);

  const schedName = document.getElementById('schedName');
  const schedCron = document.getElementById('schedCron');
  const schedTask = document.getElementById('schedTask');
  if (schedName) schedName.addEventListener('input', updateCreateScheduleBtnState);
  if (schedCron) schedCron.addEventListener('input', updateCreateScheduleBtnState);
  if (schedTask) schedTask.addEventListener('input', updateCreateScheduleBtnState);

  const btnNewScheduleTab = document.getElementById('btnNewScheduleTab');
  if (btnNewScheduleTab) {
    btnNewScheduleTab.addEventListener('click', () => {
      if (schedName) schedName.value = '';
      if (schedCron) schedCron.value = '';
      if (schedTask) schedTask.value = '';
      const schedEnabled = document.getElementById('schedEnabled');
      if (schedEnabled) schedEnabled.value = 'true';
      updateCreateScheduleBtnState();
      openModal('scheduleModal');
    });
  }

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

  // 调度下拉面板
  const scheduleDropdownBtn = document.getElementById('scheduleDropdownBtn');
  if (scheduleDropdownBtn && schedulePanelEl) {
    scheduleDropdownBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (schedulePanelEl.classList.contains('closing')) {
        schedulePanelEl.classList.remove('closing');
        schedulePanelEl.classList.add('show');
        scheduleDropdownBtn.classList.add('active');
        return;
      }
      const isOpen = schedulePanelEl.classList.toggle('show');
      scheduleDropdownBtn.classList.toggle('active', isOpen);
      if (isOpen) {
        if (!_scheduleDataLoaded) {
          _scheduleDataLoaded = true;
          fetchScheduleRuns();
          fetchSchedules();
          fetchProposals();
          fetchCronToolsPending();
          fetchCronTools();
        }
        startScheduleRunsAutoRefresh();
      }
    });
  }

  // 点击外部关闭调度下拉
  document.addEventListener('click', (e) => {
    if (!schedulePanelEl) return;
    const btn = document.getElementById('scheduleDropdownBtn');
    if (!btn) return;
    if (!schedulePanelEl.classList.contains('show')) return;
    if (!schedulePanelEl.contains(e.target) && !btn.contains(e.target)) {
      closeScheduleDropdown();
    }
  });

  // ESC 关闭调度下拉
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && schedulePanelEl && schedulePanelEl.classList.contains('show')) {
      closeScheduleDropdown();
    }
  });

  // 调度面板事件委托
  if (schedulePanelEl) {
    schedulePanelEl.addEventListener('click', handleSchedulePanelClick);
    schedulePanelEl.addEventListener('change', handleSchedulePanelChange);
  }

  // 代码块复制（事件委托）
  if (messagesEl) {
    messagesEl.addEventListener('click', (e) => {
      handleCodeCopyClick(e);
    });
  }

  // 调度刷新按钮
  const btnRefreshScheduleRuns = document.getElementById('btnRefreshScheduleRuns');
  if (btnRefreshScheduleRuns) btnRefreshScheduleRuns.addEventListener('click', fetchScheduleRuns);
  const btnRefreshProposals = document.getElementById('btnRefreshProposals');
  if (btnRefreshProposals) btnRefreshProposals.addEventListener('click', fetchProposals);
  const btnRefreshScheduleAudit = document.getElementById('btnRefreshScheduleAudit');
  if (btnRefreshScheduleAudit) btnRefreshScheduleAudit.addEventListener('click', loadScheduleAudit);
  const btnRefreshCronToolsPending = document.getElementById('btnRefreshCronToolsPending');
  if (btnRefreshCronToolsPending) btnRefreshCronToolsPending.addEventListener('click', fetchCronToolsPending);
  const btnRefreshCronTools = document.getElementById('btnRefreshCronTools');
  if (btnRefreshCronTools) btnRefreshCronTools.addEventListener('click', fetchCronTools);
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
