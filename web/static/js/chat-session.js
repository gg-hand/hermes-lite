/* ============================================================
   chat-session.js — 会话管理
   修复点：loadMessages 中 plan 工具名 plan_task→plan_create,
   update_todo→plan_update_step（与后端注册名一致）
   ============================================================ */

// ========== 持久化 ==========
function persistCurrentSession() {
  if (currentSessionId) {
    localStorage.setItem(SESSION_STORAGE_KEY, currentSessionId);
  } else {
    localStorage.removeItem(SESSION_STORAGE_KEY);
  }
}

function loadPersistedSession() {
  return localStorage.getItem(SESSION_STORAGE_KEY) || null;
}

// ========== 会话列表 ==========
async function loadSessions() {
  try {
    // exclude_cron=true 过滤掉 cron 会话（调度会话只在 /scheduler 页查看）
    const data = await api('/sessions?exclude_cron=true');
    const sessions = data.sessions || [];
    renderSessionList(sessions);
  } catch (e) {
    showToast('加载会话列表失败: ' + e.message, 'error');
  }
}

function renderSessionList(sessions) {
  if (!sessions.length) {
    sessionListEl.innerHTML = '<div class="empty-state"><div class="empty-state-icon">○</div><div class="empty-state-text">暂无会话<br>点击上方 + 新建</div></div>';
    return;
  }
  sessionListEl.innerHTML = sessions.map(s => {
    // 优先使用 title，无 title 回退到首条消息 preview（前端内存），再回退 id 前 24 字符
    const _preview = (window._firstMessagePreview && window._firstMessagePreview[s.id]) || '';
    const displayTitle = (s.title && s.title.trim()) ? s.title.trim()
      : (_preview || s.id.slice(0, 24));
    return `
    <div class="session-item ${s.id === currentSessionId ? 'active' : ''}" data-id="${s.id}">
      <div class="session-info">
        <div class="session-id" title="${escapeHtml(s.id)}">${escapeHtml(displayTitle)}</div>
        <div class="session-time">${formatTime(s.updated_at)}</div>
      </div>
      <button class="btn-icon session-delete-btn" data-del="${s.id}" title="删除">&times;</button>
    </div>`;
  }).join('');

  sessionListEl.querySelectorAll('.session-item').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.session-delete-btn')) return;
      selectSession(el.dataset.id);
    });
  });
  sessionListEl.querySelectorAll('.session-delete-btn').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteSession(el.dataset.del);
    });
  });

  // 同步顶栏标题：若 currentSessionId 在列表中，使用其 title（回退首条消息 preview / id 前 20 字符）
  if (currentSessionId) {
    const cur = sessions.find(s => s.id === currentSessionId);
    if (cur) {
      // 存储当前会话数据，供标题退避轮询比较
      if (window.HermesChatSession) window.HermesChatSession._currentSessionData = cur;
      const _preview = (window._firstMessagePreview && window._firstMessagePreview[currentSessionId]) || '';
      const t = (cur.title && cur.title.trim()) ? cur.title.trim()
        : (_preview ? _preview : currentSessionId.slice(0, 20) + '...');
      if (sessionTitleEl) sessionTitleEl.textContent = t;
    }
  }
}

// ========== 沉淀触发 ==========
// 切换/新建会话前 flush 当前会话的沉淀（fire-and-forget，不阻塞）
function flushConsolidation() {
  if (!currentSessionId) return;
  fetch(API_BASE + '/consolidation/flush', { method: 'POST' })
    .then(res => res.json())
    .then(data => {
      if (data.pending_count > 0) {
        console.log(`[flush] 已触发沉淀，待处理 ${data.pending_count} 条消息`);
      }
    })
    .catch(err => console.warn('[flush] 触发失败:', err));
}

// ========== 会话切换 ==========
async function selectSession(sessionId) {
  if (currentSessionId && currentSessionId !== sessionId) {
    flushConsolidation();
  }
  // 清理旧会话的标题轮询 timer，避免跨会话串扰
  if (window._titlePollTimer) { clearTimeout(window._titlePollTimer); window._titlePollTimer = null; }
  currentSessionId = sessionId;
  persistCurrentSession();
  // 标题占位：优先首条消息 preview，回退 id 前 20 字符
  const _preview = (window._firstMessagePreview && window._firstMessagePreview[sessionId]) || '';
  sessionTitleEl.textContent = (_preview ? _preview : sessionId.slice(0, 20)) + '...';
  await loadMessages(sessionId);
  loadSessions();
}

// ========== 历史消息加载 ==========
// 修复点：plan_create/plan_update_step 工具消息特殊处理，重建 todo 卡片
// 增强：并行拉取消息+文件，末尾追加未展示文件合成消息（跨会话/历史回填）
async function loadMessages(sessionId) {
  try {
    // 并行拉取消息 + 文件列表（文件列表失败降级为空，不阻塞消息渲染）
    const [msgData, filesData] = await Promise.all([
      api(`/sessions/${sessionId}/messages`),
      api(`/sessions/${sessionId}/files`).catch(() => ({ files: [] })),
    ]);
    const messages = msgData.messages || [];
    const files = filesData.files || [];
    welcomeScreenEl.style.display = messages.length ? 'none' : 'flex';
    messagesEl.innerHTML = '';
    _prevTodoStepStatuses = {};
    let currentTodoMsgEl = null;
    if (messages.length) {
      for (let i = 0; i < messages.length; i++) {
        const m = messages[i];
        // plan_create / plan_update_step 工具消息需要特殊处理以重建 todo 卡片
        const isPlanTodo = (m.tool_name === 'plan_create' || m.tool_name === 'plan_update_step');
        if (isPlanTodo && m.role === 'user') {
          // tool_result：尝试解析为 todo dict
          let todoData = null;
          try { todoData = JSON.parse(m.content); } catch (e) { todoData = null; }
          if (todoData && todoData.steps) {
            if (m.tool_name === 'plan_create' || !currentTodoMsgEl) {
              // plan_create（或 update 缺失前置卡片）：创建新 todo 卡片
              const msgEl = document.createElement('div');
              msgEl.className = 'message assistant';
              const roleEl = document.createElement('div');
              roleEl.className = 'message-role assistant';
              roleEl.textContent = 'Assistant';
              msgEl.appendChild(roleEl);
              messagesEl.appendChild(msgEl);
              appendTodoCard(msgEl, todoData);
              currentTodoMsgEl = msgEl;
            } else {
              // plan_update_step：更新现有 todo 卡片状态
              updateTodoCard(currentTodoMsgEl, todoData);
            }
            scrollMessagesToBottom();
          } else {
            // 降级：content 不是 todo dict JSON，按普通工具结果卡片显示
            appendToolResultCard(m.tool_name, m.content, m.is_error);
          }
        } else if (isPlanTodo && m.role === 'assistant') {
          // tool_use：跳过（todo 卡片已包含完整信息，避免冗余显示）
        } else if (m.tool_name && m.role === 'assistant') {
          // 工具调用卡片：检查下一条是否是同一次调用的结果
          const next = messages[i + 1];
          if (next && next.tool_name && next.role === 'user' &&
              next.tool_call_id === m.tool_call_id) {
            appendMergedToolCard(m.tool_name, m.content, next.content, next.is_error);
            i++;
          } else {
            appendToolCallCard(m.tool_name, m.content);
          }
        } else if (m.tool_name && m.role === 'user') {
          // 孤立的 tool_result（无前置 tool_use 配对）
          appendToolResultCard(m.tool_name, m.content, m.is_error);
        } else {
          // 普通文本消息：传递 attachments（后端 file_upload 消息有附件）
          const contentStr = (m.content || '').trim();
          if (contentStr) {
            appendMessage(m.role, m.content, m.attachments);
          }
        }
      }
      // 历史加载完毕：强制滚动到底部，展示最新内容（覆盖循环中各 append 的非强制跟随）
      scrollMessagesToBottom(true);
    } else {
      messagesEl.appendChild(welcomeScreenEl);
      welcomeScreenEl.style.display = 'flex';
    }

    // === 第二遍：为未展示的文件合成消息，追加到末尾 ===
    // 收集后端消息已展示的 file_id（去重用，避免与 Stage 4 后端消息重复）
    const attachedFileIds = new Set();
    for (const m of messages) {
      if (m.attachments) {
        const atts = safeParseJSON(m.attachments) || [];
        for (const a of atts) if (a.file_id) attachedFileIds.add(a.file_id);
      }
    }
    // 筛选未被后端消息展示的文件（历史回填 / 跨会话去重关联文件）
    const unrepresented = files.filter(f => !attachedFileIds.has(f.file_id));
    if (unrepresented.length) {
      // 边界情况：messages.length === 0 时 else 分支已重新 append welcomeScreen，
      // 此处需隐藏欢迎屏避免与历史文件同时显示
      if (welcomeScreenEl && welcomeScreenEl.parentNode === messagesEl) {
        welcomeScreenEl.style.display = 'none';
      }
      // 插入分隔线，区分后端消息与合成消息
      const sep = document.createElement('div');
      sep.className = 'timeline-separator';
      sep.textContent = '历史文件';
      messagesEl.appendChild(sep);
      // 为每个文件合成 user 消息
      for (const f of unrepresented) {
        const file_type = f.type || '';
        const is_image = ['.png', '.jpg', '.jpeg', '.gif'].includes(file_type);
        const attachment = {
          file_id: f.file_id,
          name: f.original_name,
          type: file_type,
          size: f.size,
          category: is_image ? 'image' : 'document',
          etl_status: f.etl_status,
        };
        const versionLabel = (f.version_seq && f.is_latest === false)
          ? `（v${f.version_seq}，旧版）` : '';
        appendMessage('user', `已上传文件：${f.original_name}${versionLabel}`, [attachment]);
      }
      scrollMessagesToBottom(true);
    }
  } catch (e) {
    showToast('加载消息失败: ' + e.message, 'error');
  }
}

// ========== 删除会话 ==========
async function deleteSession(sessionId) {
  if (!confirm('确认删除此会话及其所有消息？')) return;
  try {
    await api(`/sessions/${sessionId}`, { method: 'DELETE' });
    if (currentSessionId === sessionId) {
      currentSessionId = null;
      persistCurrentSession();
      sessionTitleEl.textContent = '未选择会话';
      messagesEl.innerHTML = '';
      messagesEl.appendChild(welcomeScreenEl);
      welcomeScreenEl.style.display = 'flex';
    }
    loadSessions();
    showToast('会话已删除', 'success');
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

// ========== 新建会话 ==========
async function newSession() {
  flushConsolidation();
  currentSessionId = null;
  persistCurrentSession();
  sessionTitleEl.textContent = '新会话';
  messagesEl.innerHTML = '';
  messagesEl.appendChild(welcomeScreenEl);
  welcomeScreenEl.style.display = 'flex';
  messageInputEl.focus();
  showToast('已创建新会话，发送消息后自动保存');
}

window.HermesChatSession = {
  persistCurrentSession,
  loadPersistedSession,
  loadSessions,
  renderSessionList,
  flushConsolidation,
  selectSession,
  loadMessages,
  deleteSession,
  newSession,
};
