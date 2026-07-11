from reference_runtime.metadata_saving import (
    InMemoryMetadataStore,
    apply_duplicate_check,
    apply_review_and_write,
    build_authoring_payload,
    build_authoring_json_prompt,
    normalize_authoring_result,
    run_authoring_dry_run,
    split_raw_text_blocks,
)


def test_current_single_authoring_prompt_is_korean_first():
    authoring_prompt = build_authoring_json_prompt("domain", "DA 공정 그룹 설명", [])

    assert "metadata saving JSON" in authoring_prompt
    assert "원문에 없는" in authoring_prompt
    assert "SQL" in authoring_prompt


def test_table_catalog_dry_run_preserves_query_and_does_not_write():
    raw_text = "wip_today는 SELECT WORK_DATE, OPER_NAME, WIP FROM WIP_TABLE WHERE WORK_DATE = {DATE} 로 조회해."
    authoring_json = {
        "items": [
            {
                "dataset_key": "wip_today",
                "status": "active",
                "payload": {
                    "display_name": "WIP Today",
                    "dataset_family": "wip",
                    "source_type": "oracle",
                    "source_config": {
                        "source_type": "oracle",
                        "db_key": "PNT_RPT",
                        "query_template": "SELECT WORK_DATE, OPER_NAME, WIP FROM WIP_TABLE WHERE WORK_DATE = {DATE}",
                    },
                    "required_params": ["DATE"],
                    "required_param_mappings": {"DATE": ["WORK_DATE"]},
                    "filter_mappings": {"OPER_NAME": ["OPER_NAME"], "MODE": ["MODE"]},
                },
            }
        ]
    }

    result = run_authoring_dry_run("table_catalog", raw_text, authoring_json)

    assert result["write_result"]["dry_run"] is True
    assert result["write_result"]["saved_count"] == 0
    assert result["write_result"]["would_save_count"] == 1
    assert result["items"][0]["payload"]["source_config"]["query_template"].startswith("SELECT WORK_DATE")


def test_table_catalog_truncated_query_is_blocked():
    payload = build_authoring_payload("table_catalog", "축약 SQL", dry_run=True)
    payload = normalize_authoring_result(
        payload,
        {
            "items": [
                {
                    "dataset_key": "bad_oracle",
                    "payload": {
                        "source_type": "oracle",
                        "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT ..."},
                    },
                }
            ]
        },
    )

    result = apply_review_and_write(payload)

    assert result["write_result"]["success"] is False
    assert any(error["type"] == "truncated_query" for error in result["write_result"]["errors"])
    assert "저장하지" in result["write_result"]["message"]


def test_legacy_duplicate_ask_is_normalized_to_safe_skip():
    existing = [
        {
            "section": "process_groups",
            "key": "DA",
            "payload": {"aliases": ["DA", "D/A"], "processes": ["D/A1"]},
        }
    ]
    payload = build_authoring_payload("domain", "DA는 D/A1부터 D/A6까지야.", duplicate_action="ask", dry_run=True)
    payload = normalize_authoring_result(
        payload,
        {
            "items": [
                {
                    "section": "process_groups",
                    "key": "DA",
                    "payload": {"aliases": ["DA", "D/A"], "processes": ["D/A1", "D/A2"]},
                }
            ]
        },
    )
    payload = apply_duplicate_check(payload, existing)
    result = apply_review_and_write(payload)

    assert payload["request"]["duplicate_action"] == "skip"
    assert result["write_result"]["success"] is True
    assert result["write_result"]["would_save_count"] == 0
    assert result["write_result"]["skipped_count"] == 1
    assert result["write_result"]["operation_by_key"] == [{"key": "process_groups:DA", "operation": "skipped"}]


def test_domain_rejects_source_config():
    payload = build_authoring_payload("domain", "domain에 SQL 넣는 잘못된 후보", dry_run=True)
    payload = normalize_authoring_result(
        payload,
        {
            "items": [
                {
                    "section": "process_groups",
                    "key": "BAD",
                    "payload": {"source_config": {"query_template": "SELECT * FROM X"}},
                }
            ]
        },
    )
    result = apply_review_and_write(payload)

    assert result["write_result"]["success"] is False
    assert any(error["type"] == "domain_source_config_forbidden" for error in result["write_result"]["errors"])


def test_non_dry_run_requires_explicit_store_and_merge_is_deep():
    store = InMemoryMetadataStore()
    store.upsert_item(
        "domain",
        {
            "section": "process_groups",
            "key": "WB",
            "payload": {"aliases": ["WB"], "processes": ["W/B1"]},
        },
    )
    payload = build_authoring_payload("domain", "WB 보강", duplicate_action="merge", dry_run=False)
    payload = normalize_authoring_result(
        payload,
        {
            "items": [
                {
                    "section": "process_groups",
                    "key": "WB",
                    "payload": {"aliases": ["W/B"], "processes": ["W/B1", "W/B2"]},
                }
            ]
        },
    )
    result = apply_review_and_write(payload, store=store)
    saved = store.get_item("domain", "process_groups:WB")

    assert result["write_result"]["saved_count"] == 1
    assert saved["payload"]["aliases"] == ["WB", "W/B"]
    assert saved["payload"]["processes"] == ["W/B1", "W/B2"]


def test_reference_domain_replace_retargets_unique_alias_and_inserts_when_new():
    store = InMemoryMetadataStore()
    existing = {
        "section": "process_groups",
        "key": "BG",
        "payload": {"display_name": "BG", "aliases": ["BG", "B/G"], "processes": ["B/G1", "B/G2"]},
    }
    store.upsert_item("domain", existing)
    payload = build_authoring_payload("domain", "BG 공정 그룹 교체", duplicate_action="replace", dry_run=False)
    payload = normalize_authoring_result(
        payload,
        {
            "items": [
                {
                    "section": "process_groups",
                    "key": "BG_PROCESS_GROUP",
                    "payload": {"display_name": "BG 공정 그룹", "aliases": ["BG", "B/G"], "processes": ["B/G1", "B/G2", "B/G3"]},
                }
            ]
        },
    )
    payload = apply_duplicate_check(payload, [existing])

    result = apply_review_and_write(payload, store=store)

    assert store.get_item("domain", "process_groups:BG")["payload"]["processes"] == ["B/G1", "B/G2", "B/G3"]
    assert store.get_item("domain", "process_groups:BG_PROCESS_GROUP") is None
    assert result["write_result"]["operation_by_key"][0]["key"] == "process_groups:BG"
    assert result["write_result"]["operation_by_key"][0]["operation"] == "replaced"

    new_payload = build_authoring_payload("domain", "신규 CMP 공정", duplicate_action="replace", dry_run=False)
    new_payload = normalize_authoring_result(new_payload, {"items": [{"section": "process_groups", "key": "CMP", "payload": {"display_name": "CMP", "aliases": ["CMP"], "processes": ["CMP1"]}}]})
    new_result = apply_review_and_write(new_payload, store=store)
    assert new_result["write_result"]["operation_by_key"][0]["operation"] == "created"
    assert store.get_item("domain", "process_groups:CMP") is not None


def test_split_raw_blocks_preserves_single_markers():
    raw = """앞 블록

<!-- single_wip_today:start -->
WIP Today
query_template:
SELECT * FROM WIP_TABLE
<!-- single_wip_today:end -->

뒤 블록"""

    blocks = split_raw_text_blocks(raw)

    assert any("single_wip_today:start" in block for block in blocks)
    assert any(block == "앞 블록" for block in blocks)
    assert any(block == "뒤 블록" for block in blocks)


def test_split_raw_blocks_keeps_sql_comment_query_with_dataset_text():
    raw = """text
당일 생산 실적 데이터는 production_today로 등록해줘.
source는 oracle이고 db_key는 PNT_RPT야.

query_template:

--쿼리 작성
SELECT WORK_DATE, OPER_NAME, PRODUCTION
FROM PROD_TABLE
WHERE WORK_DATE = {DATE}

filter_mappings는 DATE -> WORK_DATE, OPER_NAME -> OPER_NAME로 연결해줘.

Production History

text
이력 생산 실적 데이터는 production으로 등록해줘."""

    blocks = split_raw_text_blocks(raw)

    first = blocks[0]
    assert "production_today" in first
    assert "--쿼리 작성" in first
    assert "SELECT WORK_DATE" in first
    assert "filter_mappings" in first
    assert not any(block.startswith("--쿼리 작성") for block in blocks[1:])
    assert any("Production History" in block and "production으로 등록" in block for block in blocks)


def test_split_raw_blocks_keeps_with_query_with_dataset_text():
    raw = """text
WITH 기반 조회 데이터는 cte_dataset으로 등록해줘.
source는 oracle이고 db_key는 PNT_RPT야.

query_template:

WITH base AS (
  SELECT WORK_DATE, OPER_NAME, WIP FROM WIP_TABLE
)
SELECT * FROM base WHERE WORK_DATE = {DATE}

filter_mappings는 DATE -> WORK_DATE로 연결해줘."""

    blocks = split_raw_text_blocks(raw)

    assert len(blocks) == 1
    assert "WITH base AS" in blocks[0]
    assert "SELECT * FROM base" in blocks[0]


def test_split_table_catalog_blocks_keep_dataset_sql_and_descriptions_together():
    raw = """Equipment Recipe UPH

text
장비모델/제품별 UPH 데이터는 eqp_uph로 등록해줘.
source는 oracle이고 db_key는 GMS_DB야.

query_template:

SELECT

  EQUIP_MODEL
  ,OPER
  ,round(AVG_UPH_VAL,2) AS UPH
FROM UPH
WHERE 1=1

filter_mappings는 EQP_MODEL -> EQUIP_MODEL로 연결해줘.


LOT Status

text
LOT 정보조회 데이터는 lot_status로 등록해줘.
수량/지표로는 PROD_QTY, WF_QTY, IN_TAT, CUM_TAT를 사용해.

IN_TAT는 현재 공정 유입 이후 TAT이고 CUM_TAT는 누적 TAT야.

source는 oracle이고 db_key는 PNT_RPT야.

query_template:
/*Current Wip Status*/
SELECT LOT_ID, PROD_QTY, WF_QTY, IN_TAT, CUM_TAT
FROM WIP_STATE
WHERE 1=1

filter_mappings는 LOT_ID -> LOT_ID로 연결해줘.


<!-- single_hold_history:start -->
HOLD History

text
HOLD 이력 조회 데이터는 hold_history로 등록해줘.
query_template:
SELECT LOT_ID, HOLD_TM
FROM HOLD_HIS
WHERE LOT_ID = {LOT_ID}"""

    blocks = split_raw_text_blocks(raw, metadata_type="table_catalog")

    assert len(blocks) == 3
    assert blocks[0].startswith("Equipment Recipe UPH")
    assert "SELECT\n\n  EQUIP_MODEL" in blocks[0]
    assert "filter_mappings는 EQP_MODEL" in blocks[0]
    assert blocks[1].startswith("LOT Status")
    assert "IN_TAT는 현재 공정 유입 이후 TAT" in blocks[1]
    assert "FROM WIP_STATE" in blocks[1]
    assert blocks[2].startswith("<!-- single_hold_history:start -->")
    assert "FROM HOLD_HIS" in blocks[2]
