# SLR-script
---
Demo can be found here: [link](https://stevens0-my.sharepoint.com/:v:/g/personal/ealomar_stevens_edu/Ef8NDLR_e9hGnYC1q-JaUrkBPbP_XsjTGLi4eUgI1QkIHA?nav=eyJyZWZlcnJhbEluZm8iOnsicmVmZXJyYWxBcHAiOiJPbmVEcml2ZUZvckJ1c2luZXNzIiwicmVmZXJyYWxBcHBQbGF0Zm9ybSI6IldlYiIsInJlZmVycmFsTW9kZSI6InZpZXciLCJyZWZlcnJhbFZpZXciOiJNeUZpbGVzTGlua0NvcHkifX0&e=nrG5Ea)

## Requirements
- Python 3.7 or higher  
- Install dependencies with:
  ```bash
  pip install -r requirements.txt
  ```

## Usage

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
  python run_all.py --query "machine learning" --year-from 2018 --year-to 2022 --limit 50
  ```

- Search across OpenAlex, Crossref, and arXiv for "deep learning" with up to 200 results per provider:
  ```bash
  python run_all.py --query "deep learning" --sources openalex,crossref,arxiv --per-provider 200
  ```

- Merge results from multiple CSV files into one consolidated CSV:
  ```bash
  python run_all.py --merge file1.csv file2.csv --out-csv merged_results.csv
  ```