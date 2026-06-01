from __future__ import annotations

import io
from datetime import date

import pandas as pd
import streamlit as st

from backtest_core import (
    Benchmark,
    build_rebalances,
    calculate_backtest_with_details,
    export_excel,
    monthly_comparison,
)
from ifind_provider import IFIND_BENCHMARKS, IfindPriceProvider, split_stock_entries
from charting import render_png


st.set_page_config(page_title="金股组合收益率对比工具", layout="wide")


def default_dates() -> tuple[date, date]:
    today = pd.Timestamp.today().normalize()
    return (today - pd.offsets.MonthEnd(1)).date(), today.date()


def next_month_end(value: str) -> str:
    current = pd.Timestamp(value)
    return (current + pd.offsets.MonthBegin(1) + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")


def add_row(effective_date: str) -> None:
    row_id = st.session_state.next_row_id
    st.session_state.next_row_id += 1
    st.session_state.rows.append({"id": row_id, "date": effective_date, "tickers": ""})


def remove_row(index: int) -> None:
    if len(st.session_state.rows) > 1:
        st.session_state.rows.pop(index)


def calculate(
    payload_rows: list[dict[str, str]],
    cutoff_date: date,
    benchmark_entries: list[str],
    refresh_token: str,
    adjustment: str,
) -> tuple[pd.DataFrame, pd.DataFrame, list[Benchmark]]:
    provider = IfindPriceProvider(refresh_token, adjustment=adjustment)
    benchmarks = [
        Benchmark(name, code, "ifind")
        for name, code in (provider.resolve_index_identifier(value) for value in benchmark_entries)
    ]
    rebalances = build_rebalances(
        (row["date"], tuple(provider.resolve_stock_identifier(value) for value in split_stock_entries(row["tickers"])))
        for row in payload_rows
    )
    result, details = calculate_backtest_with_details(
        rebalances,
        cutoff_date,
        benchmarks,
        provider.stock_prices,
        provider.index_prices,
    )
    details.insert(5, "股票名称", details["股票代码"].map(provider.stock_name))
    return result, details, benchmarks


def excel_bytes(result: pd.DataFrame, details: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    export_excel(result, buffer, details, monthly_comparison(result))
    return buffer.getvalue()


base_default, cutoff_default = default_dates()
if "rows" not in st.session_state:
    st.session_state.rows = [{"id": 0, "date": base_default.strftime("%Y-%m-%d"), "tickers": ""}]
if "next_row_id" not in st.session_state:
    st.session_state.next_row_id = 1
if "result" not in st.session_state:
    st.session_state.result = None
if "details" not in st.session_state:
    st.session_state.details = None
if "resolved_benchmarks" not in st.session_state:
    st.session_state.resolved_benchmarks = None
if "benchmarks" not in st.session_state:
    st.session_state.benchmarks = [
        {"id": index, "value": item.name}
        for index, item in enumerate(IFIND_BENCHMARKS)
    ]
if "next_benchmark_id" not in st.session_state:
    st.session_state.next_benchmark_id = len(IFIND_BENCHMARKS)

st.title("金股组合收益率对比工具")
st.caption("每一行组合从对应调仓日收盘后生效。股票池和行情仅用于当前会话计算，不写入数据库。")

with st.container(border=True):
    st.subheader("回测参数")
    column_a, column_b, column_c, column_d = st.columns([1, 1, 1.3, 3])
    base_date = column_a.date_input("基准日（首个调仓日）", value=base_default)
    cutoff_date = column_b.date_input("截止日期", value=cutoff_default)
    adjustment_label = column_c.selectbox("股票价格口径", ["不复权", "前复权", "后复权"], index=1)
    adjustment = {"不复权": "1", "前复权": "2", "后复权": "3"}[adjustment_label]
    column_d.info("默认使用前复权收盘价。图表严格以填写的截止日期为终点。")
    refresh_token = st.text_input(
        "同花顺数据接口 refresh_token（仅在当前会话内存中使用，不写入文件）",
        type="password",
    )

with st.container(border=True):
    st.subheader("基准指数")
    st.caption("可以输入指数名称或同花顺代码。运行后表格列名会显示指数名称；默认保留四个常用基准。")
    if st.button("新增指数"):
        benchmark_id = st.session_state.next_benchmark_id
        st.session_state.next_benchmark_id += 1
        st.session_state.benchmarks.append({"id": benchmark_id, "value": ""})
        st.rerun()
    for index, item in enumerate(st.session_state.benchmarks):
        input_column, delete_column = st.columns([6, 0.7])
        item["value"] = input_column.text_input(
            "指数名称或代码",
            value=item["value"],
            key=f"benchmark_{item['id']}",
            placeholder="例如：沪深300 或 000300.SH",
        )
        if delete_column.button("删除", key=f"delete_benchmark_{item['id']}", disabled=len(st.session_state.benchmarks) == 1):
            st.session_state.benchmarks.pop(index)
            st.rerun()

with st.container(border=True):
    st.subheader("每月金股")
    action_a, action_b, action_c = st.columns([1, 1, 5])
    if action_a.button("新增下个月", use_container_width=True):
        latest_date = max(row["date"] for row in st.session_state.rows)
        add_row(next_month_end(latest_date))
        st.rerun()
    if action_b.button("新增任意调仓日", use_container_width=True):
        add_row(base_date.strftime("%Y-%m-%d"))
        st.rerun()
    action_c.caption("股票代码或股票名称均可用逗号、空格或换行分隔；每个月数量不限。")

    for index, row in enumerate(st.session_state.rows):
        row_id = row["id"]
        date_column, tickers_column, delete_column = st.columns([1, 5, 0.6])
        if index == 0:
            row["date"] = base_date.strftime("%Y-%m-%d")
            date_column.text_input("调仓日（跟随基准日）", value=row["date"], disabled=True)
        else:
            row["date"] = date_column.text_input("调仓日", value=row["date"], key=f"row_date_{row_id}")
        row["tickers"] = tickers_column.text_area(
            "金股代码",
            value=row["tickers"],
            key=f"row_tickers_{row_id}",
            height=68,
            placeholder="例如：000001, 海光信息, 920002",
        )
        if delete_column.button("删除", key=f"delete_{row_id}", disabled=len(st.session_state.rows) == 1):
            remove_row(index)
            st.rerun()

if st.button("运行回测", type="primary"):
    st.session_state.result = None
    st.session_state.details = None
    st.session_state.resolved_benchmarks = None
    try:
        rows = [dict(row) for row in st.session_state.rows]
        earliest_date = min(pd.Timestamp(row["date"]).normalize() for row in rows)
        if earliest_date != pd.Timestamp(base_date).normalize():
            raise ValueError("基准日必须与最早一行调仓日一致。")
        with st.spinner("正在拉取行情并计算，请稍候……"):
            benchmark_entries = [item["value"] for item in st.session_state.benchmarks if item["value"].strip()]
            st.session_state.result, st.session_state.details, resolved_benchmarks = calculate(
                rows,
                cutoff_date,
                benchmark_entries,
                refresh_token,
                adjustment,
            )
            st.session_state.resolved_benchmarks = resolved_benchmarks
        st.success("回测完成。")
    except Exception as exc:
        st.error(str(exc))

result = st.session_state.result
if result is not None:
    st.subheader("累计收益率表")
    st.caption(f"本次股票价格口径：{adjustment_label}")
    display = result.copy()
    display.index = display.index.strftime("%Y-%m-%d")
    st.dataframe(display.style.format("{:+.2%}"), use_container_width=True)

    st.subheader("指数名称与代码")
    st.dataframe(
        pd.DataFrame(
            [{"指数名称": item.name, "同花顺代码": item.symbol} for item in st.session_state.resolved_benchmarks]
        ),
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("月度收益率变化与指数对比")
    st.caption("该表展示每个观察区间内的单月收益率变化，不是累计收益率。")
    st.dataframe(monthly_comparison(result).style.format("{:+.2%}"), use_container_width=True)

    st.subheader("金股逐月贡献明细")
    st.caption("用于复核手工结果：当期组合收益率为该月所有金股区间收益率的等权平均值，再与历史月份链式相乘。")
    details = st.session_state.details.copy()
    details["调仓日"] = details["调仓日"].dt.strftime("%Y-%m-%d")
    for column in ("实际期初交易日", "区间截止日", "实际期末交易日"):
        details[column] = details[column].dt.strftime("%Y-%m-%d")
    st.dataframe(
        details.style.format(
            {
                "期初收盘价": "{:.4f}",
                "期末收盘价": "{:.4f}",
                "个股区间收益率": "{:+.2%}",
                "当期等权组合收益率": "{:+.2%}",
                "期末累计收益率": "{:+.2%}",
            }
        ),
        use_container_width=True,
    )

    st.subheader("累计收益率对比图")
    png = render_png(result)
    st.image(png, use_container_width=True)

    export_a, export_b, _ = st.columns([1, 1, 5])
    export_a.download_button(
        "导出 Excel",
        data=excel_bytes(result, st.session_state.details),
        file_name="gold_stock_backtest.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    export_b.download_button(
        "导出 PNG",
        data=png,
        file_name="gold_stock_comparison.png",
        mime="image/png",
        use_container_width=True,
    )
