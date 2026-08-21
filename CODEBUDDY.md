# CODEBUDDY.md

> 本文件供 CodeBuddy 打开仓库时自动加载，是 AI 理解本项目的**认知入口**。
> 详细架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，子系统接入规范见 [docs/SUBSYSTEM-SPI.md](docs/SUBSYSTEM-SPI.md)。

## 项目一句话

自托管的个人 AI Agent（Demo 项目）：长期对话、记忆沉淀、自主任务执行（Cron + Workflow）、多智能体协作（Multi-Agent）。FastAPI 单进程服务 + 纯 HTML/CSS/JS 前端，零外部基础设施（除 LLM API）。

## 双系统（最重要！改代码前先定位）

| 系统 | 路径 | 状态 | 架构 |
|------|------|------|------|
| 老系统 | `teage_liu/` | **已冻结**（只修 bug） | 单体 DI 容器 + Orchestrator |
| 新系统 | `teage_liu2/` | **开发主线**（新功能落这里） | 主干-枝干（core + branches + server） |

新系统依赖铁律：主干不知道枝干名 / 枝干互不可见（只经 `BranchContext.extra` 通信）/ 外壳只依赖 core。
权威契约：`teage_liu2/docs/CORE.md`（主干接口）、`teage_liu2/docs/INTERFACES.md`（示例）。

## 常用命令

```bash
# 启动（Windows PowerShell 前台）
.\start.ps1
# 重启 / 停止
.\restart.ps1
.\stop.ps1
# 健康检查（默认端口 8000）
curl -s http://127.0.0.1:8000/health
# 老系统直启
python -m teage_liu
```

## 关键约定

- **配置**：`config.yaml` 用 `${VAR}` 占位注入 API Key，密钥放 `.env`（不入库）；本地覆盖配置（`config*.yaml`、`.bak`）不入库。
- **文档**：大改动先写 `docs/plans/YYYY-MM-DD-主题.md`，完成后更新 `CHANGELOG-开发日志.md`。
- **仓库纯净**：不写测试/临时脚本入库；git 写操作须经用户批准。
- **代码风格**：Python 3.11 全链路 async；降级优先，主对话不中断；前端无框架。

## 目录导航

- `teage_liu/` 老系统（冻结） | `teage_liu2/` 新系统（core 主干 / branches 枝干 / server 外壳）
- `docs/` 架构与计划（plans/ 演进记录） | `web/` 前端 | `scripts/` 运维脚本
- `skills/` 运行时技能（bilibili/calculator/deploy） | `cron_tool/` cron 子进程
- `data/` 运行时数据（不入库） | `desktop/` Tauri 桌面壳（由独立分支管理，不入库）
