# CUBE API Guide

사용자가 제공하는 CUBE 메시지 수신, 요청자 식별, 채널 식별 및 메시지 발송 API 가이드를 기능별로 나누어 저장한다.

## 문서 목록

- `source/2026-08-22_cube_development_guide_raw.txt`: 사용자가 제공한 원문 보존본
- `source/2026-08-22_fastapi_callback_fallback_sanitized.txt`: FastAPI/상호작용 예시에서 개인정보를 치환한 원문형 저장본
- `01_message_send_api.md`: CUBE Rich Notification 메시지 발송 계약. 2026-08-24에 확인된 일반 답변용 필수 `process` 구조를 포함한다.
- `02_callback_api.md`: CUBE 메시지 callback 수신 및 ACK 계약
- `03_fastapi_rich_message_and_fallback.md`: FastAPI callback, Rich Message 상호작용 및 fallback 계약
- `04_callback_registration_and_network.md`: 현재 등록한 HCP callback URL과 네트워크 연결 기준
- `examples/send_message.request.json`: 메시지 발송 요청 예시
- `examples/callback.request.json`: callback 요청 예시
- `examples/callback.success.response.json`: callback 성공 응답 예시
- `examples/rich_message.request.json`: 이미지·라디오·버튼 Rich Message 발송 예시
- `examples/callback.hello.request.json`: CUBE 최초 진입 sentinel callback 예시
- `examples/callback.selection.request.json`: 사용자의 라디오/버튼 선택 callback 예시
- `examples/fallback_message.request.json`: 전체 Rich Notification schema를 사용하는 사용자 오류 메시지 예시

## 정리 원칙

- 원문의 들여쓰기가 사라진 상태임을 전제로 JSON과 Python 블록의 구조를 복원한다.
- 원문 필드명과 예시 값은 임의로 바꾸지 않는다.
- 명백한 Python 표기 오류는 정리 문서에서 바로잡되 원문 보존본은 수정하지 않는다.
- 예시만으로 확인되지 않는 인증, 재시도, callback timeout, message ID 및 오류 응답 정책은 확정 계약으로 간주하지 않는다.

## 수신 상태

메시지 발송, FastAPI callback, Rich Message 상호작용 및 오류 fallback 예시를 수신했다. 일반 텍스트 발송에는 `content[0].process`를 빈 객체로 보내지 않고, `request_cond_change_main`을 포함한 확인된 고정 구조를 사용한다. 현재 프로젝트의 callback 수신 경로는 `POST /api/v1/receiver`이며, 등록한 HCP URL과 네트워크 기준은 `04_callback_registration_and_network.md`에 기록했다. callback 인증·멱등성·timeout 정책은 아직 미확정이다.
