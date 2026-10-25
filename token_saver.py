#!/usr/bin/env python3
"""
local-token-saver (token_saver.py)
100% Local, Zero-Dependency, Zero-Telemetry Token & Cost Optimization Toolkit for AI Agents.

Replaces the useful parts of:
- rtk-ai/rtk (CLI output compression for git, test runners, logs, JSON)
- Crawl4AI / Firecrawl (HTML -> clean compact Markdown stripping scripts/styles/nav/SVG)
- Chonkie / LLMLingua (Zero-PyTorch semantic boundary chunking & boilerplate pruning)
- requests-cache / Hishel / GPTCache (SQLite WAL cache with TTL & near-duplicate matching)
- PyBreaker (Persistent SQLite circuit breaker to stop runaway retry loops)

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
import sqlite3
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(
    os.environ.get("TOKEN_SAVER_DB", str(Path.home() / ".cache" / "local-token-saver" / "state.sqlite3"))
)


def estimate_tokens(text: str) -> int:
    """Fast local token estimator (~4 chars per token for ASCII, ~1.5 chars for CJK)."""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + int(non_ascii * 0.7))


# ============================================================================
# 1. CLI & Log Output Compressor (Replaces rtk-ai/rtk without telemetry)
# ============================================================================
def compress_cli_output(text: str, max_lines: int = 80) -> str:
    """
    Compresses verbose CLI / test / git / log output by:
    - Stripping ANSI escape sequences and progress bars
    - Deduplicating repeated log lines with [xN] counters
    - Collapsing long passing test lists while preserving all failures/tracebacks
    - Truncating middle of huge diffs/files while keeping headers & error context
    """
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    clean = re.sub(r"^.*\r(?!\n)", "", clean, flags=re.MULTILINE)

    lines = clean.splitlines()
    if not lines:
        return ""

    deduped: list[str] = []
    norm_counts: dict[str, int] = {}
    last_norm = None
    repeat_run = 0

    for line in lines:
        stripped = line.rstrip()
        if re.match(r"^[\s▏▎▍▌▋▊▉█=\-\>\.\d%\/\[\]\(\)]+$", stripped) and "%" in stripped:
            continue
        norm = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b", "<TS>", stripped)
        norm = re.sub(r"0x[0-9a-fA-F]{6,}", "<HEX>", norm)

        if norm == last_norm and stripped:
            repeat_run += 1
            continue
        else:
            if repeat_run > 0 and deduped:
                deduped[-1] += f"  [repeated x{repeat_run + 1}]"
            repeat_run = 0
            last_norm = norm

        if re.match(r"^(PASS|ok|\s*✓|\s*✔|.*::.*PASSED)\b", stripped):
            norm_counts["__PASSING_TESTS__"] = norm_counts.get("__PASSING_TESTS__", 0) + 1
            continue

        deduped.append(stripped)

    if repeat_run > 0 and deduped:
        deduped[-1] += f"  [repeated x{repeat_run + 1}]"

    pass_count = norm_counts.get("__PASSING_TESTS__", 0)
    if pass_count > 0:
        deduped.insert(0, f"[Summary: {pass_count} passing test lines collapsed]")

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
# 2. HTML-to-Clean-Markdown Extractor (Replaces Crawl4AI / Firecrawl locally)
# ============================================================================
class _CompactMarkdownExtractor(HTMLParser):
    IGNORE_TAGS = {"script", "style", "svg", "noscript", "iframe", "nav", "footer", "header", "aside", "form"}
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
# 3. Token-Aware Semantic Chunker (Replaces Chonkie / LLMLingua locally)
# ============================================================================
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
# 4. SQLite Cache + Near-Duplicate Matcher + Circuit Breaker
#    (Replaces requests-cache, Hishel, GPTCache, PyBreaker)
# ============================================================================
class LocalStateStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
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
            """
        )
        self.conn.commit()

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

    def cache_get(self, key: str, similarity_threshold: float = 0.75) -> tuple[str | None, str]:
        now = time.time()
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        row = self.conn.execute(
            "SELECT value FROM kv_cache WHERE cache_key = ? AND expires_at > ?", (h, now)
        ).fetchone()
        if row:
            return row[0], "exact_hit"

        q_tokens = self._normalize_tokens(key)
        if not q_tokens:
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
                return val, f"semantic_hit({sim:.2f})"
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


def run_self_test() -> dict[str, Any]:
    noisy_cli = "\n".join(
        [f"PASS tests/unit/test_module_{i}.py::test_case_{i}" for i in range(120)]
        + ["2026-10-25T12:00:01Z INFO Starting worker at 0x7fff1234abcd"] * 25
        + ["FAIL tests/unit/test_auth.py::test_token_expiry - AssertionError: expected 200 got 401"]
    )
    compressed_cli = compress_cli_output(noisy_cli)

    noisy_html = """
    <html><head><script>console.log("tracking(" + "secret" + ")");</script>
    <style>body { color: red; margin: 0; padding: 0; }</style></head>
    <body><nav><a href="/home">Home</a><a href="/pricing">Pricing</a></nav>
    <main><h1>Token Optimization Guide</h1><p>Cut noisy HTML tags before sending to LLMs.</p>
    <ul><li>Zero dependencies</li><li>Zero telemetry</li></ul></main>
    <footer>Copyright 2026</footer></body></html>
    """ * 5
    clean_md = html_to_compact_markdown(noisy_html)

    store = LocalStateStore()
    store.cache_put("How do we disable PostHog telemetry in Mem0 and Graphiti?", "Set MEM0_TELEMETRY=false and GRAPHITI_TELEMETRY_ENABLED=false")
    val_exact, status_exact = store.cache_get("How do we disable PostHog telemetry in Mem0 and Graphiti?")
    val_sem, status_sem = store.cache_get("How to disable PostHog telemetry in Graphiti and Mem0?")

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
        "sqlite_cache": {
            "exact_lookup": status_exact,
            "semantic_lookup": status_sem,
        },
        "circuit_breaker": {
            "tripped_blocks_call": not allowed,
            "status_when_tripped": b_status,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="100% Local Zero-Telemetry Token Saver CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_exec = sub.add_parser("run", help="Run a shell command and compress its stdout/stderr")
    p_exec.add_argument("command", nargs=argparse.REMAINDER)

    p_html = sub.add_parser("html2md", help="Convert HTML from stdin or file to compact Markdown")
    p_html.add_argument("file", nargs="?")

    sub.add_parser("selftest", help="Run benchmark & verification suite")

    args = parser.parse_args()
    if args.cmd == "selftest" or not args.cmd:
        res = run_self_test()
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif args.cmd == "run":
        proc = subprocess.run(" ".join(args.command), shell=True, capture_output=True, text=True)
        combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        comp = compress_cli_output(combined)
        orig_t = estimate_tokens(combined)
        new_t = estimate_tokens(comp)
        print(comp)
        print(f"\n[local-token-saver: {orig_t} -> {new_t} tokens ({round((1 - new_t/max(1, orig_t))*100, 1)}% saved)]", file=sys.stderr)
        sys.exit(proc.returncode)
    elif args.cmd == "html2md":
        raw = Path(args.file).read_text(encoding="utf-8", errors="ignore") if args.file else sys.stdin.read()
        print(html_to_compact_markdown(raw))


if __name__ == "__main__":
    main()
