import akshare as ak
import pandas as pd
import time
import os
# import requests

# 设置代理
proxy = 'http://127.0.0.1:7890' #代理地址
# os.environ['http_proxy'] = proxy
# os.environ['https_proxy'] = proxy
# # 设置代理
# proxies = {
#     "http": 'http://127.0.0.1:7890',
#     "https": 'https://127.0.0.1:7890',
# }
# session = requests.Session()
# session.proxies.update(proxies)
# fund_etf_spot_em_df = ak.fund_etf_spot_em()
# print(fund_etf_spot_em_df)
# 保存至csv
# fund_etf_spot_em_df.to_csv('../data/fund_etf_spot_em_eastmoney.csv', index=False, encoding='utf-8-sig')
# 读取fund_etf_spot_em_eastmoney.csv
fund_etf_spot_em_df = pd.read_csv('../data/fund_etf_spot_em_eastmoney.csv', encoding='utf-8-sig')
# 分别读取fund_etf_spot_em_df中的证券代码，并利用ak.stock_zh_a_hist读取其近1年的数据，存入在日线文件夹下
# 创建文件夹
import os
if not os.path.exists('基金_东方财富'):
    os.makedirs('基金_东方财富')
# 使用tqdm查看code进度条
from tqdm import tqdm

def get_last_trading_day(target_date=None):
    # 获取交易日历
    trade_cal = ak.tool_trade_date_hist_sina()

    # 明确指定日期列
    trade_dates = pd.to_datetime(trade_cal['trade_date'])

    today = pd.to_datetime('today').normalize()
    last_trade_day = trade_dates[trade_dates <= today].max()
    return last_trade_day
today = get_last_trading_day()
today = pd.to_datetime('today').strftime('%Y%m%d')
print(f'today:{today}')
is_complete = False
while(not is_complete):
    is_complete = True
    for i in tqdm(range(len(fund_etf_spot_em_df))):
        # 获取证券代码
        code = fund_etf_spot_em_df.iloc[i]['代码']
        # 读取近1年的数据
        # 查看当前代码的文件是否存在，不存在时才下载近1年，存在时补下载至今日  
        try:
            if os.path.exists(f'../data/基金_东方财富/{code}.csv'):
                # 读取文件，获取最后一行的日期
                # print(f'正在更新 {code} 的数据...')
                try:
                    df = pd.read_csv(f'../data/基金_东方财富/{code}.csv', encoding='utf-8-sig')
                    df = df.sort_values(by='日期', ascending=True)
                except pd.errors.EmptyDataError:
                    # continue
                    start_date = '20230101'
                    # 获取今天的日期
                    today = get_last_trading_day()
                    stock_zh_a_hist_df = ak.stock_zh_a_hist(
                                            symbol="159312",
                                            period="daily",
                                            start_date="20230101",
                                            end_date=today.strftime('%Y%m%d'),
                                            adjust="qfq")   # 前复权
    # 如果stock_zh_a_hist_df为空，则跳过
                    if stock_zh_a_hist_df.empty or len(stock_zh_a_hist_df) == 0:
                        print(f'{code} 为空，已跳过')
                        continue
                    # 保存至csv
                    stock_zh_a_hist_df.to_csv(f'../data/基金_东方财富/{code}.csv', index=False, encoding='utf-8-sig')
                    time.sleep(0.3)  # 添加延时，避免请求过快被封IP
                    print(f'{code} 为空，已更新')
                    continue
                last_date = df.iloc[-1]['日期']
                # print(today, last_date,code)
                # 如果最后一行的日期不是今天，则下载近1年数据
                # 比较today和last_date是否相等

                if pd.to_datetime(last_date).date() != pd.to_datetime(today).date():
                    # 计算开始日期,开始日期为最后一行的日期加1天
                    start_date = pd.to_datetime(last_date) + pd.Timedelta(days=1)
                    start_date_str = start_date.strftime('%Y%m%d')
                    stock_zh_a_hist_df = ak.stock_zh_a_hist(
                                            symbol=str(code),
                                            period="daily",
                                            start_date=start_date.strftime('%Y%m%d'),
                                            end_date=today.strftime('%Y%m%d'),
                                            adjust="qfq")   # 前复权
                    print(today, last_date,code)
                    if stock_zh_a_hist_df.empty or len(stock_zh_a_hist_df) == 0:
                        print(f'{code} 为空，已跳过')
                        continue
                    # 将新数据追加到文件中
                    stock_zh_a_hist_df.to_csv(f'../data/基金_东方财富/{code}.csv', mode='a', index=False, encoding='utf-8-sig', header=False)    
                    time.sleep(0.3)  # 添加延时，避免请求过快被封IP
                    print(f'{code} 非最新，已更新')
                    
            else:
                start_date = '20230101'
                # 获取今天的日期
                today = get_last_trading_day()
                stock_zh_a_hist_df = ak.stock_zh_a_hist(
                                            symbol=str(code),
                                            period="daily",
                                            start_date="20230101",
                                            end_date=today.strftime('%Y%m%d'),
                                            adjust="qfq")   # 前复权                # stock_zh_a_hist_df = ak.fund_etf_hist_em(symbol='159707', period='daily', start_date='20230101', end_date=today)

                # 如果stock_zh_a_hist_df为空，则跳过
                if stock_zh_a_hist_df.empty or len(stock_zh_a_hist_df) == 0:
                    print(f'{code} 为空，已跳过')
                    continue
                # 保存至csv
                stock_zh_a_hist_df.to_csv(f'../data/基金_东方财富/{code}.csv', index=False, encoding='utf-8-sig')
                time.sleep(0.3)  # 添加延时，避免请求过快被封IP
                
                print(f'{code} 不存在，已更新')
        except Exception as e:
            print(f'{code}:{e}, 下载失败，已跳过')
            is_complete = False
            continue

