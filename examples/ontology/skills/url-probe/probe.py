"""url-probe: fetch a page by its URL and check that it is a page.

    python3 probe.py URL     # one JSON line; exit 0 200 with HTML and a <title>, 1 it was not, 2 could not fetch
"""
import json
import sys
import urllib.request


def main(argv):
    if len(argv) != 1:
        print(json.dumps({"checked": False, "reason": "give one URL"}))
        return 2
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(argv[0], timeout=10) as r:
            status, ctype = r.status, r.headers.get("Content-Type", "")
            body = r.read(200000).decode("utf-8", "replace")
    except Exception as exc:  # any failure to fetch means the check did not happen
        print(json.dumps({"checked": False, "reason": str(exc)[:200]}))
        return 2
    problems = []
    if status != 200:
        problems.append("status %d" % status)
    if "text/html" not in ctype:
        problems.append("content type %s" % ctype)
    if "<title>" not in body:
        problems.append("no <title>")
    print(json.dumps({"checked": True, "status": status, "problems": problems}))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
