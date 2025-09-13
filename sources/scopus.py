import requests
import pandas as pd
from tqdm import tqdm
import re
import argparse
import time
from requests import PreparedRequest
from abstract import fetch_abstract

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
    'count': 25,
    'field': ('dc:title,prism:doi,dc:creator,author,prism:coverDate,prism:url,link,eid,'
              'dc:description,prism:publicationName,prism:aggregationType,subtype,subtypeDescription,'
              'citedby-count,prism:volume,prism:issueIdentifier,prism:pageRange,authkeywords'),
    'httpAccept': 'application/json',
}

parser = argparse.ArgumentParser(description="Search Scopus and export CSV (title, authors, year, doi, url)")
parser.add_argument("query", help="Scopus search query string, e.g., TITLE-ABS-KEY((refactor*) AND (LLM))")
parser.add_argument("--count", type=int, default=25, help="Items per page (1-200), default 25")
parser.add_argument("--limit", type=int, help="Optional max total items to fetch across pages. If omitted, fetch all available.")
parser.add_argument("--output", help="Output CSV file path (default: Scopus_with_abstracts.csv)")
parser.add_argument("--api-key", dest="api_key", help="Elsevier/Scopus API key. Overrides hardcoded key or ENV ELSEVIER_API_KEY")
parser.add_argument("--normalize-query", action="store_true", help="Normalize query: collapse whitespace/newlines")
parser.add_argument("--use-alt-abstracts", action="store_true", default=True,
                    help="Use alternative sources for abstracts")
args = parser.parse_args()

# Resolve API key precedence: CLI > ENV > hardcoded
import os as _os
_api_from_env = _os.getenv("ELSEVIER_API_KEY")
if args.api_key:
    API_KEY = args.api_key
elif _api_from_env:
    API_KEY = _api_from_env
# Ensure headers use the final API_KEY
headers['X-ELS-APIKey'] = API_KEY

print(f"[INFO] Using Scopus API key: {'provided via --api-key' if args.api_key else ('from ENV' if _api_from_env else 'hardcoded')}\n")

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
    """Tokenize a boolean expression into a list of tokens while respecting quotes and parentheses."""
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

    # Merge case-insensitive operators
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
    """Rebuild tokens, quoting multi-word terms between operators/parentheses."""
    out = []
    seg = []

    def flush_segment():
        nonlocal seg
        if not seg:
            return
        seg_text = ' '.join(seg).strip()

        # If already quoted or proximity operator, keep as-is
        if any((t.startswith('"') and t.endswith('"')) for t in seg) or _is_proximity(seg_text):
            out.extend(seg)
            seg = []
            return

        # If contains field function, keep as-is
        if re.search(r"\b(TITLE-ABS-KEY|TITLE|ABS|KEY)\s*\(", seg_text, re.IGNORECASE):
            out.extend(seg)
            seg = []
            return

        # Multi-token: quote as phrase
        if any(ch.isspace() for ch in seg_text):
            out.append(f'"{seg_text}"')
            seg = []
            return

        # Single-token: quote it
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


def _has_scopus_field(q: str) -> bool:
    return bool(_SCOPUS_FIELD_RE.search(q or ""))


def _normalize_scopus_query(q: str) -> str:
    if not q:
        return q

    # Normalize whitespace and hyphen-newline
    s = q.replace("\r", "")
    s = re.sub(r"-\s*\n\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\u2010-\u2015]", "-", s)

    # Ensure field wrapper
    if not _has_scopus_field(s):
        s = f"TITLE-ABS-KEY({s})"

    # Process inner content
    m = re.search(r"\bTITLE-ABS-KEY\s*\((.*)\)\s*$", s, flags=re.I)
    if m:
        inner = m.group(1)
        inner = _auto_quote_phrases(inner)
        s = f"TITLE-ABS-KEY({inner})"
    else:
        s = _auto_quote_phrases(s)

    return s


def _build_url(base: str, params: dict) -> str:
    pr = PreparedRequest()
    pr.prepare_url(base, params)
    return pr.url


def get_abstract_multi_source(entry, use_alt=True):
    """Enhanced abstract fetching using universal fetcher"""
    # First try Scopus's own abstract
    abstract = entry.get('dc:description')

    if (not abstract or abstract == 'N/A') and use_alt:
        title = entry.get('dc:title', '')
        doi = entry.get('prism:doi', '')

        # Use universal fetcher
        abstract = fetch_abstract(
            title=title,
            doi=doi,
            verbose=False
        )

    return abstract if abstract else 'N/A'


# ============= Main Functions =============

def _extract_authors(entry):
    names = []
    # Try structured author list
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

    # Fallback to dc:creator
    if not names and entry.get('dc:creator'):
        raw = entry.get('dc:creator') or ""
        parts = [p.strip() for p in re.split(r';|,', raw) if p.strip()]
        names.extend(parts)

    return names


def get_metadata(base_url, params, headers, total_limit=None):
    all_data = []
    pbar = tqdm(desc="Fetching papers from Scopus", unit="paper")
    fetched = 0
    per_page = max(1, min(200, params.get('count', 25)))

    while True:
        debug_url = _build_url(base_url, params)
        print(f"[DEBUG] GET {debug_url}")
        response = requests.get(base_url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()

        if params.get('start', 0) == 0:
            total = data.get('search-results', {}).get('opensearch:totalResults')
            if total is not None:
                print(f"[INFO] Total results found: {total}")

        entries = data['search-results']['entry']
        all_data.extend(entries)
        fetched += len(entries)
        pbar.update(len(entries))

        # Respect total_limit if provided
        if total_limit is not None and fetched >= total_limit:
            break

        links = data.get('search-results', {}).get('link', []) or []
        has_next = any((isinstance(link, dict) and link.get('@ref') == 'next') for link in links)
        if not has_next:
            break
        # If we have a limit, compute remaining and adjust page size
        if total_limit is not None:
            remaining = max(0, total_limit - fetched)
            if remaining <= 0:
                break
            params['count'] = max(1, min(200, remaining))
        # Advance start
        params['start'] += params['count']

    pbar.close()
    return all_data


def parse_metadata(entries):
    papers_metadata = []

    print("\n[INFO] Processing papers and fetching abstracts from alternative sources...")

    for entry in tqdm(entries, desc="Processing papers", unit="paper"):
        title = entry.get('dc:title', 'N/A')
        doi = entry.get('prism:doi', 'N/A')

        # Skip if no DOI
        if doi == 'N/A':
            continue

        # Get abstract from multiple sources
        abstract = get_abstract_multi_source(entry, use_alt=args.use_alt_abstracts)
        if abstract != 'N/A':
            abstract = re.sub(r'\s+', ' ', abstract).strip()

        venue = entry.get('prism:publicationName', 'N/A')

        # Determine content type
        agg_type = entry.get('prism:aggregationType', '')
        subtype_desc = entry.get('subtypeDescription', '')
        subtype_code = entry.get('subtype', '')

        if subtype_desc:
            content_type = subtype_desc
        else:
            content_type_map = {
                'ar': 'Article',
                'cp': 'Conference Paper',
                're': 'Review',
                'ch': 'Book Chapter',
                'bk': 'Book',
                'ed': 'Editorial',
                'le': 'Letter',
                'no': 'Note',
                'sh': 'Short Survey',
                'er': 'Erratum',
                'ip': 'Article in Press',
                'cr': 'Conference Review',
                'ab': 'Abstract Report'
            }
            content_type = content_type_map.get(subtype_code, agg_type or 'N/A')

        citations = entry.get('citedby-count', '0')
        keywords = entry.get('authkeywords', 'N/A')

        # Build URL
        url_best = None
        prism_url = entry.get('prism:url', '')
        if prism_url and 'scopus_id/' in prism_url:
            scopus_id = prism_url.split('scopus_id/')[-1]
            url_best = f"https://www.scopus.com/record/display.uri?eid=2-s2.0-{scopus_id}&origin=resultslist"

        if not url_best:
            eid = entry.get('eid', '')
            if eid:
                url_best = f"https://www.scopus.com/record/display.uri?eid={eid}&origin=resultslist"

        if not url_best and doi and doi != 'N/A':
            url_best = f"https://doi.org/{doi}"

        authors = _extract_authors(entry)
        year = entry.get('prism:coverDate', '')[:4] if entry.get('prism:coverDate') else 'N/A'

        metadata = {
            'title': title,
            'authors': ", ".join(authors) if authors else 'N/A',
            'published date': year,
            'url': url_best or 'N/A',
            'content_type': content_type,
            'DOI': doi,
            'abstract': abstract,
            'venue': venue,
            'keywords': keywords,
            'citations': citations,
            'abstract_source': 'Semantic Scholar/CrossRef' if abstract != 'N/A' else 'Not available'
        }
        papers_metadata.append(metadata)

    return papers_metadata


# ============= Main Execution =============

# Process query
query = args.query
if args.normalize_query:
    query = _normalize_query_string(query)
    print(f"[DEBUG] Normalized query: {query}")

query = _normalize_scopus_query(query)
print(f"[DEBUG] Final Scopus query: {query}")
params['query'] = query
params['count'] = max(1, min(200, args.count))

# Get metadata
print("\n[INFO] Starting Scopus search...")
entries = get_metadata(base_url, params, headers, total_limit=args.limit)

print(f"\n[INFO] Found {len(entries)} papers in Scopus")
papers_metadata = parse_metadata(entries)

# Convert to DataFrame and save
df = pd.DataFrame(papers_metadata)

column_order = ['title', 'authors', 'published date', 'url', 'content_type', 'DOI',
                'abstract', 'venue', 'keywords', 'citations', 'abstract_source']

df = df[[col for col in column_order if col in df.columns]]

# Save to CSV
output_file = args.output or 'Scopus_with_abstracts.csv'
df.to_csv(output_file, index=False)

# Statistics
total_papers = len(papers_metadata)
papers_with_abstract = df[df['abstract'] != 'N/A'].shape[0]
abstract_rate = (papers_with_abstract / total_papers * 100) if total_papers > 0 else 0

print(f"\n{'=' * 60}")
print(f"SUMMARY:")
print(f"{'=' * 60}")
print(f"Total papers extracted: {total_papers}")
print(f"Papers with abstracts: {papers_with_abstract} ({abstract_rate:.1f}%)")
print(f"Output saved to: {output_file}")
print(f"{'=' * 60}")