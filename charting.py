from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import io

import pandas as pd


COLORS = {
    "国投计算机金股": "#c00000",
    "沪深300": "#808080",
    "计算机（申万）": "#45a4c2",
    "科创50": "#06255f",
    "创业板指": "#f8cf70",
}


def chart_result(result: pd.DataFrame) -> pd.DataFrame:
    if len(result.index) >= 2 and (result.index[-1] - result.index[-2]).days < 10:
        return result.drop(index=result.index[-2])
    return result


@lru_cache(maxsize=1)
def configure_chinese_font() -> str:
    from matplotlib import font_manager, rcParams

    candidate_files = [
        *Path("/usr/share/fonts").rglob("*NotoSansCJK*.ttc"),
        *Path("/usr/share/fonts").rglob("*NotoSansCJK*.otf"),
        *Path("/usr/share/fonts").rglob("*NotoSansSC*.otf"),
        *Path("/usr/share/fonts").rglob("*NotoSansSC*.ttf"),
    ]
    for path in candidate_files:
        try:
            font_manager.fontManager.addfont(str(path))
            family = font_manager.FontProperties(fname=str(path)).get_name()
            rcParams["font.family"] = "sans-serif"
            rcParams["font.sans-serif"] = [family, "DejaVu Sans"]
            rcParams["axes.unicode_minus"] = False
            return family
        except Exception:
            continue
    rcParams["font.family"] = "sans-serif"
    rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "Noto Sans CJK SC", "Microsoft YaHei", "SimHei", "DejaVu Sans"]
    rcParams["axes.unicode_minus"] = False
    return rcParams["font.sans-serif"][0]


def render_png(result: pd.DataFrame) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.dates import date2num
    from matplotlib.lines import Line2D
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path
    from matplotlib.ticker import PercentFormatter

    configure_chinese_font()
    plot_result = chart_result(result)
    figure = plt.figure(figsize=(15, 6.5), dpi=150)
    axis = figure.add_axes([0.06, 0.14, 0.64, 0.73])
    summary = figure.add_axes([0.75, 0.43, 0.23, 0.42])
    legend_handles = []
    for name in plot_result.columns:
        color = COLORS.get(name)
        x_values = date2num(plot_result.index.to_pydatetime())
        y_values = plot_result[name].to_numpy()
        if len(x_values) > 1:
            vertices = [(x_values[0], y_values[0])]
            codes = [Path.MOVETO]
            for index in range(len(x_values) - 1):
                x0, y0 = x_values[index], y_values[index]
                x1, y1 = x_values[index + 1], y_values[index + 1]
                previous_y = y_values[index - 1] if index > 0 else y0
                next_y = y_values[index + 2] if index + 2 < len(y_values) else y1
                vertices.extend(
                    [
                        (x0 + (x1 - x0) / 3, y0 + (y1 - previous_y) / 6),
                        (x1 - (x1 - x0) / 3, y1 - (next_y - y0) / 6),
                        (x1, y1),
                    ]
                )
                codes.extend([Path.CURVE4, Path.CURVE4, Path.CURVE4])
            axis.add_patch(PathPatch(Path(vertices, codes), fill=False, color=color, linewidth=3))
        axis.plot(plot_result.index, plot_result[name], color=color, marker="o", linestyle="none", markersize=6)
        legend_handles.append(Line2D([0], [0], color=color, marker="o", linewidth=3, markersize=6, label=name))
    axis.axhline(0, color="black", linewidth=0.8)
    axis.yaxis.set_major_formatter(PercentFormatter(1))
    axis.grid(axis="y", alpha=0.2)
    axis.legend(handles=legend_handles, loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=5, frameon=False)
    tick_dates = [
        value
        for index, value in enumerate(plot_result.index)
        if index == len(plot_result.index) - 1 or (plot_result.index[index + 1] - value).days >= 10
    ]
    axis.set_xticks(tick_dates)
    axis.set_xticklabels([value.strftime("%Y/%m/%d") for value in tick_dates], fontsize=9, rotation=25, ha="right")
    summary.set_xticks([])
    summary.set_yticks([])
    for spine in summary.spines.values():
        spine.set_color("#d00000")
        spine.set_linestyle((0, (3, 3)))
        spine.set_linewidth(1.5)
    summary.text(0.07, 0.86, "区间累计收益率", fontsize=15, weight="bold")
    for index, (name, value) in enumerate(plot_result.iloc[-1].items()):
        summary.text(0.08, 0.69 - index * 0.14, f"• {name}", fontsize=12)
        summary.text(0.95, 0.69 - index * 0.14, f"{value:+.2%}", fontsize=12, color="#d00000", ha="right")
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(figure)
    return buffer.getvalue()
