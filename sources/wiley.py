#!/usr/bin/env python3
"""
Enhanced Wiley Academic Search Scraper
Scrapes research papers from Wiley using CrossRef API with enhanced abstract fetching
"""

import argparse
import csv
import json
import re
import time
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Optional
import requests

# Import the abstract fetcher
from abstract import UniversalAbstractFetcher


@dataclass
class WileyPaper:
    """Data structure for Wiley papers with extended metadata"""
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
    publisher: str = "Wiley"
    abstract_source: Optional[str] = None  # Track where abstract came from
    abstract_confidence: Optional[float] = None


class WileyScraper:
    """
    Enhanced scraper for Wiley papers using CrossRef API with abstract fetching
    """

    def __init__(self, delay: float = 0.5, fetch_abstracts: bool = True,
                 abstract_delay: float = 0.3, verbose: bool = False):
        """
        Initialize scraper with request delay and abstract fetching options

        Args:
            delay: Seconds to wait between CrossRef requests
            fetch_abstracts: Whether to fetch missing abstracts from multiple sources
            abstract_delay: Seconds to wait between abstract API requests
            verbose: Print detailed progress information
        """
        self.delay = delay
        self.fetch_abstracts = fetch_abstracts
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Academic Research Bot 1.0 (mailto:research@example.com)'
        })

        # Initialize abstract fetcher if needed
        if self.fetch_abstracts:
            self.abstract_fetcher = UniversalAbstractFetcher(
                use_cache=True,
                delay=abstract_delay,
                verbose=verbose
            )
            print("🔍 Abstract fetching enabled - will try multiple sources for missing abstracts")
        else:
            self.abstract_fetcher = None

    def search(self,
               query: str,
               year_from: Optional[int] = None,
               year_to: Optional[int] = None,
               limit: Optional[int] = None) -> List[WileyPaper]:
        """
        Search for Wiley papers via CrossRef API with enhanced abstract fetching

        Args:
            query: Search query (supports basic boolean: AND, OR, NOT)
            year_from: Start year (inclusive)
            year_to: End year (inclusive)
            limit: Maximum number of results (None = get all available)

        Returns:
            List of WileyPaper objects with enhanced abstracts
        """
        print("Using CrossRef API to search Wiley content...")

        # Process complex boolean queries
        if self._is_complex_query(query):
            print("Processing complex boolean query...")
            search_queries = self._expand_boolean_query(query)
            if self.verbose:
                print(f"  Generated {len(search_queries)} query variants")
        else:
            search_queries = [query]

        all_papers = []
        seen_dois = set()

        for i, search_query in enumerate(search_queries):
            if limit and len(all_papers) >= limit:
                break

            print(f"Searching with query {i + 1}/{len(search_queries)}: {search_query[:80]}...")
            papers = self._search_crossref(search_query, year_from, year_to,
                                           limit - len(all_papers) if limit else None)

            # Deduplicate and enhance with abstracts
            for paper in papers:
                if paper.doi and paper.doi not in seen_dois:
                    seen_dois.add(paper.doi)
                    enhanced_paper = self._enhance_paper_with_abstract(paper)
                    all_papers.append(enhanced_paper)
                elif not paper.doi:
                    # Include papers without DOI (rare but possible)
                    enhanced_paper = self._enhance_paper_with_abstract(paper)
                    all_papers.append(enhanced_paper)

            time.sleep(self.delay)

        if limit:
            return all_papers[:limit]
        return all_papers

    def _enhance_paper_with_abstract(self, paper: WileyPaper) -> WileyPaper:
        """
        Enhance paper with better abstract if needed

        Args:
            paper: Original WileyPaper object

        Returns:
            Enhanced WileyPaper with improved abstract
        """
        # If we already have a good abstract, keep it but try to improve
        original_abstract = paper.abstract
        should_fetch = False

        if not original_abstract:
            should_fetch = True
            if self.verbose:
                print(f"  📄 No abstract found for: {paper.title[:60]}...")
        elif len(original_abstract) < 100:
            should_fetch = True
            if self.verbose:
                print(f"  📝 Short abstract ({len(original_abstract)} chars) for: {paper.title[:60]}...")
        elif self.fetch_abstracts and self.verbose:
            print(f"  ✓ Abstract present ({len(original_abstract)} chars) for: {paper.title[:60]}...")

        # Try to fetch better abstract if needed
        if should_fetch and self.fetch_abstracts and self.abstract_fetcher:
            try:
                if self.verbose:
                    print(f"    🔍 Fetching abstract from multiple sources...")

                result = self.abstract_fetcher.fetch_abstract(
                    title=paper.title,
                    doi=paper.doi,
                    authors=paper.authors,
                    year=paper.year,
                    venue=paper.venue
                )

                if result and result.abstract:
                    # Use the fetched abstract if it's better
                    if not original_abstract or len(result.abstract) > len(original_abstract):
                        paper.abstract = result.abstract
                        paper.abstract_source = result.source
                        paper.abstract_confidence = result.confidence

                        if self.verbose:
                            print(f"    ✅ Enhanced abstract from {result.source} "
                                  f"(confidence: {result.confidence:.2f})")
                    else:
                        if self.verbose:
                            print(f"    ℹ️  Original abstract is better than {result.source}")
                else:
                    if self.verbose:
                        print(f"    ❌ No abstract found from external sources")

            except Exception as e:
                if self.verbose:
                    print(f"    ⚠️  Error fetching abstract: {e}")

        return paper

    def _resolve_real_url(self, doi: str, venue: str = None, publisher: str = None) -> Optional[str]:
        """
        Resolve DOI to actual publisher URL by following redirects
        """
        if not doi:
            return None

        try:
            # First try to construct known publisher URLs
            if doi.startswith('10.1002/'):
                return f"https://onlinelibrary.wiley.com/doi/{doi}"
            elif doi.startswith('10.1111/'):
                return f"https://onlinelibrary.wiley.com/doi/{doi}"
            elif doi.startswith('10.1046/'):
                return f"https://onlinelibrary.wiley.com/doi/{doi}"
            elif doi.startswith('10.1038/'):
                return f"https://www.nature.com/articles/{doi}"

            # For other DOIs, follow the redirect from doi.org (but don't actually fetch)
            # Most publishers redirect doi.org/10.xxxx to their actual page
            doi_url = f"https://doi.org/{doi}"

            # Try to resolve redirect without fetching full content
            response = self.session.head(doi_url, allow_redirects=True, timeout=5)
            if response.url != doi_url:
                return response.url

            return doi_url

        except Exception as e:
            if self.verbose:
                print(f"    Warning: Could not resolve URL for DOI {doi}: {e}")
            return f"https://doi.org/{doi}"

    def _search_crossref(self,
                         query: str,
                         year_from: Optional[int],
                         year_to: Optional[int],
                         limit: Optional[int]) -> List[WileyPaper]:
        """Search using CrossRef API filtered for Wiley content"""
        papers = []

        url = "https://api.crossref.org/works"

        # Prepare initial parameters
        params = {
            "query": query,
            "rows": 100 if not limit else min(100, limit)
        }

        # Add Wiley filter - these are Wiley's DOI prefixes
        filters = []

        # Wiley DOI prefixes - expanded for better coverage
        wiley_prefixes = [
            "10.1002",  # Main Wiley prefix
            "10.1111",  # Wiley-Blackwell
            "10.1046",  # Older Blackwell
            "10.1038",  # Nature (partially Wiley)
            "10.1113",  # Physiological Society (Wiley)
            "10.1049",  # IET (Institution of Engineering and Technology)
            "10.1155",  # Hindawi (acquired by Wiley)
        ]

        filters.append("prefix:" + ",prefix:".join(wiley_prefixes))

        # Add year filters
        if year_from:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to:
            filters.append(f"until-pub-date:{year_to}-12-31")

        if filters:
            params["filter"] = ",".join(filters)

        # Add fields to retrieve
        params[
            "select"] = "DOI,title,author,published-print,published-online,container-title,type,abstract,subject,is-referenced-by-count,volume,issue,page,publisher"

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

                if self.verbose:
                    print(f"  Retrieved {total_retrieved} papers so far...")

                # Check if we've reached the limit
                if limit and total_retrieved >= limit:
                    break

                # Get next cursor for pagination
                next_cursor = message.get("next-cursor")
                if not next_cursor:
                    break
                cursor = next_cursor

                # Be respectful to the API
                time.sleep(self.delay)

            except requests.exceptions.RequestException as e:
                print(f"Warning: Request failed: {e}")
                break
            except Exception as e:
                print(f"Warning: Unexpected error: {e}")
                break

        return papers

    def _parse_crossref_item(self, item: Dict[str, Any]) -> Optional[WileyPaper]:
        """Parse a paper from CrossRef response"""
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

            # Extract URL
            link = item.get('URL', '')
            if not link and doi:
                link = f"https://doi.org/{doi}"

            # Extract abstract
            abstract = item.get('abstract', '').strip()
            # Remove XML/HTML tags from abstract if present
            abstract = re.sub(r'<[^>]+>', '', abstract)

            # Track original source
            abstract_source = "CrossRef" if abstract else None
            abstract_confidence = 1.0 if abstract else None

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
            publisher = item.get('publisher', 'Wiley')

            return WileyPaper(
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
                publisher=publisher,
                abstract_source=abstract_source,
                abstract_confidence=abstract_confidence
            )

        except Exception as e:
            print(f"Warning: Failed to parse item: {e}")
            return None

    def _is_complex_query(self, query: str) -> bool:
        """Check if query is complex and needs boolean expansion"""
        # Check for boolean operators
        bool_ops = len(re.findall(r'\b(AND|OR|NOT)\b', query, re.IGNORECASE))
        paren_count = query.count('(')

        return (bool_ops > 2) or (paren_count > 1) or (len(query) > 200)

    def _expand_boolean_query(self, query: str, max_combos: int = 8) -> List[str]:
        """
        Expand boolean query into multiple simple queries
        Based on the universal search tool's expand_boolean_queries_for_weak_provider
        """
        if not query:
            return []

        # Normalize: fix hyphenation at EOL and collapse whitespace
        q_norm = query.replace("\r", "")
        q_norm = re.sub(r"-\s*\n\s*", "", q_norm)
        q_norm = re.sub(r"\s+", " ", q_norm).strip()

        # Strip outer parentheses repeatedly
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

        # Split by top-level AND into groups
        and_groups = self._split_top_level_and(expr)
        if len(and_groups) < 2 and 'AND' in expr.upper():
            # Fallback: try parentheses-aware split
            and_groups = self._split_and_groups_by_parens(expr)

        or_lists: List[List[str]] = []
        for g in and_groups:
            g = self._strip_outer_parens(g)
            alts = self._split_top_level_or(g)
            if not alts:
                alts = [g]
            cleaned = []
            for a in alts:
                a2 = a.strip()
                a2 = re.sub(r'\b(AND|OR|NOT|ANDNOT)\b', ' ', a2, flags=re.IGNORECASE)
                a2 = re.sub(r'^[()\s]+|[()\s]+$', '', a2)
                if a2:
                    cleaned.append(a2)
            if cleaned:
                or_lists.append(cleaned)

        if not or_lists:
            return [expr]

        # Cartesian product with cap
        def _maybe_quote(s: str) -> str:
            s = s.strip()
            if not s:
                return s
            if s.startswith('"') and s.endswith('"'):
                return s
            if any(ch.isspace() for ch in s):
                return f'"{s}"'
            return s

        combos: List[List[str]] = [[]]
        for group in or_lists:
            new: List[List[str]] = []
            for base in combos:
                for alt in group:
                    new.append(base + [alt])

            # Sample if too many combinations
            if len(new) > max_combos:
                alt_count = len(group) if len(group) > 0 else 1
                sampled = []
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

    def _split_top_level_and(self, expr: str) -> List[str]:
        """Split expression by top-level AND, respecting quotes and parentheses"""
        return self._split_top_level(expr, 'AND')

    def _split_top_level_or(self, expr: str) -> List[str]:
        """Split expression by top-level OR, respecting quotes and parentheses"""
        return self._split_top_level(expr, 'OR')

    def _split_top_level(self, expr: str, sep: str) -> List[str]:
        """Split expression by top-level separator, respecting quotes and parentheses"""
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
                    pre_ok = (i == 0) or (not expr[i - 1].isalnum())
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

    def _strip_outer_parens(self, s: str) -> str:
        """Remove outer parentheses if they wrap the entire string"""
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

    def _split_and_groups_by_parens(self, expr: str) -> List[str]:
        """Alternative AND splitting that's parentheses-aware"""
        groups = []
        buf = []
        depth = 0
        in_q = False
        i, n = 0, len(expr)
        while i < n:
            ch = expr[i]
            if ch == '"':
                in_q = not in_q
                buf.append(ch);
                i += 1;
                continue
            if not in_q:
                if ch == '(':
                    if depth == 0 and buf and ''.join(buf).strip().upper().endswith('AND'):
                        s = ''.join(buf).strip()
                        s = re.sub(r'\bAND\s*$', '', s, flags=re.IGNORECASE)
                        if s:
                            groups.append(s)
                        buf = []
                    depth += 1
                elif ch == ')':
                    depth = max(0, depth - 1)
                else:
                    if depth == 0 and expr[i:].upper().startswith('AND'):
                        pre_ok = (i == 0) or (not expr[i - 1].isalnum())
                        post_ok = (i + 3 >= n) or (not expr[i + 3].isalnum())
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


def save_to_csv(papers: List[WileyPaper], filename: str):
    """Save papers to CSV file with abstract metadata"""
    if not papers:
        print("No papers to save")
        return

    fieldnames = [
        'title', 'authors', 'published_date', 'year', 'link',
        'content_type', 'doi', 'abstract', 'venue', 'keywords',
        'citations', 'volume', 'issue', 'pages', 'publisher',
        'abstract_source', 'abstract_confidence'
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

    # Print abstract statistics
    total_papers = len(papers)
    papers_with_abstracts = sum(1 for p in papers if p.abstract)
    enhanced_abstracts = sum(1 for p in papers if p.abstract_source and p.abstract_source != "CrossRef")

    print(f"✅ Saved {total_papers} papers to {filename}")
    print(
        f"   📊 Abstract coverage: {papers_with_abstracts}/{total_papers} ({papers_with_abstracts / total_papers * 100:.1f}%)")
    if enhanced_abstracts > 0:
        print(f"   🔍 Enhanced abstracts: {enhanced_abstracts} papers")


def save_to_json(papers: List[WileyPaper], filename: str):
    """Save papers to JSON file"""
    if not papers:
        print("No papers to save")
        return

    data = [asdict(paper) for paper in papers]

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Print abstract statistics
    total_papers = len(papers)
    papers_with_abstracts = sum(1 for p in papers if p.abstract)
    enhanced_abstracts = sum(1 for p in papers if p.abstract_source and p.abstract_source != "CrossRef")

    print(f"✅ Saved {total_papers} papers to {filename}")
    print(
        f"   📊 Abstract coverage: {papers_with_abstracts}/{total_papers} ({papers_with_abstracts / total_papers * 100:.1f}%)")
    if enhanced_abstracts > 0:
        print(f"   🔍 Enhanced abstracts: {enhanced_abstracts} papers")


def main():
    parser = argparse.ArgumentParser(
        description="Enhanced Wiley academic papers scraper with complex boolean query support and multi-source abstract fetching",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simple search with abstract enhancement
  python enhanced_wiley.py "machine learning"

  # Complex boolean query (will be automatically expanded)
  python enhanced_wiley.py "((extract method OR method extract) AND (refactor OR refactoring))"

  # Search without abstract fetching (faster)
  python enhanced_wiley.py "neural networks" --no-abstracts

  # Verbose output to see query expansion and abstract fetching progress
  python enhanced_wiley.py "deep learning" --verbose

  # Search with year range and abstract enhancement
  python enhanced_wiley.py "artificial intelligence" --year-from 2020 --year-to 2023

  # Limit results and customize delays
  python enhanced_wiley.py "robotics" --limit 50 --delay 1.0 --abstract-delay 0.5
        """
    )

    parser.add_argument('query', help='Search query (supports complex boolean: AND, OR, NOT with parentheses)')
    parser.add_argument('--year-from', type=int, help='Start year (inclusive)')
    parser.add_argument('--year-to', type=int, help='End year (inclusive)')
    parser.add_argument('--limit', type=int, default=None,
                        help='Maximum number of results (default: no limit)')
    parser.add_argument('--output', default='enhanced_wiley_results',
                        help='Output filename (without extension)')
    parser.add_argument('--format', choices=['csv', 'json', 'both'], default='both',
                        help='Output format (default: both)')
    parser.add_argument('--delay', type=float, default=0.5,
                        help='Delay between CrossRef API requests in seconds (default: 0.5)')
    parser.add_argument('--abstract-delay', type=float, default=0.3,
                        help='Delay between abstract API requests in seconds (default: 0.3)')
    parser.add_argument('--no-abstracts', action='store_true',
                        help='Disable abstract fetching from external sources (faster)')
    parser.add_argument('--verbose', action='store_true',
                        help='Print detailed progress information including query expansion')

    args = parser.parse_args()

    # Create enhanced scraper
    scraper = WileyScraper(
        delay=args.delay,
        fetch_abstracts=not args.no_abstracts,
        abstract_delay=args.abstract_delay,
        verbose=args.verbose
    )

    # Display search info
    print(f"🔍 Searching for: {args.query}")
    if args.year_from or args.year_to:
        print(f"📅 Year range: {args.year_from or 'any'} - {args.year_to or 'any'}")
    if args.limit:
        print(f"📊 Limit: {args.limit} papers")
    else:
        print("📊 Fetching all available results...")

    if not args.no_abstracts:
        print("🔍 Abstract enhancement: ENABLED")
    else:
        print("🔍 Abstract enhancement: DISABLED")

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
    else:
        print("No papers found")


if __name__ == "__main__":
    main()