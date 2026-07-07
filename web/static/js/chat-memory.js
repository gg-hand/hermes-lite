/* ============================================================
   chat-memory.js — 记忆面板 + 用户画像
   ============================================================ */

let memoryPanelMode = 'all';

// ========== 侧边栏 Tab 切换 ==========
function switchSidebarTab(tabName) {
  document.querySelectorAll('.sidebar-nav .nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  });
  sessionListEl.classList.toggle('active', tabName === 'sessions');
  memoryPanelEl.classList.toggle('active', tabName === 'memories');
  if (filePanelEl) filePanelEl.classList.toggle('active', tabName === 'files');
  if (tabName === 'memories') {
    loadMemories();
  } else if (tabName === 'files' && typeof loadFiles === 'function') {
    loadFiles();
  }
}

// ========== 加载记忆列表 ==========
// 双模式：'search'（向量检索）/ 'all'（按类型列出全部）
async function loadMemories() {
  const typeFilterEl = document.getElementById('memoryTypeFilter');
  const searchEl = document.getElementById('memorySearchInput');
  const listEl = document.getElementById('memoryList');
  if (!listEl) return;

  const typeFilter = (typeFilterEl && typeFilterEl.value) || 'all';
  const typeParam = typeFilter === 'all' ? '' : `&type=${encodeURIComponent(typeFilter)}`;
  try {
    let data;
    if (memoryPanelMode === 'search') {
      const q = searchEl ? searchEl.value.trim() : '';
      if (!q) {
        memoryPanelMode = 'all';
        return loadMemories();
      }
      data = await api(`/memories?q=${encodeURIComponent(q)}&top_k=20${typeParam}`);
    } else {
      data = await api(`/memories/all?limit=100${typeParam}`);
    }
    renderMemoryList(data.memories || []);
  } catch (e) {
    listEl.innerHTML = `<div class="empty-state"><div class="empty-state-icon">!</div><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

// ========== 渲染记忆列表 ==========
function renderMemoryList(memories) {
  const listEl = document.getElementById('memoryList');
  if (!listEl) return;
  if (!memories.length) {
    listEl.innerHTML = '<div class="empty-state"><div class="empty-state-icon">○</div><div class="empty-state-text">暂无记忆</div></div>';
    return;
  }
  listEl.innerHTML = memories.map(m => {
    const mid = escapeHtml(String(m.id || '').slice(0, 8));
    const content = escapeHtml(m.content || '');
    const meta = m.metadata || {};
    const type = escapeHtml(String(meta.type || '-'));
    const sim = (m.similarity != null) ? Number(m.similarity).toFixed(3) : '';
    const importance = meta.importance != null ? escapeHtml(String(meta.importance)) : '-';
    const lastAccessed = meta.last_accessed ? formatTime(String(meta.last_accessed)) : (meta.timestamp ? formatTime(String(meta.timestamp)) : '-');
    const simHtml = sim ? `<span class="tag tag-accent">sim ${sim}</span>` : '';
    return `
      <div class="memory-item" data-id="${escapeHtml(String(m.id || ''))}">
        <div class="memory-item-header">
          <span class="memory-item-id mono">#${mid}</span>
          <span class="tag">${type}</span>
          ${simHtml}
        </div>
        <div class="memory-item-content">${content}</div>
        <div class="memory-item-meta">
          <span class="text-muted text-sm">importance ${importance}</span>
          <span class="text-muted text-sm">· ${escapeHtml(lastAccessed)}</span>
        </div>
        <div class="memory-item-actions">
          <button class="btn btn-ghost btn-sm memory-delete-btn">删除</button>
        </div>
      </div>`;
  }).join('');
}

// ========== 删除记忆 ==========
async function deleteMemory(memoryId, itemEl) {
  if (!confirm('确定删除这条记忆？')) return;
  try {
    await api(`/memories/${encodeURIComponent(memoryId)}`, { method: 'DELETE' });
    if (itemEl && itemEl.parentNode) itemEl.parentNode.removeChild(itemEl);
    showToast('记忆已删除', 'success');
  } catch (e) {
    showToast('删除记忆失败: ' + e.message, 'error');
  }
}

// ========== 用户画像 ==========
async function loadUserProfile() {
  const bodyEl = document.getElementById('profileBody');
  if (!bodyEl) return;
  bodyEl.innerHTML = '<div class="empty-state"><div class="spinner"></div><div class="empty-state-text">加载中...</div></div>';
  openModal('profileModal');
  try {
    const data = await api('/profile');
    const content = data.content || '';
    if (!content) {
      bodyEl.innerHTML = '<div class="empty-state"><div class="empty-state-icon">○</div><div class="empty-state-text">暂无用户画像内容<br>(memory.md 为空或不存在)</div></div>';
      return;
    }
    bodyEl.innerHTML = `<div class="profile-content">${escapeHtml(content)}</div>`;
  } catch (e) {
    bodyEl.innerHTML = `<div class="empty-state"><div class="empty-state-icon">!</div><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

window.HermesChatMemory = {
  switchSidebarTab,
  loadMemories,
  renderMemoryList,
  deleteMemory,
  loadUserProfile,
};
