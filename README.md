# Political Science PhD Admissions — GradCafe Data

A public dataset and visualization of Political Science PhD (and MA) admissions results reported on [thegradcafe.com](https://www.thegradcafe.com), covering 2006–present.

**Live site:** [https://nicolas-izquierdo.github.io/gradcafe-polisci/](https://nicolas-izquierdo.github.io/gradcafe-polisci/)

---

## What this is

This project aggregates self-reported admissions decisions from GradCafe into a clean, searchable dataset. It covers ~15,000+ records across all major US Political Science PhD programs, with GPA/GRE data where available. The site is rebuilt weekly (Jan–Apr) via GitHub Actions.

## Data sources

- **2006–2015:** [deedy/gradcafe_data](https://github.com/deedy/gradcafe_data) — a historical scrape of GradCafe filtered to Political Science
- **2016–present:** Scraped directly from [thegradcafe.com](https://www.thegradcafe.com/survey/?q=&program=Political+Science) via `scraper.py`

## Running locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Test run (F24, F25, F26 only — fast)
python build.py --test F24 F25 F26

# 3. Full historical scrape (takes ~30–60 min, cached after first run)
python build.py
```

After running, open `docs/index.html` in your browser.

## Contributing school name corrections

School names from GradCafe are messy free-text. Normalization patterns live in `clean.py` in the `SCHOOL_RULES` list. Each entry is:

```python
(r"regex_pattern", "Canonical School Name", usnwr_rank),
```

To fix a mapping: open `clean.py`, find the relevant pattern, and add a more specific regex. Run `python build.py --test F25` to verify, then submit a PR.

## Known limitations

- **Self-reported data:** All records are submitted voluntarily by applicants. Acceptance rates and statistics may not be representative of the actual applicant pool.
- **Survivorship bias:** Successful applicants are more likely to report results than those who were rejected or didn't hear back.
- **Incomplete GRE/GPA data:** Most records (especially older ones) are missing test scores.
- **Duplicate entries:** Some applicants report the same decision multiple times. Deduplication is approximate.
- **No admission year certainty:** Season codes (F25 = Fall 2025 cycle) are as reported by the user.

## License

Code: MIT License. Data from thegradcafe.com is subject to their terms of service. This project has no affiliation with GradCafe.
