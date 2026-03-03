# QuantWeb 量化交易系统 — 开发指南

> 基于 Django 的量化策略回测与数据分析平台。
> 项目根目录：`/Users/winssion/Desktop/akshare_proj/QuantWeb`

---

## 一、项目结构

```
QuantWeb/                          # Django 项目根目录
├── manage.py                      # Django 管理入口
├── db.sqlite3                     # SQLite 数据库（自动生成）
└── QuantWeb/                      # Django 应用包
    ├── __init__.py
    ├── settings.py                # Django 配置（模板目录、数据库、时区等）
    ├── urls.py                    # 路由定义
    ├── views.py                   # 视图函数（核心业务逻辑）
    ├── wsgi.py
    ├── asgi.py
    ├── myStrategy/ 
        ├── DualMAStrategy.py          # 双均线策略类（继承 akquant.Strategy）
        ├── ThreeDayReverseStrategy.py # 三日反转策略类
    └── templates/                 # 模板文件
        ├── index.html             # 首页
        ├── settings.html          # 设置页
        ├── strategy_analysis.html # 策略分析页
        ├── strategy_detail.html   # 股票详情页（回测报告 + K线图）
        └── stock_select.html      # 股票选择页

data/                              # 行情数据目录（与 QuantWeb 同级）
├── stock_sh_a_spot_em.csv         # 上证A股代码-名称映射
├── stock_sz_a_spot_em.csv         # 深证A股代码-名称映射
├── fund_etf_spot_em_eastmoney.csv # ETF基金代码-名称映射
├── 上证日线/                       # 上证各股票日线CSV
├── 深证日线/                       # 深证各股票日线CSV
└── 基金_东方财富/                   # 基金ETF日线CSV

data_download/                     # 数据下载脚本
├── update_main_sh.py              # 下载上证数据
├── update_main_sz.py              # 下载深证数据
└── update_main_etf_eastmoney.py   # 下载ETF数据
```

---

## 二、路由定义（urls.py）

| 路径 | 视图函数 | 名称 | 说明 |
|------|---------|------|------|
| `/` | `index` | index | 首页 |
| `/settings/` | `settings` | settings | 设置页 |
| `/strategy/<strategy_id>/` | `strategy_analysis` | strategy_analysis | 策略分析页 |
| `/strategy/<strategy_id>/<stock_code>/` | `strategy_detail` | strategy_detail | 股票详情页 |
| `/stocks/` | `stock_select` | stock_select | 股票选择页 |

---

## 三、页面功能需求

### 3.1 首页（index）`/`

**功能：**
- 显示数据统计卡片：上证股票数、深证股票数、ETF基金数（从 `data/` 目录下三个子目录的CSV文件数量统计）
  - 每个卡片显示对应数量和图标，点击后跳转到股票选择页 `/stocks/`
- 显示策略菜单列表，当前内置两个策略，点击策略后会显示所有股票及ETF的回测信息(这个功能先不实现，等主体功能调试好后再实现)：
  - **双均线策略**（DualMA）：使用快线和慢线金叉/死叉进行交易
  - **三日反转策略**（ThreeDayReverse）：连续跌三天买入，涨三天卖出
- 每个策略卡片可点击，跳转到 `/strategy/<strategy_id>/`，显示所有股票及ETF的回测信息，需要分上证股票数、深证股票数、ETF基金数3个卡片显示。
- 底部导航链接到「设置」和「股票选择」

**数据来源：**
- `data/上证日线/` 目录下 `.csv` 文件数 → `sh_count`
- `data/深证日线/` 目录下 `.csv` 文件数 → `sz_count`
- `data/基金_东方财富/` 目录下 `.csv` 文件数 → `etf_count`

**模板变量：** `strategies`(列表), `sh_count`, `sz_count`, `etf_count`

---

### 3.2 设置页（settings）`/settings/`

**功能：**
- **添加策略：** 输入策略名称，POST提交 `action=add_strategy`
- **更新股票数据：** 下拉选择数据类型（沪深A股 / 基金ETF），POST提交 `action=update_data`
  - 沪深A股 → 调用 `data_download/update_main_sh.py`
  - 基金ETF → 调用 `data_download/update_main_etf_eastmoney.py`
  - 下载后需清理：**删除空表格**，**删除2023年以前的数据**
- 操作结果通过 Django messages 框架显示成功/失败提示

---

### 3.3 策略分析页（strategy_analysis）`/strategy/<strategy_id>/`

**功能：**
- 顶部日期筛选器：选择回测起始日期 `start_date` 和结束日期 `end_date`（GET参数）
- 按三个板块分别展示数据分析结果，每个板块一个表格：
  1. **上证日线** — `data/上证日线/` 下所有股票
  2. **深证日线** — `data/深证日线/` 下所有股票
  3. **基金_东方财富** — `data/基金_东方财富/` 下所有股票
- 每只股票显示：代码、名称、起始价、结束价、总收益率
- 所有结果按**总收益率从高到低排序**
- 点击某只股票行，跳转到该股票的详情页 `/strategy/<strategy_id>/<stock_code>/`

**数据处理逻辑（`get_analysis_data`函数）：**
1. 遍历板块目录下所有 `.csv` 文件
2. 读取CSV，第1列为日期，第5列为收盘价（close）
3. 按 `start_date` / `end_date` 过滤日期范围
4. 计算总收益率 = (结束价 - 起始价) / 起始价 × 100%
5. 通过 `load_name_mapping()` 查找代码对应的股票名称

**名称映射（`load_name_mapping`函数）：**
- 从 `data/fund_etf_spot_em_eastmoney.csv`、`data/stock_sh_a_spot_em.csv`、`data/stock_sz_a_spot_em.csv` 三个文件中读取 `代码` → `名称` 的映射关系

---

### 3.4 股票详情页（strategy_detail）`/strategy/<strategy_id>/<stock_code>/`

**功能：**
- 页面标题显示股票代码和当前策略
- 两个标签页切换：
  - **Tab 1 — 回测报告：** 显示该策略在该股票上的回测统计结果
  - **Tab 2 — K线图：** 绘制该股票的K线图，并在图上**标注买卖点**（红色买入、绿色卖出）

**数据加载：**
1. **K线数据：** 依次在 `data/基金_东方财富/`、`data/上证日线/`、`data/深证日线/` 中查找 `<stock_code>.csv`
   - CSV列名处理：若含 `日期` 列则为11列格式（date, open, close, high, low, volume, amount, amplitude, pct_chg, chg, turnover）；否则为12列格式（含code列，需去除）
2. **交易信息：** 从 `trade_info/<strategy_id>/<stock_code>_trade_info.csv` 加载买卖记录
   - 按日期去重（保留最新），按日期升序排列
3. 后端将 K线数据和交易数据转为 JSON 字符串传给前端
4. 下方要显示回测报告，采用网页嵌入，代码参考下面：
```python
# 生成交互式 HTML 报告
result.report(filename=report_file,title="我的策略报告", show=False)
```

**模板变量：** `strategy_id`, `stock_code`, `kline_data`(JSON), `trade_data`(JSON)

---

### 3.5 股票选择页（stock_select）`/stocks/`

**功能：**
- 按板块分组列出所有可用股票：上证日线、深证日线、基金_东方财富
- 每只股票显示：代码、名称
- 点击股票后进入该股票的K线图页面
- 右侧可选择策略进行回测，下方显示回测报告

---

## 四、内置策略说明

### 4.1 双均线策略（DualMAStrategy）

- **文件：** `QuantWeb/DualMAStrategy.py`
- **继承：** `akquant.Strategy`
- **参数：** `fast_window=10`（快线周期），`slow_window=30`（慢线周期）
- **预热期：** `slow_window + 10`
- **逻辑：**
  - 每根K线触发 `on_bar(bar)`
  - 获取最近 `slow_window` 根收盘价
  - 计算快线均值（最近 `fast_window` 根）和慢线均值（最近 `slow_window` 根）
  - **金叉买入：** 快线 > 慢线 且 当前无持仓 → 买入 1000 股
  - **死叉卖出：** 快线 < 慢线 且 当前有持仓 → 全部卖出

### 4.2 三日反转策略（ThreeDayReverseStrategy）

- **文件：** `QuantWeb/ThreeDayReverseStrategy.py`
- **继承：** `akquant.Strategy`
- **预热期：** 4
- **逻辑：**
  - 记录每个 symbol 最近 4 个收盘价
  - **连续跌3天买入：** prices[-4] > prices[-3] > prices[-2] > prices[-1] 且 无持仓 → 买入 1000 股
  - **连续涨3天卖出：** prices[-4] < prices[-3] < prices[-2] < prices[-1] 且 有持仓 → 全部卖出

---

## 五、数据规范

### 5.1 股票日线CSV格式

**上证/深证（含 `日期` 列）— 11列：**

| 日期 | 开盘 | 收盘 | 最高 | 最低 | 成交量 | 成交额 | 振幅 | 涨跌幅 | 涨跌额 | 换手率 |

**基金ETF（含 `code` 列）— 12列：**

| date | code | open | close | high | low | volume | amount | amplitude | pct_chg | chg | turnover |

### 5.2 交易信息CSV格式

存放路径：`trade_info/<策略ID>/<股票代码>_trade_info.csv`

| date | action(buy/sell) | price | quantity | ... |

### 5.3 代码-名称映射CSV

- `data/stock_sh_a_spot_em.csv` — 含 `代码`、`名称` 列
- `data/stock_sz_a_spot_em.csv` — 含 `代码`、`名称` 列
- `data/fund_etf_spot_em_eastmoney.csv` — 含 `代码`、`名称` 列

---

## 六、待完善功能

> 以下为框架中已预留但尚未完整实现的功能，后续逐步补充：

1. **策略分析页模板：** 需在 `strategy_analysis.html` 中渲染 `analysis_data` 字典，每个板块对应一个可排序表格，点击行跳转详情页
2. **股票详情页K线图：** 需引入 ECharts 或类似图表库，用 `kline_data` JSON 绘制K线，用 `trade_data` JSON 标注买卖点
3. **股票选择页：** 需在 `stock_select.html` 中按板块分组展示股票列表，支持搜索和点击跳转
4. **设置页-添加策略：** 需实现策略持久化存储（数据库/配置文件），首页动态读取策略列表
5. **数据更新-清理逻辑：** 下载数据后需自动删除空表格、删除2023年以前的历史数据
6. **views.py 整理：** 当前文件中存在重复定义的函数（顶部有裸函数定义、import在中间），需清理为标准格式：imports → 常量 → 工具函数 → 视图函数

---

## 七、启动方式

```bash
cd /Users/winssion/Desktop/akshare_proj/QuantWeb
python manage.py runserver 0.0.0.0:8000
```

访问 http://localhost:8000/ 即可打开首页。