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
    var wbState = document.getElementById("wbState");
    var wbStatusVal = document.getElementById("wbStatusVal");
    var wbStatusDot = document.querySelector(".wb-status-dot");

    if (!status || !status.enabled) {
      // 未启用态
      if (navDot) { navDot.className = "nav-status-dot state-disabled"; }
      if (wbState) {
        wbState.className = "wb-state";
        var stateText = wbState.querySelector(".wb-state-text");
        if (stateText) stateText.textContent = STATE_LABELS.disabled;
      }
      if (wbStatusVal) { wbStatusVal.textContent = STATE_LABELS.disabled; wbStatusVal.className = "wb-status-val"; }
      if (wbStatusDot) { wbStatusDot.className = "wb-status-dot"; }
      renderAgents([]);
      return;
    }

    var directorState = (status.director && status.director.state) || "unknown";
    var stateClass = "state-" + directorState;
    var stateLabel = STATE_LABELS[directorState] || directorState;
    var onlineCount = (status.agents || []).filter(function (a) {
      return a.status === "active" || a.status === "online";
    }).length;

    // 更新 nav-item 状态圆点
    if (navDot) { navDot.className = "nav-status-dot " + stateClass; }

    // 更新工作台头部状态药丸
    if (wbState) {
      wbState.className = "wb-state " + stateClass;
      var stateText2 = wbState.querySelector(".wb-state-text");
      if (stateText2) stateText2.textContent = stateLabel;
    }

    // 更新指引 tab 状态条
    if (wbStatusVal) {
      wbStatusVal.textContent = stateLabel + " · " + onlineCount + " agents 在线";
      wbStatusVal.className = "wb-status-val " + stateClass;
    }
    if (wbStatusDot) { wbStatusDot.className = "wb-status-dot " + stateClass; }

    // 更新 Agent 列表
    renderAgents(status.agents || []);
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

  // 导出全局
  window.MultiagentRender = {
    initContainers: initContainers,
    updateStatus: updateStatus,
    renderAgents: renderAgents,
    showAlert: showAlert,
  };

  // 页面加载完成后检查 multiagent 状态
  document.addEventListener("DOMContentLoaded", function () {
    fetch("/api/multiagent/status")
      .then(function (resp) {
        if (resp.ok) {
          return resp.json();
        }
        throw new Error("multiagent disabled");
      })
      .then(function (status) {
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
