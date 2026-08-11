// multiagent-settings.js — multiagent 协作配置 UI 逻辑
// 在 chat.html 设置模态框中渲染 multiagent 配置段，提交时通过 PUT /api/config 保存
// 配置分为四个区域：基础 / Agent 配置 / 工作台配置 / Director 配置
// 暴露 window.MultiagentSettings = { render, collect } 供 chat-settings.js 集成
(function () {
  "use strict";

  var MULTIAGENT_DEFAULTS = {
    enabled: false,
    role: "worker",
    blackboard_dir: "data/blackboard",
    worker: {
      agent_id: "worker_001",
      agent_version: "1.0.0",
      capabilities: ["file_read", "file_write", "web_search"],
      specialties: [],
      heartbeat_interval_seconds: 10,
      dangerous_tools: ["execute_command", "write_file", "call_tool"],
      endpoint: "http://localhost:8000",
      owner: "",
      max_concurrent_tasks: 3,
      auth_method: "local",
      trust_score: 100,
    },
    collab: {
      poll_interval_seconds: 2,
      idle_timeout_seconds: 60,
      urgent_queue_max_size: 10,
      normal_queue_max_size: 100,
      sleep_after_empty_polls: 0,
      sleep_poll_interval_seconds: 30,
    },
    director: {
      heartbeat_timeout_seconds: 30,
      autonomous_after_seconds: 60,
    },
    // 主会话协作（用户主对话自主调用其他 agent，独立 A2A 端到端通道）
    main_session_collab: {
      enabled: true,
      inject_prompt: true,
      register_tools: true,
      default_timeout: 60,
      max_timeout: 300,
      max_result_chars: 4000,
      mode: "peer",
      discovery: { local: true, a2a: true },
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

  function splitCsv(el) {
    if (!el || !el.value) return [];
    return el.value
      .split(",")
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
  }

  function parseIntOr(el, defaultVal) {
    var v = parseInt(el && el.value, 10);
    return isNaN(v) ? defaultVal : v;
  }

  /**
   * 渲染 multiagent 配置段到指定容器。
   * 分四个区域：基础配置 / Agent 配置 / 工作台配置 / Director 配置
   */
  function renderMultiagentSection(container, currentConfig) {
    if (!container) return;
    var cfg = Object.assign(
      {},
      MULTIAGENT_DEFAULTS,
      (currentConfig && currentConfig.multiagent) || {}
    );
    var workerCfg = cfg.worker || {};
    var collabCfg = cfg.collab || {};
    var directorCfg = cfg.director || {};
    var mscCfg = cfg.main_session_collab || {};

    container.innerHTML =
      // ========== 基础配置 ==========
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
                '<option value="worker" ' + (cfg.role === "worker" ? "selected" : "") + ">Worker（参与协作）</option>" +
                '<option value="director" ' + (cfg.role === "director" ? "selected" : "") + ">Director（引导观察）</option>" +
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

          // ========== Agent 配置（worker） ==========
          '<div class="form-section-title" style="margin-top:16px;font-size:11px">Agent 配置（worker）</div>' +
          '<div class="hint" style="margin-bottom:8px">本 Agent 的身份与能力声明，写入 agent_card 供其他 Agent 识别</div>' +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>Agent ID</label>" +
              '<input class="form-input" type="text" id="multiagent-agent-id" value="' +
                escapeHtml(workerCfg.agent_id || "worker_001") + '">' +
              '<div class="hint">唯一标识（需重启生效）</div>' +
            "</div>" +
            '<div class="form-group">' +
              "<label>Agent 版本</label>" +
              '<input class="form-input" type="text" id="multiagent-agent-version" value="' +
                escapeHtml(workerCfg.agent_version || "1.0.0") + '">' +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>能力声明（逗号分隔）</label>" +
              '<input class="form-input" type="text" id="multiagent-capabilities" value="' +
                escapeHtml((workerCfg.capabilities || []).join(",")) + '">' +
              '<div class="hint">file_read, file_write, execute_command, web_search, sentiment_analysis, translation, summarization, code_review</div>' +
            "</div>" +
            '<div class="form-group">' +
              "<label>专长领域（逗号分隔）</label>" +
              '<input class="form-input" type="text" id="multiagent-specialties" value="' +
                escapeHtml((workerCfg.specialties || []).join(",")) + '">' +
              '<div class="hint">自由文本，如：Python后端,数据分析</div>' +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>服务端点</label>" +
              '<input class="form-input" type="text" id="multiagent-endpoint" value="' +
                escapeHtml(workerCfg.endpoint || "http://localhost:8000") + '">' +
              '<div class="hint">供其他 Agent A2A 调用</div>' +
            "</div>" +
            '<div class="form-group">' +
              "<label>所有者</label>" +
              '<input class="form-input" type="text" id="multiagent-owner" value="' +
                escapeHtml(workerCfg.owner || "") + '">' +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>认证方式</label>" +
              '<select class="form-select" id="multiagent-auth-method">' +
                ["local", "api_key", "signed", "oauth2", "mtls"].map(function (m) {
                  return '<option value="' + m + '" ' + (workerCfg.auth_method === m ? "selected" : "") + ">" + m + "</option>";
                }).join("") +
              "</select>" +
            "</div>" +
            '<div class="form-group">' +
              "<label>最大并发任务</label>" +
              '<input class="form-input" type="number" id="multiagent-max-concurrent" min="1" max="20" value="' +
                (workerCfg.max_concurrent_tasks != null ? workerCfg.max_concurrent_tasks : 3) + '">' +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>信任分数（0-100）</label>" +
              '<input class="form-input" type="number" id="multiagent-trust-score" min="0" max="100" value="' +
                (workerCfg.trust_score != null ? workerCfg.trust_score : 100) + '">' +
            "</div>" +
            '<div class="form-group">' +
              "<label>心跳间隔（秒）</label>" +
              '<input class="form-input" type="number" id="multiagent-heartbeat-interval" min="5" max="300" value="' +
                (workerCfg.heartbeat_interval_seconds || 10) + '">' +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>危险工具（逗号分隔）</label>" +
              '<input class="form-input" type="text" id="multiagent-dangerous-tools" value="' +
                escapeHtml((workerCfg.dangerous_tools || []).join(",")) + '">' +
              '<div class="hint">写入 agent_card 供 Director 审计</div>' +
            "</div>" +
          "</div>" +

          // ========== 工作台配置（collab） ==========
          '<div class="form-section-title" style="margin-top:16px;font-size:11px">工作台配置（collab）</div>' +
          '<div class="hint" style="margin-bottom:8px">控制 Agent 协作消息处理的行为参数，与 Agent 身份无关</div>' +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>消息轮询间隔（秒）</label>" +
              '<input class="form-input" type="number" id="multiagent-poll-interval" min="1" max="60" value="' +
                (collabCfg.poll_interval_seconds || 2) + '">' +
              '<div class="hint">WorkerAdapter 协作消息轮询周期</div>' +
            "</div>" +
            '<div class="form-group">' +
              "<label>空闲超时（秒）</label>" +
              '<input class="form-input" type="number" id="multiagent-idle-timeout" min="10" max="600" value="' +
                (collabCfg.idle_timeout_seconds || 60) + '">' +
              '<div class="hint">无 A2A 调用且队列非空时触发 LLM 的阈值</div>' +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>紧急队列最大长度</label>" +
              '<input class="form-input" type="number" id="multiagent-urgent-max" min="1" max="100" value="' +
                (collabCfg.urgent_queue_max_size || 10) + '">' +
              '<div class="hint">directive intervention / 用户广播</div>' +
            "</div>" +
            '<div class="form-group">' +
              "<label>普通队列最大长度</label>" +
              '<input class="form-input" type="number" id="multiagent-normal-max" min="10" max="1000" value="' +
                (collabCfg.normal_queue_max_size || 100) + '">' +
              '<div class="hint">directive ordering / relay / request</div>' +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>休眠阈值（空轮询次数）</label>" +
              '<input class="form-input" type="number" id="multiagent-sleep-after-empty" min="0" max="500" value="' +
                (collabCfg.sleep_after_empty_polls != null ? collabCfg.sleep_after_empty_polls : 0) + '">' +
              '<div class="hint">连续 N 次空轮询后进入休眠（0=禁用，休眠期仅 mtime 探针）</div>' +
            "</div>" +
            '<div class="form-group">' +
              "<label>休眠探针间隔（秒）</label>" +
              '<input class="form-input" type="number" id="multiagent-sleep-interval" min="5" max="300" value="' +
                (collabCfg.sleep_poll_interval_seconds || 30) + '">' +
              '<div class="hint">休眠模式唤醒探针周期，建议 30s</div>' +
            "</div>" +
          "</div>" +

          // ========== Director 配置 ==========
          '<div class="form-section-title" style="margin-top:16px;font-size:11px">Director 配置</div>' +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>心跳超时（秒）</label>" +
              '<input class="form-input" type="number" id="multiagent-director-timeout" min="10" max="600" value="' +
                (directorCfg.heartbeat_timeout_seconds || 30) + '">' +
            "</div>" +
            '<div class="form-group">' +
              "<label>自治模式触发（秒）</label>" +
              '<input class="form-input" type="number" id="multiagent-autonomous-after" min="30" max="3600" value="' +
                (directorCfg.autonomous_after_seconds || 60) + '">' +
            "</div>" +
          "</div>" +

          // ========== 主会话协作配置 ==========
          '<div class="form-section-title" style="margin-top:16px;font-size:11px">主会话协作（主对话自主调用其他 Agent）</div>' +
          '<div class="hint" style="margin-bottom:8px">需 a2a.enabled + 对端可达才生效；单实例/无对端时功能静默，不影响主会话。配置变更需重启。</div>' +
          '<div class="switch-row">' +
            "<div>" +
              '<div class="switch-label">启用主会话协作</div>' +
              '<div class="hint">主对话 LLM 可通过协作工具调用其他 Agent</div>' +
            "</div>" +
            '<label class="switch"><input type="checkbox" id="msc-enabled" ' +
              (mscCfg.enabled !== false ? "checked" : "") +
              '><span class="slider"></span></label>' +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              '<label class="switch-label">注入协作引导</label>' +
              '<label class="switch"><input type="checkbox" id="msc-inject-prompt" ' +
                (mscCfg.inject_prompt !== false ? "checked" : "") +
                '><span class="slider"></span></label>' +
              '<div class="hint">extra_system_prompt 追加协作引导段</div>' +
            "</div>" +
            '<div class="form-group">' +
              '<label class="switch-label">注册协作工具</label>' +
              '<label class="switch"><input type="checkbox" id="msc-register-tools" ' +
                (mscCfg.register_tools !== false ? "checked" : "") +
                '><span class="slider"></span></label>' +
              '<div class="hint">list_collab_agents / request_collaboration</div>' +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>默认等待超时（秒）</label>" +
              '<input class="form-input" type="number" id="msc-default-timeout" min="5" max="600" value="' +
                (mscCfg.default_timeout != null ? mscCfg.default_timeout : 60) + '">' +
            "</div>" +
            '<div class="form-group">' +
              "<label>超时上限（秒）</label>" +
              '<input class="form-input" type="number" id="msc-max-timeout" min="10" max="3600" value="' +
                (mscCfg.max_timeout != null ? mscCfg.max_timeout : 300) + '">' +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              "<label>结果摘要上限（字符）</label>" +
              '<input class="form-input" type="number" id="msc-max-result-chars" min="500" max="20000" value="' +
                (mscCfg.max_result_chars != null ? mscCfg.max_result_chars : 4000) + '">' +
              '<div class="hint">防上下文膨胀</div>' +
            "</div>" +
            '<div class="form-group">' +
              "<label>模式</label>" +
              '<select class="form-select" id="msc-mode">' +
                '<option value="peer" ' + (mscCfg.mode === "subagent" ? "" : "selected") + ">peer（平级协作，当前）</option>" +
                '<option value="subagent" ' + (mscCfg.mode === "subagent" ? "selected" : "") + ">subagent（预留扩展）</option>" +
              "</select>" +
            "</div>" +
          "</div>" +
          '<div class="form-row">' +
            '<div class="form-group">' +
              '<label class="switch-label">本地发现</label>' +
              '<label class="switch"><input type="checkbox" id="msc-discovery-local" ' +
                ((mscCfg.discovery && mscCfg.discovery.local) !== false ? "checked" : "") +
                '><span class="slider"></span></label>' +
            "</div>" +
            '<div class="form-group">' +
              '<label class="switch-label">A2A 远程发现</label>' +
              '<label class="switch"><input type="checkbox" id="msc-discovery-a2a" ' +
                ((mscCfg.discovery && mscCfg.discovery.a2a) !== false ? "checked" : "") +
                '><span class="slider"></span></label>' +
            "</div>" +
          "</div>" +
        "</div>" +
      "</div>";

    // 绑定启用开关
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

    var $ = function (id) { return container.querySelector("#" + id); };

    return {
      multiagent: {
        enabled: true,
        role: $("multiagent-role") ? $("multiagent-role").value : "worker",
        blackboard_dir: $("multiagent-blackboard-dir") ? $("multiagent-blackboard-dir").value : "data/blackboard",
        worker: {
          agent_id: $("multiagent-agent-id") ? $("multiagent-agent-id").value : "worker_001",
          agent_version: $("multiagent-agent-version") ? $("multiagent-agent-version").value : "1.0.0",
          capabilities: splitCsv($("multiagent-capabilities")),
          specialties: splitCsv($("multiagent-specialties")),
          endpoint: $("multiagent-endpoint") ? $("multiagent-endpoint").value : "http://localhost:8000",
          owner: $("multiagent-owner") ? $("multiagent-owner").value : "",
          max_concurrent_tasks: parseIntOr($("multiagent-max-concurrent"), 3),
          auth_method: $("multiagent-auth-method") ? $("multiagent-auth-method").value : "local",
          trust_score: parseIntOr($("multiagent-trust-score"), 100),
          heartbeat_interval_seconds: parseIntOr($("multiagent-heartbeat-interval"), 10),
          dangerous_tools: splitCsv($("multiagent-dangerous-tools")),
        },
        collab: {
          poll_interval_seconds: parseIntOr($("multiagent-poll-interval"), 2),
          idle_timeout_seconds: parseIntOr($("multiagent-idle-timeout"), 60),
          urgent_queue_max_size: parseIntOr($("multiagent-urgent-max"), 10),
          normal_queue_max_size: parseIntOr($("multiagent-normal-max"), 100),
          sleep_after_empty_polls: parseIntOr($("multiagent-sleep-after-empty"), 0),
          sleep_poll_interval_seconds: parseIntOr($("multiagent-sleep-interval"), 30),
        },
        director: {
          heartbeat_timeout_seconds: parseIntOr($("multiagent-director-timeout"), 30),
          autonomous_after_seconds: parseIntOr($("multiagent-autonomous-after"), 60),
        },
        main_session_collab: {
          enabled: $("msc-enabled") ? $("msc-enabled").checked : true,
          inject_prompt: $("msc-inject-prompt") ? $("msc-inject-prompt").checked : true,
          register_tools: $("msc-register-tools") ? $("msc-register-tools").checked : true,
          default_timeout: parseIntOr($("msc-default-timeout"), 60),
          max_timeout: parseIntOr($("msc-max-timeout"), 300),
          max_result_chars: parseIntOr($("msc-max-result-chars"), 4000),
          mode: $("msc-mode") ? $("msc-mode").value : "peer",
          discovery: {
            local: $("msc-discovery-local") ? $("msc-discovery-local").checked : true,
            a2a: $("msc-discovery-a2a") ? $("msc-discovery-a2a").checked : true,
          },
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
