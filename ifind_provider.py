from __future__ import annotations

import re
from typing import Iterable

import pandas as pd
import requests

from backtest_core import Benchmark


IFIND_BENCHMARKS = (
    Benchmark("沪深300", "000300.SH", "ifind"),
    Benchmark("计算机（申万）", "801750.SL", "ifind"),
    Benchmark("科创50", "000688.SH", "ifind"),
    Benchmark("创业板指", "399006.SZ", "ifind"),
)
INDEX_CATALOG = {benchmark.name: benchmark.symbol for benchmark in IFIND_BENCHMARKS}
INDEX_CATALOG.update(
    {
        "上证指数": "000001.SH",
        "深证成指": "399001.SZ",
        "中证500": "000905.SH",
        "中证1000": "000852.SH",
        "北证50": "899050.BJ",
    }
)


def split_stock_entries(text: str) -> tuple[str, ...]:
    values = re.split(r"[,，;；\s]+", text.strip())
    entries = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not entries:
        raise ValueError("每个调仓日请至少输入一只金股。")
    return entries


class IfindPriceProvider:
    API_ROOT = "https://quantapi.51ifind.com/api/v1"

    def __init__(self, refresh_token: str, adjustment: str = "1") -> None:
        self.refresh_token = refresh_token.strip()
        if not self.refresh_token:
            raise ValueError("请填写同花顺数据接口 refresh_token。")
        self.session = requests.Session()
        self.session.trust_env = False
        if adjustment not in {"1", "2", "3"}:
            raise ValueError(f"不支持的复权参数：{adjustment}")
        self.adjustment = adjustment
        self._access_token = ""
        self._stock_cache: dict[tuple[str, pd.Timestamp, pd.Timestamp], pd.Series] = {}
        self._index_cache: dict[tuple[str, pd.Timestamp, pd.Timestamp], pd.Series] = {}
        self._name_cache: dict[str, str] = {}
        self._security_name_cache: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        if not self._access_token:
            response = self.session.post(
                f"{self.API_ROOT}/get_access_token",
                headers={"Content-Type": "application/json", "refresh_token": self.refresh_token},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            self._access_token = (payload.get("data") or {}).get("access_token", "")
            if not self._access_token:
                raise RuntimeError(payload.get("message") or payload.get("msg") or "同花顺 refresh_token 无效或没有数据接口权限。")
        return {"Content-Type": "application/json", "access_token": self._access_token, "ifindlang": "cn"}

    def _post(self, endpoint: str, body: dict) -> dict:
        response = self.session.post(f"{self.API_ROOT}/{endpoint}", json=body, headers=self._headers(), timeout=30)
        response.raise_for_status()
        payload = response.json()
        error_code = payload.get("errorcode", payload.get("code", 0))
        if error_code not in (None, 0, "0"):
            raise RuntimeError(payload.get("errmsg") or payload.get("message") or payload.get("msg") or str(payload))
        return payload

    def stock_prices(self, ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
        code = self._stock_code(ticker)
        key = (code, start, end)
        if key not in self._stock_cache:
            self._stock_cache[key] = self._history_prices(code, start, end)
        return self._stock_cache[key]

    def index_prices(self, benchmark: Benchmark, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
        key = (benchmark.symbol, start, end)
        if key not in self._index_cache:
            self._index_cache[key] = self._history_prices(benchmark.symbol, start, end)
        return self._index_cache[key]

    def resolve_index_identifier(self, value: str) -> tuple[str, str]:
        value = value.strip()
        if not value:
            raise ValueError("指数名称或代码不能为空。")
        if value in INDEX_CATALOG:
            return value, INDEX_CATALOG[value]
        normalized = value.upper()
        if normalized in INDEX_CATALOG.values():
            name = next((name for name, code in INDEX_CATALOG.items() if code == normalized), normalized)
            return name, normalized
        if re.match(r"^\d{6}\.(SH|SZ|BJ|SL)$", normalized):
            return self.security_name(normalized), normalized
        payload = self._post("smart_stock_picking", {"searchstring": value, "searchtype": "index"})
        for record in self._walk_records(payload):
            code = self._record_code(record)
            name = self._record_name(record)
            if code and name == value:
                return name, code
        raise ValueError(f"无法识别指数：{value}。请输入指数名称或同花顺代码，例如 沪深300 或 000300.SH。")

    def resolve_stock_identifier(self, value: str) -> str:
        value = value.strip()
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) == 6:
            return digits
        if value in self._name_cache:
            return self._name_cache[value]
        payload = self._post("smart_stock_picking", {"searchstring": value, "searchtype": "stock"})
        for record in self._walk_records(payload):
            code = self._record_code(record)
            name = self._record_name(record)
            if code and name == value:
                ticker = code.split(".")[0]
                self._name_cache[value] = ticker
                return ticker
        codes = tuple(dict.fromkeys(self._walk_codes(payload)))
        if len(codes) == 1:
            ticker = codes[0].split(".")[0]
            self._name_cache[value] = ticker
            return ticker
        raise ValueError(f"无法通过同花顺识别股票名称：{value}。可以改为输入 6 位股票代码。")

    def _history_prices(self, code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
        payload = self._post(
            "cmd_history_quotation",
            {
                "codes": code,
                "indicators": "close",
                "startdate": start.strftime("%Y-%m-%d"),
                "enddate": end.strftime("%Y-%m-%d"),
                "functionpara": {
                    "Interval": "D",
                    "CPS": self.adjustment,
                    "baseDate": "1900-01-01",
                    "Currency": "YSHB",
                    "fill": "Previous",
                },
            },
        )
        dates, closes = self._extract_history(payload)
        series = pd.Series(pd.to_numeric(closes, errors="coerce"), index=pd.to_datetime(dates, errors="coerce"), dtype="float64")
        series = series.dropna()
        series = series[~series.index.isna()]
        series.index = series.index.normalize()
        series = series[~series.index.duplicated(keep="last")].sort_index()
        if series.empty:
            raise ValueError(f"{code} 在所选区间没有同花顺历史行情。")
        return series

    def security_name(self, code: str) -> str:
        code = code.upper()
        if code in self._security_name_cache:
            return self._security_name_cache[code]
        payload = self._post(
            "basic_data_service",
            {"codes": code, "indipara": [{"indicator": "ths_stock_short_name_stock"}]},
        )
        name = self._extract_first_text(payload)
        if not name:
            raise ValueError(f"同花顺未返回证券名称：{code}")
        self._security_name_cache[code] = name
        return name

    def stock_name(self, ticker: str) -> str:
        return self.security_name(self._stock_code(ticker))

    @staticmethod
    def _extract_history(payload: dict) -> tuple[list, list]:
        tables = payload.get("tables") or (payload.get("data") or {}).get("tables") or []
        if not tables:
            raise ValueError("同花顺历史行情接口未返回数据。")
        table = tables[0]
        values = table.get("table") or table
        dates = values.get("time") or values.get("date") or table.get("time") or table.get("date")
        closes = values.get("close") or table.get("close")
        if not dates or closes is None:
            raise ValueError(f"无法识别同花顺历史行情字段：{list(values)}")
        return list(dates), list(closes)

    @staticmethod
    def _stock_code(ticker: str) -> str:
        if ticker.startswith(("60", "68")):
            return f"{ticker}.SH"
        if ticker.startswith(("00", "30")):
            return f"{ticker}.SZ"
        if ticker.startswith(("4", "8", "92")):
            return f"{ticker}.BJ"
        raise ValueError(f"无法判断股票交易所：{ticker}")

    @classmethod
    def _walk_records(cls, value) -> Iterable[dict]:
        if isinstance(value, dict):
            yield value
            list_columns = {
                key: nested
                for key, nested in value.items()
                if isinstance(nested, list)
            }
            if list_columns:
                lengths = {len(nested) for nested in list_columns.values()}
                if len(lengths) == 1:
                    for index in range(next(iter(lengths))):
                        yield {key: nested[index] for key, nested in list_columns.items()}
            for nested in value.values():
                yield from cls._walk_records(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from cls._walk_records(nested)

    @staticmethod
    def _record_code(record: dict) -> str:
        for key in ("thscode", "thsCode", "code", "证券代码", "股票代码"):
            value = record.get(key)
            if isinstance(value, str) and re.match(r"^\d{6}\.(SH|SZ|BJ|SL)$", value):
                return value
        return ""

    @staticmethod
    def _record_name(record: dict) -> str:
        for key in ("security_name", "secName", "name", "证券简称", "股票简称"):
            value = record.get(key)
            if isinstance(value, str):
                return value.strip()
        return ""

    @classmethod
    def _extract_first_text(cls, value) -> str:
        if isinstance(value, dict):
            for key in ("ths_stock_short_name_stock", "security_name", "secName", "name", "股票简称"):
                nested = value.get(key)
                result = cls._first_string(nested)
                if result:
                    return result
            for nested in value.values():
                result = cls._extract_first_text(nested)
                if result:
                    return result
        elif isinstance(value, list):
            for nested in value:
                result = cls._extract_first_text(nested)
                if result:
                    return result
        return ""

    @staticmethod
    def _first_string(value) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            for nested in value:
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return ""

    @classmethod
    def _walk_codes(cls, value) -> Iterable[str]:
        if isinstance(value, str):
            normalized = value.upper()
            if re.match(r"^\d{6}\.(SH|SZ|BJ|SL)$", normalized):
                yield normalized
        elif isinstance(value, dict):
            for nested in value.values():
                yield from cls._walk_codes(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from cls._walk_codes(nested)
