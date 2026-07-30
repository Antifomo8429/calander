#!/usr/bin/env python3
"""台股交易日曆：用證交所「市場開休市日期」推算交易日。

資料來源：
  https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json

證交所這支 API 只會回傳「目前公告中的那一年」，忽略 queryYear 參數，
所以每次抓到的年份都存進 scripts/market_holidays.json 累積起來，
跨年時舊資料才不會不見。

主要用途：算「拆解日」= 掛牌日之後的第 6 個交易日。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

HOLIDAY_API = (
    "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json"
)
CACHE_PATH = Path(__file__).parent / "market_holidays.json"

# 這些名稱代表「當天有交易」，其餘出現在表上的日期一律視為休市。
_OPEN_MARKERS = ("開始交易", "最後交易")

_USER_AGENT = (
    "Mozilla/5.0 (compatible; twse-auction-calendar/1.0; +https://www.twse.com.tw/)"
)


def _load_cache() -> dict[str, list[str]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): list(v) for k, v in data.get("closed_days", {}).items()}


def _save_cache(by_year: dict[str, list[str]]) -> None:
    payload = {
        "_comment": "證交所市場開休市日期快取，key 為民國年，value 為該年休市日（不含例假日）",
        "closed_days": {y: sorted(set(d)) for y, d in sorted(by_year.items())},
    }
    CACHE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _fetch_holidays() -> tuple[str, list[str]] | None:
    """回傳 (民國年, 休市日列表)，抓不到時回傳 None。"""
    request = Request(HOLIDAY_API, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"[trading_calendar] 無法取得休市日曆：{exc}")
        return None

    if str(payload.get("stat", "")).lower() != "ok":
        return None

    title = str(payload.get("title", ""))
    match = re.search(r"(\d{3})\s*年", title)
    if not match:
        return None
    roc_year = match.group(1)

    closed: list[str] = []
    for row in payload.get("data") or []:
        if not row:
            continue
        day = str(row[0]).strip()
        name = str(row[1]).strip() if len(row) > 1 else ""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        if any(marker in name for marker in _OPEN_MARKERS):
            continue
        closed.append(day)

    return roc_year, closed


class TradingCalendar:
    """交易日判斷。covered_years 之外的日期只能靠「排除週末」推估。"""

    def __init__(self, closed_by_year: dict[str, list[str]]) -> None:
        self.closed_by_year = closed_by_year
        self.closed: set[str] = {d for days in closed_by_year.values() for d in days}
        self.covered_years: set[int] = {int(y) + 1911 for y in closed_by_year}

    def is_trading_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return day.isoformat() not in self.closed

    def is_covered(self, day: date) -> bool:
        return day.year in self.covered_years

    def add_trading_days(self, start: date, count: int) -> tuple[date, bool]:
        """回傳 (start 之後第 count 個交易日, 是否為推估值)。

        推估值代表過程中有日期落在休市日曆涵蓋範圍外（例如隔年行事曆還沒公告），
        當下只能用「排除週末」計算，遇到國定假日會偏晚。
        """
        current = start
        remaining = count
        estimated = False
        # 上限純粹是防呆，正常情況 6 個交易日不會走超過 30 天。
        for _ in range(count * 10 + 30):
            current += timedelta(days=1)
            if not self.is_covered(current):
                estimated = True
            if self.is_trading_day(current):
                remaining -= 1
                if remaining == 0:
                    return current, estimated
        raise RuntimeError(f"無法從 {start} 推算第 {count} 個交易日")

    def nth_trading_day(self, start: date, n: int) -> tuple[date, bool]:
        """把 start 當成第 1 個交易日，回傳 (第 n 個交易日, 是否為推估值)。

        start 本身若不是交易日（理論上掛牌日一定是），就從它之後的第一個
        交易日開始算第 1 天。
        """
        if n < 1:
            raise ValueError("n 必須 >= 1")
        estimated = not self.is_covered(start)
        base = start
        if not self.is_trading_day(base):
            base, shifted = self.add_trading_days(base, 1)
            estimated = estimated or shifted
        if n == 1:
            return base, estimated
        result, shifted = self.add_trading_days(base, n - 1)
        return result, estimated or shifted


def load_calendar(refresh: bool = True) -> TradingCalendar:
    """讀取快取；refresh=True 時順便向證交所更新目前公告年度。"""
    by_year = _load_cache()
    if refresh:
        fetched = _fetch_holidays()
        if fetched:
            roc_year, closed = fetched
            by_year[roc_year] = closed
            _save_cache(by_year)
            print(f"[trading_calendar] 已更新民國 {roc_year} 年休市日 {len(closed)} 天")
    return TradingCalendar(by_year)


def parse_date(value: str) -> date | None:
    cleaned = (value or "").strip()
    if not cleaned or cleaned in {"0", "-", "--", "－"}:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    cal = load_calendar()
    today = date.today()
    result, est = cal.nth_trading_day(today, 6)
    print(f"從 {today} 起算（當天為第 1 天）第 6 個交易日：{result}{'（推估）' if est else ''}")
