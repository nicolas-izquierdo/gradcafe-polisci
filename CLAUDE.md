# gradcafe-polisci — Project Context for Claude

## What we're building

A public GitHub Pages website at https://nicolas-izquierdo.github.io/gradcafe-polisci/ that aggregates and visualizes self-reported Political Science PhD admissions results from GradCafe, covering 2006–present.

**Audience:** Future PhD applicants in Political Science who want to understand admission timelines, acceptance patterns by school, and historical trends.

## Current status (as of 2026-05-19)

### Completed
- Full project structure created
- `clean.py` — school name normalization (60 USNWR-ranked programs via regex) and decision/degree normalization
- `scraper.py` — Deedy baseline loader (`load_deedy_baseline()`) and GradCafe scraper (`scrape_gradcafe()`)
- `build.py` — pipeline orchestrator with `--recent` and `--test` flags
- `docs/index.html` — single-file GitHub Pages site with Plotly.js timeline, volume chart, and sortable paginated table
- `.github/workflows/update.yml` — auto-update workflow (Sundays Jan–Apr)
- `README.md`, `requirements.txt`, `.gitignore`

### Test run results
- Deedy baseline: **10,448 political science rows** downloaded, **5,930 after dedup** (many entries share school+season+date+decision keys)
- GradCafe scrape (F24, F25, F26): **0 rows** — GradCafe returns 403 even on homepage (Cloudflare protection)

### Blocking issue
GradCafe is behind Cloudflare. `requests` alone gets a 403. We need `cloudscraper` (pip package) which handles the JS challenge. Added to `requirements.txt` and `scraper.py` — **not yet tested**.

## File layout

```
gradcafe-polisci/
├── .github/workflows/update.yml   # Sunday 07:00 UTC, Jan–Apr only
├── docs/index.html                # Self-contained GitHub Pages site (all data embedded as JSON)
├── data/
│   ├── raw/                       # Ignored by git — cached HTML pages
│   ├── gradcafe_polisci.csv       # Committed — unified dataset
│   ├── school_map.csv             # 60 USNWR school patterns
│   └── last_updated.txt
├── scraper.py                     # load_deedy_baseline() + scrape_gradcafe()
├── clean.py                       # clean_school(), normalize_decision(), normalize_degree()
├── build.py                       # run(recent_only, test_seasons) — full pipeline
├── requirements.txt
└── README.md
```

## Unified data schema

`rowid, school_raw, school_clean, program_raw, degree, season_code, season_year, decision, decision_class, date_posted, gpa, gre_v, gre_q, gre_w, applicant_status, source`

- `source`: `'deedy_2015'` or `'gradcafe_scrape'`
- `season_code`: `F25`, `S25`, etc. (letter + 2-digit year)
- `season_year`: integer (2025 for F25 or S25)
- `decision_class`: exactly one of `Accepted / Rejected / Waitlisted / Interview / Other`
- `degree`: exactly one of `PhD / MA / Other`

## Key design decisions

- **No server**: all data embedded as minified JSON in `docs/index.html` at build time. Works offline after load.
- **Deedy dataset** uses `all_uisc_clean.csv` (headerless, 75MB). Columns mapped by index via `_DC` dict in `scraper.py`.
- **Dedup key**: `school_raw + season_code + date_posted + decision` (MD5 hash → rowid)
- **Scrape cache**: each page saved as `data/raw/{degree}{season}{page:03d}.html` — never re-fetched if file exists
- **GradCafe URL pattern**: `https://www.thegradcafe.com/survey/?q=&sort=newest&institution=&program=Political+Science&degree={degree}&season={season}&decision=&page={page}`

## How to run

```bash
pip install -r requirements.txt
python build.py --test F24 F25 F26   # quick test
python build.py                       # full historical scrape
python build.py --recent              # re-scrape only 2 most recent seasons
```

## Known issues / next steps

1. **GradCafe 403** — need `cloudscraper` or manual cookie injection to get past Cloudflare
2. **Dedup count** — 10,448 Deedy rows → 5,930 after dedup seems high; the dedup key may be too strict for this dataset (many rows lack date_posted)
3. After scraping works: run full historical scrape for all 22 seasons, commit CSV + HTML, enable GitHub Pages from `/docs`
