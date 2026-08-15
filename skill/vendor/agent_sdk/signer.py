"""
Wallet Signer —— 钱包签名服务（私钥隔离）
==========================================
增强 B：钱包私钥由独立签名服务持有，Agent 业务进程不持有私钥，
只通过 HTTP 请求签名。即使 Agent 被攻破，攻击者最多让签名服务代签，
无法提取私钥。

架构：
  Agent 业务进程                    wallet_signer（独立进程/端口）
  ┌──────────────────┐  POST /sign  ┌──────────────────────────┐
  │ WalletSignerClient│ ───────────▶ │ WalletSignerServer        │
  │ （无私钥）         │ ◀─────────── │ 持有 Wallet（私钥驻留内存）│
  └──────────────────┘  signature   └──────────────────────────┘

鉴权：签名接口要求 `Authorization: Bearer <token>`（Agent 与签名服务共享令牌，
令牌泄露≠私钥泄露，可轮换）。

启动签名服务：
  AGENT_SIGNER_TOKEN=<令牌> AGENT_WALLET_KEY=0x<私钥hex> \
    python3 -m agent_sdk.signer --port 9100

Agent 侧使用（接口兼容 Wallet，业务代码无需改动）：
  from agent_sdk.signer import WalletSignerClient
  wallet = WalletSignerClient("http://127.0.0.1:9100", token, address=预知地址)
  client = HubClient(HUB, wallet, keys)   # 注册/握手/群消息签名全部远程完成
"""

from __future__ import annotations

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .wallet import Wallet


# ---------------------------------------------------------------------------
# 签名服务（持有私钥）
# ---------------------------------------------------------------------------

class WalletSignerServer:
    def __init__(self, wallet: Wallet, port: int = 9100, token: str = "",
                 host: str = "0.0.0.0"):
        self.wallet = wallet
        self.port = port
        self.token = token
        self.host = host
        self._httpd: ThreadingHTTPServer | None = None

    def start(self, background: bool = True) -> "WalletSignerServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "WalletSigner/1.0"

            def log_message(self, fmt, *args):
                print(f"[signer {__import__('time').strftime('%H:%M:%S')}] {fmt % args}")

            def _auth(self) -> bool:
                if not owner.token:
                    return True
                auth = self.headers.get("Authorization", "")
                return auth == f"Bearer {owner.token}"

            def _send(self, code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    if n > 65536:
                        raise ValueError("too large")
                    raw = self.rfile.read(n) if n else b"{}"
                    return json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    return {}

            def do_GET(self):
                if not self._auth():
                    return self._send(401, {"ok": False, "error": "unauthorized"})
                if self.path.split("?")[0] == "/info":
                    return self._send(200, {
                        "ok": True,
                        "address": owner.wallet.address,
                        "public_key_hex": owner.wallet.public_key_bytes.hex(),
                    })
                if self.path.split("?")[0] == "/health":
                    return self._send(200, {"ok": True})
                self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if not self._auth():
                    return self._send(401, {"ok": False, "error": "unauthorized"})
                path = self.path.split("?")[0]
                body = self._body()
                try:
                    if path == "/sign":            # {message_hex} -> 65字节 r||s||v
                        msg = bytes.fromhex(body.get("message_hex", ""))
                        return self._send(200, {"ok": True, "signature": owner.wallet.sign(msg)})
                    if path == "/sign_text":       # {text} -> 65字节 r||s||v
                        return self._send(200, {"ok": True,
                                                "signature": owner.wallet.sign_text(body.get("text", ""))})
                    if path == "/sign_recoverable":  # {message_hex} -> {signature, rec_id}
                        msg = bytes.fromhex(body.get("message_hex", ""))
                        sig, rec = owner.wallet.sign_recoverable(msg)
                        return self._send(200, {"ok": True, "signature": sig, "rec_id": rec})
                    if path == "/sign_text_recoverable":  # {text} -> {signature, rec_id}
                        sig, rec = owner.wallet.sign_text_recoverable(body.get("text", ""))
                        return self._send(200, {"ok": True, "signature": sig, "rec_id": rec})
                except Exception as e:
                    return self._send(400, {"ok": False, "error": str(e)})
                self._send(404, {"ok": False, "error": "not found"})

            def do_OPTIONS(self):
                self._send(204, {})

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()
        if background:
            print(f"[signer] 钱包签名服务已启动 :{self.port}  "
                  f"address={owner.wallet.address[:14]}…  （私钥不离开本服务）")
        return self

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()


# ---------------------------------------------------------------------------
# 远程签名钱包（Agent 侧，无私钥，接口兼容 Wallet）
# ---------------------------------------------------------------------------

class WalletSignerClient:
    """不持有私钥的钱包：签名经远程 signer 服务完成。

    兼容 Wallet 的接口（address / public_key_bytes / sign / sign_text /
    sign_recoverable / sign_text_recoverable），业务代码无需区分。
    """

    def __init__(self, signer_url: str, token: str = "", address: str | None = None):
        self.signer_url = signer_url.rstrip("/")
        self.token = token
        self._cached_address = address

    # -- 信息（从 /info 获取或缓存） -------------------------------------

    def _info(self) -> dict:
        import urllib.request as _ur
        req = _ur.Request(f"{self.signer_url}/info",
                          headers={"Authorization": f"Bearer {self.token}"})
        with _ur.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "signer info failed"))
        return data

    @property
    def address(self) -> str:
        if not self._cached_address:
            self._cached_address = self._info()["address"]
        return self._cached_address

    @property
    def public_key_bytes(self) -> bytes:
        return bytes.fromhex(self._info()["public_key_hex"])

    # -- 签名（远程） -----------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        import urllib.request as _ur
        import urllib.error as _uerr
        req = _ur.Request(f"{self.signer_url}{path}",
                          data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json",
                                   "Authorization": f"Bearer {self.token}"},
                          method="POST")
        try:
            with _ur.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except _uerr.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                raise RuntimeError(f"signer HTTP {e.code}") from e

    def sign(self, message: bytes) -> str:
        resp = self._post("/sign", {"message_hex": message.hex()})
        if not resp.get("ok"):
            raise RuntimeError(f"签名失败: {resp.get('error')}")
        return resp["signature"]

    def sign_text(self, text: str) -> str:
        resp = self._post("/sign_text", {"text": text})
        if not resp.get("ok"):
            raise RuntimeError(f"签名失败: {resp.get('error')}")
        return resp["signature"]

    def sign_recoverable(self, message: bytes) -> tuple[str, int]:
        resp = self._post("/sign_recoverable", {"message_hex": message.hex()})
        if not resp.get("ok"):
            raise RuntimeError(f"签名失败: {resp.get('error')}")
        return resp["signature"], resp["rec_id"]

    def sign_text_recoverable(self, text: str) -> tuple[str, int]:
        resp = self._post("/sign_text_recoverable", {"text": text})
        if not resp.get("ok"):
            raise RuntimeError(f"签名失败: {resp.get('error')}")
        return resp["signature"], resp["rec_id"]

    def verify(self, signature_hex: str, message: bytes) -> bool:
        """验签在本地完成（公钥公开，无需私钥）。"""
        try:
            from cryptography.hazmat.primitives.asymmetric import ec as _ec
            from cryptography.hazmat.primitives import hashes as _h
            from cryptography.hazmat.primitives.asymmetric import utils as _u
            from .wallet import parse_signature
            r, s, _ = parse_signature(signature_hex)
            pub = _ec.EllipticCurvePublicKey.from_encoded_point(_ec.SECP256K1(), self.public_key_bytes)
            pub.verify(_u.encode_dss_signature(r, s), message, _ec.ECDSA(_h.SHA256()))
            return True
        except Exception:
            return False

    def verify_text(self, signature_hex: str, text: str) -> bool:
        return self.verify(signature_hex, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# CLI：启动签名服务
# ---------------------------------------------------------------------------

def main():
    import argparse
    import sys
    p = argparse.ArgumentParser(description="Wallet Signer 服务（私钥隔离）")
    p.add_argument("--port", type=int, default=int(os.environ.get("AGENT_SIGNER_PORT", "9100")))
    p.add_argument("--key", default=os.environ.get("AGENT_WALLET_KEY", ""),
                   help="钱包私钥 hex（或 AGENT_WALLET_KEY）")
    p.add_argument("--token", default=os.environ.get("AGENT_SIGNER_TOKEN", ""),
                   help="鉴权令牌（或 AGENT_SIGNER_TOKEN），空=不鉴权")
    args = p.parse_args()
    if not args.key:
        print("❌ 需要钱包私钥：--key 0x... 或 AGENT_WALLET_KEY")
        sys.exit(1)
    wallet = Wallet.from_private_hex(args.key)
    srv = WalletSignerServer(wallet, port=args.port, token=args.token)
    srv.start(background=True)
    print(f"[signer] 地址   : {wallet.address}")
    print(f"[signer] 接口   : /info /sign /sign_text /sign_recoverable")
    print(f"[signer] 鉴权   : {'Bearer ' + args.token if args.token else '无（内网部署建议启用）'}")
    print(f"[signer] 监听中……（私钥仅存于本进程内存）")
    try:
        __import__("time").sleep(3600 * 24 * 365)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
