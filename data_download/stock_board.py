import json
import time
from datetime import datetime
from pathlib import Path

import akshare as ak


ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT_DIR / "data" / "board_components.json"


def _to_float(value):
	try:
		if value is None or value == "":
			return None
		return float(value)
	except Exception:
		return None


def _fetch_board_components(board_name, retry=2, sleep_seconds=0.6):
	last_err = None
	for _ in range(retry + 1):
		try:
			cons_df = ak.stock_board_industry_cons_em(symbol=board_name)
			rows = []
			for _, row in cons_df.iterrows():
				code = str(row.get("代码", "")).strip().zfill(6)
				if not code:
					continue
				rows.append({
					"code": code,
					"name": str(row.get("名称", "")).strip(),
					"latest_price": _to_float(row.get("最新价")),
					"pct_change": _to_float(row.get("涨跌幅")),
					"turnover_rate": _to_float(row.get("换手率")),
					"pe_ttm": _to_float(row.get("市盈率-动态")),
				})
			return rows
		except Exception as e:
			last_err = str(e)
			time.sleep(sleep_seconds)
	raise RuntimeError(last_err or "unknown error")


def main():
	board_df = ak.stock_board_industry_name_em()
	boards = []
	failed = []

	for _, row in board_df.iterrows():
		board_name = str(row.get("板块名称", "")).strip()
		if not board_name:
			continue

		try:
			components = _fetch_board_components(board_name)
			boards.append({
				"board_name": board_name,
				"board_code": str(row.get("板块代码", "")).strip(),
				"latest_price": _to_float(row.get("最新价")),
				"pct_change": _to_float(row.get("涨跌幅")),
				"total_market_value": _to_float(row.get("总市值")),
				"turnover_rate": _to_float(row.get("换手率")),
				"rise_count": int(_to_float(row.get("上涨家数")) or 0),
				"fall_count": int(_to_float(row.get("下跌家数")) or 0),
				"leading_stock": str(row.get("领涨股票", "")).strip(),
				"leading_stock_pct": _to_float(row.get("领涨股票-涨跌幅")),
				"components": components,
			})
		except Exception as e:
			failed.append({"board_name": board_name, "error": str(e)})

	payload = {
		"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
		"board_type": "industry",
		"board_count": len(boards),
		"failed_count": len(failed),
		"boards": boards,
		"failed": failed,
	}

	OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
	with OUTPUT_PATH.open("w", encoding="utf-8") as f:
		json.dump(payload, f, ensure_ascii=False, indent=2)

	print(f"已写入: {OUTPUT_PATH}")
	print(f"成功板块: {len(boards)}，失败板块: {len(failed)}")


if __name__ == "__main__":
	main()