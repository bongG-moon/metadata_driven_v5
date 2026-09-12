# 사용 이력 표시와 Phoenix 장애 시 동작

`PTMORE_USAGE_HISTORY_ARCHIVE_MODE = "configured"`로 설정한 경우 화면은 먼저 `/api/dashboard/usage?cache_only=true`로 MongoDB 보관 이력을 읽습니다. 이 요청은 Phoenix 설정이나 인증키를 요구하지 않습니다. 이후 일반 사용 이력 API로 당일·누락 날짜 갱신을 시도합니다.

Phoenix 설정 누락이나 통신 실패 시 일반 API도 HTTP 200과 `source.status = "cached"`를 반환하며 MongoDB 보관 이력을 표시합니다. `warning_code`와 안내 문구로 갱신 실패를 구분합니다. 정상 갱신 완료 시에는 기존처럼 `connected` 상태와 갱신된 MongoDB 이력을 반환합니다.

Phoenix 조회 실패는 빈 조회 결과로 저장하지 않습니다. 관리자 전체 갱신 실패도 완료로 표시하지 않습니다. MongoDB 자체를 읽을 수 없으면 오류를 반환하며, 브라우저가 이미 읽어 둔 보관 이력이 있다면 해당 화면을 유지합니다.

보관 이력은 기존 사용 이력 문서 규격과 최근 21일 범위에 맞아야 합니다. 사번·이름만 들어 있는 직원 목록은 사용 이력이 아닙니다. 보관 데이터가 없으면 빈 이력으로 표시하며 테스트 데이터를 자동으로 생성하지 않습니다.

이 변경은 Flask Portal의 사용 이력 조회와 표시만 변경합니다. Scheduler Worker, CUBE 발송 API, 스케줄 실행 문서 규격은 변경하지 않습니다.
