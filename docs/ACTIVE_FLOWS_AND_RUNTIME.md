# Active Flows and Runtime Guide

## Scope

This guide describes only the nine Flow artifacts in `import_ready_flows/` and the runtime needed by them. Legacy workflow orchestration, visualization-only, CUBE scheduling, and GaiA boundary adapters are not part of the current product scope.

## Flow topology

```text
User question
  ├─ 06 Agent Tool Router ──> 01 Data Analysis (default)
  │                              ├─ Fast: deterministic query/aggregate/answer
  │                              └─ Complex: constrained pandas + optional answer LLM
  └─ 09 Continuation Router ─> 08 Data Analysis Continuation (only dependent retrieval)

Metadata authoring
  ├─ 02 Domain Saving
  ├─ 03 Table Catalog Saving
  └─ 04 Main Flow Filter Saving

Metadata inquiry ───────────> 05 Metadata QA
Fixed report request ────────> 07 Realtime Production Report
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
| 07 Realtime Production Report | A fixed end-to-end production report is requested | Compact answer and HTML/CSV report links |
| 08 Data Analysis Continuation | The first result supplies identifiers required by a second retrieval | Final two-stage answer; maximum two child runs |
| 09 Continuation Router | A chat entry point must select Flow 08 automatically | One final answer with compact continuation state |

## Data Analysis display contract

The Flow 01/08 answer adapter has two deliberately separate display paths.

| Item | User display | LLM prompt | Purpose |
| --- | --- | --- | --- |
| Result table | Optional | No | Final result preview |
| Curated intermediate results | Optional | No | Show the final pre-contract calculation, or the last successful stage on error |
| Intermediate download links | When an intermediate item is present | No | Inspect the same bounded checkpoint as CSV |
| Step output / helper trace | Never | No | Internal diagnostics only; removed from answer body |
| Suggested next questions | Message metadata only | No | UI can render suggestions without repeating them in the answer body |

`중간 결과 미리보기 행 수` applies only to the rendered table. The executor stores at most five preview rows per published checkpoint, so changing the UI value cannot increase LLM token usage or retain unlimited rows.

For a multi-source analysis, the intermediate section can show one filtered source table per source and a separate join/calculation table. A normal single-source analysis shows only the meaningful final-pre-contract checkpoint. An error shows the latest successful checkpoint instead of hiding all evidence.

## Required runtime services

### Langflow

- Target versions: Langflow 1.9.2, langflow-base 0.9.2, LFX 0.4.2.
- Configure the language-model Provider in Langflow. Default model values in Flow artifacts use `gemini-3.5-flash-lite`.
- Configure the `MONGO_URL` Credential Global Variable for metadata, result store, and session state.

### Artifact Server

Run the one FastAPI server that is currently required by supported flows:

```powershell
python -m artifact_server
```

It serves:

- `/download.csv` and `/download.json` for data-result references;
- `/reports` and `/reports/view/{report_id}` for the Flow 07 HTML report lifecycle;
- `/health` for an operational health check.

The listen address and public link address are separate. Use `ARTIFACT_LISTEN_HOST`, `ARTIFACT_LISTEN_PORT`, and `ARTIFACT_PUBLIC_BASE_URL` in `.env`; do not expose `0.0.0.0` as a user-facing URL.

## Import and update

1. Import `00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json`.
2. Set the Provider credential and `MONGO_URL`.
3. Re-select `대상 Flow` in any persisted Router tool that already has a `flow_id_selected` from an older import.
4. Set `04A 신뢰 카탈로그 조회 작업 구성기.retrieval_mode` to `live` only after a source-level smoke test. The default is `dummy`.

When custom component source changes, regenerate artifacts in this order:

```powershell
python tools\build_v5_auxiliary_flows.py
python tools\build_data_analysis_flow_v2.py
python tools\build_data_analysis_flow_v2_continuation.py
python tools\build_agent_tool_router_continuation.py
python tools\build_continuation_import_ready_bundle.py
```

## Failure behavior

- If MongoDB metadata cannot be loaded, the analysis must stop with the metadata connection/registration reason. It must not invent a dataset key or column contract.
- If an output contract fails after retrieval or filtering, the answer remains an error but exposes the last successful curated intermediate result and its download link when one exists.
- If a continuation result reference is missing, expired, cross-session, oversized, or inconsistent with its plan hash, Flow 08/09 does not run the second retrieval.
