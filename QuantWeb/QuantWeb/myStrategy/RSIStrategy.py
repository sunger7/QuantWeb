import numpy as np
from akquant import Strategy


class RSIStrategy(Strategy):
    """
    RSI（相对强弱指标）策略：
    - RSI 低于 oversold（默认30）时买入
    - RSI 高于 overbought（默认70）时卖出
    """

    warmup_period = 20  # rsi_period + 安全余量

    def __init__(self, rsi_period=14, oversold=30, overbought=70,
                 cash=100_000.0, commission=0.0003):
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.warmup_period = rsi_period + 6

    def on_start(self):
        pass

    def on_bar(self, bar):
        # 获取足够的历史收盘价来计算 RSI
        closes = self.get_history(count=self.rsi_period + 1,
                                  symbol=bar.symbol, field="close")
        if len(closes) < self.rsi_period + 1:
            return

        # 计算 RSI
        closes = np.array(closes)
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = np.mean(gains[-self.rsi_period:])
        avg_loss = np.mean(losses[-self.rsi_period:])

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - 100.0 / (1.0 + rs)

        position = self.get_position(bar.symbol)

        # RSI 超卖 → 全仓买入
        if rsi < self.oversold and position == 0:
            cash = self.get_cash()
            if cash > 0 and bar.close > 0:
                quantity = int(cash / bar.close / 100) * 100
                if quantity > 0:
                    self.buy(symbol=bar.symbol, quantity=quantity)

        # RSI 超买 → 全仓卖出
        elif rsi > self.overbought and position > 0:
            self.sell(symbol=bar.symbol, quantity=position)
