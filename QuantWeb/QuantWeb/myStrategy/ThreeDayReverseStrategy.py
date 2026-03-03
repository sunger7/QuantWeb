from akquant import Strategy

class ThreeDayReverseStrategy(Strategy):
    """
    连续跌三天后买入，连续涨三天后卖出的反转策略
    """
    warmup_period = 4

    def __init__(self,cash=100_000.0, commission=0.0003):
        super().__init__()
        self.price_history = {}

    def on_start(self):
        pass

    def on_bar(self, bar):
        symbol = bar.symbol
        current_price = bar.close
        if symbol not in self.price_history:
            self.price_history[symbol] = []
        self.price_history[symbol].append(current_price)
        if len(self.price_history[symbol]) > 4:
            self.price_history[symbol] = self.price_history[symbol][-4:]
        if len(self.price_history[symbol]) < 4:
            return
        prices = self.price_history[symbol]
        position = self.get_position(symbol)
        is_three_down = (prices[-4] > prices[-3] and prices[-3] > prices[-2] and prices[-2] > prices[-1])
        is_three_up = (prices[-4] < prices[-3] and prices[-3] < prices[-2] and prices[-2] < prices[-1])
        if is_three_down and position == 0:
            cash = self.get_cash()
            if cash > 0 and current_price > 0:
                quantity = int(cash / current_price / 100) * 100
                if quantity > 0:
                    self.buy(symbol=symbol, quantity=quantity)
        elif is_three_up and position > 0:
            self.sell(symbol=symbol, quantity=position)
