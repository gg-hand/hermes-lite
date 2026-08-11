# 多 Agent 前端面板重构设计

> 日期：2026-07-23
> 状态：待审核
> 范围：teage-liu/web 前端多 agent 协作面板改造

## 1. 背景与问题

当前多 agent 前端设计存在两个核心问题：

### 1.1 遮挡问题
`multiagent-render.js` 通过 `document.body.appendChild` 挂载三个 `position:fixed` 元素，完全脱离页面布局：
- 状态指示器：`top:12px; right:12px`（右上角药丸）
- Agent 列表面板：`top:60px; right:12px; width:280px; max-height:60vh`（右侧大面板）
- 告警横幅：`top:60px; left:50%`（顶部居中）

这些元素覆盖在聊天消息区和输入按钮之上，遮挡正常操作。

### 1.2 指引缺失
面板仅被动展示 Director 状态和 agent 列表，用户不知道：
- 如何发起多 agent 协作（操作入口、指令语法）
- 能接入哪些 agent、接入要求是什么
- 支持哪些协作模式

## 2. 设计目标

1. **零遮挡**：移除所有 `position:fixed` overlay 元素，面板融入页面 flex 布局
2. **Director 工作台视角**：面板以"中间人"视角组织，不是被动状态展示
3. **操作指引内置**：面板内提供协作指引（怎么操作）、Agent 名册（能接入什么）、接入说明（接入要求）
4. **按需唤起**：多 agent 是中频进阶功能，默认不占空间，点击切换
5. **基于原有页面改造**：不推翻现有结构，在真实 DOM 上做增量改造
6. **侧栏可收缩**：面板展开时可收起侧边栏腾出操作空间

## 3. 真实页面结构（改造基线）

`chat.html` 现有结构（行号引用）：

```
<div class="app">
  <aside class="sidebar">                          // L24
    <div class="sidebar-header">Logo + 新建会话</div> // L26
    <nav class="sidebar-nav">                       // L31
      <button data-tab="sessions">会话</button>     // L32 tab-pane 切换
      <button data-tab="memories">记忆</button>     // L35 tab-pane 切换
      <button data-tab="files">文件</button>        // L38 tab-pane 切换
    </nav>
    <div class="sidebar-content">                   // L43
      <div id="sessionList" class="tab-pane">       // L45
      <div id="memoryPanel" class="tab-pane">       // L49
      <div id="filePanel" class="tab-pane">         // L71
    </div>
    <div class="sidebar-quicklinks">                // L78
      <a href="/scheduler">调度</a>                 // L79 跳转独立页
      <a href="/monitor">监控</a>                   // L87 跳转独立页
    </div>
    <div class="sidebar-footer">设置按钮</div>      // L94
  </aside>
  <main class="main">                               // L114
    <div class="main-header">...</div>              // L115
    <div class="messages">...</div>                 // L153
    <div class="input-area">...</div>               // L161
  </main>
</div>
```

关键事实：
- `sidebar-nav` 里的 nav-item（会话/记忆/文件）通过 `data-tab` **切换 sidebar 内的 tab-pane**
- 调度/监控是 `<a href>` **跳转独立页面**（/scheduler、/monitor），不是 tab 切换
- 设置按钮打开 `settingsFlyout`，高级设置打开 `settingsModal`（内含 `#multiagent-settings-container`）
- `#menuToggle`（L116）已有侧边栏切换能力
- 页面已有 `#toast`（L230）用于消息提示

## 4. 改造设计

### 4.1 触发入口：sidebar-nav 新增 Director nav-item

在 `.sidebar-nav`（L31）末尾新增一个 nav-item：

```html
<button class="nav-item nav-item--toggle" data-action="toggle-director" id="btnDirector">
  <span class="nav-icon">◎</span>
  <span class="nav-label">Director</span>
  <span class="nav-status-dot" id="directorStatusDot"></span>
</button>
```

**行为区别于现有 nav-item**：
- 会话/记忆/文件：`data-tab` → 切换 sidebar 内 tab-pane（互斥）
- Director：`data-action="toggle-director"` → **开关主区分栏面板**，不影响 sidebar tab 状态

这意味着 Director 可与会话/记忆/文件任一 tab 并存：用户在会话列表选会话时，Director 面板可保持展开。Director nav-item 的 active 态独立于 tab-pane active 态，用 `nav-item--toggle.active` 表示面板已展开。

Director nav-item 右侧的 `.nav-status-dot` 复用四色状态（绿健康/黄降级/橙自治/红故障/灰未启用），替代原 fixed 指示器。

### 4.2 主区分栏：chat-pane + collab-pane

将 `<main class="main">` 内的三个子元素包裹为 `.chat-pane`，新增 `.collab-pane` 作为兄弟：

```html
<main class="main">
  <div class="chat-pane">
    <div class="main-header">...</div>
    <div class="messages">...</div>
    <div class="input-area">...</div>
  </div>
  <section class="collab-pane" id="collabPane" hidden>
    <!-- Director 工作台 -->
  </section>
</main>
```

**布局规则**（CSS）：
- `.main` 默认 `flex-direction:column`（chat-pane 独占全宽）
- `.main.director-on` → `flex-direction:row`，`.chat-pane` 与 `.collab-pane` 各 `flex:1`（50/50）
- `.collab-pane` 默认 `hidden`，Director 激活时显示
- 窄屏（<1100px）调整为 55/45，避免聊天区过窄
- 过渡动画：collab-pane 用 `width` + `opacity` 过渡（`transition: width 0.28s, opacity 0.2s`），与现有侧栏动画节奏一致。注：`flex` 简写不可直接 transition，用 width 百分比或 flex-basis 实现

### 4.3 Director 工作台内容（collab-pane 内部）

```
.collab-pane
├── .wb-header          // 头像 + "Director 工作台" + 状态药丸
├── .wb-tabs            // 三个 tab：协作指引 / Agent名册 / 接入说明
├── .wb-body            // tab 内容区（滚动）
└── .wb-footer          // 收起按钮 + 分派任务按钮
```

**Tab 1 — 协作指引**（默认显示）：
- Director 状态条（健康/降级/自治/故障 + 在线 agent 数）
- "如何发起协作"步骤：`@director` 指令用法、分派任务按钮、追问/接力
- "支持的协作模式"：分派、接力、辩论

**Tab 2 — Agent 名册**：
- 在线/离线 agent 列表（id、role、状态、trust score）
- 复用现有 `renderAgents` 逻辑，渲染目标改为 `.collab-pane .agents-list`

**Tab 3 — 接入说明**：
- 接入新 agent 的必须项（agent_card.md、endpoint URL、4 个原子操作、相对路径）
- 推荐项（ed25519 签名、JSON-RPC 2.0、心跳间隔）
- 接入步骤（放置 agent_card → Director 发现 → heartbeat → 可分派）

### 4.4 移除遮挡元素

`multiagent-render.js` 的 `initContainers()` 当前创建并 `appendChild` 到 `document.body`：
- `#multiagent-indicator`（fixed 指示器）→ **删除**，状态改为 Director nav-item 上的 `.nav-status-dot` + collab-pane 头部药丸
- `#multiagent-agents-panel`（fixed 280px 面板）→ **删除**，内容迁入 collab-pane 的 Agent 名册 tab
- `#multiagent-alert-banner`（fixed 横幅）→ **删除**，告警改用页面已有的 `#toast`

### 4.5 侧栏收缩

复用现有 `#menuToggle`（L116）机制：
- `#menuToggle` 点击已能切换侧边栏显示/隐藏
- PC 端（>769px）当前 CSS 强制 `.sidebar { margin-left: 0 }`，需移除该强制规则，允许 `#menuToggle` 在 PC 端也生效
- Director 展开时，用户可手动点 `#menuToggle` 收起侧栏，聊天+面板各得近半屏宽度
- 不做自动收起（避免打断用户在会话列表的操作），由用户主动决定

### 4.6 不改动的部分

- **调度/监控**：仍是 `sidebar-quicklinks` 和 `main-header` 里的 `<a href="/scheduler">` `<a href="/monitor">` 跳转独立页，不动
- **settingsModal 里的 multiagent 配置**：`#multiagent-settings-container`（L193）保持原样，仍在高级设置弹窗里
- **multiagent-sse.js**：SSE 连接逻辑不变，仅状态更新回调指向新的渲染目标
- **multiagent-settings.js**：设置面板渲染逻辑不变

## 5. 文件改动清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `web/chat.html` | 改 | sidebar-nav 加 Director nav-item；main 内包裹 chat-pane + 新增 collab-pane；引入更新后的 multiagent.css/js |
| `web/css/multiagent.css` | 重写 | 删除 fixed 定位样式；新增 collab-pane、wb-header/tabs/body/footer、nav-status-dot 样式；复用 tokens.css 变量 |
| `web/js/multiagent-render.js` | 重写 | `initContainers` 改为初始化 collab-pane 内容；`updateStatus` 更新 nav-status-dot + wb-header；`renderAgents` 渲染到 collab-pane；`showAlert` 改调 `#toast`；新增 tab 切换逻辑 |
| `web/static/css/chat.css` | 小改 | `.main` 支持 `flex-direction:row`；新增 `.chat-pane` `.collab-pane` 布局规则；移除 PC 端 sidebar 强制可见规则 |
| `web/static/js/chat-main.js` | 小改 | 绑定 `#btnDirector` click → toggle `main.director-on` + `collab-pane` 显示；Director nav-item active 态切换 |

## 6. 数据流

```
multiagent-sse.js (SSE 事件)
  → multiagent-render.js updateStatus(status)
    → 更新 #directorStatusDot 四色
    → 更新 .wb-header 状态药丸
    → renderAgents(status.agents) → .collab-pane .agents-list
    → (如有告警) showAlert() → #toast

用户点击 #btnDirector
  → chat-main.js toggleDirector()
    → main.classList.toggle('director-on')
    → collab-pane.hidden = !active
    → #btnDirector.classList.toggle('active')

用户点击 @director 发送消息
  → 正常聊天发送流程
  → 后端 Director 处理 → SSE 推送协作进度
  → collab-pane 展示分派状态（如需）
```

## 7. 交互细节

### 7.1 Director nav-item 状态
- 未启用 multiagent：nav-item 显示灰色圆点，点击仍可展开面板（显示"未启用"状态 + 引导去设置开启）
- 已启用健康：绿色圆点
- 降级/自治/故障：对应黄/橙/红圆点
- 面板展开时：nav-item 加 `.active`（紫色高亮，与 tab 切换的 active 视觉一致但语义独立）

### 7.2 collab-pane 展开/收起
- 展开：`main` 加 `.director-on`，`collab-pane` 显示，flex 50/50
- 收起：点 nav-item 再次点击、或 collab-pane footer 的"收起"按钮
- 展开动画：flex 过渡 0.28s，与侧栏动画一致

### 7.3 协作消息流
Director 分派任务的过程以紫色 `collab` 气泡出现在聊天消息流中（如"🎯 Director · 分派给 researcher-01"），让用户在聊天区看到协作进度。collab-pane 侧展示 agent 状态详情。

### 7.4 响应式
- 宽屏（≥1100px）：Director 展开时 50/50
- 窄屏（<1100px）：55/45
- 移动端（<769px）：collab-pane 全屏覆盖聊天区（侧栏已是浮层模式）

## 8. 边界与错误处理

1. **multiagent 未启用**：collab-pane 仍可展开，显示"多 agent 未启用"提示 + "前往设置"按钮（打开 settingsModal）
2. **Director 离线/故障**：nav-status-dot 显红，collab-pane 头部状态药丸显红，指引 tab 仍可查看（只读）
3. **SSE 断连**：复用 multiagent-sse.js 现有重连逻辑，断连时 nav-status-dot 显灰
4. **无 agent 在线**：Agent 名册 tab 显示"暂无活跃 agents"空状态
5. **collab-pane 展开时切换会话**：面板保持展开，不收起（会话切换不影响 Director 模式）

## 9. 测试要点

1. **布局**：Director 展开/收起时聊天区不被遮挡；50/50 比例正确；窄屏降级 55/45
2. **nav-item 独立性**：点 Director 不影响会话/记忆/文件 tab 状态；三者可并存
3. **状态同步**：SSE 推送状态更新后，nav-status-dot 和 collab-pane 头部同步变色
4. **告警**：原 alert-banner 的告警改走 toast，5 秒自动消失
5. **侧栏收缩**：PC 端 menuToggle 能收起侧栏；收起后 Director 50/50 空间增大
6. **未启用态**：multiagent 未启用时面板可展开，显示引导
7. **调度/监控不受影响**：quicklinks 和 header 里的链接仍正常跳转独立页
8. **设置面板不受影响**：settingsModal 里 multiagent 配置段仍正常渲染

## 10. 不做的事（YAGNI）

- 不做拖拽调整 chat/panel 比例（固定 50/50，需要时再加）
- 不做 Director 智能推荐（需 LLM 判断，过重）
- 不做流程向导 wizard（操作指引 tab 已足够）
- 不重构 multiagent-sse.js 和 multiagent-settings.js（不在本次范围）
- 不改调度/监控的独立页跳转机制
