"""Keyword matching that tolerates the way people actually type.

Both the comment and the keyword are normalized the same way before matching:
lowercase, accents removed (ñ → n), runs of the same letter collapsed
("colorummm" → "colorum", "kolorrum" → "kolorum"), and any whitespace turned
into single spaces. A keyword matches whole words only, so "gas" does not hit
"gastos"; a variant ending in "*" also matches longer words ("pahirap*" hits
"pahirapan").
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

from markupsafe import Markup, escape

WORD_CHAR = "a-z0-9"
WILDCARD = "*"


def _fold(ch: str) -> str:
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalized text plus, for each of its characters, the index of the
    original character it came from (used to highlight the original text)."""
    out: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                origin.append(i)
            continue
        for c in _fold(ch):
            if c.isalpha() and out and out[-1] == c:
                continue
            out.append(c)
            origin.append(i)
    return "".join(out), origin


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


def parse_variants(raw: str) -> list[str]:
    """Comma- or newline-separated spellings typed by the admin, cleaned and de-duplicated."""
    seen: dict[str, str] = {}
    for part in re.split(r"[,\n]", raw):
        variant = " ".join(part.split())
        if variant and normalize(variant.rstrip(WILDCARD)):
            seen.setdefault(normalize(variant), variant)
    return list(seen.values())


@dataclass(frozen=True)
class Keyword:
    id: int
    term: str
    variants: tuple[str, ...]

    @property
    def spellings(self) -> tuple[str, ...]:
        return (self.term, *self.variants)


def compile_keyword(keyword: Keyword) -> re.Pattern[str]:
    alternatives = []
    for spelling in keyword.spellings:
        prefix = spelling.endswith(WILDCARD)
        core = normalize(spelling.rstrip(WILDCARD))
        if not core:
            continue
        alternatives.append(re.escape(core) + (f"[{WORD_CHAR}]*" if prefix else ""))
    # Longest first, so a longer spelling wins over a shorter one at the same spot.
    alternatives.sort(key=len, reverse=True)
    body = "|".join(alternatives) or "(?!)"
    return re.compile(f"(?<![{WORD_CHAR}])(?:{body})(?![{WORD_CHAR}])")


class Matcher:
    def __init__(self, keywords: Iterable[Keyword]) -> None:
        self.keywords = list(keywords)
        self._patterns = [(k, compile_keyword(k)) for k in self.keywords]

    def hits(self, text_norm: str) -> list[Keyword]:
        return [k for k, pattern in self._patterns if pattern.search(text_norm)]

    def highlight(self, text: str) -> Markup:
        """HTML-escaped text with every keyword occurrence wrapped in <mark>."""
        if not text:
            return Markup("")
        norm, origin = normalize_with_map(text)
        spans: list[tuple[int, int]] = []
        for _, pattern in self._patterns:
            for m in pattern.finditer(norm):
                # End where the next kept character starts, so letters dropped by
                # collapsing ("colorummm") are highlighted with the word.
                end = origin[m.end()] if m.end() < len(origin) else len(text)
                spans.append((origin[m.start()], end))
        if not spans:
            return escape(text)
        spans.sort()
        merged: list[list[int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        parts: list[str] = []
        cursor = 0
        for start, end in merged:
            parts.append(str(escape(text[cursor:start])))
            parts.append(f"<mark>{escape(text[start:end])}</mark>")
            cursor = end
        parts.append(str(escape(text[cursor:])))
        return Markup("".join(parts))


def load_keywords(conn: sqlite3.Connection) -> list[Keyword]:
    rows = conn.execute("SELECT id, term, variants FROM keywords ORDER BY term").fetchall()
    return [Keyword(r["id"], r["term"], tuple(json.loads(r["variants"]))) for r in rows]


def refresh_hits(conn: sqlite3.Connection, comment_ids: Sequence[str] | None = None) -> None:
    """Recompute keyword_hits for the given comments (all comments when None).

    Run inside the caller's transaction, so hits never disagree with the
    comments or the keyword list."""
    matcher = Matcher(load_keywords(conn))
    if comment_ids is None:
        conn.execute("DELETE FROM keyword_hits")
        rows: Iterable[sqlite3.Row] = conn.execute("SELECT id, text_norm FROM comments")
    else:
        ids = list(comment_ids)
        rows = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM keyword_hits WHERE comment_id IN ({marks})", chunk)
            rows.extend(
                conn.execute(f"SELECT id, text_norm FROM comments WHERE id IN ({marks})", chunk)
            )
    pairs = [(k.id, r["id"]) for r in list(rows) for k in matcher.hits(r["text_norm"])]
    conn.executemany("INSERT INTO keyword_hits (keyword_id, comment_id) VALUES (?, ?)", pairs)


# Starting list, from the topics that came up in the scraped Lopez posts.
DEFAULT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("colorum", ["kolorum", "culorum", "colurom", "culorom", "kulurom", "kolorom", "colorom",
                 "kulorum", "colurum", "clorom", "cholorum"]),
    ("gasolina", ["gasulina", "gatong", "gas", "krudo", "diesel"]),
    ("sidecar", ["side car", "sedicar", "sidekar"]),
    ("prangkisa", ["prankisa", "frangkisa", "prangkesa", "prankesa", "frankesa", "franchise"]),
    ("kalsada", ["karsada", "lubak", "daan"]),
    ("baha", ["flood", "pagbaha"]),
    ("tubig", ["water"]),
    ("trapik", ["traffic", "trapiko"]),
    ("pamasahe", ["pasahe", "pamasahi", "pasahi", "fare"]),
    ("multa", ["fine", "penalty"]),
    ("inspection", ["inspeksyon", "inspeksiyon"]),
    ("body number", ["bodynumber"]),
    ("LTO", []),
    ("mayor", ["meyor", "alkalde"]),
    ("kagawad", ["konsehal", "councilor", "sangguniang bayan"]),
    ("ordinansa", ["ordinance", "ordenansa", "ordinansya"]),
    ("eleksyon", ["election", "eleksiyon", "halalan", "botohan", "butohan", "2028"]),
    ("pahirap", ["pahirap*", "pinapahirapan", "perwisyo", "purwisyo"]),
    ("korap", ["kurap", "kurakot", "corrupt*", "korapsyon", "kurapsyon"]),
    ("buwaya", ["buaya"]),
    ("petition", ["petisyon", "petition for amendment"]),
    ("welga", ["tigil pasada", "strike"]),
]
