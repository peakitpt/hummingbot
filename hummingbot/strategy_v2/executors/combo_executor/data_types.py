from decimal import Decimal
from enum import Enum
from typing import Optional

from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig, GridLevel, GridLevelStates
from pydantic import BaseModel

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.models.executors import TrackedOrder


class ComboExecutorConfig(GridExecutorConfig):
    type = "combo_executor"
    # Boundaries
    connector_name: str
    trading_pair: str
    start_price: Decimal
    end_price: Decimal
    limit_price: Optional[Decimal] = None
    side: TradeType = TradeType.BUY
    # Profiling
    total_amount_quote: Decimal
    min_spread_between_orders: Decimal = Decimal("0.0005")
    min_order_amount_quote: Optional[Decimal] = Decimal("5")
    # Execution
    max_open_orders: int = 5
    max_orders_per_batch: Optional[int] = None
    order_frequency: int = 0
    activation_bounds: Optional[Decimal] = None
    safe_extra_spread: Decimal = Decimal("0.0002")
    # Risk Management
    triple_barrier_config: TripleBarrierConfig
    leverage: int = 20
    level_id: Optional[str] = None
    deduct_base_fees: bool = False
