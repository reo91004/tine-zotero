# tine-zotero bridge 설계 (v0.1, 2026-09-29)

## 목표와 결정 사항

Zotero가 PDF와 annotation의 원본이 되고, Tine에서는 같은 PDF를 같은 하이라이트가 보이는 상태로 읽고 쓴다.

- **Graph**: iCloud Drive에 있는 Tine graph 하나
- **범위**: Zotero 라이브러리 전체의 PDF 첨부 파일 (현재 50개, 159MB, annotation 17개)
- **방향**: 완전한 양방향. 하이라이트를 생성하거나 삭제하면 양쪽에 반영하고, comment, color, tags도 양쪽이 같은 값을 유지한다.

## 확인한 사실

| 항목 | 내용 | 근거 |
|---|---|---|
| 버전 | Tine 0.6.987, Zotero 10.0.4 (schema 44) | `Info.plist`, `/api/` 응답 헤더 |
| Zotero 쓰기 | `POST /api/local/authorize`로 키 발급(Allow / Always Allow). 쓰기 요청에는 `Zotero-API-Key`와 `Zotero-Server-ID`가 반드시 필요. `If-Unmodified-Since-Version`이 맞지 않으면 412 | 앱 번들의 `server_localAPI.js` |
| Server ID | 설치마다 다른 12자 ID (`Zotero-Server-ID`) | `/api/` 응답 헤더 |
| 버전 번호 | 로컬 `clientVersion`이 현재 0에서 시작. `since`가 이 값을 기준으로 필터링 | `Last-Modified-Version: 0` |
| 삭제 조회 | `/deleted` 엔드포인트가 없음 → 첨부 파일별 annotation 키 목록을 비교해서 감지 | `server_localAPI.js` |
| annotation 조회 | `/items/<ATT>/children`는 annotation을 빼고 돌려줌. `?itemType=annotation`을 붙여야 나옴 | 직접 호출 |
| citekey | Zotero 기본 필드 `citationKey`로 제공됨 (예: `ravi2020Generic`) | 직접 호출 |
| PDF 경로 | `/items/<ATT>/file/view/url` → `file://~/Zotero/storage/<ATT>/...pdf` | 직접 호출 |
| Tine PDF 리더 | graph의 `assets/` 안에 있는 일반 파일만 연다. symlink는 거부 | Tine 바이너리의 가이드와 에러 문자열 |
| Tine 하이라이트 | Logseq 형식. `pages/hls__<name>.md` + `assets/<name>.edn`. 좌표는 원점이 좌상단이고 배율 1 기준 | 실제로 생성된 파일 |
| Tine 플러그인 | WASM 샌드박스(파일, 네트워크 접근 불가) → bridge는 Tine 밖의 별도 프로세스 | 가이드 문자열 |
| 볼륨 | `~/Zotero`와 iCloud Drive는 같은 APFS 볼륨(disk3s5) | `stat` |

## 구조

```
Zotero 10 ──Local API (localhost:23119)───┐
  ~/Zotero/storage/<ATT>/<file>.pdf       │
            │ APFS clone (cp -c)          │
            ▼                             ▼
PKM/assets/<citekey>.pdf          tzb run (launchd 에이전트, 이 Mac 한 대에서만 실행)
PKM/assets/<citekey>.edn   ◀──▶   state.json (매핑 + 3-way base), trash/ (삭제 백업)
PKM/pages/hls__<citekey>.md ◀─▶
```

- 코드는 `tzb/` 아래 `zotero.py`(Local API), `tine.py`(edn/md 읽기와 부분 패치, 열린 PDF 판별), `geometry.py`(좌표 변환), `bridge.py`(계획, 실행, 명령)로 나눈다.
- 의존성은 `pymupdf`(좌표 변환, 페이지 라벨) 하나다. EDN은 Tine이 쓰는 부분집합만 읽는 작은 파서를 직접 둔다. 부분 패치를 하려면 항목마다 파일 안의 위치(span)가 필요한데, 일반 EDN 라이브러리는 이를 알려 주지 않기 때문이다. HTTP는 `urllib`로 처리한다.
- 설정, 키, state, 백업, 로그(`tzb.log`)는 `~/Library/Application Support/tine-zotero/`에 둔다(`TZB_HOME`으로 바꿀 수 있다). 새 페이지 맨 위의 메모 블록 글자는 `tzb init --notes-heading`으로 정한다.
- 변경 감지는 폴링 대신 kqueue(표준 라이브러리 `select`)로 한다. 감시 대상은 graph의 `assets/`와 `pages/` 폴더, Tine 세션 폴더와 세션 파일, Zotero 데이터 폴더와 `zotero.sqlite`·`zotero.sqlite-wal`, `tzb resume` 표식 폴더다. Tine과 iCloud는 파일을 원자적으로 교체하므로 폴더 이벤트로 잡히고, Zotero는 WAL 모드라 쓰기마다 `-wal`이 바뀐다. 파일마다 fd를 여는 방식은 launchd의 기본 fd 한도(256) 때문에 PDF 약 121개에서 동기화가 영구히 멈춰서 버렸다(v0.1.2, 이제 fd 12개). 이벤트가 오면 Zotero `Last-Modified-Version`, Tine 파일 mtime, 세션 파일, resume 표식으로 된 지문을 비교하고 달라졌을 때만 동기화한다. 지문은 동기화 전에 잡는다(동기화 도중 들어온 편집을 놓치지 않도록). 페이지를 막 편집해 Tine 쪽 쓰기가 보류되면 3초 뒤, 무언가 실패하면 60초 뒤 다시 보고, PDF가 열려 있어 보류된 것은 폴링하지 않고 세션 파일 변경(탭 닫힘)을 기다린다. 이벤트가 없어도 60초마다 지문을 확인해 제자리 저장하는 다른 편집기의 변경도 잡는다.
- `tzb install-agent`가 `tzb run`을 launchd 에이전트(`io.github.tine-zotero`, `ProcessType: Background`)로 등록한다. 에이전트는 설치한 위치의 코드를 실행하므로, 코드를 고친 뒤에는 `launchctl kickstart -k gui/$(id -u)/io.github.tine-zotero`로 다시 시작해야 한다.
- iPhone이나 iPad의 Tine에서 만든 하이라이트도 iCloud를 통해 `.edn`에 들어오므로, 이 Mac의 bridge가 Zotero로 보낸다.

## PDF: hard link 대신 APFS clone

iCloud Drive 안의 hard link는 Zotero 원본과 inode를 공유한다. 그러면 iCloud가 이 파일을 evict하거나 다른 기기 버전으로 교체할 때 Zotero 원본까지 영향을 받을 위험이 있다(실제로 그런지는 검증하지 않았다). clone은 다음과 같은 이유로 이 문제를 아예 피한다.

- 로컬 디스크를 추가로 쓰지 않는다(블록을 공유하는 copy-on-write).
- inode가 따로라서 iCloud가 무슨 작업을 해도 Zotero의 `storage/`는 영향을 받지 않는다.
- Tine은 PDF에 쓰지 않고, Zotero도 annotation을 PDF가 아닌 DB에 저장하므로 두 파일의 내용이 서로 달라질 일이 없다. Zotero 원본의 크기나 mtime이 바뀌면 bridge가 다시 clone한다.

iCloud 저장 공간은 PDF 전체만큼(현재 159MB) 사용한다.

## 데이터 매핑

### 문서 (Zotero가 주도)

- PDF 첨부 파일 하나당 `assets/<citekey>.pdf`, `assets/<citekey>.edn`, `pages/hls__<citekey>.md`를 만든다. 한 item에 PDF가 여러 개면 `<citekey>-2`처럼 번호를 붙이고, citekey가 없으면 첨부 파일 키를 쓴다.
- 페이지 속성: `file::`, `file-path::`(Tine 형식 그대로), `zotero-item:: [제목](zotero://select/library/items/<KEY>)`
- Zotero에서 첨부 파일이 사라지면 PDF clone만 지운다. 페이지는 노트가 있으므로 남기고 `zotero-missing::` 표시를 붙인다.
- Tine에서 페이지를 지워도 Zotero의 논문은 **지우지 않는다**. 다음 주기에 페이지를 다시 만든다. 페이지 하나를 지웠다고 Zotero의 논문과 annotation 전체를 삭제하는 것은 되돌리기 어렵기 때문에 이 경계를 둔다.

### 하이라이트 블록과 페이지 구성

```markdown
file:: [<citekey>.pdf](../assets/<citekey>.pdf)
file-path:: ../assets/<citekey>.pdf
alias:: <citekey>
icon:: 📄
zotero-item:: [<제목>](zotero://select/library/items/<KEY>)
- ## 내 메모                              ← 사용자 메모 (bridge가 만들기만 하고 이후 건드리지 않음)
	-                                     ← 바로 쓰기 시작할 빈 블록
- ---                                   ← 구분선
- <하이라이트 원문>                        ← 하이라이트는 페이지 맨 바깥 (Tine 기본 형태)
  hl-page:: 1
  hl-color:: yellow
  ls-type:: annotation
  id:: 73011ab0-4867-50d6-ab37-d155359b76d7
  zotero-key:: L2Q33KRM
  tags:: 공격모델
	- <Zotero comment>                   ← comment 블록 (zotero:: comment), 첫 번째 자식
	- [[NARC]] 연결 메모 …                ← Tine에만 남음
```

- `id`는 Zotero 키로 만든 uuid5다. Tine에서 먼저 만든 하이라이트는 Tine이 붙인 uuid를 그대로 쓴다.
- `zotero-key::`가 있으므로 state를 잃어도 파일만으로 매핑을 복구할 수 있다.
- **comment 블록**: Zotero annotation의 comment는 하나뿐이므로 블록도 하나다. 하이라이트의 자식 중 `zotero:: comment` 속성이 붙은 블록이고, 그 블록의 글이 comment다. 여러 줄 comment는 한 블록 안의 여러 줄이 된다. 다른 자식 블록과 comment 블록 아래에 들여 쓴 블록은 Tine 전용 메모다. bridge는 새로 만들 때 첫 번째 자식으로 둔다.
- comment가 비어 있어도 comment 블록은 항상 둔다(빈 블록에 쓰면 Zotero comment가 된다).
- **comment 블록이 없어지면 Zotero 값으로 다시 만든다. Zotero comment를 비우지 않는다.** comment를 지우려면 블록의 글을 비운다. Tine이 페이지를 다시 쓰면서 블록을 떨어뜨리는 경우가 있으므로(아래 사고 기록), 블록이 없어진 것을 삭제 의도로 읽지 않는다.
- 같은 id의 하이라이트 블록이 페이지나 `.edn`에 둘 이상이면 그 문서는 양쪽 모두 쓰지 않고 건너뛴다.
- 하이라이트 블록은 옮기지 않는다. Tine은 새 하이라이트를 페이지 끝에 붙이고, bridge도 Zotero에서 온 하이라이트를 끝에 붙인다(한꺼번에 추가할 때는 PDF 순서). 그래서 사용자 메모 블록(`tzb init --notes-heading`, 기본 `## Notes`)과 빈 자식 블록, 구분선(`- ---`)을 맨 위에 둔다. bridge는 새 페이지를 만들 때만 이 부분을 쓴다.
- `alias::`가 있어 다른 페이지에서 `[[<citekey>]]`로 논문 페이지에 연결할 수 있다(Tine은 `hls__<name>` 페이지를 PDF의 노트 페이지로 쓰므로 파일 이름은 바꿀 수 없다).
- graph의 `logseq/config.edn`에 `:block-hidden-properties #{:zotero :zotero-key}`를 넣어 bridge용 속성 줄을 숨긴다.

**사고 기록 (2026-09-29 18:30)**: 하이라이트를 페이지 안의 "Zotero 코멘트 목록" 블록 아래로 들여 넣는 구성(`tine_nest`)을 Tine 앱에서 확인하기 전에 실제 graph 전체에 적용했다. 사용자가 pay2026Keep의 PDF에서 색을 바꾼 뒤 다음 일이 이어졌다.
- Tine이 그 페이지를 다른 구조로 다시 저장했다.
- bridge가 하이라이트를 다시 옮겼고, 같은 id의 블록과 `id::` 없는 블록이 중복됐다.
- bridge가 사라진 comment 블록을 "comment 삭제"로 읽고 Zotero comment 3개를 비웠다.
- `.edn`에서 빠진 하이라이트 하나(CF7WRRXY)를 Zotero에서 삭제했다. 사용자 삭제인지 Tine 재저장 때문인지는 확인하지 못했다.

Tine이 들여 쓴 하이라이트를 다루지 못한 것인지, 그 전 10분 동안 페이지 구조를 여러 번 바꿔 Tine의 메모리 속 상태가 어긋난 것인지는 파일만으로 가리지 못했다. 에이전트를 멈추고 복구했다.
- 백업 JSON으로 comment를 되돌렸다.
- CF7WRRXY는 새 키 `9Z6TLZIV`로 다시 만들었다(Zotero 10.0.4는 키 지정 생성을 거부한다).
- 페이지는 Zotero 기준으로 다시 만들었다.

그 뒤 세 가지를 바꿨다.
- 하이라이트 블록을 옮기는 기능을 없앴다.
- comment 블록이 없어져도 Zotero comment를 비우지 않는다.
- 중복 블록이 있는 문서는 건너뛴다.

각 규칙에는 회귀 테스트를 넣었다. 손상된 파일과 삭제 직전 값은 `trash/`에 있다.

### 필드별 방향

| 필드 | Z→T | T→Z | 비고 |
|---|:-:|:-:|---|
| 생성 / 삭제 | O | O | Tine 쪽에서 존재 여부의 기준은 `.edn` 항목 |
| comment | O | O | 3-way merge |
| color | O | O | Tine 5색 ↔ Zotero hex. magenta/orange/gray는 가장 가까운 색으로 표시하고, Tine에서 색을 바꾸지 않으면 원래 hex를 유지 |
| tags | O | O | 블록의 `tags::` ↔ annotation tags, 집합 단위 3-way |
| text / position / page | O | 생성할 때만 | Tine에는 범위 수정 기능이 없음 |
| area highlight ↔ image annotation | - | - | 계획만 있고 구현하지 않았다(v0.1.x). Tine의 area 하이라이트(`:image`, `[:span]`, `hl-type:: area`)는 건너뛴다 |
| underline | - | - | 계획만 있고 구현하지 않았다(v0.1.x). Zotero에만 남는다 |
| ink / note / text annotation | - | - | Tine PDF 리더에 대응하는 표현이 없음. Zotero에만 남음 |

### 좌표 변환

`M = page.transformation_matrix * page.rotation_matrix`(PyMuPDF)로 PDF 사용자 좌표를 pdf.js의 배율 1 뷰포트 좌표로 옮긴다. 이 방식은 CropBox 오프셋과 회전을 모두 처리한다.

- Zotero→Tine은 `rect * M`, Tine→Zotero는 `rect * ~M`. Tine의 `width/height`가 `page.rect`와 다르면 그 비율만큼 먼저 보정한다.
- `annotationPageLabel`은 PDF의 페이지 라벨을 쓴다. `annotationSortIndex`는 `PPPPP|OOOOOO|TTTTT` 형식이며, 글자 오프셋 O는 PyMuPDF로 추출한 페이지 텍스트에서 계산한다.
- **검증**: `pay2026Keep`(페이지 라벨 472, 473)의 하이라이트 3개를 변환한 뒤, 변환된 사각형에서 텍스트를 다시 추출해 Zotero의 `annotationText`와 비교했다. 유사도는 1.000 / 0.997 / 0.997이었다.

### Zotero 노트 (child note)

현재 Zotero의 노트는 38개다.
- child note 37개: 부모 item에 PDF가 있는 것 33개, 없는 것 4개
- standalone 1개: `XF8FIR9S`, readingTime JSON이 들어 있는 플러그인 데이터
- 대부분 요약 노트다. 29개가 `data-summary-note="1"`이고, 전형적인 구조는 `<div data-schema-version="9" data-summary-note="1"><ul><li>…</li></ul></div>`
- 그 밖에 쓰인 태그는 `p`, `ol`, `strong`, `a`, `code`, `blockquote`, `span`이 각각 1~4개. 노트 길이의 중앙값은 442자, 최대는 6164자(채팅 기록)

매핑: PDF가 있는 부모 item의 노트는 그 item의 **첫 번째 PDF**의 `hls__` 페이지 맨 위(페이지 속성 바로 아래)에 넣는다.

```markdown
- 📝 Zotero note
  zotero-note:: R4R3DB2E
	- SCA-LDPC는 CCA 보안 PQC 암호의 …      ← <li> 하나 = 블록 하나
	- 선택암호문 질의가 얻는 비밀 변수 관계를 …
```

- 변환하는 태그는 `ul/ol/li/p`(블록 구조)와 `strong/em/code/a/blockquote`(인라인 Markdown)다. 바깥 `div`의 속성(`data-schema-version`, `data-summary-note`)은 state에 저장해 두었다가 Zotero에 쓸 때 다시 붙인다.
- **무손실일 때만 양방향으로 동기화한다.** 가져오기 전에 HTML → Markdown → HTML을 한 번 왕복해 보고, 정규화한 결과가 원본과 다르면 그 노트는 `zotero-note-sync:: read-only`로 표시하고 Zotero → Tine 방향으로만 동기화한다. Tine에서 편집해도 Zotero로 보내지 않고 로그에 경고를 남긴다. 표, 이미지, 수식, 인용이 있는 노트가 여기에 해당한다. 변환 실수로 노트 서식이 깨지는 것을 막기 위한 규칙이다.
- 노트 본문 전체를 하나의 값으로 보고 3-way merge한다. 충돌하면 Tine 값을 채택하고, Zotero 값은 `zotero-conflict::` 자식 블록으로 남긴다.
- 생성과 삭제는 하이라이트와 같은 규칙을 따른다(삭제 전에 백업, 대량 삭제는 중단). Tine에서 `📝 Zotero note` 블록을 새로 만들면 Zotero에 child note로 생성한다.
- 이번 범위에서 제외: 부모 item에 PDF가 없는 노트 4개(연결할 페이지가 없음), standalone 노트 1개.

## 첫 동기화 규칙

state에 base가 없는데 양쪽 값이 다르면(bridge 도입 전에 각각 바뀐 경우) **Zotero 값을 채택한다**. `pay2026Keep`의 색 2개(Tine에서 blue, purple)는 이 규칙에 따라 Zotero 값(yellow, green)으로 되돌렸지만, 덮어쓰기 테스트에서 다시 blue, purple로 돌아갔다. 현재 Tine은 blue/red/purple/yellow, Zotero는 yellow/green/green/yellow다. bridge를 처음 실행할 때 `--dry-run`으로 이 차이를 보여 주고 정리한다.

## 동기화 알고리즘

매 주기에 첨부 파일마다 Zotero 집합 Z, Tine 집합 T(`.edn`), state를 비교한다.

| Z | T | state | 동작 |
|:-:|:-:|:-:|---|
| O | - | - | Tine에 생성 |
| - | O | - | Zotero에 POST로 생성하고 `zotero-key::`를 기록. Zotero 10.0.4는 키를 미리 정한 생성을 거부한다(`version` 없으면 428, `"version": 0`이면 400 `'primaryData' not loaded`). 그래서 응답을 잃은 경우는 아래의 짝 다시 찾기로 복구한다 |
| O | O | O | 필드별 3-way |
| O | - | O | Tine에서 삭제됨 → annotation JSON과 **state에 저장해 둔 마지막 md 블록 전체(자식 블록 포함)**를 `trash`에 백업한 뒤 Zotero에서 DELETE. Zotero의 annotation 삭제는 영구적이고, Tine도 자식 메모까지 함께 지우므로 이 백업이 양쪽의 유일한 복구 수단이다. 단, base 이후 Zotero에서 수정된 적이 있으면 삭제하지 않고 Tine에 다시 만든다 |
| - | O | O | Zotero에서 삭제됨 → `.edn` 항목 제거. md 블록은 Tine 전용 자식 노트가 없으면 삭제하고, 있으면 남긴 채 `zotero-deleted::` 표시 |

짝 다시 찾기: Zotero 쪽과 Tine 쪽에 **어느 쪽에도 연결되지 않은** 하이라이트가 있고 페이지와 텍스트(공백 정규화)가 같으면 같은 하이라이트로 연결한다. 생성 응답을 잃은 경우와, 같은 문장을 두 앱에서 따로 칠한 경우가 여기에 해당한다. 새로 만들지 않고 `zotero-key::`만 기록한다.

필드별 3-way:

- 한쪽만 base와 다르면 그 값을 다른 쪽에 쓰고 base를 갱신한다. bridge가 쓴 값은 base에 반영되므로 되돌아온 echo는 "변경 없음"으로 처리된다.
- 양쪽이 모두 바뀌었고 값이 서로 다르면 충돌이다.
  - comment: Tine 값을 채택하고, Zotero 값은 `zotero-conflict::` 자식 블록으로 Tine에 남긴다.
  - color: Zotero 값을 채택한다.
  - tags: 양쪽 변경을 합친다.
- Zotero에 쓸 때는 항상 `If-Unmodified-Since-Version`을 보낸다. 412가 오면 다음 주기에 다시 읽고 merge한다.

## 삭제 보호 (iCloud 때문에 필요)

양방향 삭제는 "파일이 잠시 비어 보이는 상황"을 "사용자가 지웠다"로 잘못 판단하면 Zotero 데이터를 지우게 된다. 그래서 다음 경우에는 삭제로 보지 않는다.

- iCloud가 파일을 evict해서 내용이 없는 dataless 상태(`st_flags & SF_DATALESS`)이면 그 문서는 이번 주기에 건너뛴다.
- `.edn`이나 `hls__` 페이지 **파일 자체가 없으면** 문서를 다시 만든다. 삭제로 간주하는 것은 파일이 있는데 그 안의 항목이 빠졌을 때뿐이다.
- 한 주기에 한 문서에서 하이라이트가 절반 이상, 그리고 3개 이상 사라지면 그 문서를 멈추고 로그와 macOS 알림을 남긴다. `tzb resume <citekey>`로 확인한 뒤에만 계속한다.
- Zotero에서 삭제하거나 comment를 덮어쓰기 전에 항상 JSON을 `trash`에 백업한다. Tine에서 지운 하이라이트는 state에 남겨 둔 마지막 md 블록도 함께 백업한다. v0.1에서 복구는 수동이다(백업 JSON을 보고 되살린다).

## 열려 있는 PDF 보호 (스파이크 2 결과로 필요)

Tine 리더는 PDF를 열 때 `.edn`을 한 번만 읽는다. 이후 리더에서 편집하면 그때 들고 있던 상태로 `.edn`과 md의 `hl-color`를 다시 쓴다. 따라서 PDF가 열려 있을 때 bridge가 이 파일들을 고치면, 그 변경은 나중에 조용히 사라진다. 여기서 끝나지 않는다. 다음 주기에 bridge는 사라진 변경을 "Tine에서 되돌렸다"고 읽게 된다. 그러면 Zotero의 변경을 되돌리고, bridge가 추가했던 하이라이트는 Zotero에서 **삭제**한다.

- **열린 PDF 판별**: `~/Library/Application Support/page.tine.Tine/sessions/<graph>-*.json`의 `layout`에서 모든 창과 탭을 확인한다. 현재 history 항목이 `{"kind":"pdf","filename":…}`인 탭이 있으면 그 PDF는 열려 있다고 본다. 이 판별을 기준으로 두 가지를 정한다.
  - 열려 있는 문서는 Zotero→Tine 변경 중 **`.edn`을 고치는 변경과 md의 하이라이트 구조를 바꾸는 변경**(추가·삭제·`hl-color`)을 보류해 두었다가, PDF 탭이 닫힌 뒤 적용한다. 리더가 건드리지 않는 md 줄, 즉 comment, 노트, `zotero-key`는 PDF가 열려 있어도 바로 쓴다. 리더를 연 뒤에 추가한 줄이 보존된 것을 확인했다.
  - Tine→Zotero 방향은 파일을 읽기만 하므로 PDF가 열려 있어도 영향이 없다.
- **판별을 놓친 경우의 방어**: 세션 파일은 늦게 갱신될 수 있다. 그래서 bridge가 리더 소유 데이터(`.edn` 항목, `hl-color`)를 쓸 때 **쓰기 직전 값**(undo)과 쓴 시각을 state에 남긴다. 세션의 PDF 탭 `viewId`(`pdf-<base36 ms>-<n>`)에는 PDF를 연 시각이 들어 있다. 쓴 시각보다 **먼저 열린** 리더가 보이면 그 문서를 stale로 표시하고, Tine 쪽 값이 undo 값과 같으면(색이 직전 값으로 돌아갔거나, bridge가 추가한 하이라이트가 사라진 경우) Zotero로 보내지 않는다. 대신 Zotero 값을 다시 쓰며, PDF가 닫힌 뒤에 적용한다. 같은 저장에 들어 있는 다른 변경(이번 테스트의 red)은 사용자 편집이므로 그대로 Zotero에 보낸다. 쓴 뒤에 연 리더의 저장은 믿는다. 그래서 방금 동기화된 하이라이트를 사용자가 지워도 bridge가 되살리는 반복이 생기지 않는다. 60초 안에 오래된 리더가 나타나지 않으면 undo를 버린다.

## 그 밖의 안전장치

- 파일 쓰기: 읽고 해시 계산 → 필요한 줄 범위만 패치 → 같은 폴더의 `.tzb-*.tmp`에 기록 → 해시를 다시 확인 → `os.replace`
- 최근 3초 안에 사용자가 저장한 페이지에는 Tine 쪽 쓰기를 미룬다(편집 중인 페이지를 밖에서 바꾸면 Tine이 그 페이지 저장을 멈춘다). 읽기와 Zotero 쪽 쓰기는 미루지 않는다(Tine은 원자적으로 쓴다). bridge 자신이 쓴 mtime은 편집으로 치지 않는다.
- 저장해 둔 `Zotero-Server-ID`와 값이 다르면 동기화를 거부하고 로그에 한 번 남긴다. 복구 명령은 아직 없다(다른 Zotero 라이브러리를 쓰려면 state를 새로 만든다).
- `--dry-run`: 실행할 동작만 출력한다. 라이브러리 전체에 처음 적용할 때 반드시 먼저 사용한다.
- Zotero PATCH가 204를 돌려줘도 다시 조회해 값이 실제로 바뀌었는지 확인하고, 아니면 그 필드는 다음 주기에 다시 보낸다(검증 중 태그 PATCH가 204였는데 반영되지 않은 일이 한 번 있었다. 재현되지 않았다).
- 한 주기에서 실패한 동작(412, 네트워크, 파일이 쓰는 사이에 바뀐 경우)은 base를 갱신하지 않는다. 다음 주기에 다시 계획하므로 반쯤 반영된 상태가 base에 기록되지 않는다.

## v0.1 범위

- 동기화: 텍스트 하이라이트의 생성, 삭제, comment, color, tags.
- 제외, Zotero에만 남음: underline, image(area), ink, note, text annotation. 현재 라이브러리의 annotation 17개는 모두 highlight다. Tine에서 만든 area 하이라이트는 건너뛴다.
- 미구현: Zotero 노트 ↔ Tine 노트 블록(위 "Zotero 노트" 절의 설계), Zotero에서 하이라이트 텍스트를 고친 경우의 Tine 반영, `tzb restore`.

## 스파이크 상태

| # | 확인할 내용 | 상태 |
|---|---|---|
| 1 | clone한 PDF를 Tine이 열고, 외부에서 만든 `.edn`의 하이라이트를 그리는가 | 통과. 3개 모두 Zotero와 같은 위치에 표시됨(사용자 확인). Tine이 이후 저장할 때도 기존 항목을 보존함 |
| 2 | PDF가 열려 있을 때 외부에서 수정한 `.edn`/md를 Tine이 반영하고, 이후 저장할 때 덮어쓰지 않는가 | `59006b15`를 green→purple로 외부 패치함. 이후 Tine이 `.edn`을 다시 저장하면서(`:extra`에 보기 상태 추가) purple을 그대로 유지. 화면: **열려 있는 리더는 바로 갱신되지 않고, PDF를 다시 열어야 반영됨**(사용자 확인). **덮어쓰기 테스트 결과는 실패(음성)**: 리더를 연 뒤 17:01에 외부에서 색 2개를 바꿨는데, 17:03에 사용자가 리더에서 다른 하이라이트 하나를 바꾸자 Tine이 리더가 들고 있던 상태로 `.edn` 전체를 저장했고, md의 모든 `hl-color`도 그 상태로 다시 썼다 → 외부 변경 2개가 사라짐. 그 밖의 md 줄(리더를 연 뒤에 추가한 `zotero-key:: RFXYJBYZ` 포함)은 보존됨. 증거는 scratchpad의 `overwrite-test/{before,patched,after}.{edn,md}` |
| 3 | Tine에서 색을 바꾸거나 삭제할 때 `zotero-key::`와 comment 자식 블록이 보존되는가 | 색 변경: 보존됨(yellow→blue). 삭제: Tine은 `.edn` 항목과 md 블록을 **자식 블록까지 모두** 지움. "Tine 전용 메모" 블록도 함께 사라졌고 `.tine-trash`에도 남지 않음. PDF 탭을 닫으면 세션 파일이 약 6초 뒤 갱신됨 |
| 4 | 좌표 변환 | Z→T: 텍스트 유사도 1.000/0.997/0.997. T→Z: Tine은 같은 줄에 겹치는 사각형을 여러 개 저장하므로 줄 단위로 합쳐서 보냄(10개 → 3개). Zotero에 저장된 `RFXYJBYZ`의 텍스트 유사도 0.997. 줄 병합에서 PyMuPDF `Rect`의 `L \|= r`는 리스트 원소를 바꾸지 않으므로 `lines[i] = L \| r`로 써야 한다(처음 만든 annotation은 이 버그로 둘째 줄이 잘려서 PATCH로 고쳤음) |
| 5 | Local API의 POST/PATCH/DELETE가 Zotero 리더에 바로 반영되는가 | 권한 승인됨(Always Allow). POST 200 → `RFXYJBYZ`(label 473, sortIndex `00001\|001082\|00260`로 기존 annotation 사이에 올바르게 정렬됨). PATCH는 현재 버전이면 204, 오래된 버전이면 412. Zotero 리더에서 Tine과 같은 위치에 표시됨(사용자 확인). DELETE(버전 확인 포함)는 204를 반환했고, 이후 GET은 404 — **annotation은 Zotero 휴지통으로 가지 않고 영구 삭제됨**. 복구 수단은 bridge의 백업뿐이다(`trash/RFXYJBYZ.json`) |

Tine이 새로 만든 하이라이트의 형식: 페이지 끝에 추가되고, uuid4를 쓰며, `.edn` 텍스트에 줄바꿈 `\n`이 들어가고, md에서는 줄마다 2칸 들여쓴 연속 줄로 기록된다. 속성 순서는 `hl-page`, `hl-color`, `ls-type`, `id`.

## v0.1 검증 (2026-09-29)

실제 Zotero 10.0.4 라이브러리와 실제 graph의 **복사본**(임시 폴더, APFS clone)으로 확인했다. 실제 graph에는 아직 쓰지 않았다.

| 확인 | 결과 |
|---|---|
| 첫 실행 | 새 문서 49개 생성, `pay2026Keep` 인수(색 3개를 Zotero 값으로), annotation 17개 모두 연결 |
| 재실행 | 동작 0개. 처음에는 파일 끝 줄바꿈이 마지막 블록의 comment에 `\n`으로 붙어 `z_patch` 2개가 계획됐다. 복사본에서 발견해 고쳤고 회귀 테스트를 추가했다 |
| Tine→Zotero (시험용 annotation 1개) | 생성, comment 두 줄, 색, 태그, 머리 블록 삭제 시 comment 비움과 재생성, 삭제(404) 모두 통과. 이 시나리오는 머리 블록 형식일 때 돌렸고, comment 블록 하나로 바꾼 뒤에는 아래 `tzb run` 측정에서 생성, comment 양방향, 삭제를 다시 확인했다 |
| Zotero→Tine | comment 수정, 생성(색과 comment 포함), 삭제 모두 통과 |
| 생성 응답 유실 | state와 md에서 키 기록을 지운 뒤 dry-run → 중복 생성 없이 `tine_key` 1개로 다시 연결됨 |
| `tzb run` (kqueue, launchd `Background`) | Zotero comment 수정 → Tine 0.6–0.7초, Tine comment 수정 → Zotero 0.7초, Tine 삭제 → Zotero 0.7–0.8초, Tine 새 하이라이트 → Zotero 생성 약 1초(`zotero-key`를 Tine에 적는 것은 편집 보호 3초 때문에 약 5초). 대기 중 30–60초 CPU 시간 증가 0.00초. 2초 폴링 방식이었을 때는 생성·삭제가 약 5초였다 |
| 끝난 뒤 | Zotero annotation 17개(시작과 같음). 시험 중 삭제한 항목은 모두 `trash/`에 백업 |
| 단위 테스트 | `tests/test_tine.py`(패치와 파서), `tests/test_plan.py`(오래된 리더 방어, 대량 삭제 정지, 수정 후 삭제, 충돌, 짝 다시 찾기, 첫 동기화) |

확인하지 않은 것: 실제 Tine 앱에서 PDF가 열린 상태의 보류와 오래된 리더 방어(단위 테스트로만 확인), `:block-hidden-properties`가 화면에서 속성을 숨기는지, iPhone/iPad에서 만든 하이라이트가 iCloud를 거쳐 들어오는 경로.

## 공개 전 독립 검토 (2026-09-29)

데이터 손상 경로만 보는 독립 검토를 한 번 받았다. 8건 가운데 7건은 가짜 graph에서 재현했고(CONFIRMED), 1건(목록 페이징 중 누락)은 Zotero 소스로 메커니즘만 확인했다. 모두 고쳤다. 고친 뒤 검토 쪽 재현 코드를 다시 돌려 막힌 것을 확인했고, 각 건에 회귀 테스트를 넣었다(`tests/test_plan.py` 15개).

| # | 문제 | 수정 |
|---|---|---|
| 1 | `.edn` 항목 1–2개만 빠져도(다른 기기의 리더, iCloud 예전 사본) 대량 삭제 가드에 걸리지 않고 Zotero에서 영구 삭제 | md 블록이 남아 있으면 삭제가 아니라 `.edn`에 되살린다. 실제 Tine 삭제는 md 블록까지 지운다(스파이크 3). `or h`를 되돌리면 테스트가 실패하는 것을 확인했다 |
| 2 | comment의 `\r` 줄바꿈 때문에 블록 구조가 깨지고 주기마다 블록이 늘어남 | Zotero에서 읽을 때 `\r\n`, `\r`를 `\n`으로 바꾼다(`unify_newlines`) |
| 3 | 계획한 뒤 실행하기 전의 Tine 편집을 백업 없이 덮어씀(실행할 때 파일을 다시 읽어 비교 기준으로 썼음) | `plan_doc`이 읽은 텍스트를 쓰기의 비교 기준으로 넘긴다. 그사이 바뀌었으면 `Changed`로 다음 주기에 다시 한다 |
| 4 | 목록을 100개씩 받는 사이 수정된 항목이 목록에서 빠지면 삭제나 첨부 소실로 오인(로컬 API는 `dateModified` 순) | 한 번에 받고, `Total-Results`와 개수가 다르면 동기화하지 않는다 |
| 5 | comment 둘째 줄 이후가 `key:: value` 모양이면 속성으로 읽혀 Zotero에서 그 줄이 지워짐 | 그런 comment는 동기화하지 않고 `skip`으로 표시한다 |
| 6 | 쉼표, `#`, `[[…]]`가 든 Zotero 태그가 `tags::` 줄에서 쪼개짐 | 그런 태그가 있는 하이라이트는 태그를 동기화하지 않는다 |
| 7 | 같은 페이지에 같은 문구가 두 번이면 짝이 틀릴 수 있음. Zotero 값으로 덮을 때 Tine 쪽 원래 값은 백업하지 않았음 | 페이지+텍스트가 양쪽에 하나씩일 때만 짝을 짓는다. `tine_comment`/`tine_tags`는 덮기 전에 md 블록을 백업한다 |
| 8 | 백업 파일 이름이 초 단위라 같은 초의 두 번째 백업이 첫 번째를 덮음 | 이름에 마이크로초를 넣는다 |

남은 한계: 다른 기기(iPad 등)의 Tine 리더가 bridge가 추가한 하이라이트를 `.edn`과 md 양쪽에서 모두 뺀 상태로 저장하면, 이 Mac의 세션 파일에는 그 리더가 보이지 않는다. 그래서 오래된 리더로 판정하지 못하고 사용자 삭제로 처리한다(백업은 남는다).


## v0.1.1 전체 리뷰와 v0.1.2 수정 (2026-09-30)

최고 수준의 코드 리뷰를 한 번 받았다.
- 방식: 10개 관점에서 후보를 찾고, 상위 16건을 각각 독립 검증자가 재현했다. 끝으로 빈틈을 한 번 더 훑었다.
- 결과: 검증한 16건 가운데 15건이 재현됐고(CONFIRMED) 1건은 그럴듯함(PLAUSIBLE)이었다. 반박된 것은 없었다. 빈틈 검토에서 8건이 더 나왔고, 모두 v0.1.2에서 고쳤다.

재현된 것 중 데이터에 직접 영향이 있던 것:
- PDF가 Tine에 열린 채 `.edn`과 md가 모두 없으면, 파일 재생성만 실행되고 하이라이트 추가는 보류됐다. 그 결과 다음 주기에 Zotero에서 모두 삭제됐다. 이제 `init_files`도 PDF가 닫힐 때까지 기다린다.
- 필드 변경이 보류되는 동안 base 버전이 올라가서, "Zotero에서 수정된 하이라이트는 지우지 않는다" 보호가 무력화됐다. 이제 보류 중인 필드가 있으면 버전도 옛 값을 유지한다.
- `.edn` 항목만 되살릴 때 base를 Zotero 값으로 덮어써서, 블록의 옛 comment가 Zotero 편집을 되돌렸다. 이제 블록이 남아 있으면 옛 base comment와 태그를 유지한다.
- Tine이 담을 수 없는 comment와 태그 대신 빈 값을 썼는데, base에는 원래 값을 기록했다. 그래서 나중의 Zotero 편집이 지워졌다. 이제 base에 실제로 쓴 값을 기록한다.
- `tags:: #a #b`를 태그 하나 `a #b`로 읽어 Zotero 태그를 오염시켰다. 이제 Logseq 형식을 인식한다.
- `tzb resume` 표시가 할 일 없는 주기에는 지워지지 않아서, 대량 삭제 보호가 계속 꺼져 있었다. 이제 표식 파일을 쓰고, 다음 한 주기에만 적용한다.
- Zotero가 저장할 때 앞뒤 공백을 자르고 NFC 정규화하는 것을 무시해서, PATCH 검증이 영원히 실패하고 충돌 블록이 쌓였다. 이제 Tine 값도 같은 방식으로 정규화해 비교한다.
- 충돌 블록과 Zotero 덮어쓰기가 따로 돌았다. 이제 충돌 블록은 한 번만 쓰고, Zotero 쓰기는 그 블록이 저장된 뒤에만 한다. 둘은 함께 보류된다.
- `.edn`을 먼저 쓰고 md 비교가 실패하면 반쯤 쓴 상태가 영원히 남았다. 이제 두 파일을 다 확인한 뒤 md부터 쓰고, 빠진 페이지 블록은 다시 만든다.
- 오래된 리더가 Zotero에서 지운 하이라이트를 다시 저장하면 Zotero에 다시 만들어졌다. 이제 지운 id를 잠시 기억해 다시 지운다. 오래된 리더 판정 표시도 이제 저장되고 해제된다.

전체 동기화를 멈추던 것:
- 하이라이트가 아닌 annotation 하나(KeyError)
- 로컬에 파일이 없는 첨부 하나
- 깨지거나 암호화되거나 페이지가 줄어든 PDF 하나(같은 문장을 두 번 칠한 경우 Zotero 중복 생성 폭주까지 이어졌다)
- 파일마다 연 fd가 launchd 한도 256에 닿은 경우(PDF 약 121개)
- "Allow"(1회용 키)

이제 문서 단위로 격리해서 한 번만 보고한다. 1회용 키는 거절하고, 401은 알림으로 알린다.

그 밖에 고친 것:
- 휴지통에 넣었다 되살린 논문이 영원히 건너뛰어지던 문제
- 동기화 도중의 편집이 다음 변경까지 누락되던 문제
- PDF가 열린 동안 3초마다 전체 동기화하던 문제
- pause 알림이 3초마다 반복되고, skip과 pause가 로그에 남지 않던 문제
- 회전(90/180/270도)과 CropBox 오프셋이 함께 있는 페이지의 좌표. PyMuPDF 1.28의 `transformation_matrix`가 회전 시 오프셋을 빠뜨린다. 렌더링 비교로 8개 조합을 확인했다.
- 음수 sortIndex
- 다른 graph의 세션 파일을 섞어 읽던 문제
- HTTP 타임아웃이 없던 문제와 시스템 프록시를 거치던 문제
- 수동 `tzb sync`가 잠금을 잡지 않던 문제
- 대소문자만 다른 citekey가 같은 파일을 쓰던 문제
- EDN `##NaN`과 문자 리터럴
- 첫 줄에 속성이 있는 comment 블록

수정분을 다시 독립 검토받았다. 수정이 새로 만든 회귀 4건이 재현됐고, 모두 고쳤다.
- 쓰기가 실패하거나 문서를 건너뛴 주기에도 오래된 리더 표시(stale)가 풀렸다. 그러면 옛 색이 Zotero로 가거나 지운 하이라이트가 되살아났다. 이제 계획이 끝까지 진행됐고, 보류된 것이 없고, 쓰기가 성공한 주기에만 푼다.
- `zotero-missing` 페이지를 다른 첨부가 같은 이름으로 인수하면 옛 하이라이트가 지워졌다. 이제 페이지의 `zotero-key`가 모두 지금 Zotero에 있을 때만 인수한다.
- 첫 줄이 `std::vector …`인 comment를 속성으로 읽어 Zotero comment를 비웠다. 이제 속성 줄은 `key:: 값`(`::` 뒤 공백)만 인정하고, 첫 줄은 블록 전체가 속성일 때만 속성으로 본다.

회귀 테스트는 모두 43개다(`test_plan` 34, `test_tine` 7, `test_geometry` 2). 핵심 수정 5개는 되돌리면 해당 테스트가 실패하는 것을 확인했다. `tests/live_roundtrip.py`를 실제 Zotero와 임시 graph에서 다시 돌려 A–I 전체가 통과했고, annotation 수도 16개로 그대로였다.

새 실행 루프를 측정한 결과:

| 항목 | 결과 |
|---|---|
| fd | 12개 |
| Zotero → Tine | 0.3초 |
| Tine에서 원자적 저장 후 생성 → Zotero | 0.5–0.6초 |
| Tine 삭제 → Zotero | 0.1–0.5초 |
| 연달아 두 번 저장 | 최종본이 0.5초 |
| 대기 30초 | CPU 0.00초 |

**사고 기록 (2026-09-29 23:12)**: 리뷰 검증 에이전트의 재현 스크립트가 `TZB_HOME`을 설정하기 전에 `tzb.bridge`를 불러오는 바람에, **실제 state.json을 가짜 문서 하나로 덮어썼다.** 서비스는 그 뒤 깨어날 이벤트가 없어서 아무것도 실행하지 않았다(로그로 확인).
- 발견: 새 코드의 dry-run이 문서 50개를 모두 `adopt`로 계획한 것을 보고 알았다.
- 복구: 서비스를 멈춘 뒤 덮어쓴 파일은 `trash/`에 보관했다. dry-run에서 Tine과 Zotero가 완전히 일치함(필드 차이 0)을 확인한 뒤, 모든 문서를 다시 adopt해 state를 만들었다.
- 결과: 다시 계획해도 할 일이 0개였다. 사라진 것은 undo 기록과 마지막 md 스냅샷뿐이다.
- 교훈: 테스트와 스크립트는 tzb를 불러오기 **전에** `TZB_HOME`을 임시 폴더로 설정해야 한다(`APP`은 불러올 때 정해진다). `tests/test_plan.py`는 파일 첫머리에서 이렇게 한다.
