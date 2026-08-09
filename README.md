# Unified news feed for normacs

This project aggregates news from a set of sources and publishes a unified JSON Feed to `docs/unified.json`.

---

## What the script does

The aggregator (`scripts/aggregate.py`):

1. Loads the source list from `sources.json`.
2. Collects links for each source (from HTML/XML/API).
3. Downloads news pages and extracts their text, publication date, and canonical URL.
4. Filters out short or empty articles.
5. Merges the results with the existing feed (`docs/unified.json`) without losing history.
6. Saves the result in JSON Feed 1.1 format.

---

## Repository structure

- `scripts/aggregate.py` — main aggregation pipeline.
- `scripts/http_client.py` — stateful HTTP client with per-host strategies, cookies, warm-up, and Selenium fallback.
- `sources.json` — source configuration.
- `docs/unified.json` — resulting feed.
- `tests/` — unit tests.
- `.cache/state.json` — state persisted between runs (cookies, metrics, seen URLs, cooldowns, and internal indexes).
- `.cache/pages/` — cache of downloaded pages.

---

## Running the aggregator

```bash
python scripts/aggregate.py
```

Useful flags:

- `--rebuild` — force a rebuild (disables the unchanged-index optimization and seen-URL filter).
- `--dry-run` — do not write `docs/unified.json` or state.
- `--smoke` — run a quick check against a limited set of sources.
- `--sources "Name 1,Name 2"` — run only the selected sources.
- `--limit-per-source N` — limit deep fetches per source (primarily useful for smoke runs).
- `--connect-timeout`, `--read-timeout` — global timeouts.
- `--max-runtime` — soft limit on total runtime.
- `--debug` — enable detailed debug logs.

Example targeted rebuild:

```bash
MODE="rebuild" python scripts/aggregate.py --rebuild --sources "Российская газета: Экономика"
```

---

## How the network layer works (`request_strategy`)

Each source can define a `request_strategy` in `sources.json`:

- `connect_timeout`, `read_timeout` — connection and read timeouts.
- `max_attempts`, `backoff_factor` — attempt count and exponential backoff.
- `retry_statuses` — HTTP status codes that trigger a retry.
- `extra_headers` — additional headers.
- `proxies` — proxy pool (`string` or `{http,https}`), rotated between attempts.
- `warmup` — warm-up request (URL/timeout/delay), usually used for anti-bot protection or cookies.
- `selenium_fallback` and related settings (`selenium_wait`, `selenium_wait_for`, `selenium_scroll_steps`) — browser fallback.
- `capture_cookies` — persist cookies in state.
- `record_path_on_success` — remember the last successful path.

### Important current behavior (after the latest changes)

- When explicit network-unavailability symptoms occur (`network unreachable` / `connect timeout`), the host may enter a temporary cooldown (`NEWSFEED_NETWORK_COOLDOWN_SECONDS`, default `900` seconds) to avoid spending tens of minutes on repeated identical timeouts.
- If a host has a **proxy pool**, cooldown is not activated prematurely: the remaining retry iterations and proxies are tried first.
- Successful attempts are logged at `DEBUG`; errors remain at `WARNING`.

---

## Fallback behavior for index pages

A source can define:

- `start_url` — primary index.
- `fallback_start_urls` — list of alternative index URLs.

The aggregator tries candidates in order:

1. Tries the primary URL.
2. If the request fails, moves to the next URL.
3. If the request "succeeds" but the index is effectively empty (0 raw candidates), tries the fallback.
4. If a cached index page exists and contains more candidates than the current response, uses the cached version.

This is especially useful for sources whose primary URL frequently returns a protection page or an empty page.

---

## Cache, state, and merging

### `.cache/state.json`

Stores:

- `host_state` (cookies, errors, cooldowns, warm-up flags),
- `stats.metrics` (attempt timings and statuses),
- `seen_urls`, `index_hash`,
- `first_seen`, `canonical_item_ids`, `content_hashes`, and other internal data.

### `.cache/pages/`

- caches HTML index and article pages;
- serves as a fallback during network outages.

### Merge behavior

- new articles are merged with the existing `docs/unified.json`;
- the more complete text is preferred for `content_text`;
- the more relevant or newer value is selected for timestamps (`published_at`, `fetched_at`);
- the resulting feed is limited by `FEED_MAX_ITEMS` (default 800).

---

## Content quality settings

The following settings are available in `sources.json`:

- `min_words` — minimum word count for an article,
- `content_selectors` — selectors for the main text,
- `include_patterns`/`include_regex`/`exclude_regex` — link filters,
- `restrict_domain`, `max_links`, `link_min_text_len`, `accept_empty_anchor`.

The aggregator also supports:

- API sources (`mode: api`, `api_endpoint`),
- API-to-HTML fallback (`html_fallback_on_empty_api`),
- AMP/mobile fallback during text extraction.

---

## Logging and diagnostics

At `INFO` level, logs usually show:

- source startup,
- index candidate selection,
- the number of discovered links (`raw`/`accepted`),
- per-source results (`-> N items`),
- the `SOURCE_SUMMARY` overview.

After aggregation, `docs/source-health.json` stores the result of the current
crawl separately from the bounded resulting feed. Running `scripts/metrics.py
--source-health docs/source-health.json` reports network failures, cached
fallbacks, and unexpectedly empty index pages. A source being absent from the 800
retained items is not an error by itself: an active source may be displaced by a
more frequently updated source. Diagnostic strict mode is available through
`--strict-source-health` and `--fail-on-empty-source`.

Each source row separates `retained_item_count` (the number of items in the
bounded feed) from current-crawl metrics included when `--source-health` is
provided: `current_crawl_status`, `index_fetch_status`, `raw_link_candidates`,
`accepted_links`, `attempted_articles`, and `accepted_articles`. The report also
shows `newest_retained_timestamp` and `retained_content_freshness_status`; a
source without a retained timestamp receives
`retained_content_freshness_status=no_data` rather than being considered fresh.
`last_successful_discovery_at` and `discovery_recency_status` describe successful
link discovery independently of the age of retained items. If a source does not
define its own interval, the global `--stale-hours` value is used for discovery
recency.
Checks can be enabled selectively in a source object in `sources.json`:
`expected_min_candidates` defines the minimum number of candidates discovered
during a crawl; `expected_update_hours` defines the maximum age of the last
successful discovery when `--source-health` is present (without it, the age of
the latest retained item is used); and `allow_empty: false` enables the missing-
item check only for that source. The `--fail-on-empty-source` flag enables this
check globally; `allow_empty: true`, or the command-line argument of the same
name with a source name as its value, provides an explicit exemption. The first
two checks become errors in `--strict-source-health` mode. Without per-source
settings or the global flag, the bounded feed may validly contain no items from
any individual source.

Normal builds use the following resource and quality limits, which can be
overridden through environment variables:

- `FEED_MIN_ITEMS_PER_SOURCE=5` reserves a small share of the feed for each
  represented source before filling the remaining slots by overall freshness;
- `NEWSFEED_SELENIUM_BUDGET_SECONDS=90` provides overall browser-runtime
  accounting; `NEWSFEED_SELENIUM_HOST_BUDGET_SECONDS=15` adds per-host
  accounting and fairness so repeated budgeted attempts by one host do not
  consume the browser opportunity intended for later hosts. These budgets are
  not hard wall-clock caps for an entire browser attempt. Navigation and
  explicit waits/sleeps are constrained by the remaining allowance where
  Selenium exposes suitable controls. Chrome/driver startup and synchronous
  WebDriver commands such as script execution, page-source or cookie retrieval,
  and driver shutdown cannot be safely preempted in the current in-process
  design and may overrun an allowance; their elapsed time is still recorded.
  If Chrome startup itself consumes the remaining allowance, the overrun is
  logged and the attempt does not proceed to navigation or rendering.
- `NEWSFEED_CACHE_MAX_BYTES=536870912` and `NEWSFEED_CACHE_MAX_AGE_DAYS=14`
  limit the HTML cache by size and age.

The report also includes `consecutive_failures` and `future_date_rejections`.
Strict validation becomes an error only after three consecutive failed crawls,
so an isolated network failure remains a warning.

At `DEBUG` level, logs additionally include:

- detailed fetch-attempt metrics,
- the content extraction path (`_content_source`),
- technical warm-up/Selenium details.

---

## Dependencies

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

Selenium fallbacks require a browser and WebDriver (Chromium/Chrome and chromedriver).

---

## CI / publication

- Automated builds run in GitHub Actions (`.github/workflows/build.yml`).
- Published file: `docs/unified.json` (GitHub Pages endpoint `/unified.json`).
