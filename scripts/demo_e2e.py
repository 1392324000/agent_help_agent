#!/usr/bin/env python3
"""
端到端 mock 演示（aha 核心闭环）：专家注册 → 客户搜索打分 → 挑选 → 订阅连接(mock支付)
→ invoke 一对一工作 → 成交汇报 → 完成。

用法（Hub 需已在 mock 模式运行，端口 20100）：
    AGENT_HUB_MOCK_CHAIN=1 python3 -u hub/hub.py          # 终端1：Hub
    python3 scripts/demo_e2e.py                            # 终端2：本演示

演示 3 个专家（各自钱包身份 + 端口 + 领域能力 + hub 标价）：
  #1 medical/radiology          X光/CT 病灶检测    2.0 USDT/h
  #2 finance/quantitative_trading 股票回测/量化策略 3.0 USDT/h
  #3 programming/code_generation 代码生成与审查    1.0 USDT/h
"""
import os
import sys
import time

# 演示 = mock 链：专家端订阅支付判定走 mock（模拟 USDT 转账），须在创建服务前设置
os.environ.setdefault("AGENT_HUB_MOCK_CHAIN", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_sdk import HubClient, AgentServer, KeyPair, Wallet

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:20100")
BASE_PORT = 20111

# ---------------------------------------------------------------------------
# 专家画像 + 能力（黑盒契约：能力名 -> 输入/产出 签名）
# ---------------------------------------------------------------------------
EXPERTS = [
    dict(name="影像专家", domain="medical", subdomain="radiology",
         skills=["xray_analysis", "ct_detection"],
         description="AI影像科：X光/CT病灶检测，输出结构化检测报告",
         model="deepseek-v4-flash 在线API", knowledge_base="影像库200G",
         workflows="影像→检测→报告",
         price=2.0, cost=0.35,
         caps={"detect_lesion": {"desc": "X光/CT影像病灶检测，输出结构化报告",
                                 "params": {"image": "str, 影像路径/URL"},
                                 "returns": {"findings": "str", "confidence": "float",
                                             "report": "str"}}}),
    dict(name="量化专家", domain="finance", subdomain="quantitative_trading",
         skills=["backtesting", "strategy_optimization"],
         description="股票回测与量化策略：历史回测+参数优化+风险报告",
         model="deepseek-v4-pro 在线API", knowledge_base="A股历史行情10年",
         workflows="数据→回测→策略报告",
         price=3.0, cost=0.6,
         caps={"backtest_strategy": {"desc": "策略历史回测，输出收益/风险指标",
                                     "params": {"symbol": "str, 标的", "strategy": "str, 策略描述"},
                                     "returns": {"annual_return": "float", "sharpe": "float",
                                                 "max_drawdown": "float", "trades": "int"}}}),
    dict(name="代码专家", domain="programming", subdomain="code_generation",
         skills=["codegen", "code_review"],
         description="代码生成与审查：需求→实现→单测，多语言",
         model="gpt-4o 在线API", knowledge_base="开源代码库",
         workflows="需求→编码→测试",
         price=1.0, cost=0.2,
         caps={"generate_code": {"desc": "按需求生成代码+单测",
                                 "params": {"requirement": "str, 需求描述", "language": "str, 语言"},
                                 "returns": {"code": "str", "language": "str",
                                             "tests": "str"}}}),
]

# 能力实现（一对一工作状态：入站已自动打 [UNTRUSTED_INPUT] 标记，出站自动脱敏防护）
def _handle_invoke(name, cap, p):
    if cap == "detect_lesion":
        img = p.get("image", "?")
        return {"findings": f"[{name}] 右上肺野见磨玻璃影，建议随访（输入影像: {img}）",
                "confidence": 0.93, "report": f"{name}检测报告：结节性病灶，直径约 6mm，BI-RADS 3 类"}
    if cap == "backtest_strategy":
        return {"annual_return": 0.187, "sharpe": 1.42, "max_drawdown": -0.086,
                "trades": 126, "note": f"[{name}] 回测完成（{p.get('symbol','?')} / {p.get('strategy','?')}）"}
    if cap == "generate_code":
        return {"code": f"def solve():\n    # [{name}] 按需求实现: {p.get('requirement','')}\n    return 'ok'",
                "language": p.get("language", "python"), "tests": "pytest 3 passed"}
    return None


def banner(t):
    print("\n" + "=" * 68 + f"\n  {t}\n" + "=" * 68)


def main():
    banner("aha 端到端演示：专家注册 → 客户搜索打分 → 挑选 → 订阅连接 → 一对一工作 → 完成")
    experts = []  # (client, server, profile)
    try:
        # ---- ① 专家注册：钱包身份 + 服务 + hub 标价 ----
        banner("① 3 位专家注册上线（各自钱包身份，mock 链自动支付，hub 标注价格）")
        for i, prof in enumerate(EXPERTS):
            port = BASE_PORT + i
            wallet = Wallet.generate()
            keys = KeyPair()
            client = HubClient(HUB_URL, wallet, keys)
            server = AgentServer(wallet, keys, domain=prof["domain"],
                                 subdomain=prof["subdomain"], skills=prof["skills"],
                                 port=port, name=prof["name"],
                                 price_usdt_per_hour=prof["price"])
            server.caps = prof["caps"]
            server.on_invoke = lambda sub, cap, p, _n=prof["name"]: _handle_invoke(_n, cap, p)
            server.start(background=True)
            endpoint = f"http://127.0.0.1:{port}"
            r = client.register_flow(endpoint, prof["domain"], prof["subdomain"],
                                     prof["skills"], description=prof["description"],
                                     model=prof["model"], knowledge_base=prof["knowledge_base"],
                                     workflows=prof["workflows"], caps=prof["caps"])
            if not r.get("ok"):
                print(f"  ❌ 专家#{i+1} {prof['name']} 注册失败: {r.get('error')}")
                sys.exit(1)
            # 提交标价到 hub（search 结果里展示 price）
            pr = client.submit_pricing(prof["cost"], prof["price"], profit_margin=0.3)
            ok = pr.get("ok")
            print(f"  ✅ 专家#{i+1} {prof['name']:8s} {wallet.address[:16]}…  "
                  f"{prof['domain']}/{prof['subdomain']}  标价 {prof['price']} USDT/h"
                  + ("" if ok else f"  ⚠️ 标价提交: {pr.get('error')}"))
            experts.append((client, server, prof))
        time.sleep(0.5)

        # ---- ② 客户搜索（站点打分，最多 20 个候选）----
        banner("② 客户搜索「X光 病灶检测」（站点打分排序）")
        cust_wallet = Wallet.generate()
        cust = HubClient(HUB_URL, cust_wallet, KeyPair())
        hits = [a for a in cust.search(q="X光 病灶检测", limit=20) if a.get("score", 0) > 0]
        for i, a in enumerate(hits, 1):
            price = a.get("price")
            print(f"  #{i} 得分 {a['score']:.2f}  " + (f"标价 {price} USDT/h" if price else "未标价"))
            print(f"     {a['agent_id'][:16]}…  {a['domain']}/{a['subdomain']}")
            if a.get("description"):
                print(f"     {a['description']}")
        if not hits:
            print("  ❌ 无匹配候选"); sys.exit(1)

        # ---- ③ 客户挑选：按需求判定（得分+能力+标价）→ 订阅连接（mock 支付）----
        banner("③ 客户挑选「影像专家」并订阅连接（mock 支付，token 验签）")
        best = hits[0]
        # 从候选里挑出 medical/radiology 专家（演示"自行判定"，不只拿最高分）
        picked = next((a for a in hits if a["domain"] == "medical"), best)
        print(f"  选定: {picked['agent_id'][:16]}…  {picked['domain']}/{picked['subdomain']}"
              f"  标价 {picked.get('price') or '?'} USDT/h")
        sub = cust.subscribe_to_peer(picked["agent_id"], 0.25)
        if not sub.get("ok"):
            print(f"  ❌ 订阅失败: {sub.get('error')}"); sys.exit(1)
        print(f"  ✅ 订阅成功: 订单 {sub['order_id']}  金额 {sub['amount_usdt']} USDT"
              f"（{sub['price_per_hour']} USDT/h × 0.25h）  token 验签通过")
        token = sub["token"]

        # ---- ④ 一对一工作：调用专家能力，直到产出结果 ----
        banner("④ 一对一工作：客户带 token 调用专家能力（需求=参数，产物=返回值）")
        cap = list(picked.get("caps") or {}).pop(0) if picked.get("caps") else "detect_lesion"
        result = cust.invoke(picked["agent_id"], token, cap,
                             {"image": "chest_xray_2026_0816.jpg"})
        if not result.get("ok"):
            print(f"  ❌ 调用失败: {result.get('error')}"); sys.exit(1)
        print(f"  能力: {cap}")
        for k, v in result.get("result", {}).items():
            print(f"  产出 {k}: {v}")

        # ---- ⑤ 成交汇报（服务方签名）→ 行情更新 ----
        banner("⑤ 成交汇报（服务方签名 deal → Hub 行情更新）")
        seller_client = experts[[e[2]["subdomain"] for e in experts].index("radiology")][0]
        deal = seller_client.report_deal(sub["order_id"], cust_wallet.address,
                                         sub["amount_usdt"], 0.25)
        print(f"  ✅ 成交已记录: {deal.get('message')}")
        market = cust.market_prices()
        print(f"  📊 市场行情: mode={market.get('market', {}).get('mode')} "
              f"source={market.get('market', {}).get('source')} "
              f"参考价 {market.get('market', {}).get('reference')} USDT/h")

        banner("🎉 演示完成：搜索打分 → 挑选 → 订阅连接 → 一对一工作 → 成交，全链路通")
        print("  Hub 记录可查: http://127.0.0.1:20100/ （仪表盘 agents/orders/deals）")
    finally:
        for client, server, prof in experts:
            try:
                server.stop()
            except Exception:
                pass
        print("\n  演示进程已清理（专家服务已停止，Hub 保留注册记录）")


if __name__ == "__main__":
    main()
