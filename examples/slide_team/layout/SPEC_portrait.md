# Target: "LLM Serving: When to Split Prefill and Decode" on a 1206 x 1441 canvas

Based on a diagram by TheAiEdge.io. The header, the three row titles and the separators are already drawn.
Each row is a checklist: one line = one component (or two). Positions are the top-left corner, in pixels.

## Row 1 "Shared worker" (y 176 to 555, ids start with r1.)
1. text "Long prompt", bold, size 28, at x 50, y 220
2. tokens: six "P", kind prompt, at x 50, y 265, w 31, h 42, gap 4
3. text "Live streams", bold, size 28, at x 50, y 386
4. tokens: "A", "B", kind decode, at x 53, y 428, w 67, h 48, gap 13
5. box kind gpu, title "1 GPU", at x 384, y 262, w 334, h 260, pins true
6. box kind decode, title "Decode", at x 414, y 317, w 153, h 148 (inside the GPU box)
7. tokens "A", "B", "P", kind decode, at x 428, y 364, w 34, h 34, gap 5; tokens "A'", "B'", "P'", kind outline, at x 428, y 407, same size
8. text "KV", green (color kv), bold, size 26, at x 619, y 293; grid kind kv, 3 rows x 3 cols, at x 612, y 340, row_labels P, A, B
9. text "local", color kv, size 24, at x 613, y 460
10. arrow color line from the P tokens into the Decode box: points [262, 287], [335, 287], [335, 378], [412, 378]
11. arrow color stream, dashed, nodes, from B into the GPU box: points [205, 452], [218, 452], [287, 452], [355, 452], [412, 452]
12. arrow color stream, dashed, nodes, from the Decode box to lane A: points [565, 466], [565, 492], [773, 492], [773, 379], [828, 379]
13. text "Output tokens", bold, size 30, at x 877, y 212; text "Schematic timing", color muted, size 18, anchor end, at x 1155, y 190
14. lanes at x 878, w 277: "New" at y 295 (label_size 22, color text) with a decode token "P" at 906; "A" at y 379 with "A" at 909 and 1127; "B" at y 462 with "B" at 904 and 1122

## Row 2 "Chunked prefill" (y 578 to 958, ids start with r2.)
1. text "Long prompt", bold, size 28, at x 50, y 623
2. tokens: four "P", kind prompt, at x 50, y 667, w 31, h 42, gap 4; then two "P", kind prefill, at x 199, y 667, same size
3. text "Live streams", bold, size 28, at x 50, y 790; tokens "A", "B", kind decode, at x 53, y 830, w 67, h 48, gap 13
4. text "WAIT", color red, bold, size 22, at x 52, y 895; text "||", color red, bold, size 34, at x 333, y 838
5. arrow color line, head false, from B toward the GPU box: points [205, 855], [384, 855]
6. arrow color chunk, nodes, from the orange P tokens into the Chunk box: points [268, 688], [312, 688], [335, 712], [335, 775], [412, 775]
7. box kind gpu, title "1 GPU", at x 384, y 663, w 334, h 262, pins true
8. box kind chunk, title "Chunk 3", at x 414, y 718, w 153, h 148
9. inside it, two token rows at y 766 and y 810: "P", "P" kind empty at x 430 (w 34, h 34, gap 5) and "P" kind prefill at x 508
10. text "KV", color kv, bold, size 26, at x 619, y 695; grid kind kv, 3 x 3, at x 612, y 740, row_labels P, A, B; text "local", color kv, size 24, at x 613, y 862
11. arrow color line from the GPU to lane A: points [565, 866], [565, 892], [773, 892], [773, 780], [828, 780]
12. text "Output tokens", bold, size 30, at x 877, y 614; text "PREFILL", color muted, bold, size 18, at x 882, y 650
13. lanes at x 878, w 277: "New" at y 697 (label_size 22, color text, no tokens); "A" at y 780 with "A" at 924, 1024, 1127; "B" at y 864 with "B" at 919, 1019, 1122

## Row 3 "Separate prefill + decode" (y 981 to 1380, ids start with r3.)
1. text "Long prompt", bold, size 28, at x 50, y 1026; tokens: six "P", kind prompt, at x 50, y 1070, w 31, h 42, gap 4
2. text "Live streams", bold, size 28, at x 50, y 1190; tokens "A", "B", kind decode, at x 53, y 1233, w 67, h 48, gap 13
3. box kind pool, title "Prefill pool", title_size 24, stack 2, pins true, at x 322, y 1065, w 176, h 250
4. inside it: box kind prefill, title "Prefill", title_size 22, at x 350, y 1123, w 120, h 74; six empty-label tokens kind prefill at x 362, y 1170, w 13, h 15, gap 3
5. text "KV cache", color kv, bold, size 22, anchor middle, at x 410, y 1214; three empty-label tokens kind kv at x 352, y 1248, w 34, h 27, gap 6
6. text "TRANSFERRING", color kv, bold, size 15, anchor middle, at x 410, y 1285
7. text "KV" and "copy" (two texts, color kv, bold, size 22, anchor middle) at x 542, y 1140 and y 1166; arrow color kv, nodes: [498, 1262], [548, 1262], [548, 1205], [598, 1205]
8. box kind pool, title "Decode pool", title_size 24, stack 2, pins true, at x 590, y 1150, w 178, h 165
9. inside it: one empty-label kv token at x 610, y 1193 (w 38, h 22) and two empty-label empty tokens at x 655 (w 38, h 22, gap 5); tokens "A", "B" kind kv at x 610, y 1223, w 60, h 20, gap 6; box kind decode, title "Decode", at x 617, y 1255, w 124, h 45
10. arrow color line from the P tokens into the Prefill pool: [262, 1090], [305, 1090], [305, 1160], [320, 1160]
11. arrow color stream, dashed, nodes, from B under the pools into Decode: [205, 1256], [282, 1256], [296, 1345], [567, 1345], [578, 1278], [615, 1278]
12. arrow color stream, nodes, from the Decode pool to lane A: [768, 1272], [786, 1272], [786, 1183], [828, 1183]
13. text "Output tokens", bold, size 30, at x 877, y 1016; text "KV COPY", color muted, bold, size 18, at x 882, y 1052
14. lanes at x 878, w 277: "New" at y 1099 (label_size 22, color text, no tokens); "A" at y 1183 with six "A" at 909, 952, 995, 1038, 1081, 1124; "B" at y 1267 with six "B" at 904, 947, 990, 1033, 1076, 1119
