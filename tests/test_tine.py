"""Self-checks for the Tine file readers and patchers. Run: uv run python tests/test_tine.py"""
from tzb.tine import (EMPTY_EDN, edn_add, edn_entry, edn_highlights, edn_remove, edn_set_color, highlight_block,
                      md_add_conflict, md_append, md_remove, md_set_comment, md_set_prop, page_highlights, parse_page)

U1, U2, U3 = (d * 8 + "-1111-1111-1111-111111111111" for d in "123")


def hl(md):
    return page_highlights(parse_page(md)[1])


def test_edn():
    e1 = edn_entry(U1, 1, [(10, 20, 30, 28), (10, 30, 25, 38)], 595.276, 841.89, 'say "hi"\nnext', "yellow")
    t = edn_add(EMPTY_EDN, e1)
    t = edn_add(t, edn_entry(U2, 2, [(1, 2, 3, 4)], 100, 200, "b", "red"))
    (a, s, _), (b, _, _) = edn_highlights(t)[0]
    assert a[":id"] == U1 and a[":content"][":text"] == 'say "hi"\nnext' and b[":properties"][":color"] == "red"
    assert a[":position"][":bounding"][":y2"] == 38 and len(a[":position"][":rects"]) == 2
    t = edn_set_color(t, U2, "blue")
    assert edn_highlights(t)[0][1][0][":properties"][":color"] == "blue"
    t = edn_remove(t, U1)
    assert [str(e[":id"]) for e, _, _ in edn_highlights(t)[0]] == [U2] and t.endswith("] :extra {}}\n")
    assert edn_highlights(edn_remove(t, U2))[0] == []


def test_md_roundtrip():
    md = md_append("file:: x.pdf\n", highlight_block("line one\nline two", 1, "yellow", U1, "ABCD2345", ["t1"], "c1\nc2"))
    md = md_append(md, highlight_block("other", 2, "red", U2, "EFGH2345", [], ""))
    h = hl(md)
    assert h[U1].block.text == "line one\nline two" and h[U1].zotero_key == "ABCD2345" and h[U1].tags == {"t1"}
    assert h[U1].comment == "c1\nc2"
    assert h[U2].comment == "" and h[U2].cblock is not None
    assert list(hl(md_remove(md, U1))) == [U2] and md_remove(md, U1).endswith("\n")


def test_comment_text_survives_exactly():
    for c in ["one line", "  lead and trail  ", "a\n\nb", "- dash\n#tag\n  - indented dash", ""]:
        md = md_append("x:: y", highlight_block("t", 1, "red", U1, "K", [], c))
        assert hl(md)[U1].comment == c, repr(c)
        assert hl(md_set_comment(md, U1, c + "!"))[U1].comment == c + "!"


def test_comment_update_keeps_id_and_notes():
    md = md_append("", highlight_block("text", 1, "yellow", U1, "K", [], "old"))
    lines = md.rstrip("\n").split("\n")
    lines += ["\t  id:: 99999999-9999-9999-9999-999999999999", "\t\t- note under the comment", "\t- tine memo"]
    md = "\n".join(lines) + "\n"
    assert hl(md)[U1].comment == "old"                                      # nested notes are not the comment
    md = md_set_comment(md, U1, "new\nsecond line")
    h = hl(md)[U1]
    assert h.comment == "new\nsecond line" and h.cblock.props["id"].startswith("9999")
    assert "\t\t- note under the comment" in md and "\t- tine memo" in md


def test_comment_block_recreated_first():
    md = md_append("", "- text\n  hl-page:: 1\n  ls-type:: annotation\n  id:: " + U1 + "\n\t- memo")
    assert hl(md)[U1].cblock is None and hl(md)[U1].comment == ""
    md = md_set_comment(md, U1, "z")
    assert hl(md)[U1].comment == "z" and md.index("zotero:: comment") < md.index("- memo")


def test_props_and_conflict():
    md = md_append("", highlight_block("text", 1, "yellow", U1, "K", [], "z"))
    md = md_set_prop(md, U1, "zotero-key", "NEWKEY23")
    md = md_set_prop(md, U1, "tags", "x, [[y]]")
    assert hl(md)[U1].zotero_key == "NEWKEY23" and hl(md)[U1].tags == {"x", "y"}
    assert hl(md_set_prop(md, U1, "tags", None))[U1].tags == set()
    md = md_add_conflict(md, U1, "zotero side", "2026-09-29")
    assert hl(md)[U1].comment == "z" and "zotero-conflict:: 2026-09-29" in md


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
