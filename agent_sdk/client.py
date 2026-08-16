"""
Agent SDK —— Hub 客户端
========================
封装：创建订单 -> 转账 -> 注册、领域搜索、心跳，以及发起加密单聊/群聊
（加密逻辑在 crypto.py，此处负责 HTTP 传输与握手编排）。
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.request
import urllib.error

from .crypto import (KeyPair, Session, GroupSession, make_handshake,
                     initiator_session, random_id)
from .wallet import Wallet
from . import protocol
from .protocol import HUB_API_PREFIX

USER_AGENT = "agent-marketplace-sdk/1.0"


class HubError(Exception):
    pass


def _post(url: str, payload: dict, timeout: float = 15) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise HubError(f"HTTP {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise HubError(f"无法连接 {url}: {e.reason}") from e


def _get(url: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise HubError(f"HTTP {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise HubError(f"无法连接 {url}: {e.reason}") from e


class HubClient:
    """连接 Hub 的客户端。每个 Agent 一个实例。"""

    def __init__(self, hub_url: str, wallet: Wallet, keys: KeyPair,
                 local_server=None):
        self.hub_url = hub_url.rstrip("/")
        self.wallet = wallet
        self.keys = keys
        self.agent_id = wallet.address
        self.local_server = local_server  # 发起会话后同步注册，使对方回复可达
        self.agent_token: str | None = None  # 注册成功后 Hub 签发的凭证（保活/续费/刷新）
        self._peer_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 注册流程（Hub 签发订单 -> 支付 -> 提交结果 -> 确认 -> 完成）
    # ------------------------------------------------------------------

    def info(self) -> dict:
        return _get(f"{self.hub_url}{protocol.HUB_ENDPOINTS['info']}")

    def apply_registration(self, endpoint: str, domain: str, subdomain: str = "",
                           skills: list[str] | None = None,
                           description: str = "", model: str = "",
                           knowledge_base: str = "", workflows: str = "",
                           caps: dict | None = None) -> dict:
        """Step 1：申请注册。Hub 验证身份签名后**签发支付订单**（status=pending）。

        返回订单号、平台钱包、要求金额。签名内容为 f"{wallet}:{endpoint}"，
        证明钱包是你的。

        注册画像（供 B 在 Hub 关键词搜索定位）：
          description  服务/工作流一句话描述
          model        模型配置（如 "deepseek-v4-flash 在线API" / "T4+local 本地推理"）
          knowledge_base 知识库配置（如 "本地财报库 20G"）
          workflows    处理的工作流（如 "财报分析→风险评估"）
          caps         能力签名 {cap: {desc, params, returns}}
        """
        skills = skills or []
        signature = self.wallet.sign_text(f"{self.agent_id}:{endpoint}")  # 65 字节 r||s||v
        return _post(f"{self.hub_url}{protocol.HUB_ENDPOINTS['applications']}", {
            "wallet": self.agent_id,
            "endpoint": endpoint,
            "domain": domain,
            "subdomain": subdomain,
            "skills": skills,
            "public_key": self.keys.public_b64,
            "signature": signature,
            "description": description,
            "model": model,
            "knowledge_base": knowledge_base,
            "workflows": workflows,
            "caps": caps or {},
        })

    def mock_transfer(self, tx_hash: str, from_addr: str | None = None,
                      amount_wei: int | None = None) -> dict:
        """Mock 模式专用：模拟向平台钱包转账（开发/演示）。"""
        body = {"tx_hash": tx_hash, "from": from_addr or self.agent_id}
        if amount_wei is not None:
            body["amount_wei"] = amount_wei
        return _post(f"{self.hub_url}/api/v1/mock/transfer", body)

    def submit_payment(self, order_id: str, tx_hash: str) -> dict:
        """Step 2：提交支付结果（tx_hash），订单 pending -> paid。"""
        return _post(f"{self.hub_url}{protocol.HUB_ENDPOINTS['order_payment'].format(order_id=order_id)}",
                     {"tx_hash": tx_hash})

    def confirm_order(self, order_id: str) -> dict:
        """Step 3：Hub 链上确认支付结果，订单 paid -> completed，生成注册。
        注册成功时响应含 agent_token（保活/续费/刷新凭证），自动保存。"""
        resp = _post(f"{self.hub_url}{protocol.HUB_ENDPOINTS['order_confirm'].format(order_id=order_id)}", {})
        if resp.get("ok") and resp.get("agent_token"):
            self.agent_token = resp["agent_token"]
        return resp

    def order_status(self, order_id: str) -> dict:
        """查询订单状态。"""
        return _get(f"{self.hub_url}{protocol.HUB_ENDPOINTS['order_status'].format(order_id=order_id)}")

    def auto_endpoint(self, port: int) -> str:
        """生成公网接口地址：http://<公网IP>:<port>（AGENT_PUBLIC_IP 可显式指定）。"""
        from . import net
        return net.public_endpoint(port)

    def register_flow(self, endpoint: str, domain: str, subdomain: str = "",
                      skills: list[str] | None = None, tx_hash: str | None = None,
                      amount_wei: int | None = None, description: str = "",
                      model: str = "", knowledge_base: str = "", workflows: str = "",
                      caps: dict | None = None) -> dict:
        """一键注册：申请（Hub 签发订单）-> 转账 -> 提交支付结果 -> 确认 -> 完成。

        返回 confirm 响应（status=completed 表示注册成功）。
        注册画像（description/model/knowledge_base/workflows/caps）供 B 搜索定位。
        """
        app = self.apply_registration(endpoint, domain, subdomain, skills,
                                      description=description, model=model,
                                      knowledge_base=knowledge_base,
                                      workflows=workflows, caps=caps)
        if not app.get("ok"):
            return app
        order_id = app["order_id"]
        tx = tx_hash or ("0x" + secrets.token_hex(32))  # 演示默认随机哈希；真实环境请填真实 tx_hash
        if app.get("chain_mode") == "mock":
            self.mock_transfer(tx, amount_wei=amount_wei)
        pay = self.submit_payment(order_id, tx)
        if not pay.get("ok"):
            return pay
        return self.confirm_order(order_id)

    # ------------------------------------------------------------------
    # 订阅续费（24h 有效期，提前续费从当前到期时间顺延）
    # ------------------------------------------------------------------

    def renew_subscription(self, tx_hash: str | None = None,
                           amount_wei: int | None = None) -> dict:
        """续费订阅：申请续费订单 -> 转账 -> 提交 -> 确认。
        优先用 agent_token 鉴权（注册凭证），无 token 时回退钱包签名。
        返回 confirm 响应（含 new_expires_at）。"""
        payload = {
            "wallet": self.agent_id,
            "agent_id": self.agent_id,
            "order_type": "renew",
        }
        if self.agent_token:
            payload["token"] = self.agent_token
        else:
            payload["signature"] = self.wallet.sign_text(f"renew:{self.agent_id}")
        app = _post(f"{self.hub_url}{protocol.HUB_ENDPOINTS['applications']}", payload)
        if not app.get("ok"):
            return app
        tx = tx_hash or ("0x" + secrets.token_hex(32))
        if app.get("chain_mode") == "mock":
            self.mock_transfer(tx, amount_wei=amount_wei)
        pay = self.submit_payment(app["order_id"], tx)
        if not pay.get("ok"):
            return pay
        return self.confirm_order(app["order_id"])

    def refresh(self, endpoint: str) -> dict:
        """重启恢复：用 agent_token 刷新 endpoint 与保活时间（断连重启后 IP/端口可能变化）。"""
        if not self.agent_token:
            raise HubError("无 agent_token（尚未注册成功或未加载凭证）")
        return _post(f"{self.hub_url}/api/v1/agents/{self.agent_id}/refresh",
                     {"token": self.agent_token, "endpoint": endpoint})

    # ------------------------------------------------------------------
    # 发现
    # ------------------------------------------------------------------

    def search(self, domain: str | None = None, subdomain: str | None = None,
               skills: str | None = None, q: str | None = None,
               limit: int = 50) -> list[dict]:
        from urllib.parse import quote
        resp = _get(f"{self.hub_url}{protocol.HUB_ENDPOINTS['agents']}"
                    f"?limit={limit}"
                    + (f"&domain={quote(domain)}" if domain else "")
                    + (f"&subdomain={quote(subdomain)}" if subdomain else "")
                    + (f"&skills={quote(skills)}" if skills else "")
                    + (f"&q={quote(q)}" if q else ""))
        if not resp.get("ok"):
            raise HubError(resp.get("error", "搜索失败"))
        return resp["agents"]

    def find_peers(self, domain: str | None = None, skills: str | None = None,
                   q: str | None = None) -> list[dict]:
        """找到"其他"专业 Agent（排除自己）。"""
        return [a for a in self.search(domain=domain, skills=skills, q=q)
                if a["agent_id"] != self.agent_id]

    def get_agent(self, agent_id: str) -> dict:
        if agent_id in self._peer_cache:
            return self._peer_cache[agent_id]
        resp = _get(f"{self.hub_url}{protocol.HUB_ENDPOINTS['agents']}/{agent_id}")
        if not resp.get("ok"):
            raise HubError(resp.get("error", "Agent 不存在"))
        self._peer_cache[agent_id] = resp["agent"]
        return resp["agent"]

    def heartbeat(self) -> dict:
        """保活：带 agent_token 鉴权（防冒名保活）。"""
        body = {"agent_id": self.agent_id}
        if self.agent_token:
            body["token"] = self.agent_token
        return _post(f"{self.hub_url}{protocol.HUB_ENDPOINTS['heartbeat']}", body)

    # ------------------------------------------------------------------
    # 自主报价：市场行情 / 提交报价
    # ------------------------------------------------------------------

    def market_prices(self, domain: str | None = None,
                      subdomain: str | None = None) -> dict:
        """拉取市场行情：该领域在线 Agent 的报价分布（median/p25/p75），
        不足 3 个报价时返回种子参考价（冷启动锚点）。"""
        qs = []
        if domain:
            qs.append(f"domain={domain}")
        if subdomain:
            qs.append(f"subdomain={subdomain}")
        url = f"{self.hub_url}{HUB_API_PREFIX}/market/prices"
        if qs:
            url += "?" + "&".join(qs)
        return _get(url)

    def submit_pricing(self, cost_per_hour: float, price: float,
                       profit_margin: float = 0.0,
                       quality_premium: float = 0.0) -> dict:
        """提交/更新报价（token 鉴权）。Hub 校验价格不低于成本下限。"""
        if not self.agent_token:
            raise HubError("无 agent_token（尚未注册成功或未加载凭证）")
        return _post(f"{self.hub_url}{HUB_API_PREFIX}/agents/{self.agent_id}/pricing", {
            "token": self.agent_token,
            "cost_per_hour": cost_per_hour,
            "price": price,
            "profit_margin": profit_margin,
            "quality_premium": quality_premium,
        })

    # ------------------------------------------------------------------
    # 订阅支付（Agent 间 USDT 结算）：订阅 -> 调用
    # ------------------------------------------------------------------

    def get_peer_manifest(self, peer_agent_id: str) -> dict:
        """拉取对方 /manifest（价格、能力、公钥）。"""
        peer = self.get_agent(peer_agent_id)
        resp = _get(f"{peer['endpoint'].rstrip('/')}{protocol.AGENT_ENDPOINTS['manifest']}")
        if not resp.get("ok"):
            raise HubError(resp.get("error", "/manifest 获取失败"))
        return resp["manifest"]

    def subscribe_to_peer(self, peer_agent_id: str, duration_hours: float,
                          tx_hash: str | None = None, verify_token: bool = True) -> dict:
        """向服务方订阅：申请订单 → 转账(mock/真实) → 提交 tx_hash → 确认签发 token。

        返回 {"token": {...}, "expires_at": int, "amount_usdt": float,
              "order_id": str, "price_per_hour": float}。
        """
        from .subscription import verify_sub_token
        peer = self.get_agent(peer_agent_id)
        endpoint = peer["endpoint"].rstrip("/")

        # ① 申请订阅
        sub = _post(f"{endpoint}{protocol.AGENT_ENDPOINTS['subscribe']}", {
            "subscriber": self.agent_id,
            "duration_hours": duration_hours,
        })
        if not sub.get("ok"):
            return sub
        order_id = sub["order_id"]
        amount = sub["amount_usdt"]

        # ② 支付：mock 模式模拟转账；真实模式需要调用方自行转账后提供 tx_hash
        if not tx_hash:
            if sub.get("chain") == "mock":
                m = _post(f"{endpoint}/subscribe/mock", {"order_id": order_id})
                if not m.get("ok"):
                    return m
                tx_hash = m["tx_hash"]
            else:
                return {"ok": False, "error": "真实链模式：请先向服务方转账 USDT 后提供 --tx-hash",
                        "order_id": order_id, "amount_usdt": amount,
                        "receiver": sub.get("receiver")}

        # ③ 提交支付结果
        pay = _post(f"{endpoint}{protocol.AGENT_ENDPOINTS['subscribe_payment']}", {
            "order_id": order_id, "tx_hash": tx_hash,
        })
        if not pay.get("ok"):
            return pay

        # ④ 确认签发 token
        conf = _post(f"{endpoint}{protocol.AGENT_ENDPOINTS['subscribe_confirm']}",
                     {"order_id": order_id})
        if not conf.get("ok"):
            return conf
        token = conf["token"]

        # ⑤ 验签：恢复地址必须 == 服务方（防伪造 token）
        if verify_token:
            payload = verify_sub_token(token, peer_agent_id)
            if not payload:
                return {"ok": False, "error": "token 验签失败：签发者不是声称的服务方或已过期"}

        return {"ok": True, "token": token, "expires_at": conf["expires_at"],
                "order_id": order_id, "amount_usdt": amount,
                "price_per_hour": sub.get("price_per_hour"),
                "chain": sub.get("chain"),
                "resumed": conf.get("resumed", False),
                "workspace": conf.get("workspace"),
                "keep_seconds": conf.get("keep_seconds")}

    def invoke(self, peer_agent_id: str, token: dict, capability: str,
               params: dict | None = None) -> dict:
        """带订阅 token 调用服务方能力（RPC 语义：需求=参数，产物=返回值）。

        请求携带**调用者钱包地址 + 对请求的 ECDSA 签名**：服务端验签 token 后
        恢复签名地址 == token.sub（绑定的客户钱包）才放行——token 无法被第三方
        复制冒用（绑定客户钱包地址，防漏洞）。
        """
        params = params or {}
        peer = self.get_agent(peer_agent_id)
        body = {
            "token": token, "capability": capability, "params": params,
            "subscriber": self.agent_id,
        }
        # 签名消息：invoke:{token的order_id}:{capability}:{规范化params}
        # （绑定具体订阅 + 防篡改 + 防跨订阅重放）
        canon_params = json.dumps(params, sort_keys=True, separators=(",", ":"))
        message = f"invoke:{token['payload']['oid']}:{capability}:{canon_params}"
        body["signature"] = self.wallet.sign_text(message)
        return _post(f"{peer['endpoint'].rstrip('/')}{protocol.AGENT_ENDPOINTS['invoke']}", body)

    def report_deal(self, order_id: str, buyer: str, amount_usdt: float,
                    duration_hours: float, tx_hash: str = "") -> dict:
        """服务方签名汇报成交给 Hub（行情数据源）。

        签名消息统一格式：deal:{order_id}:{buyer}:{amount}:{duration}
        （数字用 'g' 格式去尾零，避免 1 vs 1.0 不匹配）。
        """
        amount_s = format(float(amount_usdt), 'g')
        duration_s = format(float(duration_hours), 'g')
        message = f"deal:{order_id}:{buyer.lower()}:{amount_s}:{duration_s}"
        signature = self.wallet.sign_text(message)
        return _post(f"{self.hub_url}/api/v1/deals", {
            "order_id": order_id, "buyer": buyer.lower(), "seller": self.agent_id,
            "amount_usdt": amount_usdt, "duration_hours": duration_hours,
            "tx_hash": tx_hash, "signature": signature,
        })

    # ------------------------------------------------------------------
    # 服务评价：客户对专家服务能力打分（hub 推荐/客户选择的依据之一）
    # ------------------------------------------------------------------

    def submit_rating(self, order_id: str, seller: str, scores: dict,
                      comment: str = "") -> dict:
        """服务完成后客户对专家打分（3-5 维，1-5 分）。

        签名消息：rate:{order_id}:{seller}:{规范化scores}（buyer 钱包签名，防伪造），
        Hub 校验订单已成交且买家/卖家匹配才接受——没消费不能乱打分。
        """
        canon = json.dumps(scores, sort_keys=True, separators=(",", ":"))
        message = f"rate:{order_id}:{seller.lower()}:{canon}"
        return _post(f"{self.hub_url}{HUB_API_PREFIX}/ratings", {
            "order_id": order_id, "buyer": self.agent_id, "seller": seller.lower(),
            "scores": scores, "comment": comment,
            "signature": self.wallet.sign_text(message),
        })

    # ------------------------------------------------------------------
    # 发起加密单聊
    # ------------------------------------------------------------------

    def open_private(self, peer_agent_id: str, purpose: str = "") -> Session:
        """向对方申请单聊通道：握手后双方各自推导出同一会话密钥。

        双向认证：本钱包对握手签名（对方验证我方身份），并验证对方响应签名。
        """
        peer = self.get_agent(peer_agent_id)
        session_id = random_id("priv")
        handshake, ephemeral_priv = make_handshake(session_id, self.agent_id, self.keys)
        # 双向认证①：钱包对 session_id:ephemeral_pub 签名，对方据此验证我是真实持有该钱包
        handshake["signature"] = self.wallet.sign_text(f"{session_id}:{handshake['ephemeral_pub']}")
        resp = _post(f"{peer['endpoint'].rstrip('/')}{protocol.AGENT_ENDPOINTS['channel_private']}", {
            "session_id": session_id,
            "sender": self.agent_id,
            "handshake": handshake,
            "purpose": purpose,
        })
        if not resp.get("ok"):
            raise HubError(f"对方拒绝了单聊申请: {resp.get('error', 'unknown')}")
        # 双向认证②：验证对方响应签名（恢复地址 == 对方 agent_id），防 MITM 冒充响应方
        rsig = resp.get("responder_signature", "")
        from .wallet import recover_address_from_signature as _rec
        if not rsig or _rec(rsig, f"{session_id}:{self.agent_id}".encode("utf-8")) != peer_agent_id.lower():
            raise HubError(f"对方响应签名验证失败，可能被中间人冒充（{peer_agent_id[:12]}…）")
        session = initiator_session(session_id, self.keys, ephemeral_priv,
                                    peer["public_key"], peer_agent_id)
        if self.local_server is not None:
            self.local_server.register_client_session(session)
        return session

    def send_private(self, session: Session, text: str) -> dict:
        """发送加密消息到对方 /channel/message。

        🔒 安全边界：发送前对内容做脱敏（自身凭据恒拦截），防止被诱导后外泄。
        """
        from .security import guard_outbound, collect_own_secrets
        guard = guard_outbound(text, collect_own_secrets(self.wallet, self.keys))
        if guard.blocked:
            raise HubError(f"安全边界：{guard.reason}")
        if guard.hits:
            print(f"[send] ⚠ 消息包含敏感模式，已脱敏: {sorted({t for t, _ in guard.hits})}")
        peer = self.get_agent(session.peer)
        env = session.encrypt_text(guard.payload)
        return _post(f"{peer['endpoint'].rstrip('/')}{protocol.AGENT_ENDPOINTS['channel_message']}", env)

    # ------------------------------------------------------------------
    # 发起加密群聊（轮播模式）
    # ------------------------------------------------------------------

    def open_group(self, member_ids: list[str], topic: str = "") -> dict:
        """群主建群（中心化群服务模型）：本 Agent 即群服务。

        对每个成员独立握手（双向签名认证）建立 成员↔群主 会话；
        不分发共享密钥、成员之间不直连——消息统一经本群服务转码转发。
        返回群句柄 {"group_id", "owner", "members", "endpoints", "topic"}。
        """
        if self.local_server is None:
            raise HubError("群主必须有本地服务（local_server）作为群服务")
        group_id = random_id("grp")
        from .wallet import recover_address_from_signature as _rec
        member_sessions: dict[str, Session] = {}
        member_endpoints: dict[str, str] = {}
        for member_id in member_ids:
            peer = self.get_agent(member_id)
            handshake, ephemeral_priv = make_handshake(group_id, self.agent_id, self.keys)
            handshake["signature"] = self.wallet.sign_text(f"{group_id}:{handshake['ephemeral_pub']}")
            resp = _post(f"{peer['endpoint'].rstrip('/')}{protocol.AGENT_ENDPOINTS['channel_group']}", {
                "session_id": group_id,
                "owner": self.agent_id,
                "members": [self.agent_id] + member_ids,
                "handshake": handshake,
                "topic": topic,
                # 群服务地址 = 群主对外声明地址（注册 endpoint），保证成员可达
                "service_endpoint": self.local_server.advertised_endpoint or self.local_server.public_url(),
            })
            if not resp.get("ok"):
                raise HubError(f"成员 {member_id} 拒绝入群: {resp.get('error', 'unknown')}")
            rsig = resp.get("responder_signature", "")
            if not rsig or _rec(rsig, f"{group_id}:{self.agent_id}".encode("utf-8")) != member_id.lower():
                raise HubError(f"成员 {member_id[:12]}… 响应签名验证失败，可能被中间人冒充")
            member_sessions[member_id] = initiator_session(group_id, self.keys, ephemeral_priv,
                                                           peer["public_key"], member_id)
            member_endpoints[member_id] = peer["endpoint"]
        members = [self.agent_id] + member_ids
        self.local_server.register_group_service(group_id, member_sessions, member_endpoints,
                                                 members, topic)
        return {"group_id": group_id, "owner": self.agent_id, "members": members,
                "endpoints": member_endpoints, "topic": topic}

    def send_group(self, group: dict, text: str) -> list[tuple[str, dict]]:
        """发送群消息（端到端签名：钱包对 f"content:ts" 签名，群内任何人可验签）。
        群主：群服务内部转码广播；成员：加密发给群服务。"""
        if self.local_server is None:
            raise HubError("需要本地服务（local_server）")
        ts = int(__import__("time").time())
        group_id = group["group_id"]
        # 端到端签名：绑定群上下文（grp:group_id），防跨群重放
        payload = {
            "type": "text", "content": text, "ts": ts,
            "sender": self.agent_id,
            "signature": self.wallet.sign_text(f"grp:{group_id}:{text}:{ts}"),
        }
        if group.get("owner") == self.agent_id:
            # 群主 = 群服务：直接向各成员转码投递（payload 含群主签名）
            return self.local_server.send_group_as_service(group_id, self.agent_id, payload)
        # 成员：用与群服务的会话加密，发到群服务 /channel/group/message
        session = self.local_server.sessions.get(group_id)
        if session is None:
            raise HubError(f"本 Agent 未加入群 {group_id}")
        service_endpoint = self.local_server.group_meta(group_id).get("service_endpoint", "")
        env = session.encrypt_payload(payload)
        return [(group_id, _post(f"{service_endpoint}/channel/group/message",
                                 {"group_id": group_id, "sender": self.agent_id, "envelope": env}))]
