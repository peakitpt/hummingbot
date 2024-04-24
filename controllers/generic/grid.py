import time
import asyncio
from decimal import Decimal
from typing import List, Tuple
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
    
    checks_offset: int = Field(
      default=0, ge=0,
      client_data=ClientFieldData(
          is_updatable=True,
        prompt=lambda mi: "Enter the check time offset in secconds (e.g. 0) ",
        prompt_on_new=True))
    
    checks_interval: int = Field(
      default=10, gt=0,
      client_data=ClientFieldData(
          is_updatable=True,
        prompt=lambda mi: "Enter the check time interval in secconds (e.g. 10) ",
        prompt_on_new=True))
    
    # Flag to indicate if pair is able to run
    # If False disabled by user or another type of limit on main strategy
    is_enabled: bool = Field( 
      default=True, 
      client_data=ClientFieldData(
          is_updatable=True,
        prompt_on_new=False))  
    
    # Used in runtime to save the ammount added to the inital margin
    # Separate of initial_margin so that it can return to zero when needed
    reinvested: Decimal = Field(
        default=0, ge=0,
        client_data=ClientFieldData(is_updatable=True, prompt_on_new=False))
    

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
    start_time: int = 0
    load_levels: bool = True # True to run on first tick, then will be reset
    load_trailing: bool = True # True to run on first tick, then will be reset
    # load_maring_update_check: bool = False # False, no maigin check on first tick
    forced_checks_interval: int = 600 # TIME IN SECCONDS
    
    def __init__(self, config: GridConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.update_interval = 10
        self.config = config
        self.start_time = int(time.time())
        self.logger().info(f"{self.config.trading_pair} - Start GRID controller")

        
    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions = []
        current_tick_count = int(time.time()) - self.start_time

 
        # if (current_tick_count + self.config.checks_offset) % self.forced_checks_interval == 0: # Mandatory check every minute
        if current_tick_count % self.forced_checks_interval == 0: # Mandatory check every minute
            self.load_levels = True
            self.load_trailing = True

        if self.config.is_enabled:
            if self.load_levels or self.load_trailing:
                all_executors = self.get_all_executors()
                active_executors = self.filter_executors(executors=all_executors, filter_func=lambda x: x.is_active)
                self.logger().info(f"{self.config.trading_pair} - -----TICK {current_tick_count} Active: {len(active_executors)} | {len(all_executors)} -----")
                
                actions.extend(self.stop_actions_proposal(active_executors))
                actions.extend(self.create_actions_proposal(active_executors))
        else:
            actions.extend(self.panic_close_all())
            
        return actions

    def panic_close_all(self) -> List[StopExecutorAction]:
        stop_actions = []
        all_executors = self.get_all_executors()
        active_executors = self.filter_executors(executors=all_executors, filter_func=lambda x: x.is_active)
        for executor in active_executors:
            stop_actions.append(StopExecutorAction(controller_id = self.config.id, executor_id = executor.id))
        return stop_actions
        
    async def update_processed_data(self):
        reference_price = self.market_data_provider.get_price_by_type(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        self.processed_data = {"reference_price": reference_price, "spread_multiplier": Decimal("1")}

    def get_all_executors(self) -> List[ExecutorInfo]:
        return self.filter_executors(executors = self.executors_info, filter_func = lambda x: x.side == TradeType.BUY and x.type == "position_executor")

    def get_entry_price(self, update_file = False) -> Decimal:
        entry_price = 0
        if self.config.entry_price > 0:
            entry_price = self.config.entry_price
        else:
            entry_price = self.market_data_provider.get_price_by_type(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            if update_file:
                self.config.entry_price = entry_price
                asyncio.create_task(self.update_yml(entry_price))
      
        return entry_price


    def get_entry_price_from_level_id(self, level_id: int) -> Decimal:
        entry_price = self.get_entry_price()
        if level_id == 0:
            return entry_price
        else:
            factor = Decimal(1 - self.config.step)
            return entry_price * (factor ** level_id)

    
    def calculate_initial_margin(self) -> Decimal:
        initial_margin = self.config.initial_margin + self.config.reinvested
        return initial_margin
    
    def create_actions_proposal(self, active_executors) -> List[CreateExecutorAction]:
        create_actions = []

        if self.load_levels:
            self.load_levels = False
            create_actions.extend(self.make_missing_levels_actions(active_executors))

        return create_actions
    
    
    # Check if there is at least one executor per each level
    def make_missing_levels_actions(self, active_executors) -> List[CreateExecutorAction]:        
        actions = []
        entry_price = self.get_entry_price(update_file=True)
        initial_margin = self.calculate_initial_margin()
        
        if len(active_executors) < self.config.levels:
            self.logger().info(f"{self.config.trading_pair} - Creating missing levels")
            order_amount = initial_margin * self.config.leverage / self.config.levels / entry_price
            for i in range(0, self.config.levels, 1):
                level_id_str = self.make_level_id_str(i)
                level_positions = self.filter_executors(executors = active_executors, filter_func = lambda x: x.config.level_id == level_id_str)
                if len(level_positions) == 0:
                    actions.append(CreateExecutorAction(controller_id = self.config.id, executor_config = self.get_executor_config(self.get_entry_price_from_level_id(i), order_amount, level_id_str)))
        
        return actions
    
    
    def make_level_id_str(self, level_id: int) -> str:
        return self.config.trading_pair + '_' + str(level_id)
    
    
    def recreate_level(self, active_executors, level_id) -> List[CreateExecutorAction]:        
        actions = []
        entry_price = self.get_entry_price(update_file=True)
        initial_margin = self.calculate_initial_margin()        
        order_amount = initial_margin * self.config.leverage / self.config.levels / entry_price
        level_positions = self.filter_executors(executors = active_executors, filter_func = lambda x: x.config.level_id == self.make_level_id_str(level_id))
         
        self.logger().info(f"{self.config.trading_pair} - Creating missing level {level_id}")
        
        if len(level_positions) > 0:
            for level_position in level_positions:
                level_position.config.level_id = 'OLD_' + level_position.config.level_id
                self.logger().info(f"{self.config.trading_pair} - Rename old position executor {vars(level_position)}")
                    
        actions.append(CreateExecutorAction(controller_id = self.config.id, executor_config = self.get_executor_config(self.get_entry_price_from_level_id(level_id), order_amount, self.make_level_id_str(level_id))))
            
        return actions
    
    
    # Trailing function will remove last order , increment all levels and create new top level
    # Only should happen if every executor is not trading and the time since last update is greater than the refresh time
    def make_trailing_actions(self, active_executors) -> List[ExecutorAction]:
        actions = []
        # Trailing if the condition is met
        top_executor = max(active_executors, default = None, key = lambda x: x.config.entry_price)
        if not top_executor is None and not top_executor.is_trading and (time.time() - top_executor.timestamp + 4) > self.forced_checks_interval: 
            self.logger().info(f"{self.config.trading_pair} - Trailing.... ")
            # UPDATE LEVEL ID
            for executor in active_executors:
                current_level_id = int(executor.config.level_id.split('_')[1]) 
                executor.config.level_id = self.make_level_id_str(current_level_id + 1)
                
            # Update entry price
            new_entry_price = top_executor.config.entry_price * Decimal(1 + self.config.step)
            self.config.entry_price = new_entry_price
            asyncio.create_task(self.update_yml(new_entry_price))
            # Create New LEVEL
            actions.extend(self.recreate_level(active_executors, 0))
            # DELETE LAST LEVEL
            bottom_executor = min(active_executors, default = None, key = lambda x: x.config.entry_price)
            actions.append(StopExecutorAction(controller_id = self.config.id, executor_id = bottom_executor.id))
        return actions
    
   
    def stop_actions_proposal(self, active_executors) -> List[StopExecutorAction]:
        stop_actions = []

        if self.load_trailing:
            self.load_trailing = False
            stop_actions.extend(self.make_trailing_actions(active_executors))
            self.logger().info(f"{self.config.trading_pair} - Check if should be trailing")

        return stop_actions


    async def update_yml(self, entry_price):
        if entry_price > 0:
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