"""tzb: two-way sync of PDF highlights and their comments, colors and tags between Zotero and a Tine graph."""
import argparse
import fcntl
import json
import os
import plistlib
import re
import select
import shutil
import subprocess
import sys
import time
import traceback
import unicodedata
import urllib.error
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pymupdf

from . import geometry, tine
from .zotero import Zotero, ZoteroAuthError, ZoteroError, authorize, server_id

APP = Path(os.environ.get("TZB_HOME") or Path.home() / "Library/Application Support/tine-zotero")
NS = uuid.UUID("5b0c6f7e-3d7a-4a53-9a57-2f3e0f6f1a10")   # Tine id of a Zotero-born highlight = uuid5(NS, key)
TINE_HEX = {"yellow": "#ffd400", "red": "#ff6666", "green": "#5fb236", "blue": "#2ea8e5", "purple": "#a28ae5"}
SF_DATALESS = 0x40000000        # st_flags bit: iCloud evicted the file's contents
STALE_WINDOW_MS = 60_000        # a reader opened before a bridge write appears in Tine's session file well within this
EDIT_QUIET_MS = 3_000           # no Tine writes to a page this fresh: Tine stops saving a page changed under unsaved edits
# Changes to what an open Tine reader rewrites from memory (the .edn, highlight blocks): they wait for the PDF to close.
READER_OPS = {"create_doc", "init_files", "clone_pdf", "tine_add", "tine_remove", "tine_color"}
PDF_OPEN = "PDF open in Tine"
MISSING_MARK = "zotero-missing:: true"
WHOLE_OPS = {"tine_add", "tine_remove", "z_delete"}      # on failure the whole old base entry stays
OP_FIELDS = {"tine_color": ("color", "tine_color"), "tine_comment": ("comment",), "tine_conflict": ("comment",),
             "tine_tags": ("tags",)}
PATCH_FIELDS = {"annotationColor": ("color", "tine_color"), "annotationComment": ("comment",), "tags": ("tags",)}


def nearest(hexc):
    rgb = lambda h: [int(h[i:i + 2], 16) for i in (1, 3, 5)]
    return min(TINE_HEX, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb(TINE_HEX[n]), rgb(hexc))))


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, obj, mode=0o644):
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup(tag, obj):
    """Keep what a sync is about to destroy: Zotero annotation deletes are permanent and skip Zotero's trash."""
    (APP / "trash").mkdir(parents=True, exist_ok=True)
    save_json(APP / "trash" / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{tag}.json", obj)


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def doc_paths(graph, name):
    return graph / f"assets/{name}.pdf", graph / f"assets/{name}.edn", graph / f"pages/hls__{name}.md"


def norm(s):
    return " ".join(s.split())


def comment_fits(text):
    """True if the comment survives as one Logseq block: no line of it may parse as a property."""
    return not any(tine.is_prop_line(l) for l in text.split("\n"))


def tags_fit(tags):
    return all(tine.split_tags(t) == {t} for t in tags)


def short(s, n=50):
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


@dataclass
class Action:
    doc: str
    op: str
    zkey: str = ""              # Zotero annotation key ("" for a Tine highlight not yet in Zotero)
    tid: str = ""               # Tine highlight id
    arg: object = None
    note: str = ""
    deferred: str = ""          # why it waits for a later cycle ("" = run now)

    def __str__(self):
        return (f"{self.doc:22} {self.op:12} {self.zkey or self.tid[:8]:8} {self.note}"
                + (f"  [waits: {self.deferred}]" if self.deferred else ""))


# ---------------------------------------------------------------- reading Zotero

# Item JSON cached for the life of the process and refreshed by version: a full attachment list costs Zotero ~5 ms
# of CPU per item, and `tzb run` reads the library on every change.
_CACHE = {"attachment": {}, "annotation": {}, "parents": {}}


def _items(z, item_type):
    """{key: data} for all items of a type, fetching only those whose version changed since the last call."""
    cache, vers = _CACHE[item_type], z.versions(f"/items?itemType={item_type}")
    for k in cache.keys() - vers.keys():
        del cache[k]
    for item in z.by_keys([k for k, v in vers.items() if k not in cache or cache[k]["version"] != v]):
        cache[item["key"]] = item["data"]
    return cache


def read_zotero(z):
    """-> (PDF attachment data list, {attachment key: {annotation key: data}}, skipped annotation types)."""
    by_att, skipped = defaultdict(dict), Counter()
    for d in _items(z, "annotation").values():
        if d["annotationType"] == "highlight":
            by_att[d["parentItem"]][d["key"]] = unify_newlines(dict(d))
        else:
            skipped[d["annotationType"]] += 1       # underline/image/ink/note/text stay Zotero-only (README "Limits")
    pdfs = [d for d in _items(z, "attachment").values()
            if d.get("contentType") == "application/pdf" and d.get("linkMode") in ("imported_file", "imported_url")]
    return pdfs, by_att, skipped


def parents_of(z, pdfs):
    """{parent key: data} for the given attachments (only new documents need their citekey and title)."""
    cache = _CACHE["parents"]
    want = {a["parentItem"] for a in pdfs if a.get("parentItem")} - cache.keys()
    for item in z.by_keys(want):
        cache[item["key"]] = item["data"]
    return cache


def unify_newlines(d):
    """\\r\\n and \\r become \\n in text and comment (PDF-imported notes often use \\r): Tine files only know \\n."""
    for f in ("annotationText", "annotationComment"):
        d[f] = d[f].replace("\r\n", "\n").replace("\r", "\n")
    return d


def assign_names(pdfs, parents, state):
    """Stable Tine names: citekey (or attachment key), with -2, -3 … for further PDFs; kept in state once used.

    Unique ignoring case and Unicode normalization, as APFS file names are.
    """
    fold = lambda n: unicodedata.normalize("NFC", n).casefold()
    taken = {fold(d["name"]) for d in state["docs"].values()}
    names = {k: d["name"] for k, d in state["docs"].items()}
    for a in sorted(pdfs, key=lambda a: (a["dateAdded"], a["key"])):
        if a["key"] in names:
            continue
        key = parents.get(a.get("parentItem"), {}).get("citationKey") or a["key"]
        base = re.sub(r"[^\w.-]", "_", unicodedata.normalize("NFC", key))
        name, n = base, 1
        while fold(name) in taken:
            n += 1
            name = f"{base}-{n}"
        taken.add(fold(name))
        names[a["key"]] = name
    return names


# ---------------------------------------------------------------- planning

def base_values(z, tid):
    """The synced base for Zotero item `z`: exactly what add() writes to Tine, so a comment or tags Tine cannot hold
    are recorded as the empty values Tine actually has."""
    tags = sorted(t["tag"] for t in z["tags"])
    return {"id": tid, "version": z["version"], "color": z["annotationColor"], "tine_color": nearest(z["annotationColor"]),
            "comment": z["annotationComment"] if comment_fits(z["annotationComment"]) else "",
            "tags": tags if tags_fit(tags) else []}


def plan_doc(graph, att, name, zanns, sdoc, views, now, allow_mass_delete=False):
    """Compare Zotero, Tine and the last synced base for one PDF. Pure: it changes nothing.

    Returns (actions, new base {Zotero key: values, or None to forget}, snap). snap holds the file texts it read
    ("edn", "md"; writes later check the files against them, so an edit made after planning is never overwritten)
    and what run_doc must record: "old_reader" (a reader opened before our last write is open) and "keep_undo".
    `views` holds the open times of Tine readers showing this PDF. A base field that is None is unknown, and the
    Zotero value wins it.
    """
    out, new_base, snap = [], {}, {}

    pdf, edn_p, md_p = doc_paths(graph, name)
    files = [p for p in (pdf, edn_p, md_p) if p.exists()]
    # Tine writes atomically, so reading a fresh file is safe; only our writes to it must wait. Our own last
    # write (its mtime is kept in state) is not the user typing.
    ours = (sdoc or {}).get("wrote", {})
    fresh = any(p.stat().st_mtime * 1000 > now - EDIT_QUIET_MS and p.stat().st_mtime != ours.get(p.name)
                for p in files if p != pdf)

    def act(op, zkey="", tid="", arg=None, note=""):
        why = (PDF_OPEN if op in READER_OPS and views else
               "page edited seconds ago" if fresh and not op.startswith("z_") and op != "adopt" else "")
        out.append(Action(name, op, zkey, tid, arg, note, why))

    # An evicted PDF clone does not matter (geometry reads Zotero's own file); evicted Tine files cannot be read.
    if any(p.stat().st_flags & SF_DATALESS for p in files if p != pdf):
        return [Action(name, "skip", note="iCloud has not downloaded these files yet")], {}, snap
    snap["edn"] = edn_p.read_text() if edn_p.exists() else None
    snap["md"] = md_p.read_text() if md_p.exists() else None
    if sdoc is None:
        if not files:
            act("create_doc", note=f"new: {len(zanns)} highlights")
            return out, {k: base_values(d, str(uuid.uuid5(NS, k))) for k, d in zanns.items()}, snap
        # Our own page for a paper that left Zotero (zotero_gone removed its PDF) and is back: take it over again,
        # but only if its highlights are this attachment's (another paper reusing the name must not wipe them).
        keys = {h.zotero_key for h in tine.page_highlights(tine.parse_page(snap["md"] or "")[1]).values() if h.zotero_key}
        returning = not pdf.exists() and MISSING_MARK in (snap["md"] or "") and keys <= zanns.keys()
        if not returning and not (pdf.exists() and pdf.stat().st_size == att["_size"]):
            return [Action(name, "skip", note="name taken by a file that is not this Zotero PDF")], {}, snap
        act("adopt", note="existing Tine files: where they differ, Zotero wins")
        sdoc = {"anns": {}}

    edn_text, md = snap["edn"] or tine.EMPTY_EDN, snap["md"] or ""
    hls, roots = tine.edn_highlights(edn_text)[0], tine.parse_page(md)[1]
    edn_ids = [str(e[":id"]) for e, _, _ in hls]
    md_ids = [b.props["id"] for r in roots for b in r.walk() if tine.is_highlight(b)]
    if len(edn_ids) != len(set(edn_ids)) or len(md_ids) != len(set(md_ids)):
        # Two blocks claiming one highlight (seen when a Tine reader re-created blocks it could not find):
        # which one holds the user's comment is unknowable, so write nothing on either side.
        return [Action(name, "skip", note="duplicate highlight blocks in the page or .edn: fix by hand")], {}, snap
    entries = {str(e[":id"]): e for e, _, _ in hls}
    page = tine.page_highlights(roots)
    missing = not (edn_p.exists() and md_p.exists())
    if missing:
        act("init_files", note="Tine files missing: recreate them (never read as deletions)")
    if not pdf.exists() or pdf.stat().st_size != att["_size"]:
        act("clone_pdf", note="PDF missing or changed in Zotero")
    if MISSING_MARK in md:
        act("unmark_missing", note="back in Zotero")

    # A reader opened before our last write may still hold the old state and save it back over our write.
    written = sdoc.get("written_at")
    snap["old_reader"] = bool(written) and any(t is None or t < written for t in views)
    stale = snap["old_reader"] or sdoc.get("stale", False)
    snap["keep_undo"] = keep_undo = stale or not written or now - written <= STALE_WINDOW_MS

    base, lines = sdoc["anns"], md.split("\n")
    by_md_key = {h.zotero_key: i for i, h in page.items() if h.zotero_key}
    tid = {k: base[k]["id"] if k in base else by_md_key.get(k) for k in set(zanns) | set(base)}
    # Pair highlights that neither side has linked yet by page and text: a Tine highlight we created in Zotero
    # whose response was lost, or the same passage highlighted in both apps.
    # Only a page+text found once on each side pairs: the same passage highlighted twice stays unlinked.
    taken = set(tid.values())
    t_sig = {i: (e[":page"], norm(e.get(":content", {}).get(":text", ""))) for i, e in entries.items()
             if i not in taken and not (page.get(i) and page[i].zotero_key)}
    z_sig = {k: (json.loads(zanns[k]["annotationPosition"])["pageIndex"] + 1, norm(zanns[k]["annotationText"]))
             for k, i in tid.items() if i is None}
    t_count, z_count = Counter(t_sig.values()), Counter(z_sig.values())
    loose = {sig: i for i, sig in t_sig.items() if t_count[sig] == 1}
    for k in sorted(z_sig):
        own, sig = str(uuid.uuid5(NS, k)), z_sig[k]
        tid[k] = own if own in entries else loose.pop(sig, own) if z_count[sig] == 1 else own
    tine_deleted = []
    for k in sorted(tid, key=lambda k: (zanns[k]["annotationSortIndex"] if k in zanns else "", k)):   # PDF order
        z, b, i = zanns.get(k), base.get(k), tid[k]
        e, h = entries.get(i), page.get(i)
        undo = b.get("undo", {}) if b and stale else {}
        if z and not e:
            # A real Tine delete removes the .edn entry and the md block together (spike 3). An entry missing while
            # its block is still on the page is a stale save (another device's reader, an older iCloud copy): re-add.
            if b is None or missing or h or undo.get("present") is False or z["version"] > b["version"]:
                act("tine_add", k, i, note=short(z["annotationText"]))
                new_base[k] = base_values(z, i)
                if h:   # add() keeps that block as it is: merge its comment and tags against the old base next cycle
                    new_base[k]["comment"], new_base[k]["tags"] = (b.get("comment"), b.get("tags")) if b else (None, None)
            else:
                tine_deleted.append(k)
                act("z_delete", k, i, z["version"], "deleted in Tine: " + short(z["annotationText"]))
                new_base[k] = None
            continue
        if not z:
            if e:
                act("tine_remove", k, i, note="deleted in Zotero")
            new_base[k] = None
            continue
        # A page block lost while its .edn entry stays (page deleted or recreated, or a half-applied write) is
        # never rewritten by Tine; add() puts it back. Not for Tine-born ids: their block may simply not be saved yet.
        restore = h is None and (missing or i == str(uuid.uuid5(NS, k)))
        if restore:
            act("tine_add", k, i, note="restore the page block")
        new_base[k] = plan_fields(act, k, i, z, b, e, h, undo, keep_undo)
        if restore:
            bv = base_values(z, i)
            new_base[k]["comment"], new_base[k]["tags"] = bv["comment"], bv["tags"]
        if h:
            new_base[k]["md"] = "\n".join(lines[h.block.start:h.block.stop])

    paired = set(tid.values())
    removed = sdoc.get("removed", {}) if stale else {}
    for i, e in entries.items():
        if i in paired:
            continue
        h = page.get(i)
        content = e.get(":content", {})
        text = content.get(":text")
        if h and h.zotero_key:      # synced once, but neither Zotero nor our state knows it any more
            act("tine_remove", h.zotero_key, i, note="deleted in Zotero")
        elif i in removed:          # a reader opened before we removed it saved it back
            act("tine_remove", tid=i, note="deleted in Zotero; an old Tine reader saved it back")
        elif not text or text == "[:span]" or ":image" in content or (h and h.block.props.get("hl-type") == "area"):
            out.append(Action(name, "skip", tid=i, note="area highlight: not synced"))
        else:
            act("z_create", tid=i, note="new in Tine: " + short(text))

    # Both halves of a comment conflict wait together: the Zotero text must reach the page before it is overwritten.
    held = {a.zkey: a.deferred for a in out if a.op == "tine_conflict" and a.deferred}
    for a in out:
        if a.op == "z_patch" and a.zkey in held:
            a.deferred = held[a.zkey]

    if len(tine_deleted) >= 3 and 2 * len(tine_deleted) >= len(base) and not allow_mass_delete:
        return [Action(name, "pause", note=f"{len(tine_deleted)} of {len(base)} highlights vanished from Tine; "
                                           f"check, then `tzb resume {name}`")], {}, snap
    return out, new_base, snap


def plan_fields(act, k, i, z, b, e, h, undo, keep_undo):
    """Three-way merge of color, comment and tags for a highlight present on both sides; returns the new base."""
    known = lambda f: b is not None and b.get(f) is not None
    patch, notes, res = {}, [], base_values(z, i)
    if b and "undo" in b and keep_undo:
        res["undo"] = b["undo"]

    zc, tc = z["annotationColor"], e.get(":properties", {}).get(":color")
    if nearest(zc) != tc:
        t_changed = known("color") and tc != b["tine_color"] and undo.get("color") != tc and tc in TINE_HEX
        if t_changed and zc == b["color"]:
            patch["annotationColor"] = res["color"] = TINE_HEX[tc]
            res["tine_color"] = tc
            notes.append(f"color {tc}")
        else:       # Zotero changed, both changed, first sync, or a stale reader put the old color back
            act("tine_color", k, i, nearest(zc), f"{tc} -> {nearest(zc)}")

    if h is None:       # no page block (yet): comment and tags wait for a later cycle
        res["comment"], res["tags"] = (b.get("comment"), b.get("tags")) if b else (None, None)
        if patch:
            act("z_patch", k, i, (patch, z["version"]), "; ".join(notes))
        return res

    if h.zotero_key != k:
        act("tine_key", k, i, note="record zotero-key")
    zt, tt = z["annotationComment"], h.comment
    if not comment_fits(zt):
        act("skip", k, i, note="comment has a `key:: value` line that Tine would read as a property: not synced")
        res["comment"] = b.get("comment") if b else None
    elif h.cblock is None:
        # A missing comment block is never read as "comment deleted": Tine rewriting a page can drop it.
        # Put Zotero's comment back; clearing a comment means emptying the block's text.
        act("tine_comment", k, i, zt, "restore the comment block")
    elif zt != tt:
        t_changed = known("comment") and tt != b["comment"]
        if t_changed:
            patch["annotationComment"] = res["comment"] = tt
            notes.append("comment " + (short(tt) or "cleared"))
            if zt != b["comment"]:
                act("tine_conflict", k, i, zt, "both edited: Tine wins, Zotero text kept as zotero-conflict")
        else:
            act("tine_comment", k, i, zt, short(zt) or "(cleared)")

    zs, ts = {t["tag"] for t in z["tags"]}, h.tags
    if not tags_fit(zs):
        act("skip", k, i, note="a tag has a comma, # or [[ ]] that a `tags::` line cannot hold: tags not synced")
        res["tags"] = b.get("tags") if b else None
    elif zs != ts:
        if known("tags"):
            bs = set(b["tags"])
            merged = (bs | (zs - bs) | (ts - bs)) - (bs - zs) - (bs - ts)
        else:
            merged = zs
        if merged != ts:
            act("tine_tags", k, i, sorted(merged), ", ".join(sorted(merged)) or "(none)")
        if merged != zs:
            patch["tags"] = [t for t in z["tags"] if t["tag"] in merged] + [{"tag": t} for t in sorted(merged - zs)]
            notes.append("tags " + (", ".join(sorted(merged)) or "(none)"))
        res["tags"] = sorted(merged)
    if patch:
        act("z_patch", k, i, (patch, z["version"]), "; ".join(notes))
    return res


# ---------------------------------------------------------------- writing

class Changed(Exception):
    """A Tine file changed between our read and our write."""


class Files:
    """The Tine files of one document, edited in memory and written back once with compare-and-swap."""

    def __init__(self, graph, name, snap=None):
        """`snap`: the texts the plan was made from; the write fails with Changed if the files moved on since."""
        self.pdf, self.edn_p, self.md_p = doc_paths(graph, name)
        snap = snap or {}
        self.edn0 = snap["edn"] if "edn" in snap else (self.edn_p.read_text() if self.edn_p.exists() else None)
        self.md0 = snap["md"] if "md" in snap else (self.md_p.read_text() if self.md_p.exists() else None)
        self.edn, self.md = self.edn0, self.md0

    def entries(self):
        return {str(e[":id"]): (e, s, t) for e, s, t in tine.edn_highlights(self.edn)[0]}

    def highlights(self):
        return tine.page_highlights(tine.parse_page(self.md)[1])

    def save(self):
        """Write the changed files if all of them still hold what the plan read; returns {file name: mtime} written.

        Both are checked before either is replaced, and the page goes first: if the .edn moves on in between,
        the page has the block and the next cycle re-adds the missing .edn entry.
        """
        current = lambda p: p.read_text() if p.exists() else None
        todo = [(p, o, n) for p, o, n in ((self.md_p, self.md0, self.md), (self.edn_p, self.edn0, self.edn))
                if n is not None and n != o]
        moved = [p.name for p, o, _ in todo if current(p) != o]
        if moved:
            raise Changed(", ".join(moved))
        wrote = {}
        for path, old, new in todo:
            tmp = path.with_name(f".tzb-{path.name}.tmp")
            tmp.write_text(new)
            if current(path) != old:
                tmp.unlink()
                raise Changed(path.name)
            os.replace(tmp, path)
            wrote[path.name] = path.stat().st_mtime
        return wrote


class LazyPDF:
    """Zotero's copy of the PDF, opened on first use (most cycles need no geometry)."""

    def __init__(self, path):
        self.path, self.doc = path, None

    def __getitem__(self, i):
        if self.doc is None:
            self.doc = pymupdf.open(self.path)
        return self.doc[i]


def clone(src, dst):
    """APFS clone (no extra disk, separate inode, so iCloud never touches Zotero's file); plain copy elsewhere."""
    tmp = dst.with_name(f".tzb-{dst.name}.tmp")
    if subprocess.run(["cp", "-c", src, tmp], capture_output=True).returncode != 0:
        shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def page_header(name, att):
    lines = [f"file:: [{name}.pdf](../assets/{name}.pdf)", f"file-path:: ../assets/{name}.pdf",
             f"alias:: {name}", "icon:: 📄"]       # alias: link the paper as [[name]] instead of [[hls__name]]
    if att.get("parentItem"):
        title = re.sub(r"[\[\]\n]", " ", att["_parent"].get("title", "")) or att["parentItem"]
        lines.append(f"zotero-item:: [{title}](zotero://select/library/items/{att['parentItem']})")
    return "\n".join(lines) + "\n"


def notify(msg):
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    'display notification (item 1 of argv) with title "tine-zotero"', "-e", "end run", msg],
                   capture_output=True)


class Sync:
    def __init__(self, graph, z, state, cfg):
        self.graph, self.z, self.state = graph, z, state
        # A block for the user's own notes above the highlights: Tine appends new highlights at the page end.
        self.notes = cfg.get("notes_heading", "## Notes")
        self.retry = None       # "soon": look again in 3 s (a page is being edited); "later": something failed
        self.problems = {}      # message -> notify?; sync() logs a message only when it is new

    def save_state(self):
        save_json(APP / "state.json", self.state)

    def report(self, msg, notify=False):
        self.problems[msg] = self.problems.get(msg, False) or notify

    def failed(self, soon=False):
        self.retry = "soon" if soon or self.retry == "soon" else "later"

    def run_doc(self, att, name, zanns, acts, new_base, snap):
        docs = self.state["docs"]
        for a in acts:
            if a.op in ("skip", "pause"):
                self.report(str(a), notify=a.op == "pause")
            elif a.deferred and a.deferred != PDF_OPEN:
                self.failed(soon=True)      # the page is quiet 3 s after the last save; a closing PDF wakes us itself
        live = [a for a in acts if not a.deferred and a.op not in ("skip", "pause")]
        if any(a.op in ("create_doc", "adopt") for a in live):
            docs[att["key"]] = {"name": name, "src": att["_path"], "anns": {}}
        sdoc = docs.get(att["key"])
        if sdoc is None:            # a new document whose creation waits (its PDF shows in a Tine tab)
            return
        before = json.dumps(sdoc, sort_keys=True)
        blocked = defaultdict(set)  # Zotero key -> base fields that keep their old value ("*": the whole entry)

        def block(a):
            if a.op in WHOLE_OPS:
                blocked[a.zkey].add("*")
            elif a.op == "z_patch":
                blocked[a.zkey].update(x for p in a.arg[0] for x in PATCH_FIELDS[p])
            else:
                blocked[a.zkey].update(OP_FIELDS.get(a.op, ()))

        for a in acts:
            if a.deferred:
                block(a)
        applied = self.apply(att, name, zanns, live, new_base, snap, sdoc, block) if live else True

        if snap.get("old_reader"):
            sdoc["stale"] = True
        elif "old_reader" in snap and applied and not any(a.deferred for a in acts):
            sdoc.pop("stale", None)     # the old reader is gone and what it saved is now reconciled
        if not snap.get("keep_undo", True):
            sdoc.pop("removed", None)
        anns = sdoc["anns"]
        for k, nb in new_base.items():
            kept = blocked.get(k, set())
            if "*" in kept:
                continue
            if nb is None:
                anns.pop(k, None)
                continue
            if kept:    # a field still waits: the version must not claim Tine has seen this Zotero edit
                nb["version"] = anns.get(k, {}).get("version", 0)
                for field in kept:
                    nb[field] = anns.get(k, {}).get(field)
            anns[k] = nb
        if json.dumps(sdoc, sort_keys=True) != before:
            self.save_state()

    def apply(self, att, name, zanns, live, new_base, snap, sdoc, block):
        f, pdfdoc = Files(self.graph, name, snap), LazyPDF(att["_path"])
        # A comment conflict overwrites Zotero only after its zotero-conflict block is safely on the page.
        late = {a.zkey for a in live if a.op == "tine_conflict"}
        zops = [a for a in live if a.op.startswith("z_")]
        tine_ops = [a for a in live if not a.op.startswith("z_") and a.op != "adopt"]
        for a in zops:                              # Zotero first: new keys feed the Tine edits
            if not (a.op == "z_patch" and a.zkey in late):
                self.run_z(att, f, pdfdoc, zanns, sdoc, new_base, a, block)
        try:
            for a in tine_ops:
                getattr(self, a.op)(att, f, pdfdoc, zanns, sdoc, new_base, a)
            sdoc.setdefault("wrote", {}).update(f.save())
        except Exception as ex:
            if isinstance(ex, Changed):
                self.report(f"{name}: {ex} changed while syncing; retrying")
                self.failed(soon=True)
            else:
                self.report(f"{name}: Tine update failed: {ex!r}")
                self.failed()
            for a in tine_ops + [a for a in zops if a.op == "z_patch" and a.zkey in late]:
                block(a)
            return False
        for a in tine_ops + [a for a in live if a.op == "adopt"]:
            log(a)
        if any(a.op in READER_OPS for a in tine_ops):
            sdoc["written_at"] = time.time() * 1000     # after the write: a reader opened before it holds old files
        for a in zops:
            if a.op == "z_patch" and a.zkey in late:
                self.run_z(att, f, pdfdoc, zanns, sdoc, new_base, a, block)
        return True

    def run_z(self, att, f, pdfdoc, zanns, sdoc, new_base, a, block):
        try:
            getattr(self, a.op)(att, f, pdfdoc, zanns, sdoc, new_base, a)
            log(a)
        except Exception as ex:
            block(a)
            if isinstance(ex, ZoteroAuthError):
                self.report("Zotero rejected the write key: run `tzb init <graph>` and click Always Allow", True)
            else:
                self.report(f"{a}  FAILED: {ex!r}")
            self.failed()

    # --- document operations

    def create_doc(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        clone(att["_path"], f.pdf)
        f.edn, f.md = tine.EMPTY_EDN, page_header(a.doc, att) + self.notes_block()
        for k, z in sorted(zanns.items(), key=lambda kv: kv[1]["annotationSortIndex"]):
            self.add(f, pdfdoc, z, str(uuid.uuid5(NS, k)))

    def init_files(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        f.edn = f.edn if f.edn is not None else tine.EMPTY_EDN
        f.md = f.md if f.md is not None else page_header(a.doc, att) + self.notes_block()

    def clone_pdf(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        clone(att["_path"], f.pdf)
        sdoc["src"] = att["_path"]

    def zotero_gone(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        f.pdf.unlink(missing_ok=True)
        if f.md is not None and MISSING_MARK not in f.md:
            f.md = MISSING_MARK + "\n" + f.md

    def unmark_missing(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        f.md = "\n".join(l for l in f.md.split("\n") if l != MISSING_MARK)

    def notes_block(self):
        """The user's notes heading with an empty block to type in, then a rule above the highlights."""
        return f"- {self.notes}\n\t-\n- ---\n"

    # --- Tine operations

    def add(self, f, pdfdoc, z, tid):
        """Make sure the Zotero highlight `z` exists in the .edn and the page as Tine highlight `tid`."""
        pos = json.loads(z["annotationPosition"])
        pg, color = pos["pageIndex"] + 1, nearest(z["annotationColor"])
        if tid not in f.entries():
            rects, w, h = geometry.to_tine(pdfdoc[pos["pageIndex"]], pos["rects"])
            f.edn = tine.edn_add(f.edn, tine.edn_entry(tid, pg, rects, w, h, z["annotationText"], color))
        if tid not in f.highlights():
            bv = base_values(z, tid)        # the same comment and tags the base records
            f.md = tine.md_append(f.md, tine.highlight_block(z["annotationText"], pg, color, tid, z["key"],
                                                             bv["tags"], bv["comment"]))

    def tine_add(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        was_there = a.tid in f.entries()
        self.add(f, pdfdoc, zanns[a.zkey], a.tid)
        if not was_there:
            new_base[a.zkey]["undo"] = {"present": False}

    def tine_remove(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        entries, page = f.entries(), f.highlights()
        h = page.get(a.tid)
        snap = {"zotero_key": a.zkey, "reason": "deleted in Zotero"}
        if a.tid in entries:
            _, s, t = entries[a.tid]
            snap["edn"] = f.edn[s:t]
            f.edn = tine.edn_remove(f.edn, a.tid)
            # Remember it for a while: a reader opened before this write may save it back (see plan_doc).
            sdoc.setdefault("removed", {})[a.tid] = time.time() * 1000
        if h:
            snap["md"] = tine.md_subtree(f.md, a.tid)
            notes = [c for c in h.block.children if c is not h.cblock]
            if notes or (h.cblock and h.cblock.children):
                f.md = tine.md_set_prop(f.md, a.tid, "zotero-deleted", f"{datetime.now():%Y-%m-%d}")
            else:
                f.md = tine.md_remove(f.md, a.tid)
        backup(a.zkey or a.tid, snap)

    def tine_color(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        old = f.entries()[a.tid][0].get(":properties", {}).get(":color")
        f.edn = tine.edn_set_color(f.edn, a.tid, a.arg)
        if a.tid in f.highlights():
            f.md = tine.md_set_prop(f.md, a.tid, "hl-color", a.arg)
        new_base[a.zkey]["undo"] = new_base[a.zkey].get("undo", {}) | {"color": old}

    def tine_comment(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        h = f.highlights()[a.tid]
        if h.comment and h.comment != a.arg:
            backup(a.zkey, {"reason": "Tine comment overwritten from Zotero", "md": tine.md_subtree(f.md, a.tid)})
        f.md = tine.md_set_comment(f.md, a.tid, a.arg)

    def tine_conflict(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        f.md = tine.md_add_conflict(f.md, a.tid, a.arg, f"{datetime.now():%Y-%m-%d %H:%M}")

    def tine_tags(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        if f.highlights()[a.tid].tags - set(a.arg):
            backup(a.zkey, {"reason": "Tine tags overwritten from Zotero", "md": tine.md_subtree(f.md, a.tid)})
        f.md = tine.md_set_prop(f.md, a.tid, "tags", ", ".join(a.arg) or None)

    def tine_key(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        f.md = tine.md_set_prop(f.md, a.tid, "zotero-key", a.zkey)

    # --- Zotero operations

    def z_create(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        e = f.entries()[a.tid][0]
        h = f.highlights().get(a.tid)
        pg, text = e[":page"], e[":content"][":text"]
        page = pdfdoc[pg - 1]
        rects = [(r[":x1"], r[":y1"], r[":x2"], r[":y2"], r[":width"], r[":height"]) for r in e[":position"][":rects"]]
        pdf_rects, top = geometry.to_zotero(page, rects)
        item = {
            "itemType": "annotation", "parentItem": att["key"], "annotationType": "highlight",
            "annotationText": re.sub(r"\s*\n\s*", " ", text), "annotationComment": h.comment if h else "",
            "annotationColor": TINE_HEX.get(e.get(":properties", {}).get(":color"), TINE_HEX["yellow"]),
            "annotationPageLabel": page.get_label() or str(pg),
            "annotationSortIndex": geometry.sort_index(page, pg - 1, text, top),
            "annotationPosition": json.dumps({"pageIndex": pg - 1, "rects": pdf_rects}, separators=(",", ":")),
            "tags": [{"tag": t} for t in sorted(h.tags)] if h else [],
        }
        # Zotero 10.0.4 rejects creates with a preset key (428 without a version, 400 with "version": 0).
        # If this response is lost, plan_doc pairs the two unlinked highlights by page and text next cycle.
        key = a.zkey = self.z.create(item)
        new_base[key] = base_values(self.z.get(f"/items/{key}")[0]["data"], a.tid)
        # zotero-key:: and the comment block are written by the next cycle once the page is quiet.

    def z_patch(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        patch, version = a.arg
        z = zanns[a.zkey]
        if ("annotationComment" in patch and z["annotationComment"]) or \
                ("tags" in patch and {t["tag"] for t in z["tags"]} - {t["tag"] for t in patch["tags"]}):
            backup(a.zkey, {"reason": "overwritten from Tine", "zotero": z, "patch": patch})
        if not self.z.patch(a.zkey, patch, version):
            raise ZoteroError(f"{a.zkey} changed in Zotero meanwhile (412)")
        cur = self.z.get(f"/items/{a.zkey}")[0]["data"]
        # Only a verified write counts as synced, compared the way Zotero stores values (trimmed, NFC).
        stored = lambda v: (sorted(tine.zotero_str(t["tag"]) for t in v) if isinstance(v, list)
                            else tine.zotero_str(v) if isinstance(v, str) else v)
        if any(stored(cur[k]) != stored(v) for k, v in patch.items()):
            raise ZoteroError(f"{a.zkey}: PATCH returned 204 but Zotero does not show the new values")
        new_base[a.zkey]["version"] = cur["version"]

    def z_delete(self, att, f, pdfdoc, zanns, sdoc, new_base, a):
        backup(a.zkey, {"reason": "deleted in Tine", "zotero": zanns[a.zkey], "md": sdoc["anns"][a.zkey].get("md")})
        if not self.z.delete(a.zkey, a.arg):
            raise ZoteroError(f"{a.zkey} changed in Zotero meanwhile (412)")


# ---------------------------------------------------------------- commands

def load_config():
    cfg = load_json(APP / "config.json", None)
    if not cfg:
        raise SystemExit("not set up: run `tzb init <Tine graph folder>` first")
    return cfg


def cmd_init(graph, notes):
    graph = graph.expanduser().resolve()
    if not (graph / "logseq/config.edn").exists():
        raise SystemExit(f"{graph} is not a Tine graph (no logseq/config.edn)")
    APP.mkdir(parents=True, exist_ok=True)
    save_json(APP / "config.json", {"graph": str(graph), "notes_heading": notes})
    print("Zotero will ask to allow write access for tine-zotero: click Always Allow.")
    save_json(APP / "auth.json", authorize(), mode=0o600)
    print(f"graph: {graph}\nstate: {APP}")


_LOCK = None


def hold_lock(graph):
    """One writer per state folder: `tzb run`, a manual `tzb sync` and `tzb resume` would fight over the same files."""
    global _LOCK
    if _LOCK:
        return
    f = open(APP / "run.lock", "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise SystemExit(f"another tzb (the background service?) is syncing {graph} (lock: {APP / 'run.lock'})")
    _LOCK = f


_SAID = {}      # problems already logged: a persisting one is reported once, not every cycle


def sync(dry_run=False):
    """One cycle. Returns None, or "soon"/"later" if work is left for a later cycle."""
    cfg = load_config()
    graph = Path(cfg["graph"])
    if not dry_run:
        hold_lock(graph)
    state = load_json(APP / "state.json", {"docs": {}})
    auth = load_json(APP / "auth.json", None)
    sid = server_id()
    if state.get("server_id", sid) != sid or (auth and auth["server_id"] != sid):
        raise SystemExit(f"Zotero server id changed ({state.get('server_id')} -> {sid}): refusing to sync")
    z = Zotero(auth)
    pdfs, by_att, skipped = read_zotero(z)
    parents = parents_of(z, [a for a in pdfs if a["key"] not in state["docs"]])
    names = assign_names(pdfs, parents, state)
    opened = tine.open_pdfs(graph)
    now = time.time() * 1000
    run, plans, resume = Sync(graph, z, state, cfg), [], APP / "resume"
    for att in pdfs:
        name, sdoc = names[att["key"]], state["docs"].get(att["key"])
        try:
            att["_path"] = sdoc["src"] if sdoc and os.path.exists(sdoc["src"]) else z.file_path(att["key"])
            att["_size"] = os.stat(att["_path"]).st_size
        except (OSError, ZoteroError) as ex:    # not downloaded ("as needed" file sync) or removed by hand
            run.report(f"{name}: PDF is not on this Mac ({ex}); skipped")
            continue
        att["_parent"] = parents.get(att.get("parentItem"), {})
        marker = resume / name
        try:
            plan = plan_doc(graph, att, name, by_att.get(att["key"], {}), sdoc, opened.get(f"{name}.pdf", []), now,
                            marker.exists())
        except Exception as ex:                 # e.g. an .edn that does not parse: this document only
            run.report(f"{name}: cannot read its Tine files ({ex!r}); skipped")
            continue
        if marker.exists() and not dry_run:
            marker.unlink()                     # `tzb resume` applies to one cycle only
        plans.append((att, name, plan))
    for k in state["docs"].keys() - {a["key"] for a in pdfs}:
        if z.exists(k):     # in the Zotero trash, or under a trashed paper: keep everything until it is emptied
            continue
        name = state["docs"][k]["name"]
        gone = Action(name, "zotero_gone", note="PDF no longer in Zotero",
                      deferred=PDF_OPEN if f"{name}.pdf" in opened else "")
        plans.append(({"key": k, "_path": None}, name, ([gone], {}, {})))

    actions = [a for _, _, (acts, _, _) in plans for a in acts]
    if dry_run:
        for a in actions:
            if a.op != "create_doc":
                print(a)
        new = [a for a in actions if a.op == "create_doc"]
        if new:
            print(f"\n{len(new)} new documents:", ", ".join(f"{a.doc}({a.note.split()[1]})" for a in new))
        print("totals:", dict(Counter(a.op for a in actions)))
        for msg in run.problems:
            print(msg)
        if skipped:
            print("not synced (tzb handles highlights only):", dict(skipped))
        return None

    state["server_id"] = sid
    for att, name, (acts, new_base, snap) in plans:
        try:
            if att["_path"] is not None:
                run.run_doc(att, name, by_att.get(att["key"], {}), acts, new_base, snap)
            elif not acts[0].deferred:          # attachment gone from Zotero
                f = Files(graph, name)
                run.zotero_gone(att, f, None, {}, None, {}, acts[0])
                f.save()
                del state["docs"][att["key"]]
                run.save_state()
                log(acts[0])
        except Exception as ex:                 # one document's failure must not stop the others
            run.report(f"{name}: sync failed ({ex!r})")
            run.failed()
    for msg, alert in run.problems.items():
        if msg not in _SAID:
            log(msg)
            if alert:
                notify(msg)
    _SAID.clear()
    _SAID.update(run.problems)
    return run.retry


IDLE_S = 60                     # re-check without an event: catches editors that save in place (Tine and iCloud
                                # replace files, which wakes us at once); one fingerprint costs a few ms of CPU
TROUBLE_S = 60                  # re-check while Zotero is closed or something keeps failing (file events still wake us)
VNODE = select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME


def watched(graph, state):
    """What to wake up on: folders (Tine and iCloud replace files atomically, which changes the folder), Zotero's
    database, Tine's session files, and the `tzb resume` markers. A few fds, whatever the library size."""
    src = next((d["src"] for d in state["docs"].values()), None)
    zdir = Path(src).parents[2] if src else Path.home() / "Zotero"      # <data dir>/storage/<KEY>/<file>
    return [graph / "assets", graph / "pages", tine.SESSIONS, zdir, APP / "resume",
            zdir / "zotero.sqlite", zdir / "zotero.sqlite-wal", *tine.session_files(graph)]


def fingerprint(graph):
    """Cheap change detector: Zotero library version, mtimes of the Tine files and sessions, pending resumes."""
    paths = [*graph.glob("assets/*.edn"), *graph.glob("pages/hls__*.md"), *tine.session_files(graph)]
    resume = APP / "resume"
    return (Zotero().library_version(), sorted((str(p), p.stat().st_mtime) for p in paths if p.exists()),
            sorted(os.listdir(resume)) if resume.exists() else [])


def cmd_run():
    """Sleep in kqueue until a watched file changes; sync only when the fingerprint moved or work is pending."""
    graph, last, pending, trouble = Path(load_config()["graph"]), None, "soon", None
    hold_lock(graph)
    (APP / "resume").mkdir(parents=True, exist_ok=True)
    log(f"watching {graph}")
    while True:
        kq, fds = select.kqueue(), []
        for p in watched(graph, load_json(APP / "state.json", {"docs": {}})):      # armed before syncing: no gap
            try:
                fds.append(os.open(p, os.O_EVTONLY))
            except (FileNotFoundError, PermissionError):
                continue
            kq.control([select.kevent(fds[-1], select.KQ_FILTER_VNODE, select.KQ_EV_ADD | select.KQ_EV_CLEAR, VNODE)], 0)
        try:
            fp = fingerprint(graph)
            if pending or fp != last:
                # The fingerprint from before the cycle: an edit that lands while it runs still differs next time.
                pending, last = sync(), fp
            if trouble:
                log("Zotero is back" if trouble == "offline" else "sync works again")
                trouble = None
        # While Zotero is closed or a cycle keeps failing, report it once and wait for a file event (Zotero's
        # database changes as soon as it starts) or TROUBLE_S, instead of retrying and logging every 3 s.
        except (urllib.error.URLError, ConnectionError, TimeoutError) as ex:
            if trouble != "offline":
                log(f"Zotero not reachable ({ex}); waiting for it")
            trouble, pending, last = "offline", None, None
        except SystemExit as ex:    # a refusal (e.g. another Zotero library): say so, retry on the next change
            if trouble != str(ex):
                log(ex)
            trouble, pending, last = str(ex), None, None
        except Exception:       # keep the daemon alive; the next cycle re-reads everything
            err = traceback.format_exc()
            if trouble != err:
                log(err.rstrip())
            trouble, pending, last = err, None, None
        wait = 3 if pending == "soon" else TROUBLE_S if pending or trouble else IDLE_S
        try:
            if kq.control(None, 64, wait):
                time.sleep(0.3)         # let a burst of writes land before looking
        finally:
            kq.close()
            for fd in fds:
                os.close(fd)


AGENT = Path.home() / "Library/LaunchAgents/io.github.tine-zotero.plist"


def tzb_path():
    """This `tzb` executable (the console script, or the one on PATH when started as `python -m tzb`)."""
    me = Path(sys.argv[0])
    return str(me.resolve()) if me.name == "tzb" else shutil.which("tzb") or sys.exit("tzb is not on PATH")


def cmd_agent(install):
    """Run `tzb run` at login under launchd (logs in the state folder), or remove that."""
    target = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{target}/{AGENT.stem}"], capture_output=True)
    if not install:
        AGENT.unlink(missing_ok=True)
        print("agent removed")
        return
    load_config()
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    AGENT.write_bytes(plistlib.dumps({
        "Label": AGENT.stem, "ProgramArguments": [tzb_path(), "run"],
        "RunAtLoad": True, "KeepAlive": True, "LowPriorityIO": True, "ProcessType": "Background",
        "StandardOutPath": str(APP / "tzb.log"), "StandardErrorPath": str(APP / "tzb.log"),
    } | ({"EnvironmentVariables": {"TZB_HOME": str(APP)}} if os.environ.get("TZB_HOME") else {})))
    subprocess.run(["launchctl", "bootstrap", target, str(AGENT)], check=True)
    print(f"agent running; log: {APP / 'tzb.log'}")


def cmd_resume(name):
    """Let the next cycle apply a paused document's deletions (a marker file, so no race with the running service)."""
    if name not in {d["name"] for d in load_json(APP / "state.json", {"docs": {}})["docs"].values()}:
        raise SystemExit(f"unknown document {name}")
    (APP / "resume").mkdir(parents=True, exist_ok=True)
    (APP / "resume" / name).touch()
    print(f"{name}: the next cycle applies the deletions")


def main():
    ap = argparse.ArgumentParser(prog="tzb", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="set the Tine graph folder and get a Zotero write key")
    p.add_argument("graph", type=Path)
    p.add_argument("--notes-heading", default="## Notes", help="block put above the highlights on new pages")
    p = sub.add_parser("sync", help="run one sync cycle")
    p.add_argument("--dry-run", action="store_true", help="print the planned actions, change nothing")
    sub.add_parser("run", help="stay running and sync whenever Zotero or the Tine files change")
    sub.add_parser("install-agent", help="start `tzb run` now and at every login (launchd)")
    sub.add_parser("uninstall-agent", help="stop and remove the login agent")
    p = sub.add_parser("resume", help="allow a paused document's mass deletion")
    p.add_argument("name")
    a = ap.parse_args()
    if a.cmd == "init":
        cmd_init(a.graph, a.notes_heading)
    elif a.cmd == "sync":
        sync(a.dry_run)
    elif a.cmd == "run":
        cmd_run()
    elif a.cmd in ("install-agent", "uninstall-agent"):
        cmd_agent(a.cmd == "install-agent")
    else:
        cmd_resume(a.name)
