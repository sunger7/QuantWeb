import akshare as ak
import mplfinance as mpf  # Please install mplfinance as follows: pip install mplfinance
import pandas as pd
import time
import random
import os
# import requests
# # 设置代理
# proxy = 'http://127.0.0.1:7890' #代理地址
# os.environ['http_proxy'] = proxy
# os.environ['https_proxy'] = proxy
# # 设置代理
# proxies = {
#     "http": 'http://127.0.0.1:7890',
#     "https": 'https://127.0.0.1:7890',
# }
# session = requests.Session()
# session.proxies.update(proxies)
stock_sh_a_spot_em_df = ak.stock_sh_a_spot_em()
# print(stock_sh_a_spot_em_df)
# 保存至csv
stock_sh_a_spot_em_df.to_csv('../data/stock_sh_a_spot_em.csv', index=False, encoding='utf-8-sig')
# 读取stock_sh_a_spot_em.csv
stock_sh_a_spot_em_df = pd.read_csv('../data/stock_sh_a_spot_em.csv', encoding='utf-8-sig')
nonexistent_code_file = '../data/nonexistent_sh_codes.txt'


def load_nonexistent_codes(file_path: str) -> set[str]:
    if not os.path.exists(file_path):
        return set()
    with open(file_path, 'r', encoding='utf-8') as f:
        return {line.strip() for line in f if line.strip()}


def append_nonexistent_code(file_path: str, code: str, code_set: set[str]) -> None:
    if code in code_set:
        return
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(code + '\n')
    code_set.add(code)


nonexistent_codes = load_nonexistent_codes(nonexistent_code_file)
# 分别读取stock_sh_a_spot_em_df中的证券代码，并利用ak.stock_zh_a_hist读取其近1年的数据，存入在日线文件夹下
# 创建文件夹
if not os.path.exists('上证日线'):
    os.makedirs('上证日线')
# 使用tqdm查看code进度条
from tqdm import tqdm

today = '20260302'
print(f'today:{today}')
is_complete = False
while(not is_complete):
    is_complete = True
    for i in tqdm(range(len(stock_sh_a_spot_em_df))):
        # 获取证券代码
        code = str(stock_sh_a_spot_em_df.iloc[i]['代码'])
        code = code.strip()
        code = code.zfill(6)
        if code in nonexistent_codes:
            continue
        try:
            # 读取近1年的数据
            # 查看当前代码的文件是否存在，不存在时才下载近1年，存在时补下载至今日   
            if os.path.exists(f'../data/上证日线/{code}.csv'):
                # 读取文件，获取最后一行的日期
                # print(f'正在更新 {code} 的数据...')
                try:
                    df = pd.read_csv(f'../data/上证日线/{code}.csv', encoding='utf-8-sig')
                    df = df.sort_values(by='日期', ascending=True)
                except pd.errors.EmptyDataError:
                    start_date = '20230101'
                    # 获取今天的日期
                    # today = get_last_trading_day()
                    
                    stock_zh_a_hist_df = ak.stock_zh_a_hist(symbol=str(code), period='daily', start_date='20230101', end_date=today)
                    if stock_zh_a_hist_df.empty:
                        print(f'{code} 为空，已跳过')
                        continue
                    # 保存至csv
                    stock_zh_a_hist_df.to_csv(f'../data/上证日线/{code}.csv', index=False, encoding='utf-8-sig')
                    time.sleep(random.uniform(1, 10))  # 添加延时，避免请求过快被封IP
                    # print(f'{code} update 1')
                    continue
                last_date = df.iloc[-1]['日期']
                # print(today, last_date)
                # 如果最后一行的日期不是今天，则下载近1年数据
                # 比较today和last_date是否相等

                if pd.to_datetime(last_date).date() != pd.to_datetime(today).date():
                    # 计算开始日期,开始日期为最后一行的日期加1天
                    # print(today,last_date)
                    start_date = pd.to_datetime(last_date) + pd.Timedelta(days=1)
                    stock_zh_a_hist_df = ak.stock_zh_a_hist(symbol=str(code), period='daily', \
                                                            start_date=start_date, end_date=today)
                    # 将新数据追加到文件中
                    stock_zh_a_hist_df.to_csv(f'../data/上证日线/{code}.csv', mode='a', index=False, encoding='utf-8-sig', header=False)   
                    # print(f'{code} update 2') 
                    # print(code,stock_zh_a_hist_df)
                    # 读取数据的最后一行的交易日期
                    # try:
                    #     stock_zh_a_hist_df = pd.read_csv(f'../data/上证日线/{code}.csv', encoding='utf-8-sig')
                    #     today = stock_zh_a_hist_df.iloc[len(stock_zh_a_hist_df)-1]['日期']                
                    # except IndexError:
                    #     today = last_date
                    #     continue
                    time.sleep(random.uniform(1, 10))  # 添加延时，避免请求过快被封IP
                    
            else:
                start_date = '20230101'
                stock_zh_a_hist_df = ak.stock_zh_a_hist(symbol=str(code), period='daily', start_date='20230101', end_date=today)
                # 保存至csv
                if stock_zh_a_hist_df.empty or len(stock_zh_a_hist_df) == 0:
                    print(f'{code} 不存在，已跳过')
                    append_nonexistent_code(nonexistent_code_file, code, nonexistent_codes)
                    time.sleep(random.uniform(1, 10))  # 添加延时，避免请求过快被封IP
                    continue
                stock_zh_a_hist_df.to_csv(f'../data/上证日线/{code}.csv', index=False, encoding='utf-8-sig')
                time.sleep(random.uniform(1, 10))  # 添加延时，避免请求过快被封IP
        except Exception as e:
            print(f'{code} 下载失败，已跳过:{e}')
            is_complete = False
            time.sleep(random.uniform(1, 10))  # 添加延时，避免请求过快被封IP
            continue


