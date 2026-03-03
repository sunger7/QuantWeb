import pandas as pd
import numpy as np
from akquant import Strategy, run_backtest
import os

# --------------------------
# 双均线策略
# --------------------------
class DualMAStrategy(Strategy):
    """
    继承 akquant.Strategy 类，这是所有策略的基类。
    """

    warmup_period = 40  # 30日均线 + 10 安全余量

    def __init__(self, fast_window=10, slow_window=30):
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
        # 金叉：短线 > 长线，且当前空仓 -> 买入
        if fast_ma > slow_ma and position == 0:
            self.buy(symbol=bar.symbol, quantity=1000)

        # 死叉：短线 < 长线，且当前持仓 -> 卖出
        elif fast_ma < slow_ma and position > 0:
            self.sell(symbol=bar.symbol, quantity=position)


# --------------------------
# 三日反转策略
# --------------------------
class ThreeDayReverseStrategy(Strategy):
    """
    连续跌三天后买入，连续涨三天后卖出的反转策略
    """
    
    warmup_period = 4  # 需要至少4天数据来监控3天的涨跌情况

    def __init__(self):
        super().__init__()
        # 存储每个symbol最近几天的收盘价
        self.price_history = {}
        
    def on_start(self):
        """策略启动"""
        pass

    def on_bar(self, bar):
        """每个K线执行"""
        symbol = bar.symbol
        current_price = bar.close
        
        # 初始化该symbol的价格历史
        if symbol not in self.price_history:
            self.price_history[symbol] = []
        
        # 记录当前收盘价
        self.price_history[symbol].append(current_price)
        
        # 只保留最近4个价格（足以判断连续三天涨跌）
        if len(self.price_history[symbol]) > 4:
            self.price_history[symbol] = self.price_history[symbol][-4:]
        
        # 需要至少4个价格点才能判断连续3天的涨跌
        if len(self.price_history[symbol]) < 4:
            return
        
        prices = self.price_history[symbol]
        position = self.get_position(symbol)
        
        # 检查连续跌三天：prices[-4] > prices[-3] > prices[-2] > prices[-1]
        is_three_down = (prices[-4] > prices[-3] and 
                        prices[-3] > prices[-2] and 
                        prices[-2] > prices[-1])
        
        # 检查连续涨三天：prices[-4] < prices[-3] < prices[-2] < prices[-1]
        is_three_up = (prices[-4] < prices[-3] and 
                      prices[-3] < prices[-2] and 
                      prices[-2] < prices[-1])
        
        # 连续跌三天后买入
        if is_three_down and position == 0:
            self.buy(symbol=symbol, quantity=1000)
        
        # 连续涨三天后卖出
        elif is_three_up and position > 0:
            self.sell(symbol=symbol, quantity=position)


def prepare_etf_data(csv_path, etf_code):
    """
    准备单个ETF数据用于回测
    """
    df = pd.read_csv(csv_path)
    
    # 将日期列转换为datetime格式
    df['日期'] = pd.to_datetime(df['日期'])
    
    # 标准化列名为英文
    df = df.rename(columns={
        '日期': 'date',
        '开盘': 'open',
        '最高': 'high',
        '最低': 'low',
        '收盘': 'close',
        '成交量': 'volume',
        
    })
    
    # 确保symbol列存在，统一使用etf_code
    df['symbol'] = etf_code
    
    # 只保留需要的列
    required_cols = [col for col in ['date', 'open', 'high', 'low', 'close', 'volume', 'symbol'] if col in df.columns]
    df = df[required_cols]
    
    # 按日期排序
    df = df.sort_values('date').reset_index(drop=True)
    
    return df


def backtest_single_etf(etf_code, csv_path, strategy_class=DualMAStrategy, strategy_name="DualMA"):
    """
    对单个ETF进行回测，获取年化收益率
    
    Args:
        etf_code: ETF代码
        csv_path: CSV文件路径
        strategy_class: 策略类（默认为DualMAStrategy）
        strategy_name: 策略名称（用于显示）
    """
    if 1:
        # 准备数据
        df = prepare_etf_data(csv_path, etf_code)
        
        if len(df) < 2:
            return None
        
        # 确定策略参数
        if strategy_class == DualMAStrategy:
            strategy_params = {"fast_window": 10, "slow_window": 30}
        else:
            strategy_params = {}
        
        # 运行回测
        result = run_backtest(
            data=df,
            t_plus_one=False,
            strategy=strategy_class,
            strategy_params=strategy_params,
            cash=100_000.0,
            commission=0.0003 
        )
        
        return {
            'etf_code': etf_code,
            'annualized_return': result.metrics.annualized_return,
            'result': result,
            'strategy_name': strategy_name
        }
    try:
        pass   
    except Exception as e:
        print(f"  ✗ {etf_code}: 回测失败 - {str(e)[:50]}")
        return None


def analyze_all_etf_with_strategy(strategy_class=DualMAStrategy, strategy_name="DualMA", report_dir=None, limit=None):
    """
    分析所有ETF，找出年化收益率前10的
    
    Args:
        strategy_class: 使用的策略类
        strategy_name: 策略的显示名称
        limit: 分析的ETF数量限制（None表示全部）
    """
    etf_dir = "/Users/winssion/Desktop/akshare_proj/data/上证日线"
    # report_dir = "/Users/winssion/Desktop/akshare_proj/report"
    
    # 确保report目录存在
    os.makedirs(report_dir, exist_ok=True)
    
    # 获取所有CSV文件
    csv_files = sorted([f for f in os.listdir(etf_dir) if f.endswith('.csv')])
    if limit:
        csv_files = csv_files[:limit]
    
    print(f"找到 {len(csv_files)} 个ETF数据文件，开始分析...\n")
    
    backtest_results = []
    
    # 对每个ETF进行回测
    for idx, csv_file in enumerate(csv_files, 1):
        etf_code = csv_file.replace('.csv', '')
        csv_path = os.path.join(etf_dir, csv_file)
        
        print(f"[{idx:3d}/{len(csv_files)}] 回测 {etf_code}...", end='', flush=True)
        
        result = backtest_single_etf(etf_code, csv_path, strategy_class=strategy_class, strategy_name=strategy_name)
        
        if result:
            backtest_results.append(result)
            print(f" ✓ 年化收益率: {result['annualized_return']:.2f}%")
        else:
            print(" ✗ 失败")
    
    # 按年化收益率降序排序
    backtest_results.sort(key=lambda x: x['annualized_return'], reverse=True)
    
    # 选出前10个
    top_10 = backtest_results[:10]
    
    print("\n" + "=" * 80)
    print(f"年化收益率排序 - 前10个ETF ({strategy_name}策略)")
    print("=" * 80)
    
    summary_data = []
    for rank, item in enumerate(top_10, 1):
        etf_code = item['etf_code']
        ret = item['annualized_return']
        result_obj = item['result']
        
        print(f"{rank:2d}. {etf_code:<10} | 年化收益率: {ret:.2f}%")
        
        summary_data.append({
            '排名': rank,
            'ETF代码': etf_code,
            '年化收益率(%)': round(ret, 2),
            '策略': strategy_name
        })
        
        # 保存report
        try:
            report_file = os.path.join(report_dir, f"{etf_code}_{strategy_name}_report.html")
            result_obj.report(
                title=f"ETF {etf_code} - {strategy_name}策略回测报告",
                filename=report_file,
                show=False
            )
            print(f"   → report已保存: {report_file}")
        except Exception as e:
            print(f"   → 保存report失败: {str(e)[:50]}")
    
    print("=" * 80)
    
    # 保存汇总
    summary_df = pd.DataFrame(summary_data)
    summary_csv = os.path.join(report_dir, f'前10ETF汇总_{strategy_name}.csv')
    summary_df.to_csv(summary_csv, index=False, encoding='utf-8-sig')
    print(f"\n✓ {strategy_name}策略汇总报告已保存: {summary_csv}\n")
    
    return backtest_results


def analyze_all_etf():
    """
    分析所有ETF，找出年化收益率前10的
    """
    etf_dir = "/Users/winssion/Desktop/akshare_proj/data/上证日线"
    report_dir = "/Users/winssion/Desktop/akshare_proj/report"
    
    # 确保report目录存在
    os.makedirs(report_dir, exist_ok=True)
    
    # 获取所有CSV文件，限制为测试的前50个
    csv_files = sorted([f for f in os.listdir(etf_dir) if f.endswith('.csv')])[:]
    print(f"找到 {len(csv_files)} 个ETF数据文件，开始分析...\n")
    
    backtest_results = []
    
    # 对每个ETF进行回测
    for idx, csv_file in enumerate(csv_files, 1):
        etf_code = csv_file.replace('.csv', '')
        csv_path = os.path.join(etf_dir, csv_file)
        
        print(f"[{idx:3d}/{len(csv_files)}] 回测 {etf_code}...", end='', flush=True)
        
        result = backtest_single_etf(etf_code, csv_path)
        
        backtest_results.append({
            'etf_code': etf_code,
            'annualized_return': result['annualized_return'],
            'result': result['result']
        })
    
    # 按年化收益率降序排序
    backtest_results.sort(key=lambda x: x['annualized_return'], reverse=True)
    
    # 选出前10个
    top_10 = backtest_results[:10]
    
    print("\n" + "=" * 80)
    print("年化收益率排序 - 前10个ETF")
    print("=" * 80)
    
    summary_data = []
    for rank, item in enumerate(top_10, 1):
        etf_code = item['etf_code']
        ret = item['annualized_return']
        result_obj = item['result']
        
        print(f"{rank:2d}. {etf_code:<10} | 年化收益率: {ret}%")
        
        summary_data.append({
            '排名': rank,
            'ETF代码': etf_code,
            '年化收益率(%)': ret
        })
        
        # 保存report
        try:
            report_file = os.path.join(report_dir, f"{etf_code}_report.html")
            result_obj.report(
                title=f"ETF {etf_code} - 回测报告",
                filename=report_file,
                show=False
            )
            print(f"   → report已保存: {report_file}")
        except Exception as e:
            print(f"   → 保存report失败: {str(e)[:50]}")
    
    print("=" * 80)
    
    # 保存汇总
    summary_df = pd.DataFrame(summary_data)
    summary_csv = os.path.join(report_dir, '前10ETF汇总.csv')
    summary_df.to_csv(summary_csv, index=False, encoding='utf-8-sig')
    print(f"\n✓ 汇总报告已保存: {summary_csv}\n")


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("ETF 年化收益率分析系统 - 多策略回测")
    print("=" * 80 + "\n")
    
    # 只分析前20个ETF以节省时间
    # print("策略1：双均线策略 (DualMA)")
    # print("-" * 80)
    # analyze_all_etf_with_strategy(strategy_class=DualMAStrategy, strategy_name="DualMA", limit=20)
    
    print("\n策略2：三日反转策略 (ThreeDayReverse)")
    print("-" * 80)
    analyze_all_etf_with_strategy(strategy_class=ThreeDayReverseStrategy, strategy_name="ThreeDayReverse", limit=None,
                                  report_dir = "/Users/winssion/Desktop/akshare_proj/threeDay_report")
    
    print("\n✓ 所有分析完成！报告已保存到 report 文件夹")
