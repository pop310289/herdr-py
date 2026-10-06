import collections
import re
for w, c in collections.Counter(re.findall(r"[a-z]+", open("text.txt").read().lower())).most_common(3):
    print("%s %d" % (w, c))
