# GAIA 답변이 CUBE Rich Notification으로 바뀌는 방식

이 문서는 callback 서버가 GAIA/Langflow의 최종 답변을 CUBE 화면용 Rich Notification으로 바꾸는 규칙을 설명합니다. 실제 GAIA나 CUBE에는 호출하지 않는 미리보기 도구도 함께 제공합니다.

## 전체 흐름

```text
GAIA 응답 JSON
  └─ outputs의 마지막 Chat Output
       └─ results.gaia_response.data.answer
            └─ render_gaia_answer_to_cube_body()
                 └─ CUBE richnotification.content[0].body
```

`extract_final_answer()`는 우선 `results.gaia_response.data.answer`를 읽습니다. 이 값이 없을 때만 `results.message.data.text`를 대체값으로 사용합니다. 실제 Markdown 변환 코드는 같은 폴더의 `markdown_rich_notification.py`에 있고, `app.py`는 그 결과 body를 기존 CUBE 발송 payload에 끼워 넣기만 합니다.

## 변환 규칙

| GAIA answer 내용 | CUBE body 결과 |
| --- | --- |
| 일반 문장/문단 | 연속된 일반 줄을 줄바꿈을 유지한 하나의 `label` 행 |
| `### 제목` | `#` 기호를 뺀 일반 `label` 텍스트 |
| `- 항목` 또는 `1. 항목` | 원래 목록 표기를 유지한 `label` 텍스트 |
| 완전한 Markdown 표 (`헤더` 다음에 `---` 구분 행, 그 뒤 본문 1행 이상) | `bodystyle: "grid"`, 회색 헤더와 문자 길이 기반 열 폭을 가진 여러 `label` 열 |
| `![설명](https://...)` | 독립된 `image` 행 (`sourceurl`, 표시 폭 `70%`) |
| `📥 [표시 문구](https://...)` | `📥`를 보존한 클릭 가능한 `hypertext` |
| `🔗 <a href="https://...">표시 문구</a>` | `🔗`를 보존한 클릭 가능한 `hypertext` |
| `<p>문단</p>`, `<div>문단</div>`, `<br>` | 보이는 문장과 줄바꿈을 보존한 `label` 텍스트 |
| `javascript:`, `data:`, 공백 URL, 사용자정보 포함 URL | 링크로 만들지 않고 일반 `label` |
| 알 수 없는 HTML/Markdown | 사람이 읽을 수 있는 일반 `label` |

표를 판단할 때는 파이프(`|`)뿐 아니라 바로 다음 줄의 표 구분 행도 확인합니다. 그래서 우연히 파이프가 들어간 일반 문장을 표로 오인하지 않습니다.

이 구현은 전달받은 운영 서버의 `parser.py`/`builder.py` 구조를 따릅니다. 즉, Markdown을 직접 CUBE의 `body.row` 배열로 바꾸고, 표의 각 열 폭도 내용 길이에 따라 계산합니다. 단, 기존에 확인한 다운로드/상세화면 링크가 사라지지 않도록 안전한 HTTP(S) 링크는 `hypertext`로 유지하는 호환 확장을 추가했습니다.

다운로드/상세 화면처럼 이모지를 표시하려면 Markdown 목록 기호(`-`) 대신 이모지를 링크 바로 앞에 둡니다. 예를 들면 `📥 [CSV 다운로드](https://...)`는 CUBE에서 `📥 CSV 다운로드`라는 클릭 가능한 링크가 됩니다.

## 내 PC에서 변환 결과 보기

PowerShell에서 아래처럼 실행합니다. 이 명령은 외부 API를 호출하지 않으며, 키·토큰도 사용하지 않습니다.

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\gaia_cube_server\production_callback_server
python rich_notification_preview.py
```

실행 후 아래 두 파일이 생깁니다.

- `preview_output\gaia_to_cube_rich_notification_preview.html`: 브라우저로 열어 실제 화면에 가까운 결과를 봅니다.
- `preview_output\gaia_to_cube_rich_notification_preview.json`: CUBE 발송 payload에 들어갈 `body` JSON을 그대로 봅니다.

미리보기에는 다음 두 사례가 들어 있습니다.

1. 데이터셋 목록: 제목, 불릿, Markdown 표
2. 보고서/다운로드: Markdown 링크와 HTML 링크
3. 생산 추이: Markdown 이미지

## 이전 방식과 현재 방식의 화면 비교

같은 Markdown 입력이 이전 변환 규칙과 현재 운영 parser 규칙에서 어떻게 달라지는지 한 화면에서 보려면 아래 명령을 실행합니다.

```powershell
python markdown_renderer_comparison.py
```

`preview_output\markdown_renderer_comparison.html`이 생성됩니다. 왼쪽은 `markdown_legacy_rich_notification.py`의 이전 방식, 오른쪽은 `markdown_rich_notification.py`의 현재 방식이 실제로 생성한 CUBE `body`입니다. 이 도구는 GAIA와 CUBE API를 호출하지 않습니다. 두 CASE를 callback 서버에서 각각 실행하는 방법은 [RENDERER_CASES_GUIDE.md](RENDERER_CASES_GUIDE.md)를 따릅니다.

## 실제 callback 발송에서는 무엇이 달라지나

실제 발송 때도 변환 규칙은 같습니다. 단, 미리보기의 `cube_body`는 `richnotification.content[0].body` 부분만 보여 줍니다. 실제 API 전송에는 그 바깥에 봇 정보, 수신자 정보, 그리고 CUBE가 요구하는 비어 있지 않은 `process` 객체가 함께 붙습니다.

```text
build_cube_rich_notification(...)
  ├─ header: 봇 ID, 토큰, 표시 이름, 수신 사번/채널
  └─ content[0]
       ├─ body: 이 문서에서 설명한 변환 결과
       └─ process: CUBE 전송에 필요한 callback/session/requestid 정보
```

## 확인한 범위와 아직 확인하지 않은 범위

자동 테스트는 GAIA 응답 추출 → Rich body 변환 → CUBE outbound payload 조립까지 검증합니다. 하지만 실제 CUBE 클라이언트의 화면은 CUBE 개발 채널에서 한 번 발송해 확인해야 합니다. 특히 표의 폭, 긴 텍스트 줄바꿈, 링크의 모바일 동작은 CUBE 앱 버전에 따라 화면 차이가 있을 수 있습니다.
