# -*- coding: utf-8 -*-
"""
Market Intelligence Pipeline Runner

실행 대상
- Alphaliner
- Bunker Price
- Forwarder Top 25
- GDP & Inflation
- Shipping Index (SCFI & BDI)
- TAC Index (Air Freight)
- Dashboard HTML Generator

정책
- TAC Index는 외부 사이트 구조 변경·접속 오류 등으로 실패할 수 있으므로
  단일 실패 시 전체 배포를 중단하지 않고 WARNING 처리한다.
- 그 외 크롤러 또는 메인 Dashboard Generator 실패는 CRITICAL FAILURE로 처리한다.
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

# =============================================================================
# 1. 기본 설정
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent

PYTHON_EXECUTABLE = sys.executable

DEFAULT_TIMEOUT_SECONDS = 20 * 60

# TAC만 실패해도 전체 workflow를 계속 진행할지 여부
# True  : TAC 실패 → WARNING → GitHub Actions 성공 종료(exit code 0)
# False : TAC 실패 → FAILED  → GitHub Actions 실패 종료(exit code 1)
TAC_FAILURE_IS_NON_CRITICAL = True

# =============================================================================
# 2. 실행 대상 설정 (실제 저장소 폴더/파일명 기준)
# =============================================================================

CRAWLERS = [
    {
        "name": "Alphaliner",
        "path": BASE_DIR / "Alphaliner" / "alphaliner.py",
        "args": [],
        "timeout": 20 * 60,
    },
    {
        "name": "Bunker Price",
        "path": BASE_DIR / "BunkerPrice" / "Bunker Price.py",
        "args": ["--crawl-only"],  # Flask 서버 실행 방지
        "timeout": 20 * 60,
    },
    {
        "name": "Forwarder Top 25",
        "path": BASE_DIR / "Forwarder" / "Top25_Forwarder.py",
        "args": [],
        "timeout": 20 * 60,
    },
    {
        "name": "GDP & Inflation",
        "path": BASE_DIR / "GDP&Inflation" / "GDP_Inflation_GSCPI_DB.py",
        "args": [],
        "timeout": 20 * 60,
    },
    {
        "name": "Shipping Index (SCFI & BDI)",
        "path": BASE_DIR / "Shipping Index" / "ShippingIndex_SCFI&BDI.py",
        "args": [],
        "timeout": 20 * 60,
    },
    {
        "name": "TAC Index (Air Freight)",
        "path": BASE_DIR / "TAC Index" / "TAC_Index.py",
        "args": [],
        "timeout": 20 * 60,
    },
]

DASHBOARD_GENERATOR = {
    "name": "Dashboard HTML Generator",
    "path": BASE_DIR / "dashboard_generator.py",
    "args": [],
    "timeout": 10 * 60,
}

# =============================================================================
# 3. 출력 및 시간 유틸리티
# =============================================================================

def now_string() -> str:
    """현재 시각을 로그 표시용 문자열로 반환합니다."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def print_separator(char: str = "=", length: int = 72) -> None:
    print(char * length, flush=True)

def print_task_start(task_name: str, script_path: Path, timeout_seconds: int) -> None:
    print_separator()
    print(f"[{now_string()}] ▶ START: {task_name}", flush=True)
    print(f"[INFO] Script : {script_path}", flush=True)
    print(f"[INFO] Timeout: {timeout_seconds // 60} minutes", flush=True)
    print_separator()

def print_task_end(task_name: str, success: bool, message: Optional[str] = None) -> None:
    print_separator()
    if success:
        print(f"[{now_string()}] ✅ COMPLETED: {task_name}", flush=True)
    else:
        print(f"[{now_string()}] ❌ FAILED: {task_name}", flush=True)

    if message:
        print(f"[ERROR] {message}", flush=True)
    print_separator()

# =============================================================================
# 4. 개별 스크립트 실행 및 상태 처리
# =============================================================================

def run_task(task: Dict[str, object]) -> Dict[str, object]:
    """
    단일 파이썬 스크립트(크롤러 또는 생성기)를 실행하고 결과를 반환합니다.
    """
    task_name = str(task["name"])
    script_path = Path(str(task["path"]))
    extra_args = list(task.get("args", []))
    timeout_seconds = int(task.get("timeout", DEFAULT_TIMEOUT_SECONDS))

    if not script_path.exists():
        message = f"Script file not found: {script_path}"
        print_task_start(task_name, script_path, timeout_seconds)
        print_task_end(task_name, success=False, message=message)
        return {
            "name": task_name,
            "status": "SKIPPED",
            "message": message,
            "script": str(script_path),
        }

    print_task_start(task_name, script_path, timeout_seconds)

    command = [
        PYTHON_EXECUTABLE,
        "-u",
        str(script_path),
        *extra_args,
    ]

    env = os.environ.copy()

    try:
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            env=env,
            timeout=timeout_seconds,
            check=False,
        )

        if completed.returncode == 0:
            print_task_end(task_name, success=True)
            return {
                "name": task_name,
                "status": "SUCCESS",
                "message": "Completed successfully",
                "script": str(script_path),
            }

        message = f"Process exited with code {completed.returncode}"
        print_task_end(task_name, success=False, message=message)
        return {
            "name": task_name,
            "status": "FAILED",
            "message": message,
            "script": str(script_path),
        }

    except subprocess.TimeoutExpired:
        message = f"Timed out after {timeout_seconds // 60} minutes"
        print_task_end(task_name, success=False, message=message)
        return {
            "name": task_name,
            "status": "TIMEOUT",
            "message": message,
            "script": str(script_path),
        }

    except Exception as exc:
        message = f"Unexpected execution error ({type(exc).__name__}): {exc}"
        print_task_end(task_name, success=False, message=message)
        return {
            "name": task_name,
            "status": "FAILED",
            "message": message,
            "script": str(script_path),
        }

def convert_tac_failure_to_warning(result: Dict[str, object]) -> Dict[str, object]:
    """
    TAC 수집 실패를 전체 배포 중단 사유가 아닌 WARNING으로 변경합니다.
    """
    if result.get("status") == "SUCCESS":
        return result

    original_status = result.get("status", "FAILED")
    original_message = result.get("message", "")

    warning_message = (
        f"TAC source update failed ({original_status}: {original_message}). "
        "The pipeline will continue and preserve previously published TAC data, "
        "if available."
    )

    print_separator()
    print(f"[{now_string()}] ⚠️ WARNING: TAC Index (Air Freight)", flush=True)
    print(f"[WARNING] {warning_message}", flush=True)
    print_separator()

    return {
        **result,
        "status": "WARNING",
        "message": warning_message,
    }

# =============================================================================
# 5. 메인 실행부
# =============================================================================

def main() -> None:
    start_time = datetime.now()
    results: List[Dict[str, object]] = []

    print()
    print_separator()
    print(" MARKET INTELLIGENCE - PIPELINE START", flush=True)
    print(f" Started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f" Base dir  : {BASE_DIR}", flush=True)
    print_separator()
    print()

    # 1단계: 모든 카테고리 크롤러 순차 실행 (DB 수집/갱신)
    print(f"[{now_string()}] [PHASE 1] Running {len(CRAWLERS)} Crawlers...", flush=True)
    for crawler in CRAWLERS:
        result = run_task(crawler)

        # TAC Index 실패 격리 처리
        if crawler["name"] == "TAC Index (Air Freight)" and TAC_FAILURE_IS_NON_CRITICAL:
            result = convert_tac_failure_to_warning(result)

        results.append(result)

    # 2단계: 최신 DB 기반으로 HTML 대시보드 렌더링
    print()
    print(f"[{now_string()}] [PHASE 2] Generating HTML Dashboard...", flush=True)
    dashboard_result = run_task(DASHBOARD_GENERATOR)
    results.append(dashboard_result)

    end_time = datetime.now()
    elapsed = end_time - start_time

    # -----------------------------------------------------
    # 최종 결과 요약
    # -----------------------------------------------------
    print()
    print_separator()
    print(" PIPELINE EXECUTION - FINAL SUMMARY", flush=True)
    print_separator()

    success_count = 0
    warning_count = 0
    critical_fail_count = 0

    for res in results:
        status = str(res["status"])
        name = str(res["name"])
        message = str(res["message"])

        if status == "SUCCESS":
            icon = "✅"
            success_count += 1
        elif status == "WARNING":
            icon = "⚠️"
            warning_count += 1
        elif status == "SKIPPED":
            icon = "⏭️"
            critical_fail_count += 1
        elif status == "TIMEOUT":
            icon = "⏰"
            critical_fail_count += 1
        else:
            icon = "❌"
            critical_fail_count += 1

        print(f"{icon} [{status:<7}] {name} - {message}", flush=True)

    print_separator("-")
    print(f"Completed at  : {end_time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"Elapsed time  : {elapsed}", flush=True)
    print(f"Success       : {success_count}", flush=True)
    print(f"Warning (TAC) : {warning_count}", flush=True)
    print(f"Critical Fail : {critical_fail_count}", flush=True)
    print_separator()

    # 치명적 오류(TAC 외 크롤러 실패, 대시보드 생성 실패, 누락 파일 등)가 있는 경우에만 실패 종료
    if critical_fail_count > 0:
        print("[RESULT] Pipeline finished with CRITICAL FAILURE.", flush=True)
        sys.exit(1)

    if warning_count > 0:
        print("[RESULT] Pipeline finished with WARNINGs (Non-critical sources skipped, deployment continues).", flush=True)
    else:
        print("[RESULT] All tasks completed successfully.", flush=True)

    sys.exit(0)

if __name__ == "__main__":
    main()
