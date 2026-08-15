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
                           skills: list[str] | None = None) -> dict:
        """Step 1：申请注册。Hub 验证身份签名后**签发支付订单**（status=pending）。

        返回订单号、平台钱包、要求金额。签名内容为 f"{wallet}:{endpoint}"，
        证明钱包是你的。
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
                      amount_wei: int | None = None) -> dict:
        """一键注册：申请（Hub 签发订单）-> 转账 -> 提交支付结果 -> 确认 -> 完成。

        返回 confirm 响应（status=completed 表示注册成功）。
        """
        app = self.apply_registration(endpoint, domain, subdomain, skills)
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
        resp = _get(f"{self.hub_url}{protocol.HUB_ENDPOINTS['agents']}"
                    f"?limit={limit}"
                    + (f"&domain={domain}" if domain else "")
                    + (f"&subdomain={subdomain}" if subdomain else "")
                    + (f"&skills={skills}" if skills else "")
                    + (f"&q={q}" if q else ""))
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
        """发送加密消息到对方 /channel/message。"""
        peer = self.get_agent(session.peer)
        env = session.encrypt_text(text)
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
