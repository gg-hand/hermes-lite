/* ============================================================
   scheduler.js — 调度管理独立页逻辑
   从 chat-schedule.js 提取，去除 dropdown 相关函数，
   新增 init() 入口、refresh-picker、modal 遮罩关闭、
   表单绑定、run 卡片展开、调度项二级展开。
   修复死按钮 bug（proposalDetailModal 事件绑定）和
   容器定位问题（loadScheduleMemories/Audit 接收容器参数）。
   保持全局函数声明风格，不用 IIFE，确保 inline onclick 可用。
   ============================================================ */

// ========== 状态与缓存 ==========
let _lastScheduleRunId = null;
let selectedScheduleId = null;

const proposalToolsCache = {};
let _proposalsCache = [];
let _schedulesCache = [];
let _cronToolsCache = [];
let _pendingCronToolsCache = [];
let _runsCache = [];  // 缓存 run 详情，供展开时读取

// PENDING 空态判定状态：两个子列表（proposals + cron_tool pending）都加载完后，
// 两者皆为 0 时才显示空态引导
let _proposalsLoaded = false;
let _cronToolPendingLoaded = false;
let _proposalsCount = 0;
let _cronToolPendingCount = 0;

// refresh-picker 状态
let _refreshInterval = 30000;  // 默认 30s
let _refreshTimer = null;
let _isInFlight = false;

// 左列视图状态：'history'（执行历史）/ 'session'（单调度会话）
let currentLeftView = 'history';
let currentSessionScheduleId = null;
let _sessionMessagesCache = [];  // 缓存当前会话视图的消息列表，供重渲染使用

// ========== 调度运行历史 ==========
async function fetchScheduleRuns() {
  const listEl = document.getElementById('scheduleRunsList');
  if (!listEl) return;
  try {
    const limitSel = document.getElementById('runsLimit');
    const limit = limitSel ? parseInt(limitSel.value, 10) : 20;
    const data = await api(`/schedules/runs?limit=${limit}`);
    const runs = data.runs || [];
    const newestId = runs.length ? (runs[0].run_id || '') : '';
    if (newestId !== _lastScheduleRunId || listEl.querySelector('.empty-state, .schedule-empty')) {
      _lastScheduleRunId = newestId;
      _runsCache = runs;
      renderScheduleRuns(runs);
    }
    updateLastUpdate();
  } catch (e) {
    if (!listEl.querySelector('.schedule-run-item')) {
      listEl.innerHTML = `<div class="schedule-empty">加载失败: ${escapeHtml(e.message)}</div>`;
    }
  }
}

function renderScheduleRuns(runs) {
  const listEl = document.getElementById('scheduleRunsList');
  if (!listEl) return;
  _runsCache = runs || [];

  // 更新计数
  const countEl = document.getElementById('runsCount');
  const successEl = document.getElementById('runsSuccess');
  const failEl = document.getElementById('runsFail');
  const successCount = runs.filter(r => r.success !== false).length;
  const failCount = runs.length - successCount;
  if (countEl) countEl.textContent = runs.length;
  if (successEl) successEl.textContent = successCount;
  if (failEl) failEl.textContent = failCount;

  if (!runs.length) {
    listEl.innerHTML = '<div class="schedule-empty">暂无执行记录</div>';
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
    const runId = escapeHtml(r.run_id || '');
    const schedId = escapeHtml(r.schedule_id || '');
    // 调度名点击跳转到会话视图；仅当 schedId 存在时才可点击
    const schedNameAttr = schedId ? `data-schedule-id="${schedId}" title="点击查看调度会话"` : '';
    const schedNameTag = schedId ? 'a' : 'span';
    return `<div class="schedule-run-item ${statusClass}" data-run-id="${runId}">
      <div class="schedule-run-header">
        <span class="schedule-run-time">${escapeHtml(time)}</span>
        <${schedNameTag} class="schedule-run-sched-name" ${schedNameAttr}>${schedName}</${schedNameTag}>
        <span class="schedule-run-status ${statusClass}">${statusLabel}</span>
        <span class="run-expand-toggle" title="点击展开详情">▾</span>
      </div>
      <div class="schedule-run-meta text-muted text-sm">${escapeHtml(meta)}</div>
      <div class="schedule-run-summary">${summary}</div>
      <div class="run-detail"></div>
    </div>`;
  }).join('');
}

function toggleRunDetail(runItem) {
  const runId = runItem.dataset.runId;
  const detailEl = runItem.querySelector('.run-detail');
  if (!detailEl) return;
  const isExpanded = runItem.classList.toggle('expanded');
  if (!isExpanded) return;
  // 首次展开时渲染详情
  if (detailEl.dataset.loaded === '1') return;
  detailEl.dataset.loaded = '1';
  const run = _runsCache.find(r => (r.run_id || '') === runId);
  if (!run) {
    detailEl.innerHTML = '<div class="run-detail-body"><div class="schedule-empty">未找到 run 详情</div></div>';
    return;
  }
  const toolCallsHtml = (run.tool_calls && run.tool_calls.length)
    ? `<div class="run-detail-section">
         <div class="run-detail-label">工具调用 (${run.tool_calls.length})</div>
         <div class="run-detail-content mono"><div class="json-highlight">${highlightJSON(JSON.stringify(run.tool_calls, null, 2))}</div></div>
       </div>`
    : '';
  const responseHtml = run.assistant_response
    ? `<div class="run-detail-section">
         <div class="run-detail-label">完整响应</div>
         <div class="run-detail-content markdown-body">${renderMarkdown(run.assistant_response)}</div>
       </div>`
    : '';
  detailEl.innerHTML = `<div class="run-detail-body">${toolCallsHtml}${responseHtml || '<div class="schedule-empty">无详细响应</div>'}</div>`;
}

// ========== 调度项 CRUD ==========
async function fetchSchedules() {
  try {
    const data = await api('/schedules');
    renderSchedules(data.schedules || []);
  } catch (e) {
    const listEl = document.getElementById('scheduleListFull');
    if (listEl) listEl.innerHTML = `<div class="empty-state"><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderSchedules(schedules) {
  const listEl = document.getElementById('scheduleListFull');
  if (!listEl) return;
  _schedulesCache = schedules || [];
  if (!schedules.length) {
    listEl.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无调度项，点击「+ 新建调度」创建</div></div>';
  } else {
    listEl.innerHTML = schedules.map(s => renderListCard('schedule', s)).join('');
  }
  updateManageBadges();
}

// ========== 统一列表卡片（取代旧的 proposal-card / schedule-item / cron-tool-card） ==========
function renderListCard(kind, item) {
  if (kind === 'schedule') return renderScheduleCard(item);
  if (kind === 'cron_tool_pending') return renderCronToolPendingCard(item);
  if (kind === 'cron_tool_active') return renderCronToolActiveCard(item);
  if (kind === 'proposal_pending' || kind === 'proposal_done') return renderProposalCardItem(item);
  return '';
}

function renderScheduleCard(s) {
  const lastRun = s.last_run ? `上次 ${escapeHtml(formatTime(s.last_run))}` : '上次 未运行';
  const nextRun = s.next_run ? ` · 下次 ${escapeHtml(formatTime(s.next_run))}` : '';
  // 7.1: 状态徽章 — 已禁用优先，其次按最近执行结果判断成功/失败
  let badgeHtml = '';
  if (!s.enabled) {
    badgeHtml = '<span class="status-badge" data-status="disabled">⚫ 已禁用</span>';
  } else {
    // 从 _runsCache 查找该调度最近一次执行
    const recentRun = (_runsCache || []).find(r => r.schedule_id === s.id);
    if (recentRun) {
      if (recentRun.success === false) {
        badgeHtml = '<span class="status-badge" data-status="failed">🔴 失败</span>';
      } else {
        badgeHtml = '<span class="status-badge" data-status="success">🟢 正常</span>';
      }
    } else {
      badgeHtml = '<span class="status-badge" data-status="success">🟢 正常</span>';
    }
  }
  const sid = escapeHtml(s.id);
  return `<div class="list-card kind-schedule" data-kind="schedule" data-id="${sid}" onclick="openDrawer({kind:'schedule', id:'${sid}'})">
    <div class="list-card-header">
      <span class="list-card-status-dot ${s.enabled ? 'is-enabled' : 'is-disabled'}" title="${s.enabled ? '已启用' : '已禁用'}"></span>
      <span class="list-card-title">${escapeHtml(s.name || s.id)}</span>
      ${badgeHtml}
    </div>
    <div class="list-card-meta">
      <span class="mono">${escapeHtml(s.cron || '')}</span>
      <span class="text-muted">${lastRun}${nextRun}</span>
    </div>
    <div class="list-card-actions-row" onclick="event.stopPropagation()">
      <button class="btn btn-rerun" type="button" onclick="triggerRerun('${sid}')" title="立即触发一次执行">立即执行</button>
      <button class="btn btn-config" type="button" onclick="openConfigEditor('${sid}')" title="编辑配置">配置</button>
    </div>
  </div>`;
}

function renderCronToolPendingCard(t) {
  const name = escapeHtml(t.name || '');
  const runExt = escapeHtml(t.run_ext || '.py');
  const mdLines = (t.tool_md || '').split('\n').length;
  const runLines = (t.run_script || '').split('\n').length;
  return `<div class="list-card kind-cron_tool_pending" data-kind="cron_tool_pending" data-id="${name}" onclick="openDrawer({kind:'cron_tool_pending', id:'${name}'})">
    <div class="list-card-header">
      <span class="list-card-title mono">${name}</span>
      <span class="tag tag-warning">待审查</span>
    </div>
    <div class="list-card-meta text-muted">TOOL.md ${mdLines}行 · run${runExt} ${runLines}行</div>
  </div>`;
}

function renderCronToolActiveCard(t) {
  const name = escapeHtml(t.name || '');
  const version = escapeHtml(t.version || '');
  const descRaw = t.description || '';
  const desc = escapeHtml(truncateText(descRaw, 60));
  return `<div class="list-card kind-cron_tool_active" data-kind="cron_tool_active" data-id="${name}" onclick="openDrawer({kind:'cron_tool_active', id:'${name}'})">
    <div class="list-card-header">
      <span class="list-card-title mono">${name}</span>
      <span class="tag">v${version}</span>
    </div>
    <div class="list-card-meta">${desc || '<em class="text-muted">无描述</em>'}</div>
  </div>`;
}

function renderProposalCardItem(p) {
  const cfg = p.schedule_config || {};
  const tools = p.requested_tools || [];
  const statusLabels = {
    'pending_confirm': '待确认', 'confirmed': '已确认', 'modified': '已修改',
    'rejected': '已拒绝', 'schedule_active': '已创建调度', 'proposal_created': '已创建',
  };
  const statusLabel = statusLabels[p.status] || p.status;
  const kindClass = p.status === 'pending_confirm' ? 'kind-proposal_pending' : 'kind-schedule';
  const isPending = p.status === 'pending_confirm';
  const statusTagClass = p.status === 'pending_confirm' ? 'tag-warning' : p.status === 'rejected' ? 'tag-danger' : 'tag-success';
  const drawerKind = isPending ? 'proposal_pending' : 'proposal_done';
  return `<div class="list-card ${kindClass}" data-kind="${drawerKind}" data-id="${escapeHtml(p.proposal_id)}" onclick="openDrawer({kind:'${drawerKind}', id:'${escapeHtml(p.proposal_id)}'})">
    <div class="list-card-header">
      <span class="list-card-title">提议 ${escapeHtml(p.proposal_id)}</span>
      <span class="${statusTagClass}">${escapeHtml(statusLabel)}</span>
    </div>
    <div class="list-card-meta">
      <span class="mono">${escapeHtml(cfg.cron || '?')}</span>
      <span class="text-muted">${escapeHtml(truncateText(cfg.task || '?', 80))}</span>
    </div>
    <div class="list-card-meta text-muted">预授权工具 ${tools.length} 个</div>
  </div>`;
}

// ========== Tab 切换 + Badge 更新 ==========
function switchManageTab(tabName) {
  _activeManageTab = tabName;
  document.querySelectorAll('.sched-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.manageTab === tabName);
  });
  document.querySelectorAll('.manage-pane').forEach(p => {
    p.hidden = (p.dataset.managePane !== tabName);
  });
  refreshActiveTab();
}

function refreshActiveTab() {
  if (_activeManageTab === 'pending') {
    fetchProposals();
    fetchCronToolsPending();
  } else if (_activeManageTab === 'schedules') {
    fetchSchedules();
  } else if (_activeManageTab === 'cronTool') {
    fetchCronTools();
  }
}

function updateManageBadges() {
  const pendingCount = _proposalsCache.filter(p => p.status === 'pending_confirm').length + _pendingCronToolsCache.length;
  const schedulesCount = _schedulesCache.length;
  const cronToolCount = _cronToolsCache.length;
  const setBadge = (id, n) => {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = String(n);
      el.hidden = n === 0;
    }
  };
  setBadge('tabBadgePending', pendingCount);
  setBadge('tabBadgeSchedules', schedulesCount);
  setBadge('tabBadgeCronTool', cronToolCount);
}

// ========== Drawer 核心（重写：统一抽屉替代 4 个 modal） ==========
let _activeManageTab = 'pending';   // 'pending' | 'schedules' | 'cronTool'
let _drawerState = null;            // { kind, id }
let _drawerActiveSubTab = 'detail'; // 'detail' | 'memories' | 'audit'

function findInDrawerCache(kind, id) {
  if (kind === 'proposal_pending' || kind === 'proposal_done') {
    return _proposalsCache.find(x => x.proposal_id === id);
  }
  if (kind === 'schedule') {
    return _schedulesCache.find(x => x.id === id);
  }
  if (kind === 'cron_tool_pending') {
    return _pendingCronToolsCache.find(x => x.name === id);
  }
  if (kind === 'cron_tool_active') {
    return _cronToolsCache.find(x => x.name === id);
  }
  return null;
}

function titleFor(kind, item) {
  if (kind === 'proposal_pending' || kind === 'proposal_done') return `提议 · ${item.proposal_id}`;
  if (kind === 'schedule') return item.name || `调度 · ${item.id}`;
  if (kind === 'cron_tool_pending') return `审查 · ${item.name}`;
  if (kind === 'cron_tool_active') return `cron_tool · ${item.name}`;
  return '详情';
}

function subtitleFor(kind, item) {
  if (kind === 'proposal_pending' || kind === 'proposal_done') {
    const cfg = item.schedule_config || {};
    return cfg.cron ? `cron: ${cfg.cron}` : '';
  }
  if (kind === 'schedule') {
    return item.cron ? `cron: ${item.cron} · ${item.enabled ? '已启用' : '已禁用'}` : '';
  }
  if (kind === 'cron_tool_pending') return '待审查';
  if (kind === 'cron_tool_active') return `v${item.version || '?'} · ${item.author || '未知作者'}`;
  return '';
}

async function openDrawer({ kind, id }) {
  _drawerState = { kind, id };
  const overlay = document.getElementById('drawerOverlay');
  const item = findInDrawerCache(kind, id);
  if (!item) { showToast('未找到条目: ' + id, 'error'); return; }
  document.getElementById('drawerTitle').textContent = titleFor(kind, item);
  document.getElementById('drawerSubtitle').textContent = subtitleFor(kind, item);
  document.getElementById('drawerBody').innerHTML = '<div class="empty-state"><div class="empty-state-text">加载中...</div></div>';
  document.getElementById('drawerFooter').innerHTML = '';
  overlay.hidden = false;
  try {
    await mountDrawerContent(kind, item);
    bindDrawerActions(kind, id);
  } catch (e) {
    document.getElementById('drawerBody').innerHTML = `<div class="empty-state"><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function closeDrawer() {
  const overlay = document.getElementById('drawerOverlay');
  overlay.hidden = true;
  _drawerState = null;
  document.getElementById('drawerBody').innerHTML = '';
  document.getElementById('drawerFooter').innerHTML = '';
  document.getElementById('drawerTabBar').hidden = true;
}

async function mountDrawerContent(kind, item) {
  const body = document.getElementById('drawerBody');
  const tabBar = document.getElementById('drawerTabBar');
  if (kind === 'schedule') {
    // 调度项：3 个子 pane
    body.innerHTML = `
      <div class="drawer-pane active" data-drawer-pane="detail">${renderScheduleDetail(item)}</div>
      <div class="drawer-pane" data-drawer-pane="memories"><div class="empty-state"><div class="empty-state-text">加载中...</div></div></div>
      <div class="drawer-pane" data-drawer-pane="audit"><div class="empty-state"><div class="empty-state-text">加载中...</div></div></div>
    `;
    tabBar.hidden = false;
    _drawerActiveSubTab = 'detail';
    updateDrawerSubTabActive();
    // 后台异步加载记忆/审计
    loadScheduleMemoriesForDrawer(item.id);
    loadScheduleAuditForDrawer(item.id);
  } else {
    body.innerHTML = `<div class="drawer-pane active">${renderKindDetail(kind, item)}</div>`;
    tabBar.hidden = true;
  }
}

function updateDrawerSubTabActive() {
  document.querySelectorAll('.drawer-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.drawerTab === _drawerActiveSubTab);
  });
  document.querySelectorAll('.drawer-pane').forEach(p => {
    p.classList.toggle('active', p.dataset.drawerPane === _drawerActiveSubTab);
  });
}

function switchDrawerSubTab(name) {
  _drawerActiveSubTab = name;
  updateDrawerSubTabActive();
}

function renderKindDetail(kind, item) {
  if (kind === 'proposal_pending' || kind === 'proposal_done') return renderProposalDetail(item);
  if (kind === 'cron_tool_pending') return renderCronToolPendingDetail(item);
  if (kind === 'cron_tool_active') return renderCronToolActiveDetail(item);
  return '<div class="empty-state"><div class="empty-state-text">未知类型</div></div>';
}

function renderScheduleDetail(s) {
  return `
    <div class="drawer-section">
      <div class="drawer-section-label">名称</div>
      <div class="drawer-section-value is-plain">${escapeHtml(s.name || '')}</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">Cron 表达式</div>
      <div class="drawer-section-value is-mono">${escapeHtml(s.cron || '')}</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">任务描述</div>
      <div class="drawer-section-value is-plain">${escapeHtml(s.task || '')}</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">启用状态</div>
      <div class="drawer-section-value is-plain">
        <label class="schedule-toggle" title="${s.enabled ? '已启用' : '已禁用'}">
          <input type="checkbox" id="drawerScheduleToggle" ${s.enabled ? 'checked' : ''}>
          <span class="schedule-toggle-slider"></span>
          <span class="text-muted text-sm" style="margin-left:8px">${s.enabled ? '已启用（即时生效）' : '已禁用（即时生效）'}</span>
        </label>
      </div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">调度时序</div>
      <div class="drawer-section-value is-plain">
        ${s.last_run ? `上次执行: ${escapeHtml(formatTime(s.last_run))}\n` : '上次执行: 未运行\n'}
        ${s.next_run ? `下次执行: ${escapeHtml(formatTime(s.next_run))}` : '下次执行: 未计算'}
      </div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">调度项 ID</div>
      <div class="drawer-section-value is-mono">${escapeHtml(s.id || '')}</div>
    </div>
  `;
}

function renderProposalDetail(p) {
  const cfg = p.schedule_config || {};
  const tools = p.requested_tools || [];
  const statusLabels = {
    'pending_confirm': '待确认', 'confirmed': '已确认', 'modified': '已修改',
    'rejected': '已拒绝', 'schedule_active': '已创建调度', 'proposal_created': '已创建',
  };
  const statusLabel = statusLabels[p.status] || p.status;
  const isPending = p.status === 'pending_confirm';
  const toolsHtml = tools.map(t => {
    const toolName = escapeHtml(t.tool || '');
    const scope = escapeHtml(t.scope || 'all');
    const paths = (t.allowed_paths || []).join(', ');
    const pathsStr = paths ? ` <span class="text-muted">[${escapeHtml(paths)}]</span>` : '';
    if (isPending) {
      return `<label class="drawer-chip-tool checked" title="${scope}">
        <input type="checkbox" checked data-tool="${toolName}">
        ${toolName}${pathsStr}
      </label>`;
    }
    return `<span class="drawer-chip-tool checked" title="${scope}">${toolName}${pathsStr}</span>`;
  }).join('');

  const editAreaHtml = isPending ? `
    <div class="drawer-edit-area" id="drawerEditArea_${escapeHtml(p.proposal_id)}" hidden>
      <div class="drawer-section-label" style="margin-bottom:6px">修改后提交</div>
      <div class="form-row"><label>Cron</label><input type="text" class="form-input" id="drawerEditCron_${escapeHtml(p.proposal_id)}" value="${escapeHtml(cfg.cron || '')}" placeholder="0 9 * * *"></div>
      <div class="form-row"><label>任务</label><input type="text" class="form-input" id="drawerEditTask_${escapeHtml(p.proposal_id)}" value="${escapeHtml(cfg.task || '')}"></div>
      <div class="form-row"><label>名称</label><input type="text" class="form-input" id="drawerEditName_${escapeHtml(p.proposal_id)}" value="${escapeHtml(cfg.name || '')}"></div>
      <div class="drawer-section-label" style="margin:6px 0">勾选/取消预授权工具</div>
      <div class="drawer-chips">${toolsHtml}</div>
    </div>
  ` : '';

  const toolsDisplayHtml = isPending ? '' : `
    <div class="drawer-section">
      <div class="drawer-section-label">预授权工具</div>
      <div class="drawer-chips">${toolsHtml}</div>
    </div>
  `;

  const scheduleIdHtml = (p.schedule_id && p.status === 'schedule_active') ? `
    <div class="drawer-section">
      <div class="drawer-section-label">已创建调度项</div>
      <div class="drawer-section-value is-mono">${escapeHtml(p.schedule_id)}</div>
    </div>
  ` : '';

  const workflowHtml = cfg.workflow ? `
    <div class="drawer-section">
      <div class="drawer-section-label">工作流</div>
      <div class="drawer-section-value is-mono"><pre>${escapeHtml(JSON.stringify(cfg.workflow, null, 2))}</pre></div>
    </div>
  ` : '';

  return `
    <div class="drawer-section">
      <div class="drawer-section-label">状态</div>
      <div class="drawer-section-value is-plain">${escapeHtml(statusLabel)}</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">调度配置</div>
      <div class="drawer-section-value is-plain">
        ${cfg.name ? `名称: ${escapeHtml(cfg.name)}\n` : ''}Cron: ${escapeHtml(cfg.cron || '?')}\n任务: ${escapeHtml(cfg.task || '?')}
      </div>
    </div>
    ${workflowHtml}
    <div class="drawer-section">
      <div class="drawer-section-label">LLM 说明</div>
      <div class="drawer-section-value is-plain">${escapeHtml(p.llm_explanation || '(无说明)')}</div>
    </div>
    ${toolsDisplayHtml}
    ${scheduleIdHtml}
    ${editAreaHtml}
  `;
}

function renderCronToolPendingDetail(t) {
  const runExt = escapeHtml(t.run_ext || '.py');
  const toolMd = t.tool_md || '';
  const runScript = t.run_script || '';
  const mdLines = toolMd.split('\n').length;
  const runLines = runScript.split('\n').length;
  return `
    <div class="drawer-section">
      <div class="drawer-section-label">元信息</div>
      <div class="drawer-section-value is-plain">TOOL.md ${mdLines} 行 · run${runExt} ${runLines} 行 · 状态：待审查</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">TOOL.md</div>
      <div class="drawer-section-value is-mono">${escapeHtml(toolMd) || '<em class="text-muted">（空）</em>'}</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">run${runExt}</div>
      <div class="drawer-section-value is-mono">${escapeHtml(runScript) || '<em class="text-muted">（空）</em>'}</div>
    </div>
  `;
}

function renderCronToolActiveDetail(t) {
  const timeout = t.timeout == null ? '默认(30s)' : `${t.timeout}s`;
  return `
    <div class="drawer-section">
      <div class="drawer-section-label">名称</div>
      <div class="drawer-section-value is-mono">${escapeHtml(t.name || '')}</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">版本</div>
      <div class="drawer-section-value is-plain">${escapeHtml(t.version || '')}</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">描述</div>
      <div class="drawer-section-value is-plain">${escapeHtml(t.description || '(无描述)')}</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">作者</div>
      <div class="drawer-section-value is-plain">${escapeHtml(t.author || '(未知)')}</div>
    </div>
    <div class="drawer-section">
      <div class="drawer-section-label">超时</div>
      <div class="drawer-section-value is-plain">${escapeHtml(timeout)}</div>
    </div>
  `;
}

function bindDrawerActions(kind, id) {
  const footer = document.getElementById('drawerFooter');
  if (!footer) return;
  if (kind === 'proposal_pending') {
    footer.innerHTML = `
      <button class="btn btn-ghost" data-drawer-action="close">关闭</button>
      <button class="btn btn-danger btn-sm" data-drawer-action="reject">拒绝</button>
      <button class="btn btn-ghost" data-drawer-action="toggle-edit">修改并确认</button>
      <button class="btn btn-primary" data-drawer-action="confirm">确认</button>
    `;
    footer.onclick = (e) => handleProposalDrawerAction(e, id);
    // 工具勾选 chip 变化
    const body = document.getElementById('drawerBody');
    if (body) {
      body.addEventListener('change', (e) => {
        if (e.target.matches('.drawer-chip-tool input[type="checkbox"]')) {
          const chip = e.target.closest('.drawer-chip-tool');
          if (chip) chip.classList.toggle('checked', e.target.checked);
        }
      });
    }
  } else if (kind === 'schedule') {
    footer.innerHTML = `
      <button class="btn btn-ghost" data-drawer-action="close">关闭</button>
      <button class="btn btn-danger btn-sm" data-drawer-action="delete">删除</button>
      <button class="btn btn-primary" data-drawer-action="trigger">立即触发</button>
    `;
    footer.onclick = (e) => handleScheduleDrawerAction(e, id);
    // 启停开关
    const toggle = document.getElementById('drawerScheduleToggle');
    if (toggle) {
      toggle.addEventListener('change', () => {
        toggleSchedule(id, toggle.checked);
        const hint = toggle.parentElement.querySelector('span:last-child');
        if (hint) hint.textContent = toggle.checked ? '已启用（即时生效）' : '已禁用（即时生效）';
      });
    }
  } else if (kind === 'cron_tool_pending') {
    footer.innerHTML = `
      <button class="btn btn-ghost" data-drawer-action="close">关闭</button>
      <button class="btn btn-danger btn-sm" data-drawer-action="reject">拒绝</button>
      <button class="btn btn-primary" data-drawer-action="activate">批准</button>
    `;
    footer.onclick = (e) => handleCronToolPendingDrawerAction(e, id);
  } else if (kind === 'cron_tool_active') {
    footer.innerHTML = `
      <button class="btn btn-ghost" data-drawer-action="close">关闭</button>
      <button class="btn btn-danger btn-sm" data-drawer-action="delete">删除</button>
      <button class="btn btn-primary" data-drawer-action="reload">重新加载</button>
    `;
    footer.onclick = (e) => handleCronToolActiveDrawerAction(e, id);
  }
}

function handleProposalDrawerAction(e, proposalId) {
  const btn = e.target.closest('[data-drawer-action]');
  if (!btn) return;
  const action = btn.dataset.drawerAction;
  if (action === 'close') closeDrawer();
  else if (action === 'confirm') confirmProposal(proposalId);
  else if (action === 'reject') rejectProposal(proposalId);
  else if (action === 'toggle-edit') toggleDrawerProposalEditMode(proposalId);
}

function toggleDrawerProposalEditMode(proposalId) {
  const editArea = document.getElementById(`drawerEditArea_${proposalId}`);
  if (!editArea) return;
  const isEditing = !editArea.hidden;
  editArea.hidden = isEditing;
  const footer = document.getElementById('drawerFooter');
  if (!footer) return;
  if (!isEditing) {
    footer.innerHTML = `
      <button class="btn btn-ghost" data-drawer-action="close">关闭</button>
      <button class="btn btn-primary" data-drawer-action="modify-confirm">提交修改并确认</button>
      <button class="btn btn-ghost" data-drawer-action="toggle-edit">取消</button>
    `;
  } else {
    footer.innerHTML = `
      <button class="btn btn-ghost" data-drawer-action="close">关闭</button>
      <button class="btn btn-danger btn-sm" data-drawer-action="reject">拒绝</button>
      <button class="btn btn-ghost" data-drawer-action="toggle-edit">修改并确认</button>
      <button class="btn btn-primary" data-drawer-action="confirm">确认</button>
    `;
  }
}

function handleScheduleDrawerAction(e, scheduleId) {
  const btn = e.target.closest('[data-drawer-action]');
  if (!btn) return;
  const action = btn.dataset.drawerAction;
  if (action === 'close') closeDrawer();
  else if (action === 'delete') { closeDrawer(); deleteSchedule(scheduleId); }
  else if (action === 'trigger') { closeDrawer(); triggerSchedule(scheduleId); }
}

function handleCronToolPendingDrawerAction(e, name) {
  const btn = e.target.closest('[data-drawer-action]');
  if (!btn) return;
  const action = btn.dataset.drawerAction;
  if (action === 'close') closeDrawer();
  else if (action === 'activate') { closeDrawer(); activateCronTool(name); }
  else if (action === 'reject') { closeDrawer(); rejectCronTool(name); }
}

function handleCronToolActiveDrawerAction(e, name) {
  const btn = e.target.closest('[data-drawer-action]');
  if (!btn) return;
  const action = btn.dataset.drawerAction;
  if (action === 'close') closeDrawer();
  else if (action === 'delete') { closeDrawer(); deleteCronTool(name); }
  else if (action === 'reload') { closeDrawer(); reloadCronTool(name); }
}

async function loadScheduleMemoriesForDrawer(id) {
  const pane = document.querySelector('.drawer-pane[data-drawer-pane="memories"]');
  if (!pane) return;
  try {
    const data = await api(`/schedules/${encodeURIComponent(id)}/memories?limit=20`);
    renderScheduleMemoryList(data.memories || [], pane, id);
    // 更新右上角 badge
    const badge = document.getElementById('drawerBadgeMemories');
    if (badge) {
      const n = (data.memories || []).length;
      badge.textContent = String(n);
      badge.hidden = n === 0;
    }
  } catch (e) {
    pane.innerHTML = `<div class="empty-state"><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderScheduleMemoryList(memories, container, scheduleId) {
  if (!container) return;
  if (!memories.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无隔离记忆</div></div>';
    return;
  }
  container.innerHTML = memories.map(m => `
    <div class="schedule-memory-item" data-id="${escapeHtml(m.id)}">
      <div class="memory-text">${escapeHtml(truncateText(m.content || '', 400))}</div>
      <div class="memory-footer">
        <button class="btn btn-danger btn-sm" data-drawer-action="del-cron-mem" data-schedule-id="${escapeHtml(scheduleId)}" data-memory-id="${escapeHtml(m.id)}">删除</button>
      </div>
    </div>
  `).join('');
  // 绑定删除事件
  container.onclick = (e) => {
    const btn = e.target.closest('[data-drawer-action="del-cron-mem"]');
    if (btn) deleteScheduleMemory(btn.dataset.scheduleId, btn.dataset.memoryId);
  };
}

async function loadScheduleAuditForDrawer(id) {
  const pane = document.querySelector('.drawer-pane[data-drawer-pane="audit"]');
  if (!pane) return;
  try {
    const data = await api(`/schedules/${encodeURIComponent(id)}/audit?limit=50`);
    renderScheduleAuditList(data.logs || [], pane);
    // 更新右上角 badge
    const badge = document.getElementById('drawerBadgeAudit');
    if (badge) {
      const n = (data.logs || []).length;
      badge.textContent = String(n);
      badge.hidden = n === 0;
    }
  } catch (e) {
    pane.innerHTML = `<div class="empty-state"><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderScheduleAuditList(logs, container) {
  if (!container) return;
  if (!logs.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无工具调用审计</div></div>';
    return;
  }
  container.innerHTML = logs.map(l => {
    const source = l.decision_source || 'default_rule';
    const sourceLabelMap = {
      schedule_grant: '预授权', user_confirm: '用户确认', default_rule: '默认规则',
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

// ========== 调度项二级展开已废弃（功能移入 drawer 次级 Tab） ==========
// 旧的 toggleScheduleExpand / loadScheduleMemories / loadScheduleAudit /
// renderScheduleMemories / renderScheduleAudit / deleteScheduleMemory 等函数
// 已被上面的 loadScheduleMemoriesForDrawer / loadScheduleAuditForDrawer /
// renderScheduleMemoryList / renderScheduleAuditList 替代。

async function deleteScheduleMemory(scheduleId, memoryId) {
  if (!confirm('确认删除此条隔离记忆？')) return;
  try {
    await api(`/schedules/${encodeURIComponent(scheduleId)}/memories/${encodeURIComponent(memoryId)}`, { method: 'DELETE' });
    showToast('记忆已删除', 'success');
    // 如果 drawer 当前打开的是该调度项的记忆 pane，刷新之
    if (_drawerState && _drawerState.kind === 'schedule' && _drawerState.id === scheduleId) {
      loadScheduleMemoriesForDrawer(scheduleId);
    }
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
  // 序列化 workflow 配置（若选择简易/多步模式）
  const workflow = serializeWorkflowConfig();
  if (workflow === false) return;  // 校验失败，错误已通过 toast 提示

  const btn = document.getElementById('createScheduleBtn');
  btn.disabled = true;
  btn.textContent = '创建中...';
  try {
    const body = { name, cron, task, enabled };
    if (workflow) body.workflow = workflow;
    const data = await api('/schedules', { method: 'POST', body });
    closeModal('scheduleModal');
    document.getElementById('schedName').value = '';
    document.getElementById('schedCron').value = '';
    document.getElementById('schedTask').value = '';
    // 重置 workflow 配置
    resetWorkflowConfig();
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
  const btn = document.getElementById('createScheduleBtn');
  if (btn) btn.disabled = !(name && cron && task);
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
async function fetchProposals() {
  try {
    const data = await api('/proposals');
    renderProposals(data.proposals || []);
  } catch (e) {
    const listEl = document.getElementById('pendingListFull');
    if (listEl) listEl.innerHTML = `<div class="empty-state"><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderProposals(proposals) {
  const listEl = document.getElementById('pendingListFull');
  if (!listEl) return;
  _proposalsCache = proposals || [];
  proposals.forEach(p => {
    proposalToolsCache[p.proposal_id] = p.requested_tools || [];
  });
  // 合并：proposals 按状态排序 + pending cron_tools
  const items = [];
  proposals.forEach(p => items.push({ kind: p.status === 'pending_confirm' ? 'proposal_pending' : 'proposal_done', item: p }));
  _pendingCronToolsCache.forEach(t => items.push({ kind: 'cron_tool_pending', item: t }));
  // 排序：pending_confirm 在前，其它按时间倒序
  items.sort((a, b) => {
    const rank = { proposal_pending: 0, cron_tool_pending: 1, proposal_done: 2 };
    return (rank[a.kind] ?? 99) - (rank[b.kind] ?? 99);
  });
  if (!items.length) {
    listEl.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无待办</div></div>';
  } else {
    listEl.innerHTML = items.map(({ kind, item }) => renderListCard(kind, item)).join('');
  }
  updateManageBadges();

  // URL 参数 ?proposal_id=xxx 自动打开 drawer
  if (window._pendingProposalId) {
    const targetId = window._pendingProposalId;
    window._pendingProposalId = null;
    const found = proposals.find(p => p.proposal_id === targetId);
    if (found) {
      const kind = found.status === 'pending_confirm' ? 'proposal_pending' : 'proposal_done';
      setTimeout(() => openDrawer({ kind, id: targetId }), 50);
    } else {
      showToast(`未找到提议: ${targetId}`, 'error');
    }
  }
}

async function confirmProposal(proposalId) {
  if (!confirm('确认此提议并创建调度项？')) return;
  try {
    const data = await api(`/proposals/${encodeURIComponent(proposalId)}/confirm`, { method: 'POST' });
    showToast(data.message || '提议已确认，调度项已创建', 'success');
    closeDrawer();
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
    closeDrawer();
    fetchProposals();
  } catch (e) {
    showToast('拒绝失败: ' + e.message, 'error');
  }
}

async function modifyAndConfirmProposal(proposalId) {
  const cronInput = document.getElementById(`drawerEditCron_${proposalId}`);
  const taskInput = document.getElementById(`drawerEditTask_${proposalId}`);
  const nameInput = document.getElementById(`drawerEditName_${proposalId}`);
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
  const modalBody = document.getElementById('drawerBody');
  const originalTools = proposalToolsCache[proposalId] || [];
  const checkedTools = [];
  if (modalBody) {
    modalBody.querySelectorAll('.drawer-chip-tool input[type="checkbox"]').forEach(cb => {
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
    closeDrawer();
    fetchProposals();
    fetchSchedules();
  } catch (e) {
    showToast('修改失败: ' + e.message, 'error');
  }
}

// ========== Cron Tool 系统 ==========
async function fetchCronToolsPending() {
  try {
    const data = await api('/cron_tools/pending');
    renderCronToolsPending(data.pending || []);
  } catch (e) {
    const listEl = document.getElementById('pendingListFull');
    if (listEl) listEl.innerHTML = `<div class="empty-state"><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderCronToolsPending(items) {
  _pendingCronToolsCache = items || [];
  // 不再单独渲染列表，而是让 renderProposals 合并
  // 重新触发 renderProposals 合并渲染
  if (_proposalsCache.length || _pendingCronToolsCache.length) {
    renderProposals(_proposalsCache);
  } else {
    const listEl = document.getElementById('pendingListFull');
    if (listEl) listEl.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无待办</div></div>';
  }
  updateManageBadges();
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
  try {
    const data = await api('/cron_tools');
    renderCronTools(data.cron_tools || []);
  } catch (e) {
    const listEl = document.getElementById('cronToolListFull');
    if (listEl) listEl.innerHTML = `<div class="empty-state"><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderCronTools(items) {
  const listEl = document.getElementById('cronToolListFull');
  if (!listEl) return;
  _cronToolsCache = items || [];
  if (!items.length) {
    listEl.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无已激活 cron_tool</div></div>';
  } else {
    listEl.innerHTML = items.map(t => renderListCard('cron_tool_active', t)).join('');
  }
  updateManageBadges();
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

// ========== refresh-picker（镜像 monitor.js 模式）==========
async function refreshAll() {
  if (_isInFlight) return;
  _isInFlight = true;
  try {
    await Promise.all([
      fetchScheduleRuns(),
      fetchSchedules(),
      fetchProposals(),
      fetchCronToolsPending(),
      fetchCronTools(),
    ]);
  } finally {
    _isInFlight = false;
  }
}

function startTimer() {
  stopTimer();
  if (_refreshInterval > 0) {
    _refreshTimer = setInterval(refreshAll, _refreshInterval);
  }
}

function stopTimer() {
  if (_refreshTimer) {
    clearInterval(_refreshTimer);
    _refreshTimer = null;
  }
}

function updateLastUpdate() {
  const el = document.getElementById('lastUpdate');
  if (el) {
    const now = new Date();
    const pad = n => String(n).padStart(2, '0');
    el.textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  }
}

// ========== 左列视图切换：执行历史 <-> 单调度会话 ==========
function switchLeftViewToHistory() {
  currentLeftView = 'history';
  currentSessionScheduleId = null;
  const viewHistory = document.getElementById('leftColViewHistory');
  const viewSession = document.getElementById('leftColViewSession');
  if (viewHistory) viewHistory.hidden = false;
  if (viewSession) viewSession.hidden = true;
  // 切换控件
  document.querySelectorAll('.left-col-mode-history').forEach(el => el.hidden = false);
  document.querySelectorAll('.left-col-mode-session').forEach(el => el.hidden = true);
  // 切换标题
  const labelEl = document.getElementById('leftColLabel');
  const titleEl = document.getElementById('leftColTitle');
  if (labelEl) labelEl.textContent = 'Runs';
  if (titleEl) titleEl.textContent = '调度执行历史';
  // 返回历史时立即刷新一次，避免错过最新 run
  fetchScheduleRuns();
}

function switchLeftViewToSession(scheduleId) {
  if (!scheduleId) return;
  currentLeftView = 'session';
  currentSessionScheduleId = scheduleId;
  const viewHistory = document.getElementById('leftColViewHistory');
  const viewSession = document.getElementById('leftColViewSession');
  if (viewHistory) viewHistory.hidden = true;
  if (viewSession) viewSession.hidden = false;
  // 切换控件
  document.querySelectorAll('.left-col-mode-history').forEach(el => el.hidden = true);
  document.querySelectorAll('.left-col-mode-session').forEach(el => el.hidden = false);
  // 写入会话标题与元信息
  const titleEl = document.getElementById('sessionViewTitle');
  const metaEl = document.getElementById('sessionViewMeta');
  const labelEl = document.getElementById('leftColLabel');
  const colTitleEl = document.getElementById('leftColTitle');
  const sched = _schedulesCache.find(s => s.id === scheduleId);
  const schedName = (sched && sched.name) || scheduleId;
  if (labelEl) labelEl.textContent = 'Session';
  if (colTitleEl) colTitleEl.textContent = '调度会话';
  if (titleEl) titleEl.textContent = schedName;
  if (metaEl) metaEl.textContent = `cron:${scheduleId}`;
  loadScheduleSession(scheduleId);
}

async function loadScheduleSession(scheduleId) {
  const container = document.getElementById('sessionViewMessages');
  if (!container) return;
  container.innerHTML = '<div class="empty-state"><div class="empty-state-text">加载中...</div></div>';
  // cron 会话 session_id 形如 "cron:{schedule_id}"
  const sessionId = `cron:${scheduleId}`;
  try {
    const data = await api(`/sessions/${encodeURIComponent(sessionId)}/messages?limit=200`);
    const messages = data.messages || [];
    _sessionMessagesCache = messages;
    renderScheduleSessionMessages(messages, container);
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><div class="empty-state-text">加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderScheduleSessionMessages(messages, container) {
  if (!container) return;
  if (!messages || !messages.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-text">该调度暂无会话消息</div></div>';
    return;
  }
  // 按 created_at 排序（确保有序）
  const sorted = [...messages].sort((a, b) => {
    const ta = a.created_at || '';
    const tb = b.created_at || '';
    return ta.localeCompare(tb);
  });
  container.innerHTML = sorted.map(m => renderScheduleMessageBubble(m)).join('');
  // 渲染后增强代码块（添加复制按钮）
  if (typeof enhanceCodeBlocks === 'function') {
    container.querySelectorAll('pre').forEach(pre => {
      if (!pre.querySelector('.copy-btn')) {
        const btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.textContent = '复制';
        btn.type = 'button';
        pre.appendChild(btn);
      }
    });
  }
}

function renderScheduleMessageBubble(m) {
  const role = m.role || 'user';
  const content = m.content || '';
  const createdAt = m.created_at || '';
  const isTool = !!m.tool_name;
  const isError = m.is_error === true;
  const toolCallId = m.tool_call_id || '';
  // 角色映射
  const roleLabelMap = { user: '用户', assistant: '助手', tool: '工具', system: '系统' };
  const roleLabel = roleLabelMap[role] || role;
  const bubbleClass = isError ? 'message-bubble is-error' : 'message-bubble';
  // 时间格式化：精确到秒
  const timeStr = createdAt ? escapeHtml(formatTime(createdAt)) : '';
  // 详细参数 chips：错误标记 / tool_call_id
  const metaChips = [];
  if (isError) metaChips.push('<span class="message-meta-chip is-error">错误</span>');
  if (toolCallId) metaChips.push(`<span class="message-meta-chip is-callid" title="tool_call_id">call: ${escapeHtml(toolCallId)}</span>`);
  const metaChipsHtml = metaChips.length ? metaChips.join('') : '';

  if (isTool) {
    // 工具消息：渲染为工具卡片
    const toolName = escapeHtml(m.tool_name || '');
    const contentStr = stringifyValue(content);
    return `<div class="message tool">
      <div class="message-role">
        <span class="role-tag role-tool">工具</span>
        <span class="role-tool-name mono">${toolName}</span>
        ${metaChipsHtml}
        ${timeStr ? `<span class="message-time text-muted text-sm">${timeStr}</span>` : ''}
      </div>
      <div class="${bubbleClass} markdown-body">
        <div class="json-highlight">${highlightJSON(contentStr)}</div>
      </div>
    </div>`;
  }
  // 用户/助手消息：渲染为气泡 + markdown
  const roleClass = role === 'user' ? 'role-user' : (role === 'assistant' ? 'role-assistant' : 'role-system');
  return `<div class="message ${role}">
    <div class="message-role">
      <span class="role-tag ${roleClass}">${escapeHtml(roleLabel)}</span>
      ${metaChipsHtml}
      ${timeStr ? `<span class="message-time text-muted text-sm">${timeStr}</span>` : ''}
    </div>
    <div class="${bubbleClass} markdown-body">${renderMarkdown(content)}</div>
  </div>`;
}

// ========== UXP 空状态引导已废弃（移除） ==========
// 旧 updatePendingEmpty / updateSchedulesEmpty / setupEmptyStateActions 已删除，
// 改用 updateManageBadges() 仅更新右上角 badge 数字。

/** 触发新建调度弹窗（复用 btnNewScheduleTab 的逻辑：清空表单 + 重置 workflow + 打开 modal） */
function openScheduleModal() {
  const schedName = document.getElementById('schedName');
  const schedCron = document.getElementById('schedCron');
  const schedTask = document.getElementById('schedTask');
  if (schedName) schedName.value = '';
  if (schedCron) schedCron.value = '';
  if (schedTask) schedTask.value = '';
  const schedEnabled = document.getElementById('schedEnabled');
  if (schedEnabled) schedEnabled.value = 'true';
  if (typeof resetWorkflowConfig === 'function') resetWorkflowConfig();
  if (typeof updateCreateScheduleBtnState === 'function') updateCreateScheduleBtnState();
  openModal('scheduleModal');
}

/** 打开新建调度弹窗并预填 workflow spec（来自 /workflow 的"保存为调度"或 chat 卡片的"在编辑器打开"） */
function openScheduleModalWithWorkflow(spec) {
  // 1. 调用现有 openScheduleModal 打开弹窗（清空表单 + resetWorkflowConfig + openModal）
  openScheduleModal();

  // 2. 切换 workflow 模式为"多步"（若 spec 含 steps）
  if (spec && Array.isArray(spec.steps) && spec.steps.length > 0) {
    // 切换 radio 到 multi
    const multiRadio = document.querySelector('input[name="schedWfMode"][value="multi"]');
    if (multiRadio) multiRadio.checked = true;
    // 显隐对应区域
    const simpleEl = document.querySelector('.sched-wf-config-simple');
    const multiEl = document.querySelector('.sched-wf-config-multi');
    const emptyHint = document.querySelector('.sched-wf-empty-hint');
    if (simpleEl) simpleEl.hidden = true;
    if (multiEl) multiEl.hidden = false;
    if (emptyHint) emptyHint.hidden = true;
    if (typeof updateWorkflowStepBadge === 'function') updateWorkflowStepBadge();

    // 3. 填充 workflow name
    if (spec.name) {
      const wfName = document.getElementById('schedWfName');
      if (wfName) wfName.value = spec.name;
    }

    // 4. 遍历 spec.steps，调用 addWorkflowStep 添加每个 step 并填充字段
    spec.steps.forEach((step) => {
      if (typeof addWorkflowStep !== 'function') return;
      addWorkflowStep();
      const stepsEl = document.getElementById('schedWfSteps');
      if (!stepsEl) return;
      const allSteps = stepsEl.querySelectorAll('.sched-wf-step');
      const lastStep = allSteps[allSteps.length - 1];
      if (!lastStep) return;
      const idInput = lastStep.querySelector('.sched-wf-step-id-input');
      const nameInput = lastStep.querySelector('.sched-wf-step-name-input');
      const typeSelect = lastStep.querySelector('.sched-wf-step-type-select');
      const configArea = lastStep.querySelector('.sched-wf-step-config-area');
      const depsInput = lastStep.querySelector('.sched-wf-step-deps-input');
      const onFailureSelect = lastStep.querySelector('.sched-wf-step-onfailure-select');
      if (idInput) idInput.value = step.id || '';
      if (nameInput) nameInput.value = step.name || '';
      if (typeSelect) typeSelect.value = step.type || 'llm';
      if (configArea) configArea.value = JSON.stringify(step.config || {}, null, 2);
      if (depsInput) depsInput.value = (step.depends_on || []).join(', ');
      if (onFailureSelect && step.on_failure) {
        const action = typeof step.on_failure === 'string' ? step.on_failure : (step.on_failure.action || 'abort');
        onFailureSelect.value = action;
      }
    });
  }

  // 5. 可选：用 spec.name 填充 schedule 名称
  if (spec && spec.name) {
    const schedName = document.getElementById('schedName');
    if (schedName && !schedName.value) schedName.value = spec.name;
  }
}

/** 绑定 PENDING / SCHEDULES 空态引导按钮的 click 事件 */
function setupEmptyStateActions() {
  const pendingAction = document.getElementById('pendingEmptyAction');
  if (pendingAction) pendingAction.addEventListener('click', openScheduleModal);
  const schedulesAction = document.getElementById('schedulesEmptyAction');
  if (schedulesAction) schedulesAction.addEventListener('click', openScheduleModal);
}

// ========== 调度执行强化（7.1）：立即执行 / 配置编辑器 ==========

// 立即触发调度执行（调用 POST /cron_tools/schedules/{id}/run）
async function triggerRerun(scheduleId) {
  if (!scheduleId) return;
  if (!confirm(`确认立即触发调度 ${scheduleId} 执行？`)) return;
  try {
    const resp = await api(`/cron_tools/schedules/${encodeURIComponent(scheduleId)}/run`, { method: 'POST' });
    if (resp && resp.status === 'triggered') {
      showToast('已触发执行');
      // 3s 后刷新执行历史
      setTimeout(() => fetchScheduleRuns(), 3000);
    } else {
      showToast('触发失败：' + (resp && resp.error ? resp.error : '未知错误'), 'error');
    }
  } catch (e) {
    showToast('触发失败：' + e.message, 'error');
  }
}

// 当前正在编辑的调度 ID（配置编辑器用）
let _configEditingScheduleId = null;

// 打开配置编辑器
function openConfigEditor(scheduleId) {
  if (!scheduleId) return;
  _configEditingScheduleId = scheduleId;
  // 从缓存中读取当前调度项
  const s = (_schedulesCache || []).find(x => x.id === scheduleId);
  const enabledSel = document.getElementById('cfgEnabled');
  const catchUpSel = document.getElementById('cfgCatchUpPolicy');
  const onFailureSel = document.getElementById('cfgOnFailureAction');
  if (s) {
    if (enabledSel) enabledSel.value = s.enabled ? 'true' : 'false';
    // catch_up_policy / on_failure 暂未持久化到 Schedule，默认 skip
    if (catchUpSel) catchUpSel.value = s.catch_up_policy || 'skip';
    if (onFailureSel) onFailureSel.value = (s.workflow && s.workflow.on_failure && s.workflow.on_failure.action) || 'skip';
  }
  const modal = document.getElementById('configEditorModal');
  if (modal) {
    modal.hidden = false;
    modal.classList.add('is-open');
  }
}

// 关闭配置编辑器
function closeConfigEditor() {
  const modal = document.getElementById('configEditorModal');
  if (modal) {
    modal.hidden = true;
    modal.classList.remove('is-open');
  }
  _configEditingScheduleId = null;
}

// 保存配置（调用 PUT /schedules/{id}）
async function saveScheduleConfig() {
  if (!_configEditingScheduleId) return;
  const scheduleId = _configEditingScheduleId;
  const enabledSel = document.getElementById('cfgEnabled');
  const catchUpSel = document.getElementById('cfgCatchUpPolicy');
  const onFailureSel = document.getElementById('cfgOnFailureAction');
  const payload = {};
  if (enabledSel) payload.enabled = enabledSel.value === 'true';
  // catch_up_policy / on_failure.action 暂通过 workflow 字段传递（后端需扩展支持）
  // 当前仅保存 enabled，其他字段保留为前端状态，待后端 schema 扩展后启用
  try {
    const resp = await api(`/schedules/${encodeURIComponent(scheduleId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (resp && resp.status === 'ok') {
      showToast(resp.needs_restart ? '已保存（需重启生效）' : '已保存');
      closeConfigEditor();
      fetchSchedules();  // 刷新列表
    } else {
      showToast('保存失败：' + (resp && resp.detail ? resp.detail : '未知错误'), 'error');
    }
  } catch (e) {
    showToast('保存失败：' + e.message, 'error');
  }
}

// ========== init：页面入口 ==========
function init() {
  // 1. 初始化数据
  refreshAll();
  updateManageBadges();

  // 1.5 URL 参数处理：?proposal_id=xxx 自动展开 / ?new_workflow[_key]=xxx 打开新建弹窗预填
  const _urlParams = new URLSearchParams(window.location.search);
  const _pendingProposalIdParam = _urlParams.get('proposal_id');
  const _newWorkflowB64 = _urlParams.get('new_workflow');
  const _newWorkflowKey = _urlParams.get('new_workflow_key');
  if (_pendingProposalIdParam) {
    // 等待 fetchProposals() 渲染后自动展开（renderProposals 末尾检查 window._pendingProposalId）
    window._pendingProposalId = _pendingProposalIdParam;
  } else if (_newWorkflowKey || _newWorkflowB64) {
    // 解析 workflow spec（localStorage 降级模式或 base64 模式），打开新建调度弹窗并预填
    setTimeout(() => {
      try {
        let json;
        if (_newWorkflowKey) {
          json = localStorage.getItem(_newWorkflowKey);
          if (json) localStorage.removeItem(_newWorkflowKey);
          if (!json) throw new Error('导入数据已过期或不存在');
        } else {
          json = decodeURIComponent(escape(atob(_newWorkflowB64)));
        }
        const spec = JSON.parse(json);
        openScheduleModalWithWorkflow(spec);
      } catch (e) {
        showToast(`workflow 导入失败: ${e.message}`, 'error');
      }
    }, 100);
  }

  // 2. refresh-picker 绑定
  const refreshIntervalSel = document.getElementById('refreshInterval');
  if (refreshIntervalSel) {
    refreshIntervalSel.addEventListener('change', (e) => {
      _refreshInterval = parseInt(e.target.value, 10) * 1000;
      if (_refreshInterval > 0) {
        startTimer();
        showToast(`自动刷新 ${_refreshInterval / 1000}s`, 'success');
      } else {
        stopTimer();
        showToast('自动刷新已关闭');
      }
    });
  }
  const btnRefresh = document.getElementById('btnRefresh');
  if (btnRefresh) {
    btnRefresh.addEventListener('click', () => {
      refreshAll();
      showToast('刷新中...');
    });
  }
  startTimer();

  // 3. 左列 section refresh 按钮绑定
  const btnRefreshScheduleRuns = document.getElementById('btnRefreshScheduleRuns');
  if (btnRefreshScheduleRuns) btnRefreshScheduleRuns.addEventListener('click', fetchScheduleRuns);

  // 3.5. 右列 Manage Tab 切换
  document.querySelectorAll('.sched-tab[data-manage-tab]').forEach(tab => {
    tab.addEventListener('click', () => switchManageTab(tab.dataset.manageTab));
  });
  // 当前 Tab 刷新按钮
  const btnRefreshActive = document.getElementById('btnRefreshActive');
  if (btnRefreshActive) btnRefreshActive.addEventListener('click', refreshActiveTab);

  // 3.6. Drawer 关闭
  const drawerClose = document.getElementById('drawerClose');
  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  const drawerOverlay = document.getElementById('drawerOverlay');
  if (drawerOverlay) {
    drawerOverlay.addEventListener('click', (e) => {
      if (e.target === drawerOverlay) closeDrawer();
    });
  }

  // 3.7. 配置编辑器绑定（7.1）
  const cfgSaveBtn = document.getElementById('cfgSaveBtn');
  if (cfgSaveBtn) cfgSaveBtn.addEventListener('click', saveScheduleConfig);
  const cfgModal = document.getElementById('configEditorModal');
  if (cfgModal) {
    cfgModal.addEventListener('click', (e) => {
      if (e.target === cfgModal) closeConfigEditor();
    });
  }
  // Drawer 次级 Tab 切换
  document.querySelectorAll('.drawer-tab').forEach(tab => {
    tab.addEventListener('click', () => switchDrawerSubTab(tab.dataset.drawerTab));
  });

  // runsLimit 变更时重新加载
  const runsLimitSel = document.getElementById('runsLimit');
  if (runsLimitSel) {
    runsLimitSel.addEventListener('change', () => { _lastScheduleRunId = null; fetchScheduleRuns(); });
  }

  // 4. modal-overlay 遮罩点击关闭
  document.querySelectorAll('.modal-overlay').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target === el) el.classList.remove('show');
    });
  });

  // 5. 表单事件绑定
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
      // 重置 workflow 配置
      resetWorkflowConfig();
      updateCreateScheduleBtnState();
      // 触发 cron 预览刷新
      const cronNext = document.getElementById('cronNextPreview');
      if (cronNext && typeof previewCronNext === 'function' && schedCron) {
        cronNext.textContent = previewCronNext(schedCron.value, 5);
      }
      openModal('scheduleModal');
    });
  }

  // 6. Run 卡片点击委托（展开/折叠 + 调度名跳转到会话视图）
  const runsList = document.getElementById('scheduleRunsList');
  if (runsList) {
    runsList.addEventListener('click', (e) => {
      // 6a. 调度名点击：跳转到单调度会话视图（左列内切换）
      const schedNameEl = e.target.closest('.schedule-run-sched-name[data-schedule-id]');
      if (schedNameEl) {
        e.stopPropagation();
        const schedId = schedNameEl.dataset.scheduleId;
        if (schedId) switchLeftViewToSession(schedId);
        return;
      }
      // 6b. 默认：展开/折叠 run 详情
      const header = e.target.closest('.schedule-run-header');
      if (!header) return;
      const runItem = header.closest('.schedule-run-item');
      if (runItem) toggleRunDetail(runItem);
    });
  }

  // 7. ESC 关闭 drawer / modal（drawer 优先）
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (_drawerState) {
        closeDrawer();
      } else {
        document.querySelectorAll('.modal-overlay.show').forEach(el => el.classList.remove('show'));
      }
    }
  });

  // 8. 页面可见性变化时暂停/恢复轮询（仅历史视图自动刷新；
  //    会话视图不自动轮询，避免覆盖用户阅读位置）
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopTimer();
    } else if (_refreshInterval > 0 && currentLeftView === 'history') {
      refreshAll();
      startTimer();
    }
  });

  // 9. 左列会话视图按钮：返回历史 / 刷新当前会话
  const btnBackToHistory = document.getElementById('btnBackToHistory');
  if (btnBackToHistory) {
    btnBackToHistory.addEventListener('click', () => switchLeftViewToHistory());
  }
  const btnRefreshSession = document.getElementById('btnRefreshSession');
  if (btnRefreshSession) {
    btnRefreshSession.addEventListener('click', () => {
      if (currentLeftView === 'session' && currentSessionScheduleId) {
        loadScheduleSession(currentSessionScheduleId);
        showToast('会话已刷新', 'success');
      }
    });
  }

  // 10. 切换会话按钮 + 下拉选择器
  initSessionPicker();

  // 11. 新建调度弹窗: cron 实时预览 + 模板 chips + workflow 模式 + step 编辑器
  initScheduleModalNew();
  initScheduleModal();
}

/** 新建调度弹窗补充初始化（cron 实时预览 + workflow 模板 chips 渲染） */
function initScheduleModalNew() {
  // 1) cron 输入实时预览
  const schedCron = document.getElementById('schedCron');
  const cronNext = document.getElementById('cronNextPreview');
  if (schedCron && cronNext) {
    const refreshPreview = () => {
      const fn = (window.HermesUtils && window.HermesUtils.previewCronNext)
        || (typeof previewCronNext === 'function' ? previewCronNext : null);
      cronNext.textContent = fn ? fn(schedCron.value, 5) : '—';
    };
    schedCron.addEventListener('input', refreshPreview);
    refreshPreview();
  }

  // 2) workflow 模板 chips 渲染
  loadWorkflowPresetsToChips();
}

/** 从 window.WORKFLOW_PRESETS 渲染 chips 到 #wfTemplateChips */
function loadWorkflowPresetsToChips() {
  const container = document.getElementById('wfTemplateChips');
  if (!container) return;
  const presets = window.WORKFLOW_PRESETS || [];
  if (!presets.length) {
    container.innerHTML = '<span class="text-muted text-sm">暂无模板</span>';
    return;
  }
  container.innerHTML = presets.map(p => {
    const diff = p.difficulty || 'beginner';
    return `<button type="button" class="wf-template-chip"
      data-preset-id="${escapeHtml(p.id)}" title="${escapeHtml(p.description || '')}">
      ${escapeHtml(p.name)}
      <span class="wf-template-chip-tag ${diff}">${escapeHtml(diff)}</span>
    </button>`;
  }).join('');
  // 绑定点击事件：调用 applyWorkflowPreset
  container.querySelectorAll('.wf-template-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.presetId;
      const preset = presets.find(p => p.id === id);
      if (preset) applyWorkflowPreset(preset);
    });
  });
}

/** 把预设的 steps 灌入到新建调度的多步模式编辑区 */
function applyWorkflowPreset(preset) {
  if (!preset || !preset.spec) return;
  // 1) 切到 multi 模式
  const multiRadio = document.querySelector('input[name="schedWfMode"][value="multi"]');
  if (multiRadio) {
    multiRadio.checked = true;
    multiRadio.dispatchEvent(new Event('change'));
  }
  // 2) 填 wf 名称
  const wfNameInput = document.getElementById('schedWfName');
  if (wfNameInput && preset.spec.name) wfNameInput.value = preset.spec.name;
  // 3) 清空现有 step 并添加 preset 的所有 step
  const stepsEl = document.getElementById('schedWfSteps');
  const steps = Array.isArray(preset.spec.steps) ? preset.spec.steps : [];
  if (stepsEl) {
    stepsEl.innerHTML = '';
    _wfStepCounter = 0;
    steps.forEach(step => {
      addWorkflowStep();
      const allSteps = stepsEl.querySelectorAll('.sched-wf-step');
      const lastStep = allSteps[allSteps.length - 1];
      if (!lastStep) return;
      const idInput = lastStep.querySelector('.sched-wf-step-id-input');
      const nameInput = lastStep.querySelector('.sched-wf-step-name-input');
      const typeSelect = lastStep.querySelector('.sched-wf-step-type-select');
      const configArea = lastStep.querySelector('.sched-wf-step-config-area');
      const depsInput = lastStep.querySelector('.sched-wf-step-deps-input');
      const onFailureSelect = lastStep.querySelector('.sched-wf-step-onfailure-select');
      if (idInput) idInput.value = step.id || '';
      if (nameInput) nameInput.value = step.name || '';
      if (typeSelect && step.type) {
        typeSelect.value = step.type;
        typeSelect.dispatchEvent(new Event('change'));
      }
      if (configArea) configArea.value = JSON.stringify(step.config || {}, null, 2);
      if (depsInput) depsInput.value = (step.depends_on || []).join(', ');
      if (onFailureSelect) {
        const action = (step.on_failure && step.on_failure.action) || 'abort';
        onFailureSelect.value = action;
      }
    });
    updateWorkflowStepBadge();
  }
  showToast(`已加载模板：${preset.name}`, 'success');
}

// ============================================================
// 切换会话选择器：拉取 cron 会话列表，点击后切换左列到该调度会话
// ============================================================

let _cronSessionsCache = [];  // 缓存 cron 会话列表，供搜索过滤使用

function initSessionPicker() {
  const btn = document.getElementById('btnSwitchSession');
  const dropdown = document.getElementById('sessionPickerDropdown');
  const btnClose = document.getElementById('btnCloseSessionPicker');
  const searchInput = document.getElementById('sessionPickerSearch');
  if (!btn || !dropdown) return;

  // 切换下拉显隐
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const isHidden = dropdown.hasAttribute('hidden');
    if (isHidden) {
      await openSessionPicker();
    } else {
      closeSessionPicker();
    }
  });

  // 关闭按钮
  if (btnClose) {
    btnClose.addEventListener('click', (e) => {
      e.stopPropagation();
      closeSessionPicker();
    });
  }

  // 搜索过滤
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      const q = searchInput.value.trim().toLowerCase();
      renderSessionPickerList(filterCronSessions(_cronSessionsCache, q));
    });
  }

  // 点击下拉项委托
  const listEl = document.getElementById('sessionPickerList');
  if (listEl) {
    listEl.addEventListener('click', (e) => {
      const item = e.target.closest('.session-picker-item[data-schedule-id]');
      if (!item) return;
      const schedId = item.dataset.scheduleId;
      if (schedId) {
        closeSessionPicker();
        switchLeftViewToSession(schedId);
      }
    });
  }

  // 点击外部关闭下拉
  document.addEventListener('click', (e) => {
    if (dropdown.hasAttribute('hidden')) return;
    if (!e.target.closest('.session-picker-wrap')) {
      closeSessionPicker();
    }
  });

  // ESC 关闭
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !dropdown.hasAttribute('hidden')) {
      closeSessionPicker();
    }
  });
}

async function openSessionPicker() {
  const dropdown = document.getElementById('sessionPickerDropdown');
  const btn = document.getElementById('btnSwitchSession');
  const listEl = document.getElementById('sessionPickerList');
  if (!dropdown || !listEl) return;
  dropdown.hidden = false;
  if (btn) btn.setAttribute('aria-expanded', 'true');
  listEl.innerHTML = '<div class="session-picker-empty">加载中...</div>';
  // 清空搜索框
  const searchInput = document.getElementById('sessionPickerSearch');
  if (searchInput) searchInput.value = '';
  try {
    const data = await api('/sessions?cron_only=true');
    const sessions = data.sessions || [];
    // 按 updated_at 降序
    sessions.sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));
    _cronSessionsCache = sessions;
    renderSessionPickerList(sessions);
  } catch (e) {
    listEl.innerHTML = `<div class="session-picker-empty">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function closeSessionPicker() {
  const dropdown = document.getElementById('sessionPickerDropdown');
  const btn = document.getElementById('btnSwitchSession');
  if (dropdown) dropdown.hidden = true;
  if (btn) btn.setAttribute('aria-expanded', 'false');
}

function filterCronSessions(sessions, q) {
  if (!q) return sessions;
  return sessions.filter(s => {
    const title = (s.title || '').toLowerCase();
    const id = (s.id || '').toLowerCase();
    return title.includes(q) || id.includes(q);
  });
}

function renderSessionPickerList(sessions) {
  const listEl = document.getElementById('sessionPickerList');
  if (!listEl) return;
  if (!sessions || !sessions.length) {
    listEl.innerHTML = '<div class="session-picker-empty">暂无调度会话</div>';
    return;
  }
  listEl.innerHTML = sessions.map(s => {
    // session_id 形如 "cron:{schedule_id}"
    const sid = s.id || '';
    const schedId = sid.startsWith('cron:') ? sid.slice(5) : sid;
    const title = (s.title && s.title.trim()) ? s.title.trim() : schedId;
    const isActive = (schedId === currentSessionScheduleId && currentLeftView === 'session');
    const updated = s.updated_at ? formatTime(s.updated_at) : '';
    return `<div class="session-picker-item${isActive ? ' is-active' : ''}" data-schedule-id="${escapeHtml(schedId)}" title="${escapeHtml(title)}">
      <div class="session-picker-item-row">
        <span class="session-picker-item-name">${escapeHtml(title)}</span>
        <span class="session-picker-item-id mono">${escapeHtml(schedId.slice(0, 8))}</span>
      </div>
      <div class="session-picker-item-meta">${updated ? `最近: ${escapeHtml(updated)}` : '无消息记录'}</div>
    </div>`;
  }).join('');
}

// ============================================================
// 新建调度弹窗：Tab 切换 / Cron 预设 / Workflow 模式 / Step 编辑器
// ============================================================

let _wfStepCounter = 0;  // step 索引计数器（仅用于前端显示）

function initScheduleModal() {
  // Tab 切换
  document.querySelectorAll('.sched-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.schedTab;
      document.querySelectorAll('.sched-tab').forEach(t => t.classList.toggle('active', t === tab));
      document.querySelectorAll('.sched-tab-pane').forEach(p => {
        p.classList.toggle('active', p.dataset.schedPane === target);
      });
    });
  });

  // Cron 预设
  document.querySelectorAll('.cron-preset').forEach(btn => {
    btn.addEventListener('click', () => {
      const cronInput = document.getElementById('schedCron');
      if (cronInput) {
        cronInput.value = btn.dataset.cron;
        updateCreateScheduleBtnState();
      }
    });
  });

  // Workflow 模式切换
  document.querySelectorAll('input[name="schedWfMode"]').forEach(radio => {
    radio.addEventListener('change', () => {
      const mode = radio.value;
      const simpleEl = document.querySelector('.sched-wf-config-simple');
      const multiEl = document.querySelector('.sched-wf-config-multi');
      const emptyHint = document.querySelector('.sched-wf-empty-hint');
      if (simpleEl) simpleEl.hidden = (mode !== 'simple');
      if (multiEl) multiEl.hidden = (mode !== 'multi');
      if (emptyHint) emptyHint.hidden = (mode !== 'none');
      updateWorkflowStepBadge();
    });
  });

  // 添加 Step 按钮
  const btnAddStep = document.getElementById('btnAddStep');
  if (btnAddStep) btnAddStep.addEventListener('click', addWorkflowStep);

  // Step 列表事件委托（删除/上移/下移）
  const stepsEl = document.getElementById('schedWfSteps');
  if (stepsEl) {
    stepsEl.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-step-action]');
      if (!btn) return;
      const action = btn.dataset.stepAction;
      const stepEl = btn.closest('.sched-wf-step');
      if (!stepEl) return;
      if (action === 'remove') {
        stepEl.remove();
        reindexWorkflowSteps();
        updateWorkflowStepBadge();
      } else if (action === 'up') {
        const prev = stepEl.previousElementSibling;
        if (prev && prev.classList.contains('sched-wf-step')) {
          stepsEl.insertBefore(stepEl, prev);
          reindexWorkflowSteps();
        }
      } else if (action === 'down') {
        const next = stepEl.nextElementSibling;
        if (next && next.classList.contains('sched-wf-step')) {
          stepsEl.insertBefore(next, stepEl);
          reindexWorkflowSteps();
        }
      }
    });
  }
}

function addWorkflowStep() {
  const stepsEl = document.getElementById('schedWfSteps');
  if (!stepsEl) return;
  // 移除空状态提示
  const emptyEl = stepsEl.querySelector('.sched-wf-steps-empty');
  if (emptyEl) emptyEl.remove();
  _wfStepCounter++;
  const stepId = `step${_wfStepCounter}`;
  const stepHtml = `
    <div class="sched-wf-step" data-step-uid="${_wfStepCounter}">
      <div class="sched-wf-step-header">
        <span class="sched-wf-step-index">${_wfStepCounter}</span>
        <input type="text" class="sched-wf-step-id-input" value="${stepId}" placeholder="step id" title="step id（workflow 内唯一）">
        <input type="text" class="sched-wf-step-name-input" value="" placeholder="可读名称（可选）">
        <select class="sched-wf-step-type-select" title="step 类型">
          <option value="llm" selected>llm · 单轮 LLM</option>
          <option value="react">react · 多轮工具循环</option>
          <option value="tool">tool · 直接调工具</option>
          <option value="deterministic">deterministic · 内置模板</option>
          <option value="subworkflow">subworkflow · 子流程</option>
        </select>
        <span class="sched-wf-step-actions">
          <button type="button" class="btn btn-ghost btn-sm" data-step-action="up" title="上移">↑</button>
          <button type="button" class="btn btn-ghost btn-sm" data-step-action="down" title="下移">↓</button>
          <button type="button" class="btn btn-mini btn-danger" data-step-action="remove" title="删除">✕</button>
        </span>
      </div>
      <div class="sched-wf-step-body">
        <div class="sched-wf-step-field">
          <span class="sched-wf-step-field-label">Config (JSON)</span>
          <textarea class="sched-wf-step-config-area" placeholder='{"prompt": "...", "system": "..."}'></textarea>
        </div>
        <div class="sched-wf-step-row-2col">
          <div class="sched-wf-step-field">
            <span class="sched-wf-step-field-label">depends_on (逗号分隔 step id)</span>
            <input type="text" class="sched-wf-step-deps-input" placeholder="step1, step2">
          </div>
          <div class="sched-wf-step-field">
            <span class="sched-wf-step-field-label">on_failure</span>
            <select class="sched-wf-step-onfailure-select">
              <option value="abort" selected>abort · 终止</option>
              <option value="retry">retry · 重试</option>
              <option value="skip">skip · 跳过</option>
              <option value="fallback">fallback · 兜底</option>
            </select>
          </div>
        </div>
      </div>
    </div>
  `;
  stepsEl.insertAdjacentHTML('beforeend', stepHtml);
  updateWorkflowStepBadge();
}

function reindexWorkflowSteps() {
  const stepsEl = document.getElementById('schedWfSteps');
  if (!stepsEl) return;
  const steps = stepsEl.querySelectorAll('.sched-wf-step');
  steps.forEach((s, i) => {
    const idxEl = s.querySelector('.sched-wf-step-index');
    if (idxEl) idxEl.textContent = String(i + 1);
  });
}

function updateWorkflowStepBadge() {
  const badge = document.getElementById('workflowStepBadge');
  if (!badge) return;
  const checkedMode = document.querySelector('input[name="schedWfMode"]:checked');
  const mode = checkedMode ? checkedMode.value : 'none';
  if (mode !== 'multi') {
    badge.hidden = true;
    return;
  }
  const stepsEl = document.getElementById('schedWfSteps');
  const count = stepsEl ? stepsEl.querySelectorAll('.sched-wf-step').length : 0;
  badge.textContent = String(count);
  badge.hidden = count === 0;
}

/**
 * 序列化 workflow 配置。
 * 返回值：
 *   - null：未配置 workflow（mode=none）
 *   - false：校验失败（已通过 toast 提示）
 *   - object：合法的 workflow dict
 */
function serializeWorkflowConfig() {
  const checkedMode = document.querySelector('input[name="schedWfMode"]:checked');
  const mode = checkedMode ? checkedMode.value : 'none';
  if (mode === 'none') return null;

  if (mode === 'simple') {
    const template = (document.getElementById('schedWfTemplate') || {}).value || '';
    if (!template) {
      showToast('请选择 workflow 模板', 'error');
      return false;
    }
    const cfgText = (document.getElementById('schedWfTemplateConfig') || {}).value || '';
    let templateConfig = {};
    if (cfgText.trim()) {
      try {
        templateConfig = JSON.parse(cfgText);
      } catch (e) {
        showToast('模板配置 JSON 解析失败: ' + e.message, 'error');
        return false;
      }
    }
    return { template, template_config: templateConfig };
  }

  if (mode === 'multi') {
    const stepsEl = document.getElementById('schedWfSteps');
    if (!stepsEl) return null;
    const stepEls = stepsEl.querySelectorAll('.sched-wf-step');
    if (stepEls.length === 0) {
      showToast('请至少添加一个 step', 'error');
      return false;
    }
    const steps = [];
    const stepIds = new Set();
    for (const s of stepEls) {
      const id = (s.querySelector('.sched-wf-step-id-input') || {}).value || '';
      const name = (s.querySelector('.sched-wf-step-name-input') || {}).value || '';
      const type = (s.querySelector('.sched-wf-step-type-select') || {}).value || 'llm';
      const cfgText = (s.querySelector('.sched-wf-step-config-area') || {}).value || '';
      const depsText = (s.querySelector('.sched-wf-step-deps-input') || {}).value || '';
      const onFail = (s.querySelector('.sched-wf-step-onfailure-select') || {}).value || 'abort';

      // 清理错误状态
      s.classList.remove('is-error');
      const errEl = s.querySelector('.sched-wf-step-error');
      if (errEl) errEl.remove();

      if (!id) {
        showStepError(s, 'step id 不能为空');
        showToast('存在 step id 为空', 'error');
        return false;
      }
      if (stepIds.has(id)) {
        showStepError(s, `step id 重复: ${id}`);
        showToast(`step id 重复: ${id}`, 'error');
        return false;
      }
      stepIds.add(id);

      let config = {};
      if (cfgText.trim()) {
        try {
          config = JSON.parse(cfgText);
        } catch (e) {
          showStepError(s, 'config JSON 解析失败: ' + e.message);
          showToast(`step [${id}] config JSON 解析失败`, 'error');
          return false;
        }
      }

      const dependsOn = depsText
        .split(',')
        .map(x => x.trim())
        .filter(x => x);

      steps.push({
        id,
        name,
        type,
        config,
        depends_on: dependsOn,
        on_failure: { action: onFail },
      });
    }

    // 校验 depends_on 引用合法性
    for (const step of steps) {
      for (const dep of step.depends_on) {
        if (!stepIds.has(dep)) {
          showToast(`step [${step.id}] depends_on 引用了不存在的 step: ${dep}`, 'error');
          return false;
        }
      }
    }

    const wfName = (document.getElementById('schedWfName') || {}).value || '';
    const result = { steps };
    if (wfName) result.name = wfName;
    return result;
  }

  return null;
}

function showStepError(stepEl, msg) {
  stepEl.classList.add('is-error');
  let errEl = stepEl.querySelector('.sched-wf-step-error');
  if (!errEl) {
    errEl = document.createElement('div');
    errEl.className = 'sched-wf-step-error';
    stepEl.querySelector('.sched-wf-step-body').appendChild(errEl);
  }
  errEl.textContent = msg;
}

function resetWorkflowConfig() {
  // 重置模式为 none
  const noneRadio = document.querySelector('input[name="schedWfMode"][value="none"]');
  if (noneRadio) noneRadio.checked = true;
  // 清空简易模式
  const tmpl = document.getElementById('schedWfTemplate');
  if (tmpl) tmpl.value = '';
  const tmplCfg = document.getElementById('schedWfTemplateConfig');
  if (tmplCfg) tmplCfg.value = '';
  // 清空多步模式
  const stepsEl = document.getElementById('schedWfSteps');
  if (stepsEl) {
    stepsEl.innerHTML = '<div class="sched-wf-steps-empty text-muted">点击「+ 添加 Step」开始编排 workflow</div>';
  }
  const wfName = document.getElementById('schedWfName');
  if (wfName) wfName.value = '';
  _wfStepCounter = 0;
  // 隐藏子配置区
  document.querySelectorAll('.sched-wf-config').forEach(el => el.hidden = true);
  const emptyHint = document.querySelector('.sched-wf-empty-hint');
  if (emptyHint) emptyHint.hidden = false;
  updateWorkflowStepBadge();
  // 切回基础信息 Tab
  document.querySelectorAll('.sched-tab').forEach(t => t.classList.toggle('active', t.dataset.schedTab === 'basic'));
  document.querySelectorAll('.sched-tab-pane').forEach(p => p.classList.toggle('active', p.dataset.schedPane === 'basic'));
}

// 启动
document.addEventListener('DOMContentLoaded', init);
