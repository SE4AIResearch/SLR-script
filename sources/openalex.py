#!/usr/bin/env python3
"""
Enhanced OpenAlex Paper Crawler with Universal Abstract Fetching
Fetches papers from OpenAlex with comprehensive metadata including enhanced abstracts
Fixed version with proper URL, publisher, and venue extraction
"""

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Optional, Tuple
import requests

# Import the abstract fetcher (assumes both files are in same directory)
try:
    from abstract import UniversalAbstractFetcher

    ABSTRACT_FETCHER_AVAILABLE = True
except ImportError:
    print("Warning: abstract.py not found. Abstract enhancement disabled.", file=sys.stderr)
    ABSTRACT_FETCHER_AVAILABLE = False

# Configuration
OPENALEX_BASE_URL = "https://api.openalex.org/works"
MAX_PER_PAGE = 200  # OpenAlex maximum
DEFAULT_PER_PAGE = 100
COMBO_ENABLED = True  # Enable boolean query expansion


@dataclass
class Paper:
    """Data class for paper metadata"""
    title: str
    authors: List[str]
    year: Optional[int]
    venue: Optional[str]
    doi: Optional[str]
    url: Optional[str]
    source: str
    score: float = 0.0
    abstract: Optional[str] = None
    abstract_source: Optional[str] = None  # Track where abstract came from
    abstract_confidence: float = 0.0  # Confidence score for fetched abstracts
    keywords: List[str] = field(default_factory=list)
    citations: Optional[int] = None
    content_type: Optional[str] = None
    publisher: Optional[str] = None
    published_date: Optional[str] = None
    openalex_id: Optional[str] = None
    is_open_access: bool = False
    institutions: List[str] = field(default_factory=list)
    referenced_works: List[str] = field(default_factory=list)
    concepts: List[Dict[str, Any]] = field(default_factory=list)


# Utility functions
def _norm(s: Optional[str]) -> str:
    """Normalize string by collapsing whitespace"""
    return re.sub(r'\s+', ' ', (s or '')).strip()


def _norm_title(s: Optional[str]) -> str:
    """Normalize title for deduplication"""
    s = (s or '').lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _year_from_date(d: Optional[str]) -> Optional[int]:
    """Extract year from date string"""
    if not d:
        return None
    m = re.match(r'^(\d{4})', d)
    return int(m.group(1)) if m else None


def _clean_doi(x: Optional[str]) -> Optional[str]:
    """Clean and normalize DOI"""
    if not x:
        return None
    x = x.strip().lower()
    x = re.sub(r'^https?://(dx\.)?doi\.org/', '', x)
    return x or None


def _reconstruct_abstract(inverted_index: Dict[str, List[int]]) -> Optional[str]:
    """Reconstruct abstract text from OpenAlex inverted index"""
    if not inverted_index:
        return None

    # Create position to word mapping
    word_positions = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))

    # Sort by position and reconstruct
    word_positions.sort(key=lambda x: x[0])
    abstract = " ".join([word for _, word in word_positions])

    # Clean up
    abstract = re.sub(r'\s+([.,;!?])', r'\1', abstract)  # Fix punctuation spacing
    abstract = re.sub(r'\s+', ' ', abstract).strip()

    return abstract if len(abstract) > 50 else None  # Filter out very short abstracts


def backoff_get(url: str, headers: Dict[str, str] = None, params: Dict[str, Any] = None,
                max_tries: int = 4, timeout: int = 20) -> requests.Response:
    """HTTP GET with exponential backoff retry"""
    headers = headers or {}
    headers.setdefault("User-Agent", "OpenAlexCrawler/1.0 (+https://example.org)")
    params = params or {}
    delay = 1.0

    for i in range(max_tries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.Timeout:
            if i == max_tries - 1:
                raise
            time.sleep(delay)
            delay *= 2

    raise requests.RequestException("Max retries exceeded")


def extract_years_from_query(q: str) -> Tuple[str, Optional[int], Optional[int]]:
    """Extract PY=(YYYY-YYYY) from query if present"""
    if not q:
        return q, None, None

    py_pattern = re.compile(r"\bPY\s*=\s*\(?\s*(\d{4})\s*-\s*(\d{4})\s*\)?", re.IGNORECASE)
    m = py_pattern.search(q)
    if not m:
        return q, None, None

    y1, y2 = int(m.group(1)), int(m.group(2))
    cleaned = py_pattern.sub(" ", q).strip()
    return cleaned, min(y1, y2), max(y1, y2)


def expand_boolean_queries_for_openalex(q: str, max_combos: int = 24) -> List[str]:
    """Expand complex boolean query into multiple simpler queries"""
    if not q:
        return []

    # Normalize query
    q_norm = q.replace("\r", "")
    q_norm = re.sub(r"-\s*\n\s*", "", q_norm)
    q_norm = re.sub(r"\s+", " ", q_norm).strip()

    # For simplicity, returning just the normalized query
    # Full implementation would expand OR operations into multiple queries
    return [q_norm]


def _looks_boolean(q: str) -> bool:
    """Check if query looks like boolean search"""
    if not q:
        return False
    bool_pattern = re.compile(r'\b(AND|OR|NOT|ANDNOT)\b', re.IGNORECASE)
    if bool_pattern.search(q):
        return True
    return any(ch in q for ch in '()')


class OpenAlexCrawler:
    """Enhanced OpenAlex paper crawler with abstract fetching"""

    def __init__(self, enhance_abstracts: bool = True, verbose: bool = False):
        """
        Initialize crawler

        Args:
            enhance_abstracts: Whether to fetch missing abstracts from other sources
            verbose: Print debug information
        """
        self.base_url = OPENALEX_BASE_URL
        self.per_page = DEFAULT_PER_PAGE
        self.enhance_abstracts = enhance_abstracts and ABSTRACT_FETCHER_AVAILABLE
        self.verbose = verbose

        # Initialize abstract fetcher if available and enabled
        if self.enhance_abstracts:
            self.abstract_fetcher = UniversalAbstractFetcher(
                use_cache=True,
                delay=0.3,  # Be respectful to APIs
                verbose=verbose
            )
            if verbose:
                print("✓ Abstract enhancement enabled", file=sys.stderr)
        else:
            self.abstract_fetcher = None
            if verbose and not ABSTRACT_FETCHER_AVAILABLE:
                print("✗ Abstract enhancement disabled (abstract_fetcher not available)", file=sys.stderr)

    def search(self, query: str, year_from: Optional[int] = None,
               year_to: Optional[int] = None, limit: int = 100) -> List[Paper]:
        """
        Search OpenAlex for papers with enhanced abstracts

        Args:
            query: Search query (supports boolean operators)
            year_from: Start year (inclusive)
            year_to: End year (inclusive)
            limit: Maximum number of results

        Returns:
            List of Paper objects with enhanced metadata
        """
        # Build filters
        filters = []
        if year_from:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to:
            filters.append(f"to_publication_date:{year_to}-12-31")

        # Handle boolean queries
        if COMBO_ENABLED and _looks_boolean(query):
            queries = expand_boolean_queries_for_openalex(query)
            if self.verbose:
                print(f"Expanded complex query into {len(queries)} combinations", file=sys.stderr)
        else:
            queries = [query]

        all_results = []

        for q_idx, q_simple in enumerate(queries):
            if len(all_results) >= limit:
                break

            if self.verbose:
                print(f"Searching with query {q_idx + 1}/{len(queries)}: {q_simple[:100]}...", file=sys.stderr)

            page = 1
            consecutive_empty = 0

            while len(all_results) < limit and consecutive_empty < 2:
                params = {
                    "search": q_simple,
                    "per_page": min(self.per_page, limit - len(all_results)),
                    "sort": "relevance_score:desc",
                    "page": page
                }

                if filters:
                    params["filter"] = ",".join(filters)

                try:
                    r = backoff_get(self.base_url, params=params)
                    data = r.json()
                    results = data.get("results", [])

                    if not results:
                        consecutive_empty += 1
                        break

                    consecutive_empty = 0

                    for work in results:
                        if len(all_results) >= limit:
                            break

                        paper = self._parse_work(work)
                        all_results.append(paper)

                    page += 1

                    # Check if we've reached the end
                    meta = data.get("meta", {})
                    total = meta.get("count", 0)
                    if page * self.per_page >= total:
                        break

                except Exception as e:
                    print(f"Error fetching page {page}: {e}", file=sys.stderr)
                    break

        # Enhance abstracts if enabled
        if self.enhance_abstracts:
            all_results = self._enhance_abstracts(all_results)

        return all_results[:limit]

    def _parse_work(self, work: Dict[str, Any]) -> Paper:
        """Parse OpenAlex work object into Paper dataclass with enhanced metadata extraction"""

        # Basic fields
        title = _norm(work.get("title", ""))
        year = work.get("publication_year") or _year_from_date(work.get("publication_date"))
        doi = _clean_doi(work.get("doi"))

        # Enhanced Venue and Publisher extraction
        venue = None
        publisher = None
        venue_type = None

        # Try multiple sources for venue information
        # 1. Primary location (most reliable)
        primary_location = work.get("primary_location", {})
        if primary_location:
            source_info = primary_location.get("source", {})
            if source_info:
                venue = _norm(source_info.get("display_name"))
                publisher = _norm(source_info.get("host_organization_name"))
                venue_type = source_info.get("type")  # journal, conference, repository, etc.

                # If no publisher from host_organization_name, try publisher field
                if not publisher:
                    publisher = _norm(source_info.get("publisher"))

        # 2. Host venue (fallback)
        if not venue or not publisher:
            host_venue = work.get("host_venue", {})
            if host_venue:
                if not venue:
                    venue = _norm(host_venue.get("display_name"))
                if not publisher:
                    publisher = _norm(host_venue.get("publisher"))
                if not venue_type:
                    venue_type = host_venue.get("type")

        # 3. Check all locations for better venue/publisher info
        all_locations = work.get("locations", [])
        for location in all_locations:
            loc_source = location.get("source", {})
            if loc_source:
                # Update venue if we don't have one or if this looks more complete
                if not venue:
                    venue = _norm(loc_source.get("display_name"))

                # Try to get publisher from host_organization_name or publisher field
                if not publisher:
                    publisher = _norm(loc_source.get("host_organization_name")) or \
                                _norm(loc_source.get("publisher"))

                # If we have both, we can stop
                if venue and publisher:
                    break

        # 4. Extract publisher from venue name if still missing
        if venue and not publisher:
            # Common publisher patterns in venue names
            publisher_patterns = [
                (r'\b(IEEE)\b', 'IEEE'),
                (r'\b(ACM)\b', 'ACM'),
                (r'\b(Springer)\b', 'Springer'),
                (r'\b(Elsevier)\b', 'Elsevier'),
                (r'\b(Wiley)\b', 'Wiley'),
                (r'\b(Nature)\b', 'Nature Publishing Group'),
                (r'\b(Science)\b', 'AAAS'),
                (r'\b(PLOS)\b', 'PLOS'),
                (r'\b(Frontiers)\b', 'Frontiers'),
                (r'\b(MDPI)\b', 'MDPI'),
                (r'\b(Taylor & Francis)\b', 'Taylor & Francis'),
                (r'\b(Oxford University Press)\b', 'Oxford University Press'),
                (r'\b(Cambridge University Press)\b', 'Cambridge University Press'),
                (r'\b(MIT Press)\b', 'MIT Press'),
                (r'\b(IOP)\b', 'IOP Publishing'),
                (r'\b(AIP)\b', 'American Institute of Physics'),
                (r'\b(ACS)\b', 'American Chemical Society'),
                (r'\b(RSC)\b', 'Royal Society of Chemistry'),
                (r'\b(SAGE)\b', 'SAGE Publications'),
                (r'\b(Emerald)\b', 'Emerald Publishing'),
                (r'\b(Inderscience)\b', 'Inderscience'),
                (r'\b(World Scientific)\b', 'World Scientific'),
                (r'\b(IOS Press)\b', 'IOS Press'),
                (r'\b(De Gruyter)\b', 'De Gruyter'),
                (r'\b(Hindawi)\b', 'Hindawi'),
                (r'\b(BioMed Central|BMC)\b', 'BioMed Central'),
                (r'\b(Annual Reviews)\b', 'Annual Reviews'),
                (r'\b(JMLR)\b', 'JMLR'),
                (r'\b(AAAI)\b', 'AAAI Press'),
                (r'\b(IJCAI)\b', 'IJCAI'),
                (r'\b(NeurIPS|NIPS)\b', 'NeurIPS'),
                (r'\b(ICML)\b', 'ICML'),
                (r'\b(ICLR)\b', 'ICLR'),
                (r'\b(CVPR)\b', 'IEEE'),
                (r'\b(ICCV)\b', 'IEEE'),
                (r'\b(ECCV)\b', 'Springer'),
                (r'\b(EMNLP)\b', 'ACL'),
                (r'\b(NAACL)\b', 'ACL'),
                (r'\b(ACL)\b', 'ACL'),
            ]

            for pattern, pub_name in publisher_patterns:
                if re.search(pattern, venue, re.IGNORECASE):
                    publisher = pub_name
                    break

        # Enhanced URL extraction - get the best available real URL
        url_best = None

        # Priority order for URLs:
        # 1. Landing page URL from primary location (usually the publisher's page)
        if primary_location:
            url_best = primary_location.get("landing_page_url")

            # If no landing page, try PDF URL
            if not url_best:
                url_best = primary_location.get("pdf_url")

        # 2. Check all locations for better URLs
        if not url_best:
            for location in all_locations:
                # Prefer landing pages over PDFs
                landing_url = location.get("landing_page_url")
                if landing_url:
                    url_best = landing_url
                    break

                # Otherwise, take PDF URL
                pdf_url = location.get("pdf_url")
                if pdf_url:
                    url_best = pdf_url
                    # Don't break, keep looking for landing pages

        # 3. Open Access URL
        if not url_best and work.get("open_access"):
            oa_info = work["open_access"]
            url_best = oa_info.get("oa_url")

            # Sometimes OA status gives us better URLs
            if not url_best and oa_info.get("is_oa"):
                # Check if any location has is_oa=True
                for location in all_locations:
                    if location.get("is_oa"):
                        url_best = location.get("landing_page_url") or location.get("pdf_url")
                        if url_best:
                            break

        # 4. Best OA location
        best_oa_location = work.get("best_oa_location", {})
        if not url_best and best_oa_location:
            url_best = best_oa_location.get("landing_page_url") or \
                       best_oa_location.get("pdf_url")

        # 5. As last resort, construct DOI URL (but this is not a "real" URL)
        if not url_best and doi:
            # Try to construct publisher URL based on known patterns
            if publisher:
                publisher_lower = publisher.lower()
                if 'ieee' in publisher_lower:
                    # IEEE Xplore pattern - need to extract document ID
                    url_best = f"https://doi.org/{doi}"  # Default to DOI for IEEE
                elif 'acm' in publisher_lower:
                    # ACM Digital Library pattern
                    url_best = f"https://dl.acm.org/doi/{doi}"
                elif 'springer' in publisher_lower:
                    # Springer Link pattern
                    url_best = f"https://link.springer.com/article/{doi}"
                elif 'elsevier' in publisher_lower or (venue and 'sciencedirect' in venue.lower()):
                    # ScienceDirect pattern - complex, default to DOI
                    url_best = f"https://doi.org/{doi}"
                elif 'nature' in publisher_lower:
                    # Nature pattern
                    url_best = f"https://www.nature.com/articles/{doi.split('/')[-1]}" \
                        if '/' in doi else f"https://doi.org/{doi}"
                elif 'wiley' in publisher_lower:
                    # Wiley Online Library
                    url_best = f"https://onlinelibrary.wiley.com/doi/{doi}"
                elif 'taylor' in publisher_lower:
                    # Taylor & Francis
                    url_best = f"https://www.tandfonline.com/doi/full/{doi}"
                elif 'sage' in publisher_lower:
                    # SAGE Publications
                    url_best = f"https://journals.sagepub.com/doi/{doi}"
                elif 'oxford' in publisher_lower:
                    # Oxford Academic
                    url_best = f"https://academic.oup.com/doi/{doi}"
                elif 'cambridge' in publisher_lower:
                    # Cambridge Core
                    url_best = f"https://www.cambridge.org/core/doi/{doi}"
                else:
                    # Default to DOI.org
                    url_best = f"https://doi.org/{doi}"
            else:
                url_best = f"https://doi.org/{doi}"

        # Authors and institutions
        authors = []
        institutions = []
        for authorship in work.get("authorships", []):
            author = authorship.get("author", {})
            if author.get("display_name"):
                authors.append(_norm(author["display_name"]))

            for inst in authorship.get("institutions", []):
                inst_name = inst.get("display_name")
                if inst_name and inst_name not in institutions:
                    institutions.append(inst_name)

        # Abstract reconstruction from OpenAlex
        abstract = None
        abstract_source = None
        abstract_confidence = 0.0

        if work.get("abstract_inverted_index"):
            abstract = _reconstruct_abstract(work["abstract_inverted_index"])
            if abstract:
                abstract_source = "openalex"
                abstract_confidence = 1.0

        # Keywords from concepts
        keywords = []
        concepts = []
        for concept in work.get("concepts", []):
            if concept.get("score", 0) > 0.3:  # Filter by relevance
                concept_name = concept.get("display_name")
                if concept_name:
                    keywords.append(concept_name)
                    concepts.append({
                        "name": concept_name,
                        "score": concept.get("score", 0),
                        "id": concept.get("id")
                    })

        # Citation count
        citations = work.get("cited_by_count", 0)

        # Open access status
        is_open_access = False
        if work.get("open_access"):
            is_open_access = work["open_access"].get("is_oa", False)

        # Content type (article, proceedings-article, book-chapter, etc.)
        content_type = _norm(work.get("type"))

        # If content_type suggests conference and venue is missing, try to extract
        if content_type in ["proceedings-article", "conference-paper"] and not venue:
            # Try to get conference name from other fields
            for location in all_locations:
                source = location.get("source", {})
                if source and source.get("type") == "conference":
                    venue = _norm(source.get("display_name"))
                    if venue:
                        break

        # Referenced works
        referenced_works = work.get("referenced_works", [])

        # Add debug information if verbose
        if self.verbose and (not venue or not publisher):
            print(f"    ⚠ Missing metadata for '{title[:50]}...':", file=sys.stderr)
            if not venue:
                print(f"      - No venue found", file=sys.stderr)
            if not publisher:
                print(f"      - No publisher found", file=sys.stderr)
            if not url_best or "doi.org" in url_best:
                print(f"      - Only DOI URL available", file=sys.stderr)

        return Paper(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            url=url_best,
            source="openalex",
            abstract=abstract,
            abstract_source=abstract_source,
            abstract_confidence=abstract_confidence,
            keywords=keywords,
            citations=citations,
            content_type=content_type,
            publisher=publisher,
            published_date=work.get("publication_date"),
            openalex_id=work.get("id"),
            is_open_access=is_open_access,
            institutions=institutions,
            referenced_works=referenced_works,
            concepts=concepts
        )

    def _enhance_abstracts(self, papers: List[Paper]) -> List[Paper]:
        """Enhance papers by fetching missing abstracts from multiple sources"""
        if not self.abstract_fetcher:
            return papers

        enhanced_count = 0
        total_missing = sum(1 for p in papers if not p.abstract)

        if self.verbose and total_missing > 0:
            print(f"Enhancing {total_missing} papers without abstracts...", file=sys.stderr)

        for i, paper in enumerate(papers):
            # Skip if already has good abstract
            if paper.abstract and len(paper.abstract) > 100:
                continue

            if self.verbose:
                print(f"  Fetching abstract {i + 1}/{len(papers)}: {paper.title[:60]}...", file=sys.stderr)

            try:
                # Fetch abstract using all available information
                result = self.abstract_fetcher.fetch_abstract(
                    title=paper.title,
                    doi=paper.doi,
                    authors=paper.authors,
                    year=paper.year,
                    venue=paper.venue
                )

                # Update paper if we found a good abstract
                if result and result.abstract:
                    # Only replace if we found a better abstract
                    if not paper.abstract or len(result.abstract) > len(paper.abstract or ""):
                        paper.abstract = result.abstract
                        paper.abstract_source = result.source.lower().replace(" ", "_")
                        paper.abstract_confidence = result.confidence
                        enhanced_count += 1

                        if self.verbose:
                            print(f"    ✓ Found from {result.source} (confidence: {result.confidence:.2f})",
                                  file=sys.stderr)

            except Exception as e:
                if self.verbose:
                    print(f"    ✗ Error: {e}", file=sys.stderr)

        if self.verbose:
            print(f"Enhanced {enhanced_count} abstracts from external sources", file=sys.stderr)

        return papers

    def get_by_doi(self, doi: str) -> Optional[Paper]:
        """Fetch a specific paper by DOI with enhanced abstract"""
        doi = _clean_doi(doi)
        if not doi:
            return None

        url = f"https://api.openalex.org/works/https://doi.org/{doi}"

        try:
            r = backoff_get(url)
            work = r.json()
            paper = self._parse_work(work)

            # Enhance abstract if needed and enabled
            if self.enhance_abstracts and (not paper.abstract or len(paper.abstract) < 100):
                enhanced_papers = self._enhance_abstracts([paper])
                return enhanced_papers[0] if enhanced_papers else paper

            return paper
        except Exception as e:
            print(f"Error fetching DOI {doi}: {e}", file=sys.stderr)
            return None

    def get_citations(self, openalex_id: str, limit: int = 100) -> List[Paper]:
        """Get papers that cite a given work with enhanced abstracts"""
        if not openalex_id.startswith("W"):
            openalex_id = f"W{openalex_id}"

        params = {
            "filter": f"cites:{openalex_id}",
            "per_page": min(self.per_page, limit),
            "sort": "cited_by_count:desc"
        }

        papers = []
        page = 1

        while len(papers) < limit:
            params["page"] = page

            try:
                r = backoff_get(self.base_url, params=params)
                data = r.json()
                results = data.get("results", [])

                if not results:
                    break

                for work in results:
                    if len(papers) >= limit:
                        break
                    papers.append(self._parse_work(work))

                page += 1

            except Exception as e:
                print(f"Error fetching citations: {e}", file=sys.stderr)
                break

        # Enhance abstracts if enabled
        if self.enhance_abstracts:
            papers = self._enhance_abstracts(papers)

        return papers


def save_to_csv(papers: List[Paper], filepath: str):
    """Save papers to CSV file with all important fields including publisher"""
    if not papers:
        print("No papers to save")
        return

    # Enhanced fields including publisher and all metadata
    fields = [
        "title",
        "authors",
        "year",
        "published_date",
        "venue",
        "publisher",  # Added publisher field
        "content_type",
        "url",
        "doi",
        "abstract",
        "abstract_source",
        "abstract_confidence",
        "keywords",
        "citations",
        "is_open_access",
        "institutions",
        "openalex_id"
    ]

    with open(filepath, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()

        for paper in papers:
            row = asdict(paper)
            # Convert lists to semicolon-separated strings
            row["authors"] = "; ".join(paper.authors) if paper.authors else ""
            row["keywords"] = "; ".join(paper.keywords) if paper.keywords else ""
            row["institutions"] = "; ".join(paper.institutions) if paper.institutions else ""

            # Only keep the fields we want
            row = {k: row.get(k) for k in fields}
            writer.writerow(row)

    print(f"✅ Saved {len(papers)} papers to {filepath}")

    # Print statistics about data completeness
    papers_with_venue = sum(1 for p in papers if p.venue)
    papers_with_publisher = sum(1 for p in papers if p.publisher)
    papers_with_real_url = sum(1 for p in papers if p.url and "doi.org" not in (p.url or ""))

    print(f"📊 Data completeness:")
    print(f"  Papers with venue: {papers_with_venue}/{len(papers)} ({100 * papers_with_venue / len(papers):.1f}%)")
    print(
        f"  Papers with publisher: {papers_with_publisher}/{len(papers)} ({100 * papers_with_publisher / len(papers):.1f}%)")
    print(
        f"  Papers with real URL: {papers_with_real_url}/{len(papers)} ({100 * papers_with_real_url / len(papers):.1f}%)")

def save_to_jsonl(papers: List[Paper], filepath: str):
    """Save papers to JSONL file"""
    if not papers:
        print("No papers to save")
        return

    with open(filepath, "w", encoding="utf-8") as f:
        for paper in papers:
            # Convert to dict and write as JSON line
            paper_dict = asdict(paper)
            # Remove empty lists and None values to save space
            paper_dict = {k: v for k, v in paper_dict.items()
                          if v is not None and (not isinstance(v, list) or v)}
            f.write(json.dumps(paper_dict, ensure_ascii=False) + "\n")

    print(f"✅ Saved {len(papers)} papers to {filepath}")


def save_to_json(papers: List[Paper], filepath: str):
    """Save papers to JSON file"""
    if not papers:
        print("No papers to save")
        return

    data = []
    for paper in papers:
        paper_dict = asdict(paper)
        # Remove empty lists and None values
        paper_dict = {k: v for k, v in paper_dict.items()
                      if v is not None and (not isinstance(v, list) or v)}
        data.append(paper_dict)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"✅ Saved {len(papers)} papers to {filepath}")


def main():
    parser = argparse.ArgumentParser(
        description="Enhanced OpenAlex paper crawler with universal abstract fetching"
    )

    parser.add_argument(
        "query",
        nargs="?",
        help="Search query (supports boolean: AND, OR, parentheses)"
    )
    parser.add_argument(
        "--year-from",
        type=int,
        help="Start year (inclusive)"
    )
    parser.add_argument(
        "--year-to",
        type=int,
        help="End year (inclusive)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of results (default: 100)"
    )
    parser.add_argument(
        "--out-csv",
        default="openalex_results.csv",
        help="Output CSV file path"
    )
    parser.add_argument(
        "--out-jsonl",
        default="openalex_results.jsonl",
        help="Output JSONL file path"
    )
    parser.add_argument(
        "--out-json",
        help="Output JSON file path (optional)"
    )
    parser.add_argument(
        "--doi",
        help="Fetch specific paper by DOI"
    )
    parser.add_argument(
        "--citations-of",
        help="Get papers citing a specific OpenAlex ID"
    )
    parser.add_argument(
        "--no-combo",
        action="store_true",
        help="Disable boolean query expansion"
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=100,
        help="Results per page (max 200)"
    )
    parser.add_argument(
        "--no-enhance",
        action="store_true",
        help="Disable abstract enhancement from external sources"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed progress information"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print raw API responses for debugging"
    )

    args = parser.parse_args()

    # Global configuration
    global COMBO_ENABLED
    COMBO_ENABLED = not args.no_combo

    # Initialize enhanced crawler
    crawler = OpenAlexCrawler(
        enhance_abstracts=not args.no_enhance,
        verbose=args.verbose
    )

    if args.per_page:
        crawler.per_page = min(args.per_page, MAX_PER_PAGE)

    papers = []

    # Handle different modes
    if args.doi:
        # Fetch single paper by DOI
        paper = crawler.get_by_doi(args.doi)
        if paper:
            papers = [paper]
            print(f"Found paper: {paper.title}")
            if paper.abstract_source and paper.abstract_source != "openalex":
                print(f"Abstract enhanced from: {paper.abstract_source}")

            # Print detailed info for single paper
            print(f"\nPaper Details:")
            print(f"  Title: {paper.title}")
            print(f"  Authors: {'; '.join(paper.authors[:3])}{'...' if len(paper.authors) > 3 else ''}")
            print(f"  Year: {paper.year}")
            print(f"  Venue: {paper.venue or 'N/A'}")
            print(f"  Publisher: {paper.publisher or 'N/A'}")
            print(f"  URL: {paper.url or 'N/A'}")
            print(f"  DOI: {paper.doi or 'N/A'}")
            print(f"  Citations: {paper.citations}")
            print(f"  Open Access: {'Yes' if paper.is_open_access else 'No'}")

            if args.debug and paper.url and "doi.org" in paper.url:
                print(f"  ⚠️  Warning: Only DOI URL available")
        else:
            print(f"No paper found with DOI: {args.doi}")

    elif args.citations_of:
        # Get citing papers
        papers = crawler.get_citations(args.citations_of, limit=args.limit)
        print(f"Found {len(papers)} papers citing {args.citations_of}")

    elif args.query:
        # Extract years from query if present
        query, py_from, py_to = extract_years_from_query(args.query)
        if py_from and not args.year_from:
            args.year_from = py_from
        if py_to and not args.year_to:
            args.year_to = py_to

        # Search with query
        papers = crawler.search(
            query=query,
            year_from=args.year_from,
            year_to=args.year_to,
            limit=args.limit
        )

        print(f"Found {len(papers)} papers for query: {query[:100]}...")

    else:
        print("Please provide a query, DOI, or OpenAlex ID")
        parser.print_help()
        return

    # Save results
    if papers:
        save_to_csv(papers, args.out_csv)
        # save_to_jsonl(papers, args.out_jsonl)

        if args.out_json:
            save_to_json(papers, args.out_json)


        # Additional debug info if requested
        if args.debug:
            print(f"\n🔍 Debug Information:")

            # Show papers with missing critical metadata
            critical_missing = [p for p in papers if not p.venue or not p.publisher or (p.url and "doi.org" in p.url)]
            if critical_missing:
                print(f"  Papers with missing critical metadata: {len(critical_missing)}")
                for p in critical_missing[:5]:
                    print(f"\n  Paper: {p.title[:50]}...")
                    print(f"    - Venue: {p.venue or 'MISSING'}")
                    print(f"    - Publisher: {p.publisher or 'MISSING'}")
                    print(f"    - URL: {'DOI only' if p.url and 'doi.org' in p.url else p.url or 'MISSING'}")
                    print(f"    - Content Type: {p.content_type}")
                    print(f"    - OpenAlex ID: {p.openalex_id}")
    else:
        print("No papers found")


if __name__ == "__main__":
    main()