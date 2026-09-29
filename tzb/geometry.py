"""Highlight rectangles: Zotero (PDF user space) <-> Tine (pdf.js viewport at scale 1, origin top-left).

M = transformation_matrix * rotation_matrix handles CropBox offsets and page rotation (checked in spike 4:
re-extracted text similarity 1.000 / 0.997 / 0.997 against Zotero's annotationText).
"""
import re

import pymupdf


def _m(page):
    return page.transformation_matrix * page.rotation_matrix


def to_tine(page, rects):
    """Zotero annotationPosition rects -> ([(x0, y0, x1, y1)], width, height) for a Tine .edn entry."""
    m, out = _m(page), []
    for r in rects:
        r = pymupdf.Rect(r) * m
        r.normalize()
        out.append(tuple(r))
    return out, page.rect.width, page.rect.height


def to_zotero(page, rects):
    """Tine rects [(x1, y1, x2, y2, width, height)] -> (PDF rects, one per text line; top of the first line).

    Tine stores several overlapping rects per line; Zotero expects one per line.
    """
    W, H = page.rect.width, page.rect.height
    view = [pymupdf.Rect(x1 * W / w, y1 * H / h, x2 * W / w, y2 * H / h) for x1, y1, x2, y2, w, h in rects]
    lines = []
    for r in sorted(view, key=lambda r: (r.y0, r.x0)):
        for i, L in enumerate(lines):
            if min(L.y1, r.y1) - max(L.y0, r.y0) > 0.5 * min(L.height, r.height):
                lines[i] = L | r      # not `L |= r`: that would not update the list element
                break
        else:
            lines.append(pymupdf.Rect(r))
    inv, out = ~_m(page), []
    for L in lines:
        r = L * inv
        r.normalize()
        out.append([round(v, 3) for v in r])
    return out, lines[0].y0


def sort_index(page, page_index, text, top):
    """Zotero annotationSortIndex `PPPPP|OOOOOO|TTTTT`; the offset is where the text starts in the page text."""
    first = re.sub(r"\s+", " ", text).strip()[:30]
    offset = max(re.sub(r"\s+", " ", page.get_text()).find(first), 0)
    return f"{page_index:05d}|{offset:06d}|{int(top):05d}"
