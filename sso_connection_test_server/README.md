# HCP SSO 연결 확인 서버

이 폴더는 `hcputil.auth.sso.SSO`가 실제 Flask 요청에서 동작하는지만 확인하는 독립 테스트 서버입니다. Flask 앱을 ASGI 래퍼로 감싸므로 Uvicorn 실행 방식은 유지됩니다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\sso_connection_test_server
python -m pip install -r requirements.txt
python app.py
```

운영 환경의 `blinker==1.4`를 유지하기 위해 `Flask==2.2.5`와 `Werkzeug==2.3.8`을 사용합니다. `Flask 2.3` 이상은 더 높은 Blinker 버전을 요구하므로 이 환경에서는 설치하지 않습니다.

HCP에 배포한 뒤 `/` 또는 `/whoami`에 접속합니다.

- SSO 성공: `employee_id`, `employee_name` JSON 반환
- SSO 미로그인: HCP 로그인 화면으로 리다이렉트
- HCP 모듈 또는 SSO 자체 실패: 안전한 오류 코드와 예외 유형 반환
  - `AttributeError`이면 `missing_attribute`도 함께 반환됩니다.
  - 쿠키·전체 예외 내용은 응답에 표시하지 않습니다.

이 서버는 Portal 세션, MongoDB, GAIA, CUBE를 사용하지 않으므로 `PTMORE_SSO_SESSION_SECRET`도 필요하지 않습니다. 다만 Flask는 Uvicorn이 직접 실행할 수 없으므로 `a2wsgi`가 Flask 앱을 ASGI 앱으로 변환합니다.
