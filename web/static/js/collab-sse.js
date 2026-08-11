/* ============================================================
   collab-sse.js — 协作消息 SSE 通道订阅
   端点：GET /api/multiagent/collab/sse
   事件：data: {type: "collab_message_append", data: {message: {...}}}

   与 multiagent-sse.js 的区别：
   - multiagent-sse.js 订阅 /api/multiagent/sse（Director 健康/自治状态变更）
   - collab-sse.js 订阅 /api/multiagent/collab/sse（协作消息流增量推送）

   暴露：window.CollabSSE = { start, stop, isConnected }
   事件分发：window.dispatchEvent(new CustomEvent("collab-message", {detail: msg}))
   ============================================================ */
(function () {
  "use strict";

  var eventSource = null;
  var reconnectTimer = null;
  var reconnectAttempts = 0;
  var maxReconnectDelayMs = 30000;
  var baseReconnectDelayMs = 3000;
  var connected = false;

  /** 派发 SSE 连接状态变更事件，供 UI 更新指示灯
   * @param {string} state - connected/reconnecting/disconnected
   * @param {number} [attempts] - 重连次数（reconnecting 时有效）
   */
  function _dispatchState(state, attempts) {
    try {
      window.dispatchEvent(new CustomEvent("collab-sse-state", {
        detail: { state: state, attempts: attempts || 0 }
      }));
    } catch (e) {
      // CustomEvent 在某些环境不可用，静默忽略
    }
  }

  /**
   * 启动 SSE 订阅。
   * 幂等：重复调用会先关闭旧连接再重建。
   */
  function start() {
    // 若协作面板未显示，不启动（节省连接）
    if (!_isWorkbenchVisible()) {
      // 延迟到面板显示时启动
      _scheduleDeferredStart();
      return;
    }
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    try {
      eventSource = new EventSource("/api/multiagent/collab/sse");
    } catch (e) {
      console.warn("collab SSE: EventSource 初始化失败", e);
      scheduleReconnect();
      return;
    }

    eventSource.onopen = function () {
      connected = true;
      reconnectAttempts = 0;
      _dispatchState("connected");
    };

    // 服务器以 default message 事件推送（data: {type, data: {message}}）
    eventSource.onmessage = function (e) {
      try {
        var payload = JSON.parse(e.data);
        var msg = payload && payload.data && payload.data.message;
        if (!msg) return;
        // 分发统一事件供 collab-workbench.js 渲染
        window.dispatchEvent(
          new CustomEvent("collab-message", { detail: msg })
        );
      } catch (err) {
        console.warn("collab SSE: message 解析失败", err);
      }
    };

    eventSource.onerror = function () {
      connected = false;
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      // 404 / 403 时不再重连（multiagent 未启用或权限不足）
      // EventSource 无法直接读取 HTTP 状态，依赖 readyState 判断
      _dispatchState("reconnecting", reconnectAttempts + 1);
      scheduleReconnect();
    };
  }

  /**
   * 指数退避重连
   */
  function scheduleReconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
    }
    reconnectAttempts += 1;
    // 指数退避：3s, 6s, 12s, 24s, 30s（封顶）
    var delay = Math.min(
      baseReconnectDelayMs * Math.pow(2, reconnectAttempts - 1),
      maxReconnectDelayMs
    );
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      start();
    }, delay);
    console.warn("collab SSE: 连接失败，" + delay + "ms 后重连（第 " + reconnectAttempts + " 次）");
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
    connected = false;
    reconnectAttempts = 0;
    _dispatchState("disconnected");
  }

  /**
   * 是否已连接
   */
  function isConnected() {
    return connected && eventSource !== null;
  }

  /**
   * 检查协作面板是否可见。
   * 若 #collabPane 不存在（chat 页面），返回 true 表示直接启动 SSE。
   * 若 #collabPane 存在（工作台页面），仅在面板可见时返回 true。
   */
  function _isWorkbenchVisible() {
    var pane = document.getElementById("collabPane");
    if (!pane) return true; // chat 页面无 #collabPane，直接启动
    return !pane.hidden;
  }

  /**
   * 当面板未显示时，监听 director-on 切换，显示时启动。
   * 仅在工作台页面（有 #collabPane）有效。
   */
  var deferredScheduled = false;
  function _scheduleDeferredStart() {
    if (deferredScheduled) return;
    deferredScheduled = true;
    var pane = document.getElementById("collabPane");
    if (!pane) return; // chat 页面无需延迟
    var observer = new MutationObserver(function () {
      if (!pane.hidden && !eventSource) {
        start();
      }
    });
    observer.observe(pane, { attributes: true, attributeFilter: ["hidden"] });
  }

  // 导出全局
  window.CollabSSE = {
    start: start,
    stop: stop,
    isConnected: isConnected,
  };

  // 页面卸载时清理
  window.addEventListener("beforeunload", function () {
    stop();
  });
})();
