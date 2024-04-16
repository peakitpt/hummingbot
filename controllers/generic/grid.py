import time
import asyncio
from decimal import Decimal
from typing import List
import os
from hummingbot.client import settings
import yaml
from pydantic import Field, validator

from hummingbot.client.config.config_data_types import ClientFieldData
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.smart_components.controllers.market_making_controller_base import MarketMakingControllerBase
from hummingbot.smart_components.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.smart_components.models.executor_actions import ExecutorAction, StopExecutorAction, StoreExecutorAction
from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
from hummingbot.smart_components.executors.position_executor.data_types import (PositionExecutorConfig, TripleBarrierConfig)
from hummingbot.smart_components.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.smart_components.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.smart_components.models.executors_info import ExecutorInfo


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


class GridController(ControllerBase):

    closed_executors_buffer: int = 5

    def __init__(self, config: GridConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.logger().info("Start GRID controller")
        
    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions = []
        actions.extend(self.create_actions_proposal())
        actions.extend(self.stop_actions_proposal())
        return actions
    
    async def update_processed_data(self):
        reference_price = self.market_data_provider.get_price_by_type(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        self.processed_data = {"reference_price": reference_price, "spread_multiplier": Decimal("1")}

    def get_all_executors(self) -> List[ExecutorInfo]:
        return self.filter_executors(executors = self.executors_info, filter_func = lambda x: x.trading_pair == self.config.trading_pair and x.side == TradeType.BUY and x.type == "position_executor")
    
    def get_entry_price(self) -> Decimal:
        order_price = 0
        if self.config.entry_price > 0:
            order_price = self.config.entry_price
        else:
            order_price = self.market_data_provider.get_price_by_type(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            asyncio.create_task(self.update_yml(order_price))

        return order_price

    def check_if_positions_match_initial_margin(self, active_executors) -> List[CreateExecutorAction]:
        actions = []
         # IF ANY ACTIVE POSITION, VALIDATE IF THE INITIAL MARGIN THAT THE POSITION WAS MADE BASED ON HAS NOT CHANGED
        not_trading_positions = self.filter_executors(executors=active_executors, filter_func=lambda x: not x.is_trading)
        # Check if initial margin has changed in first open position
        
        if len(not_trading_positions) > 0:
            initial_margin = not_trading_positions[0].config.amount * self.config.entry_price * self.config.levels / self.config.leverage
            # INITIAL MARGIN CHANGED, STOP THE POSITIONs (System should create new ones)
            if self.config.initial_margin != initial_margin:
                self.logger().info(f"Initial margin changed from {initial_margin} to {self.config.initial_margin} ")
                for i in range(0, len(not_trading_positions), 1):
                    actions.append(StopExecutorAction(controller_id = self.config.id, executor_id = not_trading_positions[i].id))
        return actions

    def get_level_entry_price(self, level_id: int) -> Decimal:
        if level_id == 0:
            return self.get_entry_price()
        return self.get_level_entry_price(level_id-1) * Decimal(1 - self.config.step)

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        create_actions = []
        all_executors = self.get_all_executors()

        active_positions = self.filter_executors(executors=all_executors, filter_func=lambda x: x.is_active)
        
        self.logger().info(f"----------------------TICK--------------------------")
        
        # # LEVEL |ENTRY PRICE|AMOUNT VALIDATION
        # for executor in active_positions:
        #     self.logger().info(f"Active: {executor.config.level_id} | {executor.config.amount} | {executor.config.entry_price} {self.get_level_entry_price(int(executor.config.level_id))}")

        entry_price = self.get_entry_price()
        order_amount = self.config.initial_margin * self.config.leverage / self.config.levels / entry_price

        # IF no executors running start the bot
        if len(all_executors) == 0:
            for i in range(0, self.config.levels, 1):
                create_actions.append(CreateExecutorAction(controller_id = self.config.id,  executor_config = self.get_executor_config(self.get_level_entry_price(i), order_amount, i)))
        else:
            # Check if there is at least one executor per each level
            if len(active_positions) < self.config.levels:
                self.logger().info("Missing Level Somewhere: " )
                for i in range(0, self.config.levels, 1):
                    level_positions = self.filter_executors(executors = active_positions, filter_func = lambda x: x.config.level_id == str(i))
                    if len(level_positions) == 0:
                        self.logger().info(f"Missing level: {i} | {self.get_level_entry_price(i)}")
                        create_actions.append(CreateExecutorAction(controller_id = self.config.id, executor_config = self.get_executor_config(self.get_level_entry_price(i), order_amount, i)))

        # Trailing if the condition is met
        top_executor = max(active_positions, default = None, key = lambda x: x.config.entry_price)
        if not top_executor is None and not top_executor.is_trading and time.time() - top_executor.timestamp > self.config.executor_refresh_time: 
            self.logger().info("Trailing: Creating")
            create_actions.append(CreateExecutorAction(controller_id = self.config.id, executor_config = self.get_executor_config(top_executor.config.entry_price * Decimal(1 + self.config.step), top_executor.config.amount, 0)))
            for executor in active_positions:
                executor.config.level_id = str(int(executor.config.level_id) + 1)
            asyncio.create_task(self.update_yml(top_executor.config.entry_price * Decimal(1 + self.config.step)))
        
        
        
        # # TODO DELETE THIS MESS FOR TESTING BELLOW
        # #Get active_position with level_id 2
        # position_to_delete = self.filter_executors(executors = active_positions, filter_func = lambda x: x.config.level_id == '2')
        # if len(position_to_delete) > 0 and time.time() - position_to_delete[0].timestamp > (self.config.executor_refresh_time / 6): # TODO REALLY DELETE THIS DIVISION
        #     self.logger().info(f"TESTING DELETE 3rd Active: {position_to_delete[0].config.level_id} | {position_to_delete[0].config.amount} | {position_to_delete[0].config.entry_price} {self.get_level_entry_price(int(position_to_delete[0].config.level_id))}")
        #     create_actions.append(StopExecutorAction(controller_id = self.config.id, executor_id = position_to_delete[0].id))

        return create_actions
    
    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        stop_actions = []
        all_executors = self.get_all_executors()

        # CLOSE last executor when trailing
        active_executors = self.filter_executors(executors=all_executors, filter_func=lambda x: x.is_active)
        if len(active_executors) > self.config.levels:
            self.logger().info("Trailing: Removing last")
            bot_executor = min(active_executors, key = lambda x: x.config.entry_price)
            stop_actions.append(StopExecutorAction(controller_id = self.config.id, executor_id = bot_executor.id))

        stop_actions.extend(self.check_if_positions_match_initial_margin(active_executors = active_executors))

        return stop_actions


    async def update_yml(self, entry_price):
        if entry_price > 0:
            self.logger().info(f"Updating yaml")
            full_path = os.path.join(settings.CONTROLLERS_CONF_DIR_PATH, self.config.config_file_name)
            with open(full_path, 'r') as file:
                config_data = yaml.safe_load(file)
                config_data['entry_price'] = float(entry_price)
            with open(full_path, 'w+') as file:
                yaml.dump(config_data, file)
    
    def get_executor_config(self, entry_price: Decimal, order_amount: Decimal, level_id: str) -> PositionExecutorConfig:
        return PositionExecutorConfig(
            level_id = level_id,
            timestamp = time.time(),
            trading_pair = self.config.trading_pair,
            connector_name = self.config.connector_name,
            side = TradeType.BUY,
            amount = order_amount,
            entry_price = entry_price,
            triple_barrier_config = self.config.triple_barrier_config,
            leverage = self.config.leverage,
        )
