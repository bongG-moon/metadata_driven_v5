# Langflow Desktop Gemini Provider Bootstrap

새 Langflow Desktop에서 `No module named 'langchain_google_genai'`가 발생하면
[langflow_google_genai_bootstrap_component.py](../tools/langflow_google_genai_bootstrap_component.py)를 별도 bootstrap Flow의 Custom Component로 추가해 실행한다.

## 사용 순서

1. Langflow Desktop에서 빈 Flow를 만든다.
2. 위 Python 파일의 내용을 Custom Component로 추가한다.
3. `Install if missing=True`로 두고 컴포넌트를 한 번 실행한다.
4. 결과의 `interpreter`가 Desktop이 사용하는 Python인지 확인한다.
5. `status=installed`이면 Langflow Desktop을 완전히 재시작한다.
6. 1번 Data Analysis Flow를 다시 import하거나 열어 실행한다.

이 컴포넌트는 임의 shell 명령이나 임의 패키지명을 받지 않고
`langchain-google-genai`만 설치한다. 설치는 현재 프로세스의 `sys.executable -m pip`로 수행되므로
일반 시스템 Python에 설치한 패키지를 Desktop이 자동으로 사용한다고 가정하지 않는다.

Flow가 이미 Provider 노드를 포함한 상태에서 빌드 자체가 실패하는 경우가 있으므로,
이 컴포넌트는 기존 Data Analysis Flow 안에 삽입하지 말고 별도 bootstrap Flow에서 먼저 실행한다.
설치 후 프로세스를 재시작해야 Provider 노드가 새 모듈을 안정적으로 로드할 수 있다.

## API key

패키지 설치가 끝난 뒤에는 Desktop의 Google Gemini Credential 또는 `GOOGLE_API_KEY`
환경변수를 설정해야 한다. API key 오류는 패키지 import 오류와 별개의 다음 단계 문제다.
