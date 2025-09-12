#!/usr/bin/env python3
"""
Universal Abstract Fetcher Module
Fetches abstracts from multiple sources with intelligent fallback
"""

import re
import time
import requests
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
import hashlib
import json
import os
from datetime import datetime, timedelta


@dataclass
class AbstractResult:
    """Result from abstract fetching"""
    abstract: Optional[str]
    source: Optional[str]
    confidence: float  # 0.0 to 1.0
    fetched_at: str


class AbstractCache:
    """Simple file-based cache for abstracts to avoid repeated API calls"""

    def __init__(self, cache_dir: str = ".abstract_cache", expire_days: int = 30):
        self.cache_dir = cache_dir
        self.expire_days = expire_days
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

    def _get_cache_key(self, doi: str = None, title: str = None) -> str:
        """Generate cache key from DOI or title"""
        if doi:
            return f"doi_{hashlib.md5(doi.encode()).hexdigest()}"
        elif title:
            return f"title_{hashlib.md5(title.lower().encode()).hexdigest()}"
        return None

    def get(self, doi: str = None, title: str = None) -> Optional[AbstractResult]:
        """Get cached abstract if exists and not expired"""
        cache_key = self._get_cache_key(doi, title)
        if not cache_key:
            return None

        cache_file = os.path.join(self.cache_dir, f"{cache_key}.json")
        if not os.path.exists(cache_file):
            return None

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Check expiration
            fetched_at = datetime.fromisoformat(data['fetched_at'])
            if datetime.now() - fetched_at > timedelta(days=self.expire_days):
                os.remove(cache_file)
                return None

            return AbstractResult(**data)
        except Exception:
            return None

    def set(self, result: AbstractResult, doi: str = None, title: str = None):
        """Cache an abstract result"""
        cache_key = self._get_cache_key(doi, title)
        if not cache_key:
            return

        cache_file = os.path.join(self.cache_dir, f"{cache_key}.json")
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'abstract': result.abstract,
                    'source': result.source,
                    'confidence': result.confidence,
                    'fetched_at': result.fetched_at
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


class UniversalAbstractFetcher:
    """
    Universal abstract fetcher that tries multiple sources
    Priority order: Semantic Scholar > CrossRef > OpenAlex > PubMed > arXiv
    """

    def __init__(self, use_cache: bool = True, delay: float = 0.2, verbose: bool = False):
        """
        Initialize the fetcher

        Args:
            use_cache: Whether to use caching (recommended)
            delay: Delay between API calls in seconds
            verbose: Print debug information
        """
        self.delay = delay
        self.verbose = verbose
        self.cache = AbstractCache() if use_cache else None
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'AbstractFetcher/1.0 (https://github.com/research/abstract-fetcher)'
        })

    def fetch_abstract(self,
                       title: str = None,
                       doi: str = None,
                       authors: List[str] = None,
                       year: int = None,
                       venue: str = None) -> AbstractResult:
        """
        Main method to fetch abstract using all available information

        Args:
            title: Paper title
            doi: Digital Object Identifier
            authors: List of author names
            year: Publication year
            venue: Journal or conference name

        Returns:
            AbstractResult with abstract, source, and confidence score
        """

        # Check cache first
        if self.cache:
            cached = self.cache.get(doi=doi, title=title)
            if cached and cached.abstract:
                if self.verbose:
                    print(f"✓ Found cached abstract from {cached.source}")
                return cached

        # Clean inputs
        doi = self._clean_doi(doi) if doi else None
        title = self._clean_text(title) if title else None

        if not doi and not title:
            return AbstractResult(None, None, 0.0, datetime.now().isoformat())

        # Try sources in order of reliability
        result = None

        # 1. Try Semantic Scholar (best for CS papers)
        if not result or not result.abstract:
            result = self._try_semantic_scholar(title, doi, authors, year)
            if result and result.abstract:
                if self.verbose:
                    print(f"✓ Got abstract from Semantic Scholar")

        # 2. Try CrossRef (good general coverage)
        if not result or not result.abstract:
            if doi:
                result = self._try_crossref(doi)
                if result and result.abstract:
                    if self.verbose:
                        print(f"✓ Got abstract from CrossRef")

        # 3. Try OpenAlex (excellent coverage, requires matching)
        if not result or not result.abstract:
            result = self._try_openalex(title, doi, authors, year)
            if result and result.abstract:
                if self.verbose:
                    print(f"✓ Got abstract from OpenAlex")

        # 4. Try PubMed (for biomedical papers)
        if not result or not result.abstract:
            result = self._try_pubmed(title, doi, authors)
            if result and result.abstract:
                if self.verbose:
                    print(f"✓ Got abstract from PubMed")

        # 5. Try arXiv (for preprints)
        if not result or not result.abstract:
            result = self._try_arxiv(title, authors)
            if result and result.abstract:
                if self.verbose:
                    print(f"✓ Got abstract from arXiv")

        # 6. Try Europe PMC (alternative to PubMed)
        if not result or not result.abstract:
            result = self._try_europe_pmc(title, doi)
            if result and result.abstract:
                if self.verbose:
                    print(f"✓ Got abstract from Europe PMC")

        # Final result
        if not result:
            result = AbstractResult(None, None, 0.0, datetime.now().isoformat())

        # Cache the result
        if self.cache and result.abstract:
            self.cache.set(result, doi=doi, title=title)

        return result

    def _clean_doi(self, doi: str) -> Optional[str]:
        """Clean and normalize DOI"""
        if not doi:
            return None
        doi = doi.strip().lower()
        doi = re.sub(r'^https?://(dx\.)?doi\.org/', '', doi)
        doi = re.sub(r'^doi:', '', doi)
        return doi if doi else None

    def _clean_text(self, text: str) -> str:
        """Clean text by removing extra whitespace and HTML"""
        if not text:
            return ""
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        # Normalize whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _calculate_title_similarity(self, title1: str, title2: str) -> float:
        """Calculate similarity between two titles (0.0 to 1.0)"""
        if not title1 or not title2:
            return 0.0

        # Normalize for comparison
        t1 = re.sub(r'[^a-z0-9]+', ' ', title1.lower()).strip()
        t2 = re.sub(r'[^a-z0-9]+', ' ', title2.lower()).strip()

        # Simple word overlap similarity
        words1 = set(t1.split())
        words2 = set(t2.split())

        if not words1 or not words2:
            return 0.0

        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return intersection / union if union > 0 else 0.0

    def _try_semantic_scholar(self, title: str, doi: str, authors: List[str], year: int) -> Optional[AbstractResult]:
        """Fetch from Semantic Scholar API"""
        try:
            # Try DOI first
            if doi:
                url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
                params = {"fields": "title,abstract"}

                response = self.session.get(url, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    abstract = data.get('abstract')
                    if abstract:
                        return AbstractResult(
                            abstract=self._clean_text(abstract),
                            source="Semantic Scholar",
                            confidence=1.0,
                            fetched_at=datetime.now().isoformat()
                        )

                time.sleep(self.delay)

            # Try title search
            if title:
                search_url = "https://api.semanticscholar.org/graph/v1/paper/search"
                params = {
                    "query": title,
                    "limit": 3,
                    "fields": "title,abstract,authors,year"
                }

                response = self.session.get(search_url, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    papers = data.get('data', [])

                    for paper in papers:
                        # Check title similarity
                        similarity = self._calculate_title_similarity(title, paper.get('title', ''))

                        # Check year if provided
                        year_match = True
                        if year and paper.get('year'):
                            year_match = abs(paper['year'] - year) <= 1

                        if similarity > 0.7 and year_match:
                            abstract = paper.get('abstract')
                            if abstract:
                                return AbstractResult(
                                    abstract=self._clean_text(abstract),
                                    source="Semantic Scholar",
                                    confidence=similarity,
                                    fetched_at=datetime.now().isoformat()
                                )

                time.sleep(self.delay)

        except Exception as e:
            if self.verbose:
                print(f"Semantic Scholar error: {e}")

        return None

    def _try_crossref(self, doi: str) -> Optional[AbstractResult]:
        """Fetch from CrossRef API"""
        if not doi:
            return None

        try:
            url = f"https://api.crossref.org/works/{doi}"
            headers = {'User-Agent': 'AbstractFetcher/1.0 (mailto:research@example.com)'}

            response = self.session.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                abstract = data.get('message', {}).get('abstract')

                if abstract:
                    return AbstractResult(
                        abstract=self._clean_text(abstract),
                        source="CrossRef",
                        confidence=1.0,
                        fetched_at=datetime.now().isoformat()
                    )

            time.sleep(self.delay)

        except Exception as e:
            if self.verbose:
                print(f"CrossRef error: {e}")

        return None

    def _try_openalex(self, title: str, doi: str, authors: List[str], year: int) -> Optional[AbstractResult]:
        """Fetch from OpenAlex API"""
        try:
            # Try DOI first
            if doi:
                url = f"https://api.openalex.org/works/https://doi.org/{doi}"

                response = self.session.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    inverted_index = data.get('abstract_inverted_index')

                    if inverted_index:
                        abstract = self._reconstruct_abstract_from_inverted(inverted_index)
                        if abstract:
                            return AbstractResult(
                                abstract=abstract,
                                source="OpenAlex",
                                confidence=1.0,
                                fetched_at=datetime.now().isoformat()
                            )

                time.sleep(self.delay)

            # Try title search
            if title:
                search_url = "https://api.openalex.org/works"
                params = {
                    "search": title,
                    "per_page": 3
                }

                response = self.session.get(search_url, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    results = data.get('results', [])

                    for work in results:
                        work_title = work.get('title', '')
                        similarity = self._calculate_title_similarity(title, work_title)

                        # Check year
                        year_match = True
                        if year and work.get('publication_year'):
                            year_match = abs(work['publication_year'] - year) <= 1

                        if similarity > 0.7 and year_match:
                            inverted_index = work.get('abstract_inverted_index')
                            if inverted_index:
                                abstract = self._reconstruct_abstract_from_inverted(inverted_index)
                                if abstract:
                                    return AbstractResult(
                                        abstract=abstract,
                                        source="OpenAlex",
                                        confidence=similarity,
                                        fetched_at=datetime.now().isoformat()
                                    )

                time.sleep(self.delay)

        except Exception as e:
            if self.verbose:
                print(f"OpenAlex error: {e}")

        return None

    def _reconstruct_abstract_from_inverted(self, inverted_index: Dict[str, List[int]]) -> Optional[str]:
        """Reconstruct abstract from OpenAlex inverted index format"""
        if not inverted_index:
            return None

        # Create position to word mapping
        word_positions = []
        for word, positions in inverted_index.items():
            for pos in positions:
                word_positions.append((pos, word))

        # Sort by position
        word_positions.sort(key=lambda x: x[0])

        # Reconstruct text
        abstract = " ".join([word for _, word in word_positions])

        # Clean up punctuation spacing
        abstract = re.sub(r'\s+([.,;!?])', r'\1', abstract)
        abstract = re.sub(r'\s+', ' ', abstract).strip()

        return abstract if len(abstract) > 50 else None

    def _try_pubmed(self, title: str, doi: str, authors: List[str]) -> Optional[AbstractResult]:
        """Fetch from PubMed/NCBI E-utilities"""
        try:
            # Build search query
            query_parts = []
            if title:
                query_parts.append(f'"{title}"[Title]')
            if doi:
                query_parts.append(f'"{doi}"[DOI]')

            if not query_parts:
                return None

            query = " OR ".join(query_parts)

            # Search for paper
            search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            search_params = {
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "retmax": 3
            }

            response = self.session.get(search_url, params=search_params, timeout=10)
            if response.status_code != 200:
                return None

            search_data = response.json()
            id_list = search_data.get('esearchresult', {}).get('idlist', [])

            if not id_list:
                return None

            time.sleep(self.delay)

            # Fetch details for each ID
            for pmid in id_list[:1]:  # Just check the first match
                fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                fetch_params = {
                    "db": "pubmed",
                    "id": pmid,
                    "retmode": "xml"
                }

                response = self.session.get(fetch_url, params=fetch_params, timeout=10)
                if response.status_code == 200:
                    # Parse XML to extract abstract
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(response.content)

                    # Find abstract
                    abstract_elem = root.find('.//AbstractText')
                    if abstract_elem is not None and abstract_elem.text:
                        return AbstractResult(
                            abstract=self._clean_text(abstract_elem.text),
                            source="PubMed",
                            confidence=0.9,
                            fetched_at=datetime.now().isoformat()
                        )

                time.sleep(self.delay)

        except Exception as e:
            if self.verbose:
                print(f"PubMed error: {e}")

        return None

    def _try_arxiv(self, title: str, authors: List[str]) -> Optional[AbstractResult]:
        """Fetch from arXiv API"""
        if not title:
            return None

        try:
            url = "http://export.arxiv.org/api/query"
            params = {
                "search_query": f"ti:{title}",
                "max_results": 3
            }

            response = self.session.get(url, params=params, timeout=10)
            if response.status_code == 200:
                # Parse XML response
                import xml.etree.ElementTree as ET
                root = ET.fromstring(response.content)

                # Define namespace
                ns = {'atom': 'http://www.w3.org/2005/Atom'}

                # Find entries
                entries = root.findall('.//atom:entry', ns)

                for entry in entries:
                    entry_title = entry.find('atom:title', ns)
                    if entry_title is not None:
                        entry_title_text = entry_title.text.replace('\n', ' ').strip()
                        similarity = self._calculate_title_similarity(title, entry_title_text)

                        if similarity > 0.7:
                            summary = entry.find('atom:summary', ns)
                            if summary is not None and summary.text:
                                return AbstractResult(
                                    abstract=self._clean_text(summary.text),
                                    source="arXiv",
                                    confidence=similarity,
                                    fetched_at=datetime.now().isoformat()
                                )

            time.sleep(self.delay)

        except Exception as e:
            if self.verbose:
                print(f"arXiv error: {e}")

        return None

    def _try_europe_pmc(self, title: str, doi: str) -> Optional[AbstractResult]:
        """Fetch from Europe PMC"""
        try:
            query_parts = []
            if title:
                query_parts.append(f'TITLE:"{title}"')
            if doi:
                query_parts.append(f'DOI:"{doi}"')

            if not query_parts:
                return None

            query = " OR ".join(query_parts)

            url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            params = {
                "query": query,
                "format": "json",
                "pageSize": 3
            }

            response = self.session.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                results = data.get('resultList', {}).get('result', [])

                for result in results:
                    result_title = result.get('title', '')

                    # Check title similarity
                    similarity = 1.0
                    if title:
                        similarity = self._calculate_title_similarity(title, result_title)

                    if similarity > 0.7:
                        abstract = result.get('abstractText')
                        if abstract:
                            return AbstractResult(
                                abstract=self._clean_text(abstract),
                                source="Europe PMC",
                                confidence=similarity,
                                fetched_at=datetime.now().isoformat()
                            )

            time.sleep(self.delay)

        except Exception as e:
            if self.verbose:
                print(f"Europe PMC error: {e}")

        return None


# Convenience function for simple usage
def fetch_abstract(title: str = None,
                   doi: str = None,
                   authors: List[str] = None,
                   year: int = None,
                   venue: str = None,
                   use_cache: bool = True,
                   verbose: bool = False) -> Optional[str]:
    """
    Simple function to fetch abstract

    Returns:
        Abstract text or None if not found
    """
    fetcher = UniversalAbstractFetcher(use_cache=use_cache, verbose=verbose)
    result = fetcher.fetch_abstract(title=title, doi=doi, authors=authors, year=year, venue=venue)
    return result.abstract if result else None


# Example usage and testing
if __name__ == "__main__":
    # Test with different inputs
    test_cases = [
        {
            "title": "Attention Is All You Need",
            "authors": ["Vaswani", "Shazeer"],
            "year": 2017
        },
        {
            "doi": "10.1145/3447548.3467286"
        },
        {
            "title": "Deep Residual Learning for Image Recognition",
            "doi": "10.1109/CVPR.2016.90"
        }
    ]

    fetcher = UniversalAbstractFetcher(verbose=True)

    for i, test in enumerate(test_cases, 1):
        print(f"\n{'=' * 60}")
        print(f"Test Case {i}: {test}")
        print(f"{'=' * 60}")

        result = fetcher.fetch_abstract(**test)

        if result and result.abstract:
            print(f"✓ Found abstract from {result.source}")
            print(f"  Confidence: {result.confidence:.2f}")
            print(f"  Abstract: {result.abstract[:200]}...")
        else:
            print("✗ No abstract found")