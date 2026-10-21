---
name: url-probe
description: Fetch a page by its URL and check that it answers 200 with an HTML page that has a title.
---

# url-probe

    python3 probe.py URL     # prints one JSON line

- Exit code: 0 the page answered 200 with HTML and a <title>; 1 it did not; 2 could not fetch (not a check that passed).
- No proxy is used. Standard library only.
