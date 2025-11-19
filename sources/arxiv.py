#!/usr/bin/env python3
"""
ArXiv Paper Crawler
Fetches papers from ArXiv with comprehensive metadata
"""

import argparse
import csv
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from typing import List, Optional, Dict, Any
import requests


@dataclass
class ArxivPaper:
    """Data class for ArXiv paper metadata"""
    title: str
    authors: List[str]
    published_date: str
    link: str
    content_type: str = "preprint"
    doi: Optional[str] = None
    abstract: Optional[str] = None
    venue: str = "arXiv"
    keywords: List[str] = field(default_factory=list)
    citations: Optional[int] = None  # Not available from ArXiv API
    arxiv_id: Optional[str] = None
    categories: List[str] = field(default_factory=list)
    comment: Optional[str] = None
    journal_ref: Optional[str] = None
    primary_category: Optional[str] = None
    updated_date: Optional[str] = None


class ArxivCrawler:
    """ArXiv paper crawler using the ArXiv API"""

    BASE_URL = "http://export.arxiv.org/api/query"
    NAMESPACES = {
        "a": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
        "opensearch": "http://a9.com/-/spec/opensearch/1.1/"
    }

    def __init__(self, delay: float = 3.0):
        """
        Initialize the crawler

        Args:
            delay: Delay between API calls in seconds (ArXiv recommends 3 seconds)
        """
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "ArxivCrawler/1.0 (https://example.org)"
        })

    def _normalize_text(self, text: Optional[str]) -> str:
        """Normalize text by removing extra whitespace and newlines"""
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _extract_arxiv_id(self, url: str) -> Optional[str]:
        """Extract ArXiv ID from URL"""
        if not url:
            return None
        match = re.search(r'/(?:abs|pdf)/(\d+\.\d+(?:v\d+)?)', url)
        if match:
            return match.group(1)
        match = re.search(r'/(?:abs|pdf)/([a-z-]+/\d+(?:v\d+)?)', url)
        if match:
            return match.group(1)
        return None

    def _parse_entry(self, entry: ET.Element) -> ArxivPaper:
        """Parse a single entry from ArXiv API response"""
        ns = self.NAMESPACES

        # Title
        title = self._normalize_text(
            entry.findtext("a:title", default="", namespaces=ns)
        )

        # Authors
        authors = []
        for author_elem in entry.findall("a:author", ns):
            name = author_elem.findtext("a:name", default="", namespaces=ns)
            if name:
                authors.append(self._normalize_text(name))

        # Dates
        published = entry.findtext("a:published", default="", namespaces=ns)
        updated = entry.findtext("a:updated", default="", namespaces=ns)

        # Link (prefer HTML abstract page)
        link = None
        pdf_link = None
        for link_elem in entry.findall("a:link", ns):
            rel = link_elem.attrib.get("rel", "")
            href = link_elem.attrib.get("href", "")
            if rel == "alternate":
                link = href
            elif href.endswith(".pdf"):
                pdf_link = href

        if not link:
            link = pdf_link

        # ArXiv ID
        arxiv_id = self._extract_arxiv_id(link) if link else None

        # Abstract
        abstract = self._normalize_text(
            entry.findtext("a:summary", default="", namespaces=ns)
        )

        # DOI
        doi = entry.findtext("arxiv:doi", default=None, namespaces=ns)
        if doi:
            doi = self._normalize_text(doi)

        # Categories
        categories = []
        primary_category = None

        primary_cat_elem = entry.find("arxiv:primary_category", ns)
        if primary_cat_elem is not None:
            primary_category = primary_cat_elem.attrib.get("term", "")
            if primary_category:
                categories.append(primary_category)

        for cat_elem in entry.findall("a:category", ns):
            term = cat_elem.attrib.get("term", "")
            if term and term not in categories:
                categories.append(term)

        # Comment
        comment = entry.findtext("arxiv:comment", default=None, namespaces=ns)
        if comment:
            comment = self._normalize_text(comment)

        # Journal reference
        journal_ref = entry.findtext("arxiv:journal_ref", default=None, namespaces=ns)
        if journal_ref:
            journal_ref = self._normalize_text(journal_ref)

        # Venue
        venue = "arXiv"
        if journal_ref:
            venue = f"arXiv (published in: {journal_ref})"

        return ArxivPaper(
            title=title,
            authors=authors,
            published_date=published,
            link=link,
            content_type="preprint",
            doi=doi,
            abstract=abstract,
            venue=venue,
            keywords=categories,
            citations=None,
            arxiv_id=arxiv_id,
            categories=categories,
            comment=comment,
            journal_ref=journal_ref,
            primary_category=primary_category,
            updated_date=updated
        )

    def _simplify_query(self, query: str) -> str:
        """
        Simplify complex boolean query for ArXiv
        Extract key terms without boolean operators
        """
        # Extract quoted phrases
        quoted_phrases = re.findall(r'"([^"]+)"', query)

        # Remove quotes, boolean operators, and parentheses
        clean_query = re.sub(r'"[^"]+"', ' ', query)
        clean_query = re.sub(r'\b(AND|OR|NOT|ANDNOT)\b', ' ', clean_query, flags=re.IGNORECASE)
        clean_query = re.sub(r'[()]', ' ', clean_query)
        clean_query = re.sub(r'\*', '', clean_query)  # Remove wildcards

        # Extract meaningful terms
        terms = []
        for term in clean_query.split():
            term = term.strip()
            if len(term) > 3:  # Skip short words
                terms.append(term)

        # Combine quoted phrases and terms
        all_terms = quoted_phrases[:5] + terms[:10]

        # Remove duplicates
        seen = set()
        unique_terms = []
        for term in all_terms:
            if term.lower() not in seen:
                seen.add(term.lower())
                unique_terms.append(term)

        # Create simple query for ArXiv
        if unique_terms:
            return ' '.join(unique_terms[:10])
        return query

    def search(self,
               query: str,
               max_results: Optional[int] = None,
               sort_by: str = "relevance",
               sort_order: str = "descending",
               start: int = 0,
               year_from: Optional[int] = None,
               year_to: Optional[int] = None) -> List[ArxivPaper]:
        """
        Search ArXiv for papers

        Args:
            query: Search query
            max_results: Maximum number of results (None for unlimited)
            sort_by: Sort criterion ('relevance', 'lastUpdatedDate', 'submittedDate')
            sort_order: Sort order ('ascending' or 'descending')
            start: Starting index for pagination

        Returns:
            List of ArxivPaper objects
        """
        papers = []
        batch_size = 100

        # Check if query needs simplification
        has_complex_boolean = ('(' in query and ')' in query and
                               any(op in query.upper() for op in ['AND', 'OR']))

        search_query = query

        initial_params = {
            "search_query": search_query,
            "start": start,
            "max_results": 1,
            "sortBy": sort_by,
            "sortOrder": sort_order
        }

        total_results = 0
        try:
            response = self.session.get(self.BASE_URL, params=initial_params, timeout=30)
            response.raise_for_status()
            root = ET.fromstring(response.text)
            total_results = int(root.findtext(
                "opensearch:totalResults",
                default="0",
                namespaces=self.NAMESPACES
            ))

            print(f"Total available results: {total_results}")
            if max_results is None:
                print("No limit set - will fetch all available results")
                target_count = total_results
            else:
                target_count = min(max_results, total_results)
                print(f"Will fetch: {target_count} papers")

        except Exception as e:
            print(f"Warning: Could not get total count: {e}")
            target_count = max_results if max_results is not None else float('inf')

        current_start = start

        while True:
            # 计算这次请求需要多少条记录
            if max_results is not None:
                remaining = max_results - len(papers)
                if remaining <= 0:
                    break
                current_batch_size = min(batch_size, remaining)
            else:
                current_batch_size = batch_size

            params = {
                "search_query": search_query,
                "start": current_start,
                "max_results": current_batch_size,
                "sortBy": sort_by,
                "sortOrder": sort_order
            }

            try:
                response = self.session.get(self.BASE_URL, params=params, timeout=30)
                response.raise_for_status()

                root = ET.fromstring(response.text)

                # 检查是否有结果
                current_total_results = int(root.findtext(
                    "opensearch:totalResults",
                    default="0",
                    namespaces=self.NAMESPACES
                ))

                if current_total_results == 0:
                    print("No more results available")
                    break

                entries = root.findall("a:entry", self.NAMESPACES)
                if not entries:
                    print("No more entries found")
                    break

                # 解析当前批次的论文
                batch_papers = []
                for entry in entries:
                    paper = self._parse_entry(entry)
                    batch_papers.append(paper)

                papers.extend(batch_papers)

                # 进度显示
                if len(papers) % 100 == 0 or len(batch_papers) < current_batch_size:
                    if max_results is not None:
                        print(f"  Retrieved {len(papers)}/{max_results} papers...")
                    else:
                        print(f"  Retrieved {len(papers)}/{target_count} papers...")

                # 如果这批次的结果少于请求的数量，说明已经到底了
                if len(batch_papers) < current_batch_size:
                    print("Reached end of results")
                    break

                # 如果已达到目标数量，退出
                if max_results is not None and len(papers) >= max_results:
                    break

                # 更新起始位置
                current_start += len(batch_papers)

                # API延迟
                time.sleep(self.delay)

            except Exception as e:
                print(f"Error fetching results: {e}")
                break

        # Apply publication year filter if provided
        if year_from is not None or year_to is not None:
            filtered = []
            for p in papers:
                year = None
                if p.published_date:
                    # Expect formats like "2023-04-21T16:00:00Z" or "2023-04-21"
                    m = re.match(r"(\d{4})", p.published_date)
                    if m:
                        try:
                            year = int(m.group(1))
                        except ValueError:
                            year = None

                # If we cannot parse a year and a range is requested, skip the paper
                if year is None:
                    continue

                if year_from is not None and year < year_from:
                    continue
                if year_to is not None and year > year_to:
                    continue

                filtered.append(p)

            papers = filtered

        # 如果设置了限制，确保不超过限制
        if max_results is not None:
            papers = papers[:max_results]

        return papers

    def search_by_id(self, arxiv_id: str) -> Optional[ArxivPaper]:
        """Fetch a specific paper by ArXiv ID"""
        papers = self.search(f"id:{arxiv_id}", max_results=1)
        return papers[0] if papers else None

    def search_by_author(self, author_name: str, max_results: Optional[int] = None) -> List[ArxivPaper]:
        """Search papers by author name"""
        query = f'au:"{author_name}"'
        print(f"Searching ArXiv papers by author: {author_name}")
        if max_results is None:
            print("No limit set - will fetch all available results for this author")
        return self.search(query, max_results=max_results)

    def search_by_category(self, category: str, max_results: Optional[int] = None) -> List[ArxivPaper]:
        """Search papers by category"""
        query = f"cat:{category}"
        print(f"Searching ArXiv papers in category: {category}")
        if max_results is None:
            print("No limit set - will fetch all available results for this category")
        return self.search(query, max_results=max_results)


def save_to_csv(papers: List[ArxivPaper], filepath: str):
    """Save papers to CSV file"""
    if not papers:
        print("No papers to save")
        return

    fieldnames = [
        "title", "authors", "published_date", "link", "content_type",
        "doi", "abstract", "venue", "keywords", "citations",
        "arxiv_id", "categories", "comment", "journal_ref",
        "primary_category", "updated_date"
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for paper in papers:
            row = asdict(paper)
            row["authors"] = "; ".join(paper.authors)
            row["keywords"] = "; ".join(paper.keywords)
            row["categories"] = "; ".join(paper.categories)
            writer.writerow(row)

    print(f"Saved {len(papers)} papers to {filepath}")


def save_to_json(papers: List[ArxivPaper], filepath: str):
    """Save papers to JSON file"""
    if not papers:
        print("No papers to save")
        return

    data = [asdict(paper) for paper in papers]

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(papers)} papers to {filepath}")


def save_to_jsonl(papers: List[ArxivPaper], filepath: str):
    """Save papers to JSONL file"""
    if not papers:
        print("No papers to save")
        return

    with open(filepath, "w", encoding="utf-8") as f:
        for paper in papers:
            json_line = json.dumps(asdict(paper), ensure_ascii=False)
            f.write(json_line + "\n")

    print(f"Saved {len(papers)} papers to {filepath}")


def main():
    parser = argparse.ArgumentParser(
        description="ArXiv Paper Crawler - Fetch papers with comprehensive metadata"
    )

    parser.add_argument(
        "query",
        nargs="?",
        help="Search query (supports ArXiv syntax)"
    )
    parser.add_argument(
        "--max-results",
        type=int,
        help="Maximum number of results (omit for unlimited)"
    )
    parser.add_argument(
        "--sort-by",
        choices=["relevance", "lastUpdatedDate", "submittedDate"],
        default="relevance",
        help="Sort criterion (default: relevance)"
    )
    parser.add_argument(
        "--sort-order",
        choices=["ascending", "descending"],
        default="descending",
        help="Sort order (default: descending)"
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Starting index for pagination (default: 0)"
    )
    parser.add_argument(
        "--year-from",
        type=int,
        help="Filter by publication year (inclusive lower bound)"
    )
    parser.add_argument(
        "--year-to",
        type=int,
        help="Filter by publication year (inclusive upper bound)"
    )
    parser.add_argument(
        "--author",
        help="Search by author name"
    )
    parser.add_argument(
        "--category",
        help="Search by ArXiv category (e.g., cs.AI, math.CO)"
    )
    parser.add_argument(
        "--arxiv-id",
        help="Fetch specific paper by ArXiv ID"
    )
    parser.add_argument(
        "--output",
        default="arxiv_papers",
        help="Output filename (without extension, default: arxiv_papers)"
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json", "jsonl", "all"],
        default="csv",
        help="Output format (default: csv)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Delay between API calls in seconds (default: 3.0)"
    )

    args = parser.parse_args()

    crawler = ArxivCrawler(delay=args.delay)

    papers = []

    if args.arxiv_id:
        paper = crawler.search_by_id(args.arxiv_id)
        if paper:
            papers = [paper]
    elif args.author:
        papers = crawler.search_by_author(args.author, max_results=args.max_results)
    elif args.category:
        papers = crawler.search_by_category(args.category, max_results=args.max_results)
    elif args.query:
        papers = crawler.search(
            args.query,
            max_results=args.max_results,
            sort_by=args.sort_by,
            sort_order=args.sort_order,
            start=args.start,
            year_from=args.year_from,
            year_to=args.year_to,
        )
    else:
        print("Please provide a search query, author, category, or ArXiv ID")
        parser.print_help()
        return

    if papers:
        if args.format in ["csv", "all"]:
            save_to_csv(papers, f"{args.output}.csv")
        if args.format in ["json", "all"]:
            save_to_json(papers, f"{args.output}.json")
        if args.format in ["jsonl", "all"]:
            save_to_jsonl(papers, f"{args.output}.jsonl")

        print(f"\nFound {len(papers)} papers")

        if papers:
            print("\nSample results:")
            for i, paper in enumerate(papers[:3], 1):
                print(f"\n{i}. {paper.title}")
                print(f"   Authors: {', '.join(paper.authors[:3])}")
                print(f"   Published: {paper.published_date}")
                print(f"   ArXiv ID: {paper.arxiv_id}")
                print(f"   Categories: {', '.join(paper.categories[:3])}")
                print(f"   Link: {paper.link}")
                if paper.abstract:
                    print(f"   Abstract: {paper.abstract[:150]}...")
    else:
        print("No papers found")


if __name__ == "__main__":
    main()