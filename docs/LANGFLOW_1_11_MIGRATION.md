# Langflow 1.11.0 전환 기준

이 문서는 현재 저장소의 기본 구현, Flow JSON 재생성, import-ready bundle 생성, 그리고 런타임 검증 기준입니다. 1.9.2 기준은 별도 레거시 브랜치에만 남기고, 이 브랜치에서는 1.11.0과 다른 minor 버전의 템플릿을 섞지 않습니다.

## 고정 런타임

- `langflow==1.11.0`
- `langflow-base==0.11.0`
- `lfx==1.11.0`
- 기본 검증: Langflow Desktop 1.11이 관리하는 Python 3.13

현재 PC의 Desktop 런타임을 사용할 때는 다음처럼 같은 인터프리터로 빌드와 검증을 실행합니다.

```powershell
$lf = "$env:LOCALAPPDATA\com.LangflowDesktop\.langflow-venv\Scripts\python.exe"
& $lf -m lfx --version
& $lf -c "import importlib.metadata as m; print(m.version('langflow'), m.version('langflow-base'), m.version('lfx'))"
```

별도 CI 또는 가상환경을 사용할 때도 세 패키지를 정확히 고정합니다. Langflow와 LFX는 같은 `1.11.x` minor 라인에서 호환되도록 문서화되어 있지만, 이 저장소의 export 재현성은 위의 정확한 patch 버전을 기준으로 합니다.

```powershell
uv venv .langflow-venv --python 3.13
uv pip install --python .langflow-venv\Scripts\python.exe `
  "langflow==1.11.0" "langflow-base==0.11.0" "lfx==1.11.0"
```

## 1.11에서 달라진 구현 기준

| 항목 | 1.11 변화 | 이 저장소의 적용 방식 |
| --- | --- | --- |
| Language Model | `model_name`, `provider` override 입력이 기본 Language Model schema에 포함됩니다. | 1.11 원본을 `tools/assets/langflow_1_11_0_language_model.py`에 고정하고 두 입력을 제거하지 않습니다. |
| Component 편집/API 노출 | 예전 Input Schema pane 대신 Component Parameters와 API 토글을 사용합니다. | Flow 입력 변경 시 template의 `api_editable`/입력 계약을 검증하며, 과거 Input Schema 화면을 전제로 한 안내를 추가하지 않습니다. |
| Workflow API V2 | 새 요청 형식은 `input_value`, `mode`, `stream_protocol`, `tweaks` 중심입니다. | 기존 `/api/v1/build` 기반 운영 호출은 유지합니다. 새 client 또는 AG-UI 경로를 만들 때만 V2 요청 계약으로 구현·테스트합니다. |
| 인증 기본값 | 기본 관리자 비밀번호와 auto-login 의존이 제거·축소되었습니다. | 서버 import smoke test는 `LANGFLOW_API_KEY`를 우선 사용하고, auto-login은 명시적으로 허용된 로컬 서버에서만 fallback으로 사용합니다. |
| Message metadata | `Message.data`에는 표시 `text`를 포함한 직렬화 mapping이 유지됩니다. | Tool 결과에 trace/route metadata를 붙일 때 `message.data = {...}`로 교체하지 않고 기존 mapping에 키를 추가합니다. |
| 확장 bundle | 일부 provider/component는 `lfx-bundles`의 opt-in bundle이 되었습니다. | headless 환경에서 사용하는 native component가 빠지면 `lfx[bundles]` 또는 필요한 `lfx-bundles[...]`를 설치하고, component index를 다시 고정합니다. |
| LFX 업그레이드 | `lfx upgrade`, `--write`, `--strict`로 built-in component 호환성을 점검합니다. | native component의 보류 업그레이드는 실패로 처리합니다. standalone custom component의 `BLOCKED` 표시는 LFX가 자동 migration을 알 수 없다는 뜻이므로 source/template parse 검증으로 별도 보장합니다. |
| OpenAI-compatible provider | OpenAI-compatible endpoint의 `/v1/models`를 읽어 모델을 동적으로 선택할 수 있습니다. | 현재 Gemini 기반 기본 모델 계약은 유지합니다. 사내 OpenAI-compatible endpoint를 채택할 때만 credential·모델 목록·fallback을 별도 검증합니다. |
| Data Operations | Text/JSON/Table Operations가 하나의 Data Operations 컴포넌트로 통합되었습니다. 기존 분리 컴포넌트를 쓰는 저장 Flow는 계속 동작합니다. | 현재 9개 Flow에는 해당 구형 컴포넌트가 없습니다. 새 데이터 변환 Flow는 통합 컴포넌트를 기준으로 설계합니다. |

## 새 기능의 채택 원칙

1. **Human-in-the-loop (HITL)와 Tool Approval**: 1.11에서 Agent 도구 승인을 checkpoint/resume 방식으로 지원합니다. 현재 분석 Flow는 읽기 전용 조회·분석이므로 기본 경로에는 넣지 않습니다. 향후 쓰기 도구에 도입할 때는 승인 대상, checkpoint 식별자, 재시도·만료 규칙을 별도 Flow 계약으로 정의합니다.
2. **A2A server/component**: 다른 Agent와 Agent Card 기반으로 통신할 수 있습니다. 현재 단일 Desktop/내부 Flow 라우팅을 A2A로 자동 교체하지 않으며, 외부 Agent 연동이 실제 요구될 때 인증·접근 제어·입출력 schema를 먼저 정합니다.
3. **AG-UI streaming Workflow API**: 새 UI 통합에 사용할 수 있습니다. 기존 Playground/Chat Output 전달 경로의 동작을 바꾸지 않고, 별도 client가 필요할 때만 V2 streaming 계약으로 검증합니다.
4. **Direct tool result 호환성**: 이 저장소의 Flow 06은 1.11 Desktop에서 `return_direct`가 Agent의 일반 Chat Output을 건너뛰는 상황을 방지하기 위해 `SilentDirectReturnRouterAgent`와 `AgentDirectToolResultAdapter`를 사용합니다. 이 두 노드는 결과를 보이는 최종 Message로 정규화하는 저장소 전용 호환 계층이며, HITL·A2A와는 별개입니다.

## Flow 재생성 및 검증

빌더는 반드시 1.11 Desktop LFX component index로 실행합니다. 다른 Python에서 실행해야 한다면 아래 환경변수로 1.11.0 index를 명시합니다.

```powershell
$lf = "$env:LOCALAPPDATA\com.LangflowDesktop\.langflow-venv\Scripts\python.exe"
$env:LANGFLOW_COMPONENT_INDEX_PATH = "$env:LOCALAPPDATA\com.LangflowDesktop\.langflow-venv\Lib\site-packages\lfx\_assets\component_index.json"

& $lf tools\build_v5_auxiliary_flows.py
& $lf tools\build_data_analysis_flow_v2.py
& $lf tools\build_import_ready_bundle.py
```

그 다음 아래 검증을 모두 통과해야 합니다.

```powershell
& $lf -m pytest tests/test_data_analysis_flow_v2.py tests/test_v5_flow_export.py -q --basetemp=.pytest-tmp
& $lf tools\validate_flow_component_sources.py
& $lf tools\validate_langflow_runtime.py --all-flows
```

마지막 명령은 다음을 함께 확인합니다.

- 실행 Python과 `langflow`, `langflow-base`, `lfx`의 정확한 1.11.0 조합
- 모든 9개 export의 `last_tested_version` 및 모든 직렬화 node의 `lf_version=1.11.0`
- 모든 standalone custom component의 source/template parse와 입출력 선언 동기화
- LFX native component upgrade 상태. `SAFE`/native `BLOCKED`는 실패이고, embedded standalone custom component의 `BLOCKED`는 별도 parser 검증 대상으로 기록합니다.

운영 Import 전에는 Flow JSON을 먼저 export해 보관합니다. Import 후에는 Provider credential, `MONGO_URL`, Router Tool의 대상 Flow를 다시 설정하고, Data Analysis 단일 질문과 Flow 06 direct tool 응답을 각각 smoke test합니다.

## 공식 참고 자료

- [Langflow 1.11 release notes](https://docs.langflow.org/release-notes)
- [LFX 1.11 compatibility and upgrade guide](https://docs.langflow.org/next/lfx-compatibility)
- [Workflow API and API input configuration](https://docs.langflow.org/concepts-publish)
- [Agent tools and approval behavior](https://docs.langflow.org/agents-tools)
