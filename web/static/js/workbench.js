/* ============================================================
   workbench.js — 独立工作台页面特有逻辑
   依赖: collab-workbench.js（共享组件）、collab-sse.js（SSE 订阅）
   职责：
     1. 工作台页面初始化（collab-workbench 已自动 init，此处补充增强功能）
     2. 显示本地 agent_id
     3. 加载 Director 详细信息
     4. 消息过滤（关键词 + 类型）
     5. 统计数据更新
     6. 顶栏刷新按钮
   ============================================================ */
(function () {
  "use strict";

  // ========== 状态 ==========
  var allMessages = [];      // 所有已渲染消息（供过滤使用）
  var currentFilter = "";    // 关键词过滤
  var currentTypeFilter = ""; // 类型过滤
  var _directorActionInFlight = false;  // Director 启停请求进行中标记（防抖）

  // ========== DOM 辅助 ==========
  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(s) {
    if (!s) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ========== 本地 Agent 显示 ==========
  function updateLocalAgentDisplay(agentId) {
    var el = $("wbLocalAgent");
    var idEl = $("wbLocalAgentId");
    if (!el || !idEl) return;
    if (agentId) {
      idEl.textContent = agentId;
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  }

  // ========== Director 详情加载 ==========
  function loadDirectorDetails() {
    fetch("/api/multiagent/status")
      .then(function (resp) {
        if (!resp.ok) return null;
        return resp.json();
      })
      .then(function (data) {
        if (!data || !data.enabled) {
          _setDirectorStateLarge("disabled");
          return;
        }
        var director = data.director || {};
        $("wbDirectorRole").textContent = data.role || "—";
        $("wbDirectorEpoch").textContent = director.epoch != null ? director.epoch : "—";
        $("wbDirectorTick").textContent = director.last_tick || "—";
        $("wbAutonomousMode").textContent = data.autonomous_mode ? "是" : "否";
        updateLocalAgentDisplay(data.local_agent_id || "");

        // 同步右栏 Director 状态药丸
        // 后端返回 healthy/degraded/autonomous/fault/unknown，前端统一映射展示
        var rawState = director.state || "unknown";
        var displayState = _mapDirectorState(rawState);
        var faultReason = director.fault_reason || "";
        _setDirectorStateLarge(displayState, faultReason);
        // 同步顶栏小药丸
        if (window.CollabWorkbench && window.CollabWorkbench.setDirectorState) {
          window.CollabWorkbench.setDirectorState(displayState, faultReason);
        }
        // 同步启停按钮显隐
        updateDirectorButtons(displayState);

        // 渲染 epoch 时间线
        renderEpochTimeline(data.epoch_history || [], director.epoch);
      })
      .catch(function (err) {
        console.warn("workbench: loadDirectorDetails 失败", err);
        _setDirectorStateLarge("unknown");
      });
  }

  /** 将后端 director.state 映射为前端展示状态。
   * 后端返回 stopped/starting/crashed/healthy/degraded/autonomous/fault/unknown
   * 前端展示状态：
   *   stopped    → stopped    （休眠，灰色，无脉动）
   *   starting   → starting  （启动中，蓝色，脉动）
   *   crashed    → crashed   （已崩溃，红色，无脉动）
   *   healthy    → healthy    （运行中，绿色脉动）
   *   degraded   → degraded   （降级，黄色）
   *   autonomous → autonomous （自治中，蓝色）
   *   fault      → fault      （故障，红色）
   *   unknown    → unknown    （未知，灰色）
   * 注：observing/injected/disabled 为前端独有状态（分别用于初始观察、注入引导瞬时、未启用）
   */
  function _mapDirectorState(rawState) {
    var map = {
      stopped: "stopped",
      starting: "starting",
      crashed: "crashed",
      healthy: "healthy",
      degraded: "degraded",
      autonomous: "autonomous",
      fault: "fault",
      unknown: "unknown",
    };
    return map[rawState] || "unknown";
  }

  /** 设置大状态药丸显示
   * @param {string} state - stopped/starting/crashed/healthy/degraded/autonomous/fault/unknown/observing/injected/disabled
   * @param {string} [reason] - 故障/异常原因，作为 tooltip 展示（可选）
   */
  function _setDirectorStateLarge(state, reason) {
    var el = $("wbDirectorStateLarge");
    if (!el) return;
    var text = el.querySelector(".wb-director-state-text");
    var labels = {
      stopped: "○ 已休眠",
      starting: "● 启动中",
      crashed: "✕ 已崩溃",
      healthy: "● 运行中",
      degraded: "▲ 降级",
      autonomous: "◆ 自治中",
      fault: "✕ 故障",
      unknown: "? 未知",
      observing: "● 观察中",
      injected: "▲ 已注入引导",
      disabled: "○ 未启用",
    };
    var titleMap = {
      stopped: reason || "Director 未启动（已休眠）",
      starting: reason || "Director 正在启动中",
      crashed: reason || "Director 进程已崩溃",
      healthy: "Director 运行正常",
      degraded: reason || "Director 性能降级",
      autonomous: reason || "Director 进入自治模式",
      fault: reason || "Director 故障",
      unknown: reason || "Director 状态未知",
      observing: "Director 观察中",
      injected: "已注入引导指令",
      disabled: "协作未启用",
    };
    el.className = "wb-director-state-large state-" + state;
    if (text) text.textContent = labels[state] || state;
    // 设置 tooltip：故障原因优先，无原因时用默认说明
    el.title = titleMap[state] || state;
  }

  // ========== Director 启停控制 ==========

  /** 根据当前状态切换启停按钮显隐
   * @param {string} state - stopped/starting/crashed/healthy/degraded/autonomous/fault/unknown
   * 规则：
   *   stopped/crashed/unknown → 显示「启动」，隐藏「停止」「重启」
   *   healthy/degraded/autonomous/starting → 显示「停止」「重启」，隐藏「启动」
   *   fault → 显示「重启」，隐藏「启动」「停止」
   */
  function updateDirectorButtons(state) {
    var startBtn = $("wbDirectorStartBtn");
    var stopBtn = $("wbDirectorStopBtn");
    var restartBtn = $("wbDirectorRestartBtn");
    if (!startBtn || !stopBtn || !restartBtn) return;

    // 请求进行中时保持禁用，不切换显隐
    var showStart = (state === "stopped" || state === "crashed" || state === "unknown");
    var showStop = (state === "healthy" || state === "degraded" || state === "autonomous" || state === "starting");
    var showRestart = showStop || state === "fault";

    startBtn.hidden = !showStart;
    stopBtn.hidden = !showStop;
    restartBtn.hidden = !showRestart;
  }

  /** 通用启停请求封装：乐观更新状态 + 防抖 + 错误回滚
   * @param {string} action - start/stop/restart
   * @param {string} optimisticState - 乐观设置的临时展示状态
   * @param {string} successMsg - 成功提示
   */
  function _performDirectorAction(action, optimisticState, successMsg) {
    if (_directorActionInFlight) {
      showToast("上一个操作仍在进行中，请稍候", "info");
      return Promise.resolve();
    }
    _directorActionInFlight = true;
    // 禁用所有启停按钮，防止重复点击
    _setDirectorButtonsDisabled(true);
    // 乐观更新状态显示
    _setDirectorStateLarge(optimisticState);
    if (window.CollabWorkbench && window.CollabWorkbench.setDirectorState) {
      window.CollabWorkbench.setDirectorState(optimisticState);
    }

    return fetch("/api/multiagent/director/" + action, { method: "POST" })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().then(function (err) {
            throw new Error(err.detail || ("HTTP " + resp.status));
          }, function () {
            throw new Error("HTTP " + resp.status);
          });
        }
        return resp.json();
      })
      .then(function (data) {
        if (data && data.ok) {
          showToast(successMsg, "success");
        } else {
          throw new Error((data && data.message) || "操作失败");
        }
      })
      .catch(function (err) {
        showToast(action + " 失败：" + err.message, "error");
      })
      .finally(function () {
        // 立即拉取真实状态，覆盖乐观更新
        loadDirectorDetails();
        _directorActionInFlight = false;
        _setDirectorButtonsDisabled(false);
      });
  }

  /** 启用/禁用所有启停按钮 */
  function _setDirectorButtonsDisabled(disabled) {
    var btns = ["wbDirectorStartBtn", "wbDirectorStopBtn", "wbDirectorRestartBtn"];
    btns.forEach(function (id) {
      var b = $(id);
      if (b) b.disabled = disabled;
    });
  }

  function startDirector() {
    return _performDirectorAction("start", "starting", "Director 已启动");
  }

  function stopDirector() {
    return _performDirectorAction("stop", "stopped", "Director 已停止");
  }

  function restartDirector() {
    return _performDirectorAction("restart", "starting", "Director 重启中");
  }

  /** 渲染 epoch 时间线 */
  function renderEpochTimeline(history, currentEpoch) {
    var container = $("wbEpochNodes");
    if (!container) return;
    if (!history || history.length === 0) {
      container.innerHTML = '<span class="wb-epoch-empty">无历史</span>';
      return;
    }
    var html = "";
    history.forEach(function (node, idx) {
      var isCurrent = node.epoch === currentEpoch;
      var time = node.ts ? _formatEpochTime(node.ts) : "";
      html += '<div class="wb-epoch-node' + (isCurrent ? " wb-epoch-current" : "") + '" data-seq="' + (node.seq || 0) + '" title="Epoch ' + escapeHtml(String(node.epoch)) + (time ? ' · ' + time : '') + '">';
      html += '<span class="wb-epoch-node-dot"></span>';
      html += '<span class="wb-epoch-node-epoch">E' + escapeHtml(String(node.epoch)) + '</span>';
      if (time) html += '<span class="wb-epoch-node-time">' + escapeHtml(time) + '</span>';
      html += '</div>';
      if (idx < history.length - 1) {
        html += '<div class="wb-epoch-connector"></div>';
      }
    });
    container.innerHTML = html;

    // 点击 epoch 节点跳转对应消息
    container.querySelectorAll(".wb-epoch-node").forEach(function (node) {
      node.addEventListener("click", function () {
        var seq = parseInt(node.getAttribute("data-seq"), 10);
        if (seq > 0 && window.CollabWorkbench && window.CollabWorkbench.jumpToMessage) {
          window.CollabWorkbench.jumpToMessage(seq);
        }
      });
    });
  }

  /** 格式化 epoch 节点时间（仅显示时分） */
  function _formatEpochTime(ts) {
    if (!ts) return "";
    try {
      var d = new Date(ts);
      var hh = d.getHours().toString().padStart(2, "0");
      var mm = d.getMinutes().toString().padStart(2, "0");
      return hh + ":" + mm;
    } catch (e) {
      return "";
    }
  }

  // ========== 实时指标渲染 ==========
  /** 渲染实时指标区 */
  function renderRealtimeMetrics() {
    _renderMsgTypeChart();
    _renderAgentRanking();
    _renderLastBroadcast();
    _renderMsgTrend();
  }

  /** 消息类型分布条形图 */
  function _renderMsgTypeChart() {
    var container = $("wbMsgTypeChart");
    if (!container) return;
    var counts = {};
    var total = 0;
    allMessages.forEach(function (m) {
      var t = m.type || "unknown";
      counts[t] = (counts[t] || 0) + 1;
      total++;
    });
    if (total === 0) {
      container.innerHTML = '<div class="wb-metric-empty">暂无数据</div>';
      return;
    }
    var colors = {
      announce: "var(--info, #58a6ff)",
      request: "var(--warning, #d29922)",
      response: "var(--success, #3fb950)",
      relay: "var(--text-muted)",
      directive: "var(--danger, #f85149)",
      status: "var(--accent)"
    };
    var html = "";
    Object.keys(counts).forEach(function (type) {
      var count = counts[type];
      var pct = (count / total) * 100;
      var color = colors[type] || "var(--accent)";
      html += '<div class="wb-metric-bar-row">';
      html += '<span class="wb-metric-bar-label">' + escapeHtml(type) + '</span>';
      html += '<div class="wb-metric-bar-track"><div class="wb-metric-bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div>';
      html += '<span class="wb-metric-bar-value">' + count + '</span>';
      html += '</div>';
    });
    container.innerHTML = html;
  }

  /** Agent 活跃度排行（按消息数 Top 3） */
  function _renderAgentRanking() {
    var container = $("wbAgentRanking");
    if (!container) return;
    var counts = {};
    allMessages.forEach(function (m) {
      var f = m.from || "?";
      counts[f] = (counts[f] || 0) + 1;
    });
    var sorted = Object.keys(counts)
      .map(function (k) { return { name: k, count: counts[k] }; })
      .sort(function (a, b) { return b.count - a.count; })
      .slice(0, 3);
    if (sorted.length === 0) {
      container.innerHTML = '<div class="wb-metric-empty">暂无数据</div>';
      return;
    }
    var html = "";
    sorted.forEach(function (item, idx) {
      var rank = idx + 1;
      html += '<div class="wb-rank-row wb-rank-' + rank + '">';
      html += '<span class="wb-rank-num">' + rank + '</span>';
      html += '<span class="wb-rank-name">' + escapeHtml(item.name) + '</span>';
      html += '<span class="wb-rank-count">' + item.count + '</span>';
      html += '</div>';
    });
    container.innerHTML = html;
  }

  /** 最近广播 */
  function _renderLastBroadcast() {
    var container = $("wbLastBroadcast");
    if (!container) return;
    var broadcasts = allMessages.filter(function (m) {
      return m.from === "director" && m.type === "request";
    });
    if (broadcasts.length === 0) {
      container.innerHTML = '<div class="wb-metric-empty">暂无广播</div>';
      return;
    }
    var last = broadcasts[broadcasts.length - 1];
    var content = last.content || "";
    var ts = last.timestamp || last.ts || "";
    var timeRel = ts ? _relativeTimeShort(ts) : "";
    var html = '<div class="wb-broadcast-content">' + (typeof renderMarkdown === "function" ? renderMarkdown(content) : escapeHtml(content)) + '</div>';
    if (timeRel) html += '<div class="wb-broadcast-time">' + escapeHtml(timeRel) + '</div>';
    container.innerHTML = html;
  }

  /** 总消息趋势（最近 1 小时消息数 + 与上 1 小时对比） */
  function _renderMsgTrend() {
    var container = $("wbMsgTrend");
    if (!container) return;
    var now = Date.now();
    var oneHourAgo = now - 3600000;
    var twoHoursAgo = now - 7200000;
    var recent = 0;
    var previous = 0;
    allMessages.forEach(function (m) {
      var ts = m.timestamp || m.ts;
      if (!ts) return;
      var t = new Date(ts).getTime();
      if (isNaN(t)) return;
      if (t >= oneHourAgo) recent++;
      else if (t >= twoHoursAgo) previous++;
    });
    var valueEl = container.querySelector(".wb-metric-trend-value");
    var arrowEl = container.querySelector(".wb-metric-trend-arrow");
    if (valueEl) valueEl.textContent = String(recent);
    if (arrowEl) {
      arrowEl.className = "wb-metric-trend-arrow";
      if (recent > previous) {
        arrowEl.textContent = "↑";
        arrowEl.classList.add("up");
      } else if (recent < previous) {
        arrowEl.textContent = "↓";
        arrowEl.classList.add("down");
      } else {
        arrowEl.textContent = "→";
      }
    }
  }

  // ========== 消息过滤 ==========
  /**
   * 应用过滤：根据关键词和类型隐藏/显示消息
   */
  function applyFilter() {
    var stream = $("cwStream");
    if (!stream) return;
    var msgs = stream.querySelectorAll(".cw-msg");
    var visibleCount = 0;
    msgs.forEach(function (el) {
      var from = (el.dataset.from || "").toLowerCase();
      var type = (el.dataset.type || "").toLowerCase();
      var content = (el.textContent || "").toLowerCase();
      var keyword = currentFilter.toLowerCase();
      var typeMatch = !currentTypeFilter || type === currentTypeFilter.toLowerCase();
      var keywordMatch = !keyword || from.indexOf(keyword) >= 0 || type.indexOf(keyword) >= 0 || content.indexOf(keyword) >= 0;
      if (typeMatch && keywordMatch) {
        el.style.display = "";
        visibleCount++;
      } else {
        el.style.display = "none";
      }
    });
    $("cwStreamCount").textContent = visibleCount;
  }

  // ========== 监听协作消息（用于统计和过滤） ==========
  function setupMessageListener() {
    window.addEventListener("collab-message", function (e) {
      var msg = e.detail || {};
      allMessages.push(msg);
      if (allMessages.length > 500) allMessages.splice(0, allMessages.length - 500);
      // dataset.from / dataset.type 已由 collab-workbench.js 的 renderCollabMessage 统一设置
      // 此处仅在 DOM 更新后刷新统计和过滤
      setTimeout(function () {
        applyFilter();
        renderRealtimeMetrics();
      }, 0);
    });

    // 监听 SSE 连接状态，更新顶栏指示灯
    window.addEventListener("collab-sse-state", function (e) {
      var detail = e.detail || {};
      updateSseIndicator(detail.state, detail.attempts);
    });
  }

  /** 更新 SSE 连接状态指示灯
   * @param {string} state - connected/reconnecting/disconnected
   * @param {number} [attempts] - 重连次数
   */
  function updateSseIndicator(state, attempts) {
    var el = $("cwSseIndicator");
    if (!el) return;
    var dot = el.querySelector(".wb-sse-dot");
    var text = el.querySelector(".wb-sse-text");
    if (!dot || !text) return;

    var labels = {
      connected: "已连接",
      reconnecting: "重连中",
      disconnected: "未连接",
    };
    var titles = {
      connected: "SSE 通道已连接，实时接收协作消息",
      reconnecting: "SSE 连接断开，第 " + (attempts || 1) + " 次重连中…",
      disconnected: "SSE 未启动",
    };

    dot.className = "wb-sse-dot state-" + state;
    text.textContent = labels[state] || state;
    el.title = titles[state] || state;
  }

  // ========== 顶栏刷新 ==========
  function setupRefreshAll() {
    var btn = $("wbRefreshAll");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (window.CollabWorkbench) {
        if (window.CollabWorkbench.loadAgents) window.CollabWorkbench.loadAgents();
        if (window.CollabWorkbench.loadActivityTimeline) window.CollabWorkbench.loadActivityTimeline(false);
        if (window.CollabWorkbench.loadRecentMessages) window.CollabWorkbench.loadRecentMessages();
        if (window.CollabWorkbench.loadLocalAgentId) window.CollabWorkbench.loadLocalAgentId();
      }
      loadDirectorDetails();
    });
  }

  // ========== 过滤控件 ==========
  function setupFilters() {
    var filterInput = $("wbFilterInput");
    if (filterInput) {
      filterInput.addEventListener("input", function () {
        currentFilter = filterInput.value;
        applyFilter();
      });
    }
    var typeFilter = $("wbTypeFilter");
    if (typeFilter) {
      typeFilter.addEventListener("change", function () {
        currentTypeFilter = typeFilter.value;
        applyFilter();
      });
    }
  }

  // ========== 定时刷新统计和 Director 信息 ==========
  function setupPeriodicRefresh() {
    // 每 15 秒刷新 Director 详情
    setInterval(loadDirectorDetails, 15000);
    // 每 10 秒刷新实时指标
    setInterval(function () {
      renderRealtimeMetrics();
    }, 10000);
  }

  // ========== 主题切换 ==========
  function setupThemeSwitcher() {
    // 主题切换按钮
    var toggleBtn = $("wbThemeToggle");
    if (toggleBtn && window.TeageTheme) {
      toggleBtn.addEventListener("click", function () {
        window.TeageTheme.toggleTheme();
      });
    }
    // 强调色选择
    document.querySelectorAll(".accent-dot").forEach(function (dot) {
      dot.addEventListener("click", function () {
        if (window.TeageTheme) {
          window.TeageTheme.setAccent(dot.dataset.accent);
        }
      });
    });
    // 初始化时标记当前强调色
    updateAccentActiveState();
    // 监听主题变化更新 UI
    document.addEventListener("themechange", function () {
      updateAccentActiveState();
    });
  }

  /** 根据当前主题更新强调色点的 is-active 状态 */
  function updateAccentActiveState() {
    var currentAccent = (window.TeageTheme && window.TeageTheme.getAccent()) || "violet";
    document.querySelectorAll(".accent-dot").forEach(function (dot) {
      if (dot.dataset.accent === currentAccent) {
        dot.classList.add("is-active");
      } else {
        dot.classList.remove("is-active");
      }
    });
  }

  // ========== Agent Card 抽屉（独立组件） ==========
  var _cardReqSeq = 0; // 请求序号，用于丢弃过期响应

  function setupAgentCardView() {
    var drawer = document.getElementById("wbAgentCardDrawer");
    var btn = $("wbAgentCardBtn");
    if (!drawer || !btn) return;

    // 打开：Agent Card 按钮
    btn.addEventListener("click", function () {
      showAgentCard(null);
      drawer.classList.add("open");
    });

    // 打开：agent 列表项点击
    document.addEventListener("click", function (e) {
      var card = e.target.closest(".cw-agent-card");
      if (!card) return;
      var agentId = card.dataset.agentId;
      if (!agentId) return;
      showAgentCard(agentId);
      drawer.classList.add("open");
    });

    // 关闭：遮罩层点击 / 关闭按钮点击
    drawer.addEventListener("click", function (e) {
      if (e.target.closest(".wb-drawer-overlay") || e.target.closest(".wb-drawer-close-btn")) {
        drawer.classList.remove("open");
      }
    });

    // 复制 JSON
    setupCardCopyBtn();
    // 刷新
    setupCardRefreshBtn();
  }

  function setupCardCopyBtn() {
    var copyBtn = $("wbCardCopyJsonBtn");
    if (!copyBtn) return;
    copyBtn.addEventListener("click", function () {
      var view = $("wbCardView");
      if (!view) return;
      var currentCard = view.dataset.cardJson || "";
      if (!currentCard) {
        showToast("无数据可复制", "info");
        return;
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(currentCard).then(function () {
          showToast("JSON 已复制到剪贴板", "success");
        }).catch(function () {
          showToast("复制失败", "error");
        });
      } else {
        var ta = document.createElement("textarea");
        ta.value = currentCard;
        document.body.appendChild(ta);
        ta.select();
        try {
          document.execCommand("copy");
          showToast("JSON 已复制到剪贴板", "success");
        } catch (e) {
          showToast("复制失败", "error");
        }
        document.body.removeChild(ta);
      }
    });
  }

  function setupCardRefreshBtn() {
    var refreshBtn = $("wbCardRefreshBtn");
    if (!refreshBtn) return;
    refreshBtn.addEventListener("click", function () {
      var titleId = $("wbCardTitleId");
      var agentId = titleId ? titleId.textContent : "";
      if (agentId && agentId !== "—" && agentId !== "（未配置）") {
        showAgentCard(agentId);
      } else {
        showToast("无可用 agent_id", "info");
      }
    });
  }

  /**
   * 加载并显示 agent card（只读，始终从文件读取最新数据）
   * @param {string|null} agentId - agent_id，null 表示本地 agent（使用 localAgentId）
   */
  function showAgentCard(agentId) {
    var view = $("wbCardView");
    var titleId = $("wbCardTitleId");
    if (!view) return;
    view.innerHTML = '<div class="wb-card-loading">加载中…</div>';

    // null 时使用本地 agent_id，统一走 /agents/{id}/card 端点
    var localId = (window.CollabWorkbench && window.CollabWorkbench.getLocalAgentId) ? window.CollabWorkbench.getLocalAgentId() : "";
    var actualId = agentId || localId || "";
    if (titleId) titleId.textContent = actualId ? actualId : "（未配置）";

    if (!actualId) {
      view.innerHTML = '<div class="wb-card-empty">未配置本地 agent_id</div>';
      return;
    }

    // 请求序号去重：快速连续点击时丢弃过期响应
    var seq = ++_cardReqSeq;
    var url = "/api/multiagent/agents/" + encodeURIComponent(actualId) + "/card?_t=" + Date.now();

    fetch(url)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (seq !== _cardReqSeq) return; // 已过期，丢弃
        var card = data.card || {};
        var view2 = $("wbCardView");
        if (view2) view2.dataset.cardJson = JSON.stringify(card, null, 2);
        renderAgentCard(card, actualId);
      })
      .catch(function (err) {
        if (seq !== _cardReqSeq) return;
        view.innerHTML = '<div class="wb-card-empty">加载失败: ' + escapeHtml(err.message) + "</div>";
      });
  }

  /** 渲染 agent card 到分层视图 */
  function renderAgentCard(card, agentId) {
    var view = $("wbCardView");
    if (!view) return;

    if (!card || Object.keys(card).length === 0) {
      view.innerHTML = '<div class="wb-card-empty">无 Agent Card 数据</div>';
      return;
    }

    var html = "";

    // --- 头部摘要 banner ---
    var status = card.status || "active";
    var role = card.role || "";
    var lastHb = card.last_heartbeat || "";
    var registeredAt = card.registered_at || "";
    var hbRel = lastHb ? _relativeTimeShort(lastHb) : "";
    var regRel = registeredAt ? _relativeTimeShort(registeredAt) : "";

    html += '<div class="wb-card-banner">';
    html += '<span class="wb-card-banner-status status-' + escapeHtml(status) + '"></span>';
    html += '<div class="wb-card-banner-id">';
    html += '<div class="wb-card-banner-id-text">' + escapeHtml(agentId || card.agent_id || "—") + '</div>';
    html += '<div class="wb-card-banner-meta">';
    if (hbRel) html += '<span>心跳 ' + escapeHtml(hbRel) + '</span>';
    if (regRel) html += '<span>注册 ' + escapeHtml(regRel) + '</span>';
    html += '</div>';
    html += '</div>';
    // 徽章横向排列（role + status），节省纵向空间
    html += '<div class="wb-card-banner-badges">';
    if (role) html += '<span class="wb-card-badge">' + escapeHtml(role) + '</span>';
    html += '<span class="wb-card-badge wb-badge-status">' + escapeHtml(status) + '</span>';
    html += '</div>';
    html += '</div>';

    // --- 主字段区（capabilities / specialties / dangerous_tools） ---
    var hasMain = card.capabilities || card.specialties || card.dangerous_tools;
    if (hasMain) {
      html += '<div class="wb-card-fields-main">';
      html += _renderTagBlock("能力列表", card.capabilities, "");
      html += _renderTagBlock("专长领域", card.specialties, "wb-tag-specialty");
      html += _renderTagBlock("危险工具", card.dangerous_tools, "wb-tag-danger");
      html += '</div>';
    }

    // --- 元数据描述列表（响应式网格，自适应列数） ---
    var metaFields = [
      { key: "endpoint", label: "服务端点" },
      { key: "owner", label: "所有者" },
      { key: "auth_method", label: "认证方式" },
      { key: "agent_version", label: "Agent 版本" },
      { key: "protocol_version", label: "协议版本" },
      { key: "pid", label: "进程 PID" },
      { key: "host", label: "主机" },
    ];
    var metaHtml = "";
    metaFields.forEach(function (f) {
      var val = card[f.key];
      if (val === undefined || val === null || val === "") return;
      metaHtml += '<div class="wb-card-meta-item">';
      metaHtml += '<div class="wb-card-meta-label">' + escapeHtml(f.label) + '</div>';
      metaHtml += '<div class="wb-card-meta-value">' + escapeHtml(String(val)) + '</div>';
      metaHtml += '</div>';
    });

    if (metaHtml) {
      html += '<div class="wb-card-meta">' + metaHtml + '</div>';
    }

    // --- 进度条区域（独立全宽，避免被网格挤压） ---
    var progressHtml = "";
    if (card.trust_score !== undefined && card.trust_score !== null) {
      var trust = parseFloat(card.trust_score);
      var trustPct = isNaN(trust) ? 0 : Math.min(100, Math.max(0, trust));
      progressHtml += '<div class="wb-card-progress-row">';
      progressHtml += '<div class="wb-card-progress-head">';
      progressHtml += '<span class="wb-card-meta-label">信任分数</span>';
      progressHtml += '<span class="wb-card-progress-text">' + escapeHtml(String(card.trust_score)) + '</span>';
      progressHtml += '</div>';
      progressHtml += '<div class="wb-card-progress-bar"><div class="wb-card-progress-fill" style="width:' + trustPct + '%"></div></div>';
      progressHtml += '</div>';
    }

    if (card.max_concurrent_tasks !== undefined && card.max_concurrent_tasks !== null) {
      var maxC = parseInt(card.max_concurrent_tasks, 10);
      var maxPct = isNaN(maxC) ? 0 : Math.min(100, (maxC / 10) * 100);
      progressHtml += '<div class="wb-card-progress-row">';
      progressHtml += '<div class="wb-card-progress-head">';
      progressHtml += '<span class="wb-card-meta-label">最大并发任务</span>';
      progressHtml += '<span class="wb-card-progress-text">' + escapeHtml(String(card.max_concurrent_tasks)) + '</span>';
      progressHtml += '</div>';
      progressHtml += '<div class="wb-card-progress-bar"><div class="wb-card-progress-fill" style="width:' + maxPct + '%"></div></div>';
      progressHtml += '</div>';
    }

    if (progressHtml) {
      html += '<div class="wb-card-progress-section">' + progressHtml + '</div>';
    }

    if (!html) {
      view.innerHTML = '<div class="wb-card-empty">无可用字段</div>';
    } else {
      view.innerHTML = html;
    }
  }

  /** 渲染 tag 块（capabilities / specialties / dangerous_tools） */
  function _renderTagBlock(label, values, extraClass) {
    if (!values || (Array.isArray(values) && values.length === 0)) return "";
    var arr = Array.isArray(values) ? values : [values];
    var html = '<div class="wb-card-field-block">';
    html += '<div class="wb-card-field-block-label">' + escapeHtml(label) + '</div>';
    html += '<div class="wb-card-tags">';
    arr.forEach(function (item) {
      var cls = "wb-card-tag" + (extraClass ? " " + extraClass : "");
      html += '<span class="' + cls + '">' + escapeHtml(String(item)) + '</span>';
    });
    html += '</div></div>';
    return html;
  }

  /** 相对时间简短格式（"3s 前" / "2m 前" / "1h 前" / "2d 前"） */
  function _relativeTimeShort(ts) {
    if (!ts) return "";
    try {
      var d = new Date(ts);
      var diff = (Date.now() - d.getTime()) / 1000;
      if (diff < 60) return Math.floor(diff) + "s 前";
      if (diff < 3600) return Math.floor(diff / 60) + "m 前";
      if (diff < 86400) return Math.floor(diff / 3600) + "h 前";
      if (diff < 7 * 86400) return Math.floor(diff / 86400) + "d 前";
      return d.toLocaleDateString();
    } catch (e) {
      return "";
    }
  }

  // ========== 快捷指令模板 ==========
  /** 快捷指令模板：点击填入 directive 模态框 */
  function setupTemplateButtons() {
    var buttons = document.querySelectorAll(".wb-template-btn");
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var content = btn.getAttribute("data-content") || "";
        var rule = btn.getAttribute("data-rule") || "ordering";
        var priority = btn.getAttribute("data-priority") || "normal";

        // 打开 directive 模态框
        var modal = $("cwDirectiveModal");
        if (!modal) return;
        modal.hidden = false;

        // 填入字段
        var inputEl = $("cwDirectiveInput");
        var ruleEl = $("cwDirectiveRuleType");
        var priEl = $("cwDirectivePriority");
        var submitBtn = $("cwDirectiveSubmitBtn");
        if (inputEl) inputEl.value = content;
        if (ruleEl) ruleEl.value = rule;
        if (priEl) priEl.value = priority;
        if (submitBtn) submitBtn.disabled = false;
        if (inputEl) inputEl.focus();
      });
    });
  }

  // ========== 响应式抽屉（小屏） ==========
  /** 抽屉切换（小屏响应式）：点击按钮展开/收起侧栏抽屉 */
  function setupDrawers() {
    var rightBtn = $("wbRightDrawerToggle");
    var leftBtn = $("wbLeftDrawerToggle");
    var rightPanel = document.querySelector(".wb-action-panel");
    var leftPanel = document.querySelector(".wb-sidebar");

    if (rightBtn && rightPanel) {
      rightBtn.addEventListener("click", function () {
        rightPanel.classList.toggle("wb-drawer-open");
        // 关闭左栏抽屉（互斥）
        if (leftPanel) leftPanel.classList.remove("wb-drawer-open");
      });
    }
    if (leftBtn && leftPanel) {
      leftBtn.addEventListener("click", function () {
        leftPanel.classList.toggle("wb-drawer-open");
        // 关闭右栏抽屉（互斥）
        if (rightPanel) rightPanel.classList.remove("wb-drawer-open");
      });
    }
  }

  // ========== Director 启停按钮绑定 ==========
  function setupDirectorControls() {
    var startBtn = $("wbDirectorStartBtn");
    var stopBtn = $("wbDirectorStopBtn");
    var restartBtn = $("wbDirectorRestartBtn");
    if (startBtn) startBtn.addEventListener("click", startDirector);
    if (stopBtn) stopBtn.addEventListener("click", stopDirector);
    if (restartBtn) restartBtn.addEventListener("click", restartDirector);
  }

  // ========== 初始化 ==========
  function init() {
    // collab-workbench.js 已自动 init，此处补充工作台页面特有功能
    toastEl = document.getElementById("toast");
    loadDirectorDetails();
    setupMessageListener();
    setupRefreshAll();
    setupFilters();
    setupThemeSwitcher();
    setupAgentCardView();
    setupTemplateButtons();
    setupDrawers();
    setupPeriodicRefresh();
    setupDirectorControls();
    // 延迟渲染指标（等待 collab-workbench 加载数据）
    setTimeout(function () {
      renderRealtimeMetrics();
    }, 1500);
    console.log("[workbench] 独立工作台页面已初始化");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 暴露刷新 Director 状态入口，供 collab-workbench.js 注入引导后调用
  window.Workbench = {
    refreshDirectorState: loadDirectorDetails,
  };
})();
