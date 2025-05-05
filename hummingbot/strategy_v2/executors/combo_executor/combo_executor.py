import asyncio
import logging
from decimal import ROUND_DOWN, Decimal
from typing import Dict, List, Optional, Union
from hummingbot.connector.constants import s_decimal_NaN

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.connector.utils import get_new_client_order_id
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
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.logger import HummingbotLogger
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.strategy_v2.executors.combo_executor.data_types import ComboExecutorConfig
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.executors.grid_executor.data_types import GridExecutorConfig, GridLevel, GridLevelStates
from hummingbot.strategy_v2.executors.grid_executor.grid_executor import GridExecutor
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder
from hummingbot.strategy_v2.utils.distributions import Distributions
from ruamel.yaml import YAML
from pathlib import Path


class ComboExecutor(GridExecutor):
    _logger = None

    def __init__(self, strategy: ScriptStrategyBase, config: ComboExecutorConfig, update_interval: float = 1.0, max_retries: int = 10):
        self._stop_loss_order = None
        self.config: ComboExecutorConfig = config
        super().__init__(strategy=strategy, config=config, update_interval=update_interval, max_retries = max_retries)
    
    def place_stop_loss_order(self):
        if not self._stop_loss_order:
            price = self.config.start_price - (self.config.start_price * self.config.min_spread_between_orders)
            self.logger().info(f"Executor ID: {self.config.id} - StopLossOrder Initial Price({price})")
            connector = self.connectors[self.config.connector_name]
            order_id = get_new_client_order_id(
                is_buy=False,
                trading_pair=self.config.trading_pair,
                hbot_order_id_prefix=connector.client_order_id_prefix,
                max_id_len=connector.client_order_id_max_length
            )
            price = connector.quantize_order_price(self.config.trading_pair, price)
            safe_ensure_future(connector._create_order(
                trade_type=TradeType.SELL,
                order_id=order_id,
                trading_pair=self.config.trading_pair,
                amount=Decimal("60"),
                order_type=OrderType.MARKET,
                price=price,
                position_action=PositionAction.CLOSE,
                stop_loss=True))
            self._stop_loss_order = TrackedOrder(order_id=order_id)
            self.logger().info(f"Executor ID: {self.config.id} - StopLossOrder #{order_id}, Price({price})")

    def stop_loss_condition(self):
        """
        This method is responsible for controlling the stop loss. If the net pnl percentage is less than the stop loss
        percentage, it places the close order and cancels the open orders.

        :return: None
        """
        return not self._stop_loss_order and self.mid_price < self.config.start_price - (self.config.start_price * self.config.min_spread_between_orders)
    
    async def validate_sufficient_balance(self):
        pass    

    def process_order_created_event(self, _, market, event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]):
        super().process_order_created_event(_, market=market, event=event)
        self.logger().debug(f"Executor ID: {self.config.id} - OrderCreatedEvent #{event.order_id}")

    def process_order_filled_event(self, _, market, event: OrderFilledEvent):
        super().process_order_filled_event(_, market=market, event=event)
        if self.config.use_exchange_stop_loss:
            self.place_stop_loss_order()
            if event.order_id == self._stop_loss_order.order_id:
                self._stop_loss_order = None
                self._status = RunnableStatus.SHUTTING_DOWN
                self.logger().info(f"Executor ID: {self.config.id} - StopLossOrder #{event.order_id} filled")

    def process_order_completed_event(self, _, market, event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent]):
        super().process_order_completed_event(_, market=market, event=event)
        self.logger().debug(f"Executor ID: {self.config.id} - OrderCompletedEvent #{event.order_id}")

    def process_order_canceled_event(self, _, market: ConnectorBase, event: OrderCancelledEvent):
        super().process_order_canceled_event(_, market=market, event=event)

    def cancel_open_orders(self):
        """
        This method is responsible for canceling the open orders.

        :return: None
        """
        open_order_placed = [level.active_open_order for level in
                             self.levels_by_state[GridLevelStates.OPEN_ORDER_PLACED]]
        close_order_placed = [level.active_close_order for level in
                              self.levels_by_state[GridLevelStates.CLOSE_ORDER_PLACED]]
        
        orders = open_order_placed + close_order_placed
        if self.config.use_exchange_stop_loss and self._stop_loss_order:
            orders.append(self._stop_loss_order)

        for order in orders:
            # TODO: Implement cancel batch orders
            if order:
                self._strategy.cancel(
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    order_id=order.order_id
                )
                self.logger().debug("Removing open order")
                self.logger().debug(f"Executor ID: {self.config.id} - Canceling open order {order.order_id}")

    def stop(self):
        super().stop()
        self._stop_loss_order = None
        self.update_config_pnl()

    def update_config_pnl(self):
        current_dir = Path(__file__).resolve().parent
        yaml = YAML()
        file_path = f"{current_dir}/../../../../conf/controllers/{self.config.config_name}"
        with open(file_path, 'r') as f:
            config = yaml.load(f)
        
        new_pnl = float(config['pnl']) + float(self.get_net_pnl_quote())

        self.logger().info(f"Executor ID: {self.config.id} - Saving new pnl from {float(config['pnl'])} to {new_pnl}")

        config['pnl'] = new_pnl
        with open(file_path, 'w') as f:
            yaml.dump(config, f)

    # def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
    #     super().process_order_failed_event(_, market=market, event=event)
    #     self.check_orders()

    # def get_take_profit_price(self, level: GridLevel):
    #     if str(level.id) == f"L{len(self.grid_levels)-1}":
    #         return level.price * (1 + level.take_profit) if self.config.side == TradeType.BUY else level.price * (1 - level.take_profit)
    #     else:
    #         previous_level = next((lvl for lvl in self.grid_levels if lvl.id == f'L{int(level.id.replace("L",""))+1}'), None)
    #         return previous_level.price if self.config.side == TradeType.BUY else level.price * (1 - level.take_profit)
    
    # def take_profit_condition(self):
    #     """
    #     Take profit will be when the mid price is above the end price of the grid and there are no active executors.
    #     """
    #     if self.get_net_pnl_pct() >= Decimal("5"):
    #         self.logger().info(f"TAKE_PROFIT PCT: {self.mid_price}")
    #         return True
    #     if self.mid_price > self.config.end_price * (1+self.config.triple_barrier_config.take_profit) if self.config.side == TradeType.BUY else self.mid_price < self.config.start_price:
    #         self.logger().info(f"TAKE_PROFIT: {self.mid_price}")
    #         return True
    #     return False

    # def _get_open_order_candidate(self, level: GridLevel):
    #     if ((level.side == TradeType.BUY and level.price >= self.current_open_quote) or
    #             (level.side == TradeType.SELL and level.price <= self.current_open_quote)):
    #         entry_price = self.current_open_quote * (1 - self.config.safe_extra_spread) if level.side == TradeType.BUY else self.current_open_quote * (1 + self.config.safe_extra_spread)
    #     else:
    #         entry_price = level.price
    #     if self.is_perpetual:
    #         return PerpetualOrderCandidate(
    #             trading_pair=self.config.trading_pair,
    #             is_maker=self.config.triple_barrier_config.open_order_type.is_limit_type(),
    #             order_type=self.config.triple_barrier_config.open_order_type,
    #             order_side=self.config.side,
    #             amount=level.amount_quote / level.price,
    #             price=entry_price,
    #             leverage=Decimal(self.config.leverage)
    #         )
    #     return OrderCandidate(
    #         trading_pair=self.config.trading_pair,
    #         is_maker=self.config.triple_barrier_config.open_order_type.is_limit_type(),
    #         order_type=self.config.triple_barrier_config.open_order_type,
    #         order_side=self.config.side,
    #         amount=level.amount_quote / level.price,
    #         price=entry_price
    #     )
    
    # def get_custom_info(self) -> Dict:
    #     custom_info = super().get_custom_info()
    #     custom_info['net_pnl_pct'] = self.get_net_pnl_pct()
    #     return custom_info
