# GAIA API Guide

사용자가 제공하는 GAIA 환경, Langflow 실행 요청, 상태 확인 및 응답 수신 API 가이드를 기능별로 나누어 저장한다.

## 문서 목록

- `01_external_langflow_api.md`: 외부 Agent/Langflow 실행 API 호출 계약
- `02_response_extraction.md`: 최종 Chat Output 선택 및 답변 추출 계약
- `examples/request.example.json`: 비밀값을 제거한 요청 예시
- `examples/success_response.minimal.json`: 제공받은 응답에서 필요한 구조만 남긴 유효 JSON 예시

## 수신 상태

현재 문서는 사용자가 제공한 실제 호출 코드와 응답 예시를 기준으로 작성했다. 운영 URL, 인증 헤더 이름과 본문 필드 이름은 제공된 표기를 유지한다. 아직 제공되지 않은 오류 응답 스키마, 비동기 처리 여부 및 제한 정책은 추정하지 않는다.
