"""최근 3일을 보존하고 이전 Phoenix trace 전체를 정리하는 독립 도구.

설치: python -m pip install requests
실행: python cleanup_phoenix_history.py
.env 없이 아래 설정만 입력합니다. MongoDB에는 연결조차 하지 않습니다.

GaiA Input / CHAIN / 질문 유무 / MongoDB 적재 여부와 무관하게 선택 프로젝트의
모든 오래된 trace가 대상입니다. 최근 구간까지 이어진 trace는 통째로 보존합니다.
삭제는 되돌릴 수 없으며, MongoDB에 없던 상세 실행 과정도 함께 소실됩니다.

중요: 구버전 Portal은 재수집 시 MongoDB 이력을 지울 수 있습니다.
실제 삭제 전 MongoDB를 백업하고 모든 Portal에 병합 보존 수정본을 배포/재시작하세요.
배포하지 못한 경우 대상 날짜 재수집을 차단해야 합니다.
이 파일은 Portal 배포/재시작/재수집 차단을 자동 수행하지 않습니다.
물리 DB 파일/볼륨 용량 회수는 별도 DB 운영 절차가 필요할 수 있습니다.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

# ===== 여기만 입력하세요. 실제 키를 공유하거나 Git에 올리지 마세요. =====
PHOENIX_ENDPOINT = ""  # 예: http://phoenix.example.internal
PHOENIX_API_KEY = ""  # Phoenix 삭제 권한 API 키, Langflow 키 아님
PROJECTS = []  # 예: ["프로젝트명1", "프로젝트명2"]; 지정한 프로젝트만 정리
KEEP_RECENT_DAYS = 3  # 한국 시간 오늘 포함 최근 3개 날짜 보존 (72시간 이동창 아님)
# 예: 10월 12일 실행 -> 10월 10일 00:00 KST부터 보존, 그 이전 전체 검사
DRY_RUN = True  # 기본은 미리보기. False로 변경해도 아래 확인 및 콘솔 입력 필요
MAX_TRACES = 100000  # 한 번의 검사 상한. 초과하면 일부 삭제하지 않고 중단
PAGE_SIZE = 500  # Phoenix 페이지 크기 (1~1000)
HTTP_TIMEOUT_SECONDS = 30  # 삭제 요청 자동 재시도 없음
ARCHIVE_BACKUP_AND_REFRESH_PROTECTION_CONFIRMED = False  # DB 백업 + 전체 Portal 병합보존 배포/재시작(또는 재수집 차단) 확인
FULL_TRACE_LOSS_ACCEPTED = False  # 미보관 이력 포함 전체 trace 상세 소실 확인
INGESTION_FOR_TARGET_TRACES_STOPPED = False  # 과거 trace로 추가 span 유입 차단 완료

KST = timezone(timedelta(hours=9))
PROJECT_QUERY = """query($name: String!) {
  projects(first: 50, filter: {col: name, value: $name}) { edges { node { id name } } }
}"""
TRACE_QUERY = """query($project: ID!, $trace: ID!) {
  node(id: $project) { ... on Project { id trace(traceId: $trace) {
    id traceId startTime endTime
  } } }
}"""
DELETE_QUERY = """mutation($ids: [ID!]!) {
  deleteTraces(traceIds: $ids) { __typename }
}"""


def timestamp(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("시간대 없는 시각은 삭제 판단에 사용할 수 없습니다.")
    return parsed.astimezone(KST)


def retention_cutoff(now=None):
    now = timestamp(now or datetime.now(KST))
    return datetime.combine(now.date() - timedelta(days=KEEP_RECENT_DAYS - 1), time.min, KST)


def validate_config():
    if not PHOENIX_ENDPOINT.strip() or not PHOENIX_API_KEY.strip():
        raise ValueError("파일 상단 Phoenix 주소/키를 입력하세요.")
    if not isinstance(PROJECTS, list) or not PROJECTS or any(
        not isinstance(p, str) or not p.strip() or p != p.strip() for p in PROJECTS
    ) or len(PROJECTS) != len(set(PROJECTS)):
        raise ValueError("PROJECTS에 중복 없는 프로젝트 이름 또는 Relay ID 목록을 입력하세요.")
    if type(KEEP_RECENT_DAYS) is not int or KEEP_RECENT_DAYS < 1:
        raise ValueError("보존 일수는 양의 정수여야 합니다.")
    if type(MAX_TRACES) is not int or MAX_TRACES < 1 or not 1 <= PAGE_SIZE <= 1000 or HTTP_TIMEOUT_SECONDS <= 0:
        raise ValueError("검사 상한/페이지 크기/요청 제한 시간을 확인하세요.")
    if type(DRY_RUN) is not bool:
        raise ValueError("DRY_RUN에는 True/False만 입력하세요.")
    if not DRY_RUN:
        execution_guard()


class Phoenix:
    def __init__(self, endpoint, api_key):
        import requests
        parsed = urlsplit(endpoint.strip().rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
            raise ValueError("올바른 Phoenix http(s) 주소를 입력하세요.")
        path = parsed.path.rstrip("/")
        for suffix in ("/graphql", "/v1/traces"):
            if path.endswith(suffix):
                path = path[:-len(suffix)]
        self.url = urlunsplit((parsed.scheme, parsed.netloc, path + "/graphql", "", ""))
        self.session = requests.Session()
        key = api_key.strip()
        self.session.headers.update({"Authorization": key if key.lower().startswith("bearer ") else "Bearer " + key})

    def query(self, query, variables=None):
        # HTTP/GraphQL 응답 원문에는 내부 정보가 있을 수 있어 오류 원문을 출력하지 않음
        response = self.session.post(self.url, json={"query": query, "variables": variables or {}}, timeout=HTTP_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise RuntimeError(f"Phoenix HTTP {response.status_code}: 요청 중단 (자동 재시도 없음)")
        body = response.json()
        if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
            raise RuntimeError("Phoenix GraphQL 실패: 운영 API 지원 여부 및 삭제 권한을 확인하세요.")
        return body["data"]

    def check_delete_support(self):
        data = self.query("{ __schema { mutationType { fields { name args { name } } } } }")
        fields = (data["__schema"].get("mutationType") or {}).get("fields", [])
        if not any(f["name"] == "deleteTraces" and any(a["name"] == "traceIds" for a in f["args"]) for f in fields):
            raise RuntimeError("운영 Phoenix에 deleteTraces API가 없습니다. 삭제하지 않습니다.")

    def project(self, name):
        try:
            is_id = base64.b64decode(name, validate=True).decode().startswith("Project:")
        except (ValueError, UnicodeError):
            is_id = False
        if is_id:
            data = self.query("query($id: ID!) { node(id: $id) { ... on Project { id name } } }", {"id": name})
            node = data.get("node") or {}
            if node.get("id") != name or not node.get("name"):
                raise RuntimeError("프로젝트 ID를 확인할 수 없습니다.")
            return name
        edges = self.query(PROJECT_QUERY, {"name": name})["projects"]["edges"]
        matches = [e["node"]["id"] for e in edges if e["node"]["name"] == name]
        if len(matches) != 1:
            raise RuntimeError("프로젝트 이름을 정확히 한 개로 확인할 수 없습니다.")
        return matches[0]

    def trace(self, project, trace):
        node = self.query(TRACE_QUERY, {"project": project, "trace": trace}).get("node")
        if not node or node.get("id") != project or "trace" not in node:
            raise RuntimeError("프로젝트 범위의 trace 조회를 확인할 수 없습니다.")
        return node["trace"]

    def delete(self, relay_id):
        # OTel trace_id를 Relay ID로 임의 변환하지 않고 Phoenix 조회 결과의 id만 사용
        data = self.query(DELETE_QUERY, {"ids": [relay_id]})
        if not isinstance(data.get("deleteTraces"), dict):
            raise RuntimeError("삭제 응답을 확인할 수 없습니다.")


    def list_traces(self, project, cutoff):
        """기간 하한/이벤트/오류 여부 필터 없이 과거 전체 페이지를 조회."""
        url = self.url.removesuffix("/graphql") + "/v1/projects/" + quote(project, safe="") + "/traces"
        cursor, seen = None, set()
        while True:
            params = {"end_time": cutoff.isoformat(), "limit": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            response = self.session.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
            if response.status_code != 200:
                raise RuntimeError(f"Phoenix trace 목록 HTTP {response.status_code}: 운영 API 지원 여부 확인. 삭제 없음.")
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), list) or "next_cursor" not in body:
                raise RuntimeError("Phoenix trace 목록 응답 형식이 다릅니다.")
            for row in body["data"]:
                if not isinstance(row, dict) or row.get("project_id") != project:
                    raise RuntimeError("목록의 프로젝트가 요청 범위와 다릅니다.")
                yield {"id": row["id"], "traceId": row["trace_id"],
                       "startTime": row["start_time"], "endTime": row["end_time"]}
            cursor = body["next_cursor"]
            if cursor is None:
                return
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise RuntimeError("Phoenix 페이지 커서가 진행되지 않습니다.")
            seen.add(cursor)


def trace_is_safe(trace, cutoff):
    if not isinstance(trace, dict) or not trace.get("id") or not trace.get("traceId"):
        raise ValueError("trace 식별자가 없어 중단합니다.")
    start, end = timestamp(trace["startTime"]), timestamp(trace["endTime"])
    if end < start:
        raise ValueError("trace 시작/종료 시각이 올바르지 않습니다.")
    return start < cutoff and end < cutoff


def execution_guard():
    if not all(v is True for v in (
        ARCHIVE_BACKUP_AND_REFRESH_PROTECTION_CONFIRMED,
        FULL_TRACE_LOSS_ACCEPTED, INGESTION_FOR_TARGET_TRACES_STOPPED,
    )):
        raise ValueError("실제 삭제 전 백업/재수집 차단/전체 정보 소실/추가 span 유입 중단을 확인하세요.")


def run(phoenix, cutoff, confirm=input):
    if not DRY_RUN:
        execution_guard()
    phoenix.check_delete_support()
    projects = {p: phoenix.project(p) for p in PROJECTS}
    if len(set(projects.values())) != len(projects):
        raise ValueError("여러 설정이 같은 프로젝트를 가리킵니다. 중복을 제거하세요.")
    plan, seen, inspected, preserved = [], set(), 0, 0
    # 모든 프로젝트의 조회를 완료해야 삭제 단계로 진입 (페이지 조회 중 삭제하지 않음)
    for name, project in projects.items():
        for trace in phoenix.list_traces(project, cutoff):
            inspected += 1
            if inspected > MAX_TRACES:
                raise ValueError("검사 상한 MAX_TRACES 초과. 상한 조정 후 다시 미리보기하세요. 삭제 없음.")
            key = (project, trace["id"])
            if key in seen:
                raise RuntimeError("목록에 중복 trace가 있습니다. 삭제하지 않고 중단합니다.")
            seen.add(key)
            if not trace_is_safe(trace, cutoff):
                preserved += 1
                continue
            plan.append({"project": name, "project_id": project, "phoenix": trace, "status": "planned"})
    print(f"보존 시작: {cutoff.isoformat()} / 삭제 후보 {len(plan)}건 / 최근 구간 걸친 trace 보존 {preserved}건")
    print(f"프로젝트: {', '.join(PROJECTS)} / MongoDB 연결 없음 / DRY_RUN={DRY_RUN}")
    output = Path(__file__).resolve().parent / "reports"
    output.mkdir(exist_ok=True)
    path = output / ("phoenix_cleanup_" + datetime.now(KST).strftime("%Y%m%d_%H%M%S_%f") + ".json")
    report = {"dry_run": DRY_RUN, "cutoff_exclusive": cutoff.isoformat(),
              "scope": "all_traces_in_selected_projects", "preserved_crossing_cutoff": preserved,
              "traces": plan}
    def save():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    print(f"대상/결과 기록: {path}")
    if DRY_RUN or not plan:
        return report
    phrase = f"DELETE PHOENIX {len(plan)}"
    if confirm(f"미보관 이력까지 삭제합니다. '{phrase}' 입력: ").strip() != phrase:
        print("취소했습니다. 삭제 요청 없음.")
        return report
    for item in plan:
        try:
            planned = item["phoenix"]
            current = phoenix.trace(item["project_id"], planned["traceId"])
            if current != planned or not trace_is_safe(current, cutoff):
                raise RuntimeError("trace가 조회 후 변경되었습니다. 중단합니다.")
            item["status"] = "delete_requested"
            save()
            phoenix.delete(current["id"])
            if phoenix.trace(item["project_id"], current["traceId"]) is not None:
                raise RuntimeError("삭제 후 trace가 남아 있습니다. 중단합니다.")
            item["status"] = "deleted_verified"
            save()
        except Exception:
            item["needs_review"] = True
            save()
            raise  # timeout 시 이미 삭제되었을 수 있으므로 자동 재시도하지 않음
    print(f"Phoenix {len(plan)}건 삭제 확인 완료. MongoDB에는 연결하지 않았습니다.")
    return report


def main():
    try:
        validate_config()
        phoenix = Phoenix(PHOENIX_ENDPOINT, PHOENIX_API_KEY)
        try:
            run(phoenix, retention_cutoff())
        finally:
            phoenix.session.close()
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"중단: {exc}")
    except Exception as exc:
        print(f"중단 ({type(exc).__name__}): API/연결 상태 및 reports 기록 확인. 자동 재시도 없음.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
