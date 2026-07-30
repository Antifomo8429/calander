#!/usr/bin/env python3
"""Generate a subscribable iCalendar (.ics) from TWSE auction announcements."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import conversion_price_index
import trading_calendar

API_BASE = "https://www.twse.com.tw/rwd/zh/announcement"
SOURCE_PAGE = "https://www.twse.com.tw/zh/announcement/auction.html"

# 可轉債拆解日：掛牌（撥券）當天算第 1 個交易日，往後數到第 6 個交易日。
SPLIT_TRADING_DAY_NTH = 6

# 拆解日與「轉換價尚未取得」這類提示只對近期案件有意義，
# 太舊的案子（證交所資料回溯到 2016 年）加上去只是灌爆行事曆。
RECENT_WINDOW_DAYS = 180


@dataclass(frozen=True)
class CalendarEvent:
    event_date: date
    summary: str
    description: str
    uid: str


EVENT_DATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("投標開始日", "投標開始"),
    ("投標結束日", "投標截止"),
    ("開標日期", "開標"),
    ("撥券日期(上市、上櫃日期)", "撥券/掛牌"),
)


def fetch_json(url: str, *, retries: int = 3, timeout: int = 20) -> dict:
    """Fetch JSON with basic retry logic for temporary network errors."""
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; twse-auction-calendar/1.0; "
                "+https://www.twse.com.tw/)"
            )
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            return json.loads(body)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"無法取得資料：{url}") from last_error


def parse_twse_date(value: str) -> date | None:
    """Parse date string in TWSE format (YYYY/MM/DD)."""
    cleaned = (value or "").strip()
    if not cleaned or cleaned in {"0", "-", "--", "－"}:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def ics_escape(text: str) -> str:
    """Escape text fields per RFC 5545."""
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", r"\n")
    )


def fold_ical_line(line: str, limit: int = 75) -> list[str]:
    """Fold long iCalendar lines to <= 75 octets."""
    if not line:
        return [""]
    parts: list[str] = []
    current = ""
    current_len = 0
    for ch in line:
        ch_len = len(ch.encode("utf-8"))
        if current and current_len + ch_len > limit:
            parts.append(current)
            current = ch
            current_len = ch_len
        else:
            current += ch
            current_len += ch_len
    parts.append(current)
    return [parts[0], *[f" {chunk}" for chunk in parts[1:]]]


def normalize_row(fields: list[str], row: list[str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for idx, key in enumerate(fields):
        normalized[key] = str(row[idx]).strip() if idx < len(row) else ""
    return normalized


def stable_uid(source: str) -> str:
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()
    return f"{digest}@twse-auction-calendar"


def stable_dtstamp(event: CalendarEvent) -> str:
    """Create deterministic DTSTAMP so unchanged events keep identical output."""
    fingerprint = "|".join(
        [event.uid, event.summary, event.description, event.event_date.isoformat()]
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()
    # Map hash to a fixed 50-year UTC window to keep DTSTAMP valid and stable.
    seconds = int(digest[:10], 16) % (50 * 365 * 24 * 60 * 60)
    stamp = datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return stamp.strftime("%Y%m%dT%H%M%SZ")


def value_or_dash(row: dict[str, str], key: str) -> str:
    value = row.get(key, "").strip()
    return value if value else "-"


def split_date(
    listing_date: date, calendar: trading_calendar.TradingCalendar
) -> tuple[date, bool] | None:
    """拆解日 = 掛牌日起算（掛牌日為第 1 天）第 6 個交易日。回傳 (日期, 是否為推估值)。"""
    try:
        return calendar.nth_trading_day(listing_date, SPLIT_TRADING_DAY_NTH)
    except Exception as exc:
        print(f"無法推算拆解日（掛牌日 {listing_date}）：{exc}")
        return None


def build_events(
    fields: list[str],
    rows: list[list[str]],
    price_index: conversion_price_index.ConversionPriceIndex,
    calendar: trading_calendar.TradingCalendar,
) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    recent_cutoff = date.today() - timedelta(days=RECENT_WINDOW_DAYS)
    for raw_row in rows:
        row = normalize_row(fields, raw_row)
        security_name = value_or_dash(row, "證券名稱")
        security_code = value_or_dash(row, "證券代號")
        market = value_or_dash(row, "發行市場")
        issue_type = value_or_dash(row, "發行性質")
        auction_method = value_or_dash(row, "競拍方式")
        quantity = value_or_dash(row, "競拍數量(張)")
        min_price = value_or_dash(row, "最低投標價格(元)")
        min_bid_qty = value_or_dash(row, "最低每標單投標數量(張)")
        max_bid_qty = value_or_dash(row, "最高投(得)標數量(張)")
        broker = value_or_dash(row, "主辦券商")
        bid_start = value_or_dash(row, "投標開始日")
        bid_end = value_or_dash(row, "投標結束日")
        open_date = value_or_dash(row, "開標日期")
        allotment_date = value_or_dash(row, "撥券日期(上市、上櫃日期)")
        is_convertible = "轉換公司債" in issue_type

        # 證交所競拍 API 沒有轉換價欄位，一律從承銷商公會公告比對而來。
        conversion_price, price_source = ("", None)
        if is_convertible:
            conversion_price, price_source = price_index.lookup(row)
            conversion_price = conversion_price or ""

        # 拆解日：只有可轉債才有，掛牌（撥券）後第 6 個交易日。
        split_info = None
        listing_date = parse_twse_date(row.get("撥券日期(上市、上櫃日期)", ""))
        is_recent = listing_date is not None and listing_date >= recent_cutoff
        if is_convertible and is_recent:
            split_info = split_date(listing_date, calendar)

        date_fields = list(EVENT_DATE_FIELDS)
        if split_info is not None:
            date_fields.append((None, "拆解日"))

        for date_field, event_type in date_fields:
            if event_type == "拆解日":
                event_date, estimated = split_info
            else:
                event_date = parse_twse_date(row.get(date_field, ""))
                estimated = False
            if event_date is None:
                continue

            summary = f"[TWSE競拍] {security_name}({security_code}) {event_type}"
            if is_convertible and conversion_price:
                summary += f"｜轉換價 {conversion_price}"
            desc_lines = [
                f"事件：{event_type}",
                f"證券名稱：{security_name}",
                f"證券代號：{security_code}",
                f"發行市場：{market}",
                f"發行性質：{issue_type}",
            ]
            if is_convertible:
                if conversion_price:
                    desc_lines.append(f"轉換價(元)：{conversion_price}（來源：{price_source}）")
                elif is_recent:
                    desc_lines.append("轉換價(元)：尚未取得（承銷商公會公告未上架）")
            if event_type == "拆解日":
                note = "掛牌日起算（掛牌日為第 1 個交易日）的第 6 個交易日"
                if estimated:
                    note += "，休市日曆尚未公告該年度，此為排除週末的推估值"
                desc_lines.append(f"拆解日說明：{note}")
            if split_info is not None:
                desc_lines.append(
                    f"拆解日：{split_info[0].strftime('%Y/%m/%d')}"
                    + ("（推估）" if split_info[1] else "")
                )
            desc_lines += [
                f"競拍方式：{auction_method}",
                f"競拍數量(張)：{quantity}",
                f"最低投標價格(元)：{min_price}",
                f"最低每標單投標數量(張)：{min_bid_qty}",
                f"最高投(得)標數量(張)：{max_bid_qty}",
                f"主辦券商：{broker}",
                f"投標開始日：{bid_start}",
                f"投標結束日：{bid_end}",
                f"開標日期：{open_date}",
                f"撥券日期：{allotment_date}",
                f"資料來源：{SOURCE_PAGE}",
            ]
            description = "\n".join(desc_lines)
            # 拆解日是算出來的，休市日曆更新後日期可能微調；
            # UID 綁掛牌日而不是綁事件日，訂閱端才會是「同一個事件被移動」
            # 而不是「舊事件消失、新事件出現」。
            uid_date = listing_date if event_type == "拆解日" else event_date
            uid_source = "|".join(
                [
                    security_code,
                    security_name,
                    event_type,
                    uid_date.isoformat(),
                    market,
                    issue_type,
                ]
            )
            events.append(
                CalendarEvent(
                    event_date=event_date,
                    summary=summary,
                    description=description,
                    uid=stable_uid(uid_source),
                )
            )
    return events


def render_ics(events: Iterable[CalendarEvent]) -> str:
    lines: list[str] = []

    def add_line(raw: str) -> None:
        for folded in fold_ical_line(raw):
            lines.append(folded)

    add_line("BEGIN:VCALENDAR")
    add_line("VERSION:2.0")
    add_line("PRODID:-//TWSE Auction Calendar//EN")
    add_line("CALSCALE:GREGORIAN")
    add_line("METHOD:PUBLISH")
    add_line("X-WR-CALNAME:TWSE 競價拍賣公告")
    add_line("X-WR-CALDESC:由 TWSE 競價拍賣公告自動產生")
    add_line("X-WR-TIMEZONE:Asia/Taipei")

    for event in sorted(events, key=lambda e: (e.event_date, e.summary, e.uid)):
        add_line("BEGIN:VEVENT")
        add_line(f"UID:{event.uid}")
        add_line(f"DTSTAMP:{stable_dtstamp(event)}")
        add_line(f"DTSTART;VALUE=DATE:{event.event_date.strftime('%Y%m%d')}")
        add_line(
            "DTEND;VALUE=DATE:"
            f"{(event.event_date + timedelta(days=1)).strftime('%Y%m%d')}"
        )
        add_line(f"SUMMARY:{ics_escape(event.summary)}")
        add_line(f"DESCRIPTION:{ics_escape(event.description)}")
        add_line(f"URL:{SOURCE_PAGE}")
        add_line("STATUS:CONFIRMED")
        add_line("TRANSP:TRANSPARENT")
        add_line("END:VEVENT")

    add_line("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def resolve_year_range(start_year: int | None, end_year: int | None) -> tuple[int, int]:
    year_info = fetch_json(f"{API_BASE}/auctionYear?response=json")
    api_start = int(year_info["startYear"])
    api_end = int(year_info["endYear"])
    final_start = start_year if start_year is not None else api_start
    final_end = end_year if end_year is not None else api_end
    if final_start > final_end:
        raise ValueError("start-year 不可大於 end-year")
    return final_start, final_end


def fetch_all_events(start_year: int, end_year: int) -> list[CalendarEvent]:
    price_index = conversion_price_index.load_index()
    calendar = trading_calendar.load_calendar()
    all_events: list[CalendarEvent] = []
    for year in range(start_year, end_year + 1):
        payload = fetch_json(f"{API_BASE}/auction?date={year}&response=json")
        status = str(payload.get("stat", "")).upper()
        if status != "OK":
            continue
        fields = payload.get("fields", [])
        rows = payload.get("data", [])
        if not isinstance(fields, list) or not isinstance(rows, list):
            continue
        all_events.extend(build_events(fields, rows, price_index, calendar))

    # Deduplicate in case TWSE data overlaps between years.
    unique_by_uid: dict[str, CalendarEvent] = {}
    for event in all_events:
        unique_by_uid[event.uid] = event
    return list(unique_by_uid.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="產生 TWSE 競價拍賣行事曆 .ics 檔")
    parser.add_argument(
        "--output",
        default="calendar/twse-auction.ics",
        help="輸出路徑（預設: calendar/twse-auction.ics）",
    )
    parser.add_argument("--start-year", type=int, help="覆蓋起始年份（西元）")
    parser.add_argument("--end-year", type=int, help="覆蓋結束年份（西元）")
    args = parser.parse_args()

    start_year, end_year = resolve_year_range(args.start_year, args.end_year)
    events = fetch_all_events(start_year, end_year)
    ics_text = render_ics(events)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(ics_text, encoding="utf-8")

    print(
        f"完成：{output_path}，共 {len(events)} 個事件，資料年份 {start_year}~{end_year}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
