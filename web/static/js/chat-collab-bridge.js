/* ============================================================
   chat-collab-bridge.js — chat 页面协作消息桥接
   职责：监听 collab-message 事件，将关键节点注入主对话
   依赖：collab-sse.js（SSE 订阅 + dispatchEvent）、chat-core.js（appendMessage）

   独立工作台在 /workbench 页面，chat 页面不再嵌入工作台 UI。
   但 chat 页面仍需要：
     1. 监听协作消息（collab-sse.js 自动启动 SSE）
     2. 识别涉及本地 agent 的关键节点
     3. 调用 TeageChatCore.appendMessage 注入主对话
   ============================================================ */
(function () {
  "use strict";

  var localAgentId = "";

  /**
   * 加载本地 agent_id（从 /api/multiagent/status）
   */
  function loadLocalAgentId() {
    return fetch("/api/multiagent/status")
      .then(function (resp) {
        if (!resp.ok) return null;
        return resp.json();
      })
      .then(function (data) {
        if (data && data.enabled) {
          localAgentId = data.local_agent_id || "";
          if (localAgentId) {
            console.log("[chat-collab-bridge] 本地 agent_id:", localAgentId);
          }
        }
        return localAgentId;
      })
      .catch(function () {
        return "";
      });
  }

  /**
   * 将协作关键节点注入主对话。
   *
   * 注入规则：
   * - type='request' 且 from==本地 agent → collab_progress（"调用 {to} 正在分析…"）
   * - type='response' 且 to==本地 agent → collab_result（带 from 标识的结果）
   * - 其他消息 → 不注入（仅工作台展示）
   */
  function injectToMainChat(msg) {
    if (!localAgentId) return;
    if (!window.TeageChatCore) return;
    var core = window.TeageChatCore;

    var type = msg.type || "";
    var from = msg.from || "";
    var to = msg.to || "";
    var cid = msg.collab_id || "";
    var isMainCollab = msg.channel === "main_session";
    // 只处理主会话协作通道（worker 协作仍在工作台观察，不注入主对话）
    if (!isMainCollab) return;

    // 本地 agent 发起的协作请求 → 打开协作 Agent 内置窗口
    if (type === "request" && from === localAgentId) {
      var targetName = to && to !== "*" ? to : "其他 Agent";
      try {
        if (core.openCollabCard) {
          core.openCollabCard(cid, targetName, "协作中…");
        } else if (core.appendMessage) {
          core.appendMessage("collab_progress", "请求 " + targetName + " 协作处理，等待其执行…");
        }
      } catch (e) {
        console.warn("[chat-collab-bridge] 打开协作卡片失败", e);
      }
      return;
    }

    // 协作 agent 的执行过程步骤（relay kind=process，如工具调用）
    if (type === "relay" && msg.kind === "process"
        && from && from !== localAgentId && to === localAgentId) {
      var stepContent = (msg.content || "").trim();
      if (!stepContent) return;
      try {
        if (core.openCollabCard) core.openCollabCard(cid, from, "协作中…");
        if (core.appendCollabProcess) core.appendCollabProcess(cid, stepContent);
      } catch (e) {
        console.warn("[chat-collab-bridge] 追加协作过程失败", e);
      }
      return;
    }

    // 协作 agent 的结果流分块（relay kind=result）→ 流式追加到卡片结果区
    if (type === "relay" && msg.kind === "result"
        && from && from !== localAgentId && to === localAgentId) {
      var chunk = msg.content || "";
      if (!chunk) return;
      try {
        if (core.openCollabCard) core.openCollabCard(cid, from, "执行中…");
        if (core.appendCollabResult) core.appendCollabResult(cid, chunk);
      } catch (e) {
        console.warn("[chat-collab-bridge] 追加协作结果失败", e);
      }
      return;
    }

    // 协作 agent 的最终响应 → 完成卡片（结果作为主 agent 收集的信息）
    if (type === "response" && to === localAgentId) {
      var content = msg.content || "";
      try {
        if (core.openCollabCard) core.openCollabCard(cid, from, "执行中…");
        if (core.appendCollabResult) core.appendCollabResult(cid, content);
        if (core.closeCollabCard) core.closeCollabCard(cid);
      } catch (e) {
        console.warn("[chat-collab-bridge] 完成协作卡片失败", e);
      }
      return;
    }
  }

  /**
   * 初始化：加载 agent_id + 监听协作消息
   */
  function init() {
    loadLocalAgentId().then(function () {
      // 启动协作 SSE：chat 页面无工作台（collab-workbench.js 才调 start()），
      // 必须在此主动启动，否则收不到 collab-message 事件、无法注入协作进度/结果。
      if (window.CollabSSE && typeof window.CollabSSE.start === "function") {
        window.CollabSSE.start();
      }
      // 监听协作消息事件（由 collab-sse.js dispatch）
      window.addEventListener("collab-message", function (e) {
        var msg = e.detail || {};
        injectToMainChat(msg);
      });
      console.log("[chat-collab-bridge] 已初始化并启动协作 SSE，等待协作消息");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
