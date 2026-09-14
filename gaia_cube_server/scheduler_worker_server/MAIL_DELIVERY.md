# 사번별 CUBE / 메일 발송

최신 Worker를 먼저 배포한 뒤 Portal을 배포하고 메일을 활성화하세요. 이전 Worker는 채널 옵션을 모르므로 메일 전용 문서에도 CUBE를 보낼 수 있습니다. 기존 문서는 옵션이 없으면 CUBE 전용으로 해석합니다. MongoDB 일괄 변경은 필요 없습니다.

## Worker 설정

`.env.example`의 다음 항목을 Worker의 `.env` 또는 운영 환경변수에 설정합니다. Portal에는 새 환경변수가 필요하지 않습니다. 기존 GAIA, CUBE, MongoDB 설정은 유지합니다.

- `PTMORE_EMPLOYEE_HAPI_URL`: 제공된 사내 직원 조회 URL
- `PTMORE_EMPLOYEE_HAPI_TOKEN`: 운영 토큰 (소스나 Git에 넣지 마세요)
- `PTMORE_EMPLOYEE_HAPI_NAME_FIELD`: 이름 컬럼, 기본 `EMP_NM`. 실제 응답 컬럼에 맞춰 설정
- `PTMORE_SMTP_HOST`: `exhub.skhynix.com`
- `PTMORE_SMTP_PORT`: `25`

H-API는 `h-api-token` 헤더와 `{"bindParams":["대상자사번,등록자사번"]}` 본문으로 호출합니다. 응답은 `EMPNO`, `EMAIL`을 가진 행 목록이어야 합니다. 이름이 없으면 이메일만 사용합니다. SMTP는 제공된 사내 릴레이 방식으로 연결하며 Outlook 프로그램이나 SMTP 로그인을 사용하지 않습니다. 발신인은 `ptmorepkg.bot@sk.com`으로 고정입니다.

## 실행 및 이력

공통 사번 목록의 각 문서마다 GAIA를 한 번 호출하고 활성 채널에 같은 답변을 전달합니다. 메일은 한 대상자에게 보내고 등록자 이메일을 CC에 넣습니다. 10명이면 메일도 10건이며 등록자는 각각 CC를 받습니다. 본인에게 발송할 때 SMTP 수신 주소는 중복 제거합니다.

`portal_schedule_runs`의 `owner_id`는 실행자, `registrant_id`는 등록자, `group_id`는 그룹입니다. `channel_delivery`에 CUBE/메일별 성공, 미사용, 실패가 저장됩니다. 일부 채널만 성공하면 `delivery_status=partial_delivery`로 기록합니다. 메일 실패 때문에 성공한 CUBE를 다시 보내거나 GAIA를 재호출하지 않습니다.

대상자 또는 등록자의 이메일 조회가 실패하면 메일을 발송하지 않고 오류를 기록합니다. SMTP 시간 초과는 실제 수신 여부가 불확실할 수 있으므로 자동 재발송하지 않습니다. 실행 도중 그룹 변경 시 발송 직전 claim을 다시 확인하지만 이미 SMTP 서버에 전달된 메일은 회수할 수 없습니다.

운영에서는 H-API와 SMTP에 대한 네트워크 접근 및 릴레이 허용을 확인해야 합니다. 로컬 검증은 모의 HTTP/SMTP로 수행했으며 실제 메일을 보내지 않았습니다.

## 메일 표시 형식

`mail_renderer.py`가 원본 답변의 Markdown 제목, 목록, 강조, 코드 블록, 파이프 표와 HTTP(S) 링크를 메일 HTML로 변환합니다. 보고서 링크는 원래 본문 위치에만 표시하며 하단의 관련 링크 버튼은 추가하지 않습니다. Flow가 `<a href="..."><strong>링크 이름</strong></a>` 형식으로 반환한 경우에도 주소와 표시 문구만 추출해 안전한 하이퍼링크로 재구성합니다. 원본의 이벤트·스타일 속성은 전달하지 않으며 HTTP(S) 이외 주소는 클릭할 수 없는 문구로 표시합니다. 링크에 붙은 나침반·시계·다운로드 이모지는 Outlook 글꼴 깨짐을 방지하기 위해 제거합니다. Outlook용 표 레이아웃과 인라인 스타일을 사용하며 외부 이미지, 스크립트, 추가 패키지는 필요 없습니다. 그 외 HTML 및 코드 예시의 HTML은 안전하게 문자로 표시합니다.

원본 답변은 MIME의 일반 텍스트 대체 본문에도 보존합니다. 이미 한 줄로 합쳐진 답변은 임의로 문장이나 표를 추측해 재구성하지 않습니다. GAIA 원본의 줄바꿈을 유지해야 제목·목록·표가 정확히 구분됩니다. 실제 Outlook 버전과 보안 설정에 따른 최종 표시 확인이 필요합니다. Worker 배포 시 `mail_renderer.py`도 반드시 함께 복사하세요.
