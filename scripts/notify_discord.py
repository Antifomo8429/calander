#!/usr/bin/env python3
"""
TWSE 競價拍賣 Discord 變動通知

與 generate_twse_auction_calendar.py 搭配使用。
比對上次快照與最新資料，有任何欄位變動時透過 Discord Webhook 通知。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import conversion_price_index
import trading_calendar

API_BASE = "https://www.twse.com.tw/rwd/zh/announcement"
SNAPSHOT_PATH = Path("calendar/snapshot.json")

# 可轉債拆解日：掛牌（撥券）當天算第 1 個交易日，往後數到第 6 個交易日。
SPLIT_TRADING_DAY_NTH = 6

IGNORE_FIELDS = {"序號", "轉換價來源"}

# 證交所 API 沒有、由本專案自己補上的欄位
SYNTHETIC_FIELDS = {"轉換價", "拆解日"}

FIELD_LABELS = {
    "開標日期": "開標日期",
    "證券名稱": "證券名稱",
    "證券代號": "證券代號",
    "發行市場": "發行市場",
    "發行性質": "發行性質",
    "競拍方式": "競拍方式",
    "投標開始日": "投標開始日",
    "投標結束日": "投標結束日",
    "競拍數量(張)": "競拍數量",
    "最低投標價格(元)": "最低投標價格",
    "最低每標單投標數量(張)": "最低每標單投標數量",
    "最高投(得)標數量(張)": "最高投(得)標數量",
    "保證金成數(%)": "保證金成數",
    "每一投標單投標處理費(元)": "投標處理費",
    "撥券日期(上市、上櫃日期)": "撥券日期",
    "主辦券商": "主辦券商",
    "得標總金額(元)": "得標總金額",
    "得標手續費率(%)": "得標手續費率",
    "總合格件": "總合格件",
    "合格投標數量(張)": "合格投標數量",
    "最低得標價格(元)": "最低得標價格",
    "最高得標價格(元)": "最高得標價格",
    "得標加權平均價格(元)": "得標加權平均價格",
    "實際承銷價格(元)": "實際承銷價格",
    "取消競價拍賣(流標或取消)": "取消競價拍賣",
    "轉換價": "轉換價格",
    "拆解日": "拆解日",
}


def enrich_row(
    row: dict,
    price_index: conversion_price_index.ConversionPriceIndex,
    calendar: trading_calendar.TradingCalendar,
) -> dict:
    """補上證交所 API 沒有、但我們自己算得出來的欄位。

    這兩個欄位一起進快照，轉換價之後才補到（承銷公告晚上架）時，
    下一次排程就會以「資料更新」的形式通知。
    """
    if "轉換公司債" not in row.get("發行性質", ""):
        return row

    price, source = price_index.lookup(row)
    row["轉換價"] = price or ""
    row["轉換價來源"] = source or ""

    listing_date = trading_calendar.parse_date(
        row.get("撥券日期(上市、上櫃日期)", "")
    )
    if listing_date is not None:
        try:
            split_day, estimated = calendar.nth_trading_day(
                listing_date, SPLIT_TRADING_DAY_NTH
            )
            row["拆解日"] = split_day.strftime("%Y/%m/%d") + ("(推估)" if estimated else "")
        except Exception:
            row["拆解日"] = ""
    return row


def row_key(row: dict) -> str:
    return f"{row.get('證券代號', '').strip()}-{row.get('開標日期', '').strip()}"


def fetch_json(url: str, *, retries: int = 3, timeout: int = 20) -> dict:
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


def fetch_all_rows() -> list[dict]:
    """從 TWSE API 取得所有年份的原始資料列，並補上轉換價與拆解日。"""
    year_info = fetch_json(f"{API_BASE}/auctionYear?response=json")
    start = int(year_info["startYear"])
    end = int(year_info["endYear"])

    price_index = conversion_price_index.load_index()
    calendar = trading_calendar.load_calendar()

    all_rows: list[dict] = []
    for year in range(start, end + 1):
        payload = fetch_json(f"{API_BASE}/auction?date={year}&response=json")
        if str(payload.get("stat", "")).upper() != "OK":
            continue
        fields = payload.get("fields", [])
        for raw_row in payload.get("data", []):
            row = {}
            for idx, key in enumerate(fields):
                row[key] = str(raw_row[idx]).strip() if idx < len(raw_row) else ""
            all_rows.append(enrich_row(row, price_index, calendar))

    print(f"已從 API 取得 {len(all_rows)} 筆拍賣資料（{start}~{end}）")
    return all_rows


def load_snapshot() -> dict[str, dict]:
    if not SNAPSHOT_PATH.exists():
        return {}
    try:
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        return {row_key(r): r for r in data}
    except (json.JSONDecodeError, KeyError):
        return {}


def save_snapshot(rows: list[dict]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def diff_data(
    old_map: dict[str, dict], new_map: dict[str, dict]
) -> tuple[list[dict], list[dict], list[tuple[dict, list[tuple[str, str, str]]]]]:
    old_keys = set(old_map.keys())
    new_keys = set(new_map.keys())

    added = [new_map[k] for k in sorted(new_keys - old_keys)]
    removed = [old_map[k] for k in sorted(old_keys - new_keys)]

    changed: list[tuple[dict, list[tuple[str, str, str]]]] = []
    for k in sorted(old_keys & new_keys):
        old_row, new_row = old_map[k], new_map[k]
        diffs = []
        for field in sorted(set(list(old_row.keys()) + list(new_row.keys()))):
            if field in IGNORE_FIELDS:
                continue
            if field in SYNTHETIC_FIELDS and field not in old_row:
                # 舊快照還沒有這些自算欄位，第一次跑不要把整張表當成變動洗版。
                continue
            old_val = old_row.get(field, "").strip()
            new_val = new_row.get(field, "").strip()
            if old_val != new_val:
                label = FIELD_LABELS.get(field, field)
                diffs.append((label, old_val, new_val))
        if diffs:
            changed.append((new_row, diffs))

    return added, removed, changed


def send_discord(
    webhook_url: str,
    added: list[dict],
    removed: list[dict],
    changed: list[tuple[dict, list[tuple[str, str, str]]]],
) -> None:
    embeds: list[dict] = []

    for row in added:
        name = row.get("證券名稱", "").strip()
        code = row.get("證券代號", "").strip()
        issue_type = row.get("發行性質", "")
        fields_list = [
            {"name": "發行性質", "value": issue_type or "-", "inline": True},
            {"name": "發行市場", "value": row.get("發行市場", "-") or "-", "inline": True},
            {"name": "主辦券商", "value": row.get("主辦券商", "-") or "-", "inline": True},
            {"name": "投標期間", "value": f"{row.get('投標開始日', '-')} ~ {row.get('投標結束日', '-')}", "inline": True},
            {"name": "開標日期", "value": row.get("開標日期", "-") or "-", "inline": True},
            {"name": "撥券日期", "value": row.get("撥券日期(上市、上櫃日期)", "-") or "-", "inline": True},
            {"name": "競拍數量", "value": f"{row.get('競拍數量(張)', '-')} 張", "inline": True},
            {"name": "最低投標價格", "value": f"{row.get('最低投標價格(元)', '-')} 元", "inline": True},
        ]
        if "轉換公司債" in issue_type:
            conversion_price = row.get("轉換價", "").strip()
            source = row.get("轉換價來源", "").strip()
            fields_list.append({
                "name": "轉換價格",
                "value": f"{conversion_price} 元（{source}）" if conversion_price else "尚未取得",
                "inline": True,
            })
            split_day = row.get("拆解日", "").strip()
            if split_day:
                fields_list.append({
                    "name": "拆解日（掛牌日起算第6個交易日）",
                    "value": split_day,
                    "inline": True,
                })
        embeds.append({
            "title": f"🆕 新增拍賣｜{name}（{code}）",
            "color": 0x22C55E,
            "fields": fields_list,
        })

    for row, diffs in changed:
        name = row.get("證券名稱", "").strip()
        code = row.get("證券代號", "").strip()
        fields_list = []
        for label, old_val, new_val in diffs:
            fields_list.append({
                "name": label,
                "value": f"~~{old_val or '(空)'}~~ → **{new_val or '(空)'}**",
                "inline": True,
            })
        embeds.append({
            "title": f"📝 資料更新｜{name}（{code}）",
            "color": 0x3B82F6,
            "fields": fields_list,
        })

    for row in removed:
        name = row.get("證券名稱", "").strip()
        code = row.get("證券代號", "").strip()
        embeds.append({
            "title": f"❌ 已移除｜{name}（{code}）",
            "color": 0xEF4444,
            "description": f"開標日期：{row.get('開標日期', '-')}",
        })

    if not embeds:
        return

    summary = f"新增 {len(added)} 筆 ∣ 更新 {len(changed)} 筆 ∣ 移除 {len(removed)} 筆"

    MAX_EMBEDS = 10
    for i in range(0, len(embeds), MAX_EMBEDS):
        batch = embeds[i : i + MAX_EMBEDS]
        body = {
            "username": "TWSE 競價拍賣通知",
            "avatar_url": "https://www.twse.com.tw/favicon.ico",
            "embeds": batch,
        }
        if i == 0:
            body["content"] = f"📊 **TWSE 競價拍賣資料變動**\n{summary}"

        data = json.dumps(body).encode("utf-8")
        req = Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "TWSE-Auction-Calendar/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=15) as resp:
                print(f"[Discord] 第 {i // MAX_EMBEDS + 1} 批通知已送出（{len(batch)} 則），HTTP {resp.status}")
        except Exception as e:
            print(f"[Discord] 送出失敗: {e}")


def main() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")

    rows = fetch_all_rows()
    if not rows:
        print("未取得任何資料，跳過通知")
        return 1

    new_map = {row_key(r): r for r in rows}
    old_map = load_snapshot()

    if not old_map:
        print("首次執行，建立初始快照（不發送通知）")
        save_snapshot(rows)
        return 0

    added, removed, changed = diff_data(old_map, new_map)
    total = len(added) + len(removed) + len(changed)

    if total == 0:
        print("資料無變動，不發送通知")
        save_snapshot(rows)
        return 0

    print(f"偵測到 {total} 筆變動（新增 {len(added)}、更新 {len(changed)}、移除 {len(removed)}）")

    if not webhook_url:
        print("[Discord] 未設定 DISCORD_WEBHOOK_URL，跳過通知")
    else:
        send_discord(webhook_url, added, removed, changed)

    save_snapshot(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
