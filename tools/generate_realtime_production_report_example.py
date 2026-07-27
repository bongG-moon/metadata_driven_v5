from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "langflow_components" / "realtime_production_report_flow"
OUTPUT_ROOT = ROOT / "samples" / "realtime_production_report"


def _install_lfx_stubs() -> None:
    """Langflow가 없는 문서 생성 환경에서 순수 Report 함수를 불러오기 위한 최소 타입을 제공합니다."""

    class Component:
        pass

    class InputBase:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Data:
        def __init__(self, data=None):
            self.data = data or {}

    class Message:
        def __init__(self, text="", files=None):
            self.text = text
            self.files = list(files or [])

    for name in (
        "lfx",
        "lfx.custom",
        "lfx.custom.custom_component",
        "lfx.custom.custom_component.component",
        "lfx.io",
        "lfx.schema",
        "lfx.schema.data",
        "lfx.schema.message",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["lfx.custom.custom_component.component"].Component = Component
    for name in ("DataInput", "HandleInput", "MessageTextInput", "Output", "StrInput"):
        setattr(sys.modules["lfx.io"], name, InputBase)
    sys.modules["lfx.schema.data"].Data = Data
    sys.modules["lfx.schema.message"].Message = Message


def _load_module(name: str, path: Path):
    """파일명과 무관한 안정적인 module 이름으로 standalone 컴포넌트 원본을 로드합니다."""

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"모듈을 로드할 수 없습니다: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    """고정 seed 500행으로 브라우저에서 바로 열 수 있는 HTML과 compact API 예시를 생성합니다."""

    _install_lfx_stubs()
    generator = _load_module(
        "realtime_production_dummy_example",
        COMPONENT_ROOT / "00_dummy_production_judgement_data.py",
    )
    report = _load_module(
        "realtime_production_report_example",
        COMPONENT_ROOT / "01_realtime_production_report_builder.py",
    )
    dataset = generator.build_dummy_production_dataset(
        row_count=500,
        seed=20260727,
        work_date="2026-07-27",
        process_names="W/B1,W/B2,W/B3,W/B4",
        snapshot_at="2026-07-27T14:30:00+09:00",
    )
    rows, warnings, error = report._validate_dataset(dataset)
    if error:
        raise RuntimeError(error["message"])
    analysis = report.analyze_production_rows(rows, dataset)
    html_document = report.render_production_report_html(rows, analysis, warnings=warnings)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    html_path = OUTPUT_ROOT / "realtime_production_report_example.html"
    html_path.write_text(html_document, encoding="utf-8", newline="\n")
    descriptor = {
        "artifact_type": "html_report",
        "path": "samples/realtime_production_report/realtime_production_report_example.html",
        "mime_type": "text/html",
        "title": "실시간 생산 분석 Report",
        "download_name": html_path.name,
        "size_bytes": len(html_document.encode("utf-8")),
        "row_count": 500,
        "rendered_row_count": 500,
        "rules_version": report.RULES_VERSION,
    }
    response = {
        "contract_version": report.CONTRACT_VERSION,
        "response_type": "realtime_production_report",
        "status": "partial",
        "success": True,
        "summary": (
            f"{analysis['scope']['process_count']}개 공정 {analysis['scope']['case_count']} Case 분석: "
            f"생산부족 {analysis['production']['shortage']}건, "
            f"장비필요 {analysis['equipment']['장비필요']}건, "
            f"교체필요 {analysis['equipment']['교체필요']}건"
        ),
        "message": report._chat_message(analysis, descriptor),
        "report_scope": analysis["scope"],
        "rules_version": report.RULES_VERSION,
        "kpis": {
            "production": analysis["production"],
            "shortage": analysis["shortage"],
            "capa": analysis["capa"],
            "equipment": analysis["equipment"],
        },
        "artifacts": [descriptor],
        "warnings": warnings,
        "errors": [],
    }
    (OUTPUT_ROOT / "example_api_response.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (OUTPUT_ROOT / "dummy_data_preview.json").write_text(
        json.dumps(
            {
                **{key: value for key, value in dataset.items() if key != "rows"},
                "preview_rows": dataset["rows"][:10],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "html": str(html_path),
                "api_response": str(OUTPUT_ROOT / "example_api_response.json"),
                "row_count": dataset["row_count"],
                "html_bytes": len(html_document.encode("utf-8")),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
