#!/usr/bin/env bash
# =============================================================================
# Expert Agent Hub 一键部署脚本（随项目分发，目录自包含）
#
# 用法:   sudo bash scripts/deploy.sh
# 前置:   域名 DNS 已指向本机（agent-hub.env 中 AGENT_HUB_PUBLIC_URL）
# 幂等:   可重复执行；已存在配置会被模板生成的最新版覆盖，证书自动跳过已签发
# =============================================================================
set -euo pipefail

# ── 路径与配置来源 ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/agent-hub.env"

PORT="$(grep -E '^AGENT_HUB_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)"
PORT="${PORT:-20100}"
DOMAIN="$(grep -E '^AGENT_HUB_PUBLIC_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | sed -E 's#^https?://##; s#/.*$##' || true)"
DOMAIN="${DOMAIN:-agenthelpagent.xyz}"
WEBROOT="${WEBROOT:-/var/www/html}"

[ "$(id -u)" -eq 0 ] || { echo "需要 root 权限: sudo bash $0"; exit 1; }
echo "▶ 项目目录: $PROJECT_DIR"
echo "▶ 域名    : $DOMAIN"
echo "▶ 端口    : $PORT"

# ── 1. 系统依赖（只装缺失的）──────────────────────────────────────
DEPS="python3 python3-cryptography apache2 certbot"
NEED=""
for p in $DEPS; do dpkg -s "$p" >/dev/null 2>&1 || NEED="$NEED $p"; done
if [ -n "$NEED" ]; then
    echo "▶ 安装缺失依赖:$NEED"
    apt-get update -qq && apt-get install -y -qq $NEED
fi
# cryptography 用发行版包（Ubuntu 24.04 PEP 668 环境，避免 pip 直装系统 Python）

# ── 2. systemd 单元（模板 → /etc/systemd/system）─────────────────
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$SCRIPT_DIR/agent-hub.service" > /etc/systemd/system/agent-hub.service
systemctl daemon-reload
systemctl enable --now agent-hub
echo "✅ Hub 服务已启动 (127.0.0.1:$PORT)"

# ── 3. TLS 证书（webroot 复用现有 Apache 80 端口，零停机）─────────
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    echo "▶ 签发证书 $DOMAIN ..."
    certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" \
        --non-interactive --agree-tos --register-unsafely-without-email
else
    echo "ℹ️ 证书已存在，续期由 certbot 定时任务负责"
fi

# ── 4. Apache 反代 vhost（模板 → /etc/apache2/sites-available）───
sed -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__PORT__|$PORT|g" \
    "$SCRIPT_DIR/agenthelpagent.conf" > /etc/apache2/sites-available/agenthelpagent.conf
a2enmod -q proxy proxy_http headers ssl rewrite
a2ensite -q agenthelpagent
apache2ctl configtest
systemctl reload apache2
echo "✅ Apache 反代已配置"

# ── 5. 验证 ───────────────────────────────────────────────────────
sleep 2
curl -sf "http://127.0.0.1:$PORT/api/v1/info" >/dev/null && echo "✅ Hub 本地可达 (127.0.0.1:$PORT)"
if curl -sf "https://$DOMAIN/api/v1/info" >/dev/null; then
    echo "✅ https://$DOMAIN 域名可达"
else
    echo "⚠️  https://$DOMAIN 不可达：检查 DNS、证书、安全组/反代配置"
fi

cat <<EOF

════════════════════════════════════════════════════════════
  部署完成。智能体端接入:
    export AGENT_HUB_URL=https://$DOMAIN
    bash <(curl -fsSL \$AGENT_HUB_URL/api/v1/dist/install.sh) --auto-serve ...
════════════════════════════════════════════════════════════
EOF
