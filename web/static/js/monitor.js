/* ============================================================
   monitor.js — 监控页面逻辑
   轮询 /metrics /health /audit/logs /schedules/runs
   Canvas 原生绘制延迟直方图
   ============================================================ */

(function () {
  'use strict';

  // 直方图 bucket 边界（与后端 metrics.py 对齐）
  const HIST_BOUNDS = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000];
  const HIST_LABELS = ['≤50', '≤100', '≤200', '≤500', '≤1s', '≤2s', '≤5s', '≤10s', '≤30s', '>30s'];

  // 轮询控制
  let refreshInterval = 5000;
  let refreshTimer = null;
  let isVisible = true;
  let isInFlight = false;

  // 缓存上一次 metrics 用于增量展示
  let lastMetrics = null;

  // ----------------------------------------------------------------
  // 工具函数
  // ----------------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  function fmtNum(n) {
    if (n === null || n === undefined) return '—';
    if (typeof n !== 'number') return String(n);
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
  }

  function fmtMs(v) {
    if (v === null || v === undefined) return '—';
    if (v < 1) return v.toFixed(2) + 'ms';
    if (v < 1000) return Math.round(v) + 'ms';
    return (v / 1000).toFixed(2) + 's';
  }

  function fmtPct(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return (v * 100).toFixed(1) + '%';
  }

  function fmtTime(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      const pad = (x) => String(x).padStart(2, '0');
      return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    } catch {
      return iso;
    }
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function truncate(s, n) {
    if (!s) return '';
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  async function fetchJson(url) {
    const resp = await fetch(url, { cache: 'no-store' });
    if (!resp.ok) {
      throw new Error(`${url} → HTTP ${resp.status}`);
    }
    return resp.json();
  }

  function showToast(msg, type = '') {
    const el = $('toast');
    el.textContent = msg;
    el.className = 'toast is-show';
    if (type) el.classList.add(`is-${type}`);
    clearTimeout(el._timer);
    el._timer = setTimeout(() => {
      el.classList.remove('is-show');
    }, 2400);
  }

  // ----------------------------------------------------------------
  // 主题强调色选择器
  // ----------------------------------------------------------------
  function initAccentPicker() {
    const currentAccent = HermesTheme.getAccent();
    document.querySelectorAll('.accent-dot').forEach((dot) => {
      dot.classList.toggle('is-active', dot.dataset.accent === currentAccent);
      dot.addEventListener('click', () => {
        HermesTheme.setAccent(dot.dataset.accent);
        document.querySelectorAll('.accent-dot').forEach((d) => {
          d.classList.toggle('is-active', d === dot);
        });
        // 主题变化后重绘 Canvas（颜色变量改变）
        drawAllHistograms();
      });
    });

    // 主题切换时也重绘
    document.addEventListener('themechange', () => {
      drawAllHistograms();
    });
  }

  // ----------------------------------------------------------------
  // 健康检查渲染
  // ----------------------------------------------------------------
  // Health 检查项分组映射
  const HEALTH_GROUPS = [
    { label: '核心引擎', items: ['orchestrator', 'llm_client', 'react_loop', 'session_logger'] },
    { label: '记忆系统', items: ['chroma_store', 'history_buffer', 'consolidation_engine', 'memory_retriever', 'memory_md_manager', 'context_manager', 'condenser', 'decay'] },
    { label: '安全与审批', items: ['policy_engine', 'approval_manager', 'audit_logger'] },
    { label: '工具与任务', items: ['tool_registry', 'file_registry', 'todo_registry', 'task_manager', 'skill_loader'] },
    { label: '调度系统', items: ['cron_scheduler', 'cron_tool_registry', 'proposal_store'] },
    { label: '基础设施', items: ['mcp_manager', 'metrics_collector', 'disk'] },
  ];

  function renderHealth(data) {
    if (!data || !data.summary) {
      $('healthOk').textContent = '—';
      $('healthWarn').textContent = '—';
      $('healthCrit').textContent = '—';
      $('healthTotal').textContent = '—';
      $('overallStatus').textContent = '无数据';
      $('healthGrid').innerHTML = '<div class="empty-state"><div class="empty-state-text">健康检查未启用</div></div>';
      return;
    }

    const s = data.summary;
    $('healthOk').textContent = s.ok;
    $('healthWarn').textContent = s.warning;
    $('healthCrit').textContent = s.critical;
    $('healthTotal').textContent = s.total;

    const status = data.status || 'unknown';
    const statusEl = $('overallStatus');
    statusEl.textContent = status;
    statusEl.className = 'section-status';
    if (status === 'healthy') statusEl.classList.add('is-healthy');
    else if (status === 'degraded') statusEl.classList.add('is-degraded');
    else if (status === 'unhealthy') statusEl.classList.add('is-unhealthy');

    const checks = data.checks || {};
    const grid = $('healthGrid');
    const entries = Object.entries(checks);
    if (entries.length === 0) {
      grid.innerHTML = '<div class="empty-state"><div class="empty-state-text">无检查项</div></div>';
      return;
    }

    // 按分组渲染
    const matched = new Set();
    let html = '';
    for (const group of HEALTH_GROUPS) {
      const groupCells = [];
      let gOk = 0, gWarn = 0, gCrit = 0;
      for (const name of group.items) {
        if (checks[name]) {
          matched.add(name);
          const info = checks[name];
          const st = info.status || 'ok';
          if (st === 'ok') gOk++;
          else if (st === 'warning') gWarn++;
          else if (st === 'critical') gCrit++;
          const msg = escapeHtml(truncate(info.message || '', 60));
          groupCells.push(`
            <div class="health-cell is-${st}" title="${escapeHtml(info.message || '')}">
              <span class="health-cell-dot"></span>
              <span class="health-cell-name">${escapeHtml(name)}</span>
              <span class="health-cell-msg">${msg}</span>
            </div>
          `);
        }
      }
      if (groupCells.length === 0) continue;
      const badgeClass = gCrit > 0 ? 'is-critical' : (gWarn > 0 ? 'is-warning' : 'is-ok');
      const badgeText = gCrit > 0 ? `${gCrit} critical` : (gWarn > 0 ? `${gWarn} warning` : `${gOk} ok`);
      html += `
        <div class="health-group">
          <div class="health-group-header">
            <span class="health-group-label">${escapeHtml(group.label)}</span>
            <span class="health-group-badge ${badgeClass}">${badgeText}</span>
          </div>
          <div class="health-grid">${groupCells.join('')}</div>
        </div>
      `;
    }
    // 防御性：未匹配的检查项归入"其他"
    const others = entries.filter(([name]) => !matched.has(name));
    if (others.length > 0) {
      const otherCells = others.map(([name, info]) => {
        const st = info.status || 'ok';
        const msg = escapeHtml(truncate(info.message || '', 60));
        return `
          <div class="health-cell is-${st}" title="${escapeHtml(info.message || '')}">
            <span class="health-cell-dot"></span>
            <span class="health-cell-name">${escapeHtml(name)}</span>
            <span class="health-cell-msg">${msg}</span>
          </div>
        `;
      }).join('');
      html += `
        <div class="health-group">
          <div class="health-group-header">
            <span class="health-group-label">其他</span>
            <span class="health-group-badge is-ok">${others.length} 项</span>
          </div>
          <div class="health-grid">${otherCells}</div>
        </div>
      `;
    }
    grid.innerHTML = html;
  }

  // ----------------------------------------------------------------
  // 性能指标卡片渲染
  // ----------------------------------------------------------------
  function renderMetrics(m) {
    if (!m || Object.keys(m).length === 0) {
      ['llmCallsTotal', 'tokensInput', 'tokensOutput', 'cacheHitRate', 'llmAvgLatency',
       'toolCallsTotal', 'toolAvgLatency', 'memoryHitRate'].forEach((id) => {
        $(id).textContent = '—';
      });
      $('llmCallsSub').textContent = '监控未启用';
      return;
    }

    // LLM 调用
    const llmCalls = m.llm_calls_total || 0;
    $('llmCallsTotal').textContent = fmtNum(llmCalls);

    // Token
    const tokIn = m.llm_tokens_input_total || 0;
    const tokOut = m.llm_tokens_output_total || 0;
    const cacheRead = m.llm_cache_read_tokens_total || 0;
    const cacheCreate = m.llm_cache_creation_tokens_total || 0;
    $('tokensInput').textContent = fmtNum(tokIn);
    $('tokensOutput').textContent = fmtNum(tokOut);
    $('tokensInputSub').textContent = `cache_read ${fmtNum(cacheRead)}`;
    $('tokensOutputSub').textContent = `cache_create ${fmtNum(cacheCreate)}`;

    // 缓存命中率
    const cacheRate = tokIn > 0 ? cacheRead / tokIn : null;
    $('cacheHitRate').textContent = fmtPct(cacheRate);
    $('cacheHitSub').textContent = `${fmtNum(cacheRead)} / ${fmtNum(tokIn)}`;

    // LLM 延迟
    const llmH = m.llm_latency_ms || {};
    $('llmAvgLatency').textContent = fmtMs(llmH.avg || 0);
    $('llmLatencySub').textContent = `${fmtMs(llmH.min || 0)} / ${fmtMs(llmH.max || 0)}`;
    $('llmHistCount').textContent = `(${llmH.count || 0})`;

    // 工具调用
    const toolCalls = m.tool_calls_total || {};
    const toolErrors = m.tool_calls_errors_total || {};
    const toolTotal = Object.values(toolCalls).reduce((a, b) => a + b, 0);
    const toolErrTotal = Object.values(toolErrors).reduce((a, b) => a + b, 0);
    $('toolCallsTotal').textContent = fmtNum(toolTotal);
    $('toolCallsSub').textContent = `错误 ${fmtNum(toolErrTotal)}`;

    // 工具延迟
    const toolH = m.tool_latency_ms || {};
    $('toolAvgLatency').textContent = fmtMs(toolH.avg || 0);
    $('toolLatencySub').textContent = `${fmtMs(toolH.min || 0)} / ${fmtMs(toolH.max || 0)}`;
    $('toolHistCount').textContent = `(${toolH.count || 0})`;

    // 记忆命中率
    const memHit = m.memory_retrieval_hits_total || 0;
    const memMiss = m.memory_retrieval_misses_total || 0;
    const memTotal = memHit + memMiss;
    const memRate = memTotal > 0 ? memHit / memTotal : null;
    $('memoryHitRate').textContent = fmtPct(memRate);
    $('memoryHitSub').textContent = `${fmtNum(memHit)} / ${fmtNum(memMiss)}`;

    // 渲染工具明细表
    renderToolTable(toolCalls, toolErrors);

    // 渲染延迟直方图
    drawHistogram('llmHistCanvas', llmH, 'llmHistLegend');
    drawHistogram('toolHistCanvas', toolH, 'toolHistLegend');

    // 渲染终止原因分布
    renderTerminationReasons(m.termination_reasons_total || {});
    // 渲染工具错误分类
    renderToolErrorClasses(m.tool_error_classes_total || {}, toolCalls);
    // 渲染审批统计
    renderApprovalStats(m.approval_decisions_total || {});

    lastMetrics = m;
  }

  function renderToolTable(calls, errors) {
    const body = $('toolTableBody');
    const entries = Object.entries(calls);
    if (entries.length === 0) {
      body.innerHTML = '<tr><td colspan="6" class="empty-state-text">暂无工具调用</td></tr>';
      return;
    }

    const total = entries.reduce((a, [, n]) => a + n, 0);
    const sorted = entries.sort((a, b) => b[1] - a[1]);

    body.innerHTML = sorted.map(([name, count]) => {
      const err = errors[name] || 0;
      const errRate = count > 0 ? err / count : 0;
      const pct = total > 0 ? count / total : 0;

      return `
        <tr>
          <td class="tool-name">${escapeHtml(name)}</td>
          <td class="num">${fmtNum(count)}</td>
          <td class="num ${err > 0 ? 'text-danger' : 'text-muted'}">${fmtNum(err)}</td>
          <td class="num ${errRate > 0.1 ? 'text-danger' : ''}">${fmtPct(errRate)}</td>
          <td class="num">${fmtPct(pct)}</td>
        </tr>
      `;
    }).join('');
  }

  // ----------------------------------------------------------------
  // 终止原因分布渲染
  // ----------------------------------------------------------------
  function renderTerminationReasons(reasons) {
    const body = $('terminationReasonsBody');
    const entries = Object.entries(reasons || {});
    if (entries.length === 0) {
      body.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无数据</div></div>';
      return;
    }
    const total = entries.reduce((a, [, n]) => a + n, 0);
    const labels = {
      normal: '正常结束',
      user_cancel: '用户取消',
      tool_permanent_fail: '工具失败',
      max_loops: '达到上限',
    };
    const sorted = entries.sort((a, b) => b[1] - a[1]);
    body.innerHTML = sorted.map(([reason, count]) => {
      const pct = total > 0 ? count / total : 0;
      const label = labels[reason] || reason;
      const isDanger = reason === 'tool_permanent_fail' || reason === 'user_cancel';
      const barClass = isDanger ? 'term-bar-fill danger' : 'term-bar-fill';
      return `
        <div class="term-row">
          <span class="term-label">${escapeHtml(label)}</span>
          <div class="term-bar">
            <div class="${barClass}" style="width: ${Math.max(2, pct * 100).toFixed(1)}%;"></div>
          </div>
          <span class="term-count">${fmtNum(count)}</span>
          <span class="term-pct">${fmtPct(pct)}</span>
        </div>
      `;
    }).join('');
  }

  // ----------------------------------------------------------------
  // 工具错误分类渲染（17 类按 stage 分组：顶部 3 stage 卡片 + 下方明细表）
  // ----------------------------------------------------------------
  // stage 元数据
  const ERROR_STAGE_META = {
    pre_execution: { label: '触发前拦截', cls: 'info',    desc: 'handler 未执行' },
    execution:     { label: '执行中失败', cls: 'warning', desc: 'handler 已执行后失败' },
    protocol:      { label: '协议层错误', cls: 'danger',  desc: '消息序列/LLM 调用' },
  };

  // 错误类型元数据：17 类 + 2 兼容旧值
  const ERROR_TYPE_META = [
    // pre_execution (7)
    { key: 'param_error',     stage: 'pre_execution', label: '参数错误',     cls: 'info',    desc: 'schema 校验失败' },
    { key: 'tool_not_found',  stage: 'pre_execution', label: '工具未找到',   cls: 'info',    desc: '未注册/已禁用/未加载' },
    { key: 'policy_denied',   stage: 'pre_execution', label: '策略拒绝',     cls: 'info',    desc: 'PolicyEngine deny' },
    { key: 'user_rejected',   stage: 'pre_execution', label: '用户拒绝',     cls: 'info',    desc: 'HIL reject' },
    { key: 'non_stream_hil',  stage: 'pre_execution', label: '非流式拒审批', cls: 'info',    desc: 'run() 自动拒绝 confirm' },
    { key: 'stuck_detected',  stage: 'pre_execution', label: '卡死检测',     cls: 'accent',  desc: '同参数重复调用' },
    { key: 'cancelled',       stage: 'pre_execution', label: '已取消',       cls: 'muted',   desc: 'cancel_event 触发' },
    // execution (8)
    { key: 'not_found',       stage: 'execution', label: '资源不存在', cls: 'warning', desc: '文件/记录不存在' },
    { key: 'permission',      stage: 'execution', label: '权限不足',   cls: 'warning', desc: 'OS 权限拒绝' },
    { key: 'timeout',         stage: 'execution', label: '执行超时',   cls: 'warning', desc: 'subprocess/网络超时' },
    { key: 'transient',       stage: 'execution', label: '临时错误',   cls: 'warning', desc: '5xx/网络抖动，可重试' },
    { key: 'permanent',       stage: 'execution', label: '永久错误',   cls: 'danger',  desc: '4xx/逻辑错误，重试无效' },
    { key: 'anti_crawler',    stage: 'execution', label: '反爬虫',     cls: 'accent',  desc: '403/412 触发风控' },
    { key: 'auth_required',   stage: 'execution', label: '需认证',     cls: 'success', desc: '401 补充认证可恢复' },
    { key: 'internal_error',  stage: 'execution', label: '内部错误',   cls: 'danger',  desc: '兜底异常' },
    // protocol (2)
    { key: 'orphan_tool_result', stage: 'protocol', label: '孤立结果', cls: 'danger', desc: 'tool_use/tool_result 不配对' },
    { key: 'llm_failure',        stage: 'protocol', label: 'LLM 失败', cls: 'danger', desc: 'LLM 调用异常' },
    // 兼容旧值
    { key: 'unknown',  stage: 'protocol',      label: '未知(旧)',  cls: 'muted', desc: '历史数据/ErrorClassifier 兜底' },
    { key: 'success',  stage: 'pre_execution', label: '成功(旧)',  cls: 'muted', desc: '历史数据，新系统不再产生' },
  ];

  // 旧值 → 新 stage 的映射（用于历史数据展示）
  const LEGACY_CLASS_STAGE_FALLBACK = {
    transient: 'execution',
    anti_crawler: 'execution',
    permanent: 'execution',
    auth_required: 'execution',
    unknown: 'protocol',
  };

  // 按 stage 分组的列顺序
  const STAGE_ORDER = ['pre_execution', 'execution', 'protocol'];

  function _stageOf(cls) {
    const meta = ERROR_TYPE_META.find(m => m.key === cls);
    if (meta) return meta.stage;
    return LEGACY_CLASS_STAGE_FALLBACK[cls] || 'protocol';
  }

  function renderToolErrorClasses(classes, toolCalls) {
    const summaryEl = $('toolErrorClassSummary');
    const headEl = $('toolErrorClassesHead');
    const bodyEl = $('toolErrorClassesBody');
    const entries = Object.entries(classes || {});

    // 1. 按 stage 聚合：stage → error_class → total count
    const stageAgg = { pre_execution: {}, execution: {}, protocol: {} };
    let grandTotal = 0;
    entries.forEach(([name, counts]) => {
      Object.entries(counts || {}).forEach(([cls, cnt]) => {
        const stage = _stageOf(cls);
        stageAgg[stage][cls] = (stageAgg[stage][cls] || 0) + cnt;
        grandTotal += cnt;
      });
    });

    // 2. 渲染 3 个 stage 卡片到 summaryEl
    if (grandTotal === 0) {
      summaryEl.innerHTML = '<div class="empty-state-text">暂无错误</div>';
    } else {
      summaryEl.innerHTML = STAGE_ORDER.map(stage => {
        const sMeta = ERROR_STAGE_META[stage];
        const stageClasses = ERROR_TYPE_META.filter(m => m.stage === stage);
        const stageTotal = Object.values(stageAgg[stage]).reduce((a, b) => a + b, 0);
        const chips = stageClasses.map(m => {
          const cnt = stageAgg[stage][m.key] || 0;
          const isEmpty = cnt === 0;
          return `<span class="class-chip ${m.cls} ${isEmpty ? 'empty' : ''}" title="${m.desc}">
            <span class="label">${m.label}</span>
            <span class="count">${fmtNum(cnt)}</span>
          </span>`;
        }).join('');
        return `
          <div class="stage-card ${stage}" title="${sMeta.desc}">
            <div class="stage-card-header">
              <span class="stage-label">${sMeta.label}</span>
              <span class="badge ${sMeta.cls}">${fmtNum(stageTotal)}</span>
            </div>
            <div class="stage-classes">${chips}</div>
          </div>
        `;
      }).join('');
    }

    // 3. 渲染双行表头到 headEl
    const stageCols = STAGE_ORDER.map(stage => {
      const sMeta = ERROR_STAGE_META[stage];
      const cols = ERROR_TYPE_META.filter(m => m.stage === stage).length;
      return `<th colspan="${cols}" class="stage-th ${stage}">${sMeta.label}</th>`;
    }).join('');
    const classThs = STAGE_ORDER.map(stage => {
      return ERROR_TYPE_META.filter(m => m.stage === stage)
        .map(m => `<th class="num" title="${m.desc}">${m.label}</th>`)
        .join('');
    }).join('');
    headEl.innerHTML = `
      <tr>
        <th rowspan="2">工具</th>
        ${stageCols}
        <th rowspan="2">合计</th>
        <th rowspan="2" title="该工具错误总数 ÷ 该工具总调用次数">调用错误率</th>
      </tr>
      <tr>${classThs}</tr>
    `;

    // 4. 渲染明细表
    if (grandTotal === 0) {
      bodyEl.innerHTML = '<tr><td colspan="20" class="empty-state-text">暂无工具错误</td></tr>';
      return;
    }

    // 按错误总数降序
    const withTotals = entries.map(([name, counts]) => {
      const total = Object.values(counts || {}).reduce((a, b) => a + b, 0);
      return [name, counts, total];
    }).filter(([_, __, total]) => total > 0)
      .sort((a, b) => b[2] - a[2]);

    if (withTotals.length === 0) {
      bodyEl.innerHTML = '<tr><td colspan="20" class="empty-state-text">暂无工具错误</td></tr>';
      return;
    }

    bodyEl.innerHTML = withTotals.map(([name, counts, total]) => {
      const calls = (toolCalls && toolCalls[name]) || 0;
      const errRate = calls > 0 ? total / calls : 0;

      // 按 stage 分组 cells
      const cells = STAGE_ORDER.map(stage => {
        return ERROR_TYPE_META.filter(m => m.stage === stage)
          .map(m => {
            const v = counts?.[m.key] || 0;
            return `<td class="num ${v > 0 ? m.cls : ''}">${v > 0 ? fmtNum(v) : '—'}</td>`;
          }).join('');
      }).join('');

      return `
        <tr>
          <td class="tool-name">${escapeHtml(name)}</td>
          ${cells}
          <td class="num text-danger"><strong>${fmtNum(total)}</strong></td>
          <td class="num ${errRate > 0.1 ? 'text-danger' : ''}">${fmtPct(errRate)}</td>
        </tr>
      `;
    }).join('');
  }

  // ----------------------------------------------------------------
  // 审批统计渲染（3 个数字卡片：approve/deny/timeout）
  // ----------------------------------------------------------------
  function renderApprovalStats(decisions) {
    const body = $('approvalStatsBody');
    const entries = Object.entries(decisions || {});
    if (entries.length === 0) {
      body.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无数据</div></div>';
      return;
    }
    const total = entries.reduce((a, [, n]) => a + n, 0);
    const cards = [
      { key: 'approve', label: '批准', cls: 'approval-card approve' },
      { key: 'deny',    label: '拒绝', cls: 'approval-card deny' },
      { key: 'timeout', label: '超时', cls: 'approval-card timeout' },
    ];
    body.innerHTML = cards.map(c => {
      const v = decisions[c.key] || 0;
      const pct = total > 0 ? v / total : 0;
      return `
        <div class="${c.cls}">
          <div class="approval-card-label">${c.label}</div>
          <div class="approval-card-value">${fmtNum(v)}</div>
          <div class="approval-card-pct">${fmtPct(pct)}</div>
        </div>
      `;
    }).join('');
  }

  // ----------------------------------------------------------------
  // 信号池攻略进度条渲染（异步加载 /metrics/signals）
  // ----------------------------------------------------------------
  async function loadSignalPool() {
    const summaryEl = $('signalPoolSummary');
    const bodyEl = $('signalPoolBody');
    try {
      const res = await fetch('/metrics/signals');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const signals = data.signals || [];
      const sections = data.sections || {};
      const summary = data.summary || {};

      // 顶部汇总
      if (signals.length === 0) {
        summaryEl.innerHTML = '';
        bodyEl.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无信号</div></div>';
        return;
      }
      summaryEl.innerHTML = `
        <span class="signal-summary-item">总计 <strong>${summary.total || 0}</strong></span>
        <span class="signal-summary-item">pending <strong>${summary.pending || 0}</strong></span>
        <span class="signal-summary-item">triggered <strong>${summary.triggered || 0}</strong></span>
        <span class="signal-summary-item">written <strong>${summary.written || 0}</strong></span>
        <span class="signal-summary-item">阈值 <strong>${data.threshold || 7}</strong></span>
      `;

      // 按 section 分组渲染，每个 section 内已按 progress 降序
      const sectionNames = Object.keys(sections).sort();
      bodyEl.innerHTML = sectionNames.map(sec => {
        const items = sections[sec];
        const rows = items.map(s => {
          const pct = s.percent || 0;
          const isTriggered = s.status === 'triggered';
          const isWritten = s.status === 'written';
          const fillCls = isWritten ? 'signal-bar-fill written'
            : isTriggered ? 'signal-bar-fill triggered'
            : 'signal-bar-fill';
          const truncated = (s.content || '').length > 60
            ? (s.content || '').slice(0, 60) + '…'
            : (s.content || '');
          return `
            <div class="signal-row" title="${escapeHtml(s.content || '')}">
              <div class="signal-row-info">
                <span class="signal-content">${escapeHtml(truncated)}</span>
                <span class="signal-meta">${fmtNum(s.count)}/${s.threshold} · ${s.status}</span>
              </div>
              <div class="signal-bar">
                <div class="${fillCls}" style="width: ${Math.max(2, pct).toFixed(1)}%;"></div>
              </div>
              <span class="signal-pct">${pct}%</span>
            </div>
          `;
        }).join('');
        return `
          <div class="signal-section">
            <div class="signal-section-title">${escapeHtml(sec)} <span class="signal-section-count">(${items.length})</span></div>
            ${rows}
          </div>
        `;
      }).join('');
    } catch (e) {
      summaryEl.innerHTML = '';
      bodyEl.innerHTML = `<div class="empty-state"><div class="empty-state-text text-danger">加载失败: ${escapeHtml(e.message)}</div></div>`;
    }
  }

  // ----------------------------------------------------------------
  // 每日趋势历史数据
  // ----------------------------------------------------------------
  async function loadHistory() {
    const days = $('historyDays') ? $('historyDays').value : 30;
    try {
      const data = await fetchJson(`/metrics/history?days=${days}`);
      renderHistory(data);
    } catch (e) {
      console.error('history error:', e);
      const body = $('historyTableBody');
      if (body) body.innerHTML = '<tr><td colspan="7" class="empty-state-text text-danger">加载失败</td></tr>';
    }
  }

  function renderHistory(records) {
    const body = $('historyTableBody');
    if (!body) return;
    if (!records || records.length === 0) {
      body.innerHTML = '<tr><td colspan="7" class="empty-state-text">暂无历史数据</td></tr>';
      return;
    }
    // 按日期降序展示（最新在前）
    body.innerHTML = records.slice().reverse().map(r => {
      const llmAvg = r.llm_latency_ms && r.llm_latency_ms.count > 0
        ? fmtMs(r.llm_latency_ms.sum / r.llm_latency_ms.count) : '—';
      const memTotal = (r.memory_retrieval_hits_total || 0) + (r.memory_retrieval_misses_total || 0);
      const memRate = memTotal > 0
        ? fmtPct(r.memory_retrieval_hits_total / memTotal) : '—';
      const toolTotal = Object.values(r.tool_calls_total || {}).reduce((a, b) => a + b, 0);
      return `<tr>
        <td class="mono">${escapeHtml(r.date)}</td>
        <td class="num mono">${fmtNum(r.llm_calls_total || 0)}</td>
        <td class="num mono">${fmtNum(r.llm_tokens_input_total || 0)}</td>
        <td class="num mono">${fmtNum(r.llm_tokens_output_total || 0)}</td>
        <td class="num mono">${fmtNum(toolTotal)}</td>
        <td class="num mono">${llmAvg}</td>
        <td class="num mono">${memRate}</td>
      </tr>`;
    }).join('');
  }

  // ----------------------------------------------------------------
  // Canvas 延迟直方图绘制
  // ----------------------------------------------------------------
  function drawHistogram(canvasId, hist, legendId) {
    const canvas = $(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const buckets = (hist && hist.buckets) || [];
    const count = (hist && hist.count) || 0;

    // 适配 DPR 高清屏
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 600;
    const cssH = 180;
    if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
      canvas.width = cssW * dpr;
      canvas.height = cssH * dpr;
      ctx.scale(dpr, dpr);
    }

    ctx.clearRect(0, 0, cssW, cssH);

    // 从 CSS 变量读取主题色
    const styles = getComputedStyle(document.documentElement);
    const accent = styles.getPropertyValue('--accent').trim() || '#e0a96d';
    const accentDim = styles.getPropertyValue('--accent-dim').trim() || '#a87f4d';
    const textPrimary = styles.getPropertyValue('--text-primary').trim() || '#e8e0d5';
    const textMuted = styles.getPropertyValue('--text-muted').trim() || '#6b5d52';
    const borderStrong = styles.getPropertyValue('--border-strong').trim() || '#3d342d';
    const bgPrimary = styles.getPropertyValue('--bg-primary').trim() || '#1a1614';

    // 绘图区
    const padL = 36;
    const padR = 12;
    const padT = 12;
    const padB = 28;
    const plotW = cssW - padL - padR;
    const plotH = cssH - padT - padB;

    // 求最大值
    const maxVal = Math.max(1, ...buckets);

    // 绘 Y 轴网格 + 刻度
    ctx.strokeStyle = borderStrong;
    ctx.lineWidth = 0.5;
    ctx.fillStyle = textMuted;
    ctx.font = '10px JetBrains Mono, monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';

    const yTicks = 4;
    for (let i = 0; i <= yTicks; i++) {
      const yVal = Math.round((maxVal / yTicks) * i);
      const y = padT + plotH - (i / yTicks) * plotH;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillText(String(yVal), padL - 4, y);
    }

    // 柱状
    const n = buckets.length;
    const gap = 4;
    const barW = (plotW - gap * (n - 1)) / n;

    for (let i = 0; i < n; i++) {
      const val = buckets[i] || 0;
      const h = (val / maxVal) * plotH;
      const x = padL + i * (barW + gap);
      const y = padT + plotH - h;

      // 渐变填充
      const grad = ctx.createLinearGradient(0, y, 0, padT + plotH);
      grad.addColorStop(0, accent);
      grad.addColorStop(1, accentDim);
      ctx.fillStyle = grad;

      if (h > 0) {
        ctx.fillRect(x, y, barW, h);
      }

      // 顶部数字（仅当值大于 0）
      if (val > 0 && h > 12) {
        ctx.fillStyle = textPrimary;
        ctx.font = '10px JetBrains Mono, monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'bottom';
        ctx.fillText(String(val), x + barW / 2, y - 2);
      }
    }

    // X 轴标签
    ctx.fillStyle = textMuted;
    ctx.font = '9px JetBrains Mono, monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';

    for (let i = 0; i < HIST_LABELS.length; i++) {
      const x = padL + i * (barW + gap) + barW / 2;
      ctx.fillText(HIST_LABELS[i], x, padT + plotH + 6);
    }

    // 平均线
    if (count > 0 && hist.avg) {
      const avg = hist.avg;
      // 计算平均延迟落在哪个 bucket
      // 仅用平均值的数值在图上画一条参考线（按 avg 在 X 轴上的位置）
      let avgX = null;
      for (let i = 0; i < HIST_BOUNDS.length; i++) {
        if (avg <= HIST_BOUNDS[i]) {
          avgX = padL + i * (barW + gap) + barW / 2;
          break;
        }
      }
      if (avgX === null) {
        avgX = padL + (n - 1) * (barW + gap) + barW / 2;
      }

      ctx.strokeStyle = textPrimary;
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(avgX, padT);
      ctx.lineTo(avgX, padT + plotH);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = textPrimary;
      ctx.font = '9px JetBrains Mono, monospace';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.fillText(`avg ${fmtMs(avg)}`, avgX + 3, padT + 2);
    }

    // 渲染图例
    const legend = $(legendId);
    if (legend) {
      legend.innerHTML = `
        <span class="hist-legend-item"><span class="hist-legend-bar" style="background:${accent}"></span>调用次数</span>
        <span class="hist-legend-item">总样本 ${count}</span>
        ${hist && hist.avg ? `<span class="hist-legend-item">avg ${fmtMs(hist.avg)}</span>` : ''}
        ${hist && hist.max ? `<span class="hist-legend-item">max ${fmtMs(hist.max)}</span>` : ''}
      `;
    }
  }

  function drawAllHistograms() {
    if (!lastMetrics) return;
    drawHistogram('llmHistCanvas', lastMetrics.llm_latency_ms, 'llmHistLegend');
    drawHistogram('toolHistCanvas', lastMetrics.tool_latency_ms, 'toolHistLegend');
  }

  // ----------------------------------------------------------------
  // 审计日志渲染
  // ----------------------------------------------------------------
  function renderAudit(data) {
    const list = $('auditList');
    const logs = (data && data.logs) || [];
    const filter = $('auditTypeFilter').value;
    const filtered = filter === 'all' ? logs : logs.filter((l) => (l.entry_type || 'tool_call') === filter);

    if (filtered.length === 0) {
      list.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无审计记录</div></div>';
      return;
    }

    list.innerHTML = filtered.map((l) => {
      const type = l.entry_type || 'tool_call';
      const isGuard = type === 'guardrail';
      const isErr = l.is_error === true || l.is_error === 'true';
      const time = fmtTime(l.timestamp);
      const typeLabel = isGuard ? '护栏' : '工具';
      const typeClass = isGuard ? 'is-guardrail' : 'is-tool';
      const entryClass = isGuard ? 'is-guardrail' : (isErr ? 'is-error' : 'is-tool');

      let detail = '';
      let meta = '';
      if (isGuard) {
        detail = `<strong>${escapeHtml(l.action || '')}</strong> · ${escapeHtml(truncate(l.reason || '', 200))}`;
        meta = `${escapeHtml(l.layer || '')} · ${escapeHtml(l.risk_level || '')}`;
      } else {
        const input = typeof l.tool_input === 'object' ? JSON.stringify(l.tool_input) : (l.tool_input || '');
        detail = `<strong>${escapeHtml(l.tool_name || '?')}</strong> · ${escapeHtml(truncate(input, 80))}`;
        meta = `${l.duration_ms != null ? fmtMs(l.duration_ms) : ''} · ${escapeHtml(l.decision_source || '')}`;
        if (l.schedule_id) meta += ` · ${escapeHtml(l.schedule_id)}`;
        if (isErr && l.result) {
          detail += `<div class="audit-error-result" title="${escapeHtml(l.result)}">${escapeHtml(truncate(l.result, 200))}</div>`;
        }
      }

      return `
        <div class="audit-entry ${entryClass}">
          <div class="audit-time">${time}</div>
          <div class="audit-type ${typeClass}">${typeLabel}</div>
          <div class="audit-detail">${detail}</div>
          <div class="audit-meta">${meta}</div>
        </div>
      `;
    }).join('');
  }

  // ----------------------------------------------------------------
  // 调度运行历史渲染
  // ----------------------------------------------------------------
  function renderRuns(data) {
    const list = $('runsList');
    const runs = (data && data.runs) || [];

    $('runsCount').textContent = runs.length;
    const success = runs.filter((r) => r.success === true || r.success === 'true').length;
    $('runsSuccess').textContent = success;
    $('runsFail').textContent = runs.length - success;

    if (runs.length === 0) {
      list.innerHTML = '<div class="empty-state"><div class="empty-state-text">暂无调度运行记录</div></div>';
      return;
    }

    list.innerHTML = runs.map((r) => {
      const ok = r.success === true || r.success === 'true';
      const cls = ok ? 'is-success' : 'is-fail';
      const statusLabel = ok ? '成功' : '失败';
      const startedAt = fmtTime(r.started_at);
      const dur = r.duration_seconds != null ? `${r.duration_seconds.toFixed(1)}s` : '—';
      const task = escapeHtml(truncate(r.user_input || r.schedule_name || '', 120));
      const toolCount = (r.tool_calls || []).length;
      const errCount = (r.errors || []).length;
      const outCount = (r.outputs || []).length;

      let response = '';
      if (r.llm_summary) {
        response = `<div class="run-task">${escapeHtml(truncate(r.llm_summary, 200))}</div>`;
      } else if (r.assistant_response) {
        response = `<div class="run-task">${escapeHtml(truncate(r.assistant_response, 200))}</div>`;
      }

      return `
        <div class="run-entry ${cls}">
          <div class="run-header">
            <span class="run-name">${escapeHtml(r.schedule_name || r.schedule_id || '—')}</span>
            <span class="run-status ${cls}">${statusLabel}</span>
            <span class="run-duration">${dur}</span>
            <span class="run-time">${startedAt}</span>
          </div>
          <div class="run-task">${task}</div>
          ${response}
          <div class="run-meta">
            <span>🔧 ${toolCount}</span>
            <span>⚠ ${errCount}</span>
            <span>📤 ${outCount}</span>
            ${r.run_id ? `<span>id: ${escapeHtml(truncate(r.run_id, 12))}</span>` : ''}
          </div>
        </div>
      `;
    }).join('');
  }

  // ----------------------------------------------------------------
  // 轮询主循环
  // ----------------------------------------------------------------
  async function refreshAll() {
    if (isInFlight) return;
    isInFlight = true;

    const auditLimit = $('auditLimit').value;
    const runsLimit = $('runsLimit').value;

    const tasks = [
      fetchJson('/health').then(renderHealth).catch((e) => {
        $('overallStatus').textContent = '错误';
        $('overallStatus').className = 'section-status is-unhealthy';
        console.error('health error:', e);
      }),
      fetchJson('/metrics').then(renderMetrics).catch((e) => {
        console.error('metrics error:', e);
      }),
      fetchJson(`/audit/logs?limit=${auditLimit}`).then(renderAudit).catch((e) => {
        console.error('audit error:', e);
      }),
      fetchJson(`/schedules/runs?limit=${runsLimit}`).then(renderRuns).catch((e) => {
        console.error('runs error:', e);
      }),
      // Phase 2 反馈监控：信号池进度条（独立端点，不阻塞主指标渲染）
      loadSignalPool().catch((e) => {
        console.error('signal pool error:', e);
      }),
    ];

    try {
      await Promise.all(tasks);
      const now = new Date();
      const pad = (x) => String(x).padStart(2, '0');
      $('lastUpdate').textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
    } finally {
      isInFlight = false;
    }
  }

  function startTimer() {
    stopTimer();
    if (refreshInterval > 0) {
      refreshTimer = setInterval(refreshAll, refreshInterval);
    }
  }

  function stopTimer() {
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
  }

  // ----------------------------------------------------------------
  // 事件绑定
  // ----------------------------------------------------------------
  function bindEvents() {
    // 刷新间隔
    $('refreshInterval').addEventListener('change', (e) => {
      refreshInterval = parseInt(e.target.value, 10) * 1000;
      if (refreshInterval > 0) {
        startTimer();
        showToast(`自动刷新 ${refreshInterval / 1000}s`, 'success');
      } else {
        stopTimer();
        showToast('自动刷新已关闭');
      }
    });

    // 立即刷新
    $('btnRefresh').addEventListener('click', () => {
      refreshAll();
      showToast('刷新中...');
    });

    // 重置指标
    $('btnReset').addEventListener('click', async () => {
      if (!confirm('确认重置所有指标计数器与直方图？此操作不可撤销。')) return;
      try {
        await fetch('/metrics/reset', { method: 'POST' });
        showToast('指标已重置', 'success');
        await refreshAll();
      } catch (e) {
        showToast('重置失败: ' + e.message, 'error');
      }
    });

    // 审计筛选
    $('auditTypeFilter').addEventListener('change', () => {
      // 重新渲染当前缓存数据
      fetchJson(`/audit/logs?limit=${$('auditLimit').value}`).then(renderAudit).catch(() => {});
    });

    $('auditLimit').addEventListener('change', () => {
      fetchJson(`/audit/logs?limit=${$('auditLimit').value}`).then(renderAudit).catch(() => {});
    });

    $('runsLimit').addEventListener('change', () => {
      fetchJson(`/schedules/runs?limit=${$('runsLimit').value}`).then(renderRuns).catch(() => {});
    });

    // 页面可见性：隐藏时暂停轮询
    document.addEventListener('visibilitychange', () => {
      const wasVisible = isVisible;
      isVisible = !document.hidden;
      if (isVisible && !wasVisible) {
        // 重新可见时立即刷新一次
        refreshAll();
        startTimer();
      } else if (!isVisible && wasVisible) {
        stopTimer();
      }
    });

    // 每日趋势历史数据
    if ($('historyDays')) {
      $('historyDays').addEventListener('change', () => loadHistory());
    }
    if ($('btnRefreshHistory')) {
      $('btnRefreshHistory').addEventListener('click', () => {
        loadHistory();
        showToast('刷新历史数据...', 'success');
      });
    }

    // 窗口尺寸变化重绘 Canvas
    let resizeTimer = null;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(drawAllHistograms, 200);
    });
  }

  // ----------------------------------------------------------------
  // 启动
  // ----------------------------------------------------------------
  function init() {
    initAccentPicker();
    bindEvents();
    refreshAll();
    startTimer();
    loadHistory();  // 历史数据按天变化，不需要 5s 轮询，初始化时加载一次
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
