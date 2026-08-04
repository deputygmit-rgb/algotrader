from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"
    CDS = "CDS"
    BCD = "BCD"
    MCX = "MCX"
    BFO = "BFO"
    MF = "MF"


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ProductType(str, Enum):
    CNC = "CNC"
    NRML = "NRML"
    MIS = "MIS"
    BO = "BO"
    CO = "CO"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class OrderVariety(str, Enum):
    REGULAR = "regular"
    AMO = "amo"
    CO = "co"
    ICEBERG = "iceberg"
    AUCTION = "auction"


class OrderValidity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    TTL = "TTL"


class OrderStatus(str, Enum):
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    OPEN = "OPEN"
    PENDING = "PENDING"
    PUT_ORDER_REQ_RECEIVED = "PUT ORDER REQ RECEIVED"
    VALIDATION_PENDING = "VALIDATION PENDING"
    OPEN_PENDING = "OPEN PENDING"
    MODIFY_PENDING = "MODIFY PENDING"
    TRIGGER_PENDING = "TRIGGER PENDING"
    CANCEL_PENDING = "CANCEL PENDING"
    AMO_REQ_RECEIVED = "AMO REQ RECEIVED"


class Interval(str, Enum):
    MINUTE = "minute"
    MINUTE_3 = "3minute"
    MINUTE_5 = "5minute"
    MINUTE_10 = "10minute"
    MINUTE_15 = "15minute"
    MINUTE_30 = "30minute"
    MINUTE_60 = "60minute"
    DAY = "day"


class TickMode(str, Enum):
    LTP = "ltp"
    QUOTE = "quote"
    FULL = "full"


class GTTType(str, Enum):
    SINGLE = "single"
    TWO_LEG = "two-leg"


class GTTStatus(str, Enum):
    ACTIVE = "active"
    TRIGGERED = "triggered"
    DISABLED = "disabled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    DELETED = "deleted"


class UserProfile(BaseModel):
    user_id: str = ""
    user_type: str = ""
    email: str = ""
    user_name: str = ""
    user_shortname: str = ""
    broker: str = ""
    exchanges: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    order_types: list[str] = Field(default_factory=list)
    avatar_url: Optional[str] = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SessionData(BaseModel):
    user_id: str = ""
    user_type: str = ""
    email: str = ""
    user_name: str = ""
    user_shortname: str = ""
    broker: str = ""
    exchanges: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    order_types: list[str] = Field(default_factory=list)
    api_key: str = ""
    access_token: str = ""
    public_token: str = ""
    refresh_token: str = ""
    login_time: Optional[str] = None
    avatar_url: Optional[str] = None
    meta: dict[str, Any] = Field(default_factory=dict)


class MarginAvailable(BaseModel):
    adhoc_margin: float = 0.0
    cash: float = 0.0
    opening_balance: float = 0.0
    live_balance: float = 0.0
    collateral: float = 0.0
    intraday_payin: float = 0.0


class MarginUtilised(BaseModel):
    debits: float = 0.0
    exposure: float = 0.0
    m2m_realised: float = 0.0
    m2m_unrealised: float = 0.0
    option_premium: float = 0.0
    payout: float = 0.0
    span: float = 0.0
    holding_sales: float = 0.0
    turnover: float = 0.0
    delivery: float = 0.0


class SegmentMargin(BaseModel):
    enabled: bool = False
    net: float = 0.0
    available: MarginAvailable = Field(default_factory=MarginAvailable)
    utilised: MarginUtilised = Field(default_factory=MarginUtilised)


class Margins(BaseModel):
    equity: Optional[SegmentMargin] = None
    commodity: Optional[SegmentMargin] = None


class OrderRequest(BaseModel):
    tradingsymbol: str
    exchange: Exchange
    transaction_type: TransactionType
    order_type: OrderType
    quantity: int
    product: ProductType
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    disclosed_quantity: Optional[int] = None
    validity: OrderValidity = OrderValidity.DAY
    validity_ttl: Optional[int] = None
    iceberg_legs: Optional[int] = None
    iceberg_quantity: Optional[int] = None
    auction_number: Optional[int] = None
    tag: Optional[str] = None
    variety: OrderVariety = OrderVariety.REGULAR


class OrderModifyRequest(BaseModel):
    order_id: str
    variety: OrderVariety = OrderVariety.REGULAR
    order_type: Optional[OrderType] = None
    quantity: Optional[int] = None
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    disclosed_quantity: Optional[int] = None
    validity: Optional[OrderValidity] = None


class Order(BaseModel):
    order_id: str = ""
    parent_order_id: Optional[str] = None
    exchange_order_id: Optional[str] = None
    placed_by: str = ""
    variety: str = ""
    status: str = ""
    tradingsymbol: str = ""
    exchange: str = ""
    instrument_token: int = 0
    transaction_type: str = ""
    order_type: str = ""
    product: str = ""
    validity: str = ""
    price: float = 0.0
    quantity: int = 0
    trigger_price: float = 0.0
    average_price: float = 0.0
    pending_quantity: int = 0
    filled_quantity: int = 0
    disclosed_quantity: int = 0
    cancelled_quantity: int = 0
    market_protection: int = 0
    order_timestamp: Optional[str] = None
    exchange_timestamp: Optional[str] = None
    exchange_update_timestamp: Optional[str] = None
    status_message: Optional[str] = None
    status_message_raw: Optional[str] = None
    tag: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    guid: str = ""


class Trade(BaseModel):
    trade_id: str = ""
    order_id: str = ""
    exchange_order_id: Optional[str] = None
    tradingsymbol: str = ""
    exchange: str = ""
    instrument_token: int = 0
    transaction_type: str = ""
    product: str = ""
    average_price: float = 0.0
    quantity: int = 0
    fill_timestamp: Optional[str] = None
    exchange_timestamp: Optional[str] = None
    order_timestamp: Optional[str] = None


class Position(BaseModel):
    tradingsymbol: str = ""
    exchange: str = ""
    instrument_token: int = 0
    product: str = ""
    quantity: int = 0
    overnight_quantity: int = 0
    multiplier: float = 1.0
    average_price: float = 0.0
    close_price: float = 0.0
    last_price: float = 0.0
    value: float = 0.0
    pnl: float = 0.0
    m2m: float = 0.0
    unrealised: float = 0.0
    realised: float = 0.0
    buy_quantity: int = 0
    buy_price: float = 0.0
    buy_value: float = 0.0
    buy_m2m: float = 0.0
    sell_quantity: int = 0
    sell_price: float = 0.0
    sell_value: float = 0.0
    sell_m2m: float = 0.0
    day_buy_quantity: int = 0
    day_buy_price: float = 0.0
    day_buy_value: float = 0.0
    day_sell_quantity: int = 0
    day_sell_price: float = 0.0
    day_sell_value: float = 0.0


class Positions(BaseModel):
    net: list[Position] = Field(default_factory=list)
    day: list[Position] = Field(default_factory=list)


class Holding(BaseModel):
    tradingsymbol: str = ""
    exchange: str = ""
    instrument_token: int = 0
    isin: str = ""
    product: str = ""
    price: float = 0.0
    quantity: int = 0
    used_quantity: int = 0
    t1_quantity: int = 0
    realised_quantity: int = 0
    authorised_quantity: int = 0
    authorised_date: Optional[str] = None
    opening_quantity: int = 0
    collateral_quantity: int = 0
    collateral_type: Optional[str] = None
    discrepancy: bool = False
    average_price: float = 0.0
    last_price: float = 0.0
    close_price: float = 0.0
    pnl: float = 0.0
    day_change: float = 0.0
    day_change_percentage: float = 0.0


class Instrument(BaseModel):
    instrument_token: int = 0
    exchange_token: int = 0
    tradingsymbol: str = ""
    name: str = ""
    last_price: float = 0.0
    expiry: Optional[str] = None
    strike: float = 0.0
    tick_size: float = 0.0
    lot_size: int = 0
    instrument_type: str = ""
    segment: str = ""
    exchange: str = ""


class HistoricalCandle(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: Optional[int] = None


class OHLC(BaseModel):
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0


class DepthItem(BaseModel):
    price: float = 0.0
    quantity: int = 0
    orders: int = 0


class MarketDepth(BaseModel):
    buy: list[DepthItem] = Field(default_factory=list)
    sell: list[DepthItem] = Field(default_factory=list)


class Quote(BaseModel):
    instrument_token: int = 0
    timestamp: Optional[str] = None
    last_price: float = 0.0
    last_quantity: int = 0
    last_trade_time: Optional[str] = None
    average_price: float = 0.0
    volume: int = 0
    buy_quantity: int = 0
    sell_quantity: int = 0
    ohlc: OHLC = Field(default_factory=OHLC)
    net_change: float = 0.0
    oi: int = 0
    oi_day_high: int = 0
    oi_day_low: int = 0
    lower_circuit_limit: float = 0.0
    upper_circuit_limit: float = 0.0
    depth: MarketDepth = Field(default_factory=MarketDepth)


class LTPQuote(BaseModel):
    instrument_token: int = 0
    last_price: float = 0.0


class OHLCQuote(BaseModel):
    instrument_token: int = 0
    last_price: float = 0.0
    ohlc: OHLC = Field(default_factory=OHLC)


class GTTCondition(BaseModel):
    exchange: str = ""
    tradingsymbol: str = ""
    trigger_values: list[float] = Field(default_factory=list)
    last_price: float = 0.0
    instrument_token: int = 0


class GTTOrderResult(BaseModel):
    account_id: str = ""
    exchange: str = ""
    tradingsymbol: str = ""
    validity: str = ""
    product: str = ""
    order_type: str = ""
    transaction_type: str = ""
    quantity: int = 0
    price: float = 0.0
    result: Optional[dict[str, Any]] = None


class GTTOrder(BaseModel):
    id: int = 0
    user_id: str = ""
    type: str = ""
    status: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None
    meta: Optional[dict[str, Any]] = None
    condition: GTTCondition = Field(default_factory=GTTCondition)
    orders: list[GTTOrderResult] = Field(default_factory=list)


class GTTRequest(BaseModel):
    trigger_type: GTTType
    tradingsymbol: str
    exchange: Exchange
    trigger_values: list[float]
    last_price: float
    orders: list[dict[str, Any]]


class Tick(BaseModel):
    instrument_token: int = 0
    mode: str = ""
    tradable: bool = True
    last_price: float = 0.0
    last_traded_quantity: int = 0
    average_traded_price: float = 0.0
    volume_traded: int = 0
    total_buy_quantity: int = 0
    total_sell_quantity: int = 0
    ohlc: OHLC = Field(default_factory=OHLC)
    change: float = 0.0
    last_trade_time: Optional[datetime] = None
    exchange_timestamp: Optional[datetime] = None
    oi: int = 0
    oi_day_high: int = 0
    oi_day_low: int = 0
    depth: MarketDepth = Field(default_factory=MarketDepth)


class Signal(BaseModel):
    tradingsymbol: str
    exchange: Exchange
    transaction_type: TransactionType
    quantity: int
    price: Optional[float] = None
    order_type: OrderType = OrderType.MARKET
    product: ProductType = ProductType.MIS
    trigger_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    strategy_name: str = ""
    confidence: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BacktestResult(BaseModel):
    strategy_name: str = ""
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: float = 0.0
    final_capital: float = 0.0
    total_return: float = 0.0
    cagr: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    trades: list[dict[str, Any]] = Field(default_factory=list)
    equity_curve: list[float] = Field(default_factory=list)


class TradeJournalEntry(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.now)
    strategy: str = ""
    tradingsymbol: str = ""
    exchange: str = ""
    transaction_type: str = ""
    quantity: int = 0
    price: float = 0.0
    order_id: str = ""
    status: str = ""
    pnl: float = 0.0
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
