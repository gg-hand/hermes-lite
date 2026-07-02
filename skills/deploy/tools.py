"""deploy 技能：基于 paramiko 的 SSH/SCP 远程部署工具集。"""

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, Dict

import paramiko


# 默认超时配置（秒）
DEFAULT_CMD_TIMEOUT = 60       # 命令执行超时
DEFAULT_SCP_TIMEOUT = 120      # 单文件上传超时


TOOLS = [
    {
        "name": "ssh_run",
        "description": "在远程服务器执行 shell 命令，返回 stdout/stderr 和退出码。支持密码或密钥认证。",
        "handler": "ssh_run_handler",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "服务器 IP 或域名"},
                "username": {"type": "string", "description": "SSH 登录用户名"},
                "password": {"type": "string", "description": "SSH 登录密码（与 key_path 二选一）"},
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "port": {"type": "integer", "description": "SSH 端口，默认 22", "default": 22},
                "key_path": {"type": "string", "description": "SSH 私钥路径（与 password 二选一）"},
                "timeout": {"type": "integer", "description": "命令执行超时秒数（默认 60，超时自动中断）", "default": 60},
            },
            "required": ["host", "username", "command"],
        },
    },
    {
        "name": "scp_push",
        "description": "将本地文件或目录上传到远程服务器。支持密码或密钥认证。",
        "handler": "scp_push_handler",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "服务器 IP 或域名"},
                "username": {"type": "string", "description": "SSH 登录用户名"},
                "password": {"type": "string", "description": "SSH 登录密码（与 key_path 二选一）"},
                "local_path": {"type": "string", "description": "本地文件或目录路径"},
                "remote_path": {"type": "string", "description": "远程目标路径"},
                "port": {"type": "integer", "description": "SSH 端口，默认 22", "default": 22},
                "key_path": {"type": "string", "description": "SSH 私钥路径（与 password 二选一）"},
                "recursive": {"type": "boolean", "description": "是否递归上传目录，默认 false", "default": False},
                "timeout": {"type": "integer", "description": "单文件上传超时秒数（默认 120）", "default": 120},
            },
            "required": ["host", "username", "local_path", "remote_path"],
        },
    },
]


def _connect(host: str, username: str, port: int,
             password: str = None, key_path: str = None,
             connect_timeout: int = 15) -> paramiko.SSHClient:
    """建立 SSH 连接，支持密码或密钥认证。"""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if key_path:
        expanded_path = os.path.expanduser(key_path)
        pkey = paramiko.RSAKey.from_private_key_file(expanded_path)
        client.connect(host, port=port, username=username, pkey=pkey, timeout=connect_timeout)
    else:
        client.connect(host, port=port, username=username, password=password, timeout=connect_timeout)

    return client


def ssh_run_handler(
    host: str,
    username: str,
    command: str,
    port: int = 22,
    password: str = None,
    key_path: str = None,
    timeout: int = DEFAULT_CMD_TIMEOUT,
) -> Dict[str, Any]:
    """远程执行 shell 命令，返回 stdout/stderr/returncode。"""
    client = None
    try:
        client = _connect(host, username, port, password, key_path)
        # 打开一个 channel 执行命令
        channel = client.get_transport().open_session()
        channel.settimeout(timeout)
        channel.exec_command(command)

        # recv_exit_status 在 channel 超时后会抛 socket.timeout
        exit_status = channel.recv_exit_status()
        out = channel.makefile('rb', -1).read().decode("utf-8", errors="replace")
        err = channel.makefile_stderr('rb', -1).read().decode("utf-8", errors="replace")
        channel.close()
        return {
            "stdout": out,
            "stderr": err,
            "returncode": exit_status,
            "success": exit_status == 0,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"SSH 执行失败 ({type(e).__name__}): {str(e)}",
            "returncode": -1,
            "success": False,
        }
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


def _mkdir_p(sftp: paramiko.SFTPClient, remote_dir: str):
    """递归创建远程目录（类似 mkdir -p）。"""
    if remote_dir == "/":
        return
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        parent = os.path.dirname(remote_dir)
        _mkdir_p(sftp, parent)
        sftp.mkdir(remote_dir)


def _scp_put_with_timeout(sftp: paramiko.SFTPClient, local: str, remote: str, timeout: int):
    """在超时保护下上传单个文件。"""
    # 先确保远程目录存在
    remote_dir = os.path.dirname(remote)
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        _mkdir_p(sftp, remote_dir)

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(sftp.put, local, remote)
        try:
            fut.result(timeout=timeout)
        except TimeoutError:
            # 超时后关闭 SFTP channel 来强制中断
            try:
                sftp.get_channel().close()
            except Exception:
                pass
            raise TimeoutError(
                f"文件上传超时 ({timeout}s): {local} → {remote}"
            )


def scp_push_handler(
    host: str,
    username: str,
    local_path: str,
    remote_path: str,
    port: int = 22,
    password: str = None,
    key_path: str = None,
    recursive: bool = False,
    timeout: int = DEFAULT_SCP_TIMEOUT,
) -> Dict[str, Any]:
    """上传文件或目录到远程服务器。"""
    client = None
    sftp = None
    try:
        client = _connect(host, username, port, password, key_path)
        sftp = client.open_sftp()

        local_path = os.path.normpath(local_path)

        if os.path.isdir(local_path):
            if not recursive:
                return {
                    "stdout": "",
                    "stderr": "本地路径是目录，请设置 recursive=true 递归上传",
                    "returncode": -1,
                    "success": False,
                }
            # 上传整个目录
            file_count = 0
            for root, dirs, files in os.walk(local_path):
                for f in files:
                    local_file = os.path.join(root, f)
                    rel_path = os.path.relpath(local_file, local_path)
                    remote_file = os.path.join(remote_path, rel_path).replace("\\", "/")
                    _scp_put_with_timeout(sftp, local_file, remote_file, timeout)
                    file_count += 1
            return {
                "stdout": f"上传成功: {local_path} → {remote_path} ({file_count} 个文件)",
                "stderr": "",
                "returncode": 0,
                "success": True,
            }
        else:
            # 上传单个文件
            if remote_path.endswith("/"):
                remote_path = os.path.join(remote_path, os.path.basename(local_path)).replace("\\", "/")
            _scp_put_with_timeout(sftp, local_path, remote_path, timeout)
            return {
                "stdout": f"上传成功: {local_path} → {remote_path}",
                "stderr": "",
                "returncode": 0,
                "success": True,
            }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"SCP 上传失败 ({type(e).__name__}): {str(e)}",
            "returncode": -1,
            "success": False,
        }
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        if client:
            try:
                client.close()
            except Exception:
                pass
