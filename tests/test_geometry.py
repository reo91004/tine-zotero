"""Highlight rectangles land where the page is drawn, for every rotation with and without a CropBox offset.
Run: uv run python tests/test_geometry.py"""
import pymupdf

from tzb import geometry


def test_rects_match_the_rendered_page():
    for rot in (0, 90, 180, 270):
        for crop in (None, (30, 50, 500, 700)):                  # CropBox in PDF coordinates
            doc = pymupdf.open()
            page = doc.new_page(width=600, height=800)
            c = doc.get_new_xref()
            doc.update_object(c, "<<>>")
            doc.update_stream(c, b"0 0 0 rg 100 200 50 20 re f")  # a black box at PDF (100,200)-(150,220)
            doc.xref_set_key(page.xref, "Contents", f"{c} 0 R")
            page = doc[0]
            if crop:
                page.set_cropbox(pymupdf.Rect(crop[0], 800 - crop[3], crop[2], 800 - crop[1]))
            page.set_rotation(rot)
            pix = page.get_pixmap(dpi=72)                        # 72 dpi = pdf.js viewport at scale 1
            dark = [(x, y) for y in range(pix.height) for x in range(pix.width) if pix.pixel(x, y)[0] < 128]
            drawn = (min(x for x, _ in dark), min(y for _, y in dark), max(x for x, _ in dark) + 1,
                     max(y for _, y in dark) + 1)
            got, w, h = geometry.to_tine(page, [(100, 200, 150, 220)])
            assert tuple(round(v) for v in got[0]) == drawn and (w, h) == (pix.width, pix.height), (rot, crop)
            back, _ = geometry.to_zotero(page, [(*got[0], w, h)])
            assert [round(v) for v in back[0]] == [100, 200, 150, 220], (rot, crop)


def test_sort_index_is_never_negative():
    page = pymupdf.open().new_page()
    assert geometry.sort_index(page, 0, "x", -2.4) == "00000|000000|00000"


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
