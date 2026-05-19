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

    # Top-30 school indices (ranks 1–30 in clean.py SCHOOL_RULES)
    top30_names = {name for _, name, rank in c.SCHOOL_RULES if rank and rank <= 30}
    schools_list = json.loads(schools_json)
    top30_idxs_json = json.dumps([i for i, s in enumerate(schools_list) if s in top30_names])
    top30_count = sum(1 for s in schools_list if s in top30_names)

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
  font-size: 18px;
  line-height: 1.6;
}}
a {{ color: #1a1a1a; }}
.container {{ max-width: 1340px; margin: 0 auto; padding: 0 36px; }}

/* ── Header ── */
header {{ padding: 32px 0 20px; border-bottom: 1px solid #c0c0c0; }}
header h1 {{ font-size: 28px; font-weight: 700; margin: 0; letter-spacing: -.5px; font-family: Georgia, 'Times New Roman', serif; }}

/* ── Stats strip ── */
.stats-strip {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0;
  border-bottom: 1px solid #c0c0c0;
  padding: 14px 0;
}}
.stat-card {{ padding: 6px 0; }}
.stat-num {{ font-size: 25px; font-weight: 600; letter-spacing: -.5px; }}
.stat-label {{ font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: #555; margin-top: 2px; }}

/* ── Filter bar ── */
.filter-bar {{
  position: sticky; top: 0;
  background: #ffffff;
  border-bottom: 1px solid #c0c0c0;
  z-index: 100; padding: 10px 0;
}}
.filter-inner {{
  display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start;
}}
.filter-group {{ display: flex; flex-direction: column; gap: 4px; }}
.filter-group .glabel {{
  font-size: 12px; text-transform: uppercase; letter-spacing: .07em; color: #666;
}}
select, input[type="text"] {{
  border: 1px solid #c0c0c0; background: #fff; color: #1a1a1a;
  padding: 5px 10px; font-size: 16px; font-family: inherit;
  border-radius: 4px; outline: none;
}}
select:focus, input:focus {{ border-color: #777; }}
.search-wrap {{ position: relative; display: inline-flex; align-items: center; }}
.search-clear {{
  position: absolute; right: 6px; border: none; background: none;
  color: #999; cursor: pointer; font-size: 19px; line-height: 1;
  padding: 0; display: none;
}}
.search-clear:hover {{ color: #1a1a1a; }}
.search-clear.visible {{ display: block; }}
.cb-group {{ display: flex; flex-wrap: wrap; gap: 6px; max-width: 540px; }}
.cb-group label {{
  display: flex; align-items: center; gap: 4px;
  font-size: 15px; cursor: pointer; white-space: nowrap;
}}
/* Year pills */
.year-bar {{ display: flex; flex-wrap: wrap; gap: 5px; align-items: center; max-width: 760px; }}
.yr-shortcut {{
  font-size: 13px; padding: 3px 9px; border: 1px solid #999;
  border-radius: 12px; cursor: pointer; background: #fff; color: #444;
  font-family: inherit; white-space: nowrap;
}}
.yr-shortcut:hover {{ border-color: #444; color: #1a1a1a; }}
.yr-shortcut.active {{ background: #1a1a1a; color: #fff; border-color: #1a1a1a; }}
.yr-pill {{
  font-size: 14px; padding: 2px 8px; border: 1px solid #bbb;
  border-radius: 12px; cursor: pointer; background: #fff; color: #555;
  font-family: inherit; white-space: nowrap; transition: all .1s;
}}
.yr-pill:hover {{ border-color: #555; color: #1a1a1a; }}
.yr-pill.active {{ background: #1a1a1a; color: #fff; border-color: #1a1a1a; }}
.reset-btn {{
  font-size: 14px; color: #666; text-decoration: underline;
  cursor: pointer; align-self: flex-end; padding-bottom: 4px;
  border: none; background: none; font-family: inherit;
}}
.reset-btn:hover {{ color: #1a1a1a; }}

/* ── Scope toggle ── */
.scope-toggle {{ display: flex; }}
.scope-btn {{
  font-size: 14px; padding: 4px 12px; border: 1px solid #bbb;
  background: #fff; color: #555; cursor: pointer; font-family: inherit;
  transition: all .1s; line-height: 1.4;
}}
.scope-btn:first-child {{ border-radius: 4px 0 0 4px; }}
.scope-btn:last-child {{ border-radius: 0 4px 4px 0; border-left: none; }}
.scope-btn.active {{ background: #1a1a1a; color: #fff; border-color: #1a1a1a; }}
.scope-btn:not(.active):hover {{ background: #ebebeb; color: #1a1a1a; }}
select#school-select {{ width: 220px; }}

/* ── School spotlight ── */
#spotlight {{
  display: none;
  background: #f2f2f2;
  border-bottom: 1px solid #c0c0c0;
  padding: 14px 0;
}}
.spotlight-inner {{ display: flex; gap: 36px; align-items: flex-start; flex-wrap: wrap; }}
.spotlight-name {{ font-size: 20px; font-weight: 600; margin: 0 0 4px; }}
.spotlight-meta {{ font-size: 15px; color: #444; }}
.spotlight-stats {{ display: flex; gap: 22px; margin-top: 10px; flex-wrap: wrap; }}
.sstat {{ text-align: left; }}
.sstat-val {{ font-size: 19px; font-weight: 600; }}
.sstat-label {{ font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: #555; }}
#spotlight-mini {{ flex: 1; min-width: 260px; height: 130px; }}

/* ── Sections ── */
.section {{ padding: 26px 0 0; }}
.section:last-of-type {{ padding-bottom: 28px; }}
.section-title {{
  font-size: 14px; font-variant: small-caps; letter-spacing: .06em;
  color: #555; margin: 0 0 10px; text-transform: uppercase;
}}
.chart-pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
@media (max-width: 700px) {{ .chart-pair {{ grid-template-columns: 1fr; }} }}
.chart-note {{ font-size: 13px; color: #666; margin: 5px 0 0; font-style: italic; }}

#timeline-chart {{ width: 100%; height: 540px; }}
#gpa-chart {{ width: 100%; height: 300px; }}
#grev-chart {{ width: 100%; height: 300px; }}
#year-chart {{ width: 100%; height: 260px; }}

/* ── Table ── */
.table-header {{
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 8px;
}}
.record-count {{ font-size: 14px; color: #666; }}
table {{ width: 100%; border-collapse: collapse; font-size: 15px; }}
th {{
  font-variant: small-caps; font-size: 13px; letter-spacing: .05em;
  text-align: left; color: #555; padding: 6px 8px;
  border-bottom: 1px solid #c0c0c0;
  cursor: pointer; user-select: none; white-space: nowrap;
}}
th:hover {{ color: #1a1a1a; }}
th .arr {{ margin-left: 2px; opacity: 0.4; }}
th.sorted .arr {{ opacity: 1; }}
td {{ padding: 5px 8px; border-bottom: 1px solid #e5e5e5; }}
tr:hover td {{ background: #f2f2f2; }}
.dot {{
  display: inline-block; width: 9px; height: 9px;
  border-radius: 50%; margin-right: 5px; vertical-align: middle;
}}
.pagination {{
  display: flex; gap: 4px; align-items: center;
  justify-content: flex-end; margin-top: 10px; font-size: 15px;
}}
.pbtn {{
  border: 1px solid #c0c0c0; background: #fff; color: #1a1a1a;
  padding: 3px 10px; cursor: pointer; font-size: 15px;
  border-radius: 4px; font-family: inherit;
}}
.pbtn:hover {{ background: #ebebeb; }}
.pbtn.active {{ background: #1a1a1a; color: #fff; border-color: #1a1a1a; }}
.pbtn:disabled {{ opacity: 0.35; cursor: default; }}
.pinfo {{ color: #666; margin: 0 6px; font-size: 14px; }}

/* ── Footer ── */
footer {{
  border-top: 1px solid #c0c0c0;
  padding: 18px 0; font-size: 14px; color: #666;
}}
</style>
</head>
<body>
<div class="container">

<header>
  <h1>Political Science PhD Admissions</h1>
</header>

<div class="filter-bar">
  <div class="container">
    <div class="filter-inner">
      <div class="filter-group">
        <span class="glabel">Programs</span>
        <div class="scope-toggle">
          <button class="scope-btn active" id="scope-featured">Featured (30)</button>
          <button class="scope-btn" id="scope-all">All Programs</button>
        </div>
      </div>
      <div class="filter-group" id="school-group-featured">
        <span class="glabel">School</span>
        <select id="school-select">
          <option value="">All 30 featured</option>
        </select>
      </div>
      <div class="filter-group" id="school-group-all" style="display:none">
        <span class="glabel">School</span>
        <div class="search-wrap">
          <input type="text" id="school-search" placeholder="Type school name&hellip;" style="width:200px;padding-right:22px" autocomplete="off">
          <button class="search-clear" id="search-clear" title="Clear">&times;</button>
        </div>
      </div>
      <div class="filter-group">
        <span class="glabel">Outcome</span>
        <div class="cb-group" id="dec-checkboxes">
          <label><input type="checkbox" value="0" checked> Accepted</label>
          <label><input type="checkbox" value="1" checked> Rejected</label>
          <label><input type="checkbox" value="2" checked> Waitlisted</label>
          <label><input type="checkbox" value="3" checked> Interview</label>
          <label><input type="checkbox" value="4" checked> Other</label>
        </div>
      </div>
      <div class="filter-group">
        <span class="glabel">Cycle Year</span>
        <div class="year-bar">
          <button class="yr-shortcut active" id="yr-all">All years</button>
          <button class="yr-shortcut" id="yr-last10">Last 10 years</button>
          <button class="yr-shortcut" id="yr-last5">Last 5 years</button>
          <button class="yr-shortcut" id="yr-last1">Last year</button>
          <span style="color:#ddd;font-size:11px;align-self:center">|</span>
          <div id="year-pills"></div>
        </div>
      </div>
      <button class="reset-btn" id="reset-btn">Reset all</button>
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
  <p class="chart-note" style="margin:0 0 6px">Each dot is one reported decision. Same-day notifications stack by outcome. Hover for details.</p>
  <div id="timeline-chart"></div>
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
const TOP30_IDXS = new Set({top30_idxs_json});
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
let top30Mode = true;
let sortCI = 9;  // date
let sortDir = -1;
let page = 1;
const PG = 25;
const CFG = {{responsive:true,displaylogo:false,modeBarButtonsToRemove:['toImage','select2d','lasso2d','sendDataToCloud']}};

// ── Build year pills ─────────────────────────────────────────────────────
(function() {{
  const container = document.getElementById('year-pills');
  FALL_YEARS.forEach(y => {{
    const btn = document.createElement('button');
    btn.className = 'yr-pill active';
    btn.textContent = y;
    btn.dataset.year = y;
    container.append(btn);
  }});
}})();

// ── Populate school select ───────────────────────────────────────────────
(function() {{
  const sel = document.getElementById('school-select');
  const sorted = [...TOP30_IDXS].map(i => ({{i, name: SCHOOLS[i]}}))
    .sort((a, b) => a.name.localeCompare(b.name));
  sorted.forEach(({{i, name}}) => {{
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = name;
    sel.append(opt);
  }});
}})();

// ── Utility ──────────────────────────────────────────────────────────────
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
  exactSchool = null;
  const selEl = document.getElementById('school-select');
  const selectedIdx = (top30Mode && selEl && selEl.value !== '') ? +selEl.value : -1;
  const sq = top30Mode ? '' : schoolFilter.toLowerCase().trim();

  if (top30Mode && selectedIdx >= 0) {{
    exactSchool = SCHOOLS[selectedIdx];
  }} else if (!top30Mode && sq) {{
    const matches = SCHOOLS.filter(s => s.toLowerCase().includes(sq));
    exactSchool = matches.length === 1 ? matches[0] : null;
  }}

  F = DATA.filter(r => {{
    if (top30Mode) {{
      if (selectedIdx >= 0) {{
        if (r[0] !== selectedIdx) return false;
      }} else {{
        if (!TOP30_IDXS.has(r[0])) return false;
      }}
    }} else {{
      if (sq && !SCHOOLS[r[0]].toLowerCase().includes(sq)) return false;
    }}
    if (!activeYears.has(r[1])) return false;
    if (!activeDecs.has(r[3])) return false;
    return true;
  }});
  page = 1;
  renderAll();
}}

// ── Wire filters ─────────────────────────────────────────────────────────
function syncShortcutBtns() {{
  const maxY = FALL_YEARS.length ? Math.max(...FALL_YEARS) : 2026;
  const allActive = FALL_YEARS.every(y => activeYears.has(y));
  const last10Yrs = FALL_YEARS.filter(y => y >= maxY - 9);
  const last5Yrs  = FALL_YEARS.filter(y => y >= maxY - 4);
  const last1Yrs  = FALL_YEARS.filter(y => y === maxY);
  const last10Active = last10Yrs.length > 0
    && last10Yrs.every(y => activeYears.has(y))
    && FALL_YEARS.filter(y => y < maxY - 9).every(y => !activeYears.has(y));
  const last5Active = last5Yrs.length > 0
    && last5Yrs.every(y => activeYears.has(y))
    && FALL_YEARS.filter(y => y < maxY - 4).every(y => !activeYears.has(y));
  const last1Active = last1Yrs.length > 0
    && last1Yrs.every(y => activeYears.has(y))
    && FALL_YEARS.filter(y => y < maxY).every(y => !activeYears.has(y));
  document.getElementById('yr-all').classList.toggle('active', allActive);
  document.getElementById('yr-last10').classList.toggle('active', last10Active);
  document.getElementById('yr-last5').classList.toggle('active', last5Active);
  document.getElementById('yr-last1').classList.toggle('active', last1Active);
}}

let debounceTimer;
document.getElementById('school-search').addEventListener('input', e => {{
  schoolFilter = e.target.value;
  document.getElementById('search-clear').classList.toggle('visible', schoolFilter.length > 0);
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(applyFilters, 280);
}});
document.getElementById('search-clear').addEventListener('click', () => {{
  document.getElementById('school-search').value = '';
  schoolFilter = '';
  document.getElementById('search-clear').classList.remove('visible');
  applyFilters();
}});
document.getElementById('scope-featured').addEventListener('click', () => {{
  if (top30Mode) return;
  top30Mode = true;
  document.getElementById('scope-featured').classList.add('active');
  document.getElementById('scope-all').classList.remove('active');
  document.getElementById('school-group-featured').style.display = '';
  document.getElementById('school-group-all').style.display = 'none';
  schoolFilter = '';
  applyFilters();
}});
document.getElementById('scope-all').addEventListener('click', () => {{
  if (!top30Mode) return;
  top30Mode = false;
  document.getElementById('scope-all').classList.add('active');
  document.getElementById('scope-featured').classList.remove('active');
  document.getElementById('school-group-featured').style.display = 'none';
  document.getElementById('school-group-all').style.display = '';
  document.getElementById('school-select').value = '';
  applyFilters();
}});
document.getElementById('school-select').addEventListener('change', () => {{
  applyFilters();
}});
document.getElementById('dec-checkboxes').addEventListener('change', e => {{
  activeDecs.clear();
  document.querySelectorAll('#dec-checkboxes input:checked').forEach(cb => activeDecs.add(+cb.value));
  applyFilters();
}});
document.getElementById('year-pills').addEventListener('click', e => {{
  const pill = e.target.closest('.yr-pill');
  if (!pill) return;
  const y = +pill.dataset.year;
  if (activeYears.has(y)) {{ activeYears.delete(y); pill.classList.remove('active'); }}
  else {{ activeYears.add(y); pill.classList.add('active'); }}
  syncShortcutBtns();
  applyFilters();
}});
document.getElementById('yr-all').addEventListener('click', () => {{
  activeYears = new Set(FALL_YEARS);
  document.querySelectorAll('.yr-pill').forEach(p => p.classList.add('active'));
  syncShortcutBtns();
  applyFilters();
}});
document.getElementById('yr-last10').addEventListener('click', () => {{
  const maxY = FALL_YEARS.length ? Math.max(...FALL_YEARS) : 2026;
  activeYears = new Set(FALL_YEARS.filter(y => y >= maxY - 9));
  document.querySelectorAll('.yr-pill').forEach(p => {{
    p.classList.toggle('active', +p.dataset.year >= maxY - 9);
  }});
  syncShortcutBtns();
  applyFilters();
}});
document.getElementById('yr-last5').addEventListener('click', () => {{
  const maxY = FALL_YEARS.length ? Math.max(...FALL_YEARS) : 2026;
  activeYears = new Set(FALL_YEARS.filter(y => y >= maxY - 4));
  document.querySelectorAll('.yr-pill').forEach(p => {{
    p.classList.toggle('active', +p.dataset.year >= maxY - 4);
  }});
  syncShortcutBtns();
  applyFilters();
}});
document.getElementById('yr-last1').addEventListener('click', () => {{
  const maxY = FALL_YEARS.length ? Math.max(...FALL_YEARS) : 2026;
  activeYears = new Set(FALL_YEARS.filter(y => y === maxY));
  document.querySelectorAll('.yr-pill').forEach(p => {{
    p.classList.toggle('active', +p.dataset.year === maxY);
  }});
  syncShortcutBtns();
  applyFilters();
}});
document.getElementById('reset-btn').addEventListener('click', () => {{
  top30Mode = true;
  document.getElementById('scope-featured').classList.add('active');
  document.getElementById('scope-all').classList.remove('active');
  document.getElementById('school-group-featured').style.display = '';
  document.getElementById('school-group-all').style.display = 'none';
  document.getElementById('school-select').value = '';
  document.getElementById('school-search').value = '';
  document.getElementById('search-clear').classList.remove('visible');
  schoolFilter = '';
  document.querySelectorAll('#dec-checkboxes input').forEach(cb => cb.checked=true);
  activeYears = new Set(FALL_YEARS);
  document.querySelectorAll('.yr-pill').forEach(p => p.classList.add('active'));
  activeDecs = new Set([0,1,2,3,4]);
  syncShortcutBtns();
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

  let sidx = -1;
  if (top30Mode) {{
    const selEl = document.getElementById('school-select');
    if (selEl && selEl.value !== '') sidx = +selEl.value;
  }} else if (schoolFilter.trim()) {{
    const sq = schoolFilter.toLowerCase().trim();
    SCHOOLS.forEach((s, i) => {{ if (s.toLowerCase().includes(sq) && sidx < 0) sidx = i; }});
  }}

  if (sidx < 0) {{ el.style.display='none'; return; }}

  el.style.display='block';
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
      xaxis:{{tickfont:{{size:13,color:'#555'}},tickformat:'d',gridcolor:'#d5d5d5'}},
      yaxis:{{tickfont:{{size:13,color:'#555'}},gridcolor:'#d5d5d5',zeroline:false}},
      showlegend:false,
    }}, {{...CFG, staticPlot:false}});
  }} else {{
    document.getElementById('sp-meta').textContent = 'School not found in stats';
    document.getElementById('sp-stats').innerHTML = '';
  }}
}}

// ── Timeline (stacked dots) ───────────────────────────────────────────────
function renderTimeline() {{
  const isSingle = exactSchool !== null;
  const dotSize    = isSingle ? 10 : 4;
  const dotOpacity = isSingle ? 1.0 : 0.75;

  const dayGroups = {{}};
  F.forEach((r, ri) => {{
    if (r[4] === null) return;
    if (!dayGroups[r[4]]) dayGroups[r[4]] = [];
    dayGroups[r[4]].push(ri);
  }});

  const stackY = new Array(F.length).fill(null);
  let maxStack = 0;
  Object.values(dayGroups).forEach(indices => {{
    indices.sort((a, b) => F[a][3] - F[b][3]);
    indices.forEach((ri, pos) => {{ stackY[ri] = pos; }});
    if (indices.length > maxStack) maxStack = indices.length;
  }});

  const traceType = isSingle ? 'scatter' : 'scattergl';
  const traces = [0,1,2,3,4].map(d => {{
    const x=[], y=[], text=[];
    F.forEach((r, ri) => {{
      if (r[3] !== d || r[4] === null || stackY[ri] === null) return;
      x.push(r[4]);
      y.push(stackY[ri]);
      text.push(tooltip(r));
    }});
    return {{
      type: traceType, mode:'markers', name:DEC_NAMES[d],
      x, y, text, hovertemplate:'%{{text}}<extra></extra>',
      marker:{{color:DEC_COLORS[d], size:dotSize, opacity:dotOpacity}},
      visible: x.length > 0 ? true : 'legendonly',
    }};
  }});

  const yPad = isSingle ? Math.max(2, Math.ceil(maxStack * 0.3)) : 4;
  const tickvals=[0,30,61,92,120,151,181];
  const ticktext=['Nov','Dec','Jan','Feb','Mar','Apr','May'];
  Plotly.react('timeline-chart', traces, {{
    paper_bgcolor:'#fff', plot_bgcolor:'#fff',
    margin:{{l:8,r:16,t:6,b:40}},
    xaxis:{{tickvals,ticktext,tickfont:{{size:14,color:'#555'}},
            gridcolor:'#d5d5d5',gridwidth:1,zeroline:false,showline:false}},
    yaxis:{{visible:false, range:[-1, maxStack + yPad], zeroline:false}},
    legend:{{orientation:'h',x:0,y:-0.12,font:{{size:14}}}},
    hovermode:'closest',
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
    xaxis:{{tickfont:{{size:14,color:'#666'}},gridcolor:'#d5d5d5',zeroline:false}},
    yaxis:{{tickfont:{{size:13,color:'#666'}},gridcolor:'#d5d5d5',zeroline:false}},
    showlegend:false,
  }};

  const gpaCount = gpas.flat().length;
  const grevCount = grevs.flat().length;
  document.getElementById('gpa-note').textContent = gpaCount+' records with GPA data';
  document.getElementById('grev-note').textContent = grevCount+' records with GRE-V data (new scale 130–170 only)';

  Plotly.react('gpa-chart', gpaTraces.length ? gpaTraces : [{{type:'box',y:[],name:''}}],
    {{...baseLayout, title:{{text:'GPA',font:{{size:15,color:'#999'}},x:0.04,xanchor:'left'}},
      yaxis:{{...baseLayout.yaxis, range:[2.5,4.2]}}}}, CFG);
  Plotly.react('grev-chart', grevTraces.length ? grevTraces : [{{type:'box',y:[],name:''}}],
    {{...baseLayout, title:{{text:'GRE Verbal',font:{{size:15,color:'#999'}},x:0.04,xanchor:'left'}},
      yaxis:{{...baseLayout.yaxis, range:[140,172]}}}}, CFG);
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
    xaxis:{{tickfont:{{size:14,color:'#666'}},tickformat:'d',gridcolor:'#d5d5d5',zeroline:false}},
    yaxis:{{tickfont:{{size:13,color:'#666'}},gridcolor:'#d5d5d5',zeroline:false}},
    legend:{{orientation:'h',x:0,y:-0.2,font:{{size:14}}}},
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
  renderScores();
  renderYearTrends();
  renderTable();
}}

applyFilters();
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(recent_only: bool = False, test_seasons: list | None = None, html_only: bool = False) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    DOCS_DIR.mkdir(exist_ok=True)

    if html_only:
        print("HTML-only mode: loading existing CSV ...")
        all_rows = load_existing_csv()
        today = LAST_UPDATED_PATH.read_text().strip() if LAST_UPDATED_PATH.exists() else date.today().isoformat()
        print(f"Loaded {len(all_rows):,} rows from CSV")
        print("Generating docs/index.html ...")
        html = generate_html(all_rows, today)
        HTML_PATH.write_text(html, encoding="utf-8")
        print(f"HTML written ({len(html):,} bytes)")
        return

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
    parser.add_argument("--html-only", action="store_true")
    args = parser.parse_args()
    run(recent_only=args.recent, test_seasons=args.test, html_only=args.html_only)
