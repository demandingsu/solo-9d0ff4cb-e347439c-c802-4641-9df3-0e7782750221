"""Pydantic 请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------- 导入请求 ----------


class QuoteIn(BaseModel):
    """一条日行情。revision 为修订号，同键高修订覆盖低修订。"""

    security_code: str = Field(..., min_length=1, description="证券代码")
    trade_date: str = Field(..., description="交易日，ISO 日期 YYYY-MM-DD")
    revision: int = Field(..., ge=0, description="修订号（非负整数，越高越新）")
    open: float = Field(..., description="开盘价（原值）")
    high: float = Field(..., description="最高价（原值）")
    low: float = Field(..., description="最低价（原值）")
    close: float = Field(..., description="收盘价（原值）")
    volume: float = Field(..., description="成交量（原值）")

    @field_validator("trade_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _positive_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("价格必须为正数")
        return v

    @field_validator("volume")
    @classmethod
    def _non_negative_volume(cls, v: float) -> float:
        if v < 0:
            raise ValueError("成交量不能为负")
        return v


class QuoteImportRequest(BaseModel):
    quotes: list[QuoteIn] = Field(..., min_length=1)


class CorporateActionIn(BaseModel):
    """一条拆股/并股公告。

    ratio = 每股老股拆成的新股数：1 拆 2 填 2.0，2 并 1 填 0.5。
    """

    security_code: str = Field(..., min_length=1)
    event_no: str = Field(..., min_length=1, description="事件号")
    ex_date: str = Field(..., description="除权日 YYYY-MM-DD")
    ratio: float = Field(..., gt=0, description="比例：老股 1 股 -> 新股 ratio 股")
    revision: int = Field(..., ge=0, description="修订号（非负整数，越高越新）")
    action_type: Literal["split", "reverse_split"] = Field(
        default="split", description="split=拆股，reverse_split=并股"
    )

    @field_validator("ex_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v


class CorporateActionImportRequest(BaseModel):
    actions: list[CorporateActionIn] = Field(..., min_length=1)


# ---------- 导入响应 ----------


class ImportResponse(BaseModel):
    batch_id: int = Field(..., description="本次导入生成的入库批次号")
    payload_kind: Literal["quotes", "corporate_actions"]
    row_count: int = Field(..., description="实际新写入的行数（完全重复行不计）")
    received: int = Field(..., description="请求中收到的行数")


# ---------- 查询响应 ----------


class EventBasis(BaseModel):
    """折算依据：对某个行情日实际生效的一条公告。"""

    event_no: str
    ex_date: str
    ratio: float = Field(..., description="公告比例（老 1 股 -> 新 ratio 股）")
    action_type: Literal["split", "reverse_split"]
    revision: int
    ingest_batch_id: int = Field(..., description="该公告所在入库批次")


class AdjustedQuote(BaseModel):
    security_code: str
    trade_date: str
    # 原值（该截止批次下生效修订的原始价量）
    open: float
    high: float
    low: float
    close: float
    volume: float
    # 折算值（以区间末日为基准复权）
    adjusted_open: float
    adjusted_high: float
    adjusted_low: float
    adjusted_close: float
    adjusted_volume: float
    factor: float = Field(..., description="价格折算系数 = 1/各公告比例之积")
    applied_events: list[EventBasis] = Field(..., description="事件依据，按除权日升序")
    cutoff_batch_id: int
    basis_end_date: str = Field(..., description="基准日（= 请求区间末日）")


class RawQuote(BaseModel):
    security_code: str
    trade_date: str
    revision: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    ingest_batch_id: int


class CorporateActionOut(BaseModel):
    security_code: str
    event_no: str
    ex_date: str
    ratio: float
    action_type: Literal["split", "reverse_split"]
    revision: int
    ingest_batch_id: int


class BatchOut(BaseModel):
    id: int
    payload_kind: Literal["quotes", "corporate_actions"]
    row_count: int
    imported_at: str


# ---------- 入账（交割单核对） ----------


class TradeIn(BaseModel):
    """一条成交：成交号、交易日、方向、股数；买入另填总额。

    同一（账户, 证券, 成交号）同值重复提交只记一次，异值拒收。
    """

    trade_no: str = Field(..., min_length=1, description="成交号（账户+证券内唯一）")
    trade_date: str = Field(..., description="交易日 YYYY-MM-DD")
    direction: Literal["buy", "sell"] = Field(..., description="buy=买入，sell=卖出")
    shares: float = Field(..., gt=0, description="成交股数（>0，可为碎股）")
    amount: float | None = Field(
        None, ge=0, description="买入总额（元，按分四舍五入）；卖出不填"
    )

    @field_validator("trade_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _check_amount(self) -> "TradeIn":
        if self.direction == "buy" and self.amount is None:
            raise ValueError("买入必须填写总额 amount")
        if self.direction == "sell" and self.amount is not None:
            raise ValueError("卖出不填写总额 amount")
        return self


class PostingRequest(BaseModel):
    """入账请求：指定账户、证券、结账日与公告截止批次。

    - ``trades`` 非空：先补录成交再重算；
    - ``trades`` 为空：只用库中已入库成交按新截止批次重算。
    """

    account: str = Field(..., min_length=1, description="资金账户")
    security_code: str = Field(..., min_length=1, description="证券代码")
    closing_date: str = Field(..., description="结账日 YYYY-MM-DD（不得早于任一成交日）")
    cutoff_batch: int | None = Field(
        None, ge=0,
        description="公告截止批次；缺省=最新入库批次（库中无批次则不用任何公告）",
    )
    trades: list[TradeIn] = Field(
        default_factory=list, description="本次补录的成交；空列表=只换公告批次重算"
    )

    @field_validator("closing_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        from datetime import date

        date.fromisoformat(v)
        return v


class PostingResponse(BaseModel):
    posting_id: int = Field(..., description="入账批次号")
    account: str
    security_code: str
    closing_date: str
    cutoff_batch: int = Field(..., description="实际采用的公告截止批次（0=不用公告）")
    trades_received: int = Field(..., description="本次请求收到的成交笔数")
    trades_new: int = Field(..., description="其中新入库的成交笔数（同号同值不计）")
    trades_total: int = Field(..., description="参与本次重算的成交总笔数")
    sells_processed: int = Field(..., description="本次重算处理的卖出笔数")
    cost_carried: float = Field(..., description="全部卖出结转成本合计（元，到分）")


class PostingOut(BaseModel):
    posting_id: int
    account: str
    security_code: str
    closing_date: str
    cutoff_batch: int
    trades_received: int
    trades_new: int
    sells_processed: int
    cost_carried: float
    created_at: str


class AllocationOut(BaseModel):
    """底稿明细：一笔卖出从某笔买入扣减的股数与结转成本。"""

    sell_trade_no: str
    sell_trade_date: str
    buy_trade_no: str
    buy_trade_date: str
    shares_deducted: float = Field(..., description="扣减股数（可含碎股）")
    cost_carried: float = Field(..., description="结转成本（元，四舍五入到分）")


class PositionOut(BaseModel):
    """底稿头寸：结账日某买入批次的剩余股数与剩余成本。"""

    buy_trade_no: str
    buy_trade_date: str
    remaining_shares: float = Field(..., description="剩余股数（经拆并股调整，含碎股）")
    remaining_cost: float = Field(..., description="剩余成本（元，到分）")


class WorksheetOut(BaseModel):
    """完整底稿：每笔卖出的买入来源 + 结账日各批次剩余。"""

    posting_id: int
    account: str
    security_code: str
    closing_date: str
    cutoff_batch: int = Field(..., description="本次入账采用的公告截止批次")
    sells_processed: int
    cost_carried: float
    created_at: str
    allocations: list[AllocationOut]
    positions: list[PositionOut]


class TradeOut(BaseModel):
    trade_no: str
    trade_date: str
    direction: Literal["buy", "sell"]
    shares: float
    amount: float | None
    posting_id: int = Field(..., description="首次记录该成交的入账批次")
