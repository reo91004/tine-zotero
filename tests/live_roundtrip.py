"""Live round trip against a running Zotero, on a COPY of a synced graph. Creates one throwaway annotation and
deletes it again; other annotations are only read. Stop the login agent first (`tzb uninstall-agent`) so it
does not pull the throwaway annotation into your real graph.

Run: TZB_HOME=<temp state dir> uv run python tests/live_roundtrip.py <page name with a highlight>
where <temp state dir>/config.json names the graph copy and holds a copy of the real state.json, and
auth.json is the real one (or a link to it).
"""
import json
import sys
import time
import uuid

from tzb import bridge, tine
from tzb.zotero import Zotero, ZoteroError

NAME = sys.argv[1]
G = __import__("pathlib").Path(json.loads((bridge.APP / "config.json").read_text())["graph"])
EDN, MD = G / f"assets/{NAME}.edn", G / f"pages/hls__{NAME}.md"
z = Zotero(json.loads((bridge.APP / "auth.json").read_text()))
TID = str(uuid.uuid4())


def step(title):
    time.sleep(3.5)                 # past EDIT_QUIET_MS, so the Tine-side writes run in this cycle
    print(f"\n## {title}")
    bridge.sync()


def zget(key):
    return z.get(f"/items/{key}")[0]["data"]


def hl(tid=TID):
    return tine.page_highlights(tine.parse_page(MD.read_text())[1]).get(tid)


def edit_md(fn):
    MD.write_text(fn(MD.read_text()))


def ok(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


n0 = len(z.get_all("/items?itemType=annotation")[0])
src, _, _ = tine.edn_highlights(EDN.read_text())[0][0]
att = next(k for k, d in json.loads((bridge.APP / "state.json").read_text())["docs"].items() if d["name"] == NAME)
r0 = src[":position"][":rects"][0]

# A. highlight made in Tine (appended at the page end, no key) -> created in Zotero, nested, key and comment block
EDN.write_text(tine.edn_add(EDN.read_text(), tine.edn_entry(
    TID, src[":page"], [(r[":x1"], r[":y1"], r[":x2"], r[":y2"]) for r in src[":position"][":rects"]],
    r0[":width"], r0[":height"], "tzb test highlight", "red")))
edit_md(lambda md: tine.md_append(md, f"- tzb test highlight\n  hl-page:: {src[':page']}\n  hl-color:: red\n"
                                      f"  ls-type:: annotation\n  id:: {TID}"))
step("A. create in Tine")
step("A2. settle Tine-side follow-ups")
key = hl().zotero_key
d = zget(key)
ok(d["annotationColor"] == "#ff6666" and d["annotationComment"] == "" and d["parentItem"] == att, f"Zotero has {key}")
ok(hl().cblock is not None and hl().block.depth == 1 and not tine.loose_highlights(MD.read_text()),
   "Tine: empty comment block added, highlight moved into the section")

# B. comment written in Tine (two lines in the one comment block)
edit_md(lambda md: tine.md_set_comment(md, TID, "line one\nline two"))
step("B. comment in Tine")
ok(zget(key)["annotationComment"] == "line one\nline two", "Zotero comment = two lines")

# C. color changed in Tine (the reader rewrites both files)
EDN.write_text(tine.edn_set_color(EDN.read_text(), TID, "blue"))
edit_md(lambda md: tine.md_set_prop(md, TID, "hl-color", "blue"))
step("C. color in Tine")
ok(zget(key)["annotationColor"] == "#2ea8e5", "Zotero color = blue")

# D. tags in Tine
edit_md(lambda md: tine.md_set_prop(md, TID, "tags", "tzbtest"))
step("D. tags in Tine")
ok([t["tag"] for t in zget(key)["tags"]] == ["tzbtest"], "Zotero tags = tzbtest")

# E. comment edited in Zotero; a Tine-only memo beside it stays out of Zotero
ok(z.patch(key, {"annotationComment": "from zotero"}, zget(key)["version"]), "PATCH in Zotero")
step("E. comment in Zotero")
ok(hl().comment == "from zotero", "Tine comment = from zotero")
edit_md(lambda md: "\n".join(md.split("\n")[:hl().block.stop] + ["\t\t- tine-only memo"] + md.split("\n")[hl().block.stop:]))
step("E2. Tine-only memo")
ok(zget(key)["annotationComment"] == "from zotero", "memo not sent to Zotero")

# F. comment block deleted in Tine -> Zotero comment cleared, empty block comes back
edit_md(lambda md: "\n".join(l for i, l in enumerate(md.split("\n")) if not hl().cblock.start <= i < hl().cblock.stop))
step("F. comment block deleted in Tine")
step("F2. settle")
ok(zget(key)["annotationComment"] == "", "Zotero comment cleared")
ok(hl().cblock is not None and hl().comment == "" and "- tine-only memo" in MD.read_text(), "empty block back, memo kept")

# G. deleted in Tine -> deleted in Zotero
EDN.write_text(tine.edn_remove(EDN.read_text(), TID))
edit_md(lambda md: tine.md_remove(md, TID))
step("G. delete in Tine")
try:
    zget(key)
    ok(False, "Zotero item gone")
except ZoteroError as ex:
    ok("404" in str(ex), "Zotero item gone (404)")

# H. created then deleted in Zotero -> added (into the section) then removed in Tine
item = {k: v for k, v in zget(next(iter(json.loads((bridge.APP / "state.json").read_text())["docs"][att]["anns"]))).items()
        if k not in ("key", "version", "dateAdded", "dateModified", "relations")}
key2 = z.create(item | {"annotationText": "tzb zotero-side test", "annotationComment": "zc", "annotationColor": "#a28ae5", "tags": []})
step("H1. create in Zotero")
tid2 = str(uuid.uuid5(bridge.NS, key2))
e = {str(x[":id"]): x for x, _, _ in tine.edn_highlights(EDN.read_text())[0]}
ok(hl(tid2) and hl(tid2).comment == "zc" and hl(tid2).block.depth == 1 and e[tid2][":properties"][":color"] == "purple",
   "Tine has it in the section with comment and color")
ok(z.delete(key2, zget(key2)["version"]), "DELETE in Zotero")
step("H2. delete in Zotero")
ok(hl(tid2) is None and tid2 not in {str(x[":id"]) for x, _, _ in tine.edn_highlights(EDN.read_text())[0]},
   "Tine block and .edn entry removed")

step("I. settle")
ok(len(z.get_all("/items?itemType=annotation")[0]) == n0, f"Zotero back to {n0} annotations")
