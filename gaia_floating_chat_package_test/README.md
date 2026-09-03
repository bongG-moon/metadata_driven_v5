# GaiA Floating Chat 공식 패키지 테스트

이 폴더는 사용자 제공 방식 그대로 공식 패키지를 임베딩하는 별도 테스트입니다.

```js
import GaiaFloatingChat from "gaia-floating-chat";
import "gaia-floating-chat/style.css";
```

따라서 버튼과 채팅창의 디자인은 이 프로젝트의 CSS가 아니라 `gaia-floating-chat/style.css`에서 제공하는 공식 디자인을 사용합니다. 이 프로젝트 CSS는 빈 테스트 페이지의 안내 카드에만 적용됩니다.

## 사전 조건

현재 공개 npm 레지스트리에는 `gaia-floating-chat` 패키지가 없습니다. AI Market 담당자가 제공한 사내 npm 레지스트리, 패키지 tarball, 또는 패키지 소스가 필요합니다.

- 사내 npm 레지스트리를 받은 경우: 담당자 안내에 따라 `.npmrc`를 설정한 뒤 `npm install`을 실행합니다.
- tarball 또는 소스 폴더를 받은 경우: 담당자에게 받은 설치 명령으로 `gaia-floating-chat`을 설치합니다. 설치 후 `node_modules/gaia-floating-chat/style.css`가 존재해야 합니다.

## 설정

```powershell
Copy-Item .env.example .env
notepad .env
```

기본 예시는 사용자가 제공한 Endpoint를 기준으로 합니다.

```dotenv
GAIA_EXTERNAL_GATEWAY_ORIGIN=http://gaia.api.skhynix.com
GAIA_EXTERNAL_GATEWAY_VERIFY_SSL=true
VITE_GAIA_AGENT_URL=/v2/agents/031011/external
VITE_GAIA_API_KEY=AI_Market_External_인증키
VITE_GAIA_USER_ID=2069026
VITE_GAIA_SESSION_ID=test_2069026
```

`VITE_`로 시작하는 값은 브라우저 JavaScript로 전달됩니다. 그러므로 이 방식은 **개발 PC의 로컬 연결 테스트 전용**입니다. 운영 Portal에는 External 인증키를 브라우저에 넣지 말고, 별도 서버 프록시 또는 플랫폼이 승인한 인증 구조를 사용해야 합니다.

이 구현은 공식 `GaiaFloatingChat` 패키지가 요청과 응답 형식을 처리하도록 맡깁니다. 따라서 이전 Python 테스트의 `input_value`·`tweaks["GaiA Input"]` 직접 호출과는 별도 테스트입니다.

## 실행

```powershell
cd C:\Users\qkekt\Desktop\metadata_driven_v5\gaia_floating_chat_package_test
npm install
npm run dev
```

브라우저에서 `http://127.0.0.1:8004`를 엽니다. Vite가 `/v2/*` 요청을 `GAIA_EXTERNAL_GATEWAY_ORIGIN`으로 전달하므로 CORS를 피할 수 있습니다.

개발자 도구 콘솔에서는 아래처럼 제어할 수 있습니다.

```js
window.gaiaFloatingChat.open()
window.gaiaFloatingChat.close()
window.gaiaFloatingChat.toggle()
window.gaiaFloatingChat.destroy()
```
