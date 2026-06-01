from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd


@dataclass(frozen=True)
class Rebalance:
    effective_date: pd.Timestamp
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class Benchmark:
    name: str
    symbol: str
    source: str = "ifind"


def normalize_date(value: str | date | pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(value).normalize()


def normalize_ticker(value: str) -> str:
    ticker = "".join(character for character in str(value).strip() if character.isdigit())
    if len(ticker) != 6:
        raise ValueError(f"股票代码应为 6 位数字，收到：{value!r}")
    return ticker


def parse_tickers(text: str) -> tuple[str, ...]:
    raw_values = text.replace("，", ",").replace("；", ",").replace(";", ",")
    raw_values = raw_values.replace("\n", ",").replace("\t", ",").replace(" ", ",")
    tickers = tuple(dict.fromkeys(normalize_ticker(value) for value in raw_values.split(",") if value.strip()))
    if not tickers:
        raise ValueError("每个调仓日请至少输入一只金股。")
    return tickers


def build_rebalances(rows: Iterable[tuple[str | date | pd.Timestamp, Iterable[str]]]) -> list[Rebalance]:
    rebalances = [
        Rebalance(normalize_date(effective_date), tuple(dict.fromkeys(normalize_ticker(ticker) for ticker in tickers)))
        for effective_date, tickers in rows
    ]
    if not rebalances:
        raise ValueError("请至少添加一个调仓日。")
    if any(not item.tickers for item in rebalances):
        raise ValueError("每个调仓日请至少输入一只金股。")
    rebalances.sort(key=lambda item: item.effective_date)
    if len({item.effective_date for item in rebalances}) != len(rebalances):
        raise ValueError("调仓日不能重复。")
    return rebalances


def month_end_targets(base_date: pd.Timestamp, cutoff_date: pd.Timestamp) -> list[pd.Timestamp]:
    if cutoff_date < base_date:
        raise ValueError("截止日期不能早于基准日。")
    targets = [base_date]
    current = base_date + pd.offsets.MonthEnd(0)
    if current <= base_date:
        current += pd.offsets.MonthEnd(1)
    while current < cutoff_date:
        targets.append(current.normalize())
        current += pd.offsets.MonthEnd(1)
    if cutoff_date not in targets:
        targets.append(cutoff_date)
    return targets


def price_on_or_before(prices: pd.Series, target: pd.Timestamp, label: str) -> float:
    available = prices.loc[:target]
    if available.empty:
        raise ValueError(f"{label} 在 {target:%Y-%m-%d} 当日及之前没有可用收盘价。")
    return float(available.iloc[-1])


def trading_date_on_or_before(prices: pd.Series, target: pd.Timestamp, label: str) -> pd.Timestamp:
    available = prices.loc[:target]
    if available.empty:
        raise ValueError(f"{label} 在 {target:%Y-%m-%d} 当日及之前没有可用交易日。")
    return pd.Timestamp(available.index[-1]).normalize()


def calculate_backtest(
    rebalances: list[Rebalance],
    cutoff_date: str | date | pd.Timestamp,
    benchmarks: Iterable[Benchmark],
    stock_loader: Callable[[str, pd.Timestamp, pd.Timestamp], pd.Series],
    index_loader: Callable[[Benchmark, pd.Timestamp, pd.Timestamp], pd.Series],
) -> pd.DataFrame:
    result, _ = calculate_backtest_with_details(rebalances, cutoff_date, benchmarks, stock_loader, index_loader)
    return result


def calculate_backtest_with_details(
    rebalances: list[Rebalance],
    cutoff_date: str | date | pd.Timestamp,
    benchmarks: Iterable[Benchmark],
    stock_loader: Callable[[str, pd.Timestamp, pd.Timestamp], pd.Series],
    index_loader: Callable[[Benchmark, pd.Timestamp, pd.Timestamp], pd.Series],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not rebalances:
        raise ValueError("请至少添加一个调仓日。")
    rebalances = sorted(rebalances, key=lambda item: item.effective_date)
    base_date = rebalances[0].effective_date
    cutoff = normalize_date(cutoff_date)
    if cutoff < base_date:
        raise ValueError("截止日期不能早于基准日。")
    active_rebalances = [item for item in rebalances if item.effective_date <= cutoff]
    earliest_fetch = base_date - pd.Timedelta(days=14)
    benchmarks = tuple(benchmarks)
    benchmark_prices = {benchmark.name: index_loader(benchmark, earliest_fetch, cutoff) for benchmark in benchmarks}
    first_benchmark = next(iter(benchmark_prices.items()), None)
    if first_benchmark is None:
        evaluation_dates = month_end_targets(base_date, cutoff)
    else:
        name, prices = first_benchmark
        evaluation_dates = list(
            dict.fromkeys(trading_date_on_or_before(prices, target, name) for target in month_end_targets(base_date, cutoff))
        )
    targets = sorted(set(evaluation_dates) | {item.effective_date for item in active_rebalances})
    portfolio_value = 1.0
    portfolio_at_dates: dict[pd.Timestamp, float] = {base_date: portfolio_value}
    if evaluation_dates:
        portfolio_at_dates[evaluation_dates[0]] = portfolio_value
    stock_cache: dict[str, pd.Series] = {}
    details: list[dict[str, object]] = []
    for index, rebalance in enumerate(active_rebalances):
        interval_end = cutoff if index + 1 == len(active_rebalances) else active_rebalances[index + 1].effective_date
        interval_targets = [target for target in targets if rebalance.effective_date < target <= interval_end]
        if not interval_targets:
            continue
        for ticker in rebalance.tickers:
            if ticker not in stock_cache:
                stock_cache[ticker] = stock_loader(ticker, earliest_fetch, cutoff)
        start_prices = {ticker: price_on_or_before(stock_cache[ticker], rebalance.effective_date, ticker) for ticker in rebalance.tickers}
        for target in interval_targets:
            gross_returns = [price_on_or_before(stock_cache[ticker], target, ticker) / start_prices[ticker] for ticker in rebalance.tickers]
            portfolio_at_dates[target] = portfolio_value * sum(gross_returns) / len(gross_returns)
        interval_return = portfolio_at_dates[interval_end] / portfolio_value - 1.0
        for ticker in rebalance.tickers:
            end_price = price_on_or_before(stock_cache[ticker], interval_end, ticker)
            details.append(
                {
                    "调仓日": rebalance.effective_date,
                    "实际期初交易日": trading_date_on_or_before(stock_cache[ticker], rebalance.effective_date, ticker),
                    "区间截止日": interval_end,
                    "实际期末交易日": trading_date_on_or_before(stock_cache[ticker], interval_end, ticker),
                    "股票代码": ticker,
                    "期初收盘价": start_prices[ticker],
                    "期末收盘价": end_price,
                    "个股区间收益率": end_price / start_prices[ticker] - 1.0,
                    "当期等权组合收益率": interval_return,
                    "期末累计收益率": portfolio_at_dates[interval_end] - 1.0,
                }
            )
        portfolio_value = portfolio_at_dates[interval_end]
    result = pd.DataFrame(index=evaluation_dates)
    result.index.name = "日期"
    result["国投计算机金股"] = [portfolio_at_dates[target] - 1.0 for target in evaluation_dates]
    for benchmark in benchmarks:
        prices = benchmark_prices[benchmark.name]
        base_price = price_on_or_before(prices, base_date, benchmark.name)
        result[benchmark.name] = [price_on_or_before(prices, target, benchmark.name) / base_price - 1.0 for target in evaluation_dates]
    return result, pd.DataFrame(details)


def export_excel(
    result: pd.DataFrame,
    path: str | Path,
    details: pd.DataFrame | None = None,
    monthly: pd.DataFrame | None = None,
) -> None:
    export = result.copy()
    export.index = export.index.strftime("%Y-%m-%d")
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        export.to_excel(writer, sheet_name="累计收益率")
        worksheet = writer.sheets["累计收益率"]
        for row in worksheet.iter_rows(min_row=2, min_col=2):
            for cell in row:
                cell.number_format = "0.00%"
        worksheet.column_dimensions["A"].width = 14
        for column in "BCDEFGHIJ":
            worksheet.column_dimensions[column].width = 18
        if monthly is not None and not monthly.empty:
            monthly.to_excel(writer, sheet_name="月度收益率变化")
            monthly_worksheet = writer.sheets["月度收益率变化"]
            for row in monthly_worksheet.iter_rows(min_row=2, min_col=2):
                for cell in row:
                    cell.number_format = "0.00%"
            monthly_worksheet.column_dimensions["A"].width = 14
            for column in "BCDEFGHIJ":
                monthly_worksheet.column_dimensions[column].width = 18
        if details is not None and not details.empty:
            detail_export = details.copy()
            for column in ("调仓日", "实际期初交易日", "区间截止日", "实际期末交易日"):
                detail_export[column] = detail_export[column].dt.strftime("%Y-%m-%d")
            detail_export.to_excel(writer, sheet_name="金股逐月贡献明细", index=False)
            detail_worksheet = writer.sheets["金股逐月贡献明细"]
            for column in ("I", "J", "K"):
                for cell in detail_worksheet[column][1:]:
                    cell.number_format = "0.00%"
            for column in "ABCDEFGHIJK":
                detail_worksheet.column_dimensions[column].width = 18


def monthly_comparison(result: pd.DataFrame) -> pd.DataFrame:
    comparison = (1.0 + result).pct_change()
    comparison.iloc[0] = result.iloc[0]
    comparison.index = comparison.index.strftime("%Y-%m-%d")
    comparison.index.name = "日期"
    return comparison
