# Web UI API

[Back to the README](../README.md)

The web UI is served on `LISTEN_PORT` next to the webhook. Every path outside `/api/`, `/healthz` and
`/webhook/...` answers with the single-page app (`src/posteryard/webui/static/index.html`). This page is the contract
between the API (`src/posteryard/webui/api_*.py`) and the pages (`src/posteryard/webui/static/js/pages/*.js`).

## Conventions

- JSON in, JSON out. Errors are `{"error": "message"}` with a 4xx or 5xx status.
- Every `POST`, `PUT` and `DELETE` must send `X-Requested-With: posteryard`, or the answer is 403.
- Every route except `/api/session`, `/api/login` and `/api/logout` needs the session cookie, or the answer is 401.
- Times are Unix seconds (numbers) unless named `*_iso`. Rating keys are strings.
- Work that renders, fetches art or changes the server runs as a **job** on the worker thread. Such a route answers
  `202 {"job": "<id>"}`. The page polls `GET /api/jobs/<id>` until `state` is `done` (read `result`) or `failed`
  (read `error`).
- `status` values of an image record: `uploaded`, `uploading`, `preview`, `manual`, `failed`. `null` means
  Posteryard has no record yet. Targets: `poster`, `art` (background), `thumb` (episode thumbnail), and `item` for
  the record of an item that failed as a whole.

## Session

| Route | Answer |
| --- | --- |
| `GET /api/session` | `{"authenticated", "password_set", "setup", "setup_error", "version"}`. `setup` is true when the settings do not load; only the settings routes exist then. |
| `POST /api/login` `{"password"}` | `{"authenticated": true}` and a cookie, or 401. 429 after 10 wrong passwords in 5 minutes. |
| `POST /api/logout` | Clears the cookie. |

The password is `UI_PASSWORD`, or `WEBHOOK_SECRET` when that is empty.

## Jobs

| Route | Answer |
| --- | --- |
| `GET /api/jobs?limit=50` | `{"jobs": [Job]}`, newest first. |
| `GET /api/jobs/<id>` | `Job`: `{"id", "kind", "label", "state": "queued"\|"running"\|"done"\|"failed", "created", "started", "finished", "result", "error"}` |

## Status and actions (`api_status.py`)

`GET /api/status`:

```json
{
  "ok": true, "checks": {"threads_running": true, "worker_responsive": true, "sweep_recent": true},
  "version": "0.10.0", "dry_run": true, "paused": false, "restart_required": false,
  "server": {"name": "Plex", "url": "http://192.168.1.10:32400"},
  "libraries": ["Movies", "TV Shows"],
  "last_sweep_seconds_ago": 40, "next_sweep_seconds": 860, "sweep_minutes": 15, "daily_at": "04:15",
  "last_full_pass": "2026-10-19", "full_pass_running": false,
  "queue": {"total": 3, "by_reason": {"daily": 2, "webhook": 1}, "held": 0},
  "current": {"key": "123", "title": "Severance", "reason": "daily", "label": null, "started": 1760000000.0},
  "images": {"uploaded": 812, "preview": 0, "manual": 3, "failed": 2, "uploading": 0},
  "features": {"fanart": false, "apple_art": false, "text_check": true, "status_labels": true, "maintainerr": false,
               "collection_posters": false, "service_collections": false, "live_tv": false, "notify": true,
               "heartbeat": false, "only_rating_keys": 0},
  "art_sources": ["TMDB"],
  "alerts": [{"cause": "TMDB", "since": 1760000000.0}]
}
```

`current` is null when the worker is idle; `label` is set for a web UI job, and `title` is then the label.
`next_sweep_seconds` is null before the first sweep and while paused. `held` counts the library items set aside while
paused; they go back in the queue on resume. `alerts` lists the problems the notifier has open (empty without
`NOTIFY_URLS`); `since` is when the alert went out.

| Route | Answer |
| --- | --- |
| `POST /api/actions/sweep` | Runs a sweep now, on the schedule thread. `{"ok": true}`. 409 while paused. |
| `POST /api/actions/full` | Starts a full pass now, on the schedule thread. `{"ok": true}`. 409 while paused. |
| `POST /api/actions/pause` / `resume` | Stops or restarts sweeps, full passes and library items. Jobs still run. `{"paused"}` |
| `POST /api/actions/test-alert` | Job. Result `{"sent": bool, "message"}` |
| `POST /api/actions/restore-all` `{"confirm": "RESTORE"}` | Job, only while paused (409 otherwise; 400 without the confirm). Result `{"restored", "kept", "failed"}` |
| `POST /api/actions/restart` | Restarts the service to apply saved settings, a moment after answering `{"ok": true}`. |
| `GET /api/queue` | `{"total", "by_reason", "held", "current", "items": [{"key", "reason", "priority", "title"}]}`, first 200. `title` comes from Posteryard's records (null for an item it has not seen); a web UI job has reason `ui` and its label as title. |
| `GET /api/logs?after=0&level=info&limit=500` | `{"records": [{"seq", "time" (ISO), "level", "logger", "msg", "fields": {}}], "last"}`: the newest `limit` records after `seq` `after` at `level` or above (`debug`, `info`, `warning`, `error`). Poll with `after=last`. |
| `GET /api/activity?limit=50&offset=0&status=` | `{"total", "records": [Record]}`, newest first. `Record`: `{"key", "target", "title", "status", "updated_at", "failures", "last_error"}`; `last_error` is null when there is none. |
| `GET /api/problems` | `{"failed": [Record], "manual": [Record], "alerts": [...]}`, 500 of each at most. |
| `POST /api/problems/retry` `{"keys": [...]}` | Queues those keys, or every failed one without `keys`. `{"queued": n}`. 400 when a key is not a rating key. |
| `GET /api/previews?offset=0&limit=60&target=` | `{"total", "items": [{"name", "key", "target", "title", "mtime", "size", "url"}]}`, newest first. Lists `<key>-<target>.jpg` and `posteryard preview`'s `<key>-<target>-<name>.jpg`. |
| `GET /api/previews/<name>` | The JPEG (any `.jpg` in the folder, Live TV previews too). `DELETE /api/previews` deletes the listed previews: `{"deleted": n}` |
| `GET /api/guide` | `{"enabled", "items": [{"key", "title", "image_key", "picture", "seen_at"}]}` |

## Library and titles (`api_items.py`)

`ItemSummary`: `{"key", "title", "year", "type", "library", "library_key", "added_at", "updated_at", "status",
"labels", "override", "parent_key", "index"}`. `status` is the poster record's status (thumbnail for episodes).
`labels` lists the item's `posteryard-*` labels. `override` is `"custom"`, `"skip"` or null.

Library listings are read from the media server and kept for 60 seconds; add `refresh=1` to `/api/libraries`,
`/api/items` or `/api/search` to read them again. A media server that fails answers 502 with its error. A key that is
not a rating key answers 400; an item the server does not have answers 404.

| Route | Answer |
| --- | --- |
| `GET /api/libraries` | `{"libraries": [{"key", "title", "type", "managed", "count"}]}`. `managed`: listed in `LIBRARIES`. |
| `GET /api/items?library=&q=&status=&kind=movie\|show\|collection&sort=title\|added\|updated&offset=0&limit=60` | `{"total", "items": [ItemSummary]}`. `library` is a library key or name; without it, every managed library. Without `kind`, movies and shows; `kind=collection` lists collections. `q` matches part of the name. `status=none` finds titles without a record; `override=custom` or `skip` finds titles with custom art or skipped art. `sort=title` ignores case, then year; `added` and `updated` are newest first. `limit` is 200 at most. |
| `GET /api/search?q=` | `{"items": [ItemSummary]}`, up to 12 movies and shows, closest first. |
| `GET /api/items/<key>` | `ItemDetail` (below) |
| `GET /api/items/<key>/children` | `{"items": [ItemSummary]}`: seasons of a show, episodes of a season. |
| `GET /api/items/<key>/image?target=poster&source=auto\|preview\|server&w=` | JPEG. `preview`: the saved preview; `server`: the server's current poster; `auto`: preview, else server. `w` (16 to 2000) is the width; the shape is kept. `server` works for posters only. 404 when there is none. Sent with `Cache-Control: private, max-age=60`. |
| `POST /api/items/<key>/render` | Job. Renders every image of the item in memory; nothing is saved or uploaded. Result `{"renders": [{"target", "name", "notes", "fingerprint", "changed", "url"}]}`. `changed`: the fingerprint differs from the record's, so the next pass would render it again. |
| `GET /api/renders/<id>.jpg` | A rendered image from a render job (kept for the last 100). |
| `POST /api/items/<key>/why` | Job. Result `Why` (below). |
| `POST /api/items/<key>/art/next` | Job. Result `{"outcome"}`: `uploaded`, `preview`, `unchanged`, `manual`, `skipped`, `failed`, `gone`. |
| `POST /api/items/<key>/art/reset` | Job. `{"outcome"}` |
| `POST /api/items/<key>/art/set` | Job. Body `{"url"}`, `{"candidate": "<TMDB path or URL from Why>"}`, or the image itself with `Content-Type: image/*` (40 MB at most). A TMDB, fanart.tv or Apple TV candidate is fetched like the worker fetches art; another `https://` candidate is downloaded like `url`. `{"outcome"}` |
| `POST /api/items/<key>/forget` | Job. Hands an image changed by hand back to Posteryard and renders it. `{"outcome"}` |
| `POST /api/items/<key>/process` | Job. Renders and applies now, even when unchanged. `{"outcome"}` |

The job routes answer 404 before queuing when the server has no such item, except `forget` and `process`: they run
and answer `gone`, which also drops Posteryard's records of it.
| `POST /api/tools/tmdb-preview` `{"kind": "movie"\|"tv", "id", "seasons": []}` | Job. Same result as render. `seasons` (up to 60 numbers) is for shows only. |

`ItemDetail` = `ItemSummary` plus:

```json
{
  "parent": {"key", "title"} | null, "grandparent": {"key", "title"} | null,
  "tmdb_id": 603, "server_url": "http://.../web/index.html#!/server/.../details?key=...",
  "records": [{"target", "status", "updated_at", "failures", "last_error", "fingerprint", "image_key"}],
  "item_error": {"failures", "last_error", "updated_at"} | null,
  "override": {"custom": true, "source": "command", "skip": ["/abc.jpg"]} | null,
  "ignored": false, "all_labels": ["posteryard-ignore", "Favourite"],
  "targets": ["poster", "art"], "has_preview": {"poster": true, "art": false}, "children_count": 4
}
```

`override` here is the full record, not the summary's string. `records` leaves out the `item` record, which is
`item_error`. `server_url` opens the title in `https://app.plex.tv/desktop/` for Plex, and in the server's own web app
for Jellyfin and Emby; it is null when that cannot be told. `targets`: `poster` and `art` for movies and shows,
`poster` for seasons and collections, `thumb` for episodes.

`Why`:

```json
{
  "art": "/abc.jpg", "art_url": "https://image.tmdb.org/t/p/w500/abc.jpg", "note": "TMDB textless poster",
  "logo": "/logo.png", "logo_url": "https://image.tmdb.org/t/p/w500/logo.png",
  "notices": ["..."], "unchecked": ["fanart.tv posters"],
  "groups": [{"name": "TMDB posters", "candidates": [
    {"path": "/abc.jpg", "thumb_url": "https://image.tmdb.org/t/p/w342/abc.jpg", "full_url": "...original/abc.jpg",
     "width": 2000, "height": 3000, "votes": "rated 5.4 (12 votes)", "verdict": "used", "used": true,
     "skipped": false, "clean": true}
  ]}]
}
```

TMDB paths get `image.tmdb.org` URLs; fanart.tv and Apple TV art keep their own. Custom art (`file:...`) has no URL.
`verdict` is `used`, `skipped by art next`, `clean`, or the reason the art was not used.

## Settings (`api_settings.py`)

Settings come from the environment, overlaid by `DATA_DIR/settings.json`, which the web UI writes. A value saved in
the web UI wins over the environment. `DATA_DIR`, `LISTEN_PORT` and `WEB_UI` are read from the environment only (a
wrong value there would lock the web UI out); they are listed with `"readonly": true`.

`GET /api/settings`:

```json
{
  "groups": [{"name": "Media server", "fields": [Field]}],
  "restart_required": false, "error": null, "path": "/data/settings.json"
}
```

`Field`: `{"key", "label", "help", "type": "text"|"secret"|"switch"|"menu"|"multi"|"number"|"time"|"list",
"options": [[value, label, hint?]], "default", "value", "source": "ui"|"env"|"default", "placeholder", "readonly"}`,
plus `"min"`/`"max"` on some `number` fields and `"min"` (choices at least) on `NOTIFY_EVENTS`. `list` is text
separated by commas. `help` is plain text. A secret never comes back: its `value` is `""` and `"is_set"` says whether
it has one (a `..._FILE` in the environment counts). `error` is the reason the saved settings do not load yet, in
setup mode only; null otherwise.

| Route | Answer |
| --- | --- |
| `PUT /api/settings` `{"values": {"KEY": "text" \| null}}` | Saves; `null` drops the web UI value so the environment's applies again. A secret sent as `""` is left as it is. A value equal to the one in use (environment or default) is not saved unless the web UI already saved one. Validated by loading the whole configuration: 400 `{"error"}` when it does not load, and for an unknown or read-only key. In setup mode it saves anyway and `error` says what is left. Answers like `GET`, with `restart_required: true`. When the password (`UI_PASSWORD`, else `WEBHOOK_SECRET`) changes, it applies at once, signs every other session out, and the answer sets a fresh session cookie. |
| `POST /api/settings/test` `{"what": "server"\|"tmdb"\|"fanart"\|"maintainerr"\|"notify", "values": {...}}` | Tries the connection with the given values over the saved ones (same rules as `PUT`). Gives up after 20 seconds. `{"ok", "message"}`; a failure is `ok: false`, never an error status. |

## Setup mode

When `serve` cannot load the settings, it serves the web UI in setup mode on `LISTEN_PORT` instead of exiting (unless
`WEB_UI=false`): `/api/session` says `"setup": true` with the error, `/healthz` answers 503
`{"ok": false, "setup": true, "error"}`, and only the session, settings and `/api/actions/restart` routes exist. The
password is `UI_PASSWORD` or `WEBHOOK_SECRET` from the environment and `settings.json`. Without either, the first
visitor chooses the password: the settings routes work without a session, and `PUT /api/settings` must include a
non-empty `UI_PASSWORD` (400 otherwise); its answer signs the visitor in. From then on they need the session.
`POST /api/actions/restart` stops setup mode and starts `serve` again in the same process.

## Missing art and collections (`api_missing.py`)

A scan checks every movie and show in the managed libraries against TMDB (and fanart.tv when `FANART_API_KEY` is
set). It runs on its own thread, so posters keep being made meanwhile, and reads the art choices Posteryard already
made without changing them. The last report is kept in the database and survives a restart.

| Route | Answer |
| --- | --- |
| `GET /api/missing` | `Report` (below). Before the first scan, `scan.state` is `never` and the lists are empty. |
| `POST /api/missing/scan` | Starts a scan. `{"ok": true}`, or 409 while one runs. |

`Report`:

```json
{
  "scan": {"state": "never"|"running"|"done"|"failed", "checked": 120, "total": 812,
           "started": 1760000000.0, "finished": 1760000300.0, "error": null},
  "languages": ["en"],
  "logos": [Title],
  "textless": [Title],
  "collections": [{
    "id": 10, "name": "Dune Collection", "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
    "tmdb_url": "https://www.themoviedb.org/collection/10",
    "owned": [{"key": "101", "tmdb_id": 438631, "title": "Dune", "year": 2021}],
    "missing": [{"tmdb_id": 693134, "title": "Dune: Part Two", "year": 2024, "release_date": "2024-02-27",
                 "released": true, "poster_url": "https://image.tmdb.org/t/p/w342/y.jpg",
                 "tmdb_url": "https://www.themoviedb.org/movie/693134"}]
  }],
  "unmatched": [Title]
}
```

`Title`: `{"key", "title", "year", "type": "movie"|"show", "library", "tmdb_id", "reason", "tmdb_url"}`. `tmdb_url`
opens the TMDB page where the missing image can be added (`.../images/logos` or `.../images/posters`).

- `logos`: no logo in any of `LOGO_LANGUAGES` on TMDB, nor on fanart.tv. The poster sets the name in text.
- `textless`: no textless poster on TMDB (`reason` "TMDB has no textless poster"), or every one was found to have text
  printed on it ("Every textless poster on TMDB has text on it"). The poster then uses a backdrop, Apple TV or
  fanart.tv art.
- `collections`: TMDB collections with at least one movie in the library and at least one movie not in it. A movie
  without a release date, or released in the future, has `"released": false`.
- `unmatched`: titles the server has not matched to TMDB, which the scan could not check.
