"""Pipeline orchestrator: load -> scrape -> merge -> clean -> CSV -> HTML."""

import argparse
import csv
import json
import statistics
from datetime import date
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

JUNK_SCHOOLS = {
    "McDonalds", "London College of Piss (LCP)", "London College of Piss",
    "Donald Trump State University", "Coomtown University", "Cuumtown Seminary",
    "HELP", "Wherever I Get In", "Ravinder Singh", "NSF GRFP", "All",
    "Fulbright Canada", "Ch", "Hot Piss University", "Social Sciences",
    "University Of Massholes", "University of Massholes", "Other",
    "University of the Arts", "University Canada West", "Iqtisad Uni",
    "IED", "University of Ca", "University of Sou", "Yobe state University",
}

_DECISION_COLORS = {
    "Accepted":   "#1e7a34",
    "Rejected":   "#c0392b",
    "Waitlisted": "#c47f17",
    "Interview":  "#5b4a9a",
    "Other":      "#7a7a7a",
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
    schools = sorted(set(
        (r.get("school_clean") or r.get("school_raw", "")).strip()
        for r in rows
        if (r.get("school_clean") or r.get("school_raw", "")).strip() not in JUNK_SCHOOLS
    ))
    school_to_idx = {s: i for i, s in enumerate(schools)}

    ss: dict[str, dict] = {}
    for s in schools:
        ss[s] = {
            "total": 0,
            "by_dec": [0, 0, 0, 0, 0],
            "gpas_by_dec": {i: [] for i in range(5)},
            "grevs_by_dec": {i: [] for i in range(5)},
            "greqs_by_dec": {i: [] for i in range(5)},
            "by_year": {},
        }

    ys: dict[str, list] = {}

    data = []
    for r in rows:
        school = (r.get("school_clean") or r.get("school_raw", "")).strip()
        if school in JUNK_SCHOOLS:
            continue
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

        if school in ss:
            entry = ss[school]
            entry["total"] += 1
            entry["by_dec"][didx] += 1
            if gpa is not None and 0 < gpa <= 4.5:
                entry["gpas_by_dec"][didx].append(gpa)
            if gre_v is not None and 130 <= gre_v <= 170:
                entry["grevs_by_dec"][didx].append(gre_v)
            if gre_q is not None and 130 <= gre_q <= 170:
                entry["greqs_by_dec"][didx].append(gre_q)
            yk = str(year)
            if yk not in entry["by_year"]:
                entry["by_year"][yk] = [0, 0, 0, 0, 0]
            entry["by_year"][yk][didx] += 1

        yk = str(year)
        if yk not in ys:
            ys[yk] = [0, 0, 0, 0, 0, 0]
        ys[yk][didx] += 1
        ys[yk][5] += 1

    school_stats_out = {}
    for s, entry in ss.items():
        if entry["total"] < 3:
            continue
        school_stats_out[s] = {
            "total": entry["total"],
            "by_dec": entry["by_dec"],
            "med_gpa_acc": _median(entry["gpas_by_dec"][0]),
            "med_grev_acc": _median(entry["grevs_by_dec"][0]),
            "med_greq_acc": _median(entry["greqs_by_dec"][0]),
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

    fall_years = sorted(set(
        r["season_year"] for r in rows
        if r.get("season_year") and str(r.get("season_code", "")).startswith("F")
    ), reverse=True)
    fall_years_json = json.dumps(fall_years)
    colors_json = json.dumps(_DECISION_COLORS)

    # Top-30 schools for optgroup
    top30_names_set = {name for _, name, rank in c.SCHOOL_RULES if rank and rank <= 30}
    schools_list = json.loads(schools_json)
    top30_pairs = sorted(
        [(i, s) for i, s in enumerate(schools_list) if s in top30_names_set],
        key=lambda x: x[1].lower()
    )
    all_pairs = sorted(enumerate(schools_list), key=lambda x: x[1].lower())

    top30_options_html = "\n            ".join(
        f'<option value="{i}">{name}</option>' for i, name in top30_pairs
    )
    all_options_html = "\n            ".join(
        f'<option value="{i}">{name}</option>' for i, name in all_pairs
    )
    top30_idxs_json = json.dumps([i for i, s in enumerate(schools_list) if s in top30_names_set])

    accepted = sum(1 for r in rows if r.get("decision_class") == "Accepted")
    pct_acc = f"{100*accepted/n:.1f}" if n else "0"
    unique_schools = len(set(
        (r.get("school_clean") or r.get("school_raw", "")).strip()
        for r in rows if r.get("school_clean") or r.get("school_raw")
    ))
    year_range = "2006–2026"

    c_acc = _DECISION_COLORS["Accepted"]
    c_rej = _DECISION_COLORS["Rejected"]
    c_wl  = _DECISION_COLORS["Waitlisted"]
    c_int = _DECISION_COLORS["Interview"]
    c_oth = _DECISION_COLORS["Other"]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="Interactive browser for {n:,} self-reported Political Science PhD admissions outcomes from GradCafe, 2006–2026.">
<title>Political Science PhD Admissions — GradCafe Data</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" crossorigin="anonymous"></script>
<style>
/* ── Reset ── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

/* ── Skip link (accessibility) ── */
.skip-link {{
  position: absolute; top: -56px; left: 0; right: 0;
  background: #0052cc; color: #fff; padding: 14px 24px;
  text-align: center; text-decoration: none; font-size: 15px; font-weight: 600;
  z-index: 2000; transition: top 0.2s ease;
}}
.skip-link:focus {{ top: 0; }}

/* ── Global focus ring ── */
:focus-visible {{
  outline: 3px solid #0052cc;
  outline-offset: 2px;
  border-radius: 3px;
}}

/* ── Base ── */
body {{
  font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
  background: #fff;
  color: #111;
  font-size: 16px;
  line-height: 1.5;
}}
a {{ color: #0052cc; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.container {{ max-width: 1320px; margin: 0 auto; padding: 0 32px; }}
@media (max-width: 640px) {{ .container {{ padding: 0 16px; }} }}

/* ── Header ── */
header {{
  padding: 26px 0 18px;
  border-bottom: 1px solid #e4e4e4;
}}
header h1 {{
  font-size: 24px; font-weight: 700;
  font-family: Georgia, 'Times New Roman', serif;
  letter-spacing: -0.4px; color: #111;
  margin-bottom: 4px;
}}
.header-sub {{
  font-size: 13px; color: #666;
}}

/* ── Stats strip ── */
.stats-strip {{
  display: flex;
  justify-content: center;
  border-bottom: 1px solid #e4e4e4;
  padding: 14px 0;
}}
.stat-card {{
  padding: 6px 40px;
  text-align: center;
  border-right: 1px solid #e4e4e4;
}}
.stat-card:last-child {{ border-right: none; }}
.stat-num {{
  font-size: 26px; font-weight: 700; letter-spacing: -0.5px;
  font-variant-numeric: tabular-nums; color: #111;
}}
.stat-label {{
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: #888;
  margin-top: 2px;
}}
@media (max-width: 640px) {{
  .stats-strip {{ flex-wrap: wrap; }}
  .stat-card {{
    padding: 8px 24px;
    flex: 1 1 45%;
    border-right: none;
    border-bottom: 1px solid #e4e4e4;
  }}
  .stat-card:nth-child(odd) {{ border-right: 1px solid #e4e4e4; }}
  .stat-card:nth-last-child(-n+2) {{ border-bottom: none; }}
}}

/* ── Filter bar ── */
.filter-bar {{
  position: sticky; top: 0; z-index: 200;
  background: #fff;
  border-bottom: 1px solid #e4e4e4;
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  padding: 10px 0;
}}
.filter-inner {{
  display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start;
}}
.filter-group {{ display: flex; flex-direction: column; gap: 3px; }}
.glabel {{
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.1em; color: #888;
}}

/* ── Controls ── */
select, input[type="text"] {{
  border: 1.5px solid #ccc; background: #fff; color: #111;
  padding: 6px 10px; font-size: 14px; font-family: inherit;
  border-radius: 5px; transition: border-color 0.15s;
}}
select:hover, input[type="text"]:hover {{ border-color: #888; }}
select:focus, input[type="text"]:focus {{ border-color: #0052cc; outline: none; box-shadow: 0 0 0 3px rgba(0,82,204,0.15); }}
select#school-select {{ width: 250px; max-width: 100%; }}

/* ── Outcome toggles ── */
.cb-group {{ display: flex; flex-wrap: wrap; gap: 5px; }}
.cb-label {{
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 13px; cursor: pointer; white-space: nowrap;
  padding: 4px 10px; border: 1.5px solid #ddd; border-radius: 20px;
  user-select: none; transition: border-color 0.12s, background 0.12s;
  background: #fff; color: #555;
}}
.cb-label:hover {{ border-color: #999; color: #111; }}
.cb-label input[type="checkbox"] {{
  width: 0; height: 0; opacity: 0; position: absolute;
}}
.cb-label input[type="checkbox"]:focus-visible + .cb-swatch {{
  outline: 3px solid #0052cc; outline-offset: 3px;
}}
.cb-swatch {{
  width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0;
}}
.cb-label.checked {{
  border-color: #555; background: #f5f5f5; color: #111; font-weight: 500;
}}

/* ── Year pills ── */
.year-bar {{ display: flex; flex-wrap: wrap; gap: 4px; align-items: center; max-width: 720px; }}
.yr-shortcut {{
  font-size: 12px; padding: 4px 10px; border: 1.5px solid #ccc;
  border-radius: 20px; cursor: pointer; background: #fff; color: #666;
  font-family: inherit; white-space: nowrap; transition: all 0.12s; font-weight: 500;
}}
.yr-shortcut:hover {{ border-color: #666; color: #111; }}
.yr-shortcut.active {{ background: #111; color: #fff; border-color: #111; }}
[aria-pressed="true"].yr-shortcut {{ background: #111; color: #fff; border-color: #111; }}
.yr-pill {{
  font-size: 12px; padding: 3px 7px; border: 1.5px solid #e0e0e0;
  border-radius: 20px; cursor: pointer; background: #fff; color: #777;
  font-family: inherit; white-space: nowrap; transition: all 0.12s;
}}
.yr-pill:hover {{ border-color: #888; color: #111; }}
.yr-pill.active {{ background: #111; color: #fff; border-color: #111; }}
.year-sep {{ width: 1px; height: 18px; background: #e0e0e0; align-self: center; margin: 0 2px; }}

/* ── Reset button ── */
.reset-btn {{
  font-size: 13px; color: #666; cursor: pointer; align-self: flex-end;
  padding: 6px 14px; border: 1.5px solid #ddd; background: #fff;
  font-family: inherit; border-radius: 5px; transition: all 0.12s; white-space: nowrap;
}}
.reset-btn:hover {{ border-color: #999; color: #111; }}

/* ── School spotlight ── */
#spotlight {{
  display: none;
  background: #f8f9fc;
  border-bottom: 1px solid #e4e4e4;
  padding: 14px 0;
}}
.spotlight-inner {{ display: flex; gap: 32px; align-items: flex-start; flex-wrap: wrap; }}
.spotlight-name {{ font-size: 18px; font-weight: 700; margin-bottom: 3px; color: #111; }}
.spotlight-meta {{ font-size: 13px; color: #666; }}
.spotlight-stats {{ display: flex; gap: 22px; margin-top: 10px; flex-wrap: wrap; }}
.sstat {{ text-align: left; }}
.sstat-val {{ font-size: 20px; font-weight: 700; color: #111; font-variant-numeric: tabular-nums; }}
.sstat-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em; color: #888; margin-top: 1px; }}
#spotlight-mini {{ flex: 1; min-width: 240px; height: 120px; }}

/* ── Content sections ── */
main {{ outline: none; }}
.section {{ padding: 22px 0 0; }}
.section:last-of-type {{ padding-bottom: 36px; }}
.section-title {{
  font-size: 10px; font-weight: 700; letter-spacing: 0.12em; color: #aaa;
  margin-bottom: 10px; text-transform: uppercase;
}}
.chart-note {{ font-size: 12px; color: #999; margin-top: 4px; font-style: italic; }}

/* ── Chart containers ── */
#timeline-chart {{ width: 100%; height: 520px; }}
#gpa-chart {{ width: 100%; height: 290px; }}
#grev-chart {{ width: 100%; height: 290px; }}
#greq-chart {{ width: 100%; height: 290px; }}
#year-chart {{ width: 100%; height: 260px; }}

.chart-triple {{
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
}}
@media (max-width: 900px) {{ .chart-triple {{ grid-template-columns: 1fr 1fr; }} }}
@media (max-width: 580px) {{ .chart-triple {{ grid-template-columns: 1fr; }} }}

/* ── Table ── */
.table-header {{
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 8px;
}}
.record-count {{ font-size: 13px; color: #999; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th {{
  font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
  text-align: left; color: #aaa; padding: 8px 10px 8px 8px;
  border-bottom: 2px solid #e4e4e4;
  cursor: pointer; user-select: none; white-space: nowrap;
  transition: color 0.1s; background: #fff;
}}
th:hover {{ color: #333; }}
th.sorted {{ color: #111; }}
th .arr {{ margin-left: 3px; opacity: 0.3; font-size: 11px; }}
th.sorted .arr {{ opacity: 1; }}
td {{
  padding: 6px 8px; border-bottom: 1px solid #f0f0f0;
  color: #222; font-size: 14px;
}}
tr:last-child td {{ border-bottom: none; }}
tbody tr:hover td {{ background: #f8f9fc; }}
.dot {{
  display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 5px; vertical-align: middle; flex-shrink: 0;
}}
td.null-cell {{ color: #ccc; }}

/* ── Pagination ── */
.pagination {{
  display: flex; gap: 3px; align-items: center;
  justify-content: flex-end; margin-top: 12px; flex-wrap: wrap;
}}
.pbtn {{
  border: 1.5px solid #ddd; background: #fff; color: #333;
  padding: 4px 10px; cursor: pointer; font-size: 13px;
  border-radius: 5px; font-family: inherit; transition: all 0.1s;
}}
.pbtn:hover {{ background: #f0f0f0; border-color: #aaa; }}
.pbtn.active {{ background: #111; color: #fff; border-color: #111; }}
.pbtn:disabled {{ opacity: 0.3; cursor: default; pointer-events: none; }}
.pinfo {{ color: #aaa; margin: 0 4px; font-size: 13px; }}

/* ── Footer ── */
footer {{
  border-top: 1px solid #e4e4e4; padding: 20px 0;
  font-size: 13px; color: #999; margin-top: 4px;
}}
footer a {{ color: #777; }}

/* ── Reduced motion ── */
@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{ transition: none !important; animation: none !important; }}
}}
</style>
</head>
<body>
<a href="#main-content" class="skip-link">Skip to main content</a>

<div class="container">
  <header>
    <h1>Political Science PhD Admissions</h1>
    <p class="header-sub">
      Self-reported outcomes from {n:,} PhD applicants, 2006–2026 &mdash;
      data from <a href="https://www.thegradcafe.com">GradCafe</a> (self-reported, unverified)
    </p>
  </header>
</div>

<div class="filter-bar" role="search" aria-label="Filter admissions data">
  <div class="container">
    <div class="filter-inner">

      <div class="filter-group">
        <label class="glabel" for="school-select">Program</label>
        <select id="school-select" aria-label="Filter by program">
          <option value="">All programs</option>
          <option value="top30">&#9733; Top 30 programs</option>
          <optgroup label="Top 30 by U.S. News rank">
            {top30_options_html}
          </optgroup>
          <optgroup label="All programs A&ndash;Z">
            {all_options_html}
          </optgroup>
        </select>
      </div>

      <div class="filter-group" role="group" aria-labelledby="outcome-lbl">
        <span class="glabel" id="outcome-lbl">Outcome</span>
        <div class="cb-group" id="dec-checkboxes">
          <label class="cb-label checked">
            <input type="checkbox" value="0" checked aria-label="Accepted">
            <span class="cb-swatch" style="background:{c_acc}"></span>Accepted
          </label>
          <label class="cb-label checked">
            <input type="checkbox" value="1" checked aria-label="Rejected">
            <span class="cb-swatch" style="background:{c_rej}"></span>Rejected
          </label>
          <label class="cb-label checked">
            <input type="checkbox" value="2" checked aria-label="Waitlisted">
            <span class="cb-swatch" style="background:{c_wl}"></span>Waitlisted
          </label>
          <label class="cb-label checked">
            <input type="checkbox" value="3" checked aria-label="Interview">
            <span class="cb-swatch" style="background:{c_int}"></span>Interview
          </label>
          <label class="cb-label checked">
            <input type="checkbox" value="4" checked aria-label="Other">
            <span class="cb-swatch" style="background:{c_oth}"></span>Other
          </label>
        </div>
      </div>

      <div class="filter-group" role="group" aria-labelledby="year-lbl">
        <span class="glabel" id="year-lbl">Cycle Year</span>
        <div class="year-bar">
          <button class="yr-shortcut active" id="yr-all" aria-pressed="true">All years</button>
          <button class="yr-shortcut" id="yr-last10" aria-pressed="false">Last 10</button>
          <button class="yr-shortcut" id="yr-last5" aria-pressed="false">Last 5</button>
          <button class="yr-shortcut" id="yr-last1" aria-pressed="false">Last year</button>
          <div class="year-sep" role="separator" aria-hidden="true"></div>
          <div id="year-pills" role="group" aria-label="Individual year toggles"></div>
        </div>
      </div>

      <button class="reset-btn" id="reset-btn" aria-label="Reset all filters">Reset</button>
    </div>
  </div>
</div>

<div class="stats-strip" id="stats-strip" aria-live="polite" aria-atomic="true" aria-label="Summary statistics">
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

<main id="main-content" tabindex="-1">
<div class="container">

<div id="spotlight" role="region" aria-label="School details" aria-live="polite">
  <div class="spotlight-inner">
    <div>
      <div class="spotlight-name" id="sp-name"></div>
      <div class="spotlight-meta" id="sp-meta"></div>
      <div class="spotlight-stats" id="sp-stats"></div>
    </div>
    <div id="spotlight-mini" aria-hidden="true"></div>
  </div>
</div>

<section class="section" aria-labelledby="timeline-title">
  <p class="section-title" id="timeline-title">Decision Timeline &mdash; all cycles overlaid (Nov&ndash;May)</p>
  <p class="chart-note" style="margin-bottom:6px">
    Each dot is one self-reported decision. Same-day notifications stack vertically by outcome. Hover for details.
  </p>
  <div id="timeline-chart" role="img" aria-label="Decision timeline scatter plot"></div>
</section>

<section class="section" aria-labelledby="dist-title">
  <p class="section-title" id="dist-title">Score Distributions by Outcome</p>
  <div class="chart-triple">
    <div>
      <div id="gpa-chart" role="img" aria-label="GPA distribution by outcome"></div>
      <p class="chart-note" id="gpa-note"></p>
    </div>
    <div>
      <div id="grev-chart" role="img" aria-label="GRE Verbal distribution by outcome"></div>
      <p class="chart-note" id="grev-note"></p>
    </div>
    <div>
      <div id="greq-chart" role="img" aria-label="GRE Quantitative distribution by outcome"></div>
      <p class="chart-note" id="greq-note"></p>
    </div>
  </div>
</section>

<section class="section" aria-labelledby="trends-title">
  <p class="section-title" id="trends-title">Reports per Cycle Year</p>
  <div id="year-chart" role="img" aria-label="Line chart of reports per year by outcome"></div>
</section>

<section class="section" aria-labelledby="table-title">
  <div class="table-header">
    <p class="section-title" style="margin:0" id="table-title">Data Records</p>
    <span class="record-count" id="rec-count" aria-live="polite" aria-atomic="true"></span>
  </div>
  <table id="data-table" aria-label="Admissions data records">
    <thead>
      <tr>
        <th data-ci="0" scope="col">School <span class="arr" aria-hidden="true">&#x21D5;</span></th>
        <th data-ci="1" scope="col">Cycle <span class="arr" aria-hidden="true">&#x21D5;</span></th>
        <th data-ci="3" scope="col">Decision <span class="arr" aria-hidden="true">&#x21D5;</span></th>
        <th data-ci="9" scope="col">Date <span class="arr" aria-hidden="true">&#x21D5;</span></th>
        <th data-ci="5" scope="col">GPA <span class="arr" aria-hidden="true">&#x21D5;</span></th>
        <th data-ci="6" scope="col">GRE-V <span class="arr" aria-hidden="true">&#x21D5;</span></th>
        <th data-ci="7" scope="col">GRE-Q <span class="arr" aria-hidden="true">&#x21D5;</span></th>
        <th data-ci="8" scope="col">Status <span class="arr" aria-hidden="true">&#x21D5;</span></th>
      </tr>
    </thead>
    <tbody id="tbl-body"></tbody>
  </table>
  <div class="pagination" id="pagination" role="navigation" aria-label="Table page navigation"></div>
</section>

</div>
</main>

<footer>
  <div class="container">
    Data from <a href="https://www.thegradcafe.com">GradCafe</a> (self-reported, unverified) and
    <a href="https://github.com/deedy/gradcafe_data">deedy/gradcafe_data</a> (2006&ndash;2015).
    Not affiliated with GradCafe. &nbsp;&middot;&nbsp;
    Source: <a href="https://github.com/nicolas-izquierdo/gradcafe-polisci">github.com/nicolas-izquierdo/gradcafe-polisci</a>
    &nbsp;&middot;&nbsp; Updated: {last_updated}
  </div>
</footer>

</div><!-- /outer container -->

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
let exactSchool = null;
let activeYears = new Set(FALL_YEARS);
let activeDecs = new Set([0,1,2,3,4]);
let sortCI = 9;
let sortDir = -1;
let page = 1;
const PG = 25;
const CFG = {{
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ['toImage','select2d','lasso2d','sendDataToCloud'],
}};

// ── Build year pills ─────────────────────────────────────────────────────
(function() {{
  const container = document.getElementById('year-pills');
  FALL_YEARS.forEach(y => {{
    const btn = document.createElement('button');
    btn.className = 'yr-pill active';
    btn.textContent = y;
    btn.dataset.year = y;
    btn.setAttribute('aria-pressed', 'true');
    container.append(btn);
  }});
}})();

// ── Utility ──────────────────────────────────────────────────────────────
function fmt(v) {{
  if (v === null || v === undefined || v === '') return '<span style="color:#ccc">—</span>';
  return v;
}}

function seasonLabel(r) {{
  return (r[2] ? 'F' : 'S') + String(r[1]).slice(2);
}}

function tooltip(r) {{
  const school = SCHOOLS[r[0]] || '';
  const dec = DEC_NAMES[r[3]];
  const cy = seasonLabel(r);
  let t = '<b>' + school + '</b><br>' + dec + ' · ' + cy;
  if (r[9]) t += '<br>' + r[9];
  if (r[5] !== null) t += '<br>GPA ' + r[5].toFixed(2);
  if (r[6] !== null) t += ' · GRE-V ' + r[6];
  if (r[7] !== null) t += ' · GRE-Q ' + r[7];
  if (r[8] < 2) t += '<br>' + STATUS_NAMES[r[8]];
  return t;
}}

// ── Filtering ────────────────────────────────────────────────────────────
function applyFilters() {{
  exactSchool = null;
  const selEl = document.getElementById('school-select');
  const selVal = selEl ? selEl.value : '';
  const isTop30Only = selVal === 'top30';
  const selectedIdx = (!isTop30Only && selVal !== '') ? +selVal : -1;

  if (selectedIdx >= 0) {{
    exactSchool = SCHOOLS[selectedIdx];
  }}

  F = DATA.filter(r => {{
    if (isTop30Only && !TOP30_IDXS.has(r[0])) return false;
    if (selectedIdx >= 0 && r[0] !== selectedIdx) return false;
    if (!activeYears.has(r[1])) return false;
    if (!activeDecs.has(r[3])) return false;
    return true;
  }});
  page = 1;
  renderAll();
}}

// ── Sync year shortcut button aria-pressed states ─────────────────────────
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

  [['yr-all', allActive], ['yr-last10', last10Active],
   ['yr-last5', last5Active], ['yr-last1', last1Active]]
  .forEach(([id, isActive]) => {{
    const el = document.getElementById(id);
    el.classList.toggle('active', isActive);
    el.setAttribute('aria-pressed', String(isActive));
  }});
}}

// ── Event wiring ──────────────────────────────────────────────────────────
document.getElementById('school-select').addEventListener('change', () => applyFilters());

document.getElementById('dec-checkboxes').addEventListener('change', e => {{
  const cb = e.target.closest('input[type="checkbox"]');
  if (!cb) return;
  activeDecs.clear();
  document.querySelectorAll('#dec-checkboxes input:checked').forEach(c => activeDecs.add(+c.value));
  // Sync pill visual state
  document.querySelectorAll('.cb-label').forEach(lbl => {{
    lbl.classList.toggle('checked', lbl.querySelector('input').checked);
  }});
  applyFilters();
}});

document.getElementById('year-pills').addEventListener('click', e => {{
  const pill = e.target.closest('.yr-pill');
  if (!pill) return;
  const y = +pill.dataset.year;
  const active = activeYears.has(y);
  if (active) {{ activeYears.delete(y); }}
  else {{ activeYears.add(y); }}
  pill.classList.toggle('active', !active);
  pill.setAttribute('aria-pressed', String(!active));
  syncShortcutBtns();
  applyFilters();
}});

document.getElementById('yr-all').addEventListener('click', () => {{
  activeYears = new Set(FALL_YEARS);
  document.querySelectorAll('.yr-pill').forEach(p => {{ p.classList.add('active'); p.setAttribute('aria-pressed','true'); }});
  syncShortcutBtns();
  applyFilters();
}});
document.getElementById('yr-last10').addEventListener('click', () => {{
  const maxY = FALL_YEARS.length ? Math.max(...FALL_YEARS) : 2026;
  activeYears = new Set(FALL_YEARS.filter(y => y >= maxY - 9));
  document.querySelectorAll('.yr-pill').forEach(p => {{
    const a = +p.dataset.year >= maxY - 9;
    p.classList.toggle('active', a);
    p.setAttribute('aria-pressed', String(a));
  }});
  syncShortcutBtns();
  applyFilters();
}});
document.getElementById('yr-last5').addEventListener('click', () => {{
  const maxY = FALL_YEARS.length ? Math.max(...FALL_YEARS) : 2026;
  activeYears = new Set(FALL_YEARS.filter(y => y >= maxY - 4));
  document.querySelectorAll('.yr-pill').forEach(p => {{
    const a = +p.dataset.year >= maxY - 4;
    p.classList.toggle('active', a);
    p.setAttribute('aria-pressed', String(a));
  }});
  syncShortcutBtns();
  applyFilters();
}});
document.getElementById('yr-last1').addEventListener('click', () => {{
  const maxY = FALL_YEARS.length ? Math.max(...FALL_YEARS) : 2026;
  activeYears = new Set(FALL_YEARS.filter(y => y === maxY));
  document.querySelectorAll('.yr-pill').forEach(p => {{
    const a = +p.dataset.year === maxY;
    p.classList.toggle('active', a);
    p.setAttribute('aria-pressed', String(a));
  }});
  syncShortcutBtns();
  applyFilters();
}});

document.getElementById('reset-btn').addEventListener('click', () => {{
  document.getElementById('school-select').value = '';
  document.querySelectorAll('#dec-checkboxes input').forEach(cb => cb.checked = true);
  document.querySelectorAll('.cb-label').forEach(lbl => lbl.classList.add('checked'));
  activeYears = new Set(FALL_YEARS);
  document.querySelectorAll('.yr-pill').forEach(p => {{ p.classList.add('active'); p.setAttribute('aria-pressed','true'); }});
  activeDecs = new Set([0,1,2,3,4]);
  syncShortcutBtns();
  applyFilters();
}});

// ── Stats strip ───────────────────────────────────────────────────────────
function updateStats() {{
  const tot = F.length;
  const acc = F.filter(r => r[3] === 0).length;
  const progs = new Set(F.map(r => r[0])).size;
  document.getElementById('stat-total').textContent = tot.toLocaleString();
  document.getElementById('stat-pct').textContent = tot ? (100 * acc / tot).toFixed(1) + '%' : '—';
  document.getElementById('stat-programs').textContent = progs;
}}

// ── School spotlight ──────────────────────────────────────────────────────
function renderSpotlight() {{
  const el = document.getElementById('spotlight');
  const selEl = document.getElementById('school-select');
  const selVal = selEl ? selEl.value : '';

  if (!selVal || selVal === 'top30') {{ el.style.display = 'none'; return; }}

  el.style.display = 'block';
  const sidx = +selVal;
  const sname = SCHOOLS[sidx] || '';
  const ss = SCHOOL_STATS[sname];

  document.getElementById('sp-name').textContent = sname;

  if (ss) {{
    const total = ss.total;
    const acc = ss.by_dec[0], rej = ss.by_dec[1], wl = ss.by_dec[2];
    const accRate = total ? (100 * acc / total).toFixed(1) : '—';
    document.getElementById('sp-meta').textContent =
      total.toLocaleString() + ' total reports · ' + accRate + '% reported accepted';

    let h = '';
    if (ss.med_gpa_acc !== null)
      h += '<div class="sstat"><div class="sstat-val">' + ss.med_gpa_acc.toFixed(2) + '</div><div class="sstat-label">Median GPA (accepted)</div></div>';
    if (ss.med_grev_acc !== null)
      h += '<div class="sstat"><div class="sstat-val">' + ss.med_grev_acc + '</div><div class="sstat-label">Median GRE-V (accepted)</div></div>';
    if (ss.med_greq_acc !== null)
      h += '<div class="sstat"><div class="sstat-val">' + ss.med_greq_acc + '</div><div class="sstat-label">Median GRE-Q (accepted)</div></div>';
    h += '<div class="sstat"><div class="sstat-val" style="color:' + COLORS.Accepted + '">' + acc + '</div><div class="sstat-label">Accepted</div></div>';
    h += '<div class="sstat"><div class="sstat-val" style="color:' + COLORS.Waitlisted + '">' + wl + '</div><div class="sstat-label">Waitlisted</div></div>';
    h += '<div class="sstat"><div class="sstat-val" style="color:' + COLORS.Rejected + '">' + rej + '</div><div class="sstat-label">Rejected</div></div>';
    document.getElementById('sp-stats').innerHTML = h;

    const byYear = ss.by_year || {{}};
    const years = Object.keys(byYear).map(Number).sort((a, b) => a - b);
    const miniTraces = [0, 1, 2].map(d => ({{
      type: 'bar', name: DEC_NAMES[d],
      x: years,
      y: years.map(y => (byYear[y] || [0,0,0,0,0])[d]),
      marker: {{ color: DEC_COLORS[d], opacity: 0.85 }},
      showlegend: false,
    }}));
    Plotly.react('spotlight-mini', miniTraces, {{
      barmode: 'stack',
      paper_bgcolor: '#f8f9fc', plot_bgcolor: '#f8f9fc',
      margin: {{ l: 30, r: 8, t: 6, b: 28 }},
      xaxis: {{ tickfont: {{ size: 12, color: '#777' }}, tickformat: 'd', gridcolor: '#e4e4e4', zeroline: false }},
      yaxis: {{ tickfont: {{ size: 12, color: '#777' }}, gridcolor: '#e4e4e4', zeroline: false }},
      showlegend: false,
    }}, {{ ...CFG, staticPlot: true }});
  }} else {{
    document.getElementById('sp-meta').textContent = 'No aggregate stats available for this school';
    document.getElementById('sp-stats').innerHTML = '';
    Plotly.purge('spotlight-mini');
  }}
}}

// ── Timeline ──────────────────────────────────────────────────────────────
function renderTimeline() {{
  const isSingle = exactSchool !== null;
  const dotSize    = isSingle ? 9 : 4;
  const dotOpacity = isSingle ? 0.9 : 0.65;

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
    const x = [], y = [], text = [];
    F.forEach((r, ri) => {{
      if (r[3] !== d || r[4] === null || stackY[ri] === null) return;
      x.push(r[4]);
      y.push(stackY[ri]);
      text.push(tooltip(r));
    }});
    return {{
      type: traceType, mode: 'markers', name: DEC_NAMES[d],
      x, y, text,
      hovertemplate: '%{{text}}<extra></extra>',
      marker: {{ color: DEC_COLORS[d], size: dotSize, opacity: dotOpacity }},
      visible: x.length > 0 ? true : 'legendonly',
    }};
  }});

  const yPad = isSingle ? Math.max(2, Math.ceil(maxStack * 0.25)) : 4;
  const tickvals = [0, 30, 61, 92, 120, 151, 181];
  const ticktext = ['Nov', 'Dec', 'Jan', 'Feb', 'Mar', 'Apr', 'May'];
  Plotly.react('timeline-chart', traces, {{
    paper_bgcolor: '#fff', plot_bgcolor: '#fff',
    margin: {{ l: 8, r: 16, t: 8, b: 44 }},
    xaxis: {{
      tickvals, ticktext,
      tickfont: {{ size: 13, color: '#777' }},
      gridcolor: '#ebebeb', gridwidth: 1,
      zeroline: false, showline: false,
    }},
    yaxis: {{ visible: false, range: [-1, maxStack + yPad], zeroline: false }},
    legend: {{ orientation: 'h', x: 0, y: -0.14, font: {{ size: 13, color: '#555' }} }},
    hovermode: 'closest',
  }}, CFG);
}}

// ── Score distributions ───────────────────────────────────────────────────
function renderScores() {{
  const gpas  = [[], [], [], [], []];
  const grevs = [[], [], [], [], []];
  const greqs = [[], [], [], [], []];

  F.forEach(r => {{
    if (r[5] !== null && r[5] > 0 && r[5] <= 4.5)    gpas[r[3]].push(r[5]);
    if (r[6] !== null && r[6] >= 130 && r[6] <= 170) grevs[r[3]].push(r[6]);
    if (r[7] !== null && r[7] >= 130 && r[7] <= 170) greqs[r[3]].push(r[7]);
  }});

  const showDecs = [0, 1, 2];

  function makeTraces(byDec) {{
    return showDecs.filter(d => byDec[d].length >= 3).map(d => ({{
      type: 'box', name: DEC_NAMES[d], y: byDec[d],
      boxpoints: 'outliers', jitter: 0.3, pointpos: 0,
      marker: {{ color: DEC_COLORS[d], size: 3, opacity: 0.35 }},
      line: {{ color: DEC_COLORS[d], width: 2 }},
      fillcolor: DEC_COLORS[d] + '25',
      whiskerwidth: 0.5,
    }}));
  }}

  const baseLayout = {{
    paper_bgcolor: '#fff', plot_bgcolor: '#fafafa',
    margin: {{ l: 46, r: 12, t: 32, b: 36 }},
    xaxis: {{ tickfont: {{ size: 13, color: '#888' }}, gridcolor: '#ebebeb', zeroline: false, showline: false }},
    yaxis: {{ tickfont: {{ size: 12, color: '#888' }}, gridcolor: '#ebebeb', zeroline: false }},
    showlegend: false,
    shapes: [{{ type: 'rect', xref: 'paper', yref: 'paper', x0: 0, y0: 0, x1: 1, y1: 1,
               line: {{ color: '#e4e4e4', width: 1 }} }}],
  }};

  const gpaCount  = gpas.flat().length;
  const grevCount = grevs.flat().length;
  const greqCount = greqs.flat().length;

  document.getElementById('gpa-note').textContent =
    gpaCount.toLocaleString() + ' records with GPA data';
  document.getElementById('grev-note').textContent =
    grevCount.toLocaleString() + ' records · new scale (130–170) only';
  document.getElementById('greq-note').textContent =
    greqCount === 0
      ? 'No GRE-Q data in current selection'
      : greqCount.toLocaleString() + ' records · new scale only' +
        (greqCount < 50 ? ' (very limited — rarely self-reported)' : '');

  const emptyBox = [{{ type: 'box', y: [], name: '' }}];

  Plotly.react('gpa-chart',
    makeTraces(gpas).length ? makeTraces(gpas) : emptyBox,
    {{ ...baseLayout,
       title: {{ text: 'GPA', font: {{ size: 13, color: '#999' }}, x: 0.04, xanchor: 'left' }},
       yaxis: {{ ...baseLayout.yaxis, range: [2.5, 4.22] }} }},
    CFG);

  Plotly.react('grev-chart',
    makeTraces(grevs).length ? makeTraces(grevs) : emptyBox,
    {{ ...baseLayout,
       title: {{ text: 'GRE Verbal', font: {{ size: 13, color: '#999' }}, x: 0.04, xanchor: 'left' }},
       yaxis: {{ ...baseLayout.yaxis, range: [138, 172] }} }},
    CFG);

  Plotly.react('greq-chart',
    makeTraces(greqs).length ? makeTraces(greqs) : emptyBox,
    {{ ...baseLayout,
       title: {{ text: 'GRE Quantitative', font: {{ size: 13, color: '#999' }}, x: 0.04, xanchor: 'left' }},
       yaxis: {{ ...baseLayout.yaxis, range: [138, 172] }} }},
    CFG);
}}

// ── Year trends ───────────────────────────────────────────────────────────
function renderYearTrends() {{
  const hasSchoolFilter = exactSchool !== null;
  const hasDecFilter = activeDecs.size < 5;
  let ys;
  if (hasSchoolFilter || hasDecFilter) {{
    ys = {{}};
    F.forEach(r => {{
      const y = String(r[1]);
      if (!ys[y]) ys[y] = [0, 0, 0, 0, 0, 0];
      ys[y][r[3]]++;
      ys[y][5]++;
    }});
  }} else {{
    ys = YEAR_STATS;
  }}

  const filtYears = Object.keys(ys).map(Number)
    .filter(y => activeYears.has(y))
    .sort((a, b) => a - b);

  const traces = [0, 1, 2].map(d => ({{
    type: 'scatter', mode: 'lines+markers', name: DEC_NAMES[d],
    x: filtYears,
    y: filtYears.map(y => (ys[String(y)] || [0,0,0,0,0])[d]),
    line: {{ color: DEC_COLORS[d], width: 2.5 }},
    marker: {{ color: DEC_COLORS[d], size: 6, line: {{ color: '#fff', width: 1.5 }} }},
    fill: 'tozeroy',
    fillcolor: DEC_COLORS[d] + '12',
  }}));

  Plotly.react('year-chart', traces, {{
    paper_bgcolor: '#fff', plot_bgcolor: '#fafafa',
    margin: {{ l: 46, r: 16, t: 8, b: 40 }},
    xaxis: {{ tickfont: {{ size: 13, color: '#888' }}, tickformat: 'd', gridcolor: '#ebebeb', zeroline: false }},
    yaxis: {{ tickfont: {{ size: 12, color: '#888' }}, gridcolor: '#ebebeb', zeroline: false }},
    legend: {{ orientation: 'h', x: 0, y: -0.22, font: {{ size: 13, color: '#555' }} }},
    hovermode: 'x unified',
    hoverlabel: {{ bgcolor: '#fff', bordercolor: '#e4e4e4', font: {{ size: 13 }} }},
  }}, CFG);
}}

// ── Table ─────────────────────────────────────────────────────────────────
const THS = document.querySelectorAll('#data-table th[data-ci]');
THS.forEach(th => {{
  th.addEventListener('click', () => {{
    const ci = +th.dataset.ci;
    if (sortCI === ci) {{ sortDir *= -1; }} else {{ sortCI = ci; sortDir = -1; }}
    THS.forEach(t => {{
      t.classList.remove('sorted');
      t.querySelector('.arr').innerHTML = '&#x21D5;';
      t.removeAttribute('aria-sort');
    }});
    th.classList.add('sorted');
    th.querySelector('.arr').innerHTML = sortDir === 1 ? '&#x2191;' : '&#x2193;';
    th.setAttribute('aria-sort', sortDir === 1 ? 'ascending' : 'descending');
    renderTable();
  }});
}});

function sortedF() {{
  const ci = sortCI, sd = sortDir;
  return F.slice().sort((a, b) => {{
    let av = a[ci], bv = b[ci];
    const nil = sd === 1 ? '￿' : '';
    if (av === null || av === undefined || av === '') av = nil;
    if (bv === null || bv === undefined || bv === '') bv = nil;
    if (av < bv) return -sd;
    if (av > bv) return sd;
    return 0;
  }});
}}

function renderTable() {{
  const data = sortedF();
  const total = data.length;
  const totalPages = Math.max(1, Math.ceil(total / PG));
  if (page > totalPages) page = totalPages;
  const slice = data.slice((page - 1) * PG, page * PG);

  document.getElementById('rec-count').textContent = total.toLocaleString() + ' records';

  const tbody = document.getElementById('tbl-body');
  tbody.innerHTML = slice.map(r => {{
    const dot = `<span class="dot" style="background:${{DEC_COLORS[r[3]]}}"></span>`;
    const gpa = r[5] !== null ? r[5].toFixed(2) : '';
    const cy = seasonLabel(r);
    return `<tr>
      <td>${{SCHOOLS[r[0]] || ''}}</td>
      <td>${{cy}}</td>
      <td>${{dot}}${{DEC_NAMES[r[3]]}}</td>
      <td>${{r[9] || ''}}</td>
      <td>${{fmt(gpa)}}</td>
      <td>${{fmt(r[6])}}</td>
      <td>${{fmt(r[7])}}</td>
      <td>${{r[8] < 2 ? STATUS_NAMES[r[8]] : ''}}</td>
    </tr>`;
  }}).join('');

  renderPagination(total, totalPages);
}}

function renderPagination(total, totalPages) {{
  const el = document.getElementById('pagination');
  if (totalPages <= 1) {{ el.innerHTML = ''; return; }}
  let h = `<button class="pbtn" id="pp" ${{page === 1 ? 'disabled' : ''}} aria-label="Previous page">&#8592;</button>`;
  const pages = [];
  if (totalPages <= 7) {{
    for (let i = 1; i <= totalPages; i++) pages.push(i);
  }} else {{
    pages.push(1);
    if (page > 3) pages.push('…');
    for (let i = Math.max(2, page - 1); i <= Math.min(totalPages - 1, page + 1); i++) pages.push(i);
    if (page < totalPages - 2) pages.push('…');
    pages.push(totalPages);
  }}
  pages.forEach(p => {{
    if (p === '…') h += `<span class="pinfo" aria-hidden="true">…</span>`;
    else h += `<button class="pbtn${{p === page ? ' active' : ''}}" data-p="${{p}}" aria-label="Page ${{p}}" aria-current="${{p === page ? 'page' : 'false'}}">${{p}}</button>`;
  }});
  h += `<button class="pbtn" id="np" ${{page === totalPages ? 'disabled' : ''}} aria-label="Next page">&#8594;</button>`;
  h += `<span class="pinfo">Page ${{page}} of ${{totalPages}}</span>`;
  el.innerHTML = h;
  el.querySelectorAll('.pbtn[data-p]').forEach(b =>
    b.addEventListener('click', () => {{ page = +b.dataset.p; renderTable(); }}));
  const pp = el.querySelector('#pp');
  if (pp) pp.addEventListener('click', () => {{ page--; renderTable(); }});
  const np = el.querySelector('#np');
  if (np) np.addEventListener('click', () => {{ page++; renderTable(); }});
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

    all_deedy = load_deedy_baseline()
    deedy_rows = [r for r in all_deedy if r.get("degree") == "PhD"]
    print(f"  Deedy PhD rows: {len(deedy_rows)} (of {len(all_deedy)} total)")

    if test_seasons:
        seasons = test_seasons
    elif recent_only:
        seasons = ALL_SEASONS[-2:]
    else:
        seasons = ALL_SEASONS

    existing = load_existing_csv() if recent_only else []

    print(f"Scraping seasons: {seasons}")
    scraped = scrape_gradcafe(seasons)

    if recent_only:
        existing_deedy = [r for r in existing if r.get("source") == "deedy_2015"]
        existing_scraped = [r for r in existing if r.get("source") != "deedy_2015"]
        all_rows = merge_and_dedup(existing_deedy + deedy_rows + existing_scraped, scraped)
    else:
        all_rows = merge_and_dedup(deedy_rows, scraped)

    all_rows.sort(key=lambda r: r.get("date_posted") or "", reverse=True)
    save_csv(all_rows)

    today = date.today().isoformat()
    LAST_UPDATED_PATH.write_text(today)

    print("Generating docs/index.html ...")
    html = generate_html(all_rows, today)
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"HTML written ({len(html):,} bytes) — {len(all_rows):,} records")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--recent", action="store_true")
    parser.add_argument("--test", nargs="+", metavar="SEASON")
    parser.add_argument("--html-only", action="store_true")
    args = parser.parse_args()
    run(recent_only=args.recent, test_seasons=args.test, html_only=args.html_only)
