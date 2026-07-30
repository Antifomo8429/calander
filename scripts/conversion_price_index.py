#!/usr/bin/env python3
"""把 conversion_prices.json 的轉換價對回證交所競拍公告的每一檔可轉債。

證交所競拍 API 沒有轉換價欄位，轉換價只能從承銷商公會的公告 PDF 取得，
兩邊沒有共同的識別碼，所以這裡負責做比對：

1. 競拍公告：用「投標開始日 + 投標結束日」對。同一天常有兩檔以上一起投標
   （例如 2026/08/03~08/05 同時有皇龍二與十銓五），所以還要再用公司簡稱
   與主辦券商確認，避免張冠李戴。
2. 承銷公告：只有公司全名與申報日期，用「公司簡稱 + 主辦券商 + 申報日早於
   投標日且不超過 180 天」比對。這條路徑通常比競拍公告早 10 天以上就有資料。
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from trading_calendar import parse_date

CONVERSION_PRICES_PATH = Path(__file__).parent / "conversion_prices.json"

# 承銷公告申報日與投標開始日之間的合理間隔
_MAX_ANNOUNCE_LEAD_DAYS = 180

_COMPANY_SUFFIXES = (
    "股份有限公司",
    "有限公司",
    "控股公司",
    "公司",
)

# 證交所簡稱會把這些詞縮寫，例如「定穎投資控股」的簡稱是「定穎投控」
_ABBREVIATIONS = (
    ("投資控股", "投控"),
    ("金融控股", "金控"),
    ("工業控股", "工控"),
)


def normalize_short_name(security_name: str) -> str:
    """證交所證券名稱 -> 公司簡稱。

    「博智三」->「博智」、「威宏三KY」->「威宏」、「雍智科技一」->「雍智科技」
    """
    name = (security_name or "").strip()
    name = re.sub(r"[-－]?KY$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[一二三四五六七八九十百]+$", "", name)
    return name.strip()


def normalize_company_name(company: str) -> str:
    """公司全名 -> 去掉組織型態與 KY 標記的核心名稱。"""
    name = (company or "").strip()
    name = re.sub(r"[-－]?KY$", "", name, flags=re.IGNORECASE)
    for suffix in _COMPANY_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    for full_word, abbrev in _ABBREVIATIONS:
        name = name.replace(full_word, abbrev)
    return name.strip()


def _is_subsequence(short: str, core: str) -> bool:
    """簡稱的每個字是否依序出現在全名裡（處理中間被縮寫掉的情況）。"""
    it = iter(core)
    return all(ch in it for ch in short)


def company_matches(security_name: str, company: str) -> bool:
    short = normalize_short_name(security_name)
    core = normalize_company_name(company)
    if not short or not core:
        return False
    if core.startswith(short) or short.startswith(core):
        return True
    # 前兩個字相同、且簡稱是全名的子序列時才算命中，避免不同公司誤配。
    return (
        len(short) >= 2
        and short[:2] == core[:2]
        and _is_subsequence(short, core)
    )


def underwriter_matches(twse_broker: str, twsa_underwriter: str) -> bool:
    broker = (twse_broker or "").strip()
    underwriter = (twsa_underwriter or "").strip()
    if not broker or not underwriter:
        return False
    return underwriter.startswith(broker) or broker in underwriter


class ConversionPriceIndex:
    def __init__(self, entries: list[dict]) -> None:
        self.auction: list[dict] = []
        self.underwriting: list[dict] = []
        for entry in entries:
            if not entry.get("conversion_price"):
                continue
            if entry.get("source") == "UnderwritingNotice":
                self.underwriting.append(entry)
            else:
                # 舊格式（沒有 source 欄位）一律當競拍公告
                self.auction.append(entry)

    def lookup(self, row: dict) -> tuple[str | None, str | None]:
        """回傳 (轉換價, 來源說明)。找不到時回傳 (None, None)。"""
        security_name = (row.get("證券名稱") or "").strip()
        broker = (row.get("主辦券商") or "").strip()
        bid_start = (row.get("投標開始日") or "").strip()
        bid_end = (row.get("投標結束日") or "").strip()

        hit = self._lookup_auction(security_name, broker, bid_start, bid_end)
        if hit:
            return hit, "競拍公告"

        hit = self._lookup_underwriting(security_name, broker, bid_start)
        if hit:
            return hit, "承銷公告"

        return None, None

    def _lookup_auction(
        self, security_name: str, broker: str, bid_start: str, bid_end: str
    ) -> str | None:
        if not bid_start or not bid_end:
            return None
        candidates = [
            e
            for e in self.auction
            if e.get("bid_start") == bid_start and e.get("bid_end") == bid_end
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]["conversion_price"]

        # 同一投標期間有多檔時，一定要靠公司名／券商區分，否則寧可不給。
        for entry in candidates:
            if company_matches(security_name, entry.get("company", "")):
                return entry["conversion_price"]
        matched = [
            e for e in candidates if underwriter_matches(broker, e.get("underwriter", ""))
        ]
        if len(matched) == 1:
            return matched[0]["conversion_price"]
        return None

    def _lookup_underwriting(
        self, security_name: str, broker: str, bid_start: str
    ) -> str | None:
        bid_date = parse_date(bid_start)
        scored: list[tuple[int, date, dict]] = []
        for entry in self.underwriting:
            if not company_matches(security_name, entry.get("company", "")):
                continue
            announce = parse_date(entry.get("announce_date", ""))
            if bid_date and announce:
                lead = (bid_date - announce).days
                if lead < -7 or lead > _MAX_ANNOUNCE_LEAD_DAYS:
                    continue
            score = 1
            if underwriter_matches(broker, entry.get("underwriter", "")):
                score += 1
            scored.append((score, announce or date.min, entry))

        if not scored:
            return None
        # 分數高者優先，同分取申報日最接近投標日（也就是最新的那一筆）。
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        best_score = scored[0][0]
        top = [item for item in scored if item[0] == best_score]
        if len(top) > 1 and best_score < 2:
            # 只靠公司名對上、且有多筆同名候選時風險太高，不給值。
            return None
        return top[0][2]["conversion_price"]


def load_index(path: Path = CONVERSION_PRICES_PATH) -> ConversionPriceIndex:
    if not path.exists():
        return ConversionPriceIndex([])
    try:
        entries = json.loads(path.read_text("utf-8"))
    except Exception:
        return ConversionPriceIndex([])
    if not isinstance(entries, list):
        return ConversionPriceIndex([])
    return ConversionPriceIndex(entries)
