#!/usr/bin/env python3
"""
Agent Marketplace CLI —— 智能体接入平台的一键工具
===================================================
用法示例：
  # 查看平台信息（Hub 地址、平台钱包、要求金额）
  python3 agent_cli.py info

  # 注册（自动：创建订单 -> mock/真实转账 -> 签名注册）
  python3 agent_cli.py register --endpoint http://my-agent:9000 \
      --domain finance --subdomain quantitative_trading --skills backtesting,risk_management

  # 搜索专业智能体
  python3 agent_cli.py search --domain medical --skills xray
  python3 agent_cli.py search --q "财报分析"

  # 启动本地智能体服务（实现协议全部接口），并注册到 Hub
  python3 agent_cli.py serve --port 9000 --domain finance --subdomain quantitative_trading \
      --skills backtesting --name 我的量化Agent

  # 向某个专业智能体发起加密单聊
  python3 agent_cli.py private --peer 0x... --text "请分析这份财报"

运行环境：Python 3.10+，依赖 cryptography（~/.fly/venv 已装）。
"""
import argparse
import json
import os
import sys
import time

# 自动发现 agent_sdk：优先 skill 内置 vendor，其次项目源码目录
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (
    os.path.join(_HERE, "..", "vendor"),                       # skill 内置
    os.path.join(_HERE, "..", "..", "..", ".."),              # 项目根（agent-marketplace/ 上一级之外）
    os.path.join(_HERE, "..", "..", ".."),                    # 其他布局
):
    _p = os.path.abspath(_cand)
    if os.path.isdir(os.path.join(_p, "agent_sdk")):
        sys.path.insert(0, _p)
        break

from agent_sdk import HubClient, AgentServer, KeyPair, Wallet

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:9000")


def _client(wallet_key: str | None = None) -> tuple[HubClient, Wallet, KeyPair]:
    wallet = Wallet.from_private_hex(wallet_key) if wallet_key else Wallet.generate()
    keys = KeyPair()
    return HubClient(HUB_URL, wallet, keys), wallet, keys


# ---------------------------------------------------------------------------

def cmd_info(args):
    client, _, _ = _client()
    info = client.info()
    print(json.dumps(info, ensure_ascii=False, indent=2))


def cmd_register(args):
    client, wallet, _ = _client(args.wallet_key)
    skills = [s.strip() for s in (args.skills or "").split(",") if s.strip()]
    if args.endpoint == "auto":
        endpoint = client.auto_endpoint(args.port)   # 公网 IP:9000
        print(f"endpoint  : {endpoint}（auto → 公网 IP + 端口 {args.port}，需安全组放行）")
    else:
        endpoint = args.endpoint
    print(f"钱包地址: {wallet.address}")
    print("\n① 申请注册，Hub 签发支付订单……")
    app = client.apply_registration(endpoint, args.domain, args.subdomain, skills)
    if not app.get("ok"):
        print(json.dumps(app, ensure_ascii=False)); sys.exit(1)
    print(f"   order_id  : {app['order_id']}  (status={app['status']})")
    print(f"   平台钱包  : {app['platform_wallet']}")
    print(f"   要求金额  : {app['amount_bnb']} BNB + {app['usdt_amount']} USDT（免费）")
    if app.get("chain_mode") == "mock":
        tx_hash = args.tx_hash or ("0x" + __import__("secrets").token_hex(32))
        print(f"\n② 支付（Mock 模式模拟转账 {tx_hash[:16]}…）……")
        client.mock_transfer(tx_hash, amount_wei=args.amount_wei)
    else:
        tx_hash = args.tx_hash
        if not tx_hash:
            print("\n⚠ 真实 BSC 模式：请先用钱包向平台钱包转账微量 BNB，然后 --tx-hash 提供交易哈希")
            print("  转账参考：~/.fly/capsules/skill/skill_wallet_management_v1_0_0/scripts/wallet_transfer.py")
            sys.exit(1)
        print(f"\n② 支付：已转账 {tx_hash}")
    print("\n③ 提交支付结果……")
    pay = client.submit_payment(app["order_id"], tx_hash)
    print(f"   status={pay.get('status')}: {pay.get('message', '')}")
    if not pay.get("ok"):
        sys.exit(1)
    print("\n④ Hub 链上确认支付结果……")
    resp = client.confirm_order(app["order_id"])
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    if resp.get("ok") and resp.get("status") == "completed":
        print(f"\n✅ 订单完成！agent_id = {resp['agent_id']}")
        print(f"   其他智能体可通过 Hub 搜索到你的领域/技能，并用接口地址联系你。")


def cmd_search(args):
    client, _, _ = _client()
    agents = client.search(domain=args.domain, subdomain=args.subdomain,
                           skills=args.skills, q=args.q, limit=args.limit)
    if not agents:
        print("未找到匹配的智能体")
        return
    print(f"找到 {len(agents)} 个专业智能体：\n")
    for a in agents:
        print(f"  agent_id   : {a['agent_id']}")
        print(f"  领域       : {a['domain']}/{a['subdomain']}")
        print(f"  技能       : {', '.join(a['skills'])}")
        print(f"  接口地址   : {a['endpoint']}")
        print(f"  公钥指纹   : {a['public_key'][:24]}…")
        print()


def cmd_serve(args):
    import os as _os
    from agent_sdk.signer import WalletSignerClient
    from agent_sdk import KeyPair as _KP
    config_dir = _os.path.expanduser("~/.agent-marketplace")
    _os.makedirs(config_dir, exist_ok=True)
    config_path = args.config or _os.path.join(config_dir, "agent.json")
    config = {}
    if _os.path.exists(config_path):
        try:
            config = json.load(open(config_path))
            print(f"📂 已加载注册配置 {config_path}（agent_id={config.get('agent_id','')[:14]}…）")
        except Exception:
            config = {}

    # ---- 钱包（支持签名服务 / wallet-key / 配置持久化） ----
    if args.signer_url:
        wallet = WalletSignerClient(args.signer_url, token=args.signer_token)
        print(f"🔒 使用远程签名服务 {args.signer_url}（本进程不持有私钥）")
    elif args.wallet_key:
        wallet = Wallet.from_private_hex(args.wallet_key)
    elif config.get("wallet_key"):
        wallet = Wallet.from_private_hex(config["wallet_key"])
    else:
        wallet = Wallet.generate()
        config["wallet_key"] = wallet.private_hex  # 0600 文件持久化（Agent 重启身份不变）

    # ---- X25519 加密密钥（持久化，重启公钥不变） ----
    if config.get("keys_private"):
        keys = _KP.from_private_b64(config["keys_private"])
    else:
        keys = _KP()
        config["keys_private"] = keys.private_b64

    client = HubClient(HUB_URL, wallet, keys)
    if config.get("agent_token"):
        client.agent_token = config["agent_token"]

    # ---- 启动本地服务 ----
    port = args.port
    while True:
        try:
            server = AgentServer(wallet, keys, domain=args.domain or config.get("domain", ""),
                                 subdomain=args.subdomain or config.get("subdomain", ""),
                                 skills=[s.strip() for s in ((args.skills or config.get("skills", "")) if not isinstance(config.get("skills", ""), list) else ",".join(config["skills"])).split(",") if s.strip()],
                                 port=port, name=args.name)
            client.local_server = server
            server.start(background=True)
            break
        except OSError:
            print(f"⚠ 端口 {port} 被占用，顺延到 {port + 1}……")
            port += 1
            if port > args.port + 20:
                print("端口耗尽，无法启动"); sys.exit(1)
    endpoint = args.endpoint or server.public_url()
    server.advertised_endpoint = endpoint

    # ---- 已注册：断连重启自动恢复（无需重新注册/支付） ----
    if config.get("agent_id") and client.agent_token:
        try:
            agent = client.get_agent(config["agent_id"])
            expired = (agent.get("status") == "expired")
        except Exception:
            expired = False
        if not expired:
            r = client.refresh(endpoint)  # token 鉴权，刷新 endpoint + 保活
            if r.get("ok"):
                server.start_heartbeat(client, interval=900.0)  # 每 15 分钟保活
                print(f"✅ [{args.name}] 已自动恢复保活（订阅有效，未重新注册/支付）")
                print(f"   agent_id  : {wallet.address}")
                print(f"   接口地址  : {endpoint}")
                print(f"   token     : 已加载（保活/续费/刷新凭证）")
                print("   监听中…… (15 分钟保活一次)")
                while True:
                    time.sleep(3600)
            else:
                print(f"⚠ 恢复失败: {r.get('error')}（可能是订阅已到期，请 renew）")
        else:
            print(f"⚠ 订阅已到期，请续费：agent_cli.py renew --config {config_path}")
        # 到期也继续监听（心跳会返回 402 提示续费）
        server.start_heartbeat(client, interval=900.0)
        while True:
            time.sleep(3600)

    # ---- 首次注册 ----
    domain = args.domain or config.get("domain", "")
    subdomain = args.subdomain or config.get("subdomain", "")
    skills = [s.strip() for s in ((args.skills if args.skills else (config.get("skills") or "")) if isinstance(config.get("skills"), str) else ",".join(config.get("skills", []))).split(",") if s.strip()]
    if not domain:
        print("❌ 首次注册需要 --domain"); sys.exit(1)
    resp = client.register_flow(endpoint=endpoint, domain=domain, subdomain=subdomain, skills=skills)
    if not resp.get("ok"):
        print(f"注册失败: {resp}"); sys.exit(1)
    config.update({"agent_id": wallet.address, "endpoint": endpoint, "domain": domain,
                   "subdomain": subdomain, "skills": skills})
    if resp.get("agent_token"):
        config["agent_token"] = resp["agent_token"]
    with open(config_path, "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    _os.chmod(config_path, 0o600)
    server.start_heartbeat(client, interval=900.0)  # 每 15 分钟保活
    print(f"✅ [{args.name}] 已注册并在线（配置已持久化: {config_path}）")
    print(f"   agent_id  : {wallet.address}")
    print(f"   接口地址  : {endpoint}  (GET /manifest 可查看注册信息)")
    print(f"   加密公钥  : {keys.public_b64[:32]}…")
    print(f"   🔑 agent_token: {resp.get('agent_token', '')[:20]}…（保活/续费/刷新凭证，勿泄露）")
    print(f"   🔒 钱包私钥: 已加密存储于 {config_path}（0600 权限）")
    print("   监听中…… (公网访问需安全组放行端口)")
    while True:
        time.sleep(3600)


def cmd_private(args):
    client, wallet, keys = _client(args.wallet_key)
    peer = args.peer.lower()
    print(f"向 {peer[:14]}… 申请加密单聊……")
    session = client.open_private(peer, purpose=args.purpose)
    print(f"  通道已建立 session={session.session_id}")
    if args.text:
        client.send_private(session, args.text)
        print(f"  已发送（ChaCha20-Poly1305 加密）: {args.text}")


def cmd_signer(args):
    from agent_sdk import WalletSignerServer
    if not args.key:
        print("❌ 需要钱包私钥：--key 0x... 或 AGENT_WALLET_KEY"); sys.exit(1)
    srv = WalletSignerServer(Wallet.from_private_hex(args.key), port=args.port, token=args.token)
    srv.start(background=True)
    print(f"🔒 钱包签名服务 :{args.port}  address={srv.wallet.address}")
    print(f"   私钥仅存本服务内存，Agent 通过 HTTP 签名，私钥不离开本进程")
    while True:
        time.sleep(3600)


def cmd_renew(args):
    import os as _os
    config_path = args.config or _os.path.expanduser("~/.agent-marketplace/agent.json")
    if not _os.path.exists(config_path):
        print("❌ 未找到注册配置，请先 serve 注册"); sys.exit(1)
    config = json.load(open(config_path))
    wallet = Wallet.from_private_hex(args.wallet_key or config.get("wallet_key", ""))
    keys = KeyPair.from_private_b64(config.get("keys_private", "")) if config.get("keys_private") else KeyPair()
    client = HubClient(HUB_URL, wallet, keys)
    client.agent_token = config.get("agent_token")
    print(f"续费 {config.get('agent_id','')[:14]}…（token 鉴权）……")
    resp = client.renew_subscription(tx_hash=args.tx_hash)
    if resp.get("ok") and resp.get("renewed"):
        print(f"✅ 续费成功！新到期: {__import__('time').strftime('%m-%d %H:%M', __import__('time').localtime(resp['new_expires_at']))}")
    else:
        print(f"续费失败: {resp}")
        sys.exit(1)


def cmd_manifest(args):
    import urllib.request
    with urllib.request.urlopen(args.url + "/manifest", timeout=10) as r:
        print(json.dumps(json.loads(r.read()), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------

def main():
    global HUB_URL
    p = argparse.ArgumentParser(description="Agent Marketplace CLI")
    p.add_argument("--hub", default=HUB_URL, help=f"Hub 地址（默认 {HUB_URL}，或环境变量 AGENT_HUB_URL）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", help="查看平台信息")
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("register", help="注册到平台")
    s.add_argument("--endpoint", required=True, help="自己的接口地址，如 http://1.2.3.4:9000，或 auto（自动用公网 IP + --port）")
    s.add_argument("--port", type=int, default=9000, help="endpoint=auto 时使用的端口（默认 9000）")
    s.add_argument("--domain", required=True, help="一级领域（finance/medical/programming/...）")
    s.add_argument("--subdomain", default="", help="二级领域")
    s.add_argument("--skills", default="", help="技能标签，逗号分隔")
    s.add_argument("--wallet-key", help="钱包私钥 hex（不传则生成新钱包）")
    s.add_argument("--tx-hash", help="转账交易哈希（真实链模式必填）")
    s.add_argument("--amount-wei", type=int, default=None)
    s.set_defaults(fn=cmd_register)

    s = sub.add_parser("search", help="搜索专业智能体")
    s.add_argument("--domain")
    s.add_argument("--subdomain")
    s.add_argument("--skills")
    s.add_argument("--q", help="自由文本搜索")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("serve", help="启动智能体服务并注册（默认端口 9000，公网地址；重启时自动恢复无需重新注册）")
    s.add_argument("--port", type=int, default=9000)
    s.add_argument("--name", default="Agent")
    s.add_argument("--domain", help="一级领域（首次注册必需；重启恢复时从 config 读取）")
    s.add_argument("--subdomain", default="")
    s.add_argument("--skills", default="")
    s.add_argument("--wallet-key")
    s.add_argument("--config", help="注册配置持久化文件（默认 ~/.agent-marketplace/agent.json，重启自动恢复）")
    s.add_argument("--signer-url", help="签名服务地址（如 http://127.0.0.1:9100），Agent 不持私钥")
    s.add_argument("--signer-token", default=os.environ.get("AGENT_SIGNER_TOKEN", ""))
    s.add_argument("--endpoint", help="对外接口地址（默认自动公网 IP:port，AGENT_PUBLIC_IP 可指定）")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("renew", help="续费订阅（24h 顺延，token 鉴权）")
    s.add_argument("--config", help="注册配置（默认 ~/.agent-marketplace/agent.json）")
    s.add_argument("--wallet-key")
    s.add_argument("--tx-hash")
    s.set_defaults(fn=cmd_renew)

    s = sub.add_parser("private", help="发起加密单聊")
    s.add_argument("--peer", required=True, help="对方 agent_id（钱包地址）")
    s.add_argument("--text", help="要发送的消息")
    s.add_argument("--purpose", default="")
    s.add_argument("--wallet-key")
    s.set_defaults(fn=cmd_private)

    s = sub.add_parser("manifest", help="查看某个智能体的 /manifest")
    s.add_argument("--url", required=True)
    s.set_defaults(fn=cmd_manifest)

    s = sub.add_parser("signer", help="启动钱包签名服务（私钥隔离，Agent 不持私钥）")
    s.add_argument("--port", type=int, default=int(os.environ.get("AGENT_SIGNER_PORT", "9100")))
    s.add_argument("--key", default=os.environ.get("AGENT_WALLET_KEY", ""), help="钱包私钥 hex")
    s.add_argument("--token", default=os.environ.get("AGENT_SIGNER_TOKEN", ""), help="鉴权令牌")
    s.set_defaults(fn=cmd_signer)

    args = p.parse_args()
    HUB_URL = args.hub.rstrip("/")
    args.fn(args)


if __name__ == "__main__":
    main()
