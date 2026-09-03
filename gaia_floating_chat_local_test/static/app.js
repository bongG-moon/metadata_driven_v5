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
  const previous = log.textContent === "아직 수신한 이벤트가 없습니다." ? "" : `${log.textContent}\n`;
  const serialized = typeof event === "string" ? event : JSON.stringify(event, null, 2);
  log.textContent = `${previous}${serialized}`.slice(-30_000);
}

function partText(parts) {
  if (!Array.isArray(parts)) return "";
  return parts
    .map((part) => {
      if (!part || typeof part !== "object") return "";
      return typeof part.text === "string" ? part.text : "";
    })
    .filter(Boolean)
    .join("\n");
}

function extractAgentText(event) {
  if (!event || typeof event !== "object") return { text: "", replace: false };
  const openAiDelta = event?.choices?.[0]?.delta?.content;
  if (typeof openAiDelta === "string") return { text: openAiDelta, replace: false };

  const result = event.result && typeof event.result === "object" ? event.result : null;
  const resultText = partText(result?.parts) || partText(result?.message?.parts);
  if (resultText) return { text: resultText, replace: true };

  const params = event.params && typeof event.params === "object" ? event.params : null;
  const paramsText = partText(params?.message?.parts) || partText(params?.delta?.parts);
  if (paramsText) return { text: paramsText, replace: Boolean(params?.message) };

  return { text: "", replace: false };
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

async function consumeSse(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const data = frame
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (!data || data === "[DONE]") continue;
      try {
        onEvent(JSON.parse(data));
      } catch {
        onEvent(data);
      }
    }
  }
}

async function sendMessage(message) {
  if (!state.config?.configured || state.sending) return;
  setSending(true);
  addMessage("user", message);
  const agentMessage = addMessage("agent", "응답을 기다리고 있습니다…", { pending: true });
  let displayed = "";
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: state.sessionId }),
    });
    if (!response.ok) throw new Error(await responseError(response));

    const contentType = String(response.headers.get("content-type") || "").toLowerCase();
    const receiveEvent = (event) => {
      appendEventLog(event);
      const extracted = extractAgentText(event);
      if (!extracted.text) return;
      displayed = extracted.replace ? extracted.text : `${displayed}${extracted.text}`;
      updateMessage(agentMessage, displayed);
    };

    if (contentType.includes("text/event-stream") && response.body) {
      await consumeSse(response.body, receiveEvent);
    } else {
      const payload = await response.json();
      appendEventLog(payload);
      const extracted = extractAgentText(payload.response || payload);
      displayed = extracted.text || JSON.stringify(payload.response || payload, null, 2);
      updateMessage(agentMessage, displayed);
    }
    if (!displayed) updateMessage(agentMessage, "응답 이벤트는 수신했지만 표시 가능한 텍스트를 찾지 못했습니다. ‘수신 이벤트 확인’에서 원문을 확인해 주세요.");
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
