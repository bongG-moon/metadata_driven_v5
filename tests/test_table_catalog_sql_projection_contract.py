from __future__ import annotations

from component_test_support import ROOT, load_module


NORMALIZER = load_module(
    ROOT
    / "langflow_components"
    / "table_catalog_saving_flow"
    / "04_table_catalog_saving_result_normalizer.py"
)
WRITER = load_module(
    ROOT
    / "langflow_components"
    / "table_catalog_saving_flow"
    / "07_table_catalog_review_writer.py"
)
GUARD = load_module(
    ROOT
    / "langflow_components"
    / "metadata_saving_rev_2_common"
    / "06_metadata_authoring_contract_guard_rev_2.py"
)


def _request_payload() -> dict:
    return {
        "request": {"raw_text": "", "dry_run": True, "duplicate_action": "skip"},
        "refinement": {
            "refined_text": "",
            "needs_more_input": False,
            "missing_information": [],
            "assumptions": [],
        },
        "errors": [],
        "trace": {},
    }


def _cte_item() -> dict:
    return {
        "dataset_key": "eqp_uph_nested",
        "status": "active",
        "payload": {
            "source_type": "oracle",
            "source_config": {
                "source_type": "oracle",
                "db_key": "PNT_RPT",
                "query_template": """
                    WITH raw_uph AS (
                        SELECT EQUIP_MODEL, RECIPE_ID, OPER_NM, UPH, LOAD_DT
                        FROM EQP_UPH
                        WHERE LOAD_DT = {DATE}
                    )
                    SELECT raw_uph.EQUIP_MODEL AS EQP_MODEL,
                           raw_uph.RECIPE_ID,
                           raw_uph.OPER_NM AS OPER_NAME,
                           raw_uph.UPH
                    FROM raw_uph
                """,
            },
            "required_params": ["DATE"],
            # LOAD_DT is a query-time predicate inside the CTE, not a returned
            # DataFrame column.  The final projection check must keep it here.
            "required_param_mappings": {"DATE": ["LOAD_DT"]},
            "filter_mappings": {
                "DATE": ["LOAD_DT"],
                "EQP_MODEL": ["EQUIP_MODEL"],
                "OPER_NAME": ["OPER_NM"],
                "UPH": ["UPH"],
            },
            # Reproduce a weak-model candidate that listed CTE source columns
            # instead of the outer SELECT aliases.
            "columns": ["EQUIP_MODEL", "RECIPE_ID", "OPER_NM", "UPH", "LOAD_DT"],
            "default_detail_columns": ["EQP_MODEL", "OPER_NAME", "UPH"],
            "metric_semantics": {
                "UPH": {
                    "semantic_type": "rate",
                    "additive": False,
                    "default_rollup": "mean",
                    "allowed_rollups": ["mean"],
                    "source_already_aggregated": False,
                }
            },
        },
    }


def test_outer_select_aliases_reconcile_nested_cte_output_without_rewriting_query_params():
    normalized = NORMALIZER.normalize_authoring(_request_payload(), {"items": [_cte_item()]})
    body = normalized["items"][0]["payload"]

    assert body["columns"] == ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"]
    assert body["filter_mappings"] == {
        "EQP_MODEL": ["EQP_MODEL"],
        "OPER_NAME": ["OPER_NAME"],
        "UPH": ["UPH"],
    }
    assert body["required_param_mappings"] == {"DATE": ["LOAD_DT"]}
    trace = normalized["trace"]["sql_result_projection"]
    assert trace[0]["status"] == "reconciled"
    assert trace[0]["projected_columns"] == ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"]

    reviewed = WRITER.review_and_write(normalized)
    assert reviewed["write_result"]["success"] is True
    assert reviewed["review"]["errors"] == []


def test_select_star_is_preserved_as_a_non_blocking_projection_fallback():
    item = _cte_item()
    body = item["payload"]
    body["source_config"]["query_template"] = "SELECT * FROM (SELECT EQUIP_MODEL, LOAD_DT FROM EQP_UPH WHERE LOAD_DT = {DATE}) src"
    original_columns = list(body["columns"])
    original_mappings = {key: list(value) for key, value in body["filter_mappings"].items()}

    normalized = NORMALIZER.normalize_authoring(_request_payload(), {"items": [item]})
    result = normalized["items"][0]["payload"]

    assert result["columns"] == original_columns
    assert result["filter_mappings"] == original_mappings
    assert normalized["trace"]["sql_result_projection"][0]["status"] == "preserved"
    assert normalized["refinement"]["assumptions"] == []


def test_outer_projection_ignores_nested_select_columns_and_uses_final_aliases():
    projection = NORMALIZER._outer_select_projection(
        """
        SELECT result.EQUIP_MODEL AS EQP_MODEL,
               result.OPER_NM AS OPER_NAME,
               result.UPH AS AVG_UPH
        FROM (
            SELECT EQUIP_MODEL, OPER_NM, UPH, LOAD_DT
            FROM EQP_UPH
            WHERE LOAD_DT = {DATE}
        ) result
        """
    )

    assert projection == {
        "status": "resolved",
        "columns": ["EQP_MODEL", "OPER_NAME", "AVG_UPH"],
        "source_to_output": {
            "EQUIPMODEL": "EQP_MODEL",
            "OPERNM": "OPER_NAME",
            "UPH": "AVG_UPH",
        },
        "ambiguous_source_columns": [],
    }


def test_same_source_name_from_join_does_not_guess_a_final_filter_alias():
    item = _cte_item()
    body = item["payload"]
    body["source_config"]["query_template"] = """
        SELECT a.STATUS AS LEFT_STATUS,
               b.STATUS AS RIGHT_STATUS
        FROM ASSIGNMENT a
        JOIN UPH b ON a.EQP_ID = b.EQP_ID
    """
    body["columns"] = ["STATUS"]
    body["filter_mappings"] = {"STATUS": ["STATUS"]}
    body.pop("default_detail_columns")
    body.pop("metric_semantics")

    normalized = NORMALIZER.normalize_authoring(_request_payload(), {"items": [item]})
    result = normalized["items"][0]["payload"]

    # A plain legacy STATUS mapping is ambiguous after the join.  It must not
    # be silently redirected to LEFT_STATUS merely because that appeared first.
    assert result["columns"] == ["STATUS"]
    assert result["filter_mappings"] == {"STATUS": ["STATUS"]}
    trace = normalized["trace"]["sql_result_projection"][0]
    assert trace["status"] == "preserved"
    assert trace["reason"] == "unproven_output_contract"


def test_exact_outer_alias_replaces_normalized_but_incorrect_declared_name():
    item = _cte_item()
    body = item["payload"]
    body["source_config"]["query_template"] = "SELECT src.OPER_NM AS OPER_NAME FROM LOT_STATUS src"
    body["columns"] = ["OPERNAME"]
    body["filter_mappings"] = {"OPER_NAME": ["OPER_NM"]}
    body["default_detail_columns"] = ["OPER_NAME"]
    body.pop("metric_semantics")

    normalized = NORMALIZER.normalize_authoring(_request_payload(), {"items": [item]})
    result = normalized["items"][0]["payload"]

    assert result["columns"] == ["OPER_NAME"]
    assert result["filter_mappings"] == {"OPER_NAME": ["OPER_NAME"]}


def test_explicit_source_mapping_repairs_weak_canonical_rhs_before_writer_validation():
    """A source-local mapping in the refined request must win over a weak LLM's ``DEN -> DEN`` output.

    This is intentionally not equipment-specific: all repaired values must be
    explicit in the authoring text and present in the query's final SELECT.
    """

    item = {
        "dataset_key": "equipment_assign",
        "status": "active",
        "payload": {
            "source_type": "oracle",
            "source_config": {
                "source_type": "oracle",
                "query_template": """
                    SELECT EQUIP_ID, EQUIP_MODEL, DENSITY, PKG1, PKG2, OPER, OPER_NM
                    FROM EQP_TABLE
                    WHERE 1=1
                """,
            },
            # Reproduce the weak extraction candidate: the canonical keys are
            # correct, but its source-side values were copied as canonical.
            "filter_mappings": {
                "EQP_ID": ["EQP_ID"],
                "EQP_MODEL": ["EQP_MODEL"],
                "DEN": ["DEN"],
                "PKG_TYPE1": ["PKG_TYPE1"],
                "PKG_TYPE2": ["PKG_TYPE2"],
                "OPER_NUM": ["OPER_NUM"],
                "OPER_NAME": ["OPER_NAME"],
            },
            "columns": ["EQP_ID", "EQP_MODEL", "DEN", "PKG_TYPE1", "PKG_TYPE2", "OPER_NUM", "OPER_NAME"],
            "default_detail_columns": ["EQP_ID", "EQP_MODEL"],
        },
    }
    payload = _request_payload()
    payload["metadata_type"] = "table_catalog"
    payload["refinement"]["refined_text"] = """
        [Filter Mappings]
        EQP_ID -> EQUIP_ID, EQP_MODEL -> EQUIP_MODEL, DEN -> DENSITY,
        PKG_TYPE1 -> PKG1, PKG_TYPE2 -> PKG2, OPER_NUM -> OPER, OPER_NAME -> OPER_NM
    """

    normalized = NORMALIZER.normalize_authoring(payload, {"items": [item]})
    guarded = GUARD.guard_metadata_contract(normalized)
    assert guarded["errors"] == []
    body = guarded["items"][0]["payload"]

    assert body["columns"] == ["EQUIP_ID", "EQUIP_MODEL", "DENSITY", "PKG1", "PKG2", "OPER", "OPER_NM"]
    assert body["filter_mappings"] == {
        "EQP_ID": ["EQUIP_ID"],
        "EQP_MODEL": ["EQUIP_MODEL"],
        "DEN": ["DENSITY"],
        "PKG_TYPE1": ["PKG1"],
        "PKG_TYPE2": ["PKG2"],
        "OPER_NUM": ["OPER"],
        "OPER_NAME": ["OPER_NM"],
    }
    assert any("정제안의 명시 source mapping" in item for item in guarded["refinement"]["assumptions"])

    reviewed = WRITER.review_and_write(guarded)
    assert reviewed["write_result"]["success"] is True
    assert reviewed["review"]["errors"] == []


def test_unproven_result_filter_mapping_keeps_legacy_contract_and_only_records_trace():
    item = _cte_item()
    body = item["payload"]
    body["source_config"]["query_template"] = "SELECT src.EQUIP_MODEL AS EQP_MODEL FROM EQP_UPH src"
    body["columns"] = ["EQUIP_MODEL", "UNPROVEN_FILTER_COL"]
    body["filter_mappings"] = {
        "EQP_MODEL": ["EQUIP_MODEL"],
        "OTHER_FILTER": ["UNPROVEN_FILTER_COL"],
    }
    body.pop("default_detail_columns")
    body.pop("metric_semantics")

    normalized = NORMALIZER.normalize_authoring(_request_payload(), {"items": [item]})
    result = normalized["items"][0]["payload"]

    assert result["columns"] == ["EQUIP_MODEL", "UNPROVEN_FILTER_COL"]
    assert result["filter_mappings"] == {
        "EQP_MODEL": ["EQUIP_MODEL"],
        "OTHER_FILTER": ["UNPROVEN_FILTER_COL"],
    }
    trace = normalized["trace"]["sql_result_projection"][0]
    assert trace["status"] == "preserved"
    assert trace["reason"] == "unproven_output_contract"
    assert normalized["refinement"]["assumptions"] == []


def test_dynamic_sql_text_does_not_create_a_projection_validation_failure():
    item = _cte_item()
    body = item["payload"]
    body["source_config"]["query_template"] = "BEGIN EXECUTE IMMEDIATE 'SELECT EQUIP_MODEL AS EQP_MODEL FROM EQP_UPH'; END;"
    original_columns = list(body["columns"])

    normalized = NORMALIZER.normalize_authoring(_request_payload(), {"items": [item]})

    assert normalized["items"][0]["payload"]["columns"] == original_columns
    assert normalized["trace"]["sql_result_projection"][0]["status"] == "preserved"
    assert normalized["refinement"]["assumptions"] == []
