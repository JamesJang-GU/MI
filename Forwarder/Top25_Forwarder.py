import argparse
import datetime as dt
import html
import importlib.util
import os
import sqlite3
import subprocess
import sys
from io import StringIO
from pathlib import Path

# [IDENTIFIER] A&A Top 25 Dashboard - Required for GitHub Actions search

# =====================================================
# 1. Environment Setup
# =====================================================
def setup_environment():
    required = {
        "requests": "requests",
        "pandas": "pandas",
        "bs4": "beautifulsoup4",
        "lxml": "lxml",
    }

    for import_name, pip_name in required.items():
        if importlib.util.find_spec(import_name) is not None:
            continue

        print(
            f"[SETUP] '{pip_name}' 패키지가 없어 설치를 시도합니다...",
            flush=True,
        )

        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pip_name]
            )
        except Exception:
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    pip_name,
                    "--break-system-packages",
                ]
            )

setup_environment()

import pandas as pd
import requests
from bs4 import BeautifulSoup

# =====================================================
# 2. Configuration
# =====================================================
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_URL = (
    "https://www.3plogistics.com/3pl-market-info-resources/"
    "3pl-market-information/aas-top-25-global-freight-forwarders-list/"
)

DB_PATH = SCRIPT_DIR / "freight_forwarders_data.db"
STATIC_HTML_PATH = SCRIPT_DIR / "index.html"
STATIC_CSV_PATH = SCRIPT_DIR / "freight_forwarders_history.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

EXPECTED_COLUMNS = [
    "rank",
    "provider",
    "hq",
    "revenue",
    "teu",
    "air_tons",
]

# =====================================================
# 3. Utilities
# =====================================================
def log(message):
    print(message, flush=True)

def clean_number(value):
    """문자열 숫자를 float로 표준화합니다."""
    text = str(value).strip()

    if not text or text.lower() in {"nan", "none", "-", "n/a", "na"}:
        return 0.0

    is_negative = (
        text.startswith("-")
        or (text.startswith("(") and text.endswith(")"))
    )

    text = text.replace(",", "").replace("(", "").replace(")", "")
    text = "".join(char for char in text if char.isdigit() or char == ".")

    if not text:
        return 0.0

    try:
        number = float(text)
        return -number if is_negative else number
    except ValueError:
        return 0.0

def normalize_provider_name(value):
    """공백을 정리한 Forwarder 명칭을 반환합니다."""
    return " ".join(str(value).strip().split())

def format_compact_number(value, suffix=""):
    """대시보드 표시용 숫자 포맷입니다."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"

    if number == 0:
        return "--"

    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:,.2f}B{suffix}"

    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:,.2f}M{suffix}"

    if abs(number) >= 1_000:
        return f"{number / 1_000:,.1f}K{suffix}"

    return f"{number:,.0f}{suffix}"

def format_revenue(value):
    """
    원본 A&A 테이블의 Revenue 단위는 웹 원본 표기 기준으로 관리됩니다.
    여기서는 DB 숫자를 변형하지 않고 읽기 쉬운 형태로만 표시합니다.
    """
    return format_compact_number(value)

def calculate_percentage_change(current_value, previous_value):
    """이전 스냅샷 대비 증감률을 계산합니다."""
    try:
        current_value = float(current_value)
        previous_value = float(previous_value)
    except (TypeError, ValueError):
        return None

    if previous_value == 0:
        return None

    return ((current_value - previous_value) / abs(previous_value)) * 100

def format_delta(delta_value):
    """증감률을 화면 표시 문자열로 변환합니다."""
    if delta_value is None:
        return "N/A"

    if delta_value > 0:
        return f"+{delta_value:.1f}%"

    return f"{delta_value:.1f}%"

# =====================================================
# 4. Web Crawling
# =====================================================
def select_best_table(html_text):
    """페이지 내 후보 테이블 중 Top 25 데이터로 보이는 테이블을 선택합니다."""
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")

    if not tables:
        raise RuntimeError("웹페이지에서 table 태그를 찾지 못했습니다.")

    candidates = []

    for index, table in enumerate(tables):
        try:
            dataframe = pd.read_html(StringIO(str(table)))[0]
        except Exception:
            continue

        if dataframe is None or dataframe.empty:
            continue

        content = (
            " ".join(map(str, dataframe.columns))
            + " "
            + " ".join(map(str, dataframe.head(5).values.ravel()))
        ).lower()

        score = 0

        for keyword in [
            "rank",
            "provider",
            "headquarters",
            "hq",
            "revenue",
            "teu",
            "air",
        ]:
            if keyword in content:
                score += 5

        if dataframe.shape[1] >= 6:
            score += 3

        if dataframe.shape[0] >= 20:
            score += 5

        candidates.append((score, index, dataframe))

    if not candidates:
        raise RuntimeError("적절한 Top 25 테이블을 찾지 못했습니다.")

