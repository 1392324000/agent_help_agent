"""
Agent SDK —— 自主报价机制（自动定价引擎）
==========================================
让 Agent 根据「自身运行成本 + 目标利润 + 市场行情」自动定价，无需人工干预。

定价公式（成本加成 + 市场收敛）：
    base  = cost_per_hour × (1 + profit_margin) × (1 + quality_premium)
    无行情时  price = base                       （成本加成起步 —— 冷启动锚点）
    有行情时  price = max(base, median × 0.95)  （市场更高 → 跟随略低抢单；
                                                   市场更低 → 守住成本线，宁可不接）

与 Hub 的接口：
    GET  /api/v1/market/prices?domain=...   拉取市场行情（成交价分布 / 种子参考价）
    POST /api/v1/agents/{id}/pricing         提交报价（token 鉴权，Hub 校验不低于成本下限）

成本估算输入（CostEstimator）：
    - 硬件：gpu 型号 → 每小时成本（参考云厂商公开价）
    - 模型：模型名 → 每百万 token 价格 × 每小时 token 消耗
    - 数据/固定成本：自报（知识库维护、人力、带宽等）

定价引擎（PricingEngine）：
    - cost_per_hour 估算结果 + profit_margin(利润率) + quality_premium(质量溢价)
    - 可选传入 market(行情)，实现"成本 + 市场参照"自动定价

自动调价（AutoPricer）：
    - 后台线程定期：拉行情 → 算价 → 提交 Hub → 防抖（变化小于阈值不提交）
"""

from __future__ import annotations

import threading
import time

# ---------------------------------------------------------------------------
# 公允价参照表（冷启动锚点 —— 基于云厂商公开价 / 模型 API 公开价，非拍脑袋）
# ---------------------------------------------------------------------------

# 硬件每小时成本（USD/小时，参考主流云 GPU 按需价，CPU 仅作兜底）
HARDWARE_COSTS = {
    "h100": 3.50,   # 云 H100 按需约 $3.5/h
    "a100": 1.50,   # A100 80G 按需约 $1.5/h
    "a10": 0.80,
    "v100": 0.90,
    "l4": 0.45,
    "t4": 0.35,
    "cpu": 0.05,    # 纯 CPU 轻量服务
    "none": 0.0,    # 无自建硬件（纯 API 调用）
}

# 模型推理成本（USD/百万 token，输入+输出混合估算，参考各厂商公开价）
MODEL_API_COSTS = {
    "gpt-4o": 5.0,
    "claude-sonnet": 6.0,
    "claude-haiku": 1.0,
    "gpt-4o-mini": 0.5,
    "llama-70b": 1.0,
    "qwen-72b": 0.8,
    "deepseek": 0.3,
    "local": 0.0,   # 本地模型（已含在硬件成本）
    "none": 0.0,
}

# 平台费（未来如果平台抽成 1%，可在此预留；当前为 0）
PLATFORM_FEE_RATE = 0.0


class CostEstimator:
    """成本估算器：硬件 + 模型 API + 数据 + 固定成本 → cost_per_hour（USD/小时）。

    用法：
        cost = CostEstimator(gpu="a100", model="gpt-4o",
                             tokens_per_hour=2_000_000).estimate()
    """

    def __init__(self, gpu: str = "none", model: str = "none",
                 tokens_per_hour: int = 0, data_cost: float = 0.0,
                 fixed_cost: float = 0.0, hardware_cost: float | None = None):
        self.gpu = gpu
        self.model = model
        self.tokens_per_hour = max(0, int(tokens_per_hour))
        self.data_cost = max(0.0, float(data_cost))       # 知识库/数据订阅均摊
        self.fixed_cost = max(0.0, float(fixed_cost))     # 人力/维护/带宽均摊
        self.hardware_cost = hardware_cost                # 显式覆盖（有云账单时直接填）

    def estimate(self) -> float:
        """估算每小时运行成本（USD）。"""
        if self.hardware_cost is not None:
            hw = max(0.0, float(self.hardware_cost))
        else:
            hw = HARDWARE_COSTS.get(self.gpu, 0.0)
        api = MODEL_API_COSTS.get(self.model, 0.0) * self.tokens_per_hour / 1_000_000
        return round(hw + api + self.data_cost + self.fixed_cost, 6)

    def breakdown(self) -> dict:
        """成本构成明细（展示用）。"""
        if self.hardware_cost is not None:
            hw = max(0.0, float(self.hardware_cost))
        else:
            hw = HARDWARE_COSTS.get(self.gpu, 0.0)
        api = MODEL_API_COSTS.get(self.model, 0.0) * self.tokens_per_hour / 1_000_000
        return {
            "gpu": self.gpu, "model": self.model,
            "tokens_per_hour": self.tokens_per_hour,
            "hardware": round(hw, 6),
            "model_api": round(api, 6),
            "data_cost": round(self.data_cost, 6),
            "fixed_cost": round(self.fixed_cost, 6),
            "cost_per_hour": round(hw + api + self.data_cost + self.fixed_cost, 6),
        }


class PricingEngine:
    """定价引擎：成本加成 + 市场收敛。

    compute(market=None) -> price（USDC/小时）
        market: Hub 行情响应（{"median": x, "p25":..., "reference":...} 或 None）
    """

    def __init__(self, cost_per_hour: float, profit_margin: float = 0.3,
                 quality_premium: float = 0.0, floor_ratio: float = 1.0):
        self.cost_per_hour = max(0.0, float(cost_per_hour))
        self.profit_margin = max(0.0, float(profit_margin))     # 目标利润率（如 0.3 = 30%）
        self.quality_premium = max(0.0, float(quality_premium))  # 质量溢价（信誉/benchmark 支撑）
        self.floor_ratio = max(0.0, float(floor_ratio))          # 成本下限系数（默认 1.0 = 不低于成本）

    def base_price(self) -> float:
        """成本加成价（无行情时的报价）。"""
        return round(self.cost_per_hour * (1 + self.profit_margin) * (1 + self.quality_premium), 6)

    def floor_price(self) -> float:
        """成本下限（低于此价宁可不接）。"""
        return round(self.cost_per_hour * self.floor_ratio, 6)

    def compute(self, market: dict | None = None) -> float:
        """计算报价。"""
        base = self.base_price()
        if not market:
            return base
        median = market.get("median")
        if not median:
            return base
        median = float(median)
        # 市场更高 → 跟随市场略低 5% 抢单（但不低于自己成本加成）
        if median > base:
            return round(max(base, median * 0.95), 6)
        # 市场低于成本线 → 守住底线（不参与亏本竞争）
        return round(max(self.floor_price(), base), 6)

    def suggest_with_market(self, market: dict | None) -> dict:
        """带市场参照的完整定价说明（展示用）。"""
        price = self.compute(market)
        return {
            "cost_per_hour": self.cost_per_hour,
            "profit_margin": self.profit_margin,
            "quality_premium": self.quality_premium,
            "base_price": self.base_price(),
            "market_median": (market or {}).get("median"),
            "market_count": (market or {}).get("count", 0),
            "suggested_price": price,
            "note": "成本加成起步；有市场行情时向市场收敛（不低于成本线）",
        }


class AutoPricer:
    """自动调价器：后台线程定期「拉行情 → 算价 → 提交 Hub」，无需人工干预。

    用法：
        pricer = AutoPricer(client, cost_per_hour=0.002, domain="finance")
        pricer.start(background=True)   # 每 interval 秒自动调价
    """

    def __init__(self, client, cost_per_hour: float, profit_margin: float = 0.3,
                 quality_premium: float = 0.0, domain: str | None = None,
                 subdomain: str | None = None, interval: float = 600.0,
                 min_delta: float = 0.0001, on_change=None):
        self.client = client
        self.engine = PricingEngine(cost_per_hour, profit_margin, quality_premium)
        self.domain = domain
        self.subdomain = subdomain
        self.interval = max(30.0, float(interval))   # 调价周期（秒），防价格战抖动
        self.min_delta = min_delta                   # 变化小于此值不提交（防抖）
        self.on_change = on_change                   # 回调 fn(new_price, detail)
        self._last_price: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 单次调价（可手动调用） ----

    def tick(self) -> dict:
        """执行一次：拉行情 → 算价 → 提交。返回结果 dict。"""
        market = None
        try:
            market = self.client.market_prices(domain=self.domain,
                                               subdomain=self.subdomain).get("market")
        except Exception as e:
            market = None  # 行情不可达：退回成本加成（可用性优先）
        detail = self.engine.suggest_with_market(market)
        price = detail["suggested_price"]
        changed = self._last_price is None or abs(price - self._last_price) >= self.min_delta
        if changed:
            resp = self.client.submit_pricing(
                cost_per_hour=detail["cost_per_hour"],
                price=price,
                profit_margin=detail["profit_margin"],
                quality_premium=detail["quality_premium"],
            )
            self._last_price = price
            detail["submitted"] = resp.get("ok", False)
            detail["hub_message"] = resp.get("message") or resp.get("error")
            if self.on_change:
                try:
                    self.on_change(price, detail)
                except Exception:
                    pass
        else:
            detail["submitted"] = False
            detail["hub_message"] = "价格未变化，跳过提交（防抖）"
        detail["market"] = market
        return detail

    # ---- 后台循环 ----

    def start(self, background: bool = True) -> "AutoPricer":
        def _loop():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:
                    print(f"[pricer] ⚠ 调价失败: {e}")
                self._stop.wait(self.interval)
        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        if background:
            print(f"[pricer] 自动调价已启动（每 {self.interval/60:.0f} 分钟一次，"
                  f"成本 {self.engine.cost_per_hour} USDC/h，利润率 {self.engine.profit_margin:.0%}）")
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
