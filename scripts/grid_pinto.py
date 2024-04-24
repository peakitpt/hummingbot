import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import os
import time
from typing import Dict, List, Set

from hummingbot.core.event.event_forwarder import SourceInfoEventForwarder
from hummingbot.core.event.events import MarketEvent
from pydantic import Field

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.clock import Clock
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.smart_components.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase


class GridPintoConfig(StrategyV2ConfigBase):
    script_file_name: str = Field(default_factory=lambda: os.path.basename(__file__))
    candles_config: List[CandlesConfig] = []
    markets: Dict[str, Set[str]] = {}
    reinvestment_profit_percentage: int
    funding_profit_percentage: int
    spot_profit_percentage: int


class GridPinto(StrategyV2Base):
    
    start_time: int = 0


    def __init__(self, connectors: Dict[str, ConnectorBase], config: GridPintoConfig):
        super().__init__(connectors, config)
        self.config = config
        self.start_time = int(time.time())
        
        
        self._complete_sell_order_forwarder = SourceInfoEventForwarder(self.process_order_completed_event)

        for connector in self.market_data_provider.connectors.values():
            connector.add_listener(MarketEvent.SellOrderCompleted, self._complete_sell_order_forwarder)
            # connector.add_listener(MarketEvent.SellOrderCompleted, self._complete_sell_order_forwarder)
            

    def process_order_completed_event(self, event_tag: int, market, evt):
        # check event_tag for type
        event_trading_pair = None
        current_event = MarketEvent(event_tag)
        
        # # SellOrderCompleted | BuyOrderCompleted - base_asset + _ +  quote_asset = trading_pair
        if current_event == MarketEvent.BuyOrderCompleted or current_event == MarketEvent.SellOrderCompleted:
            event_trading_pair = evt.base_asset + "-" + evt.quote_asset
        else:
            if current_event != MarketEvent.OrderCancelled:
                event_trading_pair = evt.trading_pair
        


        if event_trading_pair:
            self.logger().info(f" Logged event {event_tag} | {event_trading_pair} | {evt.order_id} ")
            try:
                for controller_id, controller_generic in self.controllers.items():
                    if controller_generic.config.trading_pair == event_trading_pair:
                        controller = self.controllers[controller_id] 
                        all_executors = controller.get_all_executors()
                        active_executors = controller.filter_executors(executors=all_executors, filter_func=lambda x: x.is_active)
                        self.logger().info(f"{controller.config.trading_pair} - EVT Active: {len(active_executors)} | {len(all_executors)}")    

                        pair_executors = self.executor_orchestrator.executors[controller_id]
                        if current_event == MarketEvent.SellOrderCompleted:
                            for executor in pair_executors:
                                if executor._take_profit_limit_order is not None and executor._take_profit_limit_order.order_id == evt.order_id:
                                    event_actions = []
                                    
                                    all_executors = controller.get_all_executors()
                                    active_executors = controller.filter_executors(executors=all_executors, filter_func=lambda x: x.is_active)
                                    
                                    level_id = int(executor.config.level_id.split('_')[1])
                                    event_actions = []
                                    event_actions.extend(controller.recreate_level(active_executors, level_id))
                                    
                                    # Send actions
                                    asyncio.create_task(controller.send_actions(event_actions))

                    
            except Exception as e:
                self.logger().error(f"Error processing event {evt} | {e}")
        
    def start(self, clock: Clock, timestamp: float) -> None:
        """
        Start the strategy.
        :param clock: Clock to use.
        :param timestamp: Current time.
        """
        self._last_timestamp = timestamp
        self.apply_initial_setting()

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        current_tick_count = int(time.time()) - self.start_time
        
        if current_tick_count % 10 == 0: # Check every minute
            self.logger().info("--------MAIN TICK " + str(current_tick_count) + "----------------")
            # asyncio.create_task(self.analyze_profits())

        return []

    def apply_initial_setting(self):
        for controller_id, controller in self.controllers.items():
            config_dict = controller.config.dict()
            if self.is_perpetual(config_dict.get("connector_name")):
                asyncio.create_task(self.get_account(config_dict["connector_name"]))
                if "position_mode" in config_dict:
                    self.connectors[config_dict["connector_name"]].set_position_mode(config_dict["position_mode"])
                if "leverage" in config_dict:
                    self.connectors[config_dict["connector_name"]].set_leverage(leverage=config_dict["leverage"],
                                                                                trading_pair=config_dict["trading_pair"])



    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        return []




# TESTING.....
# TESTING.....
# TESTING.....
    async def analyze_profits(self) -> None:
        
        self.logger().info(self.config)
        if self.config.reinvestment_profit_percentage > 0:
            self.logger().info(f"Reinvestment Profit: {self.config.reinvestment_profit_percentage}%")
        if self.config.spot_profit_percentage > 0:
            self.logger().info(f"Spot Profit: {self.config.spot_profit_percentage}%")
        if self.config.funding_profit_percentage > 0:
            self.logger().info(f"Funding Profit: {self.config.funding_profit_percentage}%")
        
        
        # Get previous day profits
        
        self.logger().info(vars(self))
        if len(self.connectors) > 0:
            first_connector_key = next(iter(self.connectors), None)
            self.logger().info('first_connector_key')
            self.logger().info(first_connector_key)
            if first_connector_key is not None:
                self.logger().info('first_connector_key')                
                income = await self.get_income_history(first_connector_key)
                # ONLY IF PROFITS ARE ABOVE 0
                
                if income is not None and income['pnl'] is not None and income['pnl'] > 0:
                    self.logger().info(f"MONEY TO SPARE: {income['pnl']}")
                    
                    # SPLIT THE MONEY TO SPARE
                    # REINVESTMENT
                    reinvestment_ammount = income['pnl'] * self.config.reinvestment_profit_percentage / 100
                    self.logger().info(f"REINVESTMENT: {reinvestment_ammount}")
                    if reinvestment_ammount > 0:
                        asyncio.create_task(self.reinvest(reinvestment_ammount))
                    # SPOT
                    spot_ammount = income['pnl'] * self.config.spot_profit_percentage / 100
                    self.logger().info(f"SPOT: {spot_ammount}")
                    # if spot_ammount > 0:
                    #     asyncio.create_task(self.transfer_to_spot(first_connector_key, spot_ammount))
                    
                    # FUNDING
                    funding_ammount = income['pnl'] * self.config.funding_profit_percentage / 100
                    self.logger().info(f"FUNDING: {funding_ammount}")
                    if funding_ammount > 0:
                        asyncio.create_task(self.transfer_to_funding(first_connector_key, funding_ammount))
        
        pass
    
    
    
    def reinvest(self, reinvestment_ammount):
        # Get all the conontrollers
        # Split between all the active controllers
        # Update files
        pass
    
    async def transfer_to_spot(self, connector_key, spot_ammount):
        # NEEDS Enable Futures permission
        transfer = await self.connectors[connector_key]._api_get(
            params={
                "asset": 'USDT',
                "amount": spot_ammount,
                "type": 2,
            },
            path_url='v1/futures/transfer',
            is_auth_required=True)
        # Update files
        pass
    async def get_income_history(self, connector_key) -> None:
        self.logger().info("Income")
        # Get current date and time
        current_time = datetime.now()

        # Get yesterday's date
        # yesterday = current_time - timedelta(days=0)
        yesterday = current_time # Check for today

        # Set the time to 00:00:00
        start_of_yesterday = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        # Set the time to 23:59:59
        end_of_yesterday = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)

        income = await self.connectors[connector_key]._api_get(
            params={
                "startTime": int(start_of_yesterday.timestamp() * 1000),
                "endTime": int(end_of_yesterday.timestamp() * 1000),
                "limit" : 1000,
            },
            path_url='v1/income',
            is_auth_required=True)
        
        self.logger().info({
            "startTime": int(start_of_yesterday.timestamp() * 1000),
            "endTime": int(end_of_yesterday.timestamp() * 1000),
        })
        pnl = Decimal(0.0)
        realized_pnl = Decimal(0.0)
        commission = Decimal(0.0)
        other = Decimal(0.0)
        bnbpnl = Decimal(0.0)
        bnbrealized_pnl = Decimal(0.0)
        bnbcommission = Decimal(0.0)
        bnbother = Decimal(0.0)
        
        for line in income:
            self.logger().info(line)
            if line.get('asset') == 'BNB':
                bnbpnl += Decimal(line.get('income'))
                if line.get('incomeType') == 'REALIZED_PNL':
                    bnbrealized_pnl += Decimal(line.get('income'))
                elif line.get('incomeType') == 'COMMISSION':
                    bnbcommission += Decimal(line.get('income'))
                else:
                    bnbother += Decimal(line.get('income'))
            elif line.get('asset') == 'USDT':
                pnl += Decimal(line.get('income'))
                if line.get('incomeType') == 'REALIZED_PNL':
                    realized_pnl += Decimal(line.get('income'))
                elif line.get('incomeType') == 'COMMISSION':
                    commission += Decimal(line.get('income'))
                else:
                    other += Decimal(line.get('income'))

        self.logger().info(f"BNBpnl: {bnbpnl} | bnbrealized_pnl: {bnbrealized_pnl} | bnbcommission: {bnbcommission} | bnbother: {bnbother}")
        self.logger().info(f"Pnl: {pnl} | realized_pnl: {realized_pnl} | commission: {commission} | other: {other}")
        return {
            "pnl": pnl,
            "realized_pnl": realized_pnl,
            "commission": commission,
            "other": other,
            "bnbpnl": bnbpnl,
            "bnbrealized_pnl": bnbrealized_pnl,
            "bnbcommission": bnbcommission,
            "bnbother": bnbother
        }
    
    async def get_account(self, connector_key) -> None:
        self.logger().info("Getting account")
        account = await self.connectors[connector_key]._api_get(path_url='v2/account', is_auth_required=True)
        maintenance_margin = round((Decimal(account.get('totalMaintMargin')) / Decimal(account.get('totalMarginBalance'))) * 100, 2)
        self.logger().info(f"Account_data: {maintenance_margin}% | {Decimal(account.get('availableBalance'))} | {Decimal(account.get('maxWithdrawAmount'))} | {Decimal(account.get('totalCrossUnPnl'))}")
        return account
        