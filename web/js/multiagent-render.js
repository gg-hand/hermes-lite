// multiagent-render.js — Director 工作台状态渲染
// 渲染目标：collab-pane 内的工作台元素 + nav-item 状态圆点
// 告警：复用页面 #toast
// 暴露 window.MultiagentRender = { initContainers, updateStatus, renderAgents, showAlert }
(function () {
  "use strict";

  var STATE_COLORS = {
    healthy: "#22c55e",
    degraded: "#eab308",
    autonomous: "#f97316",
    fault: "#ef4444",
    unknown: "#6b7280",
    disabled: "#9ca3af",
  };

  var STATE_LABELS = {
    healthy: "健康",
    degraded: "降级",
    autonomous: "自治",
    fault: "故障",
    unknown: "未知",
    disabled: "未启用",
  };

  var ALERT_TIMEOUT_MS = 5000;

  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /**
   * 初始化工作台容器。幂等：collab-pane 内骨架已在 chat.html 中静态存在，此处仅做事件绑定。
   */
  function initContainers() {
    // 工作台骨架已在 chat.html 中静态定义，无需动态创建
    // tab 切换和收起按钮的绑定在 chat-main.js 中完成
  }

  /**
   * 更新状态显示。
   * @param {Object} status - /api/multiagent/status 返回的状态
   */
  function updateStatus(status) {
    var navDot = document.getElementById("directorStatusDot");
    // Task 9: 旧工作台元素 wbState / wbStatusVal / wbStatusDot 已被新协作观察窗替换
    // 新观察窗的 Director 状态由 collab-workbench.js 的 setDirectorState() 维护
    // 这里仅保留 nav-dot 渲染（侧栏圆点），其他元素缺失时优雅降级

    if (!status || !status.enabled) {
      // 未启用态
      if (navDot) { navDot.className = "nav-status-dot state-disabled"; }
      // 同步新观察窗的 Director 状态
      if (window.CollabWorkbench && typeof window.CollabWorkbench.setDirectorState === "function") {
        window.CollabWorkbench.setDirectorState("disabled");
      }
      return;
    }

    var directorState = (status.director && status.director.state) || "unknown";
    var stateClass = "state-" + directorState;

    // 更新 nav-item 状态圆点（保留）
    if (navDot) { navDot.className = "nav-status-dot " + stateClass; }

    // Task 9：协作观察窗的 Director 状态由 collab-workbench.js 接管
    // 当 multiagent 启用且 Director 健康时，显示为「观察中」
    if (window.CollabWorkbench && typeof window.CollabWorkbench.setDirectorState === "function") {
      // directorState: healthy/degraded/autonomous/fault/unknown
      // 新观察窗只关心：观察中 / 已注入引导 / 未启用 / 未知
      var cwState = "observing";
      if (directorState === "fault" || directorState === "unknown") {
        cwState = "unknown";
      }
      window.CollabWorkbench.setDirectorState(cwState);
    }
  }

  /**
   * 渲染 Agent 列表到工作台名册 tab。
   * @param {Array} agents - agent 列表
   */
  function renderAgents(agents) {
    var listContainer = document.getElementById("agentsList");
    if (!listContainer) return;

    if (!agents || agents.length === 0) {
      listContainer.innerHTML = '<p class="wb-empty">暂无活跃 agents</p>';
      return;
    }

    listContainer.innerHTML = agents
      .map(function (agent) {
        var trustScore = agent.trust_score || 100;
        var trustColor =
          trustScore >= 60 ? "#22c55e" : trustScore >= 30 ? "#eab308" : "#ef4444";
        return (
          '<div class="agent-card" data-agent-id="' +
          escapeHtml(agent.agent_id) +
          '">' +
          '<div class="agent-header">' +
          '<span class="agent-id">' +
          escapeHtml(agent.agent_id) +
          "</span>" +
          '<span class="agent-status status-' +
          escapeHtml(agent.status || "active") +
          '">' +
          escapeHtml(agent.status || "active") +
          "</span>" +
          "</div>" +
          '<div class="agent-meta">' +
          '<span class="agent-role">' +
          escapeHtml(agent.role || "worker") +
          "</span>" +
          '<span class="agent-last-seen">' +
          escapeHtml(agent.last_seen || agent.last_heartbeat || "") +
          "</span>" +
          "</div>" +
          '<div class="trust-score">' +
          '<div class="trust-score-bar" style="width: ' +
          trustScore +
          "%; background: " +
          trustColor +
          ';"></div>' +
          '<span class="trust-score-text">' +
          trustScore +
          "/100</span>" +
          "</div>" +
          "</div>"
        );
      })
      .join("");
  }

  /**
   * 显示告警，复用页面 #toast，5 秒后自动消失。
   * @param {string} message - 告警消息
   * @param {string} level - 告警级别（info/success/warning/error）
   */
  function showAlert(message, level) {
    var toast = document.getElementById("toast");
    if (!toast) {
      // fallback：如果 toast 不存在，用 console
      console.log("[multiagent][" + (level || "info") + "] " + message);
      return;
    }
    var lvl = level || "info";
    toast.className = "toast toast-" + lvl;
    toast.textContent = message;
    toast.classList.add("show");
    if (toast._hideTimer) {
      clearTimeout(toast._hideTimer);
    }
    toast._hideTimer = setTimeout(function () {
      toast.classList.remove("show");
      toast._hideTimer = null;
    }, ALERT_TIMEOUT_MS);
  }

  /**
   * 渲染消息记录到工作台。
   * @param {Array} messages - 消息列表
   */
  function renderMessageLog(messages) {
    var container = document.getElementById("msgLogContainer");
    if (!container) return;

    if (!messages || messages.length === 0) {
      container.innerHTML = '<p class="wb-empty">暂无消息记录</p>';
      return;
    }

    container.innerHTML = messages
      .slice(-50)
      .map(function (msg) {
        var ts = msg.ts || "";
        var tsShort = ts.length > 19 ? ts.substring(11, 19) : ts;
        return (
          '<div class="wb-msg-item">' +
          '<div class="wb-msg-meta">' +
          '<span class="wb-msg-from">' +
          escapeHtml(msg.from || "?") +
          "</span>" +
          '<span class="wb-msg-type">' +
          escapeHtml(msg.type || "") +
          "</span>" +
          '<span class="wb-msg-time">' +
          escapeHtml(tsShort) +
          "</span>" +
          "</div>" +
          '<div class="wb-msg-content">' +
          (typeof renderMarkdown === "function" ? renderMarkdown(msg.content || "") : escapeHtml((msg.content || "").substring(0, 500))) +
          "</div>" +
          "</div>"
        );
      })
      .join("");
  }

  // 导出全局
  window.MultiagentRender = {
    initContainers: initContainers,
    updateStatus: updateStatus,
    renderAgents: renderAgents,
    showAlert: showAlert,
    renderMessageLog: renderMessageLog,
  };

  // 页面加载完成后检查 multiagent 状态
  // 404 = multiagent 未启用（路由未注册），静默降级为 disabled，不产生控制台噪音
  document.addEventListener("DOMContentLoaded", function () {
    fetch("/api/multiagent/status")
      .then(function (resp) {
        if (resp.status === 404) {
          // multiagent 未启用，静默降级
          return null;
        }
        if (resp.ok) {
          return resp.json();
        }
        throw new Error("multiagent status fetch failed: " + resp.status);
      })
      .then(function (status) {
        if (status === null) {
          updateStatus({ enabled: false });
          return;
        }
        updateStatus(status);
        if (status.enabled && window.MultiagentSSE) {
          window.MultiagentSSE.start();
        }
      })
      .catch(function () {
        updateStatus({ enabled: false });
      });
  });
})();
