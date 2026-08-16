#!/usr/bin/env bash
# =============================================================================
# Expert Agent Hub —— 构建分发资产
#
# 概念分层（智能体端重建 = Skill(说明书) → Hub → SDK(代码) → 初始化）:
#   skill.tar.gz   纯文档: SKILL.md + references/protocol.md（智能体预装的说明书）
#   sdk.tar.gz     代码包: agent_sdk/ + agent_cli.py（从 Hub 拉取，解压即用）
#   install.sh     引导:  拉 SDK → 初始化身份(钱包) → 指引注册/启动微服务
#   manifest.json  清单:  版本 / 哈希 / 构建时间
#
# 用法: bash scripts/build_dist.sh
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/hub/dist"

mkdir -p "$DIST"

echo "==> 1/3 打包 SDK（agent_sdk/ + agent_cli.py，解压即用）"
rm -f "$DIST/sdk.tar.gz"
tar czf "$DIST/sdk.tar.gz" -C "$ROOT" \
    --exclude='__pycache__' --exclude='*.pyc' \
    agent_sdk agent_cli.py

echo "==> 2/3 打包 Skill（纯文档：SKILL.md + references/）"
rm -f "$DIST/skill.tar.gz"
tar czf "$DIST/skill.tar.gz" -C "$ROOT/skill" SKILL.md references

echo "==> 3/3 生成一键装机脚本 install.sh"
cat > "$DIST/install.sh" <<'INSTALL_EOF'
#!/usr/bin/env bash
# =============================================================================
# Expert Agent Hub —— 智能体端一键初始化（从 Hub 拉取 SDK，生成身份）
#
# 用法:
#   bash <(curl -fsSL $AGENT_HUB_URL/api/v1/dist/install.sh) [HUB_URL] [选项]
#   例: bash <(curl -fsSL https://agenthelpagent.xyz/api/v1/dist/install.sh)
#
# 选项:
#   --auto-serve            部署完成后自动注册并启动聊天微服务（前台运行）
#   --domain <领域>         一级领域（默认 finance）
#   --subdomain <子领域>    二级领域（默认 quantitative_trading）
#   --skills <技能>         技能标签，逗号分隔（默认 backtesting）
#   --price <USDT/小时>     服务报价（默认 0.005）
#   --port <端口>           服务端口（默认 20102，端口约定全平台统一）
#   --description <描述>    服务一句话描述（注册画像，供客户搜索定位）
#   --demo-invoke           启用演示能力 ping/echo（测试订阅-调用链路用）
#
# 流程: 拉取 SDK → 初始化钱包身份(agent.json) → [--auto-serve] 注册上线
# 真实链: 注册费由钱包自动广播（serve 自动转账），无需手工 --tx-hash
# =============================================================================
set -euo pipefail

HUB_URL="${1:-${AGENT_HUB_URL:-http://127.0.0.1:20100}}"
AUTO_SERVE=0
DOMAIN="finance"
SUBDOMAIN="quantitative_trading"
SKILLS="backtesting"
PRICE="0.005"
PORT="20102"
DESCRIPTION=""
DEMO_INVOKE=0
POS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto-serve) AUTO_SERVE=1; shift ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --subdomain) SUBDOMAIN="$2"; shift 2 ;;
    --skills) SKILLS="$2"; shift 2 ;;
    --price) PRICE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --description) DESCRIPTION="$2"; shift 2 ;;
    --demo-invoke) DEMO_INVOKE=1; shift ;;
    *)
      if [[ "$POS" -eq 0 ]]; then HUB_URL="$1"; POS=1; shift
      else echo "❌ 未知参数: $1"; exit 1; fi ;;
  esac
done

WORK_DIR="${AGENT_WORK_DIR:-$HOME/agent-marketplace}"
CLI="$WORK_DIR/agent_cli.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 优先使用带助记词依赖的 python（eth_account+mnemonic → BIP39 助记词备份），
# 否则回退 python3（钱包仍可用，但无一次性助记词展示）
PYTHON="python3"
for _cand in "$HOME/.fly/venv/bin/python3" /root/.fly/venv/bin/python3; do
  if [ -x "$_cand" ] && "$_cand" -c "import eth_account, mnemonic" >/dev/null 2>&1; then
    PYTHON="$_cand"; break
  fi
done
echo "==> Python: $PYTHON"

echo "==> Hub 注册中心 : $HUB_URL"
echo "==> 拉取资产清单 ..."
curl -fsSL --max-time 10 "$HUB_URL/api/v1/dist" -o "$TMP/dist.json" \
  || { echo "❌ 无法连接 Hub（检查地址 / 安全组 / 是否已执行 scripts/build_dist.sh）"; exit 1; }
python3 - "$TMP/dist.json" <<'PY' || true
import json, sys
m = json.load(open(sys.argv[1]))
print(f"    版本={m.get('version','?')} 构建于={m.get('built_at','?')}")
for n, f in m.get("files", {}).items():
    print(f"    - {n} ({f.get('size',0)//1024}KB)")
PY

echo "==> 拉取 SDK ..."
mkdir -p "$WORK_DIR"
curl -fsSL --max-time 30 "$HUB_URL/api/v1/dist/sdk.tar.gz" -o "$TMP/sdk.tar.gz"
tar xzf "$TMP/sdk.tar.gz" -C "$WORK_DIR"
test -f "$WORK_DIR/agent_sdk/__init__.py" && test -f "$CLI" \
  && echo "    ✅ SDK 就绪（$WORK_DIR）" \
  || { echo "❌ SDK 解压校验失败"; exit 1; }

echo "==> 注入 Skill 说明书到已安装的智能体（装过即自动激活）..."
TMP_SKILL="$TMP/skill.tar.gz"
if curl -fsSL --max-time 30 "$HUB_URL/api/v1/dist/skill.tar.gz" -o "$TMP_SKILL" 2>/dev/null; then
  SKILL_INSTALLED=0
  for _adir in "$HOME/.claude/skills" "$HOME/.codex/skills" "$HOME/.cursor/skills" \
               "$HOME/.gemini/skills" "$HOME/.copilot/skills" "$HOME/.config/agents/skills" \
               "$HOME/.hermes/skills" "$HOME/.config/opencode/skills" "$HOME/.config/goose/skills" \
               "$HOME/.continue/skills" "$HOME/.deepagents/agent/skills" "$HOME/.codeium/windsurf/skills" \
               "$HOME/.trae/skills" "$HOME/.qwen/skills" "$HOME/.windsurf/skills" \
               "$HOME/.config/devin/skills" "$HOME/.openhands/skills" "$HOME/.openclaw/skills" \
               "$HOME/.roo/skills" "$HOME/.grok/skills"; do
    [ -d "$_adir" ] || continue
    _target="$_adir/agent-marketplace"
    if [ -f "$_target/SKILL.md" ]; then
      echo "    · $_adir 已注入，跳过"
      continue
    fi
    if mkdir -p "$_target" 2>/dev/null && tar xzf "$TMP_SKILL" -C "$_target" 2>/dev/null \
        && [ -f "$_target/SKILL.md" ]; then
      echo "    ✅ 已注入: $_adir/agent-marketplace（进入智能体自动激活）"
      SKILL_INSTALLED=$((SKILL_INSTALLED + 1))
    fi
  done
  [ "$SKILL_INSTALLED" -eq 0 ] && echo "    （未检测到已安装的智能体，跳过注入）"
else
  echo "    ⚠ Skill 下载失败（不影响 SDK，可手动安装）"
fi

echo "==> 初始化身份（生成钱包，持久化 ~/.agent-marketplace/agent.json）..."
$PYTHON "$CLI" --hub "$HUB_URL" init \
  || { echo "❌ 身份初始化失败"; exit 1; }

if [[ "$AUTO_SERVE" -eq 1 ]]; then
  echo
  echo "=================================================="
  echo "  自动注册并启动聊天微服务（前台运行）"
  echo "  领域: $DOMAIN/$SUBDOMAIN | 技能: $SKILLS | 报价: $PRICE USDT/h | 端口: $PORT"
  echo "  停止: Ctrl+C（后台运行: nohup ... & 或 systemd 托管）"
  echo "=================================================="
  exec $PYTHON "$CLI" --hub "$HUB_URL" serve \
    --port "$PORT" --domain "$DOMAIN" --subdomain "$SUBDOMAIN" \
    --skills "$SKILLS" --price "$PRICE" --auto-price \
    ${DESCRIPTION:+--description "$DESCRIPTION"} ${DEMO_INVOKE:+--demo-invoke}
fi

echo
echo "=================================================="
echo "  智能体端初始化完成 ✅"
echo "=================================================="
echo "  注册并启动聊天微服务（端口约定 20102）:"
echo "    cd $WORK_DIR && python3 agent_cli.py --hub $HUB_URL serve \\"
echo "      --domain $DOMAIN --subdomain $SUBDOMAIN \\"
echo "      --skills $SKILLS --price $PRICE --auto-price"
echo "  或一条命令自动完成部署+上线:"
echo "    bash <(curl -fsSL $HUB_URL/api/v1/dist/install.sh) $HUB_URL --auto-serve \\"
echo "      --domain $DOMAIN --subdomain $SUBDOMAIN --skills $SKILLS --price $PRICE"
echo "  常用命令: info / search / private / subscribe / invoke / pricing"
echo "=================================================="
INSTALL_EOF
chmod +x "$DIST/install.sh"

echo "==> 生成 manifest.json"
VERSION="$(cd "$ROOT" && git describe --tags --always 2>/dev/null || echo "dev")"
BUILT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$DIST" "$VERSION" "$BUILT" <<'PY'
import json, hashlib, os, sys
dist, version, built = sys.argv[1], sys.argv[2], sys.argv[3]
files = {}
for name in sorted(os.listdir(dist)):
    p = os.path.join(dist, name)
    if not os.path.isfile(p):
        continue
    with open(p, "rb") as f:
        data = f.read()
    files[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
manifest = {
    "ok": True,
    "kind": "agent-marketplace-dist",
    "version": version,
    "built_at": built,
    "note": "Skill=说明书(md) 已预装；SDK/CLI 随 Hub 分发，智能体端一键拉取初始化",
    "files": files,
}
with open(os.path.join(dist, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
print(f"    manifest.json: {len(files)} 个文件, version={version}")
PY

echo
echo "✅ 构建完成 -> $DIST"
ls -lh "$DIST" | awk '{print "   ", $9, "("$5")"}'
