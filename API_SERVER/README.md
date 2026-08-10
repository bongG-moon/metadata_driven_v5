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

Copy .env.example to .env in this folder. Set API_SERVER_PUBLIC_BASE_URL to the
externally reachable DNS name, such as http://aaa.test.com. This is used only
in report URLs returned from POST /reports; the process still binds to
0.0.0.0:5000.

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
| GET | /download, /download.csv, /download.json, /view | Approved MongoDB data_ref downloads and preview |
| POST | /reports | Persist a generated HTML report |
| GET | /reports/view/{report_id} | View a stored report |
| GET | /reports/download/{report_id} | Download a stored report |
| DELETE | /reports/{report_id} | Delete a stored report |

The base route deliberately differs from the original artifact server:
API_SERVER routes / to FastAPI docs, while CSV downloads remain available at
/download or /download.csv.

## Flow 07 connection

`07_realtime_production_report_flow_v5_standalone.json` publishes a finished
report to this API. Its `report_api_url` input defaults to
`http://127.0.0.1:5000` for a co-located deployment. In production, set it to
the externally reachable API base URL (for example `http://aaa.test.com`) so
the returned `view_url` and `download_url` are usable by the recipient.

The Flow sends the report HTML to `POST /reports`; it does not need direct
MongoDB credentials and does not create a second local Langflow file copy.
