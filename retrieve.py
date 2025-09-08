import argparse
import csv
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Iterable, Tuple

import requests

COMBO_ENABLED = True

# Configuration and Constants
SOURCE_RULES = {
    "acm": {
        "doi_prefixes": ["10.1145"],
        "publisher": ["association for computing machinery"],
        "venue_contains": ["acm"]
    },
    "ieee": {
        "doi_prefixes": ["10.1109"],
        "publisher": ["institute of electrical and electronics engineers"],
        "venue_contains": ["ieee"]
    },
    "springer": {
        "doi_prefixes": ["10.1007"],
        "publisher": ["springer"],
        "venue_contains": ["springer"]
    },
    "elsevier": {
        "doi_prefixes": ["10.1016"],
        "publisher": ["elsevier"],
        "venue_contains": ["elsevier", "sciencedirect"]
    },
    "wiley": {
        "doi_prefixes": ["10.1002"],
        "publisher": ["wiley"],
        "venue_contains": ["wiley"]
    }
}


# String normalization and utility functions
def _detect_source(doi: Optional[str], venue: Optional[str], publisher: Optional[str] = None) -> Optional[str]:
    d = (doi or "").lower().strip()
    v = (venue or "").lower() if venue else ""
    p = (publisher or "").lower() if publisher else ""
    for src, rules in SOURCE_RULES.items():
        for pref in rules.get("doi_prefixes", []):
            if d.startswith(pref):
                return src
        for kw in rules.get("publisher", []):
            if kw in p:
                return src
        for kw in rules.get("venue_contains", []):
            if kw in v:
                return src
    return None


def _norm(s: Optional[str]) -> str:
    return re.sub(r'\s+', ' ', (s or '')).strip()


def _norm_title(s: Optional[str]) -> str:
    s = (s or '').lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _year_from_date(d: Optional[str]) -> Optional[int]:
    if not d:
        return None
    m = re.match(r'^(\d{4})', d)
    return int(m.group(1)) if m else None


def _uniq(seq: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _clean_doi(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    x = x.strip().lower()
    x = re.sub(r'^https?://(dx\.)?doi\.org/', '', x)
    return x or None


_DOI_FROM_URL = re.compile(r'/doi/(10\.\d{4,9}/\S+)', re.IGNORECASE)


def _doi_from_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    m = _DOI_FROM_URL.search(u)
    return _clean_doi(m.group(1)) if m else None



# Query processing and transformation functions
_PY_RE = re.compile(r"\bPY\s*=\s*\(?\s*(\d{4})\s*-\s*(\d{4})\s*\)?", re.IGNORECASE)
_ABS_BLOCK_RE = re.compile(r"\bAbstract\s*\(([^)]*)\)", re.IGNORECASE)


def extract_years_from_query(q: str) -> Tuple[str, Optional[int], Optional[int]]:
    """Extract PY=(YYYY-YYYY) from query if present; return cleaned_query, year_from, year_to."""
    if not q:
        return q, None, None
    m = _PY_RE.search(q)
    if not m:
        return q, None, None
    y1, y2 = int(m.group(1)), int(m.group(2))
    # remove this PY clause from the query string
    cleaned = _PY_RE.sub(" ", q)
    return cleaned.strip(), min(y1, y2), max(y1, y2)


def rewrite_abstract_for_springer(q: str) -> str:
    return _ABS_BLOCK_RE.sub(lambda m: f"abstract:({m.group(1)})", q)


def rewrite_abstract_for_scopus(q: str) -> str:
    return _ABS_BLOCK_RE.sub(lambda m: f"ABS({m.group(1)})", q)


def rewrite_abstract_for_arxiv(q: str) -> str:
    return _ABS_BLOCK_RE.sub(lambda m: f"abs:({m.group(1)})", q)


def strip_abstract_parenthetic(q: str) -> str:
    """Fallback: drop the 'Abstract ' prefix and keep inner content."""
    return _ABS_BLOCK_RE.sub(lambda m: f"({m.group(1)})", q)



# Database capabilities and adaptation
DB_CAPABILITIES = {
    "scopus": {"boolean": "full", "wildcards": True, "field_support": True},
    "springer": {"boolean": "medium", "wildcards": False, "field_support": True},
    "ieee": {"boolean": "medium", "wildcards": False, "field_support": False},
    "arxiv": {"boolean": "basic", "wildcards": False, "field_support": True},
    "openalex": {"boolean": "none", "wildcards": False, "field_support": False},
    "crossref": {"boolean": "none", "wildcards": False, "field_support": False},
    "acm": {"boolean": "none", "wildcards": False, "field_support": False},
}


# Boolean query processing
_BOOL_OP_RE = re.compile(r'\b(AND|OR|NOT|ANDNOT)\b', re.IGNORECASE)
_WILDCARD_RE = re.compile(r'\w+\*')

def _split_top_level(expr: str, sep: str) -> List[str]:
    """Split expression by top-level separator, respecting quotes and parentheses."""
    tokens, buffer = [], []
    depth, in_quotes = 0, False
    i, n = 0, len(expr)
    sep_upper = sep.upper()
    
    while i < n:
        ch = expr[i]
        if ch == '"':
            in_quotes = not in_quotes
            buffer.append(ch)
            i += 1
            continue
            
        if not in_quotes:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth = max(0, depth - 1)
            
            if depth == 0 and expr[i:].upper().startswith(sep_upper):
                pre_ok = (i == 0) or (not expr[i-1].isalnum())
                post_ok = (i + len(sep) >= n) or (not expr[i + len(sep)].isalnum())
                if pre_ok and post_ok:
                    tokens.append(''.join(buffer).strip())
                    buffer = []
                    i += len(sep)
                    continue
                    
        buffer.append(ch)
        i += 1
        
    if buffer:
        tokens.append(''.join(buffer).strip())
        
    return [t for t in tokens if t]

def _strip_outer_parens(s: str) -> str:
    """Remove outer parentheses if they wrap the entire string."""
    s = s.strip()
    if not (s.startswith('(') and s.endswith(')')):
        return s
        
    depth = 0
    for i, ch in enumerate(s):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0 and i != len(s) - 1:
                return s
                
    return s[1:-1].strip()

def _split_top_level_and_groups_by_parens(expr: str) -> List[str]:
    groups = []
    buf = []
    depth = 0
    in_q = False
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch == '"':
            in_q = not in_q
            buf.append(ch); i += 1; continue
        if not in_q:
            if ch == '(':
                if depth == 0 and buf and ''.join(buf).strip().upper().endswith('AND'):
                    # trim trailing AND when starting a new group
                    s = ''.join(buf).strip()
                    s = re.sub(r'\bAND\s*$', '', s, flags=re.IGNORECASE)
                    if s:
                        groups.append(s)
                    buf = []
                depth += 1
            elif ch == ')':
                depth = max(0, depth-1)
            else:
                # detect AND separators at depth 0
                if depth == 0 and expr[i:].upper().startswith('AND'):
                    pre_ok = (i == 0) or (not expr[i-1].isalnum())
                    post_ok = (i+3 >= n) or (not expr[i+3].isalnum())
                    if pre_ok and post_ok:
                        s = ''.join(buf).strip()
                        if s:
                            groups.append(s)
                        buf = []
                        i += 3
                        continue
        buf.append(ch)
        i += 1
    tail = ''.join(buf).strip()
    if tail:
        groups.append(tail)
    return [g.strip() for g in groups if g.strip()]

def expand_boolean_queries_for_weak_provider(q: str, max_combos: int = 24) -> List[str]:
    """
    (A OR B) AND (C OR D) AND E -> ['A C E', 'B C E', ...]
    Improved: normalize line breaks/hyphenation, collapse whitespace,
    strip repeated outer parens, split by top-level AND/OR, build Cartesian products with cap, dedupe.
    """
    if not q:
        return []
    # --- Normalize: fix hyphenation at EOL and collapse whitespace/newlines ---
    # Turn "separat*-\nmethod" -> "separat*method", then collapse whitespace
    q_norm = q.replace("\r", "")
    q_norm = re.sub(r"-\s*\n\s*", "", q_norm)
    q_norm = re.sub(r"\s+", " ", q_norm).strip()

    # --- Strip outer parentheses repeatedly when they wrap the whole expr ---
    expr = q_norm
    for _ in range(10):  # safety cap
        s = expr.strip()
        if not (s.startswith('(') and s.endswith(')')):
            break
        depth = 0
        wraps = True
        for i, ch in enumerate(s):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    wraps = False
                    break
        if wraps:
            expr = s[1:-1].strip()
        else:
            break

    # --- Split by top-level AND into groups ---
    and_groups = _split_top_level(expr, 'AND') if _looks_boolean(expr) else [expr]
    if len(and_groups) < 2 and 'AND' in expr.upper():
        # Fallback: try parentheses-aware split (handles cases our generic splitter misses)
        and_groups = _split_top_level_and_groups_by_parens(expr)

    or_lists: List[List[str]] = []
    for g in and_groups:
        g = _strip_outer_parens(g)
        alts = _split_top_level(g, 'OR')
        if not alts:
            alts = [g]
        cleaned = []
        for a in alts:
            a2 = a.strip()
            a2 = _BOOL_OP_RE.sub(' ', a2)
            a2 = re.sub(r'^[()\s]+|[()\s]+$', '', a2)
            if a2:
                cleaned.append(a2)
        if cleaned:
            or_lists.append(cleaned)

    if not or_lists:
        return [expr]

    # --- Cartesian product with cap ---
    def _maybe_quote(s: str) -> str:
        s = s.strip()
        if not s:
            return s
        # If already quoted, keep as is
        if s.startswith('"') and s.endswith('"'):
            return s
        # Quote phrases that contain whitespace; do NOT quote plain wildcard-only tokens
        # (e.g., refactor* stays unquoted)
        if any(ch.isspace() for ch in s):
            return f'"{s}"'
        return s

    combos: List[List[str]] = [[]]
    for group in or_lists:
        new: List[List[str]] = []
        for base in combos:
            for alt in group:
                new.append(base + [alt])
        # Instead of breaking early (which dropped later AND groups),
        # keep a sampled subset so we always carry all groups forward.
        if len(new) > max_combos:
            # Round-robin sampling to preserve diversity across alternatives in this group
            sampled = []
            alt_count = len(group) if len(group) > 0 else 1
            # Iterate over new in cycles so each alternative index appears
            start = 0
            while len(sampled) < max_combos and start < alt_count:
                idx = start
                while len(sampled) < max_combos and idx < len(new):
                    sampled.append(new[idx])
                    idx += alt_count
                start += 1
            new = sampled[:max_combos]
        combos = new
        if not combos:
            break

    # Build final strings and dedupe
    out, seen = [], set()
    for parts in combos:
        parts2 = [_maybe_quote(p) for p in parts if p]
        s = re.sub(r'\s+', ' ', ' '.join(parts2)).strip()
        k = s.lower()
        if s and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def _looks_boolean(q: str) -> bool:
    if not q:
        return False
    if _BOOL_OP_RE.search(q):
        return True
    return any(ch in q for ch in '()')


def _extract_and_groups(q: str) -> List[str]:
    """Extract major AND-connected groups from complex boolean query."""
    if not q:
        return []

    # Remove outer parentheses if they wrap the entire query
    q = q.strip()
    max_iterations = 10  # Prevent infinite loop
    iteration_count = 0

    while q.startswith('(') and q.endswith(')') and iteration_count < max_iterations:
        # Check if parentheses are balanced and wrap the whole string
        depth = 0
        wraps_whole_string = True
        for i, ch in enumerate(q):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0 and i < len(q) - 1:
                    wraps_whole_string = False
                    break

        if wraps_whole_string and len(q) > 2:
            # Parentheses wrap the whole string
            q = q[1:-1].strip()
            iteration_count += 1
        else:
            break

    # Split on top-level AND operations
    groups = []
    current_group = ""
    paren_depth = 0
    i = 0
    max_chars = len(q) + 100  # Safety limit to prevent infinite loops
    processed_chars = 0

    while i < len(q) and processed_chars < max_chars:
        ch = q[i]
        if ch == '(':
            paren_depth += 1
        elif ch == ')':
            paren_depth -= 1

        # Check for AND at depth 0
        if paren_depth == 0 and i + 3 <= len(q) and q[i:i+3].upper() == 'AND':
            # Make sure it's a word boundary
            if (i == 0 or not q[i-1].isalnum()) and (i+3 >= len(q) or not q[i+3].isalnum()):
                if current_group.strip():
                    groups.append(current_group.strip())
                current_group = ""
                i += 3
                processed_chars += 3
                continue

        current_group += ch
        i += 1
        processed_chars += 1

    if current_group.strip():
        groups.append(current_group.strip())

    return groups


def _extract_key_terms_from_or_group(group: str) -> List[str]:
    """Extract key terms from an OR group, handling quoted phrases and wildcards."""
    terms = []

    # First extract quoted phrases
    quoted_phrases = re.findall(r'"([^"]+)"', group)
    terms.extend(quoted_phrases)

    # Remove quoted phrases temporarily
    group_no_quotes = re.sub(r'"[^"]+"', ' ', group)

    # Extract wildcard terms
    wildcard_terms = _WILDCARD_RE.findall(group_no_quotes)
    terms.extend(wildcard_terms)

    # Remove wildcards and get remaining OR-separated terms
    group_no_wildcards = _WILDCARD_RE.sub(' ', group_no_quotes)
    or_terms = re.split(r'\s+OR\s+', group_no_wildcards, flags=re.IGNORECASE)

    for term in or_terms:
        term = re.sub(r'[()]', ' ', term).strip()
        if term and len(term) > 2:
            terms.append(term)

    # Deduplicate while preserving order
    seen = set()
    unique_terms = []
    for term in terms:
        term_lower = term.lower()
        if term_lower not in seen and len(term) > 1:
            seen.add(term_lower)
            unique_terms.append(term)

    return unique_terms


def _query_complexity_score(query: str) -> int:
    """Calculate query complexity score for fallback decisions."""
    score = 0

    # Boolean operators increase complexity
    bool_ops = len(_BOOL_OP_RE.findall(query))
    score += bool_ops * 2

    # Parentheses depth increases complexity
    max_depth = 0
    current_depth = 0
    for ch in query:
        if ch == '(':
            current_depth += 1
            max_depth = max(max_depth, current_depth)
        elif ch == ')':
            current_depth -= 1
    score += max_depth * 3

    # Wildcards add complexity
    wildcards = len(_WILDCARD_RE.findall(query))
    score += wildcards

    # Quoted phrases are manageable
    quotes = len(re.findall(r'"[^"]+"', query))
    score += quotes * 0.5

    # Very long queries are complex
    if len(query) > 200:
        score += (len(query) - 200) / 50

    return int(score)


def _adapt_query_with_fallback(query: str, db_name: str) -> str:
    """Adapt query with progressive fallback strategy."""
    capabilities = DB_CAPABILITIES.get(db_name, DB_CAPABILITIES["openalex"])
    complexity = _query_complexity_score(query)

    try:
        # First attempt: full adaptation
        result = _adapt_query_for_database(query, db_name)

        # Validate result isn't empty or too short
        if len(result.strip()) < 3:
            raise ValueError("Query adaptation resulted in empty query")

        # # For very complex queries on limited databases, add warning
        # if complexity > 15 and capabilities["boolean"] == "none":
        #     print(f"[WARN] Complex query simplified for {db_name}: may lose precision", file=sys.stderr)

        return result

    except Exception as e:
        print(f"[WARN] Query adaptation failed for {db_name}: {e}", file=sys.stderr)
        # Fallback to simple keyword extraction
        return _intelligent_keyword_extraction(query)


def _adapt_query_for_database(query: str, db_name: str) -> str:
    """Adapt complex query for specific database capabilities."""
    capabilities = DB_CAPABILITIES.get(db_name, DB_CAPABILITIES["openalex"])

    if capabilities["boolean"] == "full":
        # Keep full query, just normalize wildcards if needed
        if not capabilities["wildcards"]:
            # Convert wildcards to word variations
            query = _expand_wildcards(query)
        return query

    elif capabilities["boolean"] == "medium":
        # Simplify but preserve major structure
        and_groups = _extract_and_groups(query)
        if not and_groups:
            # Fallback if AND group extraction fails
            return _intelligent_keyword_extraction(query)

        simplified_groups = []

        for group in and_groups:
            key_terms = _extract_key_terms_from_or_group(group)
            if not key_terms:
                continue  # Skip empty groups

            if capabilities["wildcards"]:
                group_query = " OR ".join(f'"{term}"' if ' ' in term else term for term in key_terms[:5])
            else:
                # Expand wildcards and take top terms
                expanded_terms = []
                for term in key_terms[:5]:
                    if '*' in term:
                        expanded_terms.extend(_expand_wildcard_term(term)[:3])
                    else:
                        expanded_terms.append(term)
                group_query = " OR ".join(f'"{term}"' if ' ' in term else term for term in expanded_terms[:7])

            if group_query.strip():
                simplified_groups.append(f"({group_query})")

        if not simplified_groups:
            # Fallback if no valid groups
            return _intelligent_keyword_extraction(query)

        return " AND ".join(simplified_groups)

    else:  # basic or none
        # Extract most important keywords
        return _intelligent_keyword_extraction(query)


def _expand_wildcard_term(term: str) -> List[str]:
    """Expand a wildcard term generically (no domain-specific vocab)."""
    if not term or not term.endswith('*'):
        return [term]
    base = term[:-1]
    variants = [base, base + 's', base + 'ing', base + 'ed']
    out, seen = [], set()
    for v in variants:
        if v and v not in seen:
            seen.add(v); out.append(v)
    return out


def _expand_wildcards(query: str) -> str:
    """Replace wildcards with OR of variations."""
    def replace_wildcard(match):
        term = match.group(0)
        variations = _expand_wildcard_term(term)
        return f"({' OR '.join(variations)})"

    return _WILDCARD_RE.sub(replace_wildcard, query)


def _intelligent_keyword_extraction(query: str) -> str:
    """Extract the most important keywords from complex query for basic search."""
    # Extract quoted phrases (highest priority)
    quoted_phrases = re.findall(r'"([^"]+)"', query)

    # Extract wildcard terms and expand them
    wildcard_terms = _WILDCARD_RE.findall(query)
    expanded_wildcards = []
    for wt in wildcard_terms:
        expanded_wildcards.extend(_expand_wildcard_term(wt)[:2])

    # Remove quotes and wildcards, then extract high-value terms
    clean_query = re.sub(r'"[^"]+"', ' ', query)
    clean_query = _WILDCARD_RE.sub(' ', clean_query)
    clean_query = _BOOL_OP_RE.sub(' ', clean_query)
    clean_query = re.sub(r'[()]', ' ', clean_query)

    # Extract meaningful terms (longer than 3 chars, not common words)
    stop_words = {'the', 'and', 'but', 'for', 'are', 'this', 'that', 'with', 'from'}
    terms = [t for t in re.split(r'\s+', clean_query) if t and len(t) > 3 and t.lower() not in stop_words]

    # Combine all important terms
    all_terms = quoted_phrases + expanded_wildcards + terms[:10]

    # Deduplicate and limit
    seen = set()
    final_terms = []
    for term in all_terms:
        if term.lower() not in seen and len(term) > 1:
            seen.add(term.lower())
            if ' ' in term:
                final_terms.append(f'"{term}"')
            else:
                final_terms.append(term)

    return ' '.join(final_terms[:15])


def _simplify_boolean_to_keywords(q: str) -> str:
    """Legacy function - now redirects to intelligent extraction."""
    return _intelligent_keyword_extraction(q)



# HTTP utilities
def backoff_get(url: str, headers: Dict[str, str] = None, params: Dict[str, Any] = None, max_tries: int = 4,
                timeout: int = 20):
    headers = headers or {}
    headers.setdefault("User-Agent", "litsearch/1.0 (+https://example.org)")
    params = params or {}
    delay = 1.0
    for i in range(max_tries):
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code == 200:
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(delay)
            delay *= 2
            continue
        r.raise_for_status()
        return r
    raise requests.RequestException("Max retries exceeded")



# Data classes
@dataclass
class Paper:
    title: str
    authors: List[str]
    year: Optional[int]
    venue: Optional[str]
    doi: Optional[str]
    url: Optional[str]
    source: str
    score: float = 0.0  # ranking score (computed later)
    id_hint: Optional[str] = None
    venue_type: Optional[str] = None
    publisher: Optional[str] = None
    event: Optional[str] = None

    def dedupe_key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower().strip()
        return "title:" + _norm_title(self.title)



# Provider base class
class Provider:
    name = "base"

    def search(self, query: str, year_from: Optional[int], year_to: Optional[int], limit: int) -> List[Paper]:
        raise NotImplementedError

    def _prepare_query(self, query: str) -> List[str]:
        """Prepare query variants for this provider."""
        adapted_query = _adapt_query_with_fallback(query, self.name)
        raw_for_combo = query.strip()

        if COMBO_ENABLED and _looks_boolean(raw_for_combo):
            queries = expand_boolean_queries_for_weak_provider(raw_for_combo)
        else:
            queries = [adapted_query]

        self._log_query_debug(adapted_query, raw_for_combo, queries)
        return queries

    def _log_query_debug(self, adapted_query: str, raw_query: str, queries: List[str]):
        """Log debug information about query preparation."""
        pass


class OpenAlexProvider(Provider):
    name = "openalex"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        queries = self._prepare_query(query)

        url = "https://api.openalex.org/works"
        filt = []
        if year_from: filt.append(f"from_publication_date:{year_from}-01-01")
        if year_to:   filt.append(f"to_publication_date:{year_to}-12-31")
        per_page = min(10, max(1, limit))
        out: List[Paper] = []

        for q_simple in queries:
            if len(out) >= limit:
                break
            page = 1
            while len(out) < limit:
                params = {
                    "search": q_simple,
                    "per_page": min(per_page, limit - len(out)),
                    "sort": "relevance_score:desc",
                    "page": page
                }
                if filt:
                    params["filter"] = ",".join(filt)
                r = backoff_get(url, params=params)
                data = r.json()
                results = data.get("results", []) or []
                if not results:
                    break
                for w in results:
                    title = _norm(w.get("title"))
                    year = w.get("publication_year") or _year_from_date(w.get("publication_date"))
                    doi = _clean_doi(w.get("doi"))
                    venue = _norm(w.get("host_venue", {}).get("display_name")) if w.get("host_venue") else None
                    url_best = w.get("primary_location", {}).get("landing_page_url") \
                               or w.get("open_access", {}).get("oa_url")
                    if not url_best and doi:
                        url_best = f"https://doi.org/{doi}"
                    authors = [a.get("author", {}).get("display_name") for a in w.get("authorships", []) if a.get("author")]
                    publisher = _norm(w.get("host_venue", {}).get("publisher")) if w.get("host_venue") else None
                    venue_type = _norm(w.get("type")) if w.get("type") else None
                    specific = _detect_source(doi, venue, publisher)
                    src_name = self.name if not specific else f"{self.name}:{specific}"
                    out.append(Paper(
                        title=title, authors=_uniq([_norm(a) for a in authors]),
                        year=year, venue=venue, doi=doi, url=url_best,
                        source=src_name, id_hint=w.get("id"),
                        venue_type=venue_type, publisher=publisher
                    ))
                page += 1
        return out[:limit]


class CrossrefProvider(Provider):
    name = "crossref"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        queries = self._prepare_query(query)
        url = "https://api.crossref.org/works"
        out: List[Paper] = []
        rows = min(1000, limit)

        for q_simple in queries:
            if len(out) >= limit:
                break

            # use cursor
            cursor = "*"

            while len(out) < limit:
                params = {
                    "query": q_simple,
                    "rows": min(rows, limit - len(out)),
                    "cursor": cursor,
                    "select": "DOI,title,author,issued,container-title,URL,type"
                }
                if year_from or year_to:
                    filt = []
                    if year_from: filt.append(f"from-pub-date:{year_from}-01-01")
                    if year_to:   filt.append(f"until-pub-date:{year_to}-12-31")
                    params["filter"] = ",".join(filt)

                r = backoff_get(url, params=params)
                msg = r.json().get("message", {})
                items = msg.get("items", []) or []
                if not items:
                    break

                for it in items:
                    title = _norm(" ".join(it.get("title", []) or []))
                    date_parts = it.get("issued", {}).get("date-parts", [])
                    year = date_parts[0][0] if date_parts and date_parts[0] else None
                    doi = _clean_doi(it.get("DOI"))
                    venue = _norm(" ".join(it.get("container-title", []) or []))
                    url_best = it.get("URL") or (f"https://doi.org/{doi}" if doi else None)

                    authors = []
                    for a in it.get("author", []) or []:
                        name = " ".join([x for x in [a.get("given"), a.get("family")] if x])
                        if not name: name = a.get("name")
                        authors.append(_norm(name))

                    publisher = _norm(it.get("publisher")) if it.get("publisher") else None
                    venue_type = _norm(it.get("type")) if it.get("type") else None
                    event_name = _norm((it.get("event") or {}).get("name")) if it.get("event") else None
                    specific = _detect_source(doi, venue, publisher)
                    src_name = self.name if not specific else f"{self.name}:{specific}"

                    out.append(Paper(
                        title=title, authors=_uniq(authors), year=year, venue=venue,
                        doi=doi, url=url_best, source=src_name,
                        venue_type=venue_type, publisher=publisher, event=event_name
                    ))

                next_cursor = msg.get("next-cursor")
                if not next_cursor:
                    break
                cursor = next_cursor

        return out[:limit]

class ArxivProvider(Provider):
    name = "arxiv"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        import xml.etree.ElementTree as ET
        base = "http://export.arxiv.org/api/query"
        ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        out: List[Paper] = []

        # Apply arXiv-specific transformations
        arxiv_query = rewrite_abstract_for_arxiv(query)
        queries = self._prepare_query(arxiv_query)

        # arXiv allows up to 30000 but we keep batch modest
        batch = min(100, max(10, limit))

        for q_simple in queries:
            if len(out) >= limit:
                break
            start = 0
            while len(out) < limit:
                params = {
                    "search_query": f"all:{q_simple}",
                    "start": start,
                    "max_results": min(batch, limit - len(out)),
                    "sortBy": "relevance",
                    "sortOrder": "descending"
                }
                r = backoff_get(base, params=params)
                root = ET.fromstring(r.text)
                entries = root.findall("a:entry", ns)
                if not entries:
                    break
                got = 0
                for entry in entries:
                    title = _norm(entry.findtext("a:title", default="", namespaces=ns))
                    link = None
                    for l in entry.findall("a:link", ns):
                        if l.attrib.get("rel") == "alternate":
                            link = l.attrib.get("href")
                    published = entry.findtext("a:published", default="", namespaces=ns)
                    year = _year_from_date(published)
                    if year is None:
                        updated = entry.findtext("a:updated", default="", namespaces=ns)
                        year = _year_from_date(updated)
                    if year_from and (year is not None) and year < year_from:
                        continue
                    if year_to and (year is not None) and year > year_to:
                        continue
                    authors = [_norm(a.findtext("a:name", default="", namespaces=ns)) for a in entry.findall("a:author", ns)]
                    doi = _clean_doi(entry.findtext("arxiv:doi", default=None, namespaces=ns))
                    if (not doi) and link:
                        doi = _doi_from_url(link)
                    out.append(Paper(
                        title=title, authors=_uniq(authors), year=year, venue="arXiv",
                        doi=doi, url=link, source=self.name,
                        venue_type="preprint", publisher="arXiv"
                    ))
                    got += 1
                if got == 0:
                    break
                start += got
        return out[:limit]


class SpringerProvider(Provider):
    name = "springer"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        api_key = os.getenv("SPRINGER_API_KEY")
        if not api_key: return []
        url = "https://api.springernature.com/meta/v2/json"
        out: List[Paper] = []
        start = 1
        page_size = min(10, limit)
        while len(out) < limit:
            # Apply Springer-specific transformations and adapt for capabilities
            q2 = rewrite_abstract_for_springer(query)
            q2 = _adapt_query_with_fallback(q2, self.name)

            params = {"q": q2, "p": min(page_size, limit - len(out)), "s": start, "api_key": api_key}
            r = backoff_get(url, params=params)
            records = r.json().get("records", []) or []
            if not records:
                break
            for rec in records:
                title = _norm(rec.get("title"))
                try:
                    pub_date = rec.get("publicationDate")
                    year = int(pub_date[:4]) if pub_date and len(pub_date) >= 4 else None
                except (ValueError, TypeError):
                    year = None
                if year_from and year and year < year_from:
                    continue
                if year_to and year and year > year_to:
                    continue
                doi = _clean_doi(rec.get("doi"))
                url_best = None
                for u in rec.get("url", []) or []:
                    if u.get("format") == "html":
                        url_best = u.get("value")
                        break
                if not url_best and rec.get("url"):
                    url_best = rec["url"][0].get("value")
                if (not doi) and url_best:
                    doi = _doi_from_url(url_best)
                authors = _uniq([_norm(a.get("creator")) for a in rec.get("creators", []) if a.get("creator")])
                publisher = _norm(rec.get("publisher")) if rec.get("publisher") else None
                venue_type = _norm(rec.get("publicationType")) if rec.get("publicationType") else None
                out.append(Paper(
                    title=title, authors=authors, year=year, venue=_norm(rec.get("publicationName")),
                    doi=doi, url=url_best or (f"https://doi.org/{doi}" if doi else None),
                    source=self.name,
                    venue_type=venue_type, publisher=publisher
                ))
            start += len(records)
        return out[:limit]


class IeeeProvider(Provider):
    name = "ieee"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        api_key = os.getenv("IEEEXPLORE_API_KEY")
        if not api_key: return []
        url = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
        out: List[Paper] = []
        start_record = 1
        page_size = min(100, max(10, limit))
        while len(out) < limit:
            # Adapt query for IEEE capabilities with fallback
            adapted_query = _adapt_query_with_fallback(query, self.name)
            params = {
                "apikey": api_key,
                "format": "json",
                "max_records": min(page_size, limit - len(out)),
                "start_record": start_record,
                "sort_order": "desc",
                "sort_field": "relevance",
                "querytext": adapted_query,
            }
            if year_from: params["start_year"] = year_from
            if year_to:   params["end_year"] = year_to
            r = backoff_get(url, params=params)
            items = r.json().get("articles", []) or []
            if not items:
                break
            for it in items:
                title = _norm(it.get("title"))
                try:
                    pub_year = it.get("publication_year") or it.get("publication_years", "")
                    year = int(pub_year) if pub_year else None
                except (ValueError, TypeError):
                    year = None
                doi = _clean_doi(it.get("doi"))
                venue = _norm(it.get("publication_title"))
                url_best = it.get("htmlLink") or (f"https://doi.org/{doi}" if doi else None)
                if (not doi) and url_best:
                    doi = _doi_from_url(url_best)
                authors = []
                for a in (it.get("authors", {}).get("authors", []) or []):
                    nm = a.get("full_name") or a.get("preferred_name")
                    if nm: authors.append(_norm(nm))
                venue_type = _norm(it.get("content_type")) if it.get("content_type") else None
                publisher = "IEEE"
                out.append(Paper(
                    title=title, authors=_uniq(authors), year=year, venue=venue,
                    doi=doi, url=url_best, source=self.name, id_hint=it.get("pdf_url"),
                    venue_type=venue_type, publisher=publisher
                ))
            start_record += len(items)
        return out[:limit]


class ScopusProvider(Provider):
    name = "scopus"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        api_key = os.getenv("SCOPUS_API_KEY")
        if not api_key:
            return []

        url = "https://api.elsevier.com/content/search/scopus"

        year_clause = []
        if year_from is not None:
            year_clause.append(f"PUBYEAR AFT {year_from - 1}")
        if year_to is not None:
            year_clause.append(f"PUBYEAR BEF {year_to + 1}")

        # Apply Scopus-specific transformations first
        scopus_q = rewrite_abstract_for_scopus(query)
        # For Scopus, apply query adaptation but preserve complex boolean structure
        scopus_q = _adapt_query_with_fallback(scopus_q, self.name)

        if year_clause:
            scopus_q = f'({scopus_q}) AND ' + ' AND '.join(year_clause)

        out: List[Paper] = []
        start = 0
        page_size = min(100, max(10, limit))

        headers = {
            "X-ELS-APIKey": api_key,
            "Accept": "application/json",
        }

        while len(out) < limit:
            params = {
                "query": scopus_q,
                "start": start,
                "count": min(page_size, limit - len(out)),
            }
            r = backoff_get(url, headers=headers, params=params)
            data = r.json() or {}
            sr = data.get("search-results", {})
            entries = sr.get("entry", []) or []
            if not entries:
                break

            got = 0
            for e in entries:
                title = _norm(e.get("dc:title"))
                date = e.get("prism:coverDate")
                year = _year_from_date(date)
                doi = _clean_doi(e.get("prism:doi"))
                venue = _norm(e.get("prism:publicationName"))

                url_best = None
                links = e.get("link", []) or []
                for lk in links:
                    if isinstance(lk, dict) and lk.get("@ref") == "scopus" and lk.get("@href"):
                        url_best = lk["@href"]
                        break
                if not url_best:
                    url_best = e.get("prism:url")
                if (not url_best) and doi:
                    url_best = f"https://doi.org/{doi}"

                authors = []
                for a in e.get("author", []) or []:
                    nm = a.get("authname") or a.get("preferred-name") or a.get("surname")
                    if nm:
                        authors.append(_norm(nm))

                venue_type = _norm(e.get("prism:aggregationType")) if e.get("prism:aggregationType") else _norm(e.get("subtypeDescription")) if e.get("subtypeDescription") else None
                publisher = _norm(e.get("dc:publisher")) if e.get("dc:publisher") else None

                out.append(Paper(
                    title=title,
                    authors=_uniq(authors),
                    year=year,
                    venue=venue,
                    doi=doi,
                    url=url_best,
                    source=self.name,
                    venue_type=venue_type,
                    publisher=publisher
                ))
                got += 1

            if got == 0:
                break
            start += got

        return out[:limit]


class AcmProvider(Provider):
    name = "acm"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        url = "https://api.crossref.org/works"
        out: List[Paper] = []
        offset = 0
        rows = min(100, max(1, limit))

        while len(out) < limit:
            # Adapt query for ACM (via Crossref) capabilities with fallback
            adapted_query = _adapt_query_with_fallback(query, self.name)
            params = {
                "query": adapted_query,
                "rows": min(rows, limit - len(out)),
                "offset": offset,
                "select": "DOI,title,author,issued,container-title,URL,type",
            }
            filt_parts = ["prefix:10.1145"]  # Association for Computing Machinery (ACM)
            if year_from:
                filt_parts.append(f"from-pub-date:{year_from}-01-01")
            if year_to:
                filt_parts.append(f"until-pub-date:{year_to}-12-31")
            params["filter"] = ",".join(filt_parts)

            r = backoff_get(url, params=params)
            msg = r.json().get("message", {})
            items = msg.get("items", []) or []
            if not items:
                break

            for it in items:
                title = _norm(" ".join(it.get("title", []) or []))
                date_parts = it.get("issued", {}).get("date-parts", [])
                year = date_parts[0][0] if date_parts and date_parts[0] else None
                doi = _clean_doi(it.get("DOI"))
                venue = _norm(" ".join(it.get("container-title", []) or []))
                url_best = it.get("URL") or (f"https://doi.org/{doi}" if doi else None)

                authors = []
                for a in it.get("author", []) or []:
                    name = " ".join([x for x in [a.get("given"), a.get("family")] if x]) or a.get("name")
                    if name:
                        authors.append(_norm(name))

                out.append(Paper(
                    title=title,
                    authors=_uniq(authors),
                    year=year,
                    venue=venue,
                    doi=doi,
                    url=url_best,
                    source="acm",
                ))

            offset += len(items)

        return out[:limit]


def rank(p: Paper, q: str) -> float:
    """Simple relevance score: title match + recentness + source weight."""
    qn = _norm_title(q)
    title = _norm_title(p.title)
    score = 0.0
    # Title token overlap
    q_tokens = set(qn.split())
    t_tokens = set(title.split())
    overlap = len(q_tokens & t_tokens) / (1.0 + len(q_tokens))
    score += 2.5 * overlap
    # Recentness (log scale, favor newer)
    if p.year:
        score += 0.6 * (1.0 / (1.0 + math.exp(-(p.year - 2018) / 3.0)))
    # DOI bonus
    if p.doi: score += 0.4
    # Source trust bonus
    src_boost = {"openalex": 0.3, "crossref": 0.2, "ieee": 0.5, "springer": 0.4, "arxiv": 0.1}
    score += src_boost.get(p.source, 0.0)
    return score


def search_all(query: str,
               year_from: Optional[int],
               year_to: Optional[int],
               limit: int,
               per_provider: int,
               sources: List[str]) -> List[Paper]:
    providers: List[Provider] = []
    for name in sources:
        cls = SUPPORTED_PROVIDERS.get(name.lower())
        if cls:
            providers.append(cls())
        else:
            print(f"[WARN] Unknown provider ignored: {name}", file=sys.stderr)

    if not providers:
        print("[WARN] No valid providers selected; nothing to search.", file=sys.stderr)
        return []

    results: List[Paper] = []
    for prov in providers:
        try:
            chunk = prov.search(query, year_from, year_to, min(per_provider, limit))
            results.extend(chunk)
        except Exception as e:
            print(f"[WARN] provider '{prov.name}' failed: {e}", file=sys.stderr)

    if not results:
        return []

    by_key: Dict[str, Paper] = {}
    for p in results:
        key = p.dedupe_key()
        if key in by_key:
            base = by_key[key]
            if not base.doi and p.doi: base.doi = p.doi
            if not base.url and p.url: base.url = p.url
            if not base.venue and p.venue: base.venue = p.venue
            if not base.year and p.year: base.year = p.year
            base.authors = _uniq(base.authors + p.authors)
            if len(p.title or "") > len(base.title or ""):
                base.title = p.title
            # Prefer more specific source tags
            if base.source and p.source:
                if ":" in p.source and ":" not in base.source:
                    base.source = p.source
            # Merge new fields: venue_type, publisher, event
            if not getattr(base, "venue_type", None) and getattr(p, "venue_type", None):
                base.venue_type = p.venue_type
            if not getattr(base, "publisher", None) and getattr(p, "publisher", None):
                base.publisher = p.publisher
            if not getattr(base, "event", None) and getattr(p, "event", None):
                base.event = p.event
        else:
            by_key[key] = p

    deduped = list(by_key.values())

    # Keep original insertion order; do not rank/sort
    return deduped[:limit]


SUPPORTED_PROVIDERS: Dict[str, type] = {
    "openalex": OpenAlexProvider,
    "crossref": CrossrefProvider,
    "arxiv": ArxivProvider,
    "springer": SpringerProvider,
    "ieee": IeeeProvider,
    "scopus": ScopusProvider,
    "acm": AcmProvider,
}


def save_jsonl(path: str, papers: List[Paper]) -> None:
    wanted = ["title", "authors", "year", "venue", "doi", "url", "venue_type", "publisher", "event", "source"]
    with open(path, "w", encoding="utf-8") as f:
        for p in papers:
            data = asdict(p)
            filtered = {k: data.get(k) for k in wanted}
            f.write(json.dumps(filtered, ensure_ascii=False) + "\n")


def save_csv(path: str, papers: List[Paper]) -> None:
    fields = ["title", "authors", "year", "venue", "doi", "url", "venue_type", "source", "specific_source"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in papers:
            row = asdict(p)
            row["authors"] = "; ".join(p.authors)
            src = row.get("source") or ""
            specific = src.split(":",1)[1] if ":" in src else src
            row["specific_source"] = specific
            w.writerow({k: row.get(k) for k in fields})


def main():
    ap = argparse.ArgumentParser(
        description="Unified scholarly search (OpenAlex, Crossref, arXiv, Springer, IEEE, Scopus, ACM-via-Crossref)."
    )
    ap.add_argument("query", nargs='?', default="", help="search string, e.g., \"extract method refactoring\"")
    ap.add_argument("--year-from", type=int, default=None, help="lower bound year (inclusive)")
    ap.add_argument("--year-to", type=int, default=None, help="upper bound year (inclusive)")
    ap.add_argument("--limit", type=int, default=50, help="max total results")
    ap.add_argument("--per-provider", type=int, default=1000, help="max per provider")
    ap.add_argument("--out-jsonl", default="results.jsonl", help="output JSONL path")
    ap.add_argument("--out-csv", default="results.csv", help="output CSV path")
    ap.add_argument("--list-sources", action="store_true",
                    help="list available sources and exit")
    ap.add_argument(
        "--sources",
        default="openalex,crossref,arxiv,springer,ieee,scopus,acm",
        help="comma-separated providers to use (default: all). Choices: openalex,crossref,arxiv,springer,ieee,scopus,acm"
    )
    ap.add_argument("--no-combo", action="store_true", help="Disable boolean combination expansion on weak providers (OpenAlex/Crossref/arXiv)")
    args = ap.parse_args()

    global COMBO_ENABLED
    COMBO_ENABLED = not args.no_combo

    # Support inline PY range, e.g., PY=(2022-2025) in the query string
    cleaned_q, py_from, py_to = extract_years_from_query(args.query)
    if py_from is not None and args.year_from is None:
        args.year_from = py_from
    if py_to is not None and args.year_to is None:
        args.year_to = py_to
    args.query = cleaned_q

    if args.list_sources:
        print("Available sources:", ", ".join(SUPPORTED_PROVIDERS.keys()))
        return
        
    # if args.test_adaptation:
    #     test_complex_query_adaptation()
    #     return

    selected_sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]

    papers = search_all(
        query=args.query,
        year_from=args.year_from,
        year_to=args.year_to,
        limit=args.limit,
        per_provider=args.per_provider,
        sources=selected_sources,
    )

    save_csv(args.out_csv, papers)
    print(f"✅ Saved {len(papers)} results to:\n - {args.out_csv}")


if __name__ == "__main__":
    main()
