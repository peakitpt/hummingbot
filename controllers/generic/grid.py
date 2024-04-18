import time
import asyncio
from decimal import Decimal
from typing import List, Tuple
import os
from hummingbot.client import settings
import yaml
from hummingbot.core.event.event_forwarder import SourceInfoEventForwarder
from hummingbot.core.event.events import MarketEvent
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

    def __init__(self, config: GridConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.start_time = int(time.time())
        self.logger().info("Start GRID controller")
        # self.market_data_provider.connectors[0]
        
        self._complete_sell_order_forwarder = SourceInfoEventForwarder(self.process_order_completed_event)
        self._event_pairs: List[Tuple[MarketEvent, SourceInfoEventForwarder]] = [
            (MarketEvent.OrderCancelled, self._complete_sell_order_forwarder),
            (MarketEvent.OrderFilled, self._complete_sell_order_forwarder), # Needed?
            # (MarketEvent.BuyOrderCompleted, self._complete_sell_order_forwarder), # No need to check
            (MarketEvent.SellOrderCompleted, self._complete_sell_order_forwarder), # On sell need to check trail and levels
            (MarketEvent.OrderFailure, self._complete_sell_order_forwarder), # Recreate level if failed?
        ]

        for connector in self.market_data_provider.connectors.values():
            for event_pair in self._event_pairs:
                connector.add_listener(event_pair[0], event_pair[1])

    def process_order_completed_event(self, event_tag: int, market, evt):
        # check event_tag for type
        event_trading_pair = None
        current_event = MarketEvent(event_tag)
        
        # SellOrderCompleted | BuyOrderCompleted - base_asset + _ +  quote_asset = trading_pair
        if current_event == MarketEvent.BuyOrderCompleted or current_event == MarketEvent.SellOrderCompleted:
            event_trading_pair = evt.base_asset + "-" + evt.quote_asset
        else:
            if current_event != MarketEvent.OrderCancelled:
                event_trading_pair = evt.trading_pair
        

        self.logger().info(f"Logged event {event_tag} | {event_trading_pair} | {self.config.trading_pair} | {evt}")

        # IF IS THIS PAIR OR CANCELLED( WICH DOES NOT RETURN PAIR) CHECK EVENT
        if event_trading_pair == self.config.trading_pair or current_event == MarketEvent.OrderCancelled:
        
            if current_event == MarketEvent.OrderCancelled:
                self.logger().info(f"A ORDER WAS CANCELED ")
                self.load_levels = True
            
            if current_event == MarketEvent.OrderFailure:
                self.load_levels = True
                self.logger().info(f"A ORDER FAILED ")

            if current_event == MarketEvent.OrderFilled:
                self.load_levels = True
                self.logger().info(f"A ORDER WAS FILLED ")
                
            if current_event == MarketEvent.SellOrderCompleted:
                self.logger().info(f"A position was sold, recreate levels")
                self.load_levels = True
                self.load_trailing = True
        
        
    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions = []
        if self.config.is_enabled:
            actions.extend(self.create_actions_proposal())
            actions.extend(self.stop_actions_proposal())
        else:
            actions.extend(self.panic_close_all())
            
        return actions

    def panic_close_all(self) -> List[StopExecutorAction]:
        stop_actions = []
        all_executors = self.get_all_executors()
        active_positions = self.filter_executors(executors=all_executors, filter_func=lambda x: x.is_active)
        for executor in active_positions:
            stop_actions.append(StopExecutorAction(controller_id = self.config.id, executor_id = executor.id))
        return stop_actions
        
    async def update_processed_data(self):
        reference_price = self.market_data_provider.get_price_by_type(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        self.processed_data = {"reference_price": reference_price, "spread_multiplier": Decimal("1")}

    def get_all_executors(self) -> List[ExecutorInfo]:
        return self.filter_executors(executors = self.executors_info, filter_func = lambda x: x.trading_pair == self.config.trading_pair and x.side == TradeType.BUY and x.type == "position_executor")

    def get_entry_price(self, update_file = False) -> Decimal:
        entry_price = 0
        if self.config.entry_price > 0:
            entry_price = self.config.entry_price
        else:
            entry_price = self.market_data_provider.get_price_by_type(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            if update_file:
                asyncio.create_task(self.update_yml(entry_price))
      
        return entry_price

    def check_if_positions_match_initial_margin(self, active_executors) -> List[CreateExecutorAction]:
        actions = []
        # IF ANY ACTIVE POSITION, VALIDATE IF THE INITIAL MARGIN THAT THE POSITION WAS MADE BASED ON HAS NOT CHANGED
        not_trading_positions = self.filter_executors(executors=active_executors, filter_func=lambda x: not x.is_trading)
        if len(not_trading_positions) > 0:
            # Check if initial margin has changed in first open position
            initial_margin = round(not_trading_positions[0].config.amount * self.get_entry_price() * self.config.levels / self.config.leverage)
            # INITIAL MARGIN CHANGED, STOP THE POSITIONs (System should create new ones)
            if (self.config.initial_margin + self.config.reinvested) != initial_margin:
                self.logger().info(f"Initial margin changed from {initial_margin} to {self.config.initial_margin + self.config.reinvested} ")
                for i in range(0, len(not_trading_positions), 1):
                    actions.append(StopExecutorAction(controller_id = self.config.id, executor_id = not_trading_positions[i].id))

        return actions

    def get_level_entry_price(self, level_id: int) -> Decimal:
        if level_id == 0:
            return self.get_entry_price()
        return self.get_level_entry_price(level_id-1) * Decimal(1 - self.config.step)

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        create_actions = []
        if self.load_levels or self.load_trailing:
            # # LEVEL |ENTRY PRICE|AMOUNT VALIDATION
            # for executor in active_positions:
            #     self.logger().info(f"Active: {executor.config.level_id} | {executor.config.amount} | {executor.config.entry_price} {self.get_level_entry_price(int(executor.config.level_id))}")
            current_tick_count = int(time.time()) - self.start_time

            all_executors = self.get_all_executors()
            active_positions = self.filter_executors(executors=all_executors, filter_func=lambda x: x.is_active)
            
            self.logger().info(f"---------TICK------- { current_tick_count } Active: {len(active_positions)} {self.config.trading_pair} " )
            if self.load_levels:
                self.load_levels = False
                create_actions.extend(self.make_missing_levels_actions(active_positions))
            else: 
                self.load_trailing = False
                create_actions.extend(self.make_trailing_actions(active_positions))
    
        return create_actions
    
    def make_missing_levels_actions(self, active_positions) -> List[CreateExecutorAction]:        
        actions = []
        # Check if there is at least one executor per each level
        entry_price = self.get_entry_price(update_file=True)
        if len(active_positions) < self.config.levels:
            order_amount = (self.config.initial_margin + self.config.reinvested) * self.config.leverage / self.config.levels / entry_price
            for i in range(0, self.config.levels, 1):
                level_positions = self.filter_executors(executors = active_positions, filter_func = lambda x: x.config.level_id == str(i))
                if len(level_positions) == 0:
                    actions.append(CreateExecutorAction(controller_id = self.config.id, executor_config = self.get_executor_config(self.get_level_entry_price(i), order_amount, i)))
        
        return actions
    
    
    def make_trailing_actions(self, active_positions) -> List[CreateExecutorAction]:
        actions = []
        # Trailing if the condition is met
        top_executor = max(active_positions, default = None, key = lambda x: x.config.entry_price)
        if not top_executor is None and not top_executor.is_trading and time.time() - top_executor.timestamp > self.config.executor_refresh_time: 
            actions.append(CreateExecutorAction(controller_id = self.config.id, executor_config = self.get_executor_config(top_executor.config.entry_price * Decimal(1 + self.config.step), top_executor.config.amount, 0)))
            for executor in active_positions:
                executor.config.level_id = str(int(executor.config.level_id) + 1)
            
            asyncio.create_task(self.update_yml(top_executor.config.entry_price * Decimal(1 + self.config.step)))

        return actions
    
    
    def make_trailing_stop_actions(self, active_positions) -> List[StopExecutorAction]:
        actions = []
        if len(active_positions) > self.config.levels:
            # self.logger().info("Trailing: Removing last")
            bot_executor = min(active_positions, key = lambda x: x.config.entry_price)
            actions.append(StopExecutorAction(controller_id = self.config.id, executor_id = bot_executor.id))
        return actions
    
   
    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        stop_actions = []
        all_executors = self.get_all_executors()
        active_executors = self.filter_executors(executors=all_executors, filter_func=lambda x: x.is_active)

        stop_actions.extend(self.make_trailing_stop_actions(active_executors))
        stop_actions.extend(self.check_if_positions_match_initial_margin(active_executors = active_executors))

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