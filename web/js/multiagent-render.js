// multiagent-render.js — multiagent 状态渲染
// Director 状态指示器（四色）+ Agent 列表 + 告警横幅
// 暴露 window.MultiagentRender = { initContainers, updateStatus, renderAgents, showAlert }
(function () {
  "use strict";

  var STATE_COLORS = {
    healthy: "#22c55e", // 绿
    degraded: "#eab308", // 黄
    autonomous: "#f97316", // 橙
    fault: "#ef4444", // 红
    unknown: "#6b7280", // 灰
    disabled: "#9ca3af", // 浅灰
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
   * 初始化 multiagent UI 容器（状态指示器、Agent 面板、告警横幅）。
   * 幂等：已存在则跳过。
   */
  function initContainers() {
    // 状态指示器（顶部右侧）
    if (!document.getElementById("multiagent-indicator")) {
      var indicator = document.createElement("div");
      indicator.id = "multiagent-indicator";
      indicator.className = "multiagent-indicator state-disabled";
      indicator.setAttribute("data-state", "disabled");
      indicator.innerHTML =
        '<span class="indicator-dot"></span>' +
        '<span class="indicator-text">' +
        STATE_LABELS.disabled +
        "</span>";
      document.body.appendChild(indicator);
    }

    // Agent 列表面板（右侧）
    if (!document.getElementById("multiagent-agents-panel")) {
      var panel = document.createElement("div");
      panel.id = "multiagent-agents-panel";
      panel.className = "multiagent-agents-panel";
      panel.innerHTML =
        '<div class="panel-header">' +
        "<h4>协作 Agents</h4>" +
        '<button class="panel-toggle" data-action="toggle-panel">−</button>' +
        "</div>" +
        '<div class="panel-body">' +
        '<div class="agents-list"></div>' +
        "</div>";
      document.body.appendChild(panel);
      // 绑定折叠按钮
      var toggleBtn = panel.querySelector(".panel-toggle");
      if (toggleBtn) {
        toggleBtn.addEventListener("click", function () {
          var body = panel.querySelector(".panel-body");
          if (body) {
            var isHidden = body.style.display === "none";
            body.style.display = isHidden ? "" : "none";
            toggleBtn.textContent = isHidden ? "−" : "+";
          }
        });
      }
    }

    // 告警横幅（顶部居中）
    if (!document.getElementById("multiagent-alert-banner")) {
      var banner = document.createElement("div");
      banner.id = "multiagent-alert-banner";
      banner.className = "multiagent-alert-banner";
      banner.style.display = "none";
      document.body.appendChild(banner);
    }
  }

  /**
   * 更新状态显示。
   * @param {Object} status - /api/multiagent/status 返回的状态
   */
  function updateStatus(status) {
    initContainers();

    var indicator = document.getElementById("multiagent-indicator");
    if (!indicator) return;

    if (!status || !status.enabled) {
      indicator.className = "multiagent-indicator state-disabled";
      indicator.setAttribute("data-state", "disabled");
      var textEl = indicator.querySelector(".indicator-text");
      if (textEl) textEl.textContent = STATE_LABELS.disabled;
      renderAgents([]);
      return;
    }

    var directorState = (status.director && status.director.state) || "unknown";
    indicator.className = "multiagent-indicator state-" + directorState;
    indicator.setAttribute("data-state", directorState);
    var directorText = status.director && status.director.agent_id
      ? "Director: " +
        status.director.agent_id +
        " (" +
        (STATE_LABELS[directorState] || directorState) +
        ")"
      : STATE_LABELS[directorState] || directorState;
    var textEl2 = indicator.querySelector(".indicator-text");
    if (textEl2) textEl2.textContent = directorText;

    // 更新 Agent 列表
    renderAgents(status.agents || []);

    // 自治模式指示
    if (status.autonomous_mode) {
      indicator.className += " autonomous-active";
    }
  }

  /**
   * 渲染 Agent 列表。
   * @param {Array} agents - agent 列表
   */
  function renderAgents(agents) {
    var listContainer = document.querySelector(
      "#multiagent-agents-panel .agents-list"
    );
    if (!listContainer) return;

    if (!agents || agents.length === 0) {
      listContainer.innerHTML = '<p class="no-agents">暂无活跃 agents</p>';
      return;
    }

    listContainer.innerHTML = agents
      .map(function (agent) {
        var trustScore = agent.trust_score || 100;
        var trustColor =
          trustScore >= 60
            ? "#22c55e"
            : trustScore >= 30
            ? "#eab308"
            : "#ef4444";
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
   * 显示告警横幅，5 秒后自动消失。
   * @param {string} message - 告警消息
   * @param {string} level - 告警级别（info/success/warning/error）
   */
  function showAlert(message, level) {
    initContainers();
    var banner = document.getElementById("multiagent-alert-banner");
    if (!banner) return;
    var lvl = level || "info";
    banner.className = "multiagent-alert-banner alert-" + lvl;
    banner.textContent = message;
    banner.style.display = "block";
    // 清除之前的定时器
    if (banner._hideTimer) {
      clearTimeout(banner._hideTimer);
    }
    banner._hideTimer = setTimeout(function () {
      banner.style.display = "none";
      banner._hideTimer = null;
    }, ALERT_TIMEOUT_MS);
  }

  // 导出全局
  window.MultiagentRender = {
    initContainers: initContainers,
    updateStatus: updateStatus,
    renderAgents: renderAgents,
    showAlert: showAlert,
  };

  // 页面加载完成后初始化 + 检查 multiagent 是否启用
  document.addEventListener("DOMContentLoaded", function () {
    initContainers();
    // 尝试获取 multiagent 状态，决定是否启动 SSE
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
        // multiagent 未启用，保持禁用状态
        updateStatus({ enabled: false });
      });
  });
})();
