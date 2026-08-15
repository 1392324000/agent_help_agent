"""
BSC 链上验证（注册时验证"向平台钱包转账了微量 BNB"）
=======================================================
两种模式：
  - 真实模式：通过 BSC 公共 RPC（JSON-RPC over HTTPS）查询交易，
    验证 to == 平台钱包、value >= 阈值、交易成功且已有确认数。
  - Mock 模式（开发/演示）：AGENT_HUB_MOCK_CHAIN=1 时启用，
    任何 agent 可调用 POST /api/v1/mock/transfer 标记一笔"模拟转账"，
    Hub 据此放行注册。无需真实 RPC 与真实资金。

阈值：微量 BNB，默认 0.0001 BNB（1e14 wei）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

DEFAULT_PLATFORM_WALLET = "0x97ab218e3eaf04977ffc21f8d817d44e7a9dd1c4"
MIN_BNB_WEI = int(os.environ.get("AGENT_HUB_MIN_BNB_WEI", "100000000000000"))  # 0.0001 BNB
# BSC USDT（Binance-Peg）合约地址与精度（Agent 间结算币种）
USDT_CONTRACT = os.environ.get("AGENT_HUB_USDT_CONTRACT",
                               "0x55d398326f99059ff775485246999027b3197955").lower()
USDT_DECIMALS = 18
# keccak256("Transfer(address,address,uint256)") 前 4 字节 topic0
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# BSC RPC 端点（与 ~/.fly/capsules/skill/skill_wallet_management_v1_0_0 的 wallet_transfer.py 一致）
BSC_RPC_URLS = [
    "https://bsc-rpc.publicnode.com",
    "https://bsc.publicnode.com",
    "https://bsc-mainnet.public.blastapi.io",
]
BSC_RPC = os.environ.get("AGENT_HUB_BSC_RPC", BSC_RPC_URLS[0])
REQUIRED_CONFIRMATIONS = int(os.environ.get("AGENT_HUB_CONFIRMS", "1"))


class ChainVerifier:
    def __init__(self, platform_wallet: str = DEFAULT_PLATFORM_WALLET,
                 mock: bool | None = None):
        self.platform_wallet = platform_wallet.lower()
        self.mock = (
            os.environ.get("AGENT_HUB_MOCK_CHAIN", "0") == "1"
            if mock is None else mock
        )
        # mock 模式下记录"已转账"的 tx_hash -> (from, amount_wei, ts)
        self._mock_transfers: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Mock 模式
    # ------------------------------------------------------------------

    def mock_record_transfer(self, tx_hash: str, from_addr: str,
                             amount_wei: int = MIN_BNB_WEI) -> None:
        self._mock_transfers[tx_hash.lower()] = {
            "from": from_addr.lower(),
            "amount_wei": amount_wei,
            "ts": int(time.time()),
        }

    def _verify_mock(self, tx_hash: str, expected_from: str) -> tuple[bool, str]:
        rec = self._mock_transfers.get(tx_hash.lower())
        if not rec:
            return False, f"mock 链上未找到该交易 {tx_hash}（演示模式请先 POST /api/v1/mock/transfer）"
        if rec["from"] != expected_from.lower():
            return False, f"转账发起方 {rec['from']} 与注册钱包 {expected_from} 不一致"
        if rec["amount_wei"] < MIN_BNB_WEI:
            return False, f"转账金额不足（{rec['amount_wei']} wei < {MIN_BNB_WEI} wei）"
        return True, "ok"

    # ------------------------------------------------------------------
    # 真实模式（BSC RPC）
    # ------------------------------------------------------------------

    def _rpc(self, method: str, params: list) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        last_err = None
        for url in BSC_RPC_URLS:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "agent-marketplace/1.0"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                if data.get("error"):
                    last_err = data["error"]
                    continue
                return data
            except Exception as e:
                last_err = str(e)
                continue
        raise RuntimeError(f"所有 BSC RPC 端点均失败: {last_err}")

    def _verify_rpc(self, tx_hash: str, expected_from: str) -> tuple[bool, str]:
        try:
            tx = self._rpc("eth_getTransactionByHash", [tx_hash])
        except Exception as e:
            return False, f"RPC 查询失败：{e}"
        if not tx.get("result"):
            return False, f"链上未找到交易 {tx_hash}"
        r = tx["result"]
        to = (r.get("to") or "").lower()
        if to != self.platform_wallet:
            return False, f"收款地址 {to} 不是平台钱包 {self.platform_wallet}"
        if r.get("from", "").lower() != expected_from.lower():
            return False, f"交易发起方 {r['from']} 与注册钱包 {expected_from} 不一致"
        try:
            value = int(r.get("value", "0x0"), 16)
        except ValueError:
            return False, "交易 value 无法解析"
        if value < MIN_BNB_WEI:
            return False, f"转账金额不足（{value} wei < {MIN_BNB_WEI} wei）"
        try:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        except Exception as e:
            return False, f"查询回执失败：{e}"
        status = receipt.get("result", {}).get("status")
        if status not in ("0x1", "0x01"):
            return False, "交易未成功（status != 0x1）"
        try:
            block_number = int(r.get("blockNumber", "0x0"), 16)
            latest = int(self._rpc("eth_blockNumber", [])["result"], 16)
        except Exception:
            block_number, latest = 0, 0
        confirms = latest - block_number
        if confirms < REQUIRED_CONFIRMATIONS:
            return False, f"确认数不足（{confirms} < {REQUIRED_CONFIRMATIONS}）"
        return True, f"ok（确认数 {confirms}）"

    # ------------------------------------------------------------------

    def verify_payment(self, tx_hash: str, expected_from: str) -> tuple[bool, str]:
        """验证转账：收款=平台钱包、金额达标、交易成功。返回 (通过, 信息)。"""
        tx_hash = tx_hash.strip()
        if not tx_hash.startswith("0x"):
            return False, "tx_hash 必须是 0x 开头的交易哈希"
        if self.mock:
            return self._verify_mock(tx_hash, expected_from)
        return self._verify_rpc(tx_hash, expected_from)

    # ------------------------------------------------------------------
    # USDT（BEP-20）转账验证 —— Agent 间结算币种
    # ------------------------------------------------------------------

    def mock_record_usdt(self, tx_hash: str, from_addr: str, to_addr: str,
                         amount_usdt: float) -> None:
        """Mock 模式：模拟一笔 USDT 转账（Agent 间订阅支付）。"""
        self._mock_transfers[f"usdt:{tx_hash.lower()}"] = {
            "from": from_addr.lower(),
            "to": to_addr.lower(),
            "amount_usdt": float(amount_usdt),
            "ts": int(time.time()),
        }

    def _verify_usdt_mock(self, tx_hash: str, expected_from: str,
                          expected_to: str, min_amount: float) -> tuple[bool, str]:
        rec = self._mock_transfers.get(f"usdt:{tx_hash.lower()}")
        if not rec:
            return False, f"mock 链上未找到该 USDT 交易 {tx_hash}（演示模式请先 mock_record_usdt）"
        if rec["from"] != expected_from.lower():
            return False, f"转账发起方 {rec['from']} 与订阅方 {expected_from} 不一致"
        if rec["to"] != expected_to.lower():
            return False, f"收款地址 {rec['to']} 不是服务方 {expected_to}"
        if rec["amount_usdt"] < min_amount:
            return False, f"USDT 金额不足（{rec['amount_usdt']} < 需 {min_amount}）"
        return True, "ok"

    def _verify_usdt_rpc(self, tx_hash: str, expected_from: str,
                         expected_to: str, min_amount: float) -> tuple[bool, str]:
        """真实模式：从交易回执中解析 USDT 合约 Transfer 事件。"""
        try:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        except Exception as e:
            return False, f"RPC 查询回执失败：{e}"
        r = receipt.get("result")
        if not r:
            return False, f"链上未找到交易 {tx_hash}"
        if r.get("status") not in ("0x1", "0x01"):
            return False, "交易未成功（status != 0x1）"
        logs = r.get("logs", [])
        for log in logs:
            if (log.get("address") or "").lower() != USDT_CONTRACT:
                continue
            topics = log.get("topics", [])
            if not topics or topics[0].lower() != TRANSFER_TOPIC0:
                continue
            # topics[1]=from, topics[2]=to（32 字节，去除 0x 前缀的 24 个零 + 40 位地址）
            def _addr(t):
                return "0x" + t[-40:].lower()
            frm, to = _addr(topics[1]), _addr(topics[2])
            try:
                amount = int(log.get("data", "0x0"), 16) / (10 ** USDT_DECIMALS)
            except ValueError:
                continue
            if frm != expected_from.lower():
                return False, f"USDT 发起方 {frm} 与订阅方 {expected_from} 不一致"
            if to != expected_to.lower():
                return False, f"USDT 收款地址 {to} 不是服务方 {expected_to}"
            if amount < min_amount:
                return False, f"USDT 金额不足（{amount} < 需 {min_amount}）"
            return True, f"ok（USDT {amount} 已到账）"
        return False, f"交易中未找到 USDT Transfer 事件（合约 {USDT_CONTRACT[:10]}…）"

    def verify_usdt_transfer(self, tx_hash: str, expected_from: str,
                             expected_to: str, min_amount: float) -> tuple[bool, str]:
        """验证 USDT（BEP-20）转账：发起方、收款方、金额。返回 (通过, 信息)。"""
        tx_hash = tx_hash.strip()
        if not tx_hash.startswith("0x"):
            return False, "tx_hash 必须是 0x 开头的交易哈希"
        if self.mock:
            return self._verify_usdt_mock(tx_hash, expected_from, expected_to, min_amount)
        return self._verify_usdt_rpc(tx_hash, expected_from, expected_to, min_amount)
