"""
Hub（注册中心）—— 平台核心服务
================================
职责：
  1. 签发支付订单：Agent 申请注册 -> Hub 签发订单（绑定注册信息与钱包身份签名）
  2. 支付状态机：pending -> paid（提交支付结果）-> completed（链上确认后生成注册）
  3. 链上验证微量 BNB 到账（真实 BSC RPC 多端点轮询 / Mock 模式）
  4. 存储专业领域（三级标签）+ 接口地址 + 加密公钥，提供领域搜索/发现
  5. 心跳保活

订单状态机：
  pending   —— 已签发，等待 Agent 支付
  paid      —— Agent 已提交支付结果（tx_hash），等待 Hub 链上确认
  completed —— 链上确认通过，已生成注册（agents 表）
  failed    —— 链上确认失败（可重新提交支付结果后再次确认）
  expired   —— 超过订单有效期

零第三方依赖：Python 标准库 http.server + sqlite3 + urllib。
运行：python3 hub/hub.py   （默认 0.0.0.0:8731）
环境变量：
  AGENT_HUB_PORT        端口（默认 9000）
  AGENT_HUB_MOCK_CHAIN  1=本地模拟链（演示），0=真实 BSC RPC（默认 0）
  AGENT_HUB_PLATFORM_WALLET  平台钱包（默认自动发现 ~/.fly/users 或 0x97ab…）
  AGENT_HUB_STRICT_MANIFEST  1=endpoint /manifest 不可达时拒绝注册
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_sdk.protocol import HUB_API_PREFIX, is_valid_domain
from agent_sdk.wallet import (recover_address_from_signature,
                              parse_recoverable_signature,
                              platform_wallet_from_users_dir)
from agent_sdk import net
from hub.chain_verify import ChainVerifier, DEFAULT_PLATFORM_WALLET, MIN_BNB_WEI

PORT = int(os.environ.get("AGENT_HUB_PORT", "9000"))
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "hub.db"))
MOCK_CHAIN = os.environ.get("AGENT_HUB_MOCK_CHAIN", "0") == "1"
# 仪表盘访问令牌（留空=不启用；设置后页面需 ?token=xxx 或 Basic Auth）
DASHBOARD_TOKEN = os.environ.get("AGENT_HUB_DASHBOARD_TOKEN", "").strip()
PLATFORM_WALLET = (os.environ.get("AGENT_HUB_PLATFORM_WALLET")
                   or platform_wallet_from_users_dir()
                   or DEFAULT_PLATFORM_WALLET)
ORDER_TTL_SECONDS = 60 * 60  # 订单 1 小时有效

# ---- 注册订阅制：价格 / 有效期 / 续费 ----
# 注册费（每 24 小时），默认 0.0001 BNB；续费同样金额，从当前到期时间顺延
PRICE_BNB = float(os.environ.get("AGENT_HUB_PRICE_BNB", "0.0001"))
PRICE_WEI = int(PRICE_BNB * 1e18)
VALID_HOURS = int(os.environ.get("AGENT_HUB_VALID_HOURS", "24"))
VALID_SECONDS = VALID_HOURS * 3600

# 订单状态机
ORDER_PENDING, ORDER_PAID, ORDER_COMPLETED, ORDER_FAILED, ORDER_EXPIRED = (
    "pending", "paid", "completed", "failed", "expired")


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id    TEXT PRIMARY KEY,
                wallet      TEXT NOT NULL,           -- 申请注册的钱包地址
                order_type  TEXT NOT NULL DEFAULT 'register',  -- register | renew
                target_agent TEXT,                   -- renew 时续费的目标 agent_id
                endpoint    TEXT NOT NULL DEFAULT '',
                domain      TEXT NOT NULL DEFAULT '',
                subdomain   TEXT NOT NULL DEFAULT '',
                skills      TEXT NOT NULL DEFAULT '[]',
                public_key  TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'pending',
                tx_hash     TEXT,
                created_at  INTEGER NOT NULL,
                paid_at     INTEGER,
                confirmed_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS agents (
                agent_id        TEXT PRIMARY KEY,   -- 钱包地址
                endpoint        TEXT NOT NULL,
                domain          TEXT NOT NULL,
                subdomain       TEXT NOT NULL,
                skills          TEXT NOT NULL,      -- JSON 数组
                public_key      TEXT NOT NULL,      -- X25519 公钥 base64
                status          TEXT NOT NULL DEFAULT 'active',  -- active|expired|offline
                tx_hash         TEXT NOT NULL,
                expires_at      INTEGER NOT NULL,   -- 订阅到期时间（续费顺延）
                token_hash      TEXT,               -- 注册凭证 token 的 SHA-256（保活/续费鉴权）
                registered_at   INTEGER NOT NULL,
                last_heartbeat  INTEGER NOT NULL
            );
            """
        )
        # 迁移：旧版 orders 表（无 order_type 列）则重建
        ocols = {r[1] for r in self._db.execute("PRAGMA table_info(orders)")}
        if ocols and "order_type" not in ocols:
            self._db.execute("DROP TABLE orders")
            self._db.executescript(
                """CREATE TABLE orders (
                    order_id TEXT PRIMARY KEY, wallet TEXT NOT NULL,
                    order_type TEXT NOT NULL DEFAULT 'register', target_agent TEXT,
                    endpoint TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '',
                    subdomain TEXT NOT NULL DEFAULT '', skills TEXT NOT NULL DEFAULT '[]',
                    public_key TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                    tx_hash TEXT, created_at INTEGER NOT NULL,
                    paid_at INTEGER, confirmed_at INTEGER);"""
            )
        # 迁移：旧版 agents 表（无 token_hash 列）补列
        acols = {r[1] for r in self._db.execute("PRAGMA table_info(agents)")}
        if "expires_at" not in acols:
            self._db.execute("ALTER TABLE agents ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0")
        if "token_hash" not in acols:
            self._db.execute("ALTER TABLE agents ADD COLUMN token_hash TEXT")
        self._db.commit()

    # ---- 订单状态机 -----------------------------------------------------

    def create_order(self, wallet: str, endpoint: str, domain: str,
                     subdomain: str, skills: list, public_key: str,
                     order_type: str = "register", target_agent: str | None = None) -> str:
        order_id = "ord_" + uuid.uuid4().hex[:16]
        with self._lock:
            self._db.execute(
                """INSERT INTO orders (order_id, wallet, order_type, target_agent,
                       endpoint, domain, subdomain, skills, public_key, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, wallet.lower(), order_type, target_agent,
                 endpoint, domain, subdomain, json.dumps(skills), public_key,
                 ORDER_PENDING, int(time.time())),
            )
            self._db.commit()
        return order_id

    def get_order(self, order_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()

    def order_dict(self, order_id: str) -> dict | None:
        row = self.get_order(order_id)
        if not row:
            return None
        d = dict(row)
        d["skills"] = json.loads(d["skills"])
        return d

    def set_payment(self, order_id: str, tx_hash: str) -> bool:
        """Agent 提交支付结果：pending/failed -> paid（failed 允许重试）。"""
        with self._lock:
            cur = self._db.execute(
                """UPDATE orders SET status=?, tx_hash=?, paid_at=?
                   WHERE order_id=? AND status IN (?,?)""",
                (ORDER_PAID, tx_hash, int(time.time()), order_id, ORDER_PENDING, ORDER_FAILED),
            )
            self._db.commit()
        return cur.rowcount > 0

    def set_order_status(self, order_id: str, status: str) -> None:
        confirmed_at = int(time.time()) if status == ORDER_COMPLETED else None
        with self._lock:
            self._db.execute(
                "UPDATE orders SET status=?, confirmed_at=? WHERE order_id=?",
                (status, confirmed_at, order_id),
            )
            self._db.commit()

    # ---- 注册 / 续费（订阅制） ------------------------------------------

    def register_agent(self, agent_id: str, endpoint: str, domain: str, subdomain: str,
                       skills: list, public_key: str, tx_hash: str) -> str:
        """新注册：获得 VALID_SECONDS 有效期，并签发 agent_token（库内存哈希）。
        返回明文 token（仅此一次），用于保活/续费/刷新鉴权。"""
        now = int(time.time())
        expires = now + VALID_SECONDS
        token = secrets.token_hex(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            self._db.execute(
                """INSERT OR REPLACE INTO agents
                   (agent_id, endpoint, domain, subdomain, skills, public_key, status,
                    tx_hash, expires_at, token_hash, registered_at, last_heartbeat)
                   VALUES (?,?,?,?,?,?, 'active', ?, ?, ?, ?, ?)""",
                (agent_id, endpoint, domain, subdomain, json.dumps(skills), public_key,
                 tx_hash, expires, token_hash, now, now),
            )
            self._db.commit()
        return token

    def verify_token(self, agent_id: str, token: str) -> bool:
        """验证 agent_token（SHA-256 比对，库不存明文）。"""
        if not token:
            return False
        row = self._db.execute(
            "SELECT token_hash FROM agents WHERE agent_id=?", (agent_id.lower(),)).fetchone()
        if not row or not row["token_hash"]:
            return False  # 未签发 token 的旧记录不支持 token 鉴权
        return hashlib.sha256(token.encode()).hexdigest() == row["token_hash"]

    def refresh_agent(self, agent_id: str, endpoint: str) -> bool:
        """重启恢复：刷新 endpoint 与保活时间（endpoint 可能随 IP 变化）。"""
        with self._lock:
            cur = self._db.execute(
                """UPDATE agents SET endpoint=?, last_heartbeat=?, status='active'
                   WHERE agent_id=? AND expires_at > ?""",
                (endpoint, int(time.time()), agent_id.lower(), int(time.time())),
            )
            self._db.commit()
        return cur.rowcount > 0

    def renew_agent(self, agent_id: str, tx_hash: str) -> int | None:
        """续费：有效期从 max(now, 当前到期) 顺延 VALID_SECONDS（提前续费不损失时长）。
        返回新的到期时间。"""
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT expires_at FROM agents WHERE agent_id=?", (agent_id.lower(),)).fetchone()
            if not row:
                return None
            new_expires = max(now, row["expires_at"]) + VALID_SECONDS
            self._db.execute(
                """UPDATE agents SET expires_at=?, status='active', last_heartbeat=?, tx_hash=?
                   WHERE agent_id=?""",
                (new_expires, now, tx_hash, agent_id.lower()),
            )
            self._db.commit()
        return new_expires

    def agent_by_tx(self, tx_hash: str) -> str | None:
        """查询某笔 tx 已注册给哪个 agent（防 tx 重用/审计）。"""
        row = self._db.execute("SELECT agent_id FROM agents WHERE tx_hash=?", (tx_hash,)).fetchone()
        return row["agent_id"] if row else None

    # ---- 发现 / 心跳 ----------------------------------------------------

    @staticmethod
    def _is_expired(row) -> bool:
        """订阅是否已到期。"""
        return int(row["expires_at"] or 0) < int(time.time())

    @staticmethod
    def _publicize_endpoint(endpoint: str) -> str:
        """把回环地址（127.0.0.1/localhost/0.0.0.0）归一化为本机公网地址，
        用于列表展示——回环地址必然是与 Hub 同机的 Agent，公网可达时展示公网地址。"""
        import urllib.parse as _up
        try:
            u = _up.urlparse(endpoint)
            if u.hostname in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
                ip = net.public_ip()
                if ip:
                    port = f":{u.port}" if u.port else ""
                    return f"{u.scheme}://{ip}{port}{u.path}"
        except Exception:
            pass
        return endpoint

    def heartbeat(self, agent_id: str) -> bool:
        """心跳：订阅未过期才保持 active；过期返回 False（Agent 需续费）。"""
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT expires_at FROM agents WHERE agent_id=?", (agent_id.lower(),)).fetchone()
            if not row:
                return False
            if int(row["expires_at"] or 0) < now:
                self._db.execute("UPDATE agents SET status='expired' WHERE agent_id=?",
                                 (agent_id.lower(),))
                self._db.commit()
                return False
            cur = self._db.execute(
                "UPDATE agents SET status='active', last_heartbeat=? WHERE agent_id=?",
                (now, agent_id.lower()),
            )
            self._db.commit()
        return cur.rowcount > 0

    def search(self, domain: str | None = None, subdomain: str | None = None,
               skills: str | None = None, q: str | None = None,
               limit: int = 50) -> list[dict]:
        """仅返回订阅未过期的在线 Agent。"""
        now = int(time.time())
        sql = "SELECT * FROM agents WHERE status='active' AND expires_at > ?"
        args: list = [now]
        if domain:
            sql += " AND domain=?"
            args.append(domain)
        if subdomain:
            sql += " AND subdomain=?"
            args.append(subdomain)
        if skills:
            sql += " AND skills LIKE ?"
            args.append(f"%{skills}%")
        rows = self._db.execute(sql + " LIMIT ?", args + [limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["skills"] = json.loads(d["skills"])
            d["endpoint"] = self._publicize_endpoint(d["endpoint"])  # 列表展示公网地址
            if q:
                hay = f"{d['domain']} {d['subdomain']} {' '.join(d['skills'])} {d['endpoint']}".lower()
                if q.lower() not in hay:
                    continue
            out.append(d)
        return out

    def get_agent(self, agent_id: str) -> dict | None:
        """SDK 通信用：返回注册的原始 endpoint（同机=回环，公网部署=公网地址）。
        展示层（search/页面）才做公网归一化。订阅到期时 status 反映 expired。"""
        row = self._db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id.lower(),)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["skills"] = json.loads(d["skills"])
        if self._is_expired(row):
            d["status"] = "expired"
        return d

    def all_agents(self) -> list[dict]:
        rows = self._db.execute("SELECT * FROM agents").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["skills"] = json.loads(d["skills"])
            out.append(d)
        return out


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class HubHandler(BaseHTTPRequestHandler):
    server_version = "AgentMarketplaceHub/1.0"
    store: Store
    verifier: ChainVerifier

    def log_message(self, fmt, *args):
        print(f"[hub {time.strftime('%H:%M:%S')}] {fmt % args}")

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def dashboard_html() -> str:
        """Hub 仪表盘页面：展示注册智能体的钱包地址与专业领域（自动刷新）。"""
        return Dashboard.TEMPLATE

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    # ---- 接口所有权回查 --------------------------------------------------

    def _verify_manifest(self, endpoint: str, public_key: str, wallet: str) -> dict:
        """回查 endpoint 的 /manifest：确认接口真实存在、agent_id 与公钥一致。"""
        import urllib.request as _ur
        try:
            req = _ur.Request(f"{endpoint}/manifest", headers={"User-Agent": "agent-marketplace-hub/1.0"})
            with _ur.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "reachable": False, "error": f"/manifest 不可达: {type(e).__name__}"}
        manifest = data.get("manifest") or {}
        mid = (manifest.get("agent_id") or "").lower()
        if not mid:
            return {"ok": False, "reachable": True, "error": "/manifest 响应缺少 agent_id"}
        if mid != wallet.lower():
            return {"ok": False, "reachable": True, "error": f"/manifest 的 agent_id {mid[:14]}… 与注册钱包 {wallet[:14]}… 不一致（接口不是你的）"}
        mpub = manifest.get("public_key") or ""
        if mpub and mpub != public_key:
            return {"ok": False, "reachable": True, "error": "/manifest 公钥与注册公钥不一致"}
        return {"ok": True, "reachable": True, "manifest": manifest}

    # ---- 路由 ------------------------------------------------------------

    def _route(self, method: str):
        path = self.path.split("?")[0]
        query = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    query[k] = v

        # ---- 仪表盘页面：注册智能体（钱包地址 + 专业领域） ----
        if path in ("/", "/dashboard", "/index.html") and method == "GET":
            if DASHBOARD_TOKEN:
                # 鉴权：?token=xxx 或 Authorization: Bearer xxx / Basic base64(token:)
                import base64 as _b64
                auth_ok = query.get("token") == DASHBOARD_TOKEN
                if not auth_ok:
                    auth = self.headers.get("Authorization", "")
                    if auth.startswith("Bearer ") and auth[7:].strip() == DASHBOARD_TOKEN:
                        auth_ok = True
                    elif auth.startswith("Basic "):
                        try:
                            decoded = _b64.b64decode(auth[6:]).decode("utf-8")
                            auth_ok = decoded.split(":", 1)[0] == DASHBOARD_TOKEN
                        except Exception:
                            auth_ok = False
                if not auth_ok:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", "Basic realm=\"Expert Agent Hub Dashboard\"")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            return self._send_html(Dashboard.TEMPLATE)

        # ---- 平台信息 ----
        if path == f"{HUB_API_PREFIX}/info" and method == "GET":
            # 公网 Hub 地址：环境变量 > 自动探测 > Host header
            public_url = os.environ.get("AGENT_HUB_PUBLIC_URL", "").strip()
            if not public_url:
                ip = net.public_ip()
                public_url = f"http://{ip}:{PORT}" if ip else f"http://{self.headers.get('Host', 'localhost')}"
            return self._send(200, {
                "ok": True,
                "name": "Expert Agent Hub",
                "protocol": "agent-marketplace/v1",
                "hub_url": public_url,
                "platform_wallet": PLATFORM_WALLET,
                "min_bnb_wei": MIN_BNB_WEI,
                "min_bnb": MIN_BNB_WEI / 1e18,
                "usdt_amount": 0,
                "price_bnb": PRICE_BNB,             # 注册订阅价（每 24 小时）
                "price_wei": PRICE_WEI,
                "valid_hours": VALID_HOURS,         # 订阅有效期（小时）
                "renew_policy": "提前续费从当前到期时间顺延，不损失剩余时长",
                "chain_mode": "mock" if self.verifier.mock else "bsc-mainnet",
                "domains": {d: sub for d, sub in __import__("agent_sdk.protocol", fromlist=["PREDEFINED_DOMAINS"]).PREDEFINED_DOMAINS.items()},
                "agent_count": len(self.store.all_agents()),
            })

        # ---- ① 申请注册：Hub 签发支付订单 ----
        if path == f"{HUB_API_PREFIX}/applications" and method == "POST":
            body = self._body()
            wallet = (body.get("wallet") or "").strip().lower()
            order_type = (body.get("order_type") or "register").strip()
            endpoint = (body.get("endpoint") or "").strip()
            domain = (body.get("domain") or "").strip()
            subdomain = (body.get("subdomain") or "").strip()
            skills = body.get("skills") or []
            public_key = (body.get("public_key") or "").strip()
            signature = (body.get("signature") or "").strip()

            if not re.fullmatch(r"0x[0-9a-f]{40}", wallet):
                return self._send(400, {"ok": False, "error": "wallet 必须是 0x + 40 位 hex 的 EVM 地址"})

            if order_type == "renew":
                # ---- 续费：token 或钱包签名鉴权，只续费已有 Agent ----
                target_agent = (body.get("agent_id") or "").strip().lower()
                if target_agent != wallet:
                    return self._send(400, {"ok": False, "error": "续费目标 agent_id 必须与钱包一致"})
                existing = self.store.get_agent(target_agent)
                if not existing:
                    return self._send(404, {"ok": False, "error": f"Agent {target_agent[:14]}… 未注册，请先注册"})
                token = (body.get("token") or "").strip()
                sig_hex = rec_id = None
                if token:
                    # 方式 A：注册凭证 token 鉴权
                    if not self.store.verify_token(wallet, token):
                        return self._send(403, {"ok": False, "error": "token 无效"})
                else:
                    # 方式 B：钱包签名鉴权 f"renew:{wallet}"
                    signature = (body.get("signature") or "").strip()
                    message = f"renew:{wallet}".encode("utf-8")
                    sig_hex, rec_id = parse_recoverable_signature(signature)
                    recovered = recover_address_from_signature(sig_hex, message, rec_id)
                    if not recovered or recovered != wallet:
                        return self._send(400, {"ok": False, "error": f"续费签名无效或与钱包不一致（恢复出 {recovered}）"})
                order_id = self.store.create_order(wallet, "", "", "", [], "",
                                                   order_type="renew", target_agent=target_agent)
            else:
                # ---- 注册：校验领域/接口/签名，签发订单 ----
                if not re.match(r"^https?://", endpoint):
                    return self._send(400, {"ok": False, "error": "endpoint 必须以 http(s):// 开头"})
                if not is_valid_domain(domain, subdomain):
                    return self._send(400, {"ok": False, "error": f"领域无效：{domain}/{subdomain}，请查阅 /api/v1/info 的预定义列表"})
                message = f"{wallet}:{endpoint}".encode("utf-8")
                sig_hex, rec_id = parse_recoverable_signature(signature)
                recovered = recover_address_from_signature(sig_hex, message, rec_id)
                if not recovered or recovered != wallet:
                    return self._send(400, {"ok": False, "error": f"签名无效或与钱包不一致（恢复出 {recovered}）"})
                order_id = self.store.create_order(wallet, endpoint, domain, subdomain,
                                                   [str(s) for s in skills], public_key,
                                                   order_type="register")

            return self._send(201, {
                "ok": True,
                "order_id": order_id,
                "status": ORDER_PENDING,
                "order_type": order_type,
                "wallet": wallet,
                "platform_wallet": PLATFORM_WALLET,
                "amount_wei": PRICE_WEI,
                "amount_bnb": PRICE_BNB,
                "currency": "BNB",
                "usdt_amount": 0,
                "valid_hours": VALID_HOURS,
                "renew_note": "提前续费从当前到期时间顺延，不损失剩余时长" if order_type == "renew" else None,
                "chain_mode": "mock" if self.verifier.mock else "bsc-mainnet",
                "ttl_seconds": ORDER_TTL_SECONDS,
                "steps": [
                    f"1. 用你的钱包向平台钱包转账 {PRICE_BNB} BNB（{VALID_HOURS} 小时订阅）",
                    "2. POST /api/v1/orders/{order_id}/payment 提交支付结果 tx_hash",
                    f"3. POST /api/v1/orders/{order_id}/confirm 由 Hub 链上确认，{'续费生效' if order_type == 'renew' else '完成注册'}",
                ],
                "mock_transfer_endpoint": "/api/v1/mock/transfer" if self.verifier.mock else None,
            })

        # ---- Mock 模式：模拟转账 ----
        if path == f"{HUB_API_PREFIX}/mock/transfer" and method == "POST":
            if not self.verifier.mock:
                return self._send(403, {"ok": False, "error": "当前为真实 BSC 模式，不允许模拟转账"})
            body = self._body()
            tx_hash = (body.get("tx_hash") or "").strip()
            from_addr = (body.get("from") or "").strip()
            amount_wei = int(body.get("amount_wei", MIN_BNB_WEI))
            if not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash):
                return self._send(400, {"ok": False, "error": "tx_hash 必须是 0x + 64 位 hex"})
            if not re.fullmatch(r"0x[0-9a-f]{40}", from_addr):
                return self._send(400, {"ok": False, "error": "from 必须是 0x + 40 位 hex 地址"})
            self.verifier.mock_record_transfer(tx_hash, from_addr, amount_wei)
            return self._send(200, {"ok": True, "message": "模拟转账已记录，可提交支付结果"})

        # ---- ② 提交支付结果：pending -> paid ----
        m = re.match(rf"^{HUB_API_PREFIX}/orders/([^/]+)/payment$", path)
        if m and method == "POST":
            order_id = m.group(1)
            order = self.store.get_order(order_id)
            if not order:
                return self._send(404, {"ok": False, "error": "订单不存在"})
            if order["status"] not in (ORDER_PENDING, ORDER_FAILED):
                return self._send(400, {"ok": False, "error": f"订单当前状态为 {order['status']}，只有 pending/failed 可提交支付结果"})
            if int(time.time()) - order["created_at"] > ORDER_TTL_SECONDS:
                self.store.set_order_status(order_id, ORDER_EXPIRED)
                return self._send(400, {"ok": False, "error": "订单已过期，请重新申请"})
            body = self._body()
            tx_hash = (body.get("tx_hash") or "").strip()
            if not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash):
                return self._send(400, {"ok": False, "error": "tx_hash 必须是 0x + 64 位 hex"})
            if not self.store.set_payment(order_id, tx_hash):
                return self._send(400, {"ok": False, "error": "订单状态已变更，无法提交"})
            return self._send(200, {
                "ok": True,
                "order_id": order_id,
                "status": ORDER_PAID,
                "tx_hash": tx_hash,
                "message": "支付结果已记录，等待 Hub 链上确认（POST /confirm）",
            })

        # ---- ③ Hub 确认支付结果：paid -> completed（链上验证 + 生成注册） ----
        m = re.match(rf"^{HUB_API_PREFIX}/orders/([^/]+)/confirm$", path)
        if m and method == "POST":
            order_id = m.group(1)
            order = self.store.get_order(order_id)
            if not order:
                return self._send(404, {"ok": False, "error": "订单不存在"})
            if order["status"] == ORDER_COMPLETED:
                return self._send(200, {"ok": True, "order_id": order_id,
                                        "status": ORDER_COMPLETED,
                                        "agent_id": order["wallet"],
                                        "message": "订单已完成注册"})
            if order["status"] != ORDER_PAID:
                return self._send(400, {"ok": False, "error": f"订单当前状态为 {order['status']}，需先提交支付结果"})
            if int(time.time()) - order["created_at"] > ORDER_TTL_SECONDS:
                self.store.set_order_status(order_id, ORDER_EXPIRED)
                return self._send(400, {"ok": False, "error": "订单已过期，请重新申请"})
            tx_hash = order["tx_hash"]
            is_renew = order["order_type"] == "renew"

            # ① 链上验证转账：收款=平台钱包、from=订单钱包、金额达标、交易成功
            ok, msg = self.verifier.verify_payment(tx_hash, order["wallet"])
            if not ok:
                self.store.set_order_status(order_id, ORDER_FAILED)
                return self._send(402, {"ok": False, "order_id": order_id,
                                        "status": ORDER_FAILED,
                                        "error": f"链上验证失败：{msg}",
                                        "hint": "可重新提交支付结果后再次确认"})
            # ② 防 tx 重用：同一笔交易不允许注册为不同 agent
            used_by = self.store.agent_by_tx(tx_hash)
            if used_by and used_by != order["wallet"]:
                self.store.set_order_status(order_id, ORDER_FAILED)
                return self._send(400, {"ok": False, "order_id": order_id,
                                        "status": ORDER_FAILED,
                                        "error": f"交易 {tx_hash} 已被 agent {used_by[:12]}… 使用，不可重复注册"})

            if is_renew:
                # ---- 续费确认：有效期从当前到期顺延，订单完成 ----
                new_expires = self.store.renew_agent(order["wallet"], tx_hash)
                if new_expires is None:
                    self.store.set_order_status(order_id, ORDER_FAILED)
                    return self._send(404, {"ok": False, "order_id": order_id,
                                            "status": ORDER_FAILED,
                                            "error": "续费目标 Agent 不存在"})
                self.store.set_order_status(order_id, ORDER_COMPLETED)
                return self._send(200, {
                    "ok": True,
                    "order_id": order_id,
                    "status": ORDER_COMPLETED,
                    "order_type": "renew",
                    "agent_id": order["wallet"],
                    "renewed": True,
                    "valid_hours": VALID_HOURS,
                    "new_expires_at": new_expires,
                    "chain_msg": msg,
                })

            # ③ endpoint 所有权回查（仅注册需要）
            manifest_check = self._verify_manifest(order["endpoint"], order["public_key"], order["wallet"])
            if not manifest_check["ok"]:
                if manifest_check.get("reachable"):
                    # 可达但不匹配 = 冒名/伪造接口 -> 任何模式都拒绝
                    self.store.set_order_status(order_id, ORDER_FAILED)
                    return self._send(400, {"ok": False, "order_id": order_id,
                                            "status": ORDER_FAILED,
                                            "error": f"endpoint 验证失败：{manifest_check['error']}"})
                if os.environ.get("AGENT_HUB_STRICT_MANIFEST", "0") == "1":
                    self.store.set_order_status(order_id, ORDER_FAILED)
                    return self._send(400, {"ok": False, "order_id": order_id,
                                            "status": ORDER_FAILED,
                                            "error": f"endpoint 验证失败：{manifest_check['error']}"})
                print(f"[hub] ⚠ 订单 {order_id} 的 /manifest 不可达（宽松模式放行）: {manifest_check['error']}")

            # ④ 全部通过 -> 生成注册（签发 agent_token），订单完成
            agent_token = self.store.register_agent(order["wallet"], order["endpoint"], order["domain"],
                                                    order["subdomain"], json.loads(order["skills"]),
                                                    order["public_key"], tx_hash)
            self.store.set_order_status(order_id, ORDER_COMPLETED)
            return self._send(200, {
                "ok": True,
                "order_id": order_id,
                "status": ORDER_COMPLETED,
                "agent_id": order["wallet"],
                "registered": True,
                "agent_token": agent_token,   # 保活/续费/刷新凭证（仅此一次返回，库内存哈希）
                "token_note": "请妥善保存：heartbeat/renew/refresh 均需此 token 鉴权",
                "chain_msg": msg,
                "manifest_check": manifest_check,
                "manifest_url": f"{order['endpoint']}/manifest",
            })

        # ---- 订单状态查询 ----
        m = re.match(rf"^{HUB_API_PREFIX}/orders/([^/]+)$", path)
        if m and method == "GET":
            d = self.store.order_dict(m.group(1))
            if not d:
                return self._send(404, {"ok": False, "error": "订单不存在"})
            return self._send(200, {"ok": True, "order": d})

        # ---- 搜索 Agent ----
        if path == f"{HUB_API_PREFIX}/agents" and method == "GET":
            limit = min(int(query.get("limit", 50)), 200)
            agents = self.store.search(
                domain=query.get("domain"), subdomain=query.get("subdomain"),
                skills=query.get("skills"), q=query.get("q"), limit=limit,
            )
            return self._send(200, {"ok": True, "count": len(agents), "agents": agents})

        # ---- Agent 详情 ----
        m = re.match(rf"^{HUB_API_PREFIX}/agents/(0x[0-9a-fA-F]{{40}})$", path)
        if m and method == "GET":
            agent = self.store.get_agent(m.group(1))
            if not agent:
                return self._send(404, {"ok": False, "error": "Agent 不存在"})
            return self._send(200, {"ok": True, "agent": agent})

        # ---- 心跳（agent_token 鉴权，防冒名保活） ----
        if path == f"{HUB_API_PREFIX}/heartbeat" and method == "POST":
            body = self._body()
            agent_id = (body.get("agent_id") or "").strip().lower()
            token = (body.get("token") or "").strip()
            row = self.store.get_agent(agent_id)
            if not row:
                return self._send(404, {"ok": False, "error": "Agent 未注册"})
            # 已签发 token 的 Agent 必须用 token 保活（未签发 token 的旧记录宽松兼容）
            if row.get("token_hash") and not self.store.verify_token(agent_id, token):
                return self._send(403, {"ok": False, "error": "token 无效，请重新注册或核对凭证"})
            if self.store.heartbeat(agent_id):
                return self._send(200, {"ok": True, "status": "active"})
            return self._send(402, {"ok": False, "error": "订阅已到期，请续费（renew_subscription）"})

        # ---- 重启恢复：token 鉴权刷新 endpoint 与保活时间 ----
        m = re.match(rf"^{HUB_API_PREFIX}/agents/(0x[0-9a-fA-F]{{40}})/refresh$", path)
        if m and method == "POST":
            body = self._body()
            agent_id = m.group(1).lower()
            token = (body.get("token") or "").strip()
            endpoint = (body.get("endpoint") or "").strip()
            if not self.store.verify_token(agent_id, token):
                return self._send(403, {"ok": False, "error": "token 无效"})
            if not re.match(r"^https?://", endpoint):
                return self._send(400, {"ok": False, "error": "endpoint 必须以 http(s):// 开头"})
            if not self.store.refresh_agent(agent_id, endpoint):
                return self._send(402, {"ok": False, "error": "订阅已到期，请先续费"})
            return self._send(200, {"ok": True, "agent_id": agent_id,
                                    "status": "active", "endpoint": endpoint})

        return self._send(404, {"ok": False, "error": f"未知接口 {method} {path}"})


def make_server(port: int = PORT, mock: bool | None = None) -> ThreadingHTTPServer:
    store = Store(DB_PATH)
    verifier = ChainVerifier(PLATFORM_WALLET, mock=mock)
    server = ThreadingHTTPServer(("0.0.0.0", port), HubHandler)
    HubHandler.store = store
    HubHandler.verifier = verifier
    return server


# ---------------------------------------------------------------------------
# 仪表盘页面
# ---------------------------------------------------------------------------

class Dashboard:
    """Hub 仪表盘：展示注册智能体的钱包地址与专业领域（纯前端，无外部依赖）。"""

    TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Expert Agent Hub · 专业智能体协作平台</title>
<style>
  :root {
    --bg: #0f1420; --panel: #171e2e; --panel2: #1d2537; --line: #2a3550;
    --text: #dbe4f5; --muted: #7d8aa8; --accent: #4da3ff; --ok: #34d399;
    --warn: #fbbf24; --danger: #f87171;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font: 14px/1.6 "SF Mono", Menlo, Consolas, "Courier New", monospace; padding: 28px 20px; }
  .wrap { max-width: 1180px; margin: 0 auto; }
  header { display: flex; align-items: baseline; gap: 14px; margin-bottom: 20px; flex-wrap: wrap; }
  h1 { font-size: 20px; letter-spacing: .5px; }
  h1 .dot { color: var(--ok); }
  .sub { color: var(--muted); font-size: 12px; }
  .refresh { margin-left: auto; color: var(--muted); font-size: 12px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 16px; }
  .stat .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
  .stat .v { font-size: 17px; margin-top: 4px; word-break: break-all; }
  .stat .v small { color: var(--muted); font-size: 12px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
  .card-head { padding: 12px 18px; background: var(--panel2); border-bottom: 1px solid var(--line); font-size: 13px; color: var(--muted); display: flex; gap: 18px; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 10px 14px; font-size: 11px; color: var(--muted); letter-spacing: 1px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  td { padding: 10px 14px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  tr:hover td { background: rgba(77,163,255,.05); }
  .addr { font-size: 13px; }
  .addr .full { color: var(--muted); font-size: 11px; }
  .tag { display: inline-block; background: rgba(77,163,255,.12); color: var(--accent); border: 1px solid rgba(77,163,255,.3); border-radius: 4px; padding: 1px 7px; font-size: 11px; margin: 1px 3px 1px 0; }
  .domain { color: #c4b5fd; }
  .status-active { color: var(--ok); }
  .status-offline { color: var(--danger); }
  .status-paused { color: var(--warn); }
  .ep { color: var(--muted); font-size: 12px; word-break: break-all; }
  .empty { padding: 40px; text-align: center; color: var(--muted); }
  .empty code { background: var(--panel2); padding: 2px 8px; border-radius: 4px; }
  .badge { font-size: 10px; padding: 2px 8px; border-radius: 10px; border: 1px solid var(--line); color: var(--muted); }
  footer { margin-top: 18px; color: var(--muted); font-size: 11px; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span class="dot">●</span> Expert Agent Hub</h1>
    <span class="sub">专业智能体协作平台</span>
    <span class="refresh" id="refresh">—</span>
  </header>

  <div class="stats" id="stats"></div>

  <div class="card">
    <div class="card-head">
      <span>🧠 已注册智能体</span>
      <span id="count">0 个</span>
      <span>（钱包地址 = agent_id = 专业身份）</span>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>钱包地址 (agent_id)</th>
            <th>专业领域</th>
            <th>技能标签</th>
            <th>接口地址</th>
            <th>状态</th>
            <th>订阅到期</th>
            <th>注册时间</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
  <footer>数据来自 <code>/api/v1/agents</code> · 每 5 秒自动刷新 · 钱包地址即智能体唯一标识</footer>
</div>

<script>
const fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", {hour12: false}) : "—";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function load() {
  try {
    const [infoR, agentsR] = await Promise.all([
      fetch("/api/v1/info"), fetch("/api/v1/agents?limit=500")
    ]);
    const info = await infoR.json();
    const data = await agentsR.json();
    render(info, data.agents || []);
    document.getElementById("refresh").textContent =
      "更新于 " + new Date().toLocaleTimeString("zh-CN", {hour12: false});
  } catch (e) {
    document.getElementById("rows").innerHTML =
      '<tr><td colspan="7" class="empty">⚠ 加载失败：' + esc(e) + '</td></tr>';
  }
}

function render(info, agents) {
  const now = Math.floor(Date.now() / 1000);
  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="k">平台钱包</div><div class="v">${esc(info.platform_wallet)}</div></div>
    <div class="stat"><div class="k">链验证模式</div><div class="v">${esc(info.chain_mode)}</div></div>
    <div class="stat"><div class="k">注册费</div><div class="v">${info.min_bnb} BNB <small>+ 0 USDT</small></div></div>
    <div class="stat"><div class="k">已注册智能体</div><div class="v">${agents.length}</div></div>`;

  document.getElementById("count").textContent = agents.length + " 个";
  const rows = document.getElementById("rows");
  if (!agents.length) {
    rows.innerHTML = '<tr><td colspan="7" class="empty">暂无注册智能体<br><br>接入方法：<code>POST /api/v1/applications</code> 申请注册 → 支付 → 确认<br>或运行 <code>agent_cli.py register</code></td></tr>';
    return;
  }
  rows.innerHTML = agents.map(a => {
    const offline = now - (a.last_heartbeat || 0) > 2700;  // 15 分钟保活，45 分钟（3倍）无心跳视为离线
    const st = offline ? "offline" : (a.status || "active");
    const skills = (a.skills || []).map(s => `<span class="tag">${esc(s)}</span>`).join("");
    const exp = a.expires_at || 0;
    const expired = exp > 0 && exp < now;
    const expStatus = expired ? "offline" : (st === "active" ? "active" : st);
    return `<tr>
      <td><div class="addr">${esc(a.agent_id)}</div></td>
      <td><span class="domain">${esc(a.domain)}</span>${a.subdomain ? ` / ${esc(a.subdomain)}` : ""}</td>
      <td>${skills || "—"}</td>
      <td class="ep">${esc(a.endpoint)}</td>
      <td><span class="status-${expStatus}">● ${expired ? "expired(订阅到期)" : st}</span></td>
      <td>${exp ? fmt(exp) : "—"}${exp && !expired ? ` <span class="tag">${Math.max(0, Math.ceil((exp - now) / 3600))}h后</span>` : ""}</td>
      <td>${fmt(a.registered_at)}</td>
    </tr>`;
  }).join("");
}

load();
setInterval(load, 5000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    srv = make_server()
    mode = "MOCK（本地模拟链）" if MOCK_CHAIN else "BSC 主网 RPC"
    pub_ip = net.public_ip()
    print("=" * 60)
    print(f"  Expert Agent Hub 已启动")
    print(f"  平台钱包 : {PLATFORM_WALLET}")
    print(f"  链验证   : {mode}")
    print(f"  监听     : http://0.0.0.0:{PORT}")
    if pub_ip:
        print(f"  公网访问 : http://{pub_ip}:{PORT}/   （需安全组放行 {PORT} 端口）")
    else:
        print(f"  ⚠ 无法探测公网 IP：可设 AGENT_PUBLIC_IP 显式指定，或经安全组/反代映射")
    print(f"  仪表盘   : http://127.0.0.1:{PORT}/")
    print(f"  订单状态机: pending -> paid -> completed")
    print(f"  申请注册 : POST /api/v1/applications")
    print(f"  提交支付 : POST /api/v1/orders/{{id}}/payment")
    print(f"  确认支付 : POST /api/v1/orders/{{id}}/confirm")
    print(f"  搜索     : GET  /api/v1/agents?domain=finance")
    print("=" * 60)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nHub 已停止")
