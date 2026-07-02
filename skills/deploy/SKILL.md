---
name: deploy
version: 1.0.0
description: SSH/SCP 远程部署工具集，支持远程执行命令和文件上传
requires: []
---

# deploy

通过 SSH 和 SCP 协议远程管理服务器、上传文件、执行部署命令的工具集。

## 工具列表

- `ssh_run` — 在远程服务器执行 shell 命令，返回 stdout/stderr
- `scp_push` — 将本地文件或目录上传到远程服务器

## 使用示例

```python
# 检查服务器连通性
ssh_run(host="1.2.3.4", username="root", command="uptime")

# 上传项目并部署
scp_push(host="1.2.3.4", username="root", local_path="./deploy.sh", remote_path="/opt/hermes-lite/")
ssh_run(host="1.2.3.4", username="root", command="cd /opt/hermes-lite && sudo ./deploy.sh")
```
