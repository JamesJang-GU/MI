# -*- coding: utf-8 -*-
"""
Patch script for BunkerPrice/bunker_price.py

적용 내용
1. Ship & Bunker URL의 #_VLSFO fragment 제거
2. Selenium driver page-load/script timeout 추가
3. VLSFO 포트별 timeout 및 실패 격리
4. Forecast 포트별 timeout 및 실패 격리
5. Opinet driver.get() timeout 처리 추가

실행:
    python patch_bunker_price.py
"""

from pathlib import Path
import re
import shutil
from datetime import datetime

TARGET_PATH = Path("BunkerPrice/bunker_price.py")

if not TARGET_PATH.exists():
    raise FileNotFoundError(
        f"대상 파일을 찾을 수 없습니다: {TARGET_PATH}\n"
        "프로젝트 루트에서 실행하거나 TARGET_PATH를 수정하십시오."
    )

def replace_exact(source, old, new, label):
    """정확한 문자열 1회 치환."""
    count = source.count(old)

    if count != 1:
        raise RuntimeError(
            f"[{label}] 교체 대상이 정확히 1개여야 합니다. "
            f"현재 발견 수: {count}"
        )

    print(f"[PATCH] {label}")
    return source.replace(old, new, 1)

def replace_function(source, function_name, next_marker, replacement):
    """
    특정 함수의 시작부터 다음 함수/섹션 marker 직전까지 교체한다.
    """
    pattern = (
        rf"^def {re.escape(function_name)}\(.*?"
        rf"(?=^{re.escape(next_marker)})"
    )

    matched = re.search(
        pattern,
        source,
        flags=re.MULTILINE | re.DOTALL,
    )

    if not matched:
        raise RuntimeError(
            f"함수 교체 실패: def {function_name}(...) "
            f"부터 {next_marker} 직전까지의 블록을 찾지 못했습니다."
        )

    print(f"[PATCH] Function: {function_name}")
    return (
        source[:matched.start()]
        + replacement.rstrip()
        + "\n\n"
        + source[matched.end():]
    )

def main():
    original = TARGET_PATH.read_text(encoding="utf-8")
    patched = original

    # =========================================================================
    # 1. Ship & Bunker URL fragment 제거
    # =========================================================================

    patched = replace_exact(
        patched,
        '''ROTTERDAM_URL = (
    "https://shipandbunker.com/prices/emea/nwe/nl-rtm-rotterdam#_VLSFO"
)
FUJAIRAH_URL = (
    "https://shipandbunker.com/prices/emea/me/ae-fjr-fujairah#_VLSFO"
)
SINGAPORE_URL = (
    "https://shipandbunker.com/prices/apac/sea/sg-sin-singapore#_VLSFO"
)''',
        '''# URL fragment(#_VLSFO)는 브라우저 내부 앵커이며 HTTP 요청에는
# 전달되지 않는다. Selenium page navigation 안정성을 위해 제거한다.
ROTTERDAM_URL = (
    "https://shipandbunker.com/prices/emea/nwe/nl-rtm-rotterdam"
)
FUJAIRAH_URL = (
    "https://shipandbunker.com/prices/emea/me/ae-fjr-fujairah"
)
SINGAPORE_URL = (
    "https://shipandbunker.com/prices/apac/sea/sg-sin-singapore"
)''',
        "Ship & Bunker URL fragment 제거",
    )

    # =========================================================================
    # 2. make_driver 교체
    # =========================================================================

    make_driver_replacement = r'''
def make_driver(headless=True):
    """
    GitHub Actions 및 로컬 환경에서 안정적으로 동작하는 Selenium Driver.

    주요 통제 항목:
    - page_load_strategy='eager':
      광고·추적기·비필수 이미지까지 모두 로드될 때까지 기다리지 않는다.
    - set_page_load_timeout(45):
      driver.get()의 무기한 대기를 방지한다.
    - set_script_timeout(30):
      execute_script()의 과도한 대기를 방지한다.
    """
    options = Options()

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1600,1000")
    else:
        options.add_argument("--start-maximized")

    # Ship & Bunker의 광고/외부 리소스 지연으로 인한 장시간 대기를 줄인다.
    options.page_load_strategy = "eager"

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")

    # GitHub Actions Headless Chrome 안정성 강화
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-background-timer-throttling")

    driver = webdriver.Chrome(options=options)

    # driver.get()이 네트워크 또는 차단 페이지에서 무기한 멈추지 않도록 제한
    driver.set_page_load_timeout(45)

    # JavaScript 실행 자체의 최대 대기 시간
    driver.set_script_timeout(30)

    # 명시적 WebDriverWait를 중심으로 사용하므로 implicit wait는 최소화
    driver.implicitly_wait(2)

    return driver
'''

    patched = replace_function(
        patched,
        "make_driver",
        "# =============================================================================\n# 4.",
        make_driver_replacement,
    )

    # =========================================================================
    # 3. collect_all_data 교체
    # =========================================================================

    collect_all_data_replacement = r'''
def collect_all_data():
    """
    Ship & Bunker에서 VLSFO 데이터를 수집한다.

    운영 원칙:
    - 개별 포트의 접속 실패가 전체 VLSFO 수집 실패로 전파되지 않도록 한다.
    - driver.get() page-load timeout 발생 시 부분 로딩된 페이지에서 추출을 시도한다.
    - 모든 포트가 실패한 경우 예외를 발생시켜 상위 함수가 SQLite 기존 데이터를
      fallback으로 사용하도록 한다.
    """
    log("[VLSFO] Ship & Bunker crawl started")

    driver = make_driver()
    all_rows = []
    errors = []

    try:
        for url in URLS:
            try:
                log(f"[VLSFO] Accessing: {url}")

                try:
                    driver.get(url)

                except TimeoutException:
                    log(
                        "[VLSFO][WARN] Page-load timeout after 45 seconds. "
                        f"Attempting partial page extraction: {url}"
                    )

                    try:
                        driver.execute_script("window.stop();")
                    except Exception:
                        pass

                # 최소 DOM 준비 상태 확인
                try:
                    WebDriverWait(driver, 15).until(
                        lambda current_driver: current_driver.execute_script(
                            "return document.readyState"
                        ) in ["interactive", "complete"]
                    )

                except TimeoutException:
                    log(
                        "[VLSFO][WARN] DOM readiness timeout. "
                        f"Continuing extraction attempt: {url}"
                    )

                # Highcharts 초기화 여부를 최대 20초만 대기
                try:
                    WebDriverWait(driver, 20).until(
                        lambda current_driver: current_driver.execute_script(
                            """
                            return (
                                typeof Highcharts !== 'undefined'
                                && Highcharts.charts
                                && Highcharts.charts.length > 0
                            );
                            """
                        )
                    )

                except TimeoutException:
                    raise RuntimeError(
                        "Highcharts was not initialized within 20 seconds."
                    )

                # All range button 클릭은 가능한 경우에만 시도
                try:
                    range_result = try_click_all_for_vlsfo(driver)

                    if range_result and range_result.get("ok"):
                        log(
                            "[VLSFO] Range selector set to ALL "
                            f"/ chart={range_result.get('chartIndex')}"
                        )
                        time.sleep(1)

                    else:
                        log(
                            "[VLSFO][WARN] Range selector was not available. "
                            "Extracting currently available chart data."
                        )

                except Exception as error:
                    log(
                        "[VLSFO][WARN] Range-selector interaction skipped: "
                        f"{error}"
                    )

                extracted = extract_vlsfo_full_data(driver)

                if not extracted:
                    raise RuntimeError(
                        "No VLSFO Highcharts series was extracted."
                    )

                port_row_count = 0

                for item in extracted:
                    series_name = str(
                        item.get("seriesName") or ""
                    ).strip()

                    if not series_name:
                        continue

                    port = series_name.replace(
                        " VLSFO",
                        "",
                    ).strip()

                    for point in item.get("data", []):
                        timestamp_ms = point.get("x")
                        price = point.get("y")

                        if timestamp_ms is None or price is None:
                            continue

                        all_rows.append({
                            "port": port,
                            "series_name": series_name,
                            "source_type": item.get(
                                "sourceType",
                                "unknown",
                            ),
                            "timestamp_ms": timestamp_ms,
                            "price": price,
                            "source_url": url,
                        })

                        port_row_count += 1

                if port_row_count == 0:
                    raise RuntimeError(
                        "VLSFO chart was found, but no valid price rows "
                        "were extracted."
                    )

                log(
                    f"[VLSFO] Collected {port_row_count:,} raw rows "
                    f"from {url}"
                )

            except Exception as error:
                error_message = f"{url}: {error}"
                errors.append(error_message)

                log(
                    "[VLSFO][WARN] Port crawl failed; "
                    f"continuing to next port. {error_message}"
                )

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # 모든 포트가 실패하면 상위 load_or_collect_data()가 DB fallback을 수행
    if not all_rows:
        detail = " | ".join(errors) if errors else "Unknown error"

        raise RuntimeError(
            "No VLSFO data collected from Ship & Bunker. "
            f"Details: {detail}"
        )

    df = pd.DataFrame(all_rows)

    df["timestamp_ms"] = pd.to_numeric(
        df["timestamp_ms"],
        errors="coerce",
    )

    df["price"] = pd.to_numeric(
        df["price"],
        errors="coerce",
    )

    df = df.dropna(
        subset=["timestamp_ms", "price"],
    )

    df["date"] = pd.to_datetime(
        df["timestamp_ms"],
        unit="ms",
        errors="coerce",
    )

    df = df.dropna(subset=["date"])

    df["date_str"] = df["date"].dt.strftime("%Y-%m-%d")

    df = (
        df.drop_duplicates(
            subset=["port", "date_str", "price"],
        )
        .sort_values(["port", "date"])
        .reset_index(drop=True)
    )

    if df.empty:
        raise RuntimeError(
            "VLSFO extraction completed but no valid normalized rows remain."
        )

    save_dataframe_to_db(
        df,
        PRODUCT_TABLE,
        PRODUCT_TABLE,
    )

    if errors:
        log(
            "[VLSFO][WARN] Partial collection completed. "
            + " | ".join(errors)
        )

    log(
        f"[VLSFO] Crawl completed successfully: "
        f"{len(df):,} normalized rows."
    )

    return df
'''

    patched = replace_function(
        patched,
        "collect_all_data",
        "def load_or_collect_data",
        collect_all_data_replacement,
    )

    # =========================================================================
    # 4. collect_all_forecast_data 교체
    # =========================================================================

    collect_all_forecast_replacement = r'''
def collect_all_forecast_data():
    """
    Ship & Bunker Forecast 테이블을 수집한다.

    개별 포트 오류는 격리한다. 모든 포트가 실패하면 상위 함수가 기존
    bunker_forecast 테이블을 fallback 데이터로 사용한다.
    """
    log("[FORECAST] Ship & Bunker forecast crawl started")

    driver = make_driver()
    frames = []
    errors = []

    try:
        for port_name, url in FORECAST_URLS.items():
            try:
                log(f"[FORECAST] Accessing: {port_name} / {url}")

                try:
                    driver.get(url)

                except TimeoutException:
                    log(
                        "[FORECAST][WARN] Page-load timeout after 45 seconds. "
                        f"Attempting partial page parsing: {port_name}"
                    )

                    try:
                        driver.execute_script("window.stop();")
                    except Exception:
                        pass

                try:
                    WebDriverWait(driver, 15).until(
                        lambda current_driver: current_driver.execute_script(
                            "return document.readyState"
                        ) in ["interactive", "complete"]
                    )

                except TimeoutException:
                    log(
                        "[FORECAST][WARN] DOM readiness timeout: "
                        f"{port_name}"
                    )

                page_source = driver.page_source

                if not page_source or len(page_source) < 500:
                    raise RuntimeError(
                        "Page source is empty or too short to parse."
                    )

                forecast_df = parse_bunker_forecast_from_html(
                    page_source,
                    port_name,
                )

                if forecast_df.empty:
                    raise RuntimeError(
                        "Forecast parser returned an empty DataFrame."
                    )

                forecast_df["port"] = port_name
                forecast_df["source_url"] = url

                frames.append(forecast_df)

                log(
                    f"[FORECAST] Collected {len(forecast_df):,} rows "
                    f"for {port_name}"
                )

            except Exception as error:
                error_message = f"{port_name}: {error}"
                errors.append(error_message)

                log(
                    "[FORECAST][WARN] Forecast crawl failed; "
                    f"continuing to next port. {error_message}"
                )

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if not frames:
        raise RuntimeError(
            "All bunker forecast crawling failed: "
            + " | ".join(errors)
        )

    out = pd.concat(
        frames,
        ignore_index=True,
        sort=False,
    )

    save_dataframe_to_db(
        out,
        FORECAST_TABLE,
        FORECAST_TABLE,
    )

    return out, " | ".join(errors)
'''

    patched = replace_function(
        patched,
        "collect_all_forecast_data",
        "def load_or_collect_forecast_data",
        collect_all_forecast_replacement,
    )

    # =========================================================================
    # 5. Opinet driver.get timeout 예외 처리
    # =========================================================================

    patched = replace_exact(
        patched,
        '''    driver = make_driver()

    try:
        driver.get(OIL_SOURCE_URL)

        WebDriverWait(driver, 30).until(
            lambda current_driver:
                current_driver.execute_script(
                    "return document.readyState"
                ) == "complete"
        )''',
        '''    driver = make_driver()

    try:
        try:
            driver.get(OIL_SOURCE_URL)

        except TimeoutException:
            log(
                "[OIL][WARN] Opinet page-load timeout after 45 seconds. "
                "Attempting to continue with the partially loaded page."
            )

            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        WebDriverWait(driver, 30).until(
            lambda current_driver:
                current_driver.execute_script(
                    "return document.readyState"
                ) in ["interactive", "complete"]
        )''',
        "Opinet page-load timeout 처리",
    )

    # =========================================================================
    # 6. 백업 및 저장
    # =========================================================================

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    backup_path = TARGET_PATH.with_name(
        f"{TARGET_PATH.stem}.backup_{timestamp}{TARGET_PATH.suffix}"
    )

    shutil.copy2(TARGET_PATH, backup_path)
    TARGET_PATH.write_text(patched, encoding="utf-8")

    print()
    print("=" * 70)
    print("[SUCCESS] bunker_price.py 수정 완료")
    print(f"[BACKUP]  {backup_path}")
    print(f"[TARGET]  {TARGET_PATH}")
    print("=" * 70)
    print()
    print("다음 명령으로 문법 검증을 수행하십시오:")
    print('python -m py_compile "BunkerPrice/bunker_price.py"')
    print()
    print("그 다음 로컬 테스트:")
    print('python "BunkerPrice/bunker_price.py" --build-static --refresh')

if __name__ == "__main__":
    main()
