<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MI TERMINAL // 03. MACRO ECONOMIES & GSCPI</title>

    <!-- Core Dependencies -->
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700;800&family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>

    <script>
        tailwind.config = {
            theme: {
                extend: {
                    fontFamily: {
                        mono: ['"JetBrains Mono"', 'monospace'],
                        sans: ['Inter', 'sans-serif']
                    },
                    colors: {
                        terminal: {
                            bg: '#0a0e17',
                            panel: '#111827',
                            card: '#151e2e',
                            border: '#1f293d',
                            amber: '#f59e0b',
                            amberDark: '#b45309',
                            cyan: '#06b6d4',
                            green: '#10b981',
                            rose: '#f43f5e',
                            text: '#cbd5e1',
                            muted: '#64748b'
                        }
                    }
                }
            }
        }
    </script>
    <style>
        body {
            background-color: #0a0e17;
            color: #cbd5e1;
            font-family: 'Inter', sans-serif;
            background-image: radial-gradient(rgba(30, 41, 59, 0.4) 1px, transparent 1px);
            background-size: 20px 20px;
        }
        .mono { font-family: 'JetBrains Mono', monospace; }
        .terminal-panel {
            background: #111827;
            border: 1px solid #1f293d;
            box-shadow: 0 4px 20px -2px rgba(0, 0, 0, 0.6);
        }
        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-track { background: #0a0e17; }
        ::-webkit-scrollbar-thumb { background: #1f293d; border-radius: 2px; }
        ::-webkit-scrollbar-thumb:hover { background: #f59e0b; }

        .kpi-indicator {
            border-left: 3px solid;
        }
    </style>
</head>
<body class="p-3 md:p-5 min-h-screen flex flex-col justify-between">

    <div>
        <!-- ========================================== -->
        <!-- [STANDARD TOP HEADER] 모든 세부 화면 공통 규격 -->
        <!-- ========================================== -->
        <header class="terminal-panel p-3.5 mb-4 rounded border border-terminal-border flex flex-wrap justify-between items-center gap-4">
            <!-- Left: Module Identity -->
            <div class="flex items-center gap-3">
                <span class="inline-block w-2.5 h-2.5 bg-terminal-amber rounded-full animate-pulse shadow-[0_0_8px_#f59e0b]"></span>
                <div>
                    <h1 class="text-lg md:text-xl font-bold mono text-terminal-amber tracking-wider flex items-center gap-2">
                        <span>MI TERMINAL // 03. MACRO ECONOMIES & GSCPI</span>
                    </h1>
                    <p class="text-[11px] text-terminal-muted mono mt-0.5">FEED: NY FED GSCPI, OECD, GLOBAL CENTRAL BANKS & IMF CACHE</p>
                </div>
            </div>

            <!-- Right: System Telemetry & Global Controls -->
            <div class="flex items-center gap-4 md:gap-6 mono text-xs">
                <div class="text-right hidden sm:block">
                    <span class="text-terminal-muted text-[10px] block">DATA SNAPSHOT</span>
                    <span id="data-updated-date" class="text-terminal-text font-bold">2026-10-26</span>
                </div>
                <div class="text-right hidden sm:block">
                    <span class="text-terminal-muted text-[10px] block">SYSTEM STATUS</span>
                    <span class="text-terminal-green font-bold">● CONNECTED</span>
                </div>
                <div class="text-right">
                    <span class="text-terminal-muted text-[10px] block">LIVE KST</span>
                    <span id="terminal-clock" class="text-terminal-cyan font-bold tracking-widest">--:--:--</span>
                </div>
                <a href="../index.html" class="px-3 py-1.5 bg-terminal-card border border-terminal-border hover:border-terminal-amber text-terminal-amber text-xs mono rounded transition flex items-center gap-1.5 font-bold shadow-sm">
                    <span>◀ PORTAL</span>
                </a>
            </div>
        </header>

        <!-- KPI Summary Cards -->
        <section class="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
            <div class="terminal-panel p-3.5 rounded kpi-indicator border-terminal-amber">
                <div class="text-[11px] text-terminal-muted mono">GSCPI (SUPPLY CHAIN PRESSURE)</div>
                <div id="kpi-gscpi" class="text-2xl font-bold mono text-white mt-1">--</div>
                <div id="kpi-gscpi-change" class="text-xs mono mt-0.5 text-terminal-muted">MoM: --</div>
            </div>
            <div class="terminal-panel p-3.5 rounded kpi-indicator border-terminal-cyan">
                <div class="text-[11px] text-terminal-muted mono">US REAL GDP GROWTH</div>
                <div id="kpi-us-gdp" class="text-2xl font-bold mono text-terminal-cyan mt-1">--</div>
                <div id="kpi-us-gdp-sub" class="text-xs mono mt-0.5 text-terminal-muted">ANNUALIZED RATE</div>
            </div>
            <div class="terminal-panel p-3.5 rounded kpi-indicator border-terminal-green">
                <div class="text-[11px] text-terminal-muted mono">US CPI INFLATION (YoY)</div>
                <div id="kpi-us-cpi" class="text-2xl font-bold mono text-terminal-green mt-1">--</div>
                <div id="kpi-us-cpi-sub" class="text-xs mono mt-0.5 text-terminal-muted">FED TARGET: 2.0%</div>
            </div>
            <div class="terminal-panel p-3.5 rounded kpi-indicator border-purple-500">
                <div class="text-[11px] text-terminal-muted mono">KR BASE RATE / CPI</div>
                <div id="kpi-kr-rate" class="text-2xl font-bold mono text-purple-400 mt-1">3.25%</div>
                <div id="kpi-kr-sub" class="text-xs mono mt-0.5 text-terminal-muted">BOK BENCHMARK</div>
            </div>
        </section>

        <!-- Main Chart Section: GSCPI & Macro Grid -->
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-4 mb-4">
            <!-- Left Chart (8 cols) -->
            <section class="lg:col-span-8 terminal-panel p-4 rounded">
                <div class="flex justify-between items-center pb-2.5 border-b border-terminal-border mb-3">
                    <div class="flex items-center gap-2">
                        <span class="w-2 h-2 bg-terminal-amber"></span>
                        <h2 class="text-xs md:text-sm font-bold mono text-terminal-amber tracking-wider">NY FED GLOBAL SUPPLY CHAIN PRESSURE INDEX (GSCPI)</h2>
                    </div>
                    <span class="text-[11px] mono text-terminal-muted hidden sm:inline">ZERO = HISTORICAL AVERAGE</span>
                </div>
                <div id="gscpiChart" class="w-full h-80"></div>
            </section>

            <!-- Right Regional Matrix Table (4 cols) -->
            <section class="lg:col-span-4 terminal-panel p-4 rounded flex flex-col justify-between">
                <div>
                    <div class="flex justify-between items-center pb-2.5 border-b border-terminal-border mb-3">
                        <div class="flex items-center gap-2">
                            <span class="w-2 h-2 bg-terminal-cyan"></span>
                            <h2 class="text-xs md:text-sm font-bold mono text-terminal-cyan tracking-wider">MACRO MATRIX</h2>
                        </div>
                        <span class="text-[10px] mono bg-terminal-border px-1.5 py-0.5 text-terminal-muted rounded">LATEST</span>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-xs mono text-left">
                            <thead class="text-terminal-muted border-b border-terminal-border text-[11px]">
                                <tr>
                                    <th class="py-1.5 px-2">REGION</th>
                                    <th class="py-1.5 px-2 text-right">GDP</th>
                                    <th class="py-1.5 px-2 text-right">CPI(YoY)</th>
                                </tr>
                            </thead>
                            <tbody id="regional-tbody" class="divide-y divide-terminal-border/40">
                                <tr><td colspan="3" class="py-4 text-center text-terminal-muted">Fetching cache data...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
                <div class="p-2.5 bg-terminal-card border border-terminal-border rounded text-[11px] mono text-terminal-muted mt-3">
                    GSCPI &gt; 0: 공급망 압력 가중 (운임 상승 압력)<br>
                    GSCPI &lt; 0: 글로벌 공급망 완화 (물류 원활)
                </div>
            </section>
        </div>

        <!-- Comparative Charts -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
            <section class="terminal-panel p-4 rounded">
                <div class="flex justify-between items-center pb-2.5 border-b border-terminal-border mb-3">
                    <div class="flex items-center gap-2">
                        <span class="w-2 h-2 bg-terminal-green"></span>
                        <h2 class="text-xs md:text-sm font-bold mono text-white tracking-wider">GDP GROWTH TRAJECTORY (%)</h2>
                    </div>
                    <span class="text-[11px] mono text-terminal-muted">US / EU / CN / KR</span>
                </div>
                <div id="gdpChart" class="w-full h-64"></div>
            </section>

            <section class="terminal-panel p-4 rounded">
                <div class="flex justify-between items-center pb-2.5 border-b border-terminal-border mb-3">
                    <div class="flex items-center gap-2">
                        <span class="w-2 h-2 bg-terminal-rose"></span>
                        <h2 class="text-xs md:text-sm font-bold mono text-white tracking-wider">HEADLINE CPI INFLATION (YoY %)</h2>
                    </div>
                    <span class="text-[11px] mono text-terminal-muted">TARGET BENCHMARK 2.0%</span>
                </div>
                <div id="cpiChart" class="w-full h-64"></div>
            </section>
        </div>
    </div>

    <!-- Standard Terminal Footer -->
    <footer class="terminal-panel p-3 rounded border border-terminal-border text-center mono text-xs text-terminal-muted flex flex-wrap justify-between items-center mt-2">
        <span>MARKET INTELLIGENCE PLATFORM // GLOBAL MACROECONOMICS TERMINAL</span>
        <span class="text-[11px]">STANDARDIZED UNDER MECE PROTOCOL</span>
    </footer>

    <!-- Logic Engine -->
    <script>
        function updateClock() {
            const now = new Date();
            document.getElementById('terminal-clock').innerText = now.toTimeString().split(' ')[0];
        }
        setInterval(updateClock, 1000);
        updateClock();

        const terminalTheme = {
            backgroundColor: 'transparent',
            textStyle: { fontFamily: 'JetBrains Mono, monospace', color: '#94a3b8' },
            grid: { top: 25, right: 15, bottom: 25, left: 40, containLabel: true },
            tooltip: {
                trigger: 'axis',
                backgroundColor: 'rgba(17, 24, 39, 0.95)',
                borderColor: '#1f293d',
                textStyle: { color: '#f59e0b', fontFamily: 'JetBrains Mono', fontSize: 12 }
            }
        };

        async function initDashboard() {
            try {
                const [gscpiRes, macroRes, metaRes] = await Promise.all([
                    fetch('gscpi_data.json').then(r => r.json()).catch(() => null),
                    fetch('macro_data.json').then(r => r.json()).catch(() => null),
                    fetch('dashboard_meta.json').then(r => r.json()).catch(() => null)
                ]);

                if (metaRes && metaRes.updated_at) {
                    document.getElementById('data-updated-date').innerText = metaRes.updated_at.split(' ')[0];
                }

                // 1. GSCPI Setup
                if (gscpiRes && gscpiRes.dates && gscpiRes.dates.length > 0) {
                    const values = gscpiRes.values;
                    const latest = values[values.length - 1];
                    const prev = values[values.length - 2] || latest;
                    const diff = (latest - prev).toFixed(2);

                    document.getElementById('kpi-gscpi').innerText = Number(latest).toFixed(2);
                    const diffEl = document.getElementById('kpi-gscpi-change');
                    diffEl.innerText = `MoM: ${diff >= 0 ? '+' : ''}${diff} σ`;
                    diffEl.className = `text-xs mono mt-0.5 ${diff >= 0 ? 'text-terminal-rose' : 'text-terminal-cyan'}`;

                    const gscpiChart = echarts.init(document.getElementById('gscpiChart'));
                    gscpiChart.setOption({
                        ...terminalTheme,
                        xAxis: {
                            type: 'category',
                            data: gscpiRes.dates,
                            axisLine: { lineStyle: { color: '#1f293d' } },
                            axisLabel: { color: '#64748b', fontSize: 11 }
                        },
                        yAxis: {
                            type: 'value',
                            splitLine: { lineStyle: { color: '#1e293d', type: 'dashed' } },
                            axisLabel: { color: '#64748b', fontSize: 11 }
                        },
                        series: [{
                            name: 'GSCPI',
                            data: gscpiRes.values,
                            type: 'line',
                            smooth: true,
                            showSymbol: false,
                            lineStyle: { width: 2, color: '#f59e0b' },
                            areaStyle: {
                                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                                    { offset: 0, color: 'rgba(245, 158, 11, 0.25)' },
                                    { offset: 1, color: 'rgba(245, 158, 11, 0.0)' }
                                ])
                            },
                            markLine: {
                                symbol: 'none',
                                data: [{ yAxis: 0, lineStyle: { color: '#06b6d4', width: 1 } }]
                            }
                        }]
                    });
                }

                // 2. Macro Data Setup
                if (macroRes) {
                    const tbody = document.getElementById('regional-tbody');
                    tbody.innerHTML = '';

                    const regions = ['US', 'Eurozone', 'China', 'Korea', 'Japan'];
                    regions.forEach(reg => {
                        const d = macroRes[reg] || {};
                        const gdp = d.gdp_latest !== undefined ? `${d.gdp_latest}%` : 'N/A';
                        const cpi = d.cpi_latest !== undefined ? `${d.cpi_latest}%` : 'N/A';

                        const tr = document.createElement('tr');
                        tr.className = 'hover:bg-terminal-border/30 transition';
                        tr.innerHTML = `
                            <td class="py-2 px-2 text-white font-bold">${reg}</td>
                            <td class="py-2 px-2 text-right text-terminal-cyan">${gdp}</td>
                            <td class="py-2 px-2 text-right ${parseFloat(cpi) >= 3.0 ? 'text-terminal-rose' : 'text-terminal-green'}">${cpi}</td>
                        `;
                        tbody.appendChild(tr);
                    });

                    if (macroRes['US']) {
                        document.getElementById('kpi-us-gdp').innerText = `${macroRes['US'].gdp_latest ?? '--'}%`;
                        document.getElementById('kpi-us-cpi').innerText = `${macroRes['US'].cpi_latest ?? '--'}%`;
                    }
                }

            } catch (e) {
                console.error("Dashboard Init Error:", e);
            }
        }

        window.addEventListener('load', initDashboard);
        window.addEventListener('resize', () => {
            echarts.getInstanceByDom(document.getElementById('gscpiChart'))?.resize();
            echarts.getInstanceByDom(document.getElementById('gdpChart'))?.resize();
            echarts.getInstanceByDom(document.getElementById('cpiChart'))?.resize();
        });
    </script>
</body>
</html>
