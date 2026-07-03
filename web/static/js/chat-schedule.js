/* ============================================================
   chat-schedule.js — 调度系统 + 提议审批 + Cron Tool 管理
   最重的模块，包含：调度运行历史/调度项CRUD/隔离记忆/审计/
   提议确认拒绝修改/Cron Tool 审查激活拒绝重载删除/徽章
   ============================================================ */

// ========== 状态与缓存 ==========
let _lastScheduleRunId = null;
let _scheduleRunsTimer = null;
let selectedScheduleId = null;
let _scheduleDataLoaded = false;

const proposalToolsCache = {};
let _proposalsCache = [];
let _schedulesCache = [];
let _cronToolsCache = [];
let _pendingCronToolsCache = [];

// ========== 调度运行历史 ==========
async function fetchScheduleRuns() {
  const listEl = document.getElementById('scheduleRunsList');
  if (!listEl) return;
  try {
    const data = await api('/schedules/runs?limit=20');
    const runs = data.runs || [];
    const newestId = runs.length ? (runs[0].run_id || '') : '';
    if (newestId !== _lastScheduleRunId || listEl.querySelector('.schedule-empty')) {
      _lastScheduleRunId = newestId;
      renderScheduleRuns(runs);
    }
  } catch (e) {
    if (!listEl.querySelector('.schedule-run-item')) {
      listEl.innerHTML = `<div class="schedule-empty">加载失败: ${escapeHtml(e.message)}</div>`;
    }
  }
}

function renderScheduleRuns(runs) {
  const listEl = document.getElementById('scheduleRunsList');
  if (!listEl || !runs.length) {
    if (listEl) listEl.innerHTML = '<div class="schedule-empty">暂无执行记录</div>';
    return;
  }
  listEl.innerHTML = runs.map(r => {
    const isSuccess = r.success !== false;
    const statusClass = isSuccess ? 'is-success' : 'is-failed';
    const statusLabel = isSuccess ? '成功' : '失败';
    const time = formatTime(r.started_at);
    const schedName = escapeHtml(r.schedule_name || r.schedule_id || '');
    const summary = escapeHtml(truncateText(r.llm_summary || r.assistant_response || '(无摘要)', 200));
    const toolCount = (r.tool_calls || []).length;
    const duration = (r.duration_seconds || 0).toFixed(1);
    const meta = `工具 ${toolCount} 次 · 耗时 ${duration}s`;
    return `<div class="schedule-run-item ${statusClass}">
      <div class="schedule-run-header">
        <span class="schedule-run-time">${escapeHtml(time)}</span>
        <span class="schedule-run-sched-name">${schedName}</span>
        <span class="schedule-run-status ${statusClass}">${statusLabel}</span>
      </div>
      <div class="schedule-run-meta text-muted text-sm">${escapeHtml(meta)}</div>
      <div class="schedule-run-summary">${summary}</div>
    </div>`;
  }).join('');
}

function startScheduleRunsAutoRefresh() {
  if (_scheduleRunsTimer) return;
  _scheduleRunsTimer = setInterval(() => {
    if (document.hidden) return;
    const panel = document.getElementById('schedulePanel');
    if (!panel || !panel.classList.contains('show')) return;
    fetchScheduleRuns();
  }, 60000);
}

// ========== 调度项 CRUD ==========
async function fetchSchedules() {
  const listEl = document.getElementById('scheduleList');
  if (!listEl) return;
  try {
    const data = await api('/schedules');
    renderSchedules(data.schedules || []);
  } catch (e) {
    listEl.innerHTML = `<div class="schedule-empty">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function renderSchedules(schedules) {
  const listEl = document.getElementById('scheduleList');
  if (!listEl) return;
  _schedulesCache = schedules || [];
  const countEl = document.getElementById('scheduleCount');
  if (countEl) countEl.textContent = schedules.length;
  if (!schedules.length) {
    listEl.innerHTML = '<div class="schedule-empty">暂无调度项，点击「+ 新建」创建</div>';
    return;
  }
  listEl.innerHTML = schedules.map(s => `
    <div class="schedule-item" data-id="${escapeHtml(s.id)}">
      <div class="schedule-item-header">
        <span class="schedule-name">${escapeHtml(s.name)}</span>
        <span class="schedule-cron mono">${escapeHtml(s.cron)}</span>
        <span class="schedule-status-dot ${s.enabled ? 'is-enabled' : 'is-disabled'}" title="${s.enabled ? '已启用' : '已禁用'}"></span>
      </div>
      <div class="schedule-meta text-muted text-sm">
        ${s.last_run ? `上次: ${formatTime(s.last_run)}` : '上次: 未运行'}
        ${s.next_run ? ` | 下次: ${formatTime(s.next_run)}` : ''}
      </div>
      <div class="schedule-actions">
        <button class="btn btn-primary btn-sm" onclick="openScheduleDetail('${escapeHtml(s.id)}')">详情</button>
        <button class="btn btn-ghost btn-sm" data-action="select" data-id="${escapeHtml(s.id)}">记忆/审计</button>
      </div>
    </div>
  `).join('');
}

function openScheduleDetail(id) {
  const s = _schedulesCache.find(x => x.id === id);
  if (!s) { showToast('未找到调度项: ' + id, 'error'); return; }
  document.getElementById('scheduleDetailTitle').textContent = `调度项 · ${s.name}`;
  document.getElementById('scheduleDetailBody').innerHTML = `
    <div class="form-section">
      <div class="form-label">名称</div>
      <div class="form-value">${escapeHtml(s.name)}</div>
    </div>
    <div class="form-section">
      <div class="form-label">Cron 表达式</div>
      <div class="form-value mono">${escapeHtml(s.cron)}</div>
    </div>
    <div class="form-section">
      <div class="form-label">任务描述</div>
      <div class="form-value">${escapeHtml(s.task)}</div>
    </div>
    <div class="form-section">
      <div class="form-label">启用状态</div>
      <div class="form-value">
        <label class="schedule-toggle" title="${s.enabled ? '已启用' : '已禁用'}">
          <input type="checkbox" id="scheduleDetailToggle" ${s.enabled ? 'checked' : ''}>
          <span class="schedule-toggle-slider"></span>
          <span class="text-muted text-sm" style="margin-left:8px">${s.enabled ? '已启用（即时生效）' : '已禁用（即时生效）'}</span>
        </label>
      </div>
    </div>
    <div class="form-section">
      <div class="form-label">调度时序</div>
      <div class="form-value">
        ${s.last_run ? `上次执行: ${escapeHtml(formatTime(s.last_run))}\n` : '上次执行: 未运行\n'}
        ${s.next_run ? `下次执行: ${escapeHtml(formatTime(s.next_run))}` : '下次执行: 未计算'}
      </div>
    </div>
    <div class="form-section">
      <div class="form-label">调度项 ID</div>
      <div class="form-value mono">${escapeHtml(s.id)}</div>
    </div>
  `;
  const toggle = document.getElementById('scheduleDetailToggle');
  if (toggle) {
    toggle.addEventListener('change', () => {
      toggleSchedule(id, toggle.checked);
      const hint = toggle.parentElement.querySelector('span:last-child');
      if (hint) hint.textContent = toggle.checked ? '已启用（即时生效）' : '已禁用（即时生效）';
    });
  }
  document.getElementById('scheduleDetailFooter').innerHTML = `
    <button class="btn btn-ghost" onclick="closeModal('scheduleDetailModal')">关闭</button>
    <button class="btn btn-danger btn-sm" id="scheduleDetailDeleteBtn">删除</button>
    <button class="btn btn-primary" id="scheduleDetailTriggerBtn">立即触发</button>
  `;
  document.getElementById('scheduleDetailTriggerBtn').onclick = () => {
    closeModal('scheduleDetailModal');
    triggerSchedule(id);
  };
  document.getElementById('scheduleDetailDeleteBtn').onclick = () => {
    closeModal('scheduleDetailModal');
    deleteSchedule(id);
  };
  openModal('scheduleDetailModal');
}

// ========== 调度面板事件委托（chat-main.js 绑定到 schedulePanel）==========
function handleSchedulePanelClick(e) {
  const btn = e.target.closest('[data-action]');
  if (btn) {
    const action = btn.dataset.action;
    const id = btn.dataset.id;
    if (action === 'trigger') triggerSchedule(id);
    else if (action === 'del-sched') deleteSchedule(id);
    else if (action === 'select') selectSchedule(id);
    else if (action === 'del-cron-mem') deleteScheduleMemory(id, btn.dataset.memoryId);
    return;
  }
  const header = e.target.closest('.schedule-section.collapsible > .schedule-section-header');
  if (!header || e.target.closest('button')) return;
  header.parentElement.classList.toggle('collapsed');
}

function handleSchedulePanelChange(e) {
  if (e.target.matches('[data-action="toggle"]')) {
    toggleSchedule(e.target.dataset.id, e.target.checked);
  }
}

// ========== 调度隔离记忆 ==========
async function selectSchedule(id) {
  selectedScheduleId = id;
  const panel = document.getElementById('schedulePanel');
  if (panel) {
    panel.querySelectorAll('.schedule-item').forEach(el => {
      el.classList.toggle('selected', el.dataset.id === id);
    });
  }
  const memoriesSection = document.getElementById('scheduleMemoriesSection');
  const auditSection = document.getElementById('scheduleAuditSection');
  if (memoriesSection) memoriesSection.style.display = '';
  if (auditSection) auditSection.style.display = '';
  loadScheduleMemories(id);
  loadScheduleAudit(id);
}

async function loadScheduleMemories(id) {
  const listEl = document.getElementById('scheduleMemoriesList');
  if (!listEl) return;
  listEl.innerHTML = '<div class="schedule-empty">加载中...</div>';
  try {
    const data = await api(`/schedules/${encodeURIComponent(id)}/memories?limit=20`);
    renderScheduleMemories(id, data.memories || []);
  } catch (e) {
    listEl.innerHTML = `<div class="schedule-empty">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function renderScheduleMemories(scheduleId, memories) {
  const listEl = document.getElementById('scheduleMemoriesList');
  if (!listEl) return;
  if (!memories.length) {
    listEl.innerHTML = '<div class="schedule-empty">暂无隔离记忆</div>';
    return;
  }
  listEl.innerHTML = memories.map(m => `
    <div class="schedule-memory-item" data-id="${escapeHtml(m.id)}">
      <div class="memory-text">${escapeHtml(truncateText(m.content || '', 400))}</div>
      <div class="memory-footer">
        <button class="btn btn-danger btn-sm" data-action="del-cron-mem" data-id="${escapeHtml(scheduleId)}" data-memory-id="${escapeHtml(m.id)}">删除</button>
      </div>
    </div>
  `).join('');
}

async function loadScheduleAudit(id) {
  const listEl = document.getElementById('scheduleAuditList');
  if (!listEl) return;
  listEl.innerHTML = '<div class="schedule-empty">加载中...</div>';
  try {
    const data = await api(`/schedules/${encodeURIComponent(id)}/audit?limit=50`);
    renderScheduleAudit(data.logs || []);
  } catch (e) {
    listEl.innerHTML = `<div class="schedule-empty">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function renderScheduleAudit(logs) {
  const listEl = document.getElementById('scheduleAuditList');
  if (!listEl) return;
  if (!logs.length) {
    listEl.innerHTML = '<div class="schedule-empty">暂无工具调用审计</div>';
    return;
  }
  listEl.innerHTML = logs.map(l => {
    const source = l.decision_source || 'default_rule';
    const sourceLabelMap = {
      schedule_grant: '预授权',
      user_confirm: '用户确认',
      default_rule: '默认规则',
    };
    const sourceLabel = sourceLabelMap[source] || source;
    const inputStr = l.tool_input ? JSON.stringify(l.tool_input) : '';
    const isError = !!l.is_error;
    const sourceTagClass = source === 'schedule_grant' ? 'tag-success' : source === 'user_confirm' ? 'tag-warning' : 'tag';
    return `
    <div class="schedule-audit-item${isError ? ' is-error' : ''}">
      <div class="schedule-audit-header">
        <span class="schedule-audit-tool mono">${escapeHtml(l.tool_name || '')}</span>
        <span class="${sourceTagClass}">${escapeHtml(sourceLabel)}</span>
        <span class="schedule-audit-time text-muted text-sm">${escapeHtml(formatTime(l.timestamp))}</span>
        <span class="schedule-audit-duration text-muted text-sm">${(l.duration_ms || 0).toFixed(0)}ms</span>
        ${l.run_id ? `<span class="schedule-audit-runid text-muted text-sm">run: ${escapeHtml(l.run_id)}</span>` : ''}
      </div>
      ${inputStr ? `<div class="schedule-audit-input mono text-sm">in: ${escapeHtml(truncateText(inputStr, 300))}</div>` : ''}
      <div class="schedule-audit-result mono text-sm${isError ? ' is-error' : ''}">out: ${escapeHtml(truncateText(l.result || '', 300))}</div>
    </div>`;
  }).join('');
}

async function deleteScheduleMemory(scheduleId, memoryId) {
  if (!confirm('确认删除此条隔离记忆？')) return;
  try {
    await api(`/schedules/${encodeURIComponent(scheduleId)}/memories/${encodeURIComponent(memoryId)}`, { method: 'DELETE' });
    showToast('记忆已删除', 'success');
    if (selectedScheduleId === scheduleId) loadScheduleMemories(scheduleId);
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

// ========== 调度项创建/启停/触发/删除 ==========
async function createScheduleUI() {
  const name = document.getElementById('schedName').value.trim();
  const cron = document.getElementById('schedCron').value.trim();
  const task = document.getElementById('schedTask').value.trim();
  const enabled = document.getElementById('schedEnabled').value === 'true';
  if (!name || !cron || !task) {
    showToast('请填写名称、Cron 和任务描述', 'error');
    return;
  }
  const btn = document.getElementById('createScheduleBtn');
  btn.disabled = true;
  btn.textContent = '创建中...';
  try {
    const data = await api('/schedules', { method: 'POST', body: { name, cron, task, enabled } });
    closeModal('scheduleModal');
    document.getElementById('schedName').value = '';
    document.getElementById('schedCron').value = '';
    document.getElementById('schedTask').value = '';
    showToast(data.message || `调度已创建: ${data.schedule_id}`, 'success');
    fetchSchedules();
  } catch (e) {
    showToast('创建调度失败: ' + e.message, 'error');
  } finally {
    btn.textContent = '创建';
    updateCreateScheduleBtnState();
  }
}

function updateCreateScheduleBtnState() {
  const name = document.getElementById('schedName').value.trim();
  const cron = document.getElementById('schedCron').value.trim();
  const task = document.getElementById('schedTask').value.trim();
  document.getElementById('createScheduleBtn').disabled = !(name && cron && task);
}

async function toggleSchedule(id, enabled) {
  try {
    const data = await api(`/schedules/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: { enabled }
    });
    showToast(data.message || (enabled ? '已启用' : '已禁用'), 'success');
  } catch (e) {
    showToast('切换失败: ' + e.message, 'error');
    fetchSchedules();
  }
}

async function triggerSchedule(id) {
  try {
    const data = await api(`/schedules/${encodeURIComponent(id)}/trigger`, { method: 'POST' });
    showToast(data.message || '已触发', 'success');
  } catch (e) {
    showToast('触发失败: ' + e.message, 'error');
  }
}

async function deleteSchedule(id) {
  if (!confirm('确认删除此调度？')) return;
  try {
    await api(`/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' });
    showToast('调度已删除', 'success');
    fetchSchedules();
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

// ========== 提议系统 ==========
function _handleProposalAction(e) {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const pid = btn.dataset.proposalId;
  if (!pid) return;
  if (action === 'confirm-proposal') confirmProposal(pid);
  else if (action === 'reject-proposal') rejectProposal(pid);
  else if (action === 'modify-confirm') modifyAndConfirmProposal(pid);
  else if (action === 'toggle-edit') toggleProposalEditMode(pid);
}

function _handleProposalToolChange(e) {
  if (!e.target.matches('.proposal-tool-chip input[type="checkbox"]')) return;
  const chip = e.target.closest('.proposal-tool-chip');
  if (chip) chip.classList.toggle('checked', e.target.checked);
}

async function fetchProposals() {
  const listEl = document.getElementById('proposalList');
  if (!listEl) return;
  try {
    const data = await api('/proposals');
    renderProposals(data.proposals || []);
  } catch (e) {
    listEl.innerHTML = `<div class="schedule-empty">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function renderProposals(proposals) {
  const listEl = document.getElementById('proposalList');
  if (!listEl) return;
  _proposalsCache = proposals || [];
  const sorted = [...(proposals || [])].sort((a, b) => {
    const priority = { 'pending_confirm': 0, 'confirmed': 1, 'modified': 1, 'schedule_active': 1, 'proposal_created': 1, 'rejected': 2 };
    return (priority[a.status] ?? 99) - (priority[b.status] ?? 99);
  });
  const countEl = document.getElementById('proposalCount');
  if (countEl) countEl.textContent = proposals.length;
  updateScheduleBadge(proposals);
  if (!sorted.length) {
    listEl.innerHTML = '<div class="schedule-empty">暂无待确认的提议</div>';
    return;
  }
  proposals.forEach(p => {
    proposalToolsCache[p.proposal_id] = p.requested_tools || [];
  });
  listEl.innerHTML = sorted.map(p => renderProposalCard(p)).join('');
}

function renderProposalCard(p) {
  const cfg = p.schedule_config || {};
  const tools = p.requested_tools || [];
  const statusLabels = {
    'pending_confirm': '待确认',
    'confirmed': '已确认',
    'modified': '已修改',
    'rejected': '已拒绝',
    'schedule_active': '已创建调度',
    'proposal_created': '已创建',
  };
  const statusLabel = statusLabels[p.status] || p.status;
  const toolsCount = tools.length;
  const namePart = cfg.name ? ` · ${escapeHtml(cfg.name)}` : '';
  const statusTagClass = p.status === 'pending_confirm' ? 'tag-warning' : p.status === 'rejected' ? 'tag-danger' : 'tag-success';
  return `<div class="proposal-card" data-proposal-id="${escapeHtml(p.proposal_id)}">
    <div class="proposal-card-header">
      <span class="proposal-card-title">提议 ${escapeHtml(p.proposal_id)}</span>
      <span class="${statusTagClass}">${escapeHtml(statusLabel)}</span>
    </div>
    <div class="proposal-card-section">
      <div class="proposal-card-config text-sm">
        <span class="mono">${escapeHtml(cfg.cron || '?')}</span> · ${escapeHtml(cfg.task || '?')}${namePart}<br>
        <span class="text-muted">预授权工具 ${toolsCount} 个</span>
      </div>
    </div>
    <div class="proposal-card-actions">
      <button class="btn btn-primary btn-sm" onclick="openProposalDetail('${escapeHtml(p.proposal_id)}')">查看详情</button>
    </div>
  </div>`;
}

function openProposalDetail(proposalId) {
  const p = _proposalsCache.find(x => x.proposal_id === proposalId);
  if (!p) { showToast('未找到提议: ' + proposalId, 'error'); return; }
  const cfg = p.schedule_config || {};
  const tools = p.requested_tools || [];
  const statusLabels = {
    'pending_confirm': '待确认',
    'confirmed': '已确认',
    'modified': '已修改',
    'rejected': '已拒绝',
    'schedule_active': '已创建调度',
    'proposal_created': '已创建',
  };
  const statusLabel = statusLabels[p.status] || p.status;
  const isPending = p.status === 'pending_confirm';

  const toolsHtml = tools.map(t => {
    const toolName = escapeHtml(t.tool || '');
    const scope = escapeHtml(t.scope || 'all');
    const paths = (t.allowed_paths || []).join(', ');
    const pathsStr = paths ? ` <span class="text-muted">[${escapeHtml(paths)}]</span>` : '';
    if (isPending) {
      return `<label class="proposal-tool-chip checked" title="${scope}">
        <input type="checkbox" checked data-tool="${toolName}">
        ${toolName}${pathsStr}
      </label>`;
    }
    return `<span class="proposal-tool-chip checked" title="${scope}">${toolName}${pathsStr}</span>`;
  }).join('');

  const editAreaHtml = isPending ? `
    <div class="form-section" id="editArea_${escapeHtml(p.proposal_id)}" style="display:none;">
      <div class="form-label">修改并确认（编辑后点底部「提交修改并确认」）</div>
      <div class="form-row"><label>Cron</label><input type="text" class="input" id="editCron_${escapeHtml(p.proposal_id)}" value="${escapeHtml(cfg.cron || '')}" placeholder="0 9 * * *"></div>
      <div class="form-row"><label>任务</label><input type="text" class="input" id="editTask_${escapeHtml(p.proposal_id)}" value="${escapeHtml(cfg.task || '')}"></div>
      <div class="form-row"><label>名称</label><input type="text" class="input" id="editName_${escapeHtml(p.proposal_id)}" value="${escapeHtml(cfg.name || '')}"></div>
      <div class="form-label">勾选/取消预授权工具</div>
      <div class="proposal-detail-tools">${toolsHtml}</div>
    </div>
  ` : '';

  const toolsDisplayHtml = isPending ? '' : `
    <div class="form-section">
      <div class="form-label">预授权工具</div>
      <div class="proposal-detail-tools">${toolsHtml}</div>
    </div>
  `;

  const scheduleIdHtml = (p.schedule_id && p.status === 'schedule_active') ? `
    <div class="form-section">
      <div class="form-label">已创建调度项</div>
      <div class="form-value mono">${escapeHtml(p.schedule_id)}</div>
    </div>
  ` : '';

  const workflowHtml = cfg.workflow ? `
    <div class="form-section">
      <div class="form-label">工作流</div>
      <div class="form-value mono">${escapeHtml(JSON.stringify(cfg.workflow))}</div>
    </div>
  ` : '';

  document.getElementById('proposalDetailTitle').textContent = `提议 ${p.proposal_id} · ${statusLabel}`;
  document.getElementById('proposalDetailBody').innerHTML = `
    <div class="form-section">
      <div class="form-label">调度配置</div>
      <div class="form-value">
        ${cfg.name ? `名称: ${escapeHtml(cfg.name)}\n` : ''}Cron: ${escapeHtml(cfg.cron || '?')}\n任务: ${escapeHtml(cfg.task || '?')}
      </div>
    </div>
    ${workflowHtml}
    <div class="form-section">
      <div class="form-label">LLM 说明</div>
      <div class="form-value">${escapeHtml(p.llm_explanation || '(无说明)')}</div>
    </div>
    ${toolsDisplayHtml}
    ${scheduleIdHtml}
    ${editAreaHtml}
  `;

  const footer = document.getElementById('proposalDetailFooter');
  if (isPending) {
    footer.innerHTML = `
      <button class="btn btn-ghost" onclick="closeModal('proposalDetailModal')">关闭</button>
      <button class="btn btn-danger btn-sm" data-action="reject-proposal" data-proposal-id="${escapeHtml(p.proposal_id)}">拒绝</button>
      <button class="btn btn-ghost" data-action="toggle-edit" data-proposal-id="${escapeHtml(p.proposal_id)}">修改并确认</button>
      <button class="btn btn-primary" data-action="confirm-proposal" data-proposal-id="${escapeHtml(p.proposal_id)}">确认</button>
    `;
  } else {
    footer.innerHTML = `<button class="btn btn-ghost" onclick="closeModal('proposalDetailModal')">关闭</button>`;
  }
  openModal('proposalDetailModal');
}

function toggleProposalEditMode(proposalId) {
  const editArea = document.getElementById(`editArea_${proposalId}`);
  if (!editArea) return;
  const isEditing = editArea.style.display !== 'none';
  editArea.style.display = isEditing ? 'none' : '';
  const footer = document.getElementById('proposalDetailFooter');
  if (!footer) return;
  if (!isEditing) {
    footer.innerHTML = `
      <button class="btn btn-ghost" onclick="closeModal('proposalDetailModal')">关闭</button>
      <button class="btn btn-primary" data-action="modify-confirm" data-proposal-id="${escapeHtml(proposalId)}">提交修改并确认</button>
      <button class="btn btn-ghost" data-action="toggle-edit" data-proposal-id="${escapeHtml(proposalId)}">取消</button>
    `;
  } else {
    footer.innerHTML = `
      <button class="btn btn-ghost" onclick="closeModal('proposalDetailModal')">关闭</button>
      <button class="btn btn-danger btn-sm" data-action="reject-proposal" data-proposal-id="${escapeHtml(proposalId)}">拒绝</button>
      <button class="btn btn-ghost" data-action="toggle-edit" data-proposal-id="${escapeHtml(proposalId)}">修改并确认</button>
      <button class="btn btn-primary" data-action="confirm-proposal" data-proposal-id="${escapeHtml(proposalId)}">确认</button>
    `;
  }
}

async function confirmProposal(proposalId) {
  if (!confirm('确认此提议并创建调度项？')) return;
  try {
    const data = await api(`/proposals/${encodeURIComponent(proposalId)}/confirm`, { method: 'POST' });
    showToast(data.message || '提议已确认，调度项已创建', 'success');
    closeModal('proposalDetailModal');
    fetchProposals();
    fetchSchedules();
  } catch (e) {
    showToast('确认失败: ' + e.message, 'error');
  }
}

async function rejectProposal(proposalId) {
  if (!confirm('确认拒绝此提议？拒绝后不会创建调度项。')) return;
  try {
    const data = await api(`/proposals/${encodeURIComponent(proposalId)}/reject`, { method: 'POST' });
    showToast(data.message || '提议已拒绝', 'success');
    closeModal('proposalDetailModal');
    fetchProposals();
  } catch (e) {
    showToast('拒绝失败: ' + e.message, 'error');
  }
}

async function modifyAndConfirmProposal(proposalId) {
  const cronInput = document.getElementById(`editCron_${proposalId}`);
  const taskInput = document.getElementById(`editTask_${proposalId}`);
  const nameInput = document.getElementById(`editName_${proposalId}`);
  if (!cronInput || !taskInput) {
    showToast('编辑区未就绪', 'error');
    return;
  }
  const cronVal = cronInput.value.trim();
  const taskVal = taskInput.value.trim();
  if (!cronVal || !taskVal) {
    showToast('Cron 表达式和任务描述不能为空', 'error');
    return;
  }
  const scheduleConfigUpdates = { cron: cronVal, task: taskVal };
  if (nameInput && nameInput.value.trim()) {
    scheduleConfigUpdates.name = nameInput.value.trim();
  }
  // 保留原始 scope / allowed_paths（避免修改确认后丢失路径约束）
  const modalBody = document.getElementById('proposalDetailBody');
  const originalTools = proposalToolsCache[proposalId] || [];
  const checkedTools = [];
  if (modalBody) {
    modalBody.querySelectorAll('.proposal-tool-chip input[type="checkbox"]').forEach(cb => {
      if (cb.checked) {
        const toolName = cb.dataset.tool;
        const orig = originalTools.find(t => (t.tool || '') === toolName);
        if (orig) {
          checkedTools.push(Object.assign({}, orig));
        } else {
          checkedTools.push({ tool: toolName, scope: 'all', allowed_paths: [] });
        }
      }
    });
  }
  try {
    const data = await api(`/proposals/${encodeURIComponent(proposalId)}/modify`, {
      method: 'POST',
      body: {
        schedule_config_updates: scheduleConfigUpdates,
        requested_tools: checkedTools,
      }
    });
    showToast(data.message || '提议已修改并创建调度项', 'success');
    closeModal('proposalDetailModal');
    fetchProposals();
    fetchSchedules();
  } catch (e) {
    showToast('修改失败: ' + e.message, 'error');
  }
}

// ========== Cron Tool 系统 ==========
async function fetchCronToolsPending() {
  const listEl = document.getElementById('cronToolPendingList');
  if (!listEl) return;
  try {
    const data = await api('/cron_tools/pending');
    renderCronToolsPending(data.pending || []);
  } catch (e) {
    listEl.innerHTML = `<div class="schedule-empty">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function renderCronToolsPending(items) {
  const listEl = document.getElementById('cronToolPendingList');
  if (!listEl) return;
  _pendingCronToolsCache = items || [];
  const countEl = document.getElementById('cronToolPendingCount');
  if (countEl) countEl.textContent = items.length;
  updateScheduleBadge(_proposalsCache);
  if (!items.length) {
    listEl.innerHTML = '<div class="schedule-empty">暂无待审查 cron_tool</div>';
    return;
  }
  listEl.innerHTML = items.map(t => renderCronToolPendingCard(t)).join('');
}

function renderCronToolPendingCard(t) {
  const name = escapeHtml(t.name || '');
  const runExt = escapeHtml(t.run_ext || '.py');
  const mdLines = (t.tool_md || '').split('\n').length;
  const runLines = (t.run_script || '').split('\n').length;
  return `<div class="cron-tool-card">
    <div class="cron-tool-card-header">
      <span><span class="cron-tool-card-name mono">${name}</span><span class="tag tag-warning">待审查</span></span>
      <span class="cron-tool-card-meta text-muted text-sm">TOOL.md ${mdLines}行 · run${runExt} ${runLines}行</span>
    </div>
    <div class="cron-tool-card-actions">
      <button class="btn btn-primary btn-sm" onclick="openCronToolDetail('${name}')">查看详情</button>
    </div>
  </div>`;
}

function openCronToolDetail(name) {
  const t = _pendingCronToolsCache.find(x => x.name === name);
  if (!t) { showToast('未找到 cron_tool: ' + name, 'error'); return; }
  const runExt = escapeHtml(t.run_ext || '.py');
  const toolMd = t.tool_md || '';
  const runScript = t.run_script || '';
  const mdLines = toolMd.split('\n').length;
  const runLines = runScript.split('\n').length;
  document.getElementById('cronToolDetailTitle').textContent = '审查: ' + name;
  document.getElementById('cronToolDetailBody').innerHTML =
    `<div class="schedule-empty">TOOL.md ${mdLines} 行 · run${runExt} ${runLines} 行 · 状态：待审查</div>` +
    `<div class="form-section"><div class="form-label">TOOL.md</div><pre class="tool-card-content">${escapeHtml(toolMd) || '<em class="text-muted">（空）</em>'}</pre></div>` +
    `<div class="form-section"><div class="form-label">run${runExt}</div><pre class="tool-card-content">${escapeHtml(runScript) || '<em class="text-muted">（空）</em>'}</pre></div>`;
  const approveBtn = document.getElementById('cronToolDetailApproveBtn');
  const rejectBtn = document.getElementById('cronToolDetailRejectBtn');
  approveBtn.onclick = () => { closeModal('cronToolDetailModal'); activateCronTool(name); };
  rejectBtn.onclick = () => { closeModal('cronToolDetailModal'); rejectCronTool(name); };
  openModal('cronToolDetailModal');
}

async function activateCronTool(name) {
  if (!confirm(`确认激活 cron_tool "${name}"？激活后可被 cron 调度会话使用。`)) return;
  try {
    const data = await api(`/cron_tools/${encodeURIComponent(name)}/activate`, { method: 'POST' });
    showToast(data.message || `cron_tool ${name} 已激活`, 'success');
    fetchCronToolsPending();
    fetchCronTools();
  } catch (e) {
    showToast('激活失败: ' + e.message, 'error');
  }
}

async function rejectCronTool(name) {
  if (!confirm(`确认拒绝 cron_tool "${name}"？将删除 .pending/ 下的工具文件。`)) return;
  try {
    const data = await api(`/cron_tools/${encodeURIComponent(name)}/reject`, { method: 'POST' });
    showToast(data.message || `cron_tool ${name} 已拒绝`, 'success');
    fetchCronToolsPending();
  } catch (e) {
    showToast('拒绝失败: ' + e.message, 'error');
  }
}

async function fetchCronTools() {
  const listEl = document.getElementById('cronToolList');
  if (!listEl) return;
  try {
    const data = await api('/cron_tools');
    renderCronTools(data.cron_tools || []);
  } catch (e) {
    listEl.innerHTML = `<div class="schedule-empty">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function renderCronTools(items) {
  const listEl = document.getElementById('cronToolList');
  if (!listEl) return;
  _cronToolsCache = items || [];
  const countEl = document.getElementById('cronToolCount');
  if (countEl) countEl.textContent = items.length;
  if (!items.length) {
    listEl.innerHTML = '<div class="schedule-empty">暂无已激活 cron_tool</div>';
    return;
  }
  listEl.innerHTML = items.map(t => renderCronToolCard(t)).join('');
}

function renderCronToolCard(t) {
  const name = escapeHtml(t.name || '');
  const version = escapeHtml(t.version || '');
  const descRaw = t.description || '';
  const desc = escapeHtml(truncateText(descRaw, 60));
  return `<div class="cron-tool-card">
    <div class="cron-tool-card-header">
      <span><span class="cron-tool-card-name mono">${name}</span><span class="tag">v${version}</span></span>
      <span class="cron-tool-card-meta text-sm">${desc || '<em class="text-muted">无描述</em>'}</span>
    </div>
    <div class="cron-tool-card-actions">
      <button class="btn btn-primary btn-sm" onclick="openCronToolActiveDetail('${name}')">查看详情</button>
    </div>
  </div>`;
}

function openCronToolActiveDetail(name) {
  const t = _cronToolsCache.find(x => x.name === name);
  if (!t) { showToast('未找到 cron_tool: ' + name, 'error'); return; }
  const timeout = t.timeout == null ? '默认(30s)' : `${t.timeout}s`;
  document.getElementById('cronToolActiveDetailTitle').textContent = `cron_tool · ${t.name}`;
  document.getElementById('cronToolActiveDetailBody').innerHTML = `
    <div class="form-section"><div class="form-label">名称</div><div class="form-value mono">${escapeHtml(t.name || '')}</div></div>
    <div class="form-section"><div class="form-label">版本</div><div class="form-value">${escapeHtml(t.version || '')}</div></div>
    <div class="form-section"><div class="form-label">描述</div><div class="form-value">${escapeHtml(t.description || '(无描述)')}</div></div>
    <div class="form-section"><div class="form-label">作者</div><div class="form-value">${escapeHtml(t.author || '(未知)')}</div></div>
    <div class="form-section"><div class="form-label">超时</div><div class="form-value">${escapeHtml(timeout)}</div></div>
  `;
  document.getElementById('cronToolActiveDetailFooter').innerHTML = `
    <button class="btn btn-ghost" onclick="closeModal('cronToolActiveDetailModal')">关闭</button>
    <button class="btn btn-danger btn-sm" id="cronToolActiveDeleteBtn">删除</button>
    <button class="btn btn-primary" id="cronToolActiveReloadBtn">重新加载</button>
  `;
  document.getElementById('cronToolActiveReloadBtn').onclick = () => {
    closeModal('cronToolActiveDetailModal');
    reloadCronTool(name);
  };
  document.getElementById('cronToolActiveDeleteBtn').onclick = () => {
    closeModal('cronToolActiveDetailModal');
    deleteCronTool(name);
  };
  openModal('cronToolActiveDetailModal');
}

async function reloadCronTool(name) {
  try {
    const data = await api(`/cron_tools/${encodeURIComponent(name)}`, { method: 'PUT' });
    showToast(data.message || `cron_tool ${name} 已重新加载`, 'success');
    fetchCronTools();
  } catch (e) {
    showToast('重新加载失败: ' + e.message, 'error');
  }
}

async function deleteCronTool(name) {
  if (!confirm(`确认删除 cron_tool "${name}"？将从 registry 注销并删除工具目录，不可恢复。`)) return;
  try {
    const data = await api(`/cron_tools/${encodeURIComponent(name)}`, { method: 'DELETE' });
    showToast(data.message || `cron_tool ${name} 已删除`, 'success');
    fetchCronTools();
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

// ========== 徽章 ==========
function updateScheduleBadge(proposals) {
  const badge = document.getElementById('scheduleBadge');
  if (!badge) return;
  const proposalPending = (proposals || []).filter(p => p.status === 'pending_confirm').length;
  const cronPending = (_pendingCronToolsCache || []).length;
  const totalPending = proposalPending + cronPending;
  if (totalPending > 0) {
    badge.textContent = totalPending;
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }
}

// ========== 调度下拉控制 ==========
function closeScheduleDropdown() {
  const panel = document.getElementById('schedulePanel');
  const btn = document.getElementById('scheduleDropdownBtn');
  if (!panel || !btn) return;
  if (panel.classList.contains('closing')) return;
  panel.classList.add('closing');
  panel.addEventListener('animationend', () => {
    panel.classList.remove('show', 'closing');
    btn.classList.remove('active');
  }, { once: true });
}

// 调度下拉按钮点击处理（chat-main.js 绑定）
function handleScheduleDropdownClick(e) {
  e.stopPropagation();
  const panel = document.getElementById('schedulePanel');
  const btn = document.getElementById('scheduleDropdownBtn');
  if (panel.classList.contains('closing')) {
    panel.classList.remove('closing');
    panel.classList.add('show');
    btn.classList.add('active');
    return;
  }
  const isOpen = panel.classList.toggle('show');
  btn.classList.toggle('active', isOpen);
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
}

window.HermesChatSchedule = {
  fetchScheduleRuns, renderScheduleRuns, startScheduleRunsAutoRefresh,
  fetchSchedules, renderSchedules, openScheduleDetail,
  selectSchedule, loadScheduleMemories, renderScheduleMemories,
  loadScheduleAudit, renderScheduleAudit, deleteScheduleMemory,
  createScheduleUI, updateCreateScheduleBtnState,
  toggleSchedule, triggerSchedule, deleteSchedule,
  fetchProposals, renderProposals, renderProposalCard, openProposalDetail,
  toggleProposalEditMode, confirmProposal, rejectProposal, modifyAndConfirmProposal,
  _handleProposalAction, _handleProposalToolChange,
  fetchCronToolsPending, renderCronToolsPending, openCronToolDetail,
  activateCronTool, rejectCronTool,
  fetchCronTools, renderCronTools, openCronToolActiveDetail,
  reloadCronTool, deleteCronTool,
  updateScheduleBadge, closeScheduleDropdown, handleScheduleDropdownClick,
  handleSchedulePanelClick, handleSchedulePanelChange,
};
