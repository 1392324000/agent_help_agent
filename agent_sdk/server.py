"""
Agent SDK —— Agent 服务端框架
==============================
任何 Agent 接入平台只需：
  1. 创建 AgentServer(manifest_info, private_key=KeyPair, wallet=Wallet)
  2. 注册回调 on_private_message / on_group_message
  3. server.start()  然后调用 HubClient.register_flow() 完成注册

服务端自动实现协议要求的所有接口（与 skill 中"本地聊天接口通用协议"一致)：
  GET  /manifest                 返回注册信息
  POST /channel/private          接收单聊通道申请（完成加密握手)
  POST /channel/group            接收群聊通道申请（完成加密握手)
  POST /channel/message          接收加密消息（单聊密文 / group_key 分发 / 群聊密文)
  POST /channel/close            关闭通道
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .crypto import (KeyPair, Session, GroupSession, responder_session, b64d)
from .wallet import Wallet, recover_address_from_signature, parse_recoverable_signature
from . import protocol
from .subscription import (SubscriptionStore, create_sub_token, verify_sub_token)
from .security import (guard_outbound, collect_own_secrets, mark_inputs_auto,
                       mark_untrusted)

# 安全配置（环境变量可调)
MAX_BODY_BYTES = int(__import__("os").environ.get("AGENT_MAX_BODY_BYTES", "1048576"))   # 请求体上限 1MB
RATE_LIMIT_WINDOW = int(__import__("os").environ.get("AGENT_RATE_WINDOW", "10"))         # 窗口 10 秒
RATE_LIMIT_MAX = int(__import__("os").environ.get("AGENT_RATE_MAX", "60"))              # 窗口内最多 60 次请求


class _RateLimiter:
    """per-IP 简单滑动窗口限流，防公网暴力刷接口。"""

    def __init__(self, window: int, max_hits: int):
        self.window = window
        self.max_hits = max_hits
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits[ip] if now - t < self.window]
            if len(hits) >= self.max_hits:
                self._hits[ip] = hits
                return False
            hits.append(now)
            self._hits[ip] = hits
            return True


_handshake_auth_ok = None  # 延迟绑定（见文件底部定义，避免循环)


class AgentServer:
    def __init__(self, wallet: Wallet, keys: KeyPair,
                 domain: str, subdomain: str = "", skills: list[str] | None = None,
                 port: int = 0, host: str = "0.0.0.0",
                 name: str = "", extra_manifest: dict | None = None,
                 hub_url: str | None = None, require_registered: bool | None = None,
                 price_usdt_per_hour: float | None = None, verifier=None):
        self.wallet = wallet
        self.keys = keys
        self.agent_id = wallet.address
        self.name = name or wallet.address[:10]
        self.domain = domain
        self.subdomain = subdomain
        self.skills = skills or []
        self.extra_manifest = extra_manifest or {}
        self.port = port
        self.host = host
        # 对外声明地址：注册时设置（本机=127.0.0.1:port，公网=公网IP:port)
        self.advertised_endpoint: str | None = None
        # 安全：限流 + 可选"仅接受已注册 Agent"
        self.hub_url = (hub_url or __import__("os").environ.get("AGENT_HUB_URL", "")).rstrip("/")
        self.require_registered = (
            __import__("os").environ.get("AGENT_REQUIRE_REGISTERED", "0") == "1"
            if require_registered is None else require_registered
        )
        self._verifier = verifier  # 链验证器（可注入；None 则懒加载)
        self._rate = _RateLimiter(RATE_LIMIT_WINDOW, RATE_LIMIT_MAX)
        self._registered_cache: dict[str, bool] = {}
        self._registered_ts: float = 0

        self._sessions: dict[str, Session] = {}        # session_id -> 单聊会话（含成员↔群服务会话)
        self._group_sessions: dict[str, dict[str, Session]] = {}  # 群服务侧：group_id -> {member_id: Session}
        self._group_endpoints: dict[str, dict[str, str]] = {}     # 群服务侧：group_id -> {member_id: endpoint}
        self._groups: dict[str, GroupSession] = {}     # (兼容保留) 旧轮播群密钥会话
        self._group_meta: dict[str, dict] = {}         # session_id -> {owner, members, topic}
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._heartbeat_thread: threading.Thread | None = None

        # ---- 订阅支付（Agent 间 USDT 结算)：订单状态机 + 报价 ----
        self._subscriptions = SubscriptionStore()
        self.price_usdt_per_hour = max(0.0, float(
            price_usdt_per_hour if price_usdt_per_hour is not None
            else __import__("os").environ.get("AGENT_PRICE_USDT", "0")))

        # ---- security boundary: 出站防护（自身凭据恒拦截，通用模式按 security_mode) ----
        self.security_mode = (__import__("os").environ
                              .get("AGENT_SECURITY_MODE", "redact").strip().lower())
        self._known_secrets = collect_own_secrets(wallet, keys)
        # 入站自动打标：外部输入自动包 [UNTRUSTED_INPUT] 标记（防注入诱导，默认开)
        self.mark_inputs = (__import__("os").environ
                            .get("AGENT_MARK_INPUTS", "1") != "0")

        # 业务回调（由 Agent 自定义)
        self.on_private_message = None  # fn(sender_id, session, payload)
        self.on_group_message = None    # fn(sender_id, group, payload)
        self.on_channel_request = None  # fn(sender_id, session_id, purpose) -> bool 是否接受
        self.on_invoke = None           # fn(subscriber_id, capability, params) -> dict|None（订阅调用)
        self.caps: dict[str, dict] = {} # 能力签名（黑盒契约)：{cap: {"desc","params","returns"}}

    # ------------------------------------------------------------------
    # 安全：已注册校验（AGENT_REQUIRE_REGISTERED=1 时，仅接受平台注册过的 Agent 握手)
    # ------------------------------------------------------------------

    def _is_registered(self, agent_id: str) -> bool:
        if not self.hub_url:
            return True  # 未配置 Hub，无法校验（宽松)
        now = time.monotonic()
        if now - self._registered_ts > 30:  # 每 30 秒刷新一次注册表缓存
            try:
                import json as _json
                import urllib.request as _ur
                with _ur.urlopen(f"{self.hub_url}/api/v1/agents?limit=1000", timeout=8) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
                self._registered_cache = {a["agent_id"]: True for a in data.get("agents", [])}
                self._registered_ts = now
            except Exception:
                return True  # Hub 不可达时宽松放行（可用性优先)
        return self._registered_cache.get(agent_id, False)

    # ------------------------------------------------------------------
    # 订阅支付：USDT 链上验证（懒加载 ChainVerifier，AGENT_HUB_MOCK_CHAIN=1 走 mock)
    # ------------------------------------------------------------------

    def _get_verifier(self):
        if self._verifier is None:
            from .chain_verify import ChainVerifier
            import os as _os
            self._verifier = ChainVerifier(
                mock=_os.environ.get("AGENT_HUB_MOCK_CHAIN", "0") == "1")
        return self._verifier

    def _verify_usdt(self, tx_hash: str, subscriber: str, amount_usdt: float):
        """验证订阅方 USDT 到账：发起方=订阅方、收款=本服务、金额达标。"""
        return self._get_verifier().verify_usdt_transfer(
            tx_hash, subscriber, self.wallet.address, amount_usdt)

    def is_mock_chain(self) -> bool:
        return self._get_verifier().mock

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def public_url(self) -> str:
        """公网可达地址：host=0.0.0.0 时自动探测公网 IP（AGENT_PUBLIC_IP 可显式指定)。

        探测失败回退 127.0.0.1（此时需自行配置端口映射/反代)。
        """
        from . import net
        ip = net.public_ip()
        return f"http://{ip}:{self.port}" if ip else f"http://127.0.0.1:{self.port}"

    def start(self, background: bool = False) -> "AgentServer":
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        handler.server_owner = self
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()
        if background:
            print(f"[{self.name}] 服务已启动: http://{self.host}:{self.port}  (agent_id={self.agent_id[:12]}…)")
        return self

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    def start_heartbeat(self, client, interval: float = 900.0):
        """后台线程周期性向 Hub 心跳（默认每 15 分钟一次)，保持 active 状态。"""

        def _loop():
            while True:
                try:
                    client.heartbeat()
                except Exception:
                    pass
                time.sleep(interval)

        self._heartbeat_thread = threading.Thread(target=_loop, daemon=True)
        self._heartbeat_thread.start()
        return self

    # ------------------------------------------------------------------
    # 内部：消息处理
    # ------------------------------------------------------------------

    def register_client_session(self, session: Session) -> None:
        """把发起方在本侧建立的会话注册到本地服务，使对方回复可达。"""
        with self._lock:
            self._sessions[session.session_id] = session

    def register_group_service(self, group_id: str,
                               member_sessions: dict[str, Session],
                               member_endpoints: dict[str, str],
                               members: list[str], topic: str = "") -> None:
        """群主（群服务)注册一个群：持有与每个成员的独立会话与端点。

        成员之间不共享密钥、不直连——全部消息经群服务转码转发。
        """
        with self._lock:
            self._group_sessions[group_id] = dict(member_sessions)
            self._group_endpoints[group_id] = dict(member_endpoints)
            self._group_meta[group_id] = {
                "owner": self.agent_id, "members": members, "topic": topic,
            }

    def group_service_sessions(self, group_id: str) -> dict[str, Session]:
        with self._lock:
            return dict(self._group_sessions.get(group_id, {}))

    def send_group_as_service(self, group_id: str, sender: str,
                              payload: dict) -> list[tuple[str, dict]]:
        """群服务广播：把已验签的群消息 payload（含发送者签名)透传给各成员。

        payload 的 signature 原样保留——接收方验签确认发言者真实，
        群服务/群主也无法伪造成员发言（签名是成员钱包私钥生成的)。
        """
        import urllib.request as _ur
        import urllib.error as _uerr
        results = []
        with self._lock:
            sessions = dict(self._group_sessions.get(group_id, {}))
            endpoints = dict(self._group_endpoints.get(group_id, {}))
        for member_id, session in sessions.items():
            if member_id == sender:
                continue
            env = session.encrypt_payload(payload)
            # 平铺信封 + group_id（成员侧 _handle_envelope 按平铺字段解析)
            body = json.dumps({**env, "group_id": group_id}).encode("utf-8")
            req = _ur.Request(
                f"{endpoints.get(member_id, '').rstrip('/')}/channel/message",
                data=body, headers={"Content-Type": "application/json"},
            )
            try:
                with _ur.urlopen(req, timeout=8) as resp:
                    results.append((member_id, json.loads(resp.read().decode("utf-8"))))
            except Exception as e:
                results.append((member_id, {"ok": False, "error": str(e)}))
        return results

    def _handle_envelope(self, env: dict) -> dict:
        """处理收到的信封（密文消息)，自动路由：
        - 带 group_id 且本地持有群服务会话 -> 群消息（群服务转发而来)
        - 否则 -> 单聊
        """
        session_id = env.get("session_id", "")
        group_id = env.get("group_id")
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            payload = session.decrypt_envelope(env)
            # 🔒 入站自动打标：外部输入自动包 [UNTRUSTED_INPUT] 标记（验签之后，不影响签名校验)
            marked = payload
            if self.mark_inputs and isinstance(payload.get("content"), str):
                marked = dict(payload)
                marked["content"] = mark_untrusted(payload["content"])
            # 群服务转发的群消息：用与群服务的会话密钥解密，payload.sender 为原始发送者
            if group_id:
                # 端到端签名校验：确认发言者身份真实（群主也无法伪造成员)
                if not _verify_message_signature(payload.get("sender", ""), payload, group_id):
                    print(f"[{self.name}] ⚠ 群消息签名验证失败，已丢弃（来源 {payload.get('sender','?')[:12]}…)")
                    return {"ok": False, "error": "群消息签名验证失败"}
                if self.on_group_message:
                    self.on_group_message(payload.get("sender", ""), session, marked)
                return {"ok": True, "ack": True}
            if self.on_private_message:
                self.on_private_message(env.get("sender", ""), session, marked)
            return {"ok": True, "ack": True}

        return {"ok": False, "error": f"未知会话 {session_id}（未握手或会话已关闭)"}

    # ------------------------------------------------------------------

    def _make_handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AgentMarketplaceAgent/1.0"

            def log_message(self, fmt, *args):
                tag = owner.name
                print(f"[{tag} {time.strftime('%H:%M:%S')}] {fmt % args}")

            def _send(self, code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    if n > MAX_BODY_BYTES:
                        raise ValueError("body too large")
                    raw = self.rfile.read(n) if n else b"{}"
                    return json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    return {}

            def _rate_limited(self) -> bool:
                """per-IP 限流：超限返回 429。"""
                ip = self.client_address[0] if self.client_address else "?"
                return not owner._rate.allow(ip)

            def do_GET(self):
                if self._rate_limited():
                    return self._send(429, {"ok": False, "error": "rate limited"})
                if self.path.split("?")[0] == protocol.AGENT_ENDPOINTS["manifest"]:
                    manifest = {
                        "agent_id": owner.agent_id,
                        "name": owner.name,
                        "endpoint": f"http://{self.headers.get('Host', owner.host + ':' + str(owner.port))}",
                        "public_key": owner.keys.public_b64,
                        "capabilities": {
                            "domain": owner.domain,
                            "subdomain": owner.subdomain,
                            "skills": owner.skills,
                            "caps": owner.caps,   # 能力签名：{能力名: {desc, params, returns}}（黑盒契约)
                        },
                        "price_usdt_per_hour": owner.price_usdt_per_hour,  # 自主报价（USDT/小时)
                        "protocol": "agent-marketplace/v1",
                        **owner.extra_manifest,
                    }
                    return self._send(200, {"ok": True, "manifest": manifest})
                self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if self._rate_limited():
                    return self._send(429, {"ok": False, "error": "rate limited"})
                path = self.path.split("?")[0]
                body = self._body()

                # ---- 单聊通道申请（发起方 -> 我) ----
                if path == protocol.AGENT_ENDPOINTS["channel_private"]:
                    session_id = body.get("session_id", "")
                    sender = body.get("sender", "")
                    handshake = body.get("handshake", {})
                    if not session_id or not handshake:
                        return self._send(400, {"ok": False, "error": "missing session_id or handshake"})
                    # 双向认证①：验证发起方身份签名（恢复地址 == sender)，防未加密信道冒充
                    ok, msg = _handshake_auth_ok(sender, session_id, handshake)
                    if not ok:
                        return self._send(403, {"ok": False, "error": msg})
                    if owner.require_registered and not owner._is_registered(sender):
                        return self._send(403, {"ok": False, "error": f"{sender[:12]}… not registered on platform, handshake rejected"})
                    if owner.on_channel_request and not owner.on_channel_request(sender, session_id, body.get("purpose", "")):
                        return self._send(403, {"ok": False, "error": "peer rejected the session request"})
                    session = responder_session(session_id, owner.keys, handshake)
                    with owner._lock:
                        owner._sessions[session_id] = session
                    print(f"[{owner.name}] ✅ 接受单聊申请，会话 {session_id}（对方 {sender[:12]}…，签名已验证)")
                    return self._send(200, {"ok": True, "session_id": session_id,
                                            "message": "握手完成，通道已建立",
                                            "responder_signature": owner.wallet.sign_text(f"{session_id}:{sender}")})

                # ---- 群聊通道申请 ----
                if path == protocol.AGENT_ENDPOINTS["channel_group"]:
                    session_id = body.get("session_id", "")
                    owner_id = body.get("owner", "")
                    handshake = body.get("handshake", {})
                    if not session_id or not handshake:
                        return self._send(400, {"ok": False, "error": "missing session_id or handshake"})
                    # 双向认证①：验证发起方（群主)身份签名
                    ok, msg = _handshake_auth_ok(owner_id, session_id, handshake)
                    if not ok:
                        return self._send(403, {"ok": False, "error": msg})
                    if owner.require_registered and not owner._is_registered(owner_id):
                        return self._send(403, {"ok": False, "error": f"group owner {owner_id[:12]}… not registered on platform, join rejected"})
                    session = responder_session(session_id, owner.keys, handshake)
                    with owner._lock:
                        owner._sessions[session_id] = session
                        owner._group_meta[session_id] = {
                            "owner": owner_id,
                            "members": body.get("members", []),
                            "topic": body.get("topic", ""),
                            "service_endpoint": body.get("service_endpoint", ""),  # 群服务（群主)地址
                        }
                    print(f"[{owner.name}] 👥 收到群聊邀请 {session_id}（topic={body.get('topic','')}，群主签名已验证)，等待群消息…")
                    return self._send(200, {"ok": True, "session_id": session_id, "joined": "pending",
                                            "responder_signature": owner.wallet.sign_text(f"{session_id}:{owner_id}")})

                # ---- 群服务：接收成员群消息，验签后转码转发给其他成员 ----
                if path == "/channel/group/message":
                    group_id = body.get("group_id", "")
                    sender = body.get("sender", "")
                    env = body.get("envelope") or {}
                    with owner._lock:
                        sessions = dict(owner._group_sessions.get(group_id, {}))
                        meta = dict(owner._group_meta.get(group_id, {}))
                    member_session = sessions.get(sender)
                    if not member_session:
                        return self._send(403, {"ok": False, "error": f"not a group member or group not found ({group_id})"})
                    try:
                        payload = member_session.decrypt_envelope(env)
                    except Exception:
                        return self._send(400, {"ok": False, "error": "message decryption failed (session key mismatch/forged)"})
                    # 端到端签名校验：签名恢复地址必须 == 声称的发送者（防群内冒充)
                    if not _verify_message_signature(sender, payload, group_id):
                        return self._send(403, {"ok": False, "error": f"group message signature invalid ({sender[:12]}… cannot prove sender identity)"})
                    print(f"[{owner.name}] 👥 群「{meta.get('topic','')}」收到 {sender[:10]}… : {payload.get('content','')!r}，验签通过，转码转发")
                    results = owner.send_group_as_service(group_id, sender, payload)
                    return self._send(200, {"ok": True, "ack": True, "forwarded": len(results)})

                # ---- 加密消息 / 群服务转发消息 ----
                if path == protocol.AGENT_ENDPOINTS["channel_message"]:
                    result = owner._handle_envelope(body)
                    code = 200 if result.get("ok") else 400
                    return self._send(code, result)

                # ---- 关闭通道 ----
                if path == protocol.AGENT_ENDPOINTS["channel_close"]:
                    session_id = body.get("session_id", "")
                    with owner._lock:
                        owner._sessions.pop(session_id, None)
                        owner._groups.pop(session_id, None)
                        owner._group_meta.pop(session_id, None)
                    print(f"[{owner.name}] 🔒 会话 {session_id} 已关闭")
                    return self._send(200, {"ok": True, "closed": session_id})

                # ---- 订阅支付①：申请订阅（服务方签发订单，金额=报价×时长) ----
                if path == protocol.AGENT_ENDPOINTS["subscribe"]:
                    subscriber = (body.get("subscriber") or "").strip().lower()
                    if not re.fullmatch(r"0x[0-9a-f]{40}", subscriber):
                        return self._send(400, {"ok": False, "error": "subscriber must be a 0x+40-hex wallet address"})
                    try:
                        duration = float(body.get("duration_hours", 0))
                    except (TypeError, ValueError):
                        return self._send(400, {"ok": False, "error": "invalid duration_hours"})
                    from .protocol import MIN_SUBSCRIBE_HOURS
                    if duration < MIN_SUBSCRIBE_HOURS or duration > 720:
                        return self._send(400, {"ok": False,
                            "error": f"订阅时长需在 [{MIN_SUBSCRIBE_HOURS},720] 小时（最小一刻钟，最长 30 天)"})
                    if owner.price_usdt_per_hour <= 0:
                        return self._send(400, {"ok": False, "error": "service has no price yet (price_usdt_per_hour not set)"})
                    order = owner._subscriptions.create(
                        subscriber, duration, owner.price_usdt_per_hour, owner.wallet.address)
                    print(f"[{owner.name}] 📄 订阅订单 {order['order_id']} 已签发"
                          f"（{subscriber[:12]}…，{duration}h × {order['price_per_hour']} USDT/h = {order['amount_usdt']} USDT)")
                    return self._send(201, {
                        "ok": True, "order_id": order["order_id"], "status": "pending",
                        "amount_usdt": order["amount_usdt"], "receiver": order["receiver"],
                        "valid_hours": order["duration_hours"],
                        "price_per_hour": order["price_per_hour"],
                        "chain": "mock" if owner.is_mock_chain() else "bsc-mainnet",
                    })

                # ---- 订阅支付：mock 模拟 USDT 转账（仅演示模式) ----
                if path == "/subscribe/mock":
                    if not owner.is_mock_chain():
                        return self._send(403, {"ok": False, "error": "real BSC mode, mock transfer not allowed"})
                    order = owner._subscriptions.get(body.get("order_id", ""))
                    if not order:
                        return self._send(404, {"ok": False, "error": "order not found"})
                    tx = "0x" + secrets.token_hex(32)
                    owner._get_verifier().mock_record_usdt(
                        tx, order["subscriber"], owner.wallet.address, order["amount_usdt"])
                    return self._send(200, {"ok": True, "tx_hash": tx,
                                            "note": "mock 模式模拟 USDT 转账（演示，非真实链)"})

                # ---- 订阅支付②：提交支付结果（B 验证 USDT 到账) ----
                if path == protocol.AGENT_ENDPOINTS["subscribe_payment"]:
                    order_id = body.get("order_id", "")
                    tx_hash = (body.get("tx_hash") or "").strip()
                    order = owner._subscriptions.get(order_id)
                    if not order:
                        return self._send(404, {"ok": False, "error": "order not found"})
                    if owner._subscriptions.is_expired(order_id):
                        return self._send(400, {"ok": False, "error": "order expired, please re-apply subscription"})
                    if order["status"] != "pending":
                        return self._send(400, {"ok": False, "error": f"order status is {order['status']}, only pending can submit payment"})
                    if not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash):
                        return self._send(400, {"ok": False, "error": "tx_hash must be 0x+64-hex"})
                    ok, msg = owner._verify_usdt(tx_hash, order["subscriber"], order["amount_usdt"])
                    if not ok:
                        return self._send(400, {"ok": False, "error": f"USDT verification failed: {msg}"})
                    owner._subscriptions.mark_paid(order_id, tx_hash)
                    print(f"[{owner.name}] 💰 订单 {order_id} 已支付"
                          f"（{order['amount_usdt']} USDT，{msg})")
                    return self._send(200, {"ok": True, "status": "paid",
                                            "message": "USDT 已到账，可确认签发 token"})

                # ---- 订阅支付③：确认 -> 签发签名订阅 token ----
                if path == protocol.AGENT_ENDPOINTS["subscribe_confirm"]:
                    order_id = body.get("order_id", "")
                    order = owner._subscriptions.get(order_id)
                    if not order:
                        return self._send(404, {"ok": False, "error": "order not found"})
                    if order["status"] != "paid":
                        return self._send(400, {"ok": False, "error": f"order status is {order['status']}, pay and verify first"})
                    token = create_sub_token(owner.wallet, order["subscriber"],
                                             order["duration_hours"], order_id)
                    owner._subscriptions.mark_completed(order_id, token)
                    # 复购接续：会话保持窗口内复购 → 直接接上之前的会话
                    ws = owner._subscriptions.get_workspace(order["subscriber"])
                    resumed = bool(ws) and ws.get("order_id") != order_id
                    if resumed:
                        print(f"[{owner.name}] 🔗 客户 {order['subscriber'][:12]}… 复购，"
                              f"已接续之前的会话（保持至 "
                              f"{time.strftime('%m-%d %H:%M', time.localtime(ws['keep_until']))})")
                    print(f"[{owner.name}] 🔑 已签发订阅 token"
                          f"（{order['subscriber'][:12]}…，{order['duration_hours']}h，至 "
                          f"{time.strftime('%m-%d %H:%M', time.localtime(token['payload']['exp']))})")
                    resp = {"ok": True, "token": token,
                            "expires_at": token["payload"]["exp"],
                            "order_id": order_id,
                            "amount_usdt": order["amount_usdt"],
                            "keep_seconds": owner._subscriptions.grace_seconds()}
                    if resumed:
                        resp["resumed"] = True
                        resp["workspace"] = ws  # 未过期的会话（含上次工作上下文)
                    return self._send(200, resp)

                # ---- 调用能力：验签 token → 校验调用者==token绑定的客户钱包 ----
                #     token 过期 → 返回过期错误提示并自动断开（需重新订阅续购)
                if path == protocol.AGENT_ENDPOINTS["invoke"]:
                    tok = body.get("token") or {}
                    tp = tok.get("payload") or {}
                    exp = tp.get("exp")
                    if isinstance(exp, (int, float)) and int(exp) <= int(time.time()):
                        # 🔌 到期未续购换 token：专家端验证过期 → 提示 + 自动断开
                        sub = str(tp.get("sub") or "").lower()
                        ev = owner._subscriptions.disconnect(sub, str(tp.get("oid") or ""), int(exp))
                        keep = owner._subscriptions.grace_seconds() // 60
                        print(f"[{owner.name}] 🔌 连接已自动断开：客户 {sub[:12]}… 订阅"
                              f"于 {time.strftime('%m-%d %H:%M', time.localtime(int(exp)))} 过期未续购"
                              f"（会话保持 {keep} 分钟，复购可直接接续)")
                        return self._send(403, {"ok": False,
                                                "error": f"订阅已过期（{time.strftime('%m-%d %H:%M:%S', time.localtime(int(exp)))})，"
                                                         f"连接已自动断开；会话将保持 {keep} 分钟，"
                                                         f"请及时复购重连（复购可直接接续之前的会话，最小一刻钟)",
                                                "disconnected": True, "expired_at": int(exp),
                                                "keep_seconds": owner._subscriptions.grace_seconds()})
                    payload = verify_sub_token(tok, owner.wallet.address)
                    if not payload:
                        return self._send(403, {"ok": False,
                                                "error": "token 无效（签名校验失败或结构非法，请重新订阅)"})
                    capability = (body.get("capability") or "").strip()
                    params = body.get("params") or {}
                    # 🔒 token 绑定客户钱包：调用者地址必须 == token.sub，且请求签名
                    #    恢复地址 == token.sub（防 token 复制/转发被第三方冒用)
                    subscriber = (body.get("subscriber") or "").strip().lower()
                    if not re.fullmatch(r"0x[0-9a-f]{40}", subscriber):
                        return self._send(400, {"ok": False,
                                                "error": "subscriber must be a 0x+40-hex wallet address"})
                    if subscriber != payload["sub"]:
                        return self._send(403, {"ok": False,
                                                "error": f"调用者 {subscriber[:12]}… 不是该 token 绑定的客户 "
                                                         f"{payload['sub'][:12]}…（token 已绑定钱包地址)"})
                    canon_params = json.dumps(params, sort_keys=True, separators=(",", ":"))
                    message = f"invoke:{payload['oid']}:{capability}:{canon_params}"
                    sig_hex, rec_id = parse_recoverable_signature(body.get("signature") or "")
                    recovered = recover_address_from_signature(sig_hex, message.encode("utf-8"), rec_id)
                    if not recovered or recovered.lower() != payload["sub"]:
                        return self._send(403, {"ok": False,
                                                "error": f"invoke 签名无效（恢复出 {recovered or '?'}，"
                                                         f"不是 token 绑定的客户 {payload['sub'][:12]}…)"})
                    if not capability:
                        return self._send(400, {"ok": False, "error": "missing capability"})
                    if owner.caps and capability not in owner.caps:
                        return self._send(404, {"ok": False,
                            "error": f"unknown capability {capability}（可用: {', '.join(owner.caps)})",
                            "caps": list(owner.caps)})
                    if not owner.on_invoke:
                        return self._send(501, {"ok": False, "error": "seller has no on_invoke callback"})
                    # 🔒 入站自动打标：外部参数自动包 [UNTRUSTED_INPUT]（防注入诱导)
                    if owner.mark_inputs:
                        params = mark_inputs_auto(params)
                    try:
                        result = owner.on_invoke(payload["sub"], capability, params)
                    except Exception as e:
                        return self._send(500, {"ok": False, "error": f"capability execution failed: {e}"})
                    if result is None:
                        return self._send(404, {"ok": False, "error": f"unknown capability {capability}"})
                    # 🔒 security boundary: 出站防护（自身凭据恒拦截；通用敏感模式按 security_mode 脱敏/拒绝)
                    guard = guard_outbound(result, owner._known_secrets, owner.security_mode)
                    if guard.blocked:
                        print(f"[{owner.name}] ⛔ 安全边界拦截 invoke 响应"
                              f"（{guard.reason})")
                        return self._send(406, {"ok": False, "error": f"security boundary: {guard.reason}",
                                                "guard": [t for t, _ in guard.hits]})
                    # 📝 记录工作上下文（复购接续：断开后保持窗口内复购可直接接上)
                    try:
                        import json as _json
                        owner._subscriptions.touch_workspace(
                            payload["sub"], payload["oid"], {
                                "capability": capability,
                                "params": params,
                                "result": _json.dumps(guard.payload, ensure_ascii=False)[:2000],
                            })
                    except Exception:
                        pass
                    return self._send(200, {"ok": True, "result": guard.payload,
                                            "subscriber": payload["sub"],
                                            "expires_at": payload["exp"],
                                            "guard": [t for t, _ in guard.hits] or None})

                self._send(404, {"ok": False, "error": f"unknown endpoint {path}"})

            def do_OPTIONS(self):
                self._send(204, {})

        return Handler

    # ------------------------------------------------------------------

    @property
    def sessions(self) -> dict:
        return dict(self._sessions)

    @property
    def groups(self) -> dict:
        return dict(self._groups)

    def group_meta(self, session_id: str) -> dict:
        return dict(self._group_meta.get(session_id, {}))


def _handshake_auth_ok(sender: str, session_id: str, handshake: dict) -> tuple[bool, str]:
    """验证握手签名：发起方必须用钱包私钥对 session_id:ephemeral_pub 签名，
    恢复地址 == 声明的 sender（agent_id)。防止未加密信道上冒充身份。"""
    sig = handshake.get("signature") or ""
    ep = handshake.get("ephemeral_pub") or ""
    if not sig:
        return False, "握手缺少签名（拒绝未认证会话)"
    recovered = recover_address_from_signature(sig, f"{session_id}:{ep}".encode("utf-8"))
    if not recovered or recovered != sender.lower():
        return False, f"握手签名验证失败：恢复出 {recovered}，不是声明的 {sender[:12]}…"
    return True, "ok"


def _verify_message_signature(sender: str, payload: dict, group_id: str = "") -> bool:
    """群消息端到端验签：payload.signature 是 sender 钱包对 f"grp:{group_id}:{content}:{ts}"
    的签名，恢复地址必须 == sender。绑定群上下文防跨群重放，
    群服务/群主也无法伪造（签名由成员钱包私钥生成)。"""
    sig = payload.get("signature") or ""
    content = payload.get("content") or ""
    ts = payload.get("ts") or 0
    if not sig:
        return False
    recovered = recover_address_from_signature(sig, f"grp:{group_id}:{content}:{ts}".encode("utf-8"))
    return bool(recovered) and recovered == sender.lower()
