# Political Science PhD Admissions — GradCafe Data

**[Browse the data →](https://nicolas-izquierdo.github.io/gradcafe-polisci/)**

10,700+ self-reported admissions outcomes for Political Science PhD programs, 2006–2026. See when programs send decisions, compare years, and explore GPA/GRE profiles.

Data is self-reported by applicants on [thegradcafe.com](https://www.thegradcafe.com) — not representative of true acceptance rates.

---

**Run locally**

```bash
pip install -r requirements.txt
python build.py --test F25 F26   # quick test (uses cached pages)
python build.py                  # full rebuild — opens a Chrome window
```

**Fix a school name** — patterns are in `clean.py` → `SCHOOL_RULES`. Add a regex, run `python build.py --test F26`, open a PR.

---

MIT License · No affiliation with GradCafe
