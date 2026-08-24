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

`extract_final_answer()`는 우선 `results.gaia_response.data.answer`를 읽습니다. 이 값이 없을 때만 `results.message.data.text`를 대체값으로 사용합니다.

## 변환 규칙

| GAIA answer 내용 | CUBE body 결과 |
| --- | --- |
| 일반 문장/문단 | 한 줄당 `label` 행 |
| `### 제목` | `#` 기호를 뺀 파란색 `label` 행 |
| `- 항목` 또는 `1. 항목` | `• 항목` 또는 `1.`을 붙인 `label` 행 |
| 완전한 Markdown 표 (`헤더` 다음에 `---` 구분 행 포함) | `bodystyle: "Grid"`와 여러 `label` 열 |
| `📥 [표시 문구](https://...)` | `📥`를 보존한 클릭 가능한 `hypertext` |
| `🔗 <a href="https://...">표시 문구</a>` | `🔗`를 보존한 클릭 가능한 `hypertext` |
| `<p>문단</p>`, `<div>문단</div>`, `<br>` | 각 문단/줄을 별도의 `label` 행 |
| `주의: ...`, `경고: ...` | 주황색 안내 행 |
| `오류: ...`, `실패: ...` | 빨간색 안내 행 |
| `추가 조건 필요: ...`, `확인 필요: ...` | 파란색 확인 안내 행 |
| `javascript:`, `data:`, 공백 URL, 사용자정보 포함 URL | 링크로 만들지 않고 일반 `label` |
| 알 수 없는 HTML/Markdown | 사람이 읽을 수 있는 일반 `label` |

표를 판단할 때는 파이프(`|`)뿐 아니라 바로 다음 줄의 표 구분 행도 확인합니다. 그래서 우연히 파이프가 들어간 일반 문장을 표로 오인하지 않습니다.

다운로드/상세 화면처럼 이모지를 표시하려면 Markdown 목록 기호(`-`) 대신 이모지를 링크 바로 앞에 둡니다. 예를 들면 `📥 [CSV 다운로드](https://...)`는 CUBE에서 `📥 CSV 다운로드`라는 클릭 가능한 링크가 됩니다.

안내 색상은 문장 첫 부분이 명확히 `주의`, `경고`, `오류`, `실패`, `추가 조건 필요`, `확인 필요`처럼 시작할 때만 적용합니다. 그래서 `필수 조건이 있는 데이터셋은 1개입니다` 같은 일반 설명은 경고로 오인하지 않습니다.

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
