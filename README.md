# fb-ads-library-scraper-mcp

MCP server for scraping Facebook Ads Library search results and returning structured ad data.

## What It Does

This project exposes one MCP tool, `search_ads`, that:

1. Opens Facebook Ads Library with Playwright.
2. Scrolls to load more results (up to your `limit`).
3. Extracts ad records from embedded page JSON.
4. Returns normalized fields including:
   - `ad_archive_id`, `page_name`, `body_text`
   - `link_url`, `image_url`, `thumbnail_url`
   - `start_date_utc`, `end_date_utc`
   - `low_impression` (detected from DOM and payload signals)

## Tool

`search_ads(keywords: str, limit: int = 20) -> dict`

- `keywords`: search phrase (required)
- `limit`: max ads to return (`1-200`)

## Local Setup

```bash
uv pip install -r requirements.txt
uv run playwright install chromium
```

## Run As MCP Server

```bash
uv run python main.py
```

## Notes

- Default country is `US`.
- Search is configured as exact phrase.
- Result ordering is set to total impressions (descending).

## License

MIT. See `LICENSE`.
