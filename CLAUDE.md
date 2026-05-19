# gradcafe-polisci — Project Context for Claude

## What we're building

A public GitHub Pages website at https://nicolas-izquierdo.github.io/gradcafe-polisci/ that aggregates and visualizes self-reported Political Science PhD admissions results from GradCafe, covering 2006–present.

**Audience:** Future PhD applicants in Political Science who want to understand admission timelines, acceptance patterns by school, and historical trends.

## Current status (as of 2026-05-19)

### Completed and pushed to GitHub
- Full project structure, all scripts committed
- Test run completed: **7,682 records** (F24+F25+F26 + Deedy baseline)
- docs/index.html built and pushed (self-contained site)
- Waiting for user confirmation before running full historical scrape (22 seasons)

## Critical: How scraping works

GradCafe uses **Cloudflare Turnstile**. Everything except one approach gets 403:
- `requests` — 403
- `cloudscraper` — 403
- `playwright` (headless) — 403
- **`undetected-chromedriver` non-headless** — **WORKS**

The scraper (`scrape_gradcafe()` in `scraper.py`) uses `undetected_chromedriver` with `use_subprocess=True` and NO `--headless` flag. This opens a visible Chrome window locally. For CI (Linux), the GitHub Actions workflow uses `xvfb-run` (virtual display) so the browser thinks it's non-headless.

The scraper:
1. Launches Chrome, navigates directly to the first survey page to warm up
2. Pages are cached to `data/raw/{degree}{season}{page:03d}.html`
3. On second run, cached pages are read from disk (no browser needed)

## File layout

```
gradcafe-polisci/
├── .github/workflows/update.yml   # Sunday 07:00 UTC, Jan–Apr + xvfb-run
├── docs/index.html                # Self-contained GitHub Pages site
├── data/
│   ├── raw/                       # Gitignored — cached HTML pages
│   ├── gradcafe_polisci.csv       # Committed — 7,682 rows (test run)
│   ├── school_map.csv             # 60 USNWR patterns
│   └── last_updated.txt
├── scraper.py                     # load_deedy_baseline() + scrape_gradcafe()
├── clean.py                       # clean_school(), normalize_decision/degree()
├── build.py                       # run(recent_only, test_seasons) pipeline
├── requirements.txt
└── README.md
```

## Unified data schema

`rowid, school_raw, school_clean, program_raw, degree, season_code, season_year, decision, decision_class, date_posted, gpa, gre_v, gre_q, gre_w, applicant_status, source`

- `source`: `'deedy_2015'` or `'gradcafe_scrape'`
- `season_code`: `F25`, `S25`, etc. (letter + 2-digit year)
- `decision_class`: exactly one of `Accepted / Rejected / Waitlisted / Interview / Other`

## Deedy dataset

URL: `https://raw.githubusercontent.com/deedy/gradcafe_data/master/all_uisc_clean.csv`
Format: **headerless CSV** (75MB). Column indices in `_DC` dict in scraper.py.
- Col 2: school, 3: major, 4: degree, 5: season (S16/F16 format), 6: decision
- Col 10: GPA, 11: GRE-V, 12: GRE-Q, 13: GRE-AW
- Col 16: applicant status, 17: date_added (tuple format "(5, 11, 2015)")

## GradCafe HTML structure (F25 season, confirmed)

Main row (5 cells): [school, "Program Degree", date_posted, "Decision on MonDD", "Total comments"]
Detail row 1 (1 cell, pipe-separated): "Decision text|Season Year|Status|GPA X.X|GRE V NNN|GRE AW N.N"
Detail rows 2+ (1 cell): optional notes/comments — skip

## How to run

```bash
pip install -r requirements.txt

# Test run (3 seasons, browser will open)
python build.py --test F24 F25 F26

# Full historical scrape (22 seasons — takes ~30-90 min)
python build.py

# Re-scrape 2 most recent seasons and merge into existing CSV
python build.py --recent
```

## Next step (pending user confirmation)

Run `python build.py` for the full historical scrape of all 22 seasons (S16–F26).
This will open a Chrome window and scrape ~2-3 pages/minute per season.
Estimated time: 45–90 minutes for ~20,000+ records.
