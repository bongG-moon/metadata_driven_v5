# Prompt Cache 프롬프트 순서 A/B 검증

## 결론

`고정 규칙 -> 동적 입력 -> 사용자 질문`으로 재배치한 Flow는 그래프 구조와 입력 계약은 유지했지만, 현재 `gemini-3.5-flash-lite` 실측에서는 Prompt Cache 적중과 속도 개선이 확인되지 않았다. 일부 대표 질문의 실행 계획도 기준선과 달라졌으므로 운영 기본 Flow에는 적용하지 않고 실험용 After Flow로만 보존한다.

운영 `flow_exports/data_analysis_flow_v2_standalone.json`은 검증 전 프롬프트 순서로 복원했다.

## A/B Flow 정리

검증을 위해 생성했던 Before/After import용 Flow JSON과 패키징 manifest는 운영에 사용하지 않기로 결정하여 삭제했다. 운영 `flow_exports/data_analysis_flow_v2_standalone.json`만 유지한다.

프롬프트 snapshot과 속도 측정 결과는 기존 순서를 선택한 근거를 재확인할 수 있도록 검증 기록으로만 보존한다.

## 실제 Gemini 검증

- 모델: `gemini-3.5-flash-lite`
- 기존 대표질문 실행은 `temperature=0`을 요청했지만, 현재 모델에서는 해당 값이 무시된다.
- 기준일: `20260701`
- 대표 질문: 1, 5, 10, 13, 22, 25, 30
- 데이터 조건: 실제 MongoDB Metadata + 검증용 Dummy 조회 결과
- 실행: 원래 순서와 역순으로 각각 한 번, 총 14 case/layout
- 원문 프롬프트, 모델 응답, API 키는 결과 파일에 저장하지 않음

| 지표 | 기존 순서 | 재배치 순서 | 차이 |
| --- | ---: | ---: | ---: |
| Live 계약 통과 | 10/14 | 13/14 | 재배치 +3 |
| Intent TTFT 평균 | 972.160 ms | 1,019.647 ms | 재배치 +4.9% |
| Intent 전체 지연 평균 | 5,169.637 ms | 5,705.236 ms | 재배치 +10.4% |
| Answer 전체 지연 평균 | 1,812.963 ms | 1,791.077 ms | 재배치 -1.2% |
| 전체 모델 호출 | 22 | 30 | 재배치 +8 |
| Intent 입력 토큰 | 138,782 | 139,412 | 재배치 +0.5% |
| Cached content tokens | 0 | 0 | 적중 없음 |

속도 값은 각 실행에서 얻은 stage median 두 개의 평균이다. 재배치 버전은 제품 속성 비교와 UPH 질문의 Live 계약 성공률은 높였지만, 과거 날짜 WIP 비교 질문에서 데이터셋과 실행 경로가 실행마다 달라졌다. 전체 모델 호출 수도 늘어 wall time은 직접 비교하기 어렵다.

## 캐시·속도 통제 재검증

대표질문 전체 실행에 섞이는 검색·계획·출력 길이 차이를 제거하기 위해 Gemini 호출만 별도로 측정했다.

- sampling parameter 미전송
- 최대 출력 64 token
- 공통 warm-up 2회는 측정에서 제외
- 동일 완성 프롬프트 반복 6회/layout
- 고정 Metadata·state·schema에서 질문 suffix만 변경 6회/layout
- 호출 순서는 AB/BA로 교차

| 변경 질문 실험 | 기존 순서 | 재배치 순서 | 차이 |
| --- | ---: | ---: | ---: |
| 질문 변경 전 공통 접두부 | 174자 (1.2%) | 14,317자 (99.4%) | 재배치가 구조상 유리 |
| 첫 호출 제외 TTFT 중앙값 | 960.629 ms | 943.663 ms | 재배치 -1.8% |
| 첫 호출 제외 전체 지연 중앙값 | 1,171.120 ms | 1,169.148 ms | 재배치 -0.2% |
| 원시 `cachedContentTokenCount` 제공 | 0/6 | 0/6 | 양쪽 모두 미제공 |

완전히 동일한 프롬프트를 반복한 실험에서도 원시 `cachedContentTokenCount` 필드는 기존 0/6, 재배치 0/6으로 제공되지 않았다. 전체 24회 모두 해당 필드가 없었으므로 정규화된 0은 공급자가 명시한 cache miss가 아니다. 재배치로 공통 접두부는 충분히 길어졌지만, 현재 응답만으로 implicit cache 재사용 여부를 확인할 수 없고 0.2~1.8% 차이는 오차 수준이다.

대표질문 Flow도 정방향과 역방향으로 재실행했다. 두 실행에서 모두 동일한 계획·데이터셋·행 수를 유지한 질문 13, 30만 속도 표본으로 사용했다.

| 동일 실행 Flow 표본 4쌍 | 기존 순서 | 재배치 순서 | 차이 |
| --- | ---: | ---: | ---: |
| Intent TTFT 중앙값 | 1,065.034 ms | 1,230.605 ms | 재배치 +15.6% |
| Intent 전체 지연 중앙값 | 7,699.978 ms | 8,807.478 ms | 재배치 +14.4% |
| Flow wall time 중앙값 | 9,372.156 ms | 10,421.644 ms | 재배치 +11.2% |
| 보고된 Cached content tokens | 0 | 0 | 필드 미제공을 0으로 정규화 |

이 표본은 작아 일반적인 성능 열화를 확정하기에는 부족하지만, 적어도 현재 환경에서 재배치 버전이 더 빠르다는 일관된 근거는 없다. 공급자 cache read가 확인되지 않은 최초 호출 또는 cache miss에서는 프롬프트 순서만 옮겨도 처리해야 할 입력 token 수가 줄지 않는다.

## 결과 파일

- 정방향: `live_benchmark.json`
- 역방향: `live_benchmark_reverse_order.json`
- 동일 실행 대표질문 정방향: `stable_full_ab.json`
- 동일 실행 대표질문 역방향: `stable_full_ba.json`
- 캐시·속도 통제 실험: `cache_speed_probe_full.json`
- 재현 도구: `../../tools/benchmark_prompt_cache_ab.py`
- 캐시 통제 재현 도구: `../../tools/benchmark_prompt_cache_speed_probe.py`

## 최종 검증

- 삭제 전 A/B Flow 모두 Desktop Python 3.13.14, Langflow 1.11.0, langflow-base 0.11.0, LFX 1.11.0에서 41/41 node template parse 통과
- 운영 Flow 전체 대표 질문 결정론 검증 30/30 통과
- 보존한 cache benchmark/layout 관련 pytest 27개 통과
- 9개 export/import-ready/bundle source 동기화 오류 0
