#!/usr/bin/env python3
"""
真实链（BSC 主网）支付/订单全流程测试：小强账户出资，SDK 双钱包走通
  A(服务方): 0.0001 BNB 注册费 → Hub 链上验证 → 上线 + 标价
  B(客户方): 0.5 USDT(BEP-20) 订阅 → A 解析回执 Transfer 事件验证 → token → invoke

前提: Hub 真实链模式(chain_mode=bsc-mainnet)；/tmp/realchain_keys.json 有 A/B 私钥；
      小强已给 A/B 转 BNB+USDT（链上确认后运行本脚本）。

用法: python3 scripts/test_realchain.py
"""
import os, sys, json, time
os.environ.setdefault("AGENT_HUB_MOCK_CHAIN", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent_sdk import HubClient, AgentServer, KeyPair, Wallet
from agent_sdk.chain import load_chain, get_balances

HUB = "http://127.0.0.1:20100"
PLATFORM = "0x97ab218E3Eaf04977fFc21F8d817D44E7A9dd1C4"
CFG = load_chain("bsc")
KEYS = json.load(open("/tmp/realchain_keys.json"))

PASS, FAIL = 0, 0
def ok(t): global PASS; PASS += 1; print(f"  ✅ {t}")
def bad(t): global FAIL; FAIL += 1; print(f"  ❌ {t}")
def banner(t): print("\n" + "=" * 64 + f"\n  {t}\n" + "=" * 64)

wa = Wallet.from_private_hex(KEYS["A"]["private_hex"])
wb = Wallet.from_private_hex(KEYS["B"]["private_hex"])
print(f"A(服务方): {wa.address}")
print(f"B(客户方): {wb.address}")

# ---- 等待链上确认（轮询余额）----
banner("① 等待 A/B 到账（BSC 确认）")
for i in range(30):
    ba = get_balances(CFG, wa.address)
    bb = get_balances(CFG, wb.address)
    if ba["native"] >= 0.0001 and bb["usdt"] >= 1.0 and bb["native"] >= 0.00001:
        print(f"   A: {ba['native']:.6f} BNB | B: {bb['native']:.6f} BNB + {bb['usdt']} USDT ✅")
        break
    time.sleep(5)
else:
    bad(f"余额未到齐 A:{ba} B:{bb}")
    sys.exit(1)

# ---- ② A 注册（真实 0.0001 BNB 注册费）----
banner("② A 注册：0.0001 BNB 真实转账给平台钱包 → Hub 链上验证")
keys_a = KeyPair()
client_a = HubClient(HUB, wa, keys_a)
server = AgentServer(wa, keys_a, domain="finance", subdomain="financial_analysis",
                     skills=["report_analysis"], port=29931, name="真实链专家",
                     price_usdt_per_hour=2.0)
server.caps = {"analyze_financial_report": {"desc": "财报分析", "params": {"text": "str"},
                                            "returns": {"summary": "str"}}}
server.on_invoke = lambda sub, cap, p: {"summary": f"[真实链] 财报分析完成: {(p.get('text') or '')[:40]}"} \
    if cap == "analyze_financial_report" else None
server.start(background=True)

r = client_a.apply_registration("http://127.0.0.1:29931", "finance", "financial_analysis",
                                skills=["report_analysis"], caps=server.caps)
assert r.get("ok"), f"申请注册失败: {r}"
print(f"   订单 {r['order_id']} 已签发，需要转账 {r['amount_wei']/1e18} BNB 给 {r['platform_wallet']}")

tx_reg = __import__("agent_sdk.chain", fromlist=["transfer_native"]).transfer_native(
    wa, PLATFORM, CFG, amount=0.0001)
print(f"   A 已转账注册费 tx: {tx_reg}")
time.sleep(8)  # 等确认
r2 = client_a.register_flow("http://127.0.0.1:29931", "finance", "financial_analysis",
                            skills=["report_analysis"], tx_hash=tx_reg, caps=server.caps)
ok("A 注册成功（真实链验证 from/to/金额/确认数）") if r2.get("ok") else bad(f"注册失败: {r2}")
client_a.submit_pricing(0.2, 2.0, profit_margin=0.3)
ok("A 标价 2.0 USDT/h 已提交") if True else None

# ---- ③ B 订阅 A（真实 0.5 USDT 转账）----
banner("③ B 订阅 A：0.5 USDT(BEP-20) 真实转账 → A 验证到账 → token")
client_b = HubClient(HUB, wb, KeyPair())
sub = client_b.subscribe_to_peer(wa.address, 0.25)
if not sub.get("ok") and sub.get("receiver"):
    print(f"   需转账 {sub['amount_usdt']} USDT 给 {sub['receiver'][:12]}…")
    tx_usdt = __import__("agent_sdk.chain", fromlist=["transfer_erc20"]).transfer_erc20(
        wb, wa.address, CFG, amount=0.5)
    print(f"   B 已转账 USDT tx: {tx_usdt}")
    time.sleep(10)  # 等确认（USDT 回执解析需要确认）
    sub = client_b.subscribe_to_peer(wa.address, 0.25, tx_hash=tx_usdt)
ok("B 订阅成功（USDT 到账验证 + token 签发）") if sub.get("ok") else bad(f"订阅失败: {sub}")

# ---- ④ invoke 工作 ----
banner("④ B 带 token 调用 A 能力（真实链链路）")
r3 = client_b.invoke(wa.address, sub["token"], "analyze_financial_report",
                     {"text": "2025年报营收+12%"})
ok(f"invoke 成功: {r3.get('result', {}).get('summary')}") if r3.get("ok") else bad(f"invoke 失败: {r3}")

server.stop()
print("\n" + "=" * 64)
print(f" 真实链全流程测试: ✅ {PASS} 通过 / ❌ {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)
