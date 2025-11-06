"""
ACM Digital Library Paper Crawler - Complete Version with Automatic Abstract Fetching
Uses Semantic Scholar API to avoid 403 errors
"""

import argparse
import csv
import json
import time
import re
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Optional
import requests
from abstract import fetch_abstract

# Configuration
USER_AGENT = "ACM Paper Crawler/1.0 (https://example.org/contact)"
CROSSREF_API_URL = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR_API_URL = "https://api.semanticscholar.org/graph/v1/paper"
ACM_DOI_PREFIX = "10.1145"
DEFAULT_TIMEOUT = 20
MAX_RETRIES = 4


@dataclass
class Paper:
    """Data class for storing paper metadata"""
    title: str
    authors: List[str]
    published_date: Optional[str]  # Full date in YYYY-MM-DD format
    year: Optional[int]
    link: Optional[str]
    content_type: Optional[str]
    doi: Optional[str]
    abstract: Optional[str]
    venue: Optional[str]
    keywords: List[str] = field(default_factory=list)
    citations: Optional[int] = None
    publisher: Optional[str] = None
    isbn: Optional[str] = None
    pages: Optional[str] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    article_number: Optional[str] = None
    conference_location: Optional[str] = None
    references_count: Optional[int] = None
    acm_id: Optional[str] = None
    categories: List[str] = field(default_factory=list)
    semantic_scholar_citations: Optional[int] = None  # Additional citation count from S2


def normalize_string(s: Optional[str]) -> str:
    """Normalize whitespace in strings"""
    if not s:
        return ""
    return re.sub(r'\s+', ' ', s).strip()


def clean_doi(doi: Optional[str]) -> Optional[str]:
    """Clean and normalize DOI"""
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r'^https?://(dx\.)?doi\.org/', '', doi)
    return doi if doi else None


def format_date(date_parts: List[List[int]]) -> Optional[str]:
    """Format date parts into YYYY-MM-DD string"""
    if not date_parts or not date_parts[0]:
        return None

    parts = date_parts[0]
    if len(parts) >= 3:
        return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
    elif len(parts) == 2:
        return f"{parts[0]:04d}-{parts[1]:02d}"
    elif len(parts) == 1:
        return f"{parts[0]:04d}"
    return None


def extract_acm_id_from_doi(doi: str) -> Optional[str]:
    """Extract ACM ID from DOI (e.g., 10.1145/3447548.3467286 -> 3447548.3467286)"""
    if doi and doi.startswith(ACM_DOI_PREFIX):
        parts = doi.split('/', 1)
        if len(parts) > 1:
            return parts[1]
    return None


def construct_acm_url(doi: str) -> str:
    """Construct ACM Digital Library URL from DOI"""
    if doi:
        if doi.startswith(ACM_DOI_PREFIX):
            return f"https://dl.acm.org/doi/{doi}"
        else:
            return f"https://dl.acm.org/doi/{ACM_DOI_PREFIX}/{doi}"
    return ""


def backoff_request(url: str, params: Dict[str, Any] = None, headers: Dict[str, str] = None) -> requests.Response:
    """Make HTTP request with exponential backoff retry"""
    headers = headers or {}
    headers.setdefault("User-Agent", USER_AGENT)
    params = params or {}

    delay = 1.0
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
            if response.status_code == 200:
                return response
            elif response.status_code in (429, 500, 502, 503, 504):
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
            response.raise_for_status()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise

    raise requests.exceptions.RequestException(f"Max retries ({MAX_RETRIES}) exceeded")


def extract_paper_from_crossref_item(item: Dict[str, Any]) -> Paper:
    """Extract Paper object from Crossref API response item"""

    # Title
    title = normalize_string(" ".join(item.get("title", []) or []))
    if item.get("subtitle"):
        subtitle = normalize_string(" ".join(item.get("subtitle", []) or []))
        if subtitle:
            title = f"{title}: {subtitle}"

    # Authors
    authors = []
    for author in item.get("author", []) or []:
        name_parts = []
        if author.get("given"):
            name_parts.append(author["given"])
        if author.get("family"):
            name_parts.append(author["family"])
        if name_parts:
            authors.append(" ".join(name_parts))
        elif author.get("name"):
            authors.append(author["name"])

    # Published date
    date_parts = item.get("published-print", {}).get("date-parts", [])
    if not date_parts:
        date_parts = item.get("published-online", {}).get("date-parts", [])
    if not date_parts:
        date_parts = item.get("issued", {}).get("date-parts", [])
    if not date_parts:
        date_parts = item.get("created", {}).get("date-parts", [])

    published_date = format_date(date_parts) if date_parts else None
    year = date_parts[0][0] if date_parts and date_parts[0] else None

    # DOI and ACM ID
    doi = clean_doi(item.get("DOI"))
    acm_id = extract_acm_id_from_doi(doi) if doi else None

    # Link - construct ACM DL URL
    link = construct_acm_url(doi) if doi else item.get("URL")

    # Content type
    content_type = item.get("type")

    # Abstract - often missing in Crossref
    abstract = normalize_string(item.get("abstract")) if item.get("abstract") else None
    # Clean HTML tags if present
    if abstract:
        abstract = re.sub('<[^<]+?>', '', abstract)

    # Venue (journal/conference name)
    venue = normalize_string(" ".join(item.get("container-title", []) or []))
    if not venue:
        venue = normalize_string(" ".join(item.get("short-container-title", []) or []))

    # Event information (for conferences)
    event = item.get("event", {})
    conference_location = None
    if event:
        location_parts = []
        if event.get("location"):
            location_parts.append(event["location"])
        if event.get("name") and "location" not in event.get("name", "").lower():
            if not venue or venue.lower() not in event["name"].lower():
                venue = normalize_string(event["name"])
        conference_location = ", ".join(location_parts) if location_parts else None

    # Keywords/Subjects
    keywords = []
    if item.get("subject"):
        keywords = [normalize_string(s) for s in item.get("subject", [])]

    # Categories (ACM Computing Classification)
    categories = []
    if item.get("categories"):
        categories = [normalize_string(c) for c in item.get("categories", [])]

    # Citation count (limited in Crossref)
    citations = item.get("is-referenced-by-count")

    # Additional metadata
    publisher = "Association for Computing Machinery" if not item.get("publisher") else normalize_string(
        item.get("publisher"))
    isbn = item.get("ISBN", [""])[0] if item.get("ISBN") else None
    pages = item.get("page")
    volume = item.get("volume")
    issue = item.get("issue")
    article_number = item.get("article-number")

    # References count
    references_count = item.get("references-count")

    return Paper(
        title=title,
        authors=authors,
        published_date=published_date,
        year=year,
        link=link,
        content_type=content_type,
        doi=doi,
        abstract=abstract,
        venue=venue,
        keywords=keywords,
        citations=citations,
        publisher=publisher,
        isbn=isbn,
        pages=pages,
        volume=volume,
        issue=issue,
        article_number=article_number,
        conference_location=conference_location,
        references_count=references_count,
        acm_id=acm_id,
        categories=categories
    )


class SemanticScholarFetcher:
    """Fetcher for getting abstracts and additional metadata from Semantic Scholar"""

    def __init__(self, delay: float = 0.5):
        """
        Initialize the fetcher

        Args:
            delay: Delay between requests (Semantic Scholar is generous with rate limits)
        """
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT
        })

    def fetch_paper_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        """
        Fetch paper data from Semantic Scholar using DOI

        Args:
            doi: The DOI of the paper

        Returns:
            Paper data dict or None if not found
        """
        if not doi:
            return None

        # Semantic Scholar API endpoint
        url = f"{SEMANTIC_SCHOLAR_API_URL}/{doi}"
        params = {
            'fields': 'title,abstract,authors,year,venue,citationCount,referenceCount,fieldsOfStudy,publicationTypes,journal'
        }

        try:
            time.sleep(self.delay)  # Rate limiting

            response = self.session.get(url, params=params, timeout=10)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                # Paper not found in Semantic Scholar
                return None
            else:
                print(f"  Semantic Scholar API error {response.status_code} for DOI {doi}")

        except Exception as e:
            print(f"  Error fetching from Semantic Scholar: {e}")

        return None

    def fetch_papers_batch(self, dois: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetch data for multiple DOIs from Semantic Scholar

        Args:
            dois: List of DOIs

        Returns:
            Dictionary mapping DOI to paper data
        """
        results = {}
        total = len(dois)
        successful = 0
        not_found = 0

        print(f"\nFetching additional metadata from Semantic Scholar for {total} papers...")
        print("This will enhance abstracts and citation counts...")

        for i, doi in enumerate(dois, 1):
            paper_data = self.fetch_paper_by_doi(doi)
            if paper_data:
                results[doi] = paper_data
                successful += 1
            else:
                not_found += 1

            # Progress indicator
            if i % 20 == 0:
                print(f"  Progress: {i}/{total} papers processed, {successful} found in Semantic Scholar")

        print(f"  Completed: {successful}/{total} papers found in Semantic Scholar")
        if not_found > 0:
            print(f"  Note: {not_found} papers not found in Semantic Scholar (may be too recent or niche)")

        return results


class ACMCrawler:
    """ACM Digital Library crawler for academic papers with automatic abstract fetching"""

    def __init__(self, email: Optional[str] = None):
        """
        Initialize crawler

        Args:
            email: Optional email for polite pool (gets better rate limits)
        """
        self.email = email
        self.headers = {"User-Agent": USER_AGENT}
        if email:
            self.headers["mailto"] = email

        # Initialize Semantic Scholar fetcher for abstracts
        self.ss_fetcher = SemanticScholarFetcher()

    def enhance_papers_with_abstracts(self, papers: List[Paper]) -> int:
        """Enhanced version using universal fetcher"""
        enhanced_count = 0

        for paper in papers:
            if not paper.abstract or len(paper.abstract) < 100:
                abstract = fetch_abstract(
                    title=paper.title,
                    doi=paper.doi,
                    year=paper.year,
                    verbose=False
                )

                if abstract:
                    paper.abstract = abstract
                    enhanced_count += 1
                    print(f"✓ Enhanced abstract for paper {enhanced_count}")

        print(f"Enhanced {enhanced_count} papers with abstracts")
        return enhanced_count

    def search_via_crossref(
            self,
            query: str,
            year_from: Optional[int] = None,
            year_to: Optional[int] = None,
            limit: Optional[int] = None,
            sort: str = "relevance",
            content_type: Optional[str] = None
    ) -> List[Paper]:
        """
        Search ACM papers via Crossref API and automatically fetch abstracts

        Args:
            query: Search query string
            year_from: Start year (inclusive)
            year_to: End year (inclusive)
            limit: Maximum number of results (None for unlimited)
            sort: Sort order ('relevance', 'published', 'updated', 'indexed')
            content_type: Filter by content type (e.g., 'proceedings-article', 'journal-article')

        Returns:
            List of Paper objects with abstracts
        """
        papers = []
        cursor = "*"
        rows_per_request = 1000

        print(f"Searching ACM Digital Library via Crossref for: '{query}'")
        if limit is None:
            print("No limit set - will fetch all available ACM results")
        else:
            print(f"Limit: {limit} papers")

        # First get total count
        initial_params = {
            "query": query,
            "rows": 1,
            "cursor": "*",
            "sort": sort
        }

        # Keep ACM prefix and restore server-side year filtering; keep type filtering disabled
        filters = [f"prefix:{ACM_DOI_PREFIX}"]
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")

        initial_params["filter"] = ",".join(filters)

        try:
            response = backoff_request(CROSSREF_API_URL, initial_params, self.headers)
            data = response.json()
            total_results = data.get("message", {}).get("total-results", 0)
            print(f"Total available ACM results: {total_results}")

            if limit is not None:
                target_count = min(limit, total_results)
                print(f"Will fetch: {target_count} papers")
            else:
                target_count = total_results
                print(f"Will fetch all {target_count} papers")

        except Exception as e:
            print(f"Warning: Could not get total count: {e}")
            target_count = limit if limit is not None else float('inf')

        # Reset cursor for actual fetching
        cursor = "*"

        while True:
            # Calculate rows for this request
            if limit is not None:
                remaining = limit - len(papers)
                if remaining <= 0:
                    break
                current_rows = min(rows_per_request, remaining)
            else:
                current_rows = rows_per_request

            # Build parameters
            params = {
                "query": query,
                "rows": current_rows,
                "cursor": cursor,
                "sort": sort
            }

            params["filter"] = ",".join(filters)

            # Make request
            try:
                response = backoff_request(CROSSREF_API_URL, params, self.headers)
                data = response.json()

                message = data.get("message", {})
                items = message.get("items", [])

                if not items:
                    print("No more results available")
                    break

                # Extract papers
                for item in items:
                    if limit is not None and len(papers) >= limit:
                        break
                    try:
                        paper = extract_paper_from_crossref_item(item)
                        papers.append(paper)
                    except Exception as e:
                        print(f"Warning: Failed to extract paper: {e}")
                        continue

                # Progress indicator
                if len(papers) % 100 == 0:
                    if limit is not None:
                        print(f"  Retrieved {len(papers)}/{limit} papers so far...")
                    else:
                        print(f"  Retrieved {len(papers)}/{target_count} papers so far...")

                # Get next cursor
                next_cursor = message.get("next-cursor")
                if not next_cursor:
                    print("Reached end of results (no more cursor)")
                    break
                cursor = next_cursor

                # Check if reached target
                if limit is not None and len(papers) >= limit:
                    break

            except Exception as e:
                print(f"Error during search: {e}")
                break

        # Always enhance with Semantic Scholar data (including abstracts)
        if papers:
            print(f"\n✓ Retrieved {len(papers)} papers from Crossref")
            missing_abstracts = sum(1 for p in papers if not p.abstract)
            if missing_abstracts > 0:
                print(f"  {missing_abstracts} papers are missing abstracts")

            # Enhance all papers with Semantic Scholar
            self.enhance_papers_with_abstracts(papers)

            # Final statistics
            final_missing = sum(1 for p in papers if not p.abstract)
            if final_missing > 0:
                print(f"  Note: {final_missing} papers still missing abstracts (not in Semantic Scholar)")
            else:
                print(f"  ✓ All papers now have abstracts!")

        return papers

    def search_by_venue(
            self,
            venue_name: str,
            year: Optional[int] = None,
            limit: Optional[int] = None
    ) -> List[Paper]:
        """
        Search papers from a specific ACM venue/conference

        Args:
            venue_name: Name of the venue (e.g., "ICSE", "CHI", "SIGMOD")
            year: Optional year filter
            limit: Maximum number of results (None for unlimited)

        Returns:
            List of Paper objects with abstracts
        """
        # Build query for venue
        query = f'"{venue_name}"'

        print(f"Searching ACM venue: {venue_name}" + (f" ({year})" if year else ""))

        return self.search_via_crossref(
            query=query,
            year_from=year,
            year_to=year,
            limit=limit,
            sort="published"
        )

    def get_by_doi(self, doi: str) -> Optional[Paper]:
        """
        Get a specific ACM paper by DOI with abstract

        Args:
            doi: The DOI of the paper

        Returns:
            Paper object with abstract or None if not found
        """
        clean_doi_str = clean_doi(doi)
        if not clean_doi_str:
            return None

        # Ensure it's an ACM DOI
        if not clean_doi_str.startswith(ACM_DOI_PREFIX):
            print(f"Warning: DOI {doi} is not an ACM DOI (should start with {ACM_DOI_PREFIX})")
            return None

        url = f"{CROSSREF_API_URL}/{clean_doi_str}"

        try:
            response = requests.get(url, headers=self.headers, timeout=DEFAULT_TIMEOUT)
            if response.status_code == 200:
                data = response.json()
                item = data.get("message", {})
                paper = extract_paper_from_crossref_item(item)

                # Always try to enhance with Semantic Scholar
                if paper:
                    self.enhance_papers_with_abstracts([paper])

                return paper
        except Exception as e:
            print(f"Error fetching DOI {doi}: {e}")

        return None

    def search_by_author(
            self,
            author_name: str,
            year_from: Optional[int] = None,
            year_to: Optional[int] = None,
            limit: Optional[int] = None
    ) -> List[Paper]:
        """
        Search papers by author name in ACM Digital Library

        Args:
            author_name: Author's name
            year_from: Start year (inclusive)
            year_to: End year (inclusive)
            limit: Maximum number of results (None for unlimited)

        Returns:
            List of Paper objects with abstracts
        """
        print(f"Searching ACM papers by author: {author_name}")
        if limit is None:
            print("No limit set - will fetch all available results for this author")

        papers = []
        cursor = "*"
        rows_per_request = 1000

        # Get total count first
        initial_params = {
            "query.author": author_name,
            "rows": 1,
            "cursor": "*",
            "sort": "published"
        }

        # Keep ACM prefix and restore server-side year filtering
        filters = [f"prefix:{ACM_DOI_PREFIX}"]
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")

        initial_params["filter"] = ",".join(filters)

        try:
            response = backoff_request(CROSSREF_API_URL, initial_params, self.headers)
            data = response.json()
            total_results = data.get("message", {}).get("total-results", 0)
            print(f"Total available results for {author_name}: {total_results}")

            if limit is not None:
                target_count = min(limit, total_results)
                print(f"Will fetch: {target_count} papers")
            else:
                target_count = total_results
                print(f"Will fetch all {target_count} papers")

        except Exception as e:
            print(f"Warning: Could not get total count: {e}")
            target_count = limit if limit is not None else float('inf')

        cursor = "*"

        while True:
            # Calculate rows for this request
            if limit is not None:
                remaining = limit - len(papers)
                if remaining <= 0:
                    break
                current_rows = min(rows_per_request, remaining)
            else:
                current_rows = rows_per_request

            params = {
                "query.author": author_name,
                "rows": current_rows,
                "cursor": cursor,
                "sort": "published"
            }

            params["filter"] = ",".join(filters)

            try:
                response = backoff_request(CROSSREF_API_URL, params, self.headers)
                data = response.json()

                message = data.get("message", {})
                items = message.get("items", [])

                if not items:
                    print("No more results available")
                    break

                for item in items:
                    if limit is not None and len(papers) >= limit:
                        break
                    try:
                        paper = extract_paper_from_crossref_item(item)
                        papers.append(paper)
                    except Exception as e:
                        continue

                # Progress indicator
                if len(papers) % 50 == 0:
                    if limit is not None:
                        print(f"  Retrieved {len(papers)}/{limit} papers so far...")
                    else:
                        print(f"  Retrieved {len(papers)}/{target_count} papers so far...")

                next_cursor = message.get("next-cursor")
                if not next_cursor:
                    print("Reached end of results (no more cursor)")
                    break
                cursor = next_cursor

                # Check if reached target
                if limit is not None and len(papers) >= limit:
                    break

            except Exception as e:
                print(f"Error during author search: {e}")
                break

        # Always enhance with Semantic Scholar data
        if papers:
            print(f"\n✓ Retrieved {len(papers)} papers from Crossref")
            self.enhance_papers_with_abstracts(papers)

        return papers


def save_to_csv(papers: List[Paper], filename: str):
    """Save papers to CSV file with all metadata"""
    if not papers:
        print("No papers to save")
        return

    # Define fields to export
    fieldnames = [
        'title', 'authors', 'published_date', 'year', 'link',
        'content_type', 'doi', 'acm_id', 'abstract', 'venue',
        'keywords', 'citations_crossref', 'citations_semantic_scholar',
        'publisher', 'pages', 'volume', 'issue', 'references_count'
    ]

    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for paper in papers:
            row = {
                'title': paper.title,
                'authors': '; '.join(paper.authors),
                'published_date': paper.published_date,
                'year': paper.year,
                'link': paper.link,
                'content_type': paper.content_type,
                'doi': paper.doi,
                'acm_id': paper.acm_id,
                'abstract': paper.abstract if paper.abstract else '',
                'venue': paper.venue,
                'keywords': '; '.join(paper.keywords) if paper.keywords else '',
                'citations_crossref': paper.citations,
                'citations_semantic_scholar': paper.semantic_scholar_citations,
                'publisher': paper.publisher,
                'pages': paper.pages,
                'volume': paper.volume,
                'issue': paper.issue,
                'references_count': paper.references_count
            }
            writer.writerow(row)


def save_to_json(papers: List[Paper], filename: str):
    """Save papers to JSON file"""
    papers_dict = [asdict(paper) for paper in papers]
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(papers_dict, f, ensure_ascii=False, indent=2)


def save_to_jsonl(papers: List[Paper], filename: str):
    """Save papers to JSONL file (one JSON object per line)"""
    with open(filename, 'w', encoding='utf-8') as f:
        for paper in papers:
            json.dump(asdict(paper), f, ensure_ascii=False)
            f.write('\n')


def main():
    parser = argparse.ArgumentParser(
        description="Crawl academic papers from ACM Digital Library with automatic abstract fetching via Semantic Scholar"
    )

    # Create subparsers for different search modes
    subparsers = parser.add_subparsers(dest='mode', help='Search mode')

    # General search
    search_parser = subparsers.add_parser('search', help='General search')
    search_parser.add_argument("query", help="Search query string")
    search_parser.add_argument("--year-from", type=int, help="Start year (inclusive)")
    search_parser.add_argument("--year-to", type=int, help="End year (inclusive)")
    search_parser.add_argument("--type", help="Content type (e.g., proceedings-article, journal-article)")

    # Venue search
    venue_parser = subparsers.add_parser('venue', help='Search by venue/conference')
    venue_parser.add_argument("name", help="Venue name (e.g., ICSE, CHI, SIGMOD)")
    venue_parser.add_argument("--year", type=int, help="Year filter")

    # Author search
    author_parser = subparsers.add_parser('author', help='Search by author')
    author_parser.add_argument("name", help="Author name")
    author_parser.add_argument("--year-from", type=int, help="Start year (inclusive)")
    author_parser.add_argument("--year-to", type=int, help="End year (inclusive)")

    # DOI lookup
    doi_parser = subparsers.add_parser('doi', help='Get paper by DOI')
    doi_parser.add_argument("doi", help="DOI of the paper")

    # Common arguments
    parser.add_argument("--limit", type=int, help="Maximum number of results (omit for unlimited)")
    parser.add_argument("--email", help="Email for polite pool (gets better rate limits)")
    parser.add_argument("--output", default="acm_papers", help="Output filename (without extension)")
    parser.add_argument("--format", choices=["csv", "json", "jsonl", "all"], default="csv",
                        help="Output format (default: csv)")

    args = parser.parse_args()

    if not args.mode:
        parser.print_help()
        return

    # Initialize crawler (will automatically fetch abstracts)
    crawler = ACMCrawler(email=args.email)

    # Get papers based on mode
    papers = []

    print("=" * 60)
    print("ACM Digital Library Crawler")
    print("Abstracts will be automatically fetched from Semantic Scholar")
    print("=" * 60)

    if args.mode == 'search':
        papers = crawler.search_via_crossref(
            query=args.query,
            year_from=args.year_from,
            year_to=args.year_to,
            limit=args.limit,
            content_type=args.type
        )

    elif args.mode == 'venue':
        papers = crawler.search_by_venue(
            venue_name=args.name,
            year=args.year,
            limit=args.limit
        )

    elif args.mode == 'author':
        papers = crawler.search_by_author(
            author_name=args.name,
            year_from=args.year_from,
            year_to=args.year_to,
            limit=args.limit
        )

    elif args.mode == 'doi':
        paper = crawler.get_by_doi(args.doi)
        papers = [paper] if paper else []

    print(f"\n{'=' * 60}")
    print(f"FINAL RESULTS: Found {len(papers)} papers")
    print(f"{'=' * 60}")

    # Save results
    if papers:
        # Statistics before saving
        abstracts_count = sum(1 for p in papers if p.abstract)
        keywords_count = sum(1 for p in papers if p.keywords)
        citations_count = sum(1 for p in papers if p.citations is not None or p.semantic_scholar_citations is not None)

        print(f"\n📊 Statistics:")
        print(f"  - Papers with abstracts: {abstracts_count}/{len(papers)} ({abstracts_count*100//len(papers)}%)")
        print(f"  - Papers with keywords: {keywords_count}/{len(papers)} ({keywords_count*100//len(papers)}%)")
        print(f"  - Papers with citations: {citations_count}/{len(papers)} ({citations_count*100//len(papers)}%)")

        # Save files
        print(f"\n💾 Saving results...")

        if args.format in ["csv", "all"]:
            csv_file = f"{args.output}.csv"
            save_to_csv(papers, csv_file)
            print(f"  ✓ Saved to {csv_file}")

        if args.format in ["json", "all"]:
            json_file = f"{args.output}.json"
            save_to_json(papers, json_file)
            print(f"  ✓ Saved to {json_file}")

        if args.format in ["jsonl", "all"]:
            jsonl_file = f"{args.output}.jsonl"
            save_to_jsonl(papers, jsonl_file)
            print(f"  ✓ Saved to {jsonl_file}")

        # Print sample results
        print(f"\n📚 Sample results (showing first 3 papers):")
        print("-" * 60)

        for i, paper in enumerate(papers[:3], 1):
            print(f"\n{i}. {paper.title}")
            print(f"   Authors: {', '.join(paper.authors[:3])}")
            if len(paper.authors) > 3:
                print(f"            ... and {len(paper.authors) - 3} more")
            print(f"   Year: {paper.year}")
            print(f"   Venue: {paper.venue or 'N/A'}")
            print(f"   DOI: {paper.doi}")
            print(f"   Link: {paper.link}")

            # Show citations from both sources
            citations_str = []
            if paper.citations is not None:
                citations_str.append(f"Crossref: {paper.citations}")
            if paper.semantic_scholar_citations is not None:
                citations_str.append(f"S2: {paper.semantic_scholar_citations}")
            if citations_str:
                print(f"   Citations: {', '.join(citations_str)}")

            if paper.keywords:
                keywords_display = paper.keywords[:5] if len(paper.keywords) > 5 else paper.keywords
                print(f"   Keywords: {', '.join(keywords_display)}")
                if len(paper.keywords) > 5:
                    print(f"             ... and {len(paper.keywords) - 5} more")

            if paper.abstract:
                # Show first 200 characters of abstract
                abstract_preview = paper.abstract[:200] + "..." if len(paper.abstract) > 200 else paper.abstract
                print(f"   Abstract: {abstract_preview}")
            else:
                print(f"   Abstract: [Not available]")

        if len(papers) > 3:
            print(f"\n... and {len(papers) - 3} more papers")

        # Detailed statistics
        print(f"\n📈 Detailed Statistics:")
        print("-" * 60)

        # Year distribution
        years = [p.year for p in papers if p.year]
        if years:
            year_counts = {}
            for year in years:
                year_counts[year] = year_counts.get(year, 0) + 1

            print(f"Year distribution:")
            print(f"  - Range: {min(years)} - {max(years)}")
            print(f"  - Most productive years:")
            for year, count in sorted(year_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                print(f"    • {year}: {count} papers")

        # Venue distribution
        venue_counts = {}
        for p in papers:
            if p.venue:
                venue_counts[p.venue] = venue_counts.get(p.venue, 0) + 1

        if venue_counts:
            print(f"\nTop venues/conferences:")
            for venue, count in sorted(venue_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                venue_display = venue[:50] + "..." if len(venue) > 50 else venue
                print(f"  • {venue_display}: {count} papers")

        # Content type distribution
        type_counts = {}
        for p in papers:
            if p.content_type:
                type_counts[p.content_type] = type_counts.get(p.content_type, 0) + 1

        if type_counts:
            print(f"\nContent types:")
            for content_type, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
                print(f"  • {content_type}: {count} papers")

        # Citation statistics
        crossref_citations = [p.citations for p in papers if p.citations is not None]
        ss_citations = [p.semantic_scholar_citations for p in papers if p.semantic_scholar_citations is not None]

        if crossref_citations:
            print(f"\nCitation statistics (Crossref):")
            print(f"  • Total papers with citations: {len(crossref_citations)}")
            print(f"  • Average citations: {sum(crossref_citations) / len(crossref_citations):.1f}")
            print(f"  • Max citations: {max(crossref_citations)}")
            print(f"  • Median citations: {sorted(crossref_citations)[len(crossref_citations)//2]}")

        if ss_citations:
            print(f"\nCitation statistics (Semantic Scholar):")
            print(f"  • Total papers with citations: {len(ss_citations)}")
            print(f"  • Average citations: {sum(ss_citations) / len(ss_citations):.1f}")
            print(f"  • Max citations: {max(ss_citations)}")
            print(f"  • Median citations: {sorted(ss_citations)[len(ss_citations)//2]}")

        # Most cited papers
        if crossref_citations or ss_citations:
            print(f"\nMost cited papers:")
            # Use SS citations if available, otherwise Crossref
            papers_with_citations = [(p, p.semantic_scholar_citations if p.semantic_scholar_citations is not None else p.citations)
                                    for p in papers
                                    if p.semantic_scholar_citations is not None or p.citations is not None]
            papers_with_citations.sort(key=lambda x: x[1], reverse=True)

            for p, cites in papers_with_citations[:3]:
                title_display = p.title[:70] + "..." if len(p.title) > 70 else p.title
                print(f"  • {title_display}")
                print(f"    Citations: {cites} | Year: {p.year}")

    else:
        print("\n❌ No papers found matching your criteria")

    print(f"\n{'=' * 60}")
    print("Crawler execution completed successfully!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()