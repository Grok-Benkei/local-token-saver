# local-token-saver (v2.0.0)

**Zero-dependency, offline, zero-telemetry token reduction toolkit for AI agents, Browser Use / Playwright, and MCP bots.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-green.svg)](https://www.python.org/)
[![Dependencies: Zero](https://img.shields.io/badge/Dependencies-Zero%20%28stdlib%20%2B%20SQLite%29-brightgreen.svg)](#security--privacy)

`local-token-saver` reduces LLM input token consumption by **85% to 99.4%** without lowering model quality, reasoning depth, or output completeness. Instead of truncating agent thoughts or answers, it strips redundant input noise **before** data enters the context window.

---

## Why `local-token-saver`?

AI agents routinely burn 80–99% of their context window on boilerplate that carries zero signal:
- Raw HTML DOM trees full of `<style>`, `<svg>`, analytics `<script>` tags, inline `data:image/...;base64,...` blobs, and `utm_*` tracking URLs.
- Next.js / Nuxt / SPA pages where product price and stock live inside `<script id="__NEXT_DATA__">` or `application/ld+json`.
- Verbose Playwright / Browser-Use ARIA accessibility trees (`- generic:`, `- group:`, `- presentation:`) with hundreds of empty structural wrapper nodes.
- API & MCP JSON payloads bloated with `null` fields, avatar URLs, and inline Base64 screenshots.
- Multi-thousand-row CSV/TSV exports where the agent only needs column statistics (`min`, `max`, `mean`, `sum`) and sample rows.
- `git diff` outputs dominated by `package-lock.json`, `pnpm-lock.yaml`, `Cargo.lock`, or minified bundles.
- Test runner & FFmpeg/Remotion logs with thousands of `PASS` or `frame=` progress lines hiding the actual `ERROR` / `Traceback` in the middle of the log.
- Repeated identical or near-identical search queries (in English or Japanese/CJK) and infinite retry loops against a down API.

---

## Security & Privacy Guarantees

- **Zero External Dependencies**: Written 100% in Python standard library (`html.parser`, `sqlite3`, `json`, `csv`, `decimal`, `re`, `unicodedata`, `subprocess`). No `pip install` required.
- **Zero Network Calls & Zero Telemetry**: Never connects to the internet or external servers.
- **Built-in Secret & Credential Redaction**: Automatically redacts sensitive HTML inputs (`type="password"`, `csrf_token`) and JSON keys (`password`, `secret`, `access_token`, `api_key`, `authorization`, `cookie`, `private_key`) to `<redacted>`.
- **Single-File Auditable Architecture**: One self-contained file (`token_saver.py`).

---

## Verified Benchmarks (`token-saver selftest`)

| Module / Command | Use Case | Tokens Before | Tokens After | Reduction | What Is Preserved (100% Lossless Signal) |
| :--- | :--- | ---: | ---: | ---: | :--- |
| `json-slim` | API / X / MCP JSON + Base64 & Secret Scrub | 65,383 | 466 | **99.3%** | Core fields, metrics; scrubs `data:` URIs, Base64 & Hex blobs, redacts camelCase/snake_case secrets & API tokens |
| `batch-slim` | Multi-file MCP `write_to_file` Batch | 65,202 | 516 | **99.2%** | Slims multiple tool output files in 1 turn (collision-safe & overwrite-safe) |
| `diff-slim` | Git Diffs & PR Reviews | 10,413 | 150 | **98.6%** | All `+`/`-` changed lines & headers; collapses lockfiles, minified bundles & middle unchanged context |
| `html2md` | Browser Use / SPA / Next.js Web Pages | 9,651 | 226 | **97.7%** | Headings, text, tables, `<form>`, `<input>`, `<button>`, `<select>`, `[Image: alt]`, plus `__NEXT_DATA__`, `__INITIAL_STATE__` & JSON-LD |
| `aria-slim` | Playwright / Browser-Use ARIA Snapshots | 1,471 | 77 | **94.8%** | **100% of `[ref=...]` markers**, interactive roles & faithful sibling/parent tree hierarchy |
| `csv-slim` | Large CSV / TSV Datasets (Streaming) | 2,980 | 212 | **92.9%** | Full-precision `Decimal` `min`/`max`/`mean`/`sum` across all rows + head/tail sample |
| `md-outline` | Long Markdown Docs / Ebooks | 7,234 | 732 | **89.9%** | Full heading tree with CJK-aware token counts + code-fence-safe section extraction |
| `run` / `log-slim` | CLI, Test Suites, FFmpeg / Remotion | 1,749 | 253 | **85.5%** | Head + tail + **all middle-of-log `ERROR`/`FAIL`/`Traceback` context windows** |
| `cache` | SQLite CJK N-gram Similarity Cache | Any | **0** | **100.0%** | Instant local hit (supports v1->v2 migration, English + Japanese/CJK 2-gram/3-gram matching & namespaces) |

---

## Installation

```bash
git clone https://github.com/Grok-Benkei/local-token-saver.git
cd local-token-saver
chmod +x token_saver.py
ln -sf "$(pwd)/token_saver.py" ~/.local/bin/token-saver

# Run built-in assertion & benchmark suite
token-saver selftest
```

---

## CLI Reference (9 Core Modules)

### 1. Universal Auto-Detect Slimmer (`slim`)
Automatically detects `html`, `aria`, `json`, `jsonl`, `csv`, `tsv`, `diff`, `markdown`, or `cli` and routes to the right compressor.
- **Markdown-Safe by Default**: Running `token-saver slim notes.md` cleans HTML comments, inline Base64 data URIs, and redundant blank lines while **preserving 100% of the Markdown body text** (pass `--outline` only when you want headings only).

```bash
token-saver slim page.html
token-saver slim page.html --browser      # Keep <header>/<nav> links & forms for Browser Use
curl -s https://example.com | token-saver slim -
```

### 2. Browser-Use & SPA-Safe HTML -> Markdown (`html2md`)
Designed for both static web scraping and interactive Browser Use / E-commerce (Mercari, Yahoo Auctions, Next.js SPAs):
- Preserves `<form>`, `<input>`, `<textarea>`, `<select>`, `<option>`, `<button>`, and `<img alt="...">` as compact markers (`[Form ...]`, `[Input: ...]`, `[Button: ...]`, `[Select: ...]`, `[Image: ...]`).
- Extracts `<script id="__NEXT_DATA__">`, `<script type="application/ld+json">`, and `window.__INITIAL_STATE__` / `__NUXT_DATA__`, compacts them via `slim_json_value`, and appends them under `### [Structured Data]`.
- Strips `utm_*`, `fbclid`, `gclid` tracking parameters without breaking signed URLs (`X-Amz-Signature`, `Signature`, `token`).
- Preserves `<pre><code>` indentation.

```bash
token-saver html2md listing.html
token-saver html2md listing.html --browser --base-url "https://jp.mercari.com"
```

### 3. Playwright / Browser-Use ARIA Snapshot Slimmer (`aria-slim`)
Compresses accessibility tree snapshots (`page.locator("body").aria_snapshot()`) by collapsing empty `- generic:`, `- group:`, `- presentation:` wrappers while preserving **100% of `[ref=...]` markers** and interactive elements across the entire page.

```bash
token-saver aria-slim snapshot.aria
```

### 4. JSON & JSONL Schema Slimmer (`json-slim`, `jsonl-slim`)
Strips `null`/empty containers, noisy avatar/UI metadata keys, inline `data:...;base64,...` URIs, and raw Base64/Hex blobs (`>= 256` chars) **before** string truncation, and redacts sensitive keys (`password`, `api_key`, `access_token`, `cookie`).

```bash
token-saver json-slim api_response.json --max-items 10
token-saver jsonl-slim events.jsonl --max-records 20
```

### 5. Streaming CSV / TSV Summary + Sample (`csv-slim`)
Streams arbitrarily large CSV/TSV files in constant memory (`collections.deque` + `Decimal`), outputting exact numeric column statistics (`min`, `max`, `mean`, `sum`) and head/tail sample rows.

```bash
token-saver csv-slim sales_data.csv --head 8 --tail 5
```

### 6. Git Diff & Lockfile Compressor (`diff-slim`)
Collapses lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `Cargo.lock`, `uv.lock`, etc.) and minified assets into a 1-line summary while keeping source code diffs intact.

```bash
git diff | token-saver diff-slim -
```

### 7. Markdown Outline & Section Extractor (`md-outline`)
Inspects heading structure with CJK-aware token estimates per section and extracts only the section you need.

```bash
token-saver md-outline manuscript.md
token-saver md-outline manuscript.md --section "Architecture"
```

### 8. Error-Safe CLI Runner & Log Compressor (`run`, `log-slim`)
Executes commands safely (`shell=False` for argument lists, `stdin=DEVNULL`, configurable `--timeout`), collapses repetitive progress bars (`frame=`, `PASS`), and preserves head + tail + **every middle-of-log `ERROR`/`FAIL`/`Traceback`/`Exception` window (`+/- 2` lines)**.

```bash
token-saver run pytest -q
token-saver run ffmpeg -i input.mp4 output.mp4
token-saver log-slim build.log
```

### 9. Batch Slimmer (`batch-slim`) & CJK-Aware SQLite Cache (`cache`)
- **`batch-slim`**: Compress multiple large MCP tool output files in a single Shell call so your agent avoids multi-turn conversation history resend overhead.
- **`cache`**: Local SQLite (WAL) exact + CJK character 2-gram/3-gram Jaccard similarity cache with `--namespace` isolation and a 3-strike circuit breaker.

```bash
# Slim all large tool outputs in one turn
token-saver batch-slim /workspace/agent-tools/*.txt --max-items 10

# Store & query cache (works with English and Japanese/CJK!)
token-saver cache put research_bot "東京から大阪までの新幹線の料金を教えて" "指定席は約14,720円です。"
token-saver cache get research_bot "東京から大阪の新幹線料金を教えてください" --similarity 0.55

# Circuit breaker guard & status recording
token-saver cache guard external_api
token-saver cache record external_api failure "HTTP 502 Bad Gateway"
token-saver stats
```

---

## License

MIT License — see [LICENSE](LICENSE).
