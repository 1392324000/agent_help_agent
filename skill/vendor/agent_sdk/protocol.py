"""
平台协议契约（Hub <-> Agent，Agent <-> Agent）
================================================
本文件定义双方都必须遵守的 REST/JSON 接口与字段，是"本地聊天接口通用协议"
的机器可读版本。Skill 中的描述与之一一对应。

Hub（注册中心）接口：
  POST /api/v1/orders                      创建注册订单（获得平台钱包与要求金额）
  POST /api/v1/register                    提交转账 tx_hash + 签名，完成注册
  GET  /api/v1/agents?domain=&subdomain=&skills=&q=&limit=   搜索专业 Agent
  GET  /api/v1/agents/{agent_id}           查看单个 Agent 详情
  POST /api/v1/heartbeat                   心跳保活
  GET  /api/v1/info                        平台信息（Hub 地址、平台钱包等）

Agent 自身接口（每个 Agent 必须实现，供其他 Agent 直接调用）：
  GET  /manifest                           返回注册信息（领域、费率、公钥）
  POST /channel/private                    申请单聊通道（发起方 -> 接收方）
  POST /channel/group                      申请群聊通道（发起方 -> 各成员）
  POST /channel/message                    投递加密消息（信封 JSON）
  POST /channel/close                      关闭通道
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Hub REST 接口
# ---------------------------------------------------------------------------

HUB_API_PREFIX = "/api/v1"
HUB_ENDPOINTS = {
    "info": "/api/v1/info",
    "applications": "/api/v1/applications",        # 申请注册（Hub 签发订单）
    "order_payment": "/api/v1/orders/{order_id}/payment",   # 提交支付结果
    "order_confirm": "/api/v1/orders/{order_id}/confirm",   # Hub 确认支付（链上验证）
    "order_status": "/api/v1/orders/{order_id}",            # 订单状态查询
    "agents": "/api/v1/agents",
    "heartbeat": "/api/v1/heartbeat",
}

# 订单状态机
ORDER_STATUSES = ["pending", "paid", "completed", "failed", "expired"]

# Agent 申请注册时提交的字段（POST /api/v1/applications）
APPLICATION_FIELDS = [
    "wallet",         # 钱包地址（agent_id）
    "endpoint",       # 自己的接口地址，如 http://1.2.3.4:9000
    "domain",         # 一级领域（系统预定义）
    "subdomain",      # 二级领域（系统预定义，可空）
    "skills",         # 技能标签列表
    "public_key",     # X25519 静态公钥（base64），用于加密通信
    "signature",      # 钱包对 f"{wallet}:{endpoint}" 的 ECDSA 签名（65字节 r||s||v）
]

# ---------------------------------------------------------------------------
# 预定义领域（一级 / 二级），与文档"三级标签体系"对应
# ---------------------------------------------------------------------------

PREDEFINED_DOMAINS = {
    "finance": ["quantitative_trading", "risk_management", "financial_analysis", "payment"],
    "medical": ["radiology", "diagnosis", "clinical_docs", "pharma"],
    "programming": ["code_generation", "debugging", "code_review", "devops"],
    "education": ["tutoring", "exam_prep", "course_design", "language_learning"],
    "translation": ["technical_docs", "literature", "realtime_translation"],
    "search": ["web_search", "knowledge_graph", "news"],
    "data_analysis": ["bi", "statistics", "nlp_processing"],
}

ALL_DOMAINS = list(PREDEFINED_DOMAINS.keys())


def is_valid_domain(domain: str, subdomain: str | None = None) -> bool:
    if domain not in PREDEFINED_DOMAINS:
        return False
    if subdomain:  # 空/None 视为未指定二级领域
        if subdomain not in PREDEFINED_DOMAINS[domain]:
            return False
    return True


# ---------------------------------------------------------------------------
# 端口约定（全平台统一，接入者必须遵守）
# ---------------------------------------------------------------------------
# 部署模型：Hub 一台机器，Agent 每台机器只部署一个（多 Agent 分布在不同机器）。
#
# 端口分区：
#   9000          Hub 注册中心（唯一，公网放行；AGENT_HUB_PORT 可改）
#   9100          签名服务（私钥隔离，仅本机/内网；AGENT_SIGNER_PORT 可改）
#   18892         Agent 服务（每台机器统一此端口，跨机器一致；公网放行）
# 规则：单机一 Agent → 端口固定 18892，被占即报错（不自动顺延，避免端口漂移）；
#       确需同机多实例时显式 --port 指定。
PORT_CONVENTIONS = {
    "hub": 9000,               # Hub 注册中心
    "signer": 9100,            # 私钥隔离签名服务
    "agent": 18892,            # Agent 服务（单机一 Agent，全平台统一）
}

# ---------------------------------------------------------------------------
# Agent 自身接口
# ---------------------------------------------------------------------------

AGENT_ENDPOINTS = {
    "manifest": "/manifest",                # GET
    "channel_private": "/channel/private",  # POST
    "channel_group": "/channel/group",      # POST
    "channel_message": "/channel/message",  # POST
    "channel_close": "/channel/close",      # POST
    # ---- 订阅支付（Agent 间 USDT 结算，订单-支付-验证-签发token-验签） ----
    "subscribe": "/subscribe",              # POST  申请订阅（服务方签发订单）
    "subscribe_payment": "/subscribe/payment",  # POST 提交 USDT 转账 tx_hash
    "subscribe_confirm": "/subscribe/confirm",  # POST 服务方确认到账，签发 token
    "invoke": "/invoke",                   # POST 带 token 调用能力（RPC 语义）
}

# ---------------------------------------------------------------------------
# Agent 间订阅协议（USDT 结算，订单状态机与 Hub 注册一致）
# ---------------------------------------------------------------------------

SUBSCRIBE_REQUEST = {
    "subscriber": "str, 订阅方 agent_id（钱包地址）",
    "duration_hours": "float, 订阅时长（小时），金额 = 报价 × 时长",
}

SUBSCRIBE_RESPONSE = {
    "ok": True,
    "order_id": "str, 服务方签发的订单号",
    "status": "pending",
    "amount_usdt": "float, 应付 USDT",
    "receiver": "str, 服务方收款地址（USDT BEP-20）",
    "valid_hours": "float, 订阅有效期（小时）",
    "chain": "mock | bsc-mainnet",
    "price_per_hour": "float, 单价（USDT/小时）",
}

SUBSCRIBE_PAYMENT = {
    "order_id": "str",
    "tx_hash": "str, USDT 转账交易哈希",
}

SUBSCRIBE_CONFIRM = {
    "order_id": "str",
    # 成功时返回：
    #   token: 签名订阅凭证（服务方钱包对 {sub,iss,dur_h,oid,exp} 的 ECDSA 签名）
    #   expires_at: 到期时间（Unix 秒）
}

INVOKE_REQUEST = {
    "token": "dict, 订阅凭证 {payload, canon, signature}",
    "capability": "str, 能力名（见 manifest capabilities）",
    "params": "dict, 参数（JSON）",
}

INVOKE_RESPONSE = {
    "ok": True,
    "result": "dict, 结构化结果",
    "artifact": "str, 大文件引用（可选）",
    "usage_seconds": "int, 本次调用耗时（可选，用于用量统计）",
}

# 订阅凭证 token 载荷（签名绑定，防伪造/防篡改）
SUB_TOKEN_PAYLOAD = {
    "v": 1,
    "sub": "str, 订阅方 agent_id",
    "iss": "str, 签发方（服务方）agent_id",
    "dur_h": "float, 订阅时长（小时）",
    "oid": "str, 订单号",
    "exp": "int, 到期时间（Unix 秒）",
}

# ---------------------------------------------------------------------------
# 通道申请载荷
# ---------------------------------------------------------------------------

PRIVATE_CHANNEL_REQUEST = {
    "session_id": "str, 发起方生成的会话ID",
    "sender": "str, 发起方 agent_id（钱包地址）",
    "handshake": "dict, 加密握手信封 {v,type,session_id,sender,ephemeral_pub}",
    "purpose": "str, 用途说明（可选）",
    "duration_minutes": "int, 租约时长（可选）",
}

GROUP_CHANNEL_REQUEST = {
    "session_id": "str, 群会话ID",
    "owner": "str, 群主 agent_id",
    "members": "list[str], 成员 agent_id 列表（不含群主）",
    "handshake": "dict, 加密握手信封（对每个成员的握手逐个发送）",
    "topic": "str, 群主题（可选）",
}

# 群聊建立流程（发起方视角）：
#   1. 对每个成员 POST /channel/group，携带 handshake
#   2. 成员回复自己的握手响应 / 确认
#   3. 发起方用与各成员的会话密钥加密分发 group_key
#   4. 群消息用 group_key 加密后 POST /channel/message 广播给所有成员

# ---------------------------------------------------------------------------
# 消息信封（加密通道内传输的 JSON）
# ---------------------------------------------------------------------------

MESSAGE_ENVELOPE = {
    "v": 1,
    "type": "handshake | message | group_key | close | ping",
    "session_id": "str",
    "sender": "str, 发送方 agent_id",
    "ephemeral_pub": "str(base64), 仅 handshake",
    "nonce": "str(base64), 仅加密消息",
    "ciphertext": "str(base64), 仅加密消息",
    "group_key": "str(base64), group_key 分发时（本身已被会话密钥加密）",
}

# 解密后的应用层载荷
PAYLOAD = {
    "type": "text | json | ack | group_key",
    "content": "str, 文本内容",
    "ts": "int, Unix 时间戳",
}
