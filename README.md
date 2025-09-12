# SLR-script
---

## Requirements
- Python 3.7 or higher  
- Install dependencies with:
  ```bash
  pip install -r requirements.txt
  ```

## Usage

### Quick Start Example
Run a simple query to search for papers related to "extract method refactoring" and save the top 100 results to a CSV file:

```bash
python retrieve.py "extract method refactoring" --limit 100 --out-csv results.csv
```

### Command-line Options
- `query` (positional): Your search string, e.g., `"machine learning"`  
- `--year-from`: Lower bound year (inclusive) to filter results  
- `--year-to`: Upper bound year (inclusive) to filter results  
- `--limit`: Maximum number of results to return after deduplication (default: 100) (optional, if not provided, will crawl all the results)  
- `--per-provider`: Maximum number of results to fetch per provider (default: 100)  
- `--out-csv`: Path to output CSV file (default: `results.csv`)  
- `--out-sqlite`: Path to output SQLite database file (optional)  
- `--sources`: Comma-separated list of sources to query (e.g., `openalex,crossref,arxiv,springer,ieee`)  
- `--springer-api-key`: API key for Springer (if available)  
- `--ieee-api-key`: API key for IEEE Xplore (if available)  
- `--enrich-abstracts`: Include abstracts in the output when available  
- `--merge`: Merge results from multiple queries or files  

### Examples

- Search for papers on "machine learning" published between 2018 and 2022, limiting to 50 results:
  ```bash
  python retrieve.py "machine learning" --year-from 2018 --year-to 2022 --limit 50
  ```

- Search across OpenAlex, Crossref, and arXiv for "deep learning" with up to 200 results per provider:
  ```bash
  python retrieve.py "deep learning" --sources openalex,crossref,arxiv --per-provider 200
  ```

- Query Springer and IEEE Xplore with API keys and save results to SQLite:
  ```bash
  python retrieve.py "natural language processing" --sources springer,ieee --springer-api-key YOUR_SPRINGER_KEY --ieee-api-key YOUR_IEEE_KEY --out-sqlite results.db
  ```

- Merge results from multiple CSV files into one consolidated CSV:
  ```bash
  python retrieve.py --merge file1.csv file2.csv --out-csv merged_results.csv
  ```