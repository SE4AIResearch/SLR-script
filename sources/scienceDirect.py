#!/usr/bin/env python3
"""
ScienceDirect Academic Search Scraper
Scrapes research papers from ScienceDirect using Elsevier API
"""


import argparse
import csv
import json
import re
import time
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Optional
import requests

# Optional abstract enrichment (uses user's abstract.py)
try:
    from abstract import UniversalAbstractFetcher
except Exception:
    UniversalAbstractFetcher = None  # If module isn't available, skip enrichment


@dataclass
class ScienceDirectPaper:
    """Data structure for ScienceDirect papers with extended metadata"""
    title: str
    authors: List[str]
    published_date: Optional[str] = None
    year: Optional[int] = None
    link: Optional[str] = None
    content_type: Optional[str] = None
    doi: Optional[str] = None
    abstract: Optional[str] = None
    venue: Optional[str] = None  # Journal name
    keywords: List[str] = field(default_factory=list)
    citations: Optional[int] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None
    publisher: str = "Elsevier"


class ScienceDirectScraper:
    """
    Scraper for ScienceDirect papers using multiple APIs
    Combines CrossRef API (for metadata) and web scraping for ScienceDirect-specific content
    """

    def __init__(self, api_key: Optional[str] = None, delay: float = 0.5, enhance_abstracts: bool = True):
        """
        Initialize scraper with optional API key and request delay

        Args:
            api_key: Elsevier API key (optional, but recommended for better access)
            delay: Seconds to wait between requests
            enhance_abstracts: Whether to enrich abstracts using external sources (default: True)
        """
        self.api_key = api_key
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Academic Research Bot 1.0 (mailto:research@example.com)',
            'Accept': 'application/json'
        })

        if self.api_key:
            self.session.headers['X-ELS-APIKey'] = self.api_key

        # Abstract enrichment (default ON)
        self.enhance_abstracts = enhance_abstracts
        self.abstract_fetcher = None
        if self.enhance_abstracts and UniversalAbstractFetcher is not None:
            try:
                self.abstract_fetcher = UniversalAbstractFetcher(use_cache=True, verbose=False)
            except Exception:
                self.abstract_fetcher = None

    def search(self,
               query: str,
               year_from: Optional[int] = None,
               year_to: Optional[int] = None,
               limit: Optional[int] = None) -> List[ScienceDirectPaper]:
        """
        Search for ScienceDirect papers

        Args:
            query: Search query (supports boolean: AND, OR, NOT)
            year_from: Start year (inclusive)
            year_to: End year (inclusive)
            limit: Maximum number of results

        Returns:
            List of ScienceDirectPaper objects
        """
        print("Searching ScienceDirect/Elsevier content...")

        # Try multiple search strategies
        papers = []

        # Strategy 1: Use Elsevier Search API if API key is available
        if self.api_key:
            print("Using Elsevier API...")
            papers = self._search_elsevier_api(query, year_from, year_to, limit)

        # Strategy 2: Use CrossRef API filtered for Elsevier
        if not papers or (limit and len(papers) < limit):
            print("Using CrossRef API for Elsevier content...")
            crossref_papers = self._search_crossref(query, year_from, year_to,
                                                    limit - len(papers) if limit else None)
            papers.extend(crossref_papers)

        # Deduplicate by DOI
        seen_dois = set()
        unique_papers = []
        for paper in papers:
            if paper.doi and paper.doi not in seen_dois:
                seen_dois.add(paper.doi)
                unique_papers.append(paper)
            elif not paper.doi:
                unique_papers.append(paper)

        if limit:
            return unique_papers[:limit]
        return unique_papers

    def _search_elsevier_api(self,
                             query: str,
                             year_from: Optional[int],
                             year_to: Optional[int],
                             limit: Optional[int]) -> List[ScienceDirectPaper]:
        """Search using Elsevier Search API"""
        papers = []

        # Elsevier Search API endpoint
        url = "https://api.elsevier.com/content/search/sciencedirect"

        # Build query string
        search_query = query
        if year_from or year_to:
            year_filter = ""
            if year_from and year_to:
                year_filter = f" AND PUBYEAR AFT {year_from - 1} AND PUBYEAR BEF {year_to + 1}"
            elif year_from:
                year_filter = f" AND PUBYEAR AFT {year_from - 1}"
            elif year_to:
                year_filter = f" AND PUBYEAR BEF {year_to + 1}"
            search_query += year_filter

        # Parameters
        start = 0
        count = 25  # Max per request for Elsevier API
        total_retrieved = 0

        while True:
            params = {
                'query': search_query,
                'start': start,
                'count': count,
                'view': 'COMPLETE'  # Get full metadata
            }

            try:
                response = self.session.get(url, params=params, timeout=30)

                if response.status_code == 401:
                    print("API key invalid or missing. Skipping Elsevier API...")
                    break
                elif response.status_code == 429:
                    print("Rate limit reached. Waiting...")
                    time.sleep(5)
                    continue

                response.raise_for_status()
                data = response.json()

                # Parse search results
                search_results = data.get('search-results', {})
                entries = search_results.get('entry', [])

                if not entries:
                    break

                for entry in entries:
                    paper = self._parse_elsevier_entry(entry)
                    if paper:
                        papers.append(paper)
                        total_retrieved += 1

                print(f"  Retrieved {total_retrieved} papers from Elsevier API...")

                # Check if we've reached the limit
                if limit and total_retrieved >= limit:
                    break

                # Check if there are more results
                total_results = int(search_results.get('opensearch:totalResults', 0))
                if start + count >= total_results:
                    break

                start += count
                time.sleep(self.delay)

            except requests.exceptions.RequestException as e:
                print(f"Warning: Elsevier API request failed: {e}")
                break
            except Exception as e:
                print(f"Warning: Unexpected error with Elsevier API: {e}")
                break

        return papers

    def _parse_elsevier_entry(self, entry: Dict[str, Any]) -> Optional[ScienceDirectPaper]:
        """Parse paper from Elsevier API response"""
        try:
            # Extract title
            title = entry.get('dc:title', '').strip()
            if not title:
                return None

            # Extract DOI
            doi = entry.get('prism:doi', '').strip()

            # Extract authors
            authors = []
            creator = entry.get('dc:creator')
            if creator:
                authors.append(creator)
            # Sometimes authors are in a different field
            author_list = entry.get('authors', {}).get('author', [])
            if isinstance(author_list, list):
                for author in author_list:
                    name = author.get('$', '')
                    if name and name not in authors:
                        authors.append(name)

            # Extract dates
            cover_date = entry.get('prism:coverDate', '')
            year = None
            published_date = cover_date
            if cover_date:
                try:
                    year = int(cover_date[:4])
                except:
                    pass

            # Extract venue (journal name)
            venue = entry.get('prism:publicationName', '')

            # Extract link - construct ScienceDirect URL
            pii = entry.get('pii', '')
            link = None
            if pii:
                link = f"https://www.sciencedirect.com/science/article/pii/{pii}"
            elif doi:
                # Construct from DOI if PII not available
                link = f"https://www.sciencedirect.com/science/article/doi/{doi}"
            else:
                # Fallback to the provided link
                links = entry.get('link', [])
                for link_item in links:
                    if isinstance(link_item, dict) and link_item.get('@ref') == 'scidir':
                        link = link_item.get('@href', '')
                        break

            # Extract abstract
            abstract = entry.get('dc:description', '').strip()

            # Enrich abstract if missing
            if (not abstract) and self.abstract_fetcher is not None:
                try:
                    fetched = self.abstract_fetcher.fetch_abstract(
                        title=title or None,
                        doi=doi or None,
                        authors=authors or None,
                        year=year or None,
                        venue=venue or None,
                    )
                    if fetched and fetched.abstract:
                        abstract = fetched.abstract.strip()
                except Exception:
                    pass

            # Extract content type
            content_type = entry.get('prism:aggregationType', 'Journal Article')

            # Extract volume, issue, pages
            volume = entry.get('prism:volume', '')
            issue = entry.get('prism:issueIdentifier', '')

            start_page = entry.get('prism:startingPage', '')
            end_page = entry.get('prism:endingPage', '')
            pages = f"{start_page}-{end_page}" if start_page and end_page else start_page or end_page or ''

            # Extract keywords (may not be available in search results)
            keywords = []

            return ScienceDirectPaper(
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
                citations=None,  # Not available in search API
                volume=str(volume) if volume else None,
                issue=str(issue) if issue else None,
                pages=str(pages) if pages else None,
                publisher="Elsevier"
            )

        except Exception as e:
            print(f"Warning: Failed to parse Elsevier entry: {e}")
            return None

    def _search_crossref(self,
                         query: str,
                         year_from: Optional[int],
                         year_to: Optional[int],
                         limit: Optional[int]) -> List[ScienceDirectPaper]:
        """Search using CrossRef API filtered for Elsevier content"""
        papers = []

        url = "https://api.crossref.org/works"

        # Prepare parameters
        params = {
            "query": query,
            "rows": 100 if not limit else min(100, limit)
        }

        # Add Elsevier filter
        filters = []

        # Elsevier DOI prefixes
        elsevier_prefixes = [
            "10.1016",  # Main Elsevier prefix (ScienceDirect)
            "10.1053",  # Elsevier Health Sciences
            "10.1067",  # Mosby (Elsevier)
            "10.1078",  # Urban & Fischer (Elsevier)
            "10.1205",  # IChemE (Elsevier)
            "10.1240",  # Elsevier BV
            "10.1367",  # Elsevier
            "10.1529",  # Biophysical Society (Elsevier)
            "10.3816",  # Elsevier
        ]
        filters.append("prefix:" + ",prefix:".join(elsevier_prefixes))

        # Add year filters
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")

        if filters:
            params["filter"] = ",".join(filters)

        # Add fields to retrieve
        params[
            "select"] = "DOI,title,author,published-print,published-online,container-title,type,abstract,subject,is-referenced-by-count,volume,issue,page,publisher,link"

        # Use cursor for pagination
        cursor = "*"
        total_retrieved = 0

        while True:
            params["cursor"] = cursor

            try:
                response = self.session.get(url, params=params, timeout=30)
                response.raise_for_status()

                data = response.json()
                message = data.get("message", {})
                items = message.get("items", [])

                if not items:
                    break

                for item in items:
                    paper = self._parse_crossref_item(item)
                    if paper:
                        papers.append(paper)
                        total_retrieved += 1

                print(f"  Retrieved {total_retrieved} papers from CrossRef...")

                # Check if we've reached the limit
                if limit and total_retrieved >= limit:
                    break

                # Get next cursor for pagination
                next_cursor = message.get("next-cursor")
                if not next_cursor:
                    break
                cursor = next_cursor

                time.sleep(self.delay)

            except requests.exceptions.RequestException as e:
                print(f"Warning: CrossRef request failed: {e}")
                break
            except Exception as e:
                print(f"Warning: Unexpected error with CrossRef: {e}")
                break

        return papers

    def _parse_crossref_item(self, item: Dict[str, Any]) -> Optional[ScienceDirectPaper]:
        """Parse paper from CrossRef response"""
        try:
            # Extract title
            title_list = item.get('title', [])
            title = ' '.join(title_list).strip() if title_list else ''
            if not title:
                return None

            # Extract DOI
            doi = item.get('DOI', '').strip()

            # Extract authors
            authors = []
            for author in item.get('author', []):
                given = author.get('given', '')
                family = author.get('family', '')
                name = f"{given} {family}".strip()
                if not name:
                    name = author.get('name', '')
                if name:
                    authors.append(name.strip())

            # Extract dates
            date_parts = item.get('published-print', {}).get('date-parts', [])
            if not date_parts:
                date_parts = item.get('published-online', {}).get('date-parts', [])

            year = None
            published_date = None
            if date_parts and date_parts[0]:
                year = date_parts[0][0] if len(date_parts[0]) > 0 else None
                if len(date_parts[0]) >= 3:
                    published_date = f"{date_parts[0][0]}-{date_parts[0][1]:02d}-{date_parts[0][2]:02d}"
                elif len(date_parts[0]) >= 2:
                    published_date = f"{date_parts[0][0]}-{date_parts[0][1]:02d}"
                elif len(date_parts[0]) == 1:
                    published_date = str(date_parts[0][0])

            # Extract venue (journal name)
            container_title = item.get('container-title', [])
            venue = ' '.join(container_title).strip() if container_title else ''

            # Extract link - construct proper ScienceDirect URL
            link = None

            # Try to get direct link from CrossRef
            link_list = item.get('link', [])
            for link_item in link_list:
                if isinstance(link_item, dict):
                    content_type = link_item.get('content-type', '')
                    url = link_item.get('URL', '')
                    if 'sciencedirect' in url.lower():
                        link = url
                        break

            # If no direct link, construct from DOI
            if not link and doi:
                # Extract the article identifier from DOI
                if doi.startswith('10.1016/'):
                    # Main Elsevier/ScienceDirect format
                    article_id = doi.replace('10.1016/', '')
                    # Convert DOI suffix to PII format if needed
                    article_id = article_id.replace('.', '')
                    link = f"https://www.sciencedirect.com/science/article/abs/pii/{article_id}"
                else:
                    # Generic ScienceDirect link
                    link = f"https://www.sciencedirect.com/science/article/doi/{doi}"

            # Fallback to DOI.org if still no link
            if not link and doi:
                link = f"https://doi.org/{doi}"

            # Extract abstract
            abstract = item.get('abstract', '').strip()
            # Remove XML/HTML tags from abstract
            abstract = re.sub(r'<[^>]+>', '', abstract)

            # Enrich abstract if missing
            if (not abstract) and self.abstract_fetcher is not None:
                try:
                    fetched = self.abstract_fetcher.fetch_abstract(
                        title=title or None,
                        doi=doi or None,
                        authors=authors or None,
                        year=year or None,
                        venue=venue or None,
                    )
                    if fetched and fetched.abstract:
                        abstract = fetched.abstract.strip()
                except Exception:
                    pass

            # Extract content type
            content_type = item.get('type', '')

            # Extract keywords/subjects
            keywords = item.get('subject', [])
            if not isinstance(keywords, list):
                keywords = []

            # Extract citation count
            citations = item.get('is-referenced-by-count')
            try:
                citations = int(citations) if citations is not None else None
            except (ValueError, TypeError):
                citations = None

            # Extract volume, issue, pages
            volume = item.get('volume', '')
            issue = item.get('issue', '')
            pages = item.get('page', '')

            # Extract publisher
            publisher = item.get('publisher', 'Elsevier')

            return ScienceDirectPaper(
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
                volume=str(volume) if volume else None,
                issue=str(issue) if issue else None,
                pages=str(pages) if pages else None,
                publisher=publisher
            )

        except Exception as e:
            print(f"Warning: Failed to parse CrossRef item: {e}")
            return None


def save_to_csv(papers: List[ScienceDirectPaper], filename: str):
    """Save papers to CSV file"""
    if not papers:
        print("No papers to save")
        return

    fieldnames = [
        'title', 'authors', 'published_date', 'year', 'link',
        'content_type', 'doi', 'abstract', 'venue', 'keywords',
        'citations', 'volume', 'issue', 'pages', 'publisher'
    ]

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for paper in papers:
            row = asdict(paper)
            # Convert lists to strings
            row['authors'] = '; '.join(row['authors']) if row['authors'] else ''
            row['keywords'] = '; '.join(row['keywords']) if row['keywords'] else ''
            # Truncate abstract if too long for CSV
            if row['abstract'] and len(row['abstract']) > 1000:
                row['abstract'] = row['abstract'][:997] + '...'
            writer.writerow(row)

    print(f"✅ Saved {len(papers)} papers to {filename}")


def save_to_json(papers: List[ScienceDirectPaper], filename: str):
    """Save papers to JSON file"""
    if not papers:
        print("No papers to save")
        return

    data = [asdict(paper) for paper in papers]

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"✅ Saved {len(papers)} papers to {filename}")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape ScienceDirect academic papers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simple search
  python sciencedirect.py "machine learning"

  # Search with year range
  python sciencedirect.py "neural networks" --year-from 2020 --year-to 2023

  # Limit results to 50
  python sciencedirect.py "deep learning" --limit 50

  # Complex boolean search
  python sciencedirect.py "(machine learning OR deep learning) AND healthcare"

  # Save to specific file
  python sciencedirect.py "robotics" --output results.csv --format csv

  # Use with Elsevier API key (recommended for better access)
  python sciencedirect.py "covid-19" --api-key YOUR_API_KEY

Note: For better results, you can obtain a free Elsevier API key from:
      https://dev.elsevier.com/
        """
    )

    parser.add_argument('query', help='Search query (supports AND, OR, NOT)')
    parser.add_argument('--year-from', type=int, help='Start year (inclusive)')
    parser.add_argument('--year-to', type=int, help='End year (inclusive)')
    parser.add_argument('--limit', type=int, default=None,
                        help='Maximum number of results (default: no limit)')
    parser.add_argument('--output', default='sciencedirect_results',
                        help='Output filename (without extension)')
    parser.add_argument('--format', choices=['csv', 'json', 'both'], default='both',
                        help='Output format (default: both)')
    parser.add_argument('--api-key', help='Elsevier API key (optional but recommended)')
    parser.add_argument('--delay', type=float, default=0.5,
                        help='Delay between API requests in seconds (default: 0.5)')
    parser.add_argument('--no-enhance-abstracts', action='store_true',
                        help='Disable abstract enrichment via Semantic Scholar/OpenAlex/PubMed/arXiv (default ON).')

    args = parser.parse_args()

    # Create scraper
    scraper = ScienceDirectScraper(api_key='a17167505f5d6799ad4cf9c9f28de7f1', delay=args.delay,
                                   enhance_abstracts=(not args.no_enhance_abstracts))

    # Display search info
    print(f"Searching for: {args.query}")
    if args.year_from or args.year_to:
        print(f"Year range: {args.year_from or 'any'} - {args.year_to or 'any'}")
    if args.limit:
        print(f"Limit: {args.limit} papers")
    else:
        print("Fetching all available results...")
    if args.api_key == 'a17167505f5d6799ad4cf9c9f28de7f1':
        print("Using Elsevier API key for enhanced access")
    else:
        print("No API key provided - using CrossRef API only")
        print("Tip: Get a free API key at https://dev.elsevier.com/ for better results")

    # Perform search
    start_time = time.time()
    papers = scraper.search(
        query=args.query,
        year_from=args.year_from,
        year_to=args.year_to,
        limit=args.limit
    )
    elapsed = time.time() - start_time

    print(f"\n✅ Found {len(papers)} papers in {elapsed:.1f} seconds")

    # Save results
    if papers:
        if args.format in ['csv', 'both']:
            save_to_csv(papers, f"{args.output}.csv")
        if args.format in ['json', 'both']:
            save_to_json(papers, f"{args.output}.json")

        # Show sample of results
        print("\nSample of results:")
        for i, paper in enumerate(papers[:3], 1):
            print(f"\n{i}. {paper.title}")
            print(f"   Authors: {', '.join(paper.authors[:3])}")
            if paper.link:
                print(f"   Link: {paper.link}")
    else:
        print("No papers found")


if __name__ == "__main__":
    main()