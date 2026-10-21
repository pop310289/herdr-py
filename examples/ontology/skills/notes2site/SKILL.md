---
name: notes2site
description: Turn a notes file (headings and paragraphs) into a one-page static site with a mobile viewport.
---

# notes2site

    python3 build.py NOTES.md OUTDIR      # writes OUTDIR/index.html

- Lines starting with `# ` and `## ` become headings; other non-empty lines become paragraphs; the first `# ` heading is the title.
- Exit code: 0 the page was written; 2 NOTES.md could not be read or the arguments are wrong.
- Standard library only.
