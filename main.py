# --- ① 標準ライブラリ ---
import os
import json
import re
import time
import unicodedata

# --- ② 外部ライブラリ ---
import requests
from bs4 import BeautifulSoup
import pandas as pd  # スプレッドシート一括書き込み用に強く推奨

# --- ③ Google API & 認証関連（GitHub Actions対応版） ---
import gspread
from google import genai
from google.genai import types
from google.auth import default
from google.oauth2.service_account import Credentials

# =========================================================================
# インフラ接続系関数
# =========================================================================
def upload_to_google_sheet(df: pd.DataFrame, spreadsheet_title: str, sheet_name: str):
    """
    【GitHub Actions専用】環境変数からサービスアカウントの鍵を読み込んで認証するエンジン
    """
    print(f"\n🚀 Google スプレッドシートへデータを転送中...")
    try:
        # GitHubの隠し金庫（環境変数）からJSON文字列を回収
        secret_json = os.environ.get("GCP_SERVICE_ACCOUNT_KEY")
        if not secret_json:
            raise ValueError("環境変数 'GCP_SERVICE_ACCOUNT_KEY' が見つかりません。")

        service_account_info = json.loads(secret_json)

        # 認証スコープの設定
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]

        # 資格情報の生成とクライアント認証
        creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
        gc = gspread.authorize(creds)

        # スプレッドシートを更新
        sh = gc.open(spreadsheet_title)
        worksheet = sh.worksheet(sheet_name)
        worksheet.clear()

        data_to_write = [df.columns.values.tolist()] + df.values.tolist()
        worksheet.update('A1', data_to_write)

        print(f"✨ 転送成功！『{spreadsheet_title}』の『{sheet_name}』シートを更新しました。")

    except Exception as e:
        print(f"❌ スプレッドシート転送失敗: {e}")
        raise e # エラーを発生させてGitHubのログに赤ランプを灯す

# =========================================================================
# ⚙️ 【中央集権データベース】
# =========================================================================
TARGET_FUNDS = {
    "KKR": {
        "cik": "0001404912",  # KKRのCIK
        "keywords": ["dry powder", "distributions", "new capital", "assets under management"]
    },
    "Carlyle": {
        "cik": "0001527166",  # Carlyle
        "keywords": ["available capital", "realizations", "inflow", "assets under management"]
    },
    "Apollo": {
        "cik": "0001858681",  # Apollo Global Management, Inc.
        "keywords": ["dry powder", "realizations", "inflows", "assets under management"]
    },
    "Brookfield_AM": {
        "cik": "0001937926",  # Brookfield Asset Management Ltd. (BAM)
        "keywords": ["uninvested capital", "distributions", "inflows", "assets under management"]
    }
}

SEC_HEADERS = {
    'User-Agent': 'CorporateAnalystResearch/1.0 (analyst_data@example.com)'
}

# =========================================================================
# 📡 【SEC-API自動索敵エンジン】
# =========================================================================
def get_latest_10q_url(cik: str) -> str:
    cik_padded = str(cik).zfill(10)
    api_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    res = requests.get(api_url, headers=SEC_HEADERS)

    if res.status_code != 200:
        raise Exception(f"SEC APIへのアクセスに失敗しました (HTTP {res.status_code})")

    submission_data = res.json()
    recent_filings = submission_data['filings']['recent']

    latest_10q_index = None
    for i, form_type in enumerate(recent_filings['form']):
        if form_type == '10-Q':
            latest_10q_index = i
            break

    if latest_10q_index is None:
        raise Exception("直近の提出書類の中に '10-Q' が見つかりませんでした。")

    acc_num       = recent_filings['accessionNumber'][latest_10q_index]
    acc_num_clean = acc_num.replace('-', '')
    doc_name      = recent_filings['primaryDocument'][latest_10q_index]

    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_num_clean}/{doc_name}"

# =========================================================================
# 🧹 【汎用AIマイニング特化型・最強前処理エンジン】
# clean_ixbrl を廃止し、こちらに一本化
# =========================================================================
def notebooklm_style_cleaner(html_content: str) -> str:
    # 掃除屋1: HTMLタグ・iXBRLコードを分解
    soup = BeautifulSoup(html_content, "html.parser")

    for tag in soup(["script", "style", "noscript", "header", "footer"]):
        tag.extract()

    # iXBRLタグをテキストのみに置換
    for tag in soup.find_all(re.compile(r'^ix:')):
        tag.replace_with(tag.get_text())

    # 掃除屋2: テーブルをMarkdown形式に変換（AIの数値誤認を防ぐ核心処理）
    for table in soup.find_all("table"):
        markdown_table = []
        for row in table.find_all("tr"):
            cells = [
                re.sub(r'\s+', ' ', cell.get_text().strip())
                for cell in row.find_all(["td", "th"])
            ]
            if any(cells):
                markdown_table.append("| " + " | ".join(cells) + " |")

        if markdown_table:
            table.replace_with("\n" + "\n".join(markdown_table) + "\n")

    # プレーンテキストとして抽出
    text = soup.get_text()

    # 掃除屋3: 全角英数・特殊記号を標準形に正規化
    text = unicodedata.normalize("NFKC", text)

    # 掃除屋4: 空白・改行の圧縮
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)

    return text.strip()

# =========================================================================
# 🎯 【狙い撃ち抽出関数】
# =========================================================================
def extract_target_section(clean_text: str, keywords: list, window: int = 10000) -> str:
    text_lower = clean_text.lower()
    extracted_sections = []
    found_positions = set()

    for keyword in keywords:
        pos = 0
        while True:
            idx = text_lower.find(keyword.lower(), pos)
            if idx == -1:
                break

            is_duplicate = any(abs(idx - fp) < 3000 for fp in found_positions)
            if not is_duplicate:
                start = max(0, idx - 1000)
                end   = min(len(clean_text), idx + window)
                extracted_sections.append(clean_text[start:end])
                found_positions.add(idx)

            pos = idx + 1

    if not extracted_sections:
        return ""

    result = "\n\n--- [セクション区切り] ---\n\n".join(extracted_sections)
    print(f"  [抽出完了] {len(extracted_sections)} セクション / 合計 {len(result):,} 文字")
    return result

# =========================================================================
# 🧠 【共通AIエンジン】
# =========================================================================
def call_gemini_api_financials(fund_name: str, raw_text: str) -> str:
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    client  = genai.Client(api_key=api_key)

    system_instruction = (
        f"あなたは政策・規制・産業インフラに精通した冷徹な金融アナリストである。\n"
        f"提示されたSEC提出書類（Markdown形式に変換済みのテキスト）を解析し、"
        f"指定されたJSONフォーマットに従って厳密に出力せよ。\n"
        f"挨拶、解説、装飾は一切不要。生JSON文字列のみを返せ。\n\n"
        f"【絶対規律】\n"
        f"- テキストに記載されている数値を勝手に丸めたり省略したりすることは絶対に禁止する。\n"
        f"- 原文に『213.3 billion』や『200,345,000』と書かれていたら、必ずその数字のまま文字列として抽出せよ。\n"
        f"- 単位（billions / millions / thousands等）がテーブルのヘッダーや行内に明記されている場合は、必ずそれを見落とさずに数値の後ろに付記せよ。\n\n"
        f"【財務定義の厳密化（誤爆回避）】\n"
        f"  1. dry_powder: 表の中で『Total Available Capital』または『Available Capital』、もしくは『Total Unfunded Commitments』として記載されている総額。意味論的説明文ではなく、数字のテーブルから引くこと。\n"
        f"  2. total_aum: 『Total Assets Under Management (Total AUM)』の総額。1,000,000 Millions（1兆ドル換算）前後の規模の数字をターゲットにせよ。\n"
        f"  3. inflow_capital: アセットマネジメント部門総括（Segment Results）やAUM変動表にある、今期（Three Months Ended）の『Inflows』または『Capital Raised』の総額。\n"
        f"  4. outflow_capital: アセットマネジメント部門総括（Segment Results）やAUM変動表にある、今期（Three Months Ended）の『Realizations』または『Distributions』の総額。キャッシュフロー計算書（Statement of Cash Flows）の営業活動・投資活動の数字と混同するな。redemptionも含めること。"
    )

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=f"解析対象テキスト:\n{raw_text}",
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0,
            response_mime_type="application/json",
            response_schema=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "fund_name":       types.Schema(type=types.Type.STRING),
                    "as_of_date":      types.Schema(type=types.Type.STRING),
                    "dry_powder":      types.Schema(type=types.Type.STRING, description="原文の生数字＋単位"),
                    "total_aum":       types.Schema(type=types.Type.STRING, description="原文の生数字＋単位"),
                    "inflow_capital":  types.Schema(type=types.Type.STRING, description="原文の生数字＋単位"),
                    "outflow_capital": types.Schema(type=types.Type.STRING, description="原文の生数字＋単位"),
                    "context_summary": types.Schema(type=types.Type.STRING, description="数値を抽出する根拠となったMarkdownテーブルの該当行（例: '| Total Available Capital | 213,345,000 |' など）を原文のまま完全コピペして記載せよ。")
                },
                required=["fund_name", "as_of_date", "dry_powder", "total_aum",
                          "inflow_capital", "outflow_capital"]
            )
        )
    )
    return response.text

# =========================================================================
# 🧹 【単位統一クレンジング関数】millions/billions/trillionを統一してfloat化
# =========================================================================
def clean_to_float(val_str: str) -> float:
    if not val_str:
        return 0.0

    val_str = str(val_str).strip().lower()

    # マイナス表記「(15,000)」の検出
    is_negative = val_str.startswith('(') and val_str.endswith(')')

    # 数値部分を抽出（カンマ・$・スペース・"over"等の修飾語を除去）
    num_match = re.search(r'[\d,]+\.?\d*', val_str)
    if not num_match:
        return 0.0

    num_str = num_match.group().replace(',', '')
    try:
        num = float(num_str)
    except ValueError:
        return 0.0

    # 単位を検出してMillions（百万ドル）に統一換算
    if 'trillion' in val_str:
        num = num * 1_000_000  # 1 trillion = 1,000,000 millions
    elif 'billion' in val_str:
        num = num * 1_000      # 1 billion = 1,000 millions
    elif 'million' in val_str:
        num = num * 1          # そのまま
    # 単位記載なし → そのまま（Geminiの出力がMillions前提）

    return -num if is_negative else num

# =========================================================================
# 📊 【Google Sheets書き込みエンジン】
# =========================================================================
def get_or_create_sheet(spreadsheet_id: str, sheet_name: str = "FinancialData"):
    """
    指定スプレッドシートのシートを取得。なければ新規作成してヘッダーを挿入。
    """
    creds, _ = default()
    gc       = gspread.authorize(creds)

    spreadsheet = gc.open_by_key(spreadsheet_id)

    # シートが存在するか確認
    try:
        sheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=10)
        # ★ ヘッダーを1行目に挿入
        sheet.append_row([
            "Date",
            "Fund",
            "Dry_Powder_M",
            "Total_AUM_M",
            "Inflow_M",
            "Outflow_M",
            "DP_AUM_Ratio_Pct",
            "InOut_Ratio",
            "Source_URL"
        ])
        print(f"  [Sheets] シート '{sheet_name}' を新規作成しました。")

    return sheet
# =========================================================================
# 🔍 【重複チェック関数】既存URLをシートからキャッシュして照合
# =========================================================================
def get_existing_urls(sheet) -> set:
    all_rows = sheet.get_all_values()
    if len(all_rows) < 2:
        return set()

    urls = set(
        row[8].strip()
        for row in all_rows[1:]  # ヘッダー行をスキップ
        if len(row) >= 9 and row[8].strip()
    )

    print(f"  [重複チェック] 既存URL {len(urls)} 件をキャッシュ")
    return urls
# =========================================================================
# スプシに書き込み
# =========================================================================

def write_to_spreadsheet(
    sheet,
    as_of_date: str,
    fund_name:  str,
    dp:         float,
    aum:        float,
    inflow:     float,
    outflow:    float,
    dp_aum_ratio:  float,
    in_out_ratio:  float,
    source_url: str
):
    """
    計算済みの数値を1行としてスプレッドシートに追記する
    """
    row = [
        as_of_date,
        fund_name,
        round(dp,    1),
        round(aum,   1),
        round(inflow,  1),
        round(outflow, 1),
        round(dp_aum_ratio, 4),
        round(in_out_ratio, 4),
        source_url
    ]
    sheet.append_row(row)
    print(f"  [Sheets] {fund_name} ({as_of_date}) を書き込みました。")
# =========================================================================
# 📊 【数理分析エンジン】単位統一後に比率計算
# =========================================================================
def calculate_financial_ratios(json_str: str, sourced_url: str) -> dict:
    data = json.loads(json_str)

    dp          = clean_to_float(data["dry_powder"])
    aum         = clean_to_float(data["total_aum"])
    inflow      = clean_to_float(data["inflow_capital"])
    outflow     = clean_to_float(data["outflow_capital"])
    outflow_abs = abs(outflow)

    dp_aum_ratio = (dp / aum) * 100 if aum > 0 else 0
    in_out_ratio = (inflow / outflow_abs) if outflow_abs > 0 else 0

    # ── コンソール表示（変更なし） ──
    print("\n================== 📊 財務数理分析レポート ==================")
    print(f"対象ファンド : {data['fund_name']}")
    print(f"データ基準日 : {data['as_of_date']}")
    print(f"自動検知URL  : {sourced_url}")
    print("-" * 70)
    print(f"【AIが抽出した原文データ】")
    print(f"  ・投資待機資金 (Dry Powder) : {data['dry_powder']}")
    print(f"  ・総管理資産   (Total AUM)  : {data['total_aum']}")
    print(f"  ・新規調達額   (Inflow)     : {data['inflow_capital']}")
    print(f"  ・売却分配額   (Outflow)    : {data['outflow_capital']}")
    print("-" * 70)
    print(f"【単位統一後（Millions換算）】")
    print(f"  ・Dry Powder  : ${dp:>15,.1f} M")
    print(f"  ・Total AUM   : ${aum:>15,.1f} M")
    print(f"  ・Inflow      : ${inflow:>15,.1f} M")
    print(f"  ・Outflow     : ${outflow_abs:>15,.1f} M")
    print("-" * 70)
    print(f"【Pythonによる数理分析】")
    print(f"  ① AUMに対するDry Powderの占有率 : {dp_aum_ratio:.4f}%")
    print(f"  ② 資金回転倍率 (Inflow / Outflow): {in_out_ratio:.4f} 倍")
    print("-" * 70)
    print(f"【根拠文脈】: {data.get('context_summary', 'N/A')}")
    print("=============================================================")

    # ★ 呼び出し元に数値を返す
    return {
        "as_of_date":    data["as_of_date"],
        "fund_name":     data["fund_name"],
        "dp":            dp,
        "aum":           aum,
        "inflow":        inflow,
        "outflow":       outflow_abs,
        "dp_aum_ratio":  dp_aum_ratio,
        "in_out_ratio":  in_out_ratio
    }
# =========================================================================
# 🚀 【メイン巡回エンジン】
# =========================================================================
def run_pipeline():
    SPREADSHEET_ID = "1u3HtebzKnq2zmXDDnZq7OslCbgcnpXPPkD8LQbCvMQM"
    SHEET_NAME     = "FinancialData"

    print(f"📡 自動索敵網を起動します。対象件数: {len(TARGET_FUNDS)}件")
    print("-" * 60)

    sheet = get_or_create_sheet(SPREADSHEET_ID, SHEET_NAME)

    # ★ 追加①: 既存URLを一括キャッシュ
    existing_urls = get_existing_urls(sheet)
    print(f"  [デバッグ] キャッシュされたURL一覧:")
    for u in existing_urls:
        print(f"    - {u}")

    for name, info in TARGET_FUNDS.items():
        print(f"\n[ターゲット検知] {name} (CIK: {info['cik']})")

        try:
            print("  [URL索敵] 最新の10-Q URLを自動生成中...")
            latest_url = get_latest_10q_url(info['cik'])
            print(f"  [URL確定] {latest_url}")

            # ★ 追加②: 重複チェック
            if latest_url in existing_urls:
                print(f"  [スキップ] 処理済みURLのため省略: {latest_url}")
                continue

            res = requests.get(latest_url, headers=SEC_HEADERS)
            if res.status_code != 200:
                print(f"  [拒絶] HTTP {res.status_code}")
                continue
            print(f"  [取得完了] {len(res.text):,} 文字")

            print("  [前処理] notebooklm_style_cleaner を実行中...")
            clean_text = notebooklm_style_cleaner(res.text)
            print(f"  [前処理完了] {len(res.text):,} 文字 → {len(clean_text):,} 文字")

            keywords = info.get("keywords", ["dry powder", "available capital"])
            raw_text = extract_target_section(clean_text, keywords, window=10000)
            if not raw_text:
                print("  [警告] キーワード不検出。先頭10万文字で代替します。")
                raw_text = clean_text[:100000]

            print(f"  [AI解析] Geminiに {len(raw_text):,} 文字を渡して解析中...")
            json_result = call_gemini_api_financials(name, raw_text)

            result = calculate_financial_ratios(json_result, latest_url)

            write_to_spreadsheet(
                sheet        = sheet,
                as_of_date   = result["as_of_date"],
                fund_name    = result["fund_name"],
                dp           = result["dp"],
                aum          = result["aum"],
                inflow       = result["inflow"],
                outflow      = result["outflow"],
                dp_aum_ratio = result["dp_aum_ratio"],
                in_out_ratio = result["in_out_ratio"],
                source_url   = latest_url
            )

            # ★ 追加③: 書き込み成功後にキャッシュへ追加
            existing_urls.add(latest_url)

            time.sleep(3)

        except Exception as e:
            print(f"  [システムエラー] 処理失敗: {e}")

    print("\n[索敵完了] すべての処理を終了しました。")

if __name__ == "__main__":
    run_pipeline()
