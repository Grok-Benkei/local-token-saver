#!/usr/bin/env python3
"""
local-token-saver (v2.0.0)
~~~~~~~~~~~~~~~~~~~~~~~~~~
A zero-dependency, offline, zero-telemetry token reduction toolkit for AI agents
and bots (Grok Bot, Browser Use, Playwright, MCP, coding & research agents).

Core Philosophy:
- Never reduce AI reasoning effort, model tier, or output quality.
- Strip ONLY redundant input noise BEFORE it enters the LLM context window.
- 100% Python standard library + local SQLite (WAL mode). Zero external packages,
  zero network calls, zero telemetry, zero supply-chain risk.

Capabilities (9 Core Modules):
1. Format Auto-Detection & Universal Slimmer (`slim`):
   - Automatically detects HTML, ARIA snapshots, JSON, JSONL, CSV/TSV, Git diffs,
     Markdown, or CLI logs and routes to the right lossless compressor.
   - Safe on Markdown: cleans redundant blank lines, HTML comments, and inline
     data URIs WITHOUT destroying body text (use `--outline` or `md-outline`
     when heading-only extraction is explicitly desired).
2. Browser-Use / SPA-Safe HTML -> Markdown (`html2md`):
   - Preserves interactive DOM elements (`<form>`, `<input>`, `<textarea>`,
     `<select>`, `<option>`, `<button>`, `<label>`, `<img alt>`, `[aria-label]`,
     `[role]`) as compact markers (`[Form ...]`, `[Input: ...]`, `[Button: ...]`)
     while redacting secrets (`password`, `csrf`, `token`).
   - Supports `--browser` mode to retain `<header>`/`<nav>` navigation links and
     search forms when driving a browser.
   - Extracts `<script id="__NEXT_DATA__">`, `<script type="application/ld+json">`,
     and `window.__INITIAL_STATE__` / `__NUXT_DATA__`, compacts them via
     `slim_json_value`, and appends under `### [Structured Data]` so Next.js,
     Mercari, Yahoo Auctions, and modern SPA pages never lose product/price data.
   - Strips tracking query parameters (`utm_*`, `fbclid`, `gclid`, etc.) without
     breaking signed URLs (`X-Amz-Signature`, `Signature`, `token`), and strips
     inline `data:image/...;base64,...` URIs.
   - Preserves `<pre><code>` indentation and inline tag spacing.
3. Playwright / Browser-Use ARIA Snapshot Slimmer (`aria-slim`):
   - Collapses redundant structural wrappers (`- generic:`, `- group:`, `- none:`)
     while preserving 100% of interactive roles and 100% of `[ref=...]` markers
     across the entire page (never middle-truncates `[ref=...]` lines).
4. JSON & JSONL Schema Slimmer (`json-slim`, `jsonl-slim`):
   - Removes nulls, empty containers, and noisy metadata keys.
   - Scrubs `data:...;base64,...` and long base64/hex blobs (`>= 256` chars)
     BEFORE string truncation (`<data-uri omitted>`, `<base64 omitted>`).
   - Redacts sensitive credential keys (`password`, `secret`, `access_token`,
     `api_key`, `authorization`, `cookie`, `private_key`) to `<redacted>`.
5. CSV / TSV Statistical Summary + Sample Slimmer (`csv-slim`):
   - Single-pass streaming (`deque` + `Decimal`) over large CSV/TSV files,
     computing min/max/mean/sum per numeric column plus head/tail sample rows.
6. Git Diff & Lockfile Compressor (`diff-slim`):
   - Collapses lockfiles (`package-lock.json`, `pnpm-lock.yaml`, `Cargo.lock`, etc.),
     minified assets (`*.min.js`, `*.map`), and oversized hunks while preserving
     file headers, line stats, and source code changes.
7. Markdown Outline & Section Extractor (`md-outline`):
   - Lists heading hierarchy with accurate CJK-aware token counts per section,
     and extracts specific sections on demand.
8. CLI & Test Runner Output Compressor (`run`, `log-slim`):
   - Wraps commands (`shell=False` for argument lists, `stdin=DEVNULL`, timeout),
     deduplicates repetitive progress bars (`frame=`, `PASS`), and preserves
     head + tail + ALL middle-of-log `ERROR`/`FAIL`/`Traceback`/`Exception`
     windows (`+/- 2` context lines).
9. Batch Slimmer (`batch-slim`) & CJK-Aware SQLite Cache + Circuit Breaker (`cache`):
   - Slims multiple MCP `write_to_file` outputs in a single Shell call.
   - NFKC + CJK character 2-gram/3-gram + ASCII word Jaccard similarity cache
     with namespace isolation and 3-strike circuit breaker.

License: MIT
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import hashlib
import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from decimal import Decimal, InvalidOperation, localcontext
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

__version__ = "2.0.0"

DEFAULT_DB_PATH = Path(
    os.environ.get(
        "TOKEN_SAVER_DB",
        "/workspace/tools/local-token-saver/state.sqlite3",
    )
)

# ---------------------------------------------------------------------------
# Token Estimation & CJK-Aware Normalization Utilities
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[\u3000-\u9fff\uf900-\ufaff\uff00-\uffef]")
_ASCII_WORD_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def estimate_tokens(text: str) -> int:
    """
    Estimate LLM token count with CJK-aware weighting.
    - CJK characters (Japanese/Chinese/Korean) average ~0.85 tokens/char in modern
      BPE tokenizers (cl100k / o200k / DeepSeek / Grok).
    - Non-CJK ASCII/UTF-8 characters average ~0.25 tokens/char (~1 token per 4 chars).
    """
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    other_count = len(text) - cjk_count
    estimated = int(round(cjk_count * 0.85 + other_count * 0.25))
    return max(1, estimated)


_KANJI_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_JP_STOP_PHRASES_RE = re.compile(
    r"(?:してください|して下さい|お願いします|お願いいたします|教えて下さい|教えてください|教えて|ください|下さい)"
)


def _normalize_tokens(text: str) -> set[str]:
    """
    Extract normalized lexical features for Jaccard similarity matching:
    - Applies NFKC normalization, lowercasing, and strips polite Japanese suffixes.
    - Extracts ASCII words (length >= 2).
    - Splits CJK text by script boundaries (Kanji compounds vs Katakana terms vs
      Hiragana particles) AND generates character 2-grams and 3-grams across CJK runs
      so Japanese/Chinese queries match accurately across particle/politeness variations.
    """
    if not text:
        return set()
    norm = unicodedata.normalize("NFKC", text).lower()
    norm = _JP_STOP_PHRASES_RE.sub(" ", norm)
    tokens: set[str] = set(_ASCII_WORD_RE.findall(norm))

    for k_run in _KANJI_RUN_RE.findall(norm):
        tokens.add(k_run)
        for i in range(len(k_run) - 1):
            tokens.add(k_run[i : i + 2])

    for kat_run in re.findall(r"[\u30a0-\u30ff]{2,}", norm):
        tokens.add(kat_run)
        for i in range(len(kat_run) - 1):
            tokens.add(kat_run[i : i + 2])

    cjk_compact = "".join(_CJK_RUN_RE.findall(norm))
    cjk_content = re.sub(r"[のからまでをにへでとがもはや]", "", cjk_compact)
    for seq in (cjk_content,):
        n = len(seq)
        if n == 1:
            tokens.add(seq)
        elif n == 2:
            tokens.add(seq)
        else:
            for i in range(n - 1):
                tokens.add(seq[i : i + 2])
            for i in range(n - 2):
                tokens.add(seq[i : i + 3])
    return tokens


def jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two queries (English, Japanese, or mixed)."""
    ta = _normalize_tokens(a)
    tb = _normalize_tokens(b)
    if not ta or not tb:
        return 1.0 if a.strip() == b.strip() else 0.0
    intersection = len(ta & tb)
    union = len(ta | tb)
    return intersection / union if union else 0.0


# ---------------------------------------------------------------------------
# URL & Data URI Sanitization Helpers
# ---------------------------------------------------------------------------

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "yclid",
    "twclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
    "_hsenc",
    "_hsmi",
    "hsctatracking",
    "mkt_tok",
    "spm",
    "vero_id",
    "oly_enc_id",
    "oly_anon_id",
    "rb_clickid",
    "s_kwcid",
}

_SIGNED_URL_PARAMS = {
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
    "x-goog-signature",
    "signature",
    "sig",
    "token",
    "expires",
    "se",
    "sp",
    "sv",
}

_DATA_URI_RE = re.compile(
    r"data:([a-zA-Z0-9.+/-]+)?(?:;[a-zA-Z0-9=:+-]+)*(?:;base64,[A-Za-z0-9+/=\s]{32,}|,[^\s\"'<>)]{64,})",
    re.IGNORECASE,
)

_BASE64_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=\r\n]{256,}$")
_HEX_BLOB_RE = re.compile(r"^[0-9a-fA-F]{256,}$")


def clean_url(url: str, base_url: str = "") -> str:
    """
    Sanitize URLs for Markdown output:
    - Strip inline `data:` URIs and `javascript:`/`vbscript:` pseudo-URLs.
    - Resolve relative URLs when `base_url` is provided.
    - Strip known marketing/tracking query parameters (`utm_*`, `fbclid`, `gclid`, etc.)
      UNLESS the URL contains cryptographic signature parameters (`X-Amz-Signature`, etc.).
    """
    if not url:
        return ""
    raw = url.strip()
    lower = raw.lower()
    if lower.startswith("data:"):
        return "<data-uri omitted>"
    if lower.startswith(("javascript:", "vbscript:")):
        return ""
    if base_url and not lower.startswith(("http://", "https://", "mailto:", "tel:", "#")):
        try:
            raw = urllib.parse.urljoin(base_url, raw)
        except Exception:
            pass
    try:
        parsed = urllib.parse.urlsplit(raw)
        if not parsed.query:
            return raw
        q_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        param_names_lower = {k.lower() for k, _ in q_pairs}
        if param_names_lower & _SIGNED_URL_PARAMS:
            return raw
        filtered = [
            (k, v)
            for k, v in q_pairs
            if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_PARAMS)
        ]
        if len(filtered) == len(q_pairs):
            return raw
        new_query = urllib.parse.urlencode(filtered, doseq=True)
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment)
        )
    except Exception:
        return raw


# ---------------------------------------------------------------------------
# Format Auto-Detection
# ---------------------------------------------------------------------------

_ARIA_LINE_RE = re.compile(
    r"^\s*-\s+(?:document|button|link|textbox|searchbox|combobox|checkbox|radio|switch|"
    r"heading|img|list|listitem|navigation|main|banner|contentinfo|region|form|table|"
    r"row|cell|columnheader|rowheader|tab|tablist|tabpanel|menuitem|menu|dialog|alert|"
    r"status|generic|group|presentation|none|paragraph|text|article|section|complementary)"
    r'(?:\s+"[^"]*")?(?:\s+\[[^\]]+\])*?\s*:?(?:\s+.*)?$',
    re.IGNORECASE,
)


def detect_format(text: str, filename: str = "") -> str:
    """
    Auto-detect content format:
    Returns one of: 'html', 'aria', 'json', 'jsonl', 'csv', 'tsv', 'diff', 'markdown', 'cli'.
    """
    ext = Path(filename).suffix.lower() if filename else ""
    if ext in {".html", ".htm"}:
        return "html"
    if ext == ".json":
        return "json"
    if ext in {".jsonl", ".ndjson"}:
        return "jsonl"
    if ext == ".csv":
        return "csv"
    if ext == ".tsv":
        return "tsv"
    if ext in {".diff", ".patch"}:
        return "diff"
    if ext in {".md", ".markdown"}:
        return "markdown"
    if ext in {".aria", ".snapshot"}:
        return "aria"

    sample = text.lstrip()[:4096]
    if not sample:
        return "cli"

    if sample.startswith("diff --git ") or sample.startswith("--- a/") or "\ndiff --git " in sample:
        return "diff"

    sample_lines = [ln for ln in sample.splitlines()[:25] if ln.strip()]
    if sample_lines:
        aria_matches = sum(
            1
            for ln in sample_lines
            if _ARIA_LINE_RE.match(ln) or ("[ref=" in ln and ln.lstrip().startswith("- "))
        )
        if aria_matches >= 2 or (
            len(sample_lines) == 1
            and "[ref=" in sample_lines[0]
            and sample_lines[0].lstrip().startswith("- ")
        ):
            return "aria"

    if re.search(
        r"<!doctype\s+html|<html[\s>]|<head[\s>]|<body[\s>]|<div[\s>]|<table[\s>]|<article[\s>]|<form[\s>]|<section[\s>]|<main[\s>]|<nav[\s>]|<p[\s>]|<span[\s>]|<ul[\s>]|<ol[\s>]",
        sample,
        re.IGNORECASE,
    ):
        return "html"

    if sample[0] in "{[":
        try:
            json.loads(text)
            return "json"
        except Exception:
            pass
        lines = [ln.strip() for ln in sample.splitlines() if ln.strip()]
        if len(lines) >= 2 and all(
            (ln.startswith("{") and ln.endswith("}")) or (ln.startswith("[") and ln.endswith("]"))
            for ln in lines[:3]
        ):
            try:
                json.loads(lines[0])
                json.loads(lines[1])
                return "jsonl"
            except Exception:
                pass

    md_headings = len(re.findall(r"(?m)^#{1,6}\s+\S+", sample))
    if md_headings >= 2:
        return "markdown"

    first_lines = [ln for ln in sample.splitlines()[:10] if ln.strip()]
    if len(first_lines) >= 2:
        tab_counts = [ln.count("\t") for ln in first_lines[:5]]
        if min(tab_counts) >= 1 and len(set(tab_counts)) == 1:
            return "tsv"
        comma_counts = [ln.count(",") for ln in first_lines[:5]]
        if min(comma_counts) >= 1 and len(set(comma_counts)) <= 2 and all(
            not ln.lstrip().startswith(("#", "-", "*", "{", "[", "<")) for ln in first_lines[:3]
        ):
            return "csv"

    return "cli"


# ---------------------------------------------------------------------------
# 1. Browser-Use / SPA-Safe HTML -> Compact Markdown Converter
# ---------------------------------------------------------------------------

_SENSITIVE_ATTR_RE = re.compile(
    r"(?:password|passwd|pwd|secret|token|csrf|xsrf|auth|api[-_]?key|session|credit[-_]?card|cvv|cvc|nonce|state|verifier|challenge|saml| RelayState|signature|sig|jwt|bearer|credential)",
    re.IGNORECASE,
)

_SAFE_HIDDEN_INPUT_NAMES: frozenset[str] = frozenset(
    {
        "id",
        "uid",
        "userid",
        "itemid",
        "productid",
        "orderid",
        "categoryid",
        "page",
        "perpage",
        "limit",
        "offset",
        "sort",
        "order",
        "lang",
        "locale",
        "hl",
        "gl",
        "q",
        "query",
        "search",
        "type",
        "mode",
        "action",
        "step",
        "tab",
        "view",
        "format",
        "redirect",
        "returnurl",
        "next",
        "back",
        "ref",
        "source",
        "currency",
        "country",
        "timezone",
        "tz",
        "utf8",
    }
)

_WINDOW_STATE_ASSIGNMENTS = [
    ("__INITIAL_STATE__", re.compile(r"window\.__INITIAL_STATE__\s*=\s*")),
    ("__NUXT_DATA__", re.compile(r"window\.__NUXT(?:_DATA)?__\s*=\s*")),
    ("__APOLLO_STATE__", re.compile(r"window\.__APOLLO_STATE__\s*=\s*")),
    ("__PRELOADED_STATE__", re.compile(r"window\.__PRELOADED_STATE__\s*=\s*")),
]


def _extract_assigned_json(script_text: str, assign_re: re.Pattern[str]) -> Any | None:
    """
    Extract a JSON object or array assigned to a global `window.*` variable using
    `json.JSONDecoder().raw_decode` so `};` inside string literals or array roots
    never break extraction.
    """
    m = assign_re.search(script_text)
    if not m:
        return None
    idx = m.end()
    while idx < len(script_text) and script_text[idx].isspace():
        idx += 1
    if idx >= len(script_text) or script_text[idx] not in "{[":
        return None
    try:
        parsed, _ = json.JSONDecoder().raw_decode(script_text, idx)
        return parsed
    except Exception:
        return None


class _CompactHTMLParser(HTMLParser):
    """
    Browser-Use & SPA-aware HTML-to-Markdown parser:
    - Never drops `<form>`, `<input>`, `<textarea>`, `<select>`, `<option>`, `<button>`,
      `<label>`, `<img alt>`, or `[aria-label]` interactive controls.
    - Extracts `<script id="__NEXT_DATA__">`, `<script type="application/ld+json">`,
      `<script id="__NUXT_DATA__">`, and `window.__INITIAL_STATE__` JSON payloads before
      ignoring general scripts.
    - Uses an explicit tag stack (`_ignore_stack`) instead of an integer depth counter
      so unclosed tags cannot swallow the remainder of the document.
    - Preserves `<pre><code>` indentation and inline tag spacing.
    """

    ALWAYS_IGNORE_TAGS = {
        "style",
        "svg",
        "noscript",
        "iframe",
        "canvas",
        "template",
        "meta",
        "link",
    }
    CHROME_IGNORE_TAGS = {
        "nav",
        "footer",
        "aside",
        "header",
    }
    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    BLOCK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "nav",
        "aside",
        "blockquote",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "fieldset",
        "form",
        "details",
        "summary",
        "figure",
        "figcaption",
        "address",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
    INLINE_TAGS = {
        "span",
        "a",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "small",
        "sub",
        "sup",
        "mark",
        "abbr",
        "cite",
        "q",
        "time",
        "label",
        "code",
    }

    def __init__(
        self,
        keep_links: bool = True,
        browser_mode: bool = False,
        base_url: str = "",
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.keep_links = keep_links
        self.browser_mode = browser_mode
        self.base_url = base_url

        self._ignore_tags = set(self.ALWAYS_IGNORE_TAGS)
        if not browser_mode:
            self._ignore_tags |= self.CHROME_IGNORE_TAGS

        self._ignore_stack: list[str] = []
        self._in_script = False
        self._script_attrs: dict[str, str] = {}
        self._script_buf: list[str] = []
        self.structured_data: list[tuple[str, Any]] = []

        self._in_title = False
        self._title_buf: list[str] = []
        self.page_title: str = ""

        self._in_pre = 0
        self._in_code = 0
        self._chunks: list[str] = []

        self._link_stack: list[tuple[str, str, list[str]]] = []
        self._button_stack: list[tuple[dict[str, str], list[str]]] = []
        self._select_stack: list[tuple[dict[str, str], list[str]]] = []
        self._option_stack: list[tuple[dict[str, str], list[str]]] = []
        self._textarea_stack: list[tuple[dict[str, str], list[str]]] = []

        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._in_cell = False
        self._cell_buf: list[str] = []
        self._list_depth = 0

    def _emit(self, text: str) -> None:
        if not text:
            return
        if self._in_cell:
            self._cell_buf.append(text)
        elif self._option_stack:
            self._option_stack[-1][1].append(text)
        elif self._textarea_stack:
            self._textarea_stack[-1][1].append(text)
        elif self._button_stack:
            self._button_stack[-1][1].append(text)
        elif self._link_stack:
            self._link_stack[-1][2].append(text)
        else:
            self._chunks.append(text)

    def _ensure_inline_space(self) -> None:
        """Insert a space between adjacent inline elements when needed."""
        if self._in_pre > 0:
            return
        target = None
        if self._in_cell and self._cell_buf:
            target = self._cell_buf
        elif self._button_stack and self._button_stack[-1][1]:
            target = self._button_stack[-1][1]
        elif self._link_stack and self._link_stack[-1][2]:
            target = self._link_stack[-1][2]
        elif self._chunks:
            target = self._chunks
        if target:
            last = target[-1]
            if last and not last[-1].isspace() and last[-1] not in "([{\n>-":
                target.append(" ")

    def _capture_script_payload(self, raw_script: str, attrs: dict[str, str]) -> None:
        raw_str = raw_script.strip()
        if not raw_str:
            return
        script_id = (attrs.get("id") or "").strip()
        script_type = (attrs.get("type") or "").strip().lower()

        if script_id in {"__NEXT_DATA__", "__NUXT_DATA__"} or script_type in {
            "application/ld+json",
            "application/json",
        }:
            try:
                parsed = json.loads(raw_str)
                label = script_id or script_type or "json"
                self.structured_data.append((label, parsed))
                return
            except Exception:
                pass

        for state_name, assign_re in _WINDOW_STATE_ASSIGNMENTS:
            parsed = _extract_assigned_json(raw_str, assign_re)
            if parsed is not None:
                self.structured_data.append((state_name, parsed))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if t in self._ignore_tags:
            if t not in self.VOID_TAGS:
                self._ignore_stack.append(t)
            return

        if self._ignore_stack:
            return

        if t == "base" and attr_map.get("href") and not self.base_url:
            self.base_url = attr_map["href"].strip()
            return

        if t == "title":
            self._in_title = True
            self._title_buf = []
            return

        if t == "script":
            self._in_script = True
            self._script_attrs = attr_map
            self._script_buf = []
            return

        hidden_val = attr_map.get("hidden")
        aria_hidden = attr_map.get("aria-hidden", "").strip().lower()
        if hidden_val is not None and t not in {"input"}:
            if t not in self.VOID_TAGS:
                self._ignore_stack.append(t)
            return
        if aria_hidden == "true" and t not in {"input", "button", "a", "select", "textarea"}:
            if t not in self.VOID_TAGS:
                self._ignore_stack.append(t)
            return

        if t in self.INLINE_TAGS:
            self._ensure_inline_space()
            if t == "span":
                role = attr_map.get("role", "").strip().lower()
                aria_label = (attr_map.get("aria-label") or attr_map.get("title") or "").strip()
                if aria_label:
                    if role in {"button", "link", "tab", "menuitem", "switch", "checkbox", "searchbox", "textbox", "img", "icon"}:
                        self._emit(f"[{role.capitalize()}: {aria_label}] ")
                    elif self.browser_mode or "onclick" in attr_map or "tabindex" in attr_map or attr_map.get("aria-label"):
                        self._emit(f"[Aria: {aria_label}] ")

        if t in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(t[1])
            self._emit(f"\n\n{'#' * level} ")
        elif t in self.BLOCK_TAGS:
            if t == "form":
                action = clean_url(attr_map.get("action", ""), self.base_url)
                method = (attr_map.get("method") or "GET").upper()
                form_id = attr_map.get("id") or attr_map.get("name") or attr_map.get("aria-label") or ""
                meta_parts = [f"method={method}"]
                if action:
                    meta_parts.append(f"action={action}")
                if form_id:
                    meta_parts.append(f'id="{form_id}"')
                self._emit(f"\n\n[Form: {' '.join(meta_parts)}]\n")
            else:
                self._emit("\n")
                role = attr_map.get("role", "").strip().lower()
                aria_label = (attr_map.get("aria-label") or attr_map.get("title") or "").strip()
                if aria_label:
                    if role in {"button", "link", "tab", "menuitem", "switch", "checkbox", "searchbox", "textbox", "combobox", "dialog", "region", "navigation"}:
                        self._emit(f"[{role.capitalize()}: {aria_label}] ")
                    elif self.browser_mode or "onclick" in attr_map or "tabindex" in attr_map or t == "div":
                        self._emit(f"[Aria: {aria_label}] ")
        elif t == "br":
            self._emit("\n")
        elif t == "hr":
            self._emit("\n---\n")
        elif t == "li":
            indent = "  " * max(0, self._list_depth - 1)
            self._emit(f"\n{indent}- ")
        elif t in {"ul", "ol"}:
            self._list_depth += 1
            self._emit("\n")
        elif t == "pre":
            self._in_pre += 1
            self._emit("\n```\n")
        elif t == "code":
            self._in_code += 1
            if self._in_pre == 0:
                self._emit("`")
        elif t in {"strong", "b"}:
            self._emit("**")
        elif t in {"em", "i"}:
            self._emit("*")
        elif t == "a":
            href = clean_url(attr_map.get("href", ""), self.base_url)
            aria_label = (attr_map.get("aria-label") or attr_map.get("title") or "").strip()
            self._link_stack.append((href, aria_label, []))
        elif t == "button":
            self._button_stack.append((attr_map, []))
        elif t == "input":
            self._handle_input_tag(attr_map)
        elif t == "select":
            self._select_stack.append((attr_map, []))
        elif t == "option":
            self._option_stack.append((attr_map, []))
        elif t == "textarea":
            self._textarea_stack.append((attr_map, []))
        elif t == "img":
            alt = (attr_map.get("alt") or attr_map.get("aria-label") or attr_map.get("title") or "").strip()
            if alt:
                self._ensure_inline_space()
                self._emit(f"[Image: {alt}]")
        elif t == "table":
            self._in_table = True
            self._table_rows = []
        elif t == "tr" and self._in_table:
            self._current_row = []
        elif t in {"td", "th"} and self._in_table:
            self._in_cell = True
            self._cell_buf = []

    def _handle_input_tag(self, attr_map: dict[str, str]) -> None:
        inp_type = (attr_map.get("type") or "text").strip().lower()
        name = (
            attr_map.get("name")
            or attr_map.get("id")
            or attr_map.get("aria-label")
            or attr_map.get("placeholder")
            or ""
        ).strip()
        placeholder = (attr_map.get("placeholder") or "").strip()
        aria_label = (attr_map.get("aria-label") or "").strip()
        alt_text = (attr_map.get("alt") or "").strip()
        value = attr_map.get("value") or ""

        if inp_type == "hidden":
            norm_hidden_name = re.sub(r"[^a-z0-9]", "", name.lower())
            if (
                name
                and not _is_sensitive_key(name)
                and not _SENSITIVE_ATTR_RE.search(name)
                and value
                and not _SECRET_VALUE_RE.search(value)
                and len(value) <= 64
                and (
                    norm_hidden_name in _SAFE_HIDDEN_INPUT_NAMES
                    or norm_hidden_name.endswith(("id", "page", "mode", "type", "step", "lang", "locale"))
                )
            ):
                self._emit(f" [Hidden: {name}={value}] ")
            return

        if inp_type in {"submit", "button", "reset", "image"}:
            btn_label = value or alt_text or aria_label or name or inp_type.capitalize()
            self._ensure_inline_space()
            self._emit(f"[Button: {btn_label}] ")
            return

        is_sensitive = (
            inp_type == "password"
            or _is_sensitive_key(name)
            or bool(_SENSITIVE_ATTR_RE.search(name))
            or bool(_SECRET_VALUE_RE.search(value))
        )
        parts = [f"type={inp_type}"]
        if name:
            parts.append(f"name={name}")
        if aria_label and aria_label != name:
            parts.append(f'label="{aria_label}"')
        if placeholder and placeholder != name:
            parts.append(f'placeholder="{placeholder}"')
        if "checked" in attr_map:
            parts.append("checked")
        if "required" in attr_map:
            parts.append("required")
        if value:
            if is_sensitive:
                parts.append("value=<redacted>")
            elif len(value) <= 80:
                parts.append(f'value="{value}"')
            else:
                parts.append(f'value="{value[:80]}..."')
        self._ensure_inline_space()
        self._emit(f"[Input: {' '.join(parts)}] ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()

        if t == "title":
            if self._in_title:
                self._in_title = False
                title_text = re.sub(r"\s+", " ", "".join(self._title_buf)).strip()
                if title_text and not self.page_title:
                    self.page_title = title_text
            return

        if t == "script":
            if self._in_script:
                self._in_script = False
                self._capture_script_payload("".join(self._script_buf), self._script_attrs)
                self._script_buf = []
                self._script_attrs = {}
            return

        if t in {"body", "html"}:
            self._ignore_stack.clear()
            return

        if self._ignore_stack:
            if t == self._ignore_stack[-1]:
                self._ignore_stack.pop()
            elif t in self._ignore_stack:
                idx = len(self._ignore_stack) - 1 - self._ignore_stack[::-1].index(t)
                del self._ignore_stack[idx:]
            return

        if t in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._emit("\n")
        elif t in self.BLOCK_TAGS:
            self._emit("\n")
        elif t in {"ul", "ol"}:
            self._list_depth = max(0, self._list_depth - 1)
            self._emit("\n")
        elif t == "pre":
            self._in_pre = max(0, self._in_pre - 1)
            self._emit("\n```\n")
        elif t == "code":
            if self._in_pre == 0:
                self._emit("`")
            self._in_code = max(0, self._in_code - 1)
        elif t in {"strong", "b"}:
            self._emit("**")
        elif t in {"em", "i"}:
            self._emit("*")
        elif t == "a" and self._link_stack:
            href, aria_label, buf = self._link_stack.pop()
            label = re.sub(r"\s+", " ", "".join(buf)).strip() or aria_label
            if not label and href:
                label = href
            if self.keep_links and href and not href.startswith("#") and href != "<data-uri omitted>":
                self._emit(f"[{label}]({href})")
            elif label:
                self._emit(label)
        elif t == "button" and self._button_stack:
            attr_map, buf = self._button_stack.pop()
            label = re.sub(r"\s+", " ", "".join(buf)).strip()
            if not label:
                label = (
                    attr_map.get("aria-label")
                    or attr_map.get("title")
                    or attr_map.get("value")
                    or attr_map.get("name")
                    or "Button"
                ).strip()
            disabled = " disabled" if "disabled" in attr_map else ""
            self._emit(f"[Button: {label}{disabled}] ")
        elif t == "option" and self._option_stack:
            attr_map, buf = self._option_stack.pop()
            opt_text = re.sub(r"\s+", " ", "".join(buf)).strip()
            opt_val = (attr_map.get("value") or opt_text).strip()
            selected = "*" if "selected" in attr_map else ""
            entry = f"{selected}{opt_text}" if opt_text else f"{selected}{opt_val}"
            if self._select_stack and entry:
                self._select_stack[-1][1].append(entry)
        elif t == "select" and self._select_stack:
            attr_map, options = self._select_stack.pop()
            name = (
                attr_map.get("name")
                or attr_map.get("id")
                or attr_map.get("aria-label")
                or "select"
            ).strip()
            preview = ", ".join(options[:8])
            if len(options) > 8:
                preview += f", ... (+{len(options) - 8})"
            self._emit(f"[Select: {name} options=[{preview}]] ")
        elif t == "textarea" and self._textarea_stack:
            attr_map, buf = self._textarea_stack.pop()
            name = (
                attr_map.get("name")
                or attr_map.get("id")
                or attr_map.get("aria-label")
                or attr_map.get("placeholder")
                or "textarea"
            ).strip()
            placeholder = (attr_map.get("placeholder") or "").strip()
            val = re.sub(r"\s+", " ", "".join(buf)).strip()
            meta = f"name={name}"
            if placeholder and placeholder != name:
                meta += f' placeholder="{placeholder}"'
            if val:
                if _is_sensitive_key(name) or _SENSITIVE_ATTR_RE.search(name) or _SECRET_VALUE_RE.search(val):
                    meta += " value=<redacted>"
                else:
                    scrubbed_val = _scrub_string_value(val, max_str_len=80, redact_secrets=True, key_name=name)
                    meta += f' value="{scrubbed_val}"'
            self._emit(f"[Textarea: {meta}] ")
        elif t in {"td", "th"} and self._in_cell:
            self._in_cell = False
            cell_text = re.sub(r"\s+", " ", "".join(self._cell_buf)).strip()
            self._current_row.append(cell_text.replace("|", "\\|"))
        elif t == "tr" and self._in_table:
            if any(c for c in self._current_row):
                self._table_rows.append(self._current_row)
            self._current_row = []
        elif t == "table" and self._in_table:
            self._in_table = False
            if self._table_rows:
                max_cols = max(len(r) for r in self._table_rows)
                lines = ["\n"]
                for idx, row in enumerate(self._table_rows):
                    padded = row + [""] * (max_cols - len(row))
                    lines.append("| " + " | ".join(padded) + " |")
                    if idx == 0:
                        lines.append("| " + " | ".join(["---"] * max_cols) + " |")
                lines.append("\n")
                self._chunks.append("\n".join(lines))
            self._table_rows = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
            return
        if self._in_script:
            self._script_buf.append(data)
            return
        if self._ignore_stack:
            return
        if self._in_pre > 0:
            self._emit(data)
        else:
            collapsed = re.sub(r"[ \t\r\f\v]+", " ", data)
            if "\n" in collapsed:
                collapsed = re.sub(r"\s*\n\s*", " ", collapsed)
            self._emit(collapsed)


def html_to_compact_markdown(
    html_text: str,
    keep_links: bool = True,
    max_chars: int = 0,
    browser_mode: bool = False,
    base_url: str = "",
) -> str:
    """
    Convert raw HTML into compact, Browser-Use-safe & SPA-safe Markdown.
    - Preserves forms, inputs, buttons, selects, image alt text, tables, and headings.
    - Extracts `__NEXT_DATA__`, `application/ld+json`, and `window.__INITIAL_STATE__`
      into a compact `### [Structured Data]` section.
    - Strips `<style>`, `<svg>`, tracking query parameters, and inline `data:` URIs.
    - Preserves `<pre><code>` indentation.
    """
    cleaned = re.sub(r"<!--[\s\S]*?-->", "", html_text)
    cleaned = _DATA_URI_RE.sub(lambda m: f"<data-uri omitted ({len(m.group(0))} chars)>", cleaned)

    parser = _CompactHTMLParser(
        keep_links=keep_links,
        browser_mode=browser_mode,
        base_url=base_url,
    )
    parser.feed(cleaned)
    parser.close()

    raw = "".join(parser._chunks)

    # Normalize lines while preserving indentation inside ``` code blocks
    out_lines: list[str] = []
    in_fence = False
    blank_run = 0

    if parser.page_title:
        first_non_empty = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        if parser.page_title not in first_non_empty:
            out_lines.append(f"# {parser.page_title}")
            out_lines.append("")

    for line in raw.splitlines():
        stripped_check = line.strip()
        if stripped_check.startswith("```"):
            in_fence = not in_fence
            out_lines.append(stripped_check)
            blank_run = 0
            continue

        if in_fence:
            out_lines.append(line.rstrip())
            continue

        s = re.sub(r"[ \t]{2,}", " ", stripped_check)
        if not s:
            blank_run += 1
            if blank_run <= 1 and out_lines and out_lines[-1] != "":
                out_lines.append("")
            continue

        blank_run = 0
        out_lines.append(s)

    body_text = "\n".join(out_lines).strip()

    struct_section = ""
    if parser.structured_data:
        slimmed_blocks: list[str] = []
        for label, payload in parser.structured_data[:6]:
            if label == "__NEXT_DATA__" and isinstance(payload, dict):
                page_props = payload.get("props", {}).get("pageProps")
                if page_props:
                    payload = {"pageProps": page_props, "page": payload.get("page")}
            slimmed = slim_json_value(
                payload,
                max_items=15,
                max_str_len=240,
                max_depth=5,
            )
            if slimmed not in (None, {}, []):
                compact_json = json.dumps(slimmed, ensure_ascii=False, separators=(",", ":"))
                slimmed_blocks.append(f"- **{label}**: `{compact_json}`")
        if slimmed_blocks:
            struct_section = "### [Structured Data]\n" + "\n".join(slimmed_blocks)

    if max_chars > 0:
        if struct_section:
            # Reserve budget for Structured Data so SPA payloads stay intact while strictly honoring max_chars
            if len(struct_section) >= max_chars:
                return struct_section[:max_chars].rstrip()
            suffix = "\n\n... [truncated]"
            avail_body = max(0, max_chars - len(struct_section) - len(suffix) - 2)
            if len(body_text) > avail_body:
                body_text = (body_text[:avail_body].rstrip() + suffix) if avail_body > 0 else ""
        elif len(body_text) > max_chars:
            suffix = "\n\n... [truncated]"
            if max_chars <= len(suffix):
                return body_text[:max_chars]
            cut = max_chars - len(suffix)
            body_text = body_text[:cut].rstrip() + suffix

    if struct_section:
        body_text = f"{body_text}\n\n{struct_section}".strip() if body_text else struct_section

    if max_chars > 0 and len(body_text) > max_chars:
        body_text = body_text[:max_chars].rstrip()

    return body_text


# Alias requested by callers/audits
html_to_markdown = html_to_compact_markdown


# ---------------------------------------------------------------------------
# 2. Playwright / Browser-Use ARIA Accessibility Snapshot Slimmer
# ---------------------------------------------------------------------------

_NOISY_ARIA_WRAPPER_RE = re.compile(
    r"^(\s*)-\s+(?:generic|group|presentation|none|region|section|div)\s*:?\s*$",
    re.IGNORECASE,
)
_EMPTY_ARIA_TEXT_RE = re.compile(r'^\s*-\s+text:\s*(?:""|\'\')?\s*$', re.IGNORECASE)
_ARIA_INDENT_RE = re.compile(r"^(\s*)-\s+(.*)$")
_ARIA_REF_TOKEN_RE = re.compile(r"\[ref=[^\]]+\]")
_ARIA_LEAF_CONTENT_RE = re.compile(
    r"^(?:text|img|separator|button|link|textbox|searchbox|combobox|checkbox|radio|switch|slider|spinbutton|menuitem|option|tab|heading)\b",
    re.IGNORECASE,
)


def _is_aria_container_node(content: str) -> bool:
    """
    Determine whether an ARIA tree node (`content` without leading `- `) can have
    children in the accessibility hierarchy:
    - `text: ...` and leaf controls without a trailing `:` are leaves and must NEVER
      become parent containers of subsequent siblings when `- generic:` wrappers are
      collapsed.
    """
    s = content.strip()
    if s.lower().startswith("text:"):
        return False
    if s.endswith(":"):
        return True
    if _ARIA_LEAF_CONTENT_RE.match(s):
        return False
    return False


def slim_aria_snapshot(aria_text: str, max_text_len: int = 240) -> str:
    """
    Compress Playwright / Browser-Use ARIA accessibility snapshots:
    - Preserves 100% of `[ref=...]` markers across the ENTIRE document (never
      middle-truncates interactive refs!).
    - Preserves 100% of interactive and semantic roles (`button`, `link`, `textbox`,
      `searchbox`, `combobox`, `checkbox`, `radio`, `switch`, `tab`, `menuitem`,
      `heading`, `img`, `table`, `row`, `cell`, `form`, `dialog`, `alert`, etc.).
    - Collapses nameless, ref-less structural wrapper lines (`- generic:`, `- group:`,
      `- presentation:`, `- none:`) and re-normalizes indentation using a container-aware
      ancestor stack so sibling elements never become children of preceding leaf nodes
      (such as `- text: ...`).
    - Deduplicates consecutive identical static text nodes and trims overly long
      labels while preserving all `[ref=...]` attributes anywhere on the line.
    """
    if not aria_text.strip():
        return ""

    kept_entries: list[tuple[int, str, bool]] = []
    prev_norm: tuple[int, str] | None = None
    dup_count = 0

    for raw_line in aria_text.splitlines():
        if not raw_line.strip():
            continue
        if _EMPTY_ARIA_TEXT_RE.match(raw_line):
            continue

        refs = _ARIA_REF_TOKEN_RE.findall(raw_line)
        has_ref = bool(refs)
        if not has_ref and _NOISY_ARIA_WRAPPER_RE.match(raw_line):
            continue

        m = _ARIA_INDENT_RE.match(raw_line)
        if m:
            indent_spaces = len(m.group(1))
            content = m.group(2).strip()
        else:
            indent_spaces = len(raw_line) - len(raw_line.lstrip(" "))
            content = raw_line.strip()

        if not has_ref and content in {"img", "separator", "none", "presentation"}:
            continue

        # Strip inline data URIs inside ARIA descriptions
        content = _DATA_URI_RE.sub("data:<omitted>", content)
        is_container = _is_aria_container_node(content)

        # Truncate overly long text/labels while preserving all [ref=...] markers anywhere on the line
        if max_text_len > 0 and len(content) > max_text_len:
            trailing_colon = ":" if is_container and content.endswith(":") else ""
            if has_ref:
                without_refs = _ARIA_REF_TOKEN_RE.sub("", content).strip()
                if without_refs.endswith(":"):
                    without_refs = without_refs[:-1].rstrip()
                ref_str = " ".join(refs)
                budget = max(32, max_text_len - len(ref_str) - 16)
                if len(without_refs) > budget:
                    without_refs = without_refs[:budget].rstrip() + '..."'
                content = f"{without_refs} {ref_str}{trailing_colon}".strip()
            else:
                content = content[:max_text_len].rstrip() + f"...{trailing_colon}"

        # Deduplicate consecutive identical non-ref lines at the same indent
        if not has_ref and prev_norm == (indent_spaces, content):
            dup_count += 1
            continue
        if dup_count > 0 and kept_entries:
            last_indent, last_content, last_cont = kept_entries[-1]
            kept_entries[-1] = (last_indent, f"{last_content} [x{dup_count + 1}]", last_cont)
            dup_count = 0

        prev_norm = (indent_spaces, content)
        kept_entries.append((indent_spaces, content, is_container))

    if dup_count > 0 and kept_entries:
        last_indent, last_content, last_cont = kept_entries[-1]
        kept_entries[-1] = (last_indent, f"{last_content} [x{dup_count + 1}]", last_cont)

    if not kept_entries:
        return ""

    # Container-aware indentation normalization: only container nodes are pushed onto
    # the ancestor stack so leaf nodes (`- text: ...`, `- button ...`) never adopt siblings.
    container_stack: list[tuple[int, int]] = []
    out_lines: list[str] = []
    for raw_indent, content, is_container in kept_entries:
        while container_stack and raw_indent <= container_stack[-1][0]:
            container_stack.pop()
        level = (container_stack[-1][1] + 1) if container_stack else 0
        prefix = "  " * level
        if content.startswith("- "):
            out_lines.append(f"{prefix}{content}")
        else:
            out_lines.append(f"{prefix}- {content}")
        if is_container:
            container_stack.append((raw_indent, level))

    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# 3. JSON & JSONL Schema Slimmer (with Base64 & Secret Scrubbing)
# ---------------------------------------------------------------------------

DEFAULT_DROP_JSON_KEYS = {
    "profile_image_url",
    "profile_image_url_https",
    "profile_banner_url",
    "profile_background_image_url",
    "profile_background_image_url_https",
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
    "ext_edit_control",
    "edit_controls",
    "edit_history_tweet_ids",
    "unmention_data",
    "node_id",
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
}

SENSITIVE_JSON_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "api_key",
    "apikey",
    "x-api-key",
    "authorization",
    "cookie",
    "set-cookie",
    "private_key",
    "secret_key",
    "session_token",
    "session_id",
    "session_key",
    "csrf",
    "csrf_token",
    "xsrf",
    "xsrf_token",
    "webhook_secret",
    "signing_secret",
    "master_key",
    "bearer",
    "bearer_token",
    "jwt",
    "jwt_token",
    "oauth_token",
    "api_token",
    "x_auth_token",
    "authenticity_token",
    "code_verifier",
    "recovery_code",
    "totp",
    "otp",
    "pin",
    "cvv",
    "cvc",
    "passphrase",
    "credentials",
    "credential",
    "token",
    "auth",
    "key",
    "session",
    "private",
    "signature",
    "sig",
    "nonce",
}

SENSITIVE_KEY_NORMS: frozenset[str] = frozenset(
    re.sub(r"[^a-z0-9]", "", k.lower()) for k in SENSITIVE_JSON_KEYS
)

SENSITIVE_KEY_NORM_SUFFIXES: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "secretkey",
    "apikey",
    "privatekey",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "sessiontoken",
    "sessionid",
    "sessionkey",
    "csrftoken",
    "xsrftoken",
    "webhooksecret",
    "signingsecret",
    "masterkey",
    "bearertoken",
    "jwttoken",
    "oauthtoken",
    "apitoken",
    "xauthtoken",
    "authenticitytoken",
    "securitytoken",
    "resettoken",
    "verifymailtoken",
    "verificationtoken",
    "codeverifier",
    "passphrase",
    "privatekeypem",
    "clientcertificate",
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "privatekey",
    "secretkey",
    "sessionid",
    "session",
    "credential",
    "credentials",
    "signature",
)

_SAFE_TOKEN_KEY_NORMS: frozenset[str] = frozenset(
    {
        "tokens",
        "tokencount",
        "tokenestimate",
        "tokensbefore",
        "tokensafter",
        "tokenssaved",
        "maxtokens",
        "prompttokens",
        "completiontokens",
        "totaltokens",
        "inputtokens",
        "outputtokens",
        "cachedtokens",
        "reasoningtokens",
        "nextpagetoken",
        "pagetoken",
        "continuationtoken",
        "synctoken",
        "normtokens",
    }
)


def _is_sensitive_key(key: str) -> bool:
    """
    Normalize key names across snake_case, camelCase, and kebab-case
    (`re.sub(r'[^a-z0-9]', '', key.lower())`) and check against known secret key patterns
    while whitelisting benign token counter keys (`tokens_saved`, `max_tokens`, etc.).
    """
    norm = re.sub(r"[^a-z0-9]", "", key.strip().lower())
    if not norm or norm in _SAFE_TOKEN_KEY_NORMS:
        return False
    if norm in SENSITIVE_KEY_NORMS:
        return True
    if norm.endswith(SENSITIVE_KEY_NORM_SUFFIXES):
        return True
    return False


_SECRET_VALUE_RE = re.compile(
    r"(?:"
    r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b|"
    r"\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"\bAIza[0-9A-Za-z_-]{35}\b|"
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bglpat-[A-Za-z0-9_-]{20,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|"
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)|"
    r"(?i:\bBearer)\s+[A-Za-z0-9._~+/-]{16,}=*"
    r")"
)


def _scrub_string_value(val: str, max_str_len: int, redact_secrets: bool = True, key_name: str = "") -> str:
    """
    Scrub inline data URIs, secret token patterns (Stripe/Google/Slack/GitHub/AWS/OpenAI keys,
    JWTs, PEM keys, Bearer tokens), and large hex/base64 payloads BEFORE applying max_str_len.
    """
    if redact_secrets and key_name and _is_sensitive_key(key_name):
        return "<redacted>"
    stripped = val.strip()
    if stripped.lower().startswith("data:") and (";base64," in stripped.lower() or len(stripped) >= 64):
        return f"<data-uri omitted ({len(val)} chars)>"
    if _DATA_URI_RE.search(stripped):
        val = _DATA_URI_RE.sub(lambda m: f"<data-uri omitted ({len(m.group(0))} chars)>", val)
    if redact_secrets and _SECRET_VALUE_RE.search(val):
        val = _SECRET_VALUE_RE.sub("<redacted>", val)
    stripped = val.strip()
    if len(stripped) >= 256 and " " not in stripped:
        # Check _HEX_BLOB_RE BEFORE _BASE64_BLOB_RE because hex chars [0-9a-fA-F] are a subset of base64!
        if _HEX_BLOB_RE.match(stripped):
            return f"<hex-blob omitted ({len(val)} chars)>"
        if _BASE64_BLOB_RE.match(stripped) and not stripped.startswith(("http://", "https://", "/")):
            return f"<base64 omitted ({len(val)} chars)>"
    if max_str_len > 0 and len(val) > max_str_len:
        return val[:max_str_len] + f"... [+{len(val) - max_str_len} chars]"
    return val


def slim_json_value(
    obj: Any,
    max_items: int = 20,
    max_str_len: int = 600,
    max_depth: int = 8,
    drop_keys: set[str] | None = None,
    redact_secrets: bool = True,
    _depth: int = 0,
) -> Any:
    """
    Recursively slim a parsed JSON object:
    - Drops null/empty values and noisy UI/avatar metadata keys.
    - Redacts sensitive credential keys across snake_case, camelCase, and kebab-case
      (`password`, `accessToken`, `clientSecret`, `apiKey`, `privateKey`, etc.).
    - Replaces inline `data:...` URIs and long hex/base64 blobs with compact markers.
    - Caps arrays at `max_items` with a trailing summary marker.
    """
    if drop_keys is None:
        drop_keys = DEFAULT_DROP_JSON_KEYS

    if _depth >= max_depth:
        if isinstance(obj, (dict, list)):
            return f"<omitted depth={_depth} type={type(obj).__name__}>"
        return obj

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            k_str = str(k)
            k_lower = k_str.lower()
            if k_str in drop_keys or k_lower in drop_keys:
                continue
            if v is None or v == "" or v == [] or v == {}:
                continue
            if redact_secrets and _is_sensitive_key(k_str):
                out[k_str] = "<redacted>"
                continue
            slimmed_v = slim_json_value(
                v,
                max_items=max_items,
                max_str_len=max_str_len,
                max_depth=max_depth,
                drop_keys=drop_keys,
                redact_secrets=redact_secrets,
                _depth=_depth + 1,
            )
            if slimmed_v is None or slimmed_v == "" or slimmed_v == [] or slimmed_v == {}:
                continue
            out[k_str] = slimmed_v
        return out

    if isinstance(obj, list):
        total = len(obj)
        sliced = obj[:max_items] if max_items > 0 else obj
        out_list = []
        for item in sliced:
            slimmed_item = slim_json_value(
                item,
                max_items=max_items,
                max_str_len=max_str_len,
                max_depth=max_depth,
                drop_keys=drop_keys,
                redact_secrets=redact_secrets,
                _depth=_depth + 1,
            )
            if slimmed_item is not None and slimmed_item != "" and slimmed_item != {}:
                out_list.append(slimmed_item)
        if max_items > 0 and total > max_items:
            out_list.append(f"... [{total - max_items} more items omitted (total={total})]")
        return out_list

    if isinstance(obj, str):
        return _scrub_string_value(obj, max_str_len=max_str_len, redact_secrets=redact_secrets)

    return obj


def slim_json_text(
    json_text: str,
    max_items: int = 20,
    max_str_len: int = 600,
    max_depth: int = 8,
    indent: int | None = None,
    _parsed_obj: Any = None,
) -> str:
    """Parse JSON text (or reuse `_parsed_obj`), slim it, and return compact JSON."""
    data = _parsed_obj if _parsed_obj is not None else json.loads(json_text)
    slimmed = slim_json_value(
        data,
        max_items=max_items,
        max_str_len=max_str_len,
        max_depth=max_depth,
    )
    if indent is None:
        return json.dumps(slimmed, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(slimmed, ensure_ascii=False, indent=indent)


def slim_jsonl_text(
    jsonl_text: str | Iterable[str],
    max_records: int = 30,
    max_items: int = 10,
    max_str_len: int = 400,
) -> str:
    """
    Single-pass streaming slimmer for JSONL / NDJSON logs.
    Keeps head and tail records via a bounded `deque` without loading all parsed
    objects into memory at once.
    """
    lines_iter: Iterable[str]
    if isinstance(jsonl_text, str):
        lines_iter = jsonl_text.splitlines()
    else:
        lines_iter = jsonl_text

    head_Target = max_records // 2 if max_records > 0 else 0
    tail_target = max_records - head_Target if max_records > 0 else 0

    head_records: list[str] = []
    tail_deque: collections.deque[str] = collections.deque(
        maxlen=tail_target if tail_target > 0 else None
    )
    total = 0

    def _format_line(raw_line: str) -> str:
        try:
            obj = json.loads(raw_line)
            slimmed = slim_json_value(obj, max_items=max_items, max_str_len=max_str_len)
            return json.dumps(slimmed, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return _scrub_string_value(raw_line, max_str_len=max_str_len)

    for raw in lines_iter:
        s = raw.strip()
        if not s:
            continue
        total += 1
        if max_records <= 0 or total <= head_Target:
            head_records.append(_format_line(s))
        else:
            tail_deque.append(s)

    if total == 0:
        return ""

    out_lines: list[str] = [f"# JSONL Summary: total_records={total}"]
    out_lines.extend(head_records)

    omitted = total - len(head_records) - len(tail_deque)
    if omitted > 0:
        out_lines.append(f"# ... [{omitted} middle records omitted] ...")

    for raw_tail in tail_deque:
        out_lines.append(_format_line(raw_tail))

    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# 4. CSV / TSV Statistical Summary + Sample Slimmer (Streaming + Decimal)
# ---------------------------------------------------------------------------


def slim_csv_text(
    csv_text: str | Path | Iterable[str],
    delimiter: str | None = None,
    head_rows: int = 8,
    tail_rows: int = 5,
    max_cell_len: int = 120,
) -> str:
    """
    Single-pass O(1)-memory streaming compressor for large CSV/TSV datasets:
    - Accepts a `str`, `Path` (streamed directly from disk), or line iterator (`Iterable[str]`).
    - Keeps header + first `head_rows` + last `tail_rows` (via bounded `deque`).
    - Computes 60-digit precision `Decimal` summary statistics (`count`, `min`, `max`, `mean`, `sum`)
      for every numeric column across ALL rows without loading all rows into RAM.
    """
    try:
        csv.field_size_limit(min(sys.maxsize, 134_217_728))
    except OverflowError:
        csv.field_size_limit(134_217_728)

    file_handle = None
    try:
        if isinstance(csv_text, Path):
            file_handle = csv_text.open("r", encoding="utf-8", errors="replace", newline="")
            first_line = file_handle.readline()
            if not first_line:
                return ""
            if delimiter is None:
                delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","
            file_handle.seek(0)
            line_source: Iterable[str] = file_handle
        elif isinstance(csv_text, str):
            if not csv_text.strip():
                return ""
            if delimiter is None:
                sample_head = csv_text[:4096]
                nl_idx = sample_head.find("\n")
                first_line = sample_head[:nl_idx] if nl_idx != -1 else sample_head
                delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","
            line_source = io.StringIO(csv_text)
        else:
            it = iter(csv_text)
            first_chunk: list[str] = []
            for ln in it:
                if ln.strip():
                    first_chunk.append(ln)
                    break
            if not first_chunk:
                return ""
            if delimiter is None:
                delimiter = "\t" if first_chunk[0].count("\t") > first_chunk[0].count(",") else ","
            def _chain_iter() -> Iterable[str]:
                yield from first_chunk
                yield from it
            line_source = _chain_iter()

        return _slim_csv_from_iterable(
            line_source,
            delimiter=delimiter or ",",
            head_rows=head_rows,
            tail_rows=tail_rows,
            max_cell_len=max_cell_len,
        )
    finally:
        if file_handle is not None:
            file_handle.close()


def _slim_csv_from_iterable(
    line_source: Iterable[str],
    delimiter: str = ",",
    head_rows: int = 8,
    tail_rows: int = 5,
    max_cell_len: int = 120,
) -> str:
    reader = csv.reader(line_source, delimiter=delimiter)
    try:
        headers = [h.strip() for h in next(reader)]
    except StopIteration:
        return ""

    col_count = len(headers)
    total_rows = 0
    head_buffer: list[list[str]] = []
    tail_deque: collections.deque[list[str]] = collections.deque(
        maxlen=tail_rows if tail_rows > 0 else 0
    )

    # Per-column exact Decimal aggregators: [count, min, max, sum]
    num_count = [0] * col_count
    num_min: list[Decimal | None] = [None] * col_count
    num_max: list[Decimal | None] = [None] * col_count
    num_sum: list[Decimal] = [Decimal(0)] * col_count

    with localcontext() as ctx:
        ctx.prec = 60
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            total_rows += 1
            if len(row) < col_count:
                row = row + [""] * (col_count - len(row))
            elif len(row) > col_count:
                row = row[:col_count]

            if total_rows <= head_rows:
                head_buffer.append(row)
            elif tail_rows > 0:
                tail_deque.append(row)

            for idx, cell in enumerate(row):
                val_str = cell.strip().replace(",", "").replace("$", "").replace("¥", "").replace("€", "")
                if not val_str or val_str.endswith("%"):
                    continue
                try:
                    d_val = Decimal(val_str)
                    if d_val.is_nan() or d_val.is_infinite():
                        continue
                    num_count[idx] += 1
                    num_sum[idx] += d_val
                    if num_min[idx] is None or d_val < num_min[idx]:
                        num_min[idx] = d_val
                    if num_max[idx] is None or d_val > num_max[idx]:
                        num_max[idx] = d_val
                except (InvalidOperation, ValueError):
                    pass

        def _fmt_dec(d: Decimal) -> str:
            try:
                norm_d = d.normalize()
                if norm_d == norm_d.to_integral_value():
                    return format(norm_d.to_integral_value(), "f")
                s = format(norm_d, "f")
                if "." in s:
                    int_p, frac_p = s.split(".", 1)
                    if len(frac_p) > 12:
                        frac_p = frac_p[:12].rstrip("0")
                    else:
                        frac_p = frac_p.rstrip("0")
                    return f"{int_p}.{frac_p}" if frac_p else int_p
                return s or "0"
            except InvalidOperation:
                return str(d)

        stat_lines: list[str] = []
        for idx, h in enumerate(headers):
            cnt = num_count[idx]
            if cnt > 0 and num_min[idx] is not None and num_max[idx] is not None:
                mean_val = num_sum[idx] / Decimal(cnt)
                stat_lines.append(
                    f"- **{h or f'col_{idx}'}** (`n={cnt}`): "
                    f"min={_fmt_dec(num_min[idx])}, max={_fmt_dec(num_max[idx])}, "
                    f"mean={_fmt_dec(mean_val)}, sum={_fmt_dec(num_sum[idx])}"
                )

    def _fmt_row(r: list[str]) -> str:
        cells = []
        for c in r:
            s = re.sub(r"\s+", " ", c.strip()).replace("|", "\\|")
            if max_cell_len > 0 and len(s) > max_cell_len:
                s = s[:max_cell_len] + "..."
            cells.append(s)
        return "| " + " | ".join(cells) + " |"

    lines: list[str] = [f"### CSV Summary (`rows={total_rows}`, `columns={col_count}`)"]



    if stat_lines:
        lines.append("\n**Numeric Column Statistics (across all rows):**")
        lines.extend(stat_lines)

    lines.append("\n**Sample Rows:**")
    lines.append(_fmt_row(headers))
    lines.append("| " + " | ".join(["---"] * col_count) + " |")

    for r in head_buffer:
        lines.append(_fmt_row(r))

    omitted = total_rows - len(head_buffer) - len(tail_deque)
    if omitted > 0:
        lines.append(f"| ... ({omitted} middle rows omitted) ... |" + " |" * (col_count - 1))

    for r in tail_deque:
        lines.append(_fmt_row(r))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Git Diff & Lockfile Compressor
# ---------------------------------------------------------------------------

LOCKFILE_PATTERNS = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
    ".min.js",
    ".min.css",
    ".bundle.js",
    ".map",
    "dist/",
    "build/",
    "node_modules/",
    "__snapshots__/",
    ".snap",
)


def slim_git_diff(
    diff_text: str,
    max_hunk_lines: int = 80,
    max_line_len: int = 300,
) -> str:
    """
    Compress `git diff` / PR patch output:
    - Collapses lockfiles, generated/minified bundles, and snapshot diffs into a 1-line
      summary showing added/removed line counts.
    - Caps individual hunks at `max_hunk_lines` while keeping file headers and change stats.
    """
    if not diff_text.strip():
        return ""

    file_chunks = re.split(r"(?m)(?=^diff --git )", diff_text)
    out_blocks: list[str] = []

    for chunk in file_chunks:
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        header = lines[0]

        m = re.match(r"^diff --git a/(.+?) b/(.+)$", header)
        filepath = m.group(2) if m else header

        added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))

        is_noisy_file = any(
            filepath.endswith(pat) or f"/{pat}" in filepath or filepath.startswith(pat)
            for pat in LOCKFILE_PATTERNS
        )
        if is_noisy_file:
            out_blocks.append(
                f"{header}\n  [collapsed generated/lockfile diff: `+{added}` / `-{removed}` lines in `{filepath}`]"
            )
            continue

        kept: list[str] = []
        current_hunk: list[str] = []

        def _flush_hunk() -> None:
            nonlocal current_hunk
            if not current_hunk:
                return
            h_lines = current_hunk
            current_hunk = []

            def _trunc_line(s: str) -> str:
                if max_line_len > 0 and len(s) > max_line_len:
                    return s[:max_line_len] + f"... [+{len(s) - max_line_len} chars]"
                return s

            if max_hunk_lines <= 0 or len(h_lines) <= max_hunk_lines:
                kept.extend(_trunc_line(x) for x in h_lines)
                return

            # ALWAYS preserve 100% of changed lines (+/-) so zero code modifications are ever lost!
            # Only collapse long runs of unchanged context lines (keeping +/- 2 context lines around changes).
            changed_indices: set[int] = {
                idx for idx, hl in enumerate(h_lines) if hl.startswith(("+", "-", "\\"))
            }
            must_keep: set[int] = set(changed_indices)
            must_keep.update(range(min(4, len(h_lines))))
            must_keep.update(range(max(0, len(h_lines) - 4), len(h_lines)))
            context_radius = 1 if len(changed_indices) > max_hunk_lines else 2
            for idx in changed_indices:
                for ctx_i in range(max(0, idx - context_radius), min(len(h_lines), idx + context_radius + 1)):
                    must_keep.add(ctx_i)

            sorted_idx = sorted(must_keep)

            last_i = -1
            for idx in sorted_idx:
                if last_i != -1 and idx > last_i + 1:
                    gap = idx - last_i - 1
                    kept.append(f"  ... [{gap} unchanged/hunk lines omitted] ...")
                kept.append(_trunc_line(h_lines[idx]))
                last_i = idx
            if last_i < len(h_lines) - 1:
                gap = len(h_lines) - 1 - last_i
                kept.append(f"  ... [{gap} unchanged/hunk lines omitted] ...")

        for ln in lines:
            if ln.startswith(("diff --git ", "index ", "--- ", "+++ ", "new file", "deleted file", "similarity ", "rename ", "Binary files ")):
                _flush_hunk()
                kept.append(ln)
            elif ln.startswith("@@"):
                _flush_hunk()
                kept.append(ln)
            else:
                current_hunk.append(ln)

        _flush_hunk()
        out_blocks.append("\n".join(kept))

    return "\n\n".join(out_blocks)


# ---------------------------------------------------------------------------
# 6. Markdown Safe Cleaner & Outline / Section Extractor
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


_MD_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def slim_markdown_body(md_text: str) -> str:
    """
    Safe Markdown slimmer used by `token-saver slim <file.md>`:
    - Preserves 100% of headings, body paragraphs, tables, lists, fenced code blocks
      (```` ``` ```` and `~~~`), and inline code spans (`...`) verbatim!
    - Outside code blocks/spans: strips HTML comments (`<!-- ... -->`), replaces inline
      `data:...` images with `<data-uri omitted>`, strips tracking URL parameters (`utm_*`, etc.),
      and collapses 3+ consecutive blank lines into a single blank line.
    """
    if not md_text:
        return ""

    # 1. Stash fenced code blocks (```...```, ````...````, ~~~...~~~, including fences closed
    # by >= opening length or unclosed at EOF per CommonMark) and inline code spans (`...`)
    # so HTML comments, URLs, or blank lines inside code examples are 100% untouched.
    stash: list[str] = []

    def _stash_str(s: str) -> str:
        idx = len(stash)
        stash.append(s)
        return f"\x00MDSLIM_CODE_{idx}\x00"

    lines = md_text.splitlines(keepends=True)
    out_chunks: list[str] = []
    fence_char: str | None = None
    fence_len: int = 0
    fence_buf: list[str] = []

    for ln in lines:
        if fence_char is None:
            fm = re.match(r"^[ \t]*(`{3,}|~{3,})", ln)
            if fm:
                marker = fm.group(1)
                fence_char = marker[0]
                fence_len = len(marker)
                fence_buf = [ln]
            else:
                out_chunks.append(ln)
        else:
            fence_buf.append(ln)
            cm = re.match(rf"^[ \t]*({re.escape(fence_char)}{{{fence_len},}})[ \t]*\r?\n?$", ln)
            if cm:
                out_chunks.append(_stash_str("".join(fence_buf)))
                fence_char = None
                fence_len = 0
                fence_buf = []

    if fence_buf:
        out_chunks.append(_stash_str("".join(fence_buf)))

    protected = re.sub(
        r"(`+[^`\n]+`+)",
        lambda m: _stash_str(m.group(0)),
        "".join(out_chunks),
    )

    cleaned = re.sub(r"<!--[\s\S]*?-->", "", protected)
    cleaned = _DATA_URI_RE.sub("<data-uri omitted>", cleaned)
    cleaned = re.sub(
        r"\]\((https?://[^\s)]+)\)",
        lambda m: f"]({clean_url(m.group(1))})",
        cleaned,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    # Restore stashed code blocks and inline code spans
    cleaned = re.sub(
        r"\x00MDSLIM_CODE_(\d+)\x00",
        lambda m: stash[int(m.group(1))],
        cleaned,
    )
    return cleaned.strip()


def _iter_markdown_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """
    Return `(zero_based_line_idx, heading_level, heading_title)` for all Markdown
    headings outside ```` ``` ```` and `~~~` fenced code blocks.
    """
    headings: list[tuple[int, int, str]] = []
    fence_marker: str | None = None

    for idx, line in enumerate(lines):
        fm = _MD_FENCE_LINE_RE.match(line)
        if fm:
            marker_str = fm.group(1)
            if fence_marker is None:
                fence_marker = marker_str[0] * len(marker_str)
            elif marker_str[0] == fence_marker[0] and len(marker_str) >= len(fence_marker):
                fence_marker = None
            continue
        if fence_marker is not None:
            continue
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if m:
            headings.append((idx, len(m.group(1)), m.group(2)))
    return headings


def extract_markdown_outline(md_text: str) -> str:
    """
    Return a compact table of contents showing all Markdown headings, line numbers,
    and estimated token count per section.
    """
    lines = md_text.splitlines()
    raw_headings = _iter_markdown_headings(lines)
    headings = [(idx + 1, level, title) for idx, level, title in raw_headings]

    if not headings:
        return f"No headings found (total lines={len(lines)}, ~{estimate_tokens(md_text)} tokens)."

    out = [f"# Document Outline (lines={len(lines)}, ~{estimate_tokens(md_text)} tokens)"]
    for i, (line_no, level, title) in enumerate(headings):
        next_line = headings[i + 1][0] if i + 1 < len(headings) else len(lines) + 1
        section_text = "\n".join(lines[line_no - 1 : next_line - 1])
        sec_tokens = estimate_tokens(section_text)
        indent = "  " * (level - 1)
        out.append(f"{indent}- {'#' * level} {title} (L{line_no}-L{next_line - 1}, ~{sec_tokens} tokens)")
    return "\n".join(out)


def extract_markdown_section(md_text: str, query: str) -> str:
    """
    Extract only the Markdown section(s) whose heading matches `query` (case-insensitive
    substring match), including nested subsections until the next heading of equal or
    higher level, deduplicating overlapping parent/child ranges.
    """
    lines = md_text.splitlines()
    headings = _iter_markdown_headings(lines)

    q_lower = query.lower().strip()
    matched_ranges: list[tuple[int, int]] = []

    for i, (start_idx, level, title) in enumerate(headings):
        if q_lower in title.lower():
            end_idx = len(lines)
            for next_start, next_level, _ in headings[i + 1 :]:
                if next_level <= level:
                    end_idx = next_start
                    break
            # Skip child range if already covered by an enclosing parent section
            if matched_ranges and start_idx < matched_ranges[-1][1]:
                continue
            matched_ranges.append((start_idx, end_idx))

    if not matched_ranges:
        return extract_markdown_outline(md_text)

    sections = ["\n".join(lines[s:e]).strip() for s, e in matched_ranges]
    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# 7. CLI & Test Runner Output Compressor (Error-Window Safe + Safe Subprocess)
# ---------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_PROGRESS_NOISE_RE = re.compile(
    r"^(?:"
    r"frame=\s*\d+.*fps=.*|"
    r"\s*\d+%\|[█▉▊▋▌▍▎▏#\-\s]+\|.*|"
    r"Downloading\s+.*\[\d+/\d+\].*|"
    r"Receiving objects:\s+\d+%.*|"
    r"Resolving deltas:\s+\d+%.*|"
    r"npm WARN deprecated .*|"
    r"PASS\s+\S+.*|"
    r"✓\s+.*\(\d+\s*ms\)\s*$|"
    r"ok\s+\S+\s+\d+\.\d+s\s*$"
    r")",
    re.IGNORECASE,
)

_CRITICAL_LOG_LINE_RE = re.compile(
    r"(?:\b(?:ERROR|FAIL|FAILED|FAILURE|FATAL|PANIC|CRITICAL|EXCEPTION|SEGFAULT|TIMEOUT)\b|"
    r"Traceback \(most recent call last\)|"
    r"^\s*File \".*\", line \d+|"
    r"\b\w*(?:Error|Exception):|"
    r"\bwarning:|"
    r"^E\s+)",
    re.IGNORECASE,
)


def compress_cli_output(
    raw_output: str,
    max_head_lines: int = 40,
    max_tail_lines: int = 40,
    error_context: int = 2,
) -> str:
    """
    Compress noisy CLI / test / build / FFmpeg output while keeping 100% of errors:
    - Strips ANSI color/cursor codes and carriage-return progress overwrites.
    - Counts and collapses repetitive progress/PASS lines (`frame=...`, `PASS ...`)
      into a summary header without misattaching `[repeated xN]` to unrelated lines.
    - Deduplicates consecutive identical lines.
    - When output exceeds `max_head_lines + max_tail_lines`, keeps head + tail AND
      every middle line matching `ERROR`/`FAIL`/`Traceback`/`Exception`/`warning:`
      plus `+/- error_context` surrounding lines so mid-log failures are NEVER lost.
    """
    if not raw_output:
        return ""

    # Split on '\n' first so '\r' carriage-return progress overwrites on the same line stay together
    normalized_lines: list[str] = []
    for raw_ln in raw_output.split("\n"):
        if raw_ln.endswith("\r"):
            raw_ln = raw_ln[:-1]
        if "\r" in raw_ln:
            segments = [seg for seg in raw_ln.split("\r") if seg.strip()]
            raw_ln = segments[-1] if segments else ""
        clean_ln = _ANSI_ESCAPE_RE.sub("", raw_ln).rstrip()
        normalized_lines.append(clean_ln)

    deduped_lines: list[str] = []
    suppressed_noise_count = 0
    prev_line: str | None = None
    repeat_count = 0

    def _flush_repeat() -> None:
        nonlocal repeat_count
        if repeat_count > 0 and deduped_lines:
            deduped_lines[-1] = f"{deduped_lines[-1]} [repeated x{repeat_count + 1}]"
            repeat_count = 0

    for ln in normalized_lines:
        # Never treat a line containing an error/failure marker as progress noise
        if _PROGRESS_NOISE_RE.match(ln.strip()) and not _CRITICAL_LOG_LINE_RE.search(ln):
            _flush_repeat()
            prev_line = None
            suppressed_noise_count += 1
            continue

        if prev_line is not None and ln == prev_line:
            repeat_count += 1
            continue

        _flush_repeat()
        deduped_lines.append(ln)
        prev_line = ln

    _flush_repeat()

    total = len(deduped_lines)
    if total <= max_head_lines + max_tail_lines:
        result_lines = list(deduped_lines)
    else:
        # Mark which line indices must be preserved: head, tail, and error windows
        keep_indices: set[int] = set(range(0, min(max_head_lines, total)))
        keep_indices.update(range(max(0, total - max_tail_lines), total))

        for idx, ln in enumerate(deduped_lines):
            if _CRITICAL_LOG_LINE_RE.search(ln):
                start_w = max(0, idx - error_context)
                end_w = min(total, idx + error_context + 1)
                keep_indices.update(range(start_w, end_w))

        sorted_indices = sorted(keep_indices)
        result_lines = []
        last_idx = -1
        for idx in sorted_indices:
            if last_idx == -1 and idx > 0:
                result_lines.append(f"... [{idx} normal log lines omitted] ...")
            elif last_idx != -1 and idx > last_idx + 1:
                gap = idx - last_idx - 1
                result_lines.append(f"... [{gap} normal log lines omitted] ...")
            result_lines.append(deduped_lines[idx])
            last_idx = idx
        if last_idx != -1 and last_idx < total - 1:
            gap = total - 1 - last_idx
            result_lines.append(f"... [{gap} normal log lines omitted] ...")

    if suppressed_noise_count > 0:
        result_lines.insert(
            0,
            f"[token-saver: collapsed {suppressed_noise_count} repetitive PASS/progress lines]",
        )

    return "\n".join(result_lines).strip()



def run_compressed(
    command: Sequence[str] | str,
    max_head_lines: int = 40,
    max_tail_lines: int = 40,
    timeout: int = 300,
    cwd: str | None = None,
) -> tuple[int, str]:
    """
    Execute a command safely with `shell=False` and return `(exit_code, compressed_output)`:
    - Always executes with `shell=False` to guarantee zero shell-injection surface and
      preserve spaces/metacharacters in arguments verbatim.
    - When `command` is passed as a `str`, splits it into an argument list via `shlex.split`.
    - Sets `stdin=subprocess.DEVNULL` so interactive prompts never hang background bots.
    """
    if isinstance(command, str):
        try:
            cmd_list = shlex.split(command)
        except ValueError as exc:
            return 2, f"[token-saver: command parse error: {exc}]"
    else:
        cmd_list = [str(c) for c in command]
    if not cmd_list:
        return 2, "[token-saver: empty command]"

    max_capture_bytes = 8 * 1024 * 1024  # 8 MB safety ceiling to prevent OOM on runaway child output
    try:
        with subprocess.Popen(
            cmd_list,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
        ) as proc:
            chunks: list[bytes] = []
            captured = 0
            truncated_bytes = 0
            deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
            assert proc.stdout is not None
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raw_out = b"".join(chunks).decode("utf-8", "replace")
                    compressed = compress_cli_output(
                        raw_out,
                        max_head_lines=max_head_lines,
                        max_tail_lines=max_tail_lines,
                    )
                    msg = f"[token-saver: command timed out after {timeout}s]\n{compressed}".strip()
                    return 124, msg
                buf = proc.stdout.read(65536)
                if not buf:
                    break
                if captured < max_capture_bytes:
                    take = buf[: max_capture_bytes - captured]
                    chunks.append(take)
                    captured += len(take)
                    if len(buf) > len(take):
                        truncated_bytes += len(buf) - len(take)
                else:
                    truncated_bytes += len(buf)
            ret = proc.wait(timeout=max(1, int(deadline - time.monotonic())) if deadline else None)
            raw_out = b"".join(chunks).decode("utf-8", "replace")
            if truncated_bytes > 0:
                raw_out += f"\n[token-saver: capped child output after {max_capture_bytes} bytes (+{truncated_bytes} bytes omitted)]\n"
            compressed = compress_cli_output(
                raw_out,
                max_head_lines=max_head_lines,
                max_tail_lines=max_tail_lines,
            )
            return ret, compressed
    except FileNotFoundError:
        return 127, f"[token-saver: command not found: {cmd_list[0]}]"
    except PermissionError:
        return 126, f"[token-saver: permission denied: {cmd_list[0]}]"
    except subprocess.TimeoutExpired:
        return 124, f"[token-saver: command timed out after {timeout}s]"
    except OSError as exc:
        return 126, f"[token-saver: os error executing {cmd_list[0]}: {exc}]"


# ---------------------------------------------------------------------------
# 8. Universal Auto-Slimmer & Batch Slimmer
# ---------------------------------------------------------------------------


def auto_slim(
    text: str,
    filename: str = "",
    max_items: int = 20,
    browser_mode: bool = False,
    outline_only: bool = False,
) -> tuple[str, str]:
    """
    Automatically detect format and apply the safest lossless compressor.
    Returns `(detected_format, slimmed_text)`.
    """
    fmt = detect_format(text, filename=filename)
    if fmt == "html":
        return fmt, html_to_compact_markdown(text, browser_mode=browser_mode)
    if fmt == "aria":
        return fmt, slim_aria_snapshot(text)
    if fmt == "json":
        try:
            parsed = json.loads(text)
            return fmt, slim_json_text(text, max_items=max_items, _parsed_obj=parsed)
        except Exception:
            return "cli", compress_cli_output(text)
    if fmt == "jsonl":
        return fmt, slim_jsonl_text(text, max_records=max_items * 2, max_items=max_items)
    if fmt in {"csv", "tsv"}:
        delim = "\t" if fmt == "tsv" else ","
        return fmt, slim_csv_text(text, delimiter=delim)
    if fmt == "diff":
        return fmt, slim_git_diff(text)
    if fmt == "markdown":
        if outline_only:
            return fmt, extract_markdown_outline(text)
        return fmt, slim_markdown_body(text)
    return "cli", compress_cli_output(text)


def batch_slim_files(
    paths_or_globs: Sequence[str],
    max_items: int = 20,
    browser_mode: bool = False,
    in_place: bool = False,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """
    Slim multiple tool output files (e.g. `/workspace/agent-tools/*.txt`) in a single
    invocation so Grok Bot agents do not pay multi-turn conversation history overhead.
    """
    resolved_files: list[Path] = []
    seen: set[Path] = set()

    for pattern in paths_or_globs:
        p = Path(pattern)
        if p.is_dir():
            candidates = sorted(
                f
                for f in p.iterdir()
                if f.is_file() and not f.name.endswith((".slim", ".slim.txt"))
            )
        elif any(ch in pattern for ch in "*?[]"):
            candidates = [
                Path(m)
                for m in sorted(glob.glob(pattern, recursive=True))
                if Path(m).is_file() and not m.endswith((".slim", ".slim.txt"))
            ]
        elif p.is_file():
            candidates = [p]
        else:
            candidates = []

        for c in candidates:
            rc = c.resolve()
            if rc not in seen:
                seen.add(rc)
                resolved_files.append(c)

    if not resolved_files:
        return {
            "files_processed": 0,
            "tokens_before": 0,
            "tokens_after": 0,
            "tokens_saved": 0,
            "saved_pct": 0.0,
            "error": "No matching files found for the given paths/globs.",
            "items": [],
        }

    out_dir_path = Path(out_dir).resolve() if out_dir else None
    if out_dir_path:
        try:
            out_dir_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {
                "files_processed": 0,
                "files_failed": len(resolved_files),
                "tokens_before": 0,
                "tokens_after": 0,
                "tokens_saved": 0,
                "saved_pct": 0.0,
                "error": f"Failed to create output directory '{out_dir}': {exc}",
                "items": [],
            }

    # Detect duplicate basenames when writing to a shared out_dir
    basename_counts = collections.Counter(f.name for f in resolved_files)
    used_out_paths: set[Path] = set()

    results: list[dict[str, Any]] = []
    total_before = 0
    total_after = 0

    for fpath in resolved_files:
        try:
            raw = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            results.append({"file": str(fpath), "error": str(exc)})
            continue

        before_tok = estimate_tokens(raw)
        fmt, slimmed = auto_slim(
            raw,
            filename=fpath.name,
            max_items=max_items,
            browser_mode=browser_mode,
        )
        after_tok = estimate_tokens(slimmed)

        if in_place:
            out_path = fpath
        elif out_dir_path:
            candidate_name = fpath.name
            if basename_counts[fpath.name] > 1:
                parent_slug = re.sub(r"[^a-zA-Z0-9._-]", "_", fpath.resolve().parent.name or "dir")
                candidate_name = f"{parent_slug}__{fpath.name}"
            out_path = out_dir_path / candidate_name
            # Prevent silent in-place overwrite if --out-dir is the input file's own directory
            if out_path.resolve() == fpath.resolve():
                out_path = out_dir_path / f"{fpath.name}.slim"
            counter = 2
            while out_path.resolve() in used_out_paths:
                out_path = out_dir_path / f"{counter}__{candidate_name}"
                counter += 1
            used_out_paths.add(out_path.resolve())
        else:
            out_path = fpath.with_name(fpath.name + ".slim")

        try:
            out_path.write_text(slimmed, encoding="utf-8")
        except Exception as exc:
            results.append({"file": str(fpath), "error": str(exc)})
            continue

        total_before += before_tok
        total_after += after_tok
        saved_pct = round((1.0 - after_tok / max(1, before_tok)) * 100, 1)
        results.append(
            {
                "file": str(fpath),
                "output_file": str(out_path),
                "format": fmt,
                "tokens_before": before_tok,
                "tokens_after": after_tok,
                "saved_pct": saved_pct,
            }
        )

    overall_pct = round((1.0 - total_after / max(1, total_before)) * 100, 1) if total_before else 0.0
    succeeded_count = sum(1 for item in results if "output_file" in item)
    failed_count = sum(1 for item in results if "error" in item)
    return {
        "files_processed": succeeded_count,
        "files_failed": failed_count,
        "tokens_before": total_before,
        "tokens_after": total_after,
        "tokens_saved": max(0, total_before - total_after),
        "saved_pct": overall_pct,
        "items": results,
    }


# ---------------------------------------------------------------------------
# 9. SQLite (WAL) State Store: CJK N-gram Similarity Cache + Circuit Breaker
# ---------------------------------------------------------------------------


class LocalStateStore:
    """
    Zero-dependency SQLite state store using WAL mode:
    - Exact & CJK N-gram Jaccard similarity cache with `namespace` isolation and TTL.
    - Automatic schema migration for legacy `kv_cache` tables.
    - 3-strike Circuit Breaker to halt infinite retry loops on failing tools/APIs.
    - Cumulative token savings telemetry (`stats` table, 100% local).
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _secure_db_files(self) -> None:
        """Ensure main SQLite DB and any WAL/SHM sidecar files are mode 0600 (owner read/write only)."""
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(f"{self.db_path}{suffix}")
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o600)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        old_umask = os.umask(0o177)
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        finally:
            os.umask(old_umask)
        self._secure_db_files()
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            # 1. Inspect existing kv_cache schema BEFORE running CREATE TABLE / CREATE INDEX
            # so legacy v1 databases (`cache_key, norm_tokens, value, expires_at, created_at, namespace`)
            # are seamlessly migrated without OperationalError!
            kv_info = conn.execute("PRAGMA table_info(kv_cache)").fetchall()
            kv_cols = {row["name"] for row in kv_info}

            if kv_cols and ("key_hash" not in kv_cols or "query_text" not in kv_cols or "response_text" not in kv_cols):
                legacy_rows = [dict(r) for r in conn.execute("SELECT * FROM kv_cache").fetchall()]
                conn.execute("DROP TABLE kv_cache")
                conn.execute(
                    """
                    CREATE TABLE kv_cache (
                        key_hash TEXT PRIMARY KEY,
                        namespace TEXT NOT NULL DEFAULT 'default',
                        query_text TEXT NOT NULL,
                        response_text TEXT NOT NULL,
                        token_estimate INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        hit_count INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                now_ts = time.time()
                for lrow in legacy_rows:
                    raw_ns = str(lrow.get("namespace") or "default").strip().lower() or "default"
                    raw_val = str(
                        lrow.get("response_text")
                        if lrow.get("response_text") is not None
                        else (lrow.get("value") or "")
                    )
                    raw_q = str(lrow.get("query_text") or "")
                    if not raw_q:
                        nt = lrow.get("norm_tokens")
                        if isinstance(nt, str) and nt.strip():
                            try:
                                parsed_nt = json.loads(nt)
                                if isinstance(parsed_nt, list):
                                    raw_q = " ".join(str(x) for x in parsed_nt)
                                else:
                                    raw_q = nt
                            except Exception:
                                raw_q = nt
                    if not raw_q:
                        raw_q = str(lrow.get("key_hash") or lrow.get("cache_key") or "")
                    raw_kh = str(lrow.get("key_hash") or lrow.get("cache_key") or "")
                    if not raw_kh:
                        raw_kh = self._hash_key(raw_q, namespace=raw_ns)
                    c_at = float(lrow.get("created_at") or now_ts)
                    e_at = float(lrow.get("expires_at") or (c_at + 21600.0))
                    t_est = int(lrow.get("token_estimate") or lrow.get("tokens_saved") or estimate_tokens(raw_val))
                    h_cnt = int(lrow.get("hit_count") or lrow.get("hits") or 0)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO kv_cache
                        (key_hash, namespace, query_text, response_text, token_estimate, created_at, expires_at, hit_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (raw_kh, raw_ns, raw_q, raw_val, t_est, c_at, e_at, h_cnt),
                    )
            else:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS kv_cache (
                        key_hash TEXT PRIMARY KEY,
                        namespace TEXT NOT NULL DEFAULT 'default',
                        query_text TEXT NOT NULL,
                        response_text TEXT NOT NULL,
                        token_estimate INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        hit_count INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                if kv_cols and "namespace" not in kv_cols:
                    conn.execute(
                        "ALTER TABLE kv_cache ADD COLUMN namespace TEXT NOT NULL DEFAULT 'default'"
                    )

            conn.execute("CREATE INDEX IF NOT EXISTS idx_kv_expires ON kv_cache(expires_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kv_namespace_expires ON kv_cache(namespace, expires_at)"
            )

            # 2. Create circuit_breaker and migrate legacy v1 `circuit_breakers` (plural) if present
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS circuit_breaker (
                    target_key TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    tripped_until REAL NOT NULL DEFAULT 0
                )
                """
            )
            cb_v1_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(circuit_breakers)").fetchall()
            }
            if cb_v1_cols:
                now_ts = time.time()
                for cb_row in conn.execute("SELECT * FROM circuit_breakers").fetchall():
                    cbd = dict(cb_row)
                    t_key = str(cbd.get("target_key") or cbd.get("endpoint") or "")
                    if not t_key:
                        continue
                    f_cnt = int(cbd.get("failure_count") or cbd.get("failures") or 0)
                    l_err = str(cbd.get("last_error") or "")
                    u_at = float(cbd.get("updated_at") or cbd.get("opened_at") or now_ts)
                    t_until = float(cbd.get("tripped_until") or 0.0)
                    if t_until == 0.0 and cbd.get("opened_at") and cbd.get("cooldown_sec"):
                        opened_at = float(cbd.get("opened_at") or 0.0)
                        if opened_at > 0:
                            t_until = opened_at + float(cbd.get("cooldown_sec") or 300.0)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO circuit_breaker
                        (target_key, failure_count, last_error, updated_at, tripped_until)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (t_key, f_cnt, l_err, u_at, t_until),
                    )
                conn.execute("DROP TABLE circuit_breakers")

            # 3. Create savings_ledger
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS savings_ledger (
                    category TEXT PRIMARY KEY,
                    calls INTEGER NOT NULL DEFAULT 0,
                    tokens_before INTEGER NOT NULL DEFAULT 0,
                    tokens_after INTEGER NOT NULL DEFAULT 0,
                    tokens_saved INTEGER NOT NULL DEFAULT 0
                )
                """
            )

        # Restrict SQLite state DB + WAL/SHM sidecar file permissions to owner read/write (0600)
        self._secure_db_files()

    @staticmethod
    def _hash_key(query_text: str, namespace: str = "default") -> str:
        norm = unicodedata.normalize("NFKC", query_text).strip().lower()
        ns = (namespace or "default").strip().lower()
        return hashlib.sha256(f"{ns}::{norm}".encode("utf-8")).hexdigest()

    def get_cached(
        self,
        query_text: str,
        similarity_threshold: float = 0.72,
        namespace: str = "default",
    ) -> dict[str, Any] | None:
        """
        Look up a cached response within `namespace` by exact key hash or CJK/ASCII
        Jaccard similarity >= `similarity_threshold`.
        """
        now = time.time()
        ns = (namespace or "default").strip().lower()
        k_hash = self._hash_key(query_text, namespace=ns)

        with self._connect() as conn:
            conn.execute("DELETE FROM kv_cache WHERE expires_at < ?", (now,))

            row = conn.execute(
                "SELECT * FROM kv_cache WHERE key_hash = ? AND lower(namespace) = ? AND expires_at >= ?",
                (k_hash, ns, now),
            ).fetchone()
            if row is None:
                # Backward-compatible fallback for legacy v1 hashes (`sha256(f"{ns}:{norm}")` or `sha256(norm)`)
                norm_q = unicodedata.normalize("NFKC", query_text).strip().lower()
                for legacy_candidate in (
                    hashlib.sha256(f"{ns}:{norm_q}".encode("utf-8")).hexdigest(),
                    hashlib.sha256(norm_q.encode("utf-8")).hexdigest(),
                ):
                    row = conn.execute(
                        "SELECT * FROM kv_cache WHERE key_hash = ? AND lower(namespace) = ? AND expires_at >= ?",
                        (legacy_candidate, ns, now),
                    ).fetchone()
                    if row is not None:
                        break

            if row is not None:
                conn.execute(
                    "UPDATE kv_cache SET hit_count = hit_count + 1 WHERE key_hash = ?",
                    (row["key_hash"],),
                )
                tok_saved = int(row["token_estimate"])
                conn.execute(
                    """
                    INSERT INTO savings_ledger (category, calls, tokens_before, tokens_after, tokens_saved)
                    VALUES ('cache_hit', 1, ?, 0, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        calls = calls + 1,
                        tokens_before = tokens_before + excluded.tokens_before,
                        tokens_saved = tokens_saved + excluded.tokens_saved
                    """,
                    (tok_saved, tok_saved),
                )
                return {
                    "match_type": "exact",
                    "similarity": 1.0,
                    "namespace": row["namespace"],
                    "query_text": row["query_text"],
                    "response_text": row["response_text"],
                    "tokens_saved": tok_saved,
                    "hit_count": int(row["hit_count"]) + 1,
                }

            if similarity_threshold < 1.0:
                rows = conn.execute(
                    "SELECT * FROM kv_cache WHERE lower(namespace) = ? AND expires_at >= ? ORDER BY created_at DESC LIMIT 300",
                    (ns, now),
                ).fetchall()
                best_row = None
                best_sim = 0.0
                for cand in rows:
                    sim = jaccard_similarity(query_text, cand["query_text"])
                    if sim >= similarity_threshold and sim > best_sim:
                        best_sim = sim
                        best_row = cand

                if best_row is not None:
                    conn.execute(
                        "UPDATE kv_cache SET hit_count = hit_count + 1 WHERE key_hash = ?",
                        (best_row["key_hash"],),
                    )
                    tok_saved = int(best_row["token_estimate"])
                    conn.execute(
                        """
                        INSERT INTO savings_ledger (category, calls, tokens_before, tokens_after, tokens_saved)
                        VALUES ('cache_similarity_hit', 1, ?, 0, ?)
                        ON CONFLICT(category) DO UPDATE SET
                            calls = calls + 1,
                            tokens_before = tokens_before + excluded.tokens_before,
                            tokens_saved = tokens_saved + excluded.tokens_saved
                        """,
                        (tok_saved, tok_saved),
                    )
                    return {
                        "match_type": "similarity",
                        "similarity": round(best_sim, 3),
                        "namespace": best_row["namespace"],
                        "matched_query": best_row["query_text"],
                        "response_text": best_row["response_text"],
                        "tokens_saved": tok_saved,
                        "hit_count": int(best_row["hit_count"]) + 1,
                    }
        return None

    def set_cached(
        self,
        query_text: str,
        response_text: str,
        ttl_seconds: int = 21600,
        namespace: str = "default",
    ) -> dict[str, Any]:
        """Store a query/response pair in the SQLite cache under `namespace`."""
        now = time.time()
        ns = (namespace or "default").strip().lower()
        k_hash = self._hash_key(query_text, namespace=ns)
        tok_est = estimate_tokens(response_text)
        expires = now + max(1, ttl_seconds)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kv_cache (key_hash, namespace, query_text, response_text, token_estimate, created_at, expires_at, hit_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(key_hash) DO UPDATE SET
                    namespace = excluded.namespace,
                    query_text = excluded.query_text,
                    response_text = excluded.response_text,
                    token_estimate = excluded.token_estimate,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (k_hash, ns, query_text.strip(), response_text, tok_est, now, expires),
            )
        return {
            "status": "stored",
            "key_hash": k_hash,
            "namespace": ns,
            "token_estimate": tok_est,
            "ttl_seconds": ttl_seconds,
        }

    def check_circuit(self, target_key: str) -> dict[str, Any]:
        """Check whether `target_key` is currently tripped (open) or allowed (closed)."""
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM circuit_breaker WHERE target_key = ?",
                (target_key,),
            ).fetchone()
            if row is None:
                return {"target": target_key, "state": "closed", "allowed": True, "failure_count": 0}

            tripped_until = float(row["tripped_until"])
            if tripped_until > now:
                remaining = int(tripped_until - now)
                return {
                    "target": target_key,
                    "state": "open",
                    "allowed": False,
                    "failure_count": int(row["failure_count"]),
                    "last_error": row["last_error"],
                    "cooldown_remaining_sec": remaining,
                }
            return {
                "target": target_key,
                "state": "half_open" if int(row["failure_count"]) > 0 else "closed",
                "allowed": True,
                "failure_count": int(row["failure_count"]),
                "last_error": row["last_error"],
            }

    def record_failure(
        self,
        target_key: str,
        error_msg: str,
        max_failures: int = 3,
        cooldown_seconds: int = 900,
    ) -> dict[str, Any]:
        """Record a tool/API failure and trip the circuit breaker if `>= max_failures`."""
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT failure_count FROM circuit_breaker WHERE target_key = ?",
                (target_key,),
            ).fetchone()
            new_count = (int(row["failure_count"]) if row else 0) + 1
            tripped_until = (now + cooldown_seconds) if new_count >= max_failures else 0.0

            conn.execute(
                """
                INSERT INTO circuit_breaker (target_key, failure_count, last_error, updated_at, tripped_until)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target_key) DO UPDATE SET
                    failure_count = excluded.failure_count,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at,
                    tripped_until = excluded.tripped_until
                """,
                (target_key, new_count, error_msg[:500], now, tripped_until),
            )
        return {
            "target": target_key,
            "state": "open" if tripped_until > now else "closed",
            "allowed": tripped_until <= now,
            "failure_count": new_count,
            "tripped_until": int(tripped_until) if tripped_until else 0,
        }

    def record_success(self, target_key: str) -> dict[str, Any]:
        """Reset failure counter for `target_key` upon successful execution."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO circuit_breaker (target_key, failure_count, last_error, updated_at, tripped_until)
                VALUES (?, 0, '', ?, 0)
                ON CONFLICT(target_key) DO UPDATE SET
                    failure_count = 0,
                    last_error = '',
                    updated_at = excluded.updated_at,
                    tripped_until = 0
                """,
                (target_key, now),
            )
        return {"target": target_key, "state": "closed", "allowed": True, "failure_count": 0}

    def record_saving(self, category: str, tokens_before: int, tokens_after: int) -> None:
        saved = max(0, tokens_before - tokens_after)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO savings_ledger (category, calls, tokens_before, tokens_after, tokens_saved)
                VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(category) DO UPDATE SET
                    calls = calls + 1,
                    tokens_before = tokens_before + excluded.tokens_before,
                    tokens_after = tokens_after + excluded.tokens_after,
                    tokens_saved = tokens_saved + excluded.tokens_saved
                """,
                (category, tokens_before, tokens_after, saved),
            )

    def get_stats(self) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            ledger_rows = [dict(r) for r in conn.execute("SELECT * FROM savings_ledger ORDER BY tokens_saved DESC").fetchall()]
            cache_count = conn.execute("SELECT COUNT(*) AS c FROM kv_cache WHERE expires_at >= ?", (now,)).fetchone()["c"]
            open_circuits = [
                dict(r)
                for r in conn.execute(
                    "SELECT target_key, failure_count, last_error, tripped_until FROM circuit_breaker WHERE tripped_until > ?",
                    (now,),
                ).fetchall()
            ]
        total_before = sum(r["tokens_before"] for r in ledger_rows)
        total_after = sum(r["tokens_after"] for r in ledger_rows)
        total_saved = sum(r["tokens_saved"] for r in ledger_rows)
        overall_pct = round((total_saved / total_before * 100.0), 1) if total_before > 0 else 0.0
        return {
            "total_tokens_before": total_before,
            "total_tokens_after": total_after,
            "total_tokens_saved": total_saved,
            "overall_reduction_pct": overall_pct,
            "active_cache_entries": cache_count,
            "open_circuits": open_circuits,
            "by_category": ledger_rows,
        }


# ---------------------------------------------------------------------------
# Self-Test & Real Assertion Suite (`selftest`)
# ---------------------------------------------------------------------------


def _read_input_source(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    p = Path(source)
    return p.read_text(encoding="utf-8", errors="replace")


def _check(condition: bool, message: str) -> None:
    """Explicit verification check that works even when Python runs with `-O` (`PYTHONOPTIMIZE`)."""
    if not condition:
        raise RuntimeError(f"Self-test assertion failed: {message}")


def run_selftest() -> dict[str, Any]:
    """
    Run deterministic benchmarks and strict assertions in an isolated temporary
    directory so production SQLite state is never polluted.
    """
    with tempfile.TemporaryDirectory(prefix="token_saver_selftest_") as tmpdir:
        tmp_db = Path(tmpdir) / "selftest.sqlite3"
        store = LocalStateStore(db_path=tmp_db)

        # 0. Verify SQLite file permissions (0600) and legacy v1 -> v2 schema migration
        if os.name == "posix":
            mode_bits = tmp_db.stat().st_mode & 0o777
            _check(mode_bits == 0o600, f"Expected SQLite DB mode 0o600, got {oct(mode_bits)}")

        v1_db = Path(tmpdir) / "legacy_v1.sqlite3"
        with sqlite3.connect(str(v1_db)) as v1_conn:
            v1_conn.executescript(
                """
                CREATE TABLE kv_cache (
                    cache_key TEXT PRIMARY KEY,
                    norm_tokens TEXT NOT NULL,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    namespace TEXT NOT NULL DEFAULT 'default'
                );
                CREATE TABLE circuit_breakers (
                    endpoint TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL DEFAULT 0,
                    opened_at REAL NOT NULL DEFAULT 0.0,
                    threshold INTEGER NOT NULL DEFAULT 3,
                    cooldown_sec REAL NOT NULL DEFAULT 300.0
                );
                CREATE TABLE savings_ledger (
                    category TEXT PRIMARY KEY,
                    calls INTEGER NOT NULL DEFAULT 0,
                    tokens_before INTEGER NOT NULL DEFAULT 0,
                    tokens_after INTEGER NOT NULL DEFAULT 0,
                    tokens_saved INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            now_v1 = time.time()
            v1_conn.execute(
                "INSERT INTO kv_cache VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "9225f1fa4734867e6ad7055f5ebadfd113b03ceb321efeda70faf4f728e323ef",
                    '["louis", "monogram", "sedori", "tote", "vuitton"]',
                    "P=32000, B=13732, 12 sold items",
                    now_v1 + 7200.0,
                    now_v1,
                    "default",
                ),
            )
            v1_conn.execute(
                "INSERT INTO circuit_breakers VALUES (?, ?, ?, ?, ?)",
                ("flaky_v1_ep", 3, now_v1, 3, 300.0),
            )
            v1_conn.execute(
                "INSERT INTO savings_ledger VALUES ('html2md', 8, 113309, 20627, 92682)"
            )

        migrated_store = LocalStateStore(db_path=v1_db)
        mig_stats = migrated_store.get_stats()
        _check(mig_stats["active_cache_entries"] == 1, "v1 -> v2 kv_cache migration lost active entry")
        _check(mig_stats["total_tokens_saved"] == 92682, "v1 -> v2 savings_ledger migration lost stats")
        mig_hit = migrated_store.get_cached("louis vuitton monogram tote sedori", similarity_threshold=0.6, namespace="default")
        _check(mig_hit is not None and "P=32000" in mig_hit["response_text"], "v1 -> v2 migrated cache lookup failed")
        mig_cb = migrated_store.check_circuit("flaky_v1_ep")
        _check(mig_cb["state"] == "open" and mig_cb["allowed"] is False, "v1 -> v2 circuit_breakers migration failed")

        # 1. Browser-Use / SPA HTML -> Markdown test (Forms, Inputs, Buttons, SVG <title>, Next.js __NEXT_DATA__, Nuxt state)
        next_data_payload = {
            "props": {
                "pageProps": {
                    "item": {
                        "id": "m987654321",
                        "name": "Louis Vuitton Epi Alma Handbag Black",
                        "price": 24800,
                        "status": "on_sale",
                        "seller": "verified_store_jp",
                        "profile_image_url_https": "https://cdn.example.com/avatar.png",
                        "thumbnail_data": "data:image/png;base64," + ("A" * 1200),
                    }
                }
            },
            "page": "/item/[id]",
        }
        noisy_html = (
            "<!DOCTYPE html><html><head><title>Mercari &amp; Yahoo Auction Listing</title>"
            "<style>" + ("body { color: #333; margin: 0; padding: 0; }\n" * 400) + "</style>"
            "<script>" + ("console.log('analytics tracker init');\n" * 400) + "</script>"
            '<script>window.__INITIAL_STATE__ = {"note": "has }; semicolon in string", "stock": 3};</script>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data_payload)}</script>'
            "</head><body>"
            "<svg><title>SVG Icon Title Must Not Overwrite Page Title</title></svg>"
            "<header><nav><a href='/home?utm_source=x&fbclid=123'>Home</a></nav></header>"
            "<main>"
            "<h1><span>Louis Vuitton</span><span>Epi Alma</span></h1>"
            "<p>Condition: Minor corner wear, clean interior.</p>"
            '<img src="data:image/png;base64,' + ("B" * 2000) + '" alt="Front view of Epi Alma bag" />'
            '<form action="/cart/add?utm_medium=web&gclid=999" method="POST" id="buy-form">'
            '<input type="hidden" name="csrf_token" value="secret_csrf_999999" />'
            '<input type="hidden" name="accessToken" value=("eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c") />'
            '<input type="text" name="bid_price" value="24800" placeholder="Enter bid" />'
            '<input type="password" name="account_password" value="hunter2" />'
            '<input type="image" alt="Quick Buy Button" src="/btn.png" />'
            '<select name="shipping_method"><option value="yamato" selected>Yamato 850 JPY</option><option value="yupack">YuPack 1050 JPY</option></select>'
            '<button type="submit" aria-label="Place Bid Now">Place Bid</button>'
            "</form>"
            "<pre><code>def calc_limit(p):\n    return int(0.5 * p - 1050)\n</code></pre>"
            "<table><tr><th>Metric</th><th>Value</th></tr><tr><td>Price</td><td>24,800 JPY</td></tr></table>"
            "</main></body></html>"
        )
        md_out = html_to_compact_markdown(noisy_html)
        _check("# Mercari & Yahoo Auction Listing" in md_out, "Page <title> overwritten by <svg><title>")
        _check("SVG Icon Title" not in md_out, "<svg><title> leaked into output")
        _check("[Form: method=POST action=/cart/add" in md_out, "Form tag or cleaned action URL missing")
        _check("gclid=" not in md_out and "utm_medium=" not in md_out, "Tracking query params not stripped")
        _check("[Input: type=text name=bid_price" in md_out and 'value="24800"' in md_out, "Input value missing")
        _check("value=<redacted>" in md_out and "hunter2" not in md_out, "Password input not redacted")
        _check("secret_csrf_999999" not in md_out and "eyJhbGci" not in md_out, "CSRF/JWT hidden input not redacted")
        _check("[Button: Quick Buy Button]" in md_out, "input type=image alt text missing")
        _check("[Select: shipping_method options=[*Yamato 850 JPY, YuPack 1050 JPY]]" in md_out, "Select options missing")
        _check("[Button: Place Bid]" in md_out, "Submit button missing")
        _check("[Image: Front view of Epi Alma bag]" in md_out, "Image alt text missing")
        _check("Louis Vuitton Epi Alma" in md_out, "Adjacent inline span spacing broken")
        _check("    return int(0.5 * p - 1050)" in md_out, "<pre><code> indentation stripped")
        _check("### [Structured Data]" in md_out and "m987654321" in md_out and "24800" in md_out, "__NEXT_DATA__ lost")
        _check("has }; semicolon in string" in md_out, "window.__INITIAL_STATE__ raw_decode failed on }; inside string")
        _check("<data-uri omitted" in md_out, "Data URI in __NEXT_DATA__ not scrubbed")

        # Verify --browser mode keeps header/nav links
        md_browser = html_to_compact_markdown(noisy_html, browser_mode=True)
        _check("[Home](/home)" in md_browser, "--browser mode failed to preserve <header><nav> link")

        html_before = estimate_tokens(noisy_html)
        html_after = estimate_tokens(md_out)
        store.record_saving("html2md", html_before, html_after)

        # 2. Playwright / Browser-Use ARIA Snapshot Slimmer test (300+ lines, 100% ref + sibling hierarchy preservation)
        aria_lines = [
            "- document [ref=s1e1]:",
            '  - text: "Sibling Header Label"',
            "  - generic:",
            '    - button "Sibling Button After Generic" [ref=s1e2]',
        ]
        expected_refs = ["[ref=s1e1]", "[ref=s1e2]"]
        for i in range(1, 85):
            aria_lines.append("  - generic:")
            aria_lines.append("    - group:")
            aria_lines.append("      - presentation:")
            if i in (1, 25, 42, 60, 84):
                ref_id = f"[ref=s1e{i + 10}]"
                expected_refs.append(ref_id)
                aria_lines.append(f'        - button "Action Item {i}" {ref_id}')
            else:
                aria_lines.append('        - text: ""')
        raw_aria = "\n".join(aria_lines)
        _check(detect_format(raw_aria) == "aria", "ARIA format auto-detection failed")
        slimmed_aria = slim_aria_snapshot(raw_aria)
        for ref_marker in expected_refs:
            _check(ref_marker in slimmed_aria, f"ARIA slimmer dropped interactive ref {ref_marker}")
        _check("- generic:" not in slimmed_aria, "ARIA slimmer failed to collapse empty generic wrapper")
        # Verify sibling button did NOT become a child of leaf `- text: "Sibling Header Label"`
        slim_aria_lines = slimmed_aria.splitlines()
        txt_line = next(ln for ln in slim_aria_lines if "Sibling Header Label" in ln)
        btn_line = next(ln for ln in slim_aria_lines if "Sibling Button After Generic" in ln)
        _check(
            (len(txt_line) - len(txt_line.lstrip(" "))) == (len(btn_line) - len(btn_line.lstrip(" "))),
            f"ARIA sibling button falsely indented under preceding text leaf: {txt_line!r} vs {btn_line!r}",
        )
        aria_before = estimate_tokens(raw_aria)
        aria_after = estimate_tokens(slimmed_aria)
        store.record_saving("aria-slim", aria_before, aria_after)

        # 3. JSON Slimmer test (Hex & Base64 scrubbing, camelCase & Stripe/Google/Slack secret redaction, array truncation)
        raw_json_obj = {
            "items": [
                {
                    "id": f"tweet_{i}",
                    "text": f"Sample tweet content #{i} about AI agents and token reduction.",
                    "metrics": {"likes": i * 10, "retweets": i * 2, "tokens_saved": 120},
                    "profile_image_url_https": "https://pbs.twimg.com/profile_images/123456/avatar.jpg",
                    "default_profile": True,
                    "empty_list": [],
                    "null_field": None,
                    "raw_screenshot": "data:image/jpeg;base64," + ("Z" * 1500),
                    "raw_b64_blob": "Q" * 800,
                    "raw_hex_blob": "a1b2c3d4" * 40,
                    "api_key": ("sk-" + "live-super-secret-key"),
                    "accessToken": "camel-case-secret-token-value",
                    "stripe_val": ("rk_" + "live_" + "1234567890abcdefghijklmnop"),
                }
                for i in range(80)
            ]
        }
        raw_json_str = json.dumps(raw_json_obj, indent=2)
        slimmed_json_str = slim_json_text(raw_json_str, max_items=5)
        _check("<data-uri omitted" in slimmed_json_str, "JSON data URI not scrubbed before max_str_len")
        _check("<base64 omitted" in slimmed_json_str, "JSON base64 blob not scrubbed")
        _check("<hex-blob omitted" in slimmed_json_str, "JSON hex blob falsely classified as base64")
        _check(("sk-" + "live-super-secret-key") not in slimmed_json_str and "<redacted>" in slimmed_json_str, "JSON secret not redacted")
        _check("camel-case-secret-token-value" not in slimmed_json_str, "camelCase accessToken not redacted")
        _check(("rk_" + "live_" + "1234567890abcdefghijklmnop") not in slimmed_json_str, "Stripe rk_live_ key not redacted")
        _check('"tokens_saved":120' in slimmed_json_str, "Safe counter key tokens_saved was falsely redacted")
        json_before = estimate_tokens(raw_json_str)
        json_after = estimate_tokens(slimmed_json_str)
        store.record_saving("json-slim", json_before, json_after)

        # 4. CSV Slimmer test (Streaming + exact Decimal sums & sub-cent precision)
        csv_rows = ["id,category,price,fee_rate"]
        for i in range(1, 501):
            csv_rows.append(f"{i},bag_{(i % 5) + 1},{10000 + i * 50},0.00045")
        raw_csv_str = "\n".join(csv_rows)
        slimmed_csv_str = slim_csv_text(raw_csv_str)
        _check("rows=500" in slimmed_csv_str and "mean=" in slimmed_csv_str, "CSV stats missing")
        _check("min=0.00045" in slimmed_csv_str, "CSV sub-cent Decimal precision (0.00045) rounded to 0")
        csv_before = estimate_tokens(raw_csv_str)
        csv_after = estimate_tokens(slimmed_csv_str)
        store.record_saving("csv-slim", csv_before, csv_after)

        # 5. Git Diff Slimmer test (Lockfile collapse + long hunk changed-line preservation)
        lock_diff = ["diff --git a/package-lock.json b/package-lock.json", "--- a/package-lock.json", "+++ b/package-lock.json", "@@ -1,500 +1,500 @@"]
        lock_diff.extend([f'+    "integrity": "sha512-{"a" * 64}-{i}",' for i in range(400)])
        code_diff = [
            "diff --git a/src/app.py b/src/app.py",
            "--- a/src/app.py",
            "+++ b/src/app.py",
            "@@ -10,120 +10,122 @@ def main():",
        ]
        code_diff.extend([f"     unchanged_ctx_{i} = {i}" for i in range(100)])
        code_diff.append("+    critical_new_line = True")
        code_diff.append("+    return critical_new_line")
        raw_diff_str = "\n".join(lock_diff + code_diff)
        slimmed_diff_str = slim_git_diff(raw_diff_str, max_hunk_lines=40)
        _check("collapsed generated/lockfile diff" in slimmed_diff_str, "Lockfile diff not collapsed")
        _check("+    return critical_new_line" in slimmed_diff_str, "Changed + line after 100 context lines was dropped!")
        diff_before = estimate_tokens(raw_diff_str)
        diff_after = estimate_tokens(slimmed_diff_str)
        store.record_saving("diff-slim", diff_before, diff_after)

        # 6. Markdown Safe Body (code fence preservation) vs Outline test
        md_sections = [
            "# Complete Guide",
            "```html\n<!-- code comment inside fence must stay -->\n[link](https://example.com?utm_source=keep)\n```",
        ]
        for s_idx in range(1, 15):
            md_sections.append(f"\n## Section {s_idx}: Topic Overview\n")
            md_sections.append(("Detailed paragraph content explaining the architecture and implementation steps. " * 25) + "\n")
        raw_md_str = "\n".join(md_sections)
        fmt_md, auto_md = auto_slim(raw_md_str, filename="guide.md")
        _check(fmt_md == "markdown", "Markdown format detection failed")
        _check("Detailed paragraph content explaining" in auto_md, "auto_slim destroyed Markdown body text!")
        _check("<!-- code comment inside fence must stay -->" in auto_md, "slim_markdown_body stripped comment inside code fence!")
        _check("utm_source=keep" in auto_md, "slim_markdown_body modified URL inside code fence!")
        outline_str = extract_markdown_outline(raw_md_str)
        sec_str = extract_markdown_section(raw_md_str, "Section 3")
        md_before = estimate_tokens(raw_md_str)
        md_after = estimate_tokens(outline_str + "\n\n" + sec_str)
        store.record_saving("md-outline", md_before, md_after)

        # 7. CLI Log Compressor test (Middle-of-log ERROR preservation + [repeated] counter fix)
        cli_lines = [f"init module {i}" for i in range(50)]
        cli_lines.extend([f"PASS test/suite_{i}.spec.ts (12 ms)" for i in range(120)])
        cli_lines.extend([f"normal step {i}" for i in range(60)])
        cli_lines.append("Context line before failure")
        cli_lines.append("ERROR: Database connection refused on port 5432")
        cli_lines.append("Traceback (most recent call last):")
        cli_lines.append('  File "/app/db.py", line 42, in connect')
        cli_lines.append("ConnectionRefusedError: [Errno 111] Connection refused")
        cli_lines.extend([f"cleanup step {i}" for i in range(60)])
        raw_cli_str = "\n".join(cli_lines)
        slimmed_cli_str = compress_cli_output(raw_cli_str, max_head_lines=20, max_tail_lines=20)
        _check("ERROR: Database connection refused on port 5432" in slimmed_cli_str, "Middle-of-log ERROR was dropped!")
        _check("ConnectionRefusedError: [Errno 111] Connection refused" in slimmed_cli_str, "Middle-of-log Traceback was dropped!")
        _check("init module 49 [repeated" not in slimmed_cli_str, "Suppressed PASS lines falsely attached [repeated] to preceding line")
        cli_before = estimate_tokens(raw_cli_str)
        cli_after = estimate_tokens(slimmed_cli_str)
        store.record_saving("cli-run", cli_before, cli_after)

        # 8. Japanese / CJK Similarity Cache + Namespace Isolation + Circuit Breaker test
        jp_q1 = "東京から大阪までの新幹線の料金を教えて"
        jp_q2 = "東京から大阪の新幹線料金を教えてください"
        jp_sim = jaccard_similarity(jp_q1, jp_q2)
        _check(jp_sim >= 0.55, f"Japanese CJK N-gram similarity too low: {jp_sim}")

        store.set_cached(jp_q1, "指定席は約14,720円です。", namespace="travel_bot")
        hit_jp = store.get_cached(jp_q2, similarity_threshold=0.55, namespace="travel_bot")
        _check(hit_jp is not None and hit_jp["match_type"] in {"exact", "similarity"}, "Japanese similarity cache missed!")
        miss_other_ns = store.get_cached(jp_q2, similarity_threshold=0.55, namespace="other_bot")
        _check(miss_other_ns is None, "Cache namespace isolation failed!")

        # Circuit breaker 3-strike test
        for _ in range(3):
            store.record_failure("flaky_endpoint", "HTTP 502 Bad Gateway", max_failures=3, cooldown_seconds=60)
        cb_state = store.check_circuit("flaky_endpoint")
        _check(cb_state["state"] == "open" and cb_state["allowed"] is False, "Circuit breaker failed to trip on 3rd failure")
        store.record_success("flaky_endpoint")
        _check(store.check_circuit("flaky_endpoint")["allowed"] is True, "Circuit breaker failed to reset on success")

        # 9. Batch Slimmer test (including duplicate basename disambiguation)
        batch_dir = Path(tmpdir) / "agent-tools"
        sub_a = batch_dir / "dir1"
        sub_b = batch_dir / "dir2"
        out_batch = Path(tmpdir) / "batch-out"
        sub_a.mkdir(parents=True, exist_ok=True)
        sub_b.mkdir(parents=True, exist_ok=True)
        (sub_a / "dup.json").write_text(raw_json_str, encoding="utf-8")
        (sub_b / "dup.json").write_text(raw_json_str, encoding="utf-8")
        (batch_dir / "out2.html").write_text(noisy_html, encoding="utf-8")
        batch_summary = batch_slim_files(
            [str(sub_a / "dup.json"), str(sub_b / "dup.json"), str(batch_dir / "out2.html")],
            max_items=5,
            out_dir=str(out_batch),
        )
        _check(batch_summary["files_processed"] == 3 and batch_summary["saved_pct"] > 80.0, "batch-slim failed")
        out_files = {p.name for p in out_batch.iterdir()}
        _check(len(out_files) == 3, f"batch-slim collided on duplicate basenames: {out_files}")
        _check(_is_sensitive_key("bearer_token") and _is_sensitive_key("jwt") and _is_sensitive_key("jwt_token"), "bearer_token/jwt/jwt_token must be sensitive")
        huge_csv = "x\n" + ("9" * 40) + ".5\n"
        _check("rows=1" in slim_csv_text(huge_csv), "huge Decimal CSV failed")
        for suf in ("", "-wal", "-shm"):
            db_cand = Path(f"{store.db_path}{suf}")
            if db_cand.exists():
                _check((db_cand.stat().st_mode & 0o777) == 0o600, f"{db_cand.name} mode is not 0600")

        benchmarks = {}
        for name, b, a in [
            ("html2md (Browser/SPA + __NEXT_DATA__)", html_before, html_after),
            ("aria-slim (100% refs preserved)", aria_before, aria_after),
            ("json-slim (Base64 + secret scrub)", json_before, json_after),
            ("csv-slim (500 rows -> stats + sample)", csv_before, csv_after),
            ("diff-slim (lockfile + code diff)", diff_before, diff_after),
            ("md-outline + section extract", md_before, md_after),
            ("cli-run (mid-log ERROR preserved)", cli_before, cli_after),
        ]:
            pct = round((1.0 - (a / max(1, b))) * 100, 1)
            benchmarks[name] = {
                "tokens_before": b,
                "tokens_after": a,
                "tokens_saved": b - a,
                "reduction_pct": f"{pct}%",
            }

        return {
            "status": "all_assertions_passed",
            "version": __version__,
            "japanese_cjk_similarity": round(jp_sim, 3),
            "benchmarks": benchmarks,
            "batch_slim_test": {
                "files_processed": batch_summary["files_processed"],
                "saved_pct": f"{batch_summary['saved_pct']}%",
            },
            "cumulative_stats": store.get_stats(),
        }


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token-saver",
        description="Zero-dependency local token reduction toolkit for AI agents & Browser Use (v2.0.0).",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Path to SQLite state database (default: %(default)s)",
    )
    parser.add_argument(
        "--no-stats",
        action="store_true",
        help="Do not open or write cumulative token savings to the SQLite database (pure stateless filter mode)",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # 1. slim (universal auto-detect)
    p_slim = sub.add_parser("slim", help="Auto-detect format and compress input losslessly")
    p_slim.add_argument("source", nargs="?", default="-", help="File path or '-' for stdin")
    p_slim.add_argument("--max-items", type=int, default=20, help="Max array/record items to keep")
    p_slim.add_argument("--browser", action="store_true", help="Keep <header>/<nav> links & forms for Browser Use")
    p_slim.add_argument("--outline", action="store_true", help="For Markdown input, emit heading outline only")

    # 2. html2md
    p_html = sub.add_parser("html2md", help="Convert HTML to Browser-Use/SPA-safe compact Markdown")
    p_html.add_argument("source", nargs="?", default="-", help="HTML file path or '-' for stdin")
    p_html.add_argument("--no-links", action="store_true", help="Strip hyperlinks and keep anchor text only")
    p_html.add_argument("--browser", action="store_true", help="Keep <header>/<nav> elements for Browser Use")
    p_html.add_argument("--base-url", default="", help="Base URL for resolving relative links")
    p_html.add_argument("--max-chars", type=int, default=0, help="Optional max output characters")

    # 3. aria-slim
    p_aria = sub.add_parser("aria-slim", help="Compress Playwright / Browser-Use ARIA accessibility snapshot")
    p_aria.add_argument("source", nargs="?", default="-", help="ARIA snapshot file path or '-' for stdin")
    p_aria.add_argument("--max-text-len", type=int, default=240, help="Max length for static text nodes")

    # 4. json-slim
    p_json = sub.add_parser("json-slim", help="Slim JSON payload (scrub base64, redact secrets, cap arrays)")
    p_json.add_argument("source", nargs="?", default="-", help="JSON file path or '-' for stdin")
    p_json.add_argument("--max-items", type=int, default=20, help="Max list items per array")
    p_json.add_argument("--max-str-len", type=int, default=600, help="Max characters per string value")
    p_json.add_argument("--max-depth", type=int, default=8, help="Max nesting depth")
    p_json.add_argument("--pretty", action="store_true", help="Indent output with 2 spaces")

    # 5. jsonl-slim
    p_jsonl = sub.add_parser("jsonl-slim", help="Stream-compress JSONL / NDJSON logs")
    p_jsonl.add_argument("source", nargs="?", default="-", help="JSONL file path or '-' for stdin")
    p_jsonl.add_argument("--max-records", type=int, default=30, help="Max records to keep (head + tail)")
    p_jsonl.add_argument("--max-items", type=int, default=10, help="Max list items per record")

    # 6. csv-slim
    p_csv = sub.add_parser("csv-slim", help="Stream-compress CSV/TSV into Decimal stats + head/tail rows")
    p_csv.add_argument("source", nargs="?", default="-", help="CSV/TSV file path or '-' for stdin")
    p_csv.add_argument("--head", type=int, default=8, help="Number of leading rows to keep")
    p_csv.add_argument("--tail", type=int, default=5, help="Number of trailing rows to keep")

    # 7. diff-slim
    p_diff = sub.add_parser("diff-slim", help="Compress git diff output (collapse lockfiles & huge hunks)")
    p_diff.add_argument("source", nargs="?", default="-", help="Diff file path or '-' for stdin")
    p_diff.add_argument("--max-hunk-lines", type=int, default=80, help="Max lines per diff hunk")

    # 8. md-outline
    p_md = sub.add_parser("md-outline", help="Extract Markdown heading outline or specific section")
    p_md.add_argument("source", nargs="?", default="-", help="Markdown file path or '-' for stdin")
    p_md.add_argument("--section", default="", help="Heading substring to extract in full")

    # 9. log-slim
    p_log = sub.add_parser("log-slim", help="Compress raw CLI/test/build log text while preserving all errors")
    p_log.add_argument("source", nargs="?", default="-", help="Log file path or '-' for stdin")
    p_log.add_argument("--head", type=int, default=40, help="Max leading lines to keep")
    p_log.add_argument("--tail", type=int, default=40, help="Max trailing lines to keep")

    # 10. run
    p_run = sub.add_parser("run", help="Run a command safely and compress its stdout/stderr")
    p_run.add_argument("--head", type=int, default=40, help="Max leading lines to keep")
    p_run.add_argument("--tail", type=int, default=40, help="Max trailing lines to keep")
    p_run.add_argument("--timeout", type=int, default=300, help="Command timeout in seconds")
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="Command and arguments to execute")

    # 11. batch-slim
    p_batch = sub.add_parser("batch-slim", help="Slim multiple files or directories in one call")
    p_batch.add_argument("paths", nargs="+", help="Files, directories, or glob patterns")
    p_batch.add_argument("--max-items", type=int, default=20, help="Max array/record items to keep")
    p_batch.add_argument("--browser", action="store_true", help="Enable Browser Use mode for HTML files")
    p_batch.add_argument("--in-place", action="store_true", help="Overwrite input files in-place")
    p_batch.add_argument("--out-dir", default="", help="Optional output directory for slimmed files")

    # 12. cache
    p_cache = sub.add_parser("cache", help="Query or update SQLite similarity cache & circuit breaker")
    p_cache.add_argument(
        "action",
        choices=["get", "set", "put", "check-circuit", "guard", "record-failure", "record-success", "record"],
        help="Cache or circuit breaker action",
    )
    p_cache.add_argument("arg1", nargs="?", default="", help="Namespace/Target/Query depending on action")
    p_cache.add_argument("arg2", nargs="?", default="", help="Key/Value/Status depending on action")
    p_cache.add_argument("arg3", nargs="?", default="", help="Optional Value when namespace is positional")
    p_cache.add_argument("--key", default="", help="Query key or circuit target name")
    p_cache.add_argument("--value", default="", help="Response text or error message")
    p_cache.add_argument("--namespace", default="default", help="Cache namespace (default: 'default')")
    p_cache.add_argument("--ttl", type=int, default=21600, help="Cache TTL in seconds (default: 6h)")
    p_cache.add_argument("--similarity", type=float, default=0.72, help="Jaccard similarity threshold (0.0-1.0)")

    # 13. stats & selftest
    sub.add_parser("stats", help="Show cumulative token savings and active cache/circuit stats")
    sub.add_parser("selftest", help="Run built-in benchmark and assertion suite")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _store_instance: LocalStateStore | None = None

    def _get_store() -> LocalStateStore:
        nonlocal _store_instance
        if _store_instance is None:
            _store_instance = LocalStateStore(db_path=Path(args.db))
        return _store_instance

    def _record_saving_safe(category: str, before_tok: int, after_tok: int) -> None:
        if args.no_stats:
            return
        try:
            _get_store().record_saving(category, before_tok, after_tok)
        except Exception:
            # Never fail a stateless filter command if the filesystem is read-only
            pass

    if args.subcommand == "slim":
        if args.source != "-" and Path(args.source).is_file() and Path(args.source).suffix.lower() in {".csv", ".tsv"}:
            p_src = Path(args.source)
            delim = "\t" if p_src.suffix.lower() == ".tsv" else ","
            out = slim_csv_text(p_src, delimiter=delim)
            est_in = max(estimate_tokens(out), p_src.stat().st_size // 4)
            _record_saving_safe(f"slim:{p_src.suffix.lower().lstrip('.')}", est_in, estimate_tokens(out))
            print(out)
            return 0
        raw = _read_input_source(args.source)
        fmt, out = auto_slim(
            raw,
            filename="" if args.source == "-" else args.source,
            max_items=args.max_items,
            browser_mode=args.browser,
            outline_only=args.outline,
        )
        _record_saving_safe(f"slim:{fmt}", estimate_tokens(raw), estimate_tokens(out))
        print(out)
        return 0

    if args.subcommand == "html2md":
        raw = _read_input_source(args.source)
        out = html_to_compact_markdown(
            raw,
            keep_links=not args.no_links,
            max_chars=args.max_chars,
            browser_mode=args.browser,
            base_url=args.base_url,
        )
        _record_saving_safe("html2md", estimate_tokens(raw), estimate_tokens(out))
        print(out)
        return 0

    if args.subcommand == "aria-slim":
        raw = _read_input_source(args.source)
        out = slim_aria_snapshot(raw, max_text_len=args.max_text_len)
        _record_saving_safe("aria-slim", estimate_tokens(raw), estimate_tokens(out))
        print(out)
        return 0

    if args.subcommand == "json-slim":
        raw = _read_input_source(args.source)
        try:
            out = slim_json_text(
                raw,
                max_items=args.max_items,
                max_str_len=args.max_str_len,
                max_depth=args.max_depth,
                indent=2 if args.pretty else None,
            )
        except json.JSONDecodeError as exc:
            print(f"[token-saver: invalid JSON input: {exc}]", file=sys.stderr)
            return 1
        _record_saving_safe("json-slim", estimate_tokens(raw), estimate_tokens(out))
        print(out)
        return 0

    if args.subcommand == "jsonl-slim":
        if args.source != "-" and Path(args.source).is_file():
            p_src = Path(args.source)
            with p_src.open("r", encoding="utf-8", errors="replace") as fh:
                out = slim_jsonl_text(fh, max_records=args.max_records, max_items=args.max_items)
            est_in = max(estimate_tokens(out), p_src.stat().st_size // 4)
            _record_saving_safe("jsonl-slim", est_in, estimate_tokens(out))
            print(out)
            return 0
        raw = _read_input_source(args.source)
        out = slim_jsonl_text(raw, max_records=args.max_records, max_items=args.max_items)
        _record_saving_safe("jsonl-slim", estimate_tokens(raw), estimate_tokens(out))
        print(out)
        return 0

    if args.subcommand == "csv-slim":
        if args.source != "-" and Path(args.source).is_file():
            p_src = Path(args.source)
            out = slim_csv_text(p_src, head_rows=args.head, tail_rows=args.tail)
            est_in = max(estimate_tokens(out), p_src.stat().st_size // 4)
            _record_saving_safe("csv-slim", est_in, estimate_tokens(out))
            print(out)
            return 0
        raw = _read_input_source(args.source)
        out = slim_csv_text(raw, head_rows=args.head, tail_rows=args.tail)
        _record_saving_safe("csv-slim", estimate_tokens(raw), estimate_tokens(out))
        print(out)
        return 0

    if args.subcommand == "diff-slim":
        raw = _read_input_source(args.source)
        out = slim_git_diff(raw, max_hunk_lines=args.max_hunk_lines)
        _record_saving_safe("diff-slim", estimate_tokens(raw), estimate_tokens(out))
        print(out)
        return 0

    if args.subcommand == "md-outline":
        raw = _read_input_source(args.source)
        if args.section:
            out = extract_markdown_section(raw, args.section)
        else:
            out = extract_markdown_outline(raw)
        _record_saving_safe("md-outline", estimate_tokens(raw), estimate_tokens(out))
        print(out)
        return 0

    if args.subcommand == "log-slim":
        raw = _read_input_source(args.source)
        out = compress_cli_output(raw, max_head_lines=args.head, max_tail_lines=args.tail)
        _record_saving_safe("log-slim", estimate_tokens(raw), estimate_tokens(out))
        print(out)
        return 0

    if args.subcommand == "run":
        cmd_list = list(args.command)
        if cmd_list and cmd_list[0] == "--":
            cmd_list = cmd_list[1:]
        if not cmd_list:
            print("Error: no command specified for 'run'", file=sys.stderr)
            return 2
        code, out = run_compressed(
            cmd_list,
            max_head_lines=args.head,
            max_tail_lines=args.tail,
            timeout=args.timeout,
        )
        print(out)
        return code

    if args.subcommand == "batch-slim":
        summary = batch_slim_files(
            args.paths,
            max_items=args.max_items,
            browser_mode=args.browser,
            in_place=args.in_place,
            out_dir=args.out_dir or None,
        )
        if summary["files_processed"] == 0:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1
        _record_saving_safe("batch-slim", summary["tokens_before"], summary["tokens_after"])
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "cache":
        store = _get_store()
        act = args.action
        # Support both flag style (`--namespace ns --key k --value v`) and positional style (`cache get <ns> <key>`)
        ns = args.namespace
        key = args.key
        val = args.value

        if act == "get":
            if not key:
                if args.arg2:
                    ns = args.arg1
                    key = args.arg2
                else:
                    key = args.arg1
            hit = store.get_cached(key, similarity_threshold=args.similarity, namespace=ns)
            if hit is None:
                print(json.dumps({"hit": False, "namespace": (ns or "default").strip().lower()}, ensure_ascii=False))
                return 1
            print(json.dumps({"hit": True, **hit}, ensure_ascii=False))
            return 0

        if act in {"set", "put"}:
            if not key:
                if args.arg3:
                    ns = args.arg1
                    key = args.arg2
                    val = args.arg3
                elif args.arg2:
                    key = args.arg1
                    val = args.arg2
                else:
                    key = args.arg1
            if not val and not sys.stdin.isatty():
                val = sys.stdin.read()
            res = store.set_cached(key, val, ttl_seconds=args.ttl, namespace=ns)
            print(json.dumps(res, ensure_ascii=False))
            return 0

        if act in {"check-circuit", "guard"}:
            target = key or args.arg1
            res = store.check_circuit(target)
            print(json.dumps(res, ensure_ascii=False))
            return 0 if res["allowed"] else 1

        if act == "record-failure":
            target = key or args.arg1
            err = val or args.arg2 or "error"
            res = store.record_failure(target, err)
            print(json.dumps(res, ensure_ascii=False))
            return 0

        if act == "record-success":
            target = key or args.arg1
            res = store.record_success(target)
            print(json.dumps(res, ensure_ascii=False))
            return 0

        if act == "record":
            target = key or args.arg1
            status = (args.arg2 or val or "success").lower()
            if status in {"ok", "success", "pass", "0"}:
                res = store.record_success(target)
            else:
                res = store.record_failure(target, args.arg3 or status)
            print(json.dumps(res, ensure_ascii=False))
            return 0

    if args.subcommand == "stats":
        print(json.dumps(_get_store().get_stats(), ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "selftest":
        print(json.dumps(run_selftest(), ensure_ascii=False, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
