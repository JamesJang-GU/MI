# -*- coding: utf-8 -*-
"""
MI Terminal Theme & Markup Generator
------------------------------------
- 모든 MI 모듈 대시보드의 표준 <head>, <header>, 공통 JS를 조립하는 엔진
- assets/mi_terminal.css와 연동되어 일관된 Bloomberg Terminal UI를 제공
"""

from typing import List, Dict, Optional

def get_html_head(page_title: str, depth: int = 1, version: str = "20261027") -> str:
    """
    대시보드 표준 <head> HTML을 생성합니다.
    :param page_title: 브라우저 탭 및 상단 타이틀
    :param depth: 폴더 깊이 (1: 하위 모듈 '../assets/', 0: 루트 './assets/')
    :param version: 브라우저 캐시 방지용 파라미터
    """
    prefix = "../" if depth == 1 else "./"

    return f"""<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_title} | MI Terminal</title>

    <!-- Google Monospace Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800;900&display=swap">

    <!-- MI Terminal Design System -->
    <link rel="stylesheet" href="{prefix}assets/mi_terminal.css?v={version}">
</head>"""

def render_terminal_header(
    module_title: str,
    status_text: str = "DATA ONLINE",
    tickers: Optional[List[Dict[str, str]]] = None,
    source_text: Optional[str] = None,
    depth: int = 1
) -> str:
    """
    대시보드 상단 표준 Bloomberg 헤더를 생성합니다.
    :param module_title: 대시보드 모듈명 (예: 'MACRO ECONOMIES & GSCPI')
    :param status_text: 시스템 상태 문구 (기본: 'DATA ONLINE')
    :param tickers: 티커 데이터 리스트 [{'label': '...', 'value': '...', 'change': '...', 'direction': 'up|down|neutral', 'id': '...'}, ...]
    :param source_text: 출처 표시 문구 (선택 사항)
    :param depth: 포털 링크 깊이
    """
    portal_link = "../index.html" if depth == 1 else "./index.html"
    tickers = tickers or []

    ticker_items = []
    for t in tickers:
        direction_cls = f"mi-{t.get('direction', 'neutral')}"
        ticker_id = t.get('id', '')
        id_val_attr = f'id="{ticker_id}-value"' if ticker_id else ''
        id_chg_attr = f'id="{ticker_id}-change"' if ticker_id else ''

        ticker_items.append(f"""
        <div class="mi-ticker">
            <span class="mi-ticker-label">{t.get('label', '--')}</span>
            <div class="mi-ticker-data">
                <span class="mi-ticker-change {direction_cls}" {id_chg_attr}>{t.get('change', '--')}</span>
                <strong class="mi-ticker-value" {id_val_attr}>{t.get('value', '--')}</strong>
            </div>
        </div>""")

    ticker_html = "".join(ticker_items)

    source_html = ""
    if source_text:
        source_html = f'<span class="mi-refresh-label">SOURCE / </span><span class="mi-refresh-value">{source_text}</span>&nbsp;|&nbsp; '

    return f"""<header class="mi-terminal-header">
    <div class="mi-ticker-bar">
        <div class="mi-module-name">
            <span class="mi-function-key">MI&lt;GO&gt;</span>
            <span>{module_title}</span>
        </div>
        {ticker_html}
        <div class="mi-header-action">
            <a href="{portal_link}">← MI PORTAL</a>
        </div>
    </div>
    <div class="mi-status-bar">
        <div class="mi-system-status">SYSTEM STATUS / <span class="mi-status-emphasis">{status_text}</span></div>
        <div>
            {source_html}<span class="mi-refresh-label">DATA SNAPSHOT / </span><span class="mi-refresh-value" id="data-updated-date">--</span>
            <span class="mi-refresh-label">&nbsp;|&nbsp; KST </span><span class="mi-refresh-value" id="terminal-clock">--:--:--</span>
        </div>
    </div>
</header>"""

def get_terminal_scripts() -> str:
    """
    상단 KST 시계 실시간 갱신 및 공통 동작을 담당하는 표준 스크립트를 반환합니다.
    """
    return """<script>
    // KST Clock Engine
    function updateTerminalClock() {
        const clockEl = document.getElementById('terminal-clock');
        if (!clockEl) return;

        const now = new Date();
        const utc = now.getTime() + (now.getTimezoneOffset() * 60000);
        const kstTime = new Date(utc + (9 * 3600000));

        const hh = String(kstTime.getHours()).padStart(2, '0');
        const mm = String(kstTime.getMinutes()).padStart(2, '0');
        const ss = String(kstTime.getSeconds()).padStart(2, '0');

        clockEl.textContent = `${hh}:${mm}:${ss}`;
    }

    setInterval(updateTerminalClock, 1000);
    updateTerminalClock();
</script>"""
