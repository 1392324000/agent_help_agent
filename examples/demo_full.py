"""
端到端演示：Hub + 专业 Agent + 加密单聊/群聊
=============================================
运行：  cd agent-marketplace && python3 examples/demo_full.py

流程：
  1. 启动 Hub（Mock 链模式）
  2. 启动 金融 Agent 与 医疗 Agent，各自：创建钱包 -> 注册订单 -> 模拟转账 -> 注册
  3. 调用方搜索发现"专业 Agent"
  4. 加密单聊：金融 Agent -> 医疗 Agent（握手 + ChaCha20-Poly1305 密文传输）
  5. 加密群聊：金融 Agent 作为中心化群服务建群，消息经群服务转码转发
"""

import secrets
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["AGENT_HUB_MOCK_CHAIN"] = "1"  # 演示用 Mock 链（真实模式见 README）

from agent_sdk import HubClient, AgentServer, KeyPair, Wallet, HubError

HUB_URL = "http://127.0.0.1:9000"
SEP = "=" * 68


def start_hub() -> None:
    from hub.hub import make_server
    srv = make_server(9000, mock=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)


# ---------------------------------------------------------------------------
# 专业 Agent 工厂：注册 + 启动服务 + 业务回调
# ---------------------------------------------------------------------------

def make_agent(name: str, domain: str, subdomain: str, skills: list[str],
               port: int, reply_text: str | None = None) -> tuple[HubClient, AgentServer, Wallet, KeyPair]:
    """reply_text 为 None 时只接收不自动回复（避免对话死循环）。"""
    wallet = Wallet.generate()
    keys = KeyPair()
    client = HubClient(HUB_URL, wallet, keys)
    server = AgentServer(wallet, keys, domain=domain, subdomain=subdomain,
                         skills=skills, port=port, name=name)
    client.local_server = server  # 发起会话后同步注册到本地，使对方回复可达
    agent_id = wallet.address

    def on_private(sender, session, payload):
        print(f"   💬 [{name}] 收到加密单聊来自 {sender[:10]}… : {payload.get('content')!r}")
        if reply_text:
            print(f"   ↩️  [{name}] 加密回复（仅一次）……")
            try:
                client.send_private(session, reply_text)
            except Exception as e:
                print(f"   ⚠ [{name}] 回复失败: {e}")

    def on_group(sender, group, payload):
        # group 为群服务会话（成员↔群主）；群消息经群服务转码转发而来
        meta = server.group_meta(group.session_id)
        print(f"   👥 [{name}] 群「{meta.get('topic','')}」收到 {sender[:10]}… : {payload.get('content')!r}")

    server.on_private_message = on_private
    server.on_group_message = on_group
    server.start(background=True)

    # 注册（Hub 签发订单 -> 支付 -> 提交支付结果 -> 确认 -> 生成注册）
    endpoint = f"http://127.0.0.1:{server.port}"
    server.advertised_endpoint = endpoint
    app = client.apply_registration(endpoint, domain, subdomain, skills)
    assert app.get("ok"), f"{name} 申请注册失败: {app}"
    print(f"   📋 [{name}] 申请注册 → Hub 签发订单 {app['order_id']} (status={app['status']})")
    print(f"      → 要求转账 {app['amount_bnb']} BNB 至平台钱包 {app['platform_wallet'][:12]}…")
    tx_hash = "0x" + secrets.token_hex(32)
    if app.get("chain_mode") == "mock":
        client.mock_transfer(tx_hash)
        print(f"      → (Mock) 已转账，tx={tx_hash[:16]}…")
    pay = client.submit_payment(app["order_id"], tx_hash)
    assert pay.get("ok"), f"{name} 提交支付失败: {pay}"
    print(f"      → 提交支付结果: status={pay['status']}")
    conf = client.confirm_order(app["order_id"])
    assert conf.get("ok"), f"{name} 确认支付失败: {conf}"
    print(f"   ✅ [{name}] 确认支付 → status={conf['status']}，已生成注册")
    print(f"      agent_id={agent_id[:16]}…  domain={domain}/{subdomain}")
    return client, server, wallet, keys


# ---------------------------------------------------------------------------

def main():
    print(SEP)
    print("  Agent Marketplace 端到端演示（Hub + 加密单聊/群聊）")
    print(SEP)

    print("\n[1/5] 启动 Hub（Mock 链模式）……")
    start_hub()
    probe = HubClient(HUB_URL, Wallet.generate(), KeyPair())
    info = probe.info()
    print(f"      平台钱包: {info['platform_wallet']}")
    print(f"      要求金额: {info['min_bnb']} BNB + {info['usdt_amount']} USDT（免费）")
    print(f"      模式    : {info['chain_mode']}")

    print("\n[2/5] 启动并注册 金融 / 医疗 Agent……")
    fin_client, fin_server, _, _ = make_agent(
        "金融Agent", "finance", "quantitative_trading",
        ["backtesting", "risk_management"], 9001)   # 发起方：只收不自动回复
    med_client, med_server, _, _ = make_agent(
        "医疗Agent", "medical", "radiology",
        ["xray_analysis", "report_generation"], 9002,
        "影像分析完成：未见明显异常，建议随访")   # 被邀请方：自动回复一次

    print("\n[3/5] 调用方注册为「通用搜索 Agent」，并通过 Hub 搜索专业 Agent……")
    caller_wallet = Wallet.generate()
    caller_keys = KeyPair()
    caller = HubClient(HUB_URL, caller_wallet, caller_keys)
    caller_server = AgentServer(caller_wallet, caller_keys, domain="search", subdomain="web_search",
                                skills=["knowledge_query"], port=9003, name="通用搜索Agent")
    caller.local_server = caller_server
    caller_server.start(background=True)
    resp = caller.register_flow(endpoint=f"http://127.0.0.1:{caller_server.port}",
                                domain="search", subdomain="web_search", skills=["knowledge_query"])
    assert resp.get("ok"), f"调用方注册失败: {resp}"
    print(f"       ✅ [通用搜索Agent] 注册成功  agent_id={caller.agent_id[:16]}…")
    finance_peers = caller.find_peers(domain="finance")
    medical_peers = caller.find_peers(domain="medical")
    print(f"      finance 领域找到 {len(finance_peers)} 个:")
    for a in finance_peers:
        print(f"        - {a['agent_id'][:14]}… {a['domain']}/{a['subdomain']} skills={a['skills']} @ {a['endpoint']}")
    print(f"      medical 领域找到 {len(medical_peers)} 个:")
    for a in medical_peers:
        print(f"        - {a['agent_id'][:14]}… {a['domain']}/{a['subdomain']} skills={a['skills']} @ {a['endpoint']}")

    print("\n[4/5] 加密单聊：金融 Agent 请求医疗 Agent……")
    med_id = medical_peers[0]["agent_id"]
    session = fin_client.open_private(med_id, purpose="请求胸部X光影像分析")
    print(f"      ✅ 单聊通道已建立（加密握手完成）session={session.session_id}")
    print(f"      📨 发送加密消息……")
    fin_client.send_private(session, "请分析这份胸部X光影像，重点看右下肺野")
    time.sleep(0.5)

    print("\n[5/5] 加密群聊（中心化群服务）：金融 Agent 作为群服务建群，拉入医疗 Agent + 通用搜索 Agent……")
    group = fin_client.open_group([med_id, caller.agent_id], topic="多学科会诊")
    print(f"      ✅ 群「多学科会诊」建立，群服务 = 金融Agent，成员: {[m[:10]+'…' for m in group['members']]}")
    print(f"      📨 群主（群服务）发群消息 → 转码转发给每个成员……")
    fin_client.send_group(group, "各位，这是肺癌筛查联合分析项目，请发表意见")
    time.sleep(0.5)

    # 医疗 Agent 作为成员：用与群服务的会话加密，发到群服务（金融Agent）转发
    print(f"      📨 医疗 Agent 发群消息 → 群服务转码转发……")
    meta = med_server.group_meta(group["group_id"])
    med_group = {"group_id": group["group_id"], "owner": meta.get("owner"), "members": meta.get("members", [])}
    med_client.send_group(med_group, "影像学支持随访，病理建议穿刺活检")
    time.sleep(0.5)

    print("\n" + SEP)
    print("  演示完成 ✅  Hub 注册、领域发现、加密单聊、加密群聊全部跑通")
    print("  Hub 数据: hub/hub.db | 各 Agent 心跳保活中")
    print(SEP)


if __name__ == "__main__":
    main()
