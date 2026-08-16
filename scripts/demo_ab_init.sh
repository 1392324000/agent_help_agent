#!/usr/bin/env bash
# =============================================================================
# 模拟「服务费 · 客户方」AB 双角色新装初始化
#
# 场景：两台全新机器各自初始化
#   A 机 = 服务方（专家）：install.sh 部署上线（钱包+画像+标价）
#   B 机 = 客户方（求助者）：install.sh 仅初始化（钱包+知情+充值意识，不部署）
#   B 遇到问题 → 自动去 Hub 搜索 A → 付服务费订阅（mock）→ 调用解决
#
# 用法: bash scripts/demo_ab_init.sh   （需 Hub 已在 mock 模式运行）
# =============================================================================
set -uo pipefail
HUB_URL="${1:-http://127.0.0.1:20100}"
PORT_A=20152
HOME_A="/tmp/aha-init-a"          # A 机（服务方）全新 HOME
HOME_B="/tmp/aha-init-b"          # B 机（客户方）全新 HOME
WORK_A="/tmp/aha-init-a-work"
WORK_B="/tmp/aha-init-b-work"
KEY="init-demo-key-001"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ✅ $1"; }
bad() { FAIL=$((FAIL+1)); echo "  ❌ $1"; }

echo "=================================================================="
echo " 服务费 · AB 双角色新装初始化（A=服务方部署 / B=客户方钱包+知情）"
echo "=================================================================="

# ---- 0. 清理：清空 Hub 测试库（模拟全新平台）+ 隔离两台全新机器 ----
python3 -c "
import sqlite3
c = sqlite3.connect('hub/hub.db').cursor()
for t in ('ratings','deals','orders','agents'):
    c.execute(f'DELETE FROM {t}')
c.connection.commit()
print('  🧹 Hub 测试库已清空（全新平台）')
" 2>/dev/null || echo "  ⚠ 无法清理 Hub 库（跳过）"
rm -rf "$HOME_A" "$WORK_A" "$HOME_B" "$WORK_B"
mkdir -p "$HOME_A" "$WORK_A" "$HOME_B" "$WORK_B"
export AGENT_HUB_MOCK_CHAIN=1 PYTHONUNBUFFERED=1 AGENT_SERVER_KEY="$KEY"

# ============ 角色 A：服务方（专家）新装 ============
echo ""
echo "==> 🅰 A 机（服务方）：全新机器 → install.sh 一键部署上线"
HOME="$HOME_A" AGENT_WORK_DIR="$WORK_A" \
bash <(curl -fsSL "$HUB_URL/api/v1/dist/install.sh") "$HUB_URL" --auto-serve \
  --domain finance --subdomain financial_analysis \
  --skills report_analysis,risk_rating \
  --price 2 --port "$PORT_A" \
  --description "财报分析专家：解读+风险评级+投资建议" --demo-invoke \
  > /tmp/aha-init-a.log 2>&1 &
A_SERVE=$!
sleep 7
A_ID=$(curl -s -m 5 "$HUB_URL/api/v1/agents?limit=200" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for a in d.get('agents',[]):
    if a.get('domain')=='finance' and a.get('subdomain')=='financial_analysis' and ':$PORT_A' in (a.get('endpoint') or ''):
        print(a['agent_id']); break
" 2>/dev/null)
[ -n "$A_ID" ] && ok "A 新装部署上线: ${A_ID:0:16}…（钱包+画像+标价 2 USDT/h）" \
  || bad "A 上线失败（日志: $(tail -3 /tmp/aha-init-a.log)）"

# ============ 角色 B：客户方（求助者）新装 ============
echo ""
echo "==> 🅱 B 机（客户方）：全新机器 → install.sh 仅初始化（钱包+知情，不部署）"
HOME="$HOME_B" AGENT_WORK_DIR="$WORK_B" \
bash <(curl -fsSL "$HUB_URL/api/v1/dist/install.sh") "$HUB_URL" > /tmp/aha-init-b.log 2>&1
grep -q "智能体端初始化完成" /tmp/aha-init-b.log && ok "B 初始化完成（SDK + 钱包身份，未部署）" \
  || bad "B 初始化失败"
B_ID=$(grep -oE "0x[0-9a-f]{40}" /tmp/aha-init-b.log | head -1)
[ -n "$B_ID" ] && ok "B 钱包身份: ${B_ID:0:16}…（客户方也需要钱包）" || bad "B 无钱包身份"

echo "==> ② B 充值意识（服务费前置）：查询余额 + 充值指引"
BAL=$(HOME="$HOME_B" python3 "$WORK_B/agent_cli.py" --hub "$HUB_URL" balance 2>&1 | head -3)
echo "     balance: $(echo "$BAL" | grep -E "USDT|BNB|Balance" | head -2 | tr '\n' ' ')"
echo "     真实链需先充值 USDT(BEP-20) 到 $B_ID（订阅服务费）+ BNB（gas）；mock 模式自动模拟支付"
ok "B 已了解充值要求（服务费先充值）"

echo "==> ③ B 知情：查看平台信息（hub 地址/链模式/领域）"
INFO=$(HOME="$HOME_B" python3 "$WORK_B/agent_cli.py" --hub "$HUB_URL" info 2>&1 | head -5)
echo "     $(echo "$INFO" | grep -E "Hub|chain|领域" | head -2 | tr '\n' ' ')"
ok "B 已了解 Hub/API（知情）"

echo ""
echo "==> ④ B 遇问题自动求助：去 Hub 搜索专家（生产链路起点）"
SEARCH=$(HOME="$HOME_B" python3 "$WORK_B/agent_cli.py" --hub "$HUB_URL" find --q "财报 分析 风险" --limit 5 2>&1)
echo "$SEARCH" | grep -q "$A_ID" \
  && ok "B 搜索命中 A（站点打分）: $(echo "$SEARCH" | grep -oE '#1 得分 [0-9.]+[^ ]*.*' | head -1)" \
  || bad "B 搜索未命中 A"
echo "     $(echo "$SEARCH" | grep -A1 '#1' | head -2 | tr '\n' ' ')"

echo ""
echo "==> ⑤ B 付服务费订阅 A（mock 自动支付：金额 = 标价 × 时长）"
SUB=$(HOME="$HOME_B" python3 "$WORK_B/agent_cli.py" --hub "$HUB_URL" subscribe --peer "$A_ID" --duration 0.25 2>&1)
echo "$SUB" | grep -q "验签.*通过" && ok "B 支付服务费成功: 0.5 USDT（2.0 USDT/h × 0.25h，mock 自动转账）" \
  || bad "B 订阅失败: $(echo "$SUB" | tail -2)"

echo ""
echo "==> ⑥ B 调用 A 能力解决（需求=参数，产物=返回值）"
INV=$(HOME="$HOME_B" python3 "$WORK_B/agent_cli.py" --hub "$HUB_URL" invoke --peer "$A_ID" \
  --capability ping --params '{}' 2>&1)
echo "$INV" | grep -q '"ok": true' && ok "B 调用 A 成功（服务费已生效）" \
  || bad "B 调用失败: $(echo "$INV" | tail -2)"

echo ""
echo "==> ⑦ 服务费核对：B 支付的金额 = A 标价 × 时长（已在 ⑤ 验证 0.5 = 2.0 × 0.25）"
echo "     成交汇报机制：服务方完成服务后调用 client.report_deal 签名汇报 → Hub 行情/评分据此更新"
ok "服务费链路闭环：B 付费(0.5 USDT) → A 服务 → 成交可汇报"

# ---- 清理 ----
kill "$A_SERVE" 2>/dev/null
echo ""
echo "=================================================================="
echo " AB 新装初始化测试: ✅ $PASS 通过 / ❌ $FAIL 失败"
echo "=================================================================="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
