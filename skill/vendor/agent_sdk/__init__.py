"""
Agent Marketplace SDK
=====================
去中心化智能体协作平台 —— Agent 侧开发包。

快速开始：
    from agent_sdk import HubClient, AgentServer, KeyPair, Wallet

    wallet = Wallet.generate()          # 自己的钱包（agent_id = wallet.address）
    keys = KeyPair()                    # X25519 静态密钥对（加密通信）
    client = HubClient(HUB_URL, wallet, keys)
    server = AgentServer(wallet, keys, domain="finance", subdomain="quantitative_trading",
                         skills=["backtesting"], port=9000)
    server.on_private_message = lambda sender, ses, payload: print("收到:", payload)
    server.start(background=True)
    client.register_flow(endpoint=server_endpoint, domain="finance", ...)
"""

from .crypto import (
    KeyPair, Session, GroupSession, make_handshake,
    responder_session, initiator_session, fingerprint, random_id,
)
from .wallet import Wallet, recover_address_from_signature, keccak256
from .client import HubClient, HubError
from .server import AgentServer
from .signer import WalletSignerServer, WalletSignerClient
from . import protocol
from . import net

__version__ = "0.1.0"
__all__ = [
    "HubClient", "AgentServer", "KeyPair", "Session", "GroupSession",
    "Wallet", "WalletSignerServer", "WalletSignerClient", "HubError",
    "protocol", "net", "fingerprint", "random_id",
    "make_handshake", "responder_session", "initiator_session",
]
