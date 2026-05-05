"""
总控流水线：串联「基础池 → **快照粗筛（spot）** → **企业质量（财报）** → **PE 近五年分位**
→ 分红 → 日K/回撤」，并输出 CSV。

先用 ``apply_coarse_prefilter`` 压缩全市场（无逐股财报），再 ``screen_financials``，再百度 PE 分位。
**日 K** 仍在 **分红** 之后。控制台日志使用标准 ``logging``。
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from deep_value_funnel import config
from deep_value_funnel.export_artifacts import (
    build_step2_export_rows,
    save_step1_basic_pool,
    save_step1_comparison,
    save_step2_finance_pool,
)
from deep_value_funnel.request_identity import ensure_request_identity
from deep_value_funnel.stage_dividend import screen_dividend
from deep_value_funnel.stage_financial import screen_financials
from deep_value_funnel.stage_market import screen_drawdown_stage
from deep_value_funnel.symbols import to_em_sec_code
from deep_value_funnel.universe import apply_coarse_prefilter, apply_pe_prefilter, build_base_universe

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def run_screening(
    *,
    max_hist: int | None = None,
    max_deep: int | None = None,
    drawdown_min: float | None = None,
    verbose: bool = False,
    artifact_dir: Path | None = None,
) -> pd.DataFrame:
    """
    执行完整筛选并返回结果表（可能为空表）。

    :param max_hist: 覆盖 ``config.MAX_HIST_CANDIDATES``（分红通过后、进入 K 线前截断）。
    :param max_deep: 覆盖 ``config.MAX_DEEP_CANDIDATES``（质量初筛通过后、进入 PE 分位前截断）。
    :param drawdown_min: 覆盖 ``config.DRAWDOWN_MIN``（小数，如 0.25～0.30）；``None`` 表示沿用已加载的
        ``config`` / 环境变量 ``AK_DRAWDOWN_MIN``。
    :param artifact_dir: 中间态 ``step1_*.csv`` / ``step2_*.csv`` 输出目录；默认当前工作目录。
    """
    _setup_logging(verbose)
    ensure_request_identity()
    if max_hist is not None:
        config.MAX_HIST_CANDIDATES = max_hist
    if max_deep is not None:
        config.MAX_DEEP_CANDIDATES = max_deep
    if drawdown_min is not None:
        config.DRAWDOWN_MIN = config.clamp_drawdown_min(float(drawdown_min))

    out_dir = (artifact_dir or Path.cwd()).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    base = build_base_universe()
    base = apply_coarse_prefilter(base)
    if base.empty:
        logger.warning("快照粗筛后无标的，提前结束。")
        save_step1_basic_pool(pd.DataFrame(), out_dir / "step1_basic_pool.csv")
        return pd.DataFrame()

    quality_pairs: list[tuple[dict, pd.Series]] = []
    n_base = len(base)
    for i, (_, row) in enumerate(base.iterrows(), start=1):
        code = str(row["代码"]).zfill(6)
        logger.info("质量初筛 [%s/%s] %s %s …", i, n_base, code, row.get("名称", ""))
        row_ext = row.copy()
        row_ext["sec_code"] = to_em_sec_code(code)
        fin = screen_financials(row_ext)
        if fin is None:
            continue
        quality_pairs.append((fin, row.copy()))

    if not quality_pairs:
        logger.warning("质量初筛后无标的，提前结束（不会请求 PE 分位与分红、日 K）。")
        save_step1_basic_pool(pd.DataFrame(), out_dir / "step1_basic_pool.csv")
        return pd.DataFrame()

    if config.MAX_DEEP_CANDIDATES is not None:
        quality_pairs = quality_pairs[: config.MAX_DEEP_CANDIDATES]
        logger.info(
            "调试模式：PE 分位仅处理质量初筛后的前 %s 只股票",
            config.MAX_DEEP_CANDIDATES,
        )

    df_pre_pe = pd.DataFrame([sr.to_dict() for _, sr in quality_pairs])
    pe_pool = apply_pe_prefilter(df_pre_pe)

    fin_by_code = {str(f["代码"]).zfill(6): f for f, _ in quality_pairs}
    fin_passed: list[dict] = []
    for _, prow in pe_pool.iterrows():
        c = str(prow["代码"]).zfill(6)
        fin_passed.append(fin_by_code[c])

    save_step1_basic_pool(pe_pool, out_dir / "step1_basic_pool.csv")

    # ── Tushare 副源：构建基础池并与 AKShare 对照 ─────────────────────────────
    _run_tushare_comparison(pe_pool, out_dir)

    step2_rows = build_step2_export_rows(fin_passed)
    save_step2_finance_pool(step2_rows, out_dir / "step2_finance_pool.csv")

    if not fin_passed:
        logger.warning("PE 分位后无标的，提前结束（不会请求分红与日 K）。")
        return pd.DataFrame()

    div_passed: list[dict] = []
    total_fin = len(fin_passed)
    for i, fin in enumerate(fin_passed, start=1):
        code = str(fin["代码"]).zfill(6)
        logger.info("分红漏斗 [%s/%s] %s %s …", i, total_fin, code, fin.get("名称", ""))
        res = screen_dividend(fin)
        if res is not None:
            div_passed.append(res)
        if i < total_fin and float(getattr(config, "DIV_INTER_STOCK_SLEEP", 0.0) or 0.0) > 0:
            m = float(getattr(config, "REQUEST_THROTTLE_MULTIPLIER", 1.0))
            gap = (
                float(config.DIV_INTER_STOCK_SLEEP) + random.uniform(0.0, 0.35)
            ) * m
            time.sleep(gap)

    if not div_passed:
        logger.warning("分红过滤后无标的，提前结束（不会请求日 K）。")
        return pd.DataFrame()

    draw_ok = screen_drawdown_stage(div_passed)
    if not draw_ok:
        logger.warning("日 K / 回撤过滤后无标的，提前结束。")
        return pd.DataFrame()

    return pd.DataFrame(draw_ok)


def _run_tushare_comparison(ak_pe_pool: pd.DataFrame, out_dir: Path) -> None:
    """
    可选副流程：用 Tushare Pro 构建基础池 + PE 分位初筛，并与 AKShare 结果对照。

    注意：AKShare 主流程的 ``step1_basic_pool`` 已是 **质量初筛 + PE 分位** 后的池子；
    Tushare 侧仍为「基础池 + PE 分位」**不含**与 AK 相同的质量条件，对照仅供数据源差异参考。

    若 ``config.ENABLE_TUSHARE_COMPARE`` 为 False 或 token 为空，则跳过并打印提示。
    生成文件：
    - ``step1_tushare_pool.csv``：Tushare 版 PE 初筛结果。
    - ``step1_comparison.csv``  ：双源对照表（并集 + 入选标记 + PE 差值）。
    """
    if not getattr(config, "ENABLE_TUSHARE_COMPARE", False):
        logger.info("Tushare 对照已关闭（ENABLE_TUSHARE_COMPARE=False），跳过。")
        return
    token = getattr(config, "TUSHARE_TOKEN", "")
    if not token:
        logger.warning(
            "未检测到 TUSHARE_TOKEN，跳过 Tushare 对照。"
            "请在 config.py 或环境变量中设置 TUSHARE_TOKEN。"
        )
        return

    try:
        from deep_value_funnel.universe_tushare import (  # noqa: PLC0415
            apply_pe_prefilter_tushare,
            build_tushare_universe,
        )

        logger.info("═══ Tushare 副源：开始拉取基础池 ═══")
        ts_base = build_tushare_universe()
        ts_pe_pool = apply_pe_prefilter_tushare(ts_base)

        # 落盘 Tushare 版 step1
        save_step1_basic_pool(ts_pe_pool, out_dir / "step1_tushare_pool.csv")

        # 双源对照
        cmp = save_step1_comparison(ak_pe_pool, ts_pe_pool, out_dir / "step1_comparison.csv")

        # 控制台摘要
        n_both = int(cmp["双源均入选"].sum())
        n_ak_only = int(cmp["仅AK入选"].sum())
        n_ts_only = int(cmp["仅TS入选"].sum())
        print(
            f"\n── step1 双源 PE 初筛对照 ──\n"
            f"  双源均入选（共识）：{n_both} 只\n"
            f"  仅 AKShare 入选  ：{n_ak_only} 只\n"
            f"  仅 Tushare 入选  ：{n_ts_only} 只\n"
            f"  对照表已写入：{out_dir / 'step1_comparison.csv'}\n"
        )
        logger.info("═══ Tushare 副源：对照完成 ═══")

    except Exception as exc:  # noqa: BLE001
        logger.warning("Tushare 对照流程失败（不影响主筛选）：%s", exc)


def _default_out_path() -> Path:
    today = date.today().isoformat().replace("-", "")
    return Path(f"deep_value_pool_{today}.csv")


def save_funnel_csv(df: pd.DataFrame, out_path: Path, *, print_preview: bool = True) -> None:
    """
    将漏斗筛选结果写入 CSV（与 CLI 行为一致：列改名、去掉内部字段、空表仍写表头）。
    """
    out_path = Path(out_path).resolve()
    empty_cols = [
        "代码",
        "名称",
        "市盈率-动态",
        "最新价",
        "总市值_快照",
        "自由现金流收益率_年报_pct",
        "现金流收益率口径",
        "十年累计留存_元",
        "留存市值创造比_10年",
        "回撤幅度_pct",
        "销售毛利率_最近一期pct",
        "ROE加权_五年平均_pct",
        "ROE加权_最近一期_pct",
        "资产负债率_最近一期_pct",
        "经营现金流净额_净利润比_最近一期",
        "股息率_pct",
        "近三年平均分红率_pct",
        "分红条件说明",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if df.empty:
        logger.warning("最终无完全符合条件的股票；仍写入空表头文件：%s", out_path)
        pd.DataFrame(columns=empty_cols).to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"未筛出标的，已生成空文件：{out_path.resolve()}")
        return

    df = df.copy()
    df["回撤幅度_pct"] = (pd.to_numeric(df["drawdown"], errors="coerce") * 100).round(3)
    df["股息率_pct"] = (pd.to_numeric(df["股息率_东财最近实施_小数"], errors="coerce") * 100).round(
        3
    )

    show_cols = [
        "代码",
        "名称",
        "市盈率-动态",
        "自由现金流收益率_年报_pct",
        "现金流收益率口径",
        "留存市值创造比_10年",
        "销售毛利率_最近一期pct",
        "ROE加权_五年平均_pct",
        "ROE加权_最近一期_pct",
        "资产负债率_最近一期_pct",
        "回撤幅度_pct",
        "股息率_pct",
        "近三年平均分红率_pct",
    ]
    cols = [c for c in show_cols if c in df.columns]
    if print_preview and cols:
        print(df[cols].to_string(index=False))

    dfc = df.drop(columns=[c for c in ("drawdown", "sec_code") if c in df.columns], errors="ignore")
    dfc.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n已保存：{out_path.resolve()}  （共 {len(df)} 行）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A 股深度价值 + 宽护城河漏斗选股")
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=str(_default_out_path()),
        help="输出 CSV 路径（默认 deep_value_pool_YYYYMMDD.csv）",
    )
    parser.add_argument(
        "--max-hist",
        type=int,
        default=None,
        help="限制「分红通过后」进入日 K/回撤阶段的最大数量（调试）",
    )
    parser.add_argument(
        "--max-deep",
        type=int,
        default=None,
        help="限制「质量初筛后」进入 PE 分位阶段的最大数量（调试）",
    )
    parser.add_argument(
        "--drawdown-min",
        type=float,
        default=None,
        metavar="DD",
        help="覆盖近 250 日最大回撤下限（小数，如 0.25～0.30）；默认读 config 与 AK_DRAWDOWN_MIN",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    args = parser.parse_args(argv)

    out_path = Path(args.output).resolve()
    artifact_dir = out_path.parent

    df = run_screening(
        max_hist=args.max_hist,
        max_deep=args.max_deep,
        drawdown_min=args.drawdown_min,
        verbose=args.verbose,
        artifact_dir=artifact_dir,
    )

    for step_name in (
        "step1_basic_pool.csv",
        "step1_tushare_pool.csv",
        "step1_comparison.csv",
        "step2_finance_pool.csv",
    ):
        p = artifact_dir / step_name
        if p.exists():
            print(f"中间态已写入：{p}")

    save_funnel_csv(df, out_path, print_preview=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
