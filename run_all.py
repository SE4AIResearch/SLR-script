#!/usr/bin/env python3
"""
Unified Runner for Academic Crawlers

Usage examples:
  # Run Crossref + OpenAlex with a boolean query, limit to 200, and merge results
  python run_all.py --sources crossref,openalex --query "((extract method OR method extract) AND (refactor OR refactoring))" \
    --year-from 2015 --year-to 2025 --limit 200 --formats csv,jsonl --out-dir results --merge

  # Run everything with minimal options, save raw per-source outputs
  python run_all.py --sources all --query "large language model software engineering" --out-dir results
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---- Configuration of supported sources and how to invoke them ----


THIS_DIR = Path(__file__).resolve().parent

# ---- Query preprocessing to translate user-style boolean into source-friendly queries ----
import re as _re
from typing import Tuple as _Tuple

def _strip_outer_parens(s: str) -> str:
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        return s[1:-1].strip()
    return s

def preprocess_user_query(q: str) -> dict:
    """
    Parse a user boolean like:
      (A OR B) AND\n  Abstract (X OR Y) AND\n  Abstract (M OR N) AND\n  PY = (2022-2025)
    Return a dict with normalized query and extracted facets.
    """
    if not q:
        return {}
    raw = q
    # Collapse newlines and multiple spaces
    q = _re.sub(r"\s+", " ", q).strip()

    # Extract PY range
    year_from, year_to = None, None
    m = _re.search(r"PY\s*=\s*\(?\s*(\d{4})\s*[-–]\s*(\d{4})\s*\)?", q, flags=_re.I)
    if m:
        year_from, year_to = int(m.group(1)), int(m.group(2))
        # remove the PY clause
        q = q[:m.start()] + q[m.end():]
        q = q.strip()

    # Extract Abstract(...) clauses
    abstract_terms: list[str] = []
    def _collect_terms(block: str):
        # Split on OR at top-level; keep quoted phrases as-is
        parts = [p.strip() for p in _re.split(r"\s+OR\s+", block, flags=_re.I)]
        return [p for p in parts if p]

    # Find all Abstract ( ... ) occurrences
    for am in _re.finditer(r"Abstract\s*\(([^)]*)\)", q, flags=_re.I):
        inner = am.group(1)
        abstract_terms += _collect_terms(inner)
    # Remove all Abstract(...) clauses from the base string
    q = _re.sub(r"Abstract\s*\([^)]*\)", "", q, flags=_re.I).strip()

    # Remaining may look like (domain terms) AND AND (model terms) etc. Clean duplicate AND/OR
    q = _re.sub(r"\bAND\b\s*\bAND\b", "AND", q, flags=_re.I).strip()
    q = _re.sub(r"\s{2,}", " ", q).strip(" AND ")

    # Split remaining by AND to try to recover groups
    groups = [g.strip() for g in _re.split(r"\bAND\b", q, flags=_re.I) if g.strip()]

    # Flatten parentheses text for each group
    flat_groups = []
    for g in groups:
        g = _strip_outer_parens(g)
        if g:
            flat_groups.append(g)

    # Build normalized boolean: join non-abstract groups with AND; append abstract block (without field) for general sources
    normalized_core = " AND ".join([f"({g})" if (" OR " in g or " or " in g) else g for g in flat_groups])

    # Extract model-like block heuristically if present (contains LLM/ChatGPT/GPT etc.)
    # but we keep everything as part of normalized_core; abstract_terms stay separate for fielded sources like Crossref

    normalized = normalized_core
    if abstract_terms:
        if normalized:
            normalized = f"{normalized} AND (" + " OR ".join(abstract_terms) + ")"
        else:
            normalized = "(" + " OR ".join(abstract_terms) + ")"

    result = {
        "normalized_core": normalized_core.strip(),
        "normalized_query": normalized.strip() or raw,
        "abstract_terms": abstract_terms,
        "year_from": year_from,
        "year_to": year_to,
    }
    return result

@dataclass
class SourceSpec:
    name: str
    script: Path
    # function building argv given common args; returns (argv list, out_prefix str)
    def build(self, args: argparse.Namespace, out_dir: Path) -> Tuple[List[str], str]:
        raise NotImplementedError

class CrossrefSpec(SourceSpec):
    def build(self, args, out_dir):
        out_prefix = out_dir / "phase_1" / "crossref"
        # Apply preprocessed query if available; add fielded abstract clause for Crossref
        q = args.query
        abs_terms = getattr(args, "abstract_terms", None)
        if abs_terms and getattr(args, "enrich_abstracts", True) and not getattr(args, "no_enhance", False):
            abs_clause = "abstract:(" + " OR ".join(abs_terms) + ")"
            q = f"({q}) AND {abs_clause}" if q else abs_clause
        argv = [sys.executable, str(self.script), q]
        if args.year_from is not None: argv += ["--year-from", str(args.year_from)]
        if args.year_to   is not None: argv += ["--year-to",   str(args.year_to)]
        limit_per_source = getattr(args, "limit_per_source", None)
        if limit_per_source is not None:
            argv += ["--limit", str(limit_per_source)]
        if args.sort is not None:      argv += ["--sort", args.sort]
        if args.type:                  argv += ["--type", args.type]
        if args.email:                 argv += ["--email", args.email]
        # output / format
        argv += ["--output", str(out_prefix)]
        formats = args.formats or ["csv"]
        if "csv" in formats and len(formats) == 1:
            argv += ["--format", "csv"]
        elif "json" in formats and len(formats) == 1:
            argv += ["--format", "json"]
        elif "jsonl" in formats and len(formats) == 1:
            argv += ["--format", "jsonl"]
        else:
            argv += ["--format", "all"]
        if args.resolve_urls:
            argv += ["--resolve-urls"]
        if getattr(args, "no_enhance", False):
            argv += ["--no-enhance-abstracts"]
        return argv, str(out_prefix)

class OpenAlexSpec(SourceSpec):
    def build(self, args, out_dir):
        out_prefix = out_dir / "phase_1" / "openalex"
        argv = [sys.executable, str(self.script)]
        if args.doi:
            argv += ["--doi", args.doi]
        else:
            argv += [args.query]
        if args.year_from is not None: argv += ["--year-from", str(args.year_from)]
        if args.year_to   is not None: argv += ["--year-to",   str(args.year_to)]
        limit_per_source = getattr(args, "limit_per_source", None)
        if limit_per_source is not None:
            argv += ["--limit", str(limit_per_source)]
        if args.per_page  is not None: argv += ["--per-page",  str(args.per_page)]
        if args.no_enhance:            argv += ["--no-enhance"]
        if args.verbose:               argv += ["--verbose"]
        if args.debug:                 argv += ["--debug"]
        argv += ["--out-jsonl",str(out_prefix) + ".jsonl"]
        return argv, str(out_prefix)

class ACMSpec(SourceSpec):
    def build(self, args, out_dir):
        out_prefix = out_dir / "phase_1" / "acm"
        argv = [sys.executable, str(self.script)]
        # Parent-level options must appear before the subcommand in argparse
        limit_per_source = getattr(args, "limit_per_source", None)
        if limit_per_source is not None:
            argv += ["--limit", str(limit_per_source)]
        if args.email:                 argv += ["--email", args.email]
        if args.format:                argv += ["--format", args.format]
        argv += ["--output", str(out_prefix)]
        # Subcommand and its own options
        argv += ["search", args.query]
        if args.year_from is not None: argv += ["--year-from", str(args.year_from)]
        if args.year_to   is not None: argv += ["--year-to",   str(args.year_to)]
        if args.type:                  argv += ["--type", args.type]
        return argv, str(out_prefix)

class ArxivSpec(SourceSpec):
    def build(self, args, out_dir):
        out_prefix = out_dir / "phase_1" / "arxiv"
        argv = [sys.executable, str(self.script), args.query]
        # Prefer explicit --max-results; otherwise map per-source limit (if any) to arxiv --max-results
        limit_per_source = getattr(args, "limit_per_source", None)
        if args.max_results is not None:
            argv += ["--max-results", str(args.max_results)]
        elif limit_per_source is not None:
            argv += ["--max-results", str(limit_per_source)]
        if args.sort_by:                 argv += ["--sort-by", args.sort_by]
        if args.sort_order:              argv += ["--sort-order", args.sort_order]
        if args.start is not None:       argv += ["--start", str(args.start)]
        if args.year_from is not None: argv += ["--year-from", str(args.year_from)]
        if args.year_to is not None: argv += ["--year-to", str(args.year_to)]
        # arxiv.py uses --output and writes CSV/JSON/JSONL if present in code
        argv += ["--output", str(out_prefix)]
        return argv, str(out_prefix)

class ScienceDirectSpec(SourceSpec):
    def build(self, args, out_dir):
        out_prefix = out_dir / "phase_1" / "sciencedirect"
        argv = [sys.executable, str(self.script), args.query]
        if args.year_from is not None: argv += ["--year-from", str(args.year_from)]
        if args.year_to   is not None: argv += ["--year-to",   str(args.year_to)]
        limit_per_source = getattr(args, "limit_per_source", None)
        if limit_per_source is not None:
            argv += ["--limit", str(limit_per_source)]
        if args.format:                argv += ["--format", args.format]
        if args.api_key:               argv += ["--api-key", args.api_key]
        # Reuse runner-level no_enhance to disable abstract enrichment in scienceDirect.py
        if getattr(args, "no_enhance", False):
            argv += ["--no-enhance-abstracts"]
        argv += ["--output", str(out_prefix)]
        return argv, str(out_prefix)

class ScopusSpec(SourceSpec):
    def build(self, args, out_dir):
        out_prefix = out_dir / "phase_1" / "scopus"
        argv = [sys.executable, str(self.script), args.query, "--output", str(out_prefix) + ".csv"]
        if args.year_from is not None:
            argv += ["--year-from", str(args.year_from)]
        if args.year_to is not None:
            argv += ["--year-to", str(args.year_to)]
        limit_per_source = getattr(args, "limit_per_source", None)
        if limit_per_source is not None:
            argv += ["--limit", str(limit_per_source)]
        if getattr(args, "no_enhance", False):
            argv += ["--no-enhance"]
        return argv, str(out_prefix)

class SpringerSpec(SourceSpec):
    def build(self, args, out_dir):
        out_prefix = out_dir / "phase_1" / "springer"
        argv = [sys.executable, str(self.script), args.query]
        if args.max_pages is not None: argv += ["--max-pages", str(args.max_pages)]
        if args.discipline:            argv += ["--discipline", args.discipline]
        if args.sort:                  argv += ["--sort", args.sort]
        if args.date_from:             argv += ["--date-from", args.date_from]
        if args.date_to:               argv += ["--date-to", args.date_to]
        # springer writes its own timestamped CSV into ./springer_results; we just run it.
        return argv, str(out_prefix)

class WileySpec(SourceSpec):
    def build(self, args, out_dir):
        out_prefix = out_dir / "phase_1" / "wiley"
        argv = [sys.executable, str(self.script), args.query]
        if args.year_from is not None: argv += ["--year-from", str(args.year_from)]
        if args.year_to   is not None: argv += ["--year-to",   str(args.year_to)]
        limit_per_source = getattr(args, "limit_per_source", None)
        if limit_per_source is not None:
            argv += ["--limit", str(limit_per_source)]
        if args.no_abstracts:          argv += ["--no-abstracts"]
        if args.verbose:               argv += ["--verbose"]
        argv += ["--output", str(out_prefix)]
        return argv, str(out_prefix)

# Registry
SOURCES: Dict[str, SourceSpec] = {
    "crossref":      CrossrefSpec("crossref", THIS_DIR/"sources"/"crossref.py"),
    "openalex":      OpenAlexSpec("openalex", THIS_DIR/"sources"/"openalex.py"),
    "acm":           ACMSpec("acm", THIS_DIR/"sources"/"acm.py"),
    "arxiv":         ArxivSpec("arxiv", THIS_DIR/"sources"/"arxiv.py"),
    "sciencedirect": ScienceDirectSpec("sciencedirect", THIS_DIR/"sources"/"scienceDirect.py"),
    "scopus":        ScopusSpec("scopus", THIS_DIR/"sources"/"scopus.py"),
    "springer":      SpringerSpec("springer", THIS_DIR/"sources"/"springer.py"),
    "wiley":         WileySpec("wiley", THIS_DIR/"sources"/"wiley.py"),
}

def parse_sources(s: str) -> List[str]:
    if s.strip().lower() == "all":
        return list(SOURCES.keys())
    return [x.strip().lower() for x in s.split(",") if x.strip()]

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def run_cmd(argv: List[str]) -> int:
    print(">>", " ".join(shlex.quote(a) for a in argv), flush=True)
    try:
        proc = subprocess.run(argv, check=False)
        return proc.returncode
    except FileNotFoundError:
        print(f"[ERROR] Command not found: {argv[0]}")
        return 127

def discover_latest_csvs(base_dir: Path, allowed_sources: Optional[List[str]] = None) -> Dict[str, Path]:
    """Find per-source CSVs saved by scripts (best-effort). If allowed_sources is provided, restrict to those."""
    found: Dict[str, Path] = {}
    preferred = ["crossref", "openalex", "acm", "arxiv", "sciencedirect", "scopus", "wiley", "springer"]
    for key in preferred:
        if allowed_sources is not None and key not in allowed_sources:
            continue
        if key == "springer":
            # Prefer legacy springer_results dir if present
            springer_dir = THIS_DIR / "springer_results"
            candidates = []
            if springer_dir.exists():
                candidates = sorted(springer_dir.glob("springer_*.csv"))
            # Also check current base_dir for timestamped files (some scripts now write here)
            more = sorted(base_dir.glob("springer_*.csv"))
            candidates = sorted(candidates + more)
            # And check for a plain springer.csv in base_dir
            if (base_dir / "springer.csv").exists():
                candidates.append(base_dir / "springer.csv")
            if candidates:
                found["springer"] = candidates[-1]
            continue
        p = base_dir / f"{key}.csv"
        if p.exists():
            found[key] = p
    return found

from collections import OrderedDict


# ---- Helper: Load all CSVs without deduplication ----
def load_all_csvs(csv_map: Dict[str, Path]) -> List[dict]:
    """Load multiple CSVs and concatenate all records without de-duplication.
    Annotates each record with a 'source' derived from the CSV origin if missing.
    """
    import csv
    rows: List[dict] = []
    for src, path in csv_map.items():
        if not path.exists():
            continue
        with open(path, 'r', encoding='utf-8', newline='') as f:
            r = csv.DictReader(f)
            for rec in r:
                if not (rec.get('source') or rec.get('Source')):
                    rec['source'] = src
                rows.append(rec)
    return rows


def _norm_doi(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = _re.sub(r'^https?://(dx\.)?doi\.org/', '', s)
    s = _re.sub(r'^doi:\s*', '', s)
    return s

def _is_valid_doi(s: str) -> bool:
    """Return True if s looks like a real DOI (e.g., 10.xxxx/...). Reject placeholders like N/A, -, none, null."""
    if not s:
        return False
    s = str(s).strip().lower()
    # Reject common placeholders
    if s in {"n/a", "na", "none", "null", "-", "n.a", "n a", "n\u00a0/a"}:
        return False
    s = _norm_doi(s)
    # Basic DOI shape: 10.<4-9 digits>/<non-space>
    return bool(_re.match(r'^10\.\d{4,9}/\S+$', s))

def _norm_title(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    # collapse whitespace and remove most punctuation for matching
    s = _re.sub(r'\s+', ' ', s)
    s = _re.sub(r'[^a-z0-9\s]', '', s)
    return s

def _rec_has_abstract(rec: dict) -> bool:
    val = rec.get('abstract') or rec.get('Abstract') or ''
    return bool(str(val).strip())

def _dedup_key(rec: dict) -> str:
    doi = _norm_doi(rec.get('doi') or rec.get('DOI') or '')
    if _is_valid_doi(doi):
        return f"doi::{doi}"
    title = _norm_title(rec.get('title') or rec.get('Title') or rec.get('paper_title') or rec.get('Paper Title') or '')
    year = str(rec.get('year') or rec.get('Year') or rec.get('published year') or rec.get('Published Year') or '').strip()
    if title:
        return f"title::{title}::year::{year}"
    # fallback to URL if nothing else
    url = (rec.get('url') or rec.get('URL') or rec.get('link') or rec.get('Link') or '').strip().lower()
    if url:
        return f"url::{url}"
    # last resort: hash of full record
    return f"hash::{hash(str(sorted(rec.items())))}"

def _dedup_merge_records(existing: dict, new: dict, priority_rank: Optional[Dict[str, int]] = None) -> dict:
    """Return the preferred record between two duplicates.
    Preference order:
      1) With abstract over without
      2) Longer abstract length
      3) Tie-break by source priority if provided
      4) Otherwise keep existing (stable)
    """
    has_abs_a = _rec_has_abstract(existing)
    has_abs_b = _rec_has_abstract(new)
    if has_abs_a and not has_abs_b:
        return existing
    if has_abs_b and not has_abs_a:
        return new
    if has_abs_a and has_abs_b:
        la = len(str(existing.get('abstract') or existing.get('Abstract') or ''))
        lb = len(str(new.get('abstract') or new.get('Abstract') or ''))
        if lb > la:
            return new
    # Tie-break by source priority if provided
    if priority_rank is not None:
        sa = (existing.get('source') or existing.get('Source') or '').strip().lower()
        sb = (new.get('source') or new.get('Source') or '').strip().lower()
        ra = priority_rank.get(sa, 1_000_000)
        rb = priority_rank.get(sb, 1_000_000)
        if rb < ra:
            return new
    return existing

def load_and_dedup_csvs(csv_map: Dict[str, Path], priority_rank: Optional[Dict[str, int]] = None) -> List[dict]:
    """Load multiple CSVs, de-duplicate by DOI or (title,year) with abstract preference.
    Also annotate each record with a 'source' field derived from the CSV origin (if not already present).
    """
    import csv
    uniq: OrderedDict[str, dict] = OrderedDict()
    for src, path in csv_map.items():
        if not path.exists():
            continue
        with open(path, 'r', encoding='utf-8', newline='') as f:
            r = csv.DictReader(f)
            for rec in r:
                # Ensure source annotation if missing
                if not (rec.get('source') or rec.get('Source')):
                    rec['source'] = src
                key = _dedup_key(rec)
                if key in uniq:
                    chosen = _dedup_merge_records(uniq[key], rec, priority_rank=priority_rank)
                    uniq[key] = chosen
                else:
                    uniq[key] = rec
    return list(uniq.values())

import sqlite3

def _ensure_sqlite_table(conn: sqlite3.Connection, table: str):
    cur = conn.cursor()
    # Drop & recreate to ensure a clean snapshot for this run
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute(
        f"""
        CREATE TABLE {table} (
            source TEXT,
            title TEXT,
            authors TEXT,
            year TEXT,
            doi TEXT,
            url TEXT,
            abstract TEXT,
            venue TEXT,
            publisher TEXT,
            keywords TEXT,
            citations TEXT,
            published_date TEXT,
            content_type TEXT
        )
        """
    )
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_doi ON {table}(doi)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_title ON {table}(title)")
    conn.commit()

def _write_rows_to_sqlite(rows: List[Dict[str, str]], db_path: Path, table: str):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_sqlite_table(conn, table)
        cur = conn.cursor()
        cur.executemany(
            f"""
            INSERT INTO {table} (
                source, title, authors, year, doi, url, abstract, venue, publisher,
                keywords, citations, published_date, content_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.get("source", ""), r.get("title", ""), r.get("authors", ""), r.get("year", ""),
                    r.get("doi", ""), r.get("url", ""), r.get("abstract", ""), r.get("venue", ""),
                    r.get("publisher", ""), r.get("keywords", ""), r.get("citations", ""),
                    r.get("published_date", ""), r.get("content_type", "")
                )
                for r in rows
            ]
        )
        conn.commit()
    finally:
        conn.close()

def normalize_and_merge(
    csv_map: Dict[str, Path],
    merged_path: Path,
    target_limit: Optional[int] = None,
    sql_db_path: Optional[Path] = None,
    sql_table: str = "papers",
    priority_rank: Optional[Dict[str, int]] = None,
    dedup: bool = True,
    drop_empty_doi: bool = False,
) -> int:
    """
    Normalize different CSV schemas into a unified set of columns and merge.
    Returns the number of unique rows written.
    Optionally writes merged rows to SQLite if sql_db_path is given.
    """
    import csv
    COMMON_FIELDS = [
        "source", "title", "authors", "year", "doi", "url", "abstract",
        "venue", "publisher", "keywords", "citations", "published_date", "content_type"
    ]

    # Load records (either deduplicated or raw concatenation)
    if dedup:
        records = load_and_dedup_csvs(csv_map, priority_rank=priority_rank)
    else:
        records = load_all_csvs(csv_map)

    # Optionally drop records with empty DOI (for phase 2)
    if drop_empty_doi:
        def _has_doi(r: dict) -> bool:
            raw = r.get('doi') or r.get('DOI') or r.get('Doi') or ''
            return _is_valid_doi(raw)
        records = [r for r in records if _has_doi(r)]

    # Optionally trim to target_limit while preserving order
    if target_limit is not None and target_limit > 0:
        records = records[:target_limit]

    rows: List[Dict[str, str]] = []

    def norm_authors(a):
        if isinstance(a, list):
            return "; ".join(a)
        return a or ""

    for rec in records:
        source = rec.get("source") or rec.get("Source") or ""
        title = rec.get("title") or rec.get("Title") or rec.get("paper_title") or rec.get("Paper Title") or ""
        authors = rec.get("authors") or rec.get("Authors") or rec.get("author") or ""
        year = rec.get("year") or rec.get("Year") or rec.get("published year") or rec.get("Published Year") or ""
        doi_raw = rec.get("doi") or rec.get("DOI") or rec.get("Doi") or ""
        doi = _norm_doi(doi_raw)
        if not _is_valid_doi(doi):
            doi = ""
        url = rec.get("url") or rec.get("URL") or rec.get("link") or rec.get("Link") or ""
        abstract = rec.get("abstract") or rec.get("Abstract") or ""
        venue = rec.get("venue") or rec.get("Venue") or rec.get("journal") or rec.get("Journal") or ""
        publisher = rec.get("publisher") or rec.get("Publisher") or ""
        keywords = rec.get("keywords") or rec.get("Keywords") or ""
        citations = rec.get("citations") or rec.get("Citations") or ""
        published_date = rec.get("published_date") or rec.get("Published Date") or rec.get("published date") or ""
        content_type = rec.get("content_type") or rec.get("Content Type") or rec.get("type") or ""

        row = {
            "source": source,
            "title": title,
            "authors": norm_authors(authors),
            "year": year,
            "doi": doi,
            "url": url,
            "abstract": abstract,
            "venue": venue,
            "publisher": publisher,
            "keywords": keywords,
            "citations": citations,
            "published_date": published_date,
            "content_type": content_type,
        }
        rows.append(row)

    # Write merged CSV
    ensure_dir(merged_path.parent)
    with open(merged_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COMMON_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow(row)

    # Optionally write to SQLite
    if sql_db_path is not None:
        _write_rows_to_sqlite(rows, sql_db_path, sql_table)

    return len(rows)

def main():
    p = argparse.ArgumentParser(description="Unified runner for your academic crawlers")
    p.add_argument("--sources", help="Comma-separated sources or 'all'. If omitted (and --source is not set), defaults to 'all' sources.")
    p.add_argument("--source", help="Alias of --sources (singular); can be combined with --sources. If both are omitted, all sources are used.")
    p.add_argument("--query", help="Search query (ignored if --doi is set and a source supports DOI)")
    p.add_argument("--doi", help="DOI for sources that support fetching by DOI (e.g., openalex, crossref)")
    p.add_argument("--year-from", type=int, dest="year_from")
    p.add_argument("--year-to", type=int, dest="year_to")
    p.add_argument(
        "--limit",
        type=int,
        help=(
            "Optional global cap on the merged, deduplicated output when used with --merge. "
            "By default, sources will use their own pagination and fetch as many results as they expose, and the merged output will be truncated to --limit rows if provided. "
            "When combined with --per-provider, this value is also used as a per-provider fetch cap (and may be increased internally during iterative re-runs). "
            "For ArXiv, an active per-provider cap is forwarded as --max-results."
        ),
    )
    p.add_argument(
        "--per-provider",
        action="store_true",
        help=(
            "Treat --limit as a per-provider fetch cap during crawling. "
            "By default, --limit only caps the merged, deduplicated output when --merge is used."
        ),
    )
    p.add_argument("--fresh", action="store_true", help="Before running, delete any existing per-source outputs in --out-dir for the chosen sources to avoid merging stale files.")
    p.add_argument("--source-priority", help="Comma-separated source priority for dedup tie-breaks (default: chosen order). Example: sciencedirect,openalex,crossref")
    p.add_argument("--per-page", type=int, dest="per_page")
    p.add_argument("--type", help="Content type filter for sources that support it")
    p.add_argument("--email", help="Email for Crossref polite pool")
    p.add_argument("--format", dest="format", help="Preferred per-source format when single format is needed (csv|json|jsonl)")
    p.add_argument("--no-enhance", dest="no_enhance", action="store_true", help="Disable enhanced abstracts where available (OpenAlex)")
    p.add_argument(
        "--enrich-abstracts",
        dest="enrich_abstracts",
        nargs="?",
        default=True,
        const=True,
        type=lambda s: False if isinstance(s, str) and s.lower() in ("false", "0", "no") else True,
        help=(
            "Whether to append Abstract(...) terms to queries (and fielded abstract clauses for sources that support it). "
            "Default: true. Set to 'false' to disable."
        ),
    )
    p.add_argument("--resolve-urls", action="store_true", help="Crossref: resolve URLs (slower)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--max-results", type=int, dest="max_results", help="ArXiv: max results")
    p.add_argument("--sort-by", help="ArXiv: sort-by")
    p.add_argument("--sort-order", help="ArXiv: sort-order")
    p.add_argument("--start", type=int, help="ArXiv: start offset")

    # ScienceDirect / Elsevier
    p.add_argument("--api-key", help="Elsevier API key for ScienceDirect/Scopus")

    # Springer
    p.add_argument("--max-pages", type=int, help="Springer: max pages")
    p.add_argument("--discipline", help="Springer: discipline facet (default Computer Science)")
    p.add_argument("--date-from", help="Springer: YYYY or empty")
    p.add_argument("--date-to", help="Springer: YYYY or empty")
    p.add_argument("--sort", help="Crossref/Springer: sort setting")

    p.add_argument("--out-dir", default="results", help="Directory to store outputs")
    p.add_argument("--formats", help="Comma-separated formats to request when supported (csv,json,jsonl). If multiple, 'all' will be used when available.")
    p.add_argument("--merge", action="store_true", help="Normalize & merge per-source CSVs into one CSV")
    p.add_argument("--no-dedup", action="store_true", help="When used with --merge, skip de-duplication and keep all rows from every source.")
    # Accept both --save-sql and --save_sql
    p.add_argument("--save-sql", "--save_sql", dest="save_sql", action="store_true",
                   help="Also save merged (deduped) results to a SQLite database.")
    p.add_argument("--sql-db", help="Path to SQLite DB file (default: <out-dir>/merged_all_sources.sqlite)")
    p.add_argument("--sql-table", default="papers", help="Table name for SQLite output (default: papers)")

    args = p.parse_args()

    # Validate query/doi
    if not args.query and not args.doi:
        p.error("You must provide --query (or --doi for DOI-capable sources).")

    # Allow using --source as an alias to --sources; default to 'all' when neither is given
    if args.sources and args.source:
        # Both provided: merge them
        args.sources = f"{args.sources},{args.source}"
    elif not args.sources and args.source:
        # Only --source provided
        args.sources = args.source
    elif not args.sources and not args.source:
        # Neither provided: default to all sources
        args.sources = "all"

    if args.limit is None:
        print("[INFO] No --limit provided: each source will attempt to fetch all available results (may take a long time).")

    formats = None
    if args.formats:
        formats = [x.strip().lower() for x in args.formats.split(",") if x.strip()]
    setattr(args, "formats", formats)

    # Preprocess complex user boolean queries (e.g., Abstract(...) and PY ranges)
    if args.query:
        _pp = preprocess_user_query(args.query)
        if _pp:
            # Use normalized query; if enrich_abstracts is False, keep only the core (non-abstract) part
            if getattr(args, "enrich_abstracts", True) and not getattr(args, "no_enhance", False):
                args.query = _pp.get("normalized_query", args.query)
                setattr(args, "abstract_terms", _pp.get("abstract_terms", []))
            else:
                # Strip abstract terms entirely from both generic query and fielded sources
                args.query = _pp.get("normalized_core", args.query)
                setattr(args, "abstract_terms", [])
            # Set year range if not provided via flags
            if args.year_from is None and _pp.get("year_from") is not None:
                args.year_from = _pp["year_from"]
            if args.year_to is None and _pp.get("year_to") is not None:
                args.year_to = _pp["year_to"]

    out_dir = Path(args.out_dir).resolve()
    # Two-phase output directories
    phase1_dir = out_dir / "phase_1"
    phase2_dir = out_dir / "phase_2"
    phase1_dir.mkdir(parents=True, exist_ok=True)
    phase2_dir.mkdir(parents=True, exist_ok=True)
    sql_db_path = None
    if args.save_sql:
        sql_db_path = Path(args.sql_db) if args.sql_db else (out_dir / "merged_all_sources.sqlite")

    # Map of optional flags for specs
    setattr(args, "no_abstracts", args.no_enhance)

    # Build and run commands
    chosen = parse_sources(args.sources)
    unknown = [c for c in chosen if c not in SOURCES]
    if unknown:
        print(f"[WARN] Unknown sources: {unknown}. Known: {list(SOURCES.keys())}", file=sys.stderr)
        chosen = [c for c in chosen if c in SOURCES]

    # Build source priority for dedup (ties)
    priority_list = [s.strip().lower() for s in (args.source_priority.split(',') if args.source_priority else chosen)]
    priority_rank = {name: idx for idx, name in enumerate(priority_list)}

    # Internal per-source fetch limit: only used when --per-provider is enabled.
    limit_per_source = None
    if args.limit is not None and getattr(args, "per_provider", False):
        limit_per_source = args.limit
    setattr(args, "limit_per_source", limit_per_source)

    # Optionally remove stale outputs for chosen sources
    if args.fresh:
        for key in chosen:
            for ext in (".csv", ".json", ".jsonl"):
                fp = (phase1_dir / f"{key}{ext}")
                if fp.exists():
                    try:
                        fp.unlink()
                        print(f"[INFO] Removed stale file: {fp}")
                    except Exception as e:
                        print(f"[WARN] Failed to remove {fp}: {e}")

    results: Dict[str, str] = {}
    for key in chosen:
        spec = SOURCES[key]
        if not spec.script.exists():
            print(f"[WARN] Script not found for source '{key}': {spec.script}", file=sys.stderr)
            continue
        argv, out_prefix = spec.build(args, out_dir)
        rc = run_cmd(argv)
        results[key] = f"exit={rc}; out_prefix={out_prefix}"

    # Merge CSVs into two phases if requested
    if args.merge:
        # Phase 1: concatenate without deduplication and keep empty DOIs
        merged_phase1 = phase1_dir / "merged_all_sources.csv"
        csv_map_p1 = discover_latest_csvs(phase1_dir, allowed_sources=chosen)
        if not csv_map_p1:
            print("[WARN] No per-source CSVs found in phase_1 to merge. Skipping.", file=sys.stderr)
        else:
            total_rows = normalize_and_merge(
                csv_map_p1, merged_phase1,
                target_limit=(args.limit if args.limit else None),
                sql_db_path=(phase1_dir / "merged_all_sources.sqlite" if args.save_sql else None),
                sql_table=args.sql_table,
                priority_rank=None,
                dedup=False,
                drop_empty_doi=False,
            )
            print(f"[OK] Phase 1 merged (no dedup, keep empty DOI) rows: {total_rows}. Written to: {merged_phase1}")

        # Phase 2: deduplicate and drop empty DOI
        merged_phase2 = phase2_dir / "merged_all_sources.csv"
        csv_map_p2 = discover_latest_csvs(phase1_dir, allowed_sources=chosen)
        if not csv_map_p2:
            print("[WARN] No per-source CSVs found in phase_1 to build phase_2. Skipping.", file=sys.stderr)
        else:
            if args.limit:
                if getattr(args, "per_provider", False):
                    # Per-provider mode: keep iterative re-crawl behavior, but bump per-source fetch limit only.
                    attempt = 0
                    max_attempts = 3
                    growth = 1.5
                    target_unique = args.limit
                    while True:
                        unique_count = normalize_and_merge(
                            csv_map_p2, merged_phase2, target_limit=None,
                            sql_db_path=(phase2_dir / "merged_all_sources.sqlite" if args.save_sql else None),
                            sql_table=args.sql_table,
                            priority_rank={name: idx for idx, name in enumerate(chosen)},
                            dedup=True,
                            drop_empty_doi=True,
                        )
                        print(f"[OK] Phase 2 merged (dedup + drop empty DOI) rows: {unique_count}. Written to: {merged_phase2}")
                        if unique_count >= target_unique or attempt >= max_attempts:
                            if unique_count > target_unique:
                                _ = normalize_and_merge(
                                    csv_map_p2, merged_phase2, target_limit=target_unique,
                                    sql_db_path=(phase2_dir / "merged_all_sources.sqlite" if args.save_sql else None),
                                    sql_table=args.sql_table,
                                    priority_rank={name: idx for idx, name in enumerate(chosen)},
                                    dedup=True,
                                    drop_empty_doi=True,
                                )
                                print(f"[OK] Phase 2 trimmed to target {target_unique} rows.")
                            break
                        attempt += 1
                        prev_limit = getattr(args, "limit_per_source", None) or 100
                        new_limit = int(prev_limit * growth)
                        if new_limit == prev_limit:
                            new_limit += 50
                        args.limit_per_source = new_limit
                        print(f"[INFO] Phase 2 unique < target ({unique_count} < {target_unique}). Increasing per-provider fetch limit to {args.limit_per_source} and re-running sources...")
                        # Re-run the selected sources with higher per-provider limit
                        for key in chosen:
                            spec = SOURCES[key]
                            if not spec.script.exists():
                                continue
                            argv, _ = spec.build(args, out_dir)
                            _ = run_cmd(argv)
                        # Refresh csv_map after re-run (from phase_1)
                        csv_map_p2 = discover_latest_csvs(phase1_dir, allowed_sources=chosen)
                else:
                    # Global mode: single merge and cap merged output to --limit rows.
                    unique_count = normalize_and_merge(
                        csv_map_p2, merged_phase2,
                        target_limit=args.limit,
                        sql_db_path=(phase2_dir / "merged_all_sources.sqlite" if args.save_sql else None),
                        sql_table=args.sql_table,
                        priority_rank={name: idx for idx, name in enumerate(chosen)},
                        dedup=True,
                        drop_empty_doi=True,
                    )
                    print(f"[OK] Phase 2 merged (dedup + drop empty DOI) rows: {unique_count}. Written to: {merged_phase2}")
            else:
                unique_count = normalize_and_merge(
                    csv_map_p2, merged_phase2,
                    target_limit=None,
                    sql_db_path=(phase2_dir / "merged_all_sources.sqlite" if args.save_sql else None),
                    sql_table=args.sql_table,
                    priority_rank={name: idx for idx, name in enumerate(chosen)},
                    dedup=True,
                    drop_empty_doi=True,
                )
                print(f"[OK] Phase 2 merged (dedup + drop empty DOI) rows: {unique_count}. Written to: {merged_phase2}")

    # Save a small run manifest for traceability
    manifest = {
        "args": vars(args),
        "results": results,
        "phase_1_dir": str(phase1_dir),
        "phase_2_dir": str(phase2_dir),
    }
    with open(out_dir / "run_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
