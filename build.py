"""Pipeline orchestrator: load -> scrape -> merge -> clean -> CSV -> HTML."""

import argparse
import csv
import json
import re
import statistics
from datetime import date, datetime
from pathlib import Path

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

_DECISION_COLORS = {
    "Accepted":   "#2d6a2d",
    "Rejected":   "#1a3a5c",
    "Waitlisted": "#7a4f1a",
    "Interview":  "#4a1a5c",
    "Other":      "#555555",
}

_MONTH_OFFSETS = {11: 0, 12: 30, 1: 61, 2: 92, 3: 120, 4: 151, 5: 181}
_DEC_IDX = {"Accepted": 0, "Rejected": 1, "Waitlisted": 2, "Interview": 3, "Other": 4}
_DEC_NAMES = ["Accepted", "Rejected", "Waitlisted", "Interview", "Other"]
_STATUS_IDX = {"American": 0, "International": 1}


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
            for field in ("gpa", "gre_w"):
                v = row.get(field)
                row[field] = None if v in ("", "None", None) else _safe_float(v)
            for field in ("gre_v", "gre_q", "season_year"):
                v = row.get(field)
                row[field] = None if v in ("", "None", None) else _safe_int(v)
            rows.append(row)
    return rows


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def save_csv(rows: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=UNIFIED_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {len(rows)} rows -> {CSV_PATH}")


# ---------------------------------------------------------------------------
# Data compression for JS embedding
# ---------------------------------------------------------------------------

def _cycle_day(date_str: str) -> int | None:
    """Return cycle day (0=Nov 1 ... ~211=May 31) or None."""
    if not date_str or len(date_str) < 10:
        return None
    try:
        mo = int(date_str[5:7])
        dy = int(date_str[8:10])
        if mo in _MONTH_OFFSETS:
            return _MONTH_OFFSETS[mo] + dy - 1
        return None
    except (ValueError, IndexError):
        return None


def _median(lst: list) -> float | None:
    if not lst:
        return None
    return round(statistics.median(lst), 2)


def _compress_data(rows: list[dict]) -> tuple[str, str, str, str]:
    """
    Returns (data_json, schools_json, school_stats_json, year_stats_json).

    DATA format per record:
    [school_idx, season_year, is_fall(0/1), dec_idx(0-4),
     cycle_day|null, gpa|null, gre_v|null, gre_q|null, status_idx(0-2), date_str]
    """
    # Build school index
    schools = sorted(set(
        (r.get("school_clean") or r.get("school_raw", "")).strip()
        for r in rows
    ))
    school_to_idx = {s: i for i, s in enumerate(schools)}

    # Accumulators for school stats
    # school -> {total, by_dec[5], gpas_by_dec{0:[], ...}, grevs_by_dec{0:[], ...}, by_year{yr: [5]}}
    ss: dict[str, dict] = {}
    for s in schools:
        ss[s] = {
            "total": 0,
            "by_dec": [0, 0, 0, 0, 0],
            "gpas_by_dec": {i: [] for i in range(5)},
            "grevs_by_dec": {i: [] for i in range(5)},
            "by_year": {},
        }

    # Accumulator for year stats: year_str -> [dec0..4, total]
    ys: dict[str, list] = {}

    data = []
    for r in rows:
        school = (r.get("school_clean") or r.get("school_raw", "")).strip()
        sidx = school_to_idx.get(school, 0)
        year = r.get("season_year") or 0
        is_fall = 1 if str(r.get("season_code", "")).startswith("F") else 0
        didx = _DEC_IDX.get(r.get("decision_class", "Other"), 4)
        cd = _cycle_day(r.get("date_posted") or "")
        gpa = r.get("gpa")
        gre_v = r.get("gre_v")
        gre_q = r.get("gre_q")
        status = _STATUS_IDX.get((r.get("applicant_status") or "").strip(), 2)
        date_str = r.get("date_posted") or ""

        # Round floats
        if gpa is not None:
            try:
                gpa = round(float(gpa), 2)
            except (TypeError, ValueError):
                gpa = None
        if gre_v is not None:
            try:
                gre_v = int(gre_v)
            except (TypeError, ValueError):
                gre_v = None
        if gre_q is not None:
            try:
                gre_q = int(gre_q)
            except (TypeError, ValueError):
                gre_q = None

        data.append([sidx, year, is_fall, didx, cd, gpa, gre_v, gre_q, status, date_str])

        # Accumulate school stats
        if school in ss:
            entry = ss[school]
            entry["total"] += 1
            entry["by_dec"][didx] += 1
            if gpa is not None:
                entry["gpas_by_dec"][didx].append(gpa)
            if gre_v is not None:
                entry["grevs_by_dec"][didx].append(gre_v)
            yk = str(year)
            if yk not in entry["by_year"]:
                entry["by_year"][yk] = [0, 0, 0, 0, 0]
            entry["by_year"][yk][didx] += 1

        # Accumulate year stats
        yk = str(year)
        if yk not in ys:
            ys[yk] = [0, 0, 0, 0, 0, 0]
        ys[yk][didx] += 1
        ys[yk][5] += 1

    # Compact school stats (drop raw arrays, keep medians and summary data)
    school_stats_out = {}
    for s, entry in ss.items():
        if entry["total"] < 3:
            continue
        school_stats_out[s] = {
            "total": entry["total"],
            "by_dec": entry["by_dec"],
            "med_gpa_acc": _median(entry["gpas_by_dec"][0]),
            "med_grev_acc": _median(entry["grevs_by_dec"][0]),
            "by_year": entry["by_year"],
        }

    return (
        json.dumps(data, separators=(",", ":")),
        json.dumps(schools, separators=(",", ":")),
        json.dumps(school_stats_out, separators=(",", ":")),
        json.dumps(ys, separators=(",", ":")),
    )


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(rows: list[dict], last_updated: str) -> str:
    n = len(rows)
    data_json, schools_json, school_stats_json, year_stats_json = _compress_data(rows)

    # Fall cycle years for filter checkboxes
    fall_years = sorted(set(
        r["season_year"] for r in rows
        if r.get("season_year") and str(r.get("season_code", "")).startswith("F")
    ), reverse=True)
    fall_years_json = json.dumps(fall_years)
    colors_json = json.dumps(_DECISION_COLORS)

    # Overall acceptance rate for display
    accepted = sum(1 for r in rows if r.get("decision_class") == "Accepted")
    pct_acc = f"{100*accepted/n:.1f}" if n else "0"
    unique_schools = len(set(
        (r.get("school_clean") or r.get("school_raw", "")).strip()
        for r in rows if r.get("school_clean") or r.get("school_raw")
    ))
    year_range = "2006–2026"

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
  font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  background: #ffffff;
  color: #1a1a1a;
  margin: 0; padding: 0;
  font-size: 14px;
  line-height: 1.5;
}}
a {{ color: #1a1a1a; }}
.container {{ max-width: 1280px; margin: 0 auto; padding: 0 28px; }}

/* ── Header ── */
header {{ padding: 28px 0 14px; border-bottom: 1px solid #e0e0e0; }}
header h1 {{ font-size: 21px; font-weight: 600; margin: 0 0 3px; letter-spacing: -.3px; }}
.subtitle {{ color: #555; font-size: 13px; margin: 0 0 5px; }}
.caveat {{ color: #aaa; font-size: 11px; margin: 0; font-style: italic; }}

/* ── Stats strip ── */
.stats-strip {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0;
  border-bottom: 1px solid #e0e0e0;
  padding: 10px 0;
}}
.stat-card {{ padding: 6px 0; }}
.stat-num {{ font-size: 20px; font-weight: 600; letter-spacing: -.5px; }}
.stat-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: #999; margin-top: 1px; }}

/* ── Filter bar ── */
.filter-bar {{
  position: sticky; top: 0;
  background: #ffffff;
  border-bottom: 1px solid #e0e0e0;
  z-index: 100; padding: 8px 0;
}}
.filter-inner {{
  display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start;
}}
.filter-group {{ display: flex; flex-direction: column; gap: 3px; }}
.filter-group .glabel {{
  font-size: 9px; text-transform: uppercase; letter-spacing: .07em; color: #bbb;
}}
select, input[type="text"] {{
  border: 1px solid #e0e0e0; background: #fff; color: #1a1a1a;
  padding: 4px 8px; font-size: 13px; font-family: inherit;
  border-radius: 3px; outline: none;
}}
select:focus, input:focus {{ border-color: #aaa; }}
.cb-group {{ display: flex; flex-wrap: wrap; gap: 5px; max-width: 480px; }}
.cb-group label {{
  display: flex; align-items: center; gap: 3px;
  font-size: 12px; cursor: pointer; white-space: nowrap;
}}
.reset-btn {{
  font-size: 11px; color: #bbb; text-decoration: underline;
  cursor: pointer; align-self: flex-end; padding-bottom: 4px;
  border: none; background: none; font-family: inherit;
}}
.reset-btn:hover {{ color: #1a1a1a; }}

/* ── School spotlight ── */
#spotlight {{
  display: none;
  background: #fafafa;
  border-bottom: 1px solid #e0e0e0;
  padding: 12px 0;
}}
.spotlight-inner {{ display: flex; gap: 32px; align-items: flex-start; flex-wrap: wrap; }}
.spotlight-name {{ font-size: 16px; font-weight: 600; margin: 0 0 4px; }}
.spotlight-meta {{ font-size: 12px; color: #555; }}
.spotlight-stats {{ display: flex; gap: 20px; margin-top: 8px; flex-wrap: wrap; }}
.sstat {{ text-align: left; }}
.sstat-val {{ font-size: 15px; font-weight: 600; }}
.sstat-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: #999; }}
#spotlight-mini {{ flex: 1; min-width: 260px; height: 120px; }}

/* ── Sections ── */
.section {{ padding: 22px 0 0; }}
.section:last-of-type {{ padding-bottom: 24px; }}
.section-title {{
  font-size: 11px; font-variant: small-caps; letter-spacing: .06em;
  color: #888; margin: 0 0 8px; text-transform: uppercase;
}}
.chart-pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
@media (max-width: 700px) {{ .chart-pair {{ grid-template-columns: 1fr; }} }}
.chart-note {{ font-size: 10px; color: #bbb; margin: 4px 0 0; font-style: italic; }}

#timeline-chart {{ width: 100%; height: 400px; }}
#volume-chart {{ width: 100%; height: 180px; }}
#gpa-chart {{ width: 100%; height: 300px; }}
#grev-chart {{ width: 100%; height: 300px; }}
#school-chart {{ width: 100%; height: 560px; }}
#year-chart {{ width: 100%; height: 240px; }}

/* ── Table ── */
.table-header {{
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 6px;
}}
.record-count {{ font-size: 11px; color: #bbb; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{
  font-variant: small-caps; font-size: 10px; letter-spacing: .05em;
  text-align: left; color: #999; padding: 5px 7px;
  border-bottom: 1px solid #e0e0e0;
  cursor: pointer; user-select: none; white-space: nowrap;
}}
th:hover {{ color: #1a1a1a; }}
th .arr {{ margin-left: 2px; opacity: 0.35; }}
th.sorted .arr {{ opacity: 1; }}
td {{ padding: 4px 7px; border-bottom: 1px solid #f0f0f0; }}
tr:hover td {{ background: #fafafa; }}
.dot {{
  display: inline-block; width: 7px; height: 7px;
  border-radius: 50%; margin-right: 4px; vertical-align: middle;
}}
.pagination {{
  display: flex; gap: 3px; align-items: center;
  justify-content: flex-end; margin-top: 8px; font-size: 12px;
}}
.pbtn {{
  border: 1px solid #e0e0e0; background: #fff; color: #1a1a1a;
  padding: 2px 8px; cursor: pointer; font-size: 12px;
  border-radius: 3px; font-family: inherit;
}}
.pbtn:hover {{ background: #f5f5f5; }}
.pbtn.active {{ background: #1a1a1a; color: #fff; border-color: #1a1a1a; }}
.pbtn:disabled {{ opacity: 0.35; cursor: default; }}
.pinfo {{ color: #bbb; margin: 0 5px; font-size: 11px; }}

/* ── Footer ── */
footer {{
  border-top: 1px solid #e0e0e0;
  padding: 16px 0; font-size: 11px; color: #bbb;
}}
</style>
</head>
<body>
<div class="container">

<header>
  <h1>Political Science PhD Admissions</h1>
  <p class="subtitle" id="hdr-subtitle">{n:,} records &middot; GradCafe {year_range} &middot; Updated {last_updated}</p>
  <p class="caveat">Self-reported by applicants on thegradcafe.com. Not representative of actual admission rates. Use for reference only.</p>
</header>

<div class="filter-bar">
  <div class="container">
    <div class="filter-inner">
      <div class="filter-group">
        <span class="glabel">School</span>
        <input type="text" id="school-search" placeholder="Search school&hellip;" style="width:190px" autocomplete="off">
      </div>
      <div class="filter-group">
        <span class="glabel">Decision</span>
        <div class="cb-group" id="dec-checkboxes">
          <label><input type="checkbox" value="0" checked> Accepted</label>
          <label><input type="checkbox" value="1" checked> Rejected</label>
          <label><input type="checkbox" value="2" checked> Waitlisted</label>
          <label><input type="checkbox" value="3" checked> Interview</label>
          <label><input type="checkbox" value="4" checked> Other</label>
        </div>
      </div>
      <div class="filter-group">
        <span class="glabel">Cycle Year (Fall)</span>
        <div class="cb-group" id="year-checkboxes"></div>
      </div>
      <button class="reset-btn" id="reset-btn">Reset</button>
    </div>
  </div>
</div>

<div class="stats-strip" id="stats-strip">
  <div class="stat-card">
    <div class="stat-num" id="stat-total">{n:,}</div>
    <div class="stat-label">Records</div>
  </div>
  <div class="stat-card">
    <div class="stat-num" id="stat-pct">{pct_acc}%</div>
    <div class="stat-label">Reported accepted</div>
  </div>
  <div class="stat-card">
    <div class="stat-num" id="stat-programs">{unique_schools}</div>
    <div class="stat-label">Programs</div>
  </div>
  <div class="stat-card">
    <div class="stat-num">{year_range}</div>
    <div class="stat-label">Years covered</div>
  </div>
</div>

<div id="spotlight">
  <div class="container">
    <div class="spotlight-inner">
      <div>
        <div class="spotlight-name" id="sp-name"></div>
        <div class="spotlight-meta" id="sp-meta"></div>
        <div class="spotlight-stats" id="sp-stats"></div>
      </div>
      <div id="spotlight-mini"></div>
    </div>
  </div>
</div>

<div class="section">
  <p class="section-title">Decision Timeline &mdash; all cycles overlaid (Nov&ndash;May)</p>
  <div id="timeline-chart"></div>
</div>

<div class="section">
  <p class="section-title">Weekly Decision Volume</p>
  <div id="volume-chart"></div>
</div>

<div class="section">
  <p class="section-title">GPA &amp; GRE-V Distributions by Outcome</p>
  <div class="chart-pair">
    <div>
      <div id="gpa-chart"></div>
      <p class="chart-note" id="gpa-note"></p>
    </div>
    <div>
      <div id="grev-chart"></div>
      <p class="chart-note" id="grev-note"></p>
    </div>
  </div>
</div>

<div class="section">
  <p class="section-title">Top Programs by Reports &mdash; sorted by acceptance rate</p>
  <div id="school-chart"></div>
  <p class="chart-note">Only programs with &ge;10 reports shown. Acceptance rate reflects GradCafe reports, not true admission rate.</p>
</div>

<div class="section">
  <p class="section-title">Reports per Cycle Year</p>
  <div id="year-chart"></div>
</div>

<div class="section">
  <div class="table-header">
    <p class="section-title" style="margin:0">Data Records</p>
    <span class="record-count" id="rec-count"></span>
  </div>
  <table id="data-table">
    <thead><tr>
      <th data-ci="0">School <span class="arr">&#x21D5;</span></th>
      <th data-ci="1">Cycle <span class="arr">&#x21D5;</span></th>
      <th data-ci="3">Decision <span class="arr">&#x21D5;</span></th>
      <th data-ci="9">Date <span class="arr">&#x21D5;</span></th>
      <th data-ci="5">GPA <span class="arr">&#x21D5;</span></th>
      <th data-ci="6">GRE-V <span class="arr">&#x21D5;</span></th>
      <th data-ci="7">GRE-Q <span class="arr">&#x21D5;</span></th>
      <th data-ci="8">Status <span class="arr">&#x21D5;</span></th>
    </tr></thead>
    <tbody id="tbl-body"></tbody>
  </table>
  <div class="pagination" id="pagination"></div>
</div>

<footer>
  Data from <a href="https://www.thegradcafe.com">thegradcafe.com</a> (self-reported) and
  <a href="https://github.com/deedy/gradcafe_data">deedy/gradcafe_data</a> (2006&ndash;2015).
  No affiliation with GradCafe. Code:
  <a href="https://github.com/nicolas-izquierdo/gradcafe-polisci">github.com/nicolas-izquierdo/gradcafe-polisci</a>
</footer>

</div><!-- /container -->

<script>
// ── Embedded data ────────────────────────────────────────────────────────
// DATA: [school_idx, year, is_fall, dec_idx, cycle_day|null,
//        gpa|null, gre_v|null, gre_q|null, status_idx, date_str]
const DATA = {data_json};
const SCHOOLS = {schools_json};
const SCHOOL_STATS = {school_stats_json};
const YEAR_STATS = {year_stats_json};
const FALL_YEARS = {fall_years_json};
const COLORS = {colors_json};

const DEC_NAMES = ["Accepted","Rejected","Waitlisted","Interview","Other"];
const STATUS_NAMES = ["American","International","Unknown"];
const DEC_COLORS = [COLORS.Accepted, COLORS.Rejected, COLORS.Waitlisted, COLORS.Interview, COLORS.Other];

// ── State ────────────────────────────────────────────────────────────────
let F = DATA.slice();
let schoolFilter = "";
let exactSchool = null;  // matched school name from SCHOOLS
let activeYears = new Set(FALL_YEARS);
let activeDecs = new Set([0,1,2,3,4]);
let sortCI = 9;  // date
let sortDir = -1;
let page = 1;
const PG = 25;
const CFG = {{responsive:true,displaylogo:false,modeBarButtonsToRemove:['toImage','select2d','lasso2d','sendDataToCloud']}};

// ── Build year checkboxes ────────────────────────────────────────────────
(function() {{
  const el = document.getElementById('year-checkboxes');
  FALL_YEARS.forEach(y => {{
    const lbl = document.createElement('label');
    const cb = document.createElement('input');
    cb.type='checkbox'; cb.value=y; cb.checked=true;
    lbl.append(cb, ' '+y);
    el.append(lbl);
  }});
}})();

// ── Utility ──────────────────────────────────────────────────────────────
function seededJitter(seed) {{
  let h = seed | 0;
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b);
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b);
  return ((h >>> 0) / 0x100000000) * 2 - 1;
}}

function fmt(v) {{
  return (v===null||v===undefined||v==='') ? '<span style="color:#ddd">—</span>' : v;
}}

function pctStr(num, total) {{
  if (!total) return '—';
  return (100*num/total).toFixed(1)+'%';
}}

function seasonLabel(r) {{
  return (r[2] ? 'F' : 'S') + String(r[1]).slice(2);
}}

function tooltip(r) {{
  const school = SCHOOLS[r[0]] || '';
  const dec = DEC_NAMES[r[3]];
  const cy = seasonLabel(r);
  let t = '<b>'+school+'</b><br>'+dec+' · '+cy;
  if (r[9]) t += '<br>'+r[9];
  if (r[5]!==null) t += '<br>GPA '+r[5].toFixed(2);
  if (r[6]!==null) t += ' · GRE-V '+r[6];
  if (r[7]!==null) t += ' · GRE-Q '+r[7];
  if (r[8]<2) t += '<br>'+STATUS_NAMES[r[8]];
  return t;
}}

// ── Filtering ────────────────────────────────────────────────────────────
function applyFilters() {{
  const sq = schoolFilter.toLowerCase().trim();
  exactSchool = null;
  if (sq) {{
    // Find best school match
    const matches = SCHOOLS.filter(s => s.toLowerCase().includes(sq));
    exactSchool = matches.length === 1 ? matches[0] : null;
  }}
  F = DATA.filter(r => {{
    if (sq && !SCHOOLS[r[0]].toLowerCase().includes(sq)) return false;
    if (!activeYears.has(r[1])) return false;
    if (!activeDecs.has(r[3])) return false;
    return true;
  }});
  page = 1;
  renderAll();
}}

// ── Wire filters ─────────────────────────────────────────────────────────
let debounceTimer;
document.getElementById('school-search').addEventListener('input', e => {{
  schoolFilter = e.target.value;
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(applyFilters, 280);
}});
document.getElementById('dec-checkboxes').addEventListener('change', e => {{
  activeDecs.clear();
  document.querySelectorAll('#dec-checkboxes input:checked').forEach(cb => activeDecs.add(+cb.value));
  applyFilters();
}});
document.getElementById('year-checkboxes').addEventListener('change', () => {{
  activeYears.clear();
  document.querySelectorAll('#year-checkboxes input:checked').forEach(cb => activeYears.add(+cb.value));
  applyFilters();
}});
document.getElementById('reset-btn').addEventListener('click', () => {{
  document.getElementById('school-search').value = '';
  schoolFilter = '';
  document.querySelectorAll('#dec-checkboxes input').forEach(cb => cb.checked=true);
  document.querySelectorAll('#year-checkboxes input').forEach(cb => cb.checked=true);
  activeDecs = new Set([0,1,2,3,4]);
  activeYears = new Set(FALL_YEARS);
  applyFilters();
}});

// ── Stats strip ───────────────────────────────────────────────────────────
function updateStats() {{
  const tot = F.length;
  const acc = F.filter(r=>r[3]===0).length;
  const progs = new Set(F.map(r=>r[0])).size;
  document.getElementById('stat-total').textContent = tot.toLocaleString();
  document.getElementById('stat-pct').textContent = tot ? (100*acc/tot).toFixed(1)+'%' : '—';
  document.getElementById('stat-programs').textContent = progs;
}}

// ── School spotlight ──────────────────────────────────────────────────────
function renderSpotlight() {{
  const el = document.getElementById('spotlight');
  if (!schoolFilter.trim()) {{ el.style.display='none'; return; }}

  el.style.display='block';
  const sq = schoolFilter.toLowerCase().trim();

  // Find matching schools
  const matchIdxs = [];
  SCHOOLS.forEach((s,i) => {{ if (s.toLowerCase().includes(sq)) matchIdxs.push(i); }});
  if (!matchIdxs.length) {{ el.style.display='none'; return; }}

  const sidx = matchIdxs[0];
  const sname = SCHOOLS[sidx];
  const ss = SCHOOL_STATS[sname];

  document.getElementById('sp-name').textContent = sname;

  if (ss) {{
    const total = ss.total;
    const acc = ss.by_dec[0], rej = ss.by_dec[1], wl = ss.by_dec[2];
    const accRate = total ? (100*acc/total).toFixed(1) : '—';
    document.getElementById('sp-meta').textContent =
      total+' total reports · '+accRate+'% reported accepted';

    let statsHtml = '';
    if (ss.med_gpa_acc !== null)
      statsHtml += '<div class="sstat"><div class="sstat-val">'+ss.med_gpa_acc.toFixed(2)+'</div><div class="sstat-label">Med. GPA (accepted)</div></div>';
    if (ss.med_grev_acc !== null)
      statsHtml += '<div class="sstat"><div class="sstat-val">'+ss.med_grev_acc+'</div><div class="sstat-label">Med. GRE-V (accepted)</div></div>';
    statsHtml += '<div class="sstat"><div class="sstat-val">'+acc+'</div><div class="sstat-label">Accepted</div></div>';
    statsHtml += '<div class="sstat"><div class="sstat-val">'+wl+'</div><div class="sstat-label">Waitlisted</div></div>';
    statsHtml += '<div class="sstat"><div class="sstat-val">'+rej+'</div><div class="sstat-label">Rejected</div></div>';
    document.getElementById('sp-stats').innerHTML = statsHtml;

    // Mini year trend for this school
    const byYear = ss.by_year || {{}};
    const years = Object.keys(byYear).map(Number).sort((a,b)=>a-b);
    const miniTraces = [0,1,2].map(d => ({{
      type:'bar', name:DEC_NAMES[d],
      x: years,
      y: years.map(y => (byYear[y]||[0,0,0,0,0])[d]),
      marker:{{color:DEC_COLORS[d], opacity:0.8}},
      showlegend: false,
    }}));
    Plotly.react('spotlight-mini', miniTraces, {{
      barmode:'stack',
      paper_bgcolor:'#fafafa', plot_bgcolor:'#fafafa',
      margin:{{l:28,r:8,t:8,b:28}},
      xaxis:{{tickfont:{{size:10,color:'#aaa'}},tickformat:'d',gridcolor:'#eee'}},
      yaxis:{{tickfont:{{size:10,color:'#aaa'}},gridcolor:'#eee',zeroline:false}},
      showlegend:false,
    }}, {{...CFG, staticPlot:false}});
  }} else {{
    document.getElementById('sp-meta').textContent = 'School not found in stats';
    document.getElementById('sp-stats').innerHTML = '';
  }}
}}

// ── Timeline ──────────────────────────────────────────────────────────────
function renderTimeline() {{
  const traces = [0,1,2,3,4].map(d => {{
    const x=[], y=[], text=[];
    F.forEach(r => {{
      if (r[3]!==d || r[4]===null) return;
      x.push(r[4]);
      y.push(seededJitter(r[0]*7+d));
      text.push(tooltip(r));
    }});
    return {{
      type:'scattergl', mode:'markers', name:DEC_NAMES[d],
      x, y, text, hovertemplate:'%{{text}}<extra></extra>',
      marker:{{color:DEC_COLORS[d], size:5, opacity:0.5}},
      visible: x.length>0 ? true : 'legendonly',
    }};
  }});
  const tickvals=[0,30,61,92,120,151,181];
  const ticktext=['Nov','Dec','Jan','Feb','Mar','Apr','May'];
  Plotly.react('timeline-chart', traces, {{
    paper_bgcolor:'#fff', plot_bgcolor:'#fff',
    margin:{{l:16,r:16,t:8,b:40}},
    xaxis:{{tickvals,ticktext,tickfont:{{size:11,color:'#bbb'}},
            gridcolor:'#eee',gridwidth:1,zeroline:false,showline:false}},
    yaxis:{{visible:false,range:[-1.6,1.6],zeroline:false}},
    legend:{{orientation:'h',x:0,y:-0.14,font:{{size:11}}}},
    hovermode:'closest',
  }}, CFG);
}}

// ── Volume chart ──────────────────────────────────────────────────────────
function renderVolume() {{
  const wc = {{}};
  F.forEach(r => {{
    if (r[4]===null) return;
    const w = Math.floor(r[4]/7);
    if (!wc[w]) wc[w]={{}};
    wc[w][r[3]] = (wc[w][r[3]]||0)+1;
  }});
  const weeks = Object.keys(wc).map(Number).sort((a,b)=>a-b);
  const tickvals=[0,30,61,92,120,151,181];
  const ticktext=['Nov','Dec','Jan','Feb','Mar','Apr','May'];
  const traces = [0,1,2,3,4].map(d => ({{
    type:'bar', name:DEC_NAMES[d],
    x: weeks.map(w=>w*7),
    y: weeks.map(w=>(wc[w][d]||0)),
    marker:{{color:DEC_COLORS[d],opacity:0.75}},
    showlegend:false,
  }}));
  Plotly.react('volume-chart', traces, {{
    barmode:'stack',
    paper_bgcolor:'#fff', plot_bgcolor:'#fff',
    margin:{{l:38,r:16,t:6,b:36}},
    xaxis:{{tickvals,ticktext,tickfont:{{size:11,color:'#bbb'}},showgrid:false,zeroline:false}},
    yaxis:{{tickfont:{{size:10,color:'#bbb'}},gridcolor:'#eee',zeroline:false}},
    hovermode:'x unified',
  }}, CFG);
}}

// ── Score distributions ───────────────────────────────────────────────────
function renderScores() {{
  const gpas  = [[], [], [], [], []];
  const grevs = [[], [], [], [], []];
  F.forEach(r => {{
    if (r[5]!==null && r[5]>0 && r[5]<=4.5) gpas[r[3]].push(r[5]);
    if (r[6]!==null) {{
      // Accept both old (200-800) and new (130-170) GRE scales
      if (r[6]>=130 && r[6]<=170) grevs[r[3]].push(r[6]);
      // Old scale: skip (would require rescaling, confusing to mix)
    }}
  }});

  const showDecs = [0,1,2]; // Accepted, Rejected, Waitlisted
  const gpaTraces = showDecs.filter(d=>gpas[d].length>=3).map(d => ({{
    type:'box', name:DEC_NAMES[d], y:gpas[d],
    boxpoints:'outliers', jitter:0.35, pointpos:0,
    marker:{{color:DEC_COLORS[d], size:3, opacity:0.4}},
    line:{{color:DEC_COLORS[d], width:1.5}},
    fillcolor:DEC_COLORS[d]+'22',
  }}));
  const grevTraces = showDecs.filter(d=>grevs[d].length>=3).map(d => ({{
    type:'box', name:DEC_NAMES[d], y:grevs[d],
    boxpoints:'outliers', jitter:0.35, pointpos:0,
    marker:{{color:DEC_COLORS[d], size:3, opacity:0.4}},
    line:{{color:DEC_COLORS[d], width:1.5}},
    fillcolor:DEC_COLORS[d]+'22',
  }}));

  const baseLayout = {{
    paper_bgcolor:'#fff', plot_bgcolor:'#fff',
    margin:{{l:44,r:16,t:24,b:40}},
    xaxis:{{tickfont:{{size:11,color:'#bbb'}},gridcolor:'#eee',zeroline:false}},
    yaxis:{{tickfont:{{size:10,color:'#bbb'}},gridcolor:'#eee',zeroline:false}},
    showlegend:false,
  }};

  const gpaCount = gpas.flat().length;
  const grevCount = grevs.flat().length;
  document.getElementById('gpa-note').textContent = gpaCount+' records with GPA data';
  document.getElementById('grev-note').textContent = grevCount+' records with GRE-V data (new scale 130–170 only)';

  Plotly.react('gpa-chart', gpaTraces.length ? gpaTraces : [{{type:'box',y:[],name:''}}],
    {{...baseLayout, title:{{text:'GPA',font:{{size:12,color:'#999'}},x:0.04,xanchor:'left'}},
      yaxis:{{...baseLayout.yaxis, range:[2.5,4.2]}}}}, CFG);
  Plotly.react('grev-chart', grevTraces.length ? grevTraces : [{{type:'box',y:[],name:''}}],
    {{...baseLayout, title:{{text:'GRE Verbal',font:{{size:12,color:'#999'}},x:0.04,xanchor:'left'}},
      yaxis:{{...baseLayout.yaxis, range:[140,172]}}}}, CFG);
}}

// ── School breakdown chart ────────────────────────────────────────────────
function renderSchoolChart() {{
  // Aggregate from SCHOOL_STATS filtered to activeYears and activeDecs
  // For simplicity: use SCHOOL_STATS overall (unaffected by year/dec filters)
  // but respect the school name filter for highlighting
  const entries = Object.entries(SCHOOL_STATS)
    .filter(([_,ss]) => ss.total >= 10)
    .sort((a,b) => {{
      const ra = a[1].by_dec[0]/Math.max(a[1].total,1);
      const rb = b[1].by_dec[0]/Math.max(b[1].total,1);
      return ra - rb; // ascending: most selective at bottom
    }})
    .slice(-35); // top 35 by acceptance rate (most accessible)

  const names = entries.map(([n,_]) => n);
  const colors2 = [COLORS.Accepted, COLORS.Waitlisted, COLORS.Interview, COLORS.Rejected, COLORS.Other];
  const decLabels = ['Accepted','Waitlisted','Interview','Rejected','Other'];
  const decIdxMap = [0,2,3,1,4];

  const traces = decIdxMap.map((di,i) => ({{
    type:'bar', orientation:'h', name:decLabels[i],
    y: names,
    x: entries.map(([_,ss]) => ss.by_dec[di]),
    marker:{{color:colors2[i], opacity:0.82}},
    hovertemplate:'%{{y}}<br>'+decLabels[i]+': %{{x}}<extra></extra>',
  }}));

  // Add acceptance rate annotations
  const annotations = entries.map(([n,ss]) => {{
    const pct = ss.total ? (100*ss.by_dec[0]/ss.total).toFixed(0) : 0;
    return {{
      x: ss.total + ss.total*0.02,
      y: n,
      text: pct+'%',
      showarrow: false,
      font:{{size:9, color:'#999'}},
      xanchor:'left',
    }};
  }});

  const sq = schoolFilter.toLowerCase().trim();
  Plotly.react('school-chart', traces, {{
    barmode:'stack',
    paper_bgcolor:'#fff', plot_bgcolor:'#fff',
    margin:{{l:200,r:60,t:8,b:40}},
    xaxis:{{tickfont:{{size:10,color:'#bbb'}},gridcolor:'#eee',zeroline:false,title:{{text:'Reports',font:{{size:11,color:'#bbb'}}}}}},
    yaxis:{{tickfont:{{size:10,color:'#555'}},gridcolor:'#eee',zeroline:false,automargin:true}},
    legend:{{orientation:'h',x:0,y:-0.08,font:{{size:11}}}},
    annotations,
    hovermode:'y unified',
  }}, CFG);
}}

// ── Year trends ───────────────────────────────────────────────────────────
function renderYearTrends() {{
  // Filter YEAR_STATS to activeYears
  const years = Object.keys(YEAR_STATS).map(Number)
    .filter(y => activeYears.has(y))
    .sort((a,b)=>a-b);

  // If school filter is active, compute year stats from filtered data
  let ys;
  if (schoolFilter.trim()) {{
    ys = {{}};
    F.forEach(r => {{
      const y = String(r[1]);
      if (!ys[y]) ys[y] = [0,0,0,0,0,0];
      ys[y][r[3]]++;
      ys[y][5]++;
    }});
  }} else {{
    ys = YEAR_STATS;
  }}

  const filtYears = Object.keys(ys).map(Number)
    .filter(y => activeYears.has(y))
    .sort((a,b)=>a-b);

  const traces = [0,1,2].map(d => ({{
    type:'scatter', mode:'lines+markers', name:DEC_NAMES[d],
    x: filtYears,
    y: filtYears.map(y => (ys[String(y)]||[0,0,0,0,0])[d]),
    line:{{color:DEC_COLORS[d], width:2}},
    marker:{{color:DEC_COLORS[d], size:5}},
  }}));

  Plotly.react('year-chart', traces, {{
    paper_bgcolor:'#fff', plot_bgcolor:'#fff',
    margin:{{l:44,r:16,t:8,b:36}},
    xaxis:{{tickfont:{{size:11,color:'#bbb'}},tickformat:'d',gridcolor:'#eee',zeroline:false}},
    yaxis:{{tickfont:{{size:10,color:'#bbb'}},gridcolor:'#eee',zeroline:false}},
    legend:{{orientation:'h',x:0,y:-0.2,font:{{size:11}}}},
    hovermode:'x unified',
  }}, CFG);
}}

// ── Table ─────────────────────────────────────────────────────────────────
const THS = document.querySelectorAll('#data-table th[data-ci]');
THS.forEach(th => {{
  th.addEventListener('click', () => {{
    const ci = +th.dataset.ci;
    if (sortCI===ci) sortDir*=-1; else {{sortCI=ci; sortDir=-1;}}
    THS.forEach(t => {{t.classList.remove('sorted'); t.querySelector('.arr').innerHTML='&#x21D5;';}});
    th.classList.add('sorted');
    th.querySelector('.arr').innerHTML = sortDir===1 ? '&#x2191;' : '&#x2193;';
    renderTable();
  }});
}});

function sortedF() {{
  const ci=sortCI, sd=sortDir;
  return F.slice().sort((a,b) => {{
    let av=a[ci], bv=b[ci];
    const nil = sd===1 ? '￿' : '';
    if (av===null||av===undefined||av==='') av=nil;
    if (bv===null||bv===undefined||bv==='') bv=nil;
    if (av<bv) return -sd; if (av>bv) return sd; return 0;
  }});
}}

function renderTable() {{
  const data = sortedF();
  const total = data.length;
  const totalPages = Math.max(1,Math.ceil(total/PG));
  if (page>totalPages) page=totalPages;
  const slice = data.slice((page-1)*PG, page*PG);

  document.getElementById('rec-count').textContent = total.toLocaleString()+' records';

  const tbody = document.getElementById('tbl-body');
  tbody.innerHTML = slice.map(r => {{
    const dot = `<span class="dot" style="background:${{DEC_COLORS[r[3]]}}"></span>`;
    const gpa = r[5]!==null ? r[5].toFixed(2) : '';
    const cy = seasonLabel(r);
    return `<tr>
      <td>${{SCHOOLS[r[0]]||''}}</td>
      <td>${{cy}}</td>
      <td>${{dot}}${{DEC_NAMES[r[3]]}}</td>
      <td>${{r[9]||''}}</td>
      <td>${{fmt(gpa)}}</td>
      <td>${{fmt(r[6])}}</td>
      <td>${{fmt(r[7])}}</td>
      <td>${{r[8]<2?STATUS_NAMES[r[8]]:''}}</td>
    </tr>`;
  }}).join('');

  renderPagination(total,totalPages);
}}

function renderPagination(total, totalPages) {{
  const el = document.getElementById('pagination');
  if (totalPages<=1) {{el.innerHTML=''; return;}}
  let h=`<button class="pbtn" id="pp" ${{page===1?'disabled':''}}>&#8592;</button>`;
  const pages=[];
  if (totalPages<=7) for (let i=1;i<=totalPages;i++) pages.push(i);
  else {{
    pages.push(1);
    if (page>3) pages.push('…');
    for (let i=Math.max(2,page-1);i<=Math.min(totalPages-1,page+1);i++) pages.push(i);
    if (page<totalPages-2) pages.push('…');
    pages.push(totalPages);
  }}
  pages.forEach(p => {{
    if (p==='…') h+=`<span class="pinfo">…</span>`;
    else h+=`<button class="pbtn${{p===page?' active':''}}" data-p="${{p}}">${{p}}</button>`;
  }});
  h+=`<button class="pbtn" id="np" ${{page===totalPages?'disabled':''}}>&#8594;</button>`;
  h+=`<span class="pinfo">Page ${{page}} of ${{totalPages}}</span>`;
  el.innerHTML=h;
  el.querySelectorAll('.pbtn[data-p]').forEach(b => b.addEventListener('click',()=>{{page=+b.dataset.p;renderTable();}}));
  const pp=el.querySelector('#pp'); if(pp) pp.addEventListener('click',()=>{{page--;renderTable();}});
  const np=el.querySelector('#np'); if(np) np.addEventListener('click',()=>{{page++;renderTable();}});
}}

// ── Render all ────────────────────────────────────────────────────────────
function renderAll() {{
  updateStats();
  renderSpotlight();
  renderTimeline();
  renderVolume();
  renderScores();
  renderSchoolChart();
  renderYearTrends();
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

def run(recent_only: bool = False, test_seasons: list | None = None) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    DOCS_DIR.mkdir(exist_ok=True)

    # Build school map
    c.build_school_map(str(DATA_DIR / "school_map.csv"))

    # Load Deedy baseline — PhD only
    all_deedy = load_deedy_baseline()
    deedy_rows = [r for r in all_deedy if r.get("degree") == "PhD"]
    print(f"  Deedy PhD rows: {len(deedy_rows)} (of {len(all_deedy)} total)")

    # Determine seasons to scrape
    if test_seasons:
        seasons = test_seasons
    elif recent_only:
        seasons = ALL_SEASONS[-2:]
    else:
        seasons = ALL_SEASONS

    # Load existing CSV if doing recent update
    existing = load_existing_csv() if recent_only else []

    # Scrape (PhD only — scraper.py has degrees=["PhD"])
    print(f"Scraping seasons: {seasons}")
    scraped = scrape_gradcafe(seasons)

    # Merge
    if recent_only:
        existing_deedy = [r for r in existing if r.get("source") == "deedy_2015"]
        existing_scraped = [r for r in existing if r.get("source") != "deedy_2015"]
        all_rows = merge_and_dedup(existing_deedy + deedy_rows + existing_scraped, scraped)
    else:
        all_rows = merge_and_dedup(deedy_rows, scraped)

    # Sort by date_posted desc
    all_rows.sort(key=lambda r: r.get("date_posted") or "", reverse=True)

    # Save CSV
    save_csv(all_rows)

    # Write last_updated
    today = date.today().isoformat()
    LAST_UPDATED_PATH.write_text(today)

    # Generate HTML
    print("Generating docs/index.html ...")
    html = generate_html(all_rows, today)
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"HTML written ({len(html):,} bytes)")

    # Summary
    print("\n--- Summary ---")
    print(f"Total records: {len(all_rows):,}")
    dcount = sum(1 for r in all_rows if r.get("source") == "deedy_2015")
    scount = sum(1 for r in all_rows if r.get("source") == "gradcafe_scrape")
    print(f"  Deedy PhD: {dcount:,}")
    print(f"  GradCafe scrape: {scount:,}")
    dec_counts = {}
    for r in all_rows:
        d = r.get("decision_class", "Other")
        dec_counts[d] = dec_counts.get(d, 0) + 1
    for d, ct in sorted(dec_counts.items(), key=lambda x: -x[1]):
        print(f"  {d}: {ct:,}")

    print("\nFirst 10 rows:")
    cols = ["school_clean", "season_code", "degree", "decision_class", "date_posted", "gpa", "gre_v"]
    print("  ".join(cols))
    print("-" * 80)
    for row in all_rows[:10]:
        print("  ".join(str(row.get(col, "")) for col in cols))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--recent", action="store_true")
    parser.add_argument("--test", nargs="+", metavar="SEASON")
    args = parser.parse_args()
    run(recent_only=args.recent, test_seasons=args.test)
