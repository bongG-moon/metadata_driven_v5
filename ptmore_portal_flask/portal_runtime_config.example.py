"""PTMORE Flask Portal의 배포 전용 Python 설정 예시.

이 파일을 ``portal_runtime_config.py``로 복사한 뒤 실제 배포 폴더에서만
수정합니다. ``portal_runtime_config.py``는 Git에 올리지 않습니다.

문자열은 그대로 넣고, JSON 환경변수였던 목록/객체는 Python list/dict로
입력해도 됩니다. 앱이 기존 설정 형식의 JSON 문자열로 자동 변환합니다.
"""

# 로그인 방식입니다. 운영은 "sso", 임시 화면 확인만 "mock"을 사용합니다.
PTMORE_PORTAL_FLASK_AUTH_MODE = "sso"

# 초기 관리자 목록입니다. MongoDB에 관리자가 없을 때만 기본 권한을 부여합니다.
PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON = [
    {"employee_id": "2069026", "name": "문봉건"},
]

# Portal이 설정·스케줄·이력을 저장할 MongoDB 접속 주소입니다.
MONGODB_URI = "mongodb://ptmore_portal:CHANGE_ME@mongo.example.skhynix.com:27017/?authSource=admin"
# Portal이 사용할 MongoDB 데이터베이스 이름입니다.
MONGODB_DATABASE = "datagov_test"
# Portal 공통 설정과 관리자 기준값을 저장합니다.
PTMORE_PORTAL_SETTINGS_COLLECTION = "portal_settings"
# 관리자·권한·설정 변경 이력을 기록합니다.
PTMORE_PORTAL_AUDIT_COLLECTION = "portal_audit_log"
# 사용자가 등록한 스케줄 정보를 저장합니다.
PTMORE_SCHEDULE_COLLECTION = "portal_schedules"
# 스케줄 실행 결과와 실패 이력을 저장합니다.
PTMORE_SCHEDULE_RUN_COLLECTION = "portal_schedule_runs"
# Phoenix에서 수집한 사용 이력을 보관합니다.
PTMORE_USAGE_HISTORY_COLLECTION = "portal_usage_history"

# 메타데이터 등록을 외부 Flow API로 실행합니다.
PTMORE_METADATA_API_MODE = "api"
# API 키를 담아 보낼 HTTP 헤더 이름입니다. 예: x-api-key / X-Gaia-Auth-Key
PTMORE_METADATA_API_AUTH_HEADER = "x-api-key"
# 위 HTTP 헤더에 넣을 실제 API 키입니다.
PTMORE_METADATA_API_AUTH_KEY = "change-me"
# Flow API 응답을 기다리는 최대 시간(초)입니다.
PTMORE_METADATA_API_TIMEOUT_SECONDS = 300
# HTTPS 인증서를 검증할지 여부입니다. 운영 환경에서는 True를 권장합니다.
PTMORE_METADATA_API_VERIFY_TLS = True
# Flow 호출 본문 형식입니다. Langflow Run API를 쓰면 "langflow"를 사용합니다.
PTMORE_METADATA_API_PAYLOAD_MODE = "langflow"
# Langflow에 전달할 입력 메시지 타입입니다. 일반적으로 "chat"을 사용합니다.
PTMORE_METADATA_API_INPUT_TYPE = "chat"
# Langflow에서 구조화된 등록 결과를 받기 위한 출력 타입입니다. rev_2 Flow는 "any"를 사용합니다.
PTMORE_METADATA_API_OUTPUT_TYPE = "any"

# 데이터 카탈로그 등록 Flow의 API 주소입니다.
PTMORE_METADATA_TABLE_CATALOG_API_URL = ""
# 메인 플로우 필터 등록 Flow의 API 주소입니다.
PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL = ""
# 도메인 정보 등록 Flow의 API 주소입니다.
PTMORE_METADATA_DOMAIN_API_URL = ""

# 실제 MongoDB 메타데이터 목록 조회를 켜려면 "configured", 끄려면 "disabled"를 사용합니다.
PTMORE_METADATA_LIVE_READ_MODE = "configured"
# 목록 화면이 읽기 전용으로 조회할 도메인·카탈로그·필터 컬렉션 이름입니다.
PTMORE_METADATA_LIVE_COLLECTION_MAP_JSON = {
    "domain": "agent_v4_test_domain_items",
    "table_catalog": "agent_v4_test_table_catalog_items",
    "main_flow_filters": "agent_v4_test_main_flow_filters",
}

# 대시보드 사용 이력의 실시간 원본입니다. 실제 Phoenix 조회는 "phoenix"를 사용합니다.
PTMORE_USAGE_HISTORY_MODE = "phoenix"
# Phoenix 조회 결과를 MongoDB에 장기 보관하려면 "configured"를 사용합니다.
PTMORE_USAGE_HISTORY_ARCHIVE_MODE = "configured"
# Phoenix GraphQL 서비스의 기본 주소입니다.
PTMORE_PHOENIX_ENDPOINT = "http://gaia-phoenix.example.skhynix.com"
# Phoenix GraphQL 조회에 사용할 API 키입니다.
PTMORE_PHOENIX_API_KEY = "change-me"
# 조회할 Phoenix 프로젝트 이름 또는 프로젝트 ID 목록입니다.
PTMORE_PHOENIX_PROJECTS_JSON = ["my-gaia-project-a", "my-gaia-project-b"]
# Phoenix 요청 하나당 기다리는 최대 시간(초)입니다.
PTMORE_PHOENIX_TIMEOUT_SECONDS = 30
# Phoenix GraphQL 한 페이지에서 가져올 최대 Span 수입니다.
PTMORE_PHOENIX_PAGE_SIZE = 500
# 사용 이력 후보 Span을 찾기 위한 Phoenix 필터 조건입니다.
PTMORE_PHOENIX_FILTER_CONDITION = "span_kind == 'CHAIN'"
# 이 이름으로 시작하는 Span만 실제 Agent 질의로 집계합니다.
PTMORE_PHOENIX_SPAN_NAME_PREFIX = "GaiA Input"
