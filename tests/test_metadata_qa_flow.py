from __future__ import annotations

import json

import pytest

from component_test_support import ROOT, load_module


QA_ROOT = ROOT / "langflow_components" / "metadata_qa_flow"


def _modules():
    return (
        load_module(QA_ROOT / "02_metadata_qa_context_builder.py"),
        load_module(QA_ROOT / "04_metadata_qa_response_normalizer.py"),
    )


def _table(
    dataset_key: str,
    display_name: str,
    required_params,
    description: str,
    *,
    dataset_family: str = "production",
    source_type: str = "oracle",
    db_key: str = "PNT_RPT",
    selection_criteria: dict | None = None,
    usage_rule: str = "",
) -> dict:
    payload = {
        "display_name": display_name,
        "dataset_family": dataset_family,
        "source_type": source_type,
        "required_params": required_params,
        "description": description,
        "source_config": {"db_key": db_key},
    }
    if selection_criteria is not None:
        payload["selection_criteria"] = selection_criteria
    if usage_rule:
        payload["usage_rule"] = usage_rule
    return {
        "dataset_key": dataset_key,
        "status": "active",
        "payload": payload,
    }


def _catalog() -> list[dict]:
    return [
        _table("production_today", "Production Today", ["DATE"], "당일 생산량 조회"),
        _table("production", "Production History", ["DATE"], "생산 이력 조회"),
        _table("wip_today", "WIP Today", "DATE", "당일 재공 조회"),
        _table("wip", "WIP History", {"DATE": "required"}, "재공 이력 조회"),
        _table("hold_history", "HOLD History", ["LOT_ID"], "HOLD 이력 조회"),
        _table("eqp_history_today", "EQP HISTORY TODAY", {"EQP_ID": "required"}, "당일 장비 이력 조회"),
    ]


def _domain_items() -> list[dict]:
    return [
        {
            "section": "quantity_terms",
            "key": "wip_boh_quantity",
            "status": "active",
            "payload": {"display_name": "BOH 재공", "description": "DATE가 포함된 도메인 설명"},
        },
        {
            "section": "analysis_recipes",
            "key": "current_hold_lot_selection",
            "status": "active",
            "payload": {"display_name": "현재 HOLD LOT", "description": "필수 조건과 무관한 도메인"},
        },
    ]


def _filters() -> list[dict]:
    return [
        {
            "filter_key": "DATE",
            "status": "active",
            "payload": {"display_name": "기준일", "aliases": ["날짜", "일자"]},
        }
    ]


def _build(question: str, tables: list[dict] | None = None, state: dict | None = None) -> dict:
    context_builder, _ = _modules()
    return context_builder.build_metadata_qa_context(
        {"request": {"question": question}, "state": state or {}},
        {"domain_items": _domain_items()},
        {"table_catalog_items": tables or _catalog()},
        {"main_flow_filters": _filters()},
    )


def _build_with_domains(question: str, domain_items: list[dict], *, max_bytes: int = 65536) -> dict:
    context_builder, _ = _modules()
    return context_builder.build_metadata_qa_context(
        {"request": {"question": question}, "state": {}},
        {"domain_items": domain_items},
        {"table_catalog_items": _catalog()},
        {"main_flow_filters": _filters()},
        max_bytes=str(max_bytes),
    )


def test_required_parameter_dataset_inventory_uses_catalog_only_and_fixed_columns():
    _, normalizer = _modules()
    payload = _build("DATE 조건이 필요한 데이터셋과 각각의 용도를 알려줘")
    context = payload["metadata_qa_context"]

    assert context["answer_mode"] == "datasets_by_required_param"
    assert context["answer_policy"]["use_model_response"] is False
    assert context["matched_domain_items"] == []
    assert context["matched_filters"] == []
    assert [item["dataset_key"] for item in context["matched_datasets"]] == [
        "production",
        "production_today",
        "wip",
        "wip_today",
    ]
    assert all(set(row) == {"데이터셋", "데이터셋 키", "용도", "필수 조건"} for row in context["candidate_rows"])

    result = normalizer.normalize_metadata_qa_response(
        payload,
        json.dumps(
            {
                "answer_type": "general_metadata_search",
                "answer_message": "도메인과 필터를 함께 보여줍니다.",
                "table": {"columns": ["metadata_type"], "rows": [{"metadata_type": "domain"}]},
                "source_refs": [{"metadata_type": "domain", "key": "should_not_appear"}],
            },
            ensure_ascii=False,
        ),
    )

    assert result["answer_type"] == "datasets_by_required_param"
    assert result["data"]["columns"] == ["데이터셋", "데이터셋 키", "용도", "필수 조건"]
    assert [row["데이터셋 키"] for row in result["data"]["rows"]] == [
        "production",
        "production_today",
        "wip",
        "wip_today",
    ]
    assert result["answer_sections"]["detail_table"]["title"] == "필수 조건 데이터셋"
    assert result["answer_sections"]["show_related_items"] is False
    assert result["answer_sections"]["related_items"] == []


def test_required_parameter_name_matching_is_case_insensitive_and_token_bound():
    payload = _build("date 조건이 필요한 dataset을 알려줘")
    assert payload["metadata_qa_context"]["answer_mode"] == "datasets_by_required_param"

    not_a_parameter = _build("UPDATE 조건이 필요한 데이터셋을 알려줘")
    assert not_a_parameter["metadata_qa_context"]["answer_mode"] != "datasets_by_required_param"

    unknown_parameter = _build("UNKNOWN_PARAM 조건이 필요한 데이터셋을 알려줘")
    assert unknown_parameter["metadata_qa_context"]["answer_mode"] != "datasets_by_required_param"


@pytest.mark.parametrize(
    ("question", "expected_dataset_keys"),
    [
        ("LOT_ID 조건이 필요한 데이터셋을 알려줘", ["hold_history"]),
        ("EQP_ID 조건이 필요한 데이터셋을 알려줘", ["eqp_history_today"]),
    ],
)
def test_required_parameter_dataset_inventory_is_not_limited_to_date(question: str, expected_dataset_keys: list[str]):
    payload = _build(question)
    context = payload["metadata_qa_context"]

    assert context["answer_mode"] == "datasets_by_required_param"
    assert [item["dataset_key"] for item in context["matched_datasets"]] == expected_dataset_keys


def test_specific_dataset_required_params_keeps_existing_mode():
    payload = _build("production_today의 필수 조건을 알려줘")
    assert payload["metadata_qa_context"]["answer_mode"] == "required_params"


def test_required_parameter_inventory_uses_registered_usage_when_description_is_empty():
    _, normalizer = _modules()
    table = _table("production", "Production History", ["DATE"], "")
    table["payload"]["selection_criteria"] = {"use_when": ["전일 생산", "특정 과거일 생산"]}
    payload = _build("DATE 조건이 필요한 데이터셋을 알려줘", [table])
    result = normalizer.normalize_metadata_qa_response(payload)

    assert result["data"]["rows"] == [
        {
            "데이터셋": "Production History",
            "데이터셋 키": "production",
            "용도": "전일 생산, 특정 과거일 생산",
            "필수 조건": "DATE",
        }
    ]


def test_required_parameter_dataset_inventory_preserves_more_than_twelve_rows():
    _, normalizer = _modules()
    tables = [
        _table(f"date_dataset_{index:02d}", f"DATE Dataset {index:02d}", ["DATE"], f"DATE 용도 {index:02d}")
        for index in range(13)
    ]
    payload = _build("DATE 조건이 필요한 데이터셋을 알려줘", tables)
    result = normalizer.normalize_metadata_qa_response(payload)

    assert result["data"]["row_count"] == 13
    assert result["answer_sections"]["detail_table"]["display_limit"] == 50
    assert [row["데이터셋 키"] for row in result["data"]["rows"]] == [
        f"date_dataset_{index:02d}" for index in range(13)
    ]


def test_dataset_comparison_uses_only_two_explicit_catalog_rows_and_ignores_llm_table():
    _, normalizer = _modules()
    tables = [
        _table(
            "production_today",
            "Production Today",
            ["DATE"],
            "당일 생산량 조회",
            selection_criteria={"time_scope": "current_day", "use_when": ["오늘 생산", "당일 생산"]},
        ),
        _table(
            "production",
            "Production History",
            ["DATE"],
            "이력 생산량 조회",
            selection_criteria={"time_scope": "history", "use_when": ["전일 생산", "특정 과거일 생산"]},
        ),
    ]
    payload = _build("Production History와 Production Today의 차이와 필수 조건을 비교해줘", tables)
    context = payload["metadata_qa_context"]

    assert context["answer_mode"] == "dataset_comparison"
    assert context["answer_policy"]["use_model_response"] is False
    assert context["matched_domain_items"] == []
    assert context["matched_filters"] == []
    assert [item["dataset_key"] for item in context["matched_datasets"]] == ["production_today", "production"]
    assert all(
        set(row) == {"데이터셋", "데이터셋 키", "용도·사용 시점", "기준 구분", "연결 방식", "필수 조건"}
        for row in context["candidate_rows"]
    )

    result = normalizer.normalize_metadata_qa_response(
        payload,
        json.dumps(
            {
                "answer_type": "general_metadata_search",
                "answer_message": "필터 매핑까지 모두 보여줍니다.",
                "table": {"columns": ["filter_mappings"], "rows": [{"filter_mappings": "unsafe"}]},
            },
            ensure_ascii=False,
        ),
    )

    assert result["answer_type"] == "dataset_comparison"
    assert result["data"]["columns"] == ["데이터셋", "데이터셋 키", "용도·사용 시점", "기준 구분", "연결 방식", "필수 조건"]
    assert [row["데이터셋 키"] for row in result["data"]["rows"]] == ["production_today", "production"]
    assert result["data"]["rows"][0]["기준 구분"] == "당일/현재 기준"
    assert result["data"]["rows"][1]["기준 구분"] == "이력/과거일 기준"
    assert result["answer_sections"]["detail_table"]["title"] == "데이터셋 비교"
    assert result["answer_sections"]["show_related_items"] is False


def test_wip_comparison_uses_registered_usage_and_never_leaves_blank_columns():
    _, normalizer = _modules()
    tables = [
        _table(
            "wip_today",
            "WIP Today",
            ["DATE"],
            "",
            dataset_family="wip",
            selection_criteria={"time_scope": "current_day", "use_when": ["당일 재공", "현재 재공"]},
        ),
        _table(
            "wip",
            "WIP History",
            ["DATE"],
            "",
            dataset_family="wip",
            selection_criteria={"time_scope": "history", "use_when": ["전일 재공", "특정 과거일 재공"]},
        ),
    ]
    payload = _build("WIP History와 WIP Today는 언제 각각 사용해?", tables)
    result = normalizer.normalize_metadata_qa_response(payload)

    assert result["answer_type"] == "dataset_comparison"
    rows = {row["데이터셋 키"]: row for row in result["data"]["rows"]}
    assert rows["wip_today"]["용도·사용 시점"] == "당일 재공, 현재 재공"
    assert rows["wip"]["용도·사용 시점"] == "전일 재공, 특정 과거일 재공"
    assert all(row["기준 구분"] != "" for row in rows.values())


def test_scoped_source_inventory_filters_equipment_and_goodocs_deterministically():
    _, normalizer = _modules()
    tables = [
        _table("production", "Production History", ["DATE"], "생산 이력", dataset_family="production"),
        _table("equipment_assign", "Equipment Assign현황", [], "장비 배정", dataset_family="equipment"),
        _table("eqp_uph", "Equipment UPH", [], "장비 UPH", dataset_family="equipment", db_key="GMS_DB"),
        _table("target", "PKG Target Goodocs Plan", [], "계획", dataset_family="pkg_plan", source_type="goodocs", db_key="Goodocs"),
    ]

    equipment_payload = _build("장비 관련해서 조회 가능한 데이터셋과 필요한 조건을 알려줘", tables)
    equipment_result = normalizer.normalize_metadata_qa_response(equipment_payload)
    assert equipment_payload["metadata_qa_context"]["answer_mode"] == "scoped_sources"
    assert [row["데이터셋 키"] for row in equipment_result["data"]["rows"]] == ["eqp_uph", "equipment_assign"]
    assert equipment_result["data"]["columns"] == ["데이터셋", "데이터셋 키", "분류", "연결 방식", "DB/소스", "필수 조건"]
    assert "총 2개" in equipment_result["answer_message"]

    goodocs_payload = _build("Goodocs로 연결된 데이터셋은 무엇이야?", tables)
    goodocs_result = normalizer.normalize_metadata_qa_response(goodocs_payload)
    assert goodocs_payload["metadata_qa_context"]["answer_mode"] == "scoped_sources"
    assert goodocs_result["data"]["rows"] == [
        {
            "데이터셋": "PKG Target Goodocs Plan",
            "데이터셋 키": "target",
            "분류": "pkg_plan",
            "연결 방식": "Goodocs",
            "DB/소스": "Goodocs",
            "필수 조건": "없음",
        }
    ]


def test_inventory_followup_reuses_only_valid_same_session_dataset_keys():
    _, normalizer = _modules()
    tables = [
        _table("production", "Production History", ["DATE"], "생산 이력", dataset_family="production"),
        _table("equipment_assign", "Equipment Assign현황", [], "장비 배정", dataset_family="equipment"),
        _table("eqp_uph", "Equipment UPH", [], "장비 UPH", dataset_family="equipment", db_key="GMS_DB"),
    ]
    first = _build("조회 가능한 데이터셋 목록을 알려줘", tables)
    first_result = normalizer.normalize_metadata_qa_response(first)
    inventory = first_result["state"]["metadata_qa_inventory"]

    assert inventory["contract_version"] == "metadata_qa.inventory.v1"
    assert inventory["dataset_keys"] == ["production", "equipment_assign", "eqp_uph"]

    followup = _build("여기서 장비관련 데이터셋만 알려줘", tables, first_result["state"])
    followup_result = normalizer.normalize_metadata_qa_response(followup)
    assert followup["metadata_qa_context"]["answer_mode"] == "scoped_sources"
    assert [row["데이터셋 키"] for row in followup_result["data"]["rows"]] == ["eqp_uph", "equipment_assign"]

    missing = _build("여기서 장비관련 데이터셋만 알려줘", tables)
    missing_result = normalizer.normalize_metadata_qa_response(missing)
    assert missing["metadata_qa_context"]["answer_mode"] == "inventory_followup_missing_context"
    assert missing_result["data"]["rows"] == []
    assert "직전 데이터셋 목록" in missing_result["answer_message"]


def test_session_compaction_keeps_only_bounded_metadata_qa_inventory_contract():
    loader = load_module(ROOT / "langflow_components" / "session_state_flow" / "00_mongodb_session_state_loader.py")
    writer = load_module(ROOT / "langflow_components" / "session_state_flow" / "01_mongodb_session_state_writer.py")
    state = {
        "session_id": "metadata-qa-test",
        "metadata_qa_inventory": {
            "contract_version": "metadata_qa.inventory.v1",
            "dataset_keys": ["production", "equipment_assign"],
            "scope": {"dataset_families": ["equipment"]},
            "unexpected_rows": [{"secret": "must_not_persist"}],
        },
    }

    loaded = loader._compact_state(state, 5)
    written = writer._compact_state(state, 5, 10)
    expected = {
        "contract_version": "metadata_qa.inventory.v1",
        "dataset_keys": ["production", "equipment_assign"],
        "scope": {"dataset_families": ["equipment"]},
    }
    assert loaded["metadata_qa_inventory"] == expected
    assert written["metadata_qa_inventory"] == expected


def test_request_loader_unwraps_session_loader_state_output():
    request_loader = load_module(QA_ROOT / "00_metadata_qa_request_loader.py")
    result = request_loader.build_request(
        "여기서 장비관련 데이터셋만 알려줘",
        {
            "state": {
                "session_id": "same-session",
                "metadata_qa_inventory": {
                    "contract_version": "metadata_qa.inventory.v1",
                    "dataset_keys": ["equipment_assign"],
                },
            },
            "session_state_load": {"loaded": True},
        },
    )

    assert result["state"]["session_id"] == "same-session"
    assert "session_state_load" not in result["state"]


def test_direct_domain_detail_uses_only_one_named_item_and_avoids_context_trim():
    target = {
        "section": "pandas_function_cases",
        "key": "ordered_process_range",
        "status": "active",
        "payload": {
            "display_name": "OPER_SEQ 공정 구간 필터",
            "aliases": ["공정 구간", "공정 범위", "OPER_SEQ 범위"],
            "description": "생산량이나 재공처럼 OPER_NAME과 OPER_SEQ가 있는 데이터에서 공정 구간을 요청할 때 사용",
        },
        "registration_text": "공정 순서 구간 필터의 등록 원문",
    }
    noisy_candidates = [
        {
            "section": "analysis_recipes",
            "key": f"noisy_rule_{index}",
            "status": "active",
            "payload": {"display_name": f"공정 필터 보조 규칙 {index}", "description": "공정과 필터라는 공통 단어를 포함"},
            "registration_text": "x" * 4000,
        }
        for index in range(50)
    ]

    payload = _build_with_domains("공정 순서 구간 필터는 어떤 분석에 사용해?", [target, *noisy_candidates])
    context = payload["metadata_qa_context"]

    assert [item["key"] for item in context["matched_domain_items"]] == ["ordered_process_range"]
    assert [row["key"] for row in context["candidate_rows"]] == ["ordered_process_range"]
    assert context["answer_policy"]["use_model_response"] is True
    assert payload["trace"]["inspection"]["metadata_qa_context"]["context_trimmed"] is False
    assert payload["trace"]["inspection"]["metadata_qa_context"]["context_bytes"] < 65536


def test_general_domain_metadata_candidates_are_capped_to_visible_answer_limit():
    domain_items = [
        {
            "section": "analysis_recipes",
            "key": f"rule_{index}",
            "status": "active",
            "payload": {"display_name": f"생산 계산 규칙 {index}", "description": "생산 계산 규칙"},
            "registration_text": "x" * 4000,
        }
        for index in range(50)
    ]

    payload = _build_with_domains("생산 계산 규칙은 어떤 것이 있어?", domain_items)
    context = payload["metadata_qa_context"]

    assert len(context["matched_domain_items"]) == 12
    assert len(context["candidate_rows"]) == 12
    assert payload["trace"]["inspection"]["metadata_qa_context"]["context_trimmed"] is False
