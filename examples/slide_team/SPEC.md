# Target slide: "LLM Serving: When to Split Prefill and Decode"

Recreate this infographic as ONE slide, canvas 1920 x 1080, background #F4F7FB. Based on a diagram by TheAiEdge.io:
put the small credit text "Based on TheAiEdge.io" in the top-right corner (do not imitate their logo).

## Colours
- text #1B2430, secondary text #5D6673
- Prefill: orange fill #F5A623, border #C77C02
- KV cache: green fill #4CAF50, border #2E7D32
- Decode: blue fill #2F80ED, border #1B5FB8, white letters
- prompt token "P": light grey fill #E9ECEF, border #9AA4AE, dark letter
- compute box (GPU / pool): fill #EAF1F8, border #6B7A8C, radius 12
- connector lines: #2F80ED (streams), #9AA4AE (prompt), orange #F5A623 (chunk)
- thin row separators: #D5DBE3

## Header (y 20..140)
- blue accent bar: rect x 0, y 20, w 12, h 120, fill #2F80ED
- title, bold, 44 px, two lines: "LLM Serving: When to Split" (x 40, y 22) and "Prefill and Decode" (x 40, y 76)
- legend, 26 px, at y 95: small orange square + "Prefill" (x 1000), green square + "KV cache" (x 1180), blue square + "Decode" (x 1400)
- credit "Based on TheAiEdge.io", 22 px, italic-looking blue #2F80ED, top right (x 1560, y 24)

## Three rows, stacked
Row 1 y 160..460, row 2 y 470..770, row 3 y 780..1070; a separator line above rows 2 and 3.
Every row has the same columns:
- left (x 40..560): row title (bold 32 px), label "Long prompt" (24 px), a row of six "P" token boxes (44 x 40, 6 px apart),
  label "Live streams" (24 px), two blue boxes "A" and "B" (70 x 48).
- middle (x 620..1150): the compute box(es).
- right (x 1200..1880): label "Output tokens" (bold 26 px) and three lanes, each a thin horizontal line ending in a small arrow
  at x 1870: lane "New" (y offset 70), lane "A" (offset 140), lane "B" (offset 210), with token boxes (42 x 40) on the lanes.

### Row 1 "Shared worker"
- compute box "1 GPU" (x 640, w 480, h 260) containing:
  - a blue-bordered "Decode" box with two rows of small tokens: [A][B][P] blue, [A'][B'][P'] white with blue border
  - label "KV" and a 3 x 3 grid of green squares to its right, label "local" under the grid
- prompt tokens connect with a grey line into the GPU box; streams A, B connect with a dashed blue line into it.
- outputs: New: one "P"; A: "A" near the start and "A" near the end; B: the same (sparse output: the long prompt delays the streams).

### Row 2 "Chunked prefill"
- the last two "P" prompt tokens are orange (the current chunk); an orange line runs from them into the GPU box.
- compute box "1 GPU" containing an orange "Chunk 3" box with tokens [P][P][P] twice (two grey, the last one orange), KV grid, "local".
- the live streams are paused: a red "||" mark on their line and a red label "WAIT" under A, B.
- small label "PREFILL" under "Output tokens"; outputs: New: empty lane; A: three "A" spread out; B: three "B" spread out.

### Row 3 "Separate prefill + decode"
- two compute boxes, each drawn as a small stack (two offset rectangles behind):
  - "Prefill pool" (x 640, w 240): orange "Prefill" box with six small squares, a green "KV cache" row of three squares,
    and a small label "TRANSFERRING"
  - "Decode pool" (x 920, w 250): green KV squares labelled "A" and "B", and a blue "Decode" box at the bottom
- label "KV copy" between the pools with a green arrow from the Prefill pool to the Decode pool.
- live streams A, B connect with a dashed blue line to the Decode pool.
- small label "KV COPY" under "Output tokens"; outputs: New: empty; A: six "A" boxes close together; B: six "B" close together
  (continuous output: decode is never interrupted).
