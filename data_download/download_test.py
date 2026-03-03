from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import time
import json

options = Options()
options.add_argument("--headless=new")

driver = webdriver.Chrome(options=options)
driver.get("https://quote.eastmoney.com/sh600498.html")

time.sleep(5)

# 在浏览器中执行 JS，直接 fetch K线接口
js = """
return fetch("https://push2his.eastmoney.com/api/qt/stock/kline/get?fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56&secid=1.600498&klt=101&fqt=1&beg=20220101&end=20260226")
.then(response => response.json())
"""

data = driver.execute_script(js)

driver.quit()

# 解析K线
klines = data["data"]["klines"]

daily_data = []
for item in klines:
    parts = item.split(",")
    daily_data.append({
        "date": parts[0],
        "open": parts[1],
        "close": parts[2],
        "high": parts[3],
        "low": parts[4],
        "volume": parts[5]
    })

for row in daily_data[:5]:
    print(row)