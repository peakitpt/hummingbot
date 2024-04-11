import time
import asyncio
from decimal import Decimal
from typing import List, Optional
import os
from hummingbot.client import settings
import yaml
from pydantic import Field, validator

from hummingbot.client.config.config_data_types import ClientFieldData
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.smart_components.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.smart_components.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.smart_components.models.executor_actions import ExecutorAction, StopExecutorAction, StoreExecutorAction
from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
from hummingbot.smart_components.executors.position_executor.data_types import (
    PositionExecutorConfig,
    TripleBarrierConfig,
)
from hummingbot.smart_components.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.smart_components.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.smart_components.models.executors_info import ExecutorInfo
from hummingbot.client.config.config_helpers import save_yml_from_dict, load_yml_into_dict

class GridConfig(ControllerConfigBase):
    controller_name = "grid"
    # As this controller is a simple version of the PMM, we are not using the candles feed
    candles_config: List[CandlesConfig] = Field(default=[], client_data=ClientFieldData(prompt_on_new=False))

    config_file_name: str = Field(
        default="grid.yml",
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: "Enter config file name (e.g., grid_1.yml):"))
    
    connector_name: str = Field(
        default="binance_perpetual",
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: "Enter the name of the exchange to trade on (e.g., binance_perpetual):"))
    
    trading_pair: str = Field(
        default="WLD-USDT",
        client_data=ClientFieldData(
            prompt_on_new=True,
            prompt=lambda mi: "Enter the trading pair to trade on (e.g., WLD-USDT):"))
    
    initial_margin: Decimal = Field(
        default=150, gt=0,
        client_data=ClientFieldData(
            is_updatable=True,
            prompt=lambda mi: "Enter initial margin($150): ",
            prompt_on_new=True))
    
    executor_refresh_time: int = Field(
        default=60, gt=0,
        client_data=ClientFieldData(
            prompt=lambda mi: "Enter the time in seconds to check if first order is running: ",
            prompt_on_new=True))
    
    leverage: int = Field(
        default=20, gt=0,
        client_data=ClientFieldData(
            prompt=lambda mi: "Enter the leverage (e.g. 20): ",
            prompt_on_new=True))
    
    position_mode: PositionMode = Field(
        default="ONEWAY",
        client_data=ClientFieldData(
            prompt=lambda mi: "Enter the position mode (HEDGE/ONEWAY): ",
            prompt_on_new=True
        )
    )
    
    step: Decimal = Field(
        default=Decimal("0.02"), gt=0,
        client_data=ClientFieldData(
            prompt=lambda mi: "Enter the step (e.g. 0.02 = 2%): ",
            prompt_on_new=True))

    levels: int = Field(
      default=47, gt=0,
      client_data=ClientFieldData(
          is_updatable=True,
        prompt=lambda mi: "Enter number of grids (e.g. 47): ",
        prompt_on_new=False))


    entry_price: Decimal = Field(
        default = None,
        client_data=ClientFieldData(
            prompt=lambda mi: "Enter the entry price: ",
            prompt_on_new=True
        )
    )

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            take_profit=self.step,
            open_order_type=OrderType.LIMIT,
            take_profit_order_type=OrderType.LIMIT
        )

    @validator('position_mode', pre=True, allow_reuse=True)
    def validate_position_mode(cls, v: str) -> PositionMode:
        if v.upper() in PositionMode.__members__:
            return PositionMode[v.upper()]
        raise ValueError(f"Invalid position mode: {v}. Valid options are: {', '.join(PositionMode.__members__)}")


class GridController(MarketMakingControllerBase):

    closed_executors_buffer: int = 5

    def __init__(self, config: GridConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.logger().info("Start GRID controller")
        
    def get_all_executors(self) -> List[ExecutorInfo]:
        return self.executors_info
    
    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        """
        Create actions proposal based on the current state of the executors.
        """
        self.logger().info(f"INITIAL MARGIN: {self.config.initial_margin}")

        create_actions = []

        all_executors = self.get_all_executors()

        buy_position_executors = self.filter_executors(
            executors=all_executors,
            filter_func=lambda x: x.side == TradeType.BUY and x.type == "position_executor")
        
        pair_executors = self.filter_executors(
            executors=buy_position_executors,
            filter_func=lambda x: x.is_active)
        
        len_active_buys = len(pair_executors)

        # Evaluate if we need to create new executors and create the actions
        if len_active_buys == 0:
            if self.config.entry_price > 0:
                order_price = self.config.entry_price
            else:
                order_price = self.market_data_provider.get_price_by_type(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
                asyncio.create_task(self.update_yml(order_price))
            self.logger().info(f"ORDER PRICE: {order_price}, {self.config.entry_price}")

            order_amount = self.config.initial_margin * self.config.leverage / self.config.levels / order_price
            entry_price = order_price
            
            for i in range(0, self.config.levels, 1):
                create_actions.append(CreateExecutorAction(
                    controller_id=self.config.id,
                    executor_config=PositionExecutorConfig(
                        timestamp=time.time(),
                        trading_pair=self.config.trading_pair,
                        connector_name=self.config.connector_name,
                        side=TradeType.BUY,
                        amount=order_amount,
                        entry_price=entry_price,
                        triple_barrier_config=self.config.triple_barrier_config,
                        leverage=self.config.leverage,
                    )
                ))
                entry_price = entry_price * Decimal(1 - self.config.step)
        else:
            create_actions.extend(self.trailing_create_action(buy_position_executors))

            active_positions = self.filter_executors(
                                executors=buy_position_executors,
                                filter_func=lambda x: x.is_active)
            
            completed_executors = self.filter_executors(
                executors=buy_position_executors,
                filter_func=lambda x: x.is_done)
            
            if len(completed_executors) > 0 and len(active_positions) < self.config.levels:
                for executor in completed_executors:
                    create_actions.append(CreateExecutorAction(
                        controller_id=self.config.id,
                        executor_config=PositionExecutorConfig(
                            timestamp=time.time(),
                            trading_pair=self.config.trading_pair,
                            connector_name=self.config.connector_name,
                            side=TradeType.BUY,
                            amount=executor.config.amount,
                            entry_price=executor.config.entry_price,
                            triple_barrier_config=self.config.triple_barrier_config,
                            leverage=self.config.leverage,
                        )
                    ))
                        
        return create_actions

    def trailing_create_action(self, executors) -> List[CreateExecutorAction]: 
        create_actions = []

        top_executor = max(executors, key = lambda x: x.config.entry_price)
        #  verificar se o preço currente é maior que a segunda posição
        if not top_executor.is_trading and time.time() - top_executor.timestamp > self.config.executor_refresh_time:
            self.logger().info("Trailing ACTIVE")
            create_actions.append(CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=PositionExecutorConfig(
                    timestamp=time.time(),
                    trading_pair=top_executor.trading_pair,
                    connector_name=top_executor.connector_name,
                    side=TradeType.BUY,
                    amount=top_executor.config.amount,
                    entry_price=top_executor.config.entry_price * Decimal(1 + self.config.step),
                    triple_barrier_config=self.config.triple_barrier_config,
                    leverage=self.config.leverage,
                )))
            asyncio.create_task(self.update_yml(top_executor.config.entry_price * Decimal(1 + self.config.step)))

        return create_actions
    
    def trailing_stop_action(self) -> List[StopExecutorAction]:
        stop_actions = []
        all_executors = self.get_all_executors()

        last_executors = self.filter_executors(
                      executors=all_executors,
                      filter_func=lambda x: x.side == TradeType.BUY and x.type == "position_executor" and x.is_active)
        
        if len(last_executors) > self.config.levels:
            bot_executor = min(last_executors, key = lambda x: x.config.entry_price)
            self.logger().info(f"Trailing DONE: {bot_executor.id} || {bot_executor.config.entry_price}")
            stop_actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=bot_executor.id))
        
        return stop_actions

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        """
        Create a list of actions to stop the executors based on order refresh and early stop conditions.
        """
        stop_actions = []
        stop_actions.extend(self.trailing_stop_action())
        stop_actions.extend(self.executors_to_refresh())
        return stop_actions

    def executors_to_refresh(self) -> List[StopExecutorAction]:
        #     """
        #     Create a list of actions to stop the executors that need to be refreshed.
        #     """
        stop_actions = []
        all_executors = self.get_all_executors()

        executors_to_refresh = self.filter_executors(
           executors=all_executors,
           filter_func=lambda x: x.is_done)
        
        if len(executors_to_refresh) > 0:
            for executor in executors_to_refresh:
                self.logger().info(f"CLOSING: {executor.id} || {executor.config.entry_price}")
                stop_actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=executor.id))

        return stop_actions

    async def update_yml(self, entry_price):
        self.logger().info(f"Updating yaml")
        full_path = os.path.join(settings.CONTROLLERS_CONF_DIR_PATH, self.config.config_file_name)
        with open(full_path, 'r') as file:
            config_data = yaml.safe_load(file)
            config_data['entry_price'] = float(entry_price)
        with open(full_path, 'w+') as file:
            yaml.dump(config_data, file)