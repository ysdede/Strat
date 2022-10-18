import math
import os
import sys
import datetime
from math import log2, log10
import json
from jesse.strategies import Strategy as Vanilla, cached
from jesse.helpers import is_live
import requests
from jesse import utils
from importlib.metadata import version

try:
    import JesseTradingViewLightReport
except:
    pass

dec = {
    "0": 0,
    "0.1": 1,
    "0.01": 2,
    "0.001": 3,
    "0.0001": 4,
    "0.00001": 5,
    "0.000001": 6,
    "0.0000001": 7,
    "0.00000001": 8,
}
ftx_terms = """
    Position notional: position size * MP
    IMF Factor: multiplier on the margin required for a coin.
    Base IMF: the minimum initial margin fraction needed.  This is 1 / maximum leverage.
    Position initial margin fraction: max(Base IMF, IMF factor * sqrt(position open size)) * IMF Weight
    Position maintenance margin fraction: max(3%, 0.6 * IMF Factor * abs(sqrt(position size)))
    Position unrealized PNL:  size * (future mark price - position entry price)
    Collateral: See here for more details. 
    Free collateral: amount of USD that can be withdrawn from exchange, =min(collateral, collateral + unrealized PNL) - [amount of collateral tied up in open orders]
    Total account value: collateral + unrealized pnl
    Total position notional: sum of abs(position notional) across all positions + sum of margin borrows
    Margin fraction [MF]: total account value / total position notional
    Maintenance Margin Fraction Requirement [MMF]: the minimum MF needed to avoid getting liquidated, equal to average of position MMF weighed by position notional
    Auto Close Margin Fraction [ACMF]: the minimum MF needed to avoid getting closed against the backstop liquidity provider or other users, = max(MMF / 2, MMF - 0.06)
    Zero Price (ZP): MP * (1 - MF) if long, MP * (1 + MF) if short.  The mark price that would set an account’s total account value to 0.
    Liquidation Distance: % move in futures that would make MF = MMF.
    Position open size: Max(abs(size if all buy orders get filled), abs(size if all sell orders get filled))
    Position open notional: position open size * MP
    Total open position notional: sum of abs(position open notional) across all positions
    Open margin fraction [OMF]: max(0,(min(total account value, collateral)- size from spot margin open orders)) / total open position notional 
    Initial Margin Fraction Requirement [IMF]: the minimum OMF needed to increase position size, equal to average of position IMF for all account positions weighed by position open notional
    Unused collateral: max(OMF - IMF, 0) * total open position notional
    Backstop Liquidity Provider [BLP]: an account that promises to take on liquidating accounts’ positions

    Notes:
    
    sum(value1, value2,..) = sum total of the values within the data set provided
    max(value1, value2,..) = the maximum value within the data set provided
    min(value1, value2,..) = the minimum value within the data set provided
    abs(value) = the absolute value of the data provided
    Open position means that the position has been filled and has not been closed yet. Open order means that that the order has been submitted, but has not been filled yet.
    Mark price = median of best ask, best bid, and last traded price.
    Your margin and collateral is segregated per subaccount. Therefore, throughout this article, account = subaccount. 
    Much of this article is an approximation and ignores details, e.g. fees.
"""


class Strat(Vanilla):
    """
    The proxy strategy class which adds extra methods to Jesse base strategy.
    """

    def __init__(self):
        super().__init__()
        print(f"Standalone Strategy Template v. {version('strat')}")

        ex_exchanges = [
            "Binance Futures",
            "Binance",
            "Bybit Perpetual",
            "FTX Futures",
            "FTX",
        ]
        exchange_codes = {
            "Binance Perpetual Futures": "Binance Futures",
            "Binance Spot": "Binance",
            "Bybit USDT Perpetual": "Bybit Perpetual",
            "FTX Perpetual Futures": "FTX Futures",
            "FTX Spot": "FTX",
        }

        self.trade_rule_urls = {
            "Binance": "https://api.binance.com/api/v1/exchangeInfo",
            "Binance Futures": "https://fapi.binance.com/fapi/v1/exchangeInfo",
            "Bybit Perpetual": "https://api.bybit.com/v2/public/symbols",
            "FTX": "https://ftx.com/api/markets",
        }

        # Trading rules variables
        self.quantityPrecision = 3
        self.minQty = 0.01
        self.notional = 6
        self.stepSize = 0.01
        self.pricePrecision = 6

        # Kill Switch and break even exit variables
        self.break_even_file = None
        self.pause_file = None

        # Shared variables
        self.shared_vars["ts"] = 0
        self.shared_vars["locked_balance"] = 0
        self.shared_vars["free_balance"] = 0
        self.shared_vars["previous_balance"] = 0
        self.shared_vars["min_margin"] = 0
        self.shared_vars["max_margin_ratio"] = 0
        self.shared_vars["max_lp_ratio"] = float("nan")
        self.shared_vars["margin_alert"] = "False"
        self.shared_vars["total_value"] = 0
        self.shared_vars["unrealized_pnl"] = 0
        self.shared_vars["margin_balance"] = 0
        self.shared_vars["maint_margin"] = 0
        self.shared_vars["margin_ratio"] = 0
        self.shared_vars["max_total_value"] = 0
        self.shared_vars["run_once_multi_routes"] = True

        self.max_position_value = 0
        self.active = False
        self.trade_ts = None
        self.first_run = True

        self.max_open_positions = 0
        self.current_cycle_positions = 0

        self.insuff_margin_count = 0
        self.max_insuff_margin_count = 0
        self.unique_insuff_margin_count = 0

        self.resume = False

        # Settings:
        self.log_enabled = False
        self.debug_enabled = True
        self.trade_with_bybit_rules = False
        self.margin_ratio_treshold = 97
        # enable_boosting replaced with trade_minimum variable
        self.trade_minimum = False
        self.keep_running_in_case_of_liquidation = False
        self.fixed_margin_ratio = None
        self.use_initial_balance = False

        try:
            from dotenv import load_dotenv

            load_dotenv()
        except Exception:
            pass

        try:
            self.wallets_dc_hook = os.getenv("WALLETS_DC_HOOK")
        except Exception:
            self.wallets_dc_hook = None

        try:
            self.app_port = os.getenv("APP_PORT")
        except Exception:
            self.app_port = None

        self.binance_lev_brackets = None
        self.bybit_risk_limits = None
        self.ftx_risk_limits = None

    def before(self) -> None:
        if self.first_run:
            self.run_once()

    def run_once(self):
        try:
            if self.is_open:
                self.resume = True
                self.console(
                    f"💾 Found open position at init. Position id: {self.position.id}. Resuming..."
                )
                self.console(f"{self.position.qty=}, {self.position.value=}")
                self.load_session_from_pickle()
        except Exception as e:
            self.console("Exception when checking open positions.")
            self.console(e)
        # Quick fix for Crude Oil rules. Use BTC rules for BCO.
        self._symbol = self.symbol.replace("BCO-", "BTC-")

        if self.symbol.endswith("-USD"):
            self._symbol = self.symbol.replace("-USD", "-USDT")

        if self.symbol.endswith("-PERP"):
            self._symbol = self.symbol.replace("-PERP", "-USDT")

        # If exchange rule files are not present or we're trading live, download them
        exc = "Bybit Perpetual" if self.trade_with_bybit_rules else self.exchange

        local_fn = f"{exc.replace(' ', '')}ExchangeInfo.json".replace(
            "BinanceExch", "BinanceFuturesExch"
        )

        # exchange_codes = {'Binance Perpetual Futures': 'Binance Futures', 'Binance Spot': 'Binance', 'Bybit USDT Perpetual': 'Bybit Perpetual', 'FTX Perpetual Futures': 'FTX Futures', 'FTX Spot': 'FTX'}
        if (
            self.exchange == "Bybit Perpetual"
            or self.exchange == "Bybit USDT Perpetual"
            or self.trade_with_bybit_rules
        ):
            if not os.path.exists(local_fn) or is_live():
                self.download_rules(exchange="Bybit Perpetual")
            rules = self.bybit_rules()
        elif self.exchange == "FTX Futures" or self.exchange == "FTX Perpetual Futures":
            if not os.path.exists(local_fn) or is_live():
                self.download_rules(exchange="FTX")
                self.ftx_risk_limits = self.risk_limits()
            rules = self.ftx_rules()
        else:
            # Fall back to Binance Perp rules if exchange != bybit or ftx
            if not os.path.exists(local_fn) or is_live():
                self.download_rules(exchange="Binance Futures")
            rules = self.binance_rules()

        self.minQty = float(rules["minQty"])
        self.notional = float(rules["notional"])
        self.stepSize = float(rules["stepSize"])
        self.pricePrecision = int(rules["pricePrecision"])
        self.quantityPrecision = int(rules["quantityPrecision"])  # Base asset precision

        self.console(
            f"Rules set for {self.exchange}, Rules Hack: {self.trade_with_bybit_rules}, quantityPrecision:{self.quantityPrecision}, minQty:{self.minQty}, notional:{self.notional} stepSize:{self.stepSize} pricePrecision:{self.pricePrecision}"
        )
        self.console("Trading Mode.") if is_live() else self.console(
            "Not Trading Mode.", False
        )

        self.test_leverage()

        self.initial_balance = self.balance
        self.break_even_file = f"{self.symbol}.break"
        self.pause_file = f"{self.symbol}.pause"

        # Init
        self.shared_vars["locked_balance"] = 0
        self.shared_vars["free_balance"] = self.balance
        self.shared_vars["previous_balance"] = self.balance
        self.shared_vars["min_margin"] = self.available_margin
        self.shared_vars["max_margin_ratio"] = 0
        self.shared_vars["max_lp_ratio"] = float("nan")
        self.shared_vars["margin_alert"] = "False"
        self.shared_vars["ts"] = 0
        self.shared_vars["total_value"] = 0
        self.shared_vars["unrealized_pnl"] = 0
        self.shared_vars["margin_balance"] = 0
        self.shared_vars["maint_margin"] = 0
        self.shared_vars["max_total_value"] = 0
        self.shared_vars["max_margin_ratio_ts"] = None

        self.update_shared_vars("runonce")

        self.first_run = False

    @property
    def ftx(self):
        return "ftx" in self.exchange.lower()

    def update_shared_vars(self, caller=None):

        self.shared_vars[self.symbol] = {
            "active": str(self.active),
            "is_open": str(self.is_open),
            "pos_value": round(self.position.value, 6) if self.is_open else 0,
            "pnl": round(self.position.pnl, 6) if self.is_open else 0,
            "pnl%": round(self.position.pnl_percentage, 6) if self.is_open else 0,
            #  'InsufMargin': str(self.available_margin < self.cycle_pos_size * self.boost),
            "max_open": self.max_open_positions,
            "cycle_pos": round(self.current_cycle_positions, 2),
            "maintenance_margin": round(self.maintenance_margin, 6),
            "collateral_used": round(self.collateral_used, 6)
            if self.is_open and self.ftx
            else 0,
            "position_imf": round(self.position_imf, 6)
            if self.is_open and self.ftx
            else 0,
            "position_mmf": round(self.position_mmf, 6)
            if self.is_open and self.ftx
            else 0,
            "position_notional": round(self.position_notional, 6)
            if self.is_open and self.ftx
            else 0,
            "maintenance_collateral": round(self.maintenance_collateral, 6)
            if self.is_open and self.ftx
            else 0,
        }
        # We need to store maintenance margin per route to call from other routes. See above. (Needed for Liquidation Price Calculation)

        self.max_position_value = max(
            self.max_position_value, self.shared_vars[self.symbol]["pos_value"]
        )  # Indiviual position value
        self.shared_vars["ts"] = self.ts
        self.shared_vars["total_value"] = self.get_total_value
        self.shared_vars["unrealized_pnl"] = self.unreal_pnl
        self.shared_vars["margin_balance"] = self.margin_balance
        self.shared_vars["maint_margin"] = self.maintenance_margin
        self.shared_vars["margin_ratio"] = self.margin_ratio(caller)
        self.shared_vars["min_margin"] = min(
            self.shared_vars["min_margin"], self.available_margin
        )
        self.shared_vars["lp_rate"] = self.lp_rate()
        self.shared_vars["insuff_margin_count"] = self.insuff_margin_count
        # self.shared_vars['max_lp_ratio'] = max(self.shared_vars['max_lp_ratio'], self.lp_rate())
        self.max_insuff_margin_count = max(
            self.max_insuff_margin_count, self.insuff_margin_count
        )

    def min_order_size(self):
        """Calculates the minimum order size for the current symbol/exchange rule.
        Returns:
            float: minimum allowed quantity in base asset
            float: minimum allowed position size in quote asset
        """
        cycle_pos_size = 0
        # If USD value of minQTY is greater than minimum notional, use minQTY.
        # Convert minQTY to dollar size and add potential fees before converting back to qty.
        if self.minQty * self.close >= self.notional:
            self.console(
                f"minQty * close > notional: {self.minQty * self.close} > {self.notional}",
                False,
            )
            qty = self.minQty
            cycle_pos_size = qty * self.close
            fees = cycle_pos_size * self.fee_rate * 6
            cycle_pos_size += fees
            cycle_pos_size *= 1.05
            qty = utils.size_to_qty(
                cycle_pos_size,
                self.close,
                precision=self.quantityPrecision,
                fee_rate=self.fee_rate,
            )
            self.console(
                f"⚖ Calculate minimum by Qty {self.close=}, {qty=}, Cycle Pos. Size: {cycle_pos_size:0.2f}, {self.notional=}, {self.minQty=}, {self.stepSize=}, Fees: {fees:0.3f}",
                False,
            )
            return qty, cycle_pos_size

        qty = utils.size_to_qty(
            self.notional,
            self.close,
            precision=self.quantityPrecision,
            fee_rate=self.fee_rate,
        )

        while True:  # TODO: Remove infinite loop!
            qty += self.stepSize
            cycle_pos_size = qty * self.close
            cycle_pos_size += cycle_pos_size * (self.fee_rate * 3)

            if cycle_pos_size > self.notional:
                fees = cycle_pos_size * (self.fee_rate * 6)
                cycle_pos_size += fees
                cycle_pos_size *= 1.05

                qty = utils.size_to_qty(
                    cycle_pos_size,
                    self.close,
                    precision=self.quantityPrecision,
                    fee_rate=self.fee_rate,
                )

                self.console(
                    f"Calculate minimum by Nominal {self.close=}, {qty=}, Cycle Pos. Size: {cycle_pos_size:0.2f}, {self.notional=}, {self.minQty=}, {self.stepSize=}, Fees: {fees:0.3f}",
                    False,
                )
                return qty, cycle_pos_size

    @property
    def avgEntryPrice(self) -> float:
        """
        Average entry price is none after restarting the session.
        This is a workaround to avoid crashes.
        """
        return self.position.entry_price if is_live else self.average_entry_price

    # Metrics related to liquidation calculation

    # Binance Futures:
    # see https://www.binance.com/en/support/faq/b3c689c1f50a44cabb3a84e663b81d93
    # (WB) Wallet Balance = 1,535,443.01
    # (TMM1) Maintenance Margin of all other contracts, excluding Contract 1 = 71200.81144
    # (UPNL1) Unrealized PNL of all other contracts, excluding Contract 1 = -56,354.57
    # (cumB) Maintenance Amount of BOTH position (one-way mode) = 135,365.00
    # (cumL) Maintenance amount of LONG position (hedge mode) = 0
    # (cumS) Maintenance amount of SHORT position (hedge mode) = 0
    # (Side1BOTH) Direction of BOTH position, 1 as long position, -1 as short position = 1
    # (Position1BOTH) Absolute value of BOTH position size (one-way mode) = 3,683.979
    # (EP1BOTH) Entry Price of BOTH position (one-way mode) =1,456.84
    # (Position1LONG) Absolute value of LONG position size (hedge mode) = 0
    # (EP1LONG) Entry Price of LONG position (hedge mode) = 0
    # (Position1SHORT) Absolute value of SHORT position size (hedge mode) = 0
    # (EP1SHORT) Entry Price of SHORT position (hedge mode) = 0
    # (MMRB) Maintenance margin rate of BOTH position (one-way mode) = 10%
    # (MMRL) Maintenance margin rate of LONG position (hedge mode) = 0
    # (MMRS) Maintenance margin rate of SHORT position (hedge mode) = 0

    # Bybit:
    # see https://help.bybit.com/hc/en-us/articles/900000181046-Liquidation-Price-USDT-Contract-

    @property
    def WB(self):
        """WB Wallet Balance"""
        return self.cap  # ital?

    @property
    def TMM1(self):
        """TMM1 Total Maintenance Margin of all other contracts, excluding itself"""
        tmm1 = 0
        # Iterate over all routes and sum up all maintenance margin
        for r in self.routes:
            # If route is not self, add maintenance margin to tmm1
            if r.symbol != self.symbol:
                try:
                    tmm1 += self.shared_vars[r.symbol][
                        "maintenance_margin"
                    ]  # ❗ Needs to be checked.
                except:
                    pass
        return tmm1

    @property
    def UPNL1(self):
        """UPNL1 Unrealized PNL of all other contracts, excluding itself"""
        upnl1 = 0
        # Iterate over all routes and sum up all unrealized pnl
        for r in self.routes:
            # If route is not self, add unrealized pnl to upnl1
            if r.symbol != self.symbol:
                try:
                    upnl1 += self.shared_vars[r.symbol]["pnl"]  # ❗ Needs to be checked.
                except:
                    pass

        return upnl1

    @property
    def cumB(self):
        """cumB Cumulative Maintenance Amount of BOTH position (one-way mode)"""
        # Maintenance amount of JUST this route
        return self.risk_limits()["maintAmount"]

    @property
    def cumL(self):
        """cumL Cumulative Maintenance amount of LONG position (hedge mode)"""
        # Jesse does not support hedge mode (yet)
        return 0

    @property
    def cumS(self):
        """cumS Cumulative Maintenance amount of SHORT position (hedge mode)"""
        # Jesse does not support hedge mode (yet)
        return 0

    @property
    def Side1BOTH(self):
        """Side1BOTH Direction of BOTH position, 1 as long position, -1 as short position"""
        return 1 if self.is_long else -1

    @property
    def Position1BOTH(self):
        """Position1BOTH Absolute value of BOTH position size (one-way mode)"""
        # Position size as quantity of JUST this route
        return abs(self.position.qty)

    @property
    def EP1BOTH(self):
        """EP1BOTH Entry Price of BOTH position (one-way mode)"""
        return self.avgEntryPrice

    @property
    def Position1LONG(self):
        """Position1LONG Absolute value of LONG position size (hedge mode)"""
        # Jesse does not support hedge mode (yet)
        return 0

    @property
    def EP1LONG(self):
        """EP1LONG Entry Price of LONG position (hedge mode)"""
        # Jesse does not support hedge mode (yet)
        return 0

    @property
    def Position1SHORT(self):
        """Position1SHORT Absolute value of SHORT position size (hedge mode)"""
        # Jesse does not support hedge mode (yet)
        return 0

    @property
    def EP1SHORT(self):
        """EP1SHORT Entry Price of SHORT position (hedge mode)"""
        # Jesse does not support hedge mode (yet)
        return 0

    @property
    def MMRB(self):
        """MMRB Maintenance margin rate of BOTH position (one-way mode)"""
        return self.risk_limits()["maintMarginRatio"]

    @property
    def MMRL(self):
        """MMRL Maintenance margin rate of LONG position (hedge mode)"""
        # Jesse does not support hedge mode (yet)
        return 0

    @property
    def MMRS(self):
        """MMRS Maintenance margin rate of SHORT position (hedge mode)"""
        # Jesse does not support hedge mode (yet)
        return 0

    @property
    def LP1(self):
        """LP1 Liquidation Price"""
        if not self.is_open:
            return float('nan')

        if self.ftx:
            return self.close * (1 - (self.margin_fraction - self.maintenance_margin_fraction))
        # TODO: We may have open positions and liq price when trading multi routes.
        #       is_open check commented out for now.
        # if not self.is_open:
        #     return float('inf')
        # LP1 = (self.WB - self.TMM1 + self.UPNL1 + self.cumB + self.cumL + self.cumS - self.Side1BOTH * self.Position1BOTH * self.EP1BOTH - self.Position1LONG * self.EP1LONG + self.Position1SHORT * self.EP1SHORT) / (self.Position1BOTH * self.MMRB + self.Position1LONG * self.MMRL + self.Position1SHORT * self.MMRS - self.Side1BOTH * self.Position1BOTH - self.Position1LONG + self.Position1SHORT)
        LP1_simple = (
            self.WB
            - self.TMM1
            + self.UPNL1
            + self.cumB
            - self.Side1BOTH * self.Position1BOTH * self.EP1BOTH
        ) / (self.Position1BOTH * self.MMRB - self.Side1BOTH * self.Position1BOTH)
        return LP1_simple

    # TODO: @property
    def liq_price(self) -> float:
        """Liquidation Price (if it's greater than zero)"""
        return self.LP1 if self.LP1 > 0 else float("nan")

    # TODO: @property
    def lp_rate(self) -> float:
        """Liquidation Price vs Mark Price rate"""
        if not self.is_open:
            return float(
                "nan"
            )  # self.LP1 / self.avgEntryPrice if self.avgEntryPrice > 0 else float('nan')
        
        lp = self.zero_price if self.ftx else self.LP1
        
        rate = lp / self.close if self.is_long else self.close / lp
        self.save_max_lp_ratio(rate)
        return rate

    def print_lp(self):
        if self.LP1 > 0:
            rate = self.LP1 / self.close if self.is_long else self.close / self.LP1
            msg = f"\n{self.ts} {self.symbol} LP1: {self.LP1:0.2f}, Price: {self.close:0.2f}, Rate: {rate:0.2f} Balance: {self.cap:0.2f}, AvgEntry: {self.avgEntryPrice:0.2f}, Pos Size: {self.position.value:0.2f}, Pos Qty: {self.position.qty:0.2f}, Pnl%: {self.position.pnl_percentage / self.leverage:0.2f}%, AvailMargin: {self.available_margin:0.2f}, Actual Margin Ratio: {self.margin_ratio('update position')}"
            print(f"\033[33m{msg}\033[0m")

    #

    # New metrics
    @property
    def get_total_value(self) -> float:
        """
        Calculate the total value of all open positions
        aka Total Position Notional for FTX.
        """

        tv = 0

        # If we trade single route use newest the position value.
        # Reading position value from shared vars may cause a delay. Need to be checked.

        if len(self.routes) > 1:
            for r in self.routes:
                try:
                    tv += self.shared_vars[r.symbol]["pos_value"]
                except Exception:
                    # self.debug('Not ready yet! (get_total_value)')
                    pass
        else:
            tv = self.position.value

        self.shared_vars["max_total_value"] = max(
            self.shared_vars["max_total_value"], tv
        )

        return round(tv, 6)

    @property
    def unreal_pnl(self) -> float:
        """Calculate the unrealized profit/loss of all open positions"""
        spnl = 0

        for r in self.routes:
            try:
                spnl += self.shared_vars[r.symbol]["pnl"]
                # print(f"\nOK! {self.shared_vars[r.symbol]}")
            except:
                pass
                # self.debug('Not ready yet! (unrealized_pnl)')
        return round(spnl, 6)

    @property
    def initial_margin(self) -> float:
        """Calculate the initial margin of all open positions"""
        im = 0

        for r in self.routes:
            try:
                im += (
                    self.shared_vars[r.symbol]["pos_value"] / self.leverage
                )  # * self.fee_rate
                # print(f"\nOK! {self.shared_vars[r.symbol]}")
            except:
                pass
                # self.debug('Not ready yet! (initial_margin)')
        return round(im, 6)

    @property
    def available_margin(self) -> float:
        if is_live():
            return super().available_margin * self.leverage
        else:
            return super().available_margin

    @property
    def avail_margin(self) -> float:
        """
        Calculate the available margin of all open positions
        ATTN! avail margin != margin balance
        """
        return self.margin_balance - self.initial_margin

    @property
    def margin_balance(self):
        """Calculate the margin balance"""
        return round(self.cap + self.unreal_pnl, 6)

    @property
    def total_account_value(self) -> float:
        """
        FTX ONLY!
        Total Account Value
        Total value of the collateral and unrealized PnL within a specific subaccount.
        Total Account Collateral + Unrealized PnL
        """
        return self.margin_balance

    @property
    def position_notional(self) -> float:
        """
        FTX ONLY!
        Position Notional
        Notional size of your position in USD
        Position size * Market price
        """
        return self.position.value

    @property
    def margin_fraction(self) -> float:
        """
        FTX ONLY!
        After opening the 20 BTC-PERP position, your Margin Fraction is as follows:
        = Total Account Value / Total Position Notional
        = $98,750 / $400,000
        = 24.69%
        """
        return round(self.cap / self.position.value, 6) if self.is_open else float("nan")

    @property
    def maintenance_margin_fraction(self) -> float:
        """
        FTX ONLY!
        After opening the 20 BTC-PERP position, your Margin Fraction is as follows:
        = Total Account Value / Total Position Notional
        = $98,750 / $400,000 = 24.69%

        Now, let's calculate your Maintenance Margin Fraction to understand at which point
        the position would start getting liquidated
        (assuming that’s the only open position in the account.
        We will explore how multiple positions affect your account's MMF and IMF later on in this example).
        MMF = max(3%, 0.6 * IMF Factor * sqrt [position open size in tokens]) * MMF Weight
        = max (3%, 0.6 * 0.002 * sqrt[20] )) * 1
        = 3%
        """
        # self.console(f'🥶 self.ftx_risk_limits = {self.ftx_risk_limits}')
        # position open size in tokens = self.position.qty or self.position.value ?
        return max(0.03, 0.6 * self.ftx_risk_limits["imfFactor"] * math.sqrt(self.position.qty)) * self.ftx_risk_limits["mmfWeight"] if self.is_open else float("nan")

    @property
    def max_leverage(self) -> int:
        """
        FTX ONLY!
        Max Leverage
        Max allowable leverage to open new derivatives or spot margin positions set by the user.
        A subaccount's max leverage can be adjusted on the profile page under the Margin section.
        Max allowable leverage on spot margin is 10x.

        Harcoded to 20x for now
        """
        return 20

    @property
    def base_imf(self) -> float:
        """
        FTX ONLY!
        Base IMF
        The minimum Initial Margin Fraction needed to open a new perpetual swap or futures position.
        1 / Maximum account leverage set by user
        """
        return round(1 / self.leverage, 3)

    # @property
    # def position_imf(self) -> float:
    #     """
    #     FTX ONLY!
    #     Position Initial Margin Fraction
    #     (Position IMF)
    #     The minimum margin fraction required for a particular derivatives position

    #     max(Base IMF , IMF Factor * sqrt [position open size in tokens]) * IMF Weight
    #     """
    #     imfFactor = self.ftx_risk_limits['imfFactor']
    #     imfWeight = self.ftx_risk_limits['imfWeight']
    #     return max(self.base_imf, imfFactor * math.sqrt(self.position.qty)) * imfWeight

    @property
    def position_imf(self, qty = None) -> float:
        """
        Position Initial Margin Fraction

        (Position IMF)
        The minimum margin fraction required for a particular derivatives position.
        Long positions are capped at 1 plus fees required to exit the position (considering open orders).

        If position is long:
        min (max[Base IMF , IMF Factor * sqrt {position open size in tokens}] * IMF Weight, 1 + fee rate * [short size + long size] )

        If position is short:
        max(Base IMF , IMF Factor * sqrt [position open size in tokens]) * IMF Weight
        """

        if qty is None:
            if not self.is_open:
                return float("nan")

            qty = self.position.qty

        if self.is_long:
            return min(
                max(
                    self.base_imf,
                    self.ftx_risk_limits["imfFactor"] * math.sqrt(qty),
                )
                * self.ftx_risk_limits["imfWeight"],
                1 + self.fee_rate * qty,
            )
        if self.is_short:
            return (
                max(
                    self.base_imf,
                    self.ftx_risk_limits["imfFactor"] * math.sqrt(qty),
                )
                * self.ftx_risk_limits["imfWeight"]
            )

    @property
    def collateral_used(self) -> float:
        """
        FTX ONLY!
        Collateral Used
        Collateral currently being used by a single derivatives or spot margin position
        Position IMF * Position Notional
        """

        return self.position_imf * self.position_notional if self.is_open else 0

    @property
    def total_collateral_used(self) -> float:
        """
        FTX ONLY!
        Total Collateral Used
        Total of collateral being used by all open derivatives or spot margin positions in the subaccount, as well as collateral tied up in open orders, including spot.
        = sum (Position1 Open Size Notional * Position1 IMF, Position2 Open Size Notional * Position2 IMF,...) + sum(Spot Order1 Size * Mark Price, Spot Order2 Size * Mark Price,...)
        """

        tcu = 0

        if len(self.routes) > 1:
            for r in self.routes:
                try:
                    tcu += self.shared_vars[r.symbol]["collateral_used"]
                    # print(f"\nOK! {self.shared_vars[r.symbol]}")
                except Exception as e:
                    pass
                    # self.debug('Not ready yet! (total_collateral_used)')
        else:
            tcu = self.collateral_used

        return round(tcu, 6)

    @property
    def position_mmf(self) -> float:
        """
        FTX ONLY!
        Position Maintenance Margin Fraction

        (Position MMF)
        The minimum margin fraction required to avoid liquidation on a derivatives position
        max(3%, 0.6 * IMF Factor * sqrt [position open size in tokens]) * MMF Weight
        """

        return (
            max(
                0.03,
                0.6 * self.ftx_risk_limits["imfFactor"] * math.sqrt(self.position.qty),
            )
            * self.ftx_risk_limits["mmfWeight"]
        ) if self.is_open else float("nan")

    @property
    def total_position_notional(self) -> float:
        """
        FTX ONLY!
        Total Open Position Notional

        The total notional amount in USD of all open derivatives or spot margin positions if your outstanding long or short orders were filled.

        SUM (Position Open Size1 * Mark Price1, Position Open Size2 * Mark Price2, …)
        For all positions
        """

        return self.get_total_value if self.is_open else 0

    @property
    def account_imf(self) -> float:
        """
        FTX ONLY!
        Account Initial Margin Fraction
        (Account IMF)

        The minimum account margin fraction required to increase position size,
        equal to average of position IMF for all account positions weighed by position open notional

        Sum ( [ Position Notional / Total Position Notional ] * Position IMF )
        of all derivatives and spot margin positions in subaccount

        Note: Calculate sum of all open positions' imf if we trade multiroutes
        else just return position_imf
        """

        a_imf = 0

        if len(self.routes) > 1:
            for r in self.routes:
                try:
                    a_imf += self.shared_vars[r.symbol]["position_imf"]
                    # print(f"\nOK! {self.shared_vars[r.symbol]}")
                except Exception as e:
                    pass
                    # self.debug('Not ready yet! (account_imf)')
        else:
            a_imf = self.position_imf

        return round(a_imf, 6)

    @property
    def account_mmf(self) -> float:
        """
        FTX ONLY!
        Account Maintenance Margin Fraction

        (Account MMF)
        The minimum account MF needed to avoid getting liquidated, equal to average of positions MMF weighed by the positions notional.
        Sum ( [ Position Notional / Total Position Notional ] * Position MMF )
        of all derivatives and spot margin positions in subaccount
        """

        if not self.is_open:
            return float("nan")

        a_mmf = 0

        if len(self.routes) > 1:
            for r in self.routes:
                try:
                    a_mmf += (
                        self.shared_vars[r.symbol]["position_notional"]
                        / self.total_position_notional
                    ) * self.shared_vars[r.symbol]["position_mmf"]
                    # print(f"\nOK! {self.shared_vars[r.symbol]}")
                except Exception as e:
                    pass
                    # self.debug('Not ready yet! (account_mmf)')
        else:
            a_mmf = (
                self.position_notional / self.total_position_notional
            ) * self.position_mmf

        return round(a_mmf, 6)

    @property
    def free_collateral(self) -> float:
        """
        FTX ONLY!
        Free Collateral

        Total collateral available that can be used for opening new positions and withdrawn from the exchange,
        excluding collateral locked in open orders or open positions.
        Total Account Collateral - Total
        """
        mark_price = self.close
        # TODO: Check
        return self.margin_balance - self.total_collateral_used

    @property
    def acmf(self) -> float:
        """
        Auto Close Margin Fraction

        (ACMF)
        The minimum margin fraction needed to avoid the assets and positions of a given sub being liquidated via backstop liquidity providers.
        max( Account MMF / 2, Account MMF - 0.06 )

        ACMF is the margin fraction at which your account would be completely liquidated. To calculate this, we use this formula:
        ACMF = max(MMF / 2, MMF - 0.06)
        = max( 0.036 / 2, 0.036 - 0.06 )
        = 1.53%

        So, if your Margin Fraction drops below 1.53%, all of your positions within the subaccount would be instantly liquidated.
        """

        return max(self.account_mmf / 2, self.account_mmf - 0.06) if self.is_open else float("nan")

    @property
    def zero_price(self) -> float:
        """
        Zero Price (ZP)

        This is the mark price (MP) that would set a subaccount’s Total Account Value to 0.
        Zero Price (ZP) is the price that would cause your account to get completely liquidated.

        Mark Price * (1 - Margin Fraction) if long, Mark Price * (1 + Margin Fraction) if short.
        """

        if self.is_long:
            return self.close * (1 - self.margin_fraction)
        elif self.is_short:
            return self.close * (1 + self.margin_fraction)
        else:
            return float("nan")

    @property
    def maintenance_collateral(self) -> float:
        """
        Maintenance Collateral

        The amount of collateral needed to avoid liquidation on a derivatives position
        """
        return (self.position_notional * self.position_mmf) if self.is_open else float("nan")

    @property
    def account_maintenance_collateral(self) -> float:
        """
        Account Maintenance Collateral

        The amount of collateral needed to avoid liquidation on a derivatives position
        """
        amc = 0

        if len(self.routes) > 1:
            for r in self.routes:
                try:
                    amc += self.shared_vars[r.symbol]["maintenance_collateral"]
                    # print(f"\nOK! {self.shared_vars[r.symbol]}")
                except Exception as e:
                    pass
                    # self.debug('Not ready yet! (account_maintenance_collateral)')
        else:
            amc = self.maintenance_collateral

        return round(amc, 6)

    @property
    def pmpd(self) -> float:
        """
        Position Margin Per Dollar (PMPD)

        Used for calculating Position Zero Price (PZP)

        [(Position maintenance collateral used) / (sum of Position maintenance collateral used for all account positions)] * Total account value / abs(position notional)

        Maintenance collateral = Position notional * Position MMF
        """

        return (
            (self.maintenance_collateral / self.account_maintenance_collateral)
            * self.total_account_value
            / abs(self.position_notional)
        )

    @property
    def pzp(self) -> float:
        """
        Position Zero Price (PZP)

        The fill price a bankrupted account would receive for a particular position.

        MP * (1 - PMPD) if long, MP * (1 + PMPD) if short
        """

        if self.is_long:
            return self.close * (1 - self.pmpd)
        elif self.is_short:
            return self.margin_price * (1 + self.pmpd)
        else:
            return float("nan")

    @property
    def liquidation_distance(self) -> float:
        """
        Liquidation Distance: % move in futures that would make MF = MMF.
        If you Margin Fraction falls below your Maintenance Margin Fraction, your account will begin to get liquidated.

        MF <= MMF = Liquidation
        MF > MMF = No liquidation
        
        MF : Margin Fraction                self.margin_fraction
        MMF: Maintenance Margin Fraction    self.account_mmf

        """
        # return (self.close - self.zero_price) / self.close  # Suggested by @copilot
        # return (self.margin_fraction - self.account_mmf)  # / self.margin_fraction
        return (self.margin_fraction - self.account_mmf) / self.margin_fraction if self.is_open else float("nan")


    @property
    def ld_inverse(self) -> float:
        """
        ~ Liquidation Distance: % move in futures that would make MF = MMF.
        """
        return 1 - self.liquidation_distance if self.is_open else float("nan")

    @property
    def maintenance_margin(self):
        """
        Calculate the maintenance margin
        See: https://www.binance.com/en/futures/trading-rules/perpetual/leverage-margin
        Initial Margin = Notional Position Value / Leverage Level
        Maintenance Margin = Notional Position Value * Maintenance Margin Rate - Maintenance Amount

        It is important to note that the Maintenance Margin will directly affect the liquidation price.
        To avoid auto-deleveraging, it is highly recommended to close your positions before the
        collateral falls below the Maintenance Margin.
        """
        if self.ftx:
            # TODO: FTX!
            return 0.1

        if isinstance(self.fixed_margin_ratio, (float, int)):
            return (
                self.position.value * self.fixed_margin_ratio
                - self.risk_limits()["maintAmount"]
            )  # Added maintenance amount to fixed margin calculation.

        try:
            mm = (
                self.position.value * self.risk_limits()["maintMarginRatio"]
                - self.risk_limits()["maintAmount"]
            )
        except Exception:
            mm = self.position.value * 0.75
            # # print(e)
            # print("self.position.value", type(self.position.value))
            # print("self.risk_limits()['maintMarginRatio']", type(self.risk_limits()['maintMarginRatio']))
            # print("self.risk_limits()['maintAmount']", type(self.risk_limits()['maintAmount']))
            if self.risk_limits() is None:
                print("self.risk_limits() is None")

        return mm  # self.position.value * self.risk_limits()['maintMarginRatio']  #  - self.risk_limits()['maintAmount']

    def margin_ratio(self, caller=None):
        """Calculate the margin ratio"""
        mr = round((self.maintenance_margin / self.margin_balance) * 100, 2)
        # We have MRs greater than 100% if we let it keep running.
        mr = abs(mr) + 100 if mr < 0 else mr
        self.save_max_mr(mr, caller)
        self.check_liquidation(mr, caller)
        return mr

    def check_mr_alert(self, mr, caller=None):
        """For multi route strategies use a shared var to alert the other routes."""
        if mr >= self.margin_ratio_treshold:
            self.shared_vars["margin_alert"] = "True"
            msg = f"Margin Ratio Alert!: {mr}%, Avail. margin: {round(self.available_margin, 2)}, Balance: {round(self.cap, 2)} * {self.leverage} = {round(self.cap * self.leverage, 2)}, Prev. Margin Ratio: {self.shared_vars['margin_ratio']}%, Total value: {self.shared_vars['total_value']}, Margin balance: {self.shared_vars['margin_balance']}, Maint Margin: {self.shared_vars['maint_margin']}, {self.div=}, {self.profit_ratio2=}, {(int(self.profit_ratio2 + 1) * self.div)=}\n{json.dumps(self.shared_vars, indent=4)}\nCaller: {caller}"
            self.console(msg, False)
        else:
            self.shared_vars["margin_alert"] = "False"

    def check_global_margin_alert(self, caller=None):
        if (
            self.shared_vars["margin_alert"] == "False"
            and self.shared_vars["margin_ratio"] <= self.margin_ratio_treshold
        ):
            return False

        msg = f"🚨 Margin Ratio is at limits! {self.shared_vars['margin_ratio']:0.2f} ({caller})"
        print(msg)
        # self.debug(msg)
        # self.console(msg)
        return True

    def save_max_mr(self, mr, caller=None):
        """Save the max margin ratio with timestamp"""
        max_mr_snapshot = self.shared_vars["max_margin_ratio"]
        self.shared_vars["max_margin_ratio"] = max(max_mr_snapshot, mr)

        if self.shared_vars["max_margin_ratio"] != max_mr_snapshot:
            self.shared_vars["max_margin_ratio_ts"] = self.ts
            msg = f"Margin Ratio {max_mr_snapshot} -> {self.shared_vars['max_margin_ratio']} Caller: {caller}"
            self.console(msg, False)
            # self.console(msg)

    def save_max_lp_ratio(self, lp_ratio, caller=None):
        """Save the max LP1/price ratio with timestamp"""
        max_lp_snapshot = self.shared_vars["max_lp_ratio"]
        self.shared_vars["max_lp_ratio"] = max(lp_ratio, max_lp_snapshot)

        if self.shared_vars["max_lp_ratio"] != max_lp_snapshot:
            self.shared_vars["max_lp_ratio_ts"] = self.ts
            msg = f"LP Ratio {max_lp_snapshot:0.2f} -> {self.shared_vars['max_lp_ratio']:0.2f}, Price: {self.close}, Liq. Price: {self.LP1:0.2f}, Caller: {caller}"
            # self.console(msg, False)
            # self.console(msg)

    def check_liquidation(self, mr, caller=None):
        # sourcery skip: raise-specific-error
        """
        Check if the margin balance is below the maintenance margin and throw an exception if it is.
        """
        if mr < 0 or mr >= self.margin_ratio_treshold:
            msg = (
                f"Got liqed? Margin Ratio: {mr}%, Avail. margin: {self.available_margin:0.2f}, "
                f"Balance: {self.cap:0.2f} * {self.leverage} = {self.cap * self.leverage:0.2f}, "
                f"Prev. Margin Ratio: {self.shared_vars['margin_ratio']}%, Total value: {self.shared_vars['total_value']}, "
                f"Margin balance: {self.shared_vars['margin_balance']:0.2f}, Maint Margin: {self.shared_vars['maint_margin']:0.2f}, "
                f"{self.div=}, {self.profit_ratio2=:0.2f}, {(int(self.profit_ratio2 + 1) * self.div)=:0.2f}, "
                f"\n{json.dumps(self.shared_vars, indent=4)}\nCaller: {caller}"
            )

            if is_live():
                self.console(msg)
                self.terminate()
            else:
                # print(msg)

                # Disabled for going live, any potential bug with this can cause a loss of funds
                if not self.keep_running_in_case_of_liquidation:
                    # exit()
                    self.terminate()
                    raise Exception(msg)

    def load_bybit_risk_limits(self):
        from pathlib import Path

        risk_limit_url = f"https://api.bybit.com/public/linear/risk-limit?symbol={self._symbol.replace('-', '')}"

        if not Path("bybit").exists():
            Path("bybit").mkdir()

        fname = f"bybit/risk-limit-{self.symbol}.json"

        print(f"\nLoading risk limits from {fname}")

        try:
            with open(fname) as f:
                data = json.load(f)
                self.bybit_risk_limits = data["result"]
        except Exception as e:
            print(os.listdir())
            print(f"Can not load Bybit risk limit for {self.symbol} from: {fname}")
            print("Will download from Bybit API")

            try:
                data = requests.get(risk_limit_url).json()
                if "ret_msg" in data and data["ret_msg"] == "OK":
                    self.bybit_risk_limits = data["result"]
                    print(f"Risk limits for {self.symbol} loaded from Bybit API")
                    # print(self.bybit_risk_limits)

                    try:
                        with open(fname, "w") as f:
                            json.dump(data, f, indent=4)
                        print(
                            f"'Bybit Perpetual' risk limits for {self.symbol} saved to '{fname}'."
                        )
                    except:
                        print(f"Failed to save {fname}")
            except:
                print(f"Failed to download {risk_limit_url}")
                exit()

    def load_binance_tier_brackets(self):
        from pathlib import Path

        fname = Path(__file__).parent / "Binance_lev_brackets.json"
        print("\nLoading Binance tier brackets from:", fname)

        try:
            with open(fname) as f:
                data = json.load(f)
        except Exception as e:
            print(os.listdir())
            print(f"Error loading Binance tier brackets from: {fname}")

        for i in data:
            if i["symbol"] == self._symbol.replace("-", ""):
                self.binance_lev_brackets = i["brackets"]
                break

    def load_ftx_risk_limits(self):
        from pathlib import Path

        if self.ftx and self.symbol.endswith("-USD"):
            sym = self.symbol.replace("-USD", "-PERP")

        risk_limit_url = "https://ftx.com/api/futures"

        if not Path("ftx").exists():
            Path("ftx").mkdir()

        fname = f"ftx/{sym}.json"

        print(f"\nLoading risk limits from {fname}")

        try:
            with open(fname) as f:
                data = json.load(f)
        except Exception as e:
            print(os.listdir('ftx/'))
            self.console(f"Can not load ftx risk limit for {sym} from: {fname}. Downloading from ftx API.")

            try:
                data = requests.get(risk_limit_url).json()
                if "success" in data and data["success"] == "true":
                    self.console(f"Risk limits for {sym} loaded from FTX API")
                    # print(self.bybit_risk_limits)

                    try:
                        with open(fname, "w") as f:
                            json.dump(data, f, indent=4)

                        self.console(f"'FTX Perpetual' risk limits saved to '{fname}'.")
                    except:
                        self.console(f"‼ Failed to save {fname}")
            except:
                self.console(f"Failed to download {risk_limit_url}")
                exit()

        for s in data["result"]:
            if s["name"] == sym:
                self.ftx_risk_limits = s
                print(f"Risk limits for {sym} loaded from FTX API")
                print(self.ftx_risk_limits)
                break

        if self.ftx_risk_limits is None:
            print(f"Failed to load risk limits for {sym} from {fname}")
            exit()

    def risk_limits(self, psize: float = None, force_reload: bool = False):
        """
        Pick the correct risk limits based on the exchange.
        Use binance futures limits for both spot and futures cause we backtest futures strategies with spot candles too.
        psize is the custom position size to calculate futures limits. eg. calculate the max allowed leverage or position size before increasing the order size.
        if psize is None, then use the current position size (self.position.value).
        """
        # return self.binance_limits(psize, force_reload) if 'Binance' in self.exchange and not self.trade_with_bybit_rules else self.bybit_limits(psize, force_reload)
        if self.trade_with_bybit_rules:
            return self.bybit_limits(psize, force_reload)

        if "binance" in self.exchange.lower():
            return self.binance_limits(psize, force_reload)
        elif "bybit" in self.exchange.lower():
            return self.bybit_limits(psize, force_reload)
        elif "ftx" in self.exchange.lower():
            return self.ftx_limits(psize, force_reload)

    def binance_limits(self, psize=None, force_reload=False):
        """psize is the custom position size to calculate next limits.
        eg. calculate the max allowed leverage or position size before increasing the order size."""

        r = {
            "bracket": 0,
            "initialLeverage": 0,
            "notionalCap": 0,
            "notionalFloor": 0,
            "maintMarginRatio": 0.0,
            "maint_amount": 0.0,
        }

        # if psize is None, then use the current position size.
        if not psize:
            psize = self.position.value

        if not self.binance_lev_brackets or force_reload:
            self.load_binance_tier_brackets()

        for b in self.binance_lev_brackets:
            if psize < b["notionalCap"]:
                r["bracket"] = b["bracket"]
                r["initialLeverage"] = b["initialLeverage"]
                r["notionalCap"] = b["notionalCap"]
                r["notionalFloor"] = b["notionalFloor"]

                if isinstance(self.fixed_margin_ratio, (float, int)):
                    r["maintMarginRatio"] = self.fixed_margin_ratio
                else:
                    r["maintMarginRatio"] = b["maintMarginRatio"]

                r["maintAmount"] = b["cum"]
                return r

        r[
            "maintMarginRatio"
        ] = 0.75  # TODO: Bybit jsons are missing the last tiers' maintenance margin! Calculate next tiers.
        r["maintAmount"] = 0
        # print(self.bybit_risk_limits)
        # print(psize)
        # raise Exception(f"Failed to find risk limits for {self.symbol}")
        return r  # TODO: Bybit jsons are missing the last tiers' maintenance margin! Calculate next tiers.
        # raise Exception(f"Failed to find risk limits for {self.symbol} {psize=}")
        # return None

    def bybit_limits(self, psize=None, force_reload=False):
        """
        psize is the custom position size to calculate next limits.
        eg. calculate the max allowed leverage or position size before increasing the order size.
        """
        # Term                            Formula                                                         eg: BTCUSDT(Total Position Value 3,200,000 USDT, hence limit needs to increase by 1 time)
        # New Risk Limit(RL) =            RL Base value + (Number of incremental * RL incremental value)  eg. 2,000,000 + (1*2,000,000)= 4,000,000 USDT
        # New Maintenance Margin(MM) % =  MM Base rate + (Number of incremental * MM incremental rate)    eg. 0.5% + (1*0.5%)= 1%
        # New Initial Margin (IM) % =     IM Base rate + (Number of incremental * IM incremental rate)    eg. 1% + (1*0.75%)= 1.75%
        # New Maintenance Margin Amount = New MM%* Total Position Value                                   eg. 1% * 3,200,000 = 32,000 USDT

        r = {
            "bracket": 0,
            "initialLeverage": 0,
            "notionalCap": 0,
            "notionalFloor": 0,
            "maintMarginRatio": 0.0,
            "maint_amount": 0.0,
        }

        # if psize is None, then use the current position size.
        if psize is None:
            psize = self.position.value

        if not self.bybit_risk_limits or force_reload:
            self.load_bybit_risk_limits()

        for b in self.bybit_risk_limits:
            if b["is_lowest_risk"] == 1:
                rl_base_value = b["limit"]
                mm_base_rate = b["maintain_margin"]
                im_base_rate = b["starting_margin"]
                break

        for b in self.bybit_risk_limits:
            if psize < b["limit"]:
                r["bracket"] = b["id"]
                r["initialLeverage"] = b["max_leverage"]
                r["notionalCap"] = b["limit"]
                # TODO: Do we really need it? There should be a difference between notionalFloor and previous tier's notionalCap.
                r["notionalFloor"] = (
                    b["limit"] - rl_base_value
                )  #  rl_base_value * (int(b['id']) - 1) IDs are not 1 indexed

                if isinstance(self.fixed_margin_ratio, (float, int)):
                    r["maintMarginRatio"] = self.fixed_margin_ratio
                else:
                    r["maintMarginRatio"] = b["maintain_margin"]

                # TODO: Calculate for Bybit if available/needed
                r["maintAmount"] = 0.0
                return r

        r[
            "maintMarginRatio"
        ] = 0.10  # TODO: Bybit jsons are missing the last tiers' maintenance margin! Calculate next tiers.
        r["maintAmount"] = 0
        # print(self.bybit_risk_limits)
        # print(psize)
        # raise Exception(f"Failed to find risk limits for {self.symbol}")
        return r  # TODO: Bybit jsons are missing the last tiers' maintenance margin! Calculate next tiers.

    def ftx_limits(self, psize=None, force_reload=False):
        """
        https://help.ftx.com/hc/en-us/articles/360027946371-Account-Margin-Management

        psize is the custom position size to calculate next limits.
        eg. calculate the max allowed leverage or position size before increasing the order size.


        Generic             FTX Naming
        maintMarginRatio    MMF Weight Multiplier of the margin required to maintain an existing leveraged position
        initialMarginRatio  IMF Weight Multiplier of the margin required to open a new leveraged position TODO: Not used?

        ftx sample:
        {
            "name": "1INCH-PERP",
            "underlying": "1INCH",
            "description": "1INCH Token Perpetual Futures",
            "type": "perpetual",
            "expiry": null,
            "perpetual": true,
            "expired": false,
            "enabled": true,
            "postOnly": false,
            "priceIncrement": 0.0001,
            "sizeIncrement": 1.0,
            "last": 0.5863,
            "bid": 0.5862,
            "ask": 0.5864,
            "index": 0.5864988666666666,
            "mark": 0.5865,
            "imfFactor": 0.0005,
            "lowerBound": 0.5571,
            "upperBound": 0.6158,
            "underlyingDescription": "1INCH Token",
            "expiryDescription": "Perpetual",
            "moveStart": null,
            "marginPrice": 0.5865,
            "imfWeight": 1.0,
            "mmfWeight": 1.0,
            "positionLimitWeight": 20.0,
            "group": "perpetual",
            "closeOnly": false,
            "change1h": 0.0,
            "change24h": 0.02427523576667831,
            "changeBod": 0.0022214627477785374,
            "volumeUsd24h": 2720322.7034,
            "volume": 4662092.0,
            "openInterest": 8324331.0,
            "openInterestUsd": 4882220.1315
        },
        IMF Factor	IMF Weight	MMF Weight
        """
        # Term                            Formula                                                         eg: BTCUSDT(Total Position Value 3,200,000 USDT, hence limit needs to increase by 1 time)
        # New Risk Limit(RL) =            RL Base value + (Number of incremental * RL incremental value)  eg. 2,000,000 + (1*2,000,000)= 4,000,000 USDT
        # New Maintenance Margin(MM) % =  MM Base rate + (Number of incremental * MM incremental rate)    eg. 0.5% + (1*0.5%)= 1%
        # New Initial Margin (IM) % =     IM Base rate + (Number of incremental * IM incremental rate)    eg. 1% + (1*0.75%)= 1.75%
        # New Maintenance Margin Amount = New MM%* Total Position Value                                   eg. 1% * 3,200,000 = 32,000 USDT

        r = {
            "bracket": 0,
            "initialLeverage": 0,
            "notionalCap": 0,
            "notionalFloor": 0,
            "maintMarginRatio": 0.0,
            "maint_amount": 0.0,
        }
        # ftx specific variables
        f = {"imfFactor": 0.0, "imfWeight": 0.0, "mmfWeight": 0.0}

        # if psize is None, then use the current position size.
        if psize is None:
            psize = self.position.value

        if not self.ftx_risk_limits or force_reload:
            self.console(f"🦊 Loading FTX risk limits for {self.symbol}")
            self.load_ftx_risk_limits()

        # if isinstance(self.fixed_margin_ratio, (float, int)):
        #     r["maintMarginRatio"] = self.fixed_margin_ratio
        # else:
        #     r["maintMarginRatio"] = b["maintain_margin"]

        # # TODO: Calculate for FTX if available/needed
        # r["maintAmount"] = 0.0
        # return r

        # r[
        #     "maintMarginRatio"
        # ] = 0.10  # TODO: Bybit jsons are missing the last tiers' maintenance margin! Calculate next tiers.
        # r["maintAmount"] = 0
        # # print(self.bybit_risk_limits)
        # # print(psize)
        # # raise Exception(f"Failed to find risk limits for {self.symbol}")
        # return r  # TODO: Bybit jsons are missing the last tiers' maintenance margin! Calculate next tiers.
        # self.console(f'🥶 self.ftx_risk_limits = {self.ftx_risk_limits}')
        return self.ftx_risk_limits

    def check_negative_margin(self):
        if self.available_margin >= 0:
            return False
        # self.dump_routes_info()
        self.debug(
            f"🦆 Negative Margin: {self.available_margin:0.2f}, Balance: {self.cap:0.2f} * {self.leverage} = {self.cap * self.leverage:0.2f}, Margin Ratio: {self.shared_vars['margin_ratio']}%, Total value: {self.shared_vars['total_value']}, Margin balance: {self.shared_vars['margin_balance']}, Maint Margin: {self.shared_vars['maint_margin']} - {self.shared_vars[self.symbol]}, {self.div=}, {self.profit_ratio2=} {(int(self.profit_ratio2 + 1) * self.div)=}"
        )
        return True

    def check_avail_margin_vs_capital(self):
        # will fail at bybit unleveraged margin
        # best to not call it

        if self.available_margin >= self.cap:
            return True
        self.debug(
            f"Avail. Margin: {self.available_margin:0.2f} < Capital: {self.cap:0.2f}"
        )
        return False

    def check_breakeven_or_killswitch(self):
        try:
            return self.break_even_file in os.listdir() or "KILL.SWITCH" in os.listdir()
        except:
            self.console(
                "Exception in checking break even/ks file. (TODO: Add caller.)"
            )
            return False

    def check_pause(self):
        try:
            return self.pause_file in os.listdir()
        except:
            self.console("Exception in checking pause file.")
            return False

    def test_leverage(self):
        if self.ftx:
            # TODO: FTX
            return True

        if (
            self.leverage
            > self.risk_limits(psize=0, force_reload=False)["initialLeverage"]
        ):
            print(
                f"\nThe maximum allowed leverage for {self.symbol} at {self.exchange} is {self.risk_limits()['initialLeverage']}x, you have {self.leverage}x"
            )
            return False

    def test_max_pos_size_vs_leverage(self):
        psize = self.max_position_value
        rls = self.risk_limits(psize, force_reload=False)

        if self.leverage > rls["initialLeverage"]:
            print(
                f"\n{self.exchange} {self.symbol} Exchange rule violation. The maximum allowed leverage for your max. position size ({psize:0.1f}) is {rls['initialLeverage']}x. You had {self.leverage}x leverage set."
            )
            return False

    def check_limits_before_order(self, psize=None, caller: str = ""):
        """Check if the order size is within the limits."""
        if psize is None:
            psize = self.position.value

        rls = self.risk_limits(psize, force_reload=False)

        if self.leverage > rls["initialLeverage"]:  # TODO:
            #   File "c:\jesse-projects\git\k2_base\k2_base\__init__.py", line 183, in average_down_position
            #     self.check_limits_before_order(self.cycle_pos_qty * self.close + self.position.value, caller='average_down_position')
            # File "c:\jesse-projects\git\strategysd\strategysd\__init__.py", line 480, in check_limits_before_order
            #     if self.leverage > rls['initialLeverage']:
            # TypeError: 'NoneType' object is not subscriptable
            print(
                f"\n{self.ts}{self.symbol} {self.exchange} The maximum allowed leverage for your next position size ({psize:0.2f}) is {rls['initialLeverage']}x, and you have {self.leverage}x leverage set., Caller: {caller}"
            )

    def download_rules(self, exchange: str):
        """Download the trading rules from the exchanges."""

        exc = "Bybit Perpetual" if self.trade_with_bybit_rules else exchange
        local_fn = f"{exc.replace(' ', '')}ExchangeInfo.json"

        # try:
        data = requests.get(self.trade_rule_urls[exc]).json()

        if "serverTime" not in data.keys():
            print("if 'serverTime' not in data.keys():")
            data["serverTime"] = datetime.datetime.now().timestamp() * 1000

        # Bybit api does not return server time so we need to add it manually using our server time
        if "ret_msg" in data and data["ret_msg"] == "OK":
            data["serverTime"] = datetime.datetime.now().timestamp() * 1000
            print("Added local timestamp to Bybit data")

        if "success" in data and data["success"] == "true":
            data["serverTime"] = datetime.datetime.now().timestamp() * 1000
            print("Added local timestamp to FTX data")

        if int(data["serverTime"]):
            try:
                with open(local_fn, "w") as f:
                    json.dump(data, f, indent=4)
                print(
                    f"'{exc}' exchange info saved to '{local_fn}'. Server ts: {datetime.datetime.utcfromtimestamp(data['serverTime']/1000)}"
                )
            except Exception as e:
                print(f"Failed to save {local_fn}")
                print(e)

        # except Exception as e:
        #     print(f"Error while fetching data from {exc}. {e}")

    def binance_rules(self):
        """
        Parse Binance Futures trading rules.
        """
        rules = {
            "quantityPrecision": 1,
            "pricePrecision": 6,
            "minQty": 1,
            "notional": 0.0001,
            "stepSize": 0.1,
        }

        try:
            with open("BinanceFuturesExchangeInfo.json") as f:
                data = json.load(f)

            for i in data["symbols"]:
                if (
                    i["symbol"] == self._symbol.replace("-", "")
                    or self._symbol.replace("-", "") in i["symbol"]
                ):
                    rules_json = i
                    break

            rules["pricePrecision"] = int(rules_json["pricePrecision"])
            rules["minQty"] = float(rules_json["filters"][1]["minQty"])
            rules["stepSize"] = float(rules_json["filters"][2]["stepSize"])
            rules["notional"] = float(rules_json["filters"][5]["notional"])
            rules["quantityPrecision"] = int(
                rules_json["quantityPrecision"]
            )  # Base asset precision
        except:
            print("Error in BinanceFuturesExchangeInfo.json")
            exit()

        return rules

    def bybit_rules(self):  # sourcery skip: move-assign-in-block, use-next
        """ "Parse Bybit trading rules compatible with Binance Futures."""
        rules_json = None

        rules = {
            "quantityPrecision": 1,
            "pricePrecision": 6,
            "minQty": 1,
            "notional": 0.0001,
            "stepSize": 0.1,
        }

        exc = "Bybit Perpetual"
        local_fn = f"{exc.replace(' ', '')}ExchangeInfo.json"

        try:
            with open(local_fn) as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error in {local_fn}")
            print(e)
            exit()

        for i in data["result"]:
            # TODO: Add USD pairs later!
            if i["name"] == self._symbol.replace("-", ""):
                rules_json = i
                break

        if rules_json is None:
            print(f"Error in rules_json. {local_fn}")
            exit()

        try:
            # I don't regret.
            rules["quantityPrecision"] = len(
                str(rules_json["lot_size_filter"]["qty_step"]).split(".")[1]
            )
        except Exception:
            rules["quantityPrecision"] = 1  # TODO

        rules["pricePrecision"] = rules_json["price_scale"]
        rules["minQty"] = float(rules_json["lot_size_filter"]["min_trading_qty"])
        rules["stepSize"] = float(rules_json["lot_size_filter"]["qty_step"])

        #  TODO Bybit has no notional rules. Just keep it very low to make minQty priority.
        rules["notional"] = 0.00001

        return rules

    def ftx_rules(self):  # sourcery skip: move-assign-in-block, use-next
        """ "
        Parse FTX trading rules compatible with Binance Futures.

        Example json data for FTX rules:
        {
            "name": "ETH-PERP",
            "enabled": true,
            "postOnly": false,
            "priceIncrement": 0.1,
            "sizeIncrement": 0.001,
            "minProvideSize": 0.001,
            "last": 1336.8,
            "bid": 1336.7,
            "ask": 1336.8,
            "price": 1336.8,
            "type": "future",
            "futureType": "perpetual",
            "baseCurrency": null,
            "isEtfMarket": false,
            "quoteCurrency": null,
            "underlying": "ETH",
            "restricted": false,
            "highLeverageFeeExempt": true,
            "largeOrderThreshold": 3000.0,
            "change1h": 0.00928652321630804,
            "change24h": -0.01080361107000148,
            "changeBod": -0.019006384383943642,
            "quoteVolume24h": 1719986677.1148,
            "volumeUsd24h": 1719986677.1148,
            "priceHigh24h": 1370.0,
            "priceLow24h": 1315.7
        },
        """
        rules_json = None

        rules = {
            "quantityPrecision": 1,
            "pricePrecision": 6,
            "minQty": 1,
            "notional": 0.0001,
            "stepSize": 0.1,
        }

        exc = "FTX"
        local_fn = f"{exc.replace(' ', '')}ExchangeInfo.json"

        try:
            with open(local_fn) as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error in {local_fn}")
            print(e)
            exit()

        sym = self.symbol.replace("-USD", "-PERP")

        for i in data["result"]:

            if i["name"] == sym:
                rules_json = i
                break

        if rules_json is None:
            print(f"Error in rules_json. {local_fn}")
            print(f"Symbol: {self.symbol}")
            exit()

        # "priceIncrement": 0.1,
        # "sizeIncrement": 0.001,
        # "minProvideSize": 0.001,
        # 'ETH-PERP': {'quantityPrecision': 3, 'pricePrecision': 1, 'minQty': 0.001, 'notional': 0.0001, 'stepSize': 0.001},

        rules["quantityPrecision"] = dec[str(rules_json["sizeIncrement"])]
        rules["pricePrecision"] = dec[str(rules_json["priceIncrement"])]
        rules["minQty"] = float(rules_json["minProvideSize"])
        rules["stepSize"] = float(rules_json["sizeIncrement"])

        #  TODO FTX has no notional rules. ⁉ Just keep it very low to make minQty priority.
        rules["notional"] = 0.00001

        print("rules:", rules)
        return rules

    def ftx_rules_hardcode(self):
        """
        BTC-PERP
        "priceIncrement": 1,
            "sizeIncrement": 0.0001,
            "minProvideSize": 0.001,

        ETH-PERP
        "priceIncrement": 0.1,
            "sizeIncrement": 0.001,
            "minProvideSize": 0.001,

        SOL-PERP
        "priceIncrement": 0.0025,
            "sizeIncrement": 0.01,
            "minProvideSize": 0.01,

        XMP-PERP
        "priceIncrement": 0.01,
            "sizeIncrement": 0.01,
            "minProvideSize": 0.01,
        """

        def_rules = {
            "quantityPrecision": 1,
            "pricePrecision": 6,
            "minQty": 1,
            "notional": 0.0001,
            "stepSize": 0.1,
        }

        rules = {
            "BTC-PERP": {
                "quantityPrecision": 4,
                "pricePrecision": 0,
                "minQty": 0.001,
                "notional": 0.0001,
                "stepSize": 0.0001,
            },
            "ETH-PERP": {
                "quantityPrecision": 3,
                "pricePrecision": 1,
                "minQty": 0.001,
                "notional": 0.0001,
                "stepSize": 0.001,
            },
            "XMR-PERP": {
                "quantityPrecision": 2,
                "pricePrecision": 2,
                "minQty": 0.01,
                "notional": 0.0001,
                "stepSize": 0.01,
            },
            "SOL-PERP": {
                "quantityPrecision": 2,
                "pricePrecision": 3,
                "minQty": 0.01,
                "notional": 0.0001,
                "stepSize": 0.01,
            },
        }

        return rules[self.symbol]

    # Utility functions

    @property
    def cap(self):
        """
        Return available balance (capital)
        If *use initial balance* is enabled return initial balance
        """
        return self.initial_balance if self.use_initial_balance else self.balance

    @property
    def profit_ratio2(self):
        return log2(max(self.initial_balance, self.cap) / self.initial_balance)

    @property
    def profit_ratio10(self):
        return log10(max(self.initial_balance, self.cap) / self.initial_balance)

    @property
    def is_trading(self):
        return is_live()

    @property
    def ts(self):
        if is_live():
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            return datetime.datetime.utcfromtimestamp(
                self.current_candle[0] / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")

    def console(self, msg, send_notification=True):
        if self.log_enabled:
            if is_live():
                if (
                    version("jesse").split(".")[0] == "0"
                    and int(version("jesse").split(".")[1]) < 36
                ):
                    self.log(f"{self.ts} {self.symbol} {msg}")
                else:
                    self.log(
                        f"{self.ts} {self.symbol} {msg}",
                        send_notification=send_notification,
                    )
            else:
                # self.jesse_version()
                print(f"\n{self.ts} {self.symbol} {msg}")

    def jesse_version(self):
        if (
            version("jesse").split(".")[0] == "0"
            and int(version("jesse").split(".")[1]) < 36
        ):
            print(f"\nJesse version < 0.36.0, Installed: {version('jesse')}")
        else:
            print(f"\nJesse version >= 0.36.0, Installed: {version('jesse')}")

    def debug(self, msg):
        if self.debug_enabled:
            if is_live():
                self.log(f"{self.ts} {self.symbol} {msg}")
            else:
                print(f"{self.ts} {self.symbol} {msg}")

    def log_balance_to_dc(self):
        strategy_name = self.__class__.__name__
        last_trade = self.trades[-1]
        bot_name = f"{self.app_port} {strategy_name} {self.symbol} {self.exchange} {self.leverage}x"
        msg = f"Balance: {self.initial_balance:0.2f} -> {self.balance:0.2f}, Profit: {last_trade.pnl:0.2f}"
        self.to_discord(self.wallets_dc_hook, bot_name, msg)

    def to_discord(self, hook_url=None, username="None", msg="None"):
        data = {"content": msg, "username": username}

        if is_live():
            if not hook_url:
                print(f"\n{self.ts} {self.symbol} Check custom hook url in .env file!")
                return

            try:
                result = requests.post(hook_url, json=data)
                result.raise_for_status()
            except requests.exceptions.HTTPError as err:
                self.console(err, False)
            else:
                self.console(
                    f"Payload delivered successfully, code {result.status_code}.", False
                )
        elif self.log_enabled:
            print(f"{self.ts} {self.symbol} {data}")

    def ftx_watchlist(self) -> list:
        try:
            wl = [
                ("Updated at", self.ts),
                ("Symbol", self.symbol),
                ("Liquidation Price", f"{self.LP1:0.2f}" if self.LP1 is not float("nan") else "N/A"),
                ("Margin Fraction", f"{self.margin_fraction * 100:0.2f}%" if self.margin_fraction and self.margin_fraction is not float("nan") else "N/A"),
                ("Maintenance Margin Fraction", f"{self.maintenance_margin_fraction * 100:0.2f}%" if self.maintenance_margin_fraction and self.maintenance_margin_fraction is not float("nan") else "N/A"),
                ("Liquidation Distance", f"{self.liquidation_distance:0.4f}%" if self.liquidation_distance and self.liquidation_distance is not float("nan") else "N/A"),
                ("Inv. Liquidation Distance", f"{self.ld_inverse:0.4f}%" if self.ld_inverse and self.ld_inverse is not float("nan") else "N/A"),
                ("Base IMF", f"{self.base_imf:0.3f}"),
                ("Position IMF", f"{self.position_imf:0.3f}" if self.position_imf and self.position_imf is not float("nan") else "N/A"),
                ("Position MMF", f"{self.position_mmf:0.3f}" if self.position_mmf and self.position_mmf is not float("nan") else "N/A"),
                ("Collateral Used", f"{self.collateral_used:0.2f}"),
                ("Total Position Notional", f"{self.total_position_notional:0.2f}"),
                ("Account MMF", f"{self.account_mmf:0.3f}" if self.account_mmf and self.account_mmf is not float("nan") else "N/A"),
                ("ACMF", f"{self.acmf:0.3f}" if self.acmf and self.acmf is not float("nan") else "N/A"),
                ("Zero Price", f"{self.zero_price:0.2f}" if self.zero_price and self.zero_price is not float("nan") else "N/A"),
                ("Position Zero Price", f"{self.pzp:0.2f}" if self.pzp and self.pzp is not float("nan") else "N/A"),
                ("Maintenance Collateral", f"{self.maintenance_collateral:0.2f}" if self.maintenance_collateral and self.maintenance_collateral is not float("nan") else "N/A"),
                ("LP Rate", f"{self.lp_rate():0.3f}" if self.lp_rate() and self.lp_rate() is not float("nan") else "N/A")
            ]
        except Exception as e:
            return [
                ("Updated at", self.ts),
                ("Symbol", self.symbol),
                ("Status", "Watchlist error!"),
                ("Error", e)
            ]
        
        return wl

    def watch_list(self) -> list:
        wl = [("Status", "Not ready.")]

        if self.first_run:
            return wl

        self.update_shared_vars("Watchlist")

        if self.ftx:
            return self.ftx_watchlist()

        try:
            # self.update_shared_vars('Watchlist')  # Moved method body.
            wl = [
                ("Updated at", self.ts),
                ("Symbol", self.symbol),
                (
                    "self.available_margin",
                    self.available_margin
                    if isinstance(self.available_margin, (float, int))
                    else "N/A",
                ),
                # Experimental metrics
                (
                    "self.avail_margin",
                    f"{self.avail_margin:0.2f}"
                    if isinstance(self.avail_margin, (float, int))
                    else "N/A",
                ),
                # New metric
                (
                    "Est. Liquidation Price",
                    f"{self.LP1:0.2f}" if self.LP1 > 0 else "--",
                ),
                (
                    "self.avgEntryPrice",
                    f"{self.avgEntryPrice:0.2f}"
                    if isinstance(self.avgEntryPrice, (float, int))
                    else "N/A",
                ),
                (
                    "Total Positions Value",
                    f"{self.shared_vars['total_value']:0.2f}"
                    if self.shared_vars["total_value"]
                    and self.shared_vars["total_value"] is not float("nan")
                    else "N/A",
                ),
                (
                    "Unrealized Pnl",
                    f"{self.shared_vars['unrealized_pnl']:0.2f}"
                    if self.shared_vars["unrealized_pnl"]
                    and self.shared_vars["unrealized_pnl"] is not float("nan")
                    else "N/A",
                ),
                (
                    "Margin Balance",
                    f"{self.shared_vars['margin_balance']:0.2f}"
                    if self.shared_vars["margin_balance"]
                    and self.shared_vars["margin_balance"] is not float("nan")
                    else "N/A",
                ),
                (
                    "Maintenance Margin",
                    f"{self.shared_vars['maint_margin']:0.2f}"
                    if self.shared_vars["maint_margin"]
                    and self.shared_vars["maint_margin"] is not float("nan")
                    else "N/A",
                ),
                (
                    "Margin Ratio",
                    f"{self.shared_vars['margin_ratio']:0.2f}%"
                    if self.shared_vars["margin_ratio"]
                    and self.shared_vars["margin_ratio"] is not float("nan")
                    else "N/A",
                ),
                (
                    "Max. Margin Ratio",
                    f"{self.shared_vars['max_margin_ratio']:0.2f}% at {self.shared_vars['max_margin_ratio_ts']}"
                    if self.shared_vars["max_margin_ratio"]
                    and self.shared_vars["max_margin_ratio"] is not float("nan")
                    and self.shared_vars["max_margin_ratio_ts"]
                    and self.shared_vars["max_margin_ratio_ts"] is not float("nan")
                    else "N/A",
                ),
                (f"{self.symbol} Max Total Value:", f"{self.max_position_value:0.2f}"),
                (
                    "Shared Max. Total Value",
                    f"{self.shared_vars['max_total_value']:0.2f}",
                ),
                (
                    "Locked Balance",
                    f"{self.shared_vars['locked_balance']:0.2f} $"
                    if self.shared_vars["locked_balance"]
                    and self.shared_vars["locked_balance"] is not float("nan")
                    else "N/A",
                ),
                (
                    "Free Balance",
                    f"{self.shared_vars['free_balance']:0.2f} $"
                    if self.shared_vars["free_balance"]
                    and self.shared_vars["free_balance"] is not float("nan")
                    else "N/A",
                ),
            ]
        except Exception as e:
            return [
                ("Updated at", self.ts),
                ("Symbol", self.symbol),
                ("Status", "Watchlist error!"),
                ("Error", e)
            ]

        # print(wl)
        return wl

    def terminate(self):
        print(f"Standalone Strategy Template v. {version('strat')}")

        try:
            # self.console(f'Max. PZP: {self.max_zp}')
            print(self.ftx_metrics)
        except Exception:
            pass
        
        try:
            self.test_max_pos_size_vs_leverage()
        except Exception:
            self.console("Max. position size vs leverage test failed.")

        try:
            print(
                f"{self.symbol} Max. re-entry: {self.max_open_positions}, "
                f"Max. Position Value: {self.max_position_value:0.2f}, "
                f"Min. Margin: {self.shared_vars['min_margin']:.0f}, "
                f"Max. Margin Ratio: {self.shared_vars['max_margin_ratio']:0.02f}% at {self.shared_vars['max_margin_ratio_ts']}, "
                f"Max. LP Ratio: {self.shared_vars['max_lp_ratio']:0.02f} at {self.shared_vars['max_lp_ratio_ts']}, "
                f"Free balance: {self.shared_vars['free_balance']:.0f}, "
                f"Locked balance: {self.shared_vars['locked_balance']:.0f}, "
                f"Parameters: {self.hp}"
            )

            print(
                f"\n{'Max. Margin Ratio':<24}| {self.shared_vars['max_margin_ratio']}%"
            )
            print(f"{'Minimum Margin':<24}| {round(self.shared_vars['min_margin'])}")
            print(
                f"{'Annual/MR':<24}| {self.metrics['annual_return'] / (self.shared_vars['max_margin_ratio'] * 2):0.2f}"
            )
            print(
                f"{'Shared Max. Total Value':<24}| {self.shared_vars['max_total_value']:0.2f}"
            )
            print(f"{'Max. LP Ratio':<24}| {self.shared_vars['max_lp_ratio']:0.02f}")
            # print(f"{'Insuff. Margin Count':<24}| {self.insuff_margin_count}")
            # print(f"{'Insuff. Margin Count':<24}| {self.max_insuff_margin_count}")
            print(
                f"{'Trades have Insuff. Margin Count':<24}| {self.unique_insuff_margin_count}"
            )
        except Exception as e:
            print(f"{self.symbol} {e}")

        if "--light-reports" in sys.argv:
            print("\nCreating light reports...")
            try:
                JesseTradingViewLightReport.generateReport()
            except Exception as e:
                print(e, "JesseTradingViewLightReport is not available, skipping...")

        # print(self.watch_list())
