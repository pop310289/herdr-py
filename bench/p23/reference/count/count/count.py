import os
here = os.path.dirname(os.path.abspath(__file__))
print(len(open(os.path.join(here, "notes.txt")).read().split()))
