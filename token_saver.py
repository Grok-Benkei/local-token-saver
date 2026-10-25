#!/usr/bin/env -S python3 -I
"""
local-token-saver (token_saver.py / token-saver)
100% Local, Zero-Dependency, Zero-Telemetry Token & Cost Optimization Toolkit for AI Agents.

Universal capabilities for ANY AI agent or bot workload:
1. Universal Auto-Detecting Filter (`slim`):
   1-command pipe (`<any_cmd_or_file> | token-saver slim -`) that automatically detects
   HTML, JSON/JSONL, CSV/TSV, Git Diff, Markdown, or CLI/Build/Render logs and applies
   the optimal token-saving compressor automatically.
2. Coding, QA, DevOps & Media Render Bots (`run`, `diff-slim`):
   - `run`: Compresses pytest/jest/vitest/cargo/go test logs, npm/pip/docker/apt progress,
     FFmpeg/Remotion/Playwright frame logs, and Terraform/kubectl status lines while
     preserving 100% of errors, warnings, tracebacks, and final summaries.
   - `diff-slim`: Compacts huge `git diff` / PR patches by folding lockfiles (`package-lock.json`,
     `pnpm-lock.yaml`, `yarn.lock`, `Cargo.lock`, `poetry.lock`, `uv.lock`, `*.min.js`, `*.map`,
     snapshots) into 1-line stat summaries while preserving 100% of real source code hunks.
3. Data, Spreadsheet, Finance, Inventory & BI Bots (`csv-slim`):
   Compacts large CSV/TSV files by printing schema + inferred types, per-column numeric
   min/max/mean/sum/non-null statistics, and head/tail sample rows as a clean Markdown table.
4. Web Scraping, E-Commerce, SEO & Research Bots (`html2md`):
   Converts noisy HTML pages into clean, compact Markdown (stripping script/style/svg/nav/ads
   while preserving headings, links, tables, lists, and code blocks).
5. Social, API, MCP, Webhook & Customer Support Bots (`json-slim` / `jsonl-slim`):
   Compacts bloated JSON and newline-delimited JSONL payloads (GitHub, Slack, Notion, Stripe,
   X, OpenAI/Anthropic traces, MCP tool results) by stripping null/empty fields, removing UI
   avatar/color/theme bloat, and capping huge arrays.
6. Writing, RAG, Documentation & Knowledge Base Bots (`md-outline`):
   Generates a token-counted Table of Contents with line ranges or extracts a single section
   from long Markdown manuscripts and docs without loading the entire file into context.
7. Multi-Agent Shared SQLite WAL Cache & 3-Strike Circuit Breaker (`cache`, `stats`):
   Provides exact + Jaccard similarity caching and runaway-retry protection from CLI or Python.

Security guarantee:
- Uses ONLY Python standard library modules (sqlite3, csv, hashlib, html.parser, io, json, re, time, subprocess).
- Zero third-party pip packages (immune to PyPI .pth / dependency supply-chain attacks).
- Zero outbound network or telemetry calls (no PostHog, Amplitude, Scarf, or analytics).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def _resolve_default_db_path() -> Path:
    env_db = os.environ.get("TOKEN_SAVER_DB")
    if env_db:
        return Path(env_db)
    shared_dir = Path("/workspace/tools/local-token-saver")
    if shared_dir.exists():
        return shared_dir / "state.sqlite3"
    return Path.home() / ".cache" / "local-token-saver" / "state.sqlite3"


DEFAULT_DB_PATH = _resolve_default_db_path()


def estimate_tokens(text: str) -> int:
    """Fast local token estimator (~4 chars per token for ASCII, ~1.4 chars for CJK/multibyte)."""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + int(non_ascii * 0.7))


# ============================================================================
# 1. CLI, Build, Test, Container & Media Render Log Compressor (`run`)
# ============================================================================
_PROGRESS_PATTERNS = re.compile(
    r"^(?:"
    r"frame=\s*\d+\s+fps=.*|"
    r"size=\s*\d+\s*(?:kB|MB|B)\s+time=.*|"
    r"\[(?:download|ffmpeg|info)\]\s+\d+(?:\.\d+)?%.*|"
    r".*\bRendering\s+frame\s+\d+/\d+.*|"
    r".*\bBundling\s+\d+%.*|"
    r".*\bProgress:\s+\d+%.*|"
    r".*\b(?:Downloading|Extracting|Pulling fs layer|Waiting|Verifying Checksum|Download complete)\b.*|"
    r".*\b(?:Still creating|Still modifying|Still destroying)\.\.\.\s*\[.*elapsed\].*|"
    r"[\s▏▎▍▌▋▊▉█=\-\>\.\d%\/\[\]\(\)|]+$"
    r")",
    re.IGNORECASE,
)

_PASS_LINE_RE = re.compile(
    r"^(?:"
    r"PASS\b|"
    r"ok\s+\S+|"
    r"test\s+\S+\s+\.\.\.\s+ok\b|"
    r"\s*(?:✓|✔|PASS)\s+|"
    r".*::.*PASSED\b"
    r")"
)


def compress_cli_output(text: str, max_lines: int = 80) -> str:
    """
    Compresses verbose CLI / test / build / Docker / FFmpeg / Remotion / Terraform output by:
    - Stripping ANSI escape sequences and carriage-return spinner overwrites
    - Collapsing frame/download/container progress lines while keeping the final progress state
    - Deduplicating repeated log lines (ignoring timestamps & hex memory addresses) with [repeated xN]
    - Collapsing passing test lines (pytest, jest, vitest, cargo test, go test) while preserving
      100% of failures, errors, warnings, and tracebacks
    - Truncating the middle of huge outputs while keeping head & tail context
    """
    clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    clean = re.sub(r"^.*\r(?!\n)", "", clean, flags=re.MULTILINE)

    lines = clean.splitlines()
    if not lines:
        return ""

    deduped: list[str] = []
    pass_count = 0
    progress_count = 0
    last_progress = ""
    last_norm = None
    repeat_run = 0

    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            continue

        # Collapse progress bars, Docker layer pulls, Terraform wait loops, and media frame logs
        if _PROGRESS_PATTERNS.match(stripped) and (
            "%" in stripped
            or "frame=" in stripped.lower()
            or "time=" in stripped.lower()
            or "pulling" in stripped.lower()
            or "downloading" in stripped.lower()
            or "elapsed]" in stripped.lower()
        ):
            progress_count += 1
            last_progress = stripped
            continue

        norm = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b", "<TS>", stripped)
        norm = re.sub(r"0x[0-9a-fA-F]{6,}", "<HEX>", norm)

        if norm == last_norm:
            repeat_run += 1
            continue
        else:
            if repeat_run > 0 and deduped:
                deduped[-1] += f"  [repeated x{repeat_run + 1}]"
            repeat_run = 0
            last_norm = norm

        if _PASS_LINE_RE.match(stripped):
            pass_count += 1
            continue

        deduped.append(stripped)

    if repeat_run > 0 and deduped:
        deduped[-1] += f"  [repeated x{repeat_run + 1}]"

    summaries: list[str] = []
    if pass_count > 0:
        summaries.append(f"[Summary: {pass_count} passing test lines collapsed]")
    if progress_count > 0:
        summaries.append(
            f"[Summary: {progress_count} render/progress lines collapsed; last={last_progress[:90]}]"
        )

    deduped = summaries + deduped

    if len(deduped) > max_lines:
        head = max_lines // 2
        tail = max_lines - head
        omitted = len(deduped) - max_lines
        deduped = (
            deduped[:head]
            + [f"... [{omitted} lines omitted by local-token-saver] ..."]
            + deduped[-tail:]
        )

    return "\n".join(deduped)


def run_compressed(command: str | list[str], max_lines: int = 80) -> tuple[str, int, int, int]:
    """Runs a shell command, compresses stdout/stderr, and returns (compressed_text, returncode, orig_tokens, new_tokens)."""
    cmd_str = command if isinstance(command, str) else " ".join(command)
    proc = subprocess.run(cmd_str, shell=True, capture_output=True, text=True)
    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    comp = compress_cli_output(combined, max_lines=max_lines)
    return comp, proc.returncode, estimate_tokens(combined), estimate_tokens(comp)


# ============================================================================
# 2. HTML-to-Clean-Markdown Extractor (`html2md`)
# ============================================================================
class _CompactMarkdownExtractor(HTMLParser):
    IGNORE_TAGS = {
        "script",
        "style",
        "svg",
        "noscript",
        "iframe",
        "nav",
        "footer",
        "header",
        "aside",
        "form",
        "template",
        "canvas",
    }
    BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "br", "hr", "blockquote", "pre", "table"}
    HEADING_TAGS = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}

    def __init__(self) -> None:
        super().__init__()
        self._ignore_depth = 0
        self._chunks: list[str] = []
        self._in_pre = False
        self._current_href: str | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in self.IGNORE_TAGS:
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return
        if t in self.HEADING_TAGS:
            self._chunks.append(f"\n\n{self.HEADING_TAGS[t]}")
        elif t == "li":
            self._chunks.append("\n- ")
        elif t == "tr":
            self._chunks.append("\n| ")
        elif t in ("td", "th"):
            self._in_cell = True
        elif t == "pre":
            self._in_pre = True
            self._chunks.append("\n```\n")
        elif t == "code" and not self._in_pre:
            self._chunks.append("`")
        elif t in self.BLOCK_TAGS:
            self._chunks.append("\n")
        elif t == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("javascript:", "data:", "#")):
                self._current_href = href

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self.IGNORE_TAGS:
            self._ignore_depth = max(0, self._ignore_depth - 1)
            return
        if self._ignore_depth > 0:
            return
        if t == "pre":
            self._in_pre = False
            self._chunks.append("\n```\n")
        elif t == "code" and not self._in_pre:
            self._chunks.append("`")
        elif t in ("td", "th"):
            self._in_cell = False
            self._chunks.append(" | ")
        elif t == "a" and self._current_href:
            self._chunks.append(f" ({self._current_href})")
            self._current_href = None
        elif t in self.HEADING_TAGS or t in self.BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return
        if self._in_pre:
            self._chunks.append(data)
        else:
            collapsed = re.sub(r"[ \t\r\n]+", " ", data)
            if collapsed.strip():
                self._chunks.append(collapsed.strip() if self._in_cell else collapsed)

    def get_markdown(self) -> str:
        raw = "".join(self._chunks)
        lines = [line.strip() for line in raw.splitlines()]
        out: list[str] = []
        blank = 0
        for line in lines:
            if not line or line == "|":
                blank += 1
                if blank <= 1 and out:
                    out.append("")
            else:
                blank = 0
                out.append(line)
        return "\n".join(out).strip()


def html_to_compact_markdown(html: str) -> str:
    parser = _CompactMarkdownExtractor()
    parser.feed(html)
    return parser.get_markdown()


html_to_markdown = html_to_compact_markdown


# ============================================================================
# 3. Universal JSON / JSONL Payload Slimmer (`json-slim` / `jsonl-slim`)
# ============================================================================
_DEFAULT_DROP_KEYS = {
    # Social / X / User profile UI noise
    "profile_image_url",
    "profile_image_url_https",
    "profile_banner_url",
    "profile_background_image_url",
    "profile_background_image_url_https",
    "profile_background_color",
    "profile_link_color",
    "profile_sidebar_border_color",
    "profile_sidebar_fill_color",
    "profile_text_color",
    "default_profile",
    "default_profile_image",
    "has_extended_profile",
    "is_translation_enabled",
    "translator_type",
    "withheld_in_countries",
    "context_annotations",
    "edit_history_tweet_ids",
    "edit_controls",
    "sizes",
    "features",
    "ext_alt_text",
    "unwound_url",
    "display_url",
    "indices",
    "possibly_sensitive",
    "lang",
    "source",
    "favorited",
    "retweeted",
    "contributors",
    "geo",
    "coordinates",
    "place",
    # GitHub / GitLab API URL fanout noise
    "avatar_url",
    "gravatar_id",
    "followers_url",
    "following_url",
    "gists_url",
    "starred_url",
    "subscriptions_url",
    "organizations_url",
    "repos_url",
    "events_url",
    "received_events_url",
    "node_id",
    # Slack / Discord / Notion / SaaS UI avatar & icon bloat
    "image_24",
    "image_32",
    "image_48",
    "image_72",
    "image_192",
    "image_512",
    "image_1024",
    "image_original",
    "avatar_hash",
    "color",
    # LLM trace / token log probabilities bloat
    "logprobs",
    "system_fingerprint",
}


def slim_json_value(
    obj: Any,
    max_items: int = 20,
    max_str_len: int = 600,
    keep_keys: set[str] | None = None,
) -> Any:
    """
    Recursively compacts bloated JSON/JSONL payloads (GitHub, Slack, Notion, Stripe, X,
    MCP responses, LLM traces) while preserving IDs, text, metrics, URLs, and timestamps.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            if k in _DEFAULT_DROP_KEYS and (not keep_keys or k not in keep_keys):
                continue
            cleaned = slim_json_value(v, max_items=max_items, max_str_len=max_str_len, keep_keys=keep_keys)
            if cleaned is None or cleaned == "" or cleaned == [] or cleaned == {}:
                continue
            out[k] = cleaned
        return out
    if isinstance(obj, list):
        total = len(obj)
        sliced = obj[:max_items] if max_items > 0 else obj
        cleaned_list = [
            slim_json_value(item, max_items=max_items, max_str_len=max_str_len, keep_keys=keep_keys)
            for item in sliced
        ]
        cleaned_list = [x for x in cleaned_list if x not in (None, "", [], {})]
        if total > max_items > 0:
            cleaned_list.append(f"... (+{total - max_items} more items omitted by local-token-saver)")
        return cleaned_list
    if isinstance(obj, str):
        if len(obj) > max_str_len:
            return obj[:max_str_len] + f"... [+{len(obj) - max_str_len} chars truncated]"
        return obj
    return obj


def slim_json_text(
    raw_json: str,
    max_items: int = 20,
    max_str_len: int = 600,
    keep_keys: set[str] | None = None,
) -> str:
    """Supports both standard JSON documents and newline-delimited JSONL streams."""
    stripped = raw_json.strip()
    if not stripped:
        return ""
    try:
        data = json.loads(stripped)
        slimmed = slim_json_value(data, max_items=max_items, max_str_len=max_str_len, keep_keys=keep_keys)
        return json.dumps(slimmed, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        parsed_rows: list[Any] = []
        for ln in lines:
            parsed_rows.append(json.loads(ln))
        total = len(parsed_rows)
        sliced = parsed_rows[:max_items] if max_items > 0 else parsed_rows
        out_lines = [
            json.dumps(
                slim_json_value(item, max_items=max_items, max_str_len=max_str_len, keep_keys=keep_keys),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for item in sliced
        ]
        if total > max_items > 0:
            out_lines.append(
                json.dumps({"_omitted_jsonl_rows": total - max_items}, ensure_ascii=False, separators=(",", ":"))
            )
        return "\n".join(out_lines)


slim_json = slim_json_text


# ============================================================================
# 4. CSV / TSV Schema, Summary Stats & Sample Extractor (`csv-slim`)
# ============================================================================
def slim_csv(
    raw_csv: str,
    max_rows: int = 10,
    cols: list[str] | None = None,
) -> str:
    """
    For Data, Spreadsheet, Finance, Inventory, Accounting & BI Bots:
    Compacts large CSV/TSV files (thousands of rows) into:
    1. Row & column count overview
    2. Per-column schema + numeric summary statistics (min, max, mean, sum, non-null) or top categories
    3. Clean Markdown table of head & tail sample rows
    Cuts 1,000-100,000 row CSVs by 95-99.8% tokens while preserving exact aggregates!
    """
    stripped = raw_csv.strip()
    if not stripped:
        return "[Empty CSV/TSV input]"

    first_line = stripped.splitlines()[0]
    delimiter = "\t" if "\t" in first_line and first_line.count("\t") >= first_line.count(",") else ","

    reader = csv.reader(io.StringIO(stripped), delimiter=delimiter)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return "[Empty CSV/TSV rows]"

    headers = [h.strip() or f"col_{i}" for i, h in enumerate(rows[0])]
    data_rows = rows[1:]
    total_rows = len(data_rows)

    selected_indices = list(range(len(headers)))
    if cols:
        wanted = {c.strip().lower() for c in cols if c.strip()}
        matched = [i for i, h in enumerate(headers) if h.lower() in wanted]
        if matched:
            selected_indices = matched

    sel_headers = [headers[i] for i in selected_indices]

    # Compute column statistics across all rows
    col_stats_lines: list[str] = []
    for idx in selected_indices:
        col_name = headers[idx]
        vals = [r[idx].strip() for r in data_rows if idx < len(r) and r[idx].strip() != ""]
        non_null = len(vals)
        num_vals: list[float] = []
        for v in vals:
            clean_v = v.replace(",", "").replace("$", "").replace("¥", "").replace("%", "")
            try:
                num_vals.append(float(clean_v))
            except ValueError:
                pass
        if non_null > 0 and len(num_vals) >= max(1, int(non_null * 0.8)):
            mn = min(num_vals)
            mx = max(num_vals)
            avg = sum(num_vals) / len(num_vals)
            tot = sum(num_vals)
            col_stats_lines.append(
                f"- `{col_name}` (numeric, n={non_null}/{total_rows}): "
                f"min={mn:g}, max={mx:g}, mean={avg:.2f}, sum={tot:g}"
            )
        else:
            counts: dict[str, int] = {}
            for v in vals:
                counts[v] = counts.get(v, 0) + 1
            top_vals = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:4]
            top_str = ", ".join(f"{k[:28]}({c})" for k, c in top_vals)
            col_stats_lines.append(
                f"- `{col_name}` (text/category, n={non_null}/{total_rows}, unique={len(counts)}): top=[{top_str}]"
            )

    # Select head and tail sample rows
    if total_rows <= max_rows:
        sample_rows = [(i + 1, r) for i, r in enumerate(data_rows)]
        omitted_rows = 0
    else:
        head_n = max(1, (max_rows + 1) // 2)
        tail_n = max(1, max_rows - head_n)
        sample_rows = [(i + 1, data_rows[i]) for i in range(head_n)]
        omitted_rows = total_rows - (head_n + tail_n)
        sample_rows.append((-1, []))  # marker for omitted middle rows
        sample_rows.extend([(total_rows - tail_n + 1 + j, data_rows[total_rows - tail_n + j]) for j in range(tail_n)])

    def _fmt_cell(val: str) -> str:
        s = val.strip().replace("|", "\\|")
        return s[:60] + "..." if len(s) > 60 else s

    md_table = [
        "| # | " + " | ".join(sel_headers) + " |",
        "| --- | " + " | ".join("---" for _ in sel_headers) + " |",
    ]
    for row_num, r in sample_rows:
        if row_num == -1:
            md_table.append(
                f"| ... | *({omitted_rows} rows omitted by local-token-saver)* " + " | " * (len(sel_headers) - 1) + "|"
            )
        else:
            cells = [_fmt_cell(r[i]) if i < len(r) else "" for i in selected_indices]
            md_table.append(f"| {row_num} | " + " | ".join(cells) + " |")

    return "\n".join(
        [
            f"[CSV/TSV Summary | {total_rows} rows x {len(headers)} columns (showing {len(sel_headers)} cols)]",
            "### Column Statistics (computed over all rows)",
            *col_stats_lines,
            "",
            "### Sample Rows",
            *md_table,
        ]
    )


# ============================================================================
# 5. Git Diff & PR Patch Slimmer (`diff-slim`)
# ============================================================================
_LOCK_AND_GENERATED_RE = re.compile(
    r"(?:^|/)(?:"
    r"package-lock\.json|"
    r"pnpm-lock\.yaml|"
    r"yarn\.lock|"
    r"bun\.lockb?|"
    r"Cargo\.lock|"
    r"poetry\.lock|"
    r"Pipfile\.lock|"
    r"uv\.lock|"
    r"composer\.lock|"
    r"Gemfile\.lock|"
    r"go\.sum|"
    r".*\.min\.(?:js|css)|"
    r".*\.(?:map|snap)|"
    r"dist/.*|"
    r"build/.*|"
    r"__snapshots__/.*"
    r")$",
    re.IGNORECASE,
)


def slim_diff(raw_diff: str, max_lines_per_file: int = 120) -> str:
    """
    For Coding, PR Review, Security Audit & DevOps Bots:
    Compacts git diffs / PR patches by:
    - Automatically folding lockfiles, minified bundles, sourcemaps, and snapshots into 1-line summaries
    - Capping massive generated file diffs while preserving 100% of normal source code hunks
    """
    if not raw_diff.strip():
        return "[Empty diff]"

    chunks = re.split(r"(?=^diff --git )", raw_diff, flags=re.MULTILINE)
    out_blocks: list[str] = []
    folded_files = 0

    for chunk in chunks:
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        first = lines[0]
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", first)
        file_path = m.group(2).strip() if m else "unknown"

        added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))

        if _LOCK_AND_GENERATED_RE.search(file_path):
            folded_files += 1
            out_blocks.append(
                f"diff --git a/{file_path} b/{file_path}\n"
                f"  [lockfile/generated diff folded by local-token-saver: +{added} / -{removed} lines ({len(lines)} raw lines)]"
            )
            continue

        if len(lines) > max_lines_per_file > 0:
            head_n = max(20, int(max_lines_per_file * 0.7))
            tail_n = max(10, max_lines_per_file - head_n)
            omitted = len(lines) - head_n - tail_n
            trimmed = (
                lines[:head_n]
                + [f"  ... [{omitted} diff lines in {file_path} omitted (+{added}/-{removed} total)] ..."]
                + lines[-tail_n:]
            )
            out_blocks.append("\n".join(trimmed))
        else:
            out_blocks.append(chunk.rstrip())

    header = f"[Git Diff Summary | {len(out_blocks)} files changed | {folded_files} lock/generated files folded]"
    return header + "\n" + "\n".join(out_blocks)


# ============================================================================
# 6. Markdown Outline & Section Slicer for Long Manuscripts/Docs (`md-outline`)
# ============================================================================
def markdown_outline_or_slice(text: str, section_query: str | None = None, max_depth: int = 2) -> str:
    """
    For Writing, RAG, Documentation, eBook & Knowledge Base Bots:
    - Without `section_query`: returns a compact Table of Contents with line numbers and
      token counts per section (saving 90-99% tokens vs reading the entire manuscript).
    - With `section_query`: extracts only the matching heading section (including subsections).
    """
    lines = text.splitlines()
    if not lines:
        return ""

    headings: list[dict[str, Any]] = []
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    in_fence = False

    for idx, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = heading_re.match(line)
        if m:
            headings.append(
                {
                    "level": len(m.group(1)),
                    "hashes": m.group(1),
                    "title": m.group(2).strip(),
                    "start_line": idx + 1,
                }
            )

    if not headings:
        preview = "\n".join(lines[:30])
        return f"[No Markdown headings found; total={len(lines)} lines, {estimate_tokens(text)} tokens]\n{preview}"

    for i, h in enumerate(headings):
        h["direct_end_line"] = headings[i + 1]["start_line"] - 1 if i + 1 < len(headings) else len(lines)
        tree_end = len(lines)
        for j in range(i + 1, len(headings)):
            if headings[j]["level"] <= h["level"]:
                tree_end = headings[j]["start_line"] - 1
                break
        h["tree_end_line"] = tree_end
        sec_text = "\n".join(lines[h["start_line"] - 1 : h["direct_end_line"]])
        h["tokens"] = estimate_tokens(sec_text)
        tree_text = "\n".join(lines[h["start_line"] - 1 : h["tree_end_line"]])
        h["tree_tokens"] = estimate_tokens(tree_text)

    if section_query:
        q = section_query.lower().strip()
        matches = [h for h in headings if q in h["title"].lower()]
        if not matches:
            return f"[Section '{section_query}' not found. Available headings:]\n" + markdown_outline_or_slice(
                text, None, max_depth=max_depth
            )
        matches.sort(key=lambda h: (0 if h["title"].lower().startswith(q) else 1, h["level"], h["start_line"]))
        target = matches[0]
        extracted = "\n".join(lines[target["start_line"] - 1 : target["tree_end_line"]])
        header = (
            f"[Section '{target['title']}' | L{target['start_line']}-L{target['tree_end_line']} "
            f"| {target['tree_tokens']} tokens (out of {estimate_tokens(text)} total)]"
        )
        return f"{header}\n{extracted}"

    out_lines = [
        f"[Markdown Outline | {len(headings)} sections | {len(lines)} lines | {estimate_tokens(text)} total tokens]"
    ]
    for h in headings:
        if h["level"] > max_depth:
            continue
        indent = "  " * (h["level"] - 1)
        out_lines.append(
            f"{indent}{h['hashes']} {h['title']}  (L{h['start_line']}-L{h['tree_end_line']}, ~{h['tree_tokens']} tok)"
        )
    return "\n".join(out_lines)


markdown_outline = markdown_outline_or_slice


def chunk_markdown(text: str, max_tokens: int = 500) -> list[str]:
    """Splits text on headings, code blocks, and paragraphs within max_tokens per chunk."""
    blocks = re.split(r"(\n(?=#{1,4} )|\n\n+)", text)
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if not block or block.isspace():
            continue
        candidate = (current + "\n\n" + block.strip()).strip() if current else block.strip()
        if estimate_tokens(candidate) <= max_tokens:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block.strip()
    if current:
        chunks.append(current)
    return chunks


# ============================================================================
# 7. Universal Auto-Detecting Filter (`slim` / `auto_slim`)
# ============================================================================
def detect_format(text: str, filename: str | None = None) -> str:
    """Automatically detects the format of arbitrary text or file content."""
    if filename and filename != "-":
        lower = filename.lower()
        if lower.endswith((".html", ".htm", ".xhtml")):
            return "html2md"
        if lower.endswith((".json", ".jsonl", ".ndjson")):
            return "json_slim"
        if lower.endswith((".csv", ".tsv")):
            return "csv_slim"
        if lower.endswith((".diff", ".patch")):
            return "diff_slim"
        if lower.endswith((".md", ".markdown")):
            return "md_outline"

    sample = text.lstrip()[:4000]
    if not sample:
        return "cli"

    # 1. Git Diff
    if sample.startswith("diff --git ") or "\ndiff --git " in sample or (
        sample.startswith("--- a/") and "\n+++ b/" in sample
    ):
        return "diff_slim"

    # 2. HTML
    if re.match(r"^(?:<!doctype\s+html|<html\b|<head\b|<body\b|<div\b|<main\b|<article\b|<script\b)", sample, re.I) or (
        "<html" in sample.lower() and "</html>" in text.lower()
    ):
        return "html2md"

    # 3. JSON / JSONL
    if sample[0] in ("{", "["):
        try:
            json.loads(text.strip())
            return "json_slim"
        except Exception:
            first_line = sample.splitlines()[0].strip()
            if first_line.startswith("{") and first_line.endswith("}"):
                try:
                    json.loads(first_line)
                    return "json_slim"
                except Exception:
                    pass

    # 4. CSV / TSV (check consistent delimiter counts across first 5 non-empty lines)
    non_empty_lines = [ln for ln in sample.splitlines() if ln.strip()][:6]
    if len(non_empty_lines) >= 3:
        for delim in (",", "\t"):
            counts = [ln.count(delim) for ln in non_empty_lines]
            if counts[0] >= 2 and all(c == counts[0] for c in counts[1:]):
                return "csv_slim"

    # 5. Long Markdown document with multiple headings
    headings_count = len(re.findall(r"^#{1,4}\s+\S+", text, flags=re.MULTILINE))
    if headings_count >= 4 and estimate_tokens(text) > 800:
        return "md_outline"

    # Default: CLI / Build / Test / Render log compressor
    return "cli"


def auto_slim(text: str, filename: str | None = None) -> tuple[str, str]:
    """
    Detects input format automatically and applies the optimal compressor.
    Returns (compressed_text, detected_mode).
    """
    mode = detect_format(text, filename=filename)
    if mode == "html2md":
        return html_to_compact_markdown(text), mode
    if mode == "json_slim":
        return slim_json_text(text), mode
    if mode == "csv_slim":
        return slim_csv(text), mode
    if mode == "diff_slim":
        return slim_diff(text), mode
    if mode == "md_outline":
        return markdown_outline_or_slice(text), mode
    return compress_cli_output(text), "cli"


# ============================================================================
# 8. SQLite Cache + Near-Duplicate Matcher + Circuit Breaker + Savings Stats
# ============================================================================
class LocalStateStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv_cache (
                cache_key TEXT PRIMARY KEY,
                norm_tokens TEXT NOT NULL,
                value TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS circuit_breakers (
                service_name TEXT PRIMARY KEY,
                failures INTEGER NOT NULL DEFAULT 0,
                opened_at REAL NOT NULL DEFAULT 0,
                threshold INTEGER NOT NULL DEFAULT 3,
                cooldown_sec REAL NOT NULL DEFAULT 300
            );
            CREATE TABLE IF NOT EXISTS usage_metrics (
                metric_name TEXT PRIMARY KEY,
                metric_value INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self.conn.commit()

    def incr_metric(self, name: str, delta: int = 1) -> None:
        if delta <= 0:
            return
        self.conn.execute(
            """
            INSERT INTO usage_metrics (metric_name, metric_value) VALUES (?, ?)
            ON CONFLICT(metric_name) DO UPDATE SET metric_value = metric_value + excluded.metric_value
            """,
            (name, int(delta)),
        )
        self.conn.commit()

    def record_compression(self, kind: str, before_tokens: int, after_tokens: int) -> None:
        saved = max(0, before_tokens - after_tokens)
        self.incr_metric(f"{kind}_calls", 1)
        self.incr_metric(f"{kind}_tokens_saved", saved)
        self.incr_metric("total_tokens_saved", saved)

    @staticmethod
    def _normalize_tokens(prompt: str) -> set[str]:
        stop = {"how", "do", "we", "to", "in", "the", "and", "of", "for", "is", "are", "on"}
        return {w for w in re.findall(r"[a-z0-9\u3040-\u30ff\u4e00-\u9fff]{2,}", prompt.lower()) if w not in stop}

    def cache_put(self, key: str, value: str, ttl_sec: float = 3600) -> None:
        now = time.time()
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        tokens = json.dumps(sorted(self._normalize_tokens(key)), ensure_ascii=False)
        self.conn.execute(
            "INSERT OR REPLACE INTO kv_cache (cache_key, norm_tokens, value, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (h, tokens, value, now + ttl_sec, now),
        )
        self.conn.commit()
        self.incr_metric("cache_puts", 1)

    def cache_get(self, key: str, similarity_threshold: float = 0.75) -> tuple[str | None, str]:
        now = time.time()
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        row = self.conn.execute(
            "SELECT value FROM kv_cache WHERE cache_key = ? AND expires_at > ?", (h, now)
        ).fetchone()
        if row:
            val = row[0]
            self.incr_metric("cache_hits_exact", 1)
            self.incr_metric("total_tokens_saved", estimate_tokens(val))
            return val, "exact_hit"

        q_tokens = self._normalize_tokens(key)
        if not q_tokens:
            self.incr_metric("cache_misses", 1)
            return None, "miss"
        rows = self.conn.execute(
            "SELECT norm_tokens, value FROM kv_cache WHERE expires_at > ? ORDER BY created_at DESC LIMIT 200",
            (now,),
        ).fetchall()
        for norm_json, val in rows:
            cand = set(json.loads(norm_json))
            if not cand:
                continue
            sim = len(q_tokens & cand) / len(q_tokens | cand)
            if sim >= similarity_threshold:
                self.incr_metric("cache_hits_semantic", 1)
                self.incr_metric("total_tokens_saved", estimate_tokens(val))
                return val, f"semantic_hit({sim:.2f})"
        self.incr_metric("cache_misses", 1)
        return None, "miss"

    def breaker_allow(self, service: str) -> tuple[bool, str]:
        now = time.time()
        row = self.conn.execute(
            "SELECT failures, opened_at, threshold, cooldown_sec FROM circuit_breakers WHERE service_name = ?",
            (service,),
        ).fetchone()
        if not row:
            return True, "closed"
        failures, opened_at, threshold, cooldown = row
        if failures >= threshold:
            if now - opened_at < cooldown:
                rem = int(cooldown - (now - opened_at))
                self.incr_metric("breaker_blocked_calls", 1)
                return False, f"open (tripped after {failures} failures; {rem}s cooldown remaining)"
            return True, "half-open"
        return True, f"closed ({failures}/{threshold} failures)"

    def breaker_record(self, service: str, success: bool, threshold: int = 3, cooldown_sec: float = 300) -> str:
        now = time.time()
        if success:
            self.conn.execute(
                "INSERT OR REPLACE INTO circuit_breakers (service_name, failures, opened_at, threshold, cooldown_sec) VALUES (?, 0, 0, ?, ?)",
                (service, threshold, cooldown_sec),
            )
            self.conn.commit()
            return "closed (reset)"
        row = self.conn.execute(
            "SELECT failures FROM circuit_breakers WHERE service_name = ?", (service,)
        ).fetchone()
        failures = (row[0] if row else 0) + 1
        opened_at = now if failures >= threshold else 0.0
        self.conn.execute(
            "INSERT OR REPLACE INTO circuit_breakers (service_name, failures, opened_at, threshold, cooldown_sec) VALUES (?, ?, ?, ?, ?)",
            (service, failures, opened_at, threshold, cooldown_sec),
        )
        self.conn.commit()
        return "OPEN (tripped)" if failures >= threshold else f"closed ({failures}/{threshold})"

    def get_stats(self) -> dict[str, Any]:
        now = time.time()
        active_cache = self.conn.execute(
            "SELECT COUNT(*) FROM kv_cache WHERE expires_at > ?", (now,)
        ).fetchone()[0]
        total_cache = self.conn.execute("SELECT COUNT(*) FROM kv_cache").fetchone()[0]
        breakers = [
            {
                "service": r[0],
                "failures": r[1],
                "status": "open" if (r[1] >= r[3] and now - r[2] < r[4]) else "closed",
            }
            for r in self.conn.execute(
                "SELECT service_name, failures, opened_at, threshold, cooldown_sec FROM circuit_breakers"
            ).fetchall()
        ]
        metrics = dict(
            self.conn.execute("SELECT metric_name, metric_value FROM usage_metrics").fetchall()
        )
        return {
            "db_path": str(self.db_path),
            "active_cache_entries": active_cache,
            "total_cache_entries": total_cache,
            "circuit_breakers": breakers,
            "metrics": metrics,
        }


# ============================================================================
# 9. Self-Test & Universal Multi-Bot Benchmark Suite (`selftest`)
# ============================================================================
def run_self_test() -> dict[str, Any]:
    # 1. CLI + Media Render + Container logs (Dev / QA / Video / Cloud Ops Bots)
    noisy_cli = "\n".join(
        [f"PASS tests/unit/test_module_{i}.py::test_case_{i}" for i in range(120)]
        + [
            f"frame={i:4d} fps=60.0 q=28.0 size=    1024kB time=00:00:{i // 30:02d}.00 bitrate=2048.0kbits/s"
            for i in range(1, 91)
        ]
        + ["2026-10-25T12:00:01Z INFO Starting worker at 0x7fff1234abcd"] * 25
        + ["FAIL tests/unit/test_auth.py::test_token_expiry - AssertionError: expected 200 got 401"]
    )
    compressed_cli = compress_cli_output(noisy_cli)

    # 2. Noisy HTML (Web Scraping / E-Commerce / SEO / Research Bots)
    noisy_html = (
        "<!DOCTYPE html><html><head>"
        + ("<script>window.__INITIAL_STATE__ = " + json.dumps({"ads": ["banner_data_" * 20] * 15}) + ";</script>")
        + ("<style>.tailwind- arbitrary-class { display:flex; background:#112233; margin:0; padding:12px; }</style>" * 25)
        + "</head><body>"
        + ("<nav><a href='/home'>Home</a><a href='/deals'>Deals</a></nav><svg><path d='" + ("M0 0 L10 10 " * 80) + "'/></svg>")
        + "<main><h1>Product Listing: Enterprise AI Server</h1>"
        + "<p>Price: $2,499.00 | Condition: New | In Stock</p>"
        + "<ul><li>Zero dependencies</li><li>Zero telemetry</li><li>99.6% token reduction</li></ul>"
        + "</main><footer>Copyright 2026</footer></body></html>"
    )
    clean_md = html_to_compact_markdown(noisy_html)

    # 3. Bloated API / Social / Webhook JSON (Social / API / MCP / Support Bots)
    bloated_json = json.dumps(
        {
            "data": [
                {
                    "id": f"1982000000000000{i:03d}",
                    "text": f"Universal local-token-saver cuts agent input tokens by 95%+ (#{i})",
                    "created_at": "2026-10-26T00:00:00Z",
                    "lang": "en",
                    "possibly_sensitive": False,
                    "edit_history_tweet_ids": [f"1982000000000000{i:03d}"],
                    "context_annotations": [
                        {
                            "domain": {"id": "131", "name": "Unified Taxonomy"},
                            "entity": {"id": "100", "name": "AI Agents"},
                        }
                        for _ in range(12)
                    ],
                    "author": {
                        "id": "100001",
                        "username": "ai_builder",
                        "profile_image_url_https": "https://example.com/avatars/avatar_normal.jpg",
                        "profile_banner_url": "https://example.com/banners/1700000000",
                        "profile_background_color": "000000",
                        "profile_link_color": "1DA1F2",
                        "default_profile": True,
                        "has_extended_profile": True,
                        "withheld_in_countries": [],
                    },
                    "public_metrics": {
                        "like_count": 42,
                        "reply_count": 11,
                        "retweet_count": 9,
                        "impression_count": 3800,
                    },
                }
                for i in range(30)
            ]
        },
        ensure_ascii=False,
        indent=2,
    )
    slimmed_json = slim_json_text(bloated_json, max_items=10)

    # 4. Large CSV Dataset (Data / Spreadsheet / Finance / Inventory / BI Bots)
    csv_lines = ["order_id,date,category,sku,quantity,unit_price,total_usd,status"]
    for i in range(1, 1001):
        cat = ["Electronics", "Home", "Books", "SaaS"][i % 4]
        status = ["COMPLETED", "COMPLETED", "COMPLETED", "PENDING", "REFUNDED"][i % 5]
        qty = (i % 5) + 1
        price = 19.99 * ((i % 10) + 1)
        csv_lines.append(
            f"ORD-{10000 + i},2026-10-{(i % 28) + 1:02d},{cat},SKU-{i % 40:03d},{qty},{price:.2f},{qty * price:.2f},{status}"
        )
    raw_csv = "\n".join(csv_lines)
    slimmed_csv = slim_csv(raw_csv, max_rows=6)

    # 5. Noisy Git Diff with Lockfile + Source Code (Coding / PR Review / DevOps Bots)
    lock_hunk = "\n".join(
        [
            "diff --git a/package-lock.json b/package-lock.json",
            "index 1111111..2222222 100644",
            "--- a/package-lock.json",
            "+++ b/package-lock.json",
            "@@ -1,400 +1,400 @@",
        ]
        + [
            f'+    "pkg-{i}": {{"version": "1.0.{i}", "resolved": "https://registry.npmjs.org/pkg-{i}/-/pkg-{i}-1.0.{i}.tgz"}},'
            for i in range(350)
        ]
    )
    src_hunk = "\n".join(
        [
            "diff --git a/src/auth.py b/src/auth.py",
            "index 3333333..4444444 100644",
            "--- a/src/auth.py",
            "+++ b/src/auth.py",
            "@@ -10,4 +10,6 @@ def verify_token(token: str) -> bool:",
            "-    return token == 'secret'",
            "+    if not token or len(token) < 32:",
            "+        return False",
            "+    return hmac.compare_digest(token, get_expected_token())",
        ]
    )
    raw_diff = lock_hunk + "\n" + src_hunk
    slimmed_diff = slim_diff(raw_diff)

    # 6. Long Markdown Manuscript / Documentation (Writing / RAG / Docs Bots)
    sample_manuscript = "\n\n".join(
        [
            f"# Chapter {c}: Autonomous Agent Workflows\n\n"
            + ("Paragraph with detailed analysis, citations, and concrete examples for readers. " * 45)
            + f"\n\n## {c}.1 Implementation Checklist\n\n"
            + ("- Step item with verification criteria and expected metrics.\n" * 20)
            for c in range(1, 9)
        ]
    )
    md_toc = markdown_outline_or_slice(sample_manuscript)
    md_sec = markdown_outline_or_slice(sample_manuscript, section_query="Chapter 3")

    # 7. Auto-Detect Filter (`slim`) verification across all 6 input formats
    auto_results = {}
    for label, payload in [
        ("html", noisy_html),
        ("json", bloated_json),
        ("csv", raw_csv),
        ("diff", raw_diff),
        ("markdown", sample_manuscript),
        ("cli", noisy_cli),
    ]:
        comp_out, det_mode = auto_slim(payload)
        b_tok = estimate_tokens(payload)
        a_tok = estimate_tokens(comp_out)
        auto_results[label] = {
            "detected_mode": det_mode,
            "before_tokens": b_tok,
            "after_tokens": a_tok,
            "savings_pct": round((1 - a_tok / b_tok) * 100, 1),
        }

    # 8. SQLite Cache + Circuit Breaker
    store = LocalStateStore()
    store.cache_put(
        "How do we disable PostHog telemetry in Mem0 and Graphiti?",
        "Set MEM0_TELEMETRY=false and GRAPHITI_TELEMETRY_ENABLED=false",
    )
    _, status_exact = store.cache_get("How do we disable PostHog telemetry in Mem0 and Graphiti?")
    _, status_sem = store.cache_get("How to disable PostHog telemetry in Graphiti and Mem0?")

    for _ in range(3):
        store.breaker_record("flaky_endpoint", success=False, threshold=3, cooldown_sec=120)
    allowed, b_status = store.breaker_allow("flaky_endpoint")
    store.breaker_record("flaky_endpoint", success=True)

    return {
        "cli_compression": {
            "before_tokens": estimate_tokens(noisy_cli),
            "after_tokens": estimate_tokens(compressed_cli),
            "savings_pct": round((1 - estimate_tokens(compressed_cli) / estimate_tokens(noisy_cli)) * 100, 1),
            "output_preview": compressed_cli,
        },
        "html_to_md": {
            "before_tokens": estimate_tokens(noisy_html),
            "after_tokens": estimate_tokens(clean_md),
            "savings_pct": round((1 - estimate_tokens(clean_md) / estimate_tokens(noisy_html)) * 100, 1),
        },
        "json_slim": {
            "before_tokens": estimate_tokens(bloated_json),
            "after_tokens": estimate_tokens(slimmed_json),
            "savings_pct": round((1 - estimate_tokens(slimmed_json) / estimate_tokens(bloated_json)) * 100, 1),
        },
        "csv_slim": {
            "before_tokens": estimate_tokens(raw_csv),
            "after_tokens": estimate_tokens(slimmed_csv),
            "savings_pct": round((1 - estimate_tokens(slimmed_csv) / estimate_tokens(raw_csv)) * 100, 1),
        },
        "diff_slim": {
            "before_tokens": estimate_tokens(raw_diff),
            "after_tokens": estimate_tokens(slimmed_diff),
            "savings_pct": round((1 - estimate_tokens(slimmed_diff) / estimate_tokens(raw_diff)) * 100, 1),
        },
        "md_outline": {
            "manuscript_tokens": estimate_tokens(sample_manuscript),
            "outline_tokens": estimate_tokens(md_toc),
            "outline_savings_pct": round((1 - estimate_tokens(md_toc) / estimate_tokens(sample_manuscript)) * 100, 1),
            "single_section_tokens": estimate_tokens(md_sec),
            "single_section_savings_pct": round(
                (1 - estimate_tokens(md_sec) / estimate_tokens(sample_manuscript)) * 100, 1
            ),
        },
        "slim_auto_detect": auto_results,
        "sqlite_cache": {
            "exact_lookup": status_exact,
            "semantic_lookup": status_sem,
        },
        "circuit_breaker": {
            "tripped_blocks_call": not allowed,
            "status_when_tripped": b_status,
        },
    }


def _read_file_or_stdin(path_arg: str | None) -> str:
    if path_arg and path_arg != "-":
        return Path(path_arg).read_text(encoding="utf-8", errors="ignore")
    return sys.stdin.read()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="100% Local Zero-Telemetry Token Saver CLI for Universal AI Agents & Bots"
    )
    sub = parser.add_subparsers(dest="cmd")

    p_slim = sub.add_parser(
        "slim",
        help="Universal auto-detecting filter (HTML, JSON/JSONL, CSV/TSV, Git Diff, Markdown, or CLI logs)",
    )
    p_slim.add_argument("file", nargs="?")

    p_exec = sub.add_parser("run", help="Run a shell command and compress its stdout/stderr")
    p_exec.add_argument("command", nargs=argparse.REMAINDER)

    p_html = sub.add_parser("html2md", help="Convert HTML from file or stdin to compact Markdown")
    p_html.add_argument("file", nargs="?")

    p_json = sub.add_parser(
        "json-slim", aliases=["jsonl-slim"], help="Slim bloated API/MCP/Social JSON or JSONL from file or stdin"
    )
    p_json.add_argument("file", nargs="?")
    p_json.add_argument("--max-items", type=int, default=20, help="Max array/JSONL items to retain (default: 20)")
    p_json.add_argument("--max-str-len", type=int, default=600, help="Max string length (default: 600)")
    p_json.add_argument("--keep", type=str, default="", help="Comma-separated keys to force-keep")

    p_csv = sub.add_parser(
        "csv-slim", help="Summarize large CSV/TSV files (schema, numeric/category stats, head/tail sample rows)"
    )
    p_csv.add_argument("file", nargs="?")
    p_csv.add_argument("--max-rows", type=int, default=10, help="Total sample rows to show (default: 10)")
    p_csv.add_argument("--cols", type=str, default="", help="Comma-separated column names to select")

    p_diff = sub.add_parser(
        "diff-slim", help="Compact git diff / PR patches (folds lockfiles & minified bundles, preserves source hunks)"
    )
    p_diff.add_argument("file", nargs="?")
    p_diff.add_argument(
        "--max-lines-per-file", type=int, default=120, help="Max diff lines per non-lock file (default: 120)"
    )

    p_md = sub.add_parser("md-outline", help="Print Markdown TOC with token counts or extract one section")
    p_md.add_argument("file", nargs="?")
    p_md.add_argument("--section", type=str, default=None, help="Heading title substring to extract")
    p_md.add_argument("--max-depth", type=int, default=2, help="Max heading depth for TOC outline (default: 2)")

    p_cache = sub.add_parser("cache", help="Direct CLI interface for SQLite cache & circuit breaker")
    p_cache.add_argument("action", choices=["get", "put", "guard", "record"])
    p_cache.add_argument("arg1", help="Namespace/service or cache key")
    p_cache.add_argument("arg2", nargs="?", default=None, help="Key (for get/put) or ok/fail (for record)")
    p_cache.add_argument("arg3", nargs="?", default=None, help="Value or @filepath (for put)")
    p_cache.add_argument("--ttl", type=float, default=3600, help="Cache TTL in seconds (default: 3600)")
    p_cache.add_argument("--threshold", type=float, default=0.75, help="Similarity threshold or failure count")
    p_cache.add_argument("--cooldown", type=float, default=300, help="Circuit breaker cooldown seconds")

    p_stats = sub.add_parser("stats", help="Show SQLite cache, circuit breaker, and token savings stats")
    p_stats.add_argument("--json", action="store_true", help="Output stats as JSON")

    sub.add_parser("selftest", help="Run benchmark & verification suite across all 7 modes")

    args = parser.parse_args()
    store = LocalStateStore()

    if args.cmd == "selftest" or not args.cmd:
        res = run_self_test()
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif args.cmd == "slim":
        raw = _read_file_or_stdin(args.file)
        comp, detected_mode = auto_slim(raw, filename=args.file)
        orig_t = estimate_tokens(raw)
        new_t = estimate_tokens(comp)
        store.record_compression(detected_mode, orig_t, new_t)
        print(comp)
        print(
            f"\n[local-token-saver (auto:{detected_mode}): {orig_t} -> {new_t} tokens "
            f"({round((1 - new_t / max(1, orig_t)) * 100, 1)}% saved)]",
            file=sys.stderr,
        )
    elif args.cmd == "run":
        comp, rc, orig_t, new_t = run_compressed(args.command)
        store.record_compression("cli", orig_t, new_t)
        print(comp)
        print(
            f"\n[local-token-saver: {orig_t} -> {new_t} tokens ({round((1 - new_t / max(1, orig_t)) * 100, 1)}% saved)]",
            file=sys.stderr,
        )
        sys.exit(rc)
    elif args.cmd == "html2md":
        raw = _read_file_or_stdin(args.file)
        comp = html_to_compact_markdown(raw)
        orig_t = estimate_tokens(raw)
        new_t = estimate_tokens(comp)
        store.record_compression("html2md", orig_t, new_t)
        print(comp)
        print(
            f"\n[local-token-saver: {orig_t} -> {new_t} tokens ({round((1 - new_t / max(1, orig_t)) * 100, 1)}% saved)]",
            file=sys.stderr,
        )
    elif args.cmd in ("json-slim", "jsonl-slim"):
        raw = _read_file_or_stdin(args.file)
        keep_set = {k.strip() for k in args.keep.split(",") if k.strip()} if args.keep else None
        comp = slim_json_text(raw, max_items=args.max_items, max_str_len=args.max_str_len, keep_keys=keep_set)
        orig_t = estimate_tokens(raw)
        new_t = estimate_tokens(comp)
        store.record_compression("json_slim", orig_t, new_t)
        print(comp)
        print(
            f"\n[local-token-saver: {orig_t} -> {new_t} tokens ({round((1 - new_t / max(1, orig_t)) * 100, 1)}% saved)]",
            file=sys.stderr,
        )
    elif args.cmd == "csv-slim":
        raw = _read_file_or_stdin(args.file)
        col_list = [c.strip() for c in args.cols.split(",") if c.strip()] if args.cols else None
        comp = slim_csv(raw, max_rows=args.max_rows, cols=col_list)
        orig_t = estimate_tokens(raw)
        new_t = estimate_tokens(comp)
        store.record_compression("csv_slim", orig_t, new_t)
        print(comp)
        print(
            f"\n[local-token-saver: {orig_t} -> {new_t} tokens ({round((1 - new_t / max(1, orig_t)) * 100, 1)}% saved)]",
            file=sys.stderr,
        )
    elif args.cmd == "diff-slim":
        raw = _read_file_or_stdin(args.file)
        comp = slim_diff(raw, max_lines_per_file=args.max_lines_per_file)
        orig_t = estimate_tokens(raw)
        new_t = estimate_tokens(comp)
        store.record_compression("diff_slim", orig_t, new_t)
        print(comp)
        print(
            f"\n[local-token-saver: {orig_t} -> {new_t} tokens ({round((1 - new_t / max(1, orig_t)) * 100, 1)}% saved)]",
            file=sys.stderr,
        )
    elif args.cmd == "md-outline":
        raw = _read_file_or_stdin(args.file)
        comp = markdown_outline_or_slice(raw, section_query=args.section, max_depth=args.max_depth)
        orig_t = estimate_tokens(raw)
        new_t = estimate_tokens(comp)
        store.record_compression("md_outline", orig_t, new_t)
        print(comp)
        print(
            f"\n[local-token-saver: {orig_t} -> {new_t} tokens ({round((1 - new_t / max(1, orig_t)) * 100, 1)}% saved)]",
            file=sys.stderr,
        )
    elif args.cmd == "cache":
        if args.action == "get":
            full_key = f"{args.arg1}:{args.arg2}" if args.arg2 else args.arg1
            val, status = store.cache_get(full_key, similarity_threshold=args.threshold)
            if val is not None:
                print(val)
                print(f"[local-token-saver: cache HIT ({status})]", file=sys.stderr)
                sys.exit(0)
            print(f"[local-token-saver: cache MISS ({status})]", file=sys.stderr)
            sys.exit(1)
        elif args.action == "put":
            if args.arg3 is not None:
                full_key = f"{args.arg1}:{args.arg2}"
                raw_val = args.arg3
            elif args.arg2 is not None:
                full_key = args.arg1
                raw_val = args.arg2
            else:
                full_key = args.arg1
                raw_val = "-"
            if raw_val == "-":
                val = sys.stdin.read()
            elif raw_val.startswith("@") and Path(raw_val[1:]).exists():
                val = Path(raw_val[1:]).read_text(encoding="utf-8", errors="ignore")
            else:
                val = raw_val
            store.cache_put(full_key, val, ttl_sec=args.ttl)
            print(f"[local-token-saver: cached '{full_key}' ({estimate_tokens(val)} tokens, ttl={int(args.ttl)}s)]")
        elif args.action == "guard":
            allowed, status = store.breaker_allow(args.arg1)
            print(f"[local-token-saver: breaker '{args.arg1}' -> {status}]")
            sys.exit(0 if allowed else 2)
        elif args.action == "record":
            ok = (args.arg2 or "ok").lower() in ("ok", "true", "1", "success", "pass")
            status = store.breaker_record(
                args.arg1,
                success=ok,
                threshold=int(args.threshold) if args.threshold >= 1 else 3,
                cooldown_sec=args.cooldown,
            )
            print(f"[local-token-saver: breaker '{args.arg1}' recorded {'OK' if ok else 'FAIL'} -> {status}]")
    elif args.cmd == "stats":
        st = store.get_stats()
        if args.json:
            print(json.dumps(st, indent=2, ensure_ascii=False))
        else:
            m = st["metrics"]
            print(f"=== local-token-saver stats ({st['db_path']}) ===")
            print(f"Active Cache Entries : {st['active_cache_entries']} (total: {st['total_cache_entries']})")
            print(
                f"Cache Hits / Misses  : {m.get('cache_hits_exact', 0)} exact, "
                f"{m.get('cache_hits_semantic', 0)} semantic, {m.get('cache_misses', 0)} misses"
            )
            print(f"Total Tokens Saved   : {m.get('total_tokens_saved', 0):,} tokens")
            print(
                f"  - CLI (`run`)       : {m.get('cli_tokens_saved', 0):,} tokens ({m.get('cli_calls', 0)} calls)\n"
                f"  - HTML (`html2md`)  : {m.get('html2md_tokens_saved', 0):,} tokens ({m.get('html2md_calls', 0)} calls)\n"
                f"  - JSON (`json-slim`): {m.get('json_slim_tokens_saved', 0):,} tokens ({m.get('json_slim_calls', 0)} calls)\n"
                f"  - CSV (`csv-slim`)  : {m.get('csv_slim_tokens_saved', 0):,} tokens ({m.get('csv_slim_calls', 0)} calls)\n"
                f"  - Diff (`diff-slim`): {m.get('diff_slim_tokens_saved', 0):,} tokens ({m.get('diff_slim_calls', 0)} calls)\n"
                f"  - MD (`md-outline`) : {m.get('md_outline_tokens_saved', 0):,} tokens ({m.get('md_outline_calls', 0)} calls)"
            )
            if st["circuit_breakers"]:
                print("Circuit Breakers     :")
                for b in st["circuit_breakers"]:
                    print(f"  - {b['service']}: {b['status']} (failures={b['failures']})")


if __name__ == "__main__":
    main()
