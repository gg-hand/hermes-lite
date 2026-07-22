#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Hermes Lite 一键部署脚本（Linux x86_64/aarch64）
# 用法: chmod +x deploy.sh && sudo ./deploy.sh
# ============================================================

APP_NAME="hermes-lite"
APP_DIR="/opt/${APP_NAME}"
SERVICE_USER="deploy"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"

echo "========================================"
echo "  Hermes Lite 裸机部署脚本"
echo "========================================"

# --- 检查环境 ---
if [ "$EUID" -ne 0 ]; then
  echo "[!] 请以 root 身份运行 (sudo ./deploy.sh)"
  exit 1
fi

# --- 1. 安装系统依赖 ---
echo ""
echo "[1/6] 安装系统依赖..."
apt-get update -qq
apt-get install -y -qq \
  python3.11 \
  python3.11-venv \
  python3.11-dev \
  curl \
  git \
  nginx 2>/dev/null || true  # nginx 可选

echo "  ✓ 系统依赖安装完成"

# --- 2. 创建用户 ---
echo ""
echo "[2/6] 创建服务用户..."
if ! id -u ${SERVICE_USER} &>/dev/null; then
  useradd -r -s /usr/sbin/nologin -M ${SERVICE_USER}
  echo "  ✓ 已创建用户 ${SERVICE_USER}"
else
  echo "  ✓ 用户 ${SERVICE_USER} 已存在"
fi

# --- 3. 部署项目 ---
echo ""
echo "[3/6] 部署项目文件..."
mkdir -p ${APP_DIR}

# 如果当前目录有源码，直接复制
if [ -f "hermes/__main__.py" ]; then
  rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.git' --exclude='.env' --exclude='data/' \
    ./ ${APP_DIR}/
  echo "  ✓ 从当前目录复制项目文件"
else
  echo "  [!] 未检测到项目文件，请先 cd 到 hermes-lite 目录再执行"
  echo "  或手动将文件复制到 ${APP_DIR}"
  exit 1
fi

# --- 4. 创建虚拟环境并安装依赖 ---
echo ""
echo "[4/6] 创建 Python 虚拟环境并安装依赖..."
python3.11 -m venv ${APP_DIR}/.venv
source ${APP_DIR}/.venv/bin/activate
pip install --no-cache-dir --upgrade pip -q
pip install --no-cache-dir -r ${APP_DIR}/requirements.txt -q
echo "  ✓ 依赖安装完成"

# --- 5. 创建数据目录与配置文件 ---
echo ""
echo "[5/6] 初始化数据目录与配置..."
mkdir -p ${APP_DIR}/data/{chroma,sessions,history}

# 如果存在 .env 则复制
if [ -f ".env" ]; then
  cp .env ${APP_DIR}/.env
  echo "  ✓ 已复制 .env 配置文件"
else
  echo "  [!] 未检测到 .env 文件，请手动创建 ${APP_DIR}/.env"
  echo "  参考模板: ${APP_DIR}/.env.example"
fi

# 修正权限
chown -R ${SERVICE_USER}:${SERVICE_USER} ${APP_DIR}
chmod 750 ${APP_DIR}
echo "  ✓ 数据目录初始化完成"

# --- 6. 配置 systemd 服务 ---
echo ""
echo "[6/6] 配置 systemd 服务..."

cat > ${SERVICE_FILE} << 'SERVICEEOF'
[Unit]
Description=Hermes Lite AI Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=deploy
Group=deploy
WorkingDirectory=/opt/hermes-lite
EnvironmentFile=/opt/hermes-lite/.env

# 启动命令
ExecStart=/opt/hermes-lite/.venv/bin/uvicorn teage_liu.app:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --loop uvloop \
  --http httptools \
  --log-level info

# 自动重启
Restart=always
RestartSec=10

# 资源限制
LimitNOFILE=65536
MemoryMax=1G

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable ${APP_NAME}
systemctl start ${APP_NAME}

echo "  ✓ systemd 服务已配置并启动"

# --- 完成 ---
echo ""
echo "========================================"
echo "  ✅ Hermes Lite 部署完成！"
echo "========================================"
echo ""
echo "  服务状态:  systemctl status ${APP_NAME}"
echo "  查看日志:  journalctl -u ${APP_NAME} -f"
echo "  访问地址:  http://$(curl -s ifconfig.me):8000"
echo "  API 文档:  http://localhost:8000/docs"
echo ""
echo "  数据目录:  ${APP_DIR}/data/"
echo "  配置文件:  ${APP_DIR}/.env"
echo ""
echo "  ⚠️  如果配置了 TEAGE_API_KEY，所有请求需携带"
echo "     Authorization: Bearer <your-api-key>"
echo ""
echo "  ⚠️  生产环境建议加 Nginx 反向代理 + HTTPS"
echo "  快速配置:  sudo nano /etc/nginx/sites-available/hermes-lite"
echo ""
echo "========================================"
