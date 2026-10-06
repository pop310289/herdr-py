import collections
import re
counts = collections.Counter(re.findall(r"[a-z]+", open("text.txt").read().lower()))
for w, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]:
    print("%s %d" % (w, c))
