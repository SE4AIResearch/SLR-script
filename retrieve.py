import os, sys, time, json, csv, re, argparse, math, hashlib
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Iterable, Tuple
import requests
from urllib.parse import quote_plus


def _norm(s: Optional[str]) -> str:
    return re.sub(r'\s+', ' ', (s or '')).strip()


def _norm_title(s: Optional[str]) -> str:
    s = (s or '').lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _year_from_date(d: Optional[str]) -> Optional[int]:
    if not d: return None
    m = re.match(r'^(\d{4})', d)
    return int(m.group(1)) if m else None


def _uniq(seq: Iterable[str]) -> List[str]:
    seen = set();
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x);
            out.append(x)
    return out


def _clean_doi(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    x = x.strip().lower()
    x = re.sub(r'^https?://(dx\.)?doi\.org/', '', x)
    return x or None


_DOI_FROM_URL = re.compile(r'/doi/(10\.\d{4,9}/\S+)', re.IGNORECASE)


def _doi_from_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    m = _DOI_FROM_URL.search(u)
    return _clean_doi(m.group(1)) if m else None


def backoff_get(url: str, headers: Dict[str, str] = None, params: Dict[str, Any] = None, max_tries: int = 4,
                timeout: int = 20):
    headers = headers or {}
    headers.setdefault("User-Agent", "litsearch/1.0 (+https://example.org)")
    params = params or {}
    delay = 1.0
    for i in range(max_tries):
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code == 200:
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(delay);
            delay *= 2
            continue
        r.raise_for_status()
    r.raise_for_status()


@dataclass
class Paper:
    title: str
    authors: List[str]
    year: Optional[int]
    venue: Optional[str]
    doi: Optional[str]
    url: Optional[str]
    source: str
    score: float = 0.0  # ranking score (computed later)
    id_hint: Optional[str] = None

    def dedupe_key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower().strip()
        return "title:" + _norm_title(self.title)


class Provider:
    name = "base"

    def search(self, query: str, year_from: Optional[int], year_to: Optional[int], limit: int) -> List[Paper]:
        raise NotImplementedError


class OpenAlexProvider(Provider):
    name = "openalex"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        url = "https://api.openalex.org/works"
        filt = []
        if year_from: filt.append(f"from_publication_date:{year_from}-01-01")
        if year_to:   filt.append(f"to_publication_date:{year_to}-12-31")
        per_page = min(200, max(10, limit))
        page = 1
        out: List[Paper] = []
        while len(out) < limit:
            params = {
                "search": query,
                "per_page": min(per_page, limit - len(out)),
                "sort": "relevance_score:desc",
                "page": page
            }
            if filt:
                params["filter"] = ",".join(filt)
            r = backoff_get(url, params=params)
            data = r.json()
            results = data.get("results", []) or []
            if not results:
                break
            for w in results:
                title = _norm(w.get("title"))
                year = w.get("publication_year") or _year_from_date(w.get("publication_date"))
                doi = _clean_doi(w.get("doi"))
                venue = _norm(w.get("host_venue", {}).get("display_name")) if w.get("host_venue") else None
                url_best = w.get("primary_location", {}).get("landing_page_url") \
                           or w.get("open_access", {}).get("oa_url")
                if not url_best and doi:
                    url_best = f"https://doi.org/{doi}"
                authors = [a.get("author", {}).get("display_name") for a in w.get("authorships", []) if a.get("author")]
                out.append(Paper(
                    title=title, authors=_uniq([_norm(a) for a in authors]),
                    year=year, venue=venue, doi=doi, url=url_best,
                    source=self.name, id_hint=w.get("id")
                ))
            page += 1
        return out[:limit]


class CrossrefProvider(Provider):
    name = "crossref"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        url = "https://api.crossref.org/works"
        out: List[Paper] = []
        offset = 0
        rows = min(100, max(1, limit))
        while len(out) < limit:
            params = {
                "query": query,
                "rows": min(rows, limit - len(out)),
                "offset": offset,
                "select": "DOI,title,author,issued,container-title,URL,type"
            }
            if year_from or year_to:
                filt = []
                if year_from: filt.append(f"from-pub-date:{year_from}-01-01")
                if year_to:   filt.append(f"until-pub-date:{year_to}-12-31")
                params["filter"] = ",".join(filt)
            r = backoff_get(url, params=params)
            items = r.json().get("message", {}).get("items", []) or []
            if not items:
                break
            for it in items:
                title = _norm(" ".join(it.get("title", []) or []))
                date_parts = it.get("issued", {}).get("date-parts", [])
                year = date_parts[0][0] if date_parts and date_parts[0] else None
                doi = _clean_doi(it.get("DOI"))
                venue = _norm(" ".join(it.get("container-title", []) or []))
                url_best = it.get("URL") or (f"https://doi.org/{doi}" if doi else None)
                authors = []
                for a in it.get("author", []) or []:
                    name = " ".join([x for x in [a.get("given"), a.get("family")] if x])
                    if not name: name = a.get("name")
                    authors.append(_norm(name))
                out.append(Paper(
                    title=title, authors=_uniq(authors), year=year, venue=venue,
                    doi=doi, url=url_best, source=self.name
                ))
            offset += len(items)
        return out[:limit]


class ArxivProvider(Provider):
    name = "arxiv"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        import xml.etree.ElementTree as ET
        base = "http://export.arxiv.org/api/query"
        batch = min(100, max(10, limit))
        start = 0
        out: List[Paper] = []
        ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        while len(out) < limit:
            params = {
                "search_query": f"all:{query}",
                "start": start,
                "max_results": min(batch, limit - len(out)),
                "sortBy": "relevance",
                "sortOrder": "descending"
            }
            r = backoff_get(base, params=params)
            root = ET.fromstring(r.text)
            entries = root.findall("a:entry", ns)
            if not entries:
                break
            got = 0
            for entry in entries:
                title = _norm(entry.findtext("a:title", default="", namespaces=ns))
                link = None
                for l in entry.findall("a:link", ns):
                    if l.attrib.get("rel") == "alternate":
                        link = l.attrib.get("href")
                published = entry.findtext("a:published", default="", namespaces=ns)
                year = _year_from_date(published)
                if year_from and (year is not None) and year < year_from:
                    continue
                if year_to and (year is not None) and year > year_to:
                    continue
                authors = [_norm(a.findtext("a:name", default="", namespaces=ns)) for a in
                           entry.findall("a:author", ns)]
                doi = _clean_doi(entry.findtext("arxiv:doi", default=None, namespaces=ns))
                if (not doi) and link:
                    doi = _doi_from_url(link)
                out.append(Paper(
                    title=title, authors=_uniq(authors), year=year, venue="arXiv",
                    doi=doi, url=link, source=self.name
                ))
                got += 1
            if got == 0:
                break
            start += got
        return out[:limit]


class SpringerProvider(Provider):
    name = "springer"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        api_key = os.getenv("SPRINGER_API_KEY")
        if not api_key: return []
        url = "https://api.springernature.com/metadata/json"
        out: List[Paper] = []
        start = 1
        page_size = min(100, max(10, limit))
        while len(out) < limit:
            params = {"q": query, "p": min(page_size, limit - len(out)), "s": start, "api_key": api_key}
            r = backoff_get(url, params=params)
            records = r.json().get("records", []) or []
            if not records:
                break
            for rec in records:
                title = _norm(rec.get("title"))
                year = None
                try:
                    year = int((rec.get("publicationDate") or "")[:4])
                except Exception:
                    year = None
                if year_from and year and year < year_from:
                    continue
                if year_to and year and year > year_to:
                    continue
                doi = _clean_doi(rec.get("doi"))
                url_best = None
                for u in rec.get("url", []) or []:
                    if u.get("format") == "html":
                        url_best = u.get("value");
                        break
                if not url_best and rec.get("url"):
                    url_best = rec["url"][0].get("value")
                if (not doi) and url_best:
                    doi = _doi_from_url(url_best)
                authors = _uniq([_norm(a.get("creator")) for a in rec.get("creators", []) if a.get("creator")])
                out.append(Paper(
                    title=title, authors=authors, year=year, venue=_norm(rec.get("publicationName")),
                    doi=doi, url=url_best or (f"https://doi.org/{doi}" if doi else None),
                    source=self.name
                ))
            start += len(records)
        return out[:limit]


class IeeeProvider(Provider):
    name = "ieee"

    def search(self, query, year_from, year_to, limit) -> List[Paper]:
        api_key = os.getenv("IEEEXPLORE_API_KEY")
        if not api_key: return []
        url = "http://ieeexploreapi.ieee.org/api/v1/search/articles"
        out: List[Paper] = []
        start_record = 1
        page_size = min(100, max(10, limit))
        while len(out) < limit:
            params = {
                "apikey": api_key,
                "format": "json",
                "max_records": min(page_size, limit - len(out)),
                "start_record": start_record,
                "sort_order": "desc",
                "sort_field": "relevance",
                "querytext": query,
            }
            if year_from: params["start_year"] = year_from
            if year_to:   params["end_year"] = year_to
            r = backoff_get(url, params=params)
            items = r.json().get("articles", []) or []
            if not items:
                break
            for it in items:
                title = _norm(it.get("title"))
                year = None
                try:
                    year = int(it.get("publication_year") or it.get("publication_years", ""))
                except Exception:
                    year = None
                doi = _clean_doi(it.get("doi"))
                venue = _norm(it.get("publication_title"))
                url_best = it.get("htmlLink") or (f"https://doi.org/{doi}" if doi else None)
                if (not doi) and url_best:
                    doi = _doi_from_url(url_best)
                authors = []
                for a in (it.get("authors", {}).get("authors", []) or []):
                    nm = a.get("full_name") or a.get("preferred_name")
                    if nm: authors.append(_norm(nm))
                out.append(Paper(
                    title=title, authors=_uniq(authors), year=year, venue=venue,
                    doi=doi, url=url_best, source=self.name, id_hint=it.get("pdf_url")
                ))
            start_record += len(items)
        return out[:limit]


def rank(p: Paper, q: str) -> float:
    """Simple relevance score: title match + recentness + source weight."""
    qn = _norm_title(q)
    title = _norm_title(p.title)
    score = 0.0
    # Title token overlap
    q_tokens = set(qn.split())
    t_tokens = set(title.split())
    overlap = len(q_tokens & t_tokens) / (1.0 + len(q_tokens))
    score += 2.5 * overlap
    # Recentness (log scale, favor newer)
    if p.year:
        score += 0.6 * (1.0 / (1.0 + math.exp(-(p.year - 2018) / 3.0)))
    # DOI bonus
    if p.doi: score += 0.4
    # Source trust bonus
    src_boost = {"openalex": 0.3, "crossref": 0.2, "ieee": 0.5, "springer": 0.4, "arxiv": 0.1}
    score += src_boost.get(p.source, 0.0)
    return score


def search_all(query: str, year_from: Optional[int], year_to: Optional[int], limit: int, per_provider: int) -> List[
    Paper]:
    providers: List[Provider] = [
        OpenAlexProvider(),
        CrossrefProvider(),
        ArxivProvider(),
        SpringerProvider(),
        IeeeProvider(),
    ]
    results: List[Paper] = []
    for prov in providers:
        try:
            chunk = prov.search(query, year_from, year_to, min(per_provider, limit))
            results.extend(chunk)
        except Exception as e:
            print(f"[WARN] {prov.name} failed: {e}", file=sys.stderr)

    # Deduplicate by DOI or normalized title
    by_key: Dict[str, Paper] = {}
    for p in results:
        key = p.dedupe_key()
        if key in by_key:
            # merge: prefer to fill missing fields
            base = by_key[key]
            if not base.doi and p.doi: base.doi = p.doi
            if not base.url and p.url: base.url = p.url
            if not base.venue and p.venue: base.venue = p.venue
            if not base.year and p.year: base.year = p.year
            # authors union
            base.authors = _uniq(base.authors + p.authors)
            if len(p.title) > len(base.title):
                base.title = p.title
        else:
            by_key[key] = p

    deduped = list(by_key.values())
    # Score & sort
    for p in deduped:
        p.score = rank(p, query)
    deduped.sort(key=lambda x: x.score, reverse=True)
    return deduped[:limit]


def save_jsonl(path: str, papers: List[Paper]) -> None:
    wanted = ["title", "authors", "year", "venue", "doi", "url", "source"]
    with open(path, "w", encoding="utf-8") as f:
        for p in papers:
            data = asdict(p)
            filtered = {k: data.get(k) for k in wanted}
            f.write(json.dumps(filtered, ensure_ascii=False) + "\n")


def save_csv(path: str, papers: List[Paper]) -> None:
    fields = ["title", "authors", "year", "venue", "doi", "url", "source"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in papers:
            row = asdict(p)
            row["authors"] = "; ".join(p.authors)
            w.writerow({k: row.get(k) for k in fields})

def main():
    ap = argparse.ArgumentParser(description="Unified scholarly search (OpenAlex, Crossref, arXiv, +Springer/IEEE).")
    ap.add_argument("query", help="search string, e.g., \"extract method refactoring\"")
    ap.add_argument("--year-from", type=int, default=None, help="lower bound year (inclusive)")
    ap.add_argument("--year-to", type=int, default=None, help="upper bound year (inclusive)")
    ap.add_argument("--limit", type=int, default=50, help="max total results")
    ap.add_argument("--per-provider", type=int, default=100, help="max per provider")
    ap.add_argument("--out-jsonl", default="results.jsonl", help="output JSONL path")
    ap.add_argument("--out-csv", default="results.csv", help="output CSV path")
    args = ap.parse_args()

    papers = search_all(
        query=args.query,
        year_from=args.year_from,
        year_to=args.year_to,
        limit=args.limit,
        per_provider=args.per_provider,
    )
    # save_jsonl(args.out_jsonl, papers)
    save_csv(args.out_csv, papers)
    print(f"✅ Saved {len(papers)} results to:\n - {args.out_csv}")


if __name__ == "__main__":
    main()
