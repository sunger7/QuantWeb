import numpy as np
from akquant import Strategy

class DualMAStrategy(Strategy):
    """
    继承 akquant.Strategy 类，这是所有策略的基类。
    """

    warmup_period = 40  # 30日均线 + 10 安全余量

    def __init__(self, fast_window=10, slow_window=30,cash=100_000.0, commission=0.0003):
        # 定义策略参数：快线周期和慢线周期
        self.fast_window = fast_window
        self.slow_window = slow_window
        # self.symbol = None

        # 动态设置预热期
        self.warmup_period = slow_window + 10

    def on_start(self):
        """策略启动时执行一次"""
        # 从数据中获取symbol
        pass

            
            # 从bar数据中动态获取symbol时无需显式订阅

    def on_bar(self, bar):
        """每一根 K 线走完时，都会触发一次这个函数"""

        # 1. 获取历史收盘价
        closes = self.get_history(count=self.slow_window, symbol=bar.symbol, field="close")

        # 如果数据还不够计算长均线，就直接返回，不操作
        if len(closes) < self.slow_window:
            return

        # 2. 计算均线
        fast_ma = np.mean(closes[-self.fast_window:])  # 快线
        slow_ma = np.mean(closes[-self.slow_window:])  # 慢线

        # 3. 获取当前持仓
        position = self.get_position(bar.symbol)

        # 4. 交易信号判断
        # 金叉：短线 > 长线，且当前空仓 -> 全仓买入
        if fast_ma > slow_ma and position == 0:
            cash = self.get_cash()
            if cash > 0 and bar.close > 0:
                quantity = int(cash / bar.close / 100) * 100  # 按100股整手买入
                if quantity > 0:
                    self.buy(symbol=bar.symbol, quantity=quantity)

        # 死叉：短线 < 长线，且当前持仓 -> 全仓卖出
        elif fast_ma < slow_ma and position > 0:
            self.sell(symbol=bar.symbol, quantity=position)