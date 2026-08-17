# Active Flows and Runtime Guide

## Scope

This guide describes only the nine Flow artifacts in `import_ready_flows/` and the runtime needed by them. Retired continuation orchestration, visualization-only, CUBE scheduling, and GaiA boundary adapters are not part of the current product scope.

## Flow topology

```text
User question
  └─ 06 Agent Tool Router
       ├─ new data question ───────> 01 Data Analysis
       ├─ fixed report request ─────> 07-1 Realtime Production Report
       │                                └─ snapshot + report.context.v1 stored
       ├─ report snapshot follow-up ──> 07-2 Report Follow-up
       │                                └─ restore and analyze the same snapshot only
       └─ report current/cross-source ─> 01 Data Analysis
                                        └─ explicit current/latest: new retrieval

Metadata authoring
  ├─ 02 Domain Saving
  ├─ 03 Table Catalog Saving
  └─ 04 Main Flow Filter Saving

Metadata inquiry ───────────> 05 Metadata QA

Direct compatibility run ──> 07 Legacy Realtime Production Report
                              └─ original direct response; no snapshot context
```

## Flow responsibilities

| Flow | Use it when | Key output |
| --- | --- | --- |
| 01 Data Analysis | A question can be answered from registered metadata and one analysis request | Answer, result rows, result/download reference, optional curated intermediate data |
| 02 Domain Saving | A work owner wants to register business-language rules | Normalized Domain item; `dry_run` is on by default |
| 03 Table Catalog Saving | A data owner wants to register a dataset, physical columns, filters, and retrieval configuration | Normalized Table Catalog item |
| 04 Main Flow Filter Saving | A shared filter rule is needed across questions | Main filter item |
| 05 Metadata QA | The user asks what is registered or how a dataset is interpreted | Metadata-grounded answer |
| 06 Agent Tool Router | A normal chat entry point should select one supported Flow | Direct child Flow answer |
| 07 Legacy Realtime Production Report | The pre-follow-up Report response and graph must be reproduced | Direct Report answer and artifact links; no snapshot/session context |
| 07-1 Realtime Production Report | A fixed end-to-end production report is requested | Compact answer, HTML/CSV links, and a session-bound snapshot context for Flow 07-2 |
| 07-2 Report Follow-up | A same-session question selects columns, filters, sorts, or ranks the last Report snapshot or a pre-aggregated Report view | Snapshot-only answer; no metadata catalog, groupby, join, or source retrieval |

## Report follow-up contract

Flow 07-1 stores the selected process-group dataset in the shared Result Store and publishes its available Report views through `report.context.v1`. Flow 07-2 restores only the referenced Report snapshot/view after validating the same session, expiry, completeness, declared columns, metrics, grain, predicates, and allowed operations. The Report API and Agent receive only compact references and KPI facts; raw rows and HTML are not copied into chat history or the Router prompt.

| Follow-up wording | Data source | Retrieval behavior |
| --- | --- | --- |
| `그중 생산부족 제품만 보여줘` | Report creation snapshot | Flow 07-2 restores the declared Report view; no source query |
| `그중 현재작업재공이 0인 제품을 5개 보여줘` | Report creation snapshot | `현재작업재공` is treated as a Report column, then Flow 07-2 filters and limits the stored view |
| `방금 Report의 현재 WIP도 알려줘` | Current registered source | Flow 01 performs a new retrieval |
| Explicit Report reference without a valid context | None | Clarify or return a context error; never silently run a new query |

The boundary is enforced by routing and again inside Flow 07-2. Flow 06 sends snapshot-only Report questions to Flow 07-2 and explicit current/latest or cross-source requests to Flow 01. Flow 07-2 contains no source retriever, validates the Report query-source contract before execution, and never falls back to Flow 01. Its guarded planner does not call the LLM for missing/expired context, clarification, or live-query handoff states. Its result loader also checks the same session, reference expiry, and complete row storage before restoring data.

## Data Analysis display contract

The Flow 01 answer adapter has two deliberately separate display paths.

| Item | User display | LLM prompt | Purpose |
| --- | --- | --- | --- |
| Result table | Optional | No | Final result preview |
| Curated intermediate results | Optional | No | Show the final pre-contract calculation, or the last successful stage on error |
| Intermediate download links | When an intermediate item is present | No | Inspect the same bounded checkpoint as CSV |
| Step output / helper trace | Never | No | Internal diagnostics only; removed from answer body |
| Suggested next questions | Message metadata only | No | UI can render suggestions without repeating them in the answer body |

`중간 결과 미리보기 행 수` applies only to the rendered table. The executor stores at most five preview rows per published checkpoint, so changing the UI value cannot increase LLM token usage or retain unlimited rows.

For a multi-source analysis, the intermediate section can show one filtered source table per source and a separate join/calculation table. A normal single-source analysis shows only the meaningful final-pre-contract checkpoint. An error shows the latest successful checkpoint instead of hiding all evidence.

### API table contract

The final-result table keeps its preview rows in data.rows and exposes only display metadata through answer_sections.result_table. Curated intermediate checkpoints now follow the same separation: intermediate_tables contains the bounded preview rows, while answer_sections.intermediate_tables contains the title, columns, labels, total count, download information, and a row_source reference.

This preserves a compact answer message and prevents a browser client from parsing trace or evidence objects to create a table. Intermediate preview rows are removed from the public analysis, trace, and answer evidence copies, leaving `intermediate_tables[n].rows` as their only API location. The client can render each intermediate table from that field, show the preview notice only when `preview_only` is true, and use the optional download object for the full CSV. The 22 API node exposes an advanced `중간 결과 미리보기 행 수` input; values are limited to the same 1–5 range as the answer-message adapter.

## Required runtime services

### Langflow

- Target versions: Langflow 1.11.0, langflow-base 0.11.0, LFX 1.11.0.
- Configure the language-model Provider in Langflow. Default model values in Flow artifacts use `gemini-3.5-flash-lite`.
- Configure the `MONGO_URL` Credential Global Variable for metadata, result store, and session state.

### API Server

Run the one FastAPI artifact/report server that the generated Report nodes target by default:

```powershell
python API_SERVER\app.py
```

It serves:

- `/download.csv` and `/download.json` for data-result references;
- `/reports` and `/reports/view/{report_id}` for the Flow 07 and 07-1 HTML report lifecycle;
- `/health` for an operational health check.

The default local URL is `http://127.0.0.1:5000`. Keep the listen address and public link address separate through `API_SERVER/.env`; do not expose `0.0.0.0` as a user-facing URL.

## Import and update

1. Import `00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json`.
2. Set the Provider credential and `MONGO_URL`.
3. Re-select `대상 Flow` in any persisted Router tool that already has a `flow_id_selected` from an older import. In Flow 06, verify that `run_realtime_production_report` selects `07-1. v5_realtime_production_report` and `run_report_followup` selects `07-2. v5_report_followup`.
4. Set `04A 신뢰 카탈로그 조회 작업 구성기.retrieval_mode` to `live` only after a source-level smoke test. The default is `dummy`.

When custom component source changes, regenerate artifacts in this order:

```powershell
python tools\build_v5_auxiliary_flows.py
python tools\build_data_analysis_flow_v2.py
python tools\build_import_ready_bundle.py
```

## Failure behavior

- If MongoDB metadata cannot be loaded, the analysis must stop with the metadata connection/registration reason. It must not invent a dataset key or column contract.
- If the Report context is missing, expired, cross-session, incomplete, or does not declare the requested operation, Flow 07-2 returns a bounded context/clarification error and does not query a live source.
- If an output contract fails after retrieval or filtering, the answer remains an error but exposes the last successful curated intermediate result and its download link when one exists.
- Flow 07 never writes Report follow-up Context. Use Flow 07-1 when a later Flow 07-2 question is required.
