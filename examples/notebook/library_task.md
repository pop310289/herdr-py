Make a one-page report on the monthly visitors of a small made-up library, the Northside Public Library.

There are three kinds of answer. The first line inside your fenced answer says which one it is, then comes the answer:

ARTIFACT: data
{"title": "...", "months": ["Jan", "Feb", ...], "visitors": [1200, 980, ...]}
  Valid when months and visitors have the same length (at least 6) and every count is a whole number >= 0.
  Score: 5 per month (at most 60).

ARTIFACT: skill
---
name: <a short name, lower case with dashes>
---
How to draw a bar chart as inline SVG, as numbered steps (1. 2. 3. ...).
  Valid with a name and at least 3 numbered steps. Score: 10, plus 2 per step after the third (at most 20).

ARTIFACT: page
<!doctype html> ... a page with the report's title and an <svg> bar chart: one <rect class="bar"> per month.
  Valid with a title and at least 6 bars. Score: 50, plus 4 per bar after the sixth (at most 74), plus 26 when
  every bar has a <title> with its month and count (a reader can hover a bar to read it).
