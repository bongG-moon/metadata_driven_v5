from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import importlib.util
import json
from pathlib import Path
import sys
import types
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOADER_PATH = ROOT / "langflow_components" / "route_flow_v2" / "00_router_session_context_loader.py"
WRITER_PATH = ROOT / "langflow_components" / "route_flow_v2" / "04_router_session_state_writer.py"
GAIA_INPUT_PATH = ROOT / "langflow_components" / "gaia_io" / "00_gaia_input.py"
GAIA_SESSION_EXTRACTOR_PATH = ROOT / "langflow_components" / "gaia_io" / "01_gaia_external_session_id_extractor.py"


def _install_lfx_stubs() -> None:
    if importlib.util.find_spec("lfx") is not None:
        return

    class Component:
        pass

    class InputBase:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Data:
        def __init__(self, data=None):
            self.data = data or {}

    class Message:
        def __init__(
            self,
            *,
            text="",
            data=None,
            sender=None,
            sender_name=None,
            files=None,
            session_id="",
            context_id="",
            flow_id=None,
            session_metadata=None,
            metadata=None,
            **_kwargs,
        ):
            self.text = text
            self.data = data if isinstance(data, dict) else {"text": text}
            self.sender = sender
            self.sender_name = sender_name
            self.files = files or []
            self.session_id = session_id
            self.context_id = context_id
            self.flow_id = flow_id
            self.session_metadata = session_metadata
            self.metadata = metadata if isinstance(metadata, dict) else {}

    for name in (
        "lfx",
        "lfx.custom",
        "lfx.custom.custom_component",
        "lfx.custom.custom_component.component",
        "lfx.io",
        "lfx.schema",
        "lfx.schema.data",
        "lfx.schema.message",
        "lfx.base",
        "lfx.base.io",
        "lfx.base.io.chat",
        "lfx.inputs",
        "lfx.inputs.inputs",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["lfx.custom.custom_component.component"].Component = Component
    for name in ("BoolInput", "HandleInput", "MessageTextInput", "MultilineInput", "Output"):
        setattr(sys.modules["lfx.io"], name, InputBase)
    sys.modules["lfx.base.io.chat"].ChatComponent = Component
    sys.modules["lfx.inputs.inputs"].HandleInput = InputBase
    sys.modules["lfx.schema.data"].Data = Data
    sys.modules["lfx.schema.message"].Message = Message


def _load_component(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Langflow's Component base resolves class source through sys.modules when
    # a real custom component instance is created in the target runtime.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_lfx_stubs()
loader = _load_component("test_router_session_context_loader", LOADER_PATH)
writer = _load_component("test_router_session_state_writer", WRITER_PATH)
gaia_input = _load_component("test_gaia_input", GAIA_INPUT_PATH)
gaia_session_extractor = _load_component("test_gaia_session_extractor", GAIA_SESSION_EXTRACTOR_PATH)


@dataclass
class _ToolBlock:
    name: str
    output: Any
    type: str = "tool_use"
    error: Any = None
    contents: list[Any] = field(default_factory=list)


@dataclass
class _AgentMessage:
    content_blocks: list[Any]


class _Client:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _WriteResult:
    def __init__(self, matched_count: int = 1) -> None:
        self.matched_count = matched_count


class _Collection:
    def __init__(self) -> None:
        self.document: dict[str, Any] = {}
        self.queries: list[dict[str, Any]] = []

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        self.queries.append(deepcopy(query))
        if query.get("_id") != self.document.get("_id"):
            return None
        return deepcopy(self.document)

    def replace_one(self, query: dict[str, Any], document: dict[str, Any], *, upsert: bool = False) -> _WriteResult:
        self.queries.append(deepcopy(query))
        if self.document:
            assert upsert is False
            assert query == {"_id": self.document["_id"], "revision": self.document["revision"]}
        else:
            assert upsert is True
            assert query == {"_id": document["_id"]}
        self.document = deepcopy(document)
        return _WriteResult()


def _context_message(*, question: str = "WB공정은?", session_id: str = "router-session"):
    return loader.Message(
        text=question,
        session_id=session_id,
        data={
            "text": question,
            writer.CONTEXT_KEY: {
                "version": loader.CONTEXT_VERSION,
                "session_id": session_id,
                "last_user_question": "WB공정 생산량 알려줘",
                "last_assistant_answer": "WB공정 생산량은 6건입니다.",
                "last_selected_flow": "run_data_analysis",
            },
        },
    )


def test_context_loader_uses_metadata_session_and_extracts_one_completed_ingress_exchange() -> None:
    question = "WB공정은?"
    incoming = loader.Message(text=question, session_id="native-new-session", data={"text": question})
    ingress_data = {
        "conversation_history": [
            {"role": "user", "content": "WB공정 생산량 알려줘"},
            {"role": "assistant", "content": "WB공정 생산량은 6건입니다."},
            {"role": "user", "content": question},
        ]
    }

    result = loader.load_router_context(
        incoming,
        data=json.dumps(ingress_data, ensure_ascii=False),
        metadata=json.dumps({"session_id": "cube-stable-session"}),
        enabled=False,
    )

    assert result["session_id"] == "cube-stable-session"
    assert result["message"].session_id == "cube-stable-session"
    context = result["context"]
    assert context["session_source"] == "metadata"
    assert context["state_status"] == "disabled"
    assert context["history_mode"] == "minimal_router_state"
    assert context["last_user_question"] == "WB공정 생산량 알려줘"
    assert context["last_assistant_answer"] == "WB공정 생산량은 6건입니다."
    assert context["last_selected_flow"] == ""
    assert "recent_turns" not in context
    assert result["message"].data[loader.CONTEXT_KEY] == context


def test_context_loader_malformed_ingress_and_missing_mongo_fail_open() -> None:
    incoming = loader.Message(text="후속 질문", session_id="router-session", data={"text": "후속 질문"})

    result = loader.load_router_context(
        incoming,
        data="{not-json",
        metadata="[]",
        mongo_uri="",
        enabled=True,
    )

    assert result["message"].text == "후속 질문"
    assert result["message"].session_id == "router-session"
    context = result["context"]
    assert context["state_status"] == "missing_mongo_uri"
    assert context["last_user_question"] == ""
    assert context["last_assistant_answer"] == ""
    assert any("data JSON" in warning for warning in context["warnings"])
    assert any("metadata은 JSON 객체" in warning for warning in context["warnings"])
    assert any("MongoDB 연결 정보" in warning for warning in context["warnings"])


def test_context_loader_keeps_message_identity_and_uses_only_one_completed_exchange() -> None:
    incoming = loader.Message(text="WB공정은?", session_id="native-session", data={"text": "WB공정은?"})
    incoming.id = "stored-chat-input-id"

    result = loader.load_router_context(
        incoming,
        data=json.dumps(
            {
                "conversation_history": [
                    {"role": "user", "content": "D/A공정 생산량 알려줘"},
                    {"role": "assistant", "content": "D/A 결과"},
                    {"role": "user", "content": "WB공정 생산량 알려줘"},
                    {"role": "assistant", "content": "WB공정 생산량은 6건입니다."},
                    {"role": "user", "content": "WB공정은?"},
                ]
            },
            ensure_ascii=False,
        ),
        enabled="false",
    )

    assert result["message"] is incoming
    assert result["message"].id == "stored-chat-input-id"
    assert result["context"]["state_status"] == "disabled"
    assert result["context"]["last_user_question"] == "WB공정 생산량 알려줘"
    assert result["context"]["last_assistant_answer"] == "WB공정 생산량은 6건입니다."


def test_gaia_input_session_extractor_emits_session_only_message_and_loader_restores_history() -> None:
    session_id = "d0d8cc6e-851d-492c-9572-4d767eaaf49f"
    question = "WB공정은?"
    ingress = gaia_input.GaiAInput()
    ingress.input_value = question
    ingress.data = json.dumps(
        {
            "conversation_history": [
                {"role": "user", "content": "WB공정 생산량 알려줘"},
                {"role": "assistant", "content": "WB공정 생산량은 6건입니다."},
                {"role": "user", "content": question},
            ]
        },
        ensure_ascii=False,
    )
    ingress.metadata = json.dumps(
        {
            "super_agent_id": "S010201",
            "session_id": session_id,
            "platform": "GaiA_Internal",
            "user_id": "2042725",
            "super_agent_trace_id": "a585478d-e7ad-426a-981e-84711705454a",
            "super_agent_service_id": "S010201",
        },
        ensure_ascii=False,
    )

    gaia_message = ingress.message_response()
    extractor = gaia_session_extractor.GaiAExternalSessionIdExtractor()
    extractor.input_message = gaia_message
    external_session_message = extractor.build_external_session_id()

    assert gaia_message.text == question
    assert gaia_message.session_id == session_id
    assert gaia_message.metadata["session_id"] == session_id
    assert isinstance(gaia_message.data["conversation_history"], str)
    assert external_session_message.text == session_id
    assert external_session_message.text != question

    # This mirrors the dedicated extractor -> 00.session_id edge.  The target
    # is a MessageTextInput, so it receives this Message's text (the UUID),
    # never the user question.
    result = loader.load_router_context(
        gaia_message,
        session_id=external_session_message,
        enabled=False,
    )

    assert result["session_id"] == session_id
    assert result["context"]["session_source"] == "explicit"
    assert result["context"]["last_user_question"] == "WB공정 생산량 알려줘"
    assert result["context"]["last_assistant_answer"] == "WB공정 생산량은 6건입니다."


def test_gaia_external_session_id_is_used_for_the_router_state_lookup(monkeypatch) -> None:
    """Exercise the actual GaiA extractor -> Router 00 session input path."""

    session_id = "d0d8cc6e-851d-492c-9572-4d767eaaf49f"
    ingress = gaia_input.GaiAInput()
    ingress.input_value = "그 제품은?"
    ingress.data = "{}"
    ingress.metadata = json.dumps({"session_id": session_id}, ensure_ascii=False)
    gaia_message = ingress.message_response()

    extractor = gaia_session_extractor.GaiAExternalSessionIdExtractor()
    extractor.input_message = gaia_message
    external_session_message = extractor.build_external_session_id()
    observed: dict[str, Any] = {}
    stored = {
        "last_selected_flow": "run_data_analysis",
        "last_user_question": "WB공정 생산량 알려줘",
        "last_assistant_answer": "WB공정 생산량은 6건입니다.",
    }

    def _read_state(**kwargs: Any):
        observed.update(kwargs)
        return stored, []

    monkeypatch.setattr(loader, "_load_router_document", _read_state)
    result = loader.load_router_context(
        gaia_message,
        session_id=external_session_message,
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        session_collection_name="router_session_states",
    )

    assert external_session_message.text == session_id
    assert observed == {
        "mongo_uri": "mongodb://fake",
        "mongo_database": "datagov",
        "collection_name": "router_session_states",
        "session_id": session_id,
    }
    assert result["message"].session_id == session_id
    assert result["context"]["session_source"] == "explicit"
    assert result["context"]["state_status"] == "loaded"
    assert result["context"]["state_lookup"] == "document_id"
    assert result["context"]["last_selected_flow"] == "run_data_analysis"


def test_context_loader_keeps_mongo_read_failure_distinct_from_no_saved_state(monkeypatch) -> None:
    incoming = loader.Message(text="WB공정은?", session_id="stable-session", data={"text": "WB공정은?"})
    monkeypatch.setattr(
        loader,
        "_load_router_document",
        lambda **_kwargs: ({}, ["Router 세션 상태를 읽지 못해 현재 질문만으로 라우팅합니다."]),
    )

    result = loader.load_router_context(incoming, mongo_uri="mongodb://fake")

    assert result["context"]["state_status"] == "load_failed"
    assert result["context"]["state_lookup"] == "load_failed"
    assert result["message"].text == "WB공정은?"


def test_router_state_loader_recovers_a_legacy_document_by_stable_session_id(monkeypatch) -> None:
    session_id = "gaia-stable-session"

    class _ReadCollection:
        def __init__(self) -> None:
            self.queries: list[dict[str, Any]] = []

        def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
            self.queries.append(deepcopy(query))
            if query == {"session_id": session_id}:
                return {
                    "session_id": session_id,
                    "last_selected_flow": "run_data_analysis",
                    "last_user_question": "WB공정 생산량 알려줘",
                    "last_assistant_answer": "WB공정 생산량은 6건입니다.",
                }
            return None

    class _ReadClient:
        def __init__(self, collection: _ReadCollection) -> None:
            self.collection = collection
            self.closed = False

        def __getitem__(self, _database: str):
            return {"router_session_states": self.collection}

        def close(self) -> None:
            self.closed = True

    collection = _ReadCollection()
    client = _ReadClient(collection)
    fake_pymongo = types.SimpleNamespace(MongoClient=lambda *_args, **_kwargs: client)
    monkeypatch.setattr(loader, "import_module", lambda _name: fake_pymongo)

    document, warnings = loader._load_router_document(
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        collection_name="router_session_states",
        session_id=session_id,
    )

    assert collection.queries == [
        {"_id": "router_session:gaia-stable-session"},
        {"session_id": "gaia-stable-session"},
    ]
    assert document["last_selected_flow"] == "run_data_analysis"
    assert any("기존 session_id 필드" in warning for warning in warnings)
    assert client.closed is True


def test_external_session_reads_v1_router_state_as_a_one_turn_migration_fallback(monkeypatch) -> None:
    incoming = loader.Message(text="WB공정은?", session_id="fresh-langflow-graph-session", data={"text": "WB공정은?"})
    stored = {
        "last_successful_route": "run_data_analysis",
        "recent_turns": [
            {"role": "user", "content": "WB공정 생산량 알려줘"},
            {"role": "assistant", "content": "WB공정 생산량은 6건입니다."},
        ],
    }
    monkeypatch.setattr(loader, "_load_router_document", lambda **_kwargs: (stored, []))

    result = loader.load_router_context(incoming, session_id="cube-stable-session", mongo_uri="mongodb://fake")

    context = result["context"]
    assert result["message"].session_id == "cube-stable-session"
    assert context["session_source"] == "explicit"
    assert context["last_selected_flow"] == "run_data_analysis"
    assert context["last_user_question"] == "WB공정 생산량 알려줘"
    assert context["last_assistant_answer"] == "WB공정 생산량은 6건입니다."


def test_writer_preserves_final_answer_and_canonical_session_when_mongo_is_missing() -> None:
    context_message = _context_message(session_id="cube-stable-session")
    agent_message = _AgentMessage(
        content_blocks=[_ToolBlock(name="run_data_analysis", output={"status": "success", "content": "분석 결과"})]
    )
    answer_message = writer.Message(
        text="### 답변\nWB공정 생산량은 6건입니다.",
        session_id="wrong-session",
        context_id="preserve-context",
        data={"text": "### 답변\nWB공정 생산량은 6건입니다."},
    )

    result = writer.write_router_session_state(context_message, agent_message, answer_message, mongo_uri="")

    assert result["status"]["saved"] is False
    assert result["status"]["reason"] == "missing_mongo_uri"
    assert result["message"] is answer_message
    assert result["message"].text == "### 답변\nWB공정 생산량은 6건입니다."
    assert result["message"].session_id == "cube-stable-session"
    assert result["message"].context_id == "preserve-context"
    assert result["message"].data["text"] == "### 답변\nWB공정 생산량은 6건입니다."


def test_writer_persists_only_one_question_answer_and_selected_flow(monkeypatch) -> None:
    context_message = _context_message()
    agent_message = _AgentMessage(
        content_blocks=[_ToolBlock(name="run_data_analysis", output={"status": "success", "content": "분석 결과"})]
    )
    answer_message = writer.Message(text="WB공정 생산량은 6건입니다.", data={"text": "WB공정 생산량은 6건입니다."})
    client = _Client()
    collection = _Collection()
    monkeypatch.setattr(writer, "_connect_collection", lambda *_args: (client, collection))

    result = writer.write_router_session_state(
        context_message,
        agent_message,
        answer_message,
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        session_collection_name="router_session_states",
        ttl_hours=2,
    )

    assert result["status"] == {
        "stage": "04_router_session_state_writer",
        "saved": True,
        "session_id": "router-session",
        "collection_name": "router_session_states",
        "reason": "",
        "errors": [],
        "revision": 1,
        "turn_count": 1,
        "last_selected_flow": "run_data_analysis",
    }
    assert result["message"].session_id == "router-session"
    assert client.closed is True
    assert collection.document["_id"] == "router_session:router-session"
    assert collection.document["state_version"] == "router-session-state-v2"
    assert collection.document["last_selected_flow"] == "run_data_analysis"
    assert collection.document["last_user_question"] == "WB공정은?"
    assert collection.document["last_assistant_answer"] == "WB공정 생산량은 6건입니다."
    assert "recent_turns" not in collection.document
    assert "last_answer_summary" not in collection.document
    assert collection.document["expires_at"] > collection.document["updated_at"]
    assert collection.document["created_at_kst"].endswith("+09:00")
    assert collection.document["updated_at_kst"].endswith("+09:00")
    assert collection.document["expires_at_kst"].endswith("+09:00")
    assert collection.document["created_at"].utcoffset().total_seconds() == 0
    assert collection.document["updated_at"].utcoffset().total_seconds() == 0


def test_writer_does_not_replace_route_for_explicit_tool_failure_or_false_string() -> None:
    context_message = _context_message()
    failing_agent_message = _AgentMessage(
        content_blocks=[_ToolBlock(name="run_data_analysis", output={"status": "error", "content": "실패"})]
    )
    answer_message = writer.Message(text="현재 답변은 반환됩니다.")

    error_result = writer.write_router_session_state(
        context_message,
        failing_agent_message,
        answer_message,
        mongo_uri="mongodb://fake",
    )
    disabled_result = writer.write_router_session_state(
        context_message,
        _AgentMessage(content_blocks=[]),
        answer_message,
        mongo_uri="mongodb://fake",
        enabled="false",
    )

    assert error_result["status"]["reason"] == "tool_error"
    assert error_result["message"].text == "현재 답변은 반환됩니다."
    assert disabled_result["status"]["reason"] == "disabled"
