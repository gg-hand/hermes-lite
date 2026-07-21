// multiagent-settings.js — multiagent 协作配置 UI 逻辑
// 在 chat.html 设置模态框中渲染 multiagent 配置段，提交时通过 PUT /api/config 保存
// 暴露 window.MultiagentSettings = { render, collect } 供 chat-settings.js 集成
(function () {
  "use strict";

  var MULTIAGENT_DEFAULTS = {
    enabled: false,
    role: "worker",
    blackboard_dir: "data/blackboard",
    worker: {
      agent_id: "worker_001",
      heartbeat_interval_seconds: 10,
      capabilities: ["file_read", "file_write", "web_search"],
      dangerous_tools: ["execute_command", "write_file", "call_tool"],
    },
    director: {
      heartbeat_timeout_seconds: 30,
      autonomous_after_seconds: 60,
    },
  };

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
   * 渲染 multiagent 配置段到指定容器。
   * @param {HTMLElement} container - 容器元素（通常为 #multiagent-settings-container）
   * @param {Object} currentConfig - 当前完整配置对象
   */
  function renderMultiagentSection(container, currentConfig) {
    if (!container) return;
    var cfg = Object.assign(
      {},
      MULTIAGENT_DEFAULTS,
      (currentConfig && currentConfig.multiagent) || {}
    );
    var workerCfg = cfg.worker || {};
    var directorCfg = cfg.director || {};

    container.innerHTML =
      '<div id="multiagent-section" class="form-section">' +
      '<div class="form-section-title">多 Agent 协作' +
      '<span class="config-tag hot">热更新</span>' +
      "</div>" +
      '<div class="switch-row">' +
      "<div>" +
      '<div class="switch-label">启用多 Agent 协作</div>' +
      '<div class="hint">开启后本机可作为 Director 或 Worker 加入黑板协作</div>' +
      "</div>" +
      '<label class="switch"><input type="checkbox" id="multiagent-enabled" ' +
      (cfg.enabled ? "checked" : "") +
      '><span class="slider"></span></label>' +
      "</div>" +
      '<div id="multiagent-detail" style="' +
      (cfg.enabled ? "" : "display:none") +
      '">' +
      '<div class="form-row">' +
      '<div class="form-group">' +
      "<label>角色</label>" +
      '<select class="form-select" id="multiagent-role">' +
      '<option value="worker" ' +
      (cfg.role === "worker" ? "selected" : "") +
      ">Worker</option>" +
      '<option value="director" ' +
      (cfg.role === "director" ? "selected" : "") +
      ">Director</option>" +
      "</select>" +
      "</div>" +
      '<div class="form-group">' +
      "<label>Blackboard 目录</label>" +
      '<input class="form-input" type="text" id="multiagent-blackboard-dir" value="' +
      escapeHtml(cfg.blackboard_dir) +
      '" placeholder="data/blackboard">' +
      '<div class="hint">协作黑板根目录（需重启生效）</div>' +
      "</div>" +
      "</div>" +
      '<div class="form-row">' +
      '<div class="form-group">' +
      "<label>Agent ID</label>" +
      '<input class="form-input" type="text" id="multiagent-agent-id" value="' +
      escapeHtml(workerCfg.agent_id || "worker_001") +
      '">' +
      "</div>" +
      '<div class="form-group">' +
      "<label>心跳间隔（秒）</label>" +
      '<input class="form-input" type="number" id="multiagent-heartbeat-interval" min="5" max="300" value="' +
      (workerCfg.heartbeat_interval_seconds || 10) +
      '">' +
      "</div>" +
      "</div>" +
      '<div class="form-row">' +
      '<div class="form-group">' +
      "<label>能力声明（逗号分隔）</label>" +
      '<input class="form-input" type="text" id="multiagent-capabilities" value="' +
      escapeHtml((workerCfg.capabilities || []).join(",")) +
      '">' +
      "</div>" +
      '<div class="form-group">' +
      "<label>危险工具（逗号分隔）</label>" +
      '<input class="form-input" type="text" id="multiagent-dangerous-tools" value="' +
      escapeHtml((workerCfg.dangerous_tools || []).join(",")) +
      '">' +
      "</div>" +
      "</div>" +
      '<div class="form-section-title" style="margin-top:16px;font-size:11px">Director 配置</div>' +
      '<div class="form-row">' +
      '<div class="form-group">' +
      "<label>心跳超时（秒）</label>" +
      '<input class="form-input" type="number" id="multiagent-director-timeout" min="10" max="600" value="' +
      (directorCfg.heartbeat_timeout_seconds || 30) +
      '">' +
      "</div>" +
      '<div class="form-group">' +
      "<label>自治模式触发（秒）</label>" +
      '<input class="form-input" type="number" id="multiagent-autonomous-after" min="30" max="3600" value="' +
      (directorCfg.autonomous_after_seconds || 60) +
      '">' +
      "</div>" +
      "</div>" +
      "</div>" +
      "</div>";

    // 绑定启用开关：切换 detail 显隐
    var enabledCheckbox = container.querySelector("#multiagent-enabled");
    var detailDiv = container.querySelector("#multiagent-detail");
    if (enabledCheckbox && detailDiv) {
      enabledCheckbox.addEventListener("change", function (e) {
        detailDiv.style.display = e.target.checked ? "" : "none";
      });
    }
  }

  /**
   * 从 UI 收集 multiagent 配置段。
   * @param {HTMLElement} container - 容器元素
   * @returns {Object} 形如 { multiagent: {...} } 的配置段
   */
  function collectMultiagentConfig(container) {
    if (!container) return { multiagent: { enabled: false } };
    var enabledEl = container.querySelector("#multiagent-enabled");
    if (!enabledEl) return { multiagent: { enabled: false } };
    var enabled = enabledEl.checked;
    if (!enabled) {
      return { multiagent: { enabled: false } };
    }

    var roleEl = container.querySelector("#multiagent-role");
    var bbDirEl = container.querySelector("#multiagent-blackboard-dir");
    var agentIdEl = container.querySelector("#multiagent-agent-id");
    var heartbeatEl = container.querySelector("#multiagent-heartbeat-interval");
    var capabilitiesEl = container.querySelector("#multiagent-capabilities");
    var dangerousToolsEl = container.querySelector(
      "#multiagent-dangerous-tools"
    );
    var directorTimeoutEl = container.querySelector(
      "#multiagent-director-timeout"
    );
    var autonomousAfterEl = container.querySelector(
      "#multiagent-autonomous-after"
    );

    function splitCsv(el) {
      if (!el || !el.value) return [];
      return el.value
        .split(",")
        .map(function (s) {
          return s.trim();
        })
        .filter(Boolean);
    }

    function parseIntOr(el, defaultVal) {
      var v = parseInt(el && el.value, 10);
      return isNaN(v) ? defaultVal : v;
    }

    return {
      multiagent: {
        enabled: true,
        role: roleEl ? roleEl.value : "worker",
        blackboard_dir: bbDirEl ? bbDirEl.value : "data/blackboard",
        worker: {
          agent_id: agentIdEl ? agentIdEl.value : "worker_001",
          heartbeat_interval_seconds: parseIntOr(heartbeatEl, 10),
          capabilities: splitCsv(capabilitiesEl),
          dangerous_tools: splitCsv(dangerousToolsEl),
        },
        director: {
          heartbeat_timeout_seconds: parseIntOr(directorTimeoutEl, 30),
          autonomous_after_seconds: parseIntOr(autonomousAfterEl, 60),
        },
      },
    };
  }

  // 导出全局
  window.MultiagentSettings = {
    render: renderMultiagentSection,
    collect: collectMultiagentConfig,
  };
})();
