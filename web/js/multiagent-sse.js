// multiagent-sse.js — multiagent_alert SSE 通道订阅
// 监听 /api/multiagent/sse 端点，将事件分发给 multiagent-render.js 渲染
// 暴露 window.MultiagentSSE = { start, stop, getCurrentStatus }
(function () {
  "use strict";

  var eventSource = null;
  var reconnectTimer = null;
  var currentStatus = null;
  var RECONNECT_DELAY_MS = 5000;

  /**
   * 启动 SSE 订阅。
   */
  function start() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    try {
      eventSource = new EventSource("/api/multiagent/sse");
    } catch (e) {
      console.warn("multiagent SSE: EventSource 初始化失败", e);
      scheduleReconnect();
      return;
    }

    // initial 事件：首次推送当前状态
    eventSource.addEventListener("initial", function (e) {
      try {
        currentStatus = JSON.parse(e.data);
        dispatchRender(currentStatus);
      } catch (err) {
        console.warn("multiagent SSE: initial 解析失败", err);
      }
    });

    // director_state_change：Director 健康状态变更
    eventSource.addEventListener("director_state_change", function (e) {
      try {
        var data = JSON.parse(e.data);
        currentStatus = data;
        dispatchRender(data);
        var state = (data.director && data.director.state) || "unknown";
        showAlertSafe("Director 状态变更: " + state, "info");
      } catch (err) {
        console.warn("multiagent SSE: director_state_change 解析失败", err);
      }
    });

    // agent_join：新 agent 加入
    eventSource.addEventListener("agent_join", function (e) {
      try {
        var data = JSON.parse(e.data);
        currentStatus = data;
        dispatchRender(data);
        var newAgents = data.agents || [];
        var ids = newAgents
          .map(function (a) {
            return a.agent_id;
          })
          .join(", ");
        showAlertSafe("Agent 加入: " + ids, "success");
      } catch (err) {
        console.warn("multiagent SSE: agent_join 解析失败", err);
      }
    });

    // agent_leave：agent 离线
    eventSource.addEventListener("agent_leave", function (e) {
      try {
        var data = JSON.parse(e.data);
        currentStatus = data;
        dispatchRender(data);
        showAlertSafe("Agent 离线", "warning");
      } catch (err) {
        console.warn("multiagent SSE: agent_leave 解析失败", err);
      }
    });

    // autonomous_enter：进入自治模式
    eventSource.addEventListener("autonomous_enter", function (e) {
      try {
        var data = JSON.parse(e.data);
        currentStatus = data;
        dispatchRender(data);
        showAlertSafe("进入自治模式（Director 故障）", "warning");
      } catch (err) {
        console.warn("multiagent SSE: autonomous_enter 解析失败", err);
      }
    });

    // autonomous_exit：退出自治模式
    eventSource.addEventListener("autonomous_exit", function (e) {
      try {
        var data = JSON.parse(e.data);
        currentStatus = data;
        dispatchRender(data);
        showAlertSafe("退出自治模式（Director 恢复）", "success");
      } catch (err) {
        console.warn("multiagent SSE: autonomous_exit 解析失败", err);
      }
    });

    // message_append：新消息追加（触发聊天界面刷新）
    eventSource.addEventListener("message_append", function (e) {
      try {
        var data = JSON.parse(e.data);
        window.dispatchEvent(
          new CustomEvent("multiagent-message", { detail: data })
        );
      } catch (err) {
        console.warn("multiagent SSE: message_append 解析失败", err);
      }
    });

    eventSource.onerror = function () {
      console.warn("multiagent SSE: 连接失败，" + RECONNECT_DELAY_MS + "ms 后重连");
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
    }
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      start();
    }, RECONNECT_DELAY_MS);
  }

  /**
   * 停止 SSE 订阅。
   */
  function stop() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    currentStatus = null;
  }

  /**
   * 获取当前状态。
   */
  function getCurrentStatus() {
    return currentStatus;
  }

  /**
   * 将状态分发给 MultiagentRender 渲染（若模块已加载）。
   */
  function dispatchRender(status) {
    if (window.MultiagentRender && typeof window.MultiagentRender.updateStatus === "function") {
      try {
        window.MultiagentRender.updateStatus(status);
      } catch (e) {
        console.warn("multiagent SSE: render 调用失败", e);
      }
    }
  }

  /**
   * 显示告警（若 MultiagentRender 已加载）。
   */
  function showAlertSafe(message, level) {
    if (window.MultiagentRender && typeof window.MultiagentRender.showAlert === "function") {
      try {
        window.MultiagentRender.showAlert(message, level);
      } catch (e) {
        console.warn("multiagent SSE: showAlert 调用失败", e);
      }
    }
  }

  // 导出全局
  window.MultiagentSSE = {
    start: start,
    stop: stop,
    getCurrentStatus: getCurrentStatus,
  };

  // 监听自定义事件（测试用）：允许通过 window.dispatchEvent 触发告警
  window.addEventListener("multiagent-alert", function (e) {
    var detail = e.detail || {};
    var evtType = detail.type;
    var evtData = detail.data || {};
    if (evtType === "autonomous_enter") {
      dispatchRender(evtData);
      showAlertSafe("进入自治模式", "warning");
    } else if (evtType === "autonomous_exit") {
      dispatchRender(evtData);
      showAlertSafe("退出自治模式", "success");
    } else if (evtType === "director_state_change") {
      dispatchRender(evtData);
      var state = (evtData.director && evtData.director.state) || "unknown";
      showAlertSafe("Director 状态变更: " + state, "info");
    } else if (evtType === "agent_join") {
      dispatchRender(evtData);
      showAlertSafe("Agent 加入", "success");
    } else if (evtType === "agent_leave") {
      dispatchRender(evtData);
      showAlertSafe("Agent 离线", "warning");
    }
  });
})();
