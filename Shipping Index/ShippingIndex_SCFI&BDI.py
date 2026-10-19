import os
import json
import pandas as pd
import sqlite3
from datetime import datetime
from pathlib import Path

# =============================================================================
# 1. 환경 설정 및 경로 정의
# =============================================================================
# 스크립트가 위치한 폴더 (MI/Shipping Index/)
SCRIPT_DIR = Path(__file__).resolve().parent
# 프로젝트 루트 폴더 (MI/)
BASE_DIR = SCRIPT_DIR.parent

# DB는 루트(MI/)에 위치
DB_PATH = BASE_DIR / "index_history.db"
# JSON과 HTML은 스크립트와 동일한 폴더(Shipping Index/)에 생성
JSON_PATH = SCRIPT_DIR / "shipping_index_data.json"
HTML_PATH = SCRIPT_DIR / "index.html"

# 지원하는 인덱스 정의
SUPPORTED_INDEXES = ["SCFI", "BDI"]

# =============================================================================
# 2. 유틸리티 함수
# =============================================================================
def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def atomic_write_text(path, text):
    """파일을 안전하게 쓰기 위해 임시 파일을 생성한 후 교체"""
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(text)
    temp_path.replace(path)

def load_long_df_from_db(index_name):
    """DB에서 특정 인덱스의 대시보드용 데이터를 안전하게 불러온다."""
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
    try:
        with sqlite3.connect(DB_PATH) as conn:
            df = pd.read_sql_query(query, conn, params=[index_name])
        log(f"[DB] {index_name} 로드 완료: {len(df):,} 건")
        return df
    except Exception as e:
        log(f"[Error] DB 로드 실패 ({index_name}): {e}")
        return pd.DataFrame(columns=["DATE", "REGION", "VALUE", "INDEX_NAME"])

def sort_regions_for_display(regions):
    """지역(Region) 목록을 정렬하여 반환"""
    return sorted([str(r) for r in regions if r])

def get_last_sync_str():
    """
    [핵심 수정] 
    JavaScript의 new Date()에서 오류가 나지 않도록 
    딕셔너리가 아닌 '단순 문자열'만 반환합니다.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# =============================================================================
# 3. 핵심 로직: JSON 생성
# =============================================================================

def build_dashboard_json():
    """
    HTML 대시보드가 기대하는 계층형 JSON 구조를 생성합니다.
    구조: { "generated_at": "...", "indexes": { "SCFI": { "regions": [...], "records": [...], "last_sync": "..." } } }
    """
    log("[JSON] 대시보드용 JSON 구조 생성 시작...")
    
    indexes_payload = {}

    for index_name in SUPPORTED_INDEXES:
        df = load_long_df_from_db(index_name)

        if not df.empty:
            df = df.copy()
            
            # 1. 데이터 정제 및 타입 강제
            df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce").dt.strftime("%Y-%m-%d")
            df["VALUE"] = pd.to_numeric(df["VALUE"], errors="coerce")
            df["REGION"] = df["REGION"].fillna("Unknown").astype(str).str.strip()
            
            # 2. 결측치 제거 및 중복 제거
            df = df.dropna(subset=["DATE", "VALUE"])
            df = df.drop_duplicates(subset=["DATE", "REGION"], keep="last")
            
            # 3. 레코드(List of Dict) 변환
            records = df.to_dict(orient="records")
            
            # 4. 지역 목록 추출
            regions = sort_regions_for_display(df["REGION"].unique())
        else:
            log(f"[Warn] {index_name} 데이터가 DB에 없습니다.")
            records = []
            regions = []

        # 각 인덱스별 페이로드 구성
        indexes_payload[index_name] = {
            "regions": regions,
            "records": records,
            "last_sync": get_last_sync_str()  # 문자열 반환 (중요)
        }

    # 최종 루트 페이로드 구성
    payload = {
        "generated_at": get_last_sync_str(),
        "indexes": indexes_payload,
        "status": "success"
    }

    # 출력 폴더 생성 보장
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 파일 저장
    atomic_write_text(
        JSON_PATH,
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    log(f"[JSON] 생성 완료: {JSON_PATH}")

def generate_html_file():
    """
    HTML 파일 생성 로직. 
    """
    log("[HTML] HTML 파일 생성을 시작합니다.")
    # 기존에 구현된 index.html 생성 함수가 있다면 여기서 호출
    pass

# =============================================================================
# 4. 메인 실행부
# =============================================================================
def main():
    log("=== Shipping Index (SCFI & BDI) 통합 프로세스 시작 ===")
    
    # 1. 데이터 정리 및 JSON 생성
    build_dashboard_json()
    
    # 2. HTML 파일 생성
    generate_html_file()
    
    log("=== 모든 프로세스 완료 ===")

if __name__ == "__main__":
    main()
