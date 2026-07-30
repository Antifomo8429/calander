#!/usr/bin/env python3
"""從承銷商公會（twsa.org.tw）的公告 PDF 抓可轉債「轉換價格」。

輸出：scripts/conversion_prices.json

兩個資料來源，兩者都抓：

1. 承銷公告（UnderwritingNotice）
   承銷商申報時就上架，實測比競拍公告早 10~14 天。
   只有公司全名 + 申報日期，沒有投標期間，所以由行事曆端以
   「公司簡稱 + 主辦券商 + 申報日在投標日之前」比對。

2. 競拍公告（Auction）
   幾乎在投標開始當天才上架，但有明確的投標期間，比對最準。

抓取策略：
- 民國年抓「本年 + 前一年」，避免跨年度（例如投標 12/30~1/02）的案子漏掉。
- 已經有轉換價的項目不再重抓 PDF；抓不到價格時保留舊值，
  絕不用 None 覆蓋既有資料。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import time
import datetime
from html.parser import HTMLParser
from pathlib import Path

import requests
from pdfminer.high_level import extract_text

BASE_URL = "https://web.twsa.org.tw/EDOC2/"
LIST_URL = BASE_URL + "default.aspx"
DOWNLOAD_BASE = "https://web.twsa.org.tw"

REPORT_AUCTION = "Auction"
REPORT_UNDERWRITING = "UnderwritingNotice"

# 每個 report type 在 gvResult 裡的檔案下載按鈕名稱片段
_BUTTON_TOKEN = {
    REPORT_AUCTION: "imgbtnAuctionFileName",
    REPORT_UNDERWRITING: "imgbtnFileName",
}

_DOWNLOAD_URL_RE = re.compile(
    r"""(?:window\.location(?:\.href)?\s*=\s*|url\s*=\s*|href\s*=\s*)['"]([^'"]*FileDownload[^'"]+)['"]""",
    re.IGNORECASE,
)
_DOWNLOAD_HREF_RE = re.compile(
    r"""href=['"]([^'"]*FileDownload\.ashx[^'"]+)['"]""",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": LIST_URL,
}

_NUM = r"([\d,]+(?:\.\d+)?)"

# 只認「結論句」。PDF 裡「轉換價格」四個字最常出現在
# 「係以 X 日為轉換價格基準日，取其前一、三、五個營業日收盤價之簡單算術平均數
#   (A 元、B 元及 C 元)擇一者(經選定為 A 元)，乘以轉換溢價率 R% 後即為每股轉換價格 P 元」
# 這種句子裡，用寬鬆的關鍵字搜尋會抓到 A/B/C 這些均價而不是真正的轉換價 P。
CONVERSION_PRICE_PATTERNS = [
    re.compile(r"每股轉換價格(?:為|即|訂為)?" + _NUM + r"元"),
    re.compile(r"轉換價格(?:即|訂)?為" + _NUM + r"元"),
    re.compile(r"轉換價格[:：]" + _NUM + r"元"),
    re.compile(r"每股轉換價格[:：]" + _NUM + r"元"),
]

# 用來交叉驗證：基準價 × 轉換溢價率 ≈ 轉換價格
_BASE_PRICE_RE = re.compile(r"(?:經選定為?|擇一者即)" + _NUM + r"元")
_PREMIUM_RE = re.compile(r"轉換溢價率(?:為)?" + _NUM + r"%")

# 轉換價格的合理範圍（每股台幣），排除面額 100,000 等非股價數字
_PRICE_MIN = 1.0
_PRICE_MAX = 9999.0

OUTPUT_PATH = Path(__file__).parent / "conversion_prices.json"

# 同一份公告解析失敗幾次之後就不再每天重抓
MAX_ATTEMPTS = 5


class _FormParser(HTMLParser):
    """抓出 ASP.NET 的 hidden 欄位，以及 gvResult 表格的每一列。"""

    def __init__(self, button_token: str) -> None:
        super().__init__()
        self.button_token = button_token
        self.hidden: dict[str, str] = {}
        self.rows: list[dict] = []
        self._in_grid = False
        self._capture_td = False
        self._current_cell = ""
        self._row_cells: list[str] = []
        self._row_btn: str = ""

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr = dict(attrs)
        if tag == "input":
            if attr.get("type") == "hidden":
                name = attr.get("name", "")
                if name:
                    self.hidden[name] = attr.get("value", "")
            elif attr.get("type") == "image":
                name = attr.get("name", "")
                if name.endswith(self.button_token) and not self._row_btn:
                    self._row_btn = name
        if tag == "table" and attr.get("id", "").endswith("gvResult"):
            self._in_grid = True
        if self._in_grid:
            if tag == "tr":
                self._row_cells = []
                self._row_btn = ""
            elif tag == "td":
                self._capture_td = True
                self._current_cell = ""

    def handle_endtag(self, tag: str) -> None:
        if not self._in_grid:
            return
        if tag == "td" and self._capture_td:
            self._capture_td = False
            self._row_cells.append(self._current_cell.strip())
        elif tag == "tr":
            if self._row_cells:
                self.rows.append(
                    {"cells": list(self._row_cells), "btn_name": self._row_btn}
                )
        elif tag == "table" and self._in_grid:
            self._in_grid = False

    def handle_data(self, data: str) -> None:
        if self._capture_td:
            self._current_cell += data


def _parse_page(html: str, button_token: str) -> tuple[dict[str, str], list[dict]]:
    parser = _FormParser(button_token)
    parser.feed(html)
    return parser.hidden, parser.rows


def _valid_price(price_str: str) -> float | None:
    try:
        value = float(price_str.replace(",", ""))
    except ValueError:
        return None
    return value if _PRICE_MIN < value < _PRICE_MAX else None


def _extract_conversion_price(pdf_bytes: bytes) -> dict | None:
    """回傳 {'conversion_price', 'base_price', 'premium_rate'}，抓不到時回傳 None。"""
    try:
        text = extract_text(io.BytesIO(pdf_bytes))
    except Exception:
        return None

    # PDF 表格常把「轉 換 價 格」拆成有空白的字元，先整份去空白再比對。
    flat = re.sub(r"\s+", "", text)

    price = None
    for pattern in CONVERSION_PRICE_PATTERNS:
        for match in pattern.finditer(flat):
            value = _valid_price(match.group(1))
            if value is not None:
                price = value
                break
        if price is not None:
            break

    if price is None:
        return None

    base_match = _BASE_PRICE_RE.search(flat)
    premium_match = _PREMIUM_RE.search(flat)
    base = _valid_price(base_match.group(1)) if base_match else None
    try:
        premium = float(premium_match.group(1).replace(",", "")) if premium_match else None
    except ValueError:
        premium = None

    # 交叉驗證：基準價 × 溢價率應該要等於轉換價，差太多代表抓錯數字。
    if base and premium:
        expected = base * premium / 100
        if expected > 0 and abs(expected - price) / expected > 0.02:
            print(
                f"    ⚠ 轉換價 {price} 與 基準價 {base} × 溢價率 {premium}% "
                f"= {expected:.2f} 不符，改用推算值"
            )
            price = round(expected, 2)

    return {
        "conversion_price": _format_price(price),
        "base_price": _format_price(base) if base else None,
        "premium_rate": premium,
    }


def _format_price(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _resolve_download_url(html: str) -> str | None:
    """從 POST 回應的 HTML/JS 裡挖出真正的 FileDownload.ashx 連結。"""
    for pattern in (_DOWNLOAD_URL_RE, _DOWNLOAD_HREF_RE):
        m = pattern.search(html)
        if m:
            url = m.group(1)
            if url.startswith("/"):
                return DOWNLOAD_BASE + url
            if url.startswith("http"):
                return url
            return BASE_URL + url
    m = re.search(r"""['"]([^'"]*edoc2/FileDownload\.ashx\?[^'"]+)['"]""", html, re.IGNORECASE)
    if m:
        url = m.group(1)
        if url.startswith("/"):
            return DOWNLOAD_BASE + url
        if url.startswith("http"):
            return url
        return "https://web.twsa.org.tw/" + url.lstrip("/")
    return None


def _get_year_page(
    session: requests.Session, roc_year: int, report_type: str
) -> tuple[str, dict[str, str], list[dict]]:
    """取得指定民國年 + 指定公告類型的列表頁（處理 ASP.NET postback）。"""
    ce_year = str(roc_year + 1911)
    token = _BUTTON_TOKEN[report_type]

    resp = session.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    hidden, rows = _parse_page(resp.text, token)

    needs_type = hidden.get("ctl00$cphMain$rblReportType") != report_type
    needs_year = hidden.get("ctl00$cphMain$ddlYear") != ce_year

    if needs_type or needs_year:
        # 先切公告類型（切換會重建年度下拉選單），再切年度。
        if needs_type:
            form_data = dict(hidden)
            form_data["__EVENTTARGET"] = "ctl00$cphMain$rblReportType"
            form_data["__EVENTARGUMENT"] = ""
            form_data["ctl00$cphMain$rblReportType"] = report_type
            resp = session.post(LIST_URL, data=form_data, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            hidden, rows = _parse_page(resp.text, token)

        if hidden.get("ctl00$cphMain$ddlYear") != ce_year:
            form_data = dict(hidden)
            form_data["__EVENTTARGET"] = "ctl00$cphMain$ddlYear"
            form_data["__EVENTARGUMENT"] = ""
            form_data["ctl00$cphMain$ddlYear"] = ce_year
            form_data["ctl00$cphMain$rblReportType"] = report_type
            resp = session.post(LIST_URL, data=form_data, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            hidden, rows = _parse_page(resp.text, token)

    return resp.text, hidden, rows


def _download_pdf(
    session: requests.Session, btn_name: str, roc_year: int, report_type: str
) -> bytes | None:
    """每次下載前都重抓一次年度頁面，確保 VIEWSTATE 與該年度/類型的狀態一致。

    伺服器不會直接吐 PDF，POST 之後回的是一段導向 FileDownload.ashx 的 HTML/JS，
    要把那個網址挖出來再 GET。
    """
    try:
        _, hidden, _ = _get_year_page(session, roc_year, report_type)
    except Exception as exc:
        print(f"    頁面載入失敗: {exc}")
        return None

    form_data = dict(hidden)
    form_data[btn_name + ".x"] = "10"
    form_data[btn_name + ".y"] = "10"
    form_data["ctl00$cphMain$rblReportType"] = report_type

    try:
        resp = session.post(
            LIST_URL, data=form_data, headers=HEADERS, timeout=30, allow_redirects=True
        )
        resp.raise_for_status()

        if resp.content[:4] == b"%PDF" or "pdf" in resp.headers.get("Content-Type", ""):
            return resp.content

        download_url = _resolve_download_url(resp.text)
        if download_url:
            pdf_resp = session.get(download_url, headers=HEADERS, timeout=30)
            pdf_resp.raise_for_status()
            if pdf_resp.content[:4] == b"%PDF" or "pdf" in pdf_resp.headers.get(
                "Content-Type", ""
            ):
                return pdf_resp.content
            print(f"    下載連結非 PDF: {pdf_resp.headers.get('Content-Type', '')}")
            return None

        print(f"    未找到下載連結，回應前 200 字: {resp.text[:200]}")
        return None

    except Exception as exc:
        print(f"    POST 失敗: {exc}")
        return None


def list_auction_rows(session: requests.Session, roc_year: int) -> list[dict]:
    """競拍公告：序號, 發行公司, 主辦承銷商, 發行性質, 承銷股數, 競拍股數, 投標期間, 最低承銷價格, ..."""
    _, _, raw_rows = _get_year_page(session, roc_year, REPORT_AUCTION)
    rows: list[dict] = []
    for raw in raw_rows:
        cells = raw["cells"]
        if len(cells) < 8 or not raw["btn_name"]:
            continue
        if "轉換公司債" not in cells[3]:
            continue
        bid_start, _, bid_end = cells[6].partition("~")
        rows.append({
            "source": REPORT_AUCTION,
            "seq": cells[0],
            "company": cells[1],
            "underwriter": cells[2],
            "issue_type": cells[3],
            "bid_start": bid_start.strip(),
            "bid_end": bid_end.strip(),
            "min_price": cells[7],
            "announce_date": "",
            "btn_name": raw["btn_name"],
            "roc_year": roc_year,
        })
    return rows


def list_underwriting_rows(session: requests.Session, roc_year: int) -> list[dict]:
    """承銷公告：序號, 申報日期, 主辦承銷商, 案件名稱, 方式, 發行性質, 發行種類, 配售方式一, ..."""
    _, _, raw_rows = _get_year_page(session, roc_year, REPORT_UNDERWRITING)
    rows: list[dict] = []
    for raw in raw_rows:
        cells = raw["cells"]
        if len(cells) < 8 or not raw["btn_name"]:
            continue
        if "轉換公司債" not in cells[6]:
            continue
        rows.append({
            "source": REPORT_UNDERWRITING,
            "seq": cells[0],
            "company": cells[3],
            "underwriter": cells[2],
            "issue_type": cells[6],
            "bid_start": "",
            "bid_end": "",
            "min_price": "",
            "announce_date": cells[1],
            "btn_name": raw["btn_name"],
            "roc_year": roc_year,
        })
    return rows


def entry_key(entry: dict) -> tuple:
    if entry.get("source") == REPORT_UNDERWRITING:
        return (REPORT_UNDERWRITING, entry.get("seq", ""), entry.get("company", ""))
    # 舊版資料沒有 source 欄位，一律視為競拍公告，key 保持與舊檔相容。
    return (
        REPORT_AUCTION,
        entry.get("company", ""),
        entry.get("bid_start", ""),
        entry.get("bid_end", ""),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取可轉債轉換價格")
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=80,
        help="單次執行最多下載幾份 PDF（預設 80，其餘留到下次排程）",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=2,
        help="往回抓幾個民國年（預設 2，含今年，避免跨年度案件漏掉）",
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="連已經有轉換價的項目也重新下載驗證",
    )
    args = parser.parse_args()

    current_roc = datetime.date.today().year - 1911
    roc_years = [current_roc - offset for offset in range(args.years)]

    existing: list[dict] = []
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text("utf-8"))
        except Exception:
            existing = []
    by_key: dict[tuple, dict] = {entry_key(e): e for e in existing}

    session = requests.Session()
    session.headers.update(HEADERS)

    listings: list[dict] = []
    for roc_year in roc_years:
        for lister, label in (
            (list_underwriting_rows, "承銷公告"),
            (list_auction_rows, "競拍公告"),
        ):
            try:
                rows = lister(session, roc_year)
            except Exception as exc:
                print(f"民國 {roc_year} 年 {label} 列表取得失敗：{exc}")
                continue
            print(f"民國 {roc_year} 年 {label}：{len(rows)} 筆可轉債")
            listings.extend(rows)

    downloads = 0
    for row in listings:
        key = entry_key(row)
        record = by_key.get(key)
        has_price = bool(record and record.get("conversion_price"))

        attempts = int((record or {}).get("attempts") or 0)

        if has_price and not args.recheck:
            continue
        # 有些公告 PDF 是掃描檔或格式特殊，永遠解不出價格；
        # 試過幾次就別再每天重抓，把額度留給新案件（--recheck 可強制重試）。
        if attempts >= MAX_ATTEMPTS and not args.recheck:
            continue
        if downloads >= args.max_downloads:
            print("已達單次下載上限，其餘留待下次排程")
            break

        label = row["announce_date"] or f"{row['bid_start']}~{row['bid_end']}"
        print(f"處理 [{row['source']}] {row['company']} {label} ...")
        downloads += 1

        pdf_bytes = _download_pdf(session, row["btn_name"], row["roc_year"], row["source"])
        parsed = _extract_conversion_price(pdf_bytes) if pdf_bytes else None

        if parsed:
            print(f"    -> 轉換價 {parsed['conversion_price']}")
        else:
            print("    -> 未取得轉換價（保留既有資料）")

        merged = dict(record or {})
        merged.update({
            "company": row["company"],
            "source": row["source"],
            "seq": row["seq"],
            "underwriter": row["underwriter"],
            "issue_type": row["issue_type"],
            "announce_date": row["announce_date"],
            "bid_start": row["bid_start"],
            "bid_end": row["bid_end"],
            "min_price": row["min_price"],
        })
        if parsed:
            merged.update(parsed)
            merged["attempts"] = 0
        else:
            # 抓不到就維持舊值，不要用 None 蓋掉已知的價格。
            merged.setdefault("conversion_price", None)
            merged["attempts"] = attempts + 1
        by_key[key] = merged

        time.sleep(1)

    def sort_key(entry: dict) -> tuple:
        return (
            entry.get("bid_start") or entry.get("announce_date") or "",
            entry.get("company", ""),
        )

    merged_entries = sorted(by_key.values(), key=sort_key)
    OUTPUT_PATH.write_text(
        json.dumps(merged_entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with_price = sum(1 for e in merged_entries if e.get("conversion_price"))
    print(
        f"\n完成：{len(merged_entries)} 筆（{with_price} 筆有轉換價），"
        f"本次下載 {downloads} 份 PDF，存至 {OUTPUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
