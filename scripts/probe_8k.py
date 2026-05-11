"""Probe edgartools EarningsRelease.guidance / summary / key_metrics."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))

from edgar import set_identity, Company
set_identity("Ferran Punso ferranpunso@gmail.com")

f = Company("ANET").get_filings(form="8-K").head(1)[0]
obj = f.obj()
e = obj.earnings

out = []

out.append("=== summary ===")
try:
    out.append(str(e.summary)[:3000])
except Exception as ex:
    out.append(f"error: {ex}")

out.append("\n=== guidance ===")
try:
    g = e.guidance
    out.append(f"type: {type(g).__name__}")
    out.append(str(g)[:3000])
except Exception as ex:
    out.append(f"error: {ex}")

out.append("\n=== get_key_metrics() ===")
try:
    km = e.get_key_metrics()
    out.append(f"type: {type(km).__name__}")
    out.append(str(km)[:3000])
except Exception as ex:
    out.append(f"error: {ex}")

out.append("\n=== attachment ===")
try:
    a = e.attachment
    out.append(f"type: {type(a).__name__}")
    out.append(f"attrs: {[x for x in dir(a) if not x.startswith('_')][:30]}")
    # try common methods
    for m in ["text", "html", "markdown", "name", "url"]:
        try:
            v = getattr(a, m, None)
            if v is not None and not callable(v):
                out.append(f"  {m}: {str(v)[:200]}")
            elif callable(v):
                out.append(f"  {m}() -> {str(v())[:500]}")
        except Exception as ex:
            out.append(f"  {m}: err {ex}")
except Exception as ex:
    out.append(f"attachment err: {ex}")

Path("data/logs/probe_8k.txt").write_text("\n".join(out), encoding="utf-8")
print("written")
