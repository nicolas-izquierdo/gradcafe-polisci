"""Pipeline orchestrator: load -> scrape -> merge -> clean -> CSV -> HTML."""

import argparse
import csv
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import clean as c
from scraper import (
    UNIFIED_FIELDS,
    load_deedy_baseline,
    scrape_gradcafe,
)

DATA_DIR = Path("data")
DOCS_DIR = Path("docs")
CSV_PATH = DATA_DIR / "gradcafe_polisci.csv"
LAST_UPDATED_PATH = DATA_DIR / "last_updated.txt"
HTML_PATH = DOCS_DIR / "index.html"

ALL_SEASONS = [
    "S16", "F16", "S17", "F17", "S18", "F18",
    "S19", "F19", "S20", "F20", "S21", "F21",
    "S22", "F22", "S23", "F23", "S24", "F24",
    "S25", "F25", "S26", "F26",
]


# ---------------------------------------------------------------------------
# Merge + deduplicate
# ---------------------------------------------------------------------------

def merge_and_dedup(existing: list[dict], new_rows: list[dict]) -> list[dict]:
    seen = set()
    merged = []
    for row in existing + new_rows:
        key = (
            str(row.get("school_raw", "")),
            str(row.get("season_code", "")),
            str(row.get("date_posted", "")),
            str(row.get("decision", "")),
        )
        if key not in seen:
            seen.add(key)
            merged.append(row)
    return merged


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def load_existing_csv() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Restore numeric types
            for field in ("gpa", "gre_w"):
                if row.get(field) in ("", "None", None):
                    row[field] = None
                else:
                    try:
                        row[field] = float(row[field])
                    except (ValueError, TypeError):
                        row[field] = None
            for field in ("gre_v", "gre_q", "season_year"):
                if row.get(field) in ("", "None", None):
                    row[field] = None
                else:
                    try:
                        row[field] = int(row[field])
                    except (ValueError, TypeError):
                        row[field] = None
            rows.append(row)
    return rows


def save_csv(rows: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=UNIFIED_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {len(rows)} rows to {CSV_PATH}")


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

_DECISION_COLORS = {
    "Accepted": "#2d6a2d",
    "Rejected": "#1a3a5c",
    "Waitlisted": "#7a4f1a",
    "Interview": "#4a1a5c",
    "Other": "#555555",
}


def _rows_to_json(rows: list[dict]) -> str:
    """Convert rows to a compact JSON list of lists for embedding."""
    # Columns in order for JS: rowid, school_clean, school_raw, degree,
    # season_code, season_year, decision_class, date_posted, gpa, gre_v, gre_q, gre_w, applicant_status
    out = []
    for r in rows:
        out.append([
            r.get("rowid", ""),
            r.get("school_clean") or r.get("school_raw", ""),
            r.get("school_raw", ""),
            r.get("degree", ""),
            r.get("season_code", ""),
            r.get("season_year") if r.get("season_year") else None,
            r.get("decision_class", "Other"),
            r.get("date_posted", ""),
            r.get("gpa") if r.get("gpa") is not None else None,
            r.get("gre_v") if r.get("gre_v") is not None else None,
            r.get("gre_q") if r.get("gre_q") is not None else None,
            r.get("gre_w") if r.get("gre_w") is not None else None,
            r.get("applicant_status", ""),
        ])
    return json.dumps(out, separators=(",", ":"))


def _get_unique_sorted(rows: list[dict], field: str) -> list:
    vals = sorted(set(str(r.get(field, "")) for r in rows if r.get(field)))
    return vals


def generate_html(rows: list[dict], last_updated: str) -> str:
    n = len(rows)
    data_json = _rows_to_json(rows)

    # Unique cycle years (fall only) for filter
    fall_years = sorted(set(
        r["season_year"] for r in rows
        if r.get("season_year") and str(r.get("season_code", "")).startswith("F")
    ), reverse=True)

    fall_years_json = json.dumps(fall_years)
    colors_json = json.dumps(_DECISION_COLORS)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Political Science PhD Admissions — GradCafe Data</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" crossorigin="anonymous"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    font-family: system-ui, -apple-system, sans-serif;
    background: #ffffff;
    color: #1a1a1a;
    margin: 0;
    padding: 0;
    font-size: 14px;
    line-height: 1.5;
  }}
  a {{ color: #1a1a1a; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 0 24px; }}

  /* Header */
  header {{ padding: 32px 0 16px; border-bottom: 1px solid #e0e0e0; }}
  header h1 {{
    font-size: 22px;
    font-weight: 600;
    margin: 0 0 4px;
    letter-spacing: -0.3px;
  }}
  .subtitle {{ color: #555; font-size: 13px; margin: 0 0 6px; }}
  .caveat {{ color: #999; font-size: 11px; margin: 0; font-style: italic; }}

  /* Filter bar */
  .filter-bar {{
    position: sticky;
    top: 0;
    background: #ffffff;
    border-bottom: 1px solid #e0e0e0;
    z-index: 100;
    padding: 10px 0;
  }}
  .filter-inner {{
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-items: flex-start;
  }}
  .filter-group {{ display: flex; flex-direction: column; gap: 4px; }}
  .filter-group label.group-label {{
    font-size: 10px;
    font-variant: small-caps;
    letter-spacing: 0.05em;
    color: #999;
    text-transform: uppercase;
  }}
  select, input[type="text"] {{
    border: 1px solid #e0e0e0;
    background: #fff;
    color: #1a1a1a;
    padding: 4px 8px;
    font-size: 13px;
    font-family: inherit;
    border-radius: 3px;
    outline: none;
  }}
  select:focus, input:focus {{ border-color: #aaa; }}
  .checkbox-group {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    max-width: 400px;
  }}
  .checkbox-group label {{
    display: flex;
    align-items: center;
    gap: 3px;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
  }}
  .reset-link {{
    font-size: 12px;
    color: #888;
    text-decoration: underline;
    cursor: pointer;
    align-self: flex-end;
    padding-bottom: 4px;
    border: none;
    background: none;
    font-family: inherit;
  }}
  .reset-link:hover {{ color: #1a1a1a; }}

  /* Charts */
  .chart-section {{ padding: 24px 0 0; }}
  .chart-title {{
    font-size: 12px;
    font-variant: small-caps;
    letter-spacing: 0.05em;
    color: #555;
    margin: 0 0 8px;
    text-transform: uppercase;
  }}
  #timeline-chart {{ width: 100%; height: 420px; }}
  #volume-chart {{ width: 100%; height: 200px; }}

  /* Table */
  .table-section {{ padding: 24px 0; }}
  .table-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }}
  .record-count {{ font-size: 12px; color: #888; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }}
  th {{
    font-variant: small-caps;
    font-size: 11px;
    letter-spacing: 0.05em;
    text-align: left;
    color: #888;
    padding: 6px 8px;
    border-bottom: 1px solid #e0e0e0;
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
  }}
  th:hover {{ color: #1a1a1a; }}
  th .sort-arrow {{ margin-left: 3px; opacity: 0.4; }}
  th.sorted .sort-arrow {{ opacity: 1; }}
  td {{
    padding: 5px 8px;
    border-bottom: 1px solid #f0f0f0;
    color: #1a1a1a;
  }}
  tr:hover td {{ background: #fafafa; }}
  .decision-dot {{
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    margin-right: 5px;
    vertical-align: middle;
  }}
  .pagination {{
    display: flex;
    gap: 4px;
    align-items: center;
    justify-content: flex-end;
    margin-top: 8px;
    font-size: 12px;
  }}
  .page-btn {{
    border: 1px solid #e0e0e0;
    background: #fff;
    color: #1a1a1a;
    padding: 3px 9px;
    cursor: pointer;
    font-family: inherit;
    font-size: 12px;
    border-radius: 3px;
  }}
  .page-btn:hover {{ background: #f5f5f5; }}
  .page-btn.active {{ background: #1a1a1a; color: #fff; border-color: #1a1a1a; }}
  .page-btn:disabled {{ opacity: 0.35; cursor: default; }}
  .page-info {{ color: #888; margin: 0 6px; }}

  footer {{
    border-top: 1px solid #e0e0e0;
    padding: 16px 0;
    font-size: 11px;
    color: #aaa;
  }}
</style>
</head>
<body>

<div class="container">

  <header>
    <h1>Political Science PhD Admissions</h1>
    <p class="subtitle">{n:,} records &middot; GradCafe 2006&ndash;2026 &middot; Updated {last_updated}</p>
    <p class="caveat">Data is self-reported by applicants on thegradcafe.com and may not be representative. Use for reference only.</p>
  </header>

  <div class="filter-bar">
    <div class="container">
      <div class="filter-inner">
        <div class="filter-group">
          <label class="group-label">School</label>
          <input type="text" id="school-search" placeholder="Search school…" style="width:180px">
        </div>
        <div class="filter-group">
          <label class="group-label">Degree</label>
          <select id="degree-filter">
            <option value="Both">Both</option>
            <option value="PhD">PhD</option>
            <option value="MA">MA</option>
          </select>
        </div>
        <div class="filter-group">
          <label class="group-label">Decision</label>
          <div class="checkbox-group" id="decision-checkboxes">
            <label><input type="checkbox" value="Accepted" checked> Accepted</label>
            <label><input type="checkbox" value="Rejected" checked> Rejected</label>
            <label><input type="checkbox" value="Waitlisted" checked> Waitlisted</label>
            <label><input type="checkbox" value="Interview" checked> Interview</label>
            <label><input type="checkbox" value="Other" checked> Other</label>
          </div>
        </div>
        <div class="filter-group">
          <label class="group-label">Cycle Year (Fall)</label>
          <div class="checkbox-group" id="year-checkboxes"></div>
        </div>
        <button class="reset-link" id="reset-btn">Reset filters</button>
      </div>
    </div>
  </div>

  <div class="chart-section">
    <p class="chart-title">Decision timeline — all cycles overlaid (Nov–May)</p>
    <div id="timeline-chart"></div>
  </div>

  <div class="chart-section">
    <p class="chart-title">Weekly decision volume</p>
    <div id="volume-chart"></div>
  </div>

  <div class="table-section">
    <div class="table-header">
      <p class="chart-title" style="margin:0">Records</p>
      <span class="record-count" id="record-count"></span>
    </div>
    <table id="data-table">
      <thead>
        <tr>
          <th data-col="1">School <span class="sort-arrow">↕</span></th>
          <th data-col="4">Cycle <span class="sort-arrow">↕</span></th>
          <th data-col="6">Decision <span class="sort-arrow">↕</span></th>
          <th data-col="7">Date Posted <span class="sort-arrow">↕</span></th>
          <th data-col="8">GPA <span class="sort-arrow">↕</span></th>
          <th data-col="9">GRE-V <span class="sort-arrow">↕</span></th>
          <th data-col="10">GRE-Q <span class="sort-arrow">↕</span></th>
          <th data-col="12">Status <span class="sort-arrow">↕</span></th>
        </tr>
      </thead>
      <tbody id="table-body"></tbody>
    </table>
    <div class="pagination" id="pagination"></div>
  </div>

  <footer>
    Data from <a href="https://www.thegradcafe.com">thegradcafe.com</a> (self-reported) and
    <a href="https://github.com/deedy/gradcafe_data">deedy/gradcafe_data</a> (2006&ndash;2015).
    No affiliation with GradCafe. Code:
    <a href="https://github.com/nicolas-izquierdo/gradcafe-polisci">github.com/nicolas-izquierdo/gradcafe-polisci</a>
  </footer>

</div>

<script>
// ── Data ──────────────────────────────────────────────────────────────────
// Columns: [rowid, school_clean, school_raw, degree, season_code, season_year,
//           decision_class, date_posted, gpa, gre_v, gre_q, gre_w, applicant_status]
const RAW_DATA = {data_json};
const FALL_YEARS = {fall_years_json};
const COLORS = {colors_json};

// ── State ─────────────────────────────────────────────────────────────────
let filtered = RAW_DATA.slice();
let sortCol = 7;   // date_posted
let sortDir = -1;  // -1 = desc
let currentPage = 1;
const PAGE_SIZE = 25;

// ── Build year checkboxes ─────────────────────────────────────────────────
(function buildYearCheckboxes() {{
  const container = document.getElementById('year-checkboxes');
  FALL_YEARS.forEach(yr => {{
    const lbl = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = yr;
    cb.checked = true;
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(' ' + yr));
    container.appendChild(lbl);
  }});
}})();

// ── Filter logic ──────────────────────────────────────────────────────────
function getActiveDecisions() {{
  return Array.from(document.querySelectorAll('#decision-checkboxes input:checked'))
    .map(cb => cb.value);
}}

function getActiveYears() {{
  return new Set(Array.from(document.querySelectorAll('#year-checkboxes input:checked'))
    .map(cb => parseInt(cb.value)));
}}

function applyFilters() {{
  const schoolQ = document.getElementById('school-search').value.toLowerCase().trim();
  const degree = document.getElementById('degree-filter').value;
  const decisions = new Set(getActiveDecisions());
  const years = getActiveYears();

  filtered = RAW_DATA.filter(r => {{
    // school search
    if (schoolQ && !r[1].toLowerCase().includes(schoolQ)) return false;
    // degree
    if (degree !== 'Both' && r[3] !== degree) return false;
    // decision
    if (!decisions.has(r[6])) return false;
    // year — include rows without a fall season year only if "all" are checked
    if (r[5] !== null) {{
      const seasonCode = r[4] || '';
      // For rows with a season_code starting with 'S' (spring), use that year
      if (!years.has(r[5])) return false;
    }}
    return true;
  }});

  currentPage = 1;
  renderAll();
}}

// ── Wire up filters ───────────────────────────────────────────────────────
document.getElementById('school-search').addEventListener('input', applyFilters);
document.getElementById('degree-filter').addEventListener('change', applyFilters);
document.querySelectorAll('#decision-checkboxes input').forEach(cb => cb.addEventListener('change', applyFilters));
document.getElementById('year-checkboxes').addEventListener('change', applyFilters);
document.getElementById('reset-btn').addEventListener('click', () => {{
  document.getElementById('school-search').value = '';
  document.getElementById('degree-filter').value = 'Both';
  document.querySelectorAll('#decision-checkboxes input').forEach(cb => cb.checked = true);
  document.querySelectorAll('#year-checkboxes input').forEach(cb => cb.checked = true);
  applyFilters();
}});

// ── Timeline chart ────────────────────────────────────────────────────────
function dayOfCycle(dateStr) {{
  // Returns a fractional "cycle day": Nov 1 = 0, Apr 30 ≈ 181
  if (!dateStr) return null;
  const d = new Date(dateStr);
  if (isNaN(d)) return null;
  const m = d.getMonth(); // 0-based
  const day = d.getDate();
  // Nov=10, Dec=11, Jan=0, Feb=1, Mar=2, Apr=3, May=4
  const monthOffset = {{10:0, 11:30, 0:61, 1:92, 2:120, 3:151, 4:181, 5:212}};
  if (monthOffset[m] === undefined) return null;
  return monthOffset[m] + day;
}}

function seededJitter(rowid) {{
  // Deterministic vertical jitter from rowid string
  let h = 0;
  for (let i = 0; i < rowid.length; i++) h = (Math.imul(31, h) + rowid.charCodeAt(i)) | 0;
  return ((h >>> 0) / 0xffffffff) * 2 - 1; // -1 to 1
}}

function renderTimeline() {{
  const decisionGroups = {{}};
  filtered.forEach(r => {{
    const dc = r[6] || 'Other';
    if (!decisionGroups[dc]) decisionGroups[dc] = {{ x: [], y: [], text: [], rowids: [] }};
    const cx = dayOfCycle(r[7]);
    if (cx === null) return;
    decisionGroups[dc].x.push(cx);
    decisionGroups[dc].y.push(seededJitter(r[0]));
    decisionGroups[dc].rowids.push(r[0]);
    const gpaStr = r[8] !== null ? ' · GPA ' + r[8].toFixed(2) : '';
    const greStr = (r[9] !== null && r[10] !== null) ? ' · GRE ' + r[9] + 'V/' + r[10] + 'Q' : '';
    decisionGroups[dc].text.push(
      '<b>' + r[1] + '</b><br>' +
      dc + ' · ' + (r[4]||'') + '<br>' +
      (r[7]||'') + gpaStr + greStr
    );
  }});

  const traces = Object.entries(decisionGroups).map(([dc, g]) => ({{
    type: 'scatter',
    mode: 'markers',
    name: dc,
    x: g.x,
    y: g.y,
    text: g.text,
    hovertemplate: '%{{text}}<extra></extra>',
    marker: {{
      color: COLORS[dc] || '#888',
      size: 5,
      opacity: 0.55,
    }},
  }}));

  const tickvals = [0, 30, 61, 92, 120, 151, 181];
  const ticktext = ['Nov', 'Dec', 'Jan', 'Feb', 'Mar', 'Apr', 'May'];

  const layout = {{
    paper_bgcolor: '#ffffff',
    plot_bgcolor: '#ffffff',
    margin: {{ l: 20, r: 20, t: 10, b: 40 }},
    xaxis: {{
      tickvals, ticktext,
      tickfont: {{ size: 11, color: '#aaa' }},
      showgrid: true,
      gridcolor: '#eeeeee',
      gridwidth: 1,
      zeroline: false,
      showline: false,
    }},
    yaxis: {{
      visible: false,
      range: [-1.5, 1.5],
      zeroline: false,
    }},
    legend: {{
      orientation: 'h',
      x: 0, y: -0.12,
      font: {{ size: 11 }},
      traceorder: 'normal',
    }},
    hovermode: 'closest',
  }};

  Plotly.react('timeline-chart', traces, layout, {{
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ['toImage','sendDataToCloud','select2d','lasso2d'],
  }});
}}

// ── Volume chart ──────────────────────────────────────────────────────────
function renderVolume() {{
  const weekCounts = {{}};
  filtered.forEach(r => {{
    const cx = dayOfCycle(r[7]);
    if (cx === null) return;
    const week = Math.floor(cx / 7);
    const dc = r[6] || 'Other';
    if (!weekCounts[week]) weekCounts[week] = {{}};
    weekCounts[week][dc] = (weekCounts[week][dc] || 0) + 1;
  }});

  const weeks = Object.keys(weekCounts).map(Number).sort((a,b) => a-b);
  const decisions = ['Accepted', 'Interview', 'Waitlisted', 'Rejected', 'Other'];

  const traces = decisions.map(dc => {{
    const y = weeks.map(w => (weekCounts[w] && weekCounts[w][dc]) || 0);
    return {{
      type: 'bar',
      name: dc,
      x: weeks.map(w => w * 7),
      y,
      marker: {{ color: COLORS[dc] || '#888', opacity: 0.75 }},
      showlegend: false,
    }};
  }});

  const tickvals = [0, 30, 61, 92, 120, 151, 181];
  const ticktext = ['Nov', 'Dec', 'Jan', 'Feb', 'Mar', 'Apr', 'May'];

  const layout = {{
    barmode: 'stack',
    paper_bgcolor: '#ffffff',
    plot_bgcolor: '#ffffff',
    margin: {{ l: 40, r: 20, t: 6, b: 36 }},
    xaxis: {{
      tickvals, ticktext,
      tickfont: {{ size: 11, color: '#aaa' }},
      showgrid: false,
      zeroline: false,
    }},
    yaxis: {{
      tickfont: {{ size: 10, color: '#aaa' }},
      showgrid: true,
      gridcolor: '#eeeeee',
      zeroline: false,
    }},
    hovermode: 'x unified',
  }};

  Plotly.react('volume-chart', traces, layout, {{
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ['toImage','sendDataToCloud','select2d','lasso2d'],
  }});
}}

// ── Table ─────────────────────────────────────────────────────────────────
const TH = document.querySelectorAll('#data-table th[data-col]');

TH.forEach(th => {{
  th.addEventListener('click', () => {{
    const col = parseInt(th.dataset.col);
    if (sortCol === col) sortDir *= -1;
    else {{ sortCol = col; sortDir = -1; }}
    TH.forEach(t => t.classList.remove('sorted'));
    th.classList.add('sorted');
    th.querySelector('.sort-arrow').textContent = sortDir === 1 ? '↑' : '↓';
    renderTable();
  }});
}});

function sortedData() {{
  const col = sortCol;
  return filtered.slice().sort((a, b) => {{
    let av = a[col], bv = b[col];
    if (av === null || av === undefined || av === '') av = sortDir === 1 ? '￿' : '';
    if (bv === null || bv === undefined || bv === '') bv = sortDir === 1 ? '￿' : '';
    if (av < bv) return -sortDir;
    if (av > bv) return sortDir;
    return 0;
  }});
}}

function fmt(val) {{
  if (val === null || val === undefined || val === '') return '<span style="color:#ccc">—</span>';
  return val;
}}

function renderTable() {{
  const data = sortedData();
  const total = data.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (currentPage > totalPages) currentPage = totalPages;
  const start = (currentPage - 1) * PAGE_SIZE;
  const slice = data.slice(start, start + PAGE_SIZE);

  document.getElementById('record-count').textContent =
    total.toLocaleString() + ' records';

  const tbody = document.getElementById('table-body');
  tbody.innerHTML = slice.map(r => {{
    const dot = `<span class="decision-dot" style="background:${{COLORS[r[6]]||'#888'}}"></span>`;
    return `<tr>
      <td>${{r[1]}}</td>
      <td>${{r[4]||''}}</td>
      <td>${{dot}}${{r[6]||''}}</td>
      <td>${{r[7]||''}}</td>
      <td>${{fmt(r[8] !== null ? r[8].toFixed(2) : null)}}</td>
      <td>${{fmt(r[9])}}</td>
      <td>${{fmt(r[10])}}</td>
      <td>${{fmt(r[12])}}</td>
    </tr>`;
  }}).join('');

  renderPagination(total, totalPages);
}}

function renderPagination(total, totalPages) {{
  const el = document.getElementById('pagination');
  if (totalPages <= 1) {{ el.innerHTML = ''; return; }}

  let html = `<button class="page-btn" id="prev-btn" ${{currentPage===1?'disabled':''}}>&#8592;</button>`;

  // Show page numbers with ellipsis
  const pages = [];
  if (totalPages <= 7) {{
    for (let i=1;i<=totalPages;i++) pages.push(i);
  }} else {{
    pages.push(1);
    if (currentPage > 3) pages.push('…');
    for (let i=Math.max(2,currentPage-1);i<=Math.min(totalPages-1,currentPage+1);i++) pages.push(i);
    if (currentPage < totalPages-2) pages.push('…');
    pages.push(totalPages);
  }}

  pages.forEach(p => {{
    if (p === '…') html += `<span class="page-info">…</span>`;
    else html += `<button class="page-btn${{p===currentPage?' active':''}}" data-page="${{p}}">${{p}}</button>`;
  }});

  html += `<button class="page-btn" id="next-btn" ${{currentPage===totalPages?'disabled':''}}>&#8594;</button>`;
  html += `<span class="page-info">Page ${{currentPage}} of ${{totalPages}}</span>`;
  el.innerHTML = html;

  el.querySelectorAll('.page-btn[data-page]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      currentPage = parseInt(btn.dataset.page);
      renderTable();
      el.scrollIntoView({{behavior:'smooth',block:'nearest'}});
    }});
  }});
  const prev = el.querySelector('#prev-btn');
  const next = el.querySelector('#next-btn');
  if (prev) prev.addEventListener('click', () => {{ currentPage--; renderTable(); }});
  if (next) next.addEventListener('click', () => {{ currentPage++; renderTable(); }});
}}

// ── Render all ────────────────────────────────────────────────────────────
function renderAll() {{
  renderTimeline();
  renderVolume();
  renderTable();
}}

renderAll();
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(recent_only: bool = False, test_seasons: list[str] | None = None) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    DOCS_DIR.mkdir(exist_ok=True)

    # Build school map
    c.build_school_map(str(DATA_DIR / "school_map.csv"))

    # Load deedy baseline
    deedy_rows = load_deedy_baseline()

    # Determine seasons to scrape
    if test_seasons:
        seasons = test_seasons
    elif recent_only:
        from scraper import ALL_SEASONS
        seasons = ALL_SEASONS[-2:]
    else:
        from scraper import ALL_SEASONS
        seasons = ALL_SEASONS

    # Load existing CSV if doing recent update
    existing = load_existing_csv() if recent_only else []

    # Scrape
    print(f"Scraping seasons: {seasons}")
    scraped_rows = scrape_gradcafe(seasons)

    # Merge
    if recent_only:
        # Keep deedy rows from existing CSV (source == deedy_2015)
        existing_deedy = [r for r in existing if r.get("source") == "deedy_2015"]
        existing_scraped = [r for r in existing if r.get("source") != "deedy_2015"]
        all_rows = merge_and_dedup(existing_deedy + deedy_rows + existing_scraped, scraped_rows)
    else:
        all_rows = merge_and_dedup(deedy_rows, scraped_rows)

    # Sort by date_posted desc
    all_rows.sort(key=lambda r: r.get("date_posted") or "", reverse=True)

    # Save CSV
    save_csv(all_rows)

    # Write last_updated
    today = date.today().isoformat()
    LAST_UPDATED_PATH.write_text(today)

    # Generate HTML
    print("Generating docs/index.html …")
    html = generate_html(all_rows, today)
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"HTML written ({len(html):,} bytes)")

    # Summary
    print("\n--- Summary ---")
    print(f"Total records: {len(all_rows):,}")
    deedy_count = sum(1 for r in all_rows if r.get("source") == "deedy_2015")
    scraped_count = sum(1 for r in all_rows if r.get("source") == "gradcafe_scrape")
    print(f"  Deedy baseline: {deedy_count:,}")
    print(f"  GradCafe scrape: {scraped_count:,}")
    print(f"  Seasons scraped: {seasons}")

    # First 10 rows
    print("\nFirst 10 rows:")
    cols = ["school_clean", "season_code", "degree", "decision_class", "date_posted", "gpa", "source"]
    header = "\t".join(cols)
    print(header)
    print("-" * 80)
    for row in all_rows[:10]:
        print("\t".join(str(row.get(c, "")) for c in cols))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build GradCafe PolSci dataset and site")
    parser.add_argument("--recent", action="store_true",
                        help="Only re-scrape the two most recent seasons")
    parser.add_argument("--test", nargs="+", metavar="SEASON",
                        help="Scrape only specific seasons (e.g. F24 F25)")
    args = parser.parse_args()

    run(recent_only=args.recent, test_seasons=args.test)
