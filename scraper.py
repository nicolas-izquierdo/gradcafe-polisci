"""Scraper and data loader for GradCafe Political Science admissions data."""

import os
import re
import time
import random
import hashlib
import csv
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import clean as c

RAW_DIR = Path("data/raw")
RAW_DIR.mkdir(parents=True, exist_ok=True)

DEEDY_URL = "https://raw.githubusercontent.com/deedy/gradcafe_data/master/all_uisc_clean.csv"

# Columns (headerless): idx, idx2, college, major, degree, season, decision,
# notif_method, date_added_tuple, date_added_unix, gpa, gre_v, gre_q, gre_aw,
# is_new_gre, ?, applicant_status, date_posted_tuple, date_posted_unix, notes
_DC = {
    "college": 2, "major": 3, "degree": 4, "season": 5, "result": 6,
    "gpa": 10, "gre_v": 11, "gre_q": 12, "gre_aw": 13,
    "status": 16, "date_add": 17,
}

GRADCAFE_URL = (
    "https://www.thegradcafe.com/survey/"
    "?q=&sort=newest&institution=&program=Political+Science"
    "&degree={degree}&season={season}&decision=&page={page}"
)

UNIFIED_FIELDS = [
    "rowid", "school_raw", "school_clean", "program_raw", "degree",
    "season_code", "season_year", "decision", "decision_class",
    "date_posted", "gpa", "gre_v", "gre_q", "gre_w",
    "applicant_status", "source",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _season_year(code: str) -> int:
    """F25 -> 2025, S25 -> 2025."""
    yy = int(code[1:])
    return 2000 + yy if yy < 100 else yy


def _make_rowid(parts: list) -> str:
    key = "|".join(str(p) for p in parts)
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _safe_float(val) -> float | None:
    try:
        f = float(str(val).strip())
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# STEP 2 — Deedy baseline (2006–2015)
# ---------------------------------------------------------------------------

def _parse_deedy_date(val: str) -> str:
    """Convert Deedy tuple date '(5, 11, 2015)' -> '2015-11-05', or unix ts -> date."""
    if not val or str(val).strip() in ("", "nan"):
        return ""
    val = str(val).strip()
    m = re.match(r'\((\d+),\s*(\d+),\s*(\d+)\)', val)
    if m:
        d, mo, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(yr, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    try:
        ts = float(val)
        if ts > 1e8:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        pass
    return val


def load_deedy_baseline() -> list[dict]:
    """Download and filter the Deedy dataset (headerless) to Political Science rows."""
    import io
    print("Loading Deedy baseline dataset ...")
    resp = requests.get(DEEDY_URL, timeout=120)
    resp.raise_for_status()

    dc = _DC
    reader = csv.reader(io.StringIO(resp.text))
    rows = []
    for raw in reader:
        if len(raw) <= dc["major"]:
            continue
        major = raw[dc["major"]] if len(raw) > dc["major"] else ""
        if "political science" not in major.lower():
            continue

        def col(key, default=""):
            idx = dc.get(key)
            if idx is None or idx >= len(raw):
                return default
            return raw[idx]

        school_raw = col("college").strip()
        school_clean, _ = c.clean_school(school_raw)
        program_raw = major.strip()

        degree_raw = col("degree")
        degree = c.normalize_degree(degree_raw)

        decision_raw = col("result")
        decision_class = c.normalize_decision(decision_raw)

        season_raw = col("season").strip()
        season_code = ""
        season_year = None
        m = re.match(r'([FS])(\d{2,4})$', season_raw, re.IGNORECASE)
        if m:
            prefix = m.group(1).upper()
            yy = int(m.group(2)) % 100
            season_code = f"{prefix}{yy:02d}"
            season_year = 2000 + yy
        else:
            m2 = re.match(r'([FS])(\d{4})', season_raw, re.IGNORECASE)
            if m2:
                prefix = m2.group(1).upper()
                yr = int(m2.group(2))
                season_code = f"{prefix}{yr % 100:02d}"
                season_year = yr

        date_posted = _parse_deedy_date(col("date_add"))
        gpa = _safe_float(col("gpa"))
        gre_v = _safe_int(col("gre_v"))
        gre_q = _safe_int(col("gre_q"))
        gre_w = _safe_float(col("gre_aw"))

        rowid = _make_rowid([school_raw, season_code, date_posted, decision_raw])

        rows.append({
            "rowid": rowid,
            "school_raw": school_raw,
            "school_clean": school_clean,
            "program_raw": program_raw,
            "degree": degree,
            "season_code": season_code,
            "season_year": season_year,
            "decision": decision_raw,
            "decision_class": decision_class,
            "date_posted": date_posted,
            "gpa": gpa,
            "gre_v": gre_v,
            "gre_q": gre_q,
            "gre_w": gre_w,
            "applicant_status": col("status").strip(),
            "source": "deedy_2015",
        })

    print(f"  -> {len(rows)} political science rows from Deedy baseline")
    return rows


# ---------------------------------------------------------------------------
# STEP 3 — GradCafe scraper (2016–present)
# ---------------------------------------------------------------------------

# GradCafe page structure (confirmed from live HTML):
# Main row (5 cells): [school, "Program Degree", date_posted, "Decision on MonDD", "Total comments"]
# Detail row 1 (1 cell): "Decision on MonDD|Season Year|Status|GPA X.XX|GRE V NNN|GRE AW N.N"
# Detail row 2+ (1 cell): optional notes/comments


def _parse_date_gradcafe(raw: str) -> str:
    """Parse GradCafe date strings like 'Mar 31, 2026' -> '2026-03-31'."""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def _parse_season_label(label: str) -> str:
    """'Fall 2025' -> 'F25', 'Spring 2026' -> 'S26'."""
    label = label.strip()
    m = re.match(r'(Fall|Spring)\s+(\d{4})', label, re.IGNORECASE)
    if m:
        prefix = "F" if m.group(1).lower() == "fall" else "S"
        yr = int(m.group(2)) % 100
        return f"{prefix}{yr:02d}"
    return ""


def _parse_decision_label(raw: str) -> str:
    """'Rejected on Mar 30' -> 'Rejected', 'Accepted on Feb 18' -> 'Accepted'."""
    raw = raw.strip().lower()
    if raw.startswith("accepted") or "accept" in raw:
        return "Accepted"
    if raw.startswith("rejected") or "reject" in raw or "denied" in raw:
        return "Rejected"
    if "waitlist" in raw or "wait list" in raw:
        return "Waitlisted"
    if "interview" in raw:
        return "Interview"
    return "Other"


def _parse_page(html: str, degree_label: str, season: str) -> list[dict]:
    """Parse one GradCafe survey page into unified row dicts."""
    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table", class_=lambda cls: cls and "tw-min-w-full" in cls)
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    all_rows = tbody.find_all("tr")
    results = []
    i = 0
    while i < len(all_rows):
        cells = all_rows[i].find_all("td")

        if len(cells) < 5:
            i += 1
            continue

        school_raw = cells[0].get_text(separator=" ", strip=True)
        # cell[1] = "Political SciencePhD" or "Political Science PhD" — degree embedded
        prog_deg = cells[1].get_text(separator=" ", strip=True)
        date_posted = _parse_date_gradcafe(cells[2].get_text(strip=True))
        decision_raw = cells[3].get_text(strip=True)  # "Rejected on Mar 30"
        decision_class = _parse_decision_label(decision_raw)

        # Extract degree from program string
        degree_from_prog = c.normalize_degree(prog_deg)
        if degree_from_prog == "Other":
            degree_from_prog = c.normalize_degree(degree_label)
        program_raw = re.sub(r'\b(PhD|Ph\.D|MA|M\.A|MS|M\.S)\b', '', prog_deg, flags=re.IGNORECASE).strip()

        school_clean, _ = c.clean_school(school_raw)
        season_year = _season_year(season)

        gpa = gre_v = gre_q = gre_w = None
        applicant_status = ""
        detail_season = season  # default to requested season

        # Consume all following 1-cell detail rows
        i += 1
        while i < len(all_rows):
            next_cells = all_rows[i].find_all("td")
            if len(next_cells) != 1:
                break
            detail_parts = [p.strip() for p in next_cells[0].get_text("|", strip=True).split("|")]

            # First detail row has the structured data
            if not applicant_status:
                for part in detail_parts:
                    # Season
                    if re.match(r'(Fall|Spring)\s+\d{4}', part, re.IGNORECASE):
                        sc = _parse_season_label(part)
                        if sc:
                            detail_season = sc
                            season_year = _season_year(sc)
                    # Applicant status
                    elif part.lower() in ("american", "international", "other"):
                        applicant_status = part
                    # GPA
                    elif part.startswith("GPA"):
                        gpa = _safe_float(part.replace("GPA", "").strip())
                    # GRE V
                    elif re.match(r'GRE\s*V', part, re.IGNORECASE):
                        gre_v = _safe_int(re.sub(r'GRE\s*V', '', part, flags=re.IGNORECASE).strip())
                    # GRE Q
                    elif re.match(r'GRE\s*Q', part, re.IGNORECASE):
                        gre_q = _safe_int(re.sub(r'GRE\s*Q', '', part, flags=re.IGNORECASE).strip())
                    # GRE AW / Writing
                    elif re.match(r'GRE\s*(AW|W)', part, re.IGNORECASE):
                        gre_w = _safe_float(re.sub(r'GRE\s*(AW|W)', '', part, flags=re.IGNORECASE).strip())
            i += 1

        rowid = _make_rowid([school_raw, detail_season, date_posted, decision_raw])

        results.append({
            "rowid": rowid,
            "school_raw": school_raw,
            "school_clean": school_clean,
            "program_raw": program_raw,
            "degree": degree_from_prog,
            "season_code": detail_season,
            "season_year": season_year,
            "decision": decision_raw,
            "decision_class": decision_class,
            "date_posted": date_posted,
            "gpa": gpa,
            "gre_v": gre_v,
            "gre_q": gre_q,
            "gre_w": gre_w,
            "applicant_status": applicant_status,
            "source": "gradcafe_scrape",
        })

    return results


def _launch_driver():
    """Launch undetected Chrome. Non-headless to pass Cloudflare."""
    import undetected_chromedriver as uc
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-blink-features=AutomationControlled")
    # On Linux CI use Xvfb virtual display — browser appears "non-headless"
    driver = uc.Chrome(options=options, use_subprocess=True)
    return driver


def _wait_for_survey(driver, url: str, timeout: int = 20) -> str | None:
    """Navigate to url and wait until the survey table appears (or timeout)."""
    driver.get(url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        html = driver.page_source
        if "tw-min-w-full" in html:
            return html
        if "Just a moment" not in html and "cf-browser-verification" not in html:
            # Not a CF challenge but also no table — empty results page
            if "No results" in html or len(html) < 5000:
                return html
        time.sleep(1.5)
    return None


def _get_page_with_driver(
    driver, degree: str, season: str, page: int
) -> str | None:
    """Fetch a page using an existing browser driver, with cache."""
    cache_path = RAW_DIR / f"{degree}{season}{page:03d}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    url = GRADCAFE_URL.format(degree=degree, season=season, page=page)
    time.sleep(random.uniform(0.8, 1.5))
    html = _wait_for_survey(driver, url, timeout=18)
    if html and "tw-min-w-full" in html:
        cache_path.write_text(html, encoding="utf-8")
        return html
    return None


def scrape_gradcafe(seasons: list[str]) -> list[dict]:
    """Scrape GradCafe for given season codes using a real Chrome browser."""
    degrees = ["PhD"]
    all_rows: list[dict] = []

    # Check if all pages are already cached to avoid launching browser
    needs_live = any(
        not (RAW_DIR / f"{deg}{season}001.html").exists()
        for season in seasons
        for deg in degrees
    )

    driver = None
    if needs_live:
        print("  Launching browser (Chrome window will open) ...")
        try:
            driver = _launch_driver()
            # Navigate directly to the survey to warm up the session
            warmup_url = GRADCAFE_URL.format(degree="PhD", season=seasons[0], page=1)
            html = _wait_for_survey(driver, warmup_url, timeout=25)
            if html and "tw-min-w-full" in html:
                # Cache the warmup page
                cache_path = RAW_DIR / f"PhD{seasons[0]}001.html"
                cache_path.write_text(html, encoding="utf-8")
                print(f"  Warmup OK — {seasons[0]}/PhD/page1 cached")
            else:
                print("  Warmup: CF challenge not resolved — scraping may be limited")
        except Exception as e:
            print(f"  Could not launch browser: {e}")
            print("  Install Chrome + undetected-chromedriver and try again.")
            driver = None

    try:
        for season in seasons:
            for degree in degrees:
                print(f"  Scraping {degree} / {season} ...")
                season_rows = []
                for page in range(1, 201):
                    cache_path = RAW_DIR / f"{degree}{season}{page:03d}.html"
                    if cache_path.exists():
                        html = cache_path.read_text(encoding="utf-8")
                    elif driver is not None:
                        html = _get_page_with_driver(driver, degree, season, page)
                    else:
                        break

                    if html is None:
                        break

                    page_rows = _parse_page(html, degree, season)
                    if len(page_rows) < 5:
                        season_rows.extend(page_rows)
                        break
                    season_rows.extend(page_rows)

                print(f"    -> {len(season_rows)} rows for {degree}/{season}")
                all_rows.extend(season_rows)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    return all_rows
