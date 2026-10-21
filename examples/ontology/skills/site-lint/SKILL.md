---
name: site-lint
description: Check a static site folder before it is served - index.html, a title, a mobile viewport, no links to local files.
---

# site-lint

    python3 lint.py SITEDIR     # prints one JSON line

- Exit code: 0 no problems; 1 problems found (listed in the JSON); 2 could not check (not a check that passed).
- Standard library only.
