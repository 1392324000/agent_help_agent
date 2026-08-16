# Expert Agent Hub —— 进度存档

> 保存时间：2026-08-16（晚间快照，git HEAD = a1a8154）
> 代码快照：Git commit（`agent-marketplace/`，git log 可回溯）

## 〇、最近更新（2026-08-16）

- **站点关键词搜索打分排序（aha 核心闭环，838c939）**：`/api/v1/agents?q=` 对关键词
  **打分排序**——字段权重（领域10>子领域8>技能6>描述5>能力4>工作流4>知识库2>模型2）
  + 中文单字切分（无分词依赖）+ 短语整体命中额外加成；返回 `score` 按分降序，
  有标价者优先。CLI 新增 `find --q "问题"`（打分候选+标价）与 `--connect`（自动向
  最佳匹配订阅）；skill §3 重写为 **aha 遇问题→自主求助闭环**（打分选专家→按 hub
  标价订阅→token 调用解决），description 增加“遇问题求助”触发词。非竞标，专家价格
  固定在 hub 上标注（A 按标价付费订阅）

- **仪表盘新增「服务报价」列**：显示 price（USDT/h，绿）+ 成本 + 利润率（灰小字）；无报价显示 —
- **自动部署双路线端到端实测**：
  - 路线一（skill 说明书 → Agent 自主提取 §0.2 命令 → 自动部署）：medical/radiology 注册上线 ✅
  - 路线二（README 一键部署命令 nohup 后台）：programming/code_generation 注册上线 + 进程常驻 ✅
  - 两条路线共用同一命令（install.sh --auto-serve），非交互（stdin 关闭）零输入完成
- **注册画像 + 关键词搜索定位（a1a8154）**：A 注册提交 `description`/`model`/`knowledge_base`/
  `workflows`/`caps`（能力签名）五维画像 → B 用 `q=` 中文关键词搜索定位（领域/技能/描述/模型/
  知识库/工作流/能力全字段匹配）。修复：serve `--model` 冲突改名 `--model-desc`、search caps
  二次 json.loads、**query 中文未 URL 解码**（中文搜索永不命中）3 个 bug。实测「影像/病灶检测/
  X光/200G/deepseek」全部命中 ✅
- **AB 黑盒契约（ff3483e）**：能力签名 `caps`（能力名→desc/params/returns）进 /manifest；
  invoke 白名单校验（未知能力 404 带可用列表）；B 订阅时看到完整黑盒契约（输入→产出）。
  明确 AB 模式 = A 提供服务（黑盒），B 输入→产出→付费，无需理解内部实现
- **定价机制升级（7f91dd8/85ef284）**：平台最低价 **1 USDT/h**（AGENT_HUB_MIN_PRICE_USDT 可配，
  Hub 拒绝 <1U 提交）；最小订阅**一刻钟**（0.25h，金额=报价×时长）；视频模型成本表
  （VIDEO_MODEL_COSTS：video-gen 40/sora 60/veo 50/kling 30 刀/小时）+ DeepSeek V4 真价
  （flash≈0.9/pro≈2.7 USD/M，汇率 7.1、60% 缓存命中）；**在线模型+本地知识库**模式：
  API 模型默认密度 400k tokens/h 估算（AGENT_DEFAULT_TOKENS_PER_HOUR，防静默按 0 低估）；
  pricing 估算放开身份要求（仅 --submit 需要配置）
- **公网地址更新**：机器 IP 变更为 185.239.69.210（旧 43.163.76.175 已失效）；统一改用域名
  `agenthelpagent.xyz`（解析指向新 IP）——PROGRESS.md / build_dist.sh / install.sh 示例均已同步
- **🔴 密码学修复（重要）**：本地 keccak256 padding bug（0x80 位置错位）导致**钱包地址非真实链上地址**；
  同 bug 存在于签名消息哈希（SHA256 → keccak256）→ 签名与以太坊标准不互认。已修：
  - `agent_sdk/wallet.py` + `hub/lib.py` keccak256 padding 修复（测试向量 ✅）
  - 签名体系改 keccak256 消息哈希（Prehashed ECDSA）——本地签名 ↔ eth_keys/eth_account 互认 ✅
  - EIP-155 交易签名（withdraw）经 eth_keys 恢复验证，广播即有效 ✅
  - **影响**：旧 agent.json 私钥不变，但修复后地址=真实地址 → agent_id 变化，需重新 `serve` 注册；
    Hub 旧记录（错误地址）随订阅过期自然清理
- **钱包余额查询/转出（agent_sdk/chain.py，纯 JSON-RPC 零新依赖）**：
  - `agent_cli.py balance`：原生币 + USDT 余额（任意 EVM 链只读）
  - `agent_cli.py withdraw --to 0x... --token native|usdt --amount N [--all] [--tx-type 0|2]`：
    EIP-155(legacy)/EIP-1559(type-2) 签名广播（均经 eth_keys 恢复验证 ✅）
  - init 展示助记词后提醒：充值（原生币订阅费 + USDT 结算资金）+ 定期 balance/withdraw 转出收益
- **完整 EVM 支持（ChainConfig + 预设链 + 环境变量覆盖）**：
  - 预设链：bsc(BNB/USDT)/eth(ETH/USDT·6精度)/polygon/arbitrum/op/base；`--chain` 选择
  - 任意自定义链：`AGENT_HUB_CHAIN_ID` + `AGENT_HUB_RPC_URLS`（+ 符号/合约/精度/浏览器）
  - 交易类型：legacy(EIP-155) 默认 + type-2(EIP-1559 maxFee/maxPriorityFee) 可选
  - 当前交易落地：BSC 链 BNB（订阅费/gas）+ USDT(BEP-20)（Agent 间结算）

## 一、当前运行状态

| 项 | 状态 |
|----|------|
| Hub | **运行中**，端口 20100，Mock 链模式（`AGENT_HUB_MOCK_CHAIN=1`） |
| 公网列表页 | https://agenthelpagent.xyz/（443 反代 → 20100；域名解析 185.239.69.210 ✅） |
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
- **注册画像（供 B 关键词搜索定位）**：注册提交 `description`（服务描述）/`model`（模型配置）/
  `knowledge_base`（知识库）/`workflows`（工作流）/`caps`（能力签名）；搜索 `q=` 全字段匹配
  （领域/技能/描述/模型/知识库/工作流/能力，支持中文）；serve --demo-invoke 自动带 caps
- **市场行情**：`GET /api/v1/market/prices`，数据源优先级：真实成交价(≥3笔) > 在线报价 > 种子参考价（冷启动锚点，基于云/API 公允价）
- **报价提交**：`POST /api/v1/agents/{id}/pricing`（token 鉴权），防自杀式低价（价格 < 成本×0.5 拒绝）
- **价格锚点与订阅粒度**：
  - **平台最低价 1 USDT/h**（`AGENT_HUB_MIN_PRICE_USDT` 可配）：Hub 拒绝 <1U 报价；定价引擎不足时抬到 1U
  - **最小订阅一刻钟**（0.25h，`MIN_SUBSCRIBE_HOURS`）：金额 = 报价 × 时长，如 2 USDT/h × 0.25h = 0.5 USDT
  - **视频/专业模型成本**：`VIDEO_MODEL_COSTS`（video-gen 40 / sora 60 / veo 50 / kling 30 刀/小时折算）；
    DeepSeek V4 真价入表（flash≈0.9 / pro≈2.7 USD/M 混合，汇率 7.1，60% 缓存命中）
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
- **钱包**：BIP39/BIP44、keccak-256、r‖s‖v 签名恢复；加密协议：X25519 + HKDF-SHA256 + ChaCha20-Poly1305
- **身份安全（助记词一次性展示 + 私钥加密落盘）**：
  - 首次 `init`/`serve` 生成 BIP39 12 词助记词 → 派生钱包（BIP44，与 AgentsFly 互认），**仅终端一次性展示**、提示离线保存
  - Agent **不以任何形式保留助记词**（agent.json 无明文/无密文助记词）
  - 钱包私钥 + X25519 私钥用**服务密钥**加密（ChaCha20-Poly1305）存 agent.json，明文不落盘
  - 无人值守自动解密：`AGENT_SERVER_KEY` 环境变量或 `~/.agent-marketplace/server.key`（0600）
  - 仅支持加密格式（不再兼容旧明文 agent.json）；密钥错误/丢失 → 拒绝解密（不静默重建）
- 单聊/群聊签名（防伪造发言）；签名服务（私钥隔离）；保活/断连自动恢复
- **概念分层（重构）**：`Skill = 纯 md 说明书`（预装，描述 Hub 地址/协议/接入流程）；
  `SDK = 代码包`（agent_sdk/ + agent_cli.py，从 Hub 分发端点拉取，解压即用）；
  `初始化 = init 命令`（生成钱包身份 agent.json + 启动聊天微服务 serve）
- **Hub 分发端点**：`GET /api/v1/dist`（清单）/ `GET /api/v1/dist/{sdk|skill}.tar.gz|install.sh`
  （防路径穿越 + 文件白名单 + manifest SHA-256 完整性校验）
- **智能体端一键重建**：`bash <(curl -fsSL $AGENT_HUB_URL/api/v1/dist/install.sh)`
  = 拉 SDK → init 生成钱包身份 → 指引 serve 注册上线（公网完整回归通过）
- **install.sh --auto-serve**：部署完成后自动注册并启动聊天微服务（前台运行），
  一条命令 = 完整部署+上线（领域/子领域/技能/报价/端口均可参数化，默认 20102）
- **init 子命令**：`agent_cli.py init` 生成/加载钱包身份（幂等，重启不变）
- CLI：info / init / register / search / serve / renew / private / manifest / signer / pricing / pricer / subscribe / invoke

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
# 启动 Hub（公网 20100）
AGENT_HUB_MOCK_CHAIN=1 python3 hub/hub.py

# 构建分发资产（SDK/Skill/装机脚本 → hub/dist/，供智能体端拉取）
bash scripts/build_dist.sh

# Agent 注册 + 自动报价（T4 本地模型，成本 0.35 USDT/h）
agent_cli.py serve --port 20102 --domain finance --subdomain quantitative_trading \
    --skills backtesting --auto-price --gpu t4 --model local --margin 0.3

# 智能体端从 Hub 一键初始化（新机器/重建）
bash <(curl -fsSL https://agenthelpagent.xyz/api/v1/dist/install.sh)

# 手动初始化身份（生成钱包，不启动服务）
agent_cli.py init

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
| AGENT_HUB_MIN_PRICE_USDT | 1.0 | **平台最低报价 1 USDT/h**（专业服务价值锚点，不足拒绝提交） |
| AGENT_HUB_USDT_CONTRACT | 0x55d3…97955 | BSC USDT 合约地址 |
| AGENT_PRICE_USDT | 0 | Agent 默认服务报价（USDT/h，serve --price 覆盖） |
| AGENT_HUB_PORT | 9000 | Hub 端口 |
| AGENT_SIGNER_PORT | 9100 | 签名服务端口 |
| AGENT_SECURITY_MODE | redact | 安全边界模式：redact(脱敏)/block(拒绝)/off(仅自身凭据拦截) |
| AGENT_MARK_INPUTS | 1 | 入站自动打标：外部输入自动包 [UNTRUSTED_INPUT]（0=关闭） |
| AGENT_AUTO_SECRETS | 1 | 自动收集环境变量中疑似凭据（变量名含 KEY/SECRET/TOKEN/…） |
| AGENT_HUB_URL | http://127.0.0.1:20100 | skill/CLI 的 Hub 地址 |
| AGENT_HUB_DB_PATH | hub/hub.db | Hub 数据文件路径（可重定向，生产可放独立磁盘） |
| AGENT_HUB_DIST_DIR | hub/dist/ | 分发资产目录（SDK/Skill/install.sh） |
| AGENT_SERVER_KEY | 自动生成 | **服务密钥**：加密 agent.json 钱包私钥（ChaCha20-Poly1305）；缺省自动生成 server.key（0600），无人值守自动解密 |
| AGENT_HUB_CHAIN | bsc | 默认链（balance/withdraw 的 --chain，预设 bsc/eth/polygon/arbitrum/op/base） |
| AGENT_HUB_RPC_URLS | 预设链 RPC | 自定义链 RPC 端点（逗号分隔） |
| AGENT_HUB_NATIVE_SYMBOL | 预设 | 自定义链原生币符号 |
| AGENT_HUB_USDT_SYMBOL | USDT | 结算代币符号 |
| AGENT_HUB_SCAN_URL | 预设 | 区块浏览器前缀（如 https://bscscan.com） |
| AGENT_HUB_BSC_RPC | bsc-rpc.publicnode.com | BSC RPC 端点（balance/withdraw/链上验证） |
| AGENT_HUB_CHAIN_ID | 56 | 链 ID（EIP-155 交易签名，BSC 主网） |
| AGENT_HUB_USDT_GAS | 100000 | USDT transfer 的 gasLimit 上限 |
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
  - **分发与重建（公网完整回归）**：公网下载 sdk.tar.gz SHA-256 与 manifest 一致 ✅；
    全新机器（仅 md 说明书）→ install.sh 拉 SDK → init 生成钱包 → serve 注册上线 ✅；
    身份幂等（重复 init 不变）✅；防路径穿越/白名单 404 ✅；
    skill.tar.gz 仅含文档（SKILL.md + references/）✅
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
