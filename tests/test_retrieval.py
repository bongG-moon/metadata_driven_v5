from reference_runtime.dummy_data import rows_for_dataset
from reference_runtime.retrieval import run_data_retriever, strip_runtime_sources_for_final


def _catalog():
    return {
        "wip_today": {
            "payload": {
                "dataset_family": "wip",
                "source_type": "oracle",
                "source_config": {"source_type": "oracle", "db_key": "PNT_RPT", "query_template": "SELECT * FROM WIP_TABLE WHERE WORK_DATE = {DATE}"},
                "required_params": ["DATE"],
                "required_param_mappings": {"DATE": ["WORK_DATE"]},
                "filter_mappings": {"OPER_NAME": ["OPER_NAME"], "MODE": ["MODE"], "DEN": ["DENSITY"]},
                "standard_column_aliases": {"DEN": ["DENSITY"], "PKG_TYPE1": ["PKG1"], "PKG_TYPE2": ["PKG2"]},
            }
        },
        "target": {
            "payload": {
                "dataset_family": "target",
                "source_type": "goodocs",
                "source_config": {"source_type": "goodocs", "doc_id": "1212121212121212121212"},
                "required_params": ["DATE"],
                "required_param_mappings": {"DATE": ["DATE"]},
                "filter_mappings": {"MODE": ["Mode"], "MCP_NO": ["MCP NO"]},
                "standard_column_aliases": {"MODE": ["Mode"], "MCP_NO": ["MCP NO"], "OUT_PLAN": ["OUT 계획"], "INPUT_PLAN": ["INPUT 계획"]},
            }
        },
    }


def test_dummy_retriever_executes_only_declared_jobs_and_applies_filters():
    payload = {
        "request": {"question": "현재 DA공정 재공 수량 알려줘"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "job_id": "wip_da",
                    "source_alias": "wip_data",
                    "dataset_key": "wip_today",
                    "source_type": "oracle",
                    "required_params": {"DATE": "20260701"},
                    "filters": {"OPER_NAME": {"operator": "in", "values": ["D/A1"]}},
                }
            ]
        },
    }

    result = run_data_retriever(payload, _catalog())

    assert result["trace"]["inspection"]["data_retrieval"]["status"] == "ok"
    assert result["source_results"][0]["source_alias"] == "wip_data"
    assert result["source_results"][0]["row_count"] == 1
    assert result["source_results"][0]["source_execution"]["used_dummy_data"] is True
    assert result["runtime_sources"]["wip_data"][0]["OPER_NAME"] == "D/A1"
    assert "DEN" in result["runtime_sources"]["wip_data"][0]


def test_goodocs_target_keeps_source_specific_date_format():
    payload = {
        "request": {"question": "오늘 계획 보여줘"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "job_id": "target_today",
                    "source_alias": "target_data",
                    "dataset_key": "target",
                    "source_type": "goodocs",
                    "required_params": {"DATE": "2026-07-01"},
                    "filters": {"MODE": {"operator": "eq", "value": "LPDDR5"}},
                }
            ]
        },
    }

    result = run_data_retriever(payload, _catalog())

    assert result["source_results"][0]["row_count"] == 1
    assert result["runtime_sources"]["target_data"][0]["DATE"] == "2026-07-01"
    assert result["runtime_sources"]["target_data"][0]["OUT_PLAN"] == 1200


def test_reference_dummy_data_covers_data_catalog_dataset_shapes():
    expected_columns = {
        "production_today": {"WORK_DATE", "SHIFT", "FACTORY", "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO", "TSV_DIE_TYP", "DEVICE", "DEVICE_DESC", "DIE_ATTACH_QTY", "NETDIE_300_CNT", "OPER", "OPER_NAME", "OPER_SEQ", "PRODUCTION"},
        "production": {"WORK_DATE", "SHIFT", "FACTORY", "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO", "TSV_DIE_TYP", "DEVICE", "DEVICE_DESC", "DIE_ATTACH_QTY", "NETDIE_300_CNT", "OPER", "OPER_NAME", "OPER_SEQ", "PRODUCTION"},
        "wip_today": {"WORK_DATE", "SHIFT", "FACTORY", "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO", "TSV_DIE_TYP", "DEVICE", "DEVICE_DESC", "DIE_ATTACH_QTY", "NETDIE_300_CNT", "OPER", "OPER_NAME", "OPER_SEQ", "WIP"},
        "wip": {"WORK_DATE", "SHIFT", "FACTORY", "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO", "TSV_DIE_TYP", "DEVICE", "DEVICE_DESC", "DIE_ATTACH_QTY", "NETDIE_300_CNT", "OPER", "OPER_NAME", "OPER_SEQ", "WIP"},
        "target": {"DATE", "Mode", "DEN", "TECH", "PKG1", "PKG2", "LEAD", "ORG", "MCP NO", "INPUT 계획", "OUT 계획"},
        "equipment_assign": {"BAY_ID", "EQUIP_ID", "EQUIP_MODEL", "PRESS_CNT", "OPER", "OPER_NM", "MODE", "DENSITY", "TECH", "PKG1", "PKG2", "LEAD", "ORG", "PKGSIZE", "MCP_NO", "DEVICE", "DEVICE_DESC", "LOT_ID", "RECIPE_ID"},
        "eqp_uph": {"EQUIP_MODEL", "OPER", "OPER_NAME", "PRESS_CNT", "MODE", "TECH", "ORG", "DENSITY", "PKG1", "PKG2", "LEAD", "MCP_NO", "RECIPE_ID", "UPH", "LOAD_DT", "BASE_DT"},
        "lot_status": {"ERM_ID", "OPER", "OPER_NAME", "FAB", "OWNER", "GRADE", "DEVICE", "LOT_ID", "SUB_LOT_ID", "PROD_QTY", "WF_QTY", "IN_TAT", "CUM_TAT", "EQP_ID", "FLOW_ID", "OPER_IN_TM", "FAC_IN_TIME", "HOLD_STAT", "HOLD_REASON", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "PKG3", "LEAD", "MCP_NO", "THK_CD", "LOT_STAT", "LOT_GRP", "PKG_SIZE", "HOT_LOT", "HOT_LEVEL", "PKG_COMPOSIT", "DURABLE_ID", "DURABLE_TYP", "SUB_QTY", "TSV_DIE_TYPE", "EVENT_DESC", "MOVE_IN_TM", "PAD_ABNORMAL", "SWR_REQ_NO", "INSP_TARGET"},
        "hold_history": {"LOT_ID", "PROD_QTY", "OPER", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_USER", "HOLD_DESC", "FAB", "FAMILY", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO", "GRADE", "OWNER", "DEVICE", "DEVICE_DESC", "PKG_SIZE", "THK_CD", "flow_id"},
    }

    for dataset_key, columns in expected_columns.items():
        rows = rows_for_dataset(dataset_key)

        assert rows, dataset_key
        assert columns.issubset({column for row in rows for column in row}), dataset_key


def test_missing_required_param_returns_error_without_inventing_default():
    payload = {
        "request": {"question": "현재 DA공정 재공 수량 알려줘"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "job_id": "wip_da",
                    "source_alias": "wip_data",
                    "dataset_key": "wip_today",
                    "source_type": "oracle",
                    "filters": {"OPER_NAME": {"operator": "in", "values": ["D/A1"]}},
                }
            ]
        },
    }

    result = run_data_retriever(payload, _catalog())

    assert result["runtime_sources"] == {}
    assert result["trace"]["errors"][0]["type"] == "missing_required_param"


def test_source_type_mismatch_is_error():
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "source_alias": "bad",
                    "dataset_key": "wip_today",
                    "source_type": "goodocs",
                    "required_params": {"DATE": "20260701"},
                }
            ]
        }
    }

    result = run_data_retriever(payload, _catalog())

    assert result["trace"]["errors"][0]["type"] == "source_type_mismatch"
    assert result["runtime_sources"] == {}


def test_final_payload_strips_runtime_sources():
    payload = {"runtime_sources": {"wip_data": [{"LOT_ID": "L1"}]}, "source_results": []}

    final_payload = strip_runtime_sources_for_final(payload)

    assert "runtime_sources" not in final_payload
