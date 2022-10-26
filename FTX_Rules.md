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