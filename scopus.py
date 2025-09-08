import re
import json
import requests
import pandas as pd
from tqdm import tqdm
import re
from bs4 import BeautifulSoup
import argparse
import os
from requests import PreparedRequest

# Scopus API key
API_KEY = 'a17167505f5d6799ad4cf9c9f28de7f1'

# Base URL for Scopus API
base_url = "https://api.elsevier.com/content/search/scopus"

# Headers for the API request
headers = {
    'X-ELS-APIKey': API_KEY,
    'Accept': 'application/json'
}

# Parameters for the API request
params = {
    'query': '',
    'view': 'STANDARD',
    'start': 0,
    'count': 25,  # Initial count to handle pagination
    'field': 'dc:title,prism:doi,dc:creator,author,prism:coverDate,prism:url,link',
    'httpAccept': 'application/json',
}

parser = argparse.ArgumentParser(description="Search Scopus and export CSV (title, authors, year, doi, url)")
parser.add_argument("query", help="Scopus search query string, e.g., TITLE-ABS-KEY((refactor*) AND (LLM))")
parser.add_argument("--count", type=int, default=25, help="Items per page (1-200), default 25")
parser.add_argument("--normalize-query", action="store_true", help="Normalize query: collapse whitespace/newlines and fix hyphen+newline issues")
args = parser.parse_args()

def _normalize_query_string(q: str) -> str:
    if not q:
        return q
    s = re.sub(r"\s+", " ", q).strip()
    s = re.sub(r"[\-\u2010-\u2015]\s+(?=\w)", " ", s)
    return s

# --- Scopus query normalization helpers ---
_SCOPUS_FIELD_RE = re.compile(r"\b(TITLE-ABS-KEY|TITLE|ABS|KEY)\s*\(", re.IGNORECASE)

_OPERATOR_SET = {"AND", "OR", "NOT"}


def _is_proximity(tok: str) -> bool:
    return bool(re.fullmatch(r"W/\d+", tok.strip(), flags=re.IGNORECASE))


def _tokenize_boolean(expr: str):
    """Tokenize a boolean expression into a list of tokens while respecting quotes and parentheses.
    Returns a list of strings: operators (AND/OR/NOT), parens '(', ')', and raw tokens.
    """
    tokens = []
    i, n = 0, len(expr)
    in_quote = False
    buf = []
    while i < n:
        ch = expr[i]
        if ch == '"':
            buf.append(ch)
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote:
            if ch in '()':
                if buf:
                    tok = ''.join(buf).strip()
                    if tok:
                        tokens.append(tok)
                    buf = []
                tokens.append(ch)
                i += 1
                continue
            if ch.isspace():
                if buf:
                    tok = ''.join(buf).strip()
                    if tok:
                        tokens.append(tok)
                    buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    if buf:
        tok = ''.join(buf).strip()
        if tok:
            tokens.append(tok)

    # Merge case-insensitive operators appearing as standalone tokens
    merged = []
    for tok in tokens:
        up = tok.upper()
        if up in _OPERATOR_SET:
            merged.append(up)
        else:
            merged.append(tok)
    return merged


def _is_operator(tok: str) -> bool:
    return tok.upper() in _OPERATOR_SET or _is_proximity(tok)


def _rebuild_with_generic_quotes(tokens):
    """Rebuild tokens, quoting any multi-word term segments between operators/parentheses.
    - Preserves already-quoted tokens
    - Skips quoting segments that contain proximity operators like W/1
    - Does not touch field functions (TITLE-ABS-KEY(...), etc.)
    """
    out = []
    seg = []

    def flush_segment():
        nonlocal seg
        if not seg:
            return
        seg_text = ' '.join(seg).strip()

        # If any token already quoted, or the combined segment looks like proximity, keep as-is
        if any((t.startswith('"') and t.endswith('"')) for t in seg) or _is_proximity(seg_text):
            out.extend(seg)
            seg = []
            return

        # If the segment contains a field function, keep as-is
        if re.search(r"\b(TITLE-ABS-KEY|TITLE|ABS|KEY)\s*\(", seg_text, re.IGNORECASE):
            out.extend(seg)
            seg = []
            return

        # Multi-token (contains whitespace when joined): quote as a phrase
        if any(ch.isspace() for ch in seg_text):
            out.append(f'"{seg_text}"')
            seg = []
            return

        # Single-token segment: quote everything (including wildcard tokens and proximity operators),
        # but skip quoting field functions.
        tok = seg[0]
        if re.search(r"\b(TITLE-ABS-KEY|TITLE|ABS|KEY)\s*\(", tok, re.IGNORECASE):
            out.append(tok)
        else:
            out.append(f'"{tok}"')
        seg = []

    for tok in tokens:
        if tok in ('(', ')'):
            flush_segment()
            out.append(tok)
        elif _is_operator(tok):
            flush_segment()
            out.append(tok)
        else:
            seg.append(tok)
    flush_segment()
    return out


def _auto_quote_phrases(s: str) -> str:
    tokens = _tokenize_boolean(s)
    tokens = _rebuild_with_generic_quotes(tokens)
    return ' '.join(tokens)


# Convert simple two-term adjacency with wildcard into Scopus proximity `W/1`
_ADJ_WILDCARD_PATTERNS = [
    (re.compile(r"\bmethod\s+extract\*", re.I), "method W/1 extract*"),
    (re.compile(r"\bextract\s+method\b", re.I), '"extract method"'),
    (re.compile(r"\bmethod\s+split\*", re.I), "method W/1 split*"),
    (re.compile(r"\bfunction\s+extract\*", re.I), "function W/1 extract*"),
    (re.compile(r"\bfunction\s+split\*", re.I), "function W/1 split*"),
]


def _apply_adj_wildcard_fixes(s: str) -> str:
    for rx, repl in _ADJ_WILDCARD_PATTERNS:
        s = rx.sub(repl, s)
    return s


def _has_scopus_field(q: str) -> bool:
    return bool(_SCOPUS_FIELD_RE.search(q or ""))


def _normalize_scopus_query(q: str) -> str:
    if not q:
        return q
    # 1) Normalize whitespace and hyphen+newline artifacts
    s = q.replace("\r", "")
    s = re.sub(r"-\s*\n\s*", "", s)  # join hyphenated line breaks
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\u2010-\u2015]", "-", s)  # normalize unicode hyphens

    # 2) Ensure field wrapper
    if not _has_scopus_field(s):
        s = f"TITLE-ABS-KEY({s})"

    # 3) Work on the inner content of TITLE-ABS-KEY(...)
    m = re.search(r"\bTITLE-ABS-KEY\s*\((.*)\)\s*$", s, flags=re.I)
    if m:
        inner = m.group(1)
        inner = _apply_adj_wildcard_fixes(inner)
        inner = _auto_quote_phrases(inner)
        s = f"TITLE-ABS-KEY({inner})"
    else:
        # Fallback: still apply fixes on full string
        s = _apply_adj_wildcard_fixes(s)
        s = _auto_quote_phrases(s)

    return s


def _build_url(base: str, params: dict) -> str:
    pr = PreparedRequest();
    pr.prepare_url(base, params);
    return pr.url


# Override params based on user input, normalize query for Scopus
query = args.query
if args.normalize_query:
    query = _normalize_query_string(query)
    print(f"[DEBUG] Normalized query: {query}")
query = _normalize_scopus_query(query)
print(f"[DEBUG] Final Scopus query: {query}")
params['query'] = query
# for test purpose
# params['query'] = 'TITLE-ABS-KEY((("extract method" OR "extract-method" OR "method W/1 extract*" OR "method-extract*" OR "extract function" OR "extractfunction" OR "function W/1 extract*" OR "function-extract*" OR "split method" OR "split-method" OR "method W/1 split*" OR "methodsplit*" OR "split function" OR "split-function" OR "function W/1 split*" OR "function-split*" OR "separat* method" OR "separat*method" OR "method separat*" OR "method-separat*" OR "separat* function" OR "separate-function" OR "function separat*" OR "function-separat*") AND ("long method" OR "long function" OR "large method" OR "large function" OR "duplicat* code" OR "code duplicat*" OR "code clone" OR "code bad smell" OR "code smell" OR "bad smell" OR "antipattern" OR "anti-patter" OR "design defect" OR "design flaw") AND ("refactor*") AND ("approach" OR "tool" OR "technique")))'
params['count'] = max(1, min(200, args.count))


def _extract_authors(entry):
    names = []
    # Preferred rich list (STANDARD/COMPLETE may return this)
    for a in entry.get('author', []) or []:
        name = a.get('authname')
        if not name:
            pn = a.get('preferred-name') or {}
            gn = pn.get('given-name')
            sn = pn.get('surname')
            if gn or sn:
                name = " ".join([x for x in [gn, sn] if x])
        if not name:
            name = a.get('ce:indexed-name') or a.get('$')
        if name:
            names.append(name)
    # Fallback: dc:creator is a single string like "Alice; Bob; Carol"
    if not names and entry.get('dc:creator'):
        raw = entry.get('dc:creator') or ""
        # split on ';' first, then commas as fallback
        parts = [p.strip() for p in re.split(r';|,', raw) if p.strip()]
        names.extend(parts)
    return names


# Function to get metadata
def get_metadata(base_url, params, headers):
    all_data = []
    pbar = tqdm(desc="Fetching papers", unit="paper")
    while True:
        debug_url = _build_url(base_url, params)
        print(f"[DEBUG] GET {debug_url}")
        response = requests.get(base_url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        if params.get('start', 0) == 0:
            total = data.get('search-results', {}).get('opensearch:totalResults')
            if total is not None:
                print(f"[DEBUG] Scopus totalResults={total}")
        entries = data['search-results']['entry']
        all_data.extend(entries)
        pbar.update(len(entries))
        links = data.get('search-results', {}).get('link', []) or []
        if any((isinstance(link, dict) and link.get('@ref') == 'next') for link in links):
            params['start'] += params['count']
        else:
            break
    pbar.close()
    return all_data


# Function to parse the metadata
def parse_metadata(entries):
    papers_metadata = []
    for entry in tqdm(entries, desc="Processing papers", unit="paper"):
        title = entry.get('dc:title')
        doi = entry.get('prism:doi', 'N/A')
        # Choose the best URL: prefer DOI landing, then prism:url, then Scopus link
        url_best = None
        if doi and doi != 'N/A':
            url_best = f"https://doi.org/{doi}"
        if not url_best:
            url_best = entry.get('prism:url')
        if not url_best:
            links = entry.get('link', []) or []
            # Prefer the scopus landing link
            for lk in links:
                if lk.get('@ref') == 'scopus' and lk.get('@href'):
                    url_best = lk.get('@href')
                    break
            if not url_best and links:
                # Fallback to the first link href if available
                for lk in links:
                    if lk.get('@href'):
                        url_best = lk.get('@href')
                        break
        if doi == 'N/A':
            continue
        authors = _extract_authors(entry)
        year = entry.get('prism:coverDate', '')[:4] if entry.get('prism:coverDate') else 'N/A'
        metadata = {
            'title': title,
            'authors': ", ".join(authors) if authors else 'N/A',
            'year': year,
            'doi': doi,
            'url': url_best or 'N/A'
        }
        papers_metadata.append(metadata)
    return papers_metadata


# Function to calculate number of pages from page range
def calculate_num_pages(page_range):
    if page_range and '-' in page_range:
        start, end = page_range.split('-')
        return int(end) - int(start) + 1
    return 4  # Default to 4 pages if page range is not available


# Get metadata
entries = get_metadata(base_url, params, headers)
papers_metadata = parse_metadata(entries)

# Convert to DataFrame
df = pd.DataFrame(papers_metadata)

# Save to CSV
df.to_csv('Scopus.csv', index=False)

print(f"Metadata extraction complete. {len(papers_metadata)} papers extracted. Check the Scopus.csv file.")
