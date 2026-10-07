from kit import COLORS, box, dot, line, save, text, token, tokens

# Header (done)
box(0, 20, 12, 120, fill=COLORS["decode"], stroke=COLORS["decode"], radius=0, name="accent")
text(40, 22, 900, 58, "LLM Serving: When to Split", size=44, bold=True, name="title1")
text(40, 76, 900, 58, "Prefill and Decode", size=44, bold=True, name="title2")
for i, (label, kind) in enumerate([("Prefill", "prefill"), ("KV cache", "kv"), ("Decode", "decode")]):
    token(1000 + i * 200, 103, "", kind, w=22, h=22, name="legend-box")
    text(1030 + i * 200, 96, 160, 36, label, size=26, name="legend")
text(1560, 24, 340, 30, "Based on TheAiEdge.io", size=22, color=COLORS["decode_border"], name="credit")

# Row titles and separators (done)
for title, top in [("Shared worker", 160), ("Chunked prefill", 470), ("Separate prefill + decode", 780)]:
    if top > 160:
        line(40, top - 8, 1840, 0, color=COLORS["sep"], name="separator")
    text(40, top, 560, 44, title, size=32, bold=True, name="row-title")

# ROW 1 "Shared worker" (top 160): TODO - Long prompt + six P tokens, Live streams + A B, the 1 GPU box (Decode box, KV grid, local), connectors, Output tokens lanes New / A / B
# ROW 2 "Chunked prefill" (top 470): TODO - see SPEC.md
# ROW 3 "Separate prefill + decode" (top 780): TODO - see SPEC.md

save()
