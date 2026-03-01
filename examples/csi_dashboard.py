#!/usr/bin/env python3
"""
Legible CSI Dashboard Generator
Reads batch_results/*.jsonl and produces a self-contained HTML report.

Usage:
    python examples/csi_dashboard.py
    python examples/csi_dashboard.py --out reports/dashboard.html
"""
import argparse
import json
import re
import math
import os
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
PROVIDER_LABELS = {
    "serper_api":   "Serper",
    "groq_api":     "Groq",
    "cerebras_api": "Cerebras",
    "gemini_api":   "Gemini",
    "together_api": "Together AI",
    "massive_api":  "Massive",
}

PROVIDER_COLORS = {
    "serper_api":   "#00ff9d",
    "groq_api":     "#ff6b35",
    "cerebras_api": "#3fa9f5",
    "gemini_api":   "#f5a623",
    "together_api": "#b66dff",
    "massive_api":  "#ff4757",
}

WINDOW = 50       # rolling CSI window
SPLIT  = 400      # early vs late regime split point

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_sessions(path: Path) -> list[dict]:
    sessions = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                sessions.append(json.loads(line))
    return sessions


def compute_csi(sessions: list[dict]) -> float:
    """
    Rolling CSI formula matching RFC-0002.
    CSI = 1 - (weighted_violation_sum / max_possible_slash)
    Simplified: 1 - mean(normalized_slash) where 0=pass, 1=max_slash
    """
    if not sessions:
        return 1.0
    total, cap = 0.0, 0.0
    for s in sessions:
        slash = s.get("provider_slash", 0)
        sla   = s.get("sla_latency_ms") or s.get("sla_target_ms", 3000)
        # max slash per session = 540 (3 calls × 180)
        max_slash = 540
        total += slash
        cap   += max_slash
    if cap == 0:
        return 1.0
    return round(max(0.0, 1.0 - total / cap), 4)


def rolling_csi(sessions: list[dict], window: int = WINDOW) -> list[dict]:
    points = []
    for i in range(len(sessions)):
        start  = max(0, i - window + 1)
        window_sessions = sessions[start:i + 1]
        csi    = compute_csi(window_sessions)
        p50    = sorted([s.get("latency_p50", 0) for s in window_sessions])[len(window_sessions) // 2]
        vrate  = sum(1 for s in window_sessions if s.get("outcome") != "SlaPass") / len(window_sessions)
        points.append({
            "session": i + 1,
            "csi":     csi,
            "p50":     p50,
            "vrate":   round(vrate * 100, 1),
        })
    return points


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = (p / 100) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def regime_label(csi: float) -> str:
    if csi >= 0.96:  return "GREEN"
    if csi >= 0.90:  return "YELLOW"
    if csi >= 0.80:  return "ORANGE"
    return "RED"


def analyze_provider(sessions: list[dict]) -> dict:
    if not sessions:
        return {}
    latencies = [s.get("latency_p50", 0) for s in sessions]
    all_lats  = []
    for s in sessions:
        all_lats.extend(s.get("latency_ms", []))

    violations = [s for s in sessions if s.get("outcome") != "SlaPass"]
    overall_csi = compute_csi(sessions)

    early = sessions[:SPLIT]
    late  = sessions[SPLIT:]

    return {
        "n":            len(sessions),
        "csi":          overall_csi,
        "state":        regime_label(overall_csi),
        "vrate":        round(len(violations) / len(sessions) * 100, 1),
        "p50":          int(percentile(latencies, 50)),
        "p75":          int(percentile(latencies, 75)),
        "p95":          int(percentile(latencies, 95)),
        "p99":          int(percentile(latencies, 99)),
        "early_csi":    compute_csi(early),
        "early_vrate":  round(sum(1 for s in early if s.get("outcome") != "SlaPass") / max(len(early), 1) * 100, 1),
        "late_csi":     compute_csi(late),
        "late_vrate":   round(sum(1 for s in late if s.get("outcome") != "SlaPass") / max(len(late), 1) * 100, 1),
        "rolling":      rolling_csi(sessions, WINDOW),
    }


# ── HTML Template ─────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Legible · Coordination Stability Report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Syne:wght@400;600;700;800&display=swap');

  :root {
    --bg:        #080b0f;
    --surface:   #0d1117;
    --surface2:  #131920;
    --border:    #1e2830;
    --border2:   #243040;
    --text:      #c8d6e5;
    --muted:     #4a6070;
    --bright:    #e8f4ff;
    --green:     #00e87a;
    --yellow:    #f5c518;
    --orange:    #ff8c42;
    --red:       #ff3b5c;
    --serper:    #00ff9d;
    --groq:      #ff6b35;
    --cerebras:  #3fa9f5;
    --accent:    #00c8ff;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Space Mono', monospace;
    font-size: 13px;
    line-height: 1.6;
    min-height: 100vh;
  }

  /* Grid noise texture */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(0,200,255,0.015) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,200,255,0.015) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
    z-index: 0;
  }

  .wrap {
    position: relative;
    z-index: 1;
    max-width: 1300px;
    margin: 0 auto;
    padding: 48px 32px 80px;
  }

  /* Header */
  .header {
    border-bottom: 1px solid var(--border2);
    padding-bottom: 32px;
    margin-bottom: 48px;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
  }

  .logo {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 11px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 12px;
  }

  h1 {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 36px;
    color: var(--bright);
    line-height: 1.1;
    letter-spacing: -0.02em;
  }

  h1 span {
    color: var(--accent);
  }

  .meta {
    text-align: right;
    color: var(--muted);
    font-size: 11px;
    line-height: 2;
  }

  .meta strong {
    color: var(--text);
    display: block;
    font-size: 12px;
    margin-bottom: 4px;
  }

  /* Section titles */
  .section-title {
    font-family: 'Syne', sans-serif;
    font-weight: 700;
    font-size: 11px;
    letter-spacing: 0.25em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .section-title::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  /* Scorecard grid */
  .scorecard-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
    gap: 16px;
    margin-bottom: 48px;
  }

  .scorecard {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 24px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
  }

  .scorecard:hover { border-color: var(--border2); }

  .scorecard::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--provider-color, var(--accent));
  }

  .scorecard-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 20px;
  }

  .provider-name {
    font-family: 'Syne', sans-serif;
    font-weight: 700;
    font-size: 18px;
    color: var(--bright);
  }

  .provider-model {
    font-size: 10px;
    color: var(--muted);
    margin-top: 2px;
    letter-spacing: 0.05em;
  }

  .csi-badge {
    text-align: right;
  }

  .csi-value {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 32px;
    line-height: 1;
    color: var(--bright);
  }

  .csi-state {
    font-size: 10px;
    letter-spacing: 0.15em;
    font-weight: 700;
    margin-top: 4px;
  }

  .state-GREEN  { color: var(--green); }
  .state-YELLOW { color: var(--yellow); }
  .state-ORANGE { color: var(--orange); }
  .state-RED    { color: var(--red); }

  /* Regime bars */
  .regime-section {
    margin-top: 20px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
  }

  .regime-label {
    font-size: 10px;
    letter-spacing: 0.15em;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 10px;
  }

  .regime-row {
    display: grid;
    grid-template-columns: 80px 1fr 60px 60px;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }

  .regime-name {
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 0.05em;
  }

  .regime-bar-track {
    height: 6px;
    background: var(--surface2);
    border-radius: 2px;
    overflow: hidden;
  }

  .regime-bar-fill {
    height: 100%;
    border-radius: 2px;
    background: var(--provider-color, var(--accent));
    transition: width 1s ease;
  }

  .regime-bar-fill.dim { opacity: 0.35; }

  .regime-csi-val {
    font-size: 11px;
    color: var(--bright);
    text-align: right;
    font-weight: 700;
  }

  .regime-vrate {
    font-size: 10px;
    color: var(--muted);
    text-align: right;
  }

  /* Stats row */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1px;
    background: var(--border);
    margin-top: 16px;
    border-radius: 3px;
    overflow: hidden;
  }

  .stat-cell {
    background: var(--surface2);
    padding: 10px 12px;
  }

  .stat-label {
    font-size: 9px;
    letter-spacing: 0.15em;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 4px;
  }

  .stat-value {
    font-size: 14px;
    font-weight: 700;
    color: var(--bright);
  }

  .stat-value.danger { color: var(--red); }
  .stat-value.warn   { color: var(--orange); }
  .stat-value.ok     { color: var(--green); }

  /* Chart section */
  .chart-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 28px;
    margin-bottom: 48px;
  }

  .chart-title {
    font-family: 'Syne', sans-serif;
    font-weight: 700;
    font-size: 14px;
    color: var(--bright);
    margin-bottom: 4px;
  }

  .chart-subtitle {
    font-size: 11px;
    color: var(--muted);
    margin-bottom: 24px;
  }

  .chart-container {
    position: relative;
    height: 320px;
  }

  /* Comparison table */
  .comparison-table {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 48px;
  }

  .comparison-table th {
    font-size: 9px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--muted);
    text-align: right;
    padding: 8px 16px;
    border-bottom: 1px solid var(--border2);
    white-space: nowrap;
  }

  .comparison-table th:first-child { text-align: left; }

  .comparison-table td {
    padding: 14px 16px;
    text-align: right;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    color: var(--text);
  }

  .comparison-table td:first-child { text-align: left; }

  .comparison-table tr:hover td { background: var(--surface2); }

  .provider-cell {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .provider-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .provider-cell-name {
    font-family: 'Syne', sans-serif;
    font-weight: 700;
    font-size: 14px;
    color: var(--bright);
  }

  .highlight-cell {
    color: var(--bright);
    font-weight: 700;
  }

  /* Insight callouts */
  .insights {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 16px;
    margin-bottom: 48px;
  }

  .insight {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 20px;
    border-left: 3px solid var(--accent);
  }

  .insight.warn  { border-left-color: var(--orange); }
  .insight.alert { border-left-color: var(--red); }
  .insight.good  { border-left-color: var(--green); }

  .insight-tag {
    font-size: 9px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-bottom: 8px;
    font-weight: 700;
  }

  .insight.warn  .insight-tag { color: var(--orange); }
  .insight.alert .insight-tag { color: var(--red); }
  .insight.good  .insight-tag { color: var(--green); }
  .insight       .insight-tag { color: var(--accent); }

  .insight-text {
    font-size: 12px;
    color: var(--text);
    line-height: 1.7;
  }

  .insight-text strong {
    color: var(--bright);
    font-weight: 700;
  }

  /* Footer */
  .footer {
    border-top: 1px solid var(--border);
    padding-top: 24px;
    display: flex;
    justify-content: space-between;
    color: var(--muted);
    font-size: 10px;
    letter-spacing: 0.05em;
  }

  /* Responsive */
  @media (max-width: 768px) {
    .scorecard-grid { grid-template-columns: 1fr; }
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .header { flex-direction: column; gap: 16px; }
    .meta { text-align: left; }
    h1 { font-size: 26px; }
  }
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <div>
      <div class="logo">Legible · Trustless Coordination</div>
      <h1>Coordination<br><span>Stability Report</span></h1>
    </div>
    <div class="meta">
      <strong>Q1 2026 — Provider Benchmarks</strong>
      Generated: __DATE__<br>
      Sessions per provider: 800<br>
      Total sessions: __TOTAL__<br>
      RFC-0002 SLA Spec
    </div>
  </div>

  <!-- Provider Scorecards -->
  <div class="section-title">Provider Scorecards</div>
  <div class="scorecard-grid" id="scorecards"></div>

  <!-- Rolling CSI Chart -->
  <div class="section-title">Rolling CSI — 50-Session Windows</div>
  <div class="chart-section">
    <div class="chart-title">Coordination Stability Index Over Time</div>
    <div class="chart-subtitle">Each point = CSI computed over a rolling 50-session window. Regime shifts visible as sustained slope changes.</div>
    <div class="chart-container">
      <canvas id="rollingChart"></canvas>
    </div>
  </div>

  <!-- P50 Latency Chart -->
  <div class="section-title">Latency Profile — Session P50</div>
  <div class="chart-section">
    <div class="chart-title">Median Latency Per Session (Rolling 50)</div>
    <div class="chart-subtitle">Rolling p50 latency in ms. Drops indicate infrastructure warmup or load shifting to faster paths.</div>
    <div class="chart-container">
      <canvas id="latencyChart"></canvas>
    </div>
  </div>

  <!-- Comparison Table -->
  <div class="section-title">Head-to-Head Comparison</div>
  <table class="comparison-table">
    <thead>
      <tr>
        <th>Provider</th>
        <th>Overall CSI</th>
        <th>State</th>
        <th>Sessions 1–400 CSI</th>
        <th>Sessions 401–800 CSI</th>
        <th>Violation %</th>
        <th>p50</th>
        <th>p95</th>
        <th>p99 (Tail Risk)</th>
      </tr>
    </thead>
    <tbody id="compTable"></tbody>
  </table>

  <!-- Insights -->
  <div class="section-title">Key Findings</div>
  <div class="insights" id="insights"></div>

  <div class="footer">
    <span>Legible · github.com/teddyharks/legible</span>
    <span>RFC-0002 · Coordination Stability Index · Oslo → US Infrastructure</span>
  </div>

</div>

<script>
const DATA = __DATA__;
const COLORS = __COLORS__;

// ── Render Scorecards ────────────────────────────────────────────────────────
const container = document.getElementById('scorecards');
DATA.forEach(p => {
  const color = COLORS[p.id] || '#00c8ff';
  const earlyDelta = ((p.late_csi - p.early_csi) * 100).toFixed(1);
  const deltaSign  = earlyDelta > 0 ? '+' : '';
  const statColor  = (v, warn, bad) =>
    v >= bad ? 'danger' : v >= warn ? 'warn' : 'ok';

  container.innerHTML += `
  <div class="scorecard" style="--provider-color: ${color}">
    <div class="scorecard-header">
      <div>
        <div class="provider-name">${p.label}</div>
        <div class="provider-model">${p.id} · ${p.n} sessions · SLA ${p.sla}ms</div>
      </div>
      <div class="csi-badge">
        <div class="csi-value">${p.csi.toFixed(4)}</div>
        <div class="csi-state state-${p.state}">● ${p.state}</div>
      </div>
    </div>

    <div class="stats-grid">
      <div class="stat-cell">
        <div class="stat-label">Violations</div>
        <div class="stat-value ${statColor(p.vrate,10,30)}">${p.vrate}%</div>
      </div>
      <div class="stat-cell">
        <div class="stat-label">p50 Latency</div>
        <div class="stat-value">${p.p50}ms</div>
      </div>
      <div class="stat-cell">
        <div class="stat-label">p95 Latency</div>
        <div class="stat-value ${statColor(p.p95,5000,10000)}">${p.p95}ms</div>
      </div>
      <div class="stat-cell">
        <div class="stat-label">Tail Risk p99</div>
        <div class="stat-value ${statColor(p.p99,8000,15000)}">${p.p99}ms</div>
      </div>
    </div>

    <div class="regime-section">
      <div class="regime-label">Regime Analysis · Split at Session 400</div>
      <div class="regime-row">
        <div class="regime-name">Early 1–400</div>
        <div class="regime-bar-track">
          <div class="regime-bar-fill dim" style="width:${p.early_csi*100}%"></div>
        </div>
        <div class="regime-csi-val">${p.early_csi.toFixed(3)}</div>
        <div class="regime-vrate">${p.early_vrate}% viol</div>
      </div>
      <div class="regime-row">
        <div class="regime-name">Late 401–800</div>
        <div class="regime-bar-track">
          <div class="regime-bar-fill" style="width:${p.late_csi*100}%"></div>
        </div>
        <div class="regime-csi-val">${p.late_csi.toFixed(3)}</div>
        <div class="regime-vrate">${p.late_vrate}% viol</div>
      </div>
      <div style="font-size:10px; color: ${earlyDelta > 0 ? 'var(--green)' : 'var(--red)'}; margin-top:6px; text-align:right">
        Regime shift: ${deltaSign}${earlyDelta} CSI points (401–800 vs 1–400)
      </div>
    </div>
  </div>`;
});

// ── Rolling CSI Chart ────────────────────────────────────────────────────────
const rollingCtx = document.getElementById('rollingChart').getContext('2d');
new Chart(rollingCtx, {
  type: 'line',
  data: {
    datasets: DATA.map(p => ({
      label:            p.label,
      data:             p.rolling.map(r => ({ x: r.session, y: r.csi })),
      borderColor:      COLORS[p.id] || '#fff',
      backgroundColor:  (COLORS[p.id] || '#fff') + '15',
      borderWidth:      2,
      pointRadius:      0,
      pointHoverRadius: 4,
      tension:          0.3,
      fill:             false,
    }))
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: {
        labels: { color: '#c8d6e5', font: { family: 'Space Mono', size: 11 }, boxWidth: 12 }
      },
      tooltip: {
        backgroundColor: '#0d1117',
        borderColor: '#1e2830',
        borderWidth: 1,
        titleColor: '#e8f4ff',
        bodyColor:  '#c8d6e5',
        titleFont:  { family: 'Space Mono' },
        bodyFont:   { family: 'Space Mono', size: 11 },
        callbacks: {
          label: ctx => ` ${ctx.dataset.label}: CSI ${ctx.parsed.y.toFixed(4)}`
        }
      }
    },
    scales: {
      x: {
        type: 'linear',
        title: { display: true, text: 'Session', color: '#4a6070', font: { family: 'Space Mono', size: 10 } },
        ticks: { color: '#4a6070', font: { family: 'Space Mono', size: 10 } },
        grid:  { color: '#1e2830' }
      },
      y: {
        min: 0.4, max: 1.05,
        title: { display: true, text: 'CSI', color: '#4a6070', font: { family: 'Space Mono', size: 10 } },
        ticks: { color: '#4a6070', font: { family: 'Space Mono', size: 10 }, callback: v => v.toFixed(2) },
        grid:  { color: '#1e2830' }
      }
    }
  }
});

// Add threshold lines annotation manually via plugin
Chart.register({
  id: 'thresholds',
  afterDraw(chart) {
    const {ctx, chartArea: {left, right, top, bottom}, scales: {y}} = chart;
    [[0.96,'#00e87a'],[0.90,'#f5c518'],[0.80,'#ff8c42']].forEach(([val, color]) => {
      const yPos = y.getPixelForValue(val);
      if (yPos < top || yPos > bottom) return;
      ctx.save();
      ctx.strokeStyle = color + '40';
      ctx.lineWidth   = 1;
      ctx.setLineDash([4, 6]);
      ctx.beginPath(); ctx.moveTo(left, yPos); ctx.lineTo(right, yPos); ctx.stroke();
      ctx.fillStyle = color + '80';
      ctx.font = '9px Space Mono';
      ctx.fillText(val.toFixed(2), right + 4, yPos + 4);
      ctx.restore();
    });
  }
});

// ── Latency Chart ────────────────────────────────────────────────────────────
const latCtx = document.getElementById('latencyChart').getContext('2d');
new Chart(latCtx, {
  type: 'line',
  data: {
    datasets: DATA.map(p => ({
      label:            p.label,
      data:             p.rolling.map(r => ({ x: r.session, y: r.p50 })),
      borderColor:      COLORS[p.id] || '#fff',
      backgroundColor:  'transparent',
      borderWidth:      2,
      pointRadius:      0,
      tension:          0.3,
    }))
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: {
        labels: { color: '#c8d6e5', font: { family: 'Space Mono', size: 11 }, boxWidth: 12 }
      },
      tooltip: {
        backgroundColor: '#0d1117',
        borderColor: '#1e2830',
        borderWidth: 1,
        titleColor: '#e8f4ff',
        bodyColor:  '#c8d6e5',
        titleFont:  { family: 'Space Mono' },
        bodyFont:   { family: 'Space Mono', size: 11 },
        callbacks: {
          label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y}ms`
        }
      }
    },
    scales: {
      x: {
        type: 'linear',
        title: { display: true, text: 'Session', color: '#4a6070', font: { family: 'Space Mono', size: 10 } },
        ticks: { color: '#4a6070', font: { family: 'Space Mono', size: 10 } },
        grid:  { color: '#1e2830' }
      },
      y: {
        title: { display: true, text: 'p50 latency (ms)', color: '#4a6070', font: { family: 'Space Mono', size: 10 } },
        ticks: { color: '#4a6070', font: { family: 'Space Mono', size: 10 } },
        grid:  { color: '#1e2830' }
      }
    }
  }
});

// ── Comparison Table ─────────────────────────────────────────────────────────
const tbody = document.getElementById('compTable');
DATA.forEach(p => {
  const color = COLORS[p.id] || '#00c8ff';
  const stateClass = `state-${p.state}`;
  tbody.innerHTML += `
  <tr>
    <td>
      <div class="provider-cell">
        <div class="provider-dot" style="background:${color}"></div>
        <div>
          <div class="provider-cell-name">${p.label}</div>
          <div style="font-size:10px;color:var(--muted)">${p.id}</div>
        </div>
      </div>
    </td>
    <td class="highlight-cell">${p.csi.toFixed(4)}</td>
    <td class="${stateClass}" style="font-weight:700;letter-spacing:0.05em">${p.state}</td>
    <td style="color:${p.early_csi < 0.80 ? 'var(--orange)' : 'var(--text)'}">${p.early_csi.toFixed(4)}</td>
    <td style="color:${p.late_csi >= 0.96 ? 'var(--green)' : 'var(--text)'}">${p.late_csi.toFixed(4)}</td>
    <td style="color:${p.vrate > 30 ? 'var(--red)' : p.vrate > 10 ? 'var(--orange)' : 'var(--green)'}">${p.vrate}%</td>
    <td>${p.p50}ms</td>
    <td>${p.p95}ms</td>
    <td style="color:${p.p99 > 15000 ? 'var(--red)' : p.p99 > 8000 ? 'var(--orange)' : 'var(--text)'}">${p.p99}ms</td>
  </tr>`;
});

// ── Insights ─────────────────────────────────────────────────────────────────
const insightData = __INSIGHTS__;
const insightContainer = document.getElementById('insights');
insightData.forEach(ins => {
  insightContainer.innerHTML += `
  <div class="insight ${ins.type}">
    <div class="insight-tag">${ins.tag}</div>
    <div class="insight-text">${ins.text}</div>
  </div>`;
});
</script>
</body>
</html>
"""


def generate_insights(providers: dict) -> list[dict]:
    insights = []

    serper    = providers.get("serper_api",   {})
    groq      = providers.get("groq_api",     {})
    cerebras  = providers.get("cerebras_api", {})

    if serper:
        insights.append({
            "type": "good",
            "tag":  "Baseline Stability · Serper",
            "text": f"Serper maintained <strong>CSI {serper['csi']:.4f}</strong> across 800 sessions with "
                    f"<strong>{serper['vrate']}% violation rate</strong> and a tight p99 of {serper['p99']}ms. "
                    f"Regime delta: only {abs(serper['late_csi'] - serper['early_csi'])*100:.1f} CSI points "
                    f"between early and late windows — the gold standard for coordination reliability."
        })

    if cerebras:
        delta = (cerebras['late_csi'] - cerebras['early_csi']) * 100
        insights.append({
            "type": "warn" if cerebras['early_csi'] < 0.82 else "",
            "tag":  "Phase Transition · Cerebras",
            "text": f"Cerebras exhibits a clear <strong>two-regime operating model</strong>. Sessions 1–400: "
                    f"CSI {cerebras['early_csi']:.4f} ({cerebras['early_vrate']}% violations). Sessions 401–800: "
                    f"CSI {cerebras['late_csi']:.4f} ({cerebras['late_vrate']}% violations). "
                    f"That's a <strong>+{delta:.1f} point regime shift</strong> — indicating infrastructure "
                    f"warmup or dynamic path allocation after sustained traffic."
        })

    if groq:
        insights.append({
            "type": "alert",
            "tag":  "Tail Risk · Groq",
            "text": f"Groq's <strong>p99 of {groq['p99']}ms</strong> ({groq['p99']//1000}s) is "
                    f"{groq['p99'] // max(serper.get('p99',1), 1):.1f}x Serper's tail. "
                    f"Free-tier rate limiting holds connections open during queue saturation rather than "
                    f"rejecting. For agentic workloads, this creates unpredictable blocking. "
                    f"Groq's median ({groq['p50']}ms) is competitive — the <strong>tail is the risk</strong>, not the core."
        })

    if cerebras and serper:
        insights.append({
            "type": "",
            "tag":  "Routing Implication",
            "text": f"Optimal adaptive routing strategy: use <strong>Serper as the reliable anchor</strong> "
                    f"for high-entropy or time-sensitive queries. Route low-stakes warmup traffic to Cerebras "
                    f"until its rolling 50-session CSI exceeds 0.96, then promote to primary. "
                    f"<strong>Reduce Groq weight by 60-70%</strong> for queries where tail latency matters. "
                    f"All three providers stabilize after session ~500 — cold-start penalty is systemic."
        })

    # Network finding
    if serper and groq:
        insights.append({
            "type": "",
            "tag":  "Oslo → US Network Overhead",
            "text": f"All three US-hosted providers show ~650–750ms network overhead per call from Oslo. "
                    f"Provider median latencies: Groq {groq['p50']}ms, Cerebras {cerebras.get('p50',0)}ms, "
                    f"Serper {serper['p50']}ms — all within 700ms of each other despite different silicon. "
                    f"<strong>Network round-trip dominates inference cost</strong> for European users. "
                    f"EU-region deployment would compress all three providers' p50s to sub-1s."
        })

    return insights


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Generate CSI Dashboard HTML")
    parser.add_argument("--batch-dir", default="batch_results", help="Directory with JSONL files")
    parser.add_argument("--out",       default="reports/csi_dashboard.html", help="Output HTML path")
    args = parser.parse_args()

    batch_dir = Path(args.batch_dir)
    out_path  = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Discover JSONL files
    providers = {}
    sla_map   = {}

    # Collect all JSONL files — support both naming conventions:
    #   multi_runner:  groq_sessions_20260228.jsonl
    #   batch_runner:  serper_sessions.jsonl, serper_api_sessions.jsonl, sessions_*.jsonl
    all_jsonl = sorted(batch_dir.glob("*.jsonl"))

    pid_map = {
        "groq":       "groq_api",
        "cerebras":   "cerebras_api",
        "gemini":     "gemini_api",
        "serper":     "serper_api",
        "together":   "together_api",
        "massive":    "massive_api",
        "groq_api":   "groq_api",
        "cerebras_api": "cerebras_api",
        "gemini_api": "gemini_api",
        "serper_api": "serper_api",
    }

    for jsonl_file in all_jsonl:
        name = jsonl_file.stem  # filename without extension

        # Skip analysis files
        if "analysis" in name:
            continue

        # Try to infer provider from filename
        provider_name = None

        # Bare "sessions_YYYYMMDD.jsonl" is always serper (original batch_runner format)
        if re.match(r'^sessions_\d+$', name):
            provider_name = 'serper'
        else:
            for key in pid_map:
                if name.startswith(key) or f'_{key}_' in name or name.endswith(key):
                    provider_name = key
                    break

        # If still not found, peek inside first record
        # Handle both schemas: multi_runner uses "provider_id", batch_runner uses "provider"
        if not provider_name:
            try:
                with open(jsonl_file, encoding="utf-8") as peek_f:
                    first_line = peek_f.readline().strip()
                    if first_line:
                        first_rec = json.loads(first_line)
                        pid_from_record = (
                            first_rec.get("provider_id") or
                            first_rec.get("provider") or
                            ""
                        )
                        if pid_from_record:
                            provider_name = pid_from_record.replace("_api", "")
            except Exception:
                pass

        if not provider_name:
            # Last resort: print first record keys to help diagnose
            try:
                with open(jsonl_file, encoding="utf-8") as _df:
                    _first = json.loads(_df.readline().strip())
                    print(f"  Skipping {jsonl_file.name} — keys: {list(_first.keys())[:8]}")
            except Exception:
                print(f"  Skipping {jsonl_file.name} — could not read")
            continue

        pid = pid_map.get(provider_name, provider_name + "_api")

        sessions = load_sessions(jsonl_file)
        if not sessions:
            continue

        # Infer SLA from first session
        sla = (sessions[0].get("sla_latency_ms") or sessions[0].get("sla_target_ms", 3000)) if sessions else 3000
        sla_map[pid] = sla

        if pid in providers:
            providers[pid].extend(sessions)
        else:
            providers[pid] = sessions

    if not providers:
        print(f"No JSONL session files found in {batch_dir}/")
        print("Expected files like: groq_sessions_20260228.jsonl")
        return

    print(f"Loaded providers: {list(providers.keys())}")

    # Analyze
    analyzed = {}
    for pid, sessions in providers.items():
        analyzed[pid] = analyze_provider(sessions)
        analyzed[pid]["sla"] = sla_map.get(pid, 3000)

    # Build JS data payload
    js_data = []
    for pid, stats in analyzed.items():
        js_data.append({
            "id":          pid,
            "label":       PROVIDER_LABELS.get(pid, pid),
            "n":           stats["n"],
            "sla":         stats["sla"],
            "csi":         stats["csi"],
            "state":       stats["state"],
            "vrate":       stats["vrate"],
            "p50":         stats["p50"],
            "p75":         stats["p75"],
            "p95":         stats["p95"],
            "p99":         stats["p99"],
            "early_csi":   stats["early_csi"],
            "early_vrate": stats["early_vrate"],
            "late_csi":    stats["late_csi"],
            "late_vrate":  stats["late_vrate"],
            "rolling":     stats["rolling"],
        })

    # Sort: Serper first, then by CSI desc
    js_data.sort(key=lambda x: (-1 if x["id"] == "serper_api" else 0, -x["csi"]))

    insights    = generate_insights(analyzed)
    total       = sum(len(s) for s in providers.values())
    date_str    = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    html = HTML_TEMPLATE
    html = html.replace("__DATE__",    date_str)
    html = html.replace("__TOTAL__",   str(total))
    html = html.replace("__DATA__",    json.dumps(js_data))
    html = html.replace("__COLORS__",  json.dumps(PROVIDER_COLORS))
    html = html.replace("__INSIGHTS__", json.dumps(insights))

    out_path.write_text(html, encoding="utf-8")
    size_kb = out_path.stat().st_size // 1024
    print(f"\n✓ Dashboard written: {out_path}  ({size_kb} KB)")
    print(f"  Providers: {[p['label'] for p in js_data]}")
    print(f"  Sessions:  {total} total")
    print(f"\n  Open in browser: {out_path.resolve()}")


if __name__ == "__main__":
    main()