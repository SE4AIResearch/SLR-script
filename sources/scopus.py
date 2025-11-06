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
parser.add_argument("--year-from", type=int, help="Filter to items published on/after this year (inclusive)")
parser.add_argument("--year-to", type=int, help="Filter to items published on/before this year (inclusive)")
parser.add_argument("--output", help="Output CSV file path (default: Scopus_with_abstracts.csv)")
parser.add_argument("--api-key", dest="api_key", help="Elsevier/Scopus API key. Overrides hardcoded key or ENV ELSEVIER_API_KEY")
parser.add_argument("--normalize-query", action="store_true", help="Normalize query: collapse whitespace/newlines")
parser.add_argument("--use-alt-abstracts", action="store_true", default=True,
                    help="Use alternative sources for abstracts")
parser.add_argument("--no-enhance", action="store_true",
                    help="Disable alternative abstract fetching (override --use-alt-abstracts)")
args = parser.parse_args()

if getattr(args, "no_enhance", False):
    args.use_alt_abstracts = False

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
# Expanded set of Scopus field functions we will preserve verbatim
_SCOPUS_FIELD_NAMES = [
    # Core text fields
    "TITLE-ABS-KEY", "TITLE", "ABS", "KEY", "ALL",
    # Author / affiliation
    "AUTH", "AUTHOR-NAME", "AUTHLASTNAME", "AUTHFIRST", "AU-ID",
    "AFFIL", "AF-ID", "AFFILCITY", "AFFILCOUNTRY",
    # Source / venue
    "SRCTITLE", "EXACTSRCTITLE",
    # Identifiers / metadata
    "EID", "DOI", "ISSN", "ISBN", "ORCID",
    # Year / type / subject
    "PUBYEAR", "DOCTYPE", "SUBJAREA", "INDEXTERMS",
    # Organization
    "ORG", "ORGID", "ORGNAME"
]
_SCOPUS_FIELD_RE = re.compile(
    rf"\b({'|'.join(map(re.escape, _SCOPUS_FIELD_NAMES))})\s*\(",
    re.IGNORECASE
)
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
        if _SCOPUS_FIELD_RE.search(seg_text):
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
        if _SCOPUS_FIELD_RE.search(tok):
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


def _build_year_clause(y_from: int | None, y_to: int | None) -> str | None:
    """Return a Scopus query fragment implementing an inclusive year range.
    Uses LIMIT-TO(PUBYEAR,YYYY) when the span is short (<=20 years) for
    maximum compatibility; otherwise falls back to AFT/BEF.
    """
    if not y_from and not y_to:
        return None

    # If both bounds are present and small span, enumerate years
    if y_from and y_to and y_from <= y_to and (y_to - y_from + 1) <= 20:
        yrs = [f"LIMIT-TO(PUBYEAR,{y})" for y in range(y_from, y_to + 1)]
        return "(" + " OR ".join(yrs) + ")"

    # Otherwise, use AFT/BEF with inclusive adjustment
    parts = []
    if y_from:
        parts.append(f"PUBYEAR AFT {y_from - 1}")  # >= y_from
    if y_to:
        parts.append(f"PUBYEAR BEF {y_to + 1}")    # <= y_to
    return "(" + " AND ".join(parts) + ")"


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

        # Check if Scopus reports a 'next' link
        links = data.get('search-results', {}).get('link', []) or []
        has_next = any((isinstance(link, dict) and link.get('@ref') == 'next') for link in links)
        if not has_next:
            break

        # Compute remaining and decide next page size (never exceed initial per_page)
        remaining = None
        if total_limit is not None:
            remaining = max(0, total_limit - fetched)
            if remaining <= 0:
                break
            next_count = max(1, min(per_page, remaining))
        else:
            next_count = per_page

        # Advance start by the *previous* page size or actual returned rows to avoid big jumps
        prev_count = params.get('count', per_page)
        # Be defensive: if API returned fewer entries than requested, advance by that number
        advance_by = len(entries) if len(entries) < prev_count else prev_count
        params['start'] += advance_by

        # Set the next page size
        params['count'] = next_count

    pbar.close()
    return all_data


def parse_metadata(entries):
    skipped_no_doi = skipped_no_year_with_bounds = skipped_year_out = 0

    skipped_year_out += 1

    skipped_no_year_with_bounds += 1


    papers_metadata = []

    print("\n[INFO] Processing papers and fetching abstracts from alternative sources...")

    for entry in tqdm(entries, desc="Processing papers", unit="paper"):
        title = entry.get('dc:title', 'N/A')
        doi = entry.get('prism:doi', 'N/A')


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

        # Post-filter by year (inclusive) as a safety net
        try:
            year_int = int(year) if year and year != 'N/A' else None
        except ValueError:
            year_int = None

        yf = getattr(args, 'year_from', None)
        yt = getattr(args, 'year_to', None)
        if year_int is not None:
            if yf is not None and year_int < yf:
                skipped_year_out += 1
                continue
            if yt is not None and year_int > yt:
                skipped_year_out += 1
                continue
        elif yf is not None or yt is not None:
            skipped_no_year_with_bounds += 1
            # If year unknown and bounds requested, skip conservatively
            continue

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

        # 末尾打印
    print(f"[DEBUG] Post-filter stats:"
          f"no_year_with_bounds={skipped_no_year_with_bounds}, "
          f"year_out_of_range={skipped_year_out}")

    return papers_metadata


# ============= Main Execution =============

# Process query
query = args.query
if args.normalize_query:
    query = _normalize_query_string(query)
    print(f"[DEBUG] Normalized query: {query}")

query = _normalize_scopus_query(query)

year_clause = _build_year_clause(args.year_from, args.year_to)
if year_clause:
    query = f"{query} AND {year_clause}"

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

if args.year_from or args.year_to:
    print(f"[INFO] Year filter applied: from {args.year_from or '-'} to {args.year_to or '-'} (inclusive)")

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