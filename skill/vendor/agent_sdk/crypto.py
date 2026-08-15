"""
统一加密协议 (Agent Secure Messaging Protocol v1)
====================================================
所有接入平台的 Agent 必须使用同一套加密协议，本模块是唯一实现。

协议要点：
  1. 密钥：每个 Agent 持有静态 X25519 密钥对，注册时将公钥登记到 Hub。
  2. 握手：会话发起方生成一次性 ephemeral X25519 密钥对，
     将 ephemeral 公钥发给接收方（明文，仅用于密钥协商）。
  3. 会话密钥：ECDH(ephemeral, 对方static)  ->  HKDF-SHA256(salt=session_id)
     双方各自推导出完全相同的 32 字节会话密钥。
  4. 加密：ChaCha20-Poly1305 AEAD，12 字节随机 nonce，AD 绑定 session_id。
  5. 群聊：发起方生成 32 字节 group_key，用与每个成员的会话密钥分别加密分发；
     之后群消息统一用 group_key 加密广播，群内每个成员都可解密（轮播模式）。
  6. 编码：信封 JSON 传输，密钥/密文/公钥一律 base64。

依赖：仅 cryptography（pip install cryptography）
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTOCOL_VERSION = 1
PROTOCOL_NAME = "agent-marketplace/v1"
SESSION_KEY_BYTES = 32
NONCE_BYTES = 12
GROUP_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def random_id(prefix: str = "ses", nbytes: int = 12) -> str:
    return f"{prefix}_{secrets.token_hex(nbytes)}"


# ---------------------------------------------------------------------------
# X25519 密钥对
# ---------------------------------------------------------------------------

class KeyPair:
    """一个 Agent 的静态 X25519 密钥对。"""

    def __init__(self, private: x25519.X25519PrivateKey | None = None):
        self._private = private or x25519.X25519PrivateKey.generate()

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> "KeyPair":
        return cls(x25519.X25519PrivateKey.from_private_bytes(raw))

    @classmethod
    def from_private_b64(cls, b64: str) -> "KeyPair":
        return cls.from_private_bytes(b64d(b64))

    @property
    def private_key(self) -> x25519.X25519PrivateKey:
        return self._private

    @property
    def public_bytes(self) -> bytes:
        return self._private.public_key().public_bytes_raw()

    @property
    def public_b64(self) -> str:
        return b64e(self.public_bytes)

    @property
    def private_bytes(self) -> bytes:
        return self._private.private_bytes_raw()

    @property
    def private_b64(self) -> str:
        return b64e(self.private_bytes)

    def derive(self, peer_public_b64: str, session_id: str) -> bytes:
        """与对方公钥做 ECDH + HKDF，得到 32 字节会话密钥。"""
        peer_pub = x25519.X25519PublicKey.from_public_bytes(b64d(peer_public_b64))
        shared = self._private.exchange(peer_pub)
        return HKDF(
            algorithm=hashes.SHA256(),
            length=SESSION_KEY_BYTES,
            salt=session_id.encode("utf-8"),
            info=PROTOCOL_NAME.encode("utf-8"),
        ).derive(shared)


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class Session:
    """
    一个加密会话。双方各自持有一个 Session 实例（密钥相同），
    用于加密发送 / 解密接收消息。
    """

    def __init__(self, session_id: str, key: bytes, peer: str):
        self.session_id = session_id
        self._key = key
        self.peer = peer  # 对方 agent_id（钱包地址）
        self._aead = ChaCha20Poly1305(key)

    # -- 加密 / 解密 ------------------------------------------------------

    def encrypt_payload(self, payload: dict) -> dict:
        """加密应用层载荷，返回信封（不含握手字段）。"""
        plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        nonce = secrets.token_bytes(NONCE_BYTES)
        aad = self.session_id.encode("utf-8")
        ciphertext = self._aead.encrypt(nonce, plaintext, aad)
        return {
            "v": PROTOCOL_VERSION,
            "type": "message",
            "session_id": self.session_id,
            "sender": self.peer,
            "nonce": b64e(nonce),
            "ciphertext": b64e(ciphertext),
        }

    def decrypt_envelope(self, env: dict) -> dict:
        """解密收到的信封，返回应用层载荷。校验失败抛异常。"""
        assert env.get("session_id") == self.session_id, "session_id 不匹配"
        nonce = b64d(env["nonce"])
        ciphertext = b64d(env["ciphertext"])
        aad = self.session_id.encode("utf-8")
        plaintext = self._aead.decrypt(nonce, ciphertext, aad)
        return json.loads(plaintext.decode("utf-8"))

    def encrypt_text(self, text: str) -> dict:
        return self.encrypt_payload({"type": "text", "content": text})

    def encrypt_group_key(self, group_key: bytes) -> dict:
        return self.encrypt_payload({"type": "group_key", "group_key": b64e(group_key)})


# ---------------------------------------------------------------------------
# 握手
# ---------------------------------------------------------------------------

def make_handshake(session_id: str, sender: str, my_keys: KeyPair) -> tuple[dict, x25519.X25519PrivateKey]:
    """
    发起方生成握手信封 + 临时私钥。
    返回 (envelope, ephemeral_private)：
      envelope: {v, type:"handshake", session_id, sender, ephemeral_pub}
    临时私钥必须保密保存，用于随后 derive()。
    """
    ephemeral = x25519.X25519PrivateKey.generate()
    env = {
        "v": PROTOCOL_VERSION,
        "type": "handshake",
        "session_id": session_id,
        "sender": sender,
        "ephemeral_pub": b64e(ephemeral.public_key().public_bytes_raw()),
    }
    return env, ephemeral


def responder_session(session_id: str, my_keys: KeyPair, handshake: dict) -> Session:
    """
    接收方处理握手：用自己静态私钥 + 对方 ephemeral 公钥推导会话密钥。
    handshake 的 sender 即为对方 agent_id。
    """
    ephemeral_pub = handshake["ephemeral_pub"]
    shared = my_keys.private_key.exchange(
        x25519.X25519PublicKey.from_public_bytes(b64d(ephemeral_pub))
    )
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=SESSION_KEY_BYTES,
        salt=session_id.encode("utf-8"),
        info=PROTOCOL_NAME.encode("utf-8"),
    ).derive(shared)
    return Session(session_id, key, peer=handshake["sender"])


def initiator_session(session_id: str, my_keys: KeyPair, ephemeral_priv: x25519.X25519PrivateKey,
                      peer_public_b64: str, peer_id: str) -> Session:
    """发起方用临时私钥 + 对方静态公钥推导会话密钥。"""
    peer_pub = x25519.X25519PublicKey.from_public_bytes(b64d(peer_public_b64))
    shared = ephemeral_priv.exchange(peer_pub)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=SESSION_KEY_BYTES,
        salt=session_id.encode("utf-8"),
        info=PROTOCOL_NAME.encode("utf-8"),
    ).derive(shared)
    return Session(session_id, key, peer=peer_id)


# ---------------------------------------------------------------------------
# 群聊
# ---------------------------------------------------------------------------

class GroupSession:
    """
    群聊会话。发起方持有 group_key，并用与每个成员的会话密钥加密分发；
    群内消息用 group_key 加密后广播，任何成员（含发起方）都能解密。
    """

    def __init__(self, session_id: str, group_key: bytes, owner: str):
        self.session_id = session_id
        self._group_key = group_key
        self.owner = owner
        self._aead = ChaCha20Poly1305(group_key)

    @classmethod
    def create(cls, session_id: str, owner: str) -> "GroupSession":
        return cls(session_id, secrets.token_bytes(GROUP_KEY_BYTES), owner)

    @classmethod
    def from_group_key_b64(cls, session_id: str, group_key_b64: str, owner: str) -> "GroupSession":
        return cls(session_id, b64d(group_key_b64), owner)

    def group_key_b64(self) -> str:
        return b64e(self._group_key)

    def encrypt_text(self, text: str, sender: str) -> dict:
        nonce = secrets.token_bytes(NONCE_BYTES)
        aad = self.session_id.encode("utf-8")
        plaintext = json.dumps(
            {"type": "text", "content": text, "ts": int(__import__("time").time())},
            ensure_ascii=False,
        ).encode("utf-8")
        ciphertext = self._aead.encrypt(nonce, plaintext, aad)
        return {
            "v": PROTOCOL_VERSION,
            "type": "message",
            "session_id": self.session_id,
            "sender": sender,
            "nonce": b64e(nonce),
            "ciphertext": b64e(ciphertext),
        }

    def decrypt_envelope(self, env: dict) -> dict:
        assert env.get("session_id") == self.session_id, "session_id 不匹配"
        nonce = b64d(env["nonce"])
        ciphertext = b64d(env["ciphertext"])
        aad = self.session_id.encode("utf-8")
        plaintext = self._aead.decrypt(nonce, ciphertext, aad)
        return json.loads(plaintext.decode("utf-8"))


# ---------------------------------------------------------------------------
# 指纹（用于展示/校验）
# ---------------------------------------------------------------------------

def fingerprint(public_b64: str) -> str:
    """公钥指纹：SHA-256 前 8 字节的 hex，用于人工比对。"""
    return hashlib.sha256(b64d(public_b64)).hexdigest()[:16]
