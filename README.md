## 一、 A 股深度价值漏斗Deep Value Funnel 


基于 **AKShare / 百度股市通** 等公开数据的多阶段选股流水线：基础池与快照粗筛 → 财务与「所有者盈余」质量 → 近五年 PE 分位 → 分红 → 日 K 回撤，输出 CSV。

**仅供学习与研究，不构成投资建议**；数据延迟、口径差异与 API 变更需自行评估。

--

| 路径 | 说明 |
|------|------|
| `deep_value_funnel/` | 包内全部 `.py` |
| `requirements.txt`、`README.md` | 依赖、说明 |

运行：`python -m deep_value_funnel.pipeline -o ...`


仓库根目录/
├── README.md
├── requirements.txt
└── deep_value_funnel/           # 漏斗 Python 包
    ├── __init__.py
    ├── config.py
    ├── pipeline.py
    ├── universe.py
    ├── universe_tushare.py
    ├── stage_financial.py
    ├── owner_earnings.py
    ├── retained_mcap_value.py
    ├── pe_hist_percentile.py
    ├── stage_dividend.py
    ├── stage_market.py
    ├── hist_fetch.py
    ├── export_artifacts.py
    ├── http_utils.py
    ├── request_identity.py
    └── symbols.py
```

---
## 二、环境要求

- **Python**：3.10+（与常见 akshare 版本兼容；CI 示例使用 3.10）  
- **操作系统**：Windows / macOS / Linux 均可；需能访问东财、新浪、百度等数据源（机房 IP 易被限流）

---

## 三、安装

```bash
cd /path/to/repo
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
pip install -U pip
pip install -r requirements.txt
```

**依赖说明（`requirements.txt`）**

| 包 | 用途 |
|----|------|
| `akshare` | 行情、财报、分红、行业板块等主数据 |
| `pandas` | 表格处理 |
| `numpy` | 排雷 / 深度分析脚本中的数值计算 |
| `requests` | 部分 HTTP（随 akshare 使用） |
| `tushare` | **可选**：`ENABLE_TUSHARE_COMPARE` 打开时与主流程对照 |
| `openpyxl` | 风险排雷与深度分析 **Excel** 导出 |

仅跑漏斗时，核心是 **akshare + pandas**；跑 **`run_pipeline.py` 整条链** 时还需要 **numpy、openpyxl**。未使用 Tushare 对照时可不配置 `TUSHARE_TOKEN`。

---

## 四、如何运行


在项目根目录（`deep_value_funnel` 的上一级）执行：

```bash
python -m deep_value_funnel.pipeline -o deep_value_pool_YYYYMMDD.csv
```

**试跑（减少请求量、便于调试）：**

```bash
python -m deep_value_funnel.pipeline -o out.csv --max-deep 30 --max-hist 10 -v
```

| 参数 | 含义 |
|------|------|
| `-o` / `--output` | 最终 CSV 路径 |
| `--max-deep` | 质量初筛通过后，只取前 N 只进入 PE 分位 |
| `--max-hist` | 分红通过后，只取前 N 只进入日 K/回撤 |
| `--drawdown-min` | 覆盖回撤下限（小数，如 `0.25`）；默认还受 `config` 与 `AK_DRAWDOWN_MIN` 影响 |
| `-v` | DEBUG 日志 |


产出示例：`deep_value_pool_*.csv`

---

## 五、配置说明（`deep_value_funnel/config.py`）

所有**选股阈值与开关**集中在该文件，主要项如下（具体数值以文件为准）。

| 类别 | 配置项 | 含义摘要 |
|------|--------|----------|
| PE 分位 | `PE_TTM_5Y_PERCENTILE_MAX`、`PE_TTM_HIST_MIN_SAMPLES` | 当前 PE 在近五年分布中的分位上限；最少样本数 |
| 财务 | `ROE_5Y_AVG_*`、`ROE_MIN`、`GROSS_MARGIN_MIN`、`DEBT_ASSET_RATIO_MAX` | 五年 ROE 均值、单期 ROE、毛利率、资产负债率 |
| 现金流/所有者盈余 | `FCF_YIELD_MIN`、`OWNER_EARNINGS_*` | 收益率下限；格林沃尔德 OE 开关、历史年数、是否退回 FCFF |
| 留存/市值 | `RETAINED_*` | 十年留存与市值增量比 |
| 分红 | `DIV_YIELD_MIN`、`PAYOUT_RATIO_MIN` | 股息率与三年分红率（**满足其一即通过分红阶段**） |
| 回撤 | `DRAWDOWN_MIN`、`HIST_WINDOW` | 近 N 日最大回撤下限；可被环境变量/CLI 覆盖 |
| 上市与行业 | `LISTING_MIN_YEARS`、`EXCLUDE_*` | 上市年限；是否剔除金融/公用事业等板块 |
| 快照粗筛 | `COARSE_*` | PE/PB/市值等价 spot 粗筛 |
| 调试 | `MAX_DEEP_CANDIDATES`、`MAX_HIST_CANDIDATES` | 限制进入 PE / 日 K 的数量 |
| 节流 | `REQUEST_*`、`HIST_*`、`DIV_*`、`SPOT_*` | 防封与请求节奏 |

---

## 六、常用环境变量

| 变量 | 作用 |
|------|------|
| `AK_REQUEST_THROTTLE` | 建议 `2`～`4`，成倍放慢请求间隔（出口 IP 易被风控时） |
| `AK_DRAWDOWN_MIN` | 覆盖 `DRAWDOWN_MIN`（小数，经 `clamp` 限制在合理区间） |
| `AK_SPOT_COOLDOWN` | 首次拉全市场快照前额外冷却（秒） |
| `AK_KLINE_ALLOW_EASTMONEY` | 设为 `1`/`true` 等时允许东财 K 线兜底 |
| `TUSHARE_TOKEN` | 配置后默认可开启 Tushare 对照（也可用 `ENABLE_TUSHARE_COMPARE=0` 关闭） |
| `PUSH_KEY` | **可选**：Server酱，用于 `run_pipeline.py` 漏斗结果摘要推送（`--no-notify` 可关） |

**切勿**将 Token 写入仓库；本地用 `.env` 或系统环境变量，GitHub 用 **Secrets**。

---

## 七、流水线顺序（摘要）

1. **基础池**：东财 A 股快照 + 上市日；剔 ST、非目标板块、上市年限；可选剔行业（金融/公用事业等）。  
2. **快照粗筛**：仅用 spot 字段（`COARSE_*`）。  
3. **质量初筛**：东财主要指标 + 现金流量表/资产负债表/利润表（所有者盈余）+ 十年留存 vs 市值等。  
4. **PE 近五年分位**：快照 PE + 百度历史序列。  
5. **分红**：东财分红详情 + 指标表对齐分红率。  
6. **日 K 回撤**：腾讯 K 线为主（可配置东财兜底），回撤 ≥ `DRAWDOWN_MIN`。

中间会生成 `step1_*.csv`、`step2_*.csv` 等（默认在输出目录或当前工作目录）。

---

## 八、免责声明

本项目使用第三方公开接口，**不保证数据准确性、完整性与实时性**。策略与参数仅为示例，**不构成任何证券投资建议**。使用后果由使用者自行承担。

---

## 九、许可证

若对外分享，请在本仓库根目录自行添加 `LICENSE`（如 MIT）并保留第三方库各自的许可证声明。
