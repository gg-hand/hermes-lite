#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Teage Liu 一键部署脚本（Linux x86_64/aarch64）
#
# 端口策略：
#   - 后端服务监听 127.0.0.1:7007（仅本机）
#   - Nginx 监听 88 端口对外，反向代理到 127.0.0.1:7007
#
# 用法:
#   chmod +x deploy.sh && sudo ./deploy.sh                # 完整部署（含 nginx）
#   sudo TEAGE_PORT=7007 NGINX_PORT=88 ./deploy.sh        # 自定义端口
#   sudo SKIP_NGINX=1 ./deploy.sh                          # 跳过 nginx 配置
# ============================================================

APP_NAME="teage-liu"
APP_DIR="/opt/${APP_NAME}"
SERVICE_USER="deploy"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
NGINX_SITE_FILE="/etc/nginx/sites-available/${APP_NAME}"
NGINX_SITE_LINK="/etc/nginx/sites-enabled/${APP_NAME}"

# 端口（环境变量优先）
TEAGE_HOST="${TEAGE_HOST:-127.0.0.1}"
TEAGE_PORT="${TEAGE_PORT:-7007}"
NGINX_PORT="${NGINX_PORT:-88}"
SKIP_NGINX="${SKIP_NGINX:-0}"

echo "========================================"
echo "  Teage Liu 裸机部署脚本"
echo "========================================"
echo "  后端:    ${TEAGE_HOST}:${TEAGE_PORT}"
echo "  Nginx:   0.0.0.0:${NGINX_PORT} → ${TEAGE_HOST}:${TEAGE_PORT}"
echo "  跳过 Nginx: ${SKIP_NGINX}"
echo ""

# --- 检查环境 ---
if [ "$EUID" -ne 0 ]; then
  echo "[!] 请以 root 身份运行 (sudo ./deploy.sh)"
  exit 1
fi

# --- 1. 安装系统依赖 ---
echo "[1/7] 安装系统依赖..."
apt-get update -qq
apt-get install -y -qq \
  python3.11 \
  python3.11-venv \
  python3.11-dev \
  curl \
  git \
  nginx 2>/dev/null || true

echo "  ✓ 系统依赖安装完成"

# --- 2. 创建用户 ---
echo ""
echo "[2/7] 创建服务用户..."
if ! id -u ${SERVICE_USER} &>/dev/null; then
  useradd -r -s /usr/sbin/nologin -M ${SERVICE_USER}
  echo "  ✓ 已创建用户 ${SERVICE_USER}"
else
  echo "  ✓ 用户 ${SERVICE_USER} 已存在"
fi

# --- 3. 部署项目 ---
echo ""
echo "[3/7] 部署项目文件..."
mkdir -p ${APP_DIR}

# 如果当前目录有源码，直接复制
if [ -f "teage_liu/__main__.py" ]; then
  rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.git' --exclude='.env' --exclude='data/' \
    --exclude='config.yaml' --exclude='config.yaml.bak' \
    --exclude='.server.pid' --exclude='server_out.txt' --exclude='server_err.txt' \
    ./ ${APP_DIR}/
  echo "  ✓ 从当前目录复制项目文件"
else
  echo "  [!] 未检测到项目文件，请先 cd 到 teage-liu 目录再执行"
  echo "  或手动将文件复制到 ${APP_DIR}"
  exit 1
fi

# --- 4. 创建虚拟环境并安装依赖 ---
echo ""
echo "[4/7] 创建 Python 虚拟环境并安装依赖..."
python3.11 -m venv ${APP_DIR}/.venv
source ${APP_DIR}/venv/bin/activate 2>/dev/null || source ${APP_DIR}/.venv/bin/activate
pip install --no-cache-dir --upgrade pip -q
pip install --no-cache-dir -r ${APP_DIR}/requirements.txt -q
echo "  ✓ 依赖安装完成"

# --- 5. 创建数据目录与配置文件 ---
echo ""
echo "[5/7] 初始化数据目录与配置..."
mkdir -p ${APP_DIR}/data/{chroma,sessions,history,hf-cache,cache,uploads}
mkdir -p ${APP_DIR}/data/schedules ${APP_DIR}/data/reports

# 如果存在 .env 则复制
if [ -f ".env" ]; then
  cp .env ${APP_DIR}/.env
  chmod 600 ${APP_DIR}/.env
  chown ${SERVICE_USER}:${SERVICE_USER} ${APP_DIR}/.env
  echo "  ✓ 已复制 .env 配置文件 (权限 600)"
else
  echo "  [!] 未检测到 .env 文件，请手动创建 ${APP_DIR}/.env"
  echo "  参考模板: ${APP_DIR}/.env.example"
fi

# 从 config.yaml.example 创建 config.yaml（如不存在）
if [ ! -f "${APP_DIR}/config.yaml" ] && [ -f "${APP_DIR}/config.yaml.example" ]; then
  cp ${APP_DIR}/config.yaml.example ${APP_DIR}/config.yaml
  echo "  ✓ 已从 config.yaml.example 创建 config.yaml"
fi

# 修正权限
chown -R ${SERVICE_USER}:${SERVICE_USER} ${APP_DIR}
chmod 750 ${APP_DIR}
chmod 600 ${APP_DIR}/.env 2>/dev/null || true
echo "  ✓ 数据目录初始化完成"

# --- 6. 配置 systemd 服务 ---
echo ""
echo "[6/7] 配置 systemd 服务..."

# 使用项目中的 service 文件（端口 7007 已写入），如环境变量覆盖了端口则替换
if [ -f "${APP_DIR}/teage-liu.service" ]; then
  cp ${APP_DIR}/teage-liu.service ${SERVICE_FILE}
  echo "  ✓ 已复制 teage-liu.service 到 ${SERVICE_FILE}"
else
  echo "  [!] 未找到 teage-liu.service，使用内联默认配置"
  cat > ${SERVICE_FILE} << 'SERVICEEOF'
[Unit]
Description=Teage Liu AI Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=deploy
Group=deploy
WorkingDirectory=/opt/teage-liu
EnvironmentFile=/opt/teage-liu/.env
Environment="TEAGE_HOST=127.0.0.1"
Environment="TEAGE_PORT=7007"
Environment="HF_HOME=/opt/teage-liu/data/hf-cache"
Environment="XDG_CACHE_HOME=/opt/teage-liu/data/cache"
Environment="SENTENCE_TRANSFORMERS_HOME=/opt/teage-liu/data/hf-cache"
Environment="HOME=/opt/teage-liu/data"

ExecStartPre=/bin/bash -c 'if [ -z "${TEAGE_API_KEY}" ]; then echo "WARNING: TEAGE_API_KEY 未设置"; else echo "TEAGE_API_KEY 已设置"; fi'
ExecStart=/opt/teage-liu/.venv/bin/uvicorn teage_liu.app:app \
  --host 127.0.0.1 \
  --port 7007 \
  --workers 1 \
  --loop uvloop \
  --http httptools \
  --log-level info

Restart=always
RestartSec=10
LimitNOFILE=65536

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/teage-liu/data
ProtectHome=true

[Install]
WantedBy=multi-user.target
SERVICEEOF
fi

# 如果端口被环境变量覆盖，替换 service 文件中的 7007
if [ "${TEAGE_PORT}" != "7007" ]; then
  sed -i "s|--port 7007|--port ${TEAGE_PORT}|g" ${SERVICE_FILE}
  sed -i "s|TEAGE_PORT=7007|TEAGE_PORT=${TEAGE_PORT}|g" ${SERVICE_FILE}
  echo "  ✓ 端口已替换为 ${TEAGE_PORT}"
fi
if [ "${TEAGE_HOST}" != "127.0.0.1" ]; then
  sed -i "s|--host 127.0.0.1|--host ${TEAGE_HOST}|g" ${SERVICE_FILE}
  sed -i "s|TEAGE_HOST=127.0.0.1|TEAGE_HOST=${TEAGE_HOST}|g" ${SERVICE_FILE}
  echo "  ✓ Host 已替换为 ${TEAGE_HOST}"
fi

systemctl daemon-reload
systemctl enable ${APP_NAME}
systemctl restart ${APP_NAME}
echo "  ✓ systemd 服务已配置并启动"

# --- 7. 配置 Nginx 反向代理 ---
if [ "${SKIP_NGINX}" = "1" ]; then
  echo ""
  echo "[7/7] 跳过 Nginx 配置（SKIP_NGINX=1）"
else
  echo ""
  echo "[7/7] 配置 Nginx 反向代理..."

  if ! command -v nginx &>/dev/null; then
    echo "  [!] nginx 未安装，跳过配置（请手动安装 nginx 后执行 sudo cp nginx-teage-liu.conf ...）"
  else
    # 复制 nginx 配置
    if [ -f "${APP_DIR}/nginx-teage-liu.conf" ]; then
      cp ${APP_DIR}/nginx-teage-liu.conf ${NGINX_SITE_FILE}

      # 端口覆盖：替换 upstream 和 listen
      if [ "${TEAGE_PORT}" != "7007" ]; then
        sed -i "s|127.0.0.1:7007|127.0.0.1:${TEAGE_PORT}|g" ${NGINX_SITE_FILE}
      fi
      if [ "${NGINX_PORT}" != "88" ]; then
        sed -i "s|listen 88;|listen ${NGINX_PORT};|g" ${NGINX_SITE_FILE}
        sed -i "s|listen \[::\]:88;|listen [::]:${NGINX_PORT};|g" ${NGINX_SITE_FILE}
      fi

      # 启用站点
      ln -sf ${NGINX_SITE_FILE} ${NGINX_SITE_LINK}

      # 移除默认站点（避免冲突，仅 80 端口不冲突时可保留）
      # rm -f /etc/nginx/sites-enabled/default

      # 88 < 1024，nginx 默认 root 运行，无需 setcap
      echo "  ✓ Nginx 配置已部署到 ${NGINX_SITE_FILE}"

      # 测试并 reload
      if nginx -t 2>&1; then
        systemctl reload nginx
        echo "  ✓ Nginx 已 reload"
      else
        echo "  [!] Nginx 配置测试失败，请检查 ${NGINX_SITE_FILE}"
      fi
    else
      echo "  [!] 未找到 nginx-teage-liu.conf，跳过"
    fi
  fi
fi

# --- 完成 ---
echo ""
echo "========================================"
echo "  ✅ Teage Liu 部署完成！"
echo "========================================"
echo ""
echo "  服务状态:    systemctl status ${APP_NAME}"
echo "  查看日志:    journalctl -u ${APP_NAME} -f"
echo "  后端地址:    http://${TEAGE_HOST}:${TEAGE_PORT} (仅本机访问)"
if [ "${SKIP_NGINX}" != "1" ] && command -v nginx &>/dev/null; then
  PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "<server-ip>")
  echo "  对外地址:    http://${PUBLIC_IP}:${NGINX_PORT}"
  echo "  API 文档:    http://${PUBLIC_IP}:${NGINX_PORT}/docs"
else
  echo "  对外地址:    http://<server-ip>:${TEAGE_PORT} (需自行配置反代)"
fi
echo ""
echo "  数据目录:    ${APP_DIR}/data/"
echo "  配置文件:    ${APP_DIR}/.env"
echo "  服务配置:    ${SERVICE_FILE}"
if [ "${SKIP_NGINX}" != "1" ]; then
  echo "  Nginx 配置:  ${NGINX_SITE_FILE}"
fi
echo ""
echo "  ⚠️  安全建议:"
echo "     1. 在 ${APP_DIR}/.env 中设置 TEAGE_API_KEY=<strong-password>"
echo "     2. 所有 /api/* 请求需携带 Authorization: Bearer <key>"
echo "     3. 生产环境建议为 nginx 配置 HTTPS（Let's Encrypt）"
echo ""
echo "========================================"
