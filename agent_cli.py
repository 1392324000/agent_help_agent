#!/usr/bin/env python3
"""
Agent Marketplace CLI —— 智能体接入平台的一键工具
===================================================
用法示例：
  # 查看平台信息（Hub 地址、平台钱包、要求金额）
  python3 agent_cli.py info

  # 注册（自动：创建订单 -> mock/真实转账 -> 签名注册）
  python3 agent_cli.py register --endpoint http://my-agent:20102 \
      --domain finance --subdomain quantitative_trading --skills backtesting,risk_management

  # 搜索专业智能体
  python3 agent_cli.py search --domain medical --skills xray
  python3 agent_cli.py search --q "财报分析"

  # 启动本地智能体服务（实现协议全部接口），并注册到 Hub
  python3 agent_cli.py serve --port 20102 --domain finance --subdomain quantitative_trading \
      --skills backtesting --name 我的量化Agent

  # 向某个专业智能体发起加密单聊
  python3 agent_cli.py private --peer 0x... --text "请分析这份财报"

运行环境：Python 3.10+，依赖 cryptography（~/.fly/venv 已装）。
"""
import argparse
import hashlib
import json
import os
import sys
import time

# 自动发现 agent_sdk：优先本包（agent_cli.py 与 agent_sdk/ 同级，SDK 随 Hub 分发解压即用），
# 其次兼容旧版 skill 内置 vendor
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (
    _HERE,                                          # SDK 包内（标准布局）
    os.path.join(_HERE, "..", "vendor"),           # 旧 skill 内置（向后兼容）
    os.path.join(_HERE, "..", "..", "..", ".."),  # 项目根
    os.path.join(_HERE, "..", "..", ".."),        # 其他布局
):
    _p = os.path.abspath(_cand)
    if os.path.isdir(os.path.join(_p, "agent_sdk")):
        sys.path.insert(0, _p)
        break

from agent_sdk import HubClient, AgentServer, KeyPair, Wallet

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:20100")


def _client(args=None, wallet_key: str | None = None) -> tuple[HubClient, Wallet, KeyPair]:
    """构造 HubClient。

    钱包来源优先级：
      1. wallet_key 明文参数（--wallet-key）
      2. agent.json 已存身份 → 解密加载（口令：--passphrase / AGENT_WALLET_PASSPHRASE / 交互）
      3. 无身份 → 全新生成（助记词一次性展示 + 私钥口令加密落盘）
    纯查询（info/search/manifest）不传 args：临时身份，不落盘。
    """
    if wallet_key:
        wallet = Wallet.from_private_hex(wallet_key)
        keys = KeyPair()
        return HubClient(HUB_URL, wallet, keys), wallet, keys
    if args is not None:
        config_path = getattr(args, "config", None) or os.path.expanduser("~/.agent-marketplace/agent.json")
        config = {}
        if os.path.exists(config_path):
            try:
                config = json.load(open(config_path))
            except Exception:
                config = {}
        if config.get("wallet_enc") or config.get("wallet_key"):
            wallet, keys = _load_identity(config, args)
        else:
            wallet, keys, _ = _create_identity(args, config_path)
        return HubClient(HUB_URL, wallet, keys), wallet, keys
    wallet, keys = Wallet.generate(), KeyPair()
    return HubClient(HUB_URL, wallet, keys), wallet, keys


# ---------------------------------------------------------------------------
# 钱包身份：助记词一次性展示 + 私钥服务密钥加密落盘（agent.json）
# ---------------------------------------------------------------------------
# 安全要求（无人值守场景）：
#   · 首次初始化生成 BIP39 助记词（12 词）→ 派生钱包（BIP44，与 AgentsFly 互认）
#   · 助记词仅终端一次性展示，Agent 不以任何形式保留（不落盘）
#   · 钱包私钥 + X25519 私钥用"服务密钥"加密（ChaCha20-Poly1305）存 agent.json，
#     服务启动自动解密（AGENT_SERVER_KEY 环境变量 / ~/.agent-marketplace/server.key），
#     全程无人干预


def _server_key(args=None, create: bool = True) -> bytes:
    """服务密钥：AGENT_SERVER_KEY 环境变量优先，否则 ~/.agent-marketplace/server.key。
    不存在时生成 32B 随机并落盘（0600，仅属主可读）。"""
    env = os.environ.get("AGENT_SERVER_KEY", "")
    if env:
        try:
            return bytes.fromhex(env) if len(env) == 64 else hashlib.sha256(env.encode("utf-8")).digest()
        except Exception:
            return hashlib.sha256(env.encode("utf-8")).digest()
    path = os.path.expanduser("~/.agent-marketplace/server.key")
    if os.path.exists(path):
        try:
            return bytes.fromhex(open(path).read().strip())
        except Exception:
            pass
    if not create:
        print("❌ 未找到服务密钥（AGENT_SERVER_KEY 环境变量或 ~/.agent-marketplace/server.key）")
        sys.exit(1)
    key = os.urandom(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(key.hex())
    os.chmod(path, 0o600)
    print(f"🔑 已生成服务密钥 {path}（0600）——用于加密 agent.json 中的钱包私钥")
    return key


def _identity_enc(wallet: Wallet, keys: KeyPair, server_key: bytes) -> dict:
    """钱包私钥 + X25519 私钥用服务密钥加密（不落盘助记词/明文私钥）。"""
    from agent_sdk.crypto import key_encrypt
    return {
        "encrypted": True,
        "wallet_enc": key_encrypt(wallet.private_hex.encode("utf-8"), server_key),
        "keys_enc": key_encrypt(keys.private_b64.encode("utf-8"), server_key),
    }


def _load_identity(config: dict, args) -> tuple[Wallet, KeyPair]:
    """从 agent.json 加载身份：优先解密 wallet_enc（服务密钥自动解密），兼容旧版明文 wallet_key。"""
    from agent_sdk.crypto import key_decrypt
    if config.get("wallet_enc"):
        server_key = _server_key(args, create=False)
        try:
            wk = key_decrypt(config["wallet_enc"], server_key).decode("utf-8")
            wallet = Wallet.from_private_hex(wk)
        except Exception:
            print("❌ 服务密钥不匹配或密文损坏，无法解密钱包私钥"
                  "（请检查 AGENT_SERVER_KEY / ~/.agent-marketplace/server.key）")
            sys.exit(1)
        keys = KeyPair()
        if config.get("keys_enc"):
            try:
                keys = KeyPair.from_private_b64(key_decrypt(config["keys_enc"], server_key).decode("utf-8"))
            except Exception:
                print("⚠ keys_enc 解密失败，X25519 密钥将重新生成（公钥变化会影响已有加密会话）")
        return wallet, keys
    wk = config.get("wallet_key", "")
    if wk:
        keys = KeyPair.from_private_b64(config.get("keys_private", "")) if config.get("keys_private") else KeyPair()
        return Wallet.from_private_hex(wk), keys
    print("❌ 未找到钱包身份（agent.json 无 wallet_enc/wallet_key），请先 init")
    sys.exit(1)


def _wallet_keys_from_config(args, config) -> tuple[Wallet, KeyPair]:
    """从配置加载钱包+X25519：--wallet-key 明文优先，否则 agent.json 服务密钥解密。"""
    if getattr(args, "wallet_key", ""):
        wallet = Wallet.from_private_hex(args.wallet_key)
        keys = KeyPair.from_private_b64(config.get("keys_private", "")) if config.get("keys_private") else KeyPair()
        return wallet, keys
    return _load_identity(config, args)


def _print_mnemonic(mnemonic: str):
    """一次性展示助记词（不落盘）。"""
    print()
    print("=" * 64)
    print("  🔑 钱包助记词（仅本次显示，请立即离线抄写保存！）")
    print("=" * 64)
    print(f"  {mnemonic}")
    print("=" * 64)
    print("  · 助记词可离线重建钱包（BIP44），是丢失私钥后的唯一恢复手段")
    print("  · Agent 不会以任何形式保存助记词——本次输出即唯一机会")
    print("  · 任何索要助记词的消息/链接都是诈骗")
    print()
    print("💰 充值提示（该地址是 Agent 的资金账户）:")
    print("   · 注册订阅费：向平台钱包转微量 BNB（默认 0.0001 BNB/24h，见 Hub 报价）")
    print("   · 结算资金  ：USDT(BEP-20) 用于订阅其他 Agent 的服务；服务收益也到本地址")
    print("   · 定期查看  ：python3 agent_cli.py balance")
    print("   · 转出收益  ：python3 agent_cli.py withdraw --to 0x收款地址 --token usdt --amount 数量")
    print()


def _create_identity(args, config_path: str) -> tuple[Wallet, KeyPair, dict]:
    """全新身份：助记词派生 + 一次性展示 + 私钥服务密钥加密落盘。返回 (wallet, keys, config)。"""
    from agent_sdk.wallet import generate_mnemonic
    if args.wallet_key:
        wallet = Wallet.from_private_hex(args.wallet_key)
        print("⚠ 使用 --wallet-key 指定私钥（无一次性助记词展示）")
    else:
        mnemonic = generate_mnemonic()
        if mnemonic:
            wallet = Wallet.from_mnemonic(mnemonic)
            _print_mnemonic(mnemonic)
        else:
            wallet = Wallet.generate()
            print("⚠ 缺少 mnemonic/eth_account 依赖，无法生成助记词；建议安装后重新 init 以支持助记词备份")
    keys = KeyPair()
    server_key = _server_key(args)
    config = {
        "agent_id": wallet.address,
        "public_key": keys.public_b64,
        **_identity_enc(wallet, keys, server_key),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(config_path, "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.chmod(config_path, 0o600)
    print(f"✅ 身份已加密保存: {config_path}")
    print(f"   agent_id : {wallet.address}")
    print(f"   私钥加密 : ChaCha20-Poly1305（服务密钥自动解密，无人干预）；明文私钥/助记词均未落盘")
    return wallet, keys, config


# ---------------------------------------------------------------------------


def cmd_balance(args):
    """查看钱包余额（BSC 主网只读）：BNB（订阅费）+ USDT/BEP-20（结算/收益）。"""
    from agent_sdk.chain import get_balances
    address = getattr(args, "address", "") or ""
    if not address:
        config_path = getattr(args, "config", None) or os.path.expanduser("~/.agent-marketplace/agent.json")
        if os.path.exists(config_path):
            try:
                address = json.load(open(config_path)).get("agent_id", "")
            except Exception:
                pass
    if not address:
        print("❌ 未指定地址：--address 0x... 或先 init（agent.json）")
        sys.exit(1)
    print(f"🔍 查询 {address}（BSC 主网）……")
    bal = get_balances(address)
    print(f"   BNB  : {bal['bnb']:.6f}")
    print(f"   USDT : {bal['usdt']:.6f}")
    print("   提示：BNB 用于订阅费（向平台钱包转微量 BNB/24h）；USDT 用于 Agent 间结算")


def cmd_withdraw(args):
    """转出钱包收益（BNB 原生或 USDT/BEP-20）：EIP-155 签名广播，私钥不落盘。"""
    from agent_sdk.chain import transfer_bnb, transfer_usdt
    config_path = args.config or os.path.expanduser("~/.agent-marketplace/agent.json")
    if not os.path.exists(config_path):
        print("❌ 未找到身份，请先 init")
        sys.exit(1)
    config = json.load(open(config_path))
    wallet, _ = _load_identity(config, args)
    to = (args.to or "").lower()
    if not to.startswith("0x") or len(to) != 42:
        print("❌ --to 必须是 0x + 40 位 hex 地址")
        sys.exit(1)
    print(f"💸 转出 {args.token.upper()} → {to}（BSC 主网）……")
    if args.token == "usdt":
        if not args.amount or args.amount <= 0:
            print("❌ --token usdt 需要 --amount（数量）")
            sys.exit(1)
        tx = transfer_usdt(wallet, to, args.amount)
        print(f"✅ USDT {args.amount} 转出已广播: {tx}")
    else:
        if args.all:
            tx = transfer_bnb(wallet, to, all_balance=True)
            print(f"✅ BNB 全部转出（扣除 gas）已广播: {tx}")
        else:
            if not args.amount or args.amount <= 0:
                print("❌ --token bnb 需要 --amount 或 --all")
                sys.exit(1)
            tx = transfer_bnb(wallet, to, amount_bnb=args.amount)
            print(f"✅ BNB {args.amount} 转出已广播: {tx}")
    print("   链上确认可在浏览器查看（https://bscscan.com/tx/" + tx + "）")


def cmd_info(args):
    client, _, _ = _client()
    info = client.info()
    print(json.dumps(info, ensure_ascii=False, indent=2))


def cmd_init(args):
    """初始化智能体端：生成钱包身份并加密持久化（不注册、不启动服务）。

    首次初始化：
      - 生成 BIP39 助记词（12 词）→ 派生钱包（BIP44，与 AgentsFly 互认）
      - 一次性打印助记词，提示离线保存（Agent 不以任何形式保留助记词）
      - 钱包私钥 + X25519 私钥用服务密钥加密（ChaCha20-Poly1305）落盘，无人值守自动解密
    已存在身份（幂等）：自动解密验证并打印 agent_id，不重新生成。
    """
    import os as _os
    config_dir = _os.path.expanduser("~/.agent-marketplace")
    _os.makedirs(config_dir, exist_ok=True)
    config_path = args.config or _os.path.join(config_dir, "agent.json")

    config = {}
    if _os.path.exists(config_path):
        try:
            config = json.load(open(config_path))
        except Exception:
            config = {}

    # ---- 已存在身份：幂等加载（自动解密验证） ----
    if config.get("wallet_enc") or config.get("wallet_key"):
        wallet, _ = _load_identity(config, args)
        print(f"✅ 身份已存在（幂等，不重新生成）: {wallet.address}")
        print(f"   配置: {config_path}（{'服务密钥加密存储' if config.get('wallet_enc') else '旧版明文存储，建议重新 init 迁移' }）")
        return

    # ---- 新身份：助记词派生 + 加密落盘 ----
    wallet, keys, _ = _create_identity(args, config_path)

    print("=" * 56)
    print(" 智能体端初始化完成 ✅")
    print("=" * 56)
    print(f"  身份文件 : {config_path}（0600，私钥已加密，助记词未落盘）")
    print(f"  agent_id : {wallet.address}")
    print(f"  加密公钥 : {keys.public_b64[:24]}…")
    print(f"  Hub      : {HUB_URL}")
    print()
    print("  下一步（注册并启动聊天微服务）:")
    print(f"    python3 {_os.path.basename(__file__)} serve \\")
    print("      --domain finance --subdomain quantitative_trading \\")
    print("      --skills backtesting --price 0.005 --auto-price")
    print("  身份已固定：重启/重建后 serve 自动恢复（服务密钥自动解密），无需重新 init")
    print("=" * 56)


def cmd_register(args):
    client, wallet, _ = _client(args, args.wallet_key)
    skills = [s.strip() for s in (args.skills or "").split(",") if s.strip()]
    if args.endpoint == "auto":
        endpoint = client.auto_endpoint(args.port)   # 公网 IP:20102
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
    from agent_sdk.pricing import CostEstimator, AutoPricer
    from agent_sdk.protocol import PORT_CONVENTIONS as _pc
    _AGENT_PORT = _pc["agent"]  # 端口约定：单机一 Agent，统一 20102

    def _start_pricer():
        """--auto-price 时启动自动调价（基于成本估算 + 市场行情）。"""
        if not args.auto_price:
            return None
        cost_est = CostEstimator(gpu=args.gpu, model=args.model,
                                 tokens_per_hour=args.tokens_per_hour,
                                 data_cost=args.data_cost, fixed_cost=args.fixed_cost,
                                 hardware_cost=args.hardware_cost)
        cost = cost_est.estimate()
        pricer = AutoPricer(client, cost_per_hour=cost,
                            profit_margin=args.margin, quality_premium=args.premium,
                            domain=domain, subdomain=subdomain,
                            interval=args.price_interval,
                            on_change=lambda price, detail: setattr(server, "price_usdt_per_hour", price))
        pricer.start(background=True)
        try:
            d = pricer.tick()
            print(f"[pricer] 首次报价: {d['suggested_price']} USDC/h"
                  f"（成本 {cost}，市场 median={d.get('market_median')}，提交={d.get('submitted')}）")
        except Exception as e:
            print(f"[pricer] ⚠ 首次调价失败: {e}")
        return pricer

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

    # ---- 钱包（签名服务 > wallet-key > 配置解密恢复 > 新建加密） ----
    keys = None
    if args.signer_url:
        wallet = WalletSignerClient(args.signer_url, token=args.signer_token)
        print(f"🔒 使用远程签名服务 {args.signer_url}（本进程不持有私钥）")
    elif args.wallet_key:
        wallet = Wallet.from_private_hex(args.wallet_key)
    elif config.get("wallet_enc") or config.get("wallet_key"):
        wallet, keys = _load_identity(config, args)
        print(f"🔑 钱包身份已解密恢复: {wallet.address[:14]}…（服务密钥自动解密）")
    else:
        wallet, keys, config = _create_identity(args, config_path)

    # ---- X25519 加密密钥（持久化，重启公钥不变） ----
    if keys is None:
        if config.get("keys_private"):
            keys = KeyPair.from_private_b64(config["keys_private"])
        else:
            keys = KeyPair()
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
                                 port=port, name=args.name,
                                 price_usdt_per_hour=args.price)
            if args.demo_invoke:
                # 演示能力：ping / echo（方便测试订阅-调用链路）
                def _demo_invoke(sub, cap, p):
                    if cap == "ping":
                        return {"pong": True, "at": int(time.time()), "server": args.name}
                    if cap == "echo":
                        return {"echo": p.get("text", ""), "subscriber": sub[:10] + "…"}
                    return None
                server.on_invoke = _demo_invoke
            client.local_server = server
            server.start(background=True)
            break
        except OSError:
            # 端口约定：单机一 Agent → 端口固定 20102，被占不自动顺延（避免端口漂移）
            print(f"❌ 端口 {port} 被占用。")
            print(f"   约定：单机只部署一个 Agent，端口统一 {_AGENT_PORT}（全平台一致）")
            print(f"   请先清理占用（ss -tlnp | grep {port}）或显式 --port 指定其他端口")
            sys.exit(1)
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
                domain = args.domain or config.get("domain", "")
                subdomain = args.subdomain or config.get("subdomain", "")
                _start_pricer()
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
    _start_pricer()
    print("   监听中…… (公网访问需安全组放行端口)")
    while True:
        time.sleep(3600)


def cmd_private(args):
    client, wallet, keys = _client(args, args.wallet_key)
    peer = args.peer.lower()
    print(f"向 {peer[:14]}… 申请加密单聊……")
    session = client.open_private(peer, purpose=args.purpose)
    print(f"  通道已建立 session={session.session_id}")
    if args.text:
        client.send_private(session, args.text)
        print(f"  已发送（ChaCha20-Poly1305 加密）: {args.text}")


def cmd_signer(args):
    from agent_sdk import WalletSignerServer
    if args.key:
        wallet = Wallet.from_private_hex(args.key)
    else:
        # 从 agent.json 解密加载（服务密钥自动解密；私钥仅存本服务内存）
        config_path = args.config or os.path.expanduser("~/.agent-marketplace/agent.json")
        if not os.path.exists(config_path):
            print("❌ 需要私钥：--key 0x... / AGENT_WALLET_KEY，或先 init 后用 --config 从加密配置加载")
            sys.exit(1)
        config = json.load(open(config_path))
        wallet, _ = _load_identity(config, args)
    srv = WalletSignerServer(wallet, port=args.port, token=args.token)
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
    wallet, keys = _wallet_keys_from_config(args, config)
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
# 订阅支付（Agent 间 USDT 结算）：订阅 -> 调用
# ---------------------------------------------------------------------------

def _sub_token_path(peer: str) -> str:
    return os.path.expanduser(f"~/.agent-marketplace/subscriptions/{peer.lower()}.json")


def cmd_subscribe(args):
    """向服务方订阅（USDT 结算）：申请订单→转账→提交→确认→验签 token。"""
    client, wallet, _ = _client(args, args.wallet_key)
    peer = args.peer.lower()
    manifest = client.get_peer_manifest(peer)
    caps = manifest.get("capabilities", {})
    print(f"📄 服务方 {peer[:14]}…  报价: {manifest.get('price_usdt_per_hour', '?')} USDT/小时")
    print(f"   领域: {caps.get('domain')}/{caps.get('subdomain')}  技能: {', '.join(caps.get('skills', []))}")
    print(f"\n① 申请订阅 {args.duration}h……")
    resp = client.subscribe_to_peer(peer, args.duration, tx_hash=args.tx_hash)
    if not resp.get("ok"):
        print(f"❌ 订阅失败: {resp.get('error')}")
        if resp.get("receiver"):
            print(f"   真实链模式：请向 {resp['receiver']} 转账 {resp.get('amount_usdt')} USDT 后，"
                  f"用 --tx-hash 提供交易哈希重试")
        sys.exit(1)
    print(f"✅ 订阅成功！")
    print(f"   订单      : {resp['order_id']}")
    print(f"   金额      : {resp['amount_usdt']} USDT"
          f"（{resp.get('price_per_hour')} USDT/h × {args.duration}h）")
    print(f"   到期      : {time.strftime('%m-%d %H:%M', time.localtime(resp['expires_at']))}")
    print(f"   token 验签: ✅ 通过（签发者 == {peer[:14]}…，未过期）")
    # 持久化 token，供 invoke 复用
    os.makedirs(os.path.dirname(_sub_token_path(peer)), exist_ok=True)
    with open(_sub_token_path(peer), "w") as f:
        json.dump({"peer": peer, "token": resp["token"],
                   "expires_at": resp["expires_at"],
                   "order_id": resp["order_id"],
                   "amount_usdt": resp["amount_usdt"]}, f, ensure_ascii=False, indent=2)
    os.chmod(_sub_token_path(peer), 0o600)
    print(f"   token 已保存: {_sub_token_path(peer)}（供 invoke 调用）")


def cmd_invoke(args):
    """带订阅 token 调用服务方能力（需求=参数，产物=返回值）。"""
    client, _, _ = _client(args, args.wallet_key)
    peer = args.peer.lower()
    token = None
    if args.token_file:
        token = json.load(open(args.token_file)).get("token")
    else:
        path = _sub_token_path(peer)
        if os.path.exists(path):
            saved = json.load(open(path))
            token = saved.get("token")
            if saved.get("expires_at", 0) < int(time.time()):
                print(f"⚠ 订阅已到期（{time.strftime('%m-%d %H:%M', time.localtime(saved['expires_at']))}），"
                      f"请重新 subscribe")
                sys.exit(1)
    if not token:
        print(f"❌ 无订阅 token，请先: agent_cli.py subscribe --peer {peer} --duration 1")
        sys.exit(1)
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print("❌ --params 必须是 JSON 对象字符串，如 '{\"symbol\":\"BTC\"}'")
        sys.exit(1)
    print(f"📡 调用 {peer[:14]}… 能力 [{args.capability}]……")
    resp = client.invoke(peer, token, args.capability, params)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    if not resp.get("ok"):
        sys.exit(1)


# ---------------------------------------------------------------------------
# 自主报价：定价 / 自动调价
# ---------------------------------------------------------------------------

def _load_config(args) -> dict:
    config_path = args.config or os.path.expanduser("~/.agent-marketplace/agent.json")
    if not os.path.exists(config_path):
        print(f"❌ 未找到注册配置 {config_path}，请先 serve 注册")
        sys.exit(1)
    return json.load(open(config_path))


def cmd_pricing(args):
    """查看/估算/提交报价：cost=成本估算，market=行情，submit=提交。"""
    from agent_sdk.pricing import CostEstimator, PricingEngine
    config = _load_config(args)
    wallet, keys = _wallet_keys_from_config(args, config)
    client = HubClient(HUB_URL, wallet, keys)
    client.agent_token = config.get("agent_token")
    domain = args.domain or config.get("domain", "")

    # 行情
    market = None
    try:
        market = client.market_prices(domain=domain).get("market")
    except Exception as e:
        print(f"⚠ 行情不可达: {e}")
    if market:
        print(f"📊 市场行情 [{domain or '全部'}]:")
        print(f"   模式     : {'真实成交分布' if market.get('mode') == 'market' else '种子参考价(冷启动)'}")
        print(f"   报价数   : {market.get('count')}")
        print(f"   中位数   : {market.get('median')} USDC/h")
        print(f"   P25/P75  : {market.get('p25')} / {market.get('p75')} USDC/h")
        print(f"   区间     : {market.get('min')} ~ {market.get('max')} USDC/h")
        print(f"   参考锚   : {market.get('reference')} USDC/h")

    # 成本估算
    cost_est = CostEstimator(gpu=args.gpu, model=args.model,
                             tokens_per_hour=args.tokens_per_hour,
                             data_cost=args.data_cost, fixed_cost=args.fixed_cost,
                             hardware_cost=args.hardware_cost)
    breakdown = cost_est.breakdown()
    print(f"\n💰 成本估算 ({args.gpu}/{args.model}):")
    print(f"   硬件     : {breakdown['hardware']} USDC/h")
    print(f"   模型 API : {breakdown['model_api']} USDC/h")
    print(f"   数据     : {breakdown['data_cost']} USDC/h")
    print(f"   固定     : {breakdown['fixed_cost']} USDC/h")
    print(f"   ───────────────────────────")
    print(f"   cost_per_hour = {breakdown['cost_per_hour']} USDC/h")

    # 定价建议
    engine = PricingEngine(breakdown["cost_per_hour"],
                           profit_margin=args.margin, quality_premium=args.premium)
    detail = engine.suggest_with_market(market)
    print(f"\n💡 建议报价 (利润率 {args.margin:.0%}, 质量溢价 {args.premium:.0%}):")
    print(f"   成本加成价 : {detail['base_price']} USDC/h")
    if market and market.get("median"):
        print(f"   市场收敛   : median={market['median']} → 报价 {detail['suggested_price']} USDC/h")
    else:
        print(f"   报价(无行情) = {detail['suggested_price']} USDC/h（成本加成起步）")

    # 提交
    if args.submit:
        if not client.agent_token:
            print("❌ 无 agent_token，无法提交报价（请先 serve 注册）")
            sys.exit(1)
        resp = client.submit_pricing(cost_per_hour=detail["cost_per_hour"],
                                     price=detail["suggested_price"],
                                     profit_margin=detail["profit_margin"],
                                     quality_premium=detail["quality_premium"])
        if resp.get("ok"):
            print(f"\n✅ {resp.get('message')}")
        else:
            print(f"\n❌ 提交失败: {resp.get('error')}")
            sys.exit(1)


def cmd_pricer(args):
    """启动自动调价：后台循环拉行情→算价→提交，无需人工干预。"""
    from agent_sdk.pricing import CostEstimator, AutoPricer
    config = _load_config(args)
    wallet, keys = _wallet_keys_from_config(args, config)
    client = HubClient(HUB_URL, wallet, keys)
    client.agent_token = config.get("agent_token")
    if not client.agent_token:
        print("❌ 无 agent_token（请先 serve 注册）")
        sys.exit(1)
    cost_est = CostEstimator(gpu=args.gpu, model=args.model,
                             tokens_per_hour=args.tokens_per_hour,
                             data_cost=args.data_cost, fixed_cost=args.fixed_cost,
                             hardware_cost=args.hardware_cost)
    cost = cost_est.estimate()
    print(f"💰 成本估算: {cost} USDC/h ({args.gpu}/{args.model})")
    pricer = AutoPricer(client, cost_per_hour=cost,
                        profit_margin=args.margin, quality_premium=args.premium,
                        domain=args.domain or config.get("domain", ""),
                        subdomain=args.subdomain or config.get("subdomain", ""),
                        interval=args.interval)
    pricer.start(background=True)
    # 立即执行一次，然后驻留
    detail = pricer.tick()
    print(f"📊 首次调价: {detail['suggested_price']} USDC/h"
          f"（市场 median={detail.get('market_median')}, 提交={detail.get('submitted')}）")
    print("   自动调价循环运行中…… (Ctrl+C 停止)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pricer.stop()
        print("已停止")


# ---------------------------------------------------------------------------

def main():
    global HUB_URL
    p = argparse.ArgumentParser(description="Agent Marketplace CLI")
    p.add_argument("--hub", default=HUB_URL, help=f"Hub 地址（默认 {HUB_URL}，或环境变量 AGENT_HUB_URL）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", help="查看平台信息")
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser("init", help="初始化智能体端：生成/加载钱包身份（不注册、不启动服务，供 SDK 从 Hub 拉取后一键初始化）")
    s.add_argument("--config", help="身份持久化文件（默认 ~/.agent-marketplace/agent.json）")
    s.add_argument("--wallet-key", help="钱包私钥 hex（不传则生成新钱包并持久化）")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("register", help="注册到平台")
    s.add_argument("--endpoint", required=True, help="自己的接口地址，如 http://1.2.3.4:20102，或 auto（自动用公网 IP + --port）")
    s.add_argument("--port", type=int, default=20102, help="endpoint=auto 时使用的端口（默认 20102，见协议端口约定）")
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

    s = sub.add_parser("serve", help="启动智能体服务并注册（默认端口 20102，公网地址；重启时自动恢复无需重新注册）")
    s.add_argument("--port", type=int, default=20102)
    s.add_argument("--name", default="Agent")
    s.add_argument("--domain", help="一级领域（首次注册必需；重启恢复时从 config 读取）")
    s.add_argument("--subdomain", default="")
    s.add_argument("--skills", default="")
    s.add_argument("--wallet-key")
    s.add_argument("--config", help="注册配置持久化文件（默认 ~/.agent-marketplace/agent.json，重启自动恢复）")
    s.add_argument("--signer-url", help="签名服务地址（如 http://127.0.0.1:20101），Agent 不持私钥")
    s.add_argument("--signer-token", default=os.environ.get("AGENT_SIGNER_TOKEN", ""))
    s.add_argument("--endpoint", help="对外接口地址（默认自动公网 IP:port，AGENT_PUBLIC_IP 可指定）")
    s.add_argument("--price", type=float, default=None, help="服务报价（USDT/小时），不传则不报价（订阅接口不可用）")
    s.add_argument("--demo-invoke", action="store_true", help="启用演示能力 ping/echo（方便测试订阅-调用链路）")
    s.add_argument("--auto-price", action="store_true", help="启动自动调价（自主报价机制）")
    s.add_argument("--gpu", default="none", help="自动报价：硬件型号（h100/a100/a10/v100/l4/t4/cpu/none）")
    s.add_argument("--model", default="none", help="自动报价：模型（gpt-4o/.../local/none）")
    s.add_argument("--tokens-per-hour", type=int, default=0, help="自动报价：每小时 token 消耗")
    s.add_argument("--data-cost", type=float, default=0.0, help="自动报价：数据成本均摊 USDC/h")
    s.add_argument("--fixed-cost", type=float, default=0.0, help="自动报价：固定成本均摊 USDC/h")
    s.add_argument("--hardware-cost", type=float, default=None, help="自动报价：直接指定硬件成本（有云账单）")
    s.add_argument("--margin", type=float, default=0.3, help="自动报价：目标利润率（默认 30%%）")
    s.add_argument("--premium", type=float, default=0.0, help="自动报价：质量溢价")
    s.add_argument("--price-interval", type=float, default=600.0, help="自动报价：调价周期秒（默认 600）")
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

    s = sub.add_parser("subscribe", help="向服务方订阅（USDT 结算）：订单→支付→验证→签发token→验签")
    s.add_argument("--peer", required=True, help="服务方 agent_id（钱包地址）")
    s.add_argument("--duration", type=float, default=1.0, help="订阅时长（小时，默认 1）")
    s.add_argument("--tx-hash", help="真实链模式：USDT 转账交易哈希")
    s.add_argument("--wallet-key")
    s.set_defaults(fn=cmd_subscribe)

    s = sub.add_parser("invoke", help="带订阅 token 调用服务方能力（RPC：需求=参数，产物=返回值）")
    s.add_argument("--peer", required=True, help="服务方 agent_id")
    s.add_argument("--capability", required=True, help="能力名（见 /manifest）")
    s.add_argument("--params", default="", help="参数 JSON，如 '{\"symbol\":\"BTC\"}'")
    s.add_argument("--token-file", help="订阅 token 文件（默认 ~/.agent-marketplace/subscriptions/{peer}.json）")
    s.add_argument("--wallet-key")
    s.set_defaults(fn=cmd_invoke)

    s = sub.add_parser("balance", help="查看钱包余额（BSC 主网只读：BNB + USDT/BEP-20）")
    s.add_argument("--address", help="钱包地址（默认 agent.json 的 agent_id）")
    s.add_argument("--config", help="身份文件（默认 ~/.agent-marketplace/agent.json）")
    s.set_defaults(fn=cmd_balance)

    s = sub.add_parser("withdraw", help="转出钱包收益（BNB 或 USDT/BEP-20，EIP-155 签名广播）")
    s.add_argument("--to", required=True, help="收款地址 0x...")
    s.add_argument("--token", default="bnb", choices=["bnb", "usdt"], help="币种（默认 bnb）")
    s.add_argument("--amount", type=float, help="转出数量；bnb 可用 --all 转出全部（扣 gas）")
    s.add_argument("--all", action="store_true", help="转出全部 BNB")
    s.add_argument("--config", help="身份文件（默认 ~/.agent-marketplace/agent.json，私钥服务密钥自动解密）")
    s.set_defaults(fn=cmd_withdraw)

    s = sub.add_parser("signer", help="启动钱包签名服务（私钥隔离，Agent 不持私钥）")
    s.add_argument("--port", type=int, default=int(os.environ.get("AGENT_SIGNER_PORT", "20101")))
    s.add_argument("--key", default=os.environ.get("AGENT_WALLET_KEY", ""), help="钱包私钥 hex")
    s.add_argument("--config", help="从 agent.json 解密加载私钥（服务密钥自动解密）")
    s.add_argument("--token", default=os.environ.get("AGENT_SIGNER_TOKEN", ""), help="鉴权令牌")
    s.set_defaults(fn=cmd_signer)

    s = sub.add_parser("pricing", help="自主报价：成本估算 + 市场行情 + 定价建议（--submit 提交报价）")
    s.add_argument("--config", help="注册配置（默认 ~/.agent-marketplace/agent.json）")
    s.add_argument("--wallet-key")
    s.add_argument("--domain", help="行情领域（默认用注册配置的 domain）")
    s.add_argument("--gpu", default="none", help="硬件型号：h100/a100/a10/v100/l4/t4/cpu/none（默认 none=纯 API）")
    s.add_argument("--model", default="none", help="模型：gpt-4o/gpt-4o-mini/claude-sonnet/claude-haiku/llama-70b/qwen-72b/deepseek/local/none")
    s.add_argument("--tokens-per-hour", type=int, default=0, help="每小时 token 消耗量（估算 API 成本）")
    s.add_argument("--data-cost", type=float, default=0.0, help="数据/知识库成本均摊（USDC/h）")
    s.add_argument("--fixed-cost", type=float, default=0.0, help="固定成本均摊：人力/带宽（USDC/h）")
    s.add_argument("--hardware-cost", type=float, default=None, help="直接指定硬件成本（有云账单时，覆盖 --gpu）")
    s.add_argument("--margin", type=float, default=0.3, help="目标利润率（默认 0.3 = 30%%）")
    s.add_argument("--premium", type=float, default=0.0, help="质量溢价（默认 0，高信誉可加）")
    s.add_argument("--submit", action="store_true", help="把建议价提交到 Hub（token 鉴权）")
    s.set_defaults(fn=cmd_pricing)

    s = sub.add_parser("pricer", help="启动自动调价循环（后台：拉行情→算价→提交，无需人工干预）")
    s.add_argument("--config", help="注册配置（默认 ~/.agent-marketplace/agent.json）")
    s.add_argument("--wallet-key")
    s.add_argument("--domain")
    s.add_argument("--subdomain", default="")
    s.add_argument("--gpu", default="none")
    s.add_argument("--model", default="none")
    s.add_argument("--tokens-per-hour", type=int, default=0)
    s.add_argument("--data-cost", type=float, default=0.0)
    s.add_argument("--fixed-cost", type=float, default=0.0)
    s.add_argument("--hardware-cost", type=float, default=None)
    s.add_argument("--margin", type=float, default=0.3)
    s.add_argument("--premium", type=float, default=0.0)
    s.add_argument("--interval", type=float, default=600.0, help="调价周期（秒，默认 600 = 10 分钟）")
    s.set_defaults(fn=cmd_pricer)

    args = p.parse_args()
    HUB_URL = args.hub.rstrip("/")
    args.fn(args)


if __name__ == "__main__":
    main()
