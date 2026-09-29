"""Spike (2026-09-29, superseded by the tzb package): materialize one Zotero PDF + its highlights into the Tine graph (Zotero read-only)."""
import json, os, subprocess, sys, uuid, urllib.request, urllib.parse
from pathlib import Path
import pymupdf

API = "http://localhost:23119/api/users/0"
GRAPH = Path(os.environ["TZB_GRAPH"])   # the Tine graph folder
ATT = sys.argv[1]
NS = uuid.UUID("5b0c6f7e-3d7a-4a53-9a57-2f3e0f6f1a10")
TINE = {"yellow": "#ffd400", "red": "#ff6666", "green": "#5fb236", "blue": "#2ea8e5", "purple": "#a28ae5"}


def get(path):
    return json.load(urllib.request.urlopen(API + path))


def nearest(hexc):
    rgb = lambda h: [int(h[i:i + 2], 16) for i in (1, 3, 5)]
    c = rgb(hexc)
    return min(TINE, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb(TINE[n]), c)))


att = get(f"/items/{ATT}")["data"]
parent = get(f"/items/{att['parentItem']}")["data"]
name = parent["citationKey"]
src = urllib.parse.unquote(urllib.request.urlopen(f"{API}/items/{ATT}/file/view/url").read().decode()[len("file://"):])
pdf, ednp, md = GRAPH / f"assets/{name}.pdf", GRAPH / f"assets/{name}.edn", GRAPH / f"pages/hls__{name}.md"
assert not any(p.exists() for p in (pdf, ednp, md)), "target exists"

anns = sorted((a["data"] for a in get(f"/items/{ATT}/children?itemType=annotation")), key=lambda d: d["annotationSortIndex"])
doc = pymupdf.open(src)

f = lambda v: repr(round(v, 4))
edn_hl, blocks = [], []
for d in anns:
    pos = json.loads(d["annotationPosition"])
    page = doc[pos["pageIndex"]]
    m = page.transformation_matrix * page.rotation_matrix  # PDF user space -> rotated viewport @ scale 1 (pdf.js)
    W, H = page.rect.width, page.rect.height
    rects = [pymupdf.Rect(r) * m for r in pos["rects"]]
    for r in rects:
        r.normalize()
    b = rects[0]
    for r in rects[1:]:
        b |= r
    box = lambda r: f"{{:x1 {f(r.x0)} :y1 {f(r.y0)} :x2 {f(r.x1)} :y2 {f(r.y1)} :width {f(W)} :height {f(H)}}}"
    uid = uuid.uuid5(NS, d["key"])
    color = nearest(d["annotationColor"])
    pg = pos["pageIndex"] + 1
    text = d["annotationText"]
    edn_hl.append(f'{{:id #uuid "{uid}" :page {pg} :position {{:page {pg} :bounding {box(b)} :rects ({" ".join(box(r) for r in rects)})}} '
                  f':content {{:text {json.dumps(text, ensure_ascii=False)}}} :properties {{:color "{color}"}}}}')
    lines = text.split("\n")
    blk = [f"- {lines[0]}"] + [f"  {l}" for l in lines[1:]] + [
        f"  hl-page:: {pg}", f"  hl-color:: {color}", "  ls-type:: annotation", f"  id:: {uid}", f"  zotero-key:: {d['key']}"]
    if d["annotationComment"]:
        c = d["annotationComment"].split("\n")
        blk += [f"\t- {c[0]}"] + [f"\t  {l}" for l in c[1:]] + ["\t  zotero:: comment"]
    blocks.append("\n".join(blk))

subprocess.run(["cp", "-c", src, str(pdf)], check=True)
ednp.write_text("{:highlights [" + " ".join(edn_hl) + "] :extra {}}\n")
md.write_text(f"file:: [{name}.pdf](../assets/{name}.pdf)\nfile-path:: ../assets/{name}.pdf\n" + "\n".join(blocks) + "\n")
print(name, len(anns), "highlights ->", md)
