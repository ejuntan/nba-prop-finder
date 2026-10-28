"""Inline the slate data into the page, so index.html stands alone."""
import json
import os

DIR = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(DIR, "template.html")
DATA = os.path.join(DIR, "..", "out", "tool_data.json")
OUT = os.path.join(DIR, "index.html")

with open(TPL) as fh:
    tpl = fh.read()
with open(DATA) as fh:
    data = fh.read()
json.loads(data)          # fail loudly rather than ship a broken page
head, _, rest = tpl.partition("/*DATA*/")
_, _, tail = rest.partition("/*DATA*/")
with open(OUT, "w") as fh:
    fh.write(head + data + tail)
print(f"tool: {os.path.getsize(OUT) / 1024:.0f} KB -> {OUT}")
