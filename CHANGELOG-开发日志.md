开发日志 —— 本次变更摘要

对比基准：origin/dev @ 3420269 → 当前工作树
统计：80 文件修改（+9228/-3025），73 新增文件，1 删除文件

---

新功能

- 通用 Workflow 引擎：调度从单模板升级为通用多步引擎，支持 retry/fallback/skip/abort 四种错误策略、拓扑排序、条件跳过。6 个新模块：engine/spec/adapter/retry/step_executor/step_trace/validator
- 画像信号池：三层信号摘取统一入口，信号达阈值 7 次才写入画像。解决"一次提及即写入"导致的画像噪音。持久化到 data/profile_signal_pool.json
- 监控 + 调度独立页面：调度从 chat 内嵌面板拆为独立 /scheduler 页；新增实时监控页 /monitor（Canvas 直方图、健康检查、审计日志）
- Metrics 持久化：监控指标增量持久化到 SQLite，每日合并，支持趋势查询
- 统一工具错误处理：17 种结构化异常替代字符串错误，按 pre_execution/execution/protocol 三阶段分流处理
- B站 Skill：热门视频、搜索、视频详情、UP主信息、分区排行榜

优化

- ReactLoop 重构：弱引用防 GC 泄漏；下线过敏感卡死检测规则；中断提示跨轮注入
- 前端拆分：删除 chat-schedule.js（895 行），拆为独立调度页 + 轻量徽章轮询
- 审批卡片增强：按工具类别（文件/Skill/MCP/Shell/记忆）差异化渲染
- TodoList 持久化：从纯内存升级为磁盘原子写 + 懒加载恢复
- 会话标题：cron 会话标题取自 schedule.name，用户会话首轮异步生成
- 移动端适配：侧栏遮罩层、响应式 CSS
- 安全：新增 read_paths 黑白名单，保护 src/、config.yaml、.git/ 等敏感路径

Bug 修复

- 定时清理遗漏 todo/ 子目录（已补）
- 审批缺少拒绝原因字段（已加）
- WorkflowResult 异常路径返回 None 导致空指针（已修复）
- 调度 API 缺少 workflow 字段（已补）

测试

- 新增 24 个测试文件，修改 21 个
- 核心覆盖：Workflow 引擎全套、信号池、MetricsStore、ToolError、Todo 持久化、监控页面
