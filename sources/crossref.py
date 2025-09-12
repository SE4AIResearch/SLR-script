"""
Crossref Paper Crawler
A tool to search and extract academic papers from Crossref API with comprehensive metadata
"""

import argparse
import csv
import json
import time
import re
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Optional
import requests

try:
    from abstract import UniversalAbstractFetcher
except Exception:
    UniversalAbstractFetcher = None  # If module isn't available, skip enrichment


# Configuration
USER_AGENT = "Academic Paper Crawler/1.0 (https://example.org/contact)"
CROSSREF_API_URL = "https://api.crossref.org/works"
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
    publisher_label: Optional[str] = None
    issn: List[str] = field(default_factory=list)
    pages: Optional[str] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    language: Optional[str] = None
    subjects: List[str] = field(default_factory=list)
    references_count: Optional[int] = None
    funders: List[str] = field(default_factory=list)


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


# Publisher label helper
def guess_publisher_label(doi: Optional[str], publisher: Optional[str]) -> Optional[str]:
    """Return a short publisher label like 'ACM', 'IEEE', etc., using the Crossref publisher name and/or DOI prefix."""
    name = (publisher or "").lower()
    # First try by known substrings in publisher name
    if "association for computing machinery" in name or name == "acm" or " acm" in name:
        return "ACM"
    if "ieee" in name:
        return "IEEE"
    if "springer" in name:
        return "Springer"
    if "elsevier" in name or "cell press" in name or "sciencedirect" in name:
        return "Elsevier"
    if "wiley" in name or "john wiley" in name:
        return "Wiley"
    if name == "nature" or "springer nature" in name:
        return "Nature"
    if "plos" in name:
        return "PLOS"
    if "oxford" in name:
        return "OUP"
    if "cambridge" in name:
        return "CUP"

    # Then try by DOI prefix patterns
    if doi:
        d = doi.lower()
        if d.startswith("10.1145"):
            return "ACM"
        if d.startswith("10.1109"):
            return "IEEE"
        if d.startswith("10.1007"):
            return "Springer"
        if d.startswith("10.1016"):
            return "Elsevier"
        if d.startswith("10.1038"):
            return "Nature"
        if d.startswith("10.1002") or d.startswith("10.1111"):
            return "Wiley"
        if d.startswith("10.1371"):
            return "PLOS"

    # Fallback to the original publisher name if present
    return normalize_string(publisher) if publisher else None


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


def backoff_request(url: str, params: Dict[str, Any], headers: Dict[str, str] = None) -> requests.Response:
    """Make HTTP request with exponential backoff retry"""
    headers = headers or {}
    headers.setdefault("User-Agent", USER_AGENT)

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


def extract_actual_url(item: Dict[str, Any], doi: Optional[str]) -> Optional[str]:
    """Extract the actual publisher URL from Crossref item"""

    # First, check for direct link in the 'link' field
    links = item.get("link", []) or []
    for link_obj in links:
        if isinstance(link_obj, dict):
            url = link_obj.get("URL")
            if url and not url.startswith("https://doi.org"):
                return url

    # Check for resource links
    resource = item.get("resource", {})
    if resource:
        primary = resource.get("primary", {})
        if primary and primary.get("URL"):
            return primary["URL"]

    # Check URL field (sometimes contains actual URL)
    url_field = item.get("URL")
    if url_field and not url_field.startswith("https://doi.org"):
        return url_field

    # For specific publishers, construct the URL based on DOI patterns
    if doi:
        # Elsevier/ScienceDirect
        if doi.startswith("10.1016"):
            # Extract PII from DOI if possible, or try to fetch the redirect
            return None  # Will be resolved later
        # Springer
        elif doi.startswith("10.1007"):
            return f"https://link.springer.com/article/{doi}"
        # IEEE
        elif doi.startswith("10.1109"):
            return None  # IEEE URLs are complex, need resolution
        # Nature
        elif doi.startswith("10.1038"):
            return f"https://www.nature.com/articles/{doi.replace('10.1038/', '')}"
        # Wiley
        elif doi.startswith("10.1002") or doi.startswith("10.1111"):
            return f"https://onlinelibrary.wiley.com/doi/{doi}"
        # PLOS
        elif doi.startswith("10.1371"):
            return f"https://journals.plos.org/plosone/article?id={doi}"
        # ACM
        elif doi.startswith("10.1145"):
            return f"https://dl.acm.org/doi/{doi}"

    return None


def resolve_doi_to_url(doi: str, timeout: int = 10) -> Optional[str]:
    """Resolve a DOI to its actual URL by following redirects"""
    if not doi:
        return None

    doi_url = f"https://doi.org/{doi}"

    try:
        # Use HEAD request first (faster)
        response = requests.head(
            doi_url,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT}
        )

        # Get the final URL after all redirects
        final_url = response.url

        # Make sure we got a real publisher URL, not just the DOI URL
        if final_url and not final_url.startswith("https://doi.org"):
            return final_url

    except requests.exceptions.RequestException:
        # If HEAD fails, try GET (some servers don't support HEAD)
        try:
            response = requests.get(
                doi_url,
                allow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
                stream=True  # Don't download the whole page
            )
            response.close()  # Close immediately, we just need the URL

            final_url = response.url
            if final_url and not final_url.startswith("https://doi.org"):
                return final_url

        except requests.exceptions.RequestException:
            pass

    return None


def extract_paper_from_item(item: Dict[str, Any], resolve_urls: bool = False, abstract_fetcher: Any = None) -> Paper:
    """Extract Paper object from Crossref API response item"""

    # Title
    title = normalize_string(" ".join(item.get("title", []) or []))

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

    published_date = format_date(date_parts) if date_parts else None
    year = date_parts[0][0] if date_parts and date_parts[0] else None

    # DOI
    doi = clean_doi(item.get("DOI"))

    # Try to get actual URL
    link = extract_actual_url(item, doi)

    # If no direct URL found and resolve_urls is True, resolve the DOI
    if not link and doi and resolve_urls:
        link = resolve_doi_to_url(doi)

    # Fallback to DOI URL if no actual URL found
    if not link and doi:
        link = f"https://doi.org/{doi}"

    # Content type
    content_type = item.get("type")

    # Venue (journal/conference name)
    venue = normalize_string(" ".join(item.get("container-title", []) or []))
    if not venue:
        venue = normalize_string(" ".join(item.get("short-container-title", []) or []))

    # Abstract
    abstract = normalize_string(item.get("abstract")) if item.get("abstract") else None
    # If abstract is missing and enrichment is enabled, try fetching via UniversalAbstractFetcher
    if (not abstract) and abstract_fetcher is not None and UniversalAbstractFetcher is not None:
        try:
            fetched = abstract_fetcher.fetch_abstract(
                title=title or None,
                doi=doi or None,
                authors=authors or None,
                year=year or None,
                venue=venue or None,
            )
            if fetched and fetched.abstract:
                abstract = normalize_string(fetched.abstract)
        except Exception:
            # Swallow enrichment errors to keep core crawler robust
            pass

    # Keywords/Subjects
    keywords = []
    if item.get("subject"):
        keywords = [normalize_string(s) for s in item.get("subject", [])]

    # Citation count (Crossref provides reference count, not citation count)
    # Note: Crossref doesn't directly provide citation counts. You would need
    # to use a different service like OpenCitations or Semantic Scholar for this.
    citations = item.get("is-referenced-by-count")  # This is available for some items

    # Additional metadata
    publisher = normalize_string(item.get("publisher"))
    publisher_label = guess_publisher_label(doi, publisher)
    issn = item.get("ISSN", [])
    pages = item.get("page")
    volume = item.get("volume")
    issue = item.get("issue")
    language = item.get("language")

    # References count
    references_count = item.get("references-count")

    # Funders
    funders = []
    for funder in item.get("funder", []) or []:
        if funder.get("name"):
            funders.append(normalize_string(funder["name"]))

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
        publisher_label=publisher_label,
        issn=issn,
        pages=pages,
        volume=volume,
        issue=issue,
        language=language,
        subjects=keywords,  # Using keywords as subjects
        references_count=references_count,
        funders=funders
    )


class CrossrefCrawler:
    """Crossref API crawler for academic papers"""

    def __init__(self, email: Optional[str] = None, resolve_urls: bool = False, enhance_abstracts: bool = True):
        """
        Initialize crawler

        Args:
            email: Optional email for polite pool (gets better rate limits)
            resolve_urls: Whether to resolve DOIs to actual publisher URLs (slower but gets real URLs)
            enhance_abstracts: If True, enrich missing abstracts using UniversalAbstractFetcher (abstract.py)
        """
        self.email = email
        self.resolve_urls = resolve_urls
        self.headers = {"User-Agent": USER_AGENT}
        if email:
            self.headers["mailto"] = email
        # Abstract enrichment (uses abstract.py if available)
        self.enhance_abstracts = enhance_abstracts
        self.abstract_fetcher = None
        if self.enhance_abstracts and UniversalAbstractFetcher is not None:
            try:
                self.abstract_fetcher = UniversalAbstractFetcher(use_cache=True, verbose=False)
            except Exception:
                self.abstract_fetcher = None

    def search(
        self,
        query: str,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        limit: Optional[int] = None,  # 修改为可选参数
        sort: str = "relevance",
        filter_type: Optional[str] = None,
        resolve_urls: Optional[bool] = None
    ) -> List[Paper]:
        """
        Search papers on Crossref

        Args:
            query: Search query string
            year_from: Start year (inclusive)
            year_to: End year (inclusive)
            limit: Maximum number of results (None for unlimited)
            sort: Sort order ('relevance', 'published', 'updated', 'indexed')
            filter_type: Filter by content type (e.g., 'journal-article', 'proceedings-article')
            resolve_urls: Override default resolve_urls setting for this search

        Returns:
            List of Paper objects
        """
        # Use instance setting if not overridden
        if resolve_urls is None:
            resolve_urls = self.resolve_urls

        papers = []
        cursor = "*"
        rows_per_request = 1000  # 每次请求最大数量

        print(f"Searching Crossref for: '{query}'")
        if limit is None:
            print("No limit set - will fetch all available results")
        else:
            print(f"Limit: {limit} papers")

        if resolve_urls:
            print("Note: Resolving actual URLs (this may take longer)...")

        # 首先获取总数量信息
        initial_params = {
            "query": query,
            "rows": 1,  # 只获取一条记录来查看总数
            "cursor": "*",
            "sort": sort
        }

        # Add filters
        filters = []
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")
        if filter_type:
            filters.append(f"type:{filter_type}")

        if filters:
            initial_params["filter"] = ",".join(filters)

        try:
            response = backoff_request(CROSSREF_API_URL, initial_params, self.headers)
            data = response.json()
            total_results = data.get("message", {}).get("total-results", 0)
            print(f"Total available results: {total_results}")

            # 如果设置了limit，使用较小的值
            if limit is not None:
                target_count = min(limit, total_results)
                print(f"Will fetch: {target_count} papers")
            else:
                target_count = total_results
                print(f"Will fetch all {target_count} papers")

        except Exception as e:
            print(f"Warning: Could not get total count: {e}")
            target_count = limit if limit is not None else float('inf')

        # 重置cursor开始正式爬取
        cursor = "*"

        while True:
            # 计算这次请求需要多少条记录
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

            if filters:
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
                for i, item in enumerate(items):
                    if limit is not None and len(papers) >= limit:
                        break
                    try:
                        paper = extract_paper_from_item(item, resolve_urls=resolve_urls, abstract_fetcher=self.abstract_fetcher)
                        papers.append(paper)

                        # Progress indicator
                        if len(papers) % 100 == 0:
                            if limit is not None:
                                print(f"  Processed {len(papers)}/{limit} papers...")
                            else:
                                print(f"  Processed {len(papers)}/{target_count} papers...")

                    except Exception as e:
                        print(f"Warning: Failed to extract paper: {e}")
                        continue

                # Get next cursor
                next_cursor = message.get("next-cursor")
                if not next_cursor:
                    print("Reached end of results (no more cursor)")
                    break
                cursor = next_cursor

                # 如果已达到目标数量，退出
                if limit is not None and len(papers) >= limit:
                    break

            except Exception as e:
                print(f"Error during search: {e}")
                break

        return papers

    def get_by_doi(self, doi: str, resolve_url: Optional[bool] = None) -> Optional[Paper]:
        """
        Get a specific paper by DOI

        Args:
            doi: The DOI of the paper
            resolve_url: Whether to resolve DOI to actual publisher URL

        Returns:
            Paper object or None if not found
        """
        if resolve_url is None:
            resolve_url = self.resolve_urls

        clean_doi_str = clean_doi(doi)
        if not clean_doi_str:
            return None

        url = f"{CROSSREF_API_URL}/{clean_doi_str}"

        try:
            response = requests.get(url, headers=self.headers, timeout=DEFAULT_TIMEOUT)
            if response.status_code == 200:
                data = response.json()
                item = data.get("message", {})
                return extract_paper_from_item(item, resolve_urls=resolve_url, abstract_fetcher=self.abstract_fetcher)
        except Exception as e:
            print(f"Error fetching DOI {doi}: {e}")

        return None


def save_to_csv(papers: List[Paper], filename: str):
    """Save papers to CSV file"""
    if not papers:
        print("No papers to save")
        return

    # Define fields to export
    fieldnames = [
        'title', 'authors', 'published_date', 'year', 'link',
        'content_type', 'doi', 'abstract', 'venue', 'keywords',
        'citations', 'publisher', 'publisher_label', 'issn', 'pages', 'volume',
        'issue', 'references_count'
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
                'abstract': paper.abstract[:500] if paper.abstract else '',  # Truncate long abstracts
                'venue': paper.venue,
                'keywords': '; '.join(paper.keywords),
                'citations': paper.citations,
                'publisher': paper.publisher,
                'publisher_label': paper.publisher_label,
                'issn': '; '.join(paper.issn) if paper.issn else '',
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
        description="Crawl academic papers from Crossref API"
    )
    parser.add_argument(
        "query",
        help="Search query string"
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
        help="Maximum number of results (omit for unlimited)"  # 修改帮助文本
    )
    parser.add_argument(
        "--sort",
        choices=["relevance", "published", "updated", "indexed"],
        default="relevance",
        help="Sort order (default: relevance)"
    )
    parser.add_argument(
        "--type",
        help="Filter by content type (e.g., journal-article, proceedings-article)"
    )
    parser.add_argument(
        "--email",
        help="Email for polite pool (gets better rate limits)"
    )
    parser.add_argument(
        "--output",
        default="crossref_papers",
        help="Output filename (without extension, default: crossref_papers)"
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json", "jsonl", "all"],
        default="csv",
        help="Output format (default: csv)"
    )
    parser.add_argument(
        "--doi",
        help="Get a specific paper by DOI instead of searching"
    )
    parser.add_argument(
        "--resolve-urls",
        action="store_true",
        help="Resolve DOIs to actual publisher URLs (slower but gets real URLs like ScienceDirect)"
    )
    parser.add_argument(
        "--no-enhance-abstracts",
        action="store_true",
        help="Disable abstract enrichment. Default: enrich abstracts from external sources when Crossref lacks them."
    )

    args = parser.parse_args()

    # Determine whether to resolve URLs
    resolve_urls = args.resolve_urls

    # Initialize crawler
    crawler = CrossrefCrawler(email=args.email, resolve_urls=resolve_urls,
                              enhance_abstracts=(not args.no_enhance_abstracts))
    # Get papers
    if args.doi:
        print(f"Fetching paper with DOI: {args.doi}")
        paper = crawler.get_by_doi(args.doi)
        papers = [paper] if paper else []
    else:
        print(f"Searching for: {args.query}")
        if args.year_from or args.year_to:
            print(f"Year range: {args.year_from or 'any'} - {args.year_to or 'any'}")

        papers = crawler.search(
            query=args.query,
            year_from=args.year_from,
            year_to=args.year_to,
            limit=args.limit,  # 现在可以是None
            sort=args.sort,
            filter_type=args.type
        )

    print(f"Found {len(papers)} papers")

    # Save results
    if papers:
        if args.format in ["csv", "all"]:
            csv_file = f"{args.output}.csv"
            save_to_csv(papers, csv_file)
            print(f"Saved to {csv_file}")

        if args.format in ["json", "all"]:
            json_file = f"{args.output}.json"
            save_to_json(papers, json_file)
            print(f"Saved to {json_file}")

        if args.format in ["jsonl", "all"]:
            jsonl_file = f"{args.output}.jsonl"
            save_to_jsonl(papers, jsonl_file)
            print(f"Saved to {jsonl_file}")

        # Print sample results
        print("\nSample results:")
        for i, paper in enumerate(papers[:3], 1):
            print(f"\n{i}. {paper.title}")
            print(f"   Authors: {', '.join(paper.authors[:3])}")
            print(f"   Year: {paper.year}")
            print(f"   DOI: {paper.doi}")
            print(f"   Venue: {paper.venue}")
            print(f"   Link: {paper.link}")
            if paper.abstract:
                print(f"   Abstract: {paper.abstract[:150]}...")


if __name__ == "__main__":
    main()