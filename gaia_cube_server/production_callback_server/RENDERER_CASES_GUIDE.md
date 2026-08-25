# Markdown → CUBE Rich Notification 두 가지 실행 CASE

GAIA의 최종 `answer`를 CUBE Rich Notification `body`로 바꾸는 방식만 두 가지로 분리했습니다. GAIA 호출, 세션 ID 재사용, callback 수신 경로, CUBE header/process, fallback 발송은 두 CASE에서 같습니다.

환경변수로 변환기를 고르지 않습니다. 실행할 Python 파일을 하나만 선택하면 됩니다.

| CASE | 실행 파일 | 변환 코드 | 표시 특징 |
| --- | --- | --- | --- |
| CASE 1: 기존 방식 | `app_case_legacy.py` | `markdown_legacy_rich_notification.py` | 문장별 행, 색상 안내 행, 균등 표 폭, 이미지는 설명 문구 |
| CASE 2: 운영 parser 방식 | `app_case_production.py` | `markdown_rich_notification.py` | 일반 문장 묶음, 내용 길이 기반 표 폭, CUBE `image` 행 |

기존 `app.py`도 CASE 2와 동일한 현재 운영 parser를 기본으로 사용합니다. 두 CASE는 비교·선택용으로 따로 둔 파일이므로, 같은 서버에서 동시에 실행하지 않습니다. 둘 다 포트 `5000`을 사용합니다.

## 실행

프로젝트 폴더에서 하나만 실행합니다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\gaia_cube_server\production_callback_server

# CASE 1: 이전 방식
python app_case_legacy.py

# 또는 CASE 2: 현재 운영 parser 방식
python app_case_production.py
```

두 실행 파일 모두 아래 고정 진입점을 그대로 사용합니다.

```python
if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
```

따라서 CUBE callback 경로는 두 CASE 모두 동일하게 `/api/v1/receiver`입니다. HCP에서 평가할 때는 선택한 CASE 파일을 시작 파일로 지정하고, `app.py`, 선택한 app 파일, 그리고 두 `markdown_*.py` 파일을 함께 배포합니다.

## CUBE로 실제 전송되는 값

두 CASE에서 달라지는 값은 아래 한 부분뿐입니다.

```text
richnotification.content[0].body
```

아래 값은 바뀌지 않습니다.

- `header.from`, `token`, `fromusername`, `to`
- 비어 있지 않은 `content[0].process`
- `requestid: ["request_cond_change_main"]`
- CUBE callback ACK (`200` + JSON `null`)
- 사용자 + 채널 기준 GAIA 세션 ID 재사용
- GAIA 오류 시 CUBE fallback 발송

## 실제 CUBE 발송 전 화면 비교

아래 도구는 외부 API를 호출하지 않습니다.

```powershell
python markdown_renderer_comparison.py
```

생성되는 `preview_output\markdown_renderer_comparison.html`에서 왼쪽은 CASE 1, 오른쪽은 CASE 2입니다. 이제 비교 화면도 각 CASE의 실제 변환 함수를 직접 사용합니다.
