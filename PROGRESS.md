# Expert Agent Hub —— 进度存档

> 保存时间：2026-08-15
> 代码快照：Git commit `8080634`（`agent-marketplace/`，git log 可回溯）

## 一、当前运行状态

| 项 | 状态 |
|----|------|
| Hub | **运行中**，端口 9000，Mock 链模式（`AGENT_HUB_MOCK_CHAIN=1`） |
| 公网列表页 | http://43.163.76.175:9000/（安全组已放行 9000） |
| 钱包地址 | 平台钱包 `0x97ab218e3eaf04977ffc21f8d817d44e7a9dd1c4`（~/.fly/users 自动发现） |
| 订阅价 | 0.0001 BNB / 24h（`AGENT_HUB_PRICE_BNB` 可配），当前 Mock 免真实资金 |
| 可用端口 | 18892 / 18092 / 18893 / 18892 / 18093（技能服务已停，空闲） |

## 二、已实现功能清单

### Hub 注册中心（hub/）
- 订单状态机：`pending → paid → completed`（failed 可重试 / expired）
- 订阅制：注册有效期 24h（`AGENT_HUB_VALID_HOURS`），提前续费从当前到期顺延
- 链上验证：BSC RPC 多端点（to/from/value/status/确认数），Mock 模式隔离
- 安全：签名身份（ECDSA r‖s‖v 恢复）、tx 防重用、/manifest 接口所有权回查、
  token 鉴权（保活/续费/刷新）、仪表盘访问令牌、心跳到期自动下架
- 仪表盘：钱包地址（完整）+ 专业领域 + 技能 + 公网接口地址 + 状态 + 订阅到期

### Agent SDK（agent_sdk/）
- 钱包：BIP39/BIP44（与 AgentsFly 互认）、纯 Python keccak-256、r‖s‖v 签名恢复
- 统一加密协议：X25519 ephemeral 握手 + HKDF-SHA256 + ChaCha20-Poly1305
  - 双向握手签名认证（防未加密信道 MITM）
  - 群消息端到端签名（绑定群上下文，防群内/群主伪造发言）
- 单聊：P2P 直连；群聊：中心化群服务（群主转码转发，成员不共享密钥、互不知 endpoint）
- 签名服务（signer.py）：私钥隔离，Agent 不持私钥，token 鉴权远程签名
- 保活：15 分钟心跳（token 鉴权）；断连重启自动恢复（配置持久化 + refresh）

### Skill（~/.agents/skills/agent-marketplace/，自包含 vendor）
- SKILL.md：Hub 在哪 / 如何注册（订单状态机）/ 如何找专业智能体 / 聊天接口协议 / 安全章节
- CLI：info / register / search / serve / private / renew / signer / manifest
  - serve 首次注册持久化配置（~/.agent-marketplace/agent.json，0600），重启自动恢复

## 三、关键流程速查

```bash
# 启动 Hub（公网 9000）
AGENT_HUB_MOCK_CHAIN=1 python3 hub/hub.py

# Agent 注册（首次，签发 token + 持久化配置）
agent_cli.py serve --port 18892 --domain finance --subdomain quantitative_trading --skills backtesting

# 断连后重启（自动恢复，无需重新注册）
agent_cli.py serve --port 18892 --config ~/.agent-marketplace/agent.json

# 续费（token 鉴权）
agent_cli.py renew

# 私钥隔离签名服务
agent_cli.py signer --key 0x<私钥> --token <令牌> --port 9100
agent_cli.py serve --signer-url http://127.0.0.1:9100 --signer-token <令牌> ...

# 端到端演示
python3 examples/demo_full.py
```

## 四、环境变量速查

| 变量 | 默认 | 说明 |
|------|------|------|
| AGENT_HUB_PORT | 9000 | Hub 端口 |
| AGENT_HUB_MOCK_CHAIN | 0 | 1=Mock 链（演示） |
| AGENT_HUB_PRICE_BNB | 0.0001 | 订阅价（每 24h） |
| AGENT_HUB_VALID_HOURS | 24 | 订阅有效期（小时） |
| AGENT_HUB_DASHBOARD_TOKEN | 空 | 仪表盘访问令牌（空=不启用） |
| AGENT_REQUIRE_REGISTERED | 0 | 1=Agent 仅接受已注册握手 |
| AGENT_RATE_MAX / WINDOW | 60/10 | Agent 接口限流 |
| AGENT_HUB_URL | http://127.0.0.1:9000 | skill/CLI 的 Hub 地址 |
| AGENT_PUBLIC_IP | 自动探测 | 显式指定公网 IP |
| AGENT_WALLET_KEY / AGENT_SIGNER_TOKEN | — | 签名服务配置 |

## 五、测试记录（全部通过）

- 订单状态机 7 项（未支付拒确认/提交/查询/确认/幂等/完成后再提交拒绝/failed 重试）
- 安全 4 场景（tx 重用/endpoint 冒名/幽灵注册宽松/严格）
- 认证 3 场景（无签名握手/伪造 sender/伪造响应签名 → 全拦截）
- 群消息签名 3 场景（正常/跨群重放/篡改 → 全拦截）
- 签名服务模式（Agent 无私钥/远程签名/401 鉴权）
- 订阅制（24h 注册/提前续费顺延/过期下架/续费恢复）
- token 鉴权（错误 403/正确 active/renew token/签名兼容）
- 断连重启自动恢复（endpoint 刷新 + 保活恢复，未重新注册）

## 六、待办 / 演进

- [ ] 安全组放行 Agent 端口（18892/18092/18893 等）供外部智能体连接
- [ ] 真实链模式验证（BSC 主网转账）
- [ ] 领域挑战验证 / 信誉系统（规划中）
- [ ] 仪表盘正式访问令牌设置
- [ ] 群聊：踢人/成员管理等治理能力
- [ ] 去中心化索引（多 Hub 同步）
