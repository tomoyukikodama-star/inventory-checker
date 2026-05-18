"""
在庫チェッカー - 各モールの商品ページを巡回して在庫状況をGoogle Sheetsに書き戻す
実行方法: python checker.py
定期実行: cron または GitHub Actions で1時間ごとに実行
"""

import time
import random
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Literal

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ── ログ設定 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("inventory.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── 設定 ──────────────────────────────────────────────────
SPREADSHEET_ID = "1b19YL47ZWvQIPywnA32d6oj4F112INLYeO56I6k5UeQ"  # ← あなたのスプレッドシートID
SHEET_NAME     = "在庫管理"       # シート名（実際の名前に合わせて変更）
CREDENTIALS_FILE = "credentials.json"  # Google サービスアカウントのJSONキー

# 列定義（スプレッドシートの列番号、1始まり）
COL_PRODUCT_NAME = 1   # A: 商品名
COL_RAKUTEN_URL  = 2   # B: 楽天URL
COL_YAHOO_URL    = 3   # C: Yahoo!URL
COL_AMAZON_URL   = 4   # D: Amazon URL
COL_MERCARI_URL  = 5   # E: メルカリURL
COL_RAKUTEN_STS  = 8   # H: 楽天ステータス
COL_YAHOO_STS    = 9   # I: Yahoo!ステータス
COL_AMAZON_STS   = 10  # J: Amazonステータス
COL_MERCARI_STS  = 11  # K: メルカリステータス
COL_UPDATED_AT   = 14  # N: 最終確認日時

STATUS_IN      = "✅ 在庫あり"
STATUS_OUT     = "❌ 在庫なし"
STATUS_UNKNOWN = "⚠️ 確認不可"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── データクラス ───────────────────────────────────────────
@dataclass
class ProductRow:
    row_index: int
    name: str
    rakuten_url: str = ""
    yahoo_url: str   = ""
    amazon_url: str  = ""
    mercari_url: str = ""

@dataclass
class CheckResult:
    rakuten: str  = STATUS_UNKNOWN
    yahoo: str    = STATUS_UNKNOWN
    amazon: str   = STATUS_UNKNOWN
    mercari: str  = STATUS_UNKNOWN
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y/%m/%d %H:%M"))


# ── 在庫判定ロジック ───────────────────────────────────────

def _fetch(url: str, timeout: int = 15) -> BeautifulSoup | None:
    """URLをGETしてBeautifulSoupを返す。失敗時はNone。"""
    if not url or not url.startswith("http"):
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        log.warning(f"  fetch失敗 {url[:60]}... → {e}")
        return None


def check_rakuten(url: str) -> str:
    """
    楽天: 在庫切れ時は「品切れ」「売り切れ」「ただいま品切れ」などが出る。
    カートボタンの有無でも判定できる。
    """
    soup = _fetch(url)
    if soup is None:
        return STATUS_UNKNOWN

    page_text = soup.get_text(separator=" ").lower()

    # 在庫なしキーワード
    out_keywords = ["品切れ", "売り切れ", "在庫なし", "soldout", "sold out", "ただいま品切れ"]
    for kw in out_keywords:
        if kw in page_text:
            log.info(f"    楽天: 在庫なし（キーワード: {kw}）")
            return STATUS_OUT

    # カートボタンが存在するか（class名は変わることがある）
    cart_btn = soup.select_one(
        'input[value*="カートに入れる"], button[class*="addCart"], '
        'a[class*="cart"], input[name*="purchase"]'
    )
    if cart_btn:
        log.info("    楽天: 在庫あり（カートボタン検出）")
        return STATUS_IN

    # 楽天のvariantページで「在庫あり」文言を探す
    if "在庫あり" in page_text or "残りわずか" in page_text:
        return STATUS_IN

    log.info("    楽天: 判定不能")
    return STATUS_UNKNOWN


def check_yahoo(url: str) -> str:
    """
    Yahoo!ショッピング: 在庫切れ時は「在庫がありません」「SOLD OUT」が表示される。
    """
    soup = _fetch(url)
    if soup is None:
        return STATUS_UNKNOWN

    page_text = soup.get_text(separator=" ").lower()

    out_keywords = ["在庫がありません", "売り切れ", "sold out", "品切れ"]
    for kw in out_keywords:
        if kw in page_text:
            log.info(f"    Yahoo!: 在庫なし（キーワード: {kw}）")
            return STATUS_OUT

    cart_btn = soup.select_one('button[class*="addCart"], a[class*="purchase"], .buy-button')
    if cart_btn:
        log.info("    Yahoo!: 在庫あり（カートボタン検出）")
        return STATUS_IN

    if "カートに入れる" in page_text or "今すぐ購入" in page_text:
        return STATUS_IN

    log.info("    Yahoo!: 判定不能")
    return STATUS_UNKNOWN


def check_amazon(url: str) -> str:
    """
    Amazon: availability要素またはキーワードで判定。
    ※Amazonはボット対策が特に強いため、判定不能になりやすい。
    """
    soup = _fetch(url)
    if soup is None:
        return STATUS_UNKNOWN

    # id="availability" が最も確実
    avail_el = soup.select_one("#availability span, #availability-string")
    if avail_el:
        text = avail_el.get_text(strip=True).lower()
        if any(kw in text for kw in ["in stock", "在庫あり", "残り", "注文可能"]):
            log.info("    Amazon: 在庫あり（availability要素）")
            return STATUS_IN
        if any(kw in text for kw in ["out of stock", "在庫なし", "入荷待ち", "currently unavailable"]):
            log.info("    Amazon: 在庫なし（availability要素）")
            return STATUS_OUT

    # カートボタン
    if soup.select_one("#add-to-cart-button"):
        log.info("    Amazon: 在庫あり（カートボタン検出）")
        return STATUS_IN

    log.info("    Amazon: 判定不能")
    return STATUS_UNKNOWN


def check_mercari(url: str) -> str:
    """
    メルカリShops: 「SOLD」「売り切れ」バッジ、またはカートボタンの有無で判定。
    """
    soup = _fetch(url)
    if soup is None:
        return STATUS_UNKNOWN

    page_text = soup.get_text(separator=" ").lower()

    out_keywords = ["sold", "売り切れ", "販売終了", "この商品は販売終了"]
    for kw in out_keywords:
        if kw in page_text:
            log.info(f"    メルカリ: 在庫なし（キーワード: {kw}）")
            return STATUS_OUT

    # 購入ボタン
    buy_btn = soup.select_one(
        'button[class*="buy"], button[class*="purchase"], '
        '[data-testid*="buy"], [class*="addToCart"]'
    )
    if buy_btn:
        log.info("    メルカリ: 在庫あり（購入ボタン検出）")
        return STATUS_IN

    if "カートに追加" in page_text or "購入手続きへ" in page_text:
        return STATUS_IN

    log.info("    メルカリ: 判定不能")
    return STATUS_UNKNOWN


PLATFORM_CHECKERS = {
    "rakuten": check_rakuten,
    "yahoo":   check_yahoo,
    "amazon":  check_amazon,
    "mercari": check_mercari,
}

PLATFORM_URL_COLS = {
    "rakuten": COL_RAKUTEN_URL,
    "yahoo":   COL_YAHOO_URL,
    "amazon":  COL_AMAZON_URL,
    "mercari": COL_MERCARI_URL,
}


# ── Google Sheets 連携 ─────────────────────────────────────

def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def load_products(sheet) -> list[ProductRow]:
    """シートからURL一覧を読み込む（2行目以降、ヘッダーを除く）"""
    all_values = sheet.get_all_values()
    products = []
    for i, row in enumerate(all_values[1:], start=2):  # 2行目から
        def cell(col_idx: int) -> str:
            idx = col_idx - 1
            return row[idx].strip() if idx < len(row) else ""

        name = cell(COL_PRODUCT_NAME)
        if not name:
            continue
        products.append(ProductRow(
            row_index=i,
            name=name,
            rakuten_url=cell(COL_RAKUTEN_URL),
            yahoo_url=cell(COL_YAHOO_URL),
            amazon_url=cell(COL_AMAZON_URL),
            mercari_url=cell(COL_MERCARI_URL),
        ))
    log.info(f"読み込み完了: {len(products)} 商品")
    return products


def write_results(sheet, product: ProductRow, result: CheckResult):
    """1商品分の結果をシートに書き戻す"""
    row = product.row_index
    updates = [
        gspread.Cell(row, COL_RAKUTEN_STS, result.rakuten),
        gspread.Cell(row, COL_YAHOO_STS,   result.yahoo),
        gspread.Cell(row, COL_AMAZON_STS,  result.amazon),
        gspread.Cell(row, COL_MERCARI_STS, result.mercari),
        gspread.Cell(row, COL_UPDATED_AT,  result.updated_at),
    ]
    sheet.update_cells(updates, value_input_option="USER_ENTERED")


# ── メイン処理 ────────────────────────────────────────────

def check_product(product: ProductRow) -> CheckResult:
    """1商品の全モールを順番にチェック"""
    result = CheckResult()
    url_map = {
        "rakuten": product.rakuten_url,
        "yahoo":   product.yahoo_url,
        "amazon":  product.amazon_url,
        "mercari": product.mercari_url,
    }
    status_map = {}

    for platform, url in url_map.items():
        if not url:
            status_map[platform] = "—"  # URLなし
            continue
        log.info(f"  チェック中: [{platform}] {url[:60]}...")
        checker = PLATFORM_CHECKERS[platform]
        status_map[platform] = checker(url)
        # サーバー負荷軽減のため待機（2〜5秒のランダム）
        time.sleep(random.uniform(2, 5))

    result.rakuten  = status_map.get("rakuten", STATUS_UNKNOWN)
    result.yahoo    = status_map.get("yahoo",   STATUS_UNKNOWN)
    result.amazon   = status_map.get("amazon",  STATUS_UNKNOWN)
    result.mercari  = status_map.get("mercari", STATUS_UNKNOWN)
    return result


def run():
    log.info("=" * 60)
    log.info(f"在庫チェック開始: {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}")

    sheet = get_sheet()
    products = load_products(sheet)

    out_alert = []  # 在庫なし商品の収集

    for i, product in enumerate(products, 1):
        log.info(f"[{i}/{len(products)}] {product.name}")
        result = check_product(product)
        write_results(sheet, product, result)

        # 在庫なしがある商品を記録
        outs = []
        for platform, status in [
            ("楽天", result.rakuten),
            ("Yahoo!", result.yahoo),
            ("Amazon", result.amazon),
            ("メルカリ", result.mercari),
        ]:
            if status == STATUS_OUT:
                outs.append(platform)
        if outs:
            out_alert.append(f"{product.name}（{', '.join(outs)}）")

        # シートAPI制限対策（1商品ごとに少し待機）
        time.sleep(1)

    log.info(f"\n✅ チェック完了: {len(products)} 商品")
    if out_alert:
        log.warning(f"⚠️  在庫切れ検出: {len(out_alert)} 件")
        for item in out_alert:
            log.warning(f"   - {item}")
    else:
        log.info("在庫切れなし")

    log.info("=" * 60)


if __name__ == "__main__":
    run()
