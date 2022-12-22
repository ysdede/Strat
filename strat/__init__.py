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
from pathlib import Path

# if is_live:
import pickle

try:
    import JesseTradingViewLightReport
except:
    pass

dec = {
    "0": 0,
    "1": 0,
    "1.0": 0,
    "0.1": 1,
    "0.01": 2,
    "0.001": 3,
    "0.0001": 4,
    "0.00001": 5,
    "0.000001": 6,
    "0.0000001": 7,
    "0.00000001": 8,
}


class Strat(Vanilla):
    """
    The proxy strategy class which adds extra methods to Jesse base strategy.
    """

    def __init__(self):
        super().__init__()
        print(f"Standalone Strategy Template v. {version('strat')}")

        ex_exchanges = ["Binance Futures", "Binance", "Bybit Perpetual"]
        exchange_codes = {
            "Binance Perpetual Futures": "Binance Futures",
            "Binance Spot": "Binance",
            "Bybit USDT Perpetual": "Bybit Perpetual",
        }

        self.trade_rule_urls = {
            "Binance": "https://api.binance.com/api/v1/exchangeInfo",
            "Binance Futures": "https://fapi.binance.com/fapi/v1/exchangeInfo",
            "Bybit USDT Perpetual": "https://api.bybit.com/v2/public/symbols",
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
        self.pause_ap_file = None

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
        self.shared_vars["max_dd_sim"] = 0

        self.max_position_value = 0
        self.active = False
        self.trade_ts = None
        self.first_run = True

        self.max_open_positions = 0
        self.current_cycle_positions = 0

        self.insuff_margin_count = 0
        self.max_insuff_margin_count = 0
        self.unique_insuff_margin_count = 0
        self.max_dd_sim = 0
        self.cycle_initial_balance = 0

        self.resume = False
        self.udd_stop_count = 0
        self.udd_stop_events = []
        self.udd_stop_losses = 0

        # Settings:
        self.udd_stop_enabled = False  # Disabled by default, if this setting is missing in any strategy it will not perform udd stop.
        self.cooldown_len = 2 * 60 * 60 * 1000  # h * m * s * ms
        self.last_trade_type = None  # 'udd_stop'
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

    def before(self) -> None:
        if self.first_run:
            self.run_once()

    def run_once(self):
        print("--------> RUN ONCE!")
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
            self._symbol = self.symbol.replace("-USD", "-PERP")

        # If exchange rule files are not present or we're trading live, download them
        exc = "Bybit USDT Perpetual" if self.trade_with_bybit_rules else self.exchange

        local_fn = f"{exc.replace(' ', '')}ExchangeInfo.json".replace(
            "BinanceExch", "BinanceFuturesExch"
        )

        if (
            self.exchange == "Bybit Perpetual"
            or self.exchange == "Bybit USDT Perpetual"
            or self.trade_with_bybit_rules
        ):
            if not os.path.exists(local_fn) or is_live():
                self.download_rules(exchange="Bybit USDT Perpetual")
            else:
                print(f'Loading {self.exchange} rules from cached file: {local_fn}')
            rules = self.bybit_rules()
        else:
            # Fall back to Binance Perp rules if exchange != bybit
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
        self.pause_ap_file = f"{self.symbol}.pause_ap"
        self.kill_sw_file = "KILL.SWITCH"

        self.console(
            f"INFO: Break even file name: {self.break_even_file}, Pause at profit file name: {self.pause_ap_file}, Pause file name: {self.pause_file}, Killswitch file name: {self.kill_sw_file} ",
            force=True,
        )

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
        self.shared_vars["max_dd_sim"] = 0  # Simulated max. drawdown

        self.update_shared_vars("runonce")

        self.min_pnl = 0

        self.dd = {
            "min_pnl_ratio": 0,
            "pnl": 0,
            "pnl_perc": 0,
            "lpr": 0,
            "mr_ratio": 0,
            "balance": self.balance,
            "ts": 0,
        }

        self.first_run = False

    @property
    def liq_metrics(self):
        return f"LPR: {self.lp_rate():0.2f}, Liq. Price: {self.LP1:0.2f}"

    def restore_session(self):
        self.resume = False

    @property
    def quote_currency(self):
        return self.symbol.split("-")[1]

    @property
    def base_currency(self):
        return self.symbol.split("-")[0]

    @property
    def wallet_equivalent(self):
        return self.balance + self.position.pnl  # if self.is_open else selfbalance

    @property
    def udd(self):
        if self.position.pnl < 0:
            return self.position.pnl * 100 / self.balance
        return 0

    def save_min_pnl(self):
        if self.position.pnl < 0:
            # Leveraged margin  # does capital include current PNL?
            pnl_vs_capital = self.position.pnl * 100 / self.balance

            if pnl_vs_capital < self.dd["min_pnl_ratio"]:
                self.dd["min_pnl_ratio"] = pnl_vs_capital
                self.dd["pnl"] = self.position.pnl
                self.dd["pnl_perc"] = self.position.pnl_percentage
                self.dd["lpr"] = self.lp_rate()
                self.dd["mr_ratio"] = self.margin_ratio()
                self.dd["balance"] = self.balance
                self.dd["pos_size"] = self.position.value
                self.dd["ts"] = self.ts

    @property
    def drawdown_simulated(self):
        """
        Calculate current drawdown, save max dd simulated.
        """
        # if self.wallet_equivalent < self.initial_balance:
        # if self.wallet_equivalent < self.cycle_initial_balance:
        #     dd = (self.wallet_equivalent - self.cycle_initial_balance) / self.cycle_initial_balance * 100
        #     print(f"**** {self.wallet_equivalent=}, {self.cycle_initial_balance=}, {dd=}, {self.balance}, {self.position.value}")
        #     self.max_dd_sim = min(self.max_dd_sim, dd)
        #     return dd

        if self.position.pnl_percentage < 0:
            dd = self.position.pnl_percentage / self.leverage
            # print(f"**** {self.wallet_equivalent=}, {self.cycle_initial_balance=}, {dd=}, {self.balance=}, {self.position.value=}, PNL: {self.position.pnl_percentage / self.leverage}%, {self.position.pnl=}")
            self.max_dd_sim = min(self.max_dd_sim, dd)
            return dd
        return 0

    def save_session_as_pickle(self):
        self.console("Saving session as pickle...")
        try:
            with open(f"{self.session_file_name}", "wb") as f:
                pickle.dump(self.current_state, f)
        except Exception as e:
            self.console("Failed to save session.")
            self.console(e)

    def load_session_from_pickle(self):
        self.console("Loading session from pickle")
        try:
            with open(f"{self.session_file_name}", "rb") as f:
                self.restore_state_vars(pickle.load(f))
        except Exception as e:
            self.console(f"Error loading state from {self.session_file_name}")

    def update_shared_vars(self, caller=None):
        self.save_min_pnl()
        # dd_sim = self.drawdown_simulated
        # print(f'Update shared vars. Caller: {caller}, {self.drawdown_simulated=}, {self.balance=},?{self.wallet_equivalent=}, {self.cycle_initial_balance=}, {self.position.value=}')

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

        self.shared_vars["max_dd_sim"] = self.max_dd_sim = min(
            self.drawdown_simulated, self.shared_vars["max_dd_sim"]
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
                    # ❗ Needs to be checked.
                    upnl1 += self.shared_vars[r.symbol]["pnl"]
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
            return float("nan")
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

        lp = self.LP1

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
        Calculate the total value of all open positions.
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

        # self.position.value * self.risk_limits()['maintMarginRatio']  #  - self.risk_limits()['maintAmount']
        return mm

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
        else:
            print(
                f"Unknown exchange: {self.exchange}, loading Binance limits as default"
            )
            return self.binance_limits(psize, force_reload)

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
        # TODO: Bybit jsons are missing the last tiers' maintenance margin! Calculate next tiers.
        return r
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
                )  # rl_base_value * (int(b['id']) - 1) IDs are not 1 indexed

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
        # TODO: Bybit jsons are missing the last tiers' maintenance margin! Calculate next tiers.
        return r

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

    # def check_breakeven_or_killswitch(self):
    #     try:
    #         return self.break_even_file in os.listdir() or "KILL.SWITCH" in os.listdir()
    #     except:
    #         self.console(
    #             "Exception in checking break even/ks file.
    #         )
    #         return False

    def check_breakeven_or_killswitch(self, caller=""):
        if self.check_breakeven():
            self.console(f"{self.break_even_file=} file still exits. Caller: {caller}")
            return True

        if self.check_killswitch():
            if not self.is_trading:
                print("ks.", end="")
            else:
                self.console(f"{self.kill_sw_file=} file still exits. Caller: {caller}")
            return True

        return False

    def check_breakeven(self):
        try:
            return self.break_even_file in os.listdir()
        except:
            self.console(
                f"Exception in checking break even file. {self.break_even_file=}"
            )
            return False

    def check_killswitch(self):
        try:
            return self.kill_sw_file in os.listdir()
        except:
            self.console(f"Exception in checking {self.kill_sw_file=} file.")
            return False

    def check_pause(self):
        try:
            return self.pause_file in os.listdir()
        except:
            self.console(
                f"Exception in checking pause file. {self.pause_file}", force=True
            )
            return False

    def check_pause_ap(self):
        try:
            return self.pause_ap_file in os.listdir()
        except:
            self.console(
                f"Exception in checking pause at profit file. {self.pause_ap_file}",
                force=True,
            )
            return False

    def test_leverage(self):
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

        if self.leverage > rls["initialLeverage"]:
            print(
                f"\n{self.ts}{self.symbol} {self.exchange} The maximum allowed leverage for your next position size ({psize:0.2f}) is {rls['initialLeverage']}x, and you have {self.leverage}x leverage set., Caller: {caller}"
            )

    def download_rules(self, exchange: str, local_fn: str = None):
        """Download the trading rules from the exchanges."""

        exc = "Bybit USDT Perpetual" if self.trade_with_bybit_rules else exchange

        if not local_fn:
            local_fn = f"{exc.replace(' ', '')}ExchangeInfo.json"

        print(
            f"Downloading rules for {exchange}. {local_fn=}, URL: {self.trade_rule_urls[exc]}"
        )
        # try:
        data = requests.get(self.trade_rule_urls[exc]).json()

        if "serverTime" not in data.keys():
            print("if 'serverTime' not in data.keys():")
            data["serverTime"] = datetime.datetime.now().timestamp() * 1000

        # Bybit api does not return server time so we need to add it manually using our server time
        if "ret_msg" in data and data["ret_msg"] == "OK":
            data["serverTime"] = datetime.datetime.now().timestamp() * 1000
            print("Added local timestamp to Bybit data")

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

    # Utility functions

    @property
    def current_state(self):
        return {
            "cycle_pos_size": self.cycle_pos_size,
            "max_position_value": self.max_position_value,
            "max_open_positions": self.max_open_positions,
            "max_insuff_margin_count": self.max_insuff_margin_count,
            "current_cycle_positions": self.current_cycle_positions,
            "max_cycle_entry_recorded": self.max_cycle_entry_recorded,
            "total_positions": self.total_positions,
            "insuff_margin_count": self.insuff_margin_count,
            "unique_insuff_margin_count": self.unique_insuff_margin_count,
            "last_trade_ts": self.last_trade_ts,
            "is_open": self.is_open,
            "dd": self.dd
            # 'shared_vars': self.shared_vars,  # Do we really need it?
        }

    def restore_state_vars(self, state):
        self.cycle_pos_size = state["cycle_pos_size"]
        self.max_position_value = state["max_position_value"]
        self.max_open_positions = state["max_open_positions"]
        self.max_insuff_margin_count = state["max_insuff_margin_count"]
        self.current_cycle_positions = state["current_cycle_positions"]
        self.max_cycle_entry_recorded = state["max_cycle_entry_recorded"]
        self.total_positions = state["total_positions"]
        self.insuff_margin_count = state["insuff_margin_count"]
        self.unique_insuff_margin_count = state["unique_insuff_margin_count"]
        # self.last_trade_ts = state["last_trade_ts"]

        # Reset dd metrics if it's not included in pickle save, it's just for this case.
        # We'll have it next runs.
        # It must reset values and recalculate at next candle.
        try:
            self.dd = state["dd"]
        except Exception as e:
            self.console("dd metrics not found, resetting to defaults.")
            self.dd = {
                "min_pnl_ratio": 0,
                "pnl": 0,
                "pnl_perc": 0,
                "lpr": 0,
                "mr_ratio": 0,
                "balance": self.balance,
                "ts": 0,
            }
        self.console(f"📀 {self.cycle_pos_size=}")
        # self.shared_vars = state['shared_vars']

    def clear_string(self, string):
        return string.replace(" ", "_").replace("\n", "")

    @property
    def session_file_name(self):
        strategy_name = self.__class__.__name__
        # last_trade = self.trades[-1]
        return self.clear_string(
            f"{self.exchange}-{self.symbol}-{strategy_name}-{self.leverage}-{self.timeframe}-{self.app_port}.pickle"
        )

    def binance_ob_ticker(self):
        order_book_url = f"https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={self.symbol.replace('-', '')}"

        # start = time.time()

        try:
            data = requests.get(order_book_url).json()
            return data
        except Exception as e:
            print(e)
            return ""

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

    def ts_to_str(self, ts):
        if ts:
            return datetime.datetime.utcfromtimestamp(ts / 1000).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

    @property
    def ts(self):
        if is_live():
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            return datetime.datetime.utcfromtimestamp(
                self.current_candle[0] / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")

    def console(self, msg, send_notification=True, force=False):
        if self.log_enabled or force:
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

    def log_metrics_after_closing(self, metrics):
        self.console(
            f"📈 Initial/Current Balance: {self.initial_balance:0.2f}/{self.balance:0.2f}, current udd: {self.udd:0.2f}, udd stop: {self.udd_stop}, Max udd: {self.dd['min_pnl_ratio']:0.2f}, Max. DD: {metrics['max_drawdown']:0.2f}, Total Fee: {metrics['fee']:0.3f}, Largest Win: {metrics['largest_winning_trade']:0.2f}, Sharpe: {metrics['sharpe_ratio']:0.2f}, Calmar: {metrics['calmar_ratio']:0.2f} Max. Lpr: {self.shared_vars['max_lp_ratio']:0.02f} at {self.shared_vars['max_lp_ratio_ts']} udd count: {self.udd_stop_count}, Sum of udd stop losses: {self.udd_stop_losses:0.1f}"
        )

    def log_increasing_position_msg(self, qty):
        self.console(
            f"🔼 Increasing position with {self.cycle_pos_size:0.2f} {self.quote_currency}, Qty: {qty} {self.base_currency} "
            f"Current Pnl {round(self.position.pnl_percentage / self.leverage, 2)}%, "
            f"current udd: {self.udd:0.2f}, udd stop: {self.udd_stop}, Max udd: {self.dd['min_pnl_ratio']:0.2f}, "
            f"{self.liq_metrics}, cycle_pos: {self.current_cycle_positions}, dev_limit: {self.deviation_limit}, AvgEntry: {self.avgEntryPrice:0.5f}"
        )

    def log_balance_to_dc(self):
        strategy_name = self.__class__.__name__
        last_trade = self.trades[-1]
        bot_name = f"{self.app_port} {strategy_name} {self.symbol} {self.exchange} {self.leverage}x"
        msg = f"Balance: {self.initial_balance:0.2f} -> {self.balance:0.2f}, Profit: {last_trade.pnl:0.2f}"
        # msg = f"Balance: {self.initial_balance:0.2f} -> {self.balance:0.2f}, Profit: {self.balance - self.initial_balance:0.2f}"
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

    def watch_list(self) -> list:
        wl = [("Status", "Not ready.")]

        if self.first_run:
            return wl

        self.update_shared_vars("Watchlist")

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
                ("Error", e),
            ]

        # print(wl)
        return wl

    def terminate(self):
        print(f"Standalone Strategy Template v. {version('strat')}")

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
            print(f"{'uDD Ratio':<24}| {self.dd['min_pnl_ratio']:0.2f}")

        except Exception as e:
            print(f"{self.symbol} Error printing extra metrics! {e}")

        try:
            if metrics := self.metrics:
                net_profit_percentage = metrics["net_profit_percentage"]
                profit_per_udd = net_profit_percentage / abs(self.dd["min_pnl_ratio"])
                print(f"{'ppudd Ratio':<24}| {profit_per_udd:0.2f}")
        except Exception as e:
            pass

        # try:
        #     print(f"{'Max. DD simulated':<24}| {self.max_dd_sim:0.2f}")
        # except Exception as e:
        #     print(e)

        try:
            print(f"{'udd stop Count':<24}| {self.udd_stop_count}")
            print("udd stop Events: ", self.udd_stop_events)
        except Exception as e:
            pass
        
        # try:
        #     print(self.dd)
        # except Exception as e:
        #     print(e)

        if not self.is_trading and self.kill_sw_file in os.listdir():
            print(f"Removing {self.kill_sw_file=} file.")
            try:
                os.remove(self.kill_sw_file)
            except Exception as e:
                print(f"Could not remove {self.kill_sw_file}\n {e}")

        if "--light-reports" in sys.argv:
            print("\nCreating light reports...")
            try:
                JesseTradingViewLightReport.generateReport()
            except Exception as e:
                print(e, "JesseTradingViewLightReport is not available, skipping...")

        # print(self.watch_list())
