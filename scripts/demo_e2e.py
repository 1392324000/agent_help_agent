#!/usr/bin/env python3
"""
端到端 mock 演示（aha 核心闭环）：专家注册 → 客户搜索打分 → 挑选 → 刻钟购买
→ 一对一工作（token 过期前自动续购）→ 成交汇报 → 完成。

用法（Hub 需已在 mock 模式运行，端口 20100）：
    AGENT_HUB_MOCK_CHAIN=1 python3 -u hub/hub.py          # 终端1：Hub
    python3 scripts/demo_e2e.py                            # 终端2：本演示

购买语义：专家标价是**小时价**，最小购买单位**一刻钟(0.25h)**，金额 = 标价 × 0.25。
工作期间 token 剩余有效期 < 30% 自动续购一刻钟（服务不中断），每笔订阅成交汇报。

演示 3 个专家（各自钱包身份 + 端口 + 领域能力 + hub 标价）：
  #1 medical/radiology          X光/CT 病灶检测    2.0 USDT/h
  #2 finance/quantitative_trading 股票回测/量化策略 3.0 USDT/h
  #3 programming/code_generation 代码生成与审查    1.0 USDT/h
"""
import os
import sys
import time
import json

# 演示 = mock 链：专家端订阅支付判定走 mock（模拟 USDT 转账），须在创建服务前设置
os.environ.setdefault("AGENT_HUB_MOCK_CHAIN", "1")
# 演示加速：token 有效期按 600 倍缩短（0.25h=900s→1.5s，金额不变），方便观察自动续购
os.environ.setdefault("AGENT_SUB_DURATION_SCALE", "300")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_sdk import HubClient, AgentServer, KeyPair, Wallet, protocol

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


def clean_hub_db():
    """--clean：清空 Hub 的 agents/orders/deals（仅 mock 测试环境；慎用于真实库）。"""
    import sqlite3 as _sq
    path = os.environ.get("AGENT_HUB_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub", "hub.db")
    conn = _sq.connect(path)
    c = conn.cursor()
    for t in ("deals", "orders", "agents"):
        c.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    print(f"  🧹 已清空 Hub 测试数据（{path}）")


# 客户购买参数：最小单位一刻钟（0.25h），标价是小时价
QUARTER_HOURS = 0.25
# 演示加速：token 有效期按 AGENT_SUB_DURATION_SCALE 缩短（默认 1=真实时长）
SUB_SCALE = 300          # 0.25h = 900s → 3s（金额不变，只缩有效期，方便看自动续购）
RENEW_THRESHOLD = 0.50   # 剩余有效期 < 50% 时自动续购（提前续，留出续购+调用时间）
RENEW_MAX = 5            # 演示最多续购 5 次后任务完成


def _sub_token_seconds(token) -> float:
    """该订阅 token 的总时长（秒），由 dur_h 换算（与 scale 无关的标称时长）。"""
    return float(token["payload"]["dur_h"]) * 3600


def _remaining_sec(token) -> float:
    return float(token["payload"]["exp"]) - time.time()


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

        # ---- ③ 客户以刻钟购买，连接专家（mock 支付，token 验签）----
        banner("③ 客户以刻钟(0.25h)购买影像专家服务（mock 支付，token 验签）")
        best = hits[0]
        # 从候选里挑出 medical/radiology 专家（演示"自行判定"，不只拿最高分）
        picked = next((a for a in hits if a["domain"] == "medical"), best)
        print(f"  选定: {picked['agent_id'][:16]}…  {picked['domain']}/{picked['subdomain']}"
              f"  标价 {picked.get('price') or '?'} USDT/h（最小单位：一刻钟）")

        def buy_quarter():
            """购买一刻钟，返回 (order, token)；金额 = 标价 × 0.25h。"""
            sub = cust.subscribe_to_peer(picked["agent_id"], QUARTER_HOURS)
            if not sub.get("ok"):
                print(f"  ❌ 订阅失败: {sub.get('error')}")
                sys.exit(1)
            print(f"  💳 购买一刻钟: 订单 {sub['order_id']}  金额 {sub['amount_usdt']} USDT"
                  f"（{sub['price_per_hour']} USDT/h × {QUARTER_HOURS}h）  token 验签通过")
            return sub

        sub = buy_quarter()
        token = sub["token"]
        print(f"  ⏰ token 有效期: {_sub_token_seconds(token):.0f}s（演示加速 ×{SUB_SCALE}，"
              f"真实为 15 分钟；续购阈值 {RENEW_THRESHOLD:.0%}）")

        # ---- ④ 一对一持续工作：过期前自动续购，直到工作完成 ----
        banner("④ 一对一工作：持续调用专家能力，token 剩余 < 50% 自动续购（刻钟），直到工作完成")
        seller_client = experts[[e[2]["subdomain"] for e in experts].index("radiology")][0]
        images = ["chest_xray_001.jpg", "chest_ct_002.dcm", "chest_xray_003.jpg",
                  "chest_ct_004.dcm", "chest_xray_005.jpg"]
        renews = 0
        for i, img in enumerate(images, 1):
            remain = _remaining_sec(token)
            print(f"  ── 第 {i}/{len(images)} 次工作（影像 {img}）  token 剩余 {remain:.2f}s")
            # 过期前自动续购：剩余有效期不足阈值 → 立刻续买一刻钟
            if remain < _sub_token_seconds(token) * RENEW_THRESHOLD:
                if renews >= RENEW_MAX:
                    print(f"  ⏹ 续购次数达上限({RENEW_MAX})，任务完成，不再续购")
                    break
                renews += 1
                print(f"  ↻ token 剩余 {remain:.2f}s < 阈值，自动续购第 {renews} 次（刻钟）……")
                old_exp = token["payload"]["exp"]
                sub = buy_quarter()
                token = sub["token"]
                print(f"  ✅ 续购完成: 旧 token 至 {time.strftime('%H:%M:%S', time.localtime(old_exp))}"
                      f" → 新 token 至 {time.strftime('%H:%M:%S', time.localtime(token['payload']['exp']))}"
                      f"（无空档，服务不中断）")
                # 每笔订阅都是一笔成交 → 服务方签名汇报（行情据此更新）
                seller_client.report_deal(sub["order_id"], cust_wallet.address,
                                          sub["amount_usdt"], QUARTER_HOURS)
            # 带 token 工作：需求=参数，产物=返回值（调用前兜底：剩余不足则立即续购）
            if _remaining_sec(token) < 0.3:
                print(f"  ↻ 调用前剩余不足，紧急续购……")
                sub = buy_quarter()
                token = sub["token"]
                seller_client.report_deal(sub["order_id"], cust_wallet.address,
                                          sub["amount_usdt"], QUARTER_HOURS)
            result = cust.invoke(picked["agent_id"], token, "detect_lesion", {"image": img})
            if not result.get("ok"):
                print(f"  ❌ 调用失败: {result.get('error')}")
                sys.exit(1)
            r = result.get("result", {})
            print(f"  ✅ 产出 findings: {r.get('findings')}")
            time.sleep(0.5)  # 模拟工作耗时（加速演示）

        # ---- ⑤ 成交汇总：行情 ----------------
        banner("⑤ 成交汇报汇总（每笔刻钟订阅均由服务方签名汇报 → 行情）")
        market = cust.market_prices()
        print(f"  📊 市场行情: mode={market.get('market', {}).get('mode')} "
              f"source={market.get('market', {}).get('source')} "
              f"参考价 {market.get('market', {}).get('reference')} USDT/h")

        # ---- ⑥ 安全验证：token 绑定客户钱包，第三方窃取/中间人篡改均被拒 ----
        banner("⑥ 安全验证：token 绑定客户钱包地址（防复制冒用 + 防中间人篡改）")
        attacker = HubClient(HUB_URL, Wallet.generate(), KeyPair())
        r = attacker.invoke(picked["agent_id"], dict(token), "detect_lesion", {"image": "stolen.jpg"})
        print(f"  🕵️ 攻击者({attacker.agent_id[:12]}…) 持窃取 token 调用 → "
              + (f"❌ 未被拦截! {r.get('error')}" if r.get("ok") else f"✅ 已拦截: {r.get('error')}"))

        # 中间人：截获客户合法请求，篡改 params 但不重签（签名与参数不匹配 → 拒）
        import urllib.request as _ur
        canon_ok = json.dumps({"image": "tampered.jpg"}, sort_keys=True, separators=(",", ":"))
        sig_ok = cust.wallet.sign_text(f"invoke:{token['payload']['oid']}:detect_lesion:{canon_ok}")
        body_ok = {"token": token, "capability": "detect_lesion",
                   "params": {"image": "tampered.jpg"},
                   "subscriber": cust.agent_id, "signature": sig_ok}
        body_evil = dict(body_ok)
        body_evil["params"] = {"image": "evil_injected.jpg"}  # 篡改，签名不动
        req = _ur.Request(picked["endpoint"].rstrip("/") + protocol.AGENT_ENDPOINTS["invoke"],
                          data=json.dumps(body_evil).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with _ur.urlopen(req, timeout=10) as r3:
                rr = json.loads(r3.read())
            print(f"  🕵️ 中间人篡改参数(不重签) → ❌ 未被拦截! {rr.get('error')}")
        except Exception as e:
            import urllib.error as _ue
            detail = ""
            if isinstance(e, _ue.HTTPError):
                try:
                    detail = json.loads(e.read()).get("error", str(e))
                except Exception:
                    detail = str(e)
            print(f"  🕵️ 中间人篡改参数(不重签) → ✅ 已拦截: {detail}")

        banner("🎉 演示完成：打分搜索 → 挑选 → 刻钟购买 → 过期前自动续购 → 一对一工作 → 成交")
        print(f"  共续购 {renews} 次、完成 {min(len(images), i)} 次工作；token 已绑定客户钱包（防冒用）")
        print("  Hub 记录可查: http://127.0.0.1:20100/ （仪表盘 agents/orders/deals）")
    finally:
        for client, server, prof in experts:
            try:
                server.stop()
            except Exception:
                pass
        print("\n  演示进程已清理（专家服务已停止，Hub 保留注册记录）")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean_hub_db()
    main()
