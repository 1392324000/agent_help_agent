"""
EVM 钱包（BSC/以太坊格式，与 AgentsFly 钱包体系兼容）
====================================================
每个接入平台的 Agent 必须拥有自己的钱包：
  - 钱包地址（0x + 40 hex）作为全局唯一 agent_id
  - 注册时向平台钱包转账微量 BNB，Hub 链上验证到账后放行注册
  - 注册请求用钱包私钥签名，Hub 验证签名以证明"钱包是你的"

与现有体系兼容：
  - 环境有 eth_account + mnemonic 时（如 ~/.fly/venv/bin/python3），
    钱包可由 BIP39 助记词生成/恢复（BIP44 m/44'/60'/0'/0/0），
    同一助记词可在 AgentsFly 钱包体系与平台间互认。
  - 签名统一为以太坊标准格式：r(32) || s(32) || v(1) = 65 字节 hex，
    v 内嵌 rec_id（v = rec_id + 27 时自动归一），Hub 据此唯一恢复地址。
  - keccak-256 纯 Python 实现（EVM 系用原始 Keccak，非 NIST SHA3）。
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

# 可选依赖：eth_account + mnemonic（~/.fly/venv 已装，system python 可能没有）
try:
    from eth_account import Account
    HAS_ETH_ACCOUNT = True
except ImportError:
    Account = None  # type: ignore
    HAS_ETH_ACCOUNT = False

try:
    from mnemonic import Mnemonic
    HAS_MNEMONIC = True
except ImportError:
    Mnemonic = None  # type: ignore
    HAS_MNEMONIC = False


# ---------------------------------------------------------------------------
# Keccak-256（EVM 兼容）
# ---------------------------------------------------------------------------

_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

_ROT = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
]


def _rotl(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def keccak_f1600(state: list[int]) -> list[int]:
    for rc in _KECCAK_RC:
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(state[x + 5 * y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = b[x + 5 * y] ^ ((~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y])
        state[0] ^= rc
    return state


def keccak256(data: bytes) -> bytes:
    rate = 136  # 1088 bits
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded.append(0x80)
    state = [0] * 25
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[i * 8:(i + 1) * 8], "little")
        state = keccak_f1600(state)
    return b"".join(s.to_bytes(8, "little") for s in state[:4])


# ---------------------------------------------------------------------------
# 钱包
# ---------------------------------------------------------------------------

def generate_mnemonic(strength: int = 128) -> str | None:
    """生成 BIP39 英文助记词（默认 12 词）。缺 mnemonic 依赖返回 None。"""
    if not HAS_MNEMONIC:
        return None
    return Mnemonic("english").generate(strength=strength)


class Wallet:
    CURVE = ec.SECP256K1

    def __init__(self, private_key: ec.EllipticCurvePrivateKey | None = None):
        self._priv = private_key or ec.generate_private_key(ec.SECP256K1())

    # -- 创建 / 加载 ------------------------------------------------------

    @classmethod
    def generate(cls, mnemonic: str | None = None) -> "Wallet":
        """生成钱包。传入助记词则按 BIP44 恢复（与 AgentsFly 互认）。"""
        if mnemonic and HAS_ETH_ACCOUNT:
            Account.enable_unaudited_hdwallet_features()
            acct = Account.from_mnemonic(mnemonic.strip())
            return cls.from_private_hex(acct.key.hex())
        return cls()

    @classmethod
    def from_mnemonic(cls, mnemonic: str) -> "Wallet":
        """按 BIP44 从助记词恢复钱包（与 AgentsFly 钱包体系互认）。"""
        return cls.generate(mnemonic)

    @classmethod
    def from_private_hex(cls, hex_key: str) -> "Wallet":
        raw = bytes.fromhex(hex_key.removeprefix("0x"))
        return cls(ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256K1()))

    def to_mnemonic(self) -> str | None:
        """导出助记词（需要 eth_account + mnemonic，否则返回 None）。"""
        if not (HAS_ETH_ACCOUNT and HAS_MNEMONIC):
            return None
        acct = Account.from_key(bytes.fromhex(self.private_hex[2:]))
        m = Mnemonic("english")
        # BIP44 反推助记词：从私钥构造种子不现实，仅支持从助记词创建的钱包
        return getattr(acct, "_mnemonic", None) or None

    # -- 属性 ------------------------------------------------------------

    @property
    def private_hex(self) -> str:
        num = self._priv.private_numbers().private_value
        return "0x" + num.to_bytes(32, "big").hex()

    @property
    def public_key_bytes(self) -> bytes:
        """未压缩公钥 65 字节（0x04 || x || y）。"""
        pub = self._priv.public_key().public_numbers()
        return b"\x04" + pub.x.to_bytes(32, "big") + pub.y.to_bytes(32, "big")

    @property
    def address(self) -> str:
        """EVM 地址：keccak256(pubkey[1:]) 后 20 字节，小写 hex。"""
        h = keccak256(self.public_key_bytes[1:])
        return "0x" + h[-20:].hex()

    # -- 签名（标准 r||s||v 65 字节，v 内嵌 rec_id）-----------------------

    def sign(self, message: bytes) -> str:
        """ECDSA 签名，返回 0x + r(32) + s(32) + v(1) = 65 字节 hex。"""
        der = self._priv.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        v = self._find_rec_id(r, s, message)
        return "0x" + r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex() + v.to_bytes(1, "big").hex()

    def sign_text(self, text: str) -> str:
        return self.sign(text.encode("utf-8"))

    def sign_recoverable(self, message: bytes) -> tuple[str, int]:
        """返回 (signature_hex, rec_id)。signature 为 65 字节 r||s||v。"""
        sig = self.sign(message)
        rec_id = int(sig[-2:], 16)
        return sig, rec_id

    def sign_text_recoverable(self, text: str) -> tuple[str, int]:
        return self.sign_recoverable(text.encode("utf-8"))

    def _find_rec_id(self, r: int, s: int, message: bytes) -> int:
        pub = self.public_key_bytes
        true_x = int.from_bytes(pub[1:33], "big")
        true_y = int.from_bytes(pub[33:], "big")
        for rec_id in range(4):
            Q = _recover_point(r, s, message, rec_id)
            if Q and Q[0] == true_x and Q[1] == true_y:
                return rec_id
        raise ValueError("无法确定 rec_id")

    def verify(self, signature_hex: str, message: bytes) -> bool:
        try:
            r, s, v = parse_signature(signature_hex)
            der = utils.encode_dss_signature(r, s)
            self._priv.public_key().verify(der, message, ec.ECDSA(hashes.SHA256()))
            return True
        except Exception:
            return False

    def verify_text(self, signature_hex: str, text: str) -> bool:
        return self.verify(signature_hex, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# 签名解析 / 地址恢复
# ---------------------------------------------------------------------------

def parse_signature(signature_hex: str) -> tuple[int, int, int]:
    """解析签名 -> (r, s, v)。支持：
      1. 0x + 65 字节 r||s||v（标准以太坊格式，v=27/28 时自动归一为 0/1）
      2. 0x + DER 编码（旧格式兜底）
    """
    raw = bytes.fromhex(signature_hex.removeprefix("0x"))
    if len(raw) == 65:
        r = int.from_bytes(raw[:32], "big")
        s = int.from_bytes(raw[32:64], "big")
        v = raw[64]
        if v >= 27:          # 以太坊旧式 v
            v -= 27
        elif v >= 35:        # EIP-155 v
            v = (v - 35) % 2
        return r, s, v
    r, s = utils.decode_dss_signature(raw)
    return r, s, -1


def recover_address_from_signature(signature_hex: str, message: bytes,
                                   rec_id: int | None = None) -> str | None:
    """从 ECDSA 签名恢复出钱包地址（0x...），失败返回 None。

    rec_id 缺省时从签名 v 字段提取（标准格式）；仍未提供则枚举四个候选，
    多解场景仅返回第一个，注册场景请使用带 rec_id（v）的 65 字节签名。
    """
    try:
        r, s, v = parse_signature(signature_hex)
    except Exception:
        return None
    if not (1 <= r < _N and 1 <= s < _N):
        return None
    if rec_id is None:
        rec_id = v
    candidates = [rec_id] if rec_id >= 0 else range(4)
    for rid in candidates:
        Q = _recover_point(r, s, message, rid)
        if Q is None:
            continue
        pub = b"\x04" + Q[0].to_bytes(32, "big") + Q[1].to_bytes(32, "big")
        return "0x" + keccak256(pub[1:])[-20:].hex()
    return None


def parse_recoverable_signature(signature: str) -> tuple[str, int]:
    """解析注册提交的签名 -> (signature_hex, rec_id)。兼容：
      1. "0x<65字节 r||s||v>"（标准格式，rec_id 从 v 提取）
      2. "0x<der>:<rec_id>"（旧格式兜底）
    """
    if ":" in signature:
        sig_hex, rec = signature.rsplit(":", 1)
        return sig_hex.strip(), int(rec)
    sig = signature.strip()
    try:
        r, s, v = parse_signature(sig)
        return sig, (v if v >= 0 else -1)
    except Exception:
        return sig, -1


def _recover_point(r: int, s: int, message: bytes, rec_id: int) -> tuple[int, int] | None:
    """标准 secp256k1 公钥恢复：Q = r⁻¹·(s·R − e·G)。
    R.x = r + (rec_id//2)·n，R.y 奇偶 = rec_id & 1。"""
    x = r + (rec_id // 2) * _N
    if x >= _P:
        return None
    try:
        y = _sqrt_y(x)
    except ValueError:
        return None
    if y % 2 != rec_id % 2:
        y = _P - y
    R = (x, y)
    try:
        e = int.from_bytes(_sha256(message), "big")
        eG = _point_mul(_G, e)
        sR = _point_mul(R, s)
        Q = _point_mul(_point_add(sR, (eG[0], (-eG[1]) % _P)), pow(r, -1, _N))
    except (ZeroDivisionError, ValueError):
        return None
    return Q


# ---------------------------------------------------------------------------
# secp256k1 有限域运算（公钥恢复用）
# ---------------------------------------------------------------------------

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _sha256(data: bytes) -> bytes:
    h = hashes.Hash(hashes.SHA256())
    h.update(data)
    return h.finalize()


def _sqrt_y(x: int) -> int:
    """y² = x³ + 7 (mod p)，p ≡ 3 (mod 4)，用 (p+1)/4 次幂开方。"""
    y = pow((pow(x, 3, _P) + 7) % _P, (_P + 1) // 4, _P)
    if (y * y) % _P != (pow(x, 3, _P) + 7) % _P:
        raise ValueError("x 不是曲线上的有效坐标")
    return y


def _point_add(P: tuple[int, int] | None, Q: tuple[int, int] | None) -> tuple[int, int] | None:
    if P is None:
        return Q
    if Q is None:
        return P
    x1, y1 = P
    x2, y2 = Q
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if P == Q:
        lam = (3 * x1 * x1) * pow(2 * y1, -1, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _point_mul(P: tuple[int, int] | None, k: int) -> tuple[int, int] | None:
    """double-and-add 标量乘法。"""
    R: tuple[int, int] | None = None
    while k:
        if k & 1:
            R = _point_add(R, P)
        P = _point_add(P, P)
        k >>= 1
    return R


# ---------------------------------------------------------------------------
# 平台钱包（AgentsFly 用户目录中的现有钱包）
# ---------------------------------------------------------------------------

def platform_wallet_from_users_dir() -> str | None:
    """从 ~/.fly/users/ 读取平台钱包地址（第一个 0x 目录），没有则返回 None。"""
    users_dir = os.path.expanduser("~/.fly/users")
    try:
        for entry in sorted(os.listdir(users_dir)):
            if entry.startswith("0x") and os.path.isdir(os.path.join(users_dir, entry)):
                return entry.lower()
    except OSError:
        pass
    return None
