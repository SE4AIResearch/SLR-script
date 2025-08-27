# SLR-script
# Scholarly Paper Retriever

A unified Python script to search for academic papers across multiple digital libraries and APIs (OpenAlex, Crossref, arXiv, Springer, IEEE Xplore).  
It retrieves metadata (title, authors, year, venue, DOI, URL, source) and exports them to CSV.

## Features
- Query multiple sources at once: **OpenAlex, Crossref, arXiv** (free)  
- Optional with API keys: **Springer, IEEE Xplore**  
- Automatic deduplication (by DOI or title)  
- Pagination support (fetches beyond 25/50 results per source)  
- Outputs results in CSV format  

---

## Requirements
- **Python 3.7+** (tested with Python 3.9, 3.11)
- Dependencies:
  ```bash
  pip install -r requirements.txt

## Usage
- Run from the command line:
  ```bash
  python retrieve.py "extract method refactoring" --limit 100 --out-csv results.csv

-Options:
- query: your search string, e.g. "machine learning".
- --year-from: lower bound year (inclusive).
- --year-to: upper bound year (inclusive).
- --limit: maximum number of results to return (after deduplication).
- --per-provider: maximum results to fetch per provider (default 100).
- --out-csv: path to output CSV file (default: results.csv).