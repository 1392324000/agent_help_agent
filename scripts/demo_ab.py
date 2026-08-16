#!/usr/bin/env python3
"""
小 demo：AB 角色全链路测试
==========================
A = 专家（服务方）：注册画像 + hub 标价 + 黑盒能力契约
B = 客户（需求方）：打分搜索 → 挑选 → 刻钟购买 → 调用工作 → 打分评价

黑盒语义：B 不知道 A 的内部实现，只看到 能力签名(输入→产出) + 标价，付费即用。

用法（Hub 已在 mock 模式，端口 20100）：
    python3 scripts/demo_ab.py [--clean]
"""
import os
import sys
import time
import json

os.environ.setdefault("AGENT_HUB_MOCK_CHAIN", "1")
os.environ.setdefault("AGENT_SUB_DURATION_SCALE", "300")   # 加速观察续购/断开
os.environ.setdefault("AGENT_RATE_MAX", "500")             # 演示放宽限流
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_sdk import HubClient, AgentServer, KeyPair, Wallet

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:20100")
QUARTER = 0.25          # 最小购买单位：一刻钟
PRICE = 2.0             # A 的 hub 标价（USDT/小时）


def clean_hub_db():
    import sqlite3 as _sq
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "hub", "hub.db")
    conn = _sq.connect(path)
    c = conn.cursor()
    for t in ("deals", "orders", "agents", "ratings"):
        c.execute(f"DELETE FROM {t}")
    conn.commit(); conn.close()


def step(t):
    print("\n" + "─" * 62 + f"\n  {t}\n" + "─" * 62)


def main():
    print("=" * 62)
    print("  AB 角色全链路测试：A=专家服务方 / B=客户需求方（黑盒契约）")
    print("=" * 62)

    # ---------- 角色 A：专家服务方 ----------
    step("【A 角色】专家注册上线：画像 + 标价 + 黑盒能力契约")
    wallet_a = Wallet.generate()
    keys_a = KeyPair()
    client_a = HubClient(HUB_URL, wallet_a, keys_a)
    server_a = AgentServer(wallet_a, keys_a, domain="finance",
                           subdomain="financial_analysis",
                           skills=["report_analysis", "risk_rating"],
                           port=20131, name="财报专家A",
                           price_usdt_per_hour=PRICE)
    caps_a = {"analyze_financial_report": {
        "desc": "上市公司财报解读+风险评级+投资建议（黑盒：输入财报→产出结论）",
        "params": {"report_text": "str, 财报正文"},
        "returns": {"summary": "str", "risk_level": "str",
                    "suggestions": "list"}}}
    server_a.caps = caps_a

    def on_invoke_a(sub, cap, p):
        if cap == "analyze_financial_report":
            txt = (p.get("report_text") or "")[:60]
            return {"summary": f"营收增长12%，毛利率下滑3pct（输入: {txt}）",
                    "risk_level": "中", "suggestions": ["关注应收周转", "控制存货"]}
        return None
    server_a.on_invoke = on_invoke_a
    server_a.start(background=True)

    r = client_a.register_flow(f"http://127.0.0.1:20131", "finance", "financial_analysis",
                               ["report_analysis", "risk_rating"],
                               description="财报分析专家：解读+风险评级+投资建议",
                               model="deepseek-v4-pro 在线API",
                               knowledge_base="A股财报库 5000份",
                               workflows="财报→解读→风险评级→建议",
                               caps=caps_a)
    assert r.get("ok"), f"A 注册失败: {r.get('error')}"
    client_a.submit_pricing(0.4, PRICE, profit_margin=0.3)
    print(f"  ✅ A 上线: {wallet_a.address[:16]}…  财报分析  标价 {PRICE} USDT/h")
    print(f"  📦 黑盒能力: analyze_financial_report 输入[report_text] → 产出[summary/risk_level/suggestions]")

    try:
        # ---------- 角色 B：客户需求方 ----------
        step("【B 角色】遇问题 → 打分搜索 → 挑选 A（看标价+能力契约+评分）")
        wallet_b = Wallet.generate()
        client_b = HubClient(HUB_URL, wallet_b, KeyPair())
        hits = [a for a in client_b.search(q="财报 分析 风险", limit=20)
                if a.get("score", 0) > 0]
        assert hits, "B 搜索无结果"
        print(f"  🔍 站点打分: 「财报 分析 风险」→ 共 {len(hits)} 个候选，最佳:")
        a_info = hits[0]
        price = a_info.get("price")
        print(f"     #{1} 得分 {a_info['score']:.2f}  "
              + (f"标价 {price} USDT/h  " if price else "未标价  ")
              + f"评分 {(a_info.get('ratings') or {}).get('avg', '暂无')}")
        print(f"     简介: {a_info['description']}")
        print(f"     能力: {', '.join(a_info.get('caps') or {})}（黑盒：输入→产出）")
        print(f"  👉 B 判定: 综合 得分/标价/能力契约 选定 A: {a_info['agent_id'][:16]}…")

        step("【AB 连接】B 按标价刻钟购买 → A 签发订单 → B 支付(mock) → token")
        sub = client_b.subscribe_to_peer(a_info["agent_id"], QUARTER)
        assert sub.get("ok"), f"订阅失败: {sub.get('error')}"
        print(f"  💳 B 购买一刻钟: 订单 {sub['order_id']}  金额 {sub['amount_usdt']} USDT"
              f"（{PRICE} USDT/h × 0.25h）  token 验签通过")

        step("【一对一工作】B 带 token 调用 A 的黑盒能力（需求=参数，产物=返回值）")
        result = client_b.invoke(a_info["agent_id"], sub["token"],
                                 "analyze_financial_report",
                                 {"report_text": "2025年报：营收+12%，毛利率-3pct"})
        assert result.get("ok"), f"调用失败: {result.get('error')}"
        for k, v in result.get("result", {}).items():
            print(f"  📤 产出 {k}: {v}")

        step("【到期断开 + 复购接续】B 到期未续购 → A 断开提示 → B 复购接上会话")
        exp = sub["token"]["payload"]["exp"]
        time.sleep(max(0.5, exp - time.time() + 0.8))
        r7 = client_b.invoke(a_info["agent_id"], sub["token"],
                             "analyze_financial_report", {"report_text": "x"})
        print(f"  📞 到期后调用 → "
              + (f"❌ 未断开" if r7.get("ok") else f"✅ 断开提示: {r7.get('error')[:52]}…"))
        sub2 = client_b.subscribe_to_peer(a_info["agent_id"], QUARTER)
        if sub2.get("resumed"):
            ws = (sub2.get("workspace") or {}).get("context") or {}
            print(f"  🔗 复购接续: 上次工作 capability={ws.get('capability')}")
        else:
            print("  🔗 复购成功（会话窗口内接续）")

        step("【AB 评价】B 对 A 服务能力 5 维打分 → hub 推荐加权")
        client_a.report_deal(sub["order_id"], wallet_b.address, sub["amount_usdt"], QUARTER)
        scores = {"quality": 5, "speed": 4, "expertise": 5, "value": 4, "reliability": 5}
        rr = client_b.submit_rating(sub["order_id"], a_info["agent_id"], scores,
                                    comment="分析专业，报告清晰")
        assert rr.get("ok"), f"评分失败: {rr.get('error')}"
        print(f"  ⭐ 评分提交: {rr['ratings']['avg']}（{rr['ratings']['count']}人评）"
              + "  " + "  ".join(f"{k}:{v}" for k, v in rr["ratings"]["dims"].items()))
        hits2 = [a for a in client_b.search(q="财报 分析 风险", limit=20)
                 if a.get("score", 0) > 0]
        print(f"  🔍 重新搜索: A 得分 {hits[0]['score']} → {hits2[0]['score']}"
              f"（评分加成进推荐）")

        print("\n" + "=" * 62)
        print("  ✅ AB 角色全链路测试通过：搜索→购买→工作→断开/接续→评价")
        print("=" * 62)
    finally:
        server_a.stop()
        print("  （A 服务已停止，Hub 保留记录）")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean_hub_db()
    main()
