import os
import akshare as ak
import pandas as pd


def normalize_stock_code(code) -> str:
    text = str(code).strip()
    if text.endswith('.0') and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit():
        text = text.zfill(6)
    return text


def spot_row_to_daily_row(spot_row: pd.Series, date_str: str, code: str) -> pd.DataFrame:
    return pd.DataFrame([
        {
            '日期': pd.to_datetime(date_str, format='%Y%m%d').strftime('%Y-%m-%d'),
            '股票代码': code,
            '开盘': pd.to_numeric(spot_row.get('今开'), errors='coerce'),
            '收盘': pd.to_numeric(spot_row.get('最新价'), errors='coerce'),
            '最高': pd.to_numeric(spot_row.get('最高'), errors='coerce'),
            '最低': pd.to_numeric(spot_row.get('最低'), errors='coerce'),
            '成交量': pd.to_numeric(spot_row.get('成交量'), errors='coerce'),
            '成交额': pd.to_numeric(spot_row.get('成交额'), errors='coerce'),
            '振幅': pd.to_numeric(spot_row.get('振幅'), errors='coerce'),
            '涨跌幅': pd.to_numeric(spot_row.get('涨跌幅'), errors='coerce'),
            '涨跌额': pd.to_numeric(spot_row.get('涨跌额'), errors='coerce'),
            '换手率': pd.to_numeric(spot_row.get('换手率'), errors='coerce'),
        }
    ])


def upsert_daily_row(csv_path: str, daily_row_df: pd.DataFrame, date_str: str) -> None:
    target_date = pd.to_datetime(date_str, format='%Y%m%d').strftime('%Y-%m-%d')
    if not os.path.exists(csv_path):
        daily_row_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        return

    try:
        old_df = pd.read_csv(csv_path, encoding='utf-8-sig')
    except pd.errors.EmptyDataError:
        old_df = pd.DataFrame()

    if old_df.empty:
        daily_row_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        return

    if '日期' in old_df.columns:
        old_df = old_df[old_df['日期'].astype(str) != target_date]

    merged_df = pd.concat([old_df, daily_row_df], ignore_index=True)
    merged_df.to_csv(csv_path, index=False, encoding='utf-8-sig')


def main() -> None:
    date_str = pd.to_datetime('today').strftime('%Y%m%d')
    data_dir = '../data'
    output_dir = f'{data_dir}/daily_all'
    sh_dir = f'{data_dir}/上证日线'
    sz_dir = f'{data_dir}/深证日线'
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(sh_dir, exist_ok=True)
    os.makedirs(sz_dir, exist_ok=True)

    df = ak.stock_zh_a_spot_em()
    output_file = f'{output_dir}/stock_zh_a_spot_em_{date_str}.csv'
    df.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f'已保存当日全量股票信息: {output_file}')

    updated_sh = 0
    updated_sz = 0
    for _, row in df.iterrows():
        code = normalize_stock_code(row.get('代码'))
        if not code:
            continue

        if code.startswith('6'):
            target_dir = sh_dir
        elif code.startswith(('0', '3')):
            target_dir = sz_dir
        else:
            continue

        csv_path = f'{target_dir}/{code}.csv'
        daily_row_df = spot_row_to_daily_row(row, date_str=date_str, code=code)
        upsert_daily_row(csv_path, daily_row_df, date_str=date_str)

        if code.startswith('6'):
            updated_sh += 1
        else:
            updated_sz += 1

    print(f'当日日线已更新: 上证 {updated_sh} 只, 深证 {updated_sz} 只')


if __name__ == "__main__":
    main()
