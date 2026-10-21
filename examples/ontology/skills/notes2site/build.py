"""notes2site: turn a notes file into a one-page static site.

    python3 build.py NOTES.md OUTDIR      # writes OUTDIR/index.html; exit 0, or 2 when NOTES.md cannot be read
"""
import html
import os
import sys


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    src, out = argv
    try:
        with open(src, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as exc:
        print("cannot read %s: %s" % (src, exc), file=sys.stderr)
        return 2
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), os.path.basename(src))
    body = []
    for line in lines:
        if line.startswith("## "):
            body.append("<h2>%s</h2>" % html.escape(line[3:].strip()))
        elif line.startswith("# "):
            body.append("<h1>%s</h1>" % html.escape(line[2:].strip()))
        elif line.strip():
            body.append("<p>%s</p>" % html.escape(line.strip()))
    os.makedirs(out, exist_ok=True)
    page = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<title>%s</title>\n<style>body{font:16px/1.5 sans-serif;max-width:40em;margin:0 auto;padding:0 16px}</style>\n'
            '</head><body>\n%s\n</body></html>\n') % (html.escape(title), "\n".join(body))
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
