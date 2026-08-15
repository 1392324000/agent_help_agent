"""
Agent SDK —— 订阅支付协议（Agent 间 USDT 结算）
==================================================
复刻「Agent↔Hub 注册」的订单-支付-验证-签发token-验签机制，但发生在
两个智能体之间：需求方 A 向服务方 B 购买一段时间的调用权。

流程（订单状态机 pending → paid → completed）：
    A ──POST /subscribe──────────────▶ B  申请订阅（B 签发订单：金额=报价×时长）
    A ──链上转账 USDT ──────────────▶ B  （BEP-20，链上直转，无托管）
    A ──POST /subscribe/payment─────▶ B  提交 tx_hash（B 验证 USDT 到账）
    A ◀──POST /subscribe/confirm────── B  确认后签发「签名订阅 token」
    A ──POST /invoke {token,...}────▶ B  有效期内随便调用（B 验签 token 时效）
    A ◀──{result, artifact}──────────── B  产物返回

信任模型：
    - 资金：A→B 链上 USDT 直转，无第三方托管（平台零资金风险）
    - token：B 钱包私钥对 payload 的 ECDSA 签名，A 可验签（恢复地址==B）
    - 验签：无状态（不查库），签名 + 时效即验证；防篡改（改任何字段签名失效）
"""

from __future__ import annotations

import json
import time
import uuid

from .wallet import (Wallet, recover_address_from_signature,
                     parse_recoverable_signature)

TOKEN_VERSION = 1
DEFAULT_VALID_HOURS = 24  # 订单有效期（小时，未支付则过期）


# ---------------------------------------------------------------------------
# 签名订阅 token（服务方签发，需求方验签）
# ---------------------------------------------------------------------------

def create_sub_token(wallet: Wallet, subscriber: str, duration_hours: float,
                     order_id: str, now: int | None = None) -> dict:
    """服务方签发订阅 token：对 payload 规范化 JSON 做 ECDSA 签名。

    返回 {"payload": {...}, "canon": 被签名的规范化字符串, "signature": 65字节hex}
    """
    now = now or int(time.time())
    payload = {
        "v": TOKEN_VERSION,
        "sub": subscriber.lower(),
        "iss": wallet.address.lower(),
        "dur_h": round(float(duration_hours), 6),
        "oid": order_id,
        "exp": now + int(float(duration_hours) * 3600),
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = wallet.sign_text(canon)
    return {"payload": payload, "canon": canon, "signature": signature}


def verify_sub_token(token: dict, expected_issuer: str) -> dict | None:
    """需求方/服务方验签订阅 token。

    校验：① 签名恢复地址 == 签发方(服务方)  ② 未过期。
    通过返回 payload，失败返回 None。
    """
    try:
        payload = token.get("payload") or {}
        canon = token.get("canon") or ""
        signature = token.get("signature") or ""
        if not canon or not signature:
            return None
        sig_hex, rec_id = parse_recoverable_signature(signature)
        recovered = recover_address_from_signature(sig_hex, canon.encode("utf-8"), rec_id)
        if not recovered or recovered.lower() != expected_issuer.lower():
            return None
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 服务方订单状态机（内存存储；Agent 服务重启后订单需重新申请）
# ---------------------------------------------------------------------------

class SubscriptionStore:
    """服务方本地的订阅订单状态机：pending → paid → completed。

    与 Hub 注册订单状态机语义一致，仅存于服务方进程内（无中心库）。
    """

    def __init__(self):
        self._orders: dict[str, dict] = {}
        self._lock = __import__("threading").Lock()

    def create(self, subscriber: str, duration_hours: float,
               price_per_hour: float, receiver: str,
               chain: str = "mock") -> dict:
        order_id = "sub_" + uuid.uuid4().hex[:16]
        amount = round(float(price_per_hour) * float(duration_hours), 6)
        with self._lock:
            self._orders[order_id] = {
                "order_id": order_id,
                "subscriber": subscriber.lower(),
                "duration_hours": float(duration_hours),
                "price_per_hour": float(price_per_hour),
                "amount_usdt": amount,
                "receiver": receiver,
                "status": "pending",
                "tx_hash": None,
                "created_at": int(time.time()),
                "paid_at": None,
                "token": None,
                "expires_at": None,
            }
        return dict(self._orders[order_id])

    def get(self, order_id: str) -> dict | None:
        with self._lock:
            o = self._orders.get(order_id)
            return dict(o) if o else None

    def mark_paid(self, order_id: str, tx_hash: str) -> bool:
        """pending -> paid（提交支付结果）。"""
        with self._lock:
            o = self._orders.get(order_id)
            if not o or o["status"] != "pending":
                return False
            o["status"] = "paid"
            o["tx_hash"] = tx_hash
            o["paid_at"] = int(time.time())
            return True

    def mark_completed(self, order_id: str, token: dict) -> bool:
        """paid -> completed（链上确认到账后签发 token）。"""
        with self._lock:
            o = self._orders.get(order_id)
            if not o or o["status"] != "paid":
                return False
            o["status"] = "completed"
            o["token"] = token
            o["expires_at"] = token["payload"]["exp"]
            return True

    def is_expired(self, order_id: str) -> bool:
        o = self.get(order_id)
        if not o:
            return True
        # 订单 24 小时未支付视为过期
        return o["status"] == "pending" and \
            int(time.time()) - o["created_at"] > DEFAULT_VALID_HOURS * 3600
