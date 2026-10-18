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

CRAWLER_TIMEOUT_MINUTES = 20
DASHBOARD_TIMEOUT_MINUTES = 10

# TAC만 실패해도 전체 workflow를 계속 진행할지 여부
# True  : TAC 실패 → WARNING → GitHub Actions 성공 종료
# False : TAC 실패 → FAILED  → GitHub Actions 실패 종료
TAC_FAILURE_IS_NON_CRITICAL = True

# =============================================================================
# 2. 출력 및 시간 유틸리티
# =============================================================================

def now_string() -> str:
    """현재 시각을 로그 표시용 문자열로 반환합니다."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def print_separator(char: str = "=", length: int = 72) -> None:
    print(char * length, flush=True)

def print_task_start(task_name: str, script_path: Path, timeout_minutes: int) -> None:
    print_separator()
    print(f"[{now_string()}] ▶ START: {task_name}", flush=True)
    print(f"[INFO] Script : {script_path}", flush=True)
    print(f"[INFO] Timeout: {timeout_minutes} minutes", flush=True)
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
# 3. 개별 스크립트 실행
# =============================================================================

def run_python_script(
    task_name: str,
    relative_script_path: str,
    timeout_minutes: int,
    extra_args: Optional[List[str]] = None,
) -> Dict[str, object]:
    """
    Python 스크립트를 실행하고 결과를 반환합니다.

    반환 예시:
    {
        "name": "TAC Index (Air Freight)",
        "status": "SUCCESS" | "FAILED" | "TIMEOUT" | "SKIPPED",
        "return_code": 0,
        "message": "Completed successfully",
        "script": "..."
    }
    """
    script_path = BASE_DIR / relative_script_path
    extra_args = extra_args or []

    if not script_path.exists():
        message = f"Script file not found: {script_path}"
        print_task_start(task_name, script_path, timeout_minutes)
        print_task_end(task_name, success=False, message=message)

        return {
            "name": task_name,
            "status": "FAILED",
            "return_code": None,
            "message": message,
            "script": str(script_path),
        }

    print_task_start(task_name, script_path, timeout_minutes)

    command = [
        PYTHON_EXECUTABLE,
        str(script_path),
        *extra_args,
    ]

    # GitHub Actions 환경변수 및 현재 환경변수를 자식 프로세스에 전달합니다.
    env = os.environ.copy()

    try:
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            env=env,
            timeout=timeout_minutes * 60,
            check=False,
        )

        if completed.returncode == 0:
            print_task_end(task_name, success=True)

            return {
                "name": task_name,
                "status": "SUCCESS",
                "return_code": 0,
                "message": "Completed successfully",
                "script": str(script_path),
            }

        message = f"Process exited with code {completed.returncode}"
        print_task_end(task_name, success=False, message=message)

        return {
            "name": task_name,
            "status": "FAILED",
            "return_code": completed.returncode,
            "message": message,
            "script": str(script_path),
        }

    except subprocess.TimeoutExpired:
        message = f"Timed out after {timeout_minutes} minutes"
        print_task_end(task_name, success=False, message=message)

        return {
            "name": task_name,
            "status": "TIMEOUT",
            "return_code": None,
            "message": message,
            "script": str(script_path),
        }

    except Exception as exc:
        message = f"Unexpected execution error: {exc}"
        print_task_end(task_name, success=False, message=message)

        return {
            "name": task_name,
            "status": "FAILED",
            "return_code": None,
            "message": message,
            "script": str(script_path),
        }

# =============================================================================
# 4. 결과 상태 처리
# =============================================================================

def convert_tac_failure_to_warning(result: Dict[str, object]) -> Dict[str, object]:
    """
    TAC 수집 실패를 전체 배포 중단 사유가 아닌 WARNING으로 변경합니다.

    중요:
    - TAC 코드가 기존 SQLite Snapshot과 JSON/HTML을 유지하도록 작성되어 있어야 합니다.
    - 데이터가 전혀 없는 최초 실행에서 TAC가 실패하는 경우에도,
      메인 포털 및 다른 카테고리 배포는 계속됩니다.
    """
    if result.get("status") == "SUCCESS":
        return result

    original_status = result.get("status", "FAILED")
    original_message = result.get("message", "")

    warning_message = (
        f"TAC source update failed ({original_status}: {original_message}). "
        "The
