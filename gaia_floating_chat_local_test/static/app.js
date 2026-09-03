const state = {
  config: null,
  sessionId: "",
  opened: false,
  sending: false,
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function createBrowserSession(userId) {
  const storageKey = `gaia-floating-chat-session:${userId}`;
  const existing = sessionStorage.getItem(storageKey);
  if (existing) return existing;
  const random = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const sessionId = `portal-floating-${userId}-${random}`.slice(0, 200);
  sessionStorage.setItem(storageKey, sessionId);
  return sessionId;
}

function updateConfigDisplay() {
  const config = state.config;
  if (!config?.configured) {
    const invalid = Array.isArray(config?.invalid_or_missing) ? config.invalid_or_missing.join(", ") : "설정";
    $("#config-user").textContent = "-";
    $("#config-session").textContent = "-";
    $("#config-status").textContent = `${invalid} 확인 필요`;
    $("#chat-user").textContent = "연결 설정 필요";
    $("#chat-session").textContent = "-";
    return;
  }
  $("#config-user").textContent = config.user_id;
  $("#config-session").textContent = state.sessionId;
  $("#config-status").textContent = "전송 준비 완료";
  $("#chat-user").textContent = `사번 ${config.user_id}`;
  $("#chat-session").textContent = state.sessionId;
}

function setOpen(opened) {
  state.opened = opened;
  $("#floating-chat").hidden = !opened;
  $("#floating-launcher").setAttribute("aria-expanded", String(opened));
  if (opened) $("#chat-input").focus();
}

function setSending(sending) {
  state.sending = sending;
  $("#chat-input").disabled = sending;
  $("#chat-submit").disabled = sending || !state.config?.configured;
  $("#chat-submit").textContent = sending ? "응답 받는 중…" : "전송";
}

function addMessage(role, text, { pending = false } = {}) {
  const messages = $("#chat-messages");
  messages.querySelector(".empty-message")?.remove();
  const item = document.createElement("article");
  item.className = `message message-${role}`;
  item.dataset.role = role;
  if (pending) item.dataset.pending = "true";
  item.innerHTML = `<span>${role === "user" ? "나" : "GaiA"}</span><p>${escapeHtml(text)}</p>`;
  messages.append(item);
  messages.scrollTop = messages.scrollHeight;
  return item;
}

function updateMessage(item, text) {
  const content = item.querySelector("p");
  if (content) content.textContent = text;
  item.dataset.pending = "false";
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
}

function appendEventLog(event) {
  const log = $("#event-log");
  const previous = log.textContent === "아직 요청하거나 응답을 받지 않았습니다." ? "" : `${log.textContent}\n`;
  const serialized = typeof event === "string" ? event : JSON.stringify(event, null, 2);
  log.textContent = `${previous}${serialized}`.slice(-30_000);
}

function mapping(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function textValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

function extractAgentText(payload) {
  if (typeof payload === "string") return payload.trim();
  const response = mapping(payload);
  const direct = [
    response.answer,
    response.message,
    response.text,
    mapping(response.gaia_response).answer,
    mapping(mapping(response.gaia_response).data).answer,
  ];
  for (const candidate of direct) {
    const answer = textValue(candidate);
    if (answer) return answer;
  }

  const outputs = Array.isArray(response.outputs) ? response.outputs : [];
  for (const outer of [...outputs].reverse()) {
    const components = Array.isArray(mapping(outer).outputs) ? mapping(outer).outputs : [];
    for (const component of [...components].reverse()) {
      const item = mapping(component);
      const isChatOutput = item.component_display_name === "Chat Output"
        || String(item.component_id || "").startsWith("ChatOutput-");
      if (!isChatOutput) continue;
      const results = mapping(item.results);
      const answer = textValue(mapping(mapping(results.gaia_response).data).answer)
        || textValue(mapping(mapping(results.message).data).text);
      if (answer) return answer;
    }
  }
  return "";
}

async function responseError(response) {
  const text = await response.text();
  try {
    const value = JSON.parse(text);
    const detail = value?.detail || value;
    return detail?.message || detail?.errorMessage || text;
  } catch {
    return text || `HTTP ${response.status}`;
  }
}

async function sendMessage(message) {
  if (!state.config?.configured || state.sending) return;
  setSending(true);
  addMessage("user", message);
  const agentMessage = addMessage("agent", "응답을 기다리고 있습니다…", { pending: true });
  let displayed = "";
  try {
    const response = await fetch("/api/chat/completion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: state.sessionId }),
    });
    if (!response.ok) throw new Error(await responseError(response));

    const payload = await response.json();
    appendEventLog({ request_payload: payload.request_payload, response: payload.response });
    displayed = extractAgentText(payload.response || payload);
    updateMessage(agentMessage, displayed || "응답은 수신했지만 표시 가능한 답변을 찾지 못했습니다. ‘요청·응답 원문 확인’에서 원문을 확인해 주세요.");
  } catch (error) {
    updateMessage(agentMessage, `요청 실패: ${error?.message || "알 수 없는 오류"}`);
  } finally {
    setSending(false);
  }
}

async function loadConfig() {
  try {
    const response = await fetch("/api/config", { cache: "no-store" });
    state.config = await response.json();
    if (state.config?.configured) {
      state.sessionId = state.config.fixed_session_id || createBrowserSession(state.config.user_id);
    }
  } catch (error) {
    state.config = { configured: false, invalid_or_missing: ["서버 연결"] };
  }
  updateConfigDisplay();
  setSending(false);
}

$("#floating-launcher").addEventListener("click", () => setOpen(!state.opened));
$("#chat-close").addEventListener("click", () => setOpen(false));
$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  await sendMessage(message);
});

void loadConfig();
