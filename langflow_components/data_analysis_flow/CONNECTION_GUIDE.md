# Data Analysis 공통 컴포넌트 연결 가이드

이 폴더는 Flow 01 `v5_data_analysis`와 Flow 08 `v5_data_analysis_continuation`이 함께 사용하는 조회·저장·세션·결과 처리 컴포넌트입니다. 새 구현은 Flow 01의 V2 JSON을 기준으로 합니다.

## 현재 사용 Flow

| Flow | 용도 |
| --- | --- |
| `01. v5_data_analysis` | 일반 메타데이터 기반 데이터 조회와 분석 |
| `08. v5_data_analysis_continuation` | 첫 조회 결과를 안전하게 참조하는 최대 2단계 연계 분석 |

## 공통 처리 순서

```text
질문/세션 로드
→ MongoDB 메타데이터 로드 및 후보 축소
→ 의도 계약 정규화 및 신뢰 카탈로그 보정
→ 조회 작업 검증·실행·소스 병합
→ Fast 또는 Complex 분석 실행
→ 결과/중간 산출물 저장
→ 결정론적 또는 선택적 LLM 답변 생성
→ Chat Output 및 API 응답
```

## 안전성 기준

- Table Catalog를 읽지 못했거나 활성 데이터셋이 없으면 LLM이 데이터셋·컬럼을 추측하지 않고 연결 또는 등록 오류를 반환합니다.
- 물리 컬럼은 실제 조회된 schema에서만 바인딩합니다. 결과 계약을 만족하지 못하면 pandas 실행 전에 차단합니다.
- 정상 완료 시에는 최종 결과와 다른 경우에만 `최종 집계 전 중간 데이터`를 표시합니다. 오류 시에는 오류 직전 마지막 정상 중간 데이터를 표시합니다.
- 중간 데이터, 원본 데이터, 최종 결과는 Result Store에 저장되어 CSV 링크로 내려받을 수 있습니다.
- helper 실행 상세와 다음 질문 후보는 답변 본문에 출력하지 않습니다. 필요한 후속 질문 후보는 응답 metadata로만 전달합니다.

## 운영 설정

MongoDB URI·database·collection·조회 모드·다운로드 base URL·중간 데이터 최대 표시 행 수는 각 Custom Component 입력에서 설정합니다. 비밀 값은 Flow JSON에 저장하지 않습니다.

다운로드와 실시간 Report HTML 서비스는 다음 명령으로 시작합니다.

```powershell
python -m artifact_server
```

자세한 Flow 목록과 서버 설정은 [현재 Flow 및 Runtime 가이드](../../docs/ACTIVE_FLOWS_AND_RUNTIME.md)를 참고합니다.
