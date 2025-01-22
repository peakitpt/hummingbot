import asyncio
import logging
from decimal import ROUND_DOWN, Decimal
from typing import Dict, List, Optional, Union

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PositionAction, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate, PerpetualOrderCandidate
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)
from hummingbot.logger import HummingbotLogger
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.strategy_v2.executors.combo_executor.data_types import ComboExecutorConfig
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig, GridLevel, GridLevelStates
from hummingbot.strategy_v2.executors.grid_executor.grid_executor import GridExecutor
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder
from hummingbot.strategy_v2.utils.distributions import Distributions


class ComboExecutor(GridExecutor):
    _logger = None

    def __init__(self, strategy: ScriptStrategyBase, config: ComboExecutorConfig,
                 update_interval: float = 1.0, max_retries: int = 10):
        """
        Initialize the PositionExecutor instance.

        :param strategy: The strategy to be used by the PositionExecutor.
        :param config: The configuration for the PositionExecutor, subclass of PositionExecutoConfig.
        :param update_interval: The interval at which the PositionExecutor should be updated, defaults to 1.0.
        :param max_retries: The maximum number of retries for the PositionExecutor, defaults to 5.
        """
        self.config: ComboExecutorConfig = config
        super().__init__(strategy=strategy, config=config, update_interval=update_interval, max_retries = max_retries)
        
    
    async def validate_sufficient_balance(self):
        pass    

    def close_gaps(self):
        for level in self.levels_by_state[GridLevelStates.OPEN_ORDER_PLACED]:
            order = level.active_open_order
            if order:
                self._strategy.cancel(
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    order_id=order.order_id
                )
                self.logger().debug("Removing open order")
                self.logger().debug(f"Executor ID: {self.config.id} - Canceling open order {order.order_id}")
                level.reset_level()

    # def process_order_created_event(self, _, market, event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]):
    #     super().process_order_created_event(_, market=market, event=event)
    #     self.check_orders()

    # def process_order_filled_event(self, _, market, event: OrderFilledEvent):
    #     super().process_order_filled_event(_, market=market, event=event)
    #     self.check_orders()

    def process_order_completed_event(self, _, market, event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent]):
        super().process_order_completed_event(_, market=market, event=event)
        self.close_gaps()
        # self.check_orders()

    # def process_order_canceled_event(self, _, market: ConnectorBase, event: OrderCancelledEvent):
    #     super().process_order_canceled_event(_, market=market, event=event)
    #     self.check_orders()

    # def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
    #     super().process_order_failed_event(_, market=market, event=event)
    #     self.check_orders()

    def get_take_profit_price(self, level: GridLevel):
        if str(level.id) == f"L{len(self.grid_levels)-1}":
            return level.price * (1 + level.take_profit) if self.config.side == TradeType.BUY else level.price * (1 - level.take_profit)
        else:
            previous_level = next((lvl for lvl in self.grid_levels if lvl.id == f'L{int(level.id.replace("L",""))+1}'), None)
            return previous_level.price if self.config.side == TradeType.BUY else level.price * (1 - level.take_profit)
    
    def take_profit_condition(self):
        """
        Take profit will be when the mid price is above the end price of the grid and there are no active executors.
        """
        if self.get_net_pnl_pct() >= Decimal("5"):
            self.logger().info(f"TAKE_PROFIT PCT: {self.mid_price}")
            return True
        if self.mid_price > self.config.end_price * (1+self.config.triple_barrier_config.take_profit) if self.config.side == TradeType.BUY else self.mid_price < self.config.start_price:
            self.logger().info(f"TAKE_PROFIT: {self.mid_price}")
            return True
        return False

    def _get_open_order_candidate(self, level: GridLevel):
        if ((level.side == TradeType.BUY and level.price >= self.current_open_quote) or
                (level.side == TradeType.SELL and level.price <= self.current_open_quote)):
            entry_price = self.current_open_quote * (1 - self.config.safe_extra_spread) if level.side == TradeType.BUY else self.current_open_quote * (1 + self.config.safe_extra_spread)
        else:
            entry_price = level.price
        if self.is_perpetual:
            return PerpetualOrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=self.config.triple_barrier_config.open_order_type.is_limit_type(),
                order_type=self.config.triple_barrier_config.open_order_type,
                order_side=self.config.side,
                amount=level.amount_quote / level.price,
                price=entry_price,
                leverage=Decimal(self.config.leverage)
            )
        return OrderCandidate(
            trading_pair=self.config.trading_pair,
            is_maker=self.config.triple_barrier_config.open_order_type.is_limit_type(),
            order_type=self.config.triple_barrier_config.open_order_type,
            order_side=self.config.side,
            amount=level.amount_quote / level.price,
            price=entry_price
        )
    
    def get_custom_info(self) -> Dict:
        custom_info = super().get_custom_info()
        custom_info['net_pnl_pct'] = self.get_net_pnl_pct()
        return custom_info