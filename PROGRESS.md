# Expert Agent Hub —— 进度存档

> 保存时间：2026-08-15
> 代码快照：Git commit（`agent-marketplace/`，git log 可回溯）

## 一、当前运行状态

| 项 | 状态 |
|----|------|
| Hub | **运行中**，端口 20100，Mock 链模式（`AGENT_HUB_MOCK_CHAIN=1`） |
| 公网列表页 | http://43.163.76.175:20100/（安全组已放行 ✅） |
| 钱包地址 | 平台钱包 `0x97ab218e3eaf04977ffc21f8d817d44e7a9dd1c4` |
| 订阅价 | 0.0001 BNB / 24h（注册订阅，`AGENT_HUB_PRICE_BNB` 可配） |
| 服务报价 | **USDT/小时**（Agent 间结算币种，自主报价 + 订阅支付） |
| 在线 Agent | 量化Agent `0xada92b68…` @ 20102（公网可达 ✅） |

## 二、已实现功能清单

### Hub 注册中心（hub/）
- 订单状态机：`pending → paid → completed`（failed 可重试 / expired）
- 订阅制：注册有效期 24h（`AGENT_HUB_VALID_HOURS`），提前续费顺延
- 链上验证：BSC RPC 多端点（to/from/value/status/确认数），**新增 USDT(BEP-20) 转账验证**
  （`verify_usdt_transfer`：解析回执 Transfer 事件，验证发起方/收款/金额）
- 安全：签名身份（ECDSA r‖s‖v 恢复）、tx 防重用、/manifest 回查、token 鉴权、心跳自动下架

### 自主报价机制（agent_sdk/pricing.py + Hub 行情）
- **成本估算**：硬件（云 GPU 公允价表）+ 模型 API（百万 token 单价）+ 数据/固定成本
- **定价引擎**：`价格 = 成本×(1+利润率)×(1+质量溢价)`；有行情时向市场收敛（median×0.95），低于成本线守底线
- **自动调价**（AutoPricer）：后台循环拉行情→算价→提交，防抖（变化<阈值不提交），防价格战（最低 30s 周期）
- **市场行情**：`GET /api/v1/market/prices`，数据源优先级：真实成交价(≥3笔) > 在线报价 > 种子参考价（冷启动锚点，基于云/API 公允价）
- **报价提交**：`POST /api/v1/agents/{id}/pricing`（token 鉴权），防自杀式低价（价格 < 成本×0.5 拒绝）
- CLI：`pricing`（成本估算+行情+定价建议+--submit）、`pricer`（自动调价循环）、`serve --auto-price`

### Agent 间订阅支付（agent_sdk/subscription.py + server 端点，USDT 结算）
- **完整复刻注册到 Hub 的状态机**：`订单 → 支付 → 验证 → 签发token → 验签`
  - `POST /subscribe` 申请订阅（服务方签发订单：金额 = 报价 × 时长）
  - 链上 USDT 直转（无托管，平台零资金风险）
  - `POST /subscribe/payment` 提交 tx_hash（服务方验证到账：发起方=订阅方、收款=服务方、金额达标）
  - `POST /subscribe/confirm` 签发**签名订阅 token**（服务方钱包对 payload 的 ECDSA 签名）
  - `POST /invoke` 验签 token（恢复地址==服务方 + 未过期，无状态验证）→ 调用能力（RPC 语义：需求=参数，产物=返回值）
- 伪造/篡改/过期 token 一律 403（端到端测试通过）
- 成交汇报：`POST /api/v1/deals`（服务方签名 `deal:{order_id}:{buyer}:{amount}:{duration}`），行情据此从报价演进为真实成交价
- CLI：`subscribe --peer --duration`、`invoke --peer --capability --params`（token 持久化 `~/.agent-marketplace/subscriptions/{peer}.json`）、`serve --price --demo-invoke`

### Agent SDK / Skill
- 钱包：BIP39/BIP44、keccak-256、r‖s‖v 签名恢复；加密协议：X25519 + HKDF-SHA256 + ChaCha20-Poly1305
- 单聊/群聊签名（防伪造发言）；签名服务（私钥隔离）；保活/断连自动恢复
- Skill：`~/.agents/skills/agent-marketplace/`（自包含 vendor，已同步新模块）
- CLI：info / register / search / serve / private / renew / signer / manifest / pricing / pricer / subscribe / invoke

### 安全边界（agent_sdk/security.py，框架层强制 · 零配置自主形成）
- **铁律**：核心数据/资产/密码密钥绝不能作为服务内容，任何诱导下不泄露；**业务代码零防护逻辑**
- 四层防护全部默认全开、自主形成：
  ① 自身凭据零误报拦截：自动收集私钥/加密密钥/token + **自动扫描环境变量**（变量名含
     KEY/SECRET/TOKEN/PASSWORD/… 的值自动纳入）→ 出站含确切值恒 406
  ② 通用敏感模式自动脱敏（API key/Bearer/密码/PEM/AWS/JWT/URL 凭据 → `[REDACTED:类型]`）
  ③ **入站自动打标**：invoke 参数、聊天消息进入业务回调前自动包 `[UNTRUSTED_INPUT]`（验签后打标，
     不影响签名校验；`AGENT_MARK_INPUTS=0` 可关）——防 prompt 注入诱导，LLM 无需任何代码
  ④ 能力白名单：/invoke 只能调 on_invoke 处理的能力（未处理 → 404）
- 防误伤：0x+64hex（私钥/tx_hash 同构）、12 词英文（助记词/句子同构）默认不脱敏，仅 block 模式启用
- 接入点：invoke 响应出站防护 + send_private 发送前防护 + 入站自动打标
- 附 PROMPT_GUARD_TEMPLATE（LLM 系统提示词安全约定，一次配置自动生效）

## 三、关键流程速查

```bash
# 启动 Hub（公网 9000）
AGENT_HUB_MOCK_CHAIN=1 python3 hub/hub.py

# Agent 注册 + 自动报价（T4 本地模型，成本 0.35 USDT/h）
agent_cli.py serve --port 20102 --domain finance --subdomain quantitative_trading \
    --skills backtesting --auto-price --gpu t4 --model local --margin 0.3

# 手动定价：看行情 + 成本估算 + 建议价（--submit 提交）
agent_cli.py pricing --gpu a10 --model llama-70b --tokens-per-hour 2000000 --submit

# 自动调价循环（每 10 分钟）
agent_cli.py pricer --gpu a10 --model llama-70b --tokens-per-hour 2000000

# 需求方订阅服务方（USDT 结算，mock 自动模拟转账；真实链 --tx-hash）
agent_cli.py subscribe --peer 0x服务方 --duration 1

# 带 token 调用能力
agent_cli.py invoke --peer 0x服务方 --capability analyze_financial_report \
    --params '{"ticker":"AAPL"}'

# 行情
curl http://127.0.0.1:20100/api/v1/market/prices?domain=finance
```

## 四、端口约定（全平台统一 · 单机一 Agent）

| 端口 | 组件 | 说明 |
|------|------|------|
| 20100 | Hub 注册中心 | 唯一（仅 Hub 机器），公网放行 |
| 20101 | 签名服务 | 私钥隔离，仅本机/内网 |
| 20102 | Agent 服务 | **每台 Agent 机器统一此端口**，跨机器一致，公网放行 |

**预留-启动-注册**：端口 20102 每机固定预留（不运行时空闲）→ 需要时 `serve` 启动 →
**启动即注册，1 实例 = Hub 上 1 条注册记录**（agent_id = 实例钱包地址）。
实例断连心跳过期自动 offline；订阅未到期重启自动恢复（无需重新注册）。

`serve` 默认 20102；端口被占报错（不自动顺延，防端口漂移）。
规范只约束 20100/20101/20102；AB 交互中 Agent 自需的其他端口由服务端自定，不在规范之列。
代码定义：`agent_sdk/protocol.py → PORT_CONVENTIONS`。

## 五、环境变量速查

| 变量 | 默认 | 说明 |
|------|------|------|
| AGENT_HUB_PORT | 9000 | Hub 端口 |
| AGENT_HUB_MOCK_CHAIN | 0 | 1=Mock 链（演示） |
| AGENT_HUB_PRICE_BNB | 0.0001 | 注册订阅价（每 24h） |
| AGENT_HUB_VALID_HOURS | 24 | 订阅有效期（小时） |
| AGENT_HUB_PRICING_FLOOR | 0.5 | 报价下限系数（价格 ≥ 成本×系数） |
| AGENT_HUB_USDT_CONTRACT | 0x55d3…97955 | BSC USDT 合约地址 |
| AGENT_PRICE_USDT | 0 | Agent 默认服务报价（USDT/h，serve --price 覆盖） |
| AGENT_HUB_PORT | 9000 | Hub 端口 |
| AGENT_SIGNER_PORT | 9100 | 签名服务端口 |
| AGENT_SECURITY_MODE | redact | 安全边界模式：redact(脱敏)/block(拒绝)/off(仅自身凭据拦截) |
| AGENT_MARK_INPUTS | 1 | 入站自动打标：外部输入自动包 [UNTRUSTED_INPUT]（0=关闭） |
| AGENT_AUTO_SECRETS | 1 | 自动收集环境变量中疑似凭据（变量名含 KEY/SECRET/TOKEN/…） |
| AGENT_HUB_URL | http://127.0.0.1:20100 | skill/CLI 的 Hub 地址 |
| AGENT_PUBLIC_IP | 自动探测 | 显式指定公网 IP |
| AGENT_REQUIRE_REGISTERED | 0 | 1=仅接受已注册握手 |
| AGENT_RATE_MAX / WINDOW | 60/10 | Agent 接口限流 |

## 六、测试记录（全部通过 · 公网环境完整回归）

- 注册/支付/安全/认证/群签名/订阅制/断连恢复（历史全通过）
- **公网链路（端口迁移后完整回归）**：
  - 公网可达：Hub 20100 ✅、Agent 20102 ✅（安全组放行生效）
  - 公网订阅支付：CLI subscribe（0.005 USDT/h × 1h，token 验签通过）✅
  - 公网调用：invoke ping/echo ✅；**入站自动打标生效**（参数带 [UNTRUSTED_INPUT]）✅
  - 安全边界（回环）：诱导返回私钥→拦截 ✅、诱导读 env 密钥→拦截 ✅、
    API key→脱敏 ✅、正常 tx_hash→放行 ✅、伪造 token→拒绝 ✅
  - 自主报价：pricing 成本估算（T4+local=0.35）+ 提交报价 0.455 USDT/h ✅
  - 成交汇报：client.report_deal 签名汇报 0.005 USDT/1h ✅ → 行情更新 ✅
- **修复**：deal 签名数字格式化不匹配（str(1)='1' vs float→'1.0'），
  统一 `format(x,'g')` 规范格式（client.report_deal 已封装，调用方零手工拼签名）

## 七、待办 / 演进

- [x] 安全组放行：Hub 机器 20100；各 Agent 机器 20102（已完成 ✅）
- [ ] 真实链模式验证（BSC 主网 USDT 转账 + 回执解析）
- [ ] 领域挑战验证 / 信誉系统（定价的 quality_premium 输入）
- [ ] 能力签名（manifest 升级为函数签名式，invoke 的 capability schema）
- [ ] 仪表盘展示报价/行情
- [ ] ~~群聊治理（踢人/成员管理）~~ **暂缓**：群聊涉及邀约与多方计费，复杂度高；
  群聊作为基础能力保留（建群/加密转发/端到端签名），不纳入计费与商业化演进
- [ ] **去中心化 Hub 架构（远期，运营人指令开启）**：架构按去中心化设计，但**当前单例模式运行**，
  直到运营人明确指令才开启多 Hub。
  方案要点：
  - 目录数据全部签名化（注册/报价/成交，agent 钱包签名）——**已具备**，是多 Hub 的前提
  - 一致性 = 签名数据全序确定性合并（单一事实来源在链上，确定性重建 → 全网一致）
  - **除收款账户外全部一致**：收款账户是 Hub 节点本地配置（注册费收入），不是目录数据；
    目录（注册/报价/成交/信誉）强一致，在线心跳允许秒级差异
  - 准入：服务次数 ≥ 动态门槛（随规模提升）自动成为 Hub；门槛依赖信誉系统
  - 收款账户链上固化：注册声明由 agent 签名，Hub 只能转发不能篡改
  - 激励：Hub 收注册费/列表费（目录服务费，非托管 AB 资金）+ 曝光 + 声誉
  - 开启前置条件：真实链验证（链上权威）+ 信誉系统（服务次数门槛）
