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
import urllib.parse
import os
from functools import lru_cache
from akquant import Strategy, run_backtest
from .myStrategy import DualMAStrategy, ThreeDayReverseStrategy, RSIStrategy, VWAPStrategy
# ── 常量 ─────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), '../../data')
TRADE_INFO_DIR = os.path.join(os.path.dirname(__file__), '../../trade_info')
REPORT_DIRS = {
    'DualMA': os.path.join(os.path.dirname(__file__), '../../dualma_report'),
    'ThreeDayReverse': os.path.join(os.path.dirname(__file__), '../../threeDay_report'),
    'RSI': os.path.join(os.path.dirname(__file__), '../../rsi_report'),
    'VWAP': os.path.join(os.path.dirname(__file__), '../../vwap_report'),
}
DATA_DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), '../../data_download')
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), '../../data/watchlist.json')
DEFAULT_STRATEGY_FILE = os.path.join(os.path.dirname(__file__), '../../data/stock_default_strategy.json')
SETTINGS_FILE = os.path.join(os.path.dirname(__file__), '../../data/settings.json')
AI_GUIDE_HISTORY_FILE = os.path.join(os.path.dirname(__file__), '../../data/ai_guide_history.json')
DEBATE_HISTORY_FILE = os.path.join(os.path.dirname(__file__), '../../data/debate_history.json')
BOARD_COMPONENTS_FILE = os.path.join(os.path.dirname(__file__), '../../data/board_components.json')
TODAY_STOCK_JSON_FILE = os.path.join(os.path.dirname(__file__), '../../data/today_stock_data.json')
CSI300_LIST_FILE = os.path.join(os.path.dirname(__file__), '../../data/stock_csi300_spot_em.csv')

STRATEGIES = [
    {'id': 'DualMA', 'name': '双均线策略', 'description': '使用快线和慢线金叉/死叉进行交易'},
    {'id': 'ThreeDayReverse', 'name': '三日反转策略', 'description': '连续跌三天买入，涨三天卖出'},
    {'id': 'RSI', 'name': 'RSI策略', 'description': 'RSI超卖买入(30以下)，超买卖出(70以上)'},
    {'id': 'VWAP', 'name': 'VWAP策略', 'description': '收盘价上穿VWAP买入，下穿VWAP卖出'},
]

# ── 后台任务管理 ─────────────────────────────────────
_bg_tasks = {}          # {task_id: {status, result, created, description}}
_bg_tasks_lock = threading.Lock()
_TASK_EXPIRE_SECONDS = 600  # 10 分钟后自动清理
_ai_guide_history_lock = threading.Lock()
_debate_history_lock = threading.Lock()
_today_stock_lock = threading.Lock()
_board_update_lock = threading.Lock()
_board_index_cache = {}   # { updated_at_str: { code: [board_name, ...] } }
_board_update_state = {
    'task_id': '',
    'running': False,
    'progress': 0,
    'current': 0,
    'total': 0,
    'message': '',
    'updated_at': '',
    'error': '',
}


def _load_ai_guide_history():
    if not os.path.exists(AI_GUIDE_HISTORY_FILE):
        return {}
    try:
        with open(AI_GUIDE_HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_ai_guide_history(data):
    os.makedirs(os.path.dirname(AI_GUIDE_HISTORY_FILE), exist_ok=True)
    with open(AI_GUIDE_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_ai_guide_record(stock_code):
    code = _normalize_stock_code(stock_code)
    if not code:
        return None
    with _ai_guide_history_lock:
        history = _load_ai_guide_history()
        rec = history.get(code)
    return rec if isinstance(rec, dict) else None


def _set_ai_guide_record(stock_code, payload):
    code = _normalize_stock_code(stock_code)
    if not code or not isinstance(payload, dict):
        return
    with _ai_guide_history_lock:
        history = _load_ai_guide_history()
        history[code] = payload
        _save_ai_guide_history(history)


def _append_ai_guide_record(stock_code, payload, max_records=50):
    code = _normalize_stock_code(stock_code)
    if not code or not isinstance(payload, dict):
        return
    with _ai_guide_history_lock:
        history = _load_ai_guide_history()
        old = history.get(code)
        records = []

        if isinstance(old, dict):
            if isinstance(old.get('records'), list):
                records = [x for x in old.get('records') if isinstance(x, dict)]
            elif old.get('guide_text'):
                records = [
                    {
                        'query': old.get('query', ''),
                        'google_url': old.get('google_url', ''),
                        'guide_text': old.get('guide_text', ''),
                        'updated_at': old.get('updated_at', ''),
                    }
                ]

        records.append(payload)
        if max_records > 0 and len(records) > max_records:
            records = records[-max_records:]

        history[code] = {
            'query': payload.get('query', ''),
            'google_url': payload.get('google_url', ''),
            'guide_text': payload.get('guide_text', ''),
            'prompt': payload.get('prompt', ''),
            'updated_at': payload.get('updated_at', ''),
            'records': records,
        }
        _save_ai_guide_history(history)


def _get_debate_record(stock_code):
    code = _normalize_stock_code(stock_code)
    if not code:
        return None
    with _debate_history_lock:
        if not os.path.exists(DEBATE_HISTORY_FILE):
            return None
        try:
            with open(DEBATE_HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            rec = data.get(code)
            return rec if isinstance(rec, dict) else None
        except Exception:
            return None


def _save_debate_record(stock_code, payload):
    code = _normalize_stock_code(stock_code)
    if not code or not isinstance(payload, dict):
        return
    with _debate_history_lock:
        data = {}
        if os.path.exists(DEBATE_HISTORY_FILE):
            try:
                with open(DEBATE_HISTORY_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        data[code] = payload
        os.makedirs(os.path.dirname(DEBATE_HISTORY_FILE), exist_ok=True)
        with open(DEBATE_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _to_float_or_none(value):
    try:
        num = float(value)
    except Exception:
        return None
    if pd.isna(num):
        return None
    return num


def _normalize_today_row_from_record(record, stock_code='', date_text=''):
    if not isinstance(record, dict):
        return None
    code = _normalize_stock_code(record.get('股票代码', '') or stock_code)
    if not code:
        return None
    row_date = str(record.get('日期', '') or date_text).strip()
    if not row_date:
        return None
    return {
        '日期': row_date,
        '股票代码': code,
        '开盘': _to_float_or_none(record.get('开盘')),
        '收盘': _to_float_or_none(record.get('收盘')),
        '最高': _to_float_or_none(record.get('最高')),
        '最低': _to_float_or_none(record.get('最低')),
        '成交量': _to_float_or_none(record.get('成交量')),
        '成交额': _to_float_or_none(record.get('成交额')),
        '振幅': _to_float_or_none(record.get('振幅')),
        '涨跌幅': _to_float_or_none(record.get('涨跌幅')),
        '涨跌额': _to_float_or_none(record.get('涨跌额')),
        '换手率': _to_float_or_none(record.get('换手率')),
    }


def _build_today_row_from_hist_series(row, stock_code, today_dash):
    if row is None:
        return None
    record = {
        '日期': str(row.get('日期', today_dash) if hasattr(row, 'get') else today_dash),
        '股票代码': stock_code,
        '开盘': row.get('开盘') if hasattr(row, 'get') else None,
        '收盘': row.get('收盘') if hasattr(row, 'get') else None,
        '最高': row.get('最高') if hasattr(row, 'get') else None,
        '最低': row.get('最低') if hasattr(row, 'get') else None,
        '成交量': row.get('成交量') if hasattr(row, 'get') else None,
        '成交额': row.get('成交额') if hasattr(row, 'get') else None,
        '振幅': row.get('振幅') if hasattr(row, 'get') else None,
        '涨跌幅': row.get('涨跌幅') if hasattr(row, 'get') else None,
        '涨跌额': row.get('涨跌额') if hasattr(row, 'get') else None,
        '换手率': row.get('换手率') if hasattr(row, 'get') else None,
    }
    return _normalize_today_row_from_record(record, stock_code=stock_code, date_text=today_dash)


def _load_today_stock_payload():
    if not os.path.exists(TODAY_STOCK_JSON_FILE):
        return {}
    try:
        with open(TODAY_STOCK_JSON_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_today_stock_payload(payload):
    os.makedirs(os.path.dirname(TODAY_STOCK_JSON_FILE), exist_ok=True)
    with open(TODAY_STOCK_JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _replace_today_stock_rows(today_dash, rows_by_code):
    normalized = {}
    for code, row in (rows_by_code or {}).items():
        n_code = _normalize_stock_code(code)
        n_row = _normalize_today_row_from_record(row, stock_code=n_code, date_text=today_dash)
        if n_code and n_row:
            normalized[n_code] = n_row
    payload = {
        'snapshot_date': today_dash,
        'updated_at': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S'),
        'rows': normalized,
    }
    with _today_stock_lock:
        _save_today_stock_payload(payload)


def _upsert_today_stock_row(stock_code, row_dict, today_dash):
    n_code = _normalize_stock_code(stock_code)
    n_row = _normalize_today_row_from_record(row_dict, stock_code=n_code, date_text=today_dash)
    if not n_code or not n_row:
        return
    with _today_stock_lock:
        payload = _load_today_stock_payload()
        payload_date = str(payload.get('snapshot_date', '') or '')
        rows = payload.get('rows', {}) if isinstance(payload.get('rows', {}), dict) else {}
        if payload_date != today_dash:
            rows = {}
        rows[n_code] = n_row
        payload = {
            'snapshot_date': today_dash,
            'updated_at': pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S'),
            'rows': rows,
        }
        _save_today_stock_payload(payload)


def _get_today_stock_row(stock_code):
    n_code = _normalize_stock_code(stock_code)
    if not n_code:
        return None
    today_dash = pd.to_datetime('today').strftime('%Y-%m-%d')
    with _today_stock_lock:
        payload = _load_today_stock_payload()
    if str(payload.get('snapshot_date', '') or '') != today_dash:
        return None
    rows = payload.get('rows', {}) if isinstance(payload.get('rows', {}), dict) else {}
    row = rows.get(n_code)
    return _normalize_today_row_from_record(row, stock_code=n_code, date_text=today_dash)


def _merge_today_row_into_raw_df(raw_df, stock_code):
    today_row = _get_today_stock_row(stock_code)
    if not today_row:
        return raw_df

    if raw_df is None or raw_df.empty:
        return pd.DataFrame([today_row])

    if '日期' in raw_df.columns:
        result_df = raw_df[raw_df['日期'].astype(str) != today_row['日期']].copy()
        return pd.concat([result_df, pd.DataFrame([today_row])], ignore_index=True)

    en_row = {
        'date': today_row['日期'],
        'code': today_row['股票代码'],
        'open': today_row['开盘'],
        'close': today_row['收盘'],
        'high': today_row['最高'],
        'low': today_row['最低'],
        'volume': today_row['成交量'],
        'amount': today_row['成交额'],
        'amplitude': today_row['振幅'],
        'pct_chg': today_row['涨跌幅'],
        'chg': today_row['涨跌额'],
        'turnover': today_row['换手率'],
    }
    date_col = 'date' if 'date' in raw_df.columns else raw_df.columns[0]
    result_df = raw_df[raw_df[date_col].astype(str) != today_row['日期']].copy()
    return pd.concat([result_df, pd.DataFrame([en_row])], ignore_index=True)


def _load_raw_kline_df(stock_code):
    n_code = _normalize_stock_code(stock_code)
    kline_path = _find_kline_path_by_code(n_code)
    raw_df = pd.DataFrame()
    if kline_path and os.path.exists(kline_path):
        try:
            raw_df = pd.read_csv(kline_path, encoding='utf-8-sig')
        except pd.errors.EmptyDataError:
            raw_df = pd.DataFrame()
    raw_df = _merge_today_row_into_raw_df(raw_df, n_code)
    return raw_df, kline_path


def _purge_today_row_from_csv(csv_path, today_dash):
    if not csv_path or not os.path.exists(csv_path):
        return
    try:
        old_df = pd.read_csv(csv_path, encoding='utf-8-sig')
    except pd.errors.EmptyDataError:
        return
    except Exception:
        return
    if old_df.empty or '日期' not in old_df.columns:
        return
    new_df = old_df[old_df['日期'].astype(str) != today_dash]
    if len(new_df) != len(old_df):
        new_df.to_csv(csv_path, index=False, encoding='utf-8-sig')

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
    defaults = {
        'max_workers': 8,
        'commission': 0.00015,
        'ml_train_start_date': '',
        'ml_train_end_date': '',
    }
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


def _load_board_components_payload():
    if not os.path.exists(BOARD_COMPONENTS_FILE):
        return {}
    try:
        with open(BOARD_COMPONENTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _get_stock_index(payload):
    """返回 {code: [board_name, ...]} 索引。优先用 JSON 中的 stock_index，否则实时构建并缓存。"""
    if 'stock_index' in payload:
        return payload['stock_index']
    key = payload.get('updated_at', '__nokey__')
    if key in _board_index_cache:
        return _board_index_cache[key]
    index = {}
    for _b in (payload.get('boards') or []):
        bname = str(_b.get('board_name', '')).strip()
        for c in (_b.get('components') or []):
            code = str(c.get('code', '')).strip()
            if code:
                index.setdefault(code, []).append(bname)
    _board_index_cache[key] = index
    return index


def _to_float_safe(value):
    try:
        if value is None or value == '':
            return None
        return float(value)
    except Exception:
        return None


def _fetch_board_components_once(board_name):
    import akshare as ak

    cons_df = ak.stock_board_industry_cons_em(symbol=board_name)
    rows = []
    for _, row in cons_df.iterrows():
        code = _normalize_stock_code(row.get('代码', ''))
        if not code:
            continue
        rows.append({
            'code': code,
            'name': str(row.get('名称', '')).strip(),
            'latest_price': _to_float_safe(row.get('最新价')),
            'pct_change': _to_float_safe(row.get('涨跌幅')),
            'turnover_rate': _to_float_safe(row.get('换手率')),
            'pe_ttm': _to_float_safe(row.get('市盈率-动态')),
        })
    return rows


def _update_board_data_stream():
    import akshare as ak
    from datetime import datetime

    board_df = ak.stock_board_industry_name_em()
    total = len(board_df)
    boards = []
    failed = []
    last_progress = -5

    for idx, (_, row) in enumerate(board_df.iterrows()):
        board_name = str(row.get('板块名称', '')).strip()
        if not board_name:
            continue

        try:
            components = _fetch_board_components_once(board_name)
            boards.append({
                'board_name': board_name,
                'board_code': str(row.get('板块代码', '')).strip(),
                'latest_price': _to_float_safe(row.get('最新价')),
                'pct_change': _to_float_safe(row.get('涨跌幅')),
                'total_market_value': _to_float_safe(row.get('总市值')),
                'turnover_rate': _to_float_safe(row.get('换手率')),
                'rise_count': int(_to_float_safe(row.get('上涨家数')) or 0),
                'fall_count': int(_to_float_safe(row.get('下跌家数')) or 0),
                'leading_stock': str(row.get('领涨股票', '')).strip(),
                'leading_stock_pct': _to_float_safe(row.get('领涨股票-涨跌幅')),
                'components': components,
            })
        except Exception as e:
            failed.append({'board_name': board_name, 'error': str(e)})

        progress = round((idx + 1) / total * 100, 1) if total else 100
        if progress - last_progress >= 5 or idx == total - 1:
            last_progress = int(progress // 5) * 5
            yield {
                'progress': progress,
                'current': idx + 1,
                'total': total,
                'ok_count': len(boards),
                'failed_count': len(failed),
            }

        _time.sleep(0.15)

    # 构建反向索引：{code: [board_name, ...]}
    stock_index = {}
    for _b in boards:
        bname = _b.get('board_name', '')
        for c in (_b.get('components') or []):
            code = str(c.get('code', '')).strip()
            if code:
                stock_index.setdefault(code, []).append(bname)

    payload = {
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'board_type': 'industry',
        'board_count': len(boards),
        'failed_count': len(failed),
        'boards': boards,
        'stock_index': stock_index,
        'failed': failed,
    }

    os.makedirs(os.path.dirname(BOARD_COMPONENTS_FILE), exist_ok=True)
    with open(BOARD_COMPONENTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _board_index_cache.clear()  # 清旧缓存

    yield {
        'status': 'ok',
        'progress': 100,
        'message': f'板块更新完成：成功 {len(boards)} 个，失败 {len(failed)} 个',
        'updated_at': payload['updated_at'],
        'board_count': len(boards),
        'failed_count': len(failed),
    }


def _get_board_update_snapshot():
    with _board_update_lock:
        return dict(_board_update_state)


def _set_board_update_state(**kwargs):
    with _board_update_lock:
        _board_update_state.update(kwargs)


def _run_board_update_task(task_id):
    _set_board_update_state(
        task_id=task_id,
        running=True,
        progress=0,
        current=0,
        total=0,
        message='正在更新板块数据...',
        error='',
    )
    try:
        for event in _update_board_data_stream():
            if event.get('status') == 'ok':
                _set_board_update_state(
                    running=False,
                    progress=100,
                    current=event.get('total', _board_update_state.get('total', 0)),
                    total=event.get('total', _board_update_state.get('total', 0)),
                    message=event.get('message', '板块更新完成'),
                    updated_at=event.get('updated_at', ''),
                    error='',
                )
            else:
                _set_board_update_state(
                    progress=event.get('progress', _board_update_state.get('progress', 0)),
                    current=event.get('current', _board_update_state.get('current', 0)),
                    total=event.get('total', _board_update_state.get('total', 0)),
                    message='正在更新板块数据...',
                    error='',
                )
    except Exception as e:
        _set_board_update_state(
            running=False,
            message='板块更新失败',
            error=str(e),
        )


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


def _calc_ml_next_close_change_pct(stock_code, train_days=50):
    """计算 ML 预测摘要：近4日预测vs实际涨跌幅 + 下一日预测涨跌幅。"""
    try:
        code = _normalize_stock_code(stock_code)
        if not code:
            return None

        raw_df, kline_path = _load_raw_kline_df(code)
        if (raw_df is None or raw_df.empty) and (not kline_path or not os.path.exists(kline_path)):
            return None
        if raw_df.empty:
            return None

        if '日期' in raw_df.columns:
            raw_df.columns = [
                'date', 'code', 'open', 'close', 'high', 'low',
                'volume', 'amount', 'amplitude', 'pct_chg', 'chg', 'turnover'
            ]
        else:
            raw_df.columns = [
                'date', 'code', 'open', 'close', 'high', 'low',
                'volume', 'amount', 'amplitude', 'pct_chg', 'chg', 'turnover'
            ]

        for col in ['open', 'close', 'high', 'low', 'volume', 'amount', 'turnover']:
            if col not in raw_df.columns:
                raw_df[col] = 0.0
            raw_df[col] = pd.to_numeric(raw_df[col], errors='coerce')

        raw_df['date_ts'] = pd.to_datetime(raw_df['date'], errors='coerce')
        df = raw_df.dropna(subset=['open', 'close', 'high', 'low', 'date_ts']).reset_index(drop=True)

        settings = _load_settings()
        range_start = str(settings.get('ml_train_start_date', '') or '').strip()
        range_end = str(settings.get('ml_train_end_date', '') or '').strip()
        if range_start:
            try:
                start_ts = pd.to_datetime(range_start)
                df = df[df['date_ts'] >= start_ts]
            except Exception:
                pass
        if range_end:
            try:
                end_ts = pd.to_datetime(range_end)
                df = df[df['date_ts'] <= end_ts]
            except Exception:
                pass
        df = df.reset_index(drop=True)
        if len(df) < train_days + 2:
            return None

        recent_rows = []
        start_idx = max(train_days, len(df) - 4)
        for idx in range(start_idx, len(df)):
            train_df = df.iloc[idx - train_days:idx].reset_index(drop=True)
            model = _fit_linear_nextday_model(train_df, _DEFAULT_ML_FEATURE_CONFIG)
            if model is None:
                continue
            _, pred_close = _predict_next_from_window(train_df, model)
            if pred_close is None:
                continue

            prev_close = float(df.iloc[idx - 1]['close'])
            actual_close = float(df.iloc[idx]['close'])
            if prev_close == 0:
                continue

            pred_pct = (float(pred_close) - prev_close) / prev_close * 100.0
            actual_pct = (actual_close - prev_close) / prev_close * 100.0
            recent_rows.append({
                'date': str(df.iloc[idx]['date']),
                'pred_pct': round(pred_pct, 2),
                'actual_pct': round(actual_pct, 2),
            })

        next_train_df = df.iloc[-train_days:].reset_index(drop=True)
        next_model = _fit_linear_nextday_model(next_train_df, _DEFAULT_ML_FEATURE_CONFIG)
        if next_model is None:
            return None
        _, pred_close_next = _predict_next_from_window(next_train_df, next_model)
        if pred_close_next is None:
            return None

        latest_close = float(df.iloc[-1]['close'])
        if latest_close == 0:
            return None
        next_pct = (float(pred_close_next) - latest_close) / latest_close * 100.0

        return {
            'next_pred_pct': round(next_pct, 2),
            'latest_date': str(df.iloc[-1]['date']),
            'recent': recent_rows,
        }
    except Exception:
        return None


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


@lru_cache(maxsize=1)
def load_name_mapping():
    """从映射CSV中加载 代码→名称（统一6位代码）"""
    name_mapping = {}
    mapping_files = [
        os.path.join(DATA_DIR, 'fund_etf_spot_em_eastmoney.csv'),
        os.path.join(DATA_DIR, 'stock_sh_a_spot_em.csv'),
        os.path.join(DATA_DIR, 'stock_sz_a_spot_em.csv'),
        CSI300_LIST_FILE,
        os.path.join(DATA_DIR, 'stock_csi50_spot_em.csv'),
        os.path.join(DATA_DIR, '中证300股票名称.csv'),
        os.path.join(DATA_DIR, '中证50股票名称.csv'),
    ]
    for fpath in mapping_files:
        if not os.path.exists(fpath):
            continue
        try:
            df = pd.read_csv(fpath)
            code_col = ''
            for c in ['代码', '品种代码', '证券代码', 'symbol', 'code']:
                if c in df.columns:
                    code_col = c
                    break
            name_col = ''
            for c in ['名称', '品种名称', '证券简称', 'name']:
                if c in df.columns:
                    name_col = c
                    break
            if code_col and name_col:
                for _, row in df.iterrows():
                    code = _normalize_stock_code(row.get(code_col, ''))
                    if not code:
                        continue
                    name = str(row.get(name_col, '')).strip()
                    if not name:
                        continue
                    name_mapping[code] = name
        except Exception as e:
            print(f"Error loading {fpath}: {e}")
    return name_mapping


def _load_csi300_stock_items():
    candidates = [
        CSI300_LIST_FILE,
        os.path.join(DATA_DIR, '中证300股票名称.csv'),
        os.path.join(DATA_DIR, '中证300_股票名称.csv'),
    ]
    target = ''
    for p in candidates:
        if os.path.exists(p):
            target = p
            break
    if not target:
        return []

    try:
        df = pd.read_csv(target, encoding='utf-8-sig')
    except Exception:
        return []

    code_col = ''
    for c in ['代码', '品种代码', '证券代码', 'symbol', 'code']:
        if c in df.columns:
            code_col = c
            break
    name_col = ''
    for c in ['名称', '品种名称', '证券简称', 'name']:
        if c in df.columns:
            name_col = c
            break
    if not code_col:
        return []

    items = []
    seen = set()
    for _, row in df.iterrows():
        code = _normalize_stock_code(row.get(code_col, ''))
        if not code or code in seen:
            continue
        seen.add(code)
        name = str(row.get(name_col, '')).strip() if name_col else ''
        items.append({'code': code, 'name': name or code})
    items.sort(key=lambda x: x['code'])
    return items


def get_analysis_data(data_type, start_date='', end_date='',strategy_id='DualMA',
                      strategy_params={"fast_window": 10, "slow_window": 30},
                      force_recalc=False,max_workers=8):
    """遍历板块目录下所有CSV，计算收益率并排序返回（多线程回测）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 中证300：在沪深两个目录中找到对应股票的 CSV 文件
    if data_type == '中证300':
        csi300_items = _load_csi300_stock_items()
        if not csi300_items:
            return []
        csi300_codes = {item['code'] for item in csi300_items}
        # 构建 code -> csv路径 映射
        csv_file_map = {}  # code -> (fname, data_dir)
        for sub_dir in ['上证日线', '深证日线']:
            d = os.path.join(DATA_DIR, sub_dir)
            if not os.path.exists(d):
                continue
            for f in os.listdir(d):
                if f.endswith('.csv'):
                    code = f.replace('.csv', '')
                    if code in csi300_codes:
                        csv_file_map[code] = (f, d)
        if not csv_file_map:
            return []
        # 以 (fname, actual_data_dir) 列表形式传给后续处理
        csv_entries = list(csv_file_map.values())
        actual_data_dir = None  # 由 csv_entries 各自携带
    else:
        actual_data_dir = os.path.join(DATA_DIR, data_type)
        if not os.path.exists(actual_data_dir):
            return []
        csv_entries = [(f, actual_data_dir) for f in os.listdir(actual_data_dir) if f.endswith('.csv')]

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
    def _process_single_stock(fname, data_dir=actual_data_dir):
        """处理单只股票的读取 + 回测 + 保存，返回结果 dict 或 None"""
        code = fname.replace('.csv', '')
        try:
            df = pd.read_csv(os.path.join(data_dir, fname))
        except pd.errors.EmptyDataError:
            return None

        if len(df) < 2:
            return None
        df['date'] = pd.to_datetime(df.iloc[:, 0])

        # 回测使用完整历史数据（与详情页一致），保证均线计算正确
        # start_date/end_date 仅用于计算展示区间的收益率
        df_display = df.copy()
        if start_date:
            df_display = df_display[df_display['date'] >= pd.to_datetime(start_date)]
        if end_date:
            df_display = df_display[df_display['date'] <= pd.to_datetime(end_date)]
        if len(df_display) < 2:
            return None
        sp = df_display.iloc[0, 4]
        ep = df_display.iloc[-1, 4]
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

    # 多线程并发回测
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_process_single_stock, fname, ddir): (fname, ddir)
                      for fname, ddir in csv_entries}
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
        return {'上证日线': [], '深证日线': [], '基金_东方财富': [], '中证300': []}

    name_mapping = load_name_mapping()
    # 根据代码判断属于哪个板块
    dir_codes = {}
    for data_type in ['上证日线', '深证日线', '基金_东方财富']:
        d = os.path.join(DATA_DIR, data_type)
        if os.path.exists(d):
            for f in os.listdir(d):
                if f.endswith('.csv'):
                    dir_codes[f.replace('.csv', '')] = data_type

    # 加载中证300成分股代码集合
    csi300_codes = {item['code'] for item in _load_csi300_stock_items()}

    # 扫描 backtest JSON 文件
    results = {'上证日线': [], '深证日线': [], '基金_东方财富': [], '中证300': []}
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

        # 读取最近买卖点日期（orders.json 中最近一笔已成交单）
        last_signal_date = ''
        last_signal_side = ''
        orders_path = os.path.join(report_dir, f'{code}_orders.json')
        if os.path.exists(orders_path):
            try:
                with open(orders_path, 'r', encoding='utf-8') as f:
                    orders = json.load(f)
                if isinstance(orders, list):
                    filled_orders = [o for o in orders if isinstance(o, dict) and o.get('status') == 'filled']
                    if filled_orders:
                        filled_orders.sort(key=lambda x: str(x.get('created_at', '')), reverse=True)
                        latest = filled_orders[0]
                        last_signal_date = str(latest.get('created_at', ''))[:10]
                        last_signal_side = str(latest.get('side', '')).strip().lower()
            except Exception:
                pass

        entry = {
            'code': code,
            'name': name_mapping.get(code, 'N/A'),
            'total_return_pct': total_return_pct,
            'start_price': sp,
            'end_price': ep,
            'last_signal_date': last_signal_date,
            'last_signal_side': last_signal_side,
            'data_type': data_type,
        }
        results[data_type].append(entry)
        if code in csi300_codes:
            results['中证300'].append(entry)

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
        item['trade_signal_side'] = signal if signal in ['buy', 'sell'] else ''

        ml_summary = _calc_ml_next_close_change_pct(code, train_days=50)
        item['ml_prediction'] = ml_summary if isinstance(ml_summary, dict) else None
        if isinstance(ml_summary, dict):
            item['ml_next_close_change_pct'] = ml_summary.get('next_pred_pct')
        else:
            item['ml_next_close_change_pct'] = None
        watchlist_items.append(item)

    board_payload = _load_board_components_payload()
    board_rows = []
    for b in board_payload.get('boards', []) if isinstance(board_payload.get('boards'), list) else []:
        if not isinstance(b, dict):
            continue
        board_rows.append({
            'board_name': str(b.get('board_name', '')).strip(),
            'board_code': str(b.get('board_code', '')).strip(),
            'pct_change': b.get('pct_change'),
            'leading_stock': str(b.get('leading_stock', '')).strip(),
            'leading_stock_pct': b.get('leading_stock_pct'),
        })
    board_rows.sort(key=lambda x: float(x.get('pct_change') or 0), reverse=True)

    return render(request, 'index.html', {
        'strategies': STRATEGIES,
        'sh_count': _count_csv(sh_dir),
        'sz_count': _count_csv(sz_dir),
        'etf_count': _count_csv(etf_dir),
        'csi300_count': len(_load_csi300_stock_items()),
        'sh_update': _get_last_update(sh_dir) or '未更新',
        'sz_update': _get_last_update(sz_dir) or '未更新',
        'etf_update': _get_last_update(etf_dir) or '未更新',
        'watchlist_items': watchlist_items,
        'board_rows': board_rows,
        'board_updated_at': str(board_payload.get('updated_at', '') or ''),
    })


def board_components_api(request):
    board_name = str(request.GET.get('board_name', '')).strip()
    if not board_name:
        return JsonResponse({'status': 'error', 'message': '缺少板块名称'}, status=400)

    payload = _load_board_components_payload()
    boards = payload.get('boards', []) if isinstance(payload.get('boards'), list) else []
    target = None
    for board in boards:
        if isinstance(board, dict) and str(board.get('board_name', '')).strip() == board_name:
            target = board
            break

    if not target:
        return JsonResponse({'status': 'error', 'message': f'未找到板块：{board_name}'}, status=404)

    components = target.get('components', []) if isinstance(target.get('components'), list) else []
    default_strategies = _load_default_strategies()
    fallback_sid = STRATEGIES[0]['id'] if STRATEGIES else 'DualMA'
    enriched_components = []
    for comp in components:
        if not isinstance(comp, dict):
            continue
        code = _normalize_stock_code(comp.get('code', ''))
        sid = default_strategies.get(code, fallback_sid) if code else fallback_sid
        item = dict(comp)
        item['default_strategy_id'] = sid
        item['detail_url'] = f'/strategy/{sid}/{code}/' if code else ''
        enriched_components.append(item)

    return JsonResponse({
        'status': 'ok',
        'board_name': board_name,
        'board_code': str(target.get('board_code', '')).strip(),
        'components': enriched_components,
    })


@csrf_exempt
def update_board_data(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)

    def event_stream():
        try:
            for event in _update_board_data_stream():
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': f'板块更新失败：{e}'}, ensure_ascii=False)}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


@csrf_exempt
def start_board_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)

    snapshot = _get_board_update_snapshot()
    if snapshot.get('running') and snapshot.get('task_id'):
        return JsonResponse({'status': 'running', 'task_id': snapshot.get('task_id')})

    task_id = uuid.uuid4().hex[:10]
    t = threading.Thread(target=_run_board_update_task, args=(task_id,), daemon=True)
    t.start()
    return JsonResponse({'status': 'running', 'task_id': task_id})


def board_update_status(request):
    task_id = str(request.GET.get('task_id', '')).strip()
    snapshot = _get_board_update_snapshot()
    if task_id and snapshot.get('task_id') and task_id != snapshot.get('task_id') and not snapshot.get('running'):
        return JsonResponse({'status': 'not_found', 'message': '任务不存在或已结束'})

    if snapshot.get('error'):
        return JsonResponse({
            'status': 'error',
            'task_id': snapshot.get('task_id', ''),
            'progress': snapshot.get('progress', 0),
            'current': snapshot.get('current', 0),
            'total': snapshot.get('total', 0),
            'message': snapshot.get('message', ''),
            'error': snapshot.get('error', ''),
            'updated_at': snapshot.get('updated_at', ''),
        })

    if snapshot.get('running'):
        return JsonResponse({
            'status': 'running',
            'task_id': snapshot.get('task_id', ''),
            'progress': snapshot.get('progress', 0),
            'current': snapshot.get('current', 0),
            'total': snapshot.get('total', 0),
            'message': snapshot.get('message', ''),
            'updated_at': snapshot.get('updated_at', ''),
        })

    return JsonResponse({
        'status': 'done',
        'task_id': snapshot.get('task_id', ''),
        'progress': snapshot.get('progress', 100),
        'current': snapshot.get('current', snapshot.get('total', 0)),
        'total': snapshot.get('total', 0),
        'message': snapshot.get('message', ''),
        'updated_at': snapshot.get('updated_at', ''),
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

                ml_train_start_date = str(request.POST.get('ml_train_start_date', '') or '').strip()
                ml_train_end_date = str(request.POST.get('ml_train_end_date', '') or '').strip()
                if ml_train_start_date:
                    pd.to_datetime(ml_train_start_date)
                if ml_train_end_date:
                    pd.to_datetime(ml_train_end_date)
                settings['ml_train_start_date'] = ml_train_start_date
                settings['ml_train_end_date'] = ml_train_end_date

                _save_settings(settings)
                messages.success(request, f'设置已保存（并发线程数：{mw}，佣金率：{commission}，ML范围：{ml_train_start_date or "全部"} ~ {ml_train_end_date or "全部"}）')
            except (ValueError, TypeError):
                messages.error(request, '设置项格式错误（线程数/佣金率/日期）')
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

    # 按 tab 顺序生成导航列表（仅 code，前端用于上一个/下一个）
    nav_lists = {
        category: [item['code'] for item in items]
        for category, items in analysis_data.items()
    }

    return render(request, 'strategy_analysis.html', {
        'strategy_id': strategy_id,
        'strategy_name': strategy_name,
        'analysis_data': analysis_data,
        'start_date': start_date,
        'end_date': end_date,
        'watchlist_codes': watchlist_codes,
        'nav_lists_json': json.dumps(nav_lists, ensure_ascii=False),
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
    today_dash = pd.to_datetime(today_str, format='%Y%m%d').strftime('%Y-%m-%d')
    start_default = '20230101'
    use_today_json = os.path.basename(data_dir) in ['上证日线', '深证日线']

    try:
        # 判断是追加还是新建
        append = False
        refresh_today = False
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
                    fetch_start = today_str
                    append = False
                    refresh_today = True
                else:
                    fetch_start = (last_date + pd.Timedelta(days=1)).strftime('%Y%m%d')
                    append = True
            else:
                # 文件存在但为空或无日期列，也视为追加（保留文件头）
                fetch_start = start_default
                append = True

        if use_today_json:
            _purge_today_row_from_csv(csv_path, today_dash)

        kwargs = dict(symbol=stock_code, period='daily',
                      start_date=fetch_start, end_date=today_str)
        if adjust:
            kwargs['adjust'] = adjust
        new_df = ak.stock_zh_a_hist(**kwargs)
        _time.sleep(5)  # 避免请求过快被封禁

        if new_df is None or new_df.empty:
            if refresh_today:
                return {'status': 'ok', 'message': f'{stock_code} 当日暂无可覆盖新数据（已保留原数据）'}
            return {'status': 'ok', 'message': f'{stock_code} 无新数据可更新'}

        today_written = False
        if use_today_json and '日期' in new_df.columns:
            today_rows = new_df[new_df['日期'].astype(str) == today_dash]
            if not today_rows.empty:
                latest_today_row = _build_today_row_from_hist_series(today_rows.iloc[-1], stock_code, today_dash)
                if latest_today_row:
                    _upsert_today_stock_row(stock_code, latest_today_row, today_dash)
                    today_written = True
                new_df = new_df[new_df['日期'].astype(str) != today_dash].reset_index(drop=True)

        if new_df.empty:
            if today_written:
                return {'status': 'ok', 'message': f'{stock_code} 更新成功，已覆盖当日JSON数据 1 条'}
            if refresh_today:
                return {'status': 'ok', 'message': f'{stock_code} 当日暂无可覆盖新数据（已保留原数据）'}
            return {'status': 'ok', 'message': f'{stock_code} 无新数据可更新'}

        if append:
            new_df.to_csv(csv_path, mode='a', index=False,
                          encoding='utf-8-sig', header=False)
        else:
            if refresh_today and os.path.exists(csv_path):
                try:
                    old_df = pd.read_csv(csv_path, encoding='utf-8-sig')
                except pd.errors.EmptyDataError:
                    old_df = pd.DataFrame()

                if not old_df.empty and '日期' in old_df.columns:
                    old_df = old_df[old_df['日期'].astype(str) != today_dash]
                    merged_df = pd.concat([old_df, new_df], ignore_index=True)
                    merged_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                else:
                    new_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            else:
                new_df.to_csv(csv_path, index=False, encoding='utf-8-sig')

        new_count = len(new_df)
        if today_written:
            return {'status': 'ok', 'message': f'{stock_code} 更新成功，历史新增 {new_count} 条，当日JSON已覆盖'}
        if refresh_today:
            return {'status': 'ok', 'message': f'{stock_code} 更新成功，已覆盖当日数据 {new_count} 条'}
        return {'status': 'ok', 'message': f'{stock_code} 更新成功，新增 {new_count} 条数据'}
    except Exception as e:
        return {'status': 'error', 'message': f'{stock_code} 更新失败：{e}'}


def _find_kline_path_by_code(stock_code):
    kline_directories = [
        os.path.join(DATA_DIR, '基金_东方财富'),
        os.path.join(DATA_DIR, '上证日线'),
        os.path.join(DATA_DIR, '深证日线'),
    ]
    for d in kline_directories:
        p = os.path.join(d, f'{stock_code}.csv')
        if os.path.exists(p):
            return p
    return None


@csrf_exempt
def update_watchlist_data(request):
    """更新自选股数据并重算对应策略 API (AJAX POST)"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)

    wl = _load_watchlist()
    wl_codes = list(dict.fromkeys(w.get('code', '') for w in wl if w.get('code')))
    if not wl_codes:
        return JsonResponse({'status': 'ok', 'message': '暂无自选股票可更新', 'total': 0, 'updated': 0, 'skipped': 0, 'errors': 0})

    total = len(wl_codes)
    updated = 0
    skipped = 0
    errors = 0
    details = []

    for code in wl_codes:
        result = _update_single_stock_data(code)
        status = result.get('status')
        msg = result.get('message', '')
        details.append({'code': code, 'status': status, 'message': msg})

        if status == 'ok':
            if '更新成功' in msg:
                updated += 1
            else:
                skipped += 1
        else:
            errors += 1

    # 按自选里的 (code, strategy_id) 重算对应策略
    strategy_pairs = []
    seen = set()
    for w in wl:
        code = str(w.get('code', '')).strip()
        strategy_id = str(w.get('strategy_id', '')).strip()
        if not code or not strategy_id:
            continue
        key = (code, strategy_id)
        if key in seen:
            continue
        seen.add(key)
        strategy_pairs.append(key)

    strategy_recalc_ok = 0
    strategy_recalc_errors = 0
    strategy_details = []
    for code, strategy_id in strategy_pairs:
        kline_path = _find_kline_path_by_code(code)
        if not kline_path:
            strategy_recalc_errors += 1
            strategy_details.append({
                'code': code,
                'strategy_id': strategy_id,
                'status': 'error',
                'message': '找不到K线数据，无法重算策略',
            })
            continue

        try:
            _recalc_single_stock(strategy_id, code, kline_path)
            strategy_recalc_ok += 1
            strategy_details.append({
                'code': code,
                'strategy_id': strategy_id,
                'status': 'ok',
                'message': '策略重算完成',
            })
        except Exception as e:
            strategy_recalc_errors += 1
            strategy_details.append({
                'code': code,
                'strategy_id': strategy_id,
                'status': 'error',
                'message': f'策略重算失败：{e}',
            })

    today_payload = _load_today_stock_payload()
    today_snapshot_date = str(today_payload.get('snapshot_date', '') or '')
    today_rows = today_payload.get('rows', {}) if isinstance(today_payload.get('rows', {}), dict) else {}
    today_rows_count = len(today_rows)

    return JsonResponse({
        'status': 'ok',
        'message': (
            f'自选股更新完成：共 {total} 只，更新 {updated} 只，跳过 {skipped} 只，失败 {errors} 只；'
            f'策略重算 {len(strategy_pairs)} 个，成功 {strategy_recalc_ok} 个，失败 {strategy_recalc_errors} 个'
        ),
        'today_json_file': TODAY_STOCK_JSON_FILE,
        'today_snapshot_date': today_snapshot_date,
        'today_rows_count': today_rows_count,
        'total': total,
        'updated': updated,
        'skipped': skipped,
        'errors': errors,
        'details': details,
        'strategy_recalc_total': len(strategy_pairs),
        'strategy_recalc_ok': strategy_recalc_ok,
        'strategy_recalc_errors': strategy_recalc_errors,
        'strategy_details': strategy_details,
    })


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


@csrf_exempt
def recalc_watchlist_ml(request):
    """手动重算首页自选股 ML 预测摘要。"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)

    try:
        payload = json.loads(request.body or '{}')
    except Exception:
        payload = {}

    stock_code = _normalize_stock_code(payload.get('code', ''))
    if not stock_code:
        return JsonResponse({'status': 'error', 'message': '缺少股票代码'}, status=400)

    train_days = payload.get('train_days', 50)
    try:
        train_days = int(train_days)
    except Exception:
        train_days = 50
    train_days = max(20, min(300, train_days))

    ml_summary = _calc_ml_next_close_change_pct(stock_code, train_days=train_days)
    if not isinstance(ml_summary, dict):
        return JsonResponse({
            'status': 'error',
            'message': f'{stock_code} ML重算失败：数据不足或模型不可用',
            'code': stock_code,
        })

    return JsonResponse({
        'status': 'ok',
        'message': f'{stock_code} ML预测已重算',
        'code': stock_code,
        'ml_prediction': ml_summary,
        'ml_next_close_change_pct': ml_summary.get('next_pred_pct'),
    })


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

    # 读取K线数据（历史CSV + 当日JSON）
    merged_df, _ = _load_raw_kline_df(stock_code)
    if merged_df is not None and not merged_df.empty:
        df = merged_df
    else:
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
    # 支持指定计算哪些卡片，逗号分隔，默认全部
    _all_data_types = ['上证日线', '深证日线', '基金_东方财富', '中证300']
    cards_param = request.GET.get('cards', '')
    if cards_param:
        selected_cards = [c for c in cards_param.split(',') if c in _all_data_types]
    else:
        selected_cards = _all_data_types

    if not end_date:
        from datetime import date
        end_date = date.today().strftime('%Y-%m-%d')

    def _do_analysis():
        # 不再整体删除报告目录，由 get_analysis_data 内部按单只股票判断是否跳过今天已算的
        from concurrent.futures import ThreadPoolExecutor
        settings = _load_settings()
        mw = settings.get('max_workers', 8)
        data_types = selected_cards
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
    initial_cash = 100_000.0
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

    kline_data, _ = _load_raw_kline_df(stock_code)
    if kline_data is None or kline_data.empty:
        return render(request, 'error.html', {'message': f'找不到股票 {stock_code} 的K线数据'})
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

    # 保留全量历史 K 线，前端默认只展示最近 50 天，并允许通过滑块查看更早数据
    try:
        kline_data['date'] = pd.to_datetime(kline_data['date'], errors='coerce')
        kline_data = kline_data.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
        kline_data['date'] = kline_data['date'].dt.strftime('%Y-%m-%d')
    except Exception:
        pass

    # ML 交易模拟改为前端按需触发，避免首屏打开股票页时同步重算导致等待过久
    ml_result = {
        'buy_indices': [],
        'sell_indices': [],
        'buy_dates': [],
        'sell_dates': [],
        'trades': [],
        'round_trips': [],
        'equity_curve': [],
        'total_return': None,
        'total_profit': None,
        'total_cost': None,
        'total_cost_pct': None,
        'initial_cash': initial_cash,
        'final_cash': initial_cash,
        'commission_rate': None,
        'threshold_pct': 1.0,
        'train_days': 50,
        'start_date': '',
        'end_date': '',
        'trade_count': 0,
    }

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
            'trade_cost': None,
            'trade_cost_pct': None,
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

                total_cost = 0.0
                for t in trades_list:
                    commission_val = t.get('commission', None)
                    if commission_val is not None:
                        try:
                            total_cost += float(commission_val)
                            continue
                        except (TypeError, ValueError):
                            pass
                    try:
                        pnl_val = float(t.get('pnl', 0) or 0)
                        net_pnl_val = float(t.get('net_pnl', pnl_val) or pnl_val)
                        fee_guess = pnl_val - net_pnl_val
                        if fee_guess > 0:
                            total_cost += fee_guess
                    except (TypeError, ValueError):
                        pass
                comp['trade_cost'] = round(total_cost, 2)
                comp['trade_cost_pct'] = round(total_cost / initial_cash * 100, 4) if initial_cash > 0 else None
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
    is_in_watchlist = any(w.get('code') == stock_code and w.get('strategy_id') == strategy_id for w in wl)

    # 当前股票的默认策略
    default_strategies = _load_default_strategies()
    fallback_sid = STRATEGIES[0]['id'] if STRATEGIES else 'DualMA'
    stock_default_strategy = default_strategies.get(stock_code, fallback_sid)

    saved_ai = _get_ai_guide_record(stock_code) or {}
    saved_ai_text = str(saved_ai.get('guide_text', '') or '')
    saved_ai_query = str(saved_ai.get('query', '') or '')
    saved_ai_google_url = str(saved_ai.get('google_url', '') or '')
    saved_ai_prompt = str(saved_ai.get('prompt', '') or '')
    saved_ai_updated_at = str(saved_ai.get('updated_at', '') or '')
    saved_ai_count = 0
    if isinstance(saved_ai.get('records'), list):
        saved_ai_count = len(saved_ai.get('records'))
    elif saved_ai_text:
        saved_ai_count = 1

    saved_debate = _get_debate_record(stock_code) or {}

    # 查找当前股票所属板块（O(1) 索引查找）
    board_payload = _load_board_components_payload()
    board_updated_at = str(board_payload.get('updated_at', '') or '')
    stock_index = _get_stock_index(board_payload)
    _code_key = str(stock_code).strip()
    _board_names = stock_index.get(_code_key, [])
    _boards_by_name = {str(b.get('board_name', '')).strip(): b for b in (board_payload.get('boards') or []) if isinstance(b, dict)}
    stock_boards = []
    for _bname in _board_names:
        _b = _boards_by_name.get(_bname)
        if not _b:
            continue
        _comps = _b.get('components') or []
        stock_boards.append({
            'board_name': str(_b.get('board_name', '')).strip(),
            'board_code': str(_b.get('board_code', '')).strip(),
            'pct_change': _b.get('pct_change'),
            'total_market_value': _b.get('total_market_value'),
            'turnover_rate': _b.get('turnover_rate'),
            'rise_count': _b.get('rise_count'),
            'fall_count': _b.get('fall_count'),
            'leading_stock': str(_b.get('leading_stock', '')).strip(),
            'leading_stock_pct': _b.get('leading_stock_pct'),
            'components': [
                {
                    'code': str(c.get('code', '')).strip(),
                    'name': str(c.get('name', '')).strip(),
                    'latest_price': c.get('latest_price'),
                    'pct_change': c.get('pct_change'),
                    'turnover_rate': c.get('turnover_rate'),
                    'pe_ttm': c.get('pe_ttm'),
                }
                for c in _comps if isinstance(c, dict)
            ],
        })

    global_settings = _load_settings()
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
        'is_in_watchlist': is_in_watchlist,
        'strategies': STRATEGIES,
        'strategies_json': json.dumps([{'id': s['id'], 'name': s['name']} for s in STRATEGIES], ensure_ascii=False),
        'stock_default_strategy': stock_default_strategy,
        'initial_cash': initial_cash,
        'today': _date.today().strftime('%Y-%m-%d'),
        'ai_guide_saved_text': saved_ai_text,
        'ai_guide_saved_text_json': json.dumps(saved_ai_text, ensure_ascii=False),
        'ai_guide_saved_query': saved_ai_query,
        'ai_guide_saved_google_url': saved_ai_google_url,
        'ai_guide_saved_prompt_json': json.dumps(saved_ai_prompt, ensure_ascii=False),
        'ai_guide_saved_updated_at': saved_ai_updated_at,
        'ai_guide_saved_count': saved_ai_count,
        'ml_train_start_date': str(global_settings.get('ml_train_start_date', '') or ''),
        'ml_train_end_date': str(global_settings.get('ml_train_end_date', '') or ''),
        'ml_sim_result': json.dumps(ml_result, ensure_ascii=False),
        'ollama_url': os.getenv('OLLAMA_URL', 'http://localhost:11434').rstrip('/'),
        'ollama_model': os.getenv('OLLAMA_MODEL', 'qwen2.5:7b'),
        'debate_saved_bull_json': json.dumps(str(saved_debate.get('bull', '') or ''), ensure_ascii=False),
        'debate_saved_bear_json': json.dumps(str(saved_debate.get('bear', '') or ''), ensure_ascii=False),
        'debate_saved_decision_json': json.dumps(str(saved_debate.get('decision', '') or ''), ensure_ascii=False),
        'debate_saved_updated_at': str(saved_debate.get('updated_at', '') or ''),
        'debate_saved_model': str(saved_debate.get('model', '') or ''),
        'stock_boards': stock_boards,
        'stock_boards_json': json.dumps(stock_boards, ensure_ascii=False),
        'board_updated_at': board_updated_at,
    })


_DEFAULT_ML_FEATURE_CONFIG = {
    'use_ma5': True,
    'use_ma10': True,
    'use_ma20': True,
    'use_ma30': True,
    'use_ma60': False,
    'use_vol_chg1': True,
    'use_vol_chg5': True,
    'use_atr14': True,
}


def _normalize_ml_feature_config(raw):
    cfg = dict(_DEFAULT_ML_FEATURE_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    for k in cfg.keys():
        if k in raw:
            v = raw.get(k)
            cfg[k] = bool(v)
    return cfg


def _build_ml_feature_frame(df, feature_config=None):
    cfg = _normalize_ml_feature_config(feature_config)

    needed = ['open', 'close', 'high', 'low', 'volume', 'amount', 'turnover']
    d = df.copy()
    for col in needed:
        if col not in d.columns:
            d[col] = 0.0
        d[col] = pd.to_numeric(d[col], errors='coerce')

    X = pd.DataFrame({
        'open': d['open'],
        'close': d['close'],
        'high': d['high'],
        'low': d['low'],
        'volume': d['volume'].fillna(0),
        'amount': d['amount'].fillna(0),
        'turnover': d['turnover'].fillna(0),
        'ret1': d['close'].pct_change().fillna(0),
        'range_pct': ((d['high'] - d['low']) / d['close'].replace(0, pd.NA)).fillna(0),
    })

    if cfg.get('use_ma5'):
        X['ma5'] = d['close'].rolling(5).mean().fillna(0)
    if cfg.get('use_ma10'):
        X['ma10'] = d['close'].rolling(10).mean().fillna(0)
    if cfg.get('use_ma20'):
        X['ma20'] = d['close'].rolling(20).mean().fillna(0)
    if cfg.get('use_ma30'):
        X['ma30'] = d['close'].rolling(30).mean().fillna(0)
    if cfg.get('use_ma60'):
        X['ma60'] = d['close'].rolling(60).mean().fillna(0)

    if cfg.get('use_vol_chg1'):
        X['vol_chg1'] = d['volume'].pct_change(1).replace([pd.NA, float('inf'), float('-inf')], 0).fillna(0)
    if cfg.get('use_vol_chg5'):
        X['vol_chg5'] = d['volume'].pct_change(5).replace([pd.NA, float('inf'), float('-inf')], 0).fillna(0)

    if cfg.get('use_atr14'):
        prev_close = d['close'].shift(1)
        tr1 = (d['high'] - d['low']).abs()
        tr2 = (d['high'] - prev_close).abs()
        tr3 = (d['low'] - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        X['atr14'] = tr.rolling(14).mean().fillna(0)

    X = X.replace([float('inf'), float('-inf')], 0).fillna(0)
    return X, cfg


def _fit_linear_nextday_model(train_df, feature_config=None):
    """使用训练窗口拟合“次日开盘/收盘”线性模型，返回系数。"""
    import numpy as np

    df = train_df.copy()
    for col in ['open', 'close', 'high', 'low']:
        if col not in df.columns:
            return None
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df.dropna(subset=['open', 'close', 'high', 'low'])
    if len(df) < 8:
        return None

    X, cfg = _build_ml_feature_frame(df, feature_config)
    y_open = df['open'].shift(-1)
    y_close = df['close'].shift(-1)

    data = pd.concat([X, y_open.rename('y_open'), y_close.rename('y_close')], axis=1).dropna(subset=['y_open', 'y_close'])
    if len(data) < 6:
        return None

    feature_cols = list(X.columns)
    X_np = data[feature_cols].to_numpy(dtype=float)
    design_np = np.column_stack([np.ones(len(data), dtype=float), X_np])

    y_open_np = data['y_open'].to_numpy(dtype=float)
    y_close_np = data['y_close'].to_numpy(dtype=float)

    beta_open, *_ = np.linalg.lstsq(design_np, y_open_np, rcond=None)
    beta_close, *_ = np.linalg.lstsq(design_np, y_close_np, rcond=None)

    return {
        'feature_cols': feature_cols,
        'beta_open': beta_open,
        'beta_close': beta_close,
        'feature_config': cfg,
    }


def _predict_next_from_window(train_df, model):
    """基于窗口最后一根K线生成次日开盘/收盘预测。"""
    df = train_df.copy()
    X, _ = _build_ml_feature_frame(df, model.get('feature_config') if isinstance(model, dict) else None)
    if X.empty:
        return None, None

    last_row = X.iloc[-1].to_dict()

    import numpy as np
    x = np.array([1.0] + [float(last_row.get(c, 0.0)) for c in model['feature_cols']], dtype=float)
    pred_open = float(np.dot(x, model['beta_open']))
    pred_close = float(np.dot(x, model['beta_close']))
    return pred_open, pred_close


def _simulate_ml_trades(kline_df, train_days=50, threshold_pct=1.0, start_date='', end_date=''):
    """ML交易模拟：比较“预测次日收盘”与“当日收盘”，可配置交易区间与阈值。"""
    if kline_df is None or kline_df.empty:
        return {
            'buy_indices': [],
            'sell_indices': [],
            'buy_dates': [],
            'sell_dates': [],
            'trades': [],
            'equity_curve': [],
            'total_return': 0.0,
            'total_profit': 0.0,
            'unrealized_profit': 0.0,
            'total_cost': 0.0,
            'total_cost_pct': 0.0,
            'initial_cash': 100000.0,
            'final_cash': 100000.0,
            'final_equity': 100000.0,
            'open_position': 0,
            'commission_rate': 0.0,
            'signal_win_rate': None,
            'signal_win_count': 0,
            'signal_total': 0,
            'threshold_pct': 1.0,
            'train_days': int(max(20, min(300, int(train_days) if str(train_days).isdigit() else 50))),
            'start_date': str(start_date or ''),
            'end_date': str(end_date or ''),
            'trade_count': 0,
        }

    try:
        train_days = int(train_days)
    except Exception:
        train_days = 50
    train_days = max(20, min(300, train_days))

    try:
        threshold_pct = float(threshold_pct)
    except Exception:
        threshold_pct = 1.0
    threshold_pct = max(0.0, threshold_pct)

    settings = _load_settings()
    try:
        ml_commission_rate = float(settings.get('commission', 0.00015) or 0.0)
    except Exception:
        ml_commission_rate = 0.00015
    ml_commission_rate = max(0.0, ml_commission_rate)

    df = kline_df.copy().reset_index(drop=True)
    for col in ['open', 'close', 'high', 'low', 'volume', 'amount', 'turnover']:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors='coerce')

    if 'date' not in df.columns:
        return {
            'buy_indices': [], 'sell_indices': [], 'buy_dates': [], 'sell_dates': [], 'trades': [], 'equity_curve': [],
            'total_return': 0.0, 'total_profit': 0.0, 'unrealized_profit': 0.0,
            'total_cost': 0.0, 'total_cost_pct': 0.0,
            'initial_cash': 100000.0, 'final_cash': 100000.0, 'final_equity': 100000.0, 'open_position': 0,
            'commission_rate': ml_commission_rate,
            'signal_win_rate': None, 'signal_win_count': 0, 'signal_total': 0,
            'threshold_pct': threshold_pct, 'train_days': train_days,
            'start_date': str(start_date or ''), 'end_date': str(end_date or ''), 'trade_count': 0,
        }

    df['date_ts'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date_ts', 'close']).reset_index(drop=True)
    if df.empty or len(df) < (train_days + 1):
        return {
            'buy_indices': [], 'sell_indices': [], 'buy_dates': [], 'sell_dates': [], 'trades': [], 'equity_curve': [],
            'total_return': 0.0, 'total_profit': 0.0, 'unrealized_profit': 0.0,
            'total_cost': 0.0, 'total_cost_pct': 0.0,
            'initial_cash': 100000.0, 'final_cash': 100000.0, 'final_equity': 100000.0, 'open_position': 0,
            'commission_rate': ml_commission_rate,
            'signal_win_rate': None, 'signal_win_count': 0, 'signal_total': 0,
            'threshold_pct': threshold_pct, 'train_days': train_days,
            'start_date': str(start_date or ''), 'end_date': str(end_date or ''), 'trade_count': 0,
        }

    if start_date:
        try:
            start_ts = pd.to_datetime(start_date)
        except Exception:
            start_ts = None
    else:
        start_ts = None
    if end_date:
        try:
            end_ts = pd.to_datetime(end_date)
        except Exception:
            end_ts = None
    else:
        end_ts = None

    eligible_mask = pd.Series([True] * len(df))
    if start_ts is not None:
        eligible_mask = eligible_mask & (df['date_ts'] >= start_ts)
    if end_ts is not None:
        eligible_mask = eligible_mask & (df['date_ts'] <= end_ts)

    eligible_indices = [int(i) for i, ok in enumerate(eligible_mask.tolist()) if ok]
    if not eligible_indices:
        return {
            'buy_indices': [], 'sell_indices': [], 'buy_dates': [], 'sell_dates': [], 'trades': [], 'equity_curve': [],
            'total_return': 0.0, 'total_profit': 0.0, 'unrealized_profit': 0.0,
            'total_cost': 0.0, 'total_cost_pct': 0.0,
            'initial_cash': 100000.0, 'final_cash': 100000.0, 'final_equity': 100000.0, 'open_position': 0,
            'commission_rate': ml_commission_rate,
            'signal_win_rate': None, 'signal_win_count': 0, 'signal_total': 0,
            'threshold_pct': threshold_pct, 'train_days': train_days,
            'start_date': str(start_date or ''), 'end_date': str(end_date or ''), 'trade_count': 0,
        }

    first_eligible_idx = min(eligible_indices)
    last_eligible_idx = min(max(eligible_indices), len(df) - 1)
    start_i = max(train_days - 1, first_eligible_idx)
    end_i = min(last_eligible_idx, len(df) - 2)  # 需要预测次日，所以 i 最多到 len-2

    ml_buy_indices = []
    ml_sell_indices = []
    ml_buy_dates = []
    ml_sell_dates = []
    ml_trades = []
    ml_initial_cash = 100000.0
    ml_cash = ml_initial_cash
    ml_pos = 0
    ml_total_cost = 0.0
    ml_equity_curve = []

    if start_i <= end_i:
        for i in range(start_i, end_i + 1):
            train_df = df.iloc[i - train_days + 1:i + 1].copy()
            model = _fit_linear_nextday_model(train_df, _DEFAULT_ML_FEATURE_CONFIG)
            if model is None:
                continue

            _, pred_close = _predict_next_from_window(train_df, model)
            today_close = float(df.iloc[i]['close'])
            if today_close == 0 or pred_close is None:
                continue

            pred_pct = (float(pred_close) - today_close) / today_close * 100.0

            if pred_pct > threshold_pct and ml_pos == 0:
                buy_unit_cost = today_close * (1.0 + ml_commission_rate)
                ml_pos = int(ml_cash // buy_unit_cost) if buy_unit_cost > 0 else 0
                if ml_pos > 0:
                    buy_amount = ml_pos * today_close
                    buy_cost = buy_amount * ml_commission_rate
                    ml_cash -= (buy_amount + buy_cost)
                    ml_total_cost += buy_cost
                    ml_buy_indices.append(i)
                    ml_buy_dates.append(str(df.iloc[i]['date']))
                    ml_trades.append({
                        'type': 'buy',
                        'idx': i,
                        'date': str(df.iloc[i]['date']),
                        'price': today_close,
                        'qty': int(ml_pos),
                        'cash': ml_cash,
                        'pos': ml_pos,
                        'cost': round(float(buy_cost), 4),
                        'pred_pct': round(float(pred_pct), 4),
                    })

            elif pred_pct < -threshold_pct and ml_pos > 0:
                sell_amount = ml_pos * today_close
                sell_cost = sell_amount * ml_commission_rate
                ml_cash += (sell_amount - sell_cost)
                ml_total_cost += sell_cost
                ml_trades.append({
                    'type': 'sell',
                    'idx': i,
                    'date': str(df.iloc[i]['date']),
                    'price': today_close,
                    'qty': int(ml_pos),
                    'cash': ml_cash,
                    'pos': 0,
                    'cost': round(float(sell_cost), 4),
                    'pred_pct': round(float(pred_pct), 4),
                })
                ml_sell_indices.append(i)
                ml_sell_dates.append(str(df.iloc[i]['date']))
                ml_pos = 0

            ml_equity_curve.append(ml_cash + ml_pos * today_close)

    # 区间末不强平：保留持仓，仅按最后收盘价计算未实现收益
    mark_price = None
    if last_eligible_idx >= 0:
        try:
            mark_price = float(df.iloc[last_eligible_idx]['close'])
        except Exception:
            mark_price = None
    if mark_price is None or not pd.notna(mark_price):
        try:
            mark_price = float(df.iloc[-1]['close'])
        except Exception:
            mark_price = 0.0
    if mark_price is None or not pd.notna(mark_price):
        mark_price = 0.0

    final_equity = ml_cash + ml_pos * mark_price
    unrealized_profit = ml_pos * mark_price if ml_pos > 0 else 0.0
    # 精确未实现收益 = 按市值估算权益 - 当前现金（现金已扣除买入金额与手续费）
    if ml_pos > 0:
        try:
            last_buy = next((t for t in reversed(ml_trades) if t.get('type') == 'buy'), None)
            if last_buy:
                buy_price = float(last_buy.get('price', 0) or 0)
                qty_val = int(last_buy.get('qty', 0) or 0)
                if qty_val > 0 and buy_price > 0:
                    buy_amount = qty_val * buy_price
                    buy_fee = float(last_buy.get('cost', 0) or 0)
                    unrealized_profit = qty_val * mark_price - buy_amount - buy_fee
        except Exception:
            pass

    ml_total_profit = final_equity - ml_initial_cash
    ml_total_return = (ml_total_profit / ml_initial_cash * 100.0) if ml_initial_cash > 0 else 0.0
    ml_trade_count = len([t for t in ml_trades if t.get('type') == 'buy'])

    signal_total = 0
    signal_win_count = 0
    for t in ml_trades:
        idx = t.get('idx', None)
        t_type = str(t.get('type', '') or '')
        if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(df) - 1:
            t['signal_win'] = None
            t['next_day_close'] = None
            t['next_day_move_pct'] = None
            continue
        try:
            today_close = float(df.iloc[idx]['close'])
            next_close = float(df.iloc[idx + 1]['close'])
            if not pd.notna(today_close) or not pd.notna(next_close) or today_close == 0:
                t['signal_win'] = None
                t['next_day_close'] = None
                t['next_day_move_pct'] = None
                continue
            move_pct = (next_close - today_close) / today_close * 100.0
            if t_type == 'buy':
                is_win = move_pct > 0
            elif t_type == 'sell':
                is_win = move_pct < 0
            else:
                is_win = None

            t['signal_win'] = bool(is_win) if is_win is not None else None
            t['next_day_close'] = round(float(next_close), 4)
            t['next_day_move_pct'] = round(float(move_pct), 4)

            if is_win is not None:
                signal_total += 1
                if is_win:
                    signal_win_count += 1
        except Exception:
            t['signal_win'] = None
            t['next_day_close'] = None
            t['next_day_move_pct'] = None

    signal_win_rate = round(signal_win_count / signal_total * 100.0, 2) if signal_total > 0 else None

    round_trips = []
    open_buy = None
    for t in ml_trades:
        if t.get('type') == 'buy':
            open_buy = t
            continue
        if t.get('type') != 'sell' or not open_buy:
            continue
        try:
            buy_price = float(open_buy.get('price', 0) or 0)
            sell_price = float(t.get('price', 0) or 0)
            qty_buy = int(open_buy.get('qty', 0) or 0)
            qty_sell = int(t.get('qty', 0) or 0)
            qty = min(qty_buy, qty_sell) if qty_buy > 0 and qty_sell > 0 else max(qty_buy, qty_sell, 0)
            buy_fee = float(open_buy.get('cost', 0) or 0)
            sell_fee = float(t.get('cost', 0) or 0)
        except Exception:
            open_buy = None
            continue

        if qty <= 0 or buy_price <= 0 or sell_price <= 0:
            open_buy = None
            continue

        buy_amount = qty * buy_price
        sell_amount = qty * sell_price
        total_fee = buy_fee + sell_fee
        pnl = sell_amount - buy_amount - total_fee
        pnl_pct = (pnl / buy_amount * 100.0) if buy_amount > 0 else 0.0

        round_trips.append({
            'buy_date': str(open_buy.get('date', '') or ''),
            'sell_date': str(t.get('date', '') or ''),
            'buy_price': round(float(buy_price), 4),
            'sell_price': round(float(sell_price), 4),
            'qty': int(qty),
            'pnl': round(float(pnl), 2),
            'pnl_pct': round(float(pnl_pct), 4),
            'fee': round(float(total_fee), 2),
            'buy_signal_win': open_buy.get('signal_win', None),
            'sell_signal_win': t.get('signal_win', None),
            'trade_signal_win_rate': (
                round(
                    (
                        (1 if open_buy.get('signal_win') else 0 if open_buy.get('signal_win') is not None else 0)
                        + (1 if t.get('signal_win') else 0 if t.get('signal_win') is not None else 0)
                    )
                    / max(
                        1,
                        (1 if open_buy.get('signal_win') is not None else 0)
                        + (1 if t.get('signal_win') is not None else 0)
                    ) * 100.0,
                    2
                )
                if (open_buy.get('signal_win') is not None or t.get('signal_win') is not None)
                else None
            ),
        })
        open_buy = None

    return {
        'buy_indices': ml_buy_indices,
        'sell_indices': ml_sell_indices,
        'buy_dates': ml_buy_dates,
        'sell_dates': ml_sell_dates,
        'trades': ml_trades,
        'round_trips': round_trips,
        'equity_curve': ml_equity_curve,
        'total_return': round(float(ml_total_return), 2),
        'total_profit': round(float(ml_total_profit), 2),
        'unrealized_profit': round(float(unrealized_profit), 2),
        'total_cost': round(float(ml_total_cost), 2),
        'total_cost_pct': round(float(ml_total_cost / ml_initial_cash * 100.0), 4) if ml_initial_cash > 0 else None,
        'initial_cash': round(float(ml_initial_cash), 2),
        'final_cash': round(float(ml_cash), 2),
        'final_equity': round(float(final_equity), 2),
        'open_position': int(ml_pos),
        'commission_rate': ml_commission_rate,
        'signal_win_rate': signal_win_rate,
        'signal_win_count': int(signal_win_count),
        'signal_total': int(signal_total),
        'threshold_pct': round(float(threshold_pct), 4),
        'train_days': train_days,
        'start_date': str(start_date or ''),
        'end_date': str(end_date or ''),
        'trade_count': ml_trade_count,
    }


@csrf_exempt
def ml_trade_simulation(request):
    """按参数重算 ML 交易模拟（起止日期 + 交易阈值）。"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)

    try:
        body = json.loads(request.body or '{}')
    except Exception:
        body = {}

    stock_code = _normalize_stock_code(str(body.get('code', '')))
    if not stock_code:
        return JsonResponse({'status': 'error', 'message': '缺少股票代码'}, status=400)

    train_days_raw = body.get('train_days', 50)
    threshold_pct_raw = body.get('threshold_pct', 1.0)
    start_date = str(body.get('start_date', '') or '').strip()
    end_date = str(body.get('end_date', '') or '').strip()

    try:
        train_days = int(train_days_raw)
    except Exception:
        train_days = 50
    train_days = max(20, min(300, train_days))

    try:
        threshold_pct = float(threshold_pct_raw)
    except Exception:
        threshold_pct = 1.0
    threshold_pct = max(0.0, threshold_pct)

    kline_data, _ = _load_raw_kline_df(stock_code)
    if kline_data is None or kline_data.empty:
        return JsonResponse({'status': 'error', 'message': f'找不到 {stock_code} 的K线数据'}, status=404)

    if '日期' in kline_data.columns:
        kline_data.columns = [
            'date', 'code', 'open', 'close', 'high', 'low',
            'volume', 'amount', 'amplitude', 'pct_chg', 'chg', 'turnover'
        ]
    else:
        kline_data.columns = [
            'date', 'code', 'open', 'close', 'high', 'low',
            'volume', 'amount', 'amplitude', 'pct_chg', 'chg', 'turnover'
        ]
        if 'code' in kline_data.columns:
            kline_data = kline_data.drop('code', axis=1)

    # 与详情页保持一致，仅在最近5个月范围内重算
    try:
        kline_data['date'] = pd.to_datetime(kline_data['date'], errors='coerce')
        kline_data = kline_data.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
        if not kline_data.empty:
            latest_date = kline_data['date'].max()
            start_date_5m = latest_date - pd.DateOffset(months=5)
            recent_kline = kline_data[kline_data['date'] >= start_date_5m].copy()
            if not recent_kline.empty:
                kline_data = recent_kline.reset_index(drop=True)
        kline_data['date'] = kline_data['date'].dt.strftime('%Y-%m-%d')
    except Exception:
        pass

    if start_date and end_date:
        try:
            if pd.to_datetime(start_date) > pd.to_datetime(end_date):
                return JsonResponse({'status': 'error', 'message': '开始日期不能晚于结束日期'}, status=400)
        except Exception:
            return JsonResponse({'status': 'error', 'message': '日期格式错误，应为 YYYY-MM-DD'}, status=400)

    result = _simulate_ml_trades(
        kline_data,
        train_days=train_days,
        threshold_pct=threshold_pct,
        start_date=start_date,
        end_date=end_date,
    )
    return JsonResponse({'status': 'ok', 'ml_sim_result': result})


@csrf_exempt
def ml_daily_prediction(request):
    """日线 ML 预测：用设定天数训练，预测下一交易日开盘/收盘，并返回最近可验证对比。"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)

    try:
        body = json.loads(request.body or '{}')
    except Exception:
        body = {}

    stock_code = _normalize_stock_code(str(body.get('code', '')))
    feature_config = _normalize_ml_feature_config(body.get('feature_config', {}))
    settings = _load_settings()
    train_start_date = str(body.get('train_start_date', '') or '').strip() or str(settings.get('ml_train_start_date', '') or '').strip()
    train_end_date = str(body.get('train_end_date', '') or '').strip() or str(settings.get('ml_train_end_date', '') or '').strip()
    train_days_raw = body.get('train_days', 50)
    try:
        train_days = int(train_days_raw)
    except Exception:
        train_days = 50
    train_days = max(20, min(300, train_days))

    if not stock_code:
        return JsonResponse({'status': 'error', 'message': '缺少股票代码'}, status=400)

    raw_df, kline_path = _load_raw_kline_df(stock_code)
    if (raw_df is None or raw_df.empty) and (not kline_path or not os.path.exists(kline_path)):
        return JsonResponse({'status': 'error', 'message': f'找不到 {stock_code} 的K线数据'}, status=404)

    if raw_df.empty:
        return JsonResponse({'status': 'error', 'message': 'K线数据为空'}, status=400)

    if '日期' in raw_df.columns:
        raw_df.columns = [
            'date', 'code', 'open', 'close', 'high', 'low',
            'volume', 'amount', 'amplitude', 'pct_chg', 'chg', 'turnover'
        ]
    else:
        raw_df.columns = [
            'date', 'code', 'open', 'close', 'high', 'low',
            'volume', 'amount', 'amplitude', 'pct_chg', 'chg', 'turnover'
        ]

    for col in ['open', 'close', 'high', 'low', 'volume', 'amount', 'turnover']:
        if col not in raw_df.columns:
            raw_df[col] = 0.0
        raw_df[col] = pd.to_numeric(raw_df[col], errors='coerce')

    raw_df['date_ts'] = pd.to_datetime(raw_df['date'], errors='coerce')
    df = raw_df.dropna(subset=['open', 'close', 'high', 'low', 'date_ts']).reset_index(drop=True)

    # 训练数据日期范围过滤（可选）
    if train_start_date:
        try:
            start_ts = pd.to_datetime(train_start_date)
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'train_start_date 格式错误，应为 YYYY-MM-DD'}, status=400)
        df = df[df['date_ts'] >= start_ts]
    if train_end_date:
        try:
            end_ts = pd.to_datetime(train_end_date)
        except Exception:
            return JsonResponse({'status': 'error', 'message': 'train_end_date 格式错误，应为 YYYY-MM-DD'}, status=400)
        df = df[df['date_ts'] <= end_ts]

    df = df.reset_index(drop=True)
    min_required = train_days + 2
    if len(df) < min_required:
        return JsonResponse({
            'status': 'error',
            'message': f'数据不足：当前仅 {len(df)} 条，至少需要 {min_required} 条（日线）。可放宽日期范围或降低训练天数。',
        }, status=400)

    latest_idx = len(df) - 1
    verify_train_start = latest_idx - train_days
    verify_train_end = latest_idx - 1
    verify_train_df = df.iloc[verify_train_start:verify_train_end + 1].reset_index(drop=True)
    verify_model = _fit_linear_nextday_model(verify_train_df, feature_config)
    if verify_model is None:
        return JsonResponse({'status': 'error', 'message': '训练失败：样本不足或数据异常'}, status=400)

    pred_open_last, pred_close_last = _predict_next_from_window(verify_train_df, verify_model)
    actual_last = df.iloc[latest_idx]

    next_train_df = df.iloc[-train_days:].reset_index(drop=True)
    next_model = _fit_linear_nextday_model(next_train_df, feature_config)
    if next_model is None:
        return JsonResponse({'status': 'error', 'message': '下一交易日预测训练失败'}, status=400)

    pred_open_next, pred_close_next = _predict_next_from_window(next_train_df, next_model)

    def _safe_num(v, nd=3):
        try:
            fv = float(v)
            if fv != fv:
                return None
            return round(fv, nd)
        except Exception:
            return None

    actual_open_last = _safe_num(actual_last['open'], 3)
    actual_close_last = _safe_num(actual_last['close'], 3)
    err_open = _safe_num((pred_open_last - actual_open_last) if actual_open_last is not None else None, 4)
    err_close = _safe_num((pred_close_last - actual_close_last) if actual_close_last is not None else None, 4)

    # 构建“实际 vs 预测”开收盘对比序列（滚动训练、逐日预测）
    compare_start_idx = max(train_days, len(df) - 60)
    cmp_rows = []
    for idx in range(compare_start_idx, len(df)):
        train_window = df.iloc[idx - train_days:idx].reset_index(drop=True)
        m = _fit_linear_nextday_model(train_window, feature_config)
        if m is None:
            continue
        p_open, p_close = _predict_next_from_window(train_window, m)
        actual_row = df.iloc[idx]
        cmp_rows.append({
            'date': str(actual_row['date']),
            'actual_open': _safe_num(actual_row['open'], 3),
            'actual_close': _safe_num(actual_row['close'], 3),
            'pred_open': _safe_num(p_open, 3),
            'pred_close': _safe_num(p_close, 3),
        })

    cmp_dates = [r['date'] for r in cmp_rows]
    cmp_actual_open = [r['actual_open'] for r in cmp_rows]
    cmp_actual_close = [r['actual_close'] for r in cmp_rows]
    cmp_pred_open = [r['pred_open'] for r in cmp_rows]
    cmp_pred_close = [r['pred_close'] for r in cmp_rows]

    # 在图尾追加“下一交易日预测”点（只有预测值，无实际值）
    cmp_dates.append('下一交易日(预测)')
    cmp_actual_open.append(None)
    cmp_actual_close.append(None)
    cmp_pred_open.append(_safe_num(pred_open_next, 3))
    cmp_pred_close.append(_safe_num(pred_close_next, 3))

    return JsonResponse({
        'status': 'ok',
        'message': 'ML 日线预测完成',
        'code': stock_code,
        'train_days': train_days,
        'train_samples': train_days,
        'range_start': str(df.iloc[0]['date']) if len(df) else '',
        'range_end': str(df.iloc[-1]['date']) if len(df) else '',
        'range_rows': int(len(df)),
        'feature_config': feature_config,
        'feature_cols': list(next_model.get('feature_cols', [])),
        'verify': {
            'date': str(actual_last['date']),
            'pred_open': _safe_num(pred_open_last, 3),
            'actual_open': actual_open_last,
            'error_open': err_open,
            'pred_close': _safe_num(pred_close_last, 3),
            'actual_close': actual_close_last,
            'error_close': err_close,
        },
        'next_prediction': {
            'pred_open': _safe_num(pred_open_next, 3),
            'pred_close': _safe_num(pred_close_next, 3),
            'latest_date': str(df.iloc[-1]['date']),
        },
        'comparison_series': {
            'dates': cmp_dates,
            'actual_open': cmp_actual_open,
            'actual_close': cmp_actual_close,
            'pred_open': cmp_pred_open,
            'pred_close': cmp_pred_close,
        },
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
        '中证300': _load_csi300_stock_items(),
    }
    wl = _load_watchlist()
    watchlist_items = [
        {'code': w['code'], 'strategy_id': w['strategy_id'], 'name': name_mapping.get(w['code'], 'N/A')}
        for w in wl
    ]
    return render(request, 'stock_select.html', {
        'stocks': stocks,
        'strategies': STRATEGIES,
        'watchlist_items': watchlist_items,
    })


def csi300_stock_select(request):
    """中证300股票列表页"""
    stocks = {
        '中证300': _load_csi300_stock_items(),
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


def get_kline_data(request):
    """API: 返回指定股票的最新K线数据（用于前端局部刷新，避免整页重绘）"""
    code = request.GET.get('code', '').strip()
    if not code:
        return JsonResponse({'error': 'missing code'}, status=400)

    kline_df, _ = _load_raw_kline_df(code)
    if kline_df is None or kline_df.empty:
        return JsonResponse({'error': 'no data'}, status=404)

    if '日期' in kline_df.columns:
        kline_df.columns = [
            'date', 'code', 'open', 'close', 'high', 'low',
            'volume', 'amount', 'amplitude', 'pct_chg', 'chg', 'turnover'
        ]
    else:
        kline_df.columns = [
            'date', 'code', 'open', 'close', 'high', 'low',
            'volume', 'amount', 'amplitude', 'pct_chg', 'chg', 'turnover'
        ]

    try:
        kline_df['date'] = pd.to_datetime(kline_df['date'], errors='coerce')
        kline_df = kline_df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
        kline_df['date'] = kline_df['date'].dt.strftime('%Y-%m-%d')
    except Exception:
        pass

    for col in ['open', 'close', 'high', 'low', 'volume', 'amount', 'turnover']:
        if col in kline_df.columns:
            kline_df[col] = pd.to_numeric(kline_df[col], errors='coerce').fillna(0)

    records = kline_df[['date', 'open', 'close', 'high', 'low', 'volume', 'turnover']].to_dict(orient='records')
    return JsonResponse({'kline': records})


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
    use_today_json = data_type in ['上证A股', '深证A股']

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
            today_dash = pd.to_datetime(today_str, format='%Y%m%d').strftime('%Y-%m-%d')
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path, encoding='utf-8-sig')
                except pd.errors.EmptyDataError:
                    df = pd.DataFrame()

                if not df.empty and '日期' in df.columns:
                    df = df.sort_values(by='日期', ascending=True)
                    last_date = pd.to_datetime(df.iloc[-1]['日期'])
                    if last_date.strftime('%Y%m%d') >= today_str:
                        if use_today_json:
                            today_rows = df[df['日期'].astype(str) == today_dash]
                            if not today_rows.empty:
                                today_row = _build_today_row_from_hist_series(today_rows.iloc[-1], code, today_dash)
                                if today_row:
                                    _upsert_today_stock_row(code, today_row, today_dash)
                            _purge_today_row_from_csv(csv_path, today_dash)
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

            if use_today_json:
                _purge_today_row_from_csv(csv_path, today_dash)

            kwargs = dict(symbol=code, period='daily',
                          start_date=fetch_start, end_date=today_str)
            if adjust:
                kwargs['adjust'] = adjust
            new_df = ak.stock_zh_a_hist(**kwargs)
            _time.sleep(10)  # 避免请求过快被封禁
            if new_df is None or new_df.empty:
                skipped += 1
                if not append:
                    _append_nonexistent_code(nonexistent_file, code, nonexistent_codes)
            else:
                if use_today_json and '日期' in new_df.columns:
                    today_rows = new_df[new_df['日期'].astype(str) == today_dash]
                    if not today_rows.empty:
                        today_row = _build_today_row_from_hist_series(today_rows.iloc[-1], code, today_dash)
                        if today_row:
                            _upsert_today_stock_row(code, today_row, today_dash)
                        new_df = new_df[new_df['日期'].astype(str) != today_dash].reset_index(drop=True)

                if new_df.empty:
                    skipped += 1
                    _time.sleep(0.1)
                    if progress - last_yielded_pct >= 10:
                        last_yielded_pct = int(progress // 10) * 10
                        yield {'progress': progress, 'current': current, 'total': total,
                               'updated': updated, 'skipped': skipped}
                    continue

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
    """首页更新当日数据接口：保存当日全量快照（daily_all + data/today_stock_data.json）"""
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

        rows_by_code = {}
        for _, row in spot_df.iterrows():
            code = _normalize_stock_code(row.get('代码', ''))
            if not code:
                continue
            try:
                row_df = _spot_row_to_daily_row(row, today_str, code)
                if row_df is None or row_df.empty:
                    continue
                row_dict = row_df.iloc[0].to_dict()
                norm_row = _normalize_today_row_from_record(row_dict, stock_code=code, date_text=today_date.strftime('%Y-%m-%d'))
                if norm_row:
                    rows_by_code[code] = norm_row
            except Exception:
                continue
        _replace_today_stock_rows(today_date.strftime('%Y-%m-%d'), rows_by_code)

        total = len(spot_df)

        return JsonResponse({
            'status': 'ok',
            'message': f'当日数据快照保存完成（有效交易日 {today_str}）：已保存全量CSV和当日JSON，共 {total} 条',
            'daily_all_file': daily_all_file,
            'today_json_file': TODAY_STOCK_JSON_FILE,
            'effective_today_str': today_str,
            'total': total,
            'updated': 0,
            'skipped_no_file': 0,
            'skipped_gap': 0,
            'skipped_invalid': 0,
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

        elif action == 'reorder':
            new_order = body.get('items', [])
            if isinstance(new_order, list):
                valid = [
                    {'code': str(it.get('code', '')), 'strategy_id': str(it.get('strategy_id', ''))}
                    for it in new_order
                    if isinstance(it, dict) and it.get('code') and it.get('strategy_id')
                ]
                _save_watchlist(valid)
            return JsonResponse({'status': 'ok'})

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


@csrf_exempt
def ai_stock_guide(request):
    """AI选股指南：使用 Gemini API grounding（Google Search）返回选股参考"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)

    try:
        body = json.loads(request.body or '{}')
    except Exception:
        body = {}

    stock_code = str(body.get('code', '')).strip()
    stock_name = str(body.get('name', '')).strip()
    custom_query = str(body.get('query', '')).strip()
    current_price = body.get('current_price')
    chip_distribution = body.get('chip_distribution')
    query = custom_query or (stock_name + ' ' + stock_code + ' 现在能入手吗？').strip()
    if not query:
        return JsonResponse({'status': 'error', 'message': '缺少股票名称或代码'}, status=400)

    google_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
    gemini_api_key = os.getenv('GOOGLE_API_KEY', '').strip() or os.getenv('GEMINI_API_KEY', '').strip()
    if not gemini_api_key:
        return JsonResponse({
            'status': 'partial',
            'message': '未配置 GOOGLE_API_KEY（或 GEMINI_API_KEY），无法调用 Gemini grounding。',
            'query': query,
            'google_url': google_url,
            'guide_text': '',
        })

    try:
        chip_text = ''
        if isinstance(chip_distribution, list) and chip_distribution:
            valid_bins = []
            for item in chip_distribution[:12]:
                if not isinstance(item, dict):
                    continue
                low = item.get('low')
                high = item.get('high')
                ratio = item.get('ratio')
                try:
                    low_f = float(low)
                    high_f = float(high)
                    ratio_f = float(ratio)
                except Exception:
                    continue
                valid_bins.append((low_f, high_f, ratio_f))

            valid_bins.sort(key=lambda x: x[2], reverse=True)
            top_bins = valid_bins[:5]
            if top_bins:
                chip_text = '；当前筹码分布(占比前5档)：' + '；'.join([
                    f"{b[0]:.2f}-{b[1]:.2f}:{b[2]:.2f}%" for b in top_bins
                ])

        price_text = ''
        try:
            if current_price is not None:
                price_text = f"；当前价：{float(current_price):.2f}"
        except Exception:
            price_text = ''

        base_prompt = (
            f"分析一下股票：{stock_name} ({stock_code}){price_text}{chip_text}。"
            "请结合最新的市场新闻、财报数据和技术走势，给出现在的入手建议（仅供参考）。"
            "输出结构：1) 结论（能否入手）2) 关键理由（3-5条）3) 风险提示（2-3条）4) 关注指标。"
        )

        default_query = (stock_name + ' ' + stock_code + ' 现在能入手吗？').strip()
        if query and query != default_query:
            prompt = f"用户额外问题：{query}。请先回答该问题，再给出完整分析。" + base_prompt
        else:
            prompt = base_prompt

        payload = {
            'contents': [
                {
                    'parts': [
                        {
                            'text': prompt,
                        }
                    ]
                }
            ]
        }

        curl_cmd = [
            'curl',
            'https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent',
            '-H', 'Content-Type: application/json',
            '-H', f'X-goog-api-key: {gemini_api_key}',
            '-X', 'POST',
            '-d', json.dumps(payload, ensure_ascii=False),
        ]

        result = subprocess.run(
            curl_cmd,
            capture_output=True,
            text=True,
            timeout=400,
            check=False,
        )

        if result.returncode != 0:
            return JsonResponse({
                'status': 'error',
                'message': f"获取失败：curl 执行失败（{result.returncode}）：{(result.stderr or '').strip()}",
                'query': query,
                'google_url': google_url,
                'guide_text': '',
            }, status=500)

        data = json.loads((result.stdout or '').strip() or '{}')
        error_obj = data.get('error') if isinstance(data, dict) else None
        if isinstance(error_obj, dict) and error_obj.get('code') == 429:
            return JsonResponse({
                'status': 'partial',
                'message': 'Gemini 请求过于频繁或配额不足，请稍后重试。',
                'query': query,
                'google_url': google_url,
                'guide_text': '',
            }, status=429)

        candidates = data.get('candidates') or []
        parts = []
        if candidates:
            parts = (candidates[0].get('content') or {}).get('parts') or []
        text_chunks = [p.get('text', '').strip() for p in parts if isinstance(p, dict) and p.get('text')]
        guide_text = '\n'.join([t for t in text_chunks if t]).strip()

        if not guide_text:
            return JsonResponse({
                'status': 'partial',
                'message': 'Gemini 未返回有效内容，请稍后重试。',
                'query': query,
                'google_url': google_url,
                'guide_text': '',
            })

        from datetime import datetime
        _append_ai_guide_record(stock_code, {
            'query': query,
            'google_url': google_url,
            'guide_text': guide_text,
            'prompt': prompt,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }, max_records=50)

        return JsonResponse({
            'status': 'ok',
            'message': '已获取 AI 选股参考内容',
            'query': query,
            'google_url': google_url,
            'prompt': prompt,
            'guide_text': guide_text,
        })
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'message': f'获取失败：{e}',
            'query': query,
            'google_url': google_url,
            'guide_text': '',
        }, status=500)


# ── Ollama 多空辩论 ───────────────────────────────────

@csrf_exempt
def ollama_debate(request):
    """调用本地 Ollama 模型进行多空辩论，SSE 流式依次返回多头/空头/决策者意见"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '仅支持 POST'}, status=405)

    try:
        body = json.loads(request.body or '{}')
    except Exception:
        body = {}

    _default_ollama_url = os.getenv('OLLAMA_URL', 'http://localhost:11434').rstrip('/')
    _default_ollama_model = os.getenv('OLLAMA_MODEL', 'qwen2.5:7b')

    stock_code = _normalize_stock_code(str(body.get('code', '')))
    stock_name = str(body.get('name', '')).strip()
    ollama_model = str(body.get('model', '') or _default_ollama_model).strip() or _default_ollama_model
    strategy_id = str(body.get('strategy_id', '')).strip()
    ollama_url = str(body.get('ollama_url', '') or _default_ollama_url).rstrip('/')

    if not stock_code:
        return JsonResponse({'status': 'error', 'message': '缺少股票代码'}, status=400)

    # ── 构造上下文 ──────────────────────────────────
    raw_df, _ = _load_raw_kline_df(stock_code)
    kline_summary = '（无K线数据）'
    current_price = None

    if raw_df is not None and not raw_df.empty:
        if '日期' in raw_df.columns:
            raw_df.columns = [
                'date', 'code', 'open', 'close', 'high', 'low',
                'volume', 'amount', 'amplitude', 'pct_chg', 'chg', 'turnover'
            ]
        for col in ['close', 'pct_chg', 'volume']:
            if col in raw_df.columns:
                raw_df[col] = pd.to_numeric(raw_df[col], errors='coerce')
        recent = raw_df.tail(20)
        rows = []
        for _, row in recent.iterrows():
            try:
                rows.append(
                    f"{str(row.get('date', ''))[:10]} "
                    f"收:{float(row['close']):.2f} "
                    f"涨:{float(row['pct_chg']):+.2f}% "
                    f"量:{float(row['volume']):.0f}"
                )
            except Exception:
                pass
        if rows:
            kline_summary = '\n'.join(rows)
        try:
            current_price = float(raw_df.iloc[-1]['close'])
        except Exception:
            pass

    backtest_summary = ''
    if strategy_id in REPORT_DIRS:
        bp = os.path.join(REPORT_DIRS[strategy_id], f'{stock_code}_backtest.json')
        if os.path.exists(bp):
            try:
                with open(bp, 'r', encoding='utf-8') as f:
                    m = json.load(f)
                backtest_summary = f"策略({strategy_id})回测总收益率：{m.get('total_return_pct', 0):.2f}%"
            except Exception:
                pass

    price_text = f'{current_price:.2f}' if current_price else '未知'

    # 基础 context（无需网络，先构建）
    _base_context = (
        f"股票：{stock_name}（{stock_code}）\n"
        f"当前价格：{price_text}\n"
        f"{backtest_summary}\n"
        f"近20日行情（日期 收盘价 涨跌幅 成交量）：\n{kline_summary}"
    )

    def _call_ollama(prompt):
        import urllib.request as _req
        payload = json.dumps(
            {'model': ollama_model, 'prompt': prompt, 'stream': False},
            ensure_ascii=False
        ).encode('utf-8')
        req = _req.Request(
            f'{ollama_url}/api/generate',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with _req.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data.get('response', '').strip()

    def event_stream():
        opinions = {}

        # ── Step 1: 获取新闻与财务数据 ─────────────────────
        yield f"data: {json.dumps({'role': 'progress', 'message': '正在获取新闻与财务数据...'}, ensure_ascii=False)}\n\n"

        news_summary = '（无新闻数据）'
        try:
            import akshare as _ak
            news_df = _ak.stock_news_em(symbol=stock_code)
            if news_df is not None and not news_df.empty:
                col_title = next((c for c in news_df.columns if '标题' in c), news_df.columns[1] if len(news_df.columns) > 1 else news_df.columns[0])
                col_time = next((c for c in news_df.columns if '时间' in c or '日期' in c), None)
                rows = []
                for _, row in news_df.head(5).iterrows():
                    title = str(row.get(col_title, '')).strip()
                    t = str(row.get(col_time, '')).strip() if col_time else ''
                    rows.append(f"[{t}] {title}" if t else title)
                if rows:
                    news_summary = '\n'.join(rows)
        except Exception:
            pass

        financial_summary = '（无财务数据）'
        try:
            import akshare as _ak
            fin_df = _ak.stock_financial_analysis_indicator(symbol=stock_code, start_year='2023')
            if fin_df is not None and not fin_df.empty:
                latest = fin_df.iloc[-1]
                report_date = str(latest.get('日期', '')).strip()
                key_fields = [
                    ('摊薄每股收益(元)', '每股收益'),
                    ('净资产收益率(%)', 'ROE'),
                    ('总资产净利润率(%)', '总资产净利率'),
                    ('销售净利率(%)', '净利率'),
                    ('营业利润率(%)', '营业利润率'),
                    ('主营业务收入增长率(%)', '营收增长率'),
                    ('净利润增长率(%)', '净利润增长率'),
                    ('资产负债率(%)', '资产负债率'),
                ]
                metrics = []
                for col, label in key_fields:
                    val = latest.get(col)
                    if val is not None:
                        try:
                            fv = float(val)
                            if not pd.isna(fv):
                                metrics.append(f"{label}：{fv:.2f}")
                        except Exception:
                            pass
                if metrics:
                    financial_summary = f"报告期：{report_date}  " + '  '.join(metrics)
        except Exception:
            pass

        context = (
            _base_context
            + f"\n\n最新财务指标：\n{financial_summary}"
            + f"\n\n最新新闻（最近5条）：\n{news_summary}"
        )

        bull_prompt = (
            f"你是一位激进的多头分析师。以下是该股票的行情、财务指标和最新新闻：\n{context}\n\n"
            "请综合以上行情趋势、财务数据和新闻信息，站在多头（看涨）立场，给出3-5条看多理由，并说明买入逻辑。"
            "语言简洁，字数控制在200字以内，不要重复数据，聚焦判断逻辑。"
        )
        bear_prompt = (
            f"你是一位谨慎的空头分析师。以下是该股票的行情、财务指标和最新新闻：\n{context}\n\n"
            "请综合以上行情趋势、财务数据和新闻信息，站在空头（看跌）立场，给出3-5条看空理由，并说明回避或观望逻辑。"
            "语言简洁，字数控制在200字以内，不要重复数据，聚焦风险判断。"
        )

        # ── Step 2: 多头 / 空头 ───────────────────────────
        role_label = {'bull': '多头', 'bear': '空头'}
        for role, prompt in [('bull', bull_prompt), ('bear', bear_prompt)]:
            label = role_label.get(role, role)
            yield f"data: {json.dumps({'role': 'progress', 'message': '正在生成' + label + '观点...'}, ensure_ascii=False)}\n\n"
            try:
                text = _call_ollama(prompt)
            except Exception as e:
                text = f'（调用失败：{e}）'
            opinions[role] = text
            yield f"data: {json.dumps({'role': role, 'text': text}, ensure_ascii=False)}\n\n"

        # ── Step 3: 决策者 ────────────────────────────────
        decision_prompt = (
            f"你是一位资深投资决策者，必须做出明确、唯一的操作决定，不允许模糊或骑墙。\n\n"
            f"【股票数据】\n{context}\n\n"
            f"【多头分析师观点】\n{opinions.get('bull', '（无）')}\n\n"
            f"【空头分析师观点】\n{opinions.get('bear', '（无）')}\n\n"
            "综合以上所有信息，严格按照下面格式输出，不要添加任何其他内容：\n\n"
            "【操作】买入\n"
            "（或【操作】卖出，或【操作】空仓观望，三选一，只能写一个，禁止写『/』或同时列出多个选项）\n\n"
            "【决策依据】\n"
            "1. （最关键的支撑理由，结合财务或新闻数据说明）\n"
            "2. （第二条理由）\n"
            "3. （第三条理由，可选）\n\n"
            "【主要风险】\n"
            "1. （最大的下行风险）\n"
            "2. （第二个风险，可选）\n\n"
            "总字数200字以内。【操作】行只写操作结论本身，不加任何解释。"
        )
        yield f"data: {json.dumps({'role': 'progress', 'message': '决策者正在综合多空意见...'}, ensure_ascii=False)}\n\n"
        try:
            decision_text = _call_ollama(decision_prompt)
        except Exception as e:
            decision_text = f'（决策失败：{e}）'
        yield f"data: {json.dumps({'role': 'decision', 'text': decision_text}, ensure_ascii=False)}\n\n"

        # ── 持久化保存辩论结果 ─────────────────────────────
        try:
            from datetime import datetime as _dt
            _save_debate_record(stock_code, {
                'bull': opinions.get('bull', ''),
                'bear': opinions.get('bear', ''),
                'decision': decision_text,
                'model': ollama_model,
                'updated_at': _dt.now().strftime('%Y-%m-%d %H:%M:%S'),
            })
        except Exception:
            pass

        yield f"data: {json.dumps({'role': 'done'}, ensure_ascii=False)}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response