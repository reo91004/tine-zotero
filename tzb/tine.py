"""Read a Tine graph: `assets/<name>.edn` highlight stores, `pages/hls__<name>.md` pages, open-PDF sessions."""
import glob
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- EDN (the subset Tine writes)

_WS = re.compile(r"(?:[\s,]|;[^\n]*)*")
_STR = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
_TOKEN = re.compile(r"[^\s,()\[\]{}\"]+")
_NUM = re.compile(r"[-+]?\d+(\.\d*)?([eE][-+]?\d+)?[NM]?$")
_CLOSE = {"(": ")", "[": "]", "{": "}", "#{": "}"}


class Keyword(str):
    """An EDN keyword such as `:id`; compares equal to its text so maps index as `m[":id"]`."""


def read_edn(s, i=0):
    """Parse one EDN form at `s[i:]`; return (value, end index).

    Maps -> dict, vectors -> list, lists/sets -> tuple, tagged literals (#uuid "…") -> the tagged value.
    """
    i = _WS.match(s, i).end()
    opener = "#{" if s.startswith("#{", i) else s[i]
    if opener in _CLOSE:
        i += len(opener)
        items = []
        while True:
            i = _WS.match(s, i).end()
            if s[i] == _CLOSE[opener]:
                break
            v, i = read_edn(s, i)
            items.append(v)
        i += 1
        if opener == "{":
            return dict(zip(items[::2], items[1::2])), i
        return (items if opener == "[" else tuple(items)), i
    if opener == '"':
        m = _STR.match(s, i)
        return json.JSONDecoder(strict=False).decode(f'"{m.group(1)}"'), m.end()
    if s.startswith("#_", i):
        _, i = read_edn(s, i + 2)
        return read_edn(s, i)
    if opener == "#":
        return read_edn(s, _TOKEN.match(s, i + 1).end())
    tok = _TOKEN.match(s, i).group()
    end = i + len(tok)
    if tok.startswith(":"):
        return Keyword(tok), end
    if _NUM.match(tok):
        t = tok.rstrip("NM")
        return (float(t) if any(c in t for c in ".eE") else int(t)), end
    return {"nil": None, "true": True, "false": False}.get(tok, tok), end


def edn_highlights(text):
    """Return ([(entry, start, end)], index of the closing "]") for the top-level `:highlights` vector of a Tine .edn."""
    i = _WS.match(text).end()
    if text[i] != "{":
        raise ValueError("Tine .edn must start with a map")
    i += 1
    while True:
        i = _WS.match(text, i).end()
        if text[i] == "}":
            raise ValueError("Tine .edn has no :highlights")
        k, i = read_edn(text, i)
        i = _WS.match(text, i).end()
        if k != ":highlights":
            _, i = read_edn(text, i)
            continue
        if text[i] != "[":
            raise ValueError(":highlights is not a vector")
        i += 1
        out = []
        while True:
            i = _WS.match(text, i).end()
            if text[i] == "]":
                return out, i
            v, j = read_edn(text, i)
            out.append((v, i, j))
            i = j


EMPTY_EDN = "{:highlights [] :extra {}}\n"


def _num(v):
    return repr(round(v, 4))


def edn_entry(uid, page, rects, width, height, text, color):
    """A Tine highlight map. `rects` are (x0, y0, x1, y1) in pdf.js viewport units at scale 1 on a width x height page."""
    box = lambda r: (f"{{:x1 {_num(r[0])} :y1 {_num(r[1])} :x2 {_num(r[2])} :y2 {_num(r[3])} "
                     f":width {_num(width)} :height {_num(height)}}}")
    bound = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))
    return (f'{{:id #uuid "{uid}" :page {page} :position {{:page {page} :bounding {box(bound)} '
            f':rects ({" ".join(map(box, rects))})}} :content {{:text {json.dumps(text, ensure_ascii=False)}}} '
            f':properties {{:color "{color}"}}}}')


def _find(text, uid):
    for e, s, t in edn_highlights(text)[0]:
        if str(e[":id"]) == uid:
            return s, t
    raise KeyError(uid)


def edn_add(text, entry):
    entries, close = edn_highlights(text)
    return text[:close] + (" " if entries else "") + entry + text[close:]


def edn_remove(text, uid):
    s, t = _find(text, uid)
    t = _WS.match(text, t).end()
    return text[:s] + text[t:]


def edn_set_color(text, uid, color):
    s, t = _find(text, uid)
    entry, n = re.subn(r'(:color\s+")[^"]*(")', rf"\g<1>{color}\g<2>", text[s:t], count=1)
    if not n:
        raise ValueError(f"highlight {uid} has no :color")
    return text[:s] + entry + text[t:]


# ---------------------------------------------------------------- Markdown outline (Logseq format)

_BLOCK = re.compile(r"^(\t*)-(?: (.*))?$")
_PROP = re.compile(r"^([A-Za-z0-9_-]+):: ?(.*)$")


@dataclass
class Block:
    depth: int
    start: int                  # line index of the "- " line
    lines: list[str]            # own lines, indentation removed
    children: list = field(default_factory=list)
    stop: int = 0               # exclusive end line index, descendants included

    @property
    def end(self):
        """Exclusive end line index of this block's own lines."""
        return self.start + len(self.lines)

    @property
    def props(self):
        return dict(m.groups() for l in self.lines[1:] if (m := _PROP.match(l)))

    @property
    def text(self):
        return "\n".join([self.lines[0]] + [l for l in self.lines[1:] if not _PROP.match(l)])

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def parse_page(md):
    """Parse a Logseq page into (page property lines, root blocks). Line indices refer to md.split("\\n")."""
    lines = md.split("\n")
    if lines[-1] == "":     # the file's final newline, not an empty line of the last block
        lines.pop()
    head, roots, stack = [], [], []
    for n, line in enumerate(lines):
        m = _BLOCK.match(line)
        if m:
            b = Block(len(m[1]), n, [m[2] or ""])
            while stack and stack[-1].depth >= b.depth:
                stack.pop()
            (stack[-1].children if stack else roots).append(b)
            stack.append(b)
        elif stack:
            b = stack[-1]
            ind = "\t" * b.depth + "  "
            b.lines.append(line[len(ind):] if line.startswith(ind) else line.lstrip())
        else:
            head.append(line)

    def close(b):
        for c in b.children:
            close(c)
        b.stop = b.children[-1].stop if b.children else b.end

    for b in roots:
        close(b)
    return head, roots


@dataclass
class PageHighlight:
    block: Block
    id: str
    zotero_key: str | None
    tags: set[str]
    cblock: Block | None        # the child block marked `zotero:: comment`, holding the Zotero comment
    comment: str                # its text ("" when there is no such block)


def _block(depth, text, props=()):
    """Lines of one block; `text` may span lines, props are (key, value) pairs."""
    ind = "\t" * depth
    first, *rest = text.split("\n")
    return [f"{ind}- {first}" if first else f"{ind}-"] + [f"{ind}  {l}" for l in rest] + [f"{ind}  {k}:: {v}" for k, v in props]


def comment_block(depth, comment):
    """The one block holding a highlight's Zotero comment (Zotero allows one comment per annotation)."""
    return _block(depth, comment, [("zotero", "comment")])


def highlight_block(text, page, color, uid, zkey, tags, comment):
    props = [("hl-page", page), ("hl-color", color), ("ls-type", "annotation"), ("id", uid), ("zotero-key", zkey)]
    if tags:
        props.append(("tags", ", ".join(tags)))
    return "\n".join(_block(0, text, props) + comment_block(1, comment))


def _locate(md, uid):
    lines = md.split("\n")
    h = page_highlights(parse_page(md)[1]).get(uid)
    if h is None:
        raise KeyError(uid)
    return lines, h


def md_append(md, block):
    return md.rstrip("\n") + "\n" + block + "\n"


def md_remove(md, uid):
    lines, h = _locate(md, uid)
    del lines[h.block.start:h.block.stop]
    return "\n".join(lines)


def _set_prop(lines, b, key, value):
    """Set, replace or (value None) remove a property line of block `b` in `lines` (edited in place)."""
    line = "\t" * b.depth + f"  {key}:: {value}"
    for n in range(b.start + 1, b.end):
        m = _PROP.match(lines[n].lstrip("\t").removeprefix("  "))
        if m and m[1] == key:
            if value is None:
                del lines[n]
            else:
                lines[n] = line
            return
    if value is not None:
        lines.insert(b.end, line)


def md_set_prop(md, uid, key, value):
    """Set, replace or (value None) remove a property line of a highlight block."""
    lines, h = _locate(md, uid)
    _set_prop(lines, h.block, key, value)
    return "\n".join(lines)


def md_set_comment(md, uid, comment):
    """Put `comment` in the highlight's comment block, creating it as the first child if missing.

    Only the block's text changes: its properties (such as `id::`) and any blocks nested under it stay.
    """
    lines, h = _locate(md, uid)
    c = h.cblock
    if c is None:
        lines[h.block.end:h.block.end] = comment_block(h.block.depth + 1, comment)
    else:
        props = [l for l in lines[c.start + 1:c.end] if _PROP.match(l.lstrip("\t").removeprefix("  "))]
        lines[c.start:c.end] = _block(c.depth, comment) + props
    return "\n".join(lines)


def is_highlight(b):
    return b.props.get("ls-type") == "annotation" and "id" in b.props


def md_add_conflict(md, uid, text, stamp):
    """Keep the Zotero side of a comment conflict as a Tine-only block after the comment block."""
    lines, h = _locate(md, uid)
    at = h.cblock.stop if h.cblock else h.block.end
    lines[at:at] = _block(h.block.depth + 1, text or "(empty)", [("zotero-conflict", stamp)])
    return "\n".join(lines)


def md_subtree(md, uid):
    lines, h = _locate(md, uid)
    return "\n".join(lines[h.block.start:h.block.stop])


def split_tags(value):
    return {t.strip().strip("#").removeprefix("[[").removesuffix("]]").strip() for t in value.split(",")} - {""}


def page_highlights(roots):
    """Map highlight id -> PageHighlight for every `ls-type:: annotation` block, at any depth."""
    out = {}
    for b in (x for r in roots for x in r.walk()):
        p = b.props
        if p.get("ls-type") != "annotation" or "id" not in p:
            continue
        c = next((c for c in b.children if c.props.get("zotero") == "comment"), None)
        out[p["id"]] = PageHighlight(b, p["id"], p.get("zotero-key"), split_tags(p.get("tags", "")), c, c.text if c else "")
    return out


# ---------------------------------------------------------------- Tine app state

def open_pdfs(graph: Path):
    """{PDF filename: [open time in epoch ms, or None]} for every tab showing a PDF in a saved Tine
    window or workspace of this graph. The open time comes from the viewId (`pdf-<base36 ms>-<n>`).

    Inactive workspaces count as open too; that only delays writes, which is the safe direction.
    """
    base = Path.home() / "Library/Application Support/page.tine.Tine/sessions"
    found = {}

    def visit(x):
        if isinstance(x, dict):
            hist, pos = x.get("history"), x.get("pos")
            if isinstance(hist, list) and isinstance(pos, int) and 0 <= pos < len(hist):
                cur = hist[pos]
                if isinstance(cur, dict) and cur.get("kind") == "pdf":
                    m = re.fullmatch(r"pdf-([0-9a-z]+)-\d+", str(cur.get("viewId")))
                    found.setdefault(cur.get("filename"), []).append(int(m[1], 36) if m else None)
            for v in x.values():
                visit(v)
        elif isinstance(x, list):
            for v in x:
                visit(v)

    for f in glob.glob(str(base / f"{glob.escape(graph.name)}-*.json")):
        visit(json.loads(Path(f).read_text()))
    return found
