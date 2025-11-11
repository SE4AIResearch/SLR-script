# SLR-script
---
Demo can be found here: [link](https://stevens0-my.sharepoint.com/:v:/g/personal/ealomar_stevens_edu/EQ30Ze3UXsRJq1afk5E_31gB76p_TF6SSr0zuXcbiRUodw?nav=eyJyZWZlcnJhbEluZm8iOnsicmVmZXJyYWxBcHAiOiJPbmVEcml2ZUZvckJ1c2luZXNzIiwicmVmZXJyYWxBcHBQbGF0Zm9ybSI6IldlYiIsInJlZmVycmFsTW9kZSI6InZpZXciLCJyZWZlcnJhbFZpZXciOiJNeUZpbGVzTGlua0NvcHkifX0&e=Ec7uBL)

## Requirements
- Python 3.7 or higher  
- Install dependencies with:
  ```bash
  pip install -r requirements.txt
  ```

## Usage

### Notes on Search Behavior Across Databases

Different data providers handle search queries differently. This script does not enforce a unified search model across all databases, so the same query string may be applied to different fields depending on the provider:

- **Scopus**: Queries are applied to **TITLE-ABS-KEY**, meaning only the **title, abstract, and author keywords** are searched. Scopus does **not** support full-text search in this mode.
- **ScienceDirect**: The query is applied to the **full record**, which may include more fields than Scopus. However, this should not be interpreted as guaranteed PDF full-text search, but it generally searches more metadata fields.
- **ACM (via Crossref in this script)**: Searches are performed over **bibliographic metadata** through Crossref and filtered by ACM DOI prefixes. This is **not** equivalent to ACM full-text search from the ACM Digital Library.
- **OpenAlex and arXiv**: These providers support broader or more flexible search capabilities (e.g., OpenAlex may include abstract and available full-text sources; arXiv supports field-based queries such as `ti:` or `abs:`).

Because of these differences, the **same search string may return different levels of recall across providers**. If your study requires more consistent or stricter search scope across databases, you may need to adjust queries or enable provider-specific field filters.

### Command-line Options
- `query` (positional): Your search string, e.g., `"machine learning"`  
- `--year-from`: Lower bound year (inclusive) to filter results  
- `--year-to`: Upper bound year (inclusive) to filter results  
- `--limit`: Maximum number of results to return after deduplication (default: 100) (optional, if not provided, will crawl all the results)  
- `--per-provider`: Maximum number of results to fetch per provider (default: 100)  
- `--out-csv`: Path to output CSV file (default: `results.csv`)  
- `--out-sqlite`: Path to output SQLite database file (optional)  
- `--sources`: Comma-separated list of sources to query (e.g., `acm, scienceDirect, scopus, openalex, crossref, arxiv, springer, wiley`) (optional, if not provided, will crawl all the sources)
- `--springer-api-key`: API key for Springer (if available)  
<!-- - `--ieee-api-key`: API key for IEEE Xplore (if available)   -->
- `--enrich-abstracts`: Include abstracts in the output when available (Default: True) 
- `--merge`: Merge results from multiple queries or files  
- `--no-enhance`: Disable enhanced abstracts where available


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