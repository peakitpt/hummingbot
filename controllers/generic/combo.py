from decimal import Decimal
from typing import Dict, List, Optional, Set

from hummingbot.strategy_v2.executors.combo_executor.data_types import ComboExecutorConfig
from pydantic import Field

from hummingbot.client.config.config_data_types import ClientFieldData
from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


class ComboConfig(ControllerConfigBase):
    """
    Configuration required to run the GridStrike strategy for one connector and trading pair.
    """
    controller_type = "generic"
    controller_name: str = "combo"
    candles_config: List[CandlesConfig] = []

    # Account configuration
    leverage: int = Field(default=75, client_data=ClientFieldData(is_updatable=True))
    position_mode: PositionMode = PositionMode.HEDGE

    # Boundaries
    connector_name: str = Field(default="binance_perpetual")
    trading_pair: str = Field(default="PNUT-USDT")
    side: TradeType = TradeType.BUY
    start_price: Optional[Decimal] = Field(default=None, client_data=ClientFieldData(is_updatable=True))
    end_price: Optional[Decimal] = Field(default=None, client_data=ClientFieldData(is_updatable=True))
    limit_price: Optional[Decimal] = Field(default=None, client_data=ClientFieldData(is_updatable=True))

    # Profiling
    total_amount_quote: Decimal = Field(default=Decimal("120"), client_data=ClientFieldData(is_updatable=True))
    min_spread_between_orders: Optional[Decimal] = Field(default=Decimal("0.002"),
                                                         client_data=ClientFieldData(is_updatable=True))
    min_order_amount_quote: Optional[Decimal] = Field(default=Decimal("5"),
                                                      client_data=ClientFieldData(is_updatable=True))

    # Execution
    max_open_orders: int = Field(default=30, client_data=ClientFieldData(is_updatable=True))
    max_orders_per_batch: Optional[int] = Field(default=1, client_data=ClientFieldData(is_updatable=True))
    order_frequency: int = Field(default=10, client_data=ClientFieldData(is_updatable=True))
    activation_bounds: Optional[Decimal] = Field(default=None, client_data=ClientFieldData(is_updatable=True))

    # Risk Management
    take_profit: Decimal = Field(default=Decimal("0.02"), gt=0, client_data=ClientFieldData(
        prompt_on_new=True, prompt=lambda mi: "Position take profit (e.g. 0.02 for 2%)"))

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            open_order_type=OrderType.LIMIT_MAKER,
            take_profit_order_type=OrderType.LIMIT_MAKER,
            take_profit=self.take_profit,
            stop_loss=None
        )
    
    def update_markets(self, markets: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
        if self.connector_name not in markets:
            markets[self.connector_name] = set()
        markets[self.connector_name].add(self.trading_pair)
        return markets


class Combo(ControllerBase):

    def __init__(self, config: ComboConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_grid_levels_update = 0
        self.trading_rules = None
        self.grid_levels = []
        self.initialize_rate_sources()

    def initialize_rate_sources(self):
        self.market_data_provider.initialize_rate_sources([ConnectorPair(connector_name=self.config.connector_name,
                                                                         trading_pair=self.config.trading_pair)])

    def active_executors(self) -> List[ExecutorInfo]:
        return [
            executor for executor in self.executors_info
            if executor.is_active
        ]

    def determine_executor_actions(self) -> List[ExecutorAction]:
        if len(self.active_executors()) == 0:
            # if self.config.limit_price is None:
            market_price = self.market_data_provider.get_price_by_type(self.config.connector_name,
                                                                self.config.trading_pair,
                                                                PriceType.MidPrice)
            
            # self.config.limit_price = market_price * Decimal("0.4")
            # self.config.start_price = market_price * Decimal("0.4")
            # self.config.limit_price = market_price * Decimal("0.9")
            max_drawdown = 60
            tp = 2
            drawdown = (100 - max_drawdown) / 100
            take_profit = 1 + (tp/100)
            start_price = market_price * Decimal(drawdown)
            end_price = market_price #* Decimal(take_profit)
            self.logger().info(f"start_price: {start_price}")
            self.logger().info(f"end_price: {end_price}")
            return [CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=ComboExecutorConfig(
                    timestamp=self.market_data_provider.time(),
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    start_price=start_price,
                    end_price=end_price,
                    leverage=self.config.leverage,
                    # limit_price=self.config.limit_price,
                    side=self.config.side,
                    total_amount_quote=self.config.total_amount_quote,
                    min_spread_between_orders=self.config.min_spread_between_orders,
                    min_order_amount_quote=self.config.min_order_amount_quote,
                    max_open_orders=self.config.max_open_orders,
                    max_orders_per_batch=self.config.max_orders_per_batch,
                    # order_frequency=self.config.order_frequency,
                    activation_bounds=self.config.activation_bounds,
                    triple_barrier_config=self.config.triple_barrier_config,
                    level_id=None))]
        return []

    async def update_processed_data(self):
        pass

    def to_format_status(self) -> List[str]:
        # Define column widths and spacing
        col_width = 45
        total_width = col_width * 4  # 4 columns
        status = []
        # Header
        status.append("\n" + "═" * total_width)
        header = f"Combo Executor Controller: {self.config.id}"
        status.append(header.center(total_width))
        status.append("═" * total_width)
        for level in self.active_executors():
            status.append(f"Combo Status - {level.id}:".center(total_width))
            status.append(f"Current Status: {level.status}".center(total_width))
            status.append("─" * total_width)
            # Prepare data for each column
            # grid_config = [
            #     "Grid Configuration:",
            #     f"Start: {self.config.start_price:.4f}" if self.config.start_price is not None else "Start: N/A",
            #     f"End: {self.config.end_price:.4f}" if self.config.end_price is not None else "End: N/A",
            #     f"Side: {self.config.side}",
            #     f"Limit: {self.config.limit_price:.4f}" if self.config.limit_price is not None else "Limit: N/A",
            #     f"Max Orders: {self.config.max_open_orders}"
            # ]
            level_dist = ["Level Distribution:"]
            for state, count in level.custom_info['levels_by_state'].items():
                level_dist.append(f"{state}: {len(count)} levels")
            order_stats = [
                "Order Statistics:",
                f"Total Orders: {sum(len(level.custom_info[k]) for k in ['filled_orders', 'failed_orders', 'canceled_orders'])}",
                f"Filled: {len(level.custom_info['filled_orders'])}",
                f"Failed: {len(level.custom_info['failed_orders'])}",
                f"Canceled: {len(level.custom_info['canceled_orders'])}"
            ]
            perf_metrics = [
                "Performance Metrics:",
                f"Buy Vol: {level.custom_info['realized_buy_size_quote']:.4f}",
                f"Sell Vol: {level.custom_info['realized_sell_size_quote']:.4f}",
                f"R. PnL: {level.custom_info['realized_pnl_quote']:.4f}",
                f"R. Fees: {level.custom_info['realized_fees_quote']:.4f}",
                f"P. PnL: {level.custom_info['position_pnl_quote']:.4f}",
                f"Open Liquidity: {level.custom_info['open_liquidity_placed']:.4f}",
                f"Close Liquidity: {level.custom_info['close_liquidity_placed']:.4f}",
                f"Position: {level.custom_info['position_size_quote']:.4f}",
                f"PNL (%): {level.custom_info['net_pnl_pct']:.2f}%"
            ]
            # Combine columns row by row
            # max_rows = max(len(grid_config), len(level_dist), len(order_stats), len(perf_metrics))
            # for i in range(max_rows):
            #     row = []
            #     for col in [grid_config, level_dist, order_stats, perf_metrics]:
            #         cell = col[i] if i < len(col) else ""
            #         row.append(f"{cell:<{col_width}}")
            #     status.append("".join(row))
            # status.append("═" * total_width)
            max_rows = max(len(level_dist), len(order_stats), len(perf_metrics))
            for i in range(max_rows):
                row = []
                for col in [level_dist, order_stats, perf_metrics]:
                    cell = col[i] if i < len(col) else ""
                    row.append(f"{cell:<{col_width}}")
                status.append("".join(row))
            status.append("═" * total_width)
        return status
