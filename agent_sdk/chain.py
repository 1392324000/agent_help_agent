"""
EVM 链操作（Agent 钱包：余额查询 / 收益转出）
====================================================
- 钱包地址 = agent_id = 资金账户：
    · 原生币（BSC=BNB / ETH / MATIC…）：注册订阅费、gas
    · USDT(ERC-20/BEP-20)：Agent 间结算币种（订阅支出 / 服务收益）
- **完整 EVM 支持**：任意 EVM 链可配（chain_id / RPC / 原生币符号 / 合约地址 / 精度 /
  浏览器前缀），预设常用链（bsc/eth/polygon/arbitrum/op/base…），环境变量可覆盖；
  交易类型支持 legacy(EIP-155) 与 type-2(EIP-1559)。
- 纯 JSON-RPC over HTTPS（urllib），零额外依赖；
  签名用 agent_sdk.wallet（ECDSA r||s||v + EIP-155/1559），RLP 手写编码。

安全边界：本模块只做本地 CLI 操作（balance / withdraw），
不作为服务内容暴露；私钥经 Wallet 持有，不出进程。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from .wallet import Wallet, keccak256

# secp256k1 阶 n（EIP-2 低 s 值要求 s <= n/2）
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


# ---------------------------------------------------------------------------
# 链配置（完整 EVM：任意链可配，预设常用链）
# ---------------------------------------------------------------------------

class ChainConfig:
    def __init__(self, name: str, chain_id: int, rpc_urls: list[str],
                 native_symbol: str = "BNB", native_decimals: int = 18,
                 usdt_contract: str = "", usdt_symbol: str = "USDT",
                 usdt_decimals: int = 18, scan_url: str = ""):
        self.name = name
        self.chain_id = chain_id
        self.rpc_urls = rpc_urls
        self.native_symbol = native_symbol          # 原生币符号（BNB/ETH/MATIC…）
        self.native_decimals = native_decimals
        self.usdt_contract = usdt_contract.lower()  # 结算代币合约（USDT 等 ERC-20）
        self.usdt_symbol = usdt_symbol
        self.usdt_decimals = usdt_decimals
        self.scan_url = scan_url.rstrip("/")        # 区块浏览器前缀（bscscan.com…）

    @property
    def native_unit(self) -> float:
        return 10 ** self.native_decimals

    @property
    def usdt_unit(self) -> float:
        return 10 ** self.usdt_decimals

    def to_dict(self) -> dict:
        return {"name": self.name, "chain_id": self.chain_id,
                "rpc_urls": self.rpc_urls, "native_symbol": self.native_symbol,
                "usdt_symbol": self.usdt_symbol, "usdt_contract": self.usdt_contract,
                "usdt_decimals": self.usdt_decimals, "scan_url": self.scan_url}


# 预设链（任意 EVM 链可追加；环境变量 AGENT_HUB_CHAIN_* 可覆盖）
CHAIN_PRESETS: dict[str, ChainConfig] = {
    "bsc": ChainConfig("BSC Mainnet", 56,
                       ["https://bsc-rpc.publicnode.com",
                        "https://bsc.publicnode.com",
                        "https://bsc-mainnet.public.blastapi.io"],
                       native_symbol="BNB",
                       usdt_contract="0x55d398326f99059ff775485246999027b3197955",
                       usdt_decimals=18, scan_url="https://bscscan.com"),
    "eth": ChainConfig("Ethereum Mainnet", 1,
                       ["https://eth.llamarpc.com",
                        "https://ethereum-rpc.publicnode.com",
                        "https://eth-mainnet.public.blastapi.io"],
                       native_symbol="ETH",
                       usdt_contract="0xdAC17F958D2ee523a2206206994597C13D831ec7",
                       usdt_decimals=6, scan_url="https://etherscan.io"),
    "polygon": ChainConfig("Polygon Mainnet", 137,
                           ["https://polygon-rpc.com",
                            "https://polygon-bor-rpc.publicnode.com"],
                           native_symbol="POL",
                           usdt_contract="0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
                           usdt_decimals=6, scan_url="https://polygonscan.com"),
    "arbitrum": ChainConfig("Arbitrum One", 42161,
                            ["https://arb1.arbitrum.io/rpc",
                             "https://arbitrum-one-rpc.publicnode.com"],
                            native_symbol="ETH",
                            usdt_contract="0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
                            usdt_decimals=6, scan_url="https://arbiscan.io"),
    "op": ChainConfig("OP Mainnet", 10,
                      ["https://mainnet.optimism.io",
                       "https://optimism-rpc.publicnode.com"],
                      native_symbol="ETH",
                      usdt_contract="0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
                      usdt_decimals=6, scan_url="https://optimistic.etherscan.io"),
    "base": ChainConfig("Base Mainnet", 8453,
                        ["https://mainnet.base.org",
                         "https://base-rpc.publicnode.com"],
                        native_symbol="ETH",
                        usdt_contract="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        usdt_decimals=6, scan_url="https://basescan.org"),
}


def load_chain(name: str = "bsc") -> ChainConfig:
    """加载链配置：预设 + 环境变量覆盖（AGENT_HUB_CHAIN_ID / AGENT_HUB_RPC_URLS /
    AGENT_HUB_NATIVE_SYMBOL / AGENT_HUB_USDT_CONTRACT / AGENT_HUB_USDT_DECIMALS /
    AGENT_HUB_USDT_SYMBOL / AGENT_HUB_SCAN_URL）。未知链名 + 无 chain_id env → 报错。"""
    cfg = CHAIN_PRESETS.get(name.lower())
    if cfg is None:
        if not os.environ.get("AGENT_HUB_CHAIN_ID"):
            raise ValueError(f"未知链 '{name}'（预设: {', '.join(CHAIN_PRESETS)}；"
                             f"自定义链请设 AGENT_HUB_CHAIN_ID + AGENT_HUB_RPC_URLS）")
        cfg = ChainConfig(name, 0, [], native_symbol="NATIVE")
    if os.environ.get("AGENT_HUB_CHAIN_ID"):
        cfg.chain_id = int(os.environ["AGENT_HUB_CHAIN_ID"])
    if os.environ.get("AGENT_HUB_RPC_URLS"):
        cfg.rpc_urls = [u.strip() for u in os.environ["AGENT_HUB_RPC_URLS"].split(",") if u.strip()]
    if os.environ.get("AGENT_HUB_NATIVE_SYMBOL"):
        cfg.native_symbol = os.environ["AGENT_HUB_NATIVE_SYMBOL"]
    if os.environ.get("AGENT_HUB_USDT_CONTRACT"):
        cfg.usdt_contract = os.environ["AGENT_HUB_USDT_CONTRACT"].lower()
    if os.environ.get("AGENT_HUB_USDT_DECIMALS"):
        cfg.usdt_decimals = int(os.environ["AGENT_HUB_USDT_DECIMALS"])
    if os.environ.get("AGENT_HUB_USDT_SYMBOL"):
        cfg.usdt_symbol = os.environ["AGENT_HUB_USDT_SYMBOL"]
    if os.environ.get("AGENT_HUB_SCAN_URL"):
        cfg.scan_url = os.environ["AGENT_HUB_SCAN_URL"].rstrip("/")
    return cfg


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

def _rpc(cfg: ChainConfig, method: str, params: list, timeout: int = 12) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = ""
    for url in cfg.rpc_urls:
        try:
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "agent-marketplace/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
            if "error" in resp:
                last_err = resp["error"].get("message", str(resp["error"]))
                continue
            return resp
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"[{cfg.name}] 所有 RPC 端点均失败: {last_err}")


# ---------------------------------------------------------------------------
# ABI 编码（balanceOf / transfer 选择器 + 32 字节参数）
# ---------------------------------------------------------------------------

def _selector(signature: str) -> str:
    """函数选择器：keccak256(signature)[:4] hex。"""
    return keccak256(signature.encode("utf-8"))[:4].hex()


def _abi_uint(value: int) -> str:
    return format(value, "064x")


def _abi_address(addr: str) -> str:
    return addr.lower().removeprefix("0x").rjust(64, "0")


def _call_contract(cfg: ChainConfig, to: str, data: str) -> int:
    resp = _rpc(cfg, "eth_call", [{"to": to, "data": "0x" + data}, "latest"])
    return int(resp["result"], 16)


# ---------------------------------------------------------------------------
# 余额查询（只读，无需私钥）
# ---------------------------------------------------------------------------

def get_balances(cfg: ChainConfig, address: str) -> dict:
    """查询原生币与结算代币（USDT）余额。"""
    address = address.lower()
    native_raw = int(_rpc(cfg, "eth_getBalance", [address, "latest"])["result"], 16)
    result = {
        "chain": cfg.name,
        "chain_id": cfg.chain_id,
        "address": address,
        "native_symbol": cfg.native_symbol,
        "native": native_raw / cfg.native_unit,
        "native_raw": native_raw,
    }
    if cfg.usdt_contract:
        token_raw = _call_contract(
            cfg, cfg.usdt_contract,
            _selector("balanceOf(address)") + _abi_address(address))
        result.update({
            "usdt_symbol": cfg.usdt_symbol,
            "usdt": token_raw / cfg.usdt_unit,
            "usdt_raw": token_raw,
        })
    return result


# ---------------------------------------------------------------------------
# RLP 编码（EIP-155 legacy / EIP-1559 type-2 交易）
# ---------------------------------------------------------------------------

def _rlp_encode(obj) -> bytes:
    if isinstance(obj, int):
        if obj == 0:
            return b"\x80"
        return _rlp_encode(obj.to_bytes((obj.bit_length() + 7) // 8, "big"))
    if isinstance(obj, bytes):
        if len(obj) == 1 and obj[0] < 0x80:
            return obj
        prefix = bytes([0x80 + len(obj)]) if len(obj) < 56 else \
            bytes([0xB7 + (len(obj).bit_length() + 7) // 8]) + \
            len(obj).to_bytes((len(obj).bit_length() + 7) // 8, "big")
        return prefix + obj
    if isinstance(obj, (list, tuple)):
        body = b"".join(_rlp_encode(x) for x in obj)
        prefix = bytes([0xC0 + len(body)]) if len(body) < 56 else \
            bytes([0xF7 + (len(body).bit_length() + 7) // 8]) + \
            len(body).to_bytes((len(body).bit_length() + 7) // 8, "big")
        return prefix + body
    raise TypeError(f"unsupported RLP type: {type(obj)}")


# ---------------------------------------------------------------------------
# 签名交易（EIP-155 legacy 与 EIP-1559 type-2，低 s 值）
# ---------------------------------------------------------------------------

def _sign_payload(wallet: Wallet, signing_fields: list, chain_id: int) -> tuple[int, int, int]:
    """对签名载荷做 recoverable ECDSA，返回 (r, s, rec_id)，s 已低值化。"""
    sig, rec = wallet.sign_recoverable(_rlp_encode(signing_fields))
    r = int(sig[2:66], 16)
    s = int(sig[66:130], 16)
    if s > _SECP256K1_N // 2:          # EIP-2 低 s 值
        s = _SECP256K1_N - s
        rec ^= 1
    return r, s, rec


def build_signed_tx(wallet: Wallet, cfg: ChainConfig, to: str, value_wei: int,
                    data_hex: str = "", gas_limit: int = 21000,
                    gas_price: int | None = None,
                    max_fee: int | None = None, max_priority_fee: int | None = None,
                    nonce: int | None = None, tx_type: int | None = None) -> str:
    """构造并签名交易，返回可广播 raw hex。

    tx_type: 0=legacy(EIP-155) 默认；2=EIP-1559。type-2 时用 max_fee/max_priority_fee。
    """
    cfg = cfg or load_chain("bsc")
    to = to.lower()
    if nonce is None:
        nonce = int(_rpc(cfg, "eth_getTransactionCount", [wallet.address, "pending"])["result"], 16)
    data = bytes.fromhex(data_hex.removeprefix("0x")) if data_hex else b""
    to_b = bytes.fromhex(to[2:])

    # 决定交易类型：显式 > 1559 参数给出 > legacy
    use_1559 = (tx_type == 2) or (tx_type is None and (max_fee is not None or max_priority_fee is not None))
    if use_1559:
        if max_fee is None:
            max_fee = int(_rpc(cfg, "eth_gasPrice", [])["result"], 16) * 2
        if max_priority_fee is None:
            try:
                max_priority_fee = int(_rpc(cfg, "eth_maxPriorityFeePerGas", [])["result"], 16)
            except Exception:
                max_priority_fee = max_fee // 10
        # type-2 签名载荷：rlp(chain_id, nonce, prio, maxfee, gas, to, value, data, [])
        signing = [cfg.chain_id, nonce, max_priority_fee, max_fee, gas_limit,
                   to_b, value_wei, data, []]
        r, s, rec = _sign_payload(wallet, signing, cfg.chain_id)
        raw = _rlp_encode([nonce, max_priority_fee, max_fee, gas_limit,
                           to_b, value_wei, data, [], rec, r, s])
        return "0x02" + raw.hex()                      # type-2 前缀
    # legacy：签名载荷 rlp(nonce, gp, gas, to, value, data, chainId, 0, 0)
    if gas_price is None:
        gas_price = int(_rpc(cfg, "eth_gasPrice", [])["result"], 16)
    signing = [nonce, gas_price, gas_limit, to_b, value_wei, data, cfg.chain_id, 0, 0]
    r, s, rec = _sign_payload(wallet, signing, cfg.chain_id)
    v = cfg.chain_id * 2 + 35 + rec                     # EIP-155
    raw = _rlp_encode([nonce, gas_price, gas_limit, to_b, value_wei, data, v, r, s])
    return "0x" + raw.hex()


def broadcast(cfg: ChainConfig, raw_hex: str) -> str:
    """广播交易，返回 tx_hash。"""
    resp = _rpc(cfg, "eth_sendRawTransaction", [raw_hex])
    return resp["result"]


# ---------------------------------------------------------------------------
# 高层操作（原生币 / ERC-20 结算代币）
# ---------------------------------------------------------------------------

def transfer_native(wallet: Wallet, to: str, cfg: ChainConfig | None = None,
                    amount: float | None = None, all_balance: bool = False,
                    gas_price: int | None = None, tx_type: int | None = None) -> str:
    """转出原生币（BSC=BNB / ETH…）。all_balance=True 转出全部（扣 gas）。"""
    cfg = cfg or load_chain("bsc")
    if all_balance:
        bal = get_balances(cfg, wallet.address)
        gp = gas_price or int(_rpc(cfg, "eth_gasPrice", [])["result"], 16)
        value = bal["native_raw"] - gp * 21000
        if value <= 0:
            raise ValueError("余额不足以支付 gas")
    else:
        value = int(round(amount * cfg.native_unit)) if amount is not None else 0
        if value <= 0:
            raise ValueError(f"amount（{cfg.native_symbol}）必须 > 0")
    raw = build_signed_tx(wallet, cfg, to, value, gas_limit=21000,
                          gas_price=gas_price, tx_type=tx_type)
    return broadcast(cfg, raw)


def transfer_erc20(wallet: Wallet, to: str, cfg: ChainConfig | None = None,
                   amount: float | None = None, contract: str | None = None,
                   gas_limit: int | None = None, gas_price: int | None = None,
                   tx_type: int | None = None) -> str:
    """转出结算代币（默认链配置的 USDT；可显式指定 contract）。"""
    cfg = cfg or load_chain("bsc")
    contract = (contract or cfg.usdt_contract).lower()
    if not contract:
        raise ValueError(f"[{cfg.name}] 未配置结算代币合约（AGENT_HUB_USDT_CONTRACT）")
    if amount is None or amount <= 0:
        raise ValueError("amount 必须 > 0")
    amount_raw = int(round(amount * cfg.usdt_unit))
    data = (_selector("transfer(address,uint256)") + _abi_address(to) + _abi_uint(amount_raw))
    raw = build_signed_tx(wallet, cfg, contract, 0, data_hex=data,
                          gas_limit=gas_limit or int(os.environ.get("AGENT_HUB_USDT_GAS", "100000")),
                          gas_price=gas_price, tx_type=tx_type)
    return broadcast(cfg, raw)

