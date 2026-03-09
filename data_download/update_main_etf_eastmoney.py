import akshare as ak
import pandas as pd
import time
import os

# fund_etf_spot_em_df = ak.fund_etf_spot_em()
# print(fund_etf_spot_em_df)
# 保存至csv
# fund_etf_spot_em_df.to_csv('../data/fund_etf_spot_em_eastmoney.csv', index=False, encoding='utf-8-sig')
# 读取fund_etf_spot_em_eastmoney.csv
fund_etf_spot_em_df = pd.read_csv('../data/fund_etf_spot_em_eastmoney.csv', encoding='utf-8-sig')
nonexistent_code_file = '../data/nonexistent_etf_eastmoney_codes.txt'


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
# 分别读取fund_etf_spot_em_df中的证券代码，并利用ak.stock_zh_a_hist读取其近1年的数据，存入在日线文件夹下
# 创建文件夹
if not os.path.exists('基金_东方财富'):
    os.makedirs('基金_东方财富')
# 使用tqdm查看code进度条
from tqdm import tqdm

today = pd.to_datetime('today').strftime('%Y%m%d')
print(f'today:{today}')

is_complete = False
while(not is_complete):
    is_complete = True
    for i in tqdm(range(len(fund_etf_spot_em_df))):
        # 获取证券代码
        code = str(fund_etf_spot_em_df.iloc[i]['代码']).strip().zfill(6)
        if code in nonexistent_codes:
            continue
        try:
            if os.path.exists(f'../data/基金_东方财富/{code}.csv'):
                # 读取文件，获取最后一行的日期
                # print(f'正在更新 {code} 的数据...')
                try:
                    df = pd.read_csv(f'../data/基金_东方财富/{code}.csv', encoding='utf-8-sig')
                    df = df.sort_values(by='日期', ascending=True)
                except pd.errors.EmptyDataError:
                    start_date = '20230101'
                    stock_zh_a_hist_df = ak.stock_zh_a_hist(
                                            symbol=str(code),
                                            period="daily",
                                            start_date=start_date,
                                            end_date=today,
                                            adjust="qfq")   # 前复权
                    if stock_zh_a_hist_df.empty:
                        print(f'{code} 为空，已跳过')
                        append_nonexistent_code(nonexistent_code_file, code, nonexistent_codes)
                        time.sleep(15)
                        continue
                    stock_zh_a_hist_df.to_csv(f'../data/基金_东方财富/{code}.csv', index=False, encoding='utf-8-sig')
                    time.sleep(15)
                    print(f'{code} 为空，已更新')
                    continue
                last_date = df.iloc[-1]['日期']
                if pd.to_datetime(last_date).date() != pd.to_datetime(today).date():
                    start_date = pd.to_datetime(last_date) + pd.Timedelta(days=1)
                    stock_zh_a_hist_df = ak.stock_zh_a_hist(
                                            symbol=str(code),
                                            period="daily",
                                            start_date=start_date.strftime('%Y%m%d'),
                                            end_date=today,
                                            adjust="qfq")   # 前复权
                    print(f'{code} today:{today}, last_date:{last_date}')
                    if stock_zh_a_hist_df.empty:
                        print(f'{code} 非最新，增量为空，已跳过')
                        time.sleep(15)
                        continue
                    stock_zh_a_hist_df.to_csv(f'../data/基金_东方财富/{code}.csv', mode='a', index=False, encoding='utf-8-sig', header=False)    
                    time.sleep(15)
                    print(f'{code} 非最新，已更新')
                    
            else:
                start_date = '20230101'
                stock_zh_a_hist_df = ak.stock_zh_a_hist(
                                            symbol=str(code),
                                            period="daily",
                                            start_date=start_date,
                                            end_date=today,
                                            adjust="qfq")

                if stock_zh_a_hist_df.empty:
                    print(f'{code} 不存在，已跳过')
                    append_nonexistent_code(nonexistent_code_file, code, nonexistent_codes)
                    time.sleep(15)
                    continue
                stock_zh_a_hist_df.to_csv(f'../data/基金_东方财富/{code}.csv', index=False, encoding='utf-8-sig')
                time.sleep(15)
                
                print(f'{code} 不存在，已更新')
        except Exception as e:
            print(f'{code} 下载失败，已跳过:{e}')
            is_complete = False
            time.sleep(15)
            continue

