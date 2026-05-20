"""One-off builder: writes strategies_v5_data.py with STRATEGIES (68 entries)."""
from __future__ import annotations

from pathlib import Path

# (key, name, id, category, timeframes, description)
ROWS: list[tuple[str, str, str, str, list[str], str]] = [
    ("T01_EMA_PULLBACK", "EMA Trend Pullback", "T01", "TREND", ["4h", "1d", "1w"], "Price pulls back to EMA20/50 in established trend"),
    ("T02_EMA_CROSSOVER", "EMA Crossover Momentum", "T02", "TREND", ["4h", "1d", "1w"], "EMA20 crosses EMA50 with ADX>20 confirming new trend"),
    ("T03_HH_HL_CONTINUATION", "Higher Highs Higher Lows", "T03", "TREND", ["4h", "1d", "1w"], "Swing structure shows HH+HL (bull) or LL+LH (bear), enter on pullback"),
    ("T04_ADX_TREND_ENTRY", "ADX Trend Strength Entry", "T04", "TREND", ["4h", "1d"], "ADX crosses above 25 from below with +DI/-DI confirming direction"),
    ("T05_SUPERTREND_FLIP", "Supertrend Flip", "T05", "TREND", ["4h", "1d"], "Supertrend indicator flips direction with price closing beyond it"),
    ("T06_PARABOLIC_SAR_FLIP", "Parabolic SAR Flip", "T06", "TREND", ["4h", "1d"], "SAR dots switch sides with trend confirmation from EMAs"),
    ("T07_MA_RIBBON_ALIGNMENT", "Moving Average Ribbon", "T07", "TREND", ["1d", "1w"], "5 EMAs (8,13,21,34,55) all stacked in order = strong trend, enter on touch of fastest"),
    ("T08_DONCHIAN_BREAKOUT", "Donchian Channel Breakout", "T08", "TREND", ["1d", "1w"], "Price breaks 20-period Donchian high/low with ATR expansion"),
    ("T09_KELTNER_TREND_RIDE", "Keltner Channel Trend Ride", "T09", "TREND", ["4h", "1d"], "Price closes outside Keltner channel in trend direction with ADX>25"),
    ("T10_200EMA_BOUNCE", "200 EMA Bounce", "T10", "TREND", ["4h", "1d", "1w"], "Price touches 200 EMA in strong trend and bounces with reversal candle"),
    ("R01_EXTREME_ZONE_REVERSION", "Extreme Zone Reversion", "R01", "REVERSION", ["1d", "1w"], "Zone <15% or >85% with RSI extreme + ADX<45. Counter-trend reversion."),
    ("R02_RSI_DIVERGENCE", "RSI Divergence Reversion", "R02", "REVERSION", ["4h", "1d"], "Price makes new high/low but RSI diverges. Signals exhaustion."),
    ("R03_BB_EXTREME_TOUCH", "Bollinger Band Extreme Reversal", "R03", "REVERSION", ["4h", "1d"], "Price touches outer BB with reversal candle + RSI extreme"),
    ("R04_STOCHASTIC_EXTREME", "Stochastic Oversold/Overbought", "R04", "REVERSION", ["4h", "1d"], "Stochastic K crosses D in extreme zone (<20 or >80) with zone confirmation"),
    ("R05_CCI_EXTREME", "CCI Extreme Reversion", "R05", "REVERSION", ["4h", "1d"], "CCI below -100 or above +100 with price at zone extreme"),
    ("R06_WILLIAMS_R_EXTREME", "Williams %R Extreme", "R06", "REVERSION", ["4h", "1d"], "Williams %R below -80 or above -20 with reversal candle pattern"),
    ("R07_VWAP_DEVIATION_SWING", "VWAP Deviation Swing", "R07", "REVERSION", ["4h", "1d"], "Price >2 standard deviations from VWAP, mean reversion expected"),
    ("R08_MONTHLY_LEVEL_REJECTION", "Monthly Level Rejection", "R08", "REVERSION", ["1d", "1w"], "Price at monthly open/high/low with RSI extreme = high-probability rejection"),
    ("R09_WEEKLY_GAP_FILL", "Weekly Opening Gap Fill", "R09", "REVERSION", ["4h", "1d"], "Monday opens with gap from Friday close. 70%+ fill rate by Wednesday."),
    ("R10_COT_EXTREME_REVERSION", "COT Extreme Positioning", "R10", "REVERSION", ["1w"], "Commercial hedgers at 90th percentile positioning = reliable mean reversion"),
    ("B01_RANGE_BREAKOUT", "Range Breakout", "B01", "BREAKOUT", ["1d"], "BB width <3% + ADX<30 + clean close outside BB = breakout"),
    ("B02_VOLATILITY_COMPRESSION", "Volatility Compression Breakout", "B02", "BREAKOUT", ["1d", "4h"], "BB width <2% + ADX<20 = coiled spring, trade the direction of breakout"),
    ("B03_LONDON_OPEN_BREAKOUT", "London Open Breakout", "B03", "BREAKOUT", ["15m", "30m"], "Asian range breaks at London open (7-10am UTC). Stop at range midpoint."),
    ("B04_NY_OPEN_BREAKOUT", "NY Open Breakout", "B04", "BREAKOUT", ["15m", "30m"], "NY open confirms or reverses London direction at 12:30-15:00 UTC"),
    ("B05_OPENING_RANGE_BREAKOUT", "Opening Range Breakout", "B05", "BREAKOUT", ["15m", "30m"], "First 30min range established, clean break with volume"),
    ("B06_TRIANGLE_BREAKOUT", "Consolidation Triangle Breakout", "B06", "BREAKOUT", ["4h", "1d"], "Price compressing in narrowing triangle, break of upper/lower trendline"),
    ("B07_INSIDE_BAR_BREAKOUT", "Inside Bar Breakout", "B07", "BREAKOUT", ["4h", "1d", "1w"], "Small candle completely inside previous candle, trade the break of mother bar"),
    ("B08_KEY_LEVEL_RETEST", "Key Level Breakout Retest", "B08", "BREAKOUT", ["4h", "1d"], "Price breaks swing level, pulls back to retest, enter continuation"),
    ("B09_RSI_MOMENTUM_BREAK", "RSI Momentum Breakout", "B09", "BREAKOUT", ["4h", "1d"], "RSI breaks above 50 from below (or below 50 from above) with price confirmation"),
    ("B10_WEEKLY_RANGE_BREAK", "Weekly High/Low Breakout", "B10", "BREAKOUT", ["4h", "1d"], "Clean break of prior week high or low with volume and trend alignment"),
    ("M01_MACD_DIVERGENCE", "MACD Divergence", "M01", "MOMENTUM", ["4h", "1d"], "Price new extreme but MACD histogram disagrees = exhaustion signal"),
    ("M02_MACD_ZERO_CROSS", "MACD Zero Line Cross", "M02", "MOMENTUM", ["4h", "1d"], "MACD line crosses zero with histogram expanding in trend direction"),
    ("M03_RSI_MOMENTUM_CONTINUATION", "RSI Momentum Continuation", "M03", "MOMENTUM", ["4h", "1d", "1w"], "RSI stays above 50 in uptrend (or below 50 in downtrend) after pullback"),
    ("M04_ROC_MOMENTUM", "Rate of Change Momentum", "M04", "MOMENTUM", ["4h", "1d"], "ROC crosses zero with price closing beyond EMA20"),
    ("M05_STOCHASTIC_MOMENTUM_CROSS", "Stochastic Momentum Cross", "M05", "MOMENTUM", ["4h", "1d"], "Stochastic K crosses D in trend direction (not extreme zone)"),
    ("M06_PRICE_ACCELERATION", "Price Acceleration", "M06", "MOMENTUM", ["4h", "1d"], "Price moving faster than 20-period average rate, ATR expanding"),
    ("M07_VOLUME_SURGE_MOMENTUM", "Volume Surge Momentum", "M07", "MOMENTUM", ["4h", "1d"], "Volume 200%+ of 20-period average with directional close"),
    ("M08_NEWS_MOMENTUM", "News Momentum", "M08", "MOMENTUM", ["15m", "30m"], "Benzinga sentiment >0.4 or <-0.4 with price already moving in direction"),
    ("SMC01_SR_FLIP", "Support Resistance Flip", "SMC01", "STRUCTURE", ["4h", "1d", "1w"], "Level touched 2+ times, price breaks through, retests from other side"),
    ("SMC02_ORDER_BLOCK", "Institutional Order Block", "SMC02", "STRUCTURE", ["4h", "1d"], "Strong impulsive move, price returns to origin candle of that move"),
    ("SMC03_FAIR_VALUE_GAP", "Fair Value Gap Fill", "SMC03", "STRUCTURE", ["4h", "1d"], "Gap between candles >0.5x ATR, price returns to fill the imbalance"),
    ("SMC04_LIQUIDITY_SWEEP", "Liquidity Sweep Reversal", "SMC04", "STRUCTURE", ["4h", "1d"], "Equal highs/lows swept by spike then price recovers inside range"),
    ("SMC05_EQUAL_HL_HUNT", "Equal Highs/Lows Stop Hunt", "SMC05", "STRUCTURE", ["4h", "1d"], "Cluster of equal highs or lows (within 0.3%), price sweeps then reverses hard"),
    ("SMC06_INDUCEMENT_REVERSAL", "Inducement and Reversal", "SMC06", "STRUCTURE", ["4h", "1d"], "Price takes liquidity above a minor level then reverses to fill larger imbalance"),
    ("SMC07_BREAKER_BLOCK", "Breaker Block", "SMC07", "STRUCTURE", ["4h", "1d"], "Failed order block becomes opposite signal when price breaks through it"),
    ("SMC08_MITIGATION_BLOCK", "Mitigation Block", "SMC08", "STRUCTURE", ["4h", "1d"], "Price returns to origin of an impulsive move to mitigate unfilled orders"),
    ("SMC09_PREMIUM_DISCOUNT_MSS", "Premium/Discount + Market Structure Shift", "SMC09", "STRUCTURE", ["4h", "1d", "1w"], "Price in extreme zone + market structure shift = high-probability entry"),
    ("SMC10_CHOCH", "Change of Character", "SMC10", "STRUCTURE", ["4h", "1d"], "First break of recent swing structure in opposite direction = trend reversal signal"),
    ("V01_VIX_SPIKE_REVERSION", "VIX Spike Reversion", "V01", "VOLATILITY", ["1d", "1w"], "VIX spikes above 30, buy risk assets as fear subsides"),
    ("V02_ATR_EXPANSION_ENTRY", "ATR Expansion Entry", "V02", "VOLATILITY", ["4h", "1d"], "ATR expands sharply (>150% of 20-period avg), trade continuation of the move"),
    ("V03_SQUEEZE_BREAKOUT", "Bollinger-Keltner Squeeze", "V03", "VOLATILITY", ["4h", "1d"], "BB inside Keltner channel = extreme compression, explosive breakout imminent"),
    ("V04_PRE_NEWS_COMPRESSION", "Pre-News Compression", "V04", "VOLATILITY", ["15m", "30m"], "Volatility compresses before major news, trade the initial direction of break"),
    ("V05_POST_NEWS_CONTINUATION", "Post-News Continuation", "V05", "VOLATILITY", ["15m", "30m"], "Strong move after news continues in same direction after initial pullback"),
    ("V06_VOLATILITY_MEAN_REVERSION", "Volatility Mean Reversion", "V06", "VOLATILITY", ["1d", "1w"], "After extreme volatility period, price calms and reverts toward moving averages"),
    ("Q01_DAY_OF_WEEK_EDGE", "Day of Week Edge", "Q01", "STATISTICAL", ["1d"], "Certain pairs show directional bias on specific days historically"),
    ("Q02_TIME_OF_DAY_MOMENTUM", "Time of Day Momentum", "Q02", "STATISTICAL", ["4h"], "Specific 4H candles show directional bias based on session timing"),
    ("Q03_MONTHLY_SEASONALITY", "Monthly Seasonality", "Q03", "STATISTICAL", ["1w"], "Certain pairs have monthly directional tendencies from historical data"),
    ("Q04_CARRY_TRADE_MOMENTUM", "Carry Trade Momentum", "Q04", "STATISTICAL", ["1w"], "High interest rate differential pairs trend in carry direction in risk-on environments"),
    ("Q05_CORRELATION_DIVERGENCE", "Correlation Divergence", "Q05", "STATISTICAL", ["1d", "1w"], "Normally correlated pairs diverge, trade the convergence back to mean"),
    ("Q06_REGIME_DETECTION", "Hurst Exponent Regime Detection", "Q06", "STATISTICAL", ["1d", "1w"], "Hurst >0.5 = trending; Hurst <0.5 = mean reverting"),
    ("I01_VWAP_DEVIATION_SCALP", "VWAP Deviation Scalp", "I01", "INTRADAY", ["15m", "30m"], "RSI<30 + price below VWAP = LONG scalp. RSI>70 + above VWAP = SHORT scalp."),
    ("I02_HTF_LEVEL_REJECTION", "HTF Level Rejection Scalp", "I02", "INTRADAY", ["15m", "30m"], "Price at 4H/1D swing level with rejection candle on 15M chart"),
    ("I03_FIRST_HOUR_REVERSAL", "First Hour Reversal", "I03", "INTRADAY", ["15m", "30m"], "Price reverses first hour direction at session midpoint"),
    ("I04_POWER_HOUR_MOMENTUM", "Power Hour Momentum", "I04", "INTRADAY", ["15m", "30m"], "Last hour of session shows continuation of session trend with volume"),
    ("I05_GAP_FADE", "Session Gap Fade", "I05", "INTRADAY", ["15m", "30m"], "Gap up/down at session open fades back toward prior session close"),
    ("I06_ASIA_RANGE_FADE", "Asia Session Range Fade", "I06", "INTRADAY", ["15m", "30m"], "Fade Asia session extremes when London opens, targeting range midpoint"),
    ("I07_MICRO_STRUCTURE_SCALP", "Micro Structure Scalp", "I07", "INTRADAY", ["15m"], "Orderflow imbalance on 15M with HTF level confluence = quick scalp"),
    ("I08_SESSION_CLOSE_REVERSAL", "Session Close Reversal", "I08", "INTRADAY", ["15m", "30m"], "Price reverses 30min before session close as positions unwind"),
]

def main() -> None:
    strategies: dict[str, dict[str, object]] = {}
    for key, name, sid, cat, tfs, desc in ROWS:
        strategies[key] = {
            "name": name,
            "id": sid,
            "category": cat,
            "timeframes": tfs,
            "description": desc,
        }
    assert len(strategies) == 68, len(strategies)

    out = Path(__file__).resolve().parent.parent / "strategies_v5_data.py"
    header = '''"""APEX v5.0 strategy registry (68). Generated by scripts/build_strategies_v5_data.py."""
from __future__ import annotations

from typing import Any

'''
    body = "STRATEGIES: dict[str, dict[str, Any]] = " + repr(strategies) + "\n"
    out.write_text(header + body, encoding="utf-8")
    print("Wrote", out, "keys=", len(strategies))


if __name__ == "__main__":
    main()
