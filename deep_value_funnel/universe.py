"""
阶段 0：构建基础股票池。

数据来源：
- ``ak.stock_zh_a_spot_em``：全市场最新价与动态市盈率（一次分页拉全量）。
- ``ak.stock_history_dividend``：一次性获取上市日期（新浪汇总页），避免对数千只股票逐只打个股信息接口。
- **行业剔除（可选）**：东财 ``stock_board_industry_name_em`` + 各板块 ``stock_board_industry_cons_em``，
  在基础池中剔除 ``config.EXCLUDE_INDUSTRY_BOARDS_EM`` 所列板块成份（默认金融 + 公用事业）。
- **快照粗筛**：``apply_coarse_prefilter``，仅用 spot 列（无财报）。
- **PE 分位**：``apply_pe_prefilter``，百度近五年 PE 序列（逐股），位于 **质量初筛之后**（见 ``pipeline``）。
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import akshare as ak
import pandas as pd

from deep_value_funnel import config
from deep_value_funnel.http_utils import call_with_retry, df_nonempty
from deep_value_funnel.pe_hist_percentile import percentile_for_stock_baidu
from deep_value_funnel.symbols import is_star_or_main_board_a, is_st_name

logger = logging.getLogger(__name__)


def _load_spot_universe() -> pd.DataFrame:
    """拉取东财沪深京 A 股实时行情。"""
    cd = float(getattr(config, "SPOT_EM_COOLDOWN_SEC", 0.0) or 0.0)
    if cd > 0:
        sleep_sec = cd * float(getattr(config, "REQUEST_THROTTLE_MULTIPLIER", 1.0))
        logger.info(
            "首次请求东财全市场行情前冷却 %.1f 秒（可用 AK_SPOT_COOLDOWN、AK_REQUEST_THROTTLE 调整）",
            sleep_sec,
        )
        time.sleep(sleep_sec)

    def _fetch() -> pd.DataFrame:
        return ak.stock_zh_a_spot_em()

    df = call_with_retry("stock_zh_a_spot_em", _fetch, validate=df_nonempty)
    return df


def _load_listing_dates() -> pd.DataFrame:
    """拉取新浪「历史分红」汇总表（含上市日期）。"""

    def _fetch() -> pd.DataFrame:
        return ak.stock_history_dividend()

    df = call_with_retry("stock_history_dividend", _fetch, validate=df_nonempty)
    return df


def _em_finance_util_excluded_codes() -> set[str]:
    """
    按东财行业板块名称拉取成份股代码并集，用于在基础池中剔除金融、公用事业等。

    接口失败或某板块名称不在全量行业表中时记日志并 **fail-open**（该板块或整类不剔除）。
    """
    if not getattr(config, "EXCLUDE_FINANCE_UTIL_INDUSTRY_EM", True):
        return set()

    boards_cfg: tuple[str, ...] = getattr(
        config,
        "EXCLUDE_INDUSTRY_BOARDS_EM",
        ("银行", "保险", "证券", "多元金融", "公用事业"),
    )
    out: set[str] = set()

    try:
        names_df = call_with_retry(
            "stock_board_industry_name_em",
            lambda: ak.stock_board_industry_name_em(),
            validate=df_nonempty,
        )
    except Exception:  # noqa: BLE001
        logger.exception("东财行业板块名称拉取失败，本趟不剔除配置的行业板块")
        return set()

    name_col = "板块名称"
    if name_col not in names_df.columns:
        logger.warning("行业名称表缺少「%s」列，不剔除行业板块", name_col)
        return set()

    available = set(names_df[name_col].astype(str).str.strip())

    for bn in boards_cfg:
        bn = str(bn).strip()
        if not bn:
            continue
        if bn not in available:
            logger.warning("配置的行业板块「%s」不在东财行业列表中，跳过该板块", bn)
            continue

        def _fetch_cons() -> pd.DataFrame:
            return ak.stock_board_industry_cons_em(symbol=bn)

        try:
            cons = call_with_retry(
                f"stock_board_industry_cons_em:{bn}",
                _fetch_cons,
                validate=lambda d: isinstance(d, pd.DataFrame),
            )
        except Exception:  # noqa: BLE001
            logger.warning("板块「%s」成份拉取失败，跳过", bn, exc_info=True)
            continue

        if cons.empty or "代码" not in cons.columns:
            logger.warning("板块「%s」成份为空或无「代码」列，跳过", bn)
            continue
        codes = cons["代码"].astype(str).str.zfill(6)
        out.update(codes.tolist())

    logger.info(
        "东财行业剔除：合并代码 %s 只（已解析板块 %s / 配置 %s）",
        len(out),
        len([b for b in boards_cfg if str(b).strip() in available]),
        len(boards_cfg),
    )
    return out


def build_base_universe(as_of: date | None = None) -> pd.DataFrame:
    """
    返回经过「ST / 北交所 / 上市年限」清洗后的行情 DataFrame。

    列至少包含：代码、名称、最新价、市盈率-动态；并附加 ``listing_date``。
    """
    as_of = as_of or date.today()
    cutoff = as_of - timedelta(days=365 * config.LISTING_MIN_YEARS)

    spot = _load_spot_universe()
    listing = _load_listing_dates()[["代码", "上市日期"]].copy()
    listing["代码"] = listing["代码"].astype(str).str.zfill(6)

    df = spot.copy()
    df["代码"] = df["代码"].astype(str).str.zfill(6)

    df = df.merge(listing, on="代码", how="left")

    # --- ST / 名称异常 ---
    mask_st = df["名称"].astype(str).apply(is_st_name)
    n_st = int(mask_st.sum())
    df = df.loc[~mask_st].copy()
    logger.info("剔除 ST 名称股票：%s 只", n_st)

    # --- 北交所等代码段 ---
    mask_board = df["代码"].apply(is_star_or_main_board_a)
    n_bse = int((~mask_board).sum())
    df = df.loc[mask_board].copy()
    logger.info("剔除北交所等代码段：%s 只", n_bse)

    # --- 上市日期：缺失则保守剔除（无法证明已满 5 年）---
    df["listing_date"] = pd.to_datetime(df["上市日期"], errors="coerce").dt.date
    mask_age = df["listing_date"].notna() & (df["listing_date"] <= cutoff)
    n_young = int((~mask_age).sum())
    df = df.loc[mask_age].copy()
    logger.info("剔除上市未满 %s 年或缺失上市日：%s 只", config.LISTING_MIN_YEARS, n_young)

    # --- 金融 / 公用事业等：东财行业板块成份 ---
    ex_codes = _em_finance_util_excluded_codes()
    if ex_codes:
        codes_s = df["代码"].astype(str).str.zfill(6)
        mask_ex = codes_s.isin(ex_codes)
        n_ex = int(mask_ex.sum())
        df = df.loc[~mask_ex].copy()
        logger.info("剔除东财配置行业板块成份股：%s 只", n_ex)

    # --- 数值列 ---
    df["最新价"] = pd.to_numeric(df["最新价"], errors="coerce")
    df["市盈率-动态"] = pd.to_numeric(df["市盈率-动态"], errors="coerce")
    if "总市值" in df.columns:
        df["总市值"] = pd.to_numeric(df["总市值"], errors="coerce")
    if "流通市值" in df.columns:
        df["流通市值"] = pd.to_numeric(df["流通市值"], errors="coerce")
    if "市净率" in df.columns:
        df["市净率"] = pd.to_numeric(df["市净率"], errors="coerce")

    logger.info(
        "基础池构建完成：剩余 %s 只（统计日 %s）",
        len(df),
        as_of.isoformat(),
    )
    return df


def apply_coarse_prefilter(df: pd.DataFrame) -> pd.DataFrame:
    """
    全市场快照粗筛：仅用东财 ``stock_zh_a_spot_em`` 已合并进 ``df`` 的列，无逐股远程请求。

    位于 **基础池之后、质量（财报）初筛之前**，用于先砍掉明显不符合快照条件的标的。
    各条件由 ``config.COARSE_*`` 控制；``COARSE_PREFILTER_ENABLE=False`` 时原样返回。
    """
    if not getattr(config, "COARSE_PREFILTER_ENABLE", True):
        return df.copy()

    n0 = len(df)
    out = df.copy()
    mask = pd.Series(True, index=out.index)

    pe_lo = getattr(config, "COARSE_PE_MIN_EXCL", None)
    if pe_lo is not None:
        pe = pd.to_numeric(out.get("市盈率-动态"), errors="coerce")
        mask &= pe.notna() & (pe > float(pe_lo))

    pe_hi = getattr(config, "COARSE_PE_MAX", None)
    if pe_hi is not None:
        pe = pd.to_numeric(out.get("市盈率-动态"), errors="coerce")
        mask &= pe.notna() & (pe <= float(pe_hi))

    pb_max = getattr(config, "COARSE_PB_MAX", None)
    if pb_max is not None and "市净率" in out.columns:
        pb = pd.to_numeric(out["市净率"], errors="coerce")
        mask &= pb.notna() & (pb > 0) & (pb <= float(pb_max))

    mmin = getattr(config, "COARSE_MIN_TOTAL_MV_YUAN", None)
    if mmin is not None and "总市值" in out.columns:
        mv = pd.to_numeric(out["总市值"], errors="coerce")
        mask &= mv.notna() & (mv >= float(mmin))

    fmin = getattr(config, "COARSE_MIN_FLOAT_MV_YUAN", None)
    if fmin is not None and "流通市值" in out.columns:
        fv = pd.to_numeric(out["流通市值"], errors="coerce")
        mask &= fv.notna() & (fv >= float(fmin))

    px = getattr(config, "COARSE_MIN_PRICE", None)
    if px is not None and "最新价" in out.columns:
        pr = pd.to_numeric(out["最新价"], errors="coerce")
        mask &= pr.notna() & (pr >= float(px))

    res = out.loc[mask].copy()
    logger.info("快照粗筛：%s -> %s 只", n0, len(res))
    return res


def apply_pe_prefilter(df: pd.DataFrame) -> pd.DataFrame:
    """
    估值漏斗（在 **企业质量初筛通过** 之后）：当前 PE(TTM) 在近 5 年可比历史中的分位百分数不超过
    ``config.PE_TTM_5Y_PERCENTILE_MAX``。

    剔除亏损（PE<=0）、缺失、历史样本不足或估值序列拉取失败的标的。
    """
    max_pct = float(getattr(config, "PE_TTM_5Y_PERCENTILE_MAX", 35.0))
    extra_sleep = float(getattr(config, "PE_HIST_INTER_STOCK_SLEEP", 0.0) or 0.0)
    m = float(getattr(config, "REQUEST_THROTTLE_MULTIPLIER", 1.0))

    rows: list[dict] = []
    n = len(df)
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        pe = row.get("市盈率-动态")
        if pd.isna(pe) or float(pe) <= 0:
            continue
        code = str(row["代码"]).zfill(6)
        cur = float(pe)
        try:
            res = percentile_for_stock_baidu(code, cur)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] PE 近五年分位：%s", code, exc)
            res = None
        if res is None:
            continue
        pct, n_pts = res
        if pct > max_pct:
            continue
        d = row.to_dict()
        d["PE近5年分位_pct"] = round(pct, 4)
        d["PE近5年样本数"] = int(n_pts)
        rows.append(d)
        if extra_sleep > 0:
            time.sleep(extra_sleep * m)
        if i % 250 == 0:
            logger.info("PE 分位初筛进度：%s / %s（已保留 %s）", i, n, len(rows))

    out = pd.DataFrame(rows)
    logger.info(
        "PE 分位初筛（分位%%<= %s）：%s -> %s 只",
        max_pct,
        n,
        len(out),
    )
    return out
