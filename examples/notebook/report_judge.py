"""The judge of the notebook demo (report_task.md): reads the answer file named last on the command line and prints one
JSON line {"status": "valid" | "invalid", "score": ..., "detail": ...}. Standard library only."""
import json
import re
import sys


def verdict(text):
    first, _, body = text.strip().partition("\n")
    kind = first.split(":", 1)[1].strip().lower() if first.lower().startswith("artifact") and ":" in first else None
    if kind == "data":
        try:
            d = json.loads(body)
        except ValueError as exc:
            return "invalid", 0, f"data: not JSON ({exc})"
        months, counts = d.get("months") or [], d.get("visitors") or []
        if len(months) != len(counts) or len(months) < 6:
            return "invalid", 0, f"data: {len(months)} months and {len(counts)} counts; at least 6 of each, as many of each"
        if not all(isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in counts):
            return "invalid", 0, "data: every count is a whole number >= 0"
        return "valid", min(60, 5 * len(months)), f"data: {len(months)} months"
    if kind == "skill":
        name = re.search(r"^name:\s*(\S+)", body, re.M)
        steps = re.findall(r"^\s*\d+\.\s", body, re.M)
        if not name or len(steps) < 3:
            return "invalid", 0, f"skill: a name and at least 3 numbered steps ({len(steps)} steps)"
        return "valid", min(20, 10 + 2 * (len(steps) - 3)), f"skill {name.group(1)}: {len(steps)} steps"
    if kind == "page":
        bars = re.findall(r"<rect[^>]*class=\"bar\"[^>]*>(.*?)</rect>|<rect[^>]*class=\"bar\"[^>]*/>", body, re.S)
        if "<svg" not in body or not re.search(r"<title>[^<]+</title>", body) or len(bars) < 6:
            return "invalid", 0, f"page: a title and an <svg> with at least 6 bars ({len(bars)} bars)"
        titled = all(re.search(r"<title>[^<]+</title>", b or "") for b in bars)
        return "valid", min(74, 50 + 4 * (len(bars) - 6)) + (26 if titled else 0), f"page: {len(bars)} bars" + (", titled" if titled else "")
    return "invalid", 0, "the first line names no kind (ARTIFACT: data, skill or page)"


if __name__ == "__main__":
    with open(sys.argv[-1], encoding="utf-8") as handle:
        status, score, detail = verdict(handle.read())
    print(json.dumps({"status": status, "score": score, "detail": detail}))
