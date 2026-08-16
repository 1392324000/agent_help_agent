"""
BSC 链操作（Agent 钱包：余额查询 / 收益转出）
====================================================
- 钱包地址 = agent_id = 资金账户：
    · BNB：注册订阅费（向平台钱包转微量 BNB，AGENT_HUB_PRICE_BNB/24h）
    · USDT(BEP-20)：Agent 间结算币种（订阅支出 / 服务收益）
- 纯 JSON-RPC over HTTPS（urllib），零额外依赖；
  签名用 agent_sdk.wallet（ECDSA r||s||v + EIP-155），RLP 手写编码。

安全边界：本模块只做本地 CLI 操作（balance / withdraw），
不作为服务内容暴露；私钥经 Wallet 持有，不出进程。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from .wallet import Wallet, keccak256

# BSC 主网参数（与 hub/chain_verify.py 一致）
BSC_RPC_URLS = [
    "https://bsc-rpc.publicnode.com",
    "https://bsc.publicnode.com",
    "https://bsc-mainnet.public.blastapi.io",
]
BSC_RPC = os.environ.get("AGENT_HUB_BSC_RPC", BSC_RPC_URLS[0])
USDT_CONTRACT = os.environ.get("AGENT_HUB_USDT_CONTRACT",
                               "0x55d398326f99059ff775485246999027b3197955").lower()
USDT_DECIMALS = 18
CHAIN_ID = int(os.environ.get("AGENT_HUB_CHAIN_ID", "56"))          # BSC 主网
GAS_LIMIT_BNB = 21000                                              # 原生 BNB 转账
GAS_LIMIT_USDT = int(os.environ.get("AGENT_HUB_USDT_GAS", "100000"))  # ERC-20 调用
WEI = 10 ** 18

# secp256k1 阶 n（EIP-2 低 s 值要求 s <= n/2）
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

def _rpc(method: str, params: list, timeout: int = 12) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = ""
    for url in BSC_RPC_URLS:
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
    raise RuntimeError(f"所有 BSC RPC 端点均失败: {last_err}")


# ---------------------------------------------------------------------------
# ABI 编码（仅需 balanceOf / transfer 两个函数选择器 + 32 字节参数）
# ---------------------------------------------------------------------------

def _selector(signature: str) -> str:
    """函数选择器：keccak256(signature)[:4] hex。"""
    return keccak256(signature.encode("utf-8"))[:4].hex()


def _abi_uint(value: int) -> str:
    return format(value, "064x")


def _abi_address(addr: str) -> str:
    return addr.lower().removeprefix("0x").rjust(64, "0")


def _call_contract(to: str, data: str) -> int:
    resp = _rpc("eth_call", [{"to": to, "data": "0x" + data}, "latest"])
    return int(resp["result"], 16)


# ---------------------------------------------------------------------------
# 余额查询（只读，无需私钥）
# ---------------------------------------------------------------------------

def get_balances(address: str) -> dict:
    """查询 BNB 与 USDT(BEP-20) 余额。"""
    address = address.lower()
    bnb_wei = int(_rpc("eth_getBalance", [address, "latest"])["result"], 16)
    usdt_raw = _call_contract(
        USDT_CONTRACT,
        _selector("balanceOf(address)") + _abi_address(address),
    )
    return {
        "address": address,
        "bnb": bnb_wei / WEI,
        "bnb_wei": bnb_wei,
        "usdt": usdt_raw / (10 ** USDT_DECIMALS),
        "usdt_raw": usdt_raw,
    }


# ---------------------------------------------------------------------------
# RLP 编码（EIP-155 交易）
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
# 签名交易（EIP-155，低 s 值）
# ---------------------------------------------------------------------------

def build_signed_tx(wallet: Wallet, to: str, value_wei: int,
                    data_hex: str = "", gas_limit: int = GAS_LIMIT_BNB,
                    gas_price: int | None = None, nonce: int | None = None,
                    chain_id: int = CHAIN_ID) -> str:
    """构造并签名 EIP-155 交易，返回可广播的 raw hex。"""
    to = to.lower()
    if nonce is None:
        nonce = int(_rpc("eth_getTransactionCount", [wallet.address, "pending"])["result"], 16)
    if gas_price is None:
        gas_price = int(_rpc("eth_gasPrice", [])["result"], 16)
    data = bytes.fromhex(data_hex.removeprefix("0x")) if data_hex else b""

    # 签名载荷：rlp(nonce, gp, gas, to, value, data, chainId, 0, 0)
    signing = [nonce, gas_price, gas_limit, bytes.fromhex(to[2:]), value_wei, data]
    payload = _rlp_encode(signing + [chain_id, 0, 0])
    sig, rec = wallet.sign_recoverable(payload)
    r = int(sig[2:66], 16)
    s = int(sig[66:130], 16)
    if s > _SECP256K1_N // 2:
        s = _SECP256K1_N - s
        rec ^= 1
    v = chain_id * 2 + 35 + rec                      # EIP-155
    raw = _rlp_encode([nonce, gas_price, gas_limit,
                       bytes.fromhex(to[2:]), value_wei, data, v, r, s])
    return "0x" + raw.hex()


def broadcast(raw_hex: str) -> str:
    """广播交易，返回 tx_hash。"""
    resp = _rpc("eth_sendRawTransaction", [raw_hex])
    return resp["result"]


# ---------------------------------------------------------------------------
# 高层操作
# ---------------------------------------------------------------------------

def transfer_bnb(wallet: Wallet, to: str, amount_bnb: float | None = None,
                 all_balance: bool = False, gas_price: int | None = None) -> str:
    """转出 BNB。all_balance=True 转出全部（扣 gas）。"""
    if all_balance:
        bal = get_balances(wallet.address)
        gp = gas_price or int(_rpc("eth_gasPrice", [])["result"], 16)
        value = bal["bnb_wei"] - gp * GAS_LIMIT_BNB
        if value <= 0:
            raise ValueError("余额不足以支付 gas")
    else:
        value = int(round(amount_bnb * WEI)) if amount_bnb is not None else 0
        if value <= 0:
            raise ValueError("amount_bnb 必须 > 0")
    raw = build_signed_tx(wallet, to, value, gas_limit=GAS_LIMIT_BNB, gas_price=gas_price)
    return broadcast(raw)


def transfer_usdt(wallet: Wallet, to: str, amount_usdt: float,
                  gas_price: int | None = None) -> str:
    """转出 USDT(BEP-20) 到地址（0 值 BNB 交易，data=transfer(to,amount)）。"""
    amount_raw = int(round(amount_usdt * (10 ** USDT_DECIMALS)))
    if amount_raw <= 0:
        raise ValueError("amount_usdt 必须 > 0")
    data = (_selector("transfer(address,uint256)") + _abi_address(to) + _abi_uint(amount_raw))
    raw = build_signed_tx(wallet, USDT_CONTRACT, 0, data_hex=data,
                          gas_limit=GAS_LIMIT_USDT, gas_price=gas_price)
    return broadcast(raw)
