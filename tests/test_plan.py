"""Self-checks for the sync planner on a throwaway graph (no Zotero needed). Run: uv run python tests/test_plan.py"""
import os
import tempfile
import uuid
from pathlib import Path

from tzb import tine
from tzb.bridge import NS, TINE_HEX, base_values, plan_doc

NOW = 1_800_000_000_000


def zann(key, color="yellow", comment="", version=5, text=None):
    return {"key": key, "version": version, "annotationText": text or f"text {key}", "annotationComment": comment,
            "annotationColor": TINE_HEX[color], "tags": [],
            "annotationPosition": '{"pageIndex":0,"rects":[[1,2,3,4]]}', "annotationSortIndex": f"00000|000000|{version:05d}"}


def graph(zanns, tine_colors=None, drop=()):
    """A graph whose Tine files mirror `zanns` (optionally recolored or with highlights dropped), mtimes in the past."""
    g = Path(tempfile.mkdtemp())
    (g / "assets").mkdir()
    (g / "pages").mkdir()
    (g / "assets/doc.pdf").write_bytes(b"%PDF")
    edn, md = tine.EMPTY_EDN, "file:: x\n"
    for k, z in zanns.items():
        if k in drop:
            continue
        tid, color = str(uuid.uuid5(NS, k)), (tine_colors or {}).get(k, "yellow")
        edn = tine.edn_add(edn, tine.edn_entry(tid, 1, [(1, 2, 3, 4)], 10, 10, z["annotationText"], color))
        md = tine.md_append(md, tine.highlight_block(z["annotationText"], 1, color, tid, k, [], z["annotationComment"]))
    (g / "assets/doc.edn").write_text(edn)
    (g / "pages/hls__doc.md").write_text(md)
    for p in g.rglob("*.*"):
        os.utime(p, (NOW / 1000 - 60, NOW / 1000 - 60))
    return g


def plan(g, zanns, sdoc, views=()):
    return plan_doc(g, {"_size": 4}, "doc", zanns, sdoc, list(views), NOW)


def sdoc_for(zanns, **extra):
    return {"name": "doc", "anns": {k: base_values(z, str(uuid.uuid5(NS, k))) for k, z in zanns.items()}, **extra}


def ops(acts):
    return sorted((a.op, a.zkey) for a in acts)


def test_in_sync_is_quiet():
    zs = {"AAAA1111": zann("AAAA1111", comment="c")}
    acts, _ = plan(graph(zs), zs, sdoc_for(zs))
    assert acts == []


def test_color_edit_in_tine_goes_to_zotero_unless_a_stale_reader_restored_it():
    zs = {"AAAA1111": zann("AAAA1111", color="green")}
    g = graph(zs, tine_colors={"AAAA1111": "yellow"})
    s = sdoc_for(zs)
    assert ops(plan(g, zs, s)[0]) == [("z_patch", "AAAA1111")]              # the user recolored in Tine
    s = sdoc_for(zs, written_at=NOW - 5_000)
    s["anns"]["AAAA1111"]["undo"] = {"color": "yellow"}                       # we wrote green over yellow
    acts, _ = plan(g, zs, s, views=[NOW - 10_000])                            # reader opened before that write
    assert ops(acts) == [("tine_color", "AAAA1111")] and acts[0].deferred and s["stale"]
    s = sdoc_for(zs, written_at=NOW - 5_000)
    s["anns"]["AAAA1111"]["undo"] = {"color": "yellow"}
    assert ops(plan(g, zs, s, views=[NOW - 1_000])[0]) == [("z_patch", "AAAA1111")]   # opened after: trust it


def test_fresh_page_defers_only_tine_writes():
    zs = {"AAAA1111": zann("AAAA1111", color="green", comment="new z"), "BBBB2222": zann("BBBB2222", color="green")}
    g = graph({"AAAA1111": zann("AAAA1111"), "BBBB2222": zann("BBBB2222")}, tine_colors={"BBBB2222": "red"})
    os.utime(g / "pages/hls__doc.md", (NOW / 1000 - 1, NOW / 1000 - 1))                  # Tine saved 1 s ago
    s = sdoc_for({"AAAA1111": zann("AAAA1111"), "BBBB2222": zann("BBBB2222", color="green")})
    acts = {(a.op, a.zkey): a.deferred for a in plan(g, zs, s)[0]}
    assert acts[("z_patch", "BBBB2222")] == "" and acts[("tine_comment", "AAAA1111")] == "page edited seconds ago"
    s["wrote"] = {"hls__doc.md": (g / "pages/hls__doc.md").stat().st_mtime}                 # that save was ours
    assert all(not a.deferred for a in plan(g, zs, s)[0])


def test_missing_comment_block_is_restored_not_cleared():
    zs = {"AAAA1111": zann("AAAA1111", comment="keep me")}
    g = graph(zs)
    md = (g / "pages/hls__doc.md").read_text()
    h = tine.page_highlights(tine.parse_page(md)[1])[str(uuid.uuid5(NS, "AAAA1111"))]
    lines = md.split("\n")
    del lines[h.cblock.start:h.cblock.stop]                                                 # block gone
    (g / "pages/hls__doc.md").write_text("\n".join(lines))
    os.utime(g / "pages/hls__doc.md", (NOW / 1000 - 60, NOW / 1000 - 60))
    acts = plan(g, zs, sdoc_for(zs))[0]
    assert [(a.op, a.arg) for a in acts] == [("tine_comment", "keep me")]


def test_duplicate_blocks_block_all_writes():
    zs = {"AAAA1111": zann("AAAA1111", comment="c")}
    g = graph(zs)
    md = (g / "pages/hls__doc.md").read_text()
    (g / "pages/hls__doc.md").write_text(md + md.split("\n", 1)[1])                      # the block twice
    os.utime(g / "pages/hls__doc.md", (NOW / 1000 - 60, NOW / 1000 - 60))
    assert ops(plan(g, {"AAAA1111": zann("AAAA1111", comment="changed", version=9)}, sdoc_for(zs))[0]) == [("skip", "")]


def test_delete_in_tine():
    zs = {"AAAA1111": zann("AAAA1111"), "BBBB2222": zann("BBBB2222")}
    g = graph(zs, drop={"AAAA1111"})
    assert ops(plan(g, zs, sdoc_for(zs))[0]) == [("z_delete", "AAAA1111")]
    s = sdoc_for(zs)
    s["anns"]["AAAA1111"]["version"] = 3                                      # edited in Zotero since: keep it
    assert ops(plan(g, zs, s)[0]) == [("tine_add", "AAAA1111")]


def test_mass_delete_pauses():
    zs = {k: zann(k) for k in ("AAAA1111", "BBBB2222", "CCCC3333", "DDDD4444")}
    g = graph(zs, drop={"AAAA1111", "BBBB2222", "CCCC3333"})
    assert ops(plan(g, zs, sdoc_for(zs))[0]) == [("pause", "")]
    assert len(plan(g, zs, sdoc_for(zs, allow_mass_delete=True))[0]) == 3


def test_both_edited_comment_tine_wins_and_keeps_zotero_text():
    zs = {"AAAA1111": zann("AAAA1111", comment="tine text")}
    g = graph(zs)
    s = sdoc_for({"AAAA1111": zann("AAAA1111", comment="old")})
    zs2 = {"AAAA1111": zann("AAAA1111", comment="zotero text", version=6)}
    acts, new = plan(g, zs2, s)
    assert ops(acts) == [("tine_conflict", "AAAA1111"), ("z_patch", "AAAA1111")]
    assert new["AAAA1111"]["comment"] == "tine text"


def test_unlinked_pair_by_page_and_text():
    zs = {"AAAA1111": zann("AAAA1111", text="same passage")}
    g = graph({"TINE0000": zann("TINE0000", text="same passage")})           # Tine id is not uuid5(AAAA1111)
    md = (g / "pages/hls__doc.md").read_text().replace("  zotero-key:: TINE0000\n", "")
    (g / "pages/hls__doc.md").write_text(md)
    os.utime(g / "pages/hls__doc.md", (NOW / 1000 - 60, NOW / 1000 - 60))
    assert ops(plan(g, zs, sdoc_for({}))[0]) == [("tine_key", "AAAA1111")]


def test_first_sync_zotero_wins():
    zs = {"AAAA1111": zann("AAAA1111", color="green", comment="z")}
    g = graph({"AAAA1111": zann("AAAA1111", comment="t")}, tine_colors={"AAAA1111": "red"})
    acts, _ = plan(g, zs, {"anns": {}})
    assert ops(acts) == [("tine_color", "AAAA1111"), ("tine_comment", "AAAA1111")]


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
