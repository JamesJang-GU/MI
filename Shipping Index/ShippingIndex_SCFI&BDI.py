import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

# =============================================================================
# 1. 환경 설정 및 경로 정의
# =============================================================================

# 현재 스크립트가 위치한 폴더
# 예: MI/Shipping Index/
SCRIPT_DIR = Path(__file__).resolve().parent

# 프로젝트 루트 폴더
# 예: MI/
BASE_DIR = SCRIPT_DIR.parent

# SQLite DB 위치
DB_PATH = BASE_DIR / "index_history.db"

# GitHub Pages에 게시할 결과 파일 위치
JSON_PATH = SCRIPT_DIR / "shipping_index_data.json"
HTML_PATH = SCRIPT_DIR / "index.html"

# 처리 대상 지수
SUPPORTED_INDEXES = ["SCFI", "BDI"]

# =============================================================================
# 2. 공통 유틸리티
# =============================================================================

def log(message: str) -> None:
    """시간과 함께 실행 로그를 출력한다."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def get_timestamp() -> str:
    """대시보드 표시 및 JSON 기록에 사용할 시간 문자열을 반환한다."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def atomic_write_text(path: Path, text: str) -> None:
    """
    파일을 임시 파일에 먼저 작성한 뒤 교체한다.
    작업 중 중단되더라도 빈 파일 또는 깨진 파일이 배포되는 위험을 줄인다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(path.suffix + ".tmp")

    with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(text)

    temp_path.replace(path)

def sort_regions_for_display(regions) -> list:
    """Routes/Regions 목록을 화면 표시용으로 정렬한다."""
    cleaned_regions = []

    for region in regions:
        if pd.isna(region):
            continue

        region_text = str(region).strip()

        if region_text:
            cleaned_regions.append(region_text)

    return sorted(set(cleaned_regions), key=lambda item: item.lower())

# =============================================================================
# 3. SQLite 데이터 조회 및 정제
# =============================================================================

def load_long_df_from_db(index_name: str) -> pd.DataFrame:
    """
    shipping_indices 테이블에서 특정 지수 데이터를 불러온다.

    기대 테이블 구조:
    - DATE
    - REGION
    - VALUE
    - INDEX_NAME
    """
    query = """
        SELECT
            DATE,
            REGION,
            VALUE,
            INDEX_NAME
        FROM shipping_indices
        WHERE UPPER(INDEX_NAME) = UPPER(?)
        ORDER BY DATE ASC
    """

    empty_dataframe = pd.DataFrame(
        columns=["DATE", "REGION", "VALUE", "INDEX_NAME"]
    )

    if not DB_PATH.exists():
        log(f"[Error] DB 파일을 찾을 수 없습니다: {DB_PATH}")
        return empty_dataframe

    try:
        with sqlite3.connect(DB_PATH) as connection:
            dataframe = pd.read_sql_query(
                query,
                connection,
                params=[index_name]
            )

        log(f"[DB] {index_name} 데이터 로드 완료: {len(dataframe):,}건")
        return dataframe

    except Exception as error:
        log(f"[Error] {index_name} DB 로드 실패: {error}")
        return empty_dataframe

def clean_index_dataframe(dataframe: pd.DataFrame) -> pd.DataFrame:
    """
    대시보드 출력에 적합하도록 날짜·수치·지역 데이터를 정제한다.
    """
    if dataframe.empty:
        return dataframe.copy()

    cleaned = dataframe.copy()

    # 날짜 정제
    cleaned["DATE"] = pd.to_datetime(
        cleaned["DATE"],
        errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    # 수치 정제
    cleaned["VALUE"] = pd.to_numeric(
        cleaned["VALUE"],
        errors="coerce"
    )

    # 지역 정제
    cleaned["REGION"] = (
        cleaned["REGION"]
        .fillna("Unknown")
        .astype(str)
        .str.strip()
    )

    # INDEX_NAME 정제
    cleaned["INDEX_NAME"] = (
        cleaned["INDEX_NAME"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
    )

    # 날짜 또는 값이 없는 행 제거
    cleaned = cleaned.dropna(subset=["DATE", "VALUE"])

    # 날짜 + 지역 기준 중복 행 제거
    # 마지막 데이터를 유지하여 최신 크롤링 결과를 반영
    cleaned = cleaned.drop_duplicates(
        subset=["DATE", "REGION"],
        keep="last"
    )

    # 정렬
    cleaned = cleaned.sort_values(
        by=["DATE", "REGION"],
        ascending=[True, True]
    ).reset_index(drop=True)

    # JSON 직렬화 안정성을 위해 float로 명시 변환
    cleaned["VALUE"] = cleaned["VALUE"].astype(float)

    return cleaned

# =============================================================================
# 4. 대시보드 JSON 생성
# =============================================================================

def build_dashboard_json() -> None:
    """
    대시보드용 JSON 파일을 생성한다.

    생성 구조 예시:
    {
      "generated_at": "2026-10-19 09:00:00",
      "status": "success",
      "indexes": {
        "SCFI": {
          "regions": ["Composite", "Europe"],
          "records": [
            {
              "DATE": "2026-10-16",
              "REGION": "Composite",
              "VALUE": 1234.56,
              "INDEX_NAME": "SCFI"
            }
          ],
          "last_sync": "2026-10-19 09:00:00"
        }
      }
    }
    """
    log("[JSON] shipping_index_data.json 생성 시작")

    indexes_payload = {}
    generated_at = get_timestamp()

    for index_name in SUPPORTED_INDEXES:
        raw_dataframe = load_long_df_from_db(index_name)
        dataframe = clean_index_dataframe(raw_dataframe)

        if dataframe.empty:
            log(f"[Warn] {index_name}에 표시할 데이터가 없습니다.")

            records = []
            regions = []

        else:
            records = dataframe.to_dict(orient="records")
            regions = sort_regions_for_display(dataframe["REGION"].unique())

            log(
                f"[JSON] {index_name}: "
                f"레코드 {len(records):,}건 / Routes {len(regions):,}개"
            )

        indexes_payload[index_name] = {
            "regions": regions,
            "records": records,
            "last_sync": generated_at
        }

    payload = {
        "generated_at": generated_at,
        "status": "success",
        "indexes": indexes_payload
    }

    json_text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        allow_nan=False
    )

    atomic_write_text(JSON_PATH, json_text)

    log(f"[JSON] 생성 완료: {JSON_PATH}")

# =============================================================================
# 5. index.html 생성
# =============================================================================

def generate_html_file() -> None:
    """
    GitHub Pages용 정적 대시보드 index.html을 생성한다.

    중요:
    - Python f-string을 사용하지 않는다.
    - JavaScript의 중괄호({}), 백틱(`), 문자열이 Python 코드와 충돌하지 않는다.
    - HTML 파일은 shipping_index_data.json을 fetch하여 차트와 표를 렌더링한다.
    """
    log("[HTML] index.html 생성 시작")

    html_content = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="SCFI 및 BDI Shipping Index Dashboard">
  <title>Shipping Index Dashboard | SCFI & BDI</title>

  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>

  <style>
    :root {
      --navy: #0f2747;
      --blue: #1f5fa8;
      --sky: #edf5fc;
      --line: #d9e1ea;
      --text: #1c2733;
      --muted: #687583;
      --white: #ffffff;
      --green: #138a57;
      --red: #c73c3c;
      --shadow: 0 8px 24px rgba(15, 39, 71, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      color: var(--text);
      background: #f4f7fa;
      font-family: Arial, "Noto Sans KR", sans-serif;
    }

    .header {
      color: var(--white);
      background: linear-gradient(135deg, #0f2747 0%, #1f5fa8 100%);
      padding: 34px 20px;
    }

    .header-inner {
      max-width: 1400px;
      margin: 0 auto;
