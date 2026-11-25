# Camptocamp.org scraper

Collects climbing **routes** and **outings** from the public camptocamp.org REST API (`https://api.camptocamp.org`) and stores them in a PostgreSQL database. Most outings are associated with a conditions report (excellent, good, average, bad), so this will be used as training data for an ice conditions prediction model.

All the code is in [c2c_scraper/](../c2c_scraper/), which is also the Docker build context.

- Scraper: [c2c_scraper/c2c/scraper.py](../c2c_scraper/c2c/scraper.py) (`C2CScraper`, `C2CApiCallIterator`)
- Entrypoint: [c2c_scraper/main.py](../c2c_scraper/main.py)
- Parsing helpers: [c2c_scraper/c2c/parsing.py](../c2c_scraper/c2c/parsing.py)
- DB layer: [c2c_scraper/c2c/db.py](../c2c_scraper/c2c/db.py), [c2c_scraper/c2c/schema.py](../c2c_scraper/c2c/schema.py)
- Config: [c2c_scraper/c2c/config.json](../c2c_scraper/c2c/config.json)
- Logging: [c2c_scraper/c2c/logging_config.py](../c2c_scraper/c2c/logging_config.py)

---

## 1. Running it

```bash
cd c2c_scraper
pip install -r requirements.txt    
python main.py init     # create the schema, then do a full scrape of routes and their outings
python main.py update   # incremental scrape of recent outings
```

In both modes, the scraper first applies any pending database migrations (Alembic, see [section 6](#6-data-written)), then starts scraping. On an empty database this creates the schema.

The database is set by the `DATABASE_URL` environment variable, which should be set by hand in the case the scraper is run from command line without using the docker image. The docker image reads this environment variable from `.env.runtime`

Environment variables read at runtime:

| Variable | Default | Effect |
|---|---|---|
| `DATABASE_URL` | *(none, required)* | SQLAlchemy connection string |
| `LOG_LEVEL` | `INFO` | Root log level |
| `LOG_JSON` | `1` | `1` gives one JSON object per line; anything else gives plain text |
| `LOG_TO_FILE` | `0` | `1` also writes `app.log` and `error.log` (rotating, 10 MB × 5) |
| `LOG_DIR` | `logs` | Folder for the log files |
| `RUN_ID` | random UUID | Correlation id added to every log line |

---

## 2. Configuration (`c2c/config.json`)

| Key | Value | Used for |
|---|---|---|
| `parallel` | `["init"]` | Modes that use a `multiprocessing.Pool`. `update` runs sequentially. |
| `num_processes` | `8` | Pool size |
| `debug_mode` | `false` | If true, each activity stops after its 3rd page of results |
| `c2c_scraper_parameters.routes_url` | `https://api.camptocamp.org/routes` | |
| `c2c_scraper_parameters.outings_url` | `https://api.camptocamp.org/outings` | |
| `c2c_scraper_parameters.activities_of_interest` | `snow_ice_mixed`, `mountain_climbing`, `ice_climbing` | Activities scraped, one after the other |
| `activities_filter` / `routes_filter` / `outings_date_filter` | `act=` / `r=` / `date=` | Query-string prefixes |
| `num_results_per_page` | `100` | Page size (the API maximum, enforced by an assert) |
| `refresh_window_days` | `7` | `update` only. Known outings dated within this many days are checked for changes, and the update window overlaps the last known outing by this many days. This is useful as sometimes users creates draft of their outings, without the conditions rating and updates them later |

---

## 3. Camptocamp API calls

| Call | Purpose | Retries |
|---|---|---|
| `GET /routes?act=<activity>&offset=…&limit=100` | List every route for an activity (`init`) | yes |
| `GET /outings?r=<route_id>&offset=…&limit=100` | List every outing of a route | yes |
| `GET /routes/<id>` | Full route document (French title, associated waypoints) | **no** |
| `GET /outings?date=<start>,<today>&act=<activity>&offset=…&limit=100` | List recent outings for an activity (`update`) | yes |
| `GET /outings/<id>` | Full outing document (associated routes) | **no** |

### Pagination: `C2CApiCallIterator`

This class iterates over a paginated list endpoint and yields the raw JSON of one page at a time.

- The first request reads `total` from the response and sets the page count to `ceil(total / limit)`.
- Each later request sends `offset=(page-1)*limit` and `limit`.
- **Retries:** up to 3 attempts per page, with a 20 s timeout per request. Network errors (`requests.RequestException`), HTTP 429 and 5xx are retried after a 1 s and then a 2 s backoff. There is no sleep after the last attempt.
- **Fail fast:** any other non-200 status, such as a 4xx, is logged as `c2c.api.failed` and raised immediately as `requests.HTTPError`, without a retry.
- **Give up:** when the retries run out it logs `c2c.api.give_up` and raises the last error. That is a `requests.HTTPError` for 429 and 5xx, or the network exception.
- **API limit:** the API rejects `offset + limit > 10000` with **HTTP 400**, so a listing can reach at most 10,000 items. Route totals per activity are currently about 2,500 to 5,700, which is under the limit. Past it, `init` fails fast on the 400.
- **Duplicates across pages:** listings are returned newest first. An item created during a run pushes later items back by one position, so the same document can appear on two consecutive pages. `_scrape_activity` drops documents already seen earlier in the same activity and logs `c2c.batch.duplicates_dropped`. A deletion during the run could cause an item to be skipped, but that is rare.

The single-document requests (`/routes/<id>` and `/outings/<id>`) use a plain `requests.get` with a 30 s timeout and **no retry**. They fail on the first error.

---

## 4. `init` mode: full route scrape

```mermaid
flowchart TD
    A[create_db: tables + orientations] --> B{for each activity}
    B --> C[load route_ids already in DB]
    C --> D[page through /routes?act=activity]
    D --> E["per page: Pool(8).map(scrape_route, docs)"]
    E --> F[accumulate results in memory]
    F --> D
    D -- all pages done --> G[write results sequentially, one commit per route]
    G --> B
```

For each activity in `activities_of_interest`:

1. **Load known IDs.** It loads every `route_id` whose `last_updated >= 2000-01-01`, which in practice means every route already in the DB. Because this happens before each activity, a route that belongs to several activities is scraped only once.
2. **List routes.** It pages through `/routes?act=<activity>`, dropping routes already seen on an earlier page. Each page of up to 100 route summaries goes to `scrape_route`, run through a fresh `multiprocessing.Pool(num_processes)` (one pool per page).
3. **`scrape_route(routeData)`** does the following for each route:
   - It returns `skipped` if the ID is already known.
   - It pages through `/outings?r=<route_id>` and keeps `outing_id`, `date` (`date_start`), `conditions` (`condition_rating`) and `last_updated`.
   - It fetches `/routes/<id>` to build the title (see below).
   - It extracts the fields listed in [§6](#6-data-written).
   - It never raises. An exception is returned as `{"error": "..."}` so the parent process can log it once.
4. **Write.** When **all** pages for the activity are scraped, the results are written in one `Session`. Each route is a separate `insert_route` and `commit`. Skipped and errored items are counted and not written. On an `IntegrityError` the session is rolled back and the item is logged as `c2c.db.integrity_error`.

Results are held in memory until the whole activity has been scraped. A crash partway through an activity (for example when a listing page runs out of retries) loses everything scraped for that activity. Activities that finished earlier are already committed.

---

## 5. `update` mode: incremental outing scrape

```mermaid
flowchart TD
    A["start = min(MAX(outings.date), today) - refresh_window_days"] --> B{for each activity}
    B --> C["load known outing_ids (date >= start - 30 days)<br/>+ refresh candidates (date >= today - refresh_window_days)"]
    C --> D["page through /outings?date=start,today&act=activity<br/>(drop duplicates across pages)"]
    D --> E{"scrape_outing:<br/>known id?"}
    E -- "known, recent and date/rating changed" --> R[refresh result, no API call]
    E -- "known, otherwise" --> S[skipped, no API call]
    E -- new --> N["GET /outings/id (route associations)"]
    R --> D
    S --> D
    N --> D
    D -- done --> W{write each result}
    W -- refresh --> U["update_outing: date, conditions, last_updated"]
    W -- new --> G{every associated route in DB?}
    G -- yes --> H[insert_outing linked to those routes]
    G -- "no, some missing" --> I["scrape_route(route_id) for each missing route<br/>insert route together with all its outings"]
```

1. **Window.** Unless `update_date_start` is passed to the constructor, the start date is `min(MAX(outings.date), today) - refresh_window_days` (7 days by default). The overlap catches outings that were posted late, and the `min` keeps the whole refresh window inside the listing. If the `outings` table is empty, `run()` raises a `RuntimeError` telling you to run `init` before `update`, and it is logged as `app.crash`.
2. **IDs loaded from the DB, per activity, after the previous activity's writes:**
   - *known IDs*: outings whose `date >= start - 30 days`;
   - *refresh candidates*: `{outing_id: (date, conditions)}` for outings whose `date >= today - refresh_window_days` (`load_outings_for_refresh`).
3. **List outings.** It pages through `/outings?date=<start>,<today>&act=<activity>`. Because `update` is not in `parallel`, this runs sequentially. Duplicates across pages are dropped.
4. **`scrape_outing`.** It decides from the listing document alone, which already contains `document_id`, `date_start` and `condition_rating`:
   - **Known and a refresh candidate:** if the listing's `(date_start, condition_rating)` differs from the stored `(date, conditions)`, it returns a *refresh* result. Otherwise the outing is *skipped*. Neither case makes an API call.
   - **Known but older than the refresh window:** *skipped*, with no API call.
   - **New:** it fetches the full `/outings/<id>` document, because the listing does not include `associations.routes`.
5. **Write.**
   - *Refresh* results: `update_outing` overwrites `date`, `conditions` and `last_updated`. Route links are not touched. Each one logs `c2c.outing.refreshed` with the previous and current values, and the count appears as `total_refreshed` in the summary.
   - *New outings* (`_insert_item`): if every associated route already exists, the outing is inserted and linked to those routes. If a route is missing, `scrape_route(route_id=…)` runs in the parent process: it fetches the route, all of its outings (including this one) and its title, and the route is inserted along with that outing list. In that case the outing is **not** inserted separately, because it arrives through the new route's outing list.

### Behaviors to know about

- **Limited refresh, by design.** To stay under camptocamp's rate limits, only outings dated within the last `refresh_window_days` are checked for changes, and only on `date` and `condition_rating`. Older outings are never refreshed. Route attributes are written only when the route is first inserted.
- **Refresh only covers listed activities.** `init` stores every outing of a route, including outings for other activities such as ski touring. `update` only lists the activities in `activities_of_interest`, so those other outings are never refreshed.
- **Partial links (accepted).** Take an outing linked to route A (already in the DB) and route B (missing). Route B is inserted with the outing, but the outing is never linked to route A.
- **Routes outside the target activities.** A missing route is scraped whatever its activity, so it can be inserted with all three activity flags set to `NULL`.

---

## 6. Data written

Tables: `routes`, `outings`, `outings_mapping`, `orientations`, `orientation_mapping`, `countries`, `countries_mapping`.

The schema is managed by Alembic. Migrations live in [c2c_scraper/c2c/migrations/versions/](../c2c_scraper/c2c/migrations/versions/), and `migrate_db` ([c2c/db.py](../c2c_scraper/c2c/db.py)) applies the pending ones in a single transaction each time the scraper starts. Alembic records the applied revision in the `alembic_version` table. A database created before Alembic (it has the tables but no `alembic_version`) is first stamped at the baseline revision `0001`, then upgraded. `init` then only fills the `orientations` table. Any change to [schema.py](../c2c_scraper/c2c/schema.py) needs a new migration (see [c2c_scraper_pipeline.md](c2c_scraper_pipeline.md#3-running-and-deploying)).

| Revision | Change |
|---|---|
| `0001` | Baseline: the original schema (dates stored as strings) |
| `0002` | Merges countries that share a `countryName` (keeps the lowest `country_id` and moves its route links), makes `countryName` unique, converts `outings.date`, `outings.last_updated` and `routes.last_updated` to `DATE` |

### Entity-relationship diagram

Defined in [c2c_scraper/c2c/schema.py](../c2c_scraper/c2c/schema.py) (as of revision `0002`). The three `*_mapping` tables are many-to-many join tables whose primary key is the pair of foreign keys. Columns are nullable unless marked `NOT NULL`. Alembic's `alembic_version` table is not shown.

```mermaid
erDiagram
    routes ||--o{ outings_mapping : "has"
    outings ||--o{ outings_mapping : "reports on"
    routes ||--o{ orientation_mapping : "faces"
    orientations ||--o{ orientation_mapping : ""
    routes ||--o{ countries_mapping : "located in"
    countries ||--o{ countries_mapping : ""

    routes {
        int route_id PK
        string name
        float lat "NOT NULL"
        float lon "NOT NULL"
        int snow_ice_mixed "1 or NULL"
        int mountain_climbing "1 or NULL"
        int ice_climbing "1 or NULL"
        int elevation_min
        int elevation_max
        int difficulties_height
        int height_diff_difficulties
        string glacier
        string global_rating
        string ice_rating
        string mixed_rating
        string rock_free_rating
        date last_updated
    }

    outings {
        int outing_id PK
        date date "NOT NULL"
        string conditions
        date last_updated
    }

    outings_mapping {
        int route_id PK, FK
        int outing_id PK, FK
    }

    orientations {
        int orientation_id PK
        string orientation UK "NOT NULL"
    }

    orientation_mapping {
        int route_id PK, FK
        int orientation_id PK, FK
    }

    countries {
        int country_id PK
        string countryName UK
    }

    countries_mapping {
        int route_id PK, FK
        int country_id PK, FK
    }
```

### Route fields

| Column | Source |
|---|---|
| `route_id` | `document_id` |
| `name` | `"<first waypoint FR title>, <route FR title>"`, or whichever of the two exists, or `""` (`get_title`) |
| `lat`, `lon` | `geometry.geom` (GeoJSON point in EPSG:3857), converted to WGS84 with `pyproj` |
| `snow_ice_mixed`, `mountain_climbing`, `ice_climbing` | `1` if the activity appears in `activities`, otherwise `NULL` |
| `elevation_min`, `elevation_max`, `difficulties_height`, `height_diff_difficulties` | same-named fields |
| `glacier` | `glacier_gear` |
| `global_rating`, `ice_rating`, `mixed_rating`, `rock_free_rating` | same-named fields |
| `last_updated` | scrape date (`DATE`) |
| → `orientations` | `orientations` list, matched against the `orientations` table (created if unknown) |
| → `countries` | `areas` with `area_type == "country"`, using the French locale title. Looked up by **name** (unique), created if missing. |
| → `outings` | every outing of the route (see below) |

### Outing fields

| Column | Source |
|---|---|
| `outing_id` | `document_id` |
| `date` | `date_start` (`DATE`) |
| `conditions` | `condition_rating` (for example `excellent`, `good`, `average`, `poor`, or `NULL`) |
| `last_updated` | scrape date (`DATE`) |

The scraper handles dates as `YYYY-MM-DD` strings. The `IsoDate` column type in [schema.py](../c2c_scraper/c2c/schema.py) converts them to `DATE` on write, and `load_outings_for_refresh` turns them back into strings so they can be compared with the API's `date_start`.

The insert helpers use get-or-create for related objects, so an outing that is shared by two routes is stored once and mapped to both.

---

## 7. Logging and observability

Log lines are structured (JSON by default) and go to stdout through a queue-based handler. Event names you can grep for:

| Event | Meaning |
|---|---|
| `c2c.run.start`, `c2c.update.window` | Run mode and computed update start date |
| `c2c.activity.start` / `c2c.activity.done` | Per activity, with `total_scraped`, `total_written`, `total_skipped`, `total_errored`, `total_refreshed`, `duration_s` |
| `c2c.db.loaded_ids` | Number of known IDs and refresh candidates loaded for an activity |
| `c2c.api.iterator.pages` | Total item and page count of a listing |
| `c2c.batch.start` / `c2c.batch.done` | One page of results processed |
| `c2c.batch.duplicates_dropped` | Documents repeated from an earlier page (items shifted while paging) |
| `c2c.batch.errors` | Error count for a page plus one sample error message |
| `c2c.api.retryable_status`, `c2c.api.exception`, `c2c.api.give_up` | HTTP retry activity |
| `c2c.api.failed` | Non-retryable status, raised immediately, with a snippet of the response body |
| `c2c.outing.refreshed` | A recent outing's date or condition rating changed on camptocamp, and the DB row was updated (`previous` and `current` values) |
| `c2c.route.missing_for_outing` | `update` found an outing whose route is not in the DB |
| `c2c.db.integrity_error` | Insert conflict, rolled back and skipped |
| `c2c.debug_stop` | Debug mode stopped an activity early |
| `scraper.summary` | Final per-activity summary |
| `app.crash` | Uncaught exception, with traceback |

`tqdm` progress bars are shown only when stdout is a TTY, so they do not appear in containers.

---

## 8. Load on camptocamp.org

There is no explicit rate limiting. In `init` mode, 8 worker processes each make at least 2 requests per route: one or more outing pages plus the full route document. The only throttle is the backoff that follows 429 or 5xx responses. `update` mode is sequential and much lighter. It makes one request per listing page plus one per **new** outing. 