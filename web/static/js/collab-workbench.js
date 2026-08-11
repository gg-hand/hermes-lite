/* ============================================================
   collab-workbench.js — 协作观察窗逻辑
   目标：将工作台从「Director 控制中心」改造为「透明观察窗口」
        展示 agent 间协作交流，并提供有限的用户操作（广播 + 注入引导）

   核心 API：
   - loadAgents()              加载 agent 列表（GET /api/multiagent/collab/agents）
   - renderCollabMessage(msg)  按类型渲染协作消息（左边框着色）
   - broadcastMessage(content) POST /api/multiagent/collab/broadcast
   - injectDirective(content, rule_type, priority) POST /api/multiagent/collab/directive
   - connectCollabSSE()        连接 /api/multiagent/collab/sse

   暴露：window.CollabWorkbench = { loadAgents, renderCollabMessage,
                                    broadcastMessage, injectDirective,
                                    connectCollabSSE, clearStream }
   ============================================================ */
(function () {
  "use strict";

  var MAX_MESSAGES = 200;             // 最多保留最近 200 条消息
  var SCROLL_THRESHOLD = 80;         // 距底部像素数，决定是否自动滚动
  var COLLAPSE_THRESHOLD = 200;      // 内容超此字符数默认折叠
  var messageCount = 0;

  // 本地 agent_id（工作台页面用于显示，注入主对话逻辑已移至 chat-collab-bridge.js）
  var localAgentId = "";

  /**
   * 加载本地 agent_id（从 /api/multiagent/status）
   * 工作台页面仅用于显示标识，不负责注入主对话。
   */
  function loadLocalAgentId() {
    return fetch("/api/multiagent/status")
      .then(function (resp) {
        if (!resp.ok) return "";
        return resp.json();
      })
      .then(function (data) {
        localAgentId = (data && data.local_agent_id) || "";
        return localAgentId;
      })
      .catch(function () {
        localAgentId = "";
        return "";
      });
  }

  // ========== 工具函数 ==========

  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatTime(ts) {
    if (!ts) return "";
    try {
      var d = ts instanceof Date ? ts : new Date(ts);
      if (isNaN(d.getTime())) return String(ts).substring(11, 19);
      var hh = d.getHours().toString().padStart(2, "0");
      var mm = d.getMinutes().toString().padStart(2, "0");
      var ss = d.getSeconds().toString().padStart(2, "0");
      return hh + ":" + mm + ":" + ss;
    } catch (e) {
      return String(ts).substring(11, 19);
    }
  }

  function showToast(message, level) {
    if (typeof window.showToast === "function") {
      window.showToast(message, level);
      return;
    }
    var toast = document.getElementById("toast");
    if (!toast) {
      console.log("[collab][" + (level || "info") + "] " + message);
      return;
    }
    var lvl = level || "info";
    toast.className = "toast toast-" + lvl;
    toast.textContent = message;
    toast.classList.add("show");
    if (toast._hideTimer) clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(function () {
      toast.classList.remove("show");
      toast._hideTimer = null;
    }, 4000);
  }

  // ========== Agent 列表 ==========

  /**
   * 从 /api/multiagent/collab/agents 加载 agent 列表
   */
  function loadAgents() {
    var container = document.getElementById("cwAgentsList");
    if (!container) return Promise.resolve([]);
    container.innerHTML = '<div class="cw-empty">加载中…</div>';

    return fetch("/api/multiagent/collab/agents")
      .then(function (resp) {
        if (resp.status === 404) {
          container.innerHTML = '<div class="cw-empty">协作未启用</div>';
          return { agents: [] };
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        var agents = (data && data.agents) || [];
        renderAgents(agents);
        return agents;
      })
      .catch(function (err) {
        container.innerHTML = '<div class="cw-empty">加载失败</div>';
        console.warn("collab workbench: loadAgents 失败", err);
        return [];
      });
  }

  function renderAgents(agents) {
    var container = document.getElementById("cwAgentsList");
    var countEl = document.getElementById("cwAgentsCount");
    if (!container) return;
    if (countEl) countEl.textContent = String(agents.length);

    if (!agents || agents.length === 0) {
      container.innerHTML = '<div class="cw-empty">等待 agent 接入</div>';
      return;
    }

    container.innerHTML = agents
      .map(function (agent) {
        var id = escapeHtml(agent.agent_id || "unknown");
        var name = escapeHtml(agent.agent_name || agent.agent_id || "");
        var status = escapeHtml(agent.status || "active");
        var caps = agent.capabilities || [];
        var capText = caps.length > 0 ? escapeHtml(caps.slice(0, 2).join(", ")) : "";
        var extra = caps.length > 2 ? "+" + (caps.length - 2) : "";
        var lastHb = agent.last_heartbeat ? escapeHtml(agent.last_heartbeat) : "";
        var endpoint = agent.endpoint ? escapeHtml(agent.endpoint) : "";
        // title 包含 endpoint，hover 可见 A2A 通信地址
        var titleParts = [name, status];
        if (lastHb) titleParts.push(lastHb);
        if (endpoint) titleParts.push("endpoint: " + endpoint);
        return (
          '<div class="cw-agent-card status-' + status + '" data-agent-id="' + id + '" title="' + titleParts.join(' · ') + '">' +
            '<span class="cw-agent-dot status-' + status + '"></span>' +
            '<div class="cw-agent-info">' +
              '<div class="cw-agent-name">' + (name || id) + '</div>' +
              '<div class="cw-agent-id-line">' + id + (lastHb ? ' · ' + lastHb : '') + '</div>' +
            '</div>' +
            (capText ? '<span class="cw-agent-cap" title="' + escapeHtml(caps.join(", ")) + '">' + capText + (extra ? ' ' + extra : '') + '</span>' : '') +
          '</div>'
        );
      })
      .join('');

    // 点击 agent 卡片 → 按该 agent_id 过滤消息流
    container.querySelectorAll(".cw-agent-card").forEach(function (card) {
      card.addEventListener("click", function () {
        var agentId = card.getAttribute("data-agent-id");
        if (!agentId) return;
        var filterInput = document.getElementById("wbFilterInput");
        var typeFilter = document.getElementById("wbTypeFilter");
        if (filterInput) {
          filterInput.value = agentId;
          filterInput.dispatchEvent(new Event("input", { bubbles: true }));
        }
        // 清空类型过滤，避免组合过滤导致空结果
        if (typeFilter && typeFilter.value) {
          typeFilter.value = "";
          typeFilter.dispatchEvent(new Event("change", { bubbles: true }));
        }
        showToast("已过滤 " + agentId + " 的消息", "info");
      });
    });
  }

  // ========== 消息渲染 ==========

  /**
   * 按消息类型渲染一条协作消息到流区域
   * @param {Object} msg - 协作消息（包含 from/to/type/content/ts 等字段）
   */
  // 已渲染消息的去重键集合（避免 SSE 重连或 HTTP 加载重复渲染）
  // per-collab seq 模型下用 (collab_id, seq) 复合键，避免不同协作的 seq=1 互相吞掉
  var _renderedSeqs = new Set();
  // 古早记录加载状态
  var _streamMinSeq = null;       // 当前已加载的最小 seq

  /** 构造去重复合键：collab_id（缺失视为 global）+ ":" + seq */
  function _dedupKey(msg) {
    var cid = msg.collab_id || "global";
    return cid + ":" + msg.seq;
  }
  var _streamLoadingEarlier = false;
  var _streamNoMoreEarlier = false;

  /** 检查滚动位置，显示/隐藏"跳到最新"按钮 */
  function _updateJumpToLatestBtn() {
    var stream = document.getElementById("cwStream");
    var btn = document.getElementById("cwJumpToLatestBtn");
    if (!stream || !btn) return;
    var distanceToBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight;
    // 距底部超过阈值时显示按钮（用户在查看古早记录）
    btn.hidden = distanceToBottom < SCROLL_THRESHOLD;
  }

  /** 跳到最新消息（滚动到底部） */
  function jumpToLatest() {
    var stream = document.getElementById("cwStream");
    if (!stream) return;
    stream.scrollTop = stream.scrollHeight;
    var btn = document.getElementById("cwJumpToLatestBtn");
    if (btn) btn.hidden = true;
  }

  /**
   * 阶段 1.2：在消息流容器中按 seq 升序找到插入位置（二分查找）。
   * 维护 DOM 子节点按 data-seq 升序排列，避免 SSE 与历史加载并发到达导致旧消息堆在新消息下方。
   * - seq 为 null 时追加到末尾（无法定位顺序）。
   * - 子节点无 data-seq（如顶部 loading/end 占位元素）视为最小值，保持置于顶部。
   * @param {HTMLElement} stream - 消息流容器（cwStream）
   * @param {number|null} seq - 待插入消息的 seq
   * @returns {number} 插入索引（0..stream.children.length）
   */
  function _findInsertIndex(stream, timestamp) {
    var children = stream.children;
    var len = children.length;
    if (!timestamp) return len;
    var lo = 0, hi = len;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      var midTs = children[mid].dataset.timestamp || "";
      // ISO 时间戳字典序与时间序一致，空值视为最小（占位元素保留顶部）
      if (midTs <= timestamp) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    return lo;
  }

  function renderCollabMessage(msg) {
    var stream = document.getElementById("cwStream");
    if (!stream) return;

    // 去重：per-collab seq 模型下用 (collab_id, seq) 复合键
    var seq = msg.seq;
    if (seq != null) {
      var dk = _dedupKey(msg);
      if (_renderedSeqs.has(dk)) return;
      _renderedSeqs.add(dk);
      // 更新最小 seq（用于古早记录分页游标，粗略）
      if (_streamMinSeq === null || seq < _streamMinSeq) {
        _streamMinSeq = seq;
      }
    }

    // 移除占位空状态
    var empty = stream.querySelector(".cw-empty");
    if (empty) empty.remove();

    var item = _buildMessageElement(msg);
    if (!item) return;

    // 按 timestamp 二分插入（per-collab seq 不全局连续，用时间戳保证时序）
    var insertIdx = _findInsertIndex(stream, msg.timestamp);
    stream.insertBefore(item, stream.children[insertIdx] || null);

    // 限制最大消息数：移除最早的消息
    while (stream.children.length > MAX_MESSAGES) {
      stream.removeChild(stream.firstChild);
    }

    // 更新计数
    messageCount = stream.children.length;
    var countEl = document.getElementById("cwStreamCount");
    if (countEl) countEl.textContent = String(messageCount);

    // 自动滚动到底部（如果用户已在底部附近）
    _autoScroll(stream);
  }

  /**
   * 构造消息 DOM 元素（不 append 到流，供 renderCollabMessage 和古早加载 prepend 复用）
   * @param {Object} msg - 协作消息
   * @returns {HTMLElement|null} 构造好的元素，或 null（seq 重复时）
   */
  function _buildMessageElement(msg) {
    var type = msg.type || "status";
    var from = msg.from || "?";
    var to = msg.to || "";
    var content = msg.content || "";
    var ts = formatTime(msg.ts || msg.timestamp || "");

    // 阶段 4.1：按 collab_id 归档（缺失视为 global），便于 CSS 分隔或后续过滤查询
    var collabId = msg.collab_id || "global";

    // 阶段 4.3：from → to 展示（to 为 "*" 显示"所有人"，空则不显示箭头）
    var toLabel = "";
    if (to === "*") toLabel = "所有人";
    else if (to) toLabel = to;

    // P2-5：头像徽章首字母（director→D，teagent-lu→L，teagent-liu-2→2，user→U，其他取首字母大写）
    var avatarLetter = _avatarLetter(from);

    // P2-6：轮次徽章（collab_round 字段）
    var roundBadge = "";
    if (msg.collab_round != null && typeof msg.collab_round === "number") {
      roundBadge = '<span class="cw-msg-round" title="协作轮次">R' + msg.collab_round + "</span>";
    }

    // 主会话协作标识（channel=main_session）：工作台以明显徽章区分两平面
    var channelBadge = "";
    var isMainCollab = msg.channel === "main_session";
    if (isMainCollab) {
      channelBadge = '<span class="cw-msg-channel cw-msg-channel--main" title="主会话协作（独立 A2A 通道）">主会话协作</span>';
    }

    // 构造额外信息（rule_type/priority/via/action 等）
    var extras = [];
    if (msg.rule_type) extras.push('<span class="cw-msg-extra-item"><b>rule:</b> ' + escapeHtml(msg.rule_type) + "</span>");
    if (msg.priority) extras.push('<span class="cw-msg-extra-item"><b>priority:</b> ' + escapeHtml(msg.priority) + "</span>");
    if (msg.action) extras.push('<span class="cw-msg-extra-item"><b>action:</b> ' + escapeHtml(msg.action) + "</span>");
    if (msg.via) extras.push('<span class="cw-msg-extra-item"><b>via:</b> ' + escapeHtml(msg.via) + "</span>");
    // A2A 归档消息显示归档方（forwarded_by），体现协作透明度
    if (msg.forwarded_by && msg.forwarded_by !== from) {
      extras.push('<span class="cw-msg-extra-item"><b>forwarded_by:</b> ' + escapeHtml(msg.forwarded_by) + "</span>");
    }
    // message_id 仅在 relay 类型显示（去重关键信息，其他类型无需展示）
    if (msg.message_id && type === "relay") {
      extras.push('<span class="cw-msg-extra-item cw-msg-extra-meta" title="message_id: ' + escapeHtml(msg.message_id) + '"><b>id:</b> ' + escapeHtml(msg.message_id) + "</span>");
    }

    // 阶段 4.2：状态徽章（consensus → 已共识；end → 已结束；request → 进行中）
    var statusBadge = "";
    if (type === "consensus") {
      statusBadge = '<span class="cw-msg-status-badge cw-status-ended">已共识</span>';
    } else if (type === "end") {
      statusBadge = '<span class="cw-msg-status-badge cw-status-ended">已结束</span>';
    } else if (type === "request") {
      statusBadge = '<span class="cw-msg-status-badge cw-status-active">进行中</span>';
    }

    // P2-7：长内容折叠判定
    var isLong = content.length > COLLAPSE_THRESHOLD;
    var contentClass = "cw-msg-content" + (isLong ? " cw-msg-content-collapsed" : "");
    var expandBtn = isLong
      ? '<button class="cw-msg-expand-btn" type="button">展开 (' + content.length + " 字)</button>"
      : "";

    var item = document.createElement("div");
    item.className = "cw-msg type-" + escapeHtml(type) + " from-" + escapeHtml(from);
    item.dataset.from = from;
    item.dataset.type = type;
    item.dataset.seq = (msg.seq != null) ? String(msg.seq) : "";
    item.dataset.timestamp = msg.timestamp || "";
    item.dataset.messageId = msg.message_id || "";
    item.dataset.collabId = collabId;
    item.dataset.channel = msg.channel || "";
    // 阶段 4.2：在元素上叠加状态 class，便于 CSS 高亮整条消息
    if (type === "consensus" || type === "end") {
      item.classList.add("cw-status-ended");
    } else if (type === "request") {
      item.classList.add("cw-status-active");
    }
    // 主会话协作标识：叠加 channel-main class 便于 CSS 整体高亮
    if (isMainCollab) {
      item.classList.add("cw-channel-main");
    }
    item.innerHTML =
      '<div class="cw-msg-meta">' +
        '<span class="cw-msg-avatar" title="' + escapeHtml(from) + '">' + escapeHtml(avatarLetter) + "</span>" +
        '<span class="cw-msg-from">' + escapeHtml(from) +
          (toLabel ? ' <span class="cw-msg-arrow">→</span> <span class="cw-msg-to">' + escapeHtml(toLabel) + "</span>" : "") +
        "</span>" +
        '<span class="cw-msg-type">' + escapeHtml(type) + "</span>" +
        roundBadge +
        channelBadge +
        statusBadge +
        (ts ? '<span class="cw-msg-time">' + ts + "</span>" : "") +
      "</div>" +
      '<div class="' + contentClass + '">' + (typeof renderMarkdown === "function" ? renderMarkdown(content) : escapeHtml(content)) + "</div>" +
      expandBtn +
      (extras.length ? '<div class="cw-msg-extra">' + extras.join("") + "</div>" : "");

    // P2-7：绑定展开按钮点击事件
    if (isLong) {
      var btn = item.querySelector(".cw-msg-expand-btn");
      if (btn) {
        btn.addEventListener("click", function () {
          var expanded = item.classList.toggle("cw-msg-expanded");
          btn.textContent = expanded ? "收起" : "展开 (" + content.length + " 字)";
        });
      }
    }
    return item;
  }

  /** P2-5：根据 from 计算头像徽章首字母 */
  function _avatarLetter(from) {
    if (!from) return "?";
    var f = String(from);
    if (f === "director") return "D";
    if (f === "user") return "U";
    if (f === "teagent-lu") return "L";
    if (f === "teagent-liu-2") return "2";
    // 其他 teagent-* 取末尾段首字母
    var m = f.match(/teagent-([a-z0-9]+)/i);
    if (m) return m[1].charAt(0).toUpperCase();
    return f.charAt(0).toUpperCase();
  }

  // ========== 活动档案时间线 ==========

  /** 时间线已渲染事件（按 ts 倒序）+ 分页游标 */
  var _activityEvents = [];
  var _activityNextBeforeTs = null;
  var _activityLoading = false;

  /** 时间分组标签 */
  function _activityGroupLabel(ts) {
    if (!ts) return "更早";
    try {
      var d = new Date(ts);
      var now = new Date();
      var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      var yesterday = new Date(today.getTime() - 86400000);
      var weekAgo = new Date(today.getTime() - 7 * 86400000);
      if (d >= today) return "今天";
      if (d >= yesterday) return "昨天";
      if (d >= weekAgo) return "本周";
      return "更早";
    } catch (e) {
      return "更早";
    }
  }

  /** 相对时间格式化（"3 分钟前" / "2 小时前" / "1 天前"） */
  function _relativeTime(ts) {
    if (!ts) return "";
    try {
      var d = new Date(ts);
      var diff = (Date.now() - d.getTime()) / 1000;
      if (diff < 60) return "刚刚";
      if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
      if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
      if (diff < 7 * 86400) return Math.floor(diff / 86400) + " 天前";
      return d.toLocaleDateString();
    } catch (e) {
      return "";
    }
  }

  /** 事件类型图标映射 */
  var _activityIcons = {
    agent_online: "↑",
    agent_offline: "↓",
    director_broadcast: "📡",
    user_broadcast: "📡",  // 兼容历史数据
    directive_injected: "⚡",
    a2a_relay: "🔄",
  };

  /**
   * 加载活动档案时间线
   * @param {boolean} append - true=追加更早数据；false=重新加载
   */
  function loadActivityTimeline(append) {
    var container = document.getElementById("cwActivityTimeline");
    if (!container || _activityLoading) return Promise.resolve([]);
    _activityLoading = true;

    if (!append) {
      _activityEvents = [];
      _activityNextBeforeTs = null;
      container.innerHTML = '<div class="cw-activity-loading">加载中…</div>';
    }

    var url = "/api/multiagent/collab/events?limit=50";
    if (_activityNextBeforeTs) {
      url += "&before_ts=" + encodeURIComponent(_activityNextBeforeTs);
    }

    return fetch(url)
      .then(function (resp) {
        if (resp.status === 404) return { events: [], next_before_ts: null };
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        var events = (data && data.events) || [];
        _activityNextBeforeTs = (data && data.next_before_ts) || null;
        if (append) {
          _activityEvents = _activityEvents.concat(events);
        } else {
          _activityEvents = events;
        }
        renderActivityTimeline();
        return events;
      })
      .catch(function (err) {
        console.warn("loadActivityTimeline 失败:", err);
        container.innerHTML = '<div class="cw-empty">加载失败</div>';
        return [];
      })
      .finally(function () {
        _activityLoading = false;
      });
  }

  /** 渲染活动档案时间线（按时间分组） */
  function renderActivityTimeline() {
    var container = document.getElementById("cwActivityTimeline");
    var countEl = document.getElementById("cwActivityCount");
    if (!container) return;
    if (countEl) countEl.textContent = String(_activityEvents.length);

    if (!_activityEvents || _activityEvents.length === 0) {
      container.innerHTML = '<div class="cw-empty">暂无活动</div>';
      return;
    }

    // 按时间分组
    var groups = {};
    var groupOrder = ["今天", "昨天", "本周", "更早"];
    _activityEvents.forEach(function (e) {
      var label = _activityGroupLabel(e.ts);
      if (!groups[label]) groups[label] = [];
      groups[label].push(e);
    });

    var html = "";
    groupOrder.forEach(function (label) {
      if (!groups[label]) return;
      html += '<div class="cw-activity-group">';
      html += '<div class="cw-activity-group-title">' + label + '</div>';
      groups[label].forEach(function (e) {
        var icon = _activityIcons[e.event_type] || "•";
        var summary = escapeHtml(e.summary || "");
        var agent = escapeHtml(e.agent_id || "");
        var relTime = escapeHtml(_relativeTime(e.ts));
        var seq = e.seq || 0;
        var mid = e.message_id || "";
        html +=
          '<div class="cw-activity-item" data-seq="' + seq + '" data-message-id="' + escapeHtml(mid) + '" title="点击跳转到对应消息">' +
            '<span class="cw-activity-icon type-' + escapeHtml(e.event_type) + '">' + icon + '</span>' +
            '<div class="cw-activity-body">' +
              '<div class="cw-activity-summary">' + summary + '</div>' +
              '<div class="cw-activity-meta">' +
                '<span class="cw-activity-agent">' + agent + '</span>' +
                '<span class="cw-activity-time">' + relTime + '</span>' +
              '</div>' +
            '</div>' +
          '</div>';
      });
      html += '</div>';
    });

    // 加载更多按钮
    if (_activityNextBeforeTs) {
      html += '<div class="cw-activity-load-more">';
      html += '<button id="cwActivityLoadMoreBtn">加载更早</button>';
      html += '</div>';
    }

    container.innerHTML = html;

    // 绑定点击事件
    container.querySelectorAll(".cw-activity-item").forEach(function (item) {
      item.addEventListener("click", function () {
        var mid = item.getAttribute("data-message-id");
        if (mid) {
          jumpToMessage(mid);
        } else {
          var seq = parseInt(item.getAttribute("data-seq"), 10);
          if (seq > 0) jumpToMessage(seq);
        }
      });
    });
    var loadMoreBtn = document.getElementById("cwActivityLoadMoreBtn");
    if (loadMoreBtn) {
      loadMoreBtn.addEventListener("click", function () {
        loadActivityTimeline(true);
      });
    }
  }

  /**
   * 跳转到中栏消息流的指定 seq 位置
   * 若消息不在当前 DOM（古早记录未加载），先加载该 seq 之前的消息
   * @param {number} seq - 目标消息 seq
   */
  function jumpToMessage(key) {
    var stream = document.getElementById("cwStream");
    if (!stream) return;

    // key 可以是 message_id（字符串）或 seq（数字）
    var isMid = typeof key === "string" && key.indexOf(":") < 0 && isNaN(parseInt(key, 10));
    var selector;
    if (isMid) {
      selector = '.cw-msg[data-message-id="' + key + '"]';
    } else {
      selector = '.cw-msg[data-seq="' + key + '"]';
    }

    // 先尝试在现有 DOM 中查找
    var target = stream.querySelector(selector);
    if (target) {
      _scrollAndFlash(target);
      return;
    }

    // 不在 DOM 中：重新加载消息流
    stream.innerHTML = '<div class="cw-empty">加载消息…</div>';
    _renderedSeqs.clear();
    _streamMinSeq = null;
    _streamNoMoreEarlier = false;
    _streamLoadingEarlier = false;
    var url = "/api/multiagent/collab/messages?limit=50";
    fetch(url)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        var messages = (data && data.messages) || [];
        // 移除占位
        var empty = stream.querySelector(".cw-empty");
        if (empty) empty.remove();
        messages.forEach(function (msg) {
          renderCollabMessage(msg);
          // 派发事件以更新 workbench.js 的 allMessages
          window.dispatchEvent(new CustomEvent("collab-message", { detail: msg }));
        });
        // 加载完成后定位
        setTimeout(function () {
          var target2 = stream.querySelector(selector);
          if (target2) _scrollAndFlash(target2);
        }, 50);
      })
      .catch(function (err) {
        console.warn("jumpToMessage 加载失败:", err);
        showToast("跳转失败: " + err.message, "error");
      });
  }

  /** 滚动到目标元素 + 闪烁高亮 2 秒 */
  function _scrollAndFlash(el) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.remove("cw-msg-flash");
    // 触发重绘
    void el.offsetWidth;
    el.classList.add("cw-msg-flash");
    setTimeout(function () {
      el.classList.remove("cw-msg-flash");
    }, 2000);
  }


  function _autoScroll(stream) {
    // 距底部小于阈值时，自动滚动
    var distanceToBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight;
    if (distanceToBottom < SCROLL_THRESHOLD) {
      stream.scrollTop = stream.scrollHeight;
    }
  }

  /**
   * 清空消息流显示（不影响后端数据）
   */
  function clearStream() {
    var stream = document.getElementById("cwStream");
    if (!stream) return;
    stream.innerHTML = '<div class="cw-empty">已清空，等待新消息…</div>';
    messageCount = 0;
    _renderedSeqs.clear();  // 清空去重集合，允许重新接收消息
    _streamMinSeq = null;
    _streamLoadingEarlier = false;
    _streamNoMoreEarlier = false;
    var countEl = document.getElementById("cwStreamCount");
    if (countEl) countEl.textContent = "0";
  }

  // ========== 古早记录加载（滚动到顶部自动 prepend） ==========

  /** 初始化消息流滚动加载器：滚动到顶部时自动加载更早消息 */
  function setupStreamScrollLoader() {
    var stream = document.getElementById("cwStream");
    if (!stream) return;
    stream.addEventListener("scroll", function () {
      // 滚动到顶部（scrollTop < 50px）且未在加载且未到尽头
      if (stream.scrollTop < 50 && !_streamLoadingEarlier && !_streamNoMoreEarlier) {
        _loadEarlierMessages();
      }
      // 更新"跳到最新"按钮显隐
      _updateJumpToLatestBtn();
    });
    // 绑定"跳到最新"按钮点击
    var jumpBtn = document.getElementById("cwJumpToLatestBtn");
    if (jumpBtn) {
      jumpBtn.addEventListener("click", jumpToLatest);
    }
  }

  /** 加载更早的消息并 prepend 到流顶部 */
  function _loadEarlierMessages() {
    var stream = document.getElementById("cwStream");
    if (!stream) return;
    if (_streamMinSeq === null || _streamMinSeq <= 1) {
      _streamNoMoreEarlier = true;
      return;
    }

    _streamLoadingEarlier = true;
    // 在顶部插入 loading 提示
    var loadingEl = document.createElement("div");
    loadingEl.className = "cw-stream-loading-top";
    loadingEl.textContent = "加载更早消息…";
    stream.insertBefore(loadingEl, stream.firstChild);

    var beforeSeq = _streamMinSeq;
    var url = "/api/multiagent/collab/messages?before_seq=" + beforeSeq + "&limit=50";

    fetch(url)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        var messages = (data && data.messages) || [];
        loadingEl.remove();

        if (messages.length === 0) {
          _streamNoMoreEarlier = true;
          // 显示「已到最早」提示（5 秒后消失）
          var endEl = document.createElement("div");
          endEl.className = "cw-stream-loading-top cw-stream-end";
          endEl.textContent = "已到最早消息";
          stream.insertBefore(endEl, stream.firstChild);
          setTimeout(function () { if (endEl.parentNode) endEl.remove(); }, 5000);
          return;
        }

        // 保留滚动位置：记录原 scrollHeight
        var prevScrollHeight = stream.scrollHeight;
        var prevScrollTop = stream.scrollTop;

        // 移除占位空状态
        var empty = stream.querySelector(".cw-empty");
        if (empty) empty.remove();

        // prepend 消息（按 seq 升序插入到顶部，最早在最上）
        // messages 已按 seq 升序，从最后一条往前 insertBefore 保持顺序
        var firstChild = stream.firstChild;
        var newlyAdded = [];
        for (var i = messages.length - 1; i >= 0; i--) {
          var msg = messages[i];
          var seq = msg.seq;
          // 去重：per-collab 复合键，已渲染的跳过
          if (seq != null) {
            var dk = _dedupKey(msg);
            if (_renderedSeqs.has(dk)) continue;
            _renderedSeqs.add(dk);
            if (_streamMinSeq === null || seq < _streamMinSeq) {
              _streamMinSeq = seq;
            }
          }
          var item = _buildMessageElement(msg);
          if (item) {
            stream.insertBefore(item, firstChild);
            firstChild = item;
            newlyAdded.push(msg);
          }
        }
        // 派发事件以更新 workbench.js 的 allMessages（实时指标数据源）
        // 注意：renderCollabMessage 内部 seq 去重会阻止重复渲染，仅更新 allMessages
        newlyAdded.forEach(function (msg) {
          window.dispatchEvent(new CustomEvent("collab-message", { detail: msg }));
        });

        // 恢复滚动位置（保持在原内容顶部）
        var newScrollHeight = stream.scrollHeight;
        stream.scrollTop = prevScrollTop + (newScrollHeight - prevScrollHeight);

        // 更新计数
        messageCount = stream.children.length;
        var countEl = document.getElementById("cwStreamCount");
        if (countEl) countEl.textContent = String(messageCount);
      })
      .catch(function (err) {
        console.warn("加载更早消息失败:", err);
        if (loadingEl.parentNode) loadingEl.remove();
      })
      .finally(function () {
        _streamLoadingEarlier = false;
      });
  }

  // ========== 协作档案侧栏（P2-8） ==========

  /** 当前选中的 collab_id（null=全部聚合视图） */
  var _selectedCollabId = null;

  /**
   * 加载协作索引列表（GET /api/multiagent/collab/collabs），按 active/archived 分组渲染。
   * active 在前，archived 灰色折叠（默认展开 active，折叠 archived）。
   */
  function loadCollabs() {
    var container = document.getElementById("cwCollabsList");
    if (!container) return Promise.resolve([]);
    container.innerHTML = '<div class="cw-empty">加载中…</div>';

    return fetch("/api/multiagent/collab/collabs")
      .then(function (resp) {
        if (resp.status === 404) {
          container.innerHTML = '<div class="cw-empty">协作未启用</div>';
          return { collabs: [] };
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        var collabs = (data && data.collabs) || [];
        renderCollabs(collabs);
        return collabs;
      })
      .catch(function (err) {
        container.innerHTML = '<div class="cw-empty">加载失败</div>';
        console.warn("loadCollabs 失败:", err);
        return [];
      });
  }

  /** 渲染协作列表：active 在前，archived 灰色折叠 */
  function renderCollabs(collabs) {
    var container = document.getElementById("cwCollabsList");
    var countEl = document.getElementById("cwCollabsCount");
    if (!container) return;
    if (countEl) countEl.textContent = String(collabs.length);

    if (!collabs || collabs.length === 0) {
      container.innerHTML = '<div class="cw-empty">暂无协作</div>';
      return;
    }

    // 分组：active 在前，archived 在后
    var active = collabs.filter(function (c) { return (c.status || "active") !== "archived"; });
    var archived = collabs.filter(function (c) { return c.status === "archived"; });

    var html = "";

    // 「全部消息」入口（取消过滤）
    html +=
      '<div class="cw-collab-item' + (_selectedCollabId === null ? " selected" : "") +
      '" data-collab-id="" title="显示所有协作 + 全局消息">' +
        '<span class="cw-collab-dot" style="background:var(--accent)"></span>' +
        '<div class="cw-collab-info">' +
          '<div class="cw-collab-title">全部消息</div>' +
          '<div class="cw-collab-meta"><span class="cw-collab-status">聚合视图</span></div>' +
        '</div>' +
      '</div>';

    // 活动协作分组
    if (active.length > 0) {
      html += _renderCollabGroup("活动中", active, false);
    }
    // 归档协作分组（默认折叠）
    if (archived.length > 0) {
      html += _renderCollabGroup("已归档", archived, true);
    }

    container.innerHTML = html;

    // 绑定分组标题点击（展开/折叠）
    container.querySelectorAll(".cw-collab-group-title").forEach(function (title) {
      title.addEventListener("click", function () {
        var body = title.nextElementSibling;
        var collapsed = title.classList.toggle("collapsed");
        if (body) body.hidden = collapsed;
      });
    });

    // 绑定协作条目点击 → 加载该协作消息流
    container.querySelectorAll(".cw-collab-item").forEach(function (item) {
      item.addEventListener("click", function () {
        var cid = item.getAttribute("data-collab-id") || "";
        _selectedCollabId = cid || null;
        // 更新选中态
        container.querySelectorAll(".cw-collab-item").forEach(function (el) {
          el.classList.remove("selected");
        });
        item.classList.add("selected");
        _loadCollabMessages(_selectedCollabId);
      });
    });
  }

  /** 渲染一个分组（标题 + 条目列表） */
  function _renderCollabGroup(label, items, collapsed) {
    var html = '<div class="cw-collab-group-title' + (collapsed ? " collapsed" : "") + '">';
    html += '<span class="cw-collab-group-caret">▾</span>';
    html += '<span>' + label + '</span>';
    html += '<span class="cw-collab-group-count">' + items.length + '</span>';
    html += '</div>';
    html += '<div class="cw-collab-group-body"' + (collapsed ? ' hidden' : '') + '>';
    items.forEach(function (c) {
      var cid = escapeHtml(c.collab_id || "");
      var title = escapeHtml(c.title || c.collab_id || "未命名协作");
      var status = escapeHtml(c.status || "active");
      var participants = c.participants || [];
      var pCount = participants.length;
      var selected = (_selectedCollabId === c.collab_id) ? " selected" : "";
      var metaParts = ['<span class="cw-collab-status">' + status + '</span>'];
      if (pCount > 0) metaParts.push('<span>' + pCount + ' 人</span>');
      html +=
        '<div class="cw-collab-item status-' + status + selected +
        '" data-collab-id="' + cid + '" title="' + title + ' · ' + cid + '">' +
          '<span class="cw-collab-dot"></span>' +
          '<div class="cw-collab-info">' +
            '<div class="cw-collab-title">' + title + '</div>' +
            '<div class="cw-collab-meta">' + metaParts.join("") + '</div>' +
          '</div>' +
        '</div>';
    });
    html += '</div>';
    return html;
  }

  /**
   * 加载指定协作的消息到流（collab_id=null 时加载全部聚合）
   * @param {string|null} collabId
   */
  function _loadCollabMessages(collabId) {
    var stream = document.getElementById("cwStream");
    if (!stream) return;
    stream.innerHTML = '<div class="cw-empty">加载消息…</div>';
    _renderedSeqs.clear();
    _streamMinSeq = null;
    _streamNoMoreEarlier = false;
    _streamLoadingEarlier = false;

    var url = "/api/multiagent/collab/messages?limit=100";
    if (collabId) {
      url += "&collab_id=" + encodeURIComponent(collabId);
    }
    fetch(url)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        var messages = (data && data.messages) || [];
        var empty = stream.querySelector(".cw-empty");
        if (empty) empty.remove();
        if (messages.length === 0) {
          stream.innerHTML = '<div class="cw-empty">' + (collabId ? "该协作暂无消息" : "暂无消息") + '</div>';
        } else {
          messages.forEach(function (msg) {
            renderCollabMessage(msg);
            window.dispatchEvent(new CustomEvent("collab-message", { detail: msg }));
          });
        }
        showToast(collabId ? "已切换到协作 " + collabId : "已切换到全部消息", "info");
      })
      .catch(function (err) {
        console.warn("_loadCollabMessages 失败:", err);
        stream.innerHTML = '<div class="cw-empty">加载失败</div>';
      });
  }

  // ========== 用户操作 ==========

  /**
   * 发布广播（POST /api/multiagent/collab/broadcast）
   * @param {string} content - 广播内容
   * @param {boolean} startCollab - true 发起新协作；false 追加到现有协作
   * @param {string} [collabId] - 追加模式下的目标协作 ID
   */
  function broadcastMessage(content, startCollab, collabId) {
    var payload = { content: content };
    if (startCollab) {
      payload.start_collab = true;
    } else if (collabId) {
      payload.collab_id = collabId;
    } else {
      // 无 start_collab 也无 collab_id：后端会拒绝。兜底发起新协作。
      payload.start_collab = true;
    }
    return fetch("/api/multiagent/collab/broadcast", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (resp) {
        if (resp.status === 404) {
          throw new Error("协作未启用，请在设置中开启 multiagent");
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (data.ok) {
          showToast(
            startCollab ? "新协作已发起" : "广播已追加到协作",
            "success"
          );
          return data;
        }
        throw new Error(data.detail || "广播失败");
      })
      .catch(function (err) {
        showToast("广播失败：" + err.message, "error");
        throw err;
      });
  }

  /**
   * 加载 active 协作列表到广播模态框的选择器。
   * 打开广播模态框时调用，让用户可选择追加到进行中的协作。
   */
  function loadActiveCollabsIntoSelect() {
    var select = document.getElementById("cwBroadcastCollabSelect");
    if (!select) return;
    fetch("/api/multiagent/collab/collabs")
      .then(function (resp) {
        if (!resp.ok) return null;
        return resp.json();
      })
      .then(function (data) {
        if (!data || !data.collabs) return;
        // 保留首项 "🆕 发起新协作"，清除旧的可选项
        while (select.options.length > 1) {
          select.remove(1);
        }
        data.collabs
          .filter(function (c) { return c.status !== "archived"; })
          .forEach(function (c) {
            var opt = document.createElement("option");
            opt.value = c.collab_id;
            var title = c.title || c.collab_id;
            // 主会话协作（main_*）对工作台只读：不可作为广播追加目标
            var isMain = String(c.collab_id || "").indexOf("main_") === 0;
            opt.textContent = (isMain ? "👁 " : "💬 ") + title + (isMain ? "（只读）" : "");
            if (isMain) {
              opt.disabled = true;
              opt.title = "主会话协作为只读观察对象，不可广播写入";
            }
            select.appendChild(opt);
          });
      })
      .catch(function () { /* 静默失败，保留 "发起新协作" 默认项 */ });
  }

  /**
   * 注入引导指令（POST /api/multiagent/collab/directive）
   * @param {string} content - 指令内容
   * @param {string} ruleType - 规则类型（ordering/priority/constraint/hint）
   * @param {string} priority - 优先级（normal/high/critical）
   */
  function injectDirective(content, ruleType, priority) {
    return fetch("/api/multiagent/collab/directive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: content,
        rule_type: ruleType || "ordering",
        priority: priority || "normal",
      }),
    })
      .then(function (resp) {
        if (resp.status === 404) {
          throw new Error("协作未启用，请在设置中开启 multiagent");
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (data.ok) {
          showToast("指令已注入", "success");
          // 注入引导后切换 Director 状态显示为瞬时 "injected"
          setDirectorState("injected");
          // 3 秒后触发一次状态拉取，让真实后端状态接管显示
          // 避免硬编码 "observing" 覆盖后端的 healthy/degraded/autonomous/fault
          setTimeout(function () {
            if (window.Workbench && window.Workbench.refreshDirectorState) {
              window.Workbench.refreshDirectorState();
            }
          }, 3000);
          return data;
        }
        throw new Error(data.detail || "注入失败");
      })
      .catch(function (err) {
        showToast("注入失败：" + err.message, "error");
        throw err;
      });
  }

  /**
   * 设置 Director 状态显示
   * @param {string} state - stopped/starting/crashed/healthy/degraded/autonomous/fault/unknown（后端）
   *                        或 observing/injected/disabled（前端独有）
   * @param {string} [reason] - 故障/异常原因，作为 tooltip 展示（可选）
   */
  function setDirectorState(state, reason) {
    var el = document.getElementById("cwDirectorState");
    if (!el) return;
    var dot = el.querySelector(".cw-director-dot");
    var text = el.querySelector(".cw-director-text");
    var labels = {
      stopped: "已休眠",
      starting: "启动中",
      crashed: "已崩溃",
      healthy: "运行中",
      degraded: "降级",
      autonomous: "自治中",
      fault: "故障",
      unknown: "未知",
      observing: "观察中",
      injected: "已注入引导",
      disabled: "未启用",
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
    el.className = "cw-director-state state-" + state;
    if (text) text.textContent = labels[state] || state;
    // dot 由 CSS 着色，无需手动设置
    // 设置 tooltip：故障原因优先，无原因时用默认说明
    el.title = titleMap[state] || state;
  }

  // ========== SSE 连接 ==========

  /**
   * 连接协作消息 SSE 通道
   */
  function connectCollabSSE() {
    if (window.CollabSSE && typeof window.CollabSSE.start === "function") {
      window.CollabSSE.start();
    }
  }

  // ========== 加载已有消息 ==========

  /**
   * 加载历史协作消息（首次进入时填充流）
   * 派发 collab-message 事件以更新 workbench.js 的 allMessages（实时指标数据源）
   * renderCollabMessage 内部有 seq 去重，事件触发不会导致重复渲染
   */
  function loadRecentMessages() {
    return fetch("/api/multiagent/collab/messages?limit=50")
      .then(function (resp) {
        if (resp.status === 404) return { messages: [] };
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        var messages = (data && data.messages) || [];
        messages.forEach(function (msg) {
          // 直接调用 renderCollabMessage 渲染
          renderCollabMessage(msg);
          // 派发事件以更新 workbench.js 的 allMessages（实时指标数据源）
          window.dispatchEvent(new CustomEvent("collab-message", { detail: msg }));
        });
        return messages;
      })
      .catch(function (err) {
        console.warn("collab workbench: loadRecentMessages 失败", err);
        return [];
      });
  }

  // ========== 模态框逻辑 ==========

  function _bindModal(modalId, openBtnId, closeBtnIds, submitBtnId, inputIds, onSubmit) {
    var modal = document.getElementById(modalId);
    var openBtn = document.getElementById(openBtnId);
    var submitBtn = document.getElementById(submitBtnId);
    if (!modal || !openBtn || !submitBtn) return;

    var inputs = inputIds.map(function (id) { return document.getElementById(id); });

    function open() {
      modal.hidden = false;
      // 重置输入
      inputs.forEach(function (el) {
        if (!el) return;
        if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") el.value = "";
        if (el.tagName === "SELECT") el.selectedIndex = 0;
      });
      submitBtn.disabled = true;
      // 聚焦第一个输入
      if (inputs[0]) {
        setTimeout(function () { inputs[0].focus(); }, 50);
      }
    }
    function close() {
      modal.hidden = true;
    }
    function checkInputs() {
      var allFilled = inputs.every(function (el) {
        if (!el) return true;
        if (el.tagName === "SELECT") return true; // select 有默认值
        return el.value && el.value.trim();
      });
      submitBtn.disabled = !allFilled;
    }

    openBtn.addEventListener("click", open);
    // 兼容旧写法：优先按 ID 绑定；若找不到则降级为 data-close 元素
    closeBtnIds.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) {
        el.addEventListener("click", close);
      }
    });
    modal.querySelectorAll("[data-close]").forEach(function (el) {
      el.addEventListener("click", close);
    });

    inputs.forEach(function (el) {
      if (!el) return;
      var evt = (el.tagName === "SELECT") ? "change" : "input";
      el.addEventListener(evt, checkInputs);
    });

    submitBtn.addEventListener("click", function () {
      var values = inputs.map(function (el) { return el ? el.value.trim() : ""; });
      // 简单禁用避免重复点击
      submitBtn.disabled = true;
      var originalText = submitBtn.textContent;
      submitBtn.textContent = "提交中…";
      Promise.resolve()
        .then(function () { return onSubmit(values); })
        .then(function () { close(); })
        .catch(function () { /* 错误已在 broadcastMessage/injectDirective 内提示 */ })
        .finally(function () {
          submitBtn.disabled = false;
          submitBtn.textContent = originalText;
        });
    });
  }

  // ========== 事件绑定 ==========

  function bindEvents() {
    // 刷新 agent 列表
    var refreshBtn = document.getElementById("cwRefreshAgentsBtn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", loadAgents);
    }

    // 刷新协作档案列表（P2-8）
    var refreshCollabsBtn = document.getElementById("cwRefreshCollabsBtn");
    if (refreshCollabsBtn) {
      refreshCollabsBtn.addEventListener("click", loadCollabs);
    }

    // 刷新活动档案
    var refreshActivityBtn = document.getElementById("cwRefreshActivityBtn");
    if (refreshActivityBtn) {
      refreshActivityBtn.addEventListener("click", function () {
        loadActivityTimeline(false);
      });
    }

    // 清空消息流
    var clearBtn = document.getElementById("cwClearStreamBtn");
    if (clearBtn) {
      clearBtn.addEventListener("click", clearStream);
    }

    // 收起按钮（仅 chat 内嵌模式有，独立工作台页面无此按钮）
    var collapseBtn = document.getElementById("cwCollapseBtn");
    if (collapseBtn) {
      collapseBtn.addEventListener("click", function () {
        var main = document.querySelector("main.main");
        var pane = document.getElementById("collabPane");
        var btnDirector = document.getElementById("btnDirector");
        if (main && pane) {
          main.classList.remove("director-on");
          pane.hidden = true;
          if (btnDirector) btnDirector.classList.remove("active");
        }
        // 收起时停止 SSE 节省连接
        if (window.CollabSSE && typeof window.CollabSSE.stop === "function") {
          window.CollabSSE.stop();
        }
      });
    }

    // 监听 #collabPane 显示/隐藏，自动启动/停止 SSE（仅 chat 内嵌模式）
    var pane = document.getElementById("collabPane");
    if (pane && typeof MutationObserver !== "undefined") {
      var observer = new MutationObserver(function () {
        if (!pane.hidden) {
          // 显示时：加载数据 + 启动 SSE
          loadAgents();
          loadCollabs();
          loadActivityTimeline(false);
          // 阶段 1.2：先完成历史加载再启动 SSE，避免顺序错乱
          loadRecentMessages().then(function () {
            connectCollabSSE();
          });
        } else {
          // 隐藏时：停止 SSE
          if (window.CollabSSE && typeof window.CollabSSE.stop === "function") {
            window.CollabSSE.stop();
          }
        }
      });
      observer.observe(pane, { attributes: true, attributeFilter: ["hidden"] });
    }

    // 监听协作归档事件：当后端归档协作时刷新侧栏列表（P2-8）
    window.addEventListener("collab-archived", function () {
      loadCollabs();
    });

    // 监听协作消息事件（渲染到工作台消息流）
    window.addEventListener("collab-message", function (e) {
      var msg = e.detail || {};
      renderCollabMessage(msg);
      // 注入主对话逻辑已移至 chat-collab-bridge.js（仅 chat 页面）
      // 独立工作台页面不需要注入主对话
    });

    // 监听 Director 状态变更（来自 multiagent-sse.js）→ 更新观察窗头部
    window.addEventListener("multiagent-message", function (e) {
      // 不在主对话渲染，仅消费事件避免被其他监听器误处理
      // （multiagent-render.js 仍处理 nav-dot）
    });

    // 监听 Director 健康/自治状态变更
    // 注：Director 状态由 workbench.js 的 loadDirectorDetails 定期轮询 /status 同步
    // 这里无需额外监听 multiagent-sse 事件

    // 打开广播模态框时加载 active 协作列表（供"追加到现有协作"选择）
    var broadcastOpenBtn = document.getElementById("cwBroadcastBtn");
    if (broadcastOpenBtn) {
      broadcastOpenBtn.addEventListener("click", loadActiveCollabsIntoSelect);
    }

    // 绑定广播模态框
    _bindModal(
      "cwBroadcastModal",
      "cwBroadcastBtn",
      ["cwBroadcastCloseBtn", "cwBroadcastCancelBtn"],
      "cwBroadcastSubmitBtn",
      ["cwBroadcastCollabSelect", "cwBroadcastInput"],
      function (values) {
        var collabChoice = values[0]; // "new" 或 collab_id
        var content = values[1];
        if (collabChoice === "new" || !collabChoice) {
          return broadcastMessage(content, true);
        }
        return broadcastMessage(content, false, collabChoice);
      }
    );

    // 绑定注入引导模态框
    _bindModal(
      "cwDirectiveModal",
      "cwDirectiveBtn",
      ["cwDirectiveCloseBtn", "cwDirectiveCancelBtn"],
      "cwDirectiveSubmitBtn",
      ["cwDirectiveInput", "cwDirectiveRuleType", "cwDirectivePriority"],
      function (values) {
        return injectDirective(values[0], values[1], values[2]);
      }
    );
  }

  // ========== 初始化 ==========

  function init() {
    bindEvents();
    setupStreamScrollLoader();
    // 检查 multiagent 是否启用
    fetch("/api/multiagent/status")
      .then(function (resp) {
        if (resp.status === 404) {
          setDirectorState("disabled");
          return null;
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (status) {
        if (status === null) {
          // multiagent 未启用
          var listEl = document.getElementById("cwAgentsList");
          if (listEl) listEl.innerHTML = '<div class="cw-empty">协作未启用</div>';
          var clEl = document.getElementById("cwCollabsList");
          if (clEl) clEl.innerHTML = '<div class="cw-empty">协作未启用</div>';
          var tlEl = document.getElementById("cwActivityTimeline");
          if (tlEl) tlEl.innerHTML = '<div class="cw-empty">协作未启用</div>';
          return;
        }
        if (status.enabled) {
          // 保存本地 agent_id（工作台页面用于显示）
          localAgentId = status.local_agent_id || "";
          // 用后端返回的 director.state 初始化显示（healthy/degraded/autonomous/fault/unknown）
          // workbench.js 的 loadDirectorDetails 会随后刷新完整状态
          var director = status.director || {};
          var rawState = director.state || "unknown";
          setDirectorState(rawState);
          // 独立工作台页面（无 #collabPane）直接加载
          // chat 内嵌模式（有 #collabPane）仅在面板可见时加载
          var pane = document.getElementById("collabPane");
          if (!pane || !pane.hidden) {
            loadAgents();
            loadCollabs();
            loadActivityTimeline(false);
            // 阶段 1.2：先完成历史加载再启动 SSE，避免 SSE 新消息先于历史旧消息入流导致顺序错乱
            loadRecentMessages().then(function () {
              connectCollabSSE();
            });
          }
        } else {
          setDirectorState("disabled");
        }
      })
      .catch(function () {
        setDirectorState("unknown");
      });
  }

  // 导出全局
  window.CollabWorkbench = {
    loadAgents: loadAgents,
    loadCollabs: loadCollabs,
    renderCollabMessage: renderCollabMessage,
    broadcastMessage: broadcastMessage,
    injectDirective: injectDirective,
    connectCollabSSE: connectCollabSSE,
    clearStream: clearStream,
    setDirectorState: setDirectorState,
    loadRecentMessages: loadRecentMessages,
    loadActivityTimeline: loadActivityTimeline,
    jumpToMessage: jumpToMessage,
    jumpToLatest: jumpToLatest,
    loadLocalAgentId: loadLocalAgentId,
    getLocalAgentId: function () { return localAgentId; },
  };

  // DOM 就绪后初始化
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
