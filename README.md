# tzb — two-way highlight sync between Zotero and Tine

`tzb` keeps the PDF highlights of your [Zotero](https://www.zotero.org) library and a
Tine graph in step, in both directions, within about a second:

| | Zotero → Tine | Tine → Zotero |
|---|:-:|:-:|
| create / delete a highlight | ✓ | ✓ |
| comment | ✓ | ✓ |
| color (Tine's 5 colors ↔ nearest Zotero color) | ✓ | ✓ |
| tags | ✓ | ✓ |

Each Zotero PDF becomes a Tine document: an APFS clone of the PDF in `assets/`, Tine's highlight
store `assets/<citekey>.edn`, and the notes page `pages/hls__<citekey>.md`:

```markdown
alias:: <citekey>                      ← link the paper as [[<citekey>]]
- ## Notes                             ← your notes about the paper (tzb never touches them)
	-
- ---
- <highlighted text>                   ← one block per highlight; Tine adds new ones at the end
  hl-page:: 3
  hl-color:: yellow
	- <Zotero comment>                 ← the first child is the comment, synced both ways
	- your own notes                   ← any other child stays in Tine only
```

To clear a comment, empty its block. Deleting the comment block does not clear the Zotero
comment: tzb puts the block back with Zotero's text.

## Requirements

- macOS (tzb uses kqueue, launchd and APFS clones)
- Zotero with the local API enabled: *Settings → Advanced → Allow other applications on this
  computer to communicate with Zotero*. tzb writes through the local API, so Zotero must accept
  local write requests. Tested with Zotero 10.0.4.
- Tine (tested with 0.6.987) and a graph folder; iCloud Drive works

## Install

```bash
brew install reo91004/tap/tzb
```

or, from a clone, `uv run tzb …`.

## Set up

```bash
tzb init ~/path/to/graph --notes-heading "## Notes"   # Zotero asks you to allow tzb to write: click Allow
tzb sync --dry-run                                     # see what the first sync would do
tzb sync                                               # first sync
brew services start tzb                                # keep syncing in the background
```

Without Homebrew, use `tzb install-agent` (and `tzb uninstall-agent`) instead of `brew services`.
Run only one of them: a second `tzb run` refuses to start.

To hide tzb's bookkeeping properties, add this line to the graph's `logseq/config.edn`:

```clojure
:block-hidden-properties #{:zotero :zotero-key}
```

## How it stays safe

- **First sync:** where Zotero and Tine already differ, Zotero wins. `--dry-run` shows these changes first.
- **Three-way merge:** tzb remembers the last synced value of every field and only moves real
  changes. If both sides edited a comment, Tine wins and Zotero's text is kept as a
  `zotero-conflict::` block.
- **Backups:** every Zotero deletion and every overwritten comment is saved as JSON in
  `~/Library/Application Support/tine-zotero/trash/`. Zotero deletes annotations permanently,
  so these backups are the only way back.
- **Open PDFs:** Tine's reader rewrites its files from memory. While a PDF is open in Tine, tzb
  waits to change that document's highlight store until you close the PDF. It also ignores
  changes from a reader opened before its own write.
- **Typing:** tzb waits 3 s after your last save to write a page you are editing.
- **Mass deletion:** if half or more of a document's highlights (at least 3) vanish at once,
  tzb pauses that document and notifies you. After checking, run `tzb resume <name>`.
- **Ambiguous pages:** a page with two blocks for the same highlight is left untouched.
- **iCloud:** files that iCloud has not downloaded yet are skipped. A missing file is recreated,
  never read as a deletion.
- **Stale writes:** Zotero writes are version-checked, so an item changed in Zotero meanwhile is
  re-read next cycle, not overwritten.

## Commands

| | |
|---|---|
| `tzb init <graph>` | set the graph, get a Zotero write key |
| `tzb sync [--dry-run]` | one sync cycle |
| `tzb run` | stay running; sleep until Zotero's database or a Tine file changes |
| `tzb install-agent` / `uninstall-agent` | run `tzb run` at login (launchd) |
| `tzb resume <name>` | let a paused document's mass deletion through |

Config, key, state, backups and the log live in `~/Library/Application Support/tine-zotero/`
(override with `TZB_HOME`).

## Limits (v0.1)

- Only text highlights sync. Underline, image (area), ink, note and text annotations stay in
  Zotero; area highlights made in Tine stay in Tine.
- Zotero notes are not synced yet.
- Zotero 10.0.4 does not accept a preset key when creating an item. If tzb loses the answer to a
  create, the next cycle links the two copies again by page and text instead of creating a duplicate.
- Restoring from `trash/` is manual.

## Development

```bash
uv run python tests/test_tine.py      # file readers and patchers
uv run python tests/test_plan.py      # sync decisions
```

`tests/live_roundtrip.py` runs the whole round trip against a running Zotero, on a copy of a
graph. It creates and deletes one throwaway annotation; see its docstring. `DESIGN.md` records the
design, the verification and an incident (in Korean).

## License

AGPL-3.0, because tzb uses [PyMuPDF](https://pymupdf.readthedocs.io) (AGPL-3.0).

---

### 한국어 요약

Zotero와 Tine 사이에서 PDF 하이라이트, comment, 색, 태그를 양방향으로 1초 안팎에 동기화합니다.

1. `brew install reo91004/tap/tzb`로 설치합니다.
2. `tzb init <graph 폴더>`를 실행하고 Zotero에서 Allow를 누릅니다.
3. `tzb sync --dry-run`으로 첫 동기화 내용을 확인합니다.
4. `tzb sync`를 실행한 뒤 `brew services start tzb`로 백그라운드 동기화를 켭니다.

하이라이트 바로 아래 첫 번째 블록이 Zotero comment이고, 나머지 블록은 Tine에만 남습니다. 삭제나 덮어쓰기 전의 값은 `trash/`에 백업됩니다.
