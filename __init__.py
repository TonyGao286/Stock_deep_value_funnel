"""
A 股「深度价值 + 宽护城河」漏斗式选股框架。

命令行：``python -m deep_value_funnel.pipeline``；
串联漏斗 → 风险排雷 → 深度分析：项目根目录 ``run_pipeline.py``。
"""

from .pipeline import run_screening, save_funnel_csv

__all__ = ["run_screening", "save_funnel_csv"]
