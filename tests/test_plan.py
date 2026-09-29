"""Self-checks for the sync planner and executor on throwaway graphs (no Zotero needed).
Run: uv run python tests/test_plan.py"""
import os
import tempfile
import uuid
from pathlib import Path

os.environ["TZB_HOME"] = tempfile.mkdtemp()     # before importing bridge: state and backups go to a temp folder

from tzb import bridge, tine, zotero
from tzb.bridge import (MISSING_MARK, NS, PDF_OPEN, TINE_HEX, Changed, Files, Sync, base_values, plan_doc,
                        unify_newlines)

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


def plan(g, zanns, sdoc, views=(), allow=False):
    return plan_doc(g, {"_size": 4}, "doc", zanns, sdoc, list(views), NOW, allow)


def uid(k):
    return str(uuid.uuid5(NS, k))


def rewrite(g, fname, fn):
    """Edit a Tine file as some other writer would, leaving it 'old' (not freshly edited)."""
    p = g / fname
    p.write_text(fn(p.read_text()))
    os.utime(p, (NOW / 1000 - 60, NOW / 1000 - 60))


def sdoc_for(zanns, **extra):
    return {"name": "doc", "anns": {k: base_values(z, str(uuid.uuid5(NS, k))) for k, z in zanns.items()}, **extra}


def ops(acts):
    return sorted((a.op, a.zkey) for a in acts)


def test_in_sync_is_quiet():
    zs = {"AAAA1111": zann("AAAA1111", comment="c")}
    acts, *_ = plan(graph(zs), zs, sdoc_for(zs))
    assert acts == []


def test_color_edit_in_tine_goes_to_zotero_unless_a_stale_reader_restored_it():
    zs = {"AAAA1111": zann("AAAA1111", color="green")}
    g = graph(zs, tine_colors={"AAAA1111": "yellow"})
    s = sdoc_for(zs)
    assert ops(plan(g, zs, s)[0]) == [("z_patch", "AAAA1111")]              # the user recolored in Tine
    s = sdoc_for(zs, written_at=NOW - 5_000)
    s["anns"]["AAAA1111"]["undo"] = {"color": "yellow"}                       # we wrote green over yellow
    acts, _, snap = plan(g, zs, s, views=[NOW - 10_000])                       # reader opened before that write
    assert ops(acts) == [("tine_color", "AAAA1111")] and acts[0].deferred and snap["old_reader"]
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


def test_edn_entry_gone_but_block_kept_is_re_added_not_deleted():
    zs = {"AAAA1111": zann("AAAA1111"), "BBBB2222": zann("BBBB2222")}
    g = graph(zs)
    edn = (g / "assets/doc.edn").read_text()
    (g / "assets/doc.edn").write_text(tine.edn_remove(edn, str(uuid.uuid5(NS, "AAAA1111"))))    # stale .edn save
    os.utime(g / "assets/doc.edn", (NOW / 1000 - 60, NOW / 1000 - 60))
    assert ops(plan(g, zs, sdoc_for(zs))[0]) == [("tine_add", "AAAA1111")]


def test_cr_newlines_are_quiet_after_first_write():
    zs = {"AAAA1111": unify_newlines(zann("AAAA1111", comment="Acrobat note\r- point one\r\nend"))}
    assert zs["AAAA1111"]["annotationComment"] == "Acrobat note\n- point one\nend"
    g = graph(zs)
    assert plan(g, zs, sdoc_for(zs))[0] == []


def test_edit_after_planning_is_not_overwritten():
    zs = {"AAAA1111": zann("AAAA1111", comment="zotero edit", version=9)}
    g = graph({"AAAA1111": zann("AAAA1111", comment="old")})
    acts, _, snap = plan(g, zs, sdoc_for({"AAAA1111": zann("AAAA1111", comment="old")}))
    assert [a.op for a in acts] == ["tine_comment"]
    md_p = g / "pages/hls__doc.md"
    md_p.write_text(md_p.read_text().replace("\t- old", "\t- typed in Tine meanwhile"))
    f = Files(g, "doc", snap)
    f.md = tine.md_set_comment(f.md, str(uuid.uuid5(NS, "AAAA1111")), "zotero edit")
    try:
        f.save()
        assert False, "overwrote an edit made after planning"
    except Changed:
        assert "typed in Tine meanwhile" in md_p.read_text()


def test_unrepresentable_comment_and_tags_are_left_alone():
    z = zann("AAAA1111", comment="Summary\nNote:: check eq. 3")
    z["tags"] = [{"tag": "Smith, J."}]
    g = graph({"AAAA1111": zann("AAAA1111")})
    acts = plan(g, {"AAAA1111": z}, sdoc_for({"AAAA1111": zann("AAAA1111")}))[0]
    assert [a.op for a in acts] == ["skip", "skip"]


def test_same_passage_twice_is_not_paired():
    zs = {"AAAA1111": zann("AAAA1111", text="same")}
    g = graph({"TINE0000": zann("TINE0000", text="same"), "TINE1111": zann("TINE1111", text="same")})
    md = (g / "pages/hls__doc.md").read_text().replace("  zotero-key:: TINE0000\n", "").replace("  zotero-key:: TINE1111\n", "")
    (g / "pages/hls__doc.md").write_text(md)
    os.utime(g / "pages/hls__doc.md", (NOW / 1000 - 60, NOW / 1000 - 60))
    assert sorted(a.op for a in plan(g, zs, sdoc_for({}))[0]) == ["tine_add", "z_create", "z_create"]


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
    assert len(plan(g, zs, sdoc_for(zs), allow=True)[0]) == 3


def test_both_edited_comment_tine_wins_and_keeps_zotero_text():
    zs = {"AAAA1111": zann("AAAA1111", comment="tine text")}
    g = graph(zs)
    s = sdoc_for({"AAAA1111": zann("AAAA1111", comment="old")})
    zs2 = {"AAAA1111": zann("AAAA1111", comment="zotero text", version=6)}
    acts, new, _ = plan(g, zs2, s)
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
    acts, *_ = plan(g, zs, {"anns": {}})
    assert ops(acts) == [("tine_color", "AAAA1111"), ("tine_comment", "AAAA1111")]


# ---- regressions from the v0.1.1 review

def test_missing_files_wait_with_the_pdf_open():
    """Recreating the files while tine_add waits made the next cycle read them as Tine deletions."""
    zs = {"AAAA1111": zann("AAAA1111"), "BBBB2222": zann("BBBB2222")}
    g = graph(zs)
    (g / "assets/doc.edn").unlink()
    (g / "pages/hls__doc.md").unlink()
    acts = plan(g, zs, sdoc_for(zs), views=[NOW - 1_000])[0]
    assert {a.op for a in acts} == {"init_files", "tine_add"} and all(a.deferred == PDF_OPEN for a in acts)


def run(g, zs, acts, new_base, snap, sdoc):
    state = {"docs": {"ATT": sdoc}}
    s = Sync(g, None, state, {})
    s.run_doc({"key": "ATT", "_path": str(g / "assets/doc.pdf"), "_parent": {}}, "doc", zs, acts, new_base, snap)
    return s, state["docs"]["ATT"]


def test_waiting_field_keeps_the_base_version():
    """A Zotero recolor waiting on an open PDF must still protect the highlight from a Tine delete."""
    old = {"AAAA1111": zann("AAAA1111")}
    zs = {"AAAA1111": zann("AAAA1111", color="green", version=6)}
    g = graph(old)
    sdoc = sdoc_for(old)
    acts, new_base, snap = plan(g, zs, sdoc, views=[NOW - 1_000])
    assert [a.op for a in acts] == ["tine_color"] and acts[0].deferred
    _, sdoc = run(g, zs, acts, new_base, snap, sdoc)
    assert sdoc["anns"]["AAAA1111"]["version"] == 5                    # not 6: Tine has not seen that edit
    g2 = graph(old, drop={"AAAA1111"})                                    # the user then deletes it in Tine
    assert ops(plan(g2, zs, sdoc)[0]) == [("tine_add", "AAAA1111")]     # edited in Zotero since: keep it


def test_re_added_entry_keeps_the_old_comment_base():
    """Re-adding a missing .edn entry must not make the stale block text look like a Tine edit."""
    g = graph({"AAAA1111": zann("AAAA1111", comment="old")})
    rewrite(g, "assets/doc.edn", lambda t: tine.edn_remove(t, uid("AAAA1111")))
    zs = {"AAAA1111": zann("AAAA1111", comment="new", version=9)}
    acts, new_base, _ = plan(g, zs, sdoc_for({"AAAA1111": zann("AAAA1111", comment="old")}))
    assert ops(acts) == [("tine_add", "AAAA1111")] and new_base["AAAA1111"]["comment"] == "old"
    g2 = graph({"AAAA1111": zann("AAAA1111", comment="old")})             # after the re-add
    s = {"name": "doc", "anns": {"AAAA1111": new_base["AAAA1111"]}}
    assert [(a.op, a.arg) for a in plan(g2, zs, s)[0]] == [("tine_comment", "new")]   # Zotero's edit wins, no revert


def test_unrepresentable_values_are_based_as_written():
    """add() writes "" / [] for what Tine cannot hold; the base must say so, or later Zotero edits get wiped."""
    z = zann("AAAA1111", comment="Summary\nNote:: eq. 3")
    z["tags"] = [{"tag": "Smith, J."}, {"tag": "ml"}]
    b = base_values(z, uid("AAAA1111"))
    assert b["comment"] == "" and b["tags"] == []
    g = graph({"AAAA1111": zann("AAAA1111")})                             # Tine: empty comment, no tags
    z2 = zann("AAAA1111", comment="Summary: see eq. 3", version=6)
    z2["tags"] = [{"tag": "ml"}]
    acts = plan(g, {"AAAA1111": z2}, {"name": "doc", "anns": {"AAAA1111": b}})[0]
    assert ops(acts) == [("tine_comment", "AAAA1111"), ("tine_tags", "AAAA1111")]    # nothing sent to Zotero


def test_tine_whitespace_matches_zotero_storage():
    """Zotero trims and NFC-normalizes; a Tine comment differing only by that is in sync (no endless PATCH)."""
    zs = {"AAAA1111": zann("AAAA1111", comment="hello")}
    g = graph(zs)
    rewrite(g, "pages/hls__doc.md", lambda t: t.replace("\t- hello", "\t- hello  \n\t  "))
    assert plan(g, zs, sdoc_for(zs))[0] == []


def test_conflict_halves_wait_together():
    """On a freshly saved page the Zotero overwrite waits with its zotero-conflict block."""
    g = graph({"AAAA1111": zann("AAAA1111", comment="tine text")})
    os.utime(g / "pages/hls__doc.md", (NOW / 1000 - 1, NOW / 1000 - 1))
    s = sdoc_for({"AAAA1111": zann("AAAA1111", comment="old")})
    acts = plan(g, {"AAAA1111": zann("AAAA1111", comment="zotero text", version=6)}, s)[0]
    assert sorted((a.op, a.deferred) for a in acts) == [("tine_conflict", "page edited seconds ago"),
                                                        ("z_patch", "page edited seconds ago")]


def test_lost_page_block_is_restored():
    zs = {"AAAA1111": zann("AAAA1111", comment="c")}
    g = graph(zs)
    rewrite(g, "pages/hls__doc.md", lambda t: tine.md_remove(t, uid("AAAA1111")))
    acts, new_base, _ = plan(g, zs, sdoc_for(zs))
    assert ops(acts) == [("tine_add", "AAAA1111")] and new_base["AAAA1111"]["comment"] == "c"


def test_save_checks_both_files_before_writing_either():
    zs = {"AAAA1111": zann("AAAA1111")}
    g = graph(zs)
    _, _, snap = plan(g, zs, sdoc_for(zs))
    f = Files(g, "doc", snap)
    f.md, f.edn = f.md + "- note\n", tine.edn_set_color(f.edn, uid("AAAA1111"), "red")
    rewrite(g, "assets/doc.edn", lambda t: t + " ")                      # the .edn moved on after planning
    md_before = (g / "pages/hls__doc.md").read_text()
    try:
        f.save()
        assert False
    except Changed:
        assert (g / "pages/hls__doc.md").read_text() == md_before        # nothing half-written


def test_paper_back_in_zotero_is_taken_over_again():
    zs = {"AAAA1111": zann("AAAA1111")}
    g = graph(zs)
    (g / "assets/doc.pdf").unlink()                                       # zotero_gone removed the clone
    rewrite(g, "pages/hls__doc.md", lambda t: MISSING_MARK + "\n" + t)
    ops_ = {a.op for a in plan(g, zs, None)[0]}
    assert {"adopt", "clone_pdf", "unmark_missing"} <= ops_


def test_old_reader_cannot_resurrect_a_zotero_delete():
    g = graph({"AAAA1111": zann("AAAA1111")})
    rewrite(g, "pages/hls__doc.md", lambda t: tine.md_remove(t, uid("AAAA1111")))     # we removed it...
    s = {"name": "doc", "anns": {}, "written_at": NOW - 5_000, "removed": {uid("AAAA1111"): NOW - 5_000}}
    acts = plan(g, {}, s, views=[NOW - 10_000])[0]                        # ...an older reader saved the entry back
    assert [a.op for a in acts] == ["tine_remove"]
    assert [a.op for a in plan(g, {}, {"name": "doc", "anns": {}})[0]] == ["z_create"]    # control: a new highlight


def test_area_placeholder_is_not_sent_as_text():
    g = graph({})
    rewrite(g, "assets/doc.edn", lambda t: tine.edn_add(t, tine.edn_entry("a1", 1, [(1, 2, 3, 4)], 10, 10, "[:span]", "red")))
    assert [a.op for a in plan(g, {}, {"name": "doc", "anns": {}})[0]] == ["skip"]


def test_run_doc_bookkeeping():
    old = {"AAAA1111": zann("AAAA1111")}
    zs = {"AAAA1111": zann("AAAA1111", color="green", version=6)}
    g = graph(old)
    sdoc = sdoc_for(old, written_at=NOW - 5_000)
    acts, nb, snap = plan(g, zs, sdoc, views=[NOW - 10_000])
    s, sdoc = run(g, zs, acts, nb, snap, sdoc)
    assert s.retry is None                           # waiting on an open PDF: its closing wakes us, no 3 s polling
    assert sdoc["stale"] is True                     # recorded, so it survives until the old reader is gone
    empty = graph({})                                # adding needs the PDF, and this one is not a real PDF
    s, _ = run(empty, zs, [bridge.Action("doc", "tine_add", "AAAA1111", uid("AAAA1111"))],
               {"AAAA1111": base_values(zs["AAAA1111"], uid("AAAA1111"))}, {}, sdoc_for({}))
    assert s.retry == "later" and any("Tine update failed" in m for m in s.problems)   # isolated and reported
    assert "AAAA1111" not in (empty / "assets/doc.edn").read_text()                     # nothing half-written


def test_other_annotation_types_are_skipped():
    class FakeZ:
        def versions(self, path):
            return {"IMG00001": 1, "HL000001": 1} if "annotation" in path else {}

        def by_keys(self, keys):
            items = {"IMG00001": {"key": "IMG00001", "version": 1, "annotationType": "image", "annotationComment": ""},
                     "HL000001": zann("HL000001") | {"annotationType": "highlight", "parentItem": "ATT"}}
            return [{"key": k, "data": items[k]} for k in keys]
    for c in bridge._CACHE.values():
        c.clear()
    pdfs, by_att, skipped = bridge.read_zotero(FakeZ())
    assert list(by_att["ATT"]) == ["HL000001"] and skipped == {"image": 1}


def test_watch_list_does_not_grow_with_the_library():
    assert len(bridge.watched(graph({}), {"docs": {}})) == 7


def test_one_time_zotero_key_is_refused():
    real = zotero._call
    zotero._call = lambda method, url, *a, **k: ((200, {"Zotero-Server-ID": "S"}, "") if method == "GET"
                                                 else (200, {}, {"key": "k", "remember": False}))
    try:
        zotero.authorize()
        assert False
    except zotero.ZoteroError as ex:
        assert "Always Allow" in str(ex)
    finally:
        zotero._call = real


# ---- regressions from the v0.1.2 review

def stale_color_case():
    """We wrote green over yellow; an old reader saved yellow back and has closed since."""
    old = {"AAAA1111": zann("AAAA1111", color="green", version=6)}
    g = graph(old, tine_colors={"AAAA1111": "yellow"})
    s = sdoc_for(old, written_at=NOW - 70_000, stale=True)
    s["anns"]["AAAA1111"]["undo"] = {"color": "yellow"}
    return old, g, s


def test_failed_write_keeps_the_stale_flag():
    zs, g, s = stale_color_case()
    acts, nb, snap = plan(g, zs, s)
    assert [a.op for a in acts] == ["tine_color"]                        # the old color is put back, not pushed
    rewrite(g, "pages/hls__doc.md", lambda t: t + "- typed\n")          # the write will fail with Changed
    _, s = run(g, zs, acts, nb, snap, s)
    assert s.get("stale") is True
    assert [a.op for a in plan(g, zs, s)[0]] == ["tine_color"]           # still not a z_patch of the stale color


def test_skipped_plan_keeps_the_stale_flag():
    zs, g, s = stale_color_case()
    rewrite(g, "pages/hls__doc.md", lambda t: t + t.split("\n", 1)[1])   # duplicate blocks: the doc is skipped
    acts, nb, snap = plan(g, zs, s)
    assert [a.op for a in acts] == ["skip"]
    _, s = run(g, zs, acts, nb, snap, s)
    assert s.get("stale") is True


def test_another_paper_does_not_take_over_a_missing_page():
    g = graph({"AAAA1111": zann("AAAA1111")})
    (g / "assets/doc.pdf").unlink()
    rewrite(g, "pages/hls__doc.md", lambda t: MISSING_MARK + "\n" + t)
    acts = plan(g, {"ZZZZ9999": zann("ZZZZ9999")}, None)[0]              # a new attachment reusing the name
    assert [a.op for a in acts] == ["skip"]


def test_comment_with_double_colons_is_text():
    zs = {"AAAA1111": zann("AAAA1111", comment="std::vector copies here")}
    g = graph(zs)
    assert tine.page_highlights(tine.parse_page((g / "pages/hls__doc.md").read_text())[1])[uid("AAAA1111")].comment \
        == "std::vector copies here"
    assert plan(g, zs, sdoc_for(zs))[0] == []


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
