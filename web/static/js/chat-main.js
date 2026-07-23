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
  // Task 3: 顶栏上下文元素
  topbarModelEl = document.getElementById('topbarModel');
  topbarModelValueEl = topbarModelEl ? topbarModelEl.querySelector('.topbar-model-value') : null;
  topbarSessionNameEl = document.getElementById('topbarSessionName');
  topbarSessionTextEl = topbarSessionNameEl ? topbarSessionNameEl.querySelector('.topbar-session-text') : null;
  topbarConnectionEl = document.getElementById('topbarConnection');
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
    if (topbarConnectionEl) topbarConnectionEl.classList.remove('disconnected');
  } catch {
    if (statusDotEl) statusDotEl.classList.add('offline');
    if (topbarConnectionEl) topbarConnectionEl.classList.add('disconnected');
  }
}

// ========== 侧边栏底部调度快捷入口徽章 ==========
async function loadSchedulerBadge() {
  try {
    // 尝试从 /api/schedules/pending-count 拉取待办数
    // 如果端点不存在，徽章保持 hidden（不影响功能）
    const resp = await fetch('/api/schedules/pending-count');
    if (!resp.ok) return;
    const data = await resp.json();
    const count = data.count || 0;
    if (count > 0) {
      const badge = document.getElementById('schedulerBadge');
      if (badge) {
        badge.textContent = count > 99 ? '99+' : count;
        badge.hidden = false;
      }
    }
  } catch (e) {
    // 静默失败，徽章不显示
  }
}

// ========== Task 3: 顶栏模型信息加载 ==========
async function loadTopbarModel() {
  if (!topbarModelValueEl) return;
  try {
    const data = await api('/config');
    const llm = (data && data.config && data.config.llm) || {};
    const provider = llm.main_provider || 'unknown';
    const model = llm.main_model || 'unknown';
    topbarModelValueEl.textContent = `${provider} / ${model}`;
    topbarModelValueEl.title = `${provider} / ${model}`;
  } catch (e) {
    topbarModelValueEl.textContent = '加载失败';
    topbarModelValueEl.title = e && e.message ? e.message : '加载失败';
  }
}

// ========== Task 3: 顶栏会话名显示 ==========
function updateTopbarSessionName(title) {
  if (!topbarSessionTextEl) return;
  const display = (title && title.trim()) ? title.trim() : '新会话';
  topbarSessionTextEl.textContent = display;
  if (topbarSessionNameEl) topbarSessionNameEl.title = display === '新会话' ? '点击重命名' : `${display}（点击重命名）`;
}

// ========== Task 3: 顶栏会话名点击重命名 ==========
function setupSessionRename() {
  if (!topbarSessionNameEl) return;
  // 避免重复绑定
  if (topbarSessionNameEl.dataset.bound === '1') return;
  topbarSessionNameEl.dataset.bound = '1';

  topbarSessionNameEl.addEventListener('click', () => {
    if (!topbarSessionTextEl) return;
    const container = topbarSessionNameEl;
    const current = topbarSessionTextEl.textContent || '新会话';
    if (container.querySelector('input')) return; // 已在编辑态
    const input = document.createElement('input');
    input.type = 'text';
    input.value = current === '新会话' ? '' : current;
    input.placeholder = '输入新名称...';
    input.maxLength = 100;
    container.innerHTML = '';
    container.appendChild(input);
    input.focus();
    input.select();
    let submitted = false;
    const restore = () => updateTopbarSessionName(current);
    input.addEventListener('keydown', async (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submitted = true;
        const newTitle = (input.value || '').trim();
        // 空值或未变：取消（恢复原值）
        if (!newTitle || newTitle === current) {
          restore();
          return;
        }
        // 未选会话：仅本地显示（无后端持久化）
        if (!currentSessionId) {
          updateTopbarSessionName(newTitle);
          showToast('新会话需先发送消息才会保存', 'success');
          return;
        }
        try {
          const resp = await fetch(`/sessions/${encodeURIComponent(currentSessionId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle }),
          });
          if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
          }
          updateTopbarSessionName(newTitle);
          // 同步刷新侧边栏会话列表
          if (typeof loadSessions === 'function') {
            try { await loadSessions(); } catch (_) {}
          }
          showToast('重命名成功', 'success');
        } catch (err) {
          restore();
          showToast('重命名失败：' + (err.message || err), 'error');
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        submitted = true;
        restore();
      }
    });
    input.addEventListener('blur', () => {
      if (!submitted) restore();
    });
  });
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

  // 侧边栏：PC 端始终展开，移动端浮层收纳
  const menuToggle = document.getElementById('menuToggle');
  const sidebarOverlay = document.getElementById('sidebarOverlay');
  if (sidebarEl) {
    // 初始状态：移动端默认收起，PC 端始终展开
    const isMobile = window.matchMedia('(max-width: 768px)').matches;
    if (isMobile) {
      sidebarEl.classList.add('collapsed');
    } else {
      sidebarEl.classList.remove('collapsed');
    }
    if (sidebarOverlay) sidebarOverlay.classList.remove('show');
  }

  if (menuToggle && sidebarEl) {
    const toggleSidebar = () => {
      const willCollapse = !sidebarEl.classList.contains('collapsed');
      sidebarEl.classList.toggle('collapsed');
      if (sidebarOverlay) {
        if (willCollapse) {
          sidebarOverlay.classList.remove('show');
        } else {
          sidebarOverlay.classList.add('show');
        }
      }
    };
    menuToggle.addEventListener('click', toggleSidebar);
    if (sidebarOverlay) {
      sidebarOverlay.addEventListener('click', () => {
        sidebarEl.classList.add('collapsed');
        sidebarOverlay.classList.remove('show');
      });
    }
  }

  // 窗口尺寸变化时同步侧边栏状态（PC 切移动端自动收起，移动端切 PC 自动展开）
  window.addEventListener('resize', () => {
    if (!sidebarEl) return;
    const isMobile = window.matchMedia('(max-width: 768px)').matches;
    if (!isMobile) {
      sidebarEl.classList.remove('collapsed');
      if (sidebarOverlay) sidebarOverlay.classList.remove('show');
    } else {
      sidebarEl.classList.add('collapsed');
      if (sidebarOverlay) sidebarOverlay.classList.remove('show');
    }
  });

  // 新建会话 / 设置弹出菜单
  const btnNewSession = document.getElementById('btnNewSession');
  if (btnNewSession) btnNewSession.addEventListener('click', newSession);

  const btnSettingsFlyout = document.getElementById('btnSettingsFlyout');
  if (btnSettingsFlyout) btnSettingsFlyout.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleSettingsFlyout();
  });

  const btnAdvancedSettings = document.getElementById('btnAdvancedSettings');
  if (btnAdvancedSettings) btnAdvancedSettings.addEventListener('click', openSettings);

  const btnRestartFlyout = document.getElementById('btnRestartFlyout');
  if (btnRestartFlyout) btnRestartFlyout.addEventListener('click', () => {
    closeSettingsFlyout();
    restartServer();
  });

  // 外部点击关闭 flyout
  const flyoutEl = document.getElementById('settingsFlyout');
  document.addEventListener('click', (e) => {
    if (!flyoutEl) return;
    if (flyoutEl.classList.contains('show') &&
        !flyoutEl.contains(e.target) &&
        !btnSettingsFlyout?.contains(e.target)) {
      closeSettingsFlyout();
    }
  });

  const saveConfigBtn = document.getElementById('saveConfigBtn');
  if (saveConfigBtn) saveConfigBtn.addEventListener('click', saveConfig);

  const restartServerBtn = document.getElementById('restartServerBtn');
  if (restartServerBtn) restartServerBtn.addEventListener('click', restartServer);

  // 侧边栏导航切换
  document.querySelectorAll('.sidebar-nav .nav-item[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => switchSidebarTab(btn.dataset.tab));
  });

  // Director 面板开关
  const btnDirector = document.getElementById('btnDirector');
  if (btnDirector) {
    btnDirector.addEventListener('click', () => {
      const main = document.querySelector('main.main');
      const pane = document.getElementById('collabPane');
      if (!main || !pane) return;
      const willOn = !main.classList.contains('director-on');
      main.classList.toggle('director-on', willOn);
      pane.hidden = !willOn;
      btnDirector.classList.toggle('active', willOn);
    });
  }

  // 工作台 tab 切换
  document.querySelectorAll('.wb-tab[data-wb-tab]').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.wbTab;
      document.querySelectorAll('.wb-tab').forEach(t => t.classList.toggle('active', t === tab));
      document.querySelectorAll('.wb-pane').forEach(p => p.classList.toggle('active', p.dataset.wbPane === target));
    });
  });
  // 工作台收起按钮
  const wbCollapseBtn = document.getElementById('wbCollapseBtn');
  if (wbCollapseBtn) {
    wbCollapseBtn.addEventListener('click', () => {
      const main = document.querySelector('main.main');
      const pane = document.getElementById('collabPane');
      if (main && pane) {
        main.classList.remove('director-on');
        pane.hidden = true;
        if (btnDirector) btnDirector.classList.remove('active');
      }
    });
  }

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

  // 图片缩略图点击放大（事件委托）
  if (messagesEl) {
    messagesEl.addEventListener('click', (e) => {
      const thumb = e.target.closest('.attachment-thumb');
      if (thumb) {
        openImageModal(thumb.src, thumb.dataset.name || '');
      }
    });
  }
}

// ========== 启动 ==========
function start() {
  initGlobals();
  bindEvents();
  checkHealth();
  setInterval(checkHealth, 30000);

  // Task 3: 加载顶栏模型信息 + 绑定会话重命名交互
  loadTopbarModel();
  setupSessionRename();

  // Task 4: 侧边栏底部调度快捷入口徽章
  loadSchedulerBadge();

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

// ========== 键盘快捷键 ==========
function isInputFocused() {
  const tag = document.activeElement?.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
}

function focusMessageInput() {
  const input = document.getElementById('messageInput') || document.querySelector('textarea[name="message"]');
  if (input) input.focus();
}

function setupKeyboardShortcuts() {
  document.addEventListener('keydown', (e) => {
    // Ctrl+K / Cmd+K 聚焦输入框
    if ((e.ctrlKey || e.metaKey) && e.key === 'k' && !e.shiftKey) {
      e.preventDefault();
      focusMessageInput();
      return;
    }
    // / 聚焦输入框（非输入态）
    if (e.key === '/' && !isInputFocused() && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      focusMessageInput();
      return;
    }
    // Ctrl+Shift+N 新建会话
    if (e.ctrlKey && e.shiftKey && (e.key === 'N' || e.key === 'n')) {
      e.preventDefault();
      const input = document.getElementById('messageInput');
      if (input && input.value.trim()) {
        if (!window.confirm('丢弃当前输入？')) return;
      }
      const btn = document.getElementById('btnNewSession') || document.querySelector('[data-action="new-session"]');
      if (btn) btn.click();
      return;
    }
    // Esc 关闭弹窗（按优先级）
    if (e.key === 'Escape') {
      const cheatsheet = document.querySelector('.uxp-cheatsheet.visible');
      if (cheatsheet) {
        cheatsheet.classList.remove('visible');
        return;
      }
      const flyout = document.querySelector('.settings-flyout.show');
      if (flyout) {
        flyout.classList.remove('show');
        return;
      }
      const approval = document.querySelector('.approval-card');
      if (approval) {
        const closeBtn = approval.querySelector('.approval-cancel, [data-action="cancel"]');
        if (closeBtn) { closeBtn.click(); return; }
      }
      const modal = document.querySelector('.modal-overlay.show');
      if (modal) {
        modal.classList.remove('show');
        return;
      }
    }
  }, true);  // capture 阶段

  // cheat-sheet 显示链接
  const cheatsheetLink = document.getElementById('btnShowCheatsheet');
  if (cheatsheetLink) {
    cheatsheetLink.addEventListener('click', (e) => {
      e.preventDefault();
      toggleCheatsheet();
    });
  }
}

function toggleCheatsheet() {
  let sheet = document.querySelector('.uxp-cheatsheet');
  if (!sheet) {
    sheet = document.createElement('div');
    sheet.className = 'uxp-cheatsheet';
    sheet.innerHTML = `
      <div class="cheatsheet-header">⌨ 快捷键</div>
      <div class="cheatsheet-body">
        <div class="cheatsheet-row"><kbd>Ctrl</kbd>+<kbd>K</kbd><span>聚焦输入框</span></div>
        <div class="cheatsheet-row"><kbd>/</kbd><span>聚焦输入框</span></div>
        <div class="cheatsheet-row"><kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>N</kbd><span>新建会话</span></div>
        <div class="cheatsheet-row"><kbd>Ctrl</kbd>+<kbd>Enter</kbd><span>发送消息</span></div>
        <div class="cheatsheet-row"><kbd>Esc</kbd><span>关闭弹窗</span></div>
      </div>
      <div class="cheatsheet-footer">点击外部或按 Esc 关闭</div>
    `;
    document.body.appendChild(sheet);
    // 点击外部关闭
    document.addEventListener('click', (e) => {
      if (!sheet.classList.contains('visible')) return;
      if (!sheet.contains(e.target) && e.target.id !== 'btnShowCheatsheet') {
        sheet.classList.remove('visible');
      }
    });
  }
  sheet.classList.toggle('visible');
}

// 启动时绑定快捷键
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', setupKeyboardShortcuts);
} else {
  setupKeyboardShortcuts();
}

window.TeageChatMain = { start, initGlobals, bindEvents, autoResize, checkHealth, setupKeyboardShortcuts, toggleCheatsheet };
