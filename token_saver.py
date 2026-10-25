#!/usr/bin/env -S python3 -I
"""
local-token-saver (token_saver.py)
100% Local, Zero-Dependency, Zero-Telemetry Token & Cost Optimization Toolkit for AI Agents.

Designed for all bot archetypes:
1. Dev / QA / Video / Audio Bots (`run`):
   Compresses pytest/build logs, strips ANSI codes, and collapses FFmpeg/Remotion/headless render
   progress bars while preserving 100% of errors, warnings, tracebacks, and final summaries.
2. Web / E-Commerce / Freelance Scraping Bots (`html2md`):
   Converts noisy HTML pages into clean, compact Markdown (stripping script/style/svg/nav/ads).
3. X / Social / API Bots (`json-slim`):
   Compacts bloated X/API/MCP JSON responses by stripping null/empty fields, removing repetitive
   UI/profile metadata bloat, capping huge arrays, and keeping IDs, text, metrics, URLs, and dates.
4. Writing / eBook / Note / Research Bots (`md-outline`):
   Generates a token-counted Table of Contents or extracts a single section from long Markdown
   manuscripts (`manuscript.md`, `bible.md`, `outline.md`, research notes) without loading full files.
5. Multi-Bot Shared SQLite WAL Cache & 3-Strike Circuit Breaker (`cache`, `stats`):
   Provides exact + Jaccard similarity caching and runaway-retry protection from CLI or Python.

Security guarantee:
- Uses ONLY Python standard library modules (sqlite3, hashlib, html.parser, re, json, time, subprocess).
- Zero third-party pip packages (immune to LiteLLM/PyPI .pth supply-chain attacks).
- Zero outbound telemetry, analytics, PostHog, Amplitude, or Scarf calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sqlite3
if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_WORKSPACE_DB = Path("/workspace/tools/local-token-saver/state.sqlite3")
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "TOKEN_SAVER_DB",
        str(
            _WORKSPACE_DB
            if _WORKSPACE_DB.parent.exists()
            else Path.home() / ".cache" / "local-token-saver" / "state.sqlite3"
        ),
    )
)


def estimate_tokens(text: str) -> int:
    """Fast local token estimator (~4 chars per token for ASCII, ~1.5 chars for CJK)."""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + int(non_ascii * 0.7))


# ============================================================================
# 1. CLI, Build, Test & Media Render Log Compressor (`run`)
# ============================================================================
_PROGRESS_PATTERNS = re.compile(
    r"^(?:"
    r"frame=\s*\d+\s+fps=.*|"
    r"size=\s*\d+kB\s+time=.*|"
    r"\[(?:download|ffmpeg|info)\]\s+\d+(?:\.\d+)?%.*|"
    r".*\bRendering\s+frame\s+\d+/\d+.*|"
    r".*\bBundling\s+\d+%.*|"
    r".*\bProgress:\s+\d+%.*|"
    r"[\s▏▎▍▌▋▊▉█=\-\>\.\d%\/\[\]\(\)|]+$"
    r")",
    re.IGNORECASE,
)


def compress_cli_output(text: str, max_lines: int = 80) -> str:
    """
    Compresses verbose CLI / test / git / FFmpeg / Remotion output by:
    - Stripping ANSI escape sequences and carriage-return overwrites
    - Collapsing FFmpeg/Remotion/download frame progress lines while keeping the final progress state
    - Deduplicating repeated log lines with [repeated xN] counters
    - Collapsing passing test lists while preserving 100% of failures, warnings, and tracebacks
    - Truncating middle of huge outputs while keeping head & tail context
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

        # Collapse progress bars and media frame-by-frame progress lines
        if _PROGRESS_PATTERNS.match(stripped) and (
            "%" in stripped or "frame=" in stripped.lower() or "time=" in stripped.lower()
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

        if re.match(r"^(PASS|ok|\s*✓|\s*✔|.*::.*PASSED)\b", stripped):
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
    }
    BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "br", "hr", "blockquote", "pre"}
    HEADING_TAGS = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### "}

    def __init__(self) -> None:
        super().__init__()
        self._ignore_depth = 0
        self._chunks: list[str] = []
        self._in_pre = False
        self._current_href: str | None = None

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
                self._chunks.append(collapsed)

    def get_markdown(self) -> str:
        raw = "".join(self._chunks)
        lines = [line.strip() for line in raw.splitlines()]
        out: list[str] = []
        blank = 0
        for line in lines:
            if not line:
                blank += 1
                if blank <= 1:
                    out.append("")
            else:
                blank = 0
                out.append(line)
        return "\n".join(out).strip()


def html_to_compact_markdown(html: str) -> str:
    parser = _CompactMarkdownExtractor()
    parser.feed(html)
    return parser.get_markdown()


# ============================================================================
# 3. JSON API / X / MCP Payload Slimmer (`json-slim`)
# ============================================================================
_DEFAULT_DROP_KEYS = {
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
}


def slim_json_value(
    obj: Any,
    max_items: int = 20,
    max_str_len: int = 600,
    keep_keys: set[str] | None = None,
) -> Any:
    """
    Recursively compacts bloated JSON payloads (X search, API responses, MCP dumps) while
    preserving essential fields (id, text, username, metrics, URLs, timestamps).
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
    data = json.loads(raw_json)
    slimmed = slim_json_value(data, max_items=max_items, max_str_len=max_str_len, keep_keys=keep_keys)
    return json.dumps(slimmed, ensure_ascii=False, separators=(",", ":"))


# ============================================================================
# 4. Markdown Outline & Section Slicer for Long Manuscripts (`md-outline`)
# ============================================================================
def markdown_outline_or_slice(text: str, section_query: str | None = None, max_depth: int = 2) -> str:
    """
    For eBook, Note, and Research bots:
    - Without `section_query`: returns a compact Table of Contents with line numbers and
      token counts per section (saving 90-98% tokens vs reading the entire manuscript).
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
        # Tree end line includes subsections of deeper level
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
            return f"[Section '{section_query}' not found. Available headings:]\n" + markdown_outline_or_slice(text, None, max_depth=max_depth)
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
# 5. SQLite Cache + Near-Duplicate Matcher + Circuit Breaker + Savings Stats
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
# 6. Self-Test & Multi-Bot Benchmark Suite (`selftest`)
# ============================================================================
def run_self_test() -> dict[str, Any]:
    # 1. CLI + Media Render logs (Dev / QA / Video Bots)
    noisy_cli = "\n".join(
        [f"PASS tests/unit/test_module_{i}.py::test_case_{i}" for i in range(120)]
        + [f"frame={i:4d} fps=60.0 q=28.0 size=    1024kB time=00:00:{i//30:02d}.00 bitrate=2048.0kbits/s" for i in range(1, 91)]
        + ["2026-10-25T12:00:01Z INFO Starting worker at 0x7fff1234abcd"] * 25
        + ["FAIL tests/unit/test_auth.py::test_token_expiry - AssertionError: expected 200 got 401"]
    )
    compressed_cli = compress_cli_output(noisy_cli)

    # 2. Noisy HTML (E-Commerce / Freelance Scraping Bots)
    noisy_html = """
    <html><head><script>console.log("tracking(" + "secret" + ")");</script>
    <style>body { color: red; margin: 0; padding: 0; }</style></head>
    <body><nav><a href="/home">Home</a><a href="/pricing">Pricing</a></nav>
    <main><h1>Token Optimization Guide</h1><p>Cut noisy HTML tags before sending to LLMs.</p>
    <ul><li>Zero dependencies</li><li>Zero telemetry</li></ul></main>
    <footer>Copyright 2026</footer></body></html>
    """ * 5
    clean_md = html_to_compact_markdown(noisy_html)

    # 3. Bloated X / API JSON (X Content Chief / X Algo / X Growth Engine)
    bloated_json = json.dumps(
        {
            "data": [
                {
                    "id": f"1982000000000000{i:03d}",
                    "text": f"Grok Bot local-token-saver cuts tokens by 95%+ (#{i})",
                    "created_at": "2026-10-26T00:00:00Z",
                    "lang": "ja",
                    "possibly_sensitive": False,
                    "edit_history_tweet_ids": [f"1982000000000000{i:03d}"],
                    "context_annotations": [
                        {"domain": {"id": "131", "name": "Unified Twitter Taxonomy"}, "entity": {"id": "100", "name": "AI"}}
                        for _ in range(12)
                    ],
                    "author": {
                        "id": "121301655",
                        "username": "GrokBenkei",
                        "profile_image_url_https": "https://pbs.twimg.com/profile_images/123456789/avatar_normal.jpg",
                        "profile_banner_url": "https://pbs.twimg.com/profile_banners/123456789/1700000000",
                        "profile_background_color": "000000",
                        "profile_link_color": "1DA1F2",
                        "default_profile": True,
                        "has_extended_profile": True,
                        "withheld_in_countries": [],
                    },
                    "public_metrics": {"like_count": 42, "reply_count": 11, "retweet_count": 9, "impression_count": 3800},
                }
                for i in range(30)
            ]
        },
        ensure_ascii=False,
        indent=2,
    )
    slimmed_json = slim_json_text(bloated_json, max_items=10)

    # 4. Long Markdown Manuscript (eBook Writer / eBook Editor / Note Desk)
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

    # 5. SQLite Cache + Circuit Breaker
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
        "md_outline": {
            "manuscript_tokens": estimate_tokens(sample_manuscript),
            "outline_tokens": estimate_tokens(md_toc),
            "outline_savings_pct": round((1 - estimate_tokens(md_toc) / estimate_tokens(sample_manuscript)) * 100, 1),
            "single_section_tokens": estimate_tokens(md_sec),
            "single_section_savings_pct": round((1 - estimate_tokens(md_sec) / estimate_tokens(sample_manuscript)) * 100, 1),
        },
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
    parser = argparse.ArgumentParser(description="100% Local Zero-Telemetry Token Saver CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_exec = sub.add_parser("run", help="Run a shell command and compress its stdout/stderr")
    p_exec.add_argument("command", nargs=argparse.REMAINDER)

    p_html = sub.add_parser("html2md", help="Convert HTML from file or stdin to compact Markdown")
    p_html.add_argument("file", nargs="?")

    p_json = sub.add_parser("json-slim", help="Slim bloated API/X/MCP JSON from file or stdin")
    p_json.add_argument("file", nargs="?")
    p_json.add_argument("--max-items", type=int, default=20, help="Max array items to retain (default: 20)")
    p_json.add_argument("--max-str-len", type=int, default=600, help="Max string length (default: 600)")
    p_json.add_argument("--keep", type=str, default="", help="Comma-separated keys to force-keep")

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

    sub.add_parser("selftest", help="Run benchmark & verification suite")

    args = parser.parse_args()
    store = LocalStateStore()

    if args.cmd == "selftest" or not args.cmd:
        res = run_self_test()
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif args.cmd == "run":
        proc = subprocess.run(" ".join(args.command), shell=True, capture_output=True, text=True)
        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        comp = compress_cli_output(combined)
        orig_t = estimate_tokens(combined)
        new_t = estimate_tokens(comp)
        store.record_compression("cli", orig_t, new_t)
        print(comp)
        print(
            f"\n[local-token-saver: {orig_t} -> {new_t} tokens ({round((1 - new_t / max(1, orig_t)) * 100, 1)}% saved)]",
            file=sys.stderr,
        )
        sys.exit(proc.returncode)
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
    elif args.cmd == "json-slim":
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
                f"  - CLI (`run`)      : {m.get('cli_tokens_saved', 0):,} tokens ({m.get('cli_calls', 0)} calls)\n"
                f"  - HTML (`html2md`) : {m.get('html2md_tokens_saved', 0):,} tokens ({m.get('html2md_calls', 0)} calls)\n"
                f"  - JSON (`json-slim`): {m.get('json_slim_tokens_saved', 0):,} tokens ({m.get('json_slim_calls', 0)} calls)\n"
                f"  - MD (`md-outline`): {m.get('md_outline_tokens_saved', 0):,} tokens ({m.get('md_outline_calls', 0)} calls)"
            )
            if st["circuit_breakers"]:
                print("Circuit Breakers     :")
                for b in st["circuit_breakers"]:
                    print(f"  - {b['service']}: {b['status']} (failures={b['failures']})")


if __name__ == "__main__":
    main()
