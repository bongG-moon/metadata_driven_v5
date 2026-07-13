# Langflow 컴포넌트 한글 설명 및 UTF-8 검증 보고서

## 적용 목적

Langflow 캔버스의 노드 이름만 보는 운영자뿐 아니라 Python 소스를 처음 읽는 개발자도 각 컴포넌트의 역할과 연결 계약을 이해할 수 있도록 설명을 보강했다. 실행 로직은 변경하지 않고 다음 정보를 소스 가까이에 배치했다.

- 컴포넌트 역할
- 주요 입력과 필수 여부
- 주요 출력
- 처리 흐름
- 보안·토큰·세션·중복 저장 등 유지보수 포인트
- 모든 공개·비공개 함수
- 모든 클래스·async 메서드
- 모든 중첩 함수
- Langflow Output 포트가 실제로 호출하는 메서드

## 적용 범위

- `langflow_components` Python: 69개 전체
- 활성 Custom Component 원본: 68개
- pandas Function Case 지원 helper: 1개
- 컴포넌트 Markdown/Prompt 원본: 26개
- JSON에 내장되는 Prompt/System Prompt 원본: 9개
- 현재 v5 standalone Flow export: 7개
- import-ready 개별 Flow: 7개
- 전체 Flow 단일 import JSON: 1개
- manifest JSON: 1개
- import ZIP: 10개 entry

`data_analysis_flow_v4_reference.json`은 현재 v5 Flow를 만드는 donor이자 과거 기준본이므로 수정하지 않았다. 현재 실행·import 대상인 v5 JSON만 Python 원본과 다시 동기화했다.

## Python 주석 규칙

각 Python 파일 첫 부분은 다음 구조를 사용한다.

```python
# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 분석 요청 로더
# 역할: 질문과 이전 상태를 표준 데이터 분석 페이로드로 변환합니다.
# 주요 입력: 사용자 질문 (question) · 필수, 이전 상태 (previous_state)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 질문과 이전 상태를 읽고 세션 ID와 한국 기준일을 결정한 뒤 공통 분석 페이로드를 초기화합니다.
# 유지보수 포인트: inputs/outputs 이름은 Langflow JSON edge 계약입니다.
# =============================================================================
```

공개 업무 함수와 Langflow 출력 메서드에는 각각 `주요 함수`, `Langflow 출력 함수` 설명을 둔다. private helper와 중첩 함수에는 `함수 설명`을 바로 위에 둔다. 설명은 경로별 중요 함수 규칙, 반복 helper의 공통 규칙, 함수명 동작 패턴 순으로 결정하며 모호한 일반 fallback 문장은 남기지 않았다.

## JSON 반영 방식

표준 JSON은 `//`, `#`, `/* ... */` 형태의 구조 주석을 허용하지 않는다. JSON 객체 바깥에 주석을 직접 넣으면 Langflow 업로드 시 `Expected property name or '}'` 오류가 다시 발생한다.

따라서 다음 두 위치에 설명을 제공한다.

1. 노드의 `display_name`, `description`: Langflow 캔버스와 설정 화면에서 확인
2. Custom Component의 `template.code.value`: Python 원본의 한글 주석 전체를 문자열로 내장

빌더는 `encoding="utf-8"`, `ensure_ascii=False`를 사용한다. JSON 원문에서도 한글이 `\uXXXX`로 숨지 않고 보이며, Langflow 코드 편집기에서는 정상 Python 주석으로 복원된다. `metadata.code_hash`도 재계산했기 때문에 원본과 내장 코드가 일치한다.

## 재생성 순서

Python 컴포넌트를 수정한 뒤에는 다음 순서를 지켜야 한다.

```powershell
python tools\build_v5_data_analysis_flow.py

$lfxPython = 'C:\Users\qkekt\AppData\Local\com.LangflowDesktop\.langflow-venv\Scripts\python.exe'
& $lfxPython tools\build_v5_auxiliary_flows.py

python tools\build_import_ready_bundle.py
```

Data Analysis export가 나머지 Flow 빌더의 donor이므로 첫 번째 순서를 바꾸면 안 된다.

## 재발 방지 도구

- `tools/add_korean_component_comments.py`
  - 69개 소스의 공통 설명 형식을 생성한다.
  - 다시 실행해도 이미 설명된 파일을 중복 수정하지 않는다.
  - `--check`는 파일을 바꾸지 않고 설명 누락 여부만 검사한다.
  - `--refresh-functions`는 자동 설명 규칙을 개선한 뒤 기존 함수별 주석을 최신 문구로 다시 생성한다.
- `tools/validate_korean_component_documentation.py`
  - Python strict UTF-8, BOM, U+FFFD, NUL, Unicode NFC, AST를 검사한다.
  - private, async, decorator, 중첩 함수를 포함한 모든 `def`/`async def`의 인접 설명을 검사한다.
  - 현재 JSON 전체의 strict UTF-8과 JSON parse를 검사한다.
  - JSON 내장 Python 코드의 설명과 AST를 검사한다.
  - Function Case helper가 export·개별 import·통합 bundle에 원본 그대로 들어갔는지 확인한다.
  - ZIP의 10개 entry도 UTF-8과 JSON parse를 다시 검사한다.
- `.editorconfig`
  - 저장 인코딩을 UTF-8, 줄바꿈을 LF로 명시한다.

## 검증 결과

- 한글 설명 주석: Python 67/67
- 함수별 인접 설명: 1093/1093
- Python strict UTF-8 decode: 67/67
- UTF-8 BOM: 0건
- 깨짐 대체문자·NUL: 0건
- Python AST/compile: 67/67
- 컴포넌트 Markdown strict UTF-8/BOM 검사: 26/26
- 내장 Prompt 원본과 JSON 3계층 exact match: 9/9
- 현재 JSON strict UTF-8/parse: 16/16
- JSON 내장 Custom Component 코드 검사: 222건
- JSON 내장 함수별 설명 검사: 3,615/3,615
- Python 원본과 JSON source sync: export·개별 import·통합 bundle 각각 74/74
- 비활성 Python: 0개
- ZIP UTF-8/JSON 검사: 10/10
- pytest: 260/260
- 대표 dummy 질문: 23/23
- Langflow 1.8.2 / LFX 0.3.4 전체 node template: 114/114
- 격리 Langflow 1.8.2 HTTP import: 7/7 (`HTTP 201`)

한글 주석은 실행 코드와 포트 이름을 바꾸지 않으므로 Flow 동작에는 영향을 주지 않는다. 다만 JSON 파일 크기는 설명 문자열만큼 증가하며, 이는 사용자가 Langflow 코드 편집기에서 설명을 직접 확인하기 위한 의도된 변화다.
