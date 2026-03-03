from django.shortcuts import render, redirect
from django.http import JsonResponse, FileResponse, Http404, StreamingHttpResponse
from django.contrib import messages
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import csrf_exempt
import pandas as pd
import os
import json
import subprocess
import sys
import glob
import threading
import uuid
import time as _time
from akquant import Strategy, run_backtest
from .myStrategy import DualMAStrategy, ThreeDayReverseStrategy, RSIStrategy
# ── 常量 ─────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), '../../data')
TRADE_INFO_DIR = os.path.join(os.path.dirname(__file__), '../../trade_info')
REPORT_DIRS = {
    'DualMA': os.path.join(os.path.dirname(__file__), '../../dualma_report'),
    'ThreeDayReverse': os.path.join(os.path.dirname(__file__), '../../threeDay_report'),
    'RSI': os.path.join(os.path.dirname(__file__), '../../rsi_report'),
}
DATA_DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), '../../data_download')
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), '../../data/watchlist.json')
DEFAULT_STRATEGY_FILE = os.path.join(os.path.dirname(__file__), '../../data/stock_default_strategy.json')
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), '../../data/settings.json')

STRATEGIES = [
    {'id': 'DualMA', 'name': '双均线策略', 'description': '使用快线和慢线金叉/死叉进行交易'},
    {'id': 'ThreeDayReverse', 'name': '三日反转策略', 'description': '连续跌三天买入，涨三天卖出'},
    {'id': 'RSI', 'name': 'RSI策略', 'description': 'RSI超卖买入(30以下)，超买卖出(70以上)'},
]

# ── 后台任务管理 ─────────────────────────────────────
_bg_tasks = {}          # {task_id: {status, result, created, description}}
_bg_tasks_lock = threading.Lock()
_TASK_EXPIRE_SECONDS = 600  # 10 分钟后自动清理


def _cleanup_old_tasks():
    """清理过期的任务记录"""
    now = _time.time()
    expired = [tid for tid, t in _bg_tasks.items()
               if now - t['created'] > _TASK_EXPIRE_SECONDS and t['status'] != 'running']
    for tid in expired:
        del _bg_tasks[tid]


def _start_bg_task(func, description='', args=(), kwargs=None):
    """启动后台任务，返回 task_id"""
    if kwargs is None:
        kwargs = {}
    task_id = uuid.uuid4().hex[:10]
    with _bg_tasks_lock:
        _cleanup_old_tasks()
        _bg_tasks[task_id] = {
            'status': 'running',
            'result': None,
            'created': _time.time(),
            'description': description,
        }

    def _run():
        try:
            func(*args, **kwargs)
            with _bg_tasks_lock:
                _bg_tasks[task_id]['status'] = 'done'
        except Exception as e:
            with _bg_tasks_lock:
                _bg_tasks[task_id]['status'] = 'error'
                _bg_tasks[task_id]['result'] = str(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return task_id


@csrf_exempt
def task_status(request):
    """查询后台任务状态 API"""
    task_id = request.GET.get('task_id', '')
    with _bg_tasks_lock:
        task = _bg_tasks.get(task_id)
    if not task:
        return JsonResponse({'status': 'not_found', 'message': '任务不存在或已过期'})
    return JsonResponse({
        'status': task['status'],
        'description': task.get('description', ''),
        'result': task.get('result'),
    })


# ── 工具函数 ─────────────────────────────────────────

def _load_settings():
    """读取全局设置 {max_workers: int, ...}"""
    defaults = {'max_workers': 8, 'commission': 0.00015}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            defaults.update(data)
        except Exception:
            pass
    return defaults


def _save_settings(data):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _count_csv(directory):
    """统计目录下 .csv 文件数量"""
    if not os.path.exists(directory):
        return 0
    return len([f for f in os.listdir(directory) if f.endswith('.csv')])


def _get_last_update(directory):
    """获取目录下最新 CSV 文件的修改时间"""
    if not os.path.exists(directory):
        return None
    csv_files = [os.path.join(directory, f) for f in os.listdir(directory) if f.endswith('.csv')]
    if not csv_files:
        return None
    latest = max(os.path.getmtime(f) for f in csv_files)
    from datetime import datetime
    return datetime.fromtimestamp(latest).strftime('%Y-%m-%d %H:%M')


def _load_nonexistent_codes(file_path):
    if not os.path.exists(file_path):
        return set()
    with open(file_path, 'r', encoding='utf-8') as f:
        return {line.strip() for line in f if line.strip()}


def _append_nonexistent_code(file_path, code, code_set):
    if code in code_set:
        return
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(code + '\n')
    code_set.add(code)


def _normalize_stock_code(code):
    text = str(code).strip()
    if text.endswith('.0') and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit():
        text = text.zfill(6)
    return text


def _spot_row_to_daily_row(spot_row, today_str, code):
    return pd.DataFrame([
        {
            '日期': pd.to_datetime(today_str, format='%Y%m%d').strftime('%Y-%m-%d'),
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


def _upsert_daily_row(csv_path, daily_row_df, today_str):
    target_date = pd.to_datetime(today_str, format='%Y%m%d').strftime('%Y-%m-%d')
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


def _is_prev_trade_day(last_date_str, today_str, trade_day_to_index):
    try:
        last_date = pd.to_datetime(last_date_str).date()
        today_date = pd.to_datetime(today_str, format='%Y%m%d').date()
    except Exception:
        return False

    last_idx = trade_day_to_index.get(last_date)
    today_idx = trade_day_to_index.get(today_date)
    if last_idx is None or today_idx is None:
        return False
    return today_idx - last_idx == 1


def load_name_mapping():
    """从三个映射CSV中加载 代码→名称"""
    name_mapping = {}
    mapping_files = [
        os.path.join(DATA_DIR, 'fund_etf_spot_em_eastmoney.csv'),
        os.path.join(DATA_DIR, 'stock_sh_a_spot_em.csv'),
        os.path.join(DATA_DIR, 'stock_sz_a_spot_em.csv'),
    ]
    for fpath in mapping_files:
        if not os.path.exists(fpath):
            continue
        try:
            df = pd.read_csv(fpath)
            if '代码' in df.columns and '名称' in df.columns:
                for _, row in df.iterrows():
                    name_mapping[str(row['代码'])] = row['名称']
        except Exception as e:
            print(f"Error loading {fpath}: {e}")
    return name_mapping


def get_analysis_data(data_type, start_date='', end_date='',strategy_id='DualMA', 
                      strategy_params={"fast_window": 10, "slow_window": 30},
                      force_recalc=False,max_workers=8):
    """遍历板块目录下所有CSV，计算收益率并排序返回（多线程回测）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    data_dir = os.path.join(DATA_DIR, data_type)
    if not os.path.exists(data_dir):
        return []

    name_mapping = load_name_mapping()
    
    # 根据 strategy_id 动态获取策略类
    strategy_class = None
    try:
        strategy_class = globals()[f"{strategy_id}Strategy"]
    except KeyError:
        strategy_class = DualMAStrategy  # 默认策略
    if strategy_id != 'DualMA':
        strategy_params = {}  # 其他策略不需要参数

    # 确保报告目录存在（主线程创建一次）
    os.makedirs(REPORT_DIRS[strategy_id], exist_ok=True)

    settings = _load_settings()
    commission = settings.get('commission', 0.00015)
    def _process_single_stock(fname):
        """处理单只股票的读取 + 回测 + 保存，返回结果 dict 或 None"""
        code = fname.replace('.csv', '')
        try:
            df = pd.read_csv(os.path.join(data_dir, fname))
        except pd.errors.EmptyDataError:
            return None

        if len(df) < 2:
            return None
        df['date'] = pd.to_datetime(df.iloc[:, 0])
        if start_date:
            df = df[df['date'] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df['date'] <= pd.to_datetime(end_date)]
        if len(df) < 2:
            return None
        sp = df.iloc[0, 4]
        ep = df.iloc[-1, 4]
        ret = (ep - sp) / sp * 100

        backtest_json_path = os.path.join(REPORT_DIRS[strategy_id], f'{code}_backtest.json')
        trades_json_path = os.path.join(REPORT_DIRS[strategy_id], f'{code}_trades.json')
        orders_json_path = os.path.join(REPORT_DIRS[strategy_id], f'{code}_orders.json')
        html_report_path = os.path.join(REPORT_DIRS[strategy_id], f'{code}_report.html')

        need_calc = force_recalc or not os.path.exists(backtest_json_path) or \
                    not os.path.exists(trades_json_path) or not os.path.exists(orders_json_path) or \
                    not os.path.exists(html_report_path)
        # 强制重算时，如果 backtest 文件是今天生成的则跳过
        if need_calc and force_recalc and os.path.exists(backtest_json_path):
            from datetime import date as _d
            file_mdate = _d.fromtimestamp(os.path.getmtime(backtest_json_path))
            if file_mdate == _d.today():
                need_calc = False
        if need_calc:
            backtest_result = run_backtest(
                data=df,
                t_plus_one=False,
                strategy=strategy_class,
                strategy_params=strategy_params,
                cash=100_000.0,
                commission_rate=commission,
                show_progress=False
            )
            metrics = getattr(backtest_result, 'metrics', None)
            if metrics and hasattr(metrics, 'total_return_pct'):
                metrics_dict = {'total_return_pct': metrics.total_return_pct}
                with open(backtest_json_path, 'w', encoding='utf-8') as f:
                    json.dump(metrics_dict, f, ensure_ascii=False, indent=2)
            trades_df = getattr(backtest_result, 'trades_df', None)
            if trades_df is not None and not trades_df.empty:
                trades_df.to_json(trades_json_path, orient='records', force_ascii=False, date_format='iso', indent=2)
            orders_df = getattr(backtest_result, 'orders_df', None)
            if orders_df is not None and not orders_df.empty:
                orders_df.to_json(orders_json_path, orient='records', force_ascii=False, date_format='iso', indent=2)
            report_func = getattr(backtest_result, 'report', None)
            if callable(report_func):
                report_func(title=f"{code} 策略回测报告", filename=html_report_path, show=False)

        # 从 backtest_json 读取 total_return_pct
        total_return_pct = round(ret, 2)
        if os.path.exists(backtest_json_path):
            try:
                with open(backtest_json_path, 'r', encoding='utf-8') as f:
                    metrics_data = json.load(f)
                if 'total_return_pct' in metrics_data:
                    total_return_pct = round(metrics_data['total_return_pct'], 2)
            except Exception:
                pass

        return {
            'code': code,
            'name': name_mapping.get(code, 'N/A'),
            'total_return_pct': total_return_pct,
            'start_price': round(sp, 2),
            'end_price': round(ep, 2),
            'data_type': data_type,
            'backtest_json': backtest_json_path if os.path.exists(backtest_json_path) else None,
            'trades_json': trades_json_path if os.path.exists(trades_json_path) else None,
            'orders_json': orders_json_path if os.path.exists(orders_json_path) else None,
            'html_report': html_report_path if os.path.exists(html_report_path) else None,
        }

    # 收集所有 CSV 文件名
    csv_files = [f for f in os.listdir(data_dir) if f.endswith('.csv')]

    # 多线程并发回测
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_process_single_stock, fname): fname for fname in csv_files}
        for future in as_completed(future_map):
            try:
                result = future.result()
                if result is not None:
                    results.append(result)
            except Exception:
                pass

    results.sort(key=lambda x: x['total_return_pct'], reverse=True)
    return results


# ── 视图函数 ─────────────────────────────────────────

def _load_existing_results(strategy_id):
    """只读取已有的报告文件，不做任何计算"""
    report_dir = REPORT_DIRS.get(strategy_id, '')
    if not report_dir or not os.path.exists(report_dir):
        return {'上证日线': [], '深证日线': [], '基金_东方财富': []}

    name_mapping = load_name_mapping()
    # 根据代码判断属于哪个板块
    dir_codes = {}
    for data_type in ['上证日线', '深证日线', '基金_东方财富']:
        d = os.path.join(DATA_DIR, data_type)
        if os.path.exists(d):
            for f in os.listdir(d):
                if f.endswith('.csv'):
                    dir_codes[f.replace('.csv', '')] = data_type

    # 扫描 backtest JSON 文件
    results = {'上证日线': [], '深证日线': [], '基金_东方财富': []}
    for fname in os.listdir(report_dir):
        if not fname.endswith('_backtest.json'):
            continue
        code = fname.replace('_backtest.json', '')
        data_type = dir_codes.get(code)
        if not data_type:
            continue

        backtest_json_path = os.path.join(report_dir, fname)
        try:
            with open(backtest_json_path, 'r', encoding='utf-8') as f:
                metrics_data = json.load(f)
            total_return_pct = round(metrics_data.get('total_return_pct', 0), 2)
        except Exception:
            total_return_pct = 0

        # 读取起始价/结束价（从 CSV）
        csv_path = os.path.join(DATA_DIR, data_type, f'{code}.csv')
        sp, ep = 0, 0
        if os.path.exists(csv_path):
            try:
                df = pd.read_csv(csv_path)
                if len(df) >= 2:
                    sp = round(df.iloc[0, 4], 2)
                    ep = round(df.iloc[-1, 4], 2)
            except Exception:
                pass

        results[data_type].append({
            'code': code,
            'name': name_mapping.get(code, 'N/A'),
            'total_return_pct': total_return_pct,
            'start_price': sp,
            'end_price': ep,
            'data_type': data_type,
        })

    for dt in results:
        results[dt].sort(key=lambda x: x['total_return_pct'], reverse=True)
    return results

def index(request):
    """首页：统计卡片 + 策略菜单 + 自选股"""
    sh_dir = os.path.join(DATA_DIR, '上证日线')
    sz_dir = os.path.join(DATA_DIR, '深证日线')
    etf_dir = os.path.join(DATA_DIR, '基金_东方财富')

    # 自选股数据（每只股票有自己的默认策略）
    fallback_strategy_id = STRATEGIES[0]['id'] if STRATEGIES else 'DualMA'
    default_strategies = _load_default_strategies()  # {code: strategy_id}
    strategy_name_map = {s['id']: s['name'] for s in STRATEGIES}
    wl = _load_watchlist()
    wl_codes = list(dict.fromkeys(w['code'] for w in wl))
    name_mapping = load_name_mapping()
    watchlist_items = []
    from datetime import datetime, date
    for code in wl_codes:
        stock_default_sid = default_strategies.get(code, fallback_strategy_id)
        item = {
            'code': code,
            'name': name_mapping.get(code, 'N/A'),
            'default_strategy_id': stock_default_sid,
            'default_strategy_name': strategy_name_map.get(stock_default_sid, stock_default_sid),
        }
        # 读取该股票默认策略的收益
        rd = REPORT_DIRS.get(stock_default_sid, '')
        ret_pct = None
        if rd:
            bp = os.path.join(rd, f'{code}_backtest.json')
            if os.path.exists(bp):
                try:
                    with open(bp, 'r', encoding='utf-8') as f:
                        m = json.load(f)
                    ret_pct = round(m.get('total_return_pct', 0), 2)
                except Exception:
                    pass
        item['total_return_pct'] = ret_pct

        # 读取 orders.json，提取今日或最近一次交易信号
        signal = None
        signal_date = None
        orders_path = os.path.join(rd, f'{code}_orders.json')
        today_str = date.today().strftime('%Y-%m-%d')
        if os.path.exists(orders_path):
            try:
                with open(orders_path, 'r', encoding='utf-8') as f:
                    orders = json.load(f)
                # 按 created_at 排序，找最新的
                orders_sorted = sorted(orders, key=lambda x: x.get('created_at', ''), reverse=True)
                for order in orders_sorted:
                    # 只考虑已成交的订单
                    if order.get('status') != 'filled':
                        continue
                    order_date = order.get('created_at', '')[:10]
                    if order_date == today_str:
                        signal = order.get('side')
                        signal_date = order_date
                        break
                if not signal and orders_sorted:
                    # 没有今日信号，取最近一次
                    for order in orders_sorted:
                        if order.get('status') != 'filled':
                            continue
                        signal = order.get('side')
                        signal_date = order.get('created_at', '')[:10]
                        break
            except Exception:
                pass
        # 没有任何信号时，显示“无操作”
        if signal:
            if signal == 'buy':
                signal_text = f'买入 ({signal_date})'
            elif signal == 'sell':
                signal_text = f'卖出 ({signal_date})'
            else:
                signal_text = f'持有 ({signal_date})'
        else:
            signal_text = '无操作'
        item['trade_signal'] = signal_text
        watchlist_items.append(item)

    return render(request, 'index.html', {
        'strategies': STRATEGIES,
        'sh_count': _count_csv(sh_dir),
        'sz_count': _count_csv(sz_dir),
        'etf_count': _count_csv(etf_dir),
        'sh_update': _get_last_update(sh_dir) or '未更新',
        'sz_update': _get_last_update(sz_dir) or '未更新',
        'etf_update': _get_last_update(etf_dir) or '未更新',
        'watchlist_items': watchlist_items,
    })


def settings_view(request):
    """设置页：添加策略 / 更新数据 / 调整参数"""
    settings = _load_settings()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_strategy':
            messages.success(request, '策略已添加')
        elif action == 'update_data':
            data_type = request.POST.get('data_type')
            try:
                # 消费生成器直到完成
                result = None
                for event in _update_stock_data_stream(data_type):
                    result = event
                if result and result.get('status') == 'ok':
                    messages.success(request, result['message'])
                elif result and result.get('status') == 'error':
                    messages.error(request, result['message'])
                else:
                    messages.success(request, f'{data_type} 数据已更新')
            except Exception as e:
                messages.error(request, f'数据更新失败：{e}')
        elif action == 'save_settings':
            try:
                mw = int(request.POST.get('max_workers', 8))
                mw = max(1, min(mw, 64))  # 限制 1~64
                settings['max_workers'] = mw
                commission = float(request.POST.get('commission', settings.get('commission', 0.00015)))
                commission = max(0, commission)
                settings['commission'] = commission
                _save_settings(settings)
                messages.success(request, f'设置已保存（并发线程数：{mw}，佣金率：{commission}）')
            except (ValueError, TypeError):
                messages.error(request, '线程数和佣金率必须为数字')
    commission = settings.get('commission', 0.00015)
    return render(request, 'settings.html', {'settings': settings, 'commission': commission})


def strategy_analysis(request, strategy_id):
    """策略分析页：只加载已有结果（计算交给后台任务）"""
    from datetime import date
    start_date = request.GET.get('start_date', '2023-01-01')
    end_date = request.GET.get('end_date', date.today().strftime('%Y-%m-%d'))

    # 只读取已有结果，不做计算
    analysis_data = _load_existing_results(strategy_id)

    # 找到策略名称
    strategy_name = strategy_id
    for s in STRATEGIES:
        if s['id'] == strategy_id:
            strategy_name = s['name']
            break

    # 加载当前策略的自选股代码集合
    wl = _load_watchlist()
    watchlist_codes = {w['code'] for w in wl if w['strategy_id'] == strategy_id}

    return render(request, 'strategy_analysis.html', {
        'strategy_id': strategy_id,
        'strategy_name': strategy_name,
        'analysis_data': analysis_data,
        'start_date': start_date,
        'end_date': end_date,
        'watchlist_codes': watchlist_codes,
    })


def _update_single_stock_data(stock_code):
    """增量更新单只股票的K线数据，返回 {status, message}"""
    import akshare as ak

    # 确定股票所在目录和adjust参数
    dir_config = [
        (os.path.join(DATA_DIR, '基金_东方财富'), 'qfq'),
        (os.path.join(DATA_DIR, '上证日线'), ''),
        (os.path.join(DATA_DIR, '深证日线'), ''),
    ]
    data_dir = None
    adjust = ''
    for d, adj in dir_config:
        if os.path.exists(os.path.join(d, f'{stock_code}.csv')):
            data_dir = d
            adjust = adj
            break

    if not data_dir:
        # 尝试从名单CSV判断该股票属于哪个市场
        list_configs = [
            (os.path.join(DATA_DIR, 'fund_etf_spot_em_eastmoney.csv'), os.path.join(DATA_DIR, '基金_东方财富'), 'qfq'),
            (os.path.join(DATA_DIR, 'stock_sh_a_spot_em.csv'), os.path.join(DATA_DIR, '上证日线'), ''),
            (os.path.join(DATA_DIR, 'stock_sz_a_spot_em.csv'), os.path.join(DATA_DIR, '深证日线'), ''),
        ]
        for list_csv, d, adj in list_configs:
            if os.path.exists(list_csv):
                try:
                    df_list = pd.read_csv(list_csv, encoding='utf-8-sig')
                    if str(stock_code) in df_list['代码'].astype(str).values:
                        data_dir = d
                        adjust = adj
                        break
                except Exception:
                    pass

    if not data_dir:
        return {'status': 'error', 'message': f'无法确定股票 {stock_code} 所在市场'}

    os.makedirs(data_dir, exist_ok=True)
    csv_path = os.path.join(data_dir, f'{stock_code}.csv')
    today_str = pd.to_datetime('today').strftime('%Y%m%d')
    start_default = '20230101'

    try:
        # 判断是追加还是新建
        append = False
        fetch_start = start_default
        if os.path.exists(csv_path):
            try:
                df = pd.read_csv(csv_path, encoding='utf-8-sig')
            except pd.errors.EmptyDataError:
                df = pd.DataFrame()

            if not df.empty and '日期' in df.columns:
                df = df.sort_values(by='日期', ascending=True)
                last_date = pd.to_datetime(df.iloc[-1]['日期'])
                if last_date.strftime('%Y%m%d') >= today_str:
                    return {'status': 'ok', 'message': f'{stock_code} 数据已是最新（{last_date.strftime("%Y-%m-%d")}）'}
                fetch_start = (last_date + pd.Timedelta(days=1)).strftime('%Y%m%d')
                append = True
            else:
                # 文件存在但为空或无日期列，也视为追加（保留文件头）
                fetch_start = start_default
                append = True

        kwargs = dict(symbol=stock_code, period='daily',
                      start_date=fetch_start, end_date=today_str)
        if adjust:
            kwargs['adjust'] = adjust
        new_df = ak.stock_zh_a_hist(**kwargs)

        if new_df is None or new_df.empty:
            return {'status': 'ok', 'message': f'{stock_code} 无新数据可更新'}

        if append:
            new_df.to_csv(csv_path, mode='a', index=False,
                          encoding='utf-8-sig', header=False)
        else:
            new_df.to_csv(csv_path, index=False, encoding='utf-8-sig')

        new_count = len(new_df)
        return {'status': 'ok', 'message': f'{stock_code} 更新成功，新增 {new_count} 条数据'}
    except Exception as e:
        return {'status': 'error', 'message': f'{stock_code} 更新失败：{e}'}


@csrf_exempt
def update_single_stock(request):
    """单只股票数据更新 API (AJAX POST)"""
    if request.method == 'POST':
        body = json.loads(request.body)
        stock_code = str(body.get('code', ''))
        if not stock_code:
            return JsonResponse({'status': 'error', 'message': '缺少股票代码'}, status=400)
        result = _update_single_stock_data(stock_code)
        return JsonResponse(result)
    return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)


def _recalc_single_stock(strategy_id, stock_code, kline_path, start_date='', end_date=''):
    """重新计算单只股票的策略回测结果"""
    report_dir = REPORT_DIRS.get(strategy_id, '')
    if not report_dir:
        return
    os.makedirs(report_dir, exist_ok=True)

    # 每次都强制重算，移除日期跳过逻辑

    # 删除该股票旧的报告文件
    for suffix in ['_backtest.json', '_trades.json', '_orders.json', '_report.html']:
        old_file = os.path.join(report_dir, f'{stock_code}{suffix}')
        if os.path.exists(old_file):
            os.remove(old_file)

    # 读取K线数据
    df = pd.read_csv(kline_path)
    if len(df) < 2:
        return
    df['date'] = pd.to_datetime(df.iloc[:, 0])
    # 按日期筛选
    if start_date:
        df = df[df['date'] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df['date'] <= pd.to_datetime(end_date)]
    if len(df) < 2:
        return

    # 动态获取策略类
    strategy_class = None
    try:
        strategy_class = globals()[f"{strategy_id}Strategy"]
    except KeyError:
        strategy_class = DualMAStrategy
    strategy_params = {"fast_window": 10, "slow_window": 30} if strategy_id == 'DualMA' else {}

    settings = _load_settings()
    commission = settings.get('commission', 0.00015)
    backtest_result = run_backtest(
        data=df,
        t_plus_one=False,
        strategy=strategy_class,
        strategy_params=strategy_params,
        cash=100_000.0,
        commission_rate=commission,
        show_progress=False
    )

    # 保存 metrics
    backtest_json_path = os.path.join(report_dir, f'{stock_code}_backtest.json')
    metrics = getattr(backtest_result, 'metrics', None)
    if metrics and hasattr(metrics, 'total_return_pct'):
        with open(backtest_json_path, 'w', encoding='utf-8') as f:
            json.dump({'total_return_pct': metrics.total_return_pct}, f, ensure_ascii=False, indent=2)

    # 保存 trades_df
    trades_json_path = os.path.join(report_dir, f'{stock_code}_trades.json')
    trades_df = getattr(backtest_result, 'trades_df', None)
    if trades_df is not None and not trades_df.empty:
        trades_df.to_json(trades_json_path, orient='records', force_ascii=False, date_format='iso', indent=2)

    # 保存 orders_df
    orders_json_path = os.path.join(report_dir, f'{stock_code}_orders.json')
    orders_df = getattr(backtest_result, 'orders_df', None)
    if orders_df is not None and not orders_df.empty:
        orders_df.to_json(orders_json_path, orient='records', force_ascii=False, date_format='iso', indent=2)

    # 保存 HTML 报告
    html_report_path = os.path.join(report_dir, f'{stock_code}_report.html')
    report_func = getattr(backtest_result, 'report', None)
    if callable(report_func):
        report_func(title=f"{stock_code} 策略回测报告", filename=html_report_path, show=False)


# ── 后台任务启动 API ─────────────────────────────────

@csrf_exempt
def start_analysis_task(request, strategy_id):
    """启动策略分析的后台计算任务，返回 task_id"""
    start_date = request.GET.get('start_date', '2023-01-01')
    end_date = request.GET.get('end_date', '')
    force_recalc = request.GET.get('force_recalc', '') == '1'

    if not end_date:
        from datetime import date
        end_date = date.today().strftime('%Y-%m-%d')

    def _do_analysis():
        # 不再整体删除报告目录，由 get_analysis_data 内部按单只股票判断是否跳过今天已算的
        from concurrent.futures import ThreadPoolExecutor
        settings = _load_settings()
        mw = settings.get('max_workers', 8)
        data_types = ['上证日线', '深证日线', '基金_东方财富']
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                dt: executor.submit(get_analysis_data, dt, start_date, end_date,
                                    strategy_id, force_recalc=force_recalc, max_workers=mw)
                for dt in data_types
            }
            for dt in data_types:
                futures[dt].result()  # 等待完成

    strategy_name = strategy_id
    for s in STRATEGIES:
        if s['id'] == strategy_id:
            strategy_name = s['name']
            break

    desc = f'{strategy_name} 策略分析计算'
    if force_recalc:
        desc += '（强制重算）'
    task_id = _start_bg_task(_do_analysis, description=desc)
    return JsonResponse({'task_id': task_id, 'status': 'running'})


@csrf_exempt
def start_recalc_task(request, strategy_id, stock_code):
    """启动单只股票策略重算的后台任务，返回 task_id"""
    recalc_strategy = request.GET.get('recalc_strategy', '')
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')

    # 找 K 线文件
    kline_directories = [
        os.path.join(DATA_DIR, '基金_东方财富'),
        os.path.join(DATA_DIR, '上证日线'),
        os.path.join(DATA_DIR, '深证日线'),
    ]
    kline_path = None
    for d in kline_directories:
        p = os.path.join(d, f'{stock_code}.csv')
        if os.path.exists(p):
            kline_path = p
            break
    if not kline_path:
        return JsonResponse({'status': 'error', 'message': f'找不到 {stock_code} 数据'})

    def _do_recalc():
        if recalc_strategy:
            # 只重算指定策略
            if recalc_strategy in REPORT_DIRS:
                _recalc_single_stock(recalc_strategy, stock_code, kline_path, start_date, end_date)
        else:
            # 重算所有策略
            for s in STRATEGIES:
                _recalc_single_stock(s['id'], stock_code, kline_path, start_date, end_date)

    name_mapping = load_name_mapping()
    stock_name = name_mapping.get(stock_code, stock_code)
    desc = f'{stock_name}({stock_code}) 策略重算'
    task_id = _start_bg_task(_do_recalc, description=desc)
    return JsonResponse({'task_id': task_id, 'status': 'running'})


def strategy_detail(request, strategy_id, stock_code):
    """股票详情页：K线图 + 买卖点 + 回测报告"""
    kline_directories = [
        os.path.join(DATA_DIR, '基金_东方财富'),
        os.path.join(DATA_DIR, '上证日线'),
        os.path.join(DATA_DIR, '深证日线'),
    ]

    kline_path = None
    for d in kline_directories:
        p = os.path.join(d, f'{stock_code}.csv')
        if os.path.exists(p):
            kline_path = p
            break

    if not kline_path:
        return render(request, 'error.html', {'message': f'找不到股票 {stock_code} 的K线数据'})

    # 不再同步处理强制重算——改用 start_recalc_task 异步 API

    kline_data = pd.read_csv(kline_path)
    if '日期' in kline_data.columns:
        kline_data.columns = [
            "date", "code","open", "close", "high", "low",
            "volume", "amount", "amplitude", "pct_chg", "chg", "turnover"
        ]
    else:
        kline_data.columns = [
            "date", "code", "open", "close", "high", "low",
            "volume", "amount", "amplitude", "pct_chg", "chg", "turnover"
        ]
        if 'code' in kline_data.columns:
            kline_data = kline_data.drop('code', axis=1)

    # 交易信息：从报告目录的 trades JSON 加载
    report_dir = REPORT_DIRS.get(strategy_id, '')
    orders_json_path = os.path.join(report_dir, f'{stock_code}_orders.json') if report_dir else ''
    if orders_json_path and os.path.exists(orders_json_path):
        with open(orders_json_path, 'r', encoding='utf-8') as f:
            trade_json = f.read()
    else:
        trade_json = json.dumps([], ensure_ascii=False)

    kline_json = json.dumps(kline_data.to_dict(orient='records'), ensure_ascii=False)

    # 获取股票名称
    name_mapping = load_name_mapping()
    stock_name = name_mapping.get(stock_code, stock_code)

    # 找策略名称
    strategy_name = strategy_id
    for s in STRATEGIES:
        if s['id'] == strategy_id:
            strategy_name = s['name']
            break

    # 查找回测报告文件
    report_url = None
    report_path = _find_report_file(strategy_id, stock_code)
    if report_path:
        report_url = f'/report/{strategy_id}/{stock_code}/'

    # 各策略对比数据
    strategy_comparison = []
    for s in STRATEGIES:
        sid = s['id']
        rd = REPORT_DIRS.get(sid, '')
        if not rd:
            continue
        backtest_path = os.path.join(rd, f'{stock_code}_backtest.json')
        trades_path = os.path.join(rd, f'{stock_code}_trades.json')
        orders_path = os.path.join(rd, f'{stock_code}_orders.json')
        comp = {
            'id': sid,
            'name': s['name'],
            'total_return_pct': None,
            'trade_count': 0,
            'win_count': 0,
            'loss_count': 0,
            'win_rate': None,
            'total_pnl': None,
        }
        # 读取收益率
        if os.path.exists(backtest_path):
            try:
                with open(backtest_path, 'r', encoding='utf-8') as f:
                    m = json.load(f)
                comp['total_return_pct'] = round(m.get('total_return_pct', 0), 2)
            except Exception:
                pass
        # 从 orders.json 读取交易次数
        if os.path.exists(orders_path):
            try:
                with open(orders_path, 'r', encoding='utf-8') as f:
                    orders_list = json.load(f)
                comp['trade_count'] = len(orders_list)
            except Exception:
                pass
        # 读取交易明细统计（盈亏）
        if os.path.exists(trades_path):
            try:
                with open(trades_path, 'r', encoding='utf-8') as f:
                    trades_list = json.load(f)
                wins = [t for t in trades_list if t.get('pnl', 0) > 0]
                losses = [t for t in trades_list if t.get('pnl', 0) <= 0]
                comp['win_count'] = len(wins)
                comp['loss_count'] = len(losses)
                total_rounds = len(trades_list)
                comp['win_rate'] = round(len(wins) / total_rounds * 100, 1) if total_rounds else None
                comp['total_pnl'] = round(sum(t.get('pnl', 0) for t in trades_list), 2)
            except Exception:
                pass
        strategy_comparison.append(comp)
    strategy_comparison_json = json.dumps(strategy_comparison, ensure_ascii=False)

    # 自选股列表（当前策略下的自选）
    wl = _load_watchlist()
    wl_codes = [w['code'] for w in wl if w['strategy_id'] == strategy_id]
    watchlist_items = []
    for wcode in wl_codes:
        item = {'code': wcode, 'name': name_mapping.get(wcode, 'N/A'), 'strategies': {}}
        for s in STRATEGIES:
            sid = s['id']
            rd = REPORT_DIRS.get(sid, '')
            ret_pct = None
            if rd:
                bp = os.path.join(rd, f'{wcode}_backtest.json')
                if os.path.exists(bp):
                    try:
                        with open(bp, 'r', encoding='utf-8') as f:
                            m = json.load(f)
                        ret_pct = round(m.get('total_return_pct', 0), 2)
                    except Exception:
                        pass
            item['strategies'][sid] = ret_pct
        # 当前策略的收益
        item['total_return_pct'] = item['strategies'].get(strategy_id)
        watchlist_items.append(item)
    watchlist_items_json = json.dumps(watchlist_items, ensure_ascii=False)

    # 当前股票的默认策略
    default_strategies = _load_default_strategies()
    fallback_sid = STRATEGIES[0]['id'] if STRATEGIES else 'DualMA'
    stock_default_strategy = default_strategies.get(stock_code, fallback_sid)

    from datetime import date as _date
    return render(request, 'strategy_detail.html', {
        'strategy_id': strategy_id,
        'strategy_name': strategy_name,
        'stock_code': stock_code,
        'stock_name': stock_name,
        'kline_data': kline_json,
        'trade_data': trade_json,
        'report_url': report_url,
        'strategy_comparison': strategy_comparison,
        'strategy_comparison_json': strategy_comparison_json,
        'watchlist_items': watchlist_items,
        'watchlist_items_json': watchlist_items_json,
        'strategies': STRATEGIES,
        'strategies_json': json.dumps([{'id': s['id'], 'name': s['name']} for s in STRATEGIES], ensure_ascii=False),
        'stock_default_strategy': stock_default_strategy,
        'today': _date.today().strftime('%Y-%m-%d'),
    })


def stock_select(request):
    """股票选择页：按板块列出所有股票"""
    name_mapping = load_name_mapping()

    def _list(subdir):
        d = os.path.join(DATA_DIR, subdir)
        if not os.path.exists(d):
            return []
        codes = sorted([f.replace('.csv', '') for f in os.listdir(d) if f.endswith('.csv')])
        return [{'code': c, 'name': name_mapping.get(c, 'N/A')} for c in codes]

    stocks = {
        '上证日线': _list('上证日线'),
        '深证日线': _list('深证日线'),
        '基金_东方财富': _list('基金_东方财富'),
    }
    return render(request, 'stock_select.html', {
        'stocks': stocks,
        'strategies': STRATEGIES,
    })


# ── 报告服务 ─────────────────────────────────────────

def _find_report_file(strategy_id, stock_code):
    """在对应的报告目录中查找匹配的HTML报告文件"""
    report_dir = REPORT_DIRS.get(strategy_id, '')
    
    if not report_dir or not os.path.exists(report_dir):
        return None
    # 搜索包含 stock_code 的 html 文件
    for f in os.listdir(report_dir):
        if f.endswith('.html') and stock_code in f:
            return os.path.join(report_dir, f)
    return None


@xframe_options_sameorigin
def serve_report(request, strategy_id, stock_code):
    """提供回测报告HTML文件"""
    report_path = _find_report_file(strategy_id, stock_code)
    if not report_path or not os.path.exists(report_path):
        raise Http404('报告文件不存在')
    return FileResponse(open(report_path, 'rb'), content_type='text/html')


# ── 数据更新 ─────────────────────────────────────────
_update_stop_flags = {}  # {data_type: True/False}
_update_stop_lock = threading.Lock()


@csrf_exempt
def stop_update(request):
    """停止正在进行的数据更新"""
    if request.method == 'POST':
        body = json.loads(request.body)
        data_type = body.get('data_type', '')
        with _update_stop_lock:
            _update_stop_flags[data_type] = True
        return JsonResponse({'status': 'ok', 'message': f'{data_type} 停止信号已发送'})
    return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)


def _update_stock_data_stream(data_type, today_str=None):
    """增量更新数据（生成器）：逐只 yield 进度事件，只下载缺失的日线数据"""
    import akshare as ak
    import time as _time

    # 重置停止标志
    with _update_stop_lock:
        _update_stop_flags[data_type] = False

    config = {
        '上证A股': {
            'list_csv': os.path.join(DATA_DIR, 'stock_sh_a_spot_em.csv'),
            'data_dir': os.path.join(DATA_DIR, '上证日线'),
            'adjust': '',
            'nonexistent_file': os.path.join(DATA_DIR, 'nonexistent_sh_codes.txt'),
        },
        '深证A股': {
            'list_csv': os.path.join(DATA_DIR, 'stock_sz_a_spot_em.csv'),
            'data_dir': os.path.join(DATA_DIR, '深证日线'),
            'adjust': '',
            'nonexistent_file': os.path.join(DATA_DIR, 'nonexistent_sz_codes.txt'),
        },
        '基金ETF': {
            'list_csv': os.path.join(DATA_DIR, 'fund_etf_spot_em_eastmoney.csv'),
            'data_dir': os.path.join(DATA_DIR, '基金_东方财富'),
            'adjust': 'qfq',
            'nonexistent_file': os.path.join(DATA_DIR, 'nonexistent_etf_codes.txt'),
        },
    }
    cfg = config.get(data_type)
    if not cfg:
        yield {'status': 'error', 'message': f'未知数据类型: {data_type}'}
        return

    list_csv = cfg['list_csv']
    data_dir = cfg['data_dir']
    adjust = cfg['adjust']
    nonexistent_file = cfg['nonexistent_file']
    nonexistent_codes = _load_nonexistent_codes(nonexistent_file)

    os.makedirs(data_dir, exist_ok=True)
    stock_list = pd.read_csv(list_csv, encoding='utf-8-sig')
    total = len(stock_list)
    if not today_str:
        today_str = pd.to_datetime('today').strftime('%Y%m%d')
    start_default = '20230101'

    updated = 0
    skipped = 0
    errors = 0
    last_yielded_pct = -10  # 上次推送的百分比档位

    for idx, (_, row) in enumerate(stock_list.iterrows()):
        # 检查停止标志
        with _update_stop_lock:
            if _update_stop_flags.get(data_type, False):
                yield {
                    'status': 'ok',
                    'progress': round((idx) / total * 100, 1) if total else 100,
                    'message': f'{data_type} 已停止更新（已处理 {idx}/{total}，更新 {updated} 只，跳过 {skipped} 只）',
                    'count': _count_csv(data_dir),
                    'last_update': _get_last_update(data_dir) or '未知',
                    'stopped': True,
                }
                return

        current = idx + 1
        progress = round(current / total * 100, 1) if total else 100

        code = _normalize_stock_code(row['代码'])
        if not code:
            skipped += 1
            continue
        if code in nonexistent_codes:
            skipped += 1
            if progress - last_yielded_pct >= 10:
                last_yielded_pct = int(progress // 10) * 10
                yield {'progress': progress, 'current': current, 'total': total,
                       'updated': updated, 'skipped': skipped}
            continue
        csv_path = os.path.join(data_dir, f'{code}.csv')
        try:
            # 判断是追加还是新建
            append = False
            fetch_start = start_default
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path, encoding='utf-8-sig')
                except pd.errors.EmptyDataError:
                    df = pd.DataFrame()

                if not df.empty and '日期' in df.columns:
                    df = df.sort_values(by='日期', ascending=True)
                    last_date = pd.to_datetime(df.iloc[-1]['日期'])
                    if last_date.strftime('%Y%m%d') >= today_str:
                        skipped += 1
                        # 每 10% 推送一次
                        if progress - last_yielded_pct >= 10:
                            last_yielded_pct = int(progress // 10) * 10
                            yield {'progress': progress, 'current': current, 'total': total,
                                   'updated': updated, 'skipped': skipped}
                        continue
                    fetch_start = (last_date + pd.Timedelta(days=1)).strftime('%Y%m%d')
                    append = True
                else:
                    # 文件存在但为空或无日期列，也视为追加（保留文件）
                    fetch_start = start_default
                    append = True

            kwargs = dict(symbol=code, period='daily',
                          start_date=fetch_start, end_date=today_str)
            if adjust:
                kwargs['adjust'] = adjust
            new_df = ak.stock_zh_a_hist(**kwargs)

            if new_df is None or new_df.empty:
                skipped += 1
                if not append:
                    _append_nonexistent_code(nonexistent_file, code, nonexistent_codes)
            else:
                if append:
                    new_df.to_csv(csv_path, mode='a', index=False,
                                  encoding='utf-8-sig', header=False)
                else:
                    new_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                updated += 1
            _time.sleep(0.1)
        except Exception as e:
            print(f"更新 {code} 数据时发生错误: {e}")
            skipped += 1
            errors += 1

        # 每 10% 推送一次进度
        if progress - last_yielded_pct >= 10:
            last_yielded_pct = int(progress // 10) * 10
            yield {'progress': progress, 'current': current, 'total': total,
                   'updated': updated, 'skipped': skipped}

    # 最终完成事件
    d = cfg['data_dir']
    yield {
        'status': 'ok',
        'progress': 100,
        'message': f'{data_type} 更新完成（更新 {updated} 只，跳过 {skipped} 只）',
        'count': _count_csv(d),
        'last_update': _get_last_update(d) or '未知',
    }


@csrf_exempt
def update_data(request):
    """首页更新数据接口（SSE 流式返回进度）"""
    if request.method == 'POST':
        data_type = request.POST.get('data_type', '')
        update_mode = request.POST.get('update_mode', 'incremental')
        today_str = request.POST.get('today_str', '').strip()
        if update_mode != 'incremental':
            return JsonResponse({'status': 'error', 'message': '仅支持增量更新模式'}, status=400)
        if today_str and (len(today_str) != 8 or not today_str.isdigit()):
            return JsonResponse({'status': 'error', 'message': 'today_str 格式错误，应为 YYYYMMDD'}, status=400)

        def event_stream():
            try:
                for event in _update_stock_data_stream(data_type, today_str=today_str):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'status': 'error', 'message': f'数据更新失败：{e}'}, ensure_ascii=False)}\n\n"

        response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response
    return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)


@csrf_exempt
def update_today_data(request):
    """首页更新当日数据接口：保存 daily_all 并按交易日间隔规则更新日线"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)

    req_today_str = request.POST.get('today_str', '').strip() or pd.to_datetime('today').strftime('%Y%m%d')
    if len(req_today_str) != 8 or not req_today_str.isdigit():
        return JsonResponse({'status': 'error', 'message': 'today_str 格式错误，应为 YYYYMMDD'}, status=400)

    try:
        import akshare as ak

        trade_cal = ak.tool_trade_date_hist_sina()
        trade_days = sorted(pd.to_datetime(trade_cal['trade_date']).dt.date.unique().tolist())
        trade_day_to_index = {d: i for i, d in enumerate(trade_days)}
        req_date = pd.to_datetime(req_today_str, format='%Y%m%d').date()
        valid_trade_days = [d for d in trade_days if d <= req_date]
        if not valid_trade_days:
            return JsonResponse({'status': 'error', 'message': f'{req_today_str} 之前没有可用交易日'}, status=400)

        today_date = valid_trade_days[-1]
        today_str = today_date.strftime('%Y%m%d')

        spot_df = ak.stock_zh_a_spot_em()
        daily_all_dir = os.path.join(DATA_DIR, 'daily_all')
        os.makedirs(daily_all_dir, exist_ok=True)
        daily_all_file = os.path.join(daily_all_dir, f'stock_zh_a_spot_em_{today_str}.csv')
        spot_df.to_csv(daily_all_file, index=False, encoding='utf-8-sig')

        spot_df['代码'] = spot_df['代码'].apply(_normalize_stock_code)
        total = len(spot_df)
        updated = 0
        skipped_no_file = 0
        skipped_gap = 0
        skipped_invalid = 0

        for _, row in spot_df.iterrows():
            code = row.get('代码', '')
            code = _normalize_stock_code(row.get('代码', ''))
            if not code:
                skipped_invalid += 1
                continue

            if code.startswith('6'):
                csv_path = os.path.join(DATA_DIR, '上证日线', f'{code}.csv')
            elif code.startswith(('0', '3')):
                csv_path = os.path.join(DATA_DIR, '深证日线', f'{code}.csv')
            else:
                skipped_invalid += 1
                continue

            if not os.path.exists(csv_path):
                skipped_no_file += 1
                continue

            try:
                old_df = pd.read_csv(csv_path, encoding='utf-8-sig')
            except pd.errors.EmptyDataError:
                skipped_gap += 1
                continue

            if old_df.empty or '日期' not in old_df.columns:
                skipped_gap += 1
                continue

            old_df = old_df.sort_values(by='日期', ascending=True)
            last_date = old_df.iloc[-1]['日期']
            if not _is_prev_trade_day(last_date, today_str, trade_day_to_index):
                skipped_gap += 1
                continue

            daily_row_df = _spot_row_to_daily_row(row, today_str=today_str, code=code)
            _upsert_daily_row(csv_path, daily_row_df, today_str=today_str)
            updated += 1

        return JsonResponse({
            'status': 'ok',
            'message': f'当日数据更新完成（有效交易日 {today_str}）：更新 {updated} 只，缺文件跳过 {skipped_no_file} 只，非隔一交易日跳过 {skipped_gap} 只，无效代码跳过 {skipped_invalid} 只',
            'daily_all_file': daily_all_file,
            'effective_today_str': today_str,
            'total': total,
            'updated': updated,
            'skipped_no_file': skipped_no_file,
            'skipped_gap': skipped_gap,
            'skipped_invalid': skipped_invalid,
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': f'更新失败：{e}'}, status=500)


# ── 自选股 ─────────────────────────────────────────

def _load_watchlist():
    """读取自选股列表 [{code, strategy_id}, ...]"""
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_watchlist(data):
    with open(WATCHLIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_default_strategies():
    """读取每只股票的默认策略 {code: strategy_id}"""
    if os.path.exists(DEFAULT_STRATEGY_FILE):
        try:
            with open(DEFAULT_STRATEGY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_default_strategies(data):
    os.makedirs(os.path.dirname(DEFAULT_STRATEGY_FILE), exist_ok=True)
    with open(DEFAULT_STRATEGY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@csrf_exempt
def watchlist_api(request):
    """自选股增删 + 设置默认策略 API (AJAX)"""
    if request.method == 'POST':
        body = json.loads(request.body)
        action = body.get('action')  # add / remove / set_default
        code = str(body.get('code', ''))
        strategy_id = body.get('strategy_id', '')
        wl = _load_watchlist()

        if action == 'add':
            if not any(w['code'] == code and w['strategy_id'] == strategy_id for w in wl):
                wl.append({'code': code, 'strategy_id': strategy_id})
                _save_watchlist(wl)
            return JsonResponse({'status': 'ok', 'in_watchlist': True})

        elif action == 'remove':
            wl = [w for w in wl if not (w['code'] == code and w['strategy_id'] == strategy_id)]
            _save_watchlist(wl)
            return JsonResponse({'status': 'ok', 'in_watchlist': False})

        elif action == 'set_default':
            ds = _load_default_strategies()
            ds[code] = strategy_id
            _save_default_strategies(ds)
            return JsonResponse({'status': 'ok', 'default_strategy': strategy_id})

    return JsonResponse({'status': 'error'}, status=400)


def watchlist_view(request, strategy_id):
    """自选股页面"""
    wl = _load_watchlist()
    # 只保留当前策略的自选
    codes = [w['code'] for w in wl if w['strategy_id'] == strategy_id]
    name_mapping = load_name_mapping()
    report_dir = REPORT_DIRS.get(strategy_id, '')

    items = []
    for code in codes:
        total_return_pct = 0
        sp, ep = 0, 0
        # 读取回测指标
        if report_dir:
            bp = os.path.join(report_dir, f'{code}_backtest.json')
            if os.path.exists(bp):
                try:
                    with open(bp, 'r', encoding='utf-8') as f:
                        m = json.load(f)
                    total_return_pct = round(m.get('total_return_pct', 0), 2)
                except Exception:
                    pass
        # 读取起始价/结束价
        for sub in ['上证日线', '深证日线', '基金_东方财富']:
            csv_path = os.path.join(DATA_DIR, sub, f'{code}.csv')
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path)
                    if len(df) >= 2:
                        sp = round(df.iloc[0, 4], 2)
                        ep = round(df.iloc[-1, 4], 2)
                except Exception:
                    pass
                break
        items.append({
            'code': code,
            'name': name_mapping.get(code, 'N/A'),
            'total_return_pct': total_return_pct,
            'start_price': sp,
            'end_price': ep,
        })

    strategy_name = strategy_id
    for s in STRATEGIES:
        if s['id'] == strategy_id:
            strategy_name = s['name']
            break

    return render(request, 'watchlist.html', {
        'strategy_id': strategy_id,
        'strategy_name': strategy_name,
        'items': items,
    })