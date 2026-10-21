"""site-lint: check a static site folder before it is served.

    python3 lint.py SITEDIR     # one JSON line; exit 0 no problems, 1 problems found, 2 could not check

Checks: SITEDIR/index.html exists, has a <title>, sets a mobile viewport (width=device-width), and no link or image
points at a local file (file://).
"""
import json
import os
import re
import sys


def main(argv):
    if len(argv) != 1 or not os.path.isdir(argv[0]):
        print(json.dumps({"checked": False, "reason": "give one existing folder"}))
        return 2
    page = os.path.join(argv[0], "index.html")
    if not os.path.isfile(page):
        print(json.dumps({"checked": True, "problems": ["no index.html"]}))
        return 1
    with open(page, encoding="utf-8", errors="replace") as f:
        text = f.read()
    problems = []
    if not re.search(r"<title>[^<]+</title>", text):
        problems.append("no <title>")
    if not re.search(r'<meta[^>]+name="viewport"[^>]+width=device-width', text):
        problems.append("no mobile viewport")
    if re.search(r'(href|src)="file://', text):
        problems.append("links to a local file")
    print(json.dumps({"checked": True, "problems": problems}))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
