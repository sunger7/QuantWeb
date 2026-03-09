import numpy as np
from akquant import Strategy


class VWAPStrategy(Strategy):
    """
    VWAP 策略（基于滚动窗口）：
    - 收盘价上穿 VWAP 且空仓时买入
    - 收盘价下穿 VWAP 且持仓时卖出
    """

    warmup_period = 25

    def __init__(self, vwap_window=20, cash=100_000.0, commission=0.0003):
        self.vwap_window = vwap_window
        self.warmup_period = vwap_window + 5

    def on_start(self):
        pass

    def on_bar(self, bar):
        closes = self.get_history(count=self.vwap_window, symbol=bar.symbol, field="close")
        volumes = self.get_history(count=self.vwap_window, symbol=bar.symbol, field="volume")

        if len(closes) < self.vwap_window or len(volumes) < self.vwap_window:
            return

        closes = np.array(closes, dtype=float)
        volumes = np.array(volumes, dtype=float)
        vol_sum = np.sum(volumes)
        if vol_sum <= 0:
            return

        vwap = float(np.sum(closes * volumes) / vol_sum)
        position = self.get_position(bar.symbol)

        if bar.close > vwap and position == 0:
            cash = self.get_cash()
            if cash > 0 and bar.close > 0:
                quantity = int(cash / bar.close / 100) * 100
                if quantity > 0:
                    self.buy(symbol=bar.symbol, quantity=quantity)
        elif bar.close < vwap and position > 0:
            self.sell(symbol=bar.symbol, quantity=position)
