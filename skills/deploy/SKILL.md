---
name: deploy
version: 1.1.0
description: SSH/SCP 远程部署工具集，支持远程执行命令和文件上传
requires: ["paramiko"]
---

# deploy

通过 SSH 和 SCP 协议远程管理服务器、上传文件、执行部署命令的工具集。

## 依赖

``pip install paramiko``

## 函数列表

脚本资源位于 `scripts/deploy.py`，可通过 `skill__resource(name="deploy", rel_path="scripts/deploy.py")` 读取源码。

### ssh_run_handler(host, username, command, port=22, password=None, key_path=None, timeout=60) -> dict
在远程服务器执行 shell 命令，返回 `{stdout, stderr, returncode, success}`。

### scp_push_handler(host, username, local_path, remote_path, port=22, password=None, key_path=None, recursive=False, timeout=120) -> dict
将本地文件或目录上传到远程服务器，返回 `{stdout, stderr, returncode, success}`。

## 调用方式

本 Skill 的函数需在 Python 环境中调用（依赖 paramiko 库），不支持 CLI 直接调用。如需调用，请通过 `bash_exec` 执行 Python 脚本：

```bash
python -c "from skills.deploy.scripts.deploy import ssh_run_handler; print(ssh_run_handler(host='1.2.3.4', username='root', command='uptime', password='xxx'))"
```

## 使用示例

```python
# 检查服务器连通性
ssh_run_handler(host="1.2.3.4", username="root", command="uptime", password="xxx")

# 上传项目并部署
scp_push_handler(host="1.2.3.4", username="root", local_path="./deploy.sh", remote_path="/opt/teage-liu/", password="xxx")
ssh_run_handler(host="1.2.3.4", username="root", command="cd /opt/teage-liu && sudo ./deploy.sh", password="xxx")
```
