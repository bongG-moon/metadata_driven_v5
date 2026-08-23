# API_SERVER

API_SERVER is a self-contained FastAPI deployment folder for the existing
artifact service. It has no runtime import from artifact_server, tools, or any
other parent-repository directory.

## Start

    cd API_SERVER
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    Copy-Item .env.example .env
    .\.venv\Scripts\python.exe app.py

The entry point intentionally uses the same production shape:

    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)

It listens on 0.0.0.0:5000. 0.0.0.0 is only a bind address; it is not a
browser URL. With the host DNS mapping in place, request:

    http://aaa.test.com/

The root URL redirects to /docs, so http://aaa.test.com/docs exposes FastAPI
Swagger UI. The former /hello route was an example only and is intentionally
not exposed.

## Configuration

Copy .env.example to .env in this folder. Set `API_SERVER_PUBLIC_BASE_URL` to
the browser-reachable base URL returned from `POST /reports`. This setting
changes only generated report links; the process continues to bind to
`0.0.0.0:5000`. Restart `app.py` after changing the public URL.

Flow 01's execution-process report uses an in-page data workbench, not a
separate `/view` page. When a user selects an original, intermediate, or final
data tab, the report lazily reads the approved full rows from `/download.json`
on the same browser origin. It then supports filtering, column sorting,
pagination, and the existing CSV download; if JSON loading is unavailable or
too large, the bounded report preview and CSV download remain usable.

The report response has `connect-src 'self'` in its Content Security Policy, so
do not point its data-reference URL at a different public origin. Configure
Flow 01 node 23's `다운로드 링크 Base URL` and node 25's report endpoint to the
same browser-facing API origin, normally the value of
`API_SERVER_PUBLIC_BASE_URL`. This allows only the report's own `/download.json`
request and does not enable arbitrary external browser fetches.

For data_ref CSV/JSON endpoints, set the API_SERVER_MONGODB settings.
Runtime HTML reports use one normal MongoDB collection: each document contains
the HTML text, title, expiry, URL token hash, report plan, and download
metadata together. The default report collection is `report_save_db`.

By default, reports reuse `API_SERVER_MONGODB_URI` and
`API_SERVER_MONGODB_DATABASE`. Set `API_SERVER_REPORT_MONGODB_URI` and
`API_SERVER_REPORT_MONGODB_DATABASE` only when reports must use a separate
MongoDB deployment or database. `API_SERVER_REPORT_COLLECTION` selects the one
report collection.

The public report experience is unchanged: `POST /reports` returns a browser
view URL and download URL, and the `/reports/view/{report_id}` and
`/reports/download/{report_id}` routes return the HTML saved in that report
document.
Local `API_SERVER/storage` is no longer used or read; a pre-existing local
report directory is not automatically migrated.

## Retained artifact endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | /live, /health, /ready | Liveness, service metadata, data-ref MongoDB and report collection readiness |
| GET | /download, /download.csv, /download.json | Approved MongoDB data_ref downloads |
| GET | /view | Interactive data explorer: full saved result filter, sort, page navigation, and CSV/JSON download |
| POST | /reports | Persist a generated HTML report |
| GET | /reports/view/{report_id} | View a stored report |
| GET | /reports/download/{report_id} | Download a stored report |
| DELETE | /reports/{report_id} | Delete a stored report |

The base route deliberately differs from the original artifact server:
API_SERVER routes / to FastAPI docs, while CSV downloads remain available at
/download or /download.csv.

`/view` remains an optional standalone data-reference explorer. The Flow 01
execution-process report instead keeps its original/intermediate/final data
tables in the report itself and uses the same `/download.json` contract only
when the user opens a table. `/view` and the in-report workbench both preserve
the CSV/JSON download contract. If the JSON is too large for the configured
download limit or the browser cannot load it, their preview and complete CSV
download remain available.

## Flow 07 and 07-1 connection

`07_1_realtime_production_report_flow_v5_standalone.json` publishes a finished
report and its follow-up context, while
`07_realtime_production_report_legacy_flow_v5_standalone.json` preserves the
direct-only report path. Both publish HTML to this API. Their `report_api_url`
input defaults to
`http://127.0.0.1:5000` for a co-located deployment. In production, set it to
the externally reachable API base URL (for example `http://aaa.test.com`) so
the returned `view_url` and `download_url` are usable by the recipient.

The Flow sends the report HTML to `POST /reports`; it does not need direct
MongoDB credentials and does not create a second local Langflow file copy.

For Flow 01 node 25, leave `발행 대상 HTML Report API 주소` blank to use the
Langflow process environment in this order: `API_SERVER_REPORT_API_URL`,
`API_SERVER_PUBLIC_BASE_URL`, then `http://127.0.0.1:5000`. A nonblank node
value is an explicit override. `API_SERVER/.env` is loaded by this API process,
so set the same environment variable for the Langflow process too when it must
choose a non-default address. The API response itself always builds the
browser-facing `view_url` and `download_url` from `API_SERVER_PUBLIC_BASE_URL`.
