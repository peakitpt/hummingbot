import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import os
import time
from typing import Dict, List, Set

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
            self.logger().info("--------TICK " + str(current_tick_count) + "----------------")
            asyncio.create_task(self.analyze_profits())

        return []

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        return []


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
        if len(self.controllers) > 0:
            first_connector_key = next(iter(self.connectors), None)
            self.logger().info('first_connector_key')
            self.logger().info(first_connector_key)
            if first_connector_key is not None:
                self.logger().info('first_connector_key')                
                income = await self.get_income_history(first_connector_key)
                # ONLY IF PROFITS ARE ABOVE 0
                
                if income is not None and income.get('pnl') is not None and income.get('pnl') > 0:
                    self.logger().info(f"MONEY TO SPARE: {income.get('pnl')}")
        
        pass
    
    
    async def get_income_history(self, connector_key) -> None:
        self.logger().info("Income")
        # Get current date and time
        current_time = datetime.now()

        # Get yesterday's date
        yesterday = current_time - timedelta(days=1)

        # Set the time to 00:00:00
        start_of_yesterday = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        # Set the time to 23:59:59
        end_of_yesterday = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)

        income = await self.connectors[connector_key]._api_get(
            params={
                "startTime": start_of_yesterday.timestamp(),
                "endTime": end_of_yesterday.timestamp(),
            },
            path_url='v1/income',
            is_auth_required=True)
        
        pnl = Decimal(0.0)
        realized_pnl = Decimal(0.0)
        commission = Decimal(0.0)
        other = Decimal(0.0)
        for line in income:
            pnl += Decimal(line.get('income'))
            if line.get('incomeType') == 'REALIZED_PNL':
                realized_pnl += Decimal(line.get('income'))
            elif line.get('incomeType') == 'COMMISSION':
                commission += Decimal(line.get('income'))
            else:
                other += Decimal(line.get('income'))
            
        self.logger().info(f"Pnl: {pnl} | realized_pnl: {realized_pnl} | commission: {commission} | other: {other}")
        return {
            pnl,
            realized_pnl,
            commission,
            other
        }
    
    async def get_account(self, connector_key) -> None:
        self.logger().info("Getting account")
        account = await self.connectors[connector_key]._api_get(path_url='v2/account', is_auth_required=True)
        maintenance_margin = round((Decimal(account.get('totalMaintMargin')) / Decimal(account.get('totalMarginBalance'))) * 100, 2)
        self.logger().info(f"Account_data: {maintenance_margin}% | {Decimal(account.get('availableBalance'))} | {Decimal(account.get('maxWithdrawAmount'))} | {Decimal(account.get('totalCrossUnPnl'))}")
        return account
        
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
