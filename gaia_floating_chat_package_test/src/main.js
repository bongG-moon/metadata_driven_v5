import GaiaFloatingChat from "gaia-floating-chat";
import "gaia-floating-chat/style.css";
import "./page.css";

const config = {
  agentUrl: String(import.meta.env.VITE_GAIA_AGENT_URL || "").trim(),
  apiKey: String(import.meta.env.VITE_GAIA_API_KEY || "").trim(),
  userId: String(import.meta.env.VITE_GAIA_USER_ID || "").trim(),
  sessionId: String(import.meta.env.VITE_GAIA_SESSION_ID || "").trim(),
  position: String(import.meta.env.VITE_GAIA_FLOATING_POSITION || "right").trim(),
};

const status = document.querySelector("#status");
const missing = [
  ["VITE_GAIA_AGENT_URL", config.agentUrl],
  ["VITE_GAIA_API_KEY", config.apiKey],
  ["VITE_GAIA_USER_ID", config.userId],
].filter(([, value]) => !value).map(([name]) => name);

if (missing.length) {
  status.textContent = `.env에서 ${missing.join(", ")} 값을 입력한 후 개발 서버를 다시 시작해 주세요.`;
} else {
  const chat = new GaiaFloatingChat({
    // Vite proxy를 통해 /v2/... 요청이 GAIA_EXTERNAL_GATEWAY_ORIGIN으로 전달됩니다.
    agentUrl: config.agentUrl,
    apiKey: config.apiKey,
    userId: config.userId,
    sessionId: config.sessionId,
    position: config.position === "left" ? "left" : "right",
    onFeedback: (feedback) => console.log("GaiA Floating Chat feedback:", feedback),
  });

  // 제공 예시처럼 인스턴스 생성만으로 기본 floating UI를 표시합니다.
  // 브라우저 개발자 도구에서는 chat.mount(), open(), close(), toggle()도 시험할 수 있습니다.
  window.gaiaFloatingChat = chat;
  status.textContent = `연결 준비 완료 · 사용자 ${config.userId} · 세션 ${config.sessionId || "자동 생성"}`;
}
