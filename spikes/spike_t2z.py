"""Spike (2026-09-29, superseded by the tzb package): create one Zotero annotation from a Tine highlight, then test PATCH + stale-version 412."""
import json, os, re, sys, urllib.request, urllib.error
from pathlib import Path
import pymupdf

API = "http://localhost:23119/api/users/0"
GRAPH = Path(os.environ["TZB_GRAPH"])   # the Tine graph folder
AUTH = json.load(open(Path.home() / "Library/Application Support/tine-zotero/auth.json"))
ATT, NAME, UID = "5L5K5CNK", "pay2026Keep", sys.argv[1]
HEX = {"yellow": "#ffd400", "red": "#ff6666", "green": "#5fb236", "blue": "#2ea8e5", "purple": "#a28ae5"}


def req(method, path, body=None, headers=()):
    h = {"Zotero-API-Key": AUTH["api_key"], "Zotero-Server-ID": AUTH["server_id"], "Content-Type": "application/json", **dict(headers)}
    r = urllib.request.Request(API + path, method=method, headers=h, data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


edn = (GRAPH / f"assets/{NAME}.edn").read_text()
hl = edn[edn.index(UID):]
hl = hl[:hl.find(":id #uuid", 1) if ":id #uuid" in hl[1:] else len(hl)]
pg = int(re.search(r":page (\d+)", hl).group(1))
color = re.search(r':color "(\w+)"', hl).group(1)
text = json.loads('"' + re.search(r':text "((?:[^"\\]|\\.)*)"', hl).group(1) + '"')
rects = [tuple(map(float, m)) for m in re.findall(
    r":x1 ([-\d.]+) :y1 ([-\d.]+) :x2 ([-\d.]+) :y2 ([-\d.]+) :width ([\d.]+) :height ([\d.]+)", hl.split(":rects", 1)[1])]

page = pymupdf.open(GRAPH / f"assets/{NAME}.pdf")[pg - 1]
M = page.transformation_matrix * page.rotation_matrix
W, H = page.rect.width, page.rect.height
view = [pymupdf.Rect(x1 * W / w, y1 * H / h, x2 * W / w, y2 * H / h) for x1, y1, x2, y2, w, h in rects]

# Tine stores overlapping per-glyph-run rects; merge into one rect per text line.
lines = []
for r in sorted(view, key=lambda r: (r.y0, r.x0)):
    for i, L in enumerate(lines):
        if min(L.y1, r.y1) - max(L.y0, r.y0) > 0.5 * min(L.height, r.height):
            lines[i] = L | r
            break
    else:
        lines.append(pymupdf.Rect(r))

pdf_rects = []
for L in lines:
    r = L * ~M
    r.normalize()
    pdf_rects.append([round(v, 3) for v in r])

ptext = page.get_text()
first = re.sub(r"\s+", " ", text).strip()[:30]
offset = max(re.sub(r"\s+", " ", ptext).find(first), 0)
sort_index = f"{pg - 1:05d}|{offset:06d}|{int(lines[0].y0):05d}"

ann = {
    "itemType": "annotation", "parentItem": ATT, "annotationType": "highlight",
    "annotationText": re.sub(r"\s*\n\s*", " ", text), "annotationComment": "",
    "annotationColor": HEX[color], "annotationPageLabel": page.get_label() or str(pg),
    "annotationSortIndex": sort_index,
    "annotationPosition": json.dumps({"pageIndex": pg - 1, "rects": pdf_rects}, separators=(",", ":")),
    "tags": [],
}
st, res = req("POST", "/items", [ann])
print("POST", st, json.dumps(res.get("failed") if isinstance(res, dict) else res, ensure_ascii=False))
key = res["success"]["0"]
st, cur = req("GET", f"/items/{key}")
ver = cur["version"]
print("created", key, "version", ver, "label", cur["data"]["annotationPageLabel"], "sort", cur["data"]["annotationSortIndex"])

st, _ = req("PATCH", f"/items/{key}", {"annotationComment": "tzb spike"}, {"If-Unmodified-Since-Version": str(ver)})
print("PATCH current version ->", st)
st, body = req("PATCH", f"/items/{key}", {"annotationComment": "stale write"}, {"If-Unmodified-Since-Version": str(ver)})
print("PATCH stale version ->", st, str(body)[:100])
st, cur = req("GET", f"/items/{key}")
print("comment now:", repr(cur["data"]["annotationComment"]), "version", cur["version"])
st, _ = req("PATCH", f"/items/{key}", {"annotationComment": ""}, {"If-Unmodified-Since-Version": str(cur["version"])})
print("reset comment ->", st)

# Record the mapping in the Tine page (atomic, compare-and-swap).
md_path = GRAPH / f"pages/hls__{NAME}.md"
old = md_path.read_text()
new = old.replace(f"  id:: {UID}\n", f"  id:: {UID}\n  zotero-key:: {key}\n", 1)
assert new != old
tmp = md_path.with_name(".tzb-spike.tmp")
tmp.write_text(new)
assert md_path.read_text() == old
os.replace(tmp, md_path)
print("md: zotero-key recorded")
