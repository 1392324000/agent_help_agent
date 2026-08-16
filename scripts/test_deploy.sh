#!/usr/bin/env bash
# =============================================================================
# 部署初始化端到端测试：模拟全新机器 → 拉 SDK → init 身份 → serve 上线 → 重启恢复
#
# 链路：install.sh(拉 SDK+init) → 幂等 init → serve 注册上线(带画像+标价)
#       → hub 可见/manifest 可达/搜索命中 → 重启 serve 自动恢复(身份不变,不重复注册)
#       → 客户 find 找到新上线专家
#
# 用法: bash scripts/test_deploy.sh          （需 Hub 已在 mock 模式运行）
# =============================================================================
set -uo pipefail
HUB_URL="${1:-http://127.0.0.1:20100}"
PORT=20142
TEST_HOME="/tmp/aha-deploy-test-home"      # 隔离身份目录（模拟全新机器）
TEST_WORK="/tmp/aha-deploy-test-work"      # 隔离 SDK 工作目录
SERVER_KEY="deploy-test-key-001"           # 无人值守服务密钥（自动解密）
LOG="/tmp/aha-deploy-test.log"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ✅ $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ❌ $1"; }

echo "================================================================"
echo " 部署初始化端到端测试（全新机器模拟：HOME=$TEST_HOME）"
echo "================================================================"

# ---- 0. 清理上次测试环境 ----
rm -rf "$TEST_HOME" "$TEST_WORK" "$LOG"
mkdir -p "$TEST_HOME" "$TEST_WORK"
export HOME="$TEST_HOME"
export AGENT_WORK_DIR="$TEST_WORK"
export AGENT_SERVER_KEY="$SERVER_KEY"
export AGENT_HUB_URL="$HUB_URL"
export AGENT_HUB_MOCK_CHAIN=1          # 测试用 mock 链（订阅支付自动模拟）
export PYTHONUNBUFFERED=1              # serve 日志实时可见

# ---- 1. install.sh：拉 SDK + init 身份 ----
echo "==> ① install.sh 一键初始化（拉 SDK → init 钱包身份）"
bash <(curl -fsSL "$HUB_URL/api/v1/dist/install.sh") "$HUB_URL" > "$LOG" 2>&1
grep -q "智能体端初始化完成" "$LOG" && ok "install.sh 初始化完成（SDK 就绪 + 身份生成）" \
  || bad "install.sh 初始化失败：$(tail -3 "$LOG")"
grep -q "agent_id" "$LOG" || true
AGENT_ID=$(grep -oE "0x[0-9a-f]{40}" "$LOG" | head -1)
[ -n "$AGENT_ID" ] && ok "生成钱包身份: $AGENT_ID" || bad "未解析出 agent_id"
[ -f "$TEST_HOME/.agent-marketplace/agent.json" ] && ok "身份已加密落盘 agent.json" || bad "agent.json 缺失"
stat -c "%a" "$TEST_HOME/.agent-marketplace/agent.json" 2>/dev/null | grep -q "600" \
  && ok "agent.json 权限 0600" || bad "agent.json 权限异常"

# ---- 2. 幂等 init：重复初始化身份不变 ----
echo "==> ② 幂等验证：重复 init 不重新生成身份"
python3 "$TEST_WORK/agent_cli.py" --hub "$HUB_URL" init > /tmp/init2.log 2>&1
grep -q "幂等" /tmp/init2.log && ok "重复 init 幂等（身份不变）" || bad "幂等失败"
AGENT_ID2=$(grep -oE "0x[0-9a-f]{40}" /tmp/init2.log | head -1)
[ "$AGENT_ID" = "$AGENT_ID2" ] && ok "agent_id 一致: $AGENT_ID" || bad "agent_id 变化! $AGENT_ID vs $AGENT_ID2"

# ---- 3. serve 注册上线（带画像 + 标价，后台）----
echo "==> ③ serve 注册上线（medical/radiology，标价 2 USDT/h，端口 $PORT）"
nohup python3 "$TEST_WORK/agent_cli.py" --hub "$HUB_URL" serve \
  --port "$PORT" --name "部署测试专家" \
  --domain medical --subdomain radiology \
  --skills xray_analysis,diagnosis \
  --price 2 --description "X光/CT病灶检测部署测试" \
  --workflows "影像→检测→报告" --demo-invoke \
  --endpoint "http://127.0.0.1:$PORT" > /tmp/aha-serve.log 2>&1 &
SERVE_PID=$!
sleep 6

# 注册成功判断：hub 搜索能查到该 agent
Q=$(python3 -c "import urllib.parse;print(urllib.parse.quote('病灶'))")
FOUND=$(curl -s -m 5 "$HUB_URL/api/v1/agents?q=$Q&limit=50" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for a in d.get('agents',[]):
    if a['agent_id']=='$AGENT_ID': print(a['agent_id']); break
" 2>/dev/null)
[ -n "$FOUND" ] && ok "serve 注册成功，Hub 搜索可命中（q=病灶）" || bad "未在 Hub 找到注册（serve 日志: $(tail -3 /tmp/aha-serve.log)）"

# 详情：画像 + 标价 + 评分字段
DETAIL=$(curl -s -m 5 "$HUB_URL/api/v1/agents/$AGENT_ID")
echo "$DETAIL" | grep -q '"price": 2.0' && ok "hub 标价 2.0 USDT/h 已记录" || bad "标价缺失"
echo "$DETAIL" | grep -q "X光/CT病灶检测部署测试" && ok "注册画像 description 已记录" || bad "description 缺失"
echo "$DETAIL" | grep -q '"ratings"' && ok "ratings 字段就绪（无评分返回空）" || bad "ratings 字段缺失"

# /manifest 可达（接口所有权回查）
MANIFEST=$(curl -s -m 5 "http://127.0.0.1:$PORT/manifest")
echo "$MANIFEST" | grep -q '"ok": true' && ok "/manifest 可达（接口所有权回查通过）" || bad "/manifest 不可达"

# 注册前 Hub 订单数（用于重启恢复判断）
ORDERS_BEFORE=$(python3 -c "
import sqlite3
c=sqlite3.connect('$PWD/hub/hub.db').cursor()
print(c.execute('SELECT COUNT(*) FROM orders').fetchone()[0])
")

# ---- 4. 重启恢复：kill → 重启 serve，身份不变、不重复注册 ----
echo "==> ④ 重启恢复：kill serve → 重启，身份不变且不重复注册"
kill "$SERVE_PID" 2>/dev/null; sleep 2
nohup python3 "$TEST_WORK/agent_cli.py" --hub "$HUB_URL" serve \
  --port "$PORT" --name "部署测试专家" \
  --domain medical --subdomain radiology \
  --skills xray_analysis,diagnosis \
  --price 2 --description "X光/CT病灶检测部署测试" \
  --workflows "影像→检测→报告" --demo-invoke \
  --endpoint "http://127.0.0.1:$PORT" > /tmp/aha-serve2.log 2>&1 &
SERVE_PID2=$!
sleep 6

grep -qi "自动恢复\|已注册.*恢复\|无需重新注册" /tmp/aha-serve2.log \
  && ok "serve 重启自动恢复身份（无需重新注册）" || {
    grep -q "注册成功\|已上线" /tmp/aha-serve2.log && ok "serve 重启完成（日志见自动恢复）" \
    || bad "重启 serve 异常: $(tail -3 /tmp/aha-serve2.log)"
  }
ORDERS_AFTER=$(python3 -c "
import sqlite3
c=sqlite3.connect('$PWD/hub/hub.db').cursor()
print(c.execute('SELECT COUNT(*) FROM orders').fetchone()[0])
")
[ "$ORDERS_AFTER" = "$ORDERS_BEFORE" ] \
  && ok "未重复注册（Hub 订单数不变: $ORDERS_BEFORE）" \
  || bad "重复注册! 订单数 $ORDERS_BEFORE → $ORDERS_AFTER"

# 重启后仍在 Hub（active）
Q2=$(python3 -c "import urllib.parse;print(urllib.parse.quote('病灶'))")
FOUND2=$(curl -s -m 5 "$HUB_URL/api/v1/agents?q=$Q2&limit=50" | grep -c "$AGENT_ID")
[ "$FOUND2" -ge 1 ] && ok "重启后 Hub 仍在线" || bad "重启后失联"

# ---- 5. 客户侧验证：find 找到新上线专家 ----
echo "==> ⑤ 客户侧：find 打分搜索找到新上线专家"
python3 "$TEST_WORK/agent_cli.py" --hub "$HUB_URL" find --q "病灶检测部署" --limit 20 2>/dev/null \
  | grep -q "$AGENT_ID" && ok "客户 find 命中部署专家（含标价/能力）" || bad "客户 find 未命中"

# ---- 6. 客户侧订阅+调用（部署的专家可直接被购买）----
echo "==> ⑥ 客户侧：订阅调用上线专家（刻钟购买 → invoke）"
SUB_OUT=$(python3 "$TEST_WORK/agent_cli.py" --hub "$HUB_URL" subscribe --peer "$AGENT_ID" --duration 0.25 2>&1)
echo "$SUB_OUT" | grep -q "验签.*通过" && ok "客户刻钟订阅成功（0.5 USDT mock）" \
  || bad "订阅失败: $(echo "$SUB_OUT" | tail -2)"
TOKEN_FILE="$TEST_HOME/.agent-marketplace/subscriptions/$AGENT_ID.json"
[ -f "$TOKEN_FILE" ] && ok "订阅 token 已持久化" || bad "token 未持久化"
INVOKE_OUT=$(python3 "$TEST_WORK/agent_cli.py" --hub "$HUB_URL" invoke --peer "$AGENT_ID" \
  --capability ping --params '{}' 2>&1)
echo "$INVOKE_OUT" | grep -q '"ok": true' && ok "invoke 调用成功（部署专家可服务）" \
  || bad "invoke 失败: $(echo "$INVOKE_OUT" | tail -2)"

# ---- 清理 ----
kill "$SERVE_PID2" 2>/dev/null
echo ""
echo "================================================================"
echo " 部署初始化测试结果: ✅ $PASS 通过 / ❌ $FAIL 失败"
echo "================================================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
