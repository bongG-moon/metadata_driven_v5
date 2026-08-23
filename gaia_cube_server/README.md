# GAIA-CUBE Server

이 디렉터리는 CUBE 사용자의 질문을 GAIA에서 실행되는 Langflow Agent로 전달하고, 최종 Agent 답변을 원래 CUBE 사용자와 채널에 다시 발송하는 FastAPI 서버를 구현하기 위한 작업 공간이다.

## 현재 상태

- 사용자 제공 GAIA/CUBE API 가이드를 `base_guide/`에 정리했다.
- 현재 구현 기준은 사용자 제공 API 예시와 `BEGINNER_GUIDE.md`의 기본 동기 연동 흐름이다.
- 외부 API를 호출하지 않는 더미 callback 서버와, `.env` 설정으로 실행하는 운영용 callback 서버를 구현했다.

## 실행 가능한 기본 서버

- [`dummy_callback_server/`](dummy_callback_server/): GAIA·CUBE를 실제 호출하지 않고 callback → session → 답변 추출 → CUBE payload 생성을 로컬에서 검증한다. 처음 실행하는 경우 [단계별 실행 가이드](dummy_callback_server/DUMMY_SERVER_RUN_GUIDE.md)를 사용한다.
- [`production_callback_server/`](production_callback_server/): GAIA 인증 키, Agent `svc_id`, CUBE 봇 ID·토큰과 발송 URL을 `.env`에 입력하면 실제 기본 동기 흐름을 실행한다. 실제 CUBE 연동 전에는 [상세 실행 가이드](production_callback_server/PRODUCTION_SERVER_RUN_GUIDE.md)를 따른다.

각 폴더의 `README.md`에 설치, 설정, 실행 방법을 적었다. 현재 범위에는 MongoDB, worker, outbox, 재시도 큐와 스케줄러가 포함되지 않는다.

## 현재 구현 기준 문서

- [`BEGINNER_GUIDE.md`](BEGINNER_GUIDE.md): 제공 API 예시와 현재 합의된 기본 연동 설명
- [`base_guide/README.md`](base_guide/README.md): 사용자 제공 가이드 인덱스
- [`base_guide/gaia/`](base_guide/gaia/): GAIA 호출 및 최종 응답 추출 계약
- [`base_guide/cube/`](base_guide/cube/): CUBE callback, 발송, Rich Message와 fallback 계약

## 현재 합의된 기본 원칙

1. CUBE 채널 하나는 서버 설정으로 하나의 GAIA Agent에 연결한다.
2. 사용자·CUBE 채널/thread으로 GAIA `session_id`를 구분한다.
3. 질문은 CUBE callback으로 받고, 최종 답변은 CUBE Rich Notification API로 보낸다.
4. 실제 API 정보를 받기 전에는 더미 연동으로 기본 흐름을 검증할 수 있다.

## 보류된 향후 설계 메모

아래 문서는 현재 구현 범위가 아니다. 저장소, worker, 재시도, 운영 복구처럼 나중에 필요해질 항목을 검토할 때만 다시 사용한다.

- [`IMPLEMENTATION_BLUEPRINT.md`](IMPLEMENTATION_BLUEPRINT.md): 향후 운영 구조 검토 메모
- [`base_guide/common/`](base_guide/common/): 향후 세션·최근 대화 운영 계약 메모
