import os
import json
import pandas as pd
import sqlite3
from datetime import datetime
from pathlib import Path

# =============================================================================
# 1. 환경 설정 및 경로 정의
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "index_history.db"
JSON_PATH = BASE_DIR / "Shipping Index" / "shipping_index_data.json"
HTML_PATH = BASE_DIR / "Shipping Index" / "index.html"

# 지원하는 인덱스 정의 (HTML의 탭 구조와 일치해야 함)
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
    """DB에서 특정 인덱스의 데이터를 불러온다."""
    try:
        conn = sqlite3.connect(DB_PATH)
        # INDEX_NAME이 대소문자 구분 없이 일치하는 데이터 로드
        query = f"SELECT * FROM shipping_indices WHERE UPPER(INDEX_NAME) = UPPER('{index_name}')"
        df = pd.read_sql_query(query, conn)
        conn.close()
        return df
    except Exception as e:
        log(f"[Error] DB 로드 실패 ({index_name}): {e}")
        return pd.DataFrame()

def sort_regions_for_display(regions, index_name):
    """인덱스별로 지역(Region) 목록을 정렬하여 반환"""
    return sorted([str(r) for r in regions if r])

def get_last_sync_meta(index_name):
    """마지막 동기화 시간 정보 (DB에서 가져오거나 현재 시간 반환)"""
    # 실제 운영 환경에서는 DB의 마지막 업데이트 시간을 가져오는 로직을 권장
    return {"last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

# =============================================================================
# 3. 핵심 로직: JSON 및 HTML 생성
# =============================================================================

def build_dashboard_json():
    """
    [핵심 수정사항] 
    HTML 대시보드가 기대하는 구조로 JSON을 생성한다.
    구조: { "generated_at": "...", "indexes": { "BDI": { "regions": [...], "records": [...] } } }
    """
    log("[JSON] 대시보드용 JSON 구조 생성 시작...")
    
    indexes_payload = {}

    for index_name in SUPPORTED_INDEXES:
        df = load_long_df_from_db(index_name)

        if not df.empty:
            # 1. 데이터 복사 및 날짜 포맷팅 (JSON 직렬화 에러 방지)
            df = df.copy()
            df["DATE"] = pd.to_datetime(df["DATE"]).dt.strftime("%Y-%m-%d")
            
            # 2. 결측치 처리 (NaN은 JSON에서 null로 변환되나, 명확히 처리)
            df = df.dropna(subset=["DATE", "VALUE"])
            df["VALUE"] = df["VALUE"].astype(float)
            
            # 3. 레코드 변환
            records = df.to_dict(orient="records")
            
            # 4. 지역 목록 추출
            regions = sort_regions_for_display(df["REGION"].unique(), index_name)
        else:
            log(f"[Warn] {index_name} 데이터가 DB에 없습니다.")
            records = []
            regions = []

        indexes_payload[index_name] = {
            "regions": regions,
            "records": records,
            "last_sync": get_last_sync_meta(index_name)
        }

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indexes": indexes_payload,
        "status": "success"
    }

    atomic_write_text(
        JSON_PATH,
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    log(f"[JSON] 생성 완료: {JSON_PATH}")

def generate_html_file():
    """
    기존에 생성되던 index.html을 호출하거나, 
    만약 별도의 템플릿 파일이 있다면 그 파일을 읽어 완성된 HTML을 쓴다.
    여기서는 기존 로직이 HTML을 생성한다고 가정하되, 
    필요 시 직접 HTML 템플릿을 정의하여 작성할 수 있습니다.
    """
    log("[HTML] HTML 파일 생성을 시작합니다.")
    # 만약 별도의 generator가 없다면, 가장 안정적인 방법은 
    # Python 내부에 HTML 템플릿을 두고 f-string으로 완성하는 것입니다.
    # 현재는 기존 시스템의 동작을 유지하기 위해 로그만 남깁니다.
    # (실제 운영 시에는 이 함수 내에서 완성된 HTML 문자열을 atomic_write_text로 저장)
    pass

# =============================================================================
# 4. 메인 실행부
# =============================================================================
def main():
    log("=== Shipping Index (SCFI & BDI) 통합 프로세스 시작 ===")
    
    # 1. 데이터 수집 (Crawler 실행 부분 - 기존 코드 유지)
    # [이 부분에 기존 크롤링 및 DB Insert 로직이 위치합니다]
    # ... (기존 코드 생략) ...
    
    # 2. 데이터 정리 및 JSON 생성 (수정된 로직 호출)
    build_dashboard_json()
    
    # 3. HTML 파일 생성
    generate_html_file()
    
    log("=== 모든 프로세스 완료 ===")

if __name__ == "__main__":
    main()
