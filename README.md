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
- `--year-from`: Lower bound year (inclusive)  
- `--year-to`: Upper bound year (inclusive)  
- `--limit`: Global cap **after merging and deduplication**.  
  - Defaults to 100.  
  - Does **not** limit per-provider crawling unless `--per-provider` is used.  
- `--per-provider`: Treat `--limit` as a per-provider fetch cap.  
  - When enabled, each provider fetches up to `--limit` items, and Phase 2 may increase this cap automatically to reach the target merged size.  
- `--out-dir`: Root directory where all results are saved.  
  - Raw per-provider results go to `<out-dir>/phase_1/`  
  - Merged results go to `<out-dir>/phase_2/`  
  - Default: `results/`  
- `--sources`: Comma‑separated list of providers (e.g., `crossref,scopus,openalex`)  
  - If omitted, all providers are used.  
- `--springer-api-key`: API key for Springer (optional)  
- `--enrich-abstracts`: Expand abstracts when available (default: True)  
- `--no-enhance`: Disable abstract enhancement  
- `--merge`: Merge results from multiple queries or provider outputs

*Deprecated options such as `--out-csv` and `--out-sqlite` have been removed from the tool and are no longer used.*


### Examples

- Search for papers on "machine learning" published between 2018 and 2022, keeping the final merged output to 50 rows:
  ```bash
  python run_all.py --query "machine learning" --year-from 2018 --year-to 2022 --limit 50
  ```

- Search across OpenAlex, Crossref, and arXiv with a per-provider cap of 200 results:
  ```bash
  python run_all.py --query "deep learning" --sources openalex,crossref,arxiv --limit 200 --per-provider
  ```

- Merge multiple existing CSV files into one deduplicated output (written to `<out-dir>/phase_2/`):
  ```bash
  python run_all.py --merge file1.csv file2.csv --out-dir merged_results
  ```