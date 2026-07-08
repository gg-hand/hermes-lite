# Smart Campus 工程规划体系总览

> 本文档提取了项目中所有 CLAUDE.md、settings.json、Skill 定义、Agent 定义等提示词与配置文件，按照「工作空间 → 项目 → 后端 → 前端 → Skill → Agent → 工具链」的层级组织，完整呈现工程规划的全貌。

---

## 目录

1. [L0 — 工作空间层（Workspace Rules）](#l0--工作空间层workspace-rules)
2. [L1 — 项目层（Smart Campus 项目规则）](#l1--项目层smart-campus-项目规则)
3. [L2 — 后端层（smart-campus-java）](#l2--后端层smart-campus-java)
4. [L3 — 前端层（smart-campus-front）](#l3--前端层smart-campus-front)
5. [L4 — Skill 层（可复用脚手架）](#l4--skill-层可复用脚手架)
6. [L5 — Agent 层（专用分析角色）](#l5--agent-层专用分析角色)
7. [L6 — 工具链（配置与记忆）](#l6--工具链配置与记忆)
8. [体系全景图](#体系全景图)

---

## L0 — 工作空间层（Workspace Rules）

**文件**：`/CLAUDE.md`（工作空间根）

```markdown
# Workspace Rules

前后端分离项目工作空间

## 技术规范

后端统一：
- SpringBoot 3.x / MySQL 8 / Redis

前端统一：
- Vue3 / JavaScript / Vite / Element Plus

## 接口规范
- 统一接口风格
- 接口名称必须带模块前缀
- 示例：courseInfo/loadCourseInfoList、courseInfo/getCourseInfo

## 输出要求
- 所有代码必须可直接运行
- 禁止伪代码 / 禁止省略 import/require
- 优先复用现有模块、工具、枚举
- 新增文件必须明确标注完整路径

## 目录规则
- admin：后台管理接口
- web：用户端接口
- common：公共模块
- front-admin：管理后台前端
- front-web：用户端前端

## 开发原则
- 保持与现有代码风格完全一致
- 保持命名规范统一
- 严格分离 PO / DTO / VO
- 统一返回 ResponseVO<T>
  { status:"success", code:200, info:"成功", data:T }

## 禁止行为
- 禁止修改无关代码 / 禁止随意升级依赖
- 禁止破坏现有接口 / 禁止覆盖手动修改的代码
```

> **设计意图**：工作空间层定义的是「**技术选型基线 + 最基础的规范共识**」——不绑定任何具体业务，确保工作空间内所有项目遵循相同的技术栈和代码风格。它是所有下层规范的最顶层约束。

---

## L1 — 项目层（Smart Campus 项目规则）

**文件**：`smart-campus/CLAUDE.md`

这是项目的**宪法级**文档，定义了项目的全部核心规则：

### 1. 总体架构

```
高校在线学习平台，前后端分离，后端拆成两个独立服务（管理端 + 用户端），
各自带独立拦截器与登录态。
```

### 2. 后端模块结构

| 模块 | 包路径 | 职责 |
|------|--------|------|
| smart-campus-common | `com.smart.campus.*` | PO / DTO / VO / Query、Service / Mapper、Redis 组件、异常、枚举、工具类 |
| smart-campus-admin | `com.smart.admin.*` | 管理端 Controller、Biz、管理端专用 DTO/VO；独立启动 |
| smart-campus-web | `com.smart.web.*` | 用户端 Controller、Biz、用户端专用 DTO/VO；独立启动 |

**依赖方向**：`admin → common`、`web → common`（common 不允许反向依赖）

### 3. 前端工程结构

| 工程 | 目录 | 服务对象 | Token Header |
|------|------|----------|-------------|
| 管理后台前端 | `smart-campus-front-admin` | 管理员 / 教师 | `adminToken` |
| 学生端前端 | `smart-campus-front-web` | 学生 | `studentToken` |

### 4. 系统角色

| 角色 | roleType | 权限范围 |
|------|----------|----------|
| 系统管理员 | 0 | 基础数据 + 资源 + 教学业务 + 权限菜单 + 系统通知 |
| 教师 | 1 | 维护资源 + 自己名下课程的教学业务 |
| 学生 | 2 | 学习课程 + 在线考试 + 学习分析 |

### 5. 接口与路径规范

```
全局上下文：/api（由 server.servlet.context-path 提供，路径中不再写 admin/web）
路径形式：/api/<模块名>/<动作>
常用动作：loadDataList / add / update / delete / detail / getXxxOptions
管理接口归 admin 模块、用户接口归 web 模块；两端互不复用 Controller
```

### 6. 鉴权与权限体系

```
管理端 token → header: adminToken → AdminLoginInterceptor → Redis 校验
用户端 token → header: studentToken → WebLoginInterceptor → Redis 校验
管理端接口必须打 @AdminPermission 注解
当前登录用户统一从 LoginUserContextHolder（ThreadLocal）取
```

### 7. 核心业务域

| 业务域 | 包含实体 | 关键规则 |
|--------|----------|----------|
| 基础数据 | 院系 → 专业 → 班级 → 用户 | 级联关系、状态启停 |
| 教学业务 | 课程 → 章节 → 课时 → 课时资源 | 课程归属教师、班级选课 |
| 习题与试卷 | 题库 + 题目选项 + 试卷 + 试卷题目关联 | 多题型、试卷复用 |
| 考试 | 考试信息 + 考试班级关联 | 客观题自动评分、主观题人工 |
| 学习数据 | 课程进度 + 课时进度 + 学习日志 | 视频续播、Redis 缓冲落库 |
| 资源 | 资源目录树 + 文件（视频/文档） | 分片上传、Redisson 队列异步转码 |
| 系统 | 菜单 + 角色菜单 + 通知 + 站内消息 | RBAC、消息推拉 |

### 8. 字段与返回约定

```
- 后端统一返回 ResponseVO<T>，不直接返回 Entity/PO
- 分页参数继承统一基类，结果使用统一分页结果 VO
- 数据库字段下划线 ↔ Java 字段驼峰（MyBatis 自动映射）
- 前端字段保持与后端驼峰一致，禁止前端起别名
- 时间字段统一格式化为 yyyy-MM-dd HH:mm:ss（GMT+8）
- 主键：业务实体 String UUID，基础数据自增整数
```

### 9. 禁止行为清单

```
- 禁止前后端字段不一致或私自重命名 PO 字段
- 禁止生成 mock 数据
- 禁止管理端接口不带权限注解
- 禁止用户端接口暴露管理端字段
- 禁止把仅一端使用的类放进 common 模块
- 禁止 Controller / Biz / Service / Mapper 越层
```

---

## L2 — 后端层（smart-campus-java）

**文件**：`smart-campus-java/CLAUDE.md`

L1 的项目规则定义「做什么」，L2 的后端规则定义「后端具体怎么做」。

### 1. 分层架构

```
Controller → Biz → Service → Mapper → 数据库
```

| 层级 | 位置 | 职责 | 禁止 |
|------|------|------|------|
| Controller | admin/web | 参数接收校验、调用 Biz、返回统一响应 | 不写业务逻辑 |
| Biz | admin/web | 业务编排（权限校验、跨 Service 组合、组装 VO） | 不直接操作 Mapper |
| Service | common | 单领域业务逻辑，事务边界 | 不操作 RedisTemplate |
| Mapper | common | 单表 CRUD / 简单连表，XML 复杂查询 | 不字符串拼接 SQL |

### 2. 实体类分工

| 类型 | 位置 | 说明 |
|------|------|------|
| PO | common/entity/po | 表映射，Date 用统一 JSON 格式化 |
| DTO | common/entity/dto | 接收前端入参，分组校验注解 |
| VO | common/entity/vo | 返回前端结构；端专用可放端模块 |
| Query | common/entity/query | 分页查询参数，继承基类，Fuzzy 后缀模糊匹配 |

### 3. Controller 规范

```
- @RestController + @RequestMapping("/<模块名>") + @Validated
- 管理端再加 @AdminPermission（类级默认编码，方法级覆盖）
- 继承 ABaseController，用 getSuccessResponseVO() 等，不 new 响应对象
- 路径中不要写 /api/admin/ 或 /api/web/
```

### 4. 鉴权实现

```
管理端：AdminLoginInterceptor 读 header adminToken
     → AdminLoginRedisComponent.get(token)
     → LoginUserContextHolder.set(loginUser)
     → checkPermission（@AdminPermission 注解）
     → afterCompletion 清理 ThreadLocal

用户端：WebLoginInterceptor 读 header studentToken
     → WebLoginRedisComponent.get(token)
     → LoginUserContextHolder.set(loginUser)
     → afterCompletion 清理 ThreadLocal
```

### 5. 统一返回与异常

```
- 所有 Controller 返回 ResponseVO<T>（status/code/info/data）
- 业务错误码集中维护在 ResponseCodeEnum（200/404/500/600/601/901）
- 业务异常一律抛 BusinessException
- 全局异常处理 AGlobalExceptionHandlerController 统一转响应
- 禁止 catch 后吞掉错误
```

### 6. 分页规范

```
1. Query 对象继承 BaseParam（pageNo/pageSize/simplePage）
2. 先 selectCount → 构造 SimplePage → 塞回 Query → selectList
3. 封装为 PaginationResultVO（totalCount/pageNo/pageSize/list）
4. 默认页大小由 PageSize 枚举提供（推荐 15/20）
```

### 7. Redis 使用

```
已有 Redis 组件（直接复用，不造新工具类）：
- AdminLoginRedisComponent / WebLoginRedisComponent（登录态）
- CourseStudyProgressRedisComponent（学习进度缓存）
- ResourceUploadSessionRedisComponent（分片上传 Session）
- ResourceTaskQueueRedisComponent（转码任务队列）
- 统一操作入口：WebLoginRedisComponent.RedisUtils
```

### 8. 禁止行为（后端专项）

```
- Controller 写 SQL 或操作 RedisTemplate
- 直接返回 Entity/PO 给前端
- 循环里调 Mapper（N+1）
- 吞异常 / 自定义响应结构绕过统一返回
- common 模块新增仅一端使用的类
- 管理端 Controller 不带权限注解
- 使用 MyBatis Plus 风格 API（Wrapper / IService）
- admin 复用 web 的 Controller 或反之
```

---

## L3 — 前端层（smart-campus-front）

**文件**：`smart-campus-front/CLAUDE.md`

### 1. 工程定位

| 方面 | 管理后台 (admin) | 学生端 (web) |
|------|------------------|--------------|
| 技术栈 | Vue 3 + JS + Pinia + Element Plus | 同左 |
| Token header | `adminToken` | `studentToken` |
| 代理目标 | 管理端后端 (6061) | 用户端后端 (6060) |
| 代码共享 | **不共享** | **不共享** |

### 2. 编码规范

```
- 全部 <script setup> + Composition API
- 接口请求统一封装到 src/api/<module>.js，由统一 axios 实例发出
- 禁止在页面/组件直接 import axios
```

### 3. 目录约定（两端通用）

```
src/api/       → 业务模块按文件拆分 + 统一请求入口
src/views/     → 页面，按业务子目录组织
src/components/→ 公共组件（管理端以 Base 前缀命名）
src/stores/    → Pinia store（登录态 + 业务）
src/router/    → 路由配置；管理端增加菜单配置文件
src/utils/     → 请求封装、token、消息/确认、业务工具
src/assets/    → 样式、图标、图片
```

### 4. 请求层约定

```javascript
// Axios 实例
baseURL: `${import.meta.env.PROD ? import.meta.env.VITE_DOMAIN : ''}/api`
timeout: 10 * 1000

// 请求拦截器：从登录态读 token → 写入对应 header
// 响应拦截器：
//   code=200 → 返回 responseData.data
//   code=901 → 清 token + 跳转登录页（带 redirect）
//   其他 error → 默认弹错（调用方可关闭）
```

### 5. 路由与权限

```
管理端：
  - 路由集中维护，与 adminMenu.js 同步
  - meta 至少包含 requiresAuth、menuCode 两项
  - 路由守卫：未登录跳转 + 菜单权限校验
  - 按钮级权限：authStore.hasMenu(code) 控制 v-if

学生端：
  - 所有学习页面需要登录
  - 无角色细分，只校验登录态
```

### 6. 管理后台 UI 约束（强制）

```
- 页面三段式：搜索区 → 表格 → 分页
- 列表必须分页；用标准表格组件，不直接堆 el-table
- 新增/编辑用独立弹窗组件（FormDialog）
- 删除必须二次确认
- 操作列固定为 查看/编辑/删除，权限控制显隐
- 状态用 el-tag 展示
- 下拉筛选立即触发搜索
```

### 7. 学生端 UI 约束

```
- 卡片/列表/图表布局，禁止套用后台表格 UI
- 视频统一走封装播放组件（HLS + 续播）
- PC 优先，移动端按需加媒体查询
```

### 8. 状态管理

```
- 登录 store 负责：token、用户信息、菜单树、菜单编码列表
- token 通过 localStorage 同步读写，不引入 pinia 持久化插件
- 业务 store 按需建，命名 useXxxStore
```

### 9. 环境变量

```
- 后端域名、代理目标、超时时间、端口走 VITE_* 环境变量
- 禁止硬编码到代码里
```

### 10. 禁止行为（前端）

```
- 页面/组件直接 import axios
- 硬编码后端地址或绝对 URL
- 接口请求逻辑写在 views/ 里
- 单文件组件 > 500 行不拆
- 内联样式（统一用 SCSS / scoped）
- 学生端使用后台表格 UI
- 管理后台引入移动端响应式方案
- 改动字段命名映射（必须与后端一致）
```

---

## L4 — Skill 层（可复用脚手架）

Skill 是在 CLAUDE.md 之上的「**领域知识封装**」——每个 Skill 封装一个高频任务场景的全部约束、逻辑和清单。

### L4-1. `smart-campus-java` — 后端业务专项

**职责**：业务领域知识 + 归属判定规则（配合 CLAUDE.md 使用）

```
## 业务域 → 表映射
基础数据 → department_info / major_info / class_info / user_info
课程结构 → course_info → course_chapter → course_chapter_lesson → ...
选课     → course_class（课程-班级中间表）
题库     → question_info + question_option
试卷     → paper_info + paper_question
考试     → exam_info + exam_class
作业     → course_assessment_submit + course_assessment_submit_question
学习     → course_study_progress + course_study_lesson_progress + course_study_log
资源     → resource_info（树形）
消息     → message_info + message_user
权限     → system_menu + system_role_menu

## 角色边界（业务必须显式判定）
- 管理员：全部
- 教师：只能管理自己名下课程（teacher_id = 当前用户 ID）
- 学生：通过班级访问课程

## 业务约束
- 删除课程要级联清理章节/课时/资源/进度
- 客观题自动评分，主观题人工评分
- 试卷通过中间表组装，同一题目可被多张试卷复用
- 考试通过 exam_class 按班级开考
- 学习时长先写 Redis 再异步落库
- 资源上传走分片 Session + 异步任务队列
- 消息/通知都走"主表 + 用户关联表"两层结构

## 接口命名规范
- 模块名小驼峰：courseInfo / courseChapter / examInfo / paperInfo / ...
- 动作：loadDataList / add / update / delete / detail / ...

## 自检清单
- 课程/试卷/考试操作是否校验了主从归属关系
- 教师视角接口是否过滤 teacher_id = 当前用户
- 学生视角接口是否按 班级→课程 链路过滤
- 管理端接口权限编码与菜单表是否一致
- 学习记录写入是否走了 Redis 缓冲
```

### L4-2. `smart-campus-front` — 前端工程入口

**职责**：跨页面/工程级话题（请求层、登录态、路由、env、字段一致性）

```
## 两个工程共享的"公共基建"
- 请求封装：统一的 axios 实例 + 拦截器
- token 工具：localStorage 读/写/清三个函数
- 登录 store：Pinia auth store（token + userInfo + menuList + hasMenu）
- 路由配置：路由表 + 守卫；管理端再加菜单配置文件
- 消息/确认工具：封装 ElMessage / ElMessageBox
- 业务通用工具：日期格式化、文件大小格式化等

## 请求层规范
- baseURL 固定 /api，生产环境拼 VITE_DOMAIN
- 超时默认 10s，可调
- 登录失效统一由拦截器跳转

## 字段一致性原则
- 接口字段名严格沿用后端 PO/VO（驼峰）
- 列表返回结构固定（totalCount/pageNo/pageSize/list）
- 时间统一字符串 yyyy-MM-dd HH:mm:ss
- 枚举值保持后端定义

## 自检清单
- 没有页面直接 import axios
- token header 仅请求拦截器一处注入
- 登录失效由响应拦截器统一处理
- 环境差异通过 import.meta.env.VITE_* 控制
- 新增菜单时同步配 meta.menuCode 与后端
- admin 用标准表格组件，web 用卡片+视频组件
```

### L4-3. `java-module` — 通用 CRUD 脚手架

**职责**：新模块骨架生成（文件清单、命名、分层边界）

```
## 前置确认三件事
1. 服务端归属 - admin 还是 web？
2. 表是否已存在？
3. 是否复用现有领域？

## 新模块文件清单
common 模块：
  PO → entity/po/     Query → entity/query/
  DTO → entity/dto/   VO → entity/vo/
  Mapper → mappers/（接口 + XML）
  Service → service/ + service/impl/

admin 或 web 模块：
  Biz → biz/XxxAdminBiz / XxxWebBiz
  Controller → controller/XxxController
  端专用 DTO/VO → 端模块 entity/

## 命名约定
  XxxInfo（PO） / XxxInfoQuery / XxxSaveDTO
  XxxDetailVO / XxxInfoMapper / XxxInfoService / XxxServiceImpl
  XxxAdminBiz / XxxInfoController

## 各层边界（必须遵守）
  Controller：收→派发→回，不写业务判断
  Biz：编排（权限、跨 Service、组装 VO）
  Service：单领域，事务边界
  Mapper：单表 CRUD + XML 复杂查

## 自检清单（10 项）
  包路径正确 / 列表继承分页 / 权限注解对齐
  无 N+1 / 时间格式化 / 无 MP API
  XML 放 common / 端专用不放 common
  异常不吞 / 不裸返 PO
```

### L4-4. `vue-admin-page` — 管理后台页面

**职责**：三段式管理页面的完整实现规范

```
## 一个完整管理页的产物
- 列表页：views/<业务>/XxxManagement.vue
- 表单弹窗：views/<业务>/XxxFormDialog.vue
- 接口封装：api/<模块>.js
- 路由/菜单配置

## 列表页结构
搜索区 → [操作按钮] → 表格（标准封装）→ 分页

## 表单弹窗结构
- 独立文件，props: show/mode(create/edit/view)/data/options
- events: close/submit
- 查看模式整表 disabled，不显示保存按钮
- 提交顺序：校验 → 调接口 → 弹成功 → 通知父组件刷新

## 自检清单（10 项）
  三段式齐全 / 弹窗独立 / 删除二次确认
  el-tag 状态 / 操作列固定 / 下拉立即查询
  字段名与后端一致 / 接口只走 api 文件
  无内联样式 / 权限控制按钮
```

### L4-5. `vue-web-page` — 学生端页面

**职责**：学生端学习场景页面的实现规范

```
## 页面布局原则
- 顶部：标题 + 搜索 + 排序
- 筛选：el-tabs（全部/进行中/已完成/未开始）
- 统计：横向卡片（已学课程/学习时长/平均成绩）
- 列表：卡片网格（封面/标题/教师/进度条/时长）—— 禁止 el-table
- 详情/学习：左右双栏（章节树 + 视频区）
- 移动端：媒体查询堆叠单列

## 视频学习规范
- 统一使用项目封装的播放组件
- 进入课时取 lastPositionSeconds，传给播放器
- 节流 5s 一次上报进度，接口设静默错误
- 切换课时/离开页面主动上报一次

## 自检清单（8 项）
  无 el-table / 视频走封装组件 / 字段名一致
  接口只走 api 文件 / 有媒体查询
  错误走消息工具 / 进度上报静默
  未引入后台专用依赖
```

---

## L5 — Agent 层（专用分析角色）

### `tech-implementation-tracker`

**类型**：自定义 Agent（Sonnet 模型，cyan 色）
**职责**：追踪和理解复杂技术实现（分片上传、视频转码、学习进度缓存等）

```
## 三阶段分析流程
Phase 1 - 思路分析：核心问题 + 架构分解 + 数据流 + 设计决策
Phase 2 - 代码索引：按模块组织所有参与文件的完整索引
Phase 3 - 实现原理：逐组件解释代码逻辑 + 设计理由 + 边界处理

## 输出格式
# [Feature] 技术实现追踪
## 一、思路分析
## 二、代码索引（后端/前端/配置/SQL）
## 三、实现原理（3.1/3.2/...）
## 四、数据流转全景

## 自检清单
- 所有路径已验证 / API 命名符合规范
- PO/DTO/VO 分离正确 / 响应格式正确
- 前后端契约一致 / 无推测性内容
- 错误处理已覆盖 / 异步处理已说明

## 持久性记忆
- 项目核心技术模式已存入 agent-memory（8 个模式）
  - 分片上传模式 / 视频转码模式 / 学习进度缓存+批量刷库
  - 学习分析聚合 / 脏数据校验3层 / 文件清理模式
  - 双Token认证体系 / Mapper框架模式
  - 全局约定：Redis键空间 / 时间格式 / 文件存储 / 异常处理
```

---

## L6 — 工具链（配置与记忆）

### settings.json — 权限与开关

**工作空间级**（`./claude/settings.json`）：
```json
{
  "permissions": {
    "allow": [
      "Bash(mkdir -p ...)",
      "Bash(docker compose *)",
      "Read(//d/JAVADATA/case/ai-mall/**)"
    ],
    "additionalDirectories": [...]
  },
  "customSkills": [
    ".claude/skills/java-module.md",
    ".claude/skills/vue-admin-page.md",
    ".claude/skills/vue-web-page.md",
    ".claude/skills/smart-campus-java.md",
    ".claude/skills/smart-campus-front.md"
  ],
  "projectContextFile": "CLAUDE.md",
  "autoLoadSkills": true
}
```

**项目级**（`smart-campus-java/.claude/settings.json`）：
```json
{
  "permissions": {
    "allow": [
      "Bash(ls -la ...)",
      "Read(//e/Java/study/vibe-coding/smart-campus/**)"
    ]
  }
}
```

### 记忆系统

```
.claude/projects/e--Java-study-vibe-coding/memory/
├── MEMORY.md                          ← 索引（自动加载到上下文）
└── smart-campus-interview-guide.md    ← 项目面试记忆

smart-campus-java/.claude/agent-memory/tech-implementation-tracker/
├── MEMORY.md                          ← Agent 记忆索引
└── project-patterns.md               ← 8大核心技术模式持久记忆
```

---

## 体系全景图

```
L0  工作空间规则（Workspace Rules）
     ├── 技术选型基线（SpringBoot/Vue3/MySQL/Redis）
     ├── 最基础规范（接口/输出/目录/原则）
     └── 红线（不改无关代码/不升级依赖/不覆盖手动代码）
      │
L1   项目规则（Smart Campus CLAUDE.md） ← 宪法级
     ├── 架构：双端分离 + Maven 多模块
     ├── 角色：管理员 / 教师 / 学生
     ├── 接口规范：/api/<模块>/<动作>
     ├── 鉴权：双 Token + ThreadLocal + RBAC
     ├── 业务域定义：7 大域完整划定
     └── 红线：10 条禁止行为
      │
L2   后端规则（smart-campus-java CLAUDE.md） ← 后端实现法
     ├── 分层：Controller → Biz → Service → Mapper
     ├── 实体：PO / DTO / VO / Query
     ├── 鉴权实现：拦截器 + Redis + 注解
     ├── 分页：先 count → 塞分页 → 查列表
     ├── Redis：5 个现成组件直接复用
     └── 红线：9 条后端禁止行为
      │
L3   前端规则（smart-campus-front CLAUDE.md） ← 前端实现法
     ├── 两个独立工程，互不共享代码
     ├── 请求层封装 + 响应拦截器
     ├── 路由 + 权限体系
     ├── 管理 UI：三段式 + 弹窗 + 标准表格
     ├── 学生 UI：卡片 + 视频 + 图表
     └── 红线：8 条前端禁止行为
      │
L4   Skill 层（5 个自定义 Skill）
     ├── smart-campus-java    — 后端业务领域知识
     ├── smart-campus-front   — 前端工程级话题
     ├── java-module          — CRUD 脚手架
     ├── vue-admin-page       — 管理后台页面
     └── vue-web-page         — 学生端页面
      │
L5   Agent 层（1 个自定义 Agent）
     └── tech-implementation-tracker — 技术实现追踪
          └── 持久化记忆：8 大核心技术模式
      │
L6   工具链
     ├── settings.json（权限 + Skill 注册 + 自动加载）
     ├── 项目 settings.json（子项目权限）
     └── 记忆系统（项目记忆 + Agent 记忆）
```
