import argparse
import csv
import pathlib
import re
import time
from datetime import datetime
import math
from typing import Optional
from urllib.parse import quote_plus, urlencode

import requests
from bs4 import BeautifulSoup

def normalize_query(q: str) -> str:
    """Normalize whitespace and fix hyphenated line breaks in complex Boolean strings."""
    if not q:
        return ""
    q2 = q.replace("\r", "")
    # Join hyphenated line breaks, e.g. 'separat*-\nmethod' -> 'separat*method'
    q2 = re.sub(r"-\s*\n\s*", "", q2)
    # Collapse all whitespace (spaces, tabs, newlines) to a single space
    q2 = re.sub(r"\s+", " ", q2).strip()
    return q2

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / 'results'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
BASE = "https://link.springer.com"
SEARCH_PATH = "/search"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#]+")


def build_search_url(query: str, page: int, discipline: Optional[str], sort: str,
                      date_from: str, date_to: str) -> str:
    """Construct springer link search URL with encoded query and params."""
    params = {
        "new-search": "true",
        "query": query,
        "sortBy": sort,
        "page": str(page),
        # Empty strings are accepted by site; keep them explicit
        "dateFrom": date_from,
        "dateTo": date_to,
    }
    if discipline:
        # Matches site's facet syntax: facet-discipline="Computer Science"
        params["facet-discipline"] = f'"{discipline}"'
    return f"{BASE}{SEARCH_PATH}?{urlencode(params, quote_via=quote_plus)}"


def fetch_html(url: str, timeout: int = 20, session: Optional[requests.Session] = None, referer: Optional[str] = None) -> str:
    s = session or requests.Session()
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    r = s.get(url, headers=headers, timeout=timeout)
    # Handle occasional 429/503 with a short retry once
    if r.status_code in (429, 503):
        time.sleep(1.0)
        r = s.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


RESULTS_NUM_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)")


def _parse_total_results(soup: BeautifulSoup) -> int:
    """Best-effort parse of total results count from visible text.
    Looks for patterns like: "1–20 of 123 results" or "123 results".
    """
    texts = []
    # Common containers that mention result counts
    for sel in [
        ("div", {"class": re.compile("results|summary|toolbar|header", re.I)}),
        ("p", {}),
        ("span", {"class": re.compile("results|count|summary", re.I)}),
        ("div", {"data-test": re.compile("results|count", re.I)}),
    ]:
        for el in soup.find_all(sel[0], attrs=sel[1]):
            t = el.get_text(" ", strip=True)
            if t and ("result" in t.lower() or "results" in t.lower()):
                texts.append(t)
    # Fallback: whole page
    if not texts:
        texts = [soup.get_text(" ", strip=True)]

    best = 0
    for t in texts:
        # look for "of N results"
        m = re.search(r"\bof\s+(\d{1,3}(?:,\d{3})+|\d+)\s+results\b", t, flags=re.I)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except Exception:
                pass
        # or plain "N results"
        m2 = re.search(r"\b(\d{1,3}(?:,\d{3})+|\d+)\s+results\b", t, flags=re.I)
        if m2:
            try:
                best = max(best, int(m2.group(1).replace(",", "")))
            except Exception:
                pass
    return best


def detect_total_pages(soup: BeautifulSoup, first_page_count: int, default_per_page: int = 20) -> int:
    """Combine pagination nav and total results to decide total pages."""
    # nav-based detection
    pages_by_nav = 1
    nav = soup.find("nav", class_=re.compile("pagination", re.I))
    if nav:
        nums = []
        for a in nav.find_all(["a", "span"]):
            t = (a.get_text(strip=True) or "").strip()
            if t.isdigit():
                nums.append(int(t))
            dp = a.get("data-page")
            if dp and dp.isdigit():
                nums.append(int(dp))
        if nums:
            pages_by_nav = max(nums)

    # count-based detection
    total_results = _parse_total_results(soup)
    per_page = first_page_count if first_page_count > 0 else default_per_page
    pages_by_count = math.ceil(total_results / per_page) if total_results and per_page else 1

    return max(pages_by_nav, pages_by_count, 1)


def extract_venue_from_text(text: str) -> str:
    if not text:
        return ""
    
    text = re.sub(r'\s+', ' ', text.strip())
    
    journal_patterns = [
        r'In:\s*(.+?)(?:\s*,\s*\d{4}|$)',
        r'^(.+?),\s*(?:vol\.|volume|pp\.|pages|\d{4})',
        r'<i>(.+?)</i>',
        r'<em>(.+?)</em>',
        r'(.*?(?:Journal|Proceedings|Conference|Symposium|Workshop|Transactions|Letters|Review|Magazine|Bulletin)[^,]*)',
        r'(IEEE\s+[^,]+|ACM\s+[^,]+)',
    ]
    
    for pattern in journal_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            venue = match.group(1).strip()
            venue = re.sub(r'^(In:|From:)\s*', '', venue, flags=re.IGNORECASE)
            venue = re.sub(r'\s*\([^)]*\)$', '', venue)
            if len(venue) > 5:
                return venue
    
    return text if len(text) < 100 else ""


def extract_records(soup: BeautifulSoup):
    """Yield records from a search result page."""
    DEBUG = False
    
    # Try modern list containers first
    containers = []
    lists = soup.find_all("ol", class_=re.compile("u-list-reset|results-list|content-item-list"))
    if lists:
        containers.extend(lists)
    # Some pages use <ul> or direct div wrappers
    lists2 = soup.find_all(["ul", "div"], class_=re.compile("(results|content|c-results|c-list)", re.I))
    for c in lists2:
        if c not in containers:
            containers.append(c)
    if not containers:
        # As a fallback, scan the whole document for cards/entries
        containers = [soup]

    record_count = 0
    for cont in containers:
        items = cont.find_all(["li", "article", "div"], attrs={"data-test": re.compile("result|card", re.I)})
        if not items:
            # Common fallback card/list containers
            items = (cont.find_all("li") or cont.find_all("article") or
                     cont.find_all("div", class_=re.compile("c-card|result|content-item|app-card", re.I)))
        
        for li in items:
            record_count += 1
            if DEBUG and record_count <= 3:  # 只调试前3条记录
                print(f"\n[DEBUG] Processing record {record_count}")
                print(f"[DEBUG] Item HTML snippet: {str(li)[:500]}...")
            
            # Content type (best-effort)
            meta_div = li.find("div", class_=re.compile("c-meta|content-type", re.I)) or li
            ct = (meta_div.find(["span", "div"], attrs={"data-test": "content-type"}) if meta_div else None)
            content_type = ct.get_text(strip=True) if ct else ""

            # Title/link: try several patterns
            a = (li.find("a", class_=re.compile("(app-card-open__link|title)", re.I), href=True)
                 or (li.find("h2") and li.find("h2").find("a", href=True))
                 or (li.find("p", class_=re.compile("title", re.I)) and li.find("p", class_=re.compile("title", re.I)).find("a", href=True))
                 or li.find("a", attrs={"data-track-action": re.compile("title|open", re.I)}, href=True)
                 or li.find("a", href=True))
            if not a:
                continue
            href = a.get("href", "")
            link = href if href.startswith("http") else f"{BASE}{href}" if href else ""
            title = a.get_text(strip=True) or ""
            if not title:
                # Some cards wrap title in heading text
                title = (li.find(text=True) or "").strip()
            if not title:
                continue

            # Authors (several variants)
            authors_el = (li.find("span", attrs={"data-test": "authors"}) or
                          li.find("div", class_=re.compile("authors", re.I)) or
                          li.find("ul", class_=re.compile("authors", re.I)))
            authors = authors_el.get_text(strip=True) if authors_el else ""

            # Published / year
            pub_el = (li.find("span", attrs={"data-test": "published"}) or
                      li.find("time") or
                      li.find("span", class_=re.compile("year|date", re.I)))
            published = pub_el.get_text(strip=True) if pub_el else ""

            # DOI: from href/link/title text
            doi = ""
            m = (DOI_RE.search(href or "") or DOI_RE.search(link or "") or DOI_RE.search(title))
            if m:
                doi = m.group(0)

            abstract_el = (li.find("div", class_=re.compile("abstract|summary|description", re.I)) or
                           li.find("p", class_=re.compile("snippet|preview", re.I)))
            abstract = abstract_el.get_text(strip=True) if abstract_el else ""

            journal = ""
            
            journal_link = li.find("a", attrs={"data-test": "parent"})
            if journal_link:
                journal = journal_link.get_text(strip=True)
                if DEBUG and record_count <= 3:
                    print(f"[DEBUG] Found journal via data-test='parent': '{journal}'")
            
            if not journal:
                authors_div = li.find("div", class_=re.compile("authors", re.I))
                if authors_div:
                    text_content = authors_div.get_text(" ", strip=True)
                    in_match = re.search(r'\bin\s+(.+?)(?:\s|$)', text_content)
                    if in_match:
                        potential_journal = in_match.group(1).strip()
                        potential_journal = re.sub(r'\s*\d{2}\s+\w+\s+\d{4}.*', '', potential_journal)
                        if len(potential_journal) > 2:
                            journal = potential_journal
                            if DEBUG and record_count <= 3:
                                print(f"[DEBUG] Found journal via 'in' pattern: '{journal}'")
            
            if not journal:
                potential_links = li.find_all("a", href=re.compile(r'/journal/\d+'))
                for link in potential_links:
                    link_text = link.get_text(strip=True)
                    if link_text and len(link_text) > 2:
                        journal = link_text
                        if DEBUG and record_count <= 3:
                            print(f"[DEBUG] Found journal via journal link: '{journal}'")
                        break
            
            if DEBUG and record_count <= 3:
                print(f"[DEBUG] Final journal: '{journal}'")

            volume_el = li.find("span", class_=re.compile("volume|issue|pages", re.I))
            volume_info = volume_el.get_text(strip=True) if volume_el else ""

            keywords_el = (li.find("div", class_=re.compile("keywords|tags|topics", re.I)) or
                           li.find("span", class_=re.compile("keyword|subject", re.I)) or
                           li.find("ul", class_=re.compile("keywords|tags", re.I)))
            keywords = keywords_el.get_text(strip=True) if keywords_el else ""

            lang_el = li.find("span", attrs={"data-test": "language"})
            language = lang_el.get_text(strip=True) if lang_el else ""

            oa_el = (li.find("span", class_=re.compile("open.access|oa", re.I)) or
                     li.find("div", attrs={"data-test": "open-access"}))
            open_access = bool(oa_el)

            citation_el = li.find("span", class_=re.compile("citation|cited", re.I))
            citations = citation_el.get_text(strip=True) if citation_el else ""

            download_links = []
            for a in li.find_all("a", href=True):
                href = a.get("href", "")
                if any(x in href.lower() for x in ["pdf", "download", "fulltext"]):
                    download_links.append(href if href.startswith("http") else f"{BASE}{href}")

            yield {
                "title": title,
                "authors": authors,
                "published date": published,
                "link": link,
                "content_type": content_type,
                "DOI": doi,
                "abstract": abstract,
                "venue": journal,
                "keywords": keywords,
                "citations": citations
            }


def main():
    ap = argparse.ArgumentParser(description="Scrape Springer search results to CSV (any search string).")
    ap.add_argument("query", help="Search string as you would type on springer.com (use quotes for phrases)")
    ap.add_argument("--max-pages", type=int, default=50, help="Maximum pages to crawl (auto-detect if fewer)")
    ap.add_argument("--discipline", default="Computer Science", help="Facet discipline (or empty to skip)")
    ap.add_argument("--sort", default="relevance", choices=["relevance", "newest"], help="Sort order")
    ap.add_argument("--date-from", default="", help="YYYY or empty")
    ap.add_argument("--date-to", default="", help="YYYY or empty")
    ap.add_argument("--delay", type=float, default=0.3, help="Delay between page requests (seconds)")
    ap.add_argument(
        "--output",
        default=None,
        help="Output CSV path (without extension). If not provided, a timestamped file in the default results directory is used.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of records to save (global cap across all pages).",
    )
    args = ap.parse_args()

    sess = requests.Session()

    # Normalize query to avoid newline/hyphen artifacts breaking the site search
    q_norm = normalize_query(args.query)

    # Build first page
    url1 = build_search_url(q_norm, page=1, discipline=(args.discipline or None), sort=args.sort,
                            date_from=args.date_from, date_to=args.date_to)
    print(f"[DEBUG] Fetching URL: {url1}")
    html = fetch_html(url1, session=sess)
    soup = BeautifulSoup(html, 'html.parser')

    # Parse page 1 first to know how many items/page
    first_records = list(extract_records(soup))
    first_count = len(first_records)

    detected_pages = detect_total_pages(soup, first_count)
    print(f"[DEBUG] Detected total_pages={detected_pages}")

    if args.output:
        out_path = pathlib.Path(args.output)
        # Ensure .csv suffix
        if out_path.suffix.lower() != ".csv":
            out_path = out_path.with_suffix(".csv")
    else:
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        out_path = OUTPUT_DIR / f'springer_{timestamp}.csv'

    # Normalize limit: non-positive means "no limit"
    limit = args.limit if args.limit and args.limit > 0 else None
    written = 0

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['title', 'authors', 'published date', 'link', 'content_type', 'DOI', 'abstract', 'venue', 'keywords', 'citations'])

        # page 1
        for rec in first_records:
            if limit is not None and written >= limit:
                break
            w.writerow([rec['title'], rec['authors'], rec['published date'], rec['link'], rec['content_type'],
                        rec['DOI'], rec['abstract'], rec['venue'], rec['keywords'], rec['citations']])
            written += 1

        empty_count = 0
        prev_url = url1

        # next pages: only iterate while within detected total pages and user cap
        p = 2
        while p <= min(args.max_pages, detected_pages):
            if limit is not None and written >= limit:
                break

            print(f"Page: {p}")
            url = build_search_url(q_norm, page=p, discipline=(args.discipline or None), sort=args.sort,
                                   date_from=args.date_from, date_to=args.date_to)
            print(f"[DEBUG] Fetching URL: {url}")
            html = fetch_html(url, session=sess, referer=url1 if p == 2 else prev_url)
            soup = BeautifulSoup(html, 'html.parser')
            got = 0
            for rec in extract_records(soup):
                if limit is not None and written >= limit:
                    break
                w.writerow([rec['title'], rec['authors'], rec['published date'], rec['link'], rec['content_type'],
                            rec['DOI'], rec['abstract'], rec['venue'], rec['keywords'], rec['citations']])
                got += 1
                written += 1

            # detect presence of a Next link in pagination (best-effort)
            has_next = bool(soup.find('a', attrs={'rel': 'next'}) or
                            soup.find('a', string=re.compile('^\s*Next\s*$', re.I)) or
                            soup.find('a', class_=re.compile('next', re.I)) or
                            soup.find('button', class_=re.compile('next', re.I)))

            if got == 0:
                empty_count += 1
            else:
                empty_count = 0

            if empty_count >= 2 and not has_next:
                print("[DEBUG] Stopping: two consecutive empty pages and no Next link detected")
                break

            prev_url = url
            p += 1
            time.sleep(max(0.0, args.delay))

    print(f"✅ Saved results to: {out_path}")


if __name__ == "__main__":
    main()