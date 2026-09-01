const state = {
  portal: null,
  scheduleFilter: "all",
  scheduleScope: "mine",
  scheduleSearch: "",
  metadataSearch: "",
  metadataType: "domain",
  metadataPage: 1,
  metadataPageSize: 10,
  metadataResultTab: "process",
  metadataResult: null,
  metadataApi: null,
  metadataLive: { state: "idle", payload: null },
  metadataSubmitting: false,
  metadataStatusTarget: null,
  metadataStatusUpdating: false,
  metadataStatusOpener: null,
  metadataDetailTarget: null,
  metadataDetailLoading: false,
  metadataDetailOpener: null,
  adminSettings: null,
  adminSettingsLoading: false,
  adminSettingsError: "",
  adminAdding: false,
  adminAddOpener: null,
  adminAddMessage: "",
  dashboardUsage: { state: "idle", source: null, message: "" },
  dashboardUsageFullRefreshing: false,
  dashboardUsageExporting: false,
  schedulesData: { state: "idle", message: "" },
  scheduleSubmitting: false,
  scheduleMutationId: "",
  editingScheduleId: null,
  eventsBound: false,
};

const SCHEDULE_DELIVERY_TARGET = "개인 DM";
const SCHEDULE_DELIVERY_LABEL = "등록자 개인 DM";

const viewTitles = {
  dashboard: ["활용 현황", "한눈에 보는 활용 현황"],
  schedules: ["스케줄링", "스케줄 등록·조회"],
  metadata: ["메타데이터", "Agent 메타데이터 관리"],
  settings: ["설정", "관리자 설정"],
};

const metadataTypes = {
  domain: {
    label: "도메인 정보",
    kicker: "DOMAIN KNOWLEDGE",
    totalLabel: "도메인 항목",
    description: "공정 그룹, 업무 용어, 분석 레시피처럼 Agent가 질문을 해석할 때 쓰는 업무 지식을 관리합니다.",
    filterHint: "구분, 키, 표시명과 상태를 확인합니다.",
    headers: ["구분", "키", "표시명", "상태", "조회 · 관리"],
  },
  table_catalog: {
    label: "데이터 카탈로그",
    kicker: "DATA CATALOG",
    totalLabel: "등록 데이터셋",
    description: "Agent가 어떤 데이터를 어디에서 어떤 조건으로 조회할 수 있는지 정의합니다.",
    filterHint: "데이터셋 키, 연결 방식, 필수 표준 Filter를 확인합니다.",
    headers: ["데이터셋 키", "데이터셋", "분류", "연결 방식", "필수 조건", "상태", "조회 · 관리"],
  },
  main_flow_filters: {
    label: "메인 필터",
    kicker: "MAIN FLOW FILTERS",
    totalLabel: "표준 Filter",
    description: "질문 속 일자·공정·LOT 같은 표현을 표준 키와 실제 데이터 컬럼으로 연결합니다.",
    filterHint: "필터 키, 연산자와 값 형식을 확인합니다.",
    headers: ["필터 키", "표시명", "연산자", "값 타입", "값 형태", "상태", "조회 · 관리"],
  },
};

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];

function svgIcon(name, className = "") {
  const paths = {
    check: '<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12 2.3 2.3 4.8-5.1"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/>',
    alert: '<circle cx="12" cy="12" r="8.5"/><path d="M12 8.3v4.4m0 3h.01"/>',
    question: '<circle cx="12" cy="12" r="8.5"/><path d="M9.8 9.5a2.4 2.4 0 1 1 3.8 2l-1.2.9v1.1m0 2.4h.01"/>',
    database: '<ellipse cx="12" cy="5.5" rx="6.7" ry="2.7"/><path d="M5.3 5.5v6c0 1.5 3 2.7 6.7 2.7s6.7-1.2 6.7-2.7v-6m-13.4 6v6c0 1.5 3 2.7 6.7 2.7s6.7-1.2 6.7-2.7v-6"/>',
    calendar: '<rect x="4" y="5" width="16" height="15" rx="3"/><path d="M8 3v4m8-4v4M4 10h16"/>',
  };
  const body = paths[name] || paths.question;
  const safeClassName = className ? ` class="${className}"` : "";
  return `<svg${safeClassName} aria-hidden="true" viewBox="0 0 24 24" focusable="false">${body}</svg>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusClass(status) {
  if (["성공", "활성", "active", "정상", "연결됨", "요청 가능", "등록됨"].includes(status)) return "status-success";
  if (["실패"].includes(status)) return "status-review";
  if (["비활성", "inactive", "일시중지", "재시도 예정", "검토 필요", "확인 필요", "미설정", "설정 필요", "연결 확인 필요", "API URL 미설정", "인증 정보 필요", "호출 사번 필요", "MongoDB 설정 필요", "Flow 구성 확인"].includes(status)) return "status-paused";
  if (["취소됨", "건너뜀"].includes(status)) return "status-paused";
  return "status-draft";
}

function statusPill(status) {
  return `<span class="status-pill ${statusClass(status)}">${escapeHtml(status)}</span>`;
}

function isAdmin() {
  return Boolean(state.portal?.viewer?.is_admin);
}

function viewerId() {
  return state.portal?.viewer?.employee_id || "";
}

function portalRequestHeaders(headers = {}) {
  // The Portal server now resolves the employee from its SSO session (or the
  // fixed local adapter).  Never send browser-controlled employee headers.
  return { ...headers };
}

function emptyUsageDashboard(message = "사용 이력을 아직 조회하지 않았습니다.") {
  return {
    unavailable: true,
    period_label: "최근 3주",
    range_label: "사용 이력 확인 전",
    day_count: 21,
    cumulative_user_count: 0,
    total_chat_count: 0,
    kpis: [
      { label: "일 평균 사용자", value: "—", change: "이력 조회 필요", tone: "accent", detail: "Phoenix 연결 후 표시" },
      { label: "일 평균 채팅", value: "—", change: "이력 조회 필요", tone: "positive", detail: "Phoenix 연결 후 표시" },
      { label: "누적 사용자", value: "—", change: "이력 조회 필요", tone: "accent", detail: "Phoenix 연결 후 표시" },
      { label: "누적 채팅", value: "—", change: "이력 조회 필요", tone: "positive", detail: "Phoenix 연결 후 표시" },
      { label: "활성 사용자", value: "—", change: "이력 조회 필요", tone: "positive", detail: "Phoenix 연결 후 표시" },
    ],
    usage_by_day: [],
    active_users: [],
    active_user_count: 0,
    active_user_rule: { min_distinct_days: 0, min_chat_count: 0 },
    recent_usage_history: [],
    recent_runs: [],
    recent_runs_message: "최근 스케줄 실행 이력이 없습니다.",
    empty_message: message,
  };
}

function dashboardUsageState() {
  return state.dashboardUsage || { state: "idle", source: null, message: "" };
}

function applyDashboardUsagePayload(payload) {
  const normalized = normalizeDashboardUsagePayload(payload);
  state.portal.dashboard = normalized.dashboard;
  state.portal.usage_history = normalized.usage_history;
  state.dashboardUsage = {
    state: normalized.mode === "preview" ? "preview" : "live",
    source: normalized.source,
    message: "",
  };
  return normalized;
}

function usageArchiveDisplayDetail(source) {
  const archive = source?.archive && typeof source.archive === "object" ? source.archive : {};
  const archiveMode = String(archive.mode || "").trim().toLowerCase();
  const archiveStatus = String(archive.status || "").trim().toLowerCase();
  const usesMongoCache = archiveMode === "configured" || ["cached", "synchronized"].includes(archiveStatus);
  if (!usesMongoCache) return "";

  const updatedDayCount = Number(archive.updated_day_count);
  const updatedDayLabel = Number.isInteger(updatedDayCount) && updatedDayCount > 0
    ? `${updatedDayCount}개 일자`
    : "선택 일자";
  if (archive.full_refresh === true) {
    return `MongoDB 최근 3주 캐시를 갱신하고 Phoenix 전체 범위를 다시 조회했습니다 (${updatedDayLabel}).`;
  }
  return `MongoDB 최근 3주 캐시를 먼저 표시하고 Phoenix ${updatedDayLabel}만 갱신했습니다.`;
}

function usageRecordDate(record) {
  const explicitDate = String(record?.date || "").trim();
  if (explicitDate) return explicitDate;
  const queriedAt = String(record?.occurred_at || record?.query_time || "").trim();
  return queriedAt ? queriedAt.slice(0, 10) : "-";
}

function usageRecordEmployeeId(record) {
  return String(record?.employee_id || record?.user_id || "").trim() || "-";
}

function usageRecordName(record) {
  return String(record?.user_name || record?.employee_id || record?.user_id || "").trim() || "-";
}

function normalizeDashboardUsagePayload(payload) {
  if (!payload || typeof payload !== "object") {
    throw new Error("사용 이력 API 응답 형식을 확인하지 못했습니다.");
  }
  const source = payload.source;
  const dashboard = payload.dashboard;
  if (!source || typeof source !== "object" || !dashboard || typeof dashboard !== "object") {
    throw new Error("사용 이력 API의 출처 또는 집계 결과가 없습니다.");
  }

  const configuredMode = String(source.mode || "").trim().toLowerCase();
  const configuredStatus = String(source.status || "").trim().toLowerCase();
  if (!["phoenix", "preview"].includes(configuredMode)) {
    throw new Error("사용 이력 API의 조회 모드를 확인하지 못했습니다.");
  }
  if (!["connected", "preview"].includes(configuredStatus)) {
    throw new Error("사용 이력 API의 연결 상태를 확인하지 못했습니다.");
  }
  const mode = configuredMode === "preview" || configuredStatus === "preview" ? "preview" : "phoenix";

  const fallback = emptyUsageDashboard();
  const rawUsageByDay = Array.isArray(dashboard.usage_by_day) ? dashboard.usage_by_day : [];
  const maxUsers = Math.max(...rawUsageByDay.map((item) => Number(item?.unique_users) || 0), 1);
  const maxChats = Math.max(...rawUsageByDay.map((item) => Number(item?.chat_count) || 0), 1);
  const usageByDay = rawUsageByDay.map((item) => {
    const uniqueUsers = Number(item?.unique_users) || 0;
    const chatCount = Number(item?.chat_count) || 0;
    const date = String(item?.date || "").trim();
    return {
      ...item,
      date,
      label: String(item?.label || (date.length >= 10 ? `${Number(date.slice(5, 7))}/${Number(date.slice(8, 10))}` : "-")),
      unique_users: uniqueUsers,
      chat_count: chatCount,
      user_height: Number.isFinite(Number(item?.user_height)) ? Number(item.user_height) : Math.round((uniqueUsers / maxUsers) * 1000) / 10,
      chat_height: Number.isFinite(Number(item?.chat_height)) ? Number(item.chat_height) : Math.round((chatCount / maxChats) * 1000) / 10,
    };
  });
  const normalizedDashboard = {
    ...fallback,
    ...dashboard,
    unavailable: false,
    kpis: Array.isArray(dashboard.kpis) ? dashboard.kpis : fallback.kpis,
    usage_by_day: usageByDay,
    active_users: Array.isArray(dashboard.active_users) ? dashboard.active_users : [],
    recent_usage_history: Array.isArray(dashboard.recent_usage_history) ? dashboard.recent_usage_history : [],
    recent_runs: Array.isArray(dashboard.recent_runs) ? dashboard.recent_runs : [],
    recent_runs_message: String(dashboard.recent_runs_message || fallback.recent_runs_message).trim(),
    active_user_rule: dashboard.active_user_rule && typeof dashboard.active_user_rule === "object"
      ? dashboard.active_user_rule
      : fallback.active_user_rule,
  };
  const history = Array.isArray(payload.usage_history)
    ? payload.usage_history
    : normalizedDashboard.recent_usage_history;

  return { source, mode, dashboard: normalizedDashboard, usage_history: history };
}

function formatDashboardFetchedAt(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function renderDashboardSourceStatus() {
  const container = $("#dashboard-source-status");
  const icon = $("#dashboard-source-icon");
  const title = $("#dashboard-source-title");
  const detail = $("#dashboard-source-detail");
  const retry = $("#dashboard-source-retry");
  const fullRefresh = $("#dashboard-full-refresh");
  if (!container || !icon || !title || !detail || !retry) return;

  const usage = dashboardUsageState();
  const source = usage.source && typeof usage.source === "object" ? usage.source : {};
  const sourceMode = String(source.mode || "").toLowerCase();
  const sourceLabel = String(source.label || "Phoenix").trim() || "Phoenix";
  const sourceDetail = String(source.detail || "").trim();
  const projectCount = Number(source.project_count);
  const fetchedAt = formatDashboardFetchedAt(source.fetched_at);
  const archiveDetail = usageArchiveDisplayDetail(source);
  const archive = source.archive && typeof source.archive === "object" ? source.archive : {};
  const hasFullRefreshResult = archive.full_refresh === true;
  const fullRefreshInProgress = Boolean(state.dashboardUsageFullRefreshing);
  const supportDetails = [];
  if (archiveDetail) supportDetails.push(archiveDetail);
  else if (sourceDetail) supportDetails.push(sourceDetail);
  if (Number.isInteger(projectCount) && projectCount > 0) supportDetails.push(`${projectCount}개 프로젝트 조회`);
  if (fetchedAt) supportDetails.push(`마지막 조회 ${fetchedAt}`);

  container.classList.remove("is-loading", "is-live", "is-preview", "is-error", "is-idle");
  retry.disabled = usage.state === "loading" || fullRefreshInProgress;
  retry.hidden = false;
  retry.textContent = usage.state === "loading"
    ? "조회 중…"
    : fullRefreshInProgress
      ? "전체 갱신 중…"
      : "↻ 사용 이력 새로고침";

  if (fullRefresh) {
    const admin = isAdmin();
    const isPreview = sourceMode === "preview" || usage.state === "preview";
    const canRefreshAll = admin && !isPreview && usage.state !== "loading" && !fullRefreshInProgress;
    fullRefresh.hidden = !admin;
    fullRefresh.disabled = !canRefreshAll;
    fullRefresh.classList.toggle("is-loading", fullRefreshInProgress);
    fullRefresh.setAttribute("aria-busy", String(fullRefreshInProgress));
    fullRefresh.title = isPreview
      ? "Phoenix 실사용 이력을 연결한 뒤 전체 새로고침을 사용할 수 있습니다."
      : "관리자만 Phoenix 최근 3주 이력을 전체 다시 조회할 수 있습니다.";
    fullRefresh.textContent = fullRefreshInProgress
      ? "최근 3주 전체 새로고침 중…"
      : "↻ 최근 3주 전체 새로고침";
  }

  if (fullRefreshInProgress) {
    container.classList.add("is-loading");
    icon.innerHTML = svgIcon("clock");
    title.textContent = "최근 3주 전체 새로고침 중";
    detail.textContent = "Phoenix 최근 3주 이력을 다시 조회해 MongoDB 캐시를 갱신하고 있습니다. 기존 집계는 완료될 때까지 유지됩니다.";
    return;
  }

  if (usage.state === "live") {
    container.classList.add("is-live");
    icon.innerHTML = svgIcon("check");
    title.textContent = hasFullRefreshResult
      ? "최근 3주 전체 새로고침 완료"
      : archiveDetail
        ? "MongoDB 캐시 + Phoenix 선택 갱신"
        : `${sourceLabel} 실시간 사용 이력`;
    detail.textContent = supportDetails.join(" · ") || "최근 3주 사용 이력을 Phoenix에서 조회했습니다.";
    return;
  }
  if (usage.state === "preview" || sourceMode === "preview") {
    container.classList.add("is-preview");
    icon.innerHTML = svgIcon("clock");
    title.textContent = "미리보기 데이터";
    detail.textContent = supportDetails.join(" · ") || "Phoenix 연동 전의 예시 사용 이력을 표시하고 있습니다.";
    return;
  }
  if (usage.state === "error") {
    container.classList.add("is-error");
    icon.innerHTML = svgIcon("alert");
    title.textContent = "Phoenix 사용 이력을 불러오지 못했습니다";
    detail.textContent = usage.message || "연결 상태를 확인한 뒤 다시 시도해 주세요. 이전 미리보기 수치는 표시하지 않습니다.";
    return;
  }
  if (usage.state === "loading") {
    container.classList.add("is-loading");
    icon.innerHTML = svgIcon("clock");
    title.textContent = "Phoenix 사용 이력 조회 중";
    detail.textContent = "최근 3주 이력을 조회하고 있습니다. 조회가 끝나면 실제 집계 결과로 표시됩니다.";
    return;
  }
  container.classList.add("is-idle");
  icon.innerHTML = svgIcon("question");
  title.textContent = "사용 이력 조회 대기";
  detail.textContent = "Phoenix 사용 이력 조회를 시작하면 실제 집계 결과가 표시됩니다.";
}

async function loadDashboardUsage({ notifyOnError = false } = {}) {
  if (!state.portal || state.dashboardUsageFullRefreshing) return;

  // Schedule runs come from a separate MongoDB collection.  Retain the last
  // safely loaded run slice while Phoenix usage refreshes, so an unrelated
  // usage-history failure does not temporarily erase that table.
  const previousRuns = Array.isArray(state.portal.dashboard?.recent_runs)
    ? state.portal.dashboard.recent_runs
    : [];
  const previousRunsMessage = String(
    state.portal.dashboard?.recent_runs_message || "최근 스케줄 실행 이력이 없습니다."
  ).trim();
  state.dashboardUsage = { state: "loading", source: null, message: "" };
  state.portal.dashboard = {
    ...emptyUsageDashboard("Phoenix 사용 이력을 조회하고 있습니다."),
    recent_runs: previousRuns,
    recent_runs_message: previousRuns.length ? "" : previousRunsMessage,
  };
  state.portal.usage_history = [];
  renderDashboard();
  renderMetadataApiIndicator();

  try {
    const response = await fetch("/api/dashboard/usage", {
      headers: portalRequestHeaders(),
      cache: "no-store",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const fallback = response.status === 503
        ? "Phoenix 사용 이력을 현재 조회할 수 없습니다. 연결 설정을 확인한 뒤 다시 시도해 주세요."
        : "사용 이력 API를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
      throw new Error(errorMessageFromResponse(payload, fallback));
    }

    applyDashboardUsagePayload(payload);
  } catch (error) {
    // A configured Phoenix failure must never leave dummy dashboard values in
    // view.  Only an explicit server-side preview response can display them.
    console.warn("dashboard usage unavailable", error);
    const message = error?.message || "Phoenix 사용 이력을 불러오지 못했습니다. 다시 시도해 주세요.";
    state.portal.dashboard = {
      ...emptyUsageDashboard(message),
      recent_runs: previousRuns,
      recent_runs_message: previousRuns.length ? "" : previousRunsMessage,
    };
    state.portal.usage_history = [];
    state.dashboardUsage = { state: "error", source: null, message };
    if (notifyOnError) showToast(message);
  } finally {
    renderDashboard();
    renderMetadataApiIndicator();
  }
}

async function refreshDashboardUsageFull() {
  if (
    !state.portal
    || !isAdmin()
    || state.dashboardUsageFullRefreshing
    || dashboardUsageState().state === "loading"
    || dashboardUsageState().state === "preview"
  ) return;

  state.dashboardUsageFullRefreshing = true;
  renderDashboard();

  try {
    const response = await fetch("/api/dashboard/usage/refresh", {
      method: "POST",
      headers: portalRequestHeaders({ Accept: "application/json" }),
      cache: "no-store",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const fallback = response.status === 503
        ? "Phoenix 최근 3주 사용 이력을 전체 새로고침할 수 없습니다. 연결 설정을 확인한 뒤 다시 시도해 주세요."
        : "최근 3주 사용 이력을 전체 새로고침하지 못했습니다. 잠시 후 다시 시도해 주세요.";
      throw new Error(errorMessageForAdminResponse(response.status, payload, fallback));
    }

    applyDashboardUsagePayload(payload);
    showToast("최근 3주 사용 이력을 전체 새로고침했습니다.");
  } catch (error) {
    console.error("dashboard usage full refresh failed", error);
    showToast(error?.message || "최근 3주 사용 이력을 전체 새로고침하지 못했습니다.");
  } finally {
    state.dashboardUsageFullRefreshing = false;
    renderDashboard();
    renderMetadataApiIndicator();
  }
}

function dashboardUsageExportFilename(response) {
  const contentDisposition = String(response?.headers?.get("content-disposition") || "");
  const encodedFilename = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encodedFilename) {
    try {
      return decodeURIComponent(encodedFilename).replaceAll(/[\\/:*?"<>|]/g, "_");
    } catch {
      // Fall through to the plain filename or the predictable Portal fallback.
    }
  }
  const plainFilename = contentDisposition.match(/filename="?([^";]+)"?/i)?.[1];
  return String(plainFilename || "ptmore_usage_history.csv").replaceAll(/[\\/:*?"<>|]/g, "_");
}

function setDashboardUsageExportBusy(isBusy) {
  const button = $("#dashboard-usage-export");
  if (!button) return;
  state.dashboardUsageExporting = isBusy;
  button.disabled = isBusy;
  button.classList.toggle("is-loading", isBusy);
  button.setAttribute("aria-busy", String(isBusy));
  button.innerHTML = isBusy
    ? `${svgIcon("clock", "button-spinner")}<span>CSV 준비 중…</span>`
    : '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 3v11m0 0 4-4m-4 4-4-4M5 18v2h14v-2" /></svg><span>CSV 다운로드</span>';
}

async function downloadDashboardUsageCsv() {
  if (!state.portal || state.dashboardUsageExporting) return;
  setDashboardUsageExportBusy(true);

  try {
    const response = await fetch("/api/dashboard/usage/export.csv", {
      headers: portalRequestHeaders({ Accept: "text/csv, application/json" }),
      cache: "no-store",
    });
    if (!response.ok) {
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        // A proxy may return a plain-text error page. Do not surface its raw
        // HTML in the Portal; use the concise fallback below instead.
      }
      throw new Error(errorMessageFromResponse(payload, "사용 이력 CSV를 다운로드하지 못했습니다."));
    }

    const blob = await response.blob();
    if (!blob.size) throw new Error("다운로드할 사용 이력 데이터가 없습니다.");

    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = dashboardUsageExportFilename(response);
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    showToast("최근 사용 이력 CSV를 다운로드했습니다.");
  } catch (error) {
    console.error("dashboard usage CSV export failed", error);
    showToast(error?.message || "사용 이력 CSV를 다운로드하지 못했습니다.");
  } finally {
    setDashboardUsageExportBusy(false);
  }
}

function errorMessageForAdminResponse(status, payload, fallback) {
  if (status === 401) return "로그인 사용자 정보를 확인할 수 없습니다. 다시 접속해 주세요.";
  if (status === 403) return "관리자만 설정을 확인하거나 변경할 수 있습니다.";
  return errorMessageFromResponse(payload, fallback);
}

function normalizeAdminSettings(payload) {
  const value = payload && typeof payload === "object" ? (payload.settings || payload) : {};
  const sourcePolicy = value.usage_policy && typeof value.usage_policy === "object"
    ? value.usage_policy
    : {};
  const usagePolicy = {};
  ["history_window_days", "active_user_min_distinct_days", "active_user_min_chat_count"].forEach((key) => {
    const number = Number(sourcePolicy[key]);
    if (Number.isInteger(number) && number > 0) usagePolicy[key] = number;
  });
  return {
    gaia_api_caller_employee_id: String(value.gaia_api_caller_employee_id || "").trim(),
    updated_at: String(value.updated_at || "").trim(),
    updated_by: String(value.updated_by || "").trim(),
    admins: Array.isArray(value.admins) ? value.admins : [],
    usage_policy: usagePolicy,
    storage: value.storage && typeof value.storage === "object" ? value.storage : {},
  };
}

function gaiaApiCallerEmployeeId() {
  return state.adminSettings?.gaia_api_caller_employee_id || "";
}

function applyAdminSettingsToPortal(adminSettings) {
  if (!state.portal || !adminSettings) return;
  state.portal.settings = state.portal.settings || {};
  state.portal.settings.gaia_api_caller_employee_id = adminSettings.gaia_api_caller_employee_id || "";
  state.portal.settings.usage_policy = {
    ...(state.portal.settings.usage_policy || {}),
    ...(adminSettings.usage_policy || {}),
  };
  if (Array.isArray(adminSettings.admins) && adminSettings.admins.length) {
    state.portal.settings.admins = adminSettings.admins;
  }
}

function normalizeAdministratorRecord(record, fallback = {}) {
  const source = record && typeof record === "object" ? record : {};
  const employeeId = String(
    source.employee_id
      || source.emp_no
      || fallback.employee_id
      || "",
  ).trim();
  const name = String(
    source.name
      || source.employee_name
      || source.emp_name
      || fallback.employee_name
      || "",
  ).trim();
  return {
    ...source,
    employee_id: employeeId,
    name,
    role: String(source.role || "관리자").trim() || "관리자",
    scope: String(source.scope || "포털 운영 관리").trim() || "포털 운영 관리",
    status: String(source.status || "활성").trim() || "활성",
  };
}

function applyAdministratorResponse(payload, fallback) {
  if (!payload || typeof payload !== "object") return;
  if (payload.settings && typeof payload.settings === "object") {
    state.adminSettings = normalizeAdminSettings(payload.settings);
    applyAdminSettingsToPortal(state.adminSettings);
    return;
  }

  const record = payload.administrator || payload.admin;
  if (!record || typeof record !== "object") return;
  const administrator = normalizeAdministratorRecord(record, fallback);
  if (!administrator.employee_id) return;
  const currentSettings = state.adminSettings || normalizeAdminSettings(state.portal?.settings || {});
  const currentAdmins = Array.isArray(currentSettings.admins) ? currentSettings.admins : [];
  const index = currentAdmins.findIndex((item) => String(item?.employee_id || "").trim() === administrator.employee_id);
  const admins = [...currentAdmins];
  if (index >= 0) admins[index] = { ...admins[index], ...administrator };
  else admins.push(administrator);
  state.adminSettings = { ...currentSettings, admins };
  applyAdminSettingsToPortal(state.adminSettings);
}

function setAdminAddBusy(isBusy, message = "") {
  const modal = $("#admin-add-modal");
  const form = $("#admin-add-form");
  const submitButton = $("#admin-add-submit");
  const hint = $("#admin-add-form-hint");
  if (modal) modal.setAttribute("aria-busy", String(isBusy));
  if (form) {
    $$('input, button', form).forEach((control) => {
      control.disabled = isBusy;
    });
  }
  if (submitButton) {
    submitButton.innerHTML = isBusy
      ? `${svgIcon("clock", "button-spinner")}<span>등록 중…</span>`
      : "관리자 등록";
  }
  if (hint) {
    hint.textContent = message || (isBusy
      ? "관리자 권한을 저장하고 있습니다. 잠시만 기다려 주세요."
      : "관리자 권한은 사번 기준으로 적용됩니다.");
  }
}

function openAdminAddModal(opener) {
  if (!isAdmin()) {
    showToast("관리자만 관리자 명단을 변경할 수 있습니다.");
    return;
  }
  if (state.adminAdding) return;
  const modal = $("#admin-add-modal");
  const dialog = $(".admin-add-dialog", modal);
  const form = $("#admin-add-form");
  if (!modal || !dialog || !form) return;

  form.reset();
  state.adminAddMessage = "";
  setAdminAddBusy(false);
  state.adminAddOpener = opener instanceof HTMLElement ? opener : document.activeElement;
  modal.hidden = false;
  document.body.classList.add("dialog-open");
  window.setTimeout(() => $("#admin-add-employee-id")?.focus(), 0);
}

function closeAdminAddModal({ force = false } = {}) {
  if (state.adminAdding && !force) return;
  const modal = $("#admin-add-modal");
  if (!modal || modal.hidden) return;
  const opener = state.adminAddOpener;
  modal.hidden = true;
  if ($("#metadata-detail-modal")?.hidden !== false && $("#metadata-status-modal")?.hidden !== false) {
    document.body.classList.remove("dialog-open");
  }
  state.adminAddOpener = null;
  if (opener instanceof HTMLElement && opener.isConnected) {
    window.setTimeout(() => opener.focus(), 0);
  }
}

async function submitAdministrator(form) {
  if (!isAdmin()) {
    showToast("관리자만 관리자 명단을 변경할 수 있습니다.");
    return;
  }
  if (state.adminAdding) return;

  const formData = new FormData(form);
  const employeeId = String(formData.get("employee_id") || "").trim();
  const employeeName = String(formData.get("employee_name") || "").trim();
  if (!/^\d{7}$/.test(employeeId)) {
    showToast("관리자 사번은 숫자 7자리로 입력해 주세요.");
    $("#admin-add-employee-id")?.focus();
    return;
  }
  if (!employeeName) {
    showToast("관리자 이름을 입력해 주세요.");
    $("#admin-add-employee-name")?.focus();
    return;
  }

  state.adminAdding = true;
  state.adminAddMessage = "";
  setAdminAddBusy(true);
  try {
    const response = await fetch("/api/settings/admins", {
      method: "POST",
      headers: portalRequestHeaders({ Accept: "application/json", "Content-Type": "application/json" }),
      body: JSON.stringify({ employee_id: employeeId, employee_name: employeeName }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageForAdminResponse(response.status, payload, "관리자 정보를 저장하지 못했습니다."));
    }

    applyAdministratorResponse(payload, { employee_id: employeeId, employee_name: employeeName });
    // The endpoint can return either the complete settings object or only the
    // new administrator. Re-read the canonical settings record in both cases.
    await loadAdminSettings();
    const refreshFailed = Boolean(state.adminSettingsError);
    closeAdminAddModal({ force: true });
    renderSettings();
    showToast(refreshFailed
      ? "관리자를 등록했습니다. 목록을 새로고침해 최종 상태를 확인해 주세요."
      : `${employeeName} (${employeeId}) 관리자를 등록했습니다.`);
  } catch (error) {
    console.error("administrator add request failed", error);
    state.adminAddMessage = error?.message || "관리자 정보를 저장하지 못했습니다.";
    showToast(state.adminAddMessage);
  } finally {
    state.adminAdding = false;
    setAdminAddBusy(false, state.adminAddMessage);
    renderSettings();
  }
}

function scheduleOwnerId(schedule) {
  const rawOwner = typeof schedule?.owner === "string" ? schedule.owner.trim() : "";
  return String(
    schedule?.owner_id
      || schedule?.owner_employee_id
      || (schedule?.owner && typeof schedule.owner === "object" ? schedule.owner.employee_id : "")
      || (/^\d{5,}$/.test(rawOwner) ? rawOwner : "")
      || "",
  ).trim();
}

function canEditSchedule(schedule) {
  return isAdmin() || scheduleOwnerId(schedule) === viewerId();
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2800);
}

function channelLabel(channel) {
  return {
    CUBE: "CUBE 직접 질의",
    CUBE_SCHEDULING: "정기 스케줄",
    ADMIN_TEST: "관리자 테스트",
  }[channel] || channel;
}

function renderDashboard() {
  const dashboard = state.portal?.dashboard || emptyUsageDashboard();
  const kpis = Array.isArray(dashboard.kpis) ? dashboard.kpis : [];
  const usageByDay = Array.isArray(dashboard.usage_by_day) ? dashboard.usage_by_day : [];
  const activeUsers = Array.isArray(dashboard.active_users) ? dashboard.active_users : [];
  const usageHistory = Array.isArray(dashboard.recent_usage_history) ? dashboard.recent_usage_history : [];
  const recentRuns = Array.isArray(dashboard.recent_runs) ? dashboard.recent_runs : [];
  const rule = dashboard.active_user_rule && typeof dashboard.active_user_rule === "object"
    ? dashboard.active_user_rule
    : { min_distinct_days: 0, min_chat_count: 0 };

  $("#dashboard-period").textContent = dashboard.period_label || "최근 3주";
  $("#dashboard-range").textContent = dashboard.range_label || "사용 이력 확인 전";
  $("#kpi-grid").innerHTML = kpis
    .map((item, index) => `
      <article class="kpi-card" style="--card-glow:${index === 1 ? "#e9fbf7" : index === 2 ? "#fff5de" : "#edf3ff"}">
        <span class="card-label">${escapeHtml(item.label)}</span>
        <strong>${escapeHtml(item.value)}</strong>
        <footer><span class="metric-change ${item.tone === "accent" ? "accent" : ""}">${escapeHtml(item.change)}</span>${escapeHtml(item.detail)}</footer>
      </article>`)
    .join("");

  $("#usage-total").textContent = dashboard.unavailable
    ? "집계 대기"
    : `누적 ${Number(dashboard.total_chat_count || 0).toLocaleString()}건`;
  $("#usage-chart").innerHTML = usageByDay.length
    ? usageByDay
    .map((item) => `
      <div class="bar-wrap">
        <div class="bar-pair">
          <div class="chart-bar user-bar" data-value="사용자 ${item.unique_users}명" style="height:${Math.max(item.user_height, 12)}%"></div>
          <div class="chart-bar chat-bar" data-value="채팅 ${item.chat_count}건" style="height:${Math.max(item.chat_height, 12)}%"></div>
        </div>
        <span>${escapeHtml(item.label)}</span>
      </div>`)
    .join("")
    : `<div class="usage-chart-empty">${escapeHtml(dashboard.empty_message || "표시할 사용 이력이 없습니다.")}</div>`;

  $("#active-user-summary").innerHTML = `
    <div class="active-user-number"><strong>${dashboard.unavailable ? "—" : Number(dashboard.active_user_count || 0)}</strong><span>명</span></div>
    <p>${dashboard.unavailable
      ? "Phoenix 사용 이력이 확인되면 활성 사용자 기준을 적용합니다."
      : `서로 다른 일자 <strong>${rule.min_distinct_days}일 이상</strong> · 누적 채팅 <strong>${rule.min_chat_count}건 이상</strong>`}</p>`;
  $("#active-user-list").innerHTML = activeUsers.length
    ? activeUsers
        .map((user) => `
          <li><div><strong>${escapeHtml(usageRecordName(user))}</strong><span>${escapeHtml(usageRecordEmployeeId(user))}</span></div><div><b>${escapeHtml(user.distinct_days ?? 0)}일</b><span>${escapeHtml(user.chat_count ?? 0)}건</span></div></li>`)
        .join("")
    : `<li class="empty-list">${dashboard.unavailable ? "사용 이력을 확인하면 활성 사용자를 집계합니다." : "현재 기준을 충족한 사용자가 없습니다."}</li>`;

  $("#usage-history-list").innerHTML = usageHistory.length
    ? usageHistory
    .map((record) => `
      <tr>
        <td>${escapeHtml(usageRecordDate(record))}</td><td><strong>${escapeHtml(usageRecordName(record))}</strong><span class="table-subtle">${escapeHtml(usageRecordEmployeeId(record))}</span></td><td class="question-cell">${escapeHtml(record.question || "-")}</td><td><span class="mini-tag">${escapeHtml(channelLabel(record.channel || record.platform || "-"))}</span></td>
      </tr>`)
    .join("")
    : `<tr><td colspan="4" class="dashboard-table-empty">${escapeHtml(dashboard.empty_message || "표시할 최근 사용 이력이 없습니다.")}</td></tr>`;

  $("#recent-runs").innerHTML = recentRuns.length
    ? recentRuns
    .map((run) => `
      <tr>
        <td>${escapeHtml(run.time)}</td><td><strong>${escapeHtml(run.name)}</strong></td><td>${escapeHtml(run.owner)}</td><td>${escapeHtml(run.target || SCHEDULE_DELIVERY_LABEL)}</td><td>${statusPill(run.status)}</td>
      </tr>`)
    .join("")
    : `<tr><td colspan="5" class="dashboard-table-empty">${escapeHtml(dashboard.recent_runs_message || "최근 스케줄 실행 이력이 없습니다.")}</td></tr>`;

  renderDashboardSourceStatus();
}

function buildDashboardFromHistory(history, policy) {
  const recordsByDay = new Map();
  const activityByUser = new Map();
  history.forEach((record) => {
    if (!recordsByDay.has(record.date)) recordsByDay.set(record.date, []);
    recordsByDay.get(record.date).push(record);
    if (!activityByUser.has(record.employee_id)) {
      activityByUser.set(record.employee_id, {
        employee_id: record.employee_id,
        user_name: record.user_name,
        dates: new Set(),
        chat_count: 0,
      });
    }
    const activity = activityByUser.get(record.employee_id);
    activity.dates.add(record.date);
    activity.chat_count += 1;
  });

  const usageByDay = [...recordsByDay.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([date, records]) => ({
      date,
      label: `${Number(date.slice(5, 7))}/${Number(date.slice(8, 10))}`,
      unique_users: new Set(records.map((record) => record.employee_id)).size,
      chat_count: records.length,
    }));
  const dayCount = usageByDay.length;
  const totalChats = history.length;
  const cumulativeUsers = activityByUser.size;
  const minDays = Math.max(1, Number(policy.active_user_min_distinct_days) || 1);
  const minChats = Math.max(1, Number(policy.active_user_min_chat_count) || 1);
  const activeUsers = [...activityByUser.values()]
    .map((activity) => ({
      employee_id: activity.employee_id,
      user_name: activity.user_name,
      distinct_days: activity.dates.size,
      chat_count: activity.chat_count,
    }))
    .filter((activity) => activity.distinct_days >= minDays && activity.chat_count >= minChats)
    .sort((left, right) => right.chat_count - left.chat_count || right.distinct_days - left.distinct_days);
  const maxUsers = Math.max(...usageByDay.map((item) => item.unique_users), 1);
  const maxChats = Math.max(...usageByDay.map((item) => item.chat_count), 1);
  usageByDay.forEach((item) => {
    item.user_height = Math.round((item.unique_users / maxUsers) * 1000) / 10;
    item.chat_height = Math.round((item.chat_count / maxChats) * 1000) / 10;
  });
  const averageUsers = dayCount ? usageByDay.reduce((total, item) => total + item.unique_users, 0) / dayCount : 0;
  const averageChats = dayCount ? totalChats / dayCount : 0;
  const firstDate = usageByDay[0]?.date.replaceAll("-", ".") || "-";
  const lastDate = usageByDay.at(-1)?.date.replaceAll("-", ".") || "-";
  const previousDashboard = state.portal.dashboard || {};

  return {
    ...previousDashboard,
    period_label: `최근 ${dayCount}일`,
    range_label: `${firstDate} ~ ${lastDate}`,
    day_count: dayCount,
    cumulative_user_count: cumulativeUsers,
    total_chat_count: totalChats,
    kpis: [
      { label: "일 평균 사용자", value: `${averageUsers.toFixed(1)}명`, change: "일별 고유 사번", tone: "accent", detail: `최근 ${dayCount}일 기준` },
      { label: "일 평균 채팅", value: `${averageChats.toFixed(1)}건`, change: "질문 입력 기준", tone: "positive", detail: `최근 ${dayCount}일 기준` },
      { label: "누적 사용자", value: `${cumulativeUsers}명`, change: "기간 내 고유 사번", tone: "accent", detail: "중복 사용자 제외" },
      { label: "누적 채팅", value: `${totalChats.toLocaleString()}건`, change: "사용자 질문 기준", tone: "positive", detail: "스케줄 질문 포함" },
      { label: "활성 사용자", value: `${activeUsers.length}명`, change: "활성 기준 충족", tone: "positive", detail: `${minDays}일 이상 · ${minChats}건 이상` },
    ],
    usage_by_day: usageByDay,
    active_users: activeUsers.slice(0, 5),
    active_user_count: activeUsers.length,
    active_user_rule: { min_distinct_days: minDays, min_chat_count: minChats },
    recent_usage_history: [...history].sort((left, right) => right.occurred_at.localeCompare(left.occurred_at)).slice(0, 8),
  };
}

function scheduleStatusCode(schedule) {
  const value = String(schedule?.status_code || schedule?.status || schedule?.status_label || "").trim().toLowerCase();
  return ["active", "활성"].includes(value) ? "active" : "inactive";
}

function scheduleOwnerLabel(schedule) {
  const ownerId = scheduleOwnerId(schedule);
  const rawOwner = typeof schedule?.owner === "string" ? schedule.owner.trim() : "";
  const ownerName = String(
    schedule?.owner_name
      || (schedule?.owner && typeof schedule.owner === "object" ? schedule.owner.name : "")
      || (rawOwner && rawOwner !== ownerId ? rawOwner : "")
      || "",
  ).trim();
  if (ownerName && ownerId) return `${ownerName} (${ownerId})`;
  return ownerName || ownerId || "등록자 정보 없음";
}

function formatScheduleDateTime(value, fallback) {
  const text = String(value || "").trim();
  if (!text) return fallback;
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text;
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function scheduleResponseTime(record, displayKey, isoKey, fallback) {
  const display = String(record?.[displayKey] || "").trim();
  return display || formatScheduleDateTime(record?.[isoKey], fallback);
}

function normalizeScheduleRecord(record) {
  if (!record || typeof record !== "object" || Array.isArray(record)) {
    throw new Error("스케줄 API 응답 형식을 확인하지 못했습니다.");
  }
  const id = String(record.id || record.schedule_id || "").trim();
  if (!id) throw new Error("스케줄 API 응답에 식별자가 없습니다.");

  const statusCode = scheduleStatusCode(record);
  const rawOwner = typeof record.owner === "string" ? record.owner.trim() : "";
  const ownerId = String(
    record.owner_id
      || record.owner_employee_id
      || (record.owner && typeof record.owner === "object" ? record.owner.employee_id : "")
      || (/^\d{5,}$/.test(rawOwner) ? rawOwner : "")
      || "",
  ).trim();
  const ownerName = String(
    record.owner_name
      || (record.owner && typeof record.owner === "object" ? record.owner.name : "")
      || (rawOwner && rawOwner !== ownerId ? rawOwner : "")
      || "",
  ).trim();
  const normalized = {
    ...record,
    id,
    title: String(record.title || "").trim(),
    question: String(record.question || "").trim(),
    repeat: String(record.repeat || "매일").trim(),
    time: String(record.time || "").trim(),
    interval_minutes: record.interval_minutes ?? null,
    start_time: String(record.start_time || "").trim(),
    end_time: String(record.end_time || "").trim(),
    target: SCHEDULE_DELIVERY_TARGET,
    owner_id: ownerId,
    owner_name: ownerName,
    status_code: statusCode,
    status: statusCode === "active" ? "활성" : "일시중지",
    next_run: scheduleResponseTime(record, "next_run", "next_run_at", statusCode === "active" ? "다음 실행 계산 중" : "일시중지됨"),
    last_run: scheduleResponseTime(record, "last_run", "last_run_at", "아직 실행 전"),
  };
  normalized.owner = scheduleOwnerLabel(normalized);
  return normalized;
}

function schedulesFromResponse(payload) {
  const records = Array.isArray(payload)
    ? payload
    : Array.isArray(payload?.schedules)
      ? payload.schedules
      : null;
  if (!records) throw new Error("스케줄 목록 API 응답 형식을 확인하지 못했습니다.");
  return records.map(normalizeScheduleRecord);
}

function scheduleFromResponse(payload) {
  const record = payload && typeof payload === "object" && !Array.isArray(payload)
    ? (payload.schedule || payload)
    : null;
  return normalizeScheduleRecord(record);
}

function replaceScheduleInState(schedule) {
  const index = state.portal.schedules.findIndex((item) => item.id === schedule.id);
  if (index >= 0) state.portal.schedules.splice(index, 1, schedule);
  else state.portal.schedules.unshift(schedule);
}

async function loadSchedules({ notifyOnError = false } = {}) {
  if (!state.portal) return;
  state.schedulesData = { state: "loading", message: "" };
  // Do not leave portal preview values visible while the real schedule API is
  // being requested. The backend is the only source of schedule state.
  state.portal.schedules = [];
  renderSchedules();

  try {
    const response = await fetch("/api/schedules", {
      headers: portalRequestHeaders({ Accept: "application/json" }),
      cache: "no-store",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageFromResponse(payload, "스케줄 목록을 불러오지 못했습니다."));
    }
    state.portal.schedules = schedulesFromResponse(payload);
    state.schedulesData = { state: "ready", message: "" };
  } catch (error) {
    console.warn("schedule list request failed", error);
    const message = error?.message || "스케줄 목록을 불러오지 못했습니다.";
    state.portal.schedules = [];
    state.schedulesData = { state: "error", message };
    if (notifyOnError) showToast(message);
  } finally {
    renderSchedules();
  }
}

function filteredSchedules() {
  const search = state.scheduleSearch.toLowerCase().trim();
  return (state.portal?.schedules || []).filter((schedule) => {
    const matchesScope = state.scheduleScope === "all" || scheduleOwnerId(schedule) === viewerId();
    const matchesFilter = state.scheduleFilter === "all" || schedule.status === state.scheduleFilter;
    const haystack = `${schedule.title} ${schedule.question} ${schedule.owner}`.toLowerCase();
    return matchesScope && matchesFilter && (!search || haystack.includes(search));
  });
}

function scheduleActions(schedule) {
  if (!canEditSchedule(schedule)) {
    return `
      <span class="readonly-chip">열람 전용</span>
      <button class="restricted-action" type="button" data-schedule-restricted="${escapeHtml(schedule.id)}">권한 안내</button>`;
  }
  const mutationInFlight = Boolean(state.scheduleMutationId);
  const isMutatingThisSchedule = state.scheduleMutationId === schedule.id;
  const disabledAttributes = mutationInFlight
    ? ' disabled aria-disabled="true"'
    : "";
  const pauseLabel = isMutatingThisSchedule
    ? "변경 중…"
    : (scheduleStatusCode(schedule) === "active" ? "일시중지" : "재개");
  return `
    <button class="edit-action" type="button" data-edit-schedule="${escapeHtml(schedule.id)}"${disabledAttributes}>수정</button>
    <button class="pause-action${isMutatingThisSchedule ? " is-loading" : ""}" type="button" data-toggle-schedule="${escapeHtml(schedule.id)}" aria-busy="${String(isMutatingThisSchedule)}"${disabledAttributes}>${pauseLabel}</button>
    <button class="delete-action" type="button" data-delete-schedule="${escapeHtml(schedule.id)}"${disabledAttributes}>삭제</button>`;
}

function renderSchedules() {
  if (!state.portal) return;
  const allSchedules = Array.isArray(state.portal.schedules) ? state.portal.schedules : [];
  const schedules = filteredSchedules();
  const mineCount = allSchedules.filter((schedule) => scheduleOwnerId(schedule) === viewerId()).length;
  const allCount = allSchedules.length;
  $("#schedule-count").textContent = allCount;
  $("#my-schedule-count").textContent = mineCount;
  $("#all-schedule-count").textContent = allCount;
  $$("[data-schedule-scope]").forEach((button) => {
    const isSelected = button.dataset.scheduleScope === state.scheduleScope;
    button.classList.toggle("active", isSelected);
    button.setAttribute("aria-selected", String(isSelected));
  });
  $("#schedule-scope-note").textContent = state.scheduleScope === "mine"
    ? "본인이 등록한 스케줄만 표시합니다. 이 목록에서는 수정·일시중지·삭제가 가능합니다."
    : isAdmin()
      ? "전체 스케줄을 보고 있습니다. 관리자는 모든 스케줄을 수정·일시중지·삭제할 수 있습니다."
      : "전체 스케줄은 누구나 열람할 수 있습니다. 수정·일시중지·삭제는 등록자 본인 또는 관리자만 가능합니다.";

  $("#schedule-grid").innerHTML = schedules.length
    ? schedules
        .map((schedule) => {
          const interval = isIntervalSchedule(schedule);
          const ruleLabel = scheduleRuleLabel(schedule);
          const timingLabel = interval ? scheduleWindowLabel(schedule) : schedule.next_run;
          const intervalNextRun = interval
            ? `<div><span>다음 실행</span><strong>${escapeHtml(schedule.next_run)}</strong></div>`
            : "";
          return `
        <article class="schedule-card ${interval ? "interval-schedule" : ""} ${canEditSchedule(schedule) ? "" : "readonly-schedule"}">
          <div class="schedule-card-top">
            <span class="schedule-symbol" aria-hidden="true">${svgIcon("calendar")}</span>
            <div><h3>${escapeHtml(schedule.title)}</h3><span class="schedule-id">${escapeHtml(schedule.id)}</span></div>
            ${statusPill(schedule.status)}
          </div>
          <p class="schedule-question">${escapeHtml(schedule.question)}</p>
          <div class="schedule-meta">
            <div><span>반복</span><strong>${escapeHtml(ruleLabel)}</strong></div>
            <div><span>${interval ? "실행 구간" : "다음 실행"}</span><strong class="${interval ? "interval-window" : ""}">${escapeHtml(timingLabel)}</strong></div>
            ${intervalNextRun}
            <div><span>발송 대상</span><strong>${SCHEDULE_DELIVERY_LABEL}</strong></div>
            <div><span>등록자</span><strong>${escapeHtml(schedule.owner)}</strong></div>
          </div>
          <div class="schedule-card-footer">
            <span class="last-run">최근 실행 · ${escapeHtml(schedule.last_run)}</span>
            <div class="schedule-actions">${scheduleActions(schedule)}</div>
          </div>
        </article>`;
        })
        .join("")
    : `<div class="empty-state"><strong>${state.schedulesData?.state === "loading"
      ? "스케줄 목록을 불러오는 중입니다."
      : state.schedulesData?.state === "error"
        ? "스케줄 목록을 불러오지 못했습니다."
        : "조건에 맞는 스케줄이 없습니다."}</strong><span>${state.schedulesData?.state === "error"
      ? escapeHtml(state.schedulesData.message || "잠시 후 다시 시도해 주세요.")
      : "검색어 또는 상태 필터를 변경해 보세요."}</span></div>`;
}

function liveMetadataTypeInfo(metadataType = state.metadataType) {
  const metadataTypes = state.metadataLive?.payload?.metadata_types;
  if (!metadataTypes || typeof metadataTypes !== "object") return {};
  const typeInfo = metadataTypes[metadataType];
  return typeInfo && typeof typeInfo === "object" ? typeInfo : {};
}

function liveMetadataItems(metadataType = state.metadataType) {
  const metadata = state.metadataLive?.payload?.metadata;
  if (!metadata || typeof metadata !== "object" || !Array.isArray(metadata[metadataType])) return [];
  return metadata[metadataType];
}

function metadataCollectionState(metadataType = state.metadataType) {
  const live = state.metadataLive || { state: "idle", payload: null };
  const previewItems = state.portal?.metadata?.[metadataType] || [];

  if (live.state === "loading" || live.state === "idle") {
    return { source: "loading", items: [], typeInfo: {}, payload: null };
  }
  if (live.state === "ready" && live.payload?.enabled === true) {
    const typeInfo = liveMetadataTypeInfo(metadataType);
    if (typeInfo.live === false) {
      return { source: "unavailable", items: [], typeInfo, payload: live.payload };
    }
    return {
      source: "live",
      items: liveMetadataItems(metadataType),
      typeInfo,
      payload: live.payload,
    };
  }
  if (live.state === "error") {
    return { source: "fallback", items: previewItems, typeInfo: {}, payload: null };
  }
  return { source: "preview", items: previewItems, typeInfo: liveMetadataTypeInfo(metadataType), payload: live.payload };
}

function activeMetadataItems() {
  return metadataCollectionState().items;
}

function metadataCount() {
  const collection = metadataCollectionState();
  if (collection.source !== "live") return collection.items.length;
  const count = Number(collection.typeInfo.count);
  return Number.isFinite(count) && count >= 0 ? count : collection.items.length;
}

function metadataSearchText(item) {
  const flatten = (value) => {
    if (Array.isArray(value)) return value.flatMap(flatten);
    if (value && typeof value === "object") return Object.values(value).flatMap(flatten);
    return typeof value === "string" || typeof value === "number" ? [value] : [];
  };
  return flatten(item).join(" ").toLowerCase();
}

function filteredMetadata() {
  const search = state.metadataSearch.toLowerCase().trim();
  return activeMetadataItems().filter((item) => !search || metadataSearchText(item).includes(search));
}

function metadataRecordId(item, metadataType = state.metadataType) {
  const opaqueRecordId = item?._record_id;
  if (typeof opaqueRecordId === "string" && opaqueRecordId.trim()) return opaqueRecordId.trim();
  if (typeof opaqueRecordId === "number" && Number.isFinite(opaqueRecordId)) return String(opaqueRecordId);
  const keysByType = {
    domain: ["id", "_id", "key", "domain_key"],
    table_catalog: ["id", "_id", "dataset_key", "key"],
    main_flow_filters: ["id", "_id", "filter_key", "key"],
  };
  for (const key of keysByType[metadataType] || ["id", "_id", "key"]) {
    const value = item?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}

function metadataValue(item, keys, fallback = "-") {
  for (const key of keys) {
    const value = item?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
  }
  return fallback;
}

function metadataList(item, keys) {
  for (const key of keys) {
    const value = item?.[key];
    if (Array.isArray(value)) return value.filter((entry) => entry !== null && entry !== undefined).map(String);
    if (typeof value === "string" && value.trim()) return value.split(",").map((entry) => entry.trim()).filter(Boolean);
  }
  return [];
}

function metadataStatusKey(item) {
  const raw = metadataValue(item, ["status", "state", "record_status"], "active").trim().toLowerCase();
  if (["inactive", "disabled", "deactivated", "비활성"].includes(raw)) return "inactive";
  if (["draft", "초안"].includes(raw)) return "draft";
  if (["review", "needs_input", "검토", "검토 필요"].includes(raw)) return "review";
  return "active";
}

function metadataStatus(item) {
  return {
    active: "활성",
    inactive: "비활성",
    draft: "초안",
    review: "검토 필요",
  }[metadataStatusKey(item)] || "활성";
}

function displayRequiredFilters(values) {
  const normalized = values.flatMap((value) => {
    const text = String(value || "").trim();
    const quoted = [...text.matchAll(/'([^']+)'/g)].map((match) => match[1]);
    return quoted.length ? quoted : (text ? [text] : []);
  });
  if (!normalized.length) return "없음";
  if (normalized.length <= 4) return normalized.join(", ");
  return `${normalized.slice(0, 4).join(", ")} 외 ${normalized.length - 4}개`;
}

function metadataDisplayName(item, metadataType = state.metadataType) {
  if (metadataType === "table_catalog") {
    return metadataValue(item, ["display_name", "dataset_name", "table_name", "name", "dataset_key"]);
  }
  if (metadataType === "main_flow_filters") {
    return metadataValue(item, ["display_name", "filter_name", "name", "label", "filter_key"]);
  }
  return metadataValue(item, ["display_name", "domain_name", "name", "term", "key"]);
}

function metadataActionCell(item, collection) {
  const itemId = metadataRecordId(item);
  const displayName = metadataDisplayName(item);
  const writable = isAdmin() && collection.source === "live";
  if (!itemId) {
    return `<td class="metadata-row-actions"><span class="metadata-readonly-label">상세 정보 없음</span></td>`;
  }
  const detailAction = `<button class="metadata-detail-button" type="button" data-metadata-detail="${escapeHtml(itemId)}" aria-label="${escapeHtml(displayName)} 상세 정보 보기">자세히 보기</button>`;
  if (!writable) {
    return `<td class="metadata-row-actions"><div class="metadata-row-action-buttons">${detailAction}<span class="metadata-readonly-label">읽기 전용</span></div></td>`;
  }
  const currentStatus = metadataStatusKey(item);
  const nextStatus = currentStatus === "inactive" ? "active" : "inactive";
  const actionLabel = nextStatus === "active" ? "활성화" : "비활성화";
  const mutationInFlight = Boolean(state.metadataStatusUpdating);
  const isUpdating = mutationInFlight
    && state.metadataStatusTarget?.metadataType === state.metadataType
    && state.metadataStatusTarget?.recordId === itemId;
  const disabledAttributes = mutationInFlight ? ' disabled aria-disabled="true"' : "";
  return `<td class="metadata-row-actions"><div class="metadata-row-action-buttons">${detailAction}<button class="metadata-status-button ${nextStatus}${isUpdating ? " is-loading" : ""}" type="button" data-metadata-status="${escapeHtml(itemId)}" data-next-metadata-status="${nextStatus}" aria-label="${escapeHtml(displayName)} ${actionLabel}" aria-busy="${String(isUpdating)}"${disabledAttributes}>${isUpdating ? "변경 중…" : actionLabel}</button></div></td>`;
}

const METADATA_DETAIL_HIDDEN_KEYS = new Set([
  "_id", "_record_id", "id", "owner", "created_by", "updated_by",
  "created_at", "updated_at", "last_modified", "last_run", "trace_id",
  "request_id", "message_id", "registration_trace",
]);

function metadataDetailKeyIsRestricted(key) {
  const normalized = String(key || "").trim().toLowerCase();
  if (!normalized) return false;
  if (METADATA_DETAIL_HIDDEN_KEYS.has(normalized)) return true;
  return /(password|passwd|token|secret|api[_-]?key|authorization|credential|cookie|private|access[_-]?key|bearer|mongo(?:db)?[_-]?uri|connection[_-]?string)/i.test(normalized)
    || /^(uri|url|api_url|endpoint|headers?|request_headers?|request_body)$/i.test(normalized);
}

function sanitizeMetadataDetail(value, depth = 0, key = "") {
  if (metadataDetailKeyIsRestricted(key)) return undefined;
  if (depth > 7) return "[중첩 정보 생략]";
  if (value === null || value === undefined) return value;
  if (typeof value === "string") {
    return value.length > 20_000 ? `${value.slice(0, 20_000)}\n… [길이 제한으로 일부 생략]` : value;
  }
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (Array.isArray(value)) {
    const rows = value
      .slice(0, 100)
      .map((entry) => sanitizeMetadataDetail(entry, depth + 1))
      .filter((entry) => entry !== undefined);
    if (value.length > 100) rows.push("… [항목 수 제한으로 일부 생략]");
    return rows;
  }
  if (typeof value === "object") {
    const result = {};
    Object.entries(value).slice(0, 100).forEach(([entryKey, entryValue]) => {
      const sanitized = sanitizeMetadataDetail(entryValue, depth + 1, entryKey);
      if (sanitized !== undefined) result[entryKey] = sanitized;
    });
    if (Object.keys(value).length > 100) result._truncated = "[필드 수 제한으로 일부 생략]";
    return result;
  }
  return String(value);
}

function metadataDetailFieldNames(metadataType = state.metadataType) {
  const fields = {
    domain: ["section", "key", "display_name", "status"],
    table_catalog: ["dataset_key", "display_name", "dataset_family", "source_type", "status"],
    main_flow_filters: ["filter_key", "display_name", "operator", "value_type", "value_shape", "status"],
  };
  return fields[metadataType] || [];
}

function metadataPayloadFieldNames(metadataType = state.metadataType) {
  const fields = {
    domain: [
      "display_name", "aliases", "field", "processes", "values", "description", "summary",
      "rules", "steps", "columns", "metric_semantics", "default_detail_columns", "analysis_steps",
      "function_cases",
    ],
    table_catalog: [
      "display_name", "dataset_family", "source_type", "source_config", "required_params",
      "required_filters", "required_param_mappings", "filter_mappings", "standard_column_aliases",
      "columns", "selection_criteria", "default_detail_columns", "metric_semantics",
    ],
    main_flow_filters: [
      "display_name", "aliases", "operator", "value_type", "value_shape", "description",
      "value_examples", "column_candidates", "candidate_columns", "standard_column_aliases",
      "selection_criteria",
    ],
  };
  return fields[metadataType] || [];
}

function metadataDetailRecordForDisplay(item, metadataType = state.metadataType) {
  const record = {};
  metadataDetailFieldNames(metadataType).forEach((field) => {
    if (Object.prototype.hasOwnProperty.call(item || {}, field)) record[field] = item[field];
  });
  const payload = item?.payload;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    const safePayload = {};
    metadataPayloadFieldNames(metadataType).forEach((field) => {
      if (Object.prototype.hasOwnProperty.call(payload, field)) safePayload[field] = payload[field];
    });
    if (Object.keys(safePayload).length) record.payload = safePayload;
  }
  const sanitized = sanitizeMetadataDetail(record);
  return sanitized && Object.keys(sanitized).length ? sanitized : { message: "표시할 안전한 상세 정보가 없습니다." };
}

function metadataDetailRecordFromResponse(payload) {
  if (!payload || typeof payload !== "object") return null;
  for (const key of ["item", "record", "detail", "data"]) {
    const value = payload[key];
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  }
  return payload;
}

function renderMetadataDetailModal(record, { source, message } = {}) {
  const target = state.metadataDetailTarget;
  const type = metadataTypes[target?.metadataType || state.metadataType];
  const display = metadataDetailRecordForDisplay(record || {}, target?.metadataType || state.metadataType);
  const json = JSON.stringify(display, null, 2);
  const title = target?.displayName || metadataDisplayName(record || {}, target?.metadataType || state.metadataType);
  const titleElement = $("#metadata-detail-title");
  const sourceElement = $("#metadata-detail-source");
  const messageElement = $("#metadata-detail-message");
  const jsonElement = $("#metadata-detail-json");
  const copyButton = $("#metadata-detail-copy");
  if (titleElement) titleElement.textContent = `${title || type.label} 상세 정보`;
  if (sourceElement) sourceElement.textContent = source || "상세 정보";
  if (messageElement) messageElement.textContent = message || "Flow 기반 등록 정보를 JSON 형식으로 표시합니다. 인증값과 연결 비밀 정보는 표시하지 않습니다.";
  if (jsonElement) jsonElement.textContent = json;
  if (copyButton) copyButton.disabled = !json;
}

function metadataDetailModalIsOpen() {
  const modal = $("#metadata-detail-modal");
  return Boolean(modal && !modal.hidden);
}

function openMetadataDetailModal(recordId, opener) {
  const collection = metadataCollectionState();
  const item = collection.items.find((candidate) => metadataRecordId(candidate) === recordId);
  if (!item) {
    showToast("상세 정보를 확인할 메타데이터 항목을 찾지 못했습니다. 목록을 새로고침해 주세요.");
    return;
  }

  const modal = $("#metadata-detail-modal");
  const dialog = $(".metadata-detail-dialog", modal);
  if (!modal || !dialog) return;
  state.metadataDetailTarget = {
    metadataType: state.metadataType,
    recordId,
    displayName: metadataDisplayName(item),
  };
  state.metadataDetailOpener = opener instanceof HTMLElement ? opener : document.activeElement;
  state.metadataDetailLoading = collection.source === "live";
  modal.hidden = false;
  document.body.classList.add("dialog-open");
  renderMetadataDetailModal(item, {
    source: collection.source === "live" ? "실제 MongoDB 목록" : "예시 데이터",
    message: collection.source === "live"
      ? "상세 등록 정보를 불러오는 중입니다. 인증값과 연결 비밀 정보는 표시하지 않습니다."
      : "예시 목록에 포함된 Flow 기반 등록 정보를 JSON 형식으로 표시합니다.",
  });
  window.setTimeout(() => dialog.focus(), 0);

  if (collection.source !== "live") return;
  void loadLiveMetadataDetail(state.metadataDetailTarget, item);
}

async function loadLiveMetadataDetail(target, fallbackItem) {
  try {
    const response = await fetch(
      `/api/metadata/live/${encodeURIComponent(target.metadataType)}/${encodeURIComponent(target.recordId)}`,
      { headers: portalRequestHeaders({ Accept: "application/json" }) },
    );
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageFromResponse(payload, "메타데이터 상세 정보를 불러오지 못했습니다."));
    }
    if (!metadataDetailModalIsOpen()
      || state.metadataDetailTarget?.metadataType !== target.metadataType
      || state.metadataDetailTarget?.recordId !== target.recordId) return;
    const detail = metadataDetailRecordFromResponse(payload);
    if (!detail) throw new Error("메타데이터 상세 정보 형식을 확인하지 못했습니다.");
    renderMetadataDetailModal(detail, {
      source: "실제 MongoDB 상세",
      message: "Flow가 저장한 등록 정보를 JSON 형식으로 표시합니다. 인증값과 연결 비밀 정보는 자동으로 제외됩니다.",
    });
  } catch (error) {
    console.warn("metadata detail unavailable", error);
    if (metadataDetailModalIsOpen()
      && state.metadataDetailTarget?.metadataType === target.metadataType
      && state.metadataDetailTarget?.recordId === target.recordId) {
      renderMetadataDetailModal(fallbackItem, {
        source: "목록 정보만 표시",
        message: "상세 API를 확인하지 못해 현재 목록에 표시된 안전한 정보만 보여줍니다. 상세 API 연결 상태를 확인해 주세요.",
      });
    }
  } finally {
    if (state.metadataDetailTarget?.metadataType === target.metadataType
      && state.metadataDetailTarget?.recordId === target.recordId) {
      state.metadataDetailLoading = false;
    }
  }
}

function closeMetadataDetailModal() {
  const modal = $("#metadata-detail-modal");
  if (!modal || modal.hidden) return;
  const opener = state.metadataDetailOpener;
  modal.hidden = true;
  state.metadataDetailTarget = null;
  state.metadataDetailLoading = false;
  state.metadataDetailOpener = null;
  if ($("#metadata-status-modal")?.hidden !== false) document.body.classList.remove("dialog-open");
  if (opener instanceof HTMLElement && opener.isConnected) {
    window.setTimeout(() => opener.focus(), 0);
  }
}

async function copyMetadataDetailJson() {
  const text = String($("#metadata-detail-json")?.textContent || "").trim();
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast("상세 JSON을 복사했습니다.");
  } catch (error) {
    console.warn("metadata detail copy failed", error);
    showToast("브라우저 복사 권한을 확인해 주세요.");
  }
}

function metadataTableRow(item, collection) {
  const status = metadataStatus(item);
  if (state.metadataType === "table_catalog") {
    const filters = metadataList(item, ["required_params", "required_filters", "required_conditions", "mandatory_filters"]);
    const displayName = metadataDisplayName(item);
    const datasetKey = metadataValue(item, ["dataset_key", "key", "dataset_id"]);
    const sourceType = metadataValue(item, ["source_type", "connection_type", "db_type", "source"]);
    const category = metadataValue(item, ["dataset_family", "category", "dataset_category", "data_type"]);
    return `
      <tr>
        <td><code class="metadata-key">${escapeHtml(datasetKey)}</code></td>
        <td>${escapeHtml(displayName)}</td>
        <td><span class="mini-tag">${escapeHtml(category)}</span></td>
        <td>${escapeHtml(sourceType)}</td>
        <td>${escapeHtml(displayRequiredFilters(filters))}</td>
        <td>${statusPill(status)}</td>
        ${metadataActionCell(item, collection)}
      </tr>`;
  }
  if (state.metadataType === "main_flow_filters") {
    const displayName = metadataDisplayName(item);
    const filterKey = metadataValue(item, ["filter_key", "key", "id"]);
    return `
      <tr>
        <td><code class="metadata-key">${escapeHtml(filterKey)}</code></td>
        <td>${escapeHtml(displayName)}</td>
        <td>${escapeHtml(metadataValue(item, ["operator", "default_operator"]))}</td>
        <td>${escapeHtml(metadataValue(item, ["value_type", "data_type", "type"]))}</td>
        <td>${escapeHtml(metadataValue(item, ["value_shape", "value_mode", "shape"]))}</td>
        <td>${statusPill(status)}</td>
        ${metadataActionCell(item, collection)}
      </tr>`;
  }
  const displayName = metadataDisplayName(item);
  const section = metadataValue(item, ["section_label", "section", "category", "domain_type"]);
  const domainKey = metadataValue(item, ["key", "domain_key", "id"]);
  return `
    <tr>
      <td><span class="mini-tag">${escapeHtml(section)}</span></td>
      <td><code class="metadata-key">${escapeHtml(domainKey)}</code></td>
      <td>${escapeHtml(displayName)}</td>
      <td>${statusPill(status)}</td>
      ${metadataActionCell(item, collection)}
    </tr>`;
}

function paginateMetadata(items) {
  const pageSize = Math.max(1, Number(state.metadataPageSize) || 10);
  const totalItems = items.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
  state.metadataPage = Math.min(Math.max(1, Number(state.metadataPage) || 1), totalPages);
  const start = totalItems ? (state.metadataPage - 1) * pageSize : 0;
  const end = Math.min(start + pageSize, totalItems);
  return {
    items: items.slice(start, end),
    totalItems,
    totalPages,
    page: state.metadataPage,
    start,
    end,
  };
}

function paginationPageList(totalPages, currentPage) {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, index) => index + 1);
  const pages = [1];
  const start = Math.max(2, currentPage - 1);
  const end = Math.min(totalPages - 1, currentPage + 1);
  if (start > 2) pages.push("…");
  for (let page = start; page <= end; page += 1) pages.push(page);
  if (end < totalPages - 1) pages.push("…");
  pages.push(totalPages);
  return pages;
}

function renderMetadataPagination(pagination) {
  const element = $("#metadata-pagination");
  if (!element) return;
  if (pagination.totalPages <= 1) {
    element.hidden = true;
    element.innerHTML = "";
    return;
  }

  element.hidden = false;
  const pageButtons = paginationPageList(pagination.totalPages, pagination.page)
    .map((entry) => {
      if (entry === "…") return '<span class="pagination-ellipsis" aria-hidden="true">…</span>';
      const selected = entry === pagination.page;
      return `<button class="pagination-button ${selected ? "active" : ""}" type="button" data-metadata-page="${entry}" aria-label="${entry}페이지" ${selected ? 'aria-current="page"' : ""}>${entry}</button>`;
    })
    .join("");
  element.innerHTML = `
    <p class="metadata-pagination-summary">${pagination.start + 1}–${pagination.end} / ${pagination.totalItems}건</p>
    <div class="metadata-pagination-controls">
      <button class="pagination-button pagination-direction" type="button" data-metadata-page="previous" aria-label="이전 페이지" ${pagination.page === 1 ? "disabled" : ""}>이전</button>
      ${pageButtons}
      <button class="pagination-button pagination-direction" type="button" data-metadata-page="next" aria-label="다음 페이지" ${pagination.page === pagination.totalPages ? "disabled" : ""}>다음</button>
    </div>`;
}

function renderMetadataDataNote(collection) {
  const note = $("#metadata-data-note");
  const copy = $("#metadata-data-note-copy");
  const badge = $("#metadata-source-badge");
  if (!note || !copy || !badge) return;

  note.classList.remove("is-live", "is-preview", "is-loading", "is-fallback");
  const typeInfo = collection.typeInfo || {};
  if (collection.source === "loading") {
    note.classList.add("is-loading");
    badge.textContent = "목록 확인 중";
    copy.innerHTML = "<strong>실제 목록을 불러오는 중입니다.</strong> MongoDB 읽기 전용 조회 상태를 확인하고 있습니다.";
    return;
  }
  if (collection.source === "live") {
    note.classList.add("is-live");
    badge.textContent = "실제 데이터 · 관리자 상태 변경 가능";
    const database = String(collection.payload?.source?.database || "").trim();
    const collectionName = String(typeInfo.collection || "").trim();
    const source = [database, collectionName].filter(Boolean).join(".") || "MongoDB 컬렉션";
    const returned = Number(typeInfo.returned_count);
    const count = Number(typeInfo.count);
    const renderedCount = Number.isFinite(returned) ? returned : collection.items.length;
    const fullCount = Number.isFinite(count) ? count : renderedCount;
    const truncation = typeInfo.truncated ? " 일부 항목만 표시합니다." : "";
    copy.innerHTML = `<strong>실제 MongoDB 목록을 표시합니다.</strong> <code>${escapeHtml(source)}</code>에서 ${escapeHtml(fullCount)}건 중 ${escapeHtml(renderedCount)}건을 불러왔습니다. 상태 변경은 관리자 확인 후에만 실행되며, 원본 항목은 유지됩니다.${truncation}`;
    return;
  }
  if (collection.source === "fallback") {
    note.classList.add("is-fallback");
    badge.textContent = "예시 데이터";
    copy.innerHTML = "<strong>실제 목록을 지금 확인하지 못했습니다.</strong> 화면 흐름 확인을 위해 예시 데이터를 표시하며, MongoDB의 실제 내용과 다를 수 있습니다.";
    return;
  }
  if (collection.source === "unavailable") {
    note.classList.add("is-fallback");
    badge.textContent = "실조회 설정 필요";
    copy.innerHTML = "<strong>이 유형의 실제 MongoDB 읽기 설정이 준비되지 않았습니다.</strong> 예시 데이터로 대체하지 않고 빈 목록으로 표시합니다.";
    return;
  }
  note.classList.add("is-preview");
  badge.textContent = "예시 데이터";
  copy.innerHTML = "<strong>실제 목록 읽기가 아직 설정되지 않았습니다.</strong> 아래는 화면 확인용 예시이며, MongoDB에 저장된 실제 메타데이터 조회 결과가 아닙니다.";
}

function renderMetadata() {
  if (!isAdmin()) return;
  const type = metadataTypes[state.metadataType];
  const collection = metadataCollectionState();
  const filteredItems = filteredMetadata();
  const pagination = paginateMetadata(filteredItems);
  const allItems = activeMetadataItems();
  $("#metadata-type-title").textContent = type.label;
  $("#metadata-type-description").textContent = type.description;
  $("#metadata-total-label").textContent = type.totalLabel;
  $("#metadata-total").textContent = metadataCount();
  const hasExplicitStatus = allItems.some((item) => ["status", "state", "record_status"].some((key) => String(item?.[key] || "").trim()));
  $("#metadata-active-total").textContent = hasExplicitStatus
    ? allItems.filter((item) => metadataStatus(item) === "활성").length
    : "-";
  $("#metadata-review-total").textContent = hasExplicitStatus
    ? allItems.filter((item) => metadataStatus(item) === "검토 필요").length
    : "-";
  $("#metadata-summary-note").textContent = collection.source === "live"
    ? `${type.label} 실제 목록을 확인하고, 필요한 항목은 활성 또는 비활성 상태로 관리하세요.`
    : `${type.label} 등록 전 필수 항목과 기존 항목을 확인하세요.`;
  $("#metadata-filter-hint").textContent = type.filterHint;
  $("#metadata-table-head").innerHTML = `<tr>${type.headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr>`;
  $("#metadata-list").innerHTML = pagination.items.length
    ? pagination.items.map((item) => metadataTableRow(item, collection)).join("")
    : `<tr><td colspan="${type.headers.length}" class="empty-cell">${collection.source === "loading" ? "실제 메타데이터 목록을 불러오는 중입니다." : `조건에 맞는 ${escapeHtml(type.label)} 항목이 없습니다.`}</td></tr>`;
  renderMetadataPagination(pagination);
  $$("[data-metadata-type]").forEach((button) => {
    const isSelected = button.dataset.metadataType === state.metadataType;
    button.classList.toggle("active", isSelected);
    button.setAttribute("aria-selected", String(isSelected));
  });
  renderMetadataDataNote(collection);
  renderMetadataAuthoring();
}

function openMetadataStatusModal(recordId, nextStatus, opener) {
  if (!isAdmin()) {
    showToast("관리자만 메타데이터 상태를 변경할 수 있습니다.");
    return;
  }
  if (state.metadataStatusUpdating) {
    showToast("메타데이터 상태를 변경하고 있습니다. 잠시만 기다려 주세요.");
    return;
  }
  const collection = metadataCollectionState();
  if (collection.source !== "live") {
    showToast("예시 또는 미연결 목록은 상태를 변경할 수 없습니다. 실제 MongoDB 목록에서만 변경할 수 있습니다.");
    return;
  }
  const item = collection.items.find((candidate) => metadataRecordId(candidate) === recordId);
  if (!item) {
    showToast("상태를 변경할 메타데이터 항목을 찾지 못했습니다. 목록을 새로고침해 주세요.");
    return;
  }

  const normalizedNextStatus = nextStatus === "inactive" ? "inactive" : "active";
  const modal = $("#metadata-status-modal");
  const dialog = $(".metadata-status-dialog", modal);
  const kicker = $("#metadata-status-kicker");
  const title = $("#metadata-status-title");
  const message = $("#metadata-status-message");
  const confirmButton = $("#metadata-status-confirm");
  if (!modal || !dialog || !kicker || !title || !message || !confirmButton) return;

  const displayName = metadataDisplayName(item);
  const actionLabel = normalizedNextStatus === "active" ? "활성화" : "비활성화";
  state.metadataStatusTarget = {
    metadataType: state.metadataType,
    recordId,
    displayName,
    nextStatus: normalizedNextStatus,
  };
  state.metadataStatusOpener = opener instanceof HTMLElement ? opener : document.activeElement;
  dialog.classList.toggle("is-activate", normalizedNextStatus === "active");
  kicker.textContent = normalizedNextStatus === "active" ? "REACTIVATE METADATA" : "PAUSE METADATA";
  title.textContent = `“${displayName}” 항목을 ${actionLabel}하시겠습니까?`;
  message.textContent = normalizedNextStatus === "active"
    ? "다시 활성 상태로 변경하면 일반 Agent 조회 대상에 포함됩니다. MongoDB의 원본 항목은 그대로 유지됩니다."
    : "비활성 상태로 변경하면 일반 Agent 조회 대상에서 제외됩니다. MongoDB의 원본 항목은 그대로 유지됩니다.";
  confirmButton.textContent = actionLabel;
  confirmButton.classList.toggle("is-activate", normalizedNextStatus === "active");
  modal.hidden = false;
  document.body.classList.add("dialog-open");
  window.setTimeout(() => dialog.focus(), 0);
}

function closeMetadataStatusModal({ force = false } = {}) {
  if (state.metadataStatusUpdating && !force) return;
  const modal = $("#metadata-status-modal");
  if (!modal || modal.hidden) return;
  const opener = state.metadataStatusOpener;
  modal.hidden = true;
  if ($("#metadata-detail-modal")?.hidden !== false) document.body.classList.remove("dialog-open");
  state.metadataStatusTarget = null;
  state.metadataStatusOpener = null;
  if (opener instanceof HTMLElement && opener.isConnected) {
    window.setTimeout(() => opener.focus(), 0);
  }
}

function updateLiveMetadataRecordStatus(target, responsePayload) {
  const payload = state.metadataLive?.payload;
  const items = payload?.metadata?.[target.metadataType];
  if (!Array.isArray(items)) return false;
  const index = items.findIndex((item) => metadataRecordId(item, target.metadataType) === target.recordId);
  if (index < 0) return false;
  const updatedRecord = ["item", "record", "detail", "data"]
    .map((key) => responsePayload?.[key])
    .find((value) => value && typeof value === "object" && !Array.isArray(value));
  if (updatedRecord && typeof updatedRecord === "object" && !Array.isArray(updatedRecord)) {
    items[index] = { ...items[index], ...updatedRecord, status: target.nextStatus };
  } else {
    items[index] = { ...items[index], status: target.nextStatus };
  }
  return true;
}

async function confirmMetadataStatusUpdate() {
  const target = state.metadataStatusTarget;
  if (!target || state.metadataStatusUpdating) return;
  const collection = metadataCollectionState(target.metadataType);
  if (!isAdmin() || collection.source !== "live") {
    closeMetadataStatusModal();
    showToast("실제 MongoDB 목록에서 관리자만 메타데이터 상태를 변경할 수 있습니다.");
    return;
  }

  const confirmButton = $("#metadata-status-confirm");
  const statusMessage = $("#metadata-status-message");
  const actionLabel = target.nextStatus === "active" ? "활성화" : "비활성화";
  state.metadataStatusUpdating = true;
  renderMetadata();
  if (confirmButton) {
    confirmButton.disabled = true;
    confirmButton.setAttribute("aria-busy", "true");
    confirmButton.textContent = "변경 중…";
  }
  if (statusMessage) {
    statusMessage.textContent = "상태 변경을 처리하고 있습니다. 완료될 때까지 잠시만 기다려 주세요.";
  }

  try {
    const response = await fetch(
      `/api/metadata-authoring/${encodeURIComponent(target.metadataType)}/${encodeURIComponent(target.recordId)}/status`,
      {
        method: "PATCH",
        headers: portalRequestHeaders({ Accept: "application/json", "Content-Type": "application/json" }),
        body: JSON.stringify({ status: target.nextStatus }),
      },
    );
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const fallback = response.status === 403
        ? "관리자만 메타데이터 상태를 변경할 수 있습니다."
        : "메타데이터 상태 변경에 실패했습니다.";
      throw new Error(errorMessageFromResponse(payload, fallback));
    }
    updateLiveMetadataRecordStatus(target, payload);
    state.metadataStatusUpdating = false;
    closeMetadataStatusModal({ force: true });
    renderMetadata();
    showToast(`${target.displayName} 항목을 ${actionLabel}했습니다. 원본 항목은 유지됩니다.`);
  } catch (error) {
    console.error(error);
    const message = error?.message || "메타데이터 상태를 변경하지 못했습니다.";
    if (statusMessage) statusMessage.textContent = message;
    showToast(message);
  } finally {
    state.metadataStatusUpdating = false;
    if (confirmButton && !$("#metadata-status-modal")?.hidden) {
      confirmButton.disabled = false;
      confirmButton.setAttribute("aria-busy", "false");
      confirmButton.textContent = actionLabel;
    }
    renderMetadata();
  }
}

function authoringData() {
  return state.portal?.metadata_authoring || { contract: {}, examples: {}, recent_results: [] };
}

function activeAuthoringExample() {
  return authoringData().examples?.[state.metadataType] || null;
}

function metadataExampleRawText(example) {
  const tableCatalogFallback = [
    "dataset_key: production_today",
    "표시명: Production Today",
    "분류: production",
    "source_type: oracle",
    "db_key: PNT_RPT",
    "",
    "query_template:",
    "SELECT",
    "  WORK_DATE,",
    "  OPER_NAME,",
    "  PRODUCTION",
    "FROM PROD_TABLE",
    "WHERE WORK_DATE = {DATE}",
    "  AND OPER_NAME = {PROCESS_GROUP}",
    "",
    "columns:",
    "- WORK_DATE",
    "- OPER_NAME",
    "- PRODUCTION",
    "",
    "required_param_mappings:",
    "- DATE -> WORK_DATE",
    "- PROCESS_GROUP -> OPER_NAME",
    "",
    "filter_mappings:",
    "- DATE -> WORK_DATE",
    "- PROCESS_GROUP -> OPER_NAME",
  ].join("\n");
  const rawText = String(example?.raw_text || (state.metadataType === "table_catalog" ? tableCatalogFallback : "")).trim();
  if (state.metadataType !== "table_catalog") return rawText;

  const additions = [];
  if (!/(query_template|조회\s*쿼리|\bselect\b)/i.test(rawText)) {
    additions.push(
      "조회 쿼리(query_template) 예시는 다음과 같습니다.\n"
      + "SELECT\n"
      + "  WORK_DATE,\n"
      + "  OPER_NAME,\n"
      + "  PRODUCTION\n"
      + "FROM PROD_TABLE\n"
      + "WHERE WORK_DATE = {DATE}\n"
      + "  AND OPER_NAME = {PROCESS_GROUP}",
    );
  }
  if (!/(filter_mappings|필터\s*[·/]?\s*컬럼\s*매핑|required_param_mappings)/i.test(rawText)) {
    additions.push(
      "필터·컬럼 매핑(filter_mappings)은 DATE → WORK_DATE, PROCESS_GROUP → OPER_NAME 입니다. "
      + "필수 조건 매핑(required_param_mappings)도 DATE → WORK_DATE, PROCESS_GROUP → OPER_NAME으로 저장합니다. "
      + "조회 컬럼(columns)은 WORK_DATE, OPER_NAME, PRODUCTION 입니다.",
    );
  }
  if (!additions.length) return rawText;
  return [rawText, ...additions].filter(Boolean).join("\n\n");
}

function metadataExampleRequiredInput(example) {
  const fields = Array.isArray(example?.required_input) ? [...example.required_input] : [];
  if (state.metadataType === "table_catalog") {
    [
      { field: "조회 쿼리(query_template)", pattern: /(query|쿼리)/i },
      { field: "조회 컬럼(columns)", pattern: /(column|컬럼)/i },
      { field: "필터·컬럼 매핑(filter_mappings)", pattern: /(mapping|매핑)/i },
    ].forEach(({ field, pattern }) => {
      if (!fields.some((value) => pattern.test(String(value)))) fields.push(field);
    });
  }
  return fields.join(" · ");
}

function cloneValue(value) {
  return JSON.parse(JSON.stringify(value));
}

function metadataApiState() {
  return state.metadataApi || { mode: "unavailable", ready: false, preview_only: false, missing: [] };
}

const metadataConnectionTypes = [
  { key: "domain", label: "도메인 정보 등록 Flow" },
  { key: "table_catalog", label: "데이터 카탈로그 등록 Flow" },
  { key: "main_flow_filters", label: "메인 플로우 필터 등록 Flow" },
];

function metadataTypeStatus(api, metadataType) {
  const types = api?.metadata_types;
  if (types && typeof types === "object" && types[metadataType] && typeof types[metadataType] === "object") {
    return types[metadataType];
  }
  return {};
}

function endpointSourceLabel(source) {
  return {
    type_specific_url: "유형별 API 주소",
    common_url: "공통 API 주소",
  }[source] || "API 주소";
}

function metadataStorageDetail(metadataType, api, typeStatus) {
  const livePayload = state.metadataLive?.payload;
  const liveType = liveMetadataTypeInfo(metadataType);
  if (state.metadataLive?.state === "ready" && livePayload?.enabled === true && liveType.live !== false) {
    const database = String(livePayload?.source?.database || "").trim();
    const collection = String(liveType.collection || "").trim();
    const count = Number(liveType.count);
    const destination = [database, collection].filter(Boolean).join(".") || "실제 MongoDB 컬렉션";
    const countLabel = Number.isFinite(count) ? ` · ${count}건 확인` : "";
    return `실제 목록 읽기 확인: ${destination}${countLabel} · 읽기 전용`;
  }
  const mongo = api?.flow_metadata_mongodb && typeof api.flow_metadata_mongodb === "object"
    ? api.flow_metadata_mongodb
    : (api?.mongodb && typeof api.mongodb === "object" ? api.mongodb : {});
  const database = String(mongo.database || "").trim();
  const expectedCollection = String(typeStatus.expected_flow_collection_name || "").trim();
  const portalCollection = String(typeStatus.portal_configured_collection_name || "").trim();

  if (typeStatus.writer_tweak_will_be_sent && expectedCollection) {
    const destination = database ? `${database}.${expectedCollection}` : expectedCollection;
    return `Flow에 MongoDB Writer 설정 전달 예정: ${destination} (컬렉션 존재·내용은 미확인)`;
  }
  if (portalCollection) {
    return `포털 계산명: ${portalCollection} · Flow 전달 안 함 · 실제 저장 위치 미확인`;
  }
  return "MongoDB Writer 설정은 Flow에 전달하지 않으며, 포털은 실제 저장 내용을 확인하지 않습니다.";
}

function metadataConnectionItem(metadataType, label) {
  const api = metadataApiState();
  const apiConfig = api?.api && typeof api.api === "object" ? api.api : {};
  const typeStatus = metadataTypeStatus(api, metadataType);
  const endpointConfigured = typeStatus.endpoint_configured === true
    || apiConfig.endpoint_configured?.[metadataType] === true;
  const endpointReady = typeStatus.endpoint_ready === true
    || (Object.keys(typeStatus).length === 0 && api.ready === true && endpointConfigured);
  const componentConfigured = apiConfig.component_map_configured?.[metadataType] === true
    && apiConfig.api_terminal_configured?.[metadataType] === true;
  const authenticationConfigured = Boolean(apiConfig.auth_key_configured || apiConfig.bearer_token_configured);
  const callerConfigured = apiConfig.gaia_api_caller_employee_id_configured === true;
  const mongo = api?.mongodb && typeof api.mongodb === "object" ? api.mongodb : {};

  if (!state.metadataApi) {
    return {
      label,
      detail: "서버의 메타데이터 상태 정보를 불러오지 못했습니다.",
      status: "상태 확인 불가",
      icon: "question",
      tone: "unknown",
    };
  }
  if (api.preview_only || api.mode === "preview") {
    return {
      label,
      detail: "미리보기 모드입니다. 외부 Flow와 MongoDB에는 요청하지 않습니다.",
      status: "미리보기",
      icon: "clock",
      tone: "preview",
    };
  }
  if (!endpointConfigured) {
    return {
      label,
      detail: "이 Flow의 API 주소가 설정되지 않았습니다.",
      status: "API URL 미설정",
      icon: "alert",
      tone: "attention",
    };
  }
  if (!authenticationConfigured) {
    return {
      label,
      detail: "공통 API 인증 키 또는 Bearer 토큰 설정이 필요합니다.",
      status: "인증 정보 필요",
      icon: "alert",
      tone: "attention",
    };
  }
  if (!callerConfigured) {
    return {
      label,
      detail: "관리자 설정에서 GAIA API 호출 권한 사번을 등록해 주세요.",
      status: "호출 사번 필요",
      icon: "alert",
      tone: "attention",
    };
  }
  if (mongo.tweaks_enabled && !mongo.writer_tweaks_configured) {
    return {
      label,
      detail: "MongoDB Writer 전달을 사용하도록 했지만 DB 연결 정보가 부족합니다.",
      status: "MongoDB 설정 필요",
      icon: "alert",
      tone: "attention",
    };
  }
  if (!componentConfigured) {
    return {
      label,
      detail: "Langflow 입력 또는 API 응답 컴포넌트 설정을 확인해 주세요.",
      status: "Flow 구성 확인",
      icon: "alert",
      tone: "attention",
    };
  }
  if (!endpointReady || !api.ready) {
    const missing = Array.isArray(api.missing) && api.missing.length
      ? `확인 항목: ${api.missing.join(", ")}`
      : "서버의 공통 API 설정을 확인해 주세요.";
    return {
      label,
      detail: missing,
      status: "설정 필요",
      icon: "alert",
      tone: "attention",
    };
  }
  return {
    label,
      detail: `${endpointSourceLabel(typeStatus.endpoint_source)}·인증·호출 사번 설정 완료 · ${metadataStorageDetail(metadataType, api, typeStatus)}`,
      status: "요청 가능",
      icon: "check",
    tone: "ready",
  };
}

function mongodbConnectionItem() {
  const api = metadataApiState();
  // The server returns only safe connection facts here.  Never infer or show a
  // MongoDB URI, credentials, collection contents, or other secret settings.
  const connection = api?.portal_mongodb_connection && typeof api.portal_mongodb_connection === "object"
    ? api.portal_mongodb_connection
    // Keep the status row useful while a server update is rolling out.  The
    // fallback is the older safe response shape and exposes the same facts.
    : (api?.portal_settings_mongodb && typeof api.portal_settings_mongodb === "object"
      ? api.portal_settings_mongodb
      : {});
  const configured = connection.configured === true;
  const connected = connection.connected === true || connection.connection_read_verified === true;
  const database = String(connection.database || "").trim();
  const serverMessage = String(connection.message || "").trim();
  const databaseDetail = database ? ` · ${database} 데이터베이스` : "";

  if (!state.metadataApi) {
    return {
      label: "MongoDB 연결 상태",
      detail: "서버의 MongoDB 연결 상태를 불러오지 못했습니다.",
      status: "확인 필요",
      icon: "question",
      tone: "unknown",
    };
  }

  if (!configured) {
    return {
      label: "MongoDB 연결 상태",
      detail: `${serverMessage || "MongoDB 연결 정보가 설정되지 않았습니다."}${databaseDetail}`,
      status: "미설정",
      icon: "database",
      tone: "attention",
    };
  }

  if (!connected) {
    return {
      label: "MongoDB 연결 상태",
      detail: `${serverMessage || "MongoDB 연결 정보를 확인했지만 현재 연결을 검증하지 못했습니다."}${databaseDetail}`,
      status: "확인 필요",
      icon: "alert",
      tone: "attention",
    };
  }

  return {
    label: "MongoDB 연결 상태",
    detail: `${serverMessage || "MongoDB 연결을 확인했습니다."}${databaseDetail}`,
    status: "정상",
    icon: "database",
    tone: "ready",
  };
}

function metadataConnectionItems() {
  return [
    mongodbConnectionItem(),
    ...metadataConnectionTypes.map(({ key, label }) => metadataConnectionItem(key, label)),
  ];
}

function renderMetadataApiIndicator() {
  const label = $("#portal-runtime-label");
  const copy = $("#portal-runtime-copy");
  if (!label || !copy) return;
  const usage = dashboardUsageState();
  const dashboardCopy = usage.state === "live"
    ? "대시보드 사용 이력은 Phoenix에서 실제 조회합니다."
    : usage.state === "preview"
      ? "대시보드 사용 이력은 현재 미리보기 데이터입니다."
      : usage.state === "error"
        ? "Phoenix 사용 이력은 현재 표시하지 않습니다. 다시 조회해 주세요."
        : "대시보드 사용 이력을 확인하고 있습니다.";
  if (!isAdmin()) {
    label.textContent = usage.state === "live" ? "Phoenix 사용 이력 연결됨" : "포털 화면 미리보기";
    copy.textContent = `${dashboardCopy} 메타데이터와 설정은 관리자에게만 제공됩니다.`;
    return;
  }
  const api = metadataApiState();
  if (api.preview_only || api.mode === "preview") {
    label.textContent = "메타데이터 미리보기 모드";
    copy.textContent = `${dashboardCopy} 메타데이터는 외부 API 없이 안전한 미리보기로 실행됩니다.`;
    return;
  }
  if (api.ready) {
    label.textContent = usage.state === "live" ? "Phoenix 이력 · 메타데이터 API 연결됨" : "메타데이터 API 연결 준비됨";
    copy.textContent = `${dashboardCopy} 메타데이터 등록은 외부 Flow API로 실행됩니다.`;
    return;
  }
  label.textContent = "메타데이터 API 설정 필요";
  copy.textContent = `${dashboardCopy} 메타데이터 API는 서버 .env 설정 후 실행할 수 있습니다.`;
}

function errorMessageFromResponse(payload, fallback) {
  if (!payload || typeof payload !== "object") return fallback;
  const detail = payload.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object" && typeof detail.message === "string") return detail.message;
  if (typeof payload.message === "string") return payload.message;
  return fallback;
}

async function loadAdminSettings({ notifyOnError = false } = {}) {
  if (!isAdmin()) {
    state.adminSettings = null;
    state.adminSettingsLoading = false;
    state.adminSettingsError = "";
    return;
  }

  state.adminSettingsLoading = true;
  state.adminSettingsError = "";
  renderSettings();
  try {
    const response = await fetch("/api/admin/settings", {
      headers: portalRequestHeaders(),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageForAdminResponse(response.status, payload, "관리자 설정을 불러오지 못했습니다."));
    }
    state.adminSettings = normalizeAdminSettings(payload);
    applyAdminSettingsToPortal(state.adminSettings);
  } catch (error) {
    console.error("admin settings request failed", error);
    state.adminSettings = null;
    state.adminSettingsError = error?.message || "관리자 설정을 불러오지 못했습니다.";
    if (notifyOnError) showToast(state.adminSettingsError);
  } finally {
    state.adminSettingsLoading = false;
    renderSettings();
  }
}

async function loadMetadataApiStatus() {
  if (!isAdmin()) {
    state.metadataApi = null;
    return;
  }

  try {
    const response = await fetch("/api/metadata-authoring/status", {
      headers: portalRequestHeaders(),
    });
    if (!response.ok) throw new Error("metadata authoring status request failed");
    state.metadataApi = await response.json();
  } catch (error) {
    // The existing dashboard, schedule, and employee-preview data must remain
    // available even when only the metadata API status route is unavailable.
    console.warn("metadata authoring status unavailable", error);
    state.metadataApi = null;
  }
}

async function loadLiveMetadata() {
  if (!isAdmin()) {
    state.metadataLive = { state: "idle", payload: null };
    return;
  }

  state.metadataPage = 1;
  state.metadataLive = { state: "loading", payload: null };
  renderMetadata();
  try {
    const response = await fetch("/api/metadata/live", {
      headers: portalRequestHeaders(),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload || typeof payload !== "object") {
      throw new Error("live metadata request failed");
    }
    state.metadataLive = { state: "ready", payload };
  } catch (error) {
    // Never surface infrastructure details in the browser. The screen keeps a
    // clearly labelled preview fallback until the next successful refresh.
    console.warn("live metadata unavailable", error);
    state.metadataLive = { state: "error", payload: null };
  } finally {
    renderMetadata();
  }
}

async function refreshAdminConfiguration() {
  if (!isAdmin()) {
    showToast("관리자만 설정을 확인할 수 있습니다.");
    return;
  }
  await Promise.all([
    loadAdminSettings({ notifyOnError: true }),
    loadMetadataApiStatus(),
    loadLiveMetadata(),
  ]);
  renderMetadataApiIndicator();
  renderSettings();
  if (!state.adminSettingsError) showToast("관리자 설정을 새로고침했습니다.");
}

async function saveUsagePolicy(form) {
  if (!isAdmin()) {
    showToast("관리자만 설정을 변경할 수 있습니다.");
    return;
  }

  const values = Object.fromEntries(new FormData(form));
  const usagePolicy = {
    active_user_min_distinct_days: Math.max(1, Number(values.minDays) || 1),
    active_user_min_chat_count: Math.max(1, Number(values.minChats) || 1),
  };
  const submitButton = form.querySelector("button[type='submit']");
  submitButton.disabled = true;
  submitButton.textContent = "저장 중…";
  try {
    const response = await fetch("/api/admin/settings", {
      method: "PUT",
      headers: portalRequestHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ usage_policy: usagePolicy }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageForAdminResponse(response.status, payload, "활성 사용자 기준을 저장하지 못했습니다."));
    }

    state.adminSettings = normalizeAdminSettings(payload);
    applyAdminSettingsToPortal(state.adminSettings);
    // Re-read the complete server-side Phoenix window after changing the
    // policy.  Rebuilding from the recent table would drop zero-usage dates
    // and turn a 21-day chart into a shorter, misleading period.
    await loadDashboardUsage({ notifyOnError: true });
    renderSettings();
    showToast("활성 사용자 기준을 저장했습니다.");
  } catch (error) {
    console.error("usage policy update failed", error);
    showToast(error?.message || "활성 사용자 기준을 저장하지 못했습니다.");
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "기준 적용";
  }
}

async function saveGaiaApiCaller(form) {
  if (!isAdmin()) {
    showToast("관리자만 설정을 변경할 수 있습니다.");
    return;
  }

  const employeeId = String(new FormData(form).get("gaia_api_caller_employee_id") || "").trim();
  if (!employeeId) {
    showToast("GAIA API 호출 권한 사번을 입력해 주세요.");
    $("#gaia-api-caller-employee-id")?.focus();
    return;
  }

  const submitButton = $("#gaia-api-caller-save");
  submitButton.disabled = true;
  submitButton.textContent = "저장 중…";
  state.adminSettingsError = "";
  try {
    const response = await fetch("/api/admin/settings", {
      method: "PUT",
      headers: portalRequestHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ gaia_api_caller_employee_id: employeeId }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageForAdminResponse(response.status, payload, "GAIA API 호출 권한 사번을 저장하지 못했습니다."));
    }

    state.adminSettings = normalizeAdminSettings(payload);
    if (!state.adminSettings.gaia_api_caller_employee_id) {
      state.adminSettings.gaia_api_caller_employee_id = employeeId;
    }
    applyAdminSettingsToPortal(state.adminSettings);
    showToast("GAIA API 호출 권한 사번을 저장했습니다.");
  } catch (error) {
    console.error("admin settings update failed", error);
    state.adminSettingsError = error?.message || "GAIA API 호출 권한 사번을 저장하지 못했습니다.";
    showToast(state.adminSettingsError);
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "권한 사번 저장";
    renderSettings();
  }
}

function defaultMetadataResult() {
  const recent = (authoringData().recent_results || []).find((run) => run.metadata_type === state.metadataType);
  const example = activeAuthoringExample();
  const response = recent?.result || example?.result;
  if (!response) return null;
  return {
    metadataType: state.metadataType,
    runId: recent?.id || "EXAMPLE-RUN",
    requestedAt: recent?.requested_at || "기본 예시",
    requestedBy: recent?.requested_by || "예시 데이터",
    previewOnly: true,
    requestedDryRun: true,
    response: cloneValue(response),
  };
}

function activeMetadataResult() {
  if (state.metadataResult?.metadataType === state.metadataType) return state.metadataResult;
  return defaultMetadataResult();
}

function flowResultStatus(status) {
  const labels = {
    saved: "저장 완료",
    completed: "처리 완료",
    success: "처리 완료",
    dry_run: "테스트 실행",
    needs_input: "보완 필요",
    skipped: "저장 건너뜀",
    error: "오류",
    not_saved: "미저장",
  };
  const tone = {
    saved: "status-success",
    completed: "status-success",
    success: "status-success",
    dry_run: "status-draft",
    needs_input: "status-paused",
    skipped: "status-paused",
    error: "status-paused",
    not_saved: "status-draft",
  };
  return { label: labels[status] || String(status || "결과 확인"), tone: tone[status] || "status-draft" };
}

function renderResultTable(data) {
  const columns = Array.isArray(data?.columns) ? data.columns : [];
  const rows = Array.isArray(data?.rows) ? data.rows : [];
  if (!columns.length) return `<p class="result-empty">표시할 생성 후보가 없습니다.</p>`;
  return `
    <div class="result-table-wrap"><table class="result-table"><thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>
    <tbody>${rows.length ? rows.map((row) => `<tr>${columns.map((column) => `<td>${escapeHtml(row[column] ?? "-")}</td>`).join("")}</tr>`).join("") : `<tr><td colspan="${columns.length}" class="empty-cell">생성된 후보가 없습니다.</td></tr>`}</tbody></table></div>`;
}

function textList(items, emptyText) {
  const values = Array.isArray(items) ? items.filter(Boolean) : [];
  return values.length
    ? `<ul class="result-text-list">${values.map((item) => `<li>${escapeHtml(typeof item === "string" ? item : item.message || item.title || JSON.stringify(item))}</li>`).join("")}</ul>`
    : `<p class="result-empty">${escapeHtml(emptyText)}</p>`;
}

function compactFlowResultText(value, fallback = "") {
  const text = String(value ?? "").trim();
  if (!text) return fallback;

  // The Flow message may be Markdown.  The compact summary intentionally uses
  // plain text so a full table or link list does not turn into one long line.
  const compact = text
    .replace(/```[\s\S]*?```/g, " 코드 블록 ")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/[>*_`~]/g, " ")
    .replace(/\|/g, " · ")
    .replace(/\s+/g, " ")
    .trim();
  if (!compact) return fallback;
  return compact.length > 180 ? `${compact.slice(0, 177).trimEnd()}…` : compact;
}

function metadataResultCount(authoring, response) {
  const candidates = [
    authoring?.generated_count,
    response?.data?.row_count,
    Array.isArray(response?.data?.rows) ? response.data.rows.length : null,
  ];
  for (const value of candidates) {
    const count = Number(value);
    if (Number.isFinite(count) && count >= 0) return count;
  }
  return 0;
}

function metadataResultProcess(response, authoring, write, run) {
  const status = String(response?.status || write?.status || authoring?.status || "").trim();
  if (run?.requestedDryRun || write?.dry_run || authoring?.dry_run || status === "dry_run") return "저장 전 테스트 실행";
  if (status === "saved" || Number(write?.saved_count || 0) > 0) return "MongoDB 저장 완료";
  if (status === "needs_input") return "입력 보완 확인";
  if (status === "skipped") return "중복 정책에 따라 저장 건너뜀";
  if (status === "error") return "오류 내용 확인";
  if (write?.ready_to_save) return "저장 요청 처리";
  return "Flow 결과 검토";
}

function renderMetadataResultOverview(response, authoring, write, run, resultStatus) {
  const overview = $("#metadata-result-message");
  if (!overview) return;

  const flowSummary = response?.answer_sections?.summary || {};
  const message = compactFlowResultText(
    flowSummary.description || flowSummary.headline || response?.message,
    "등록 결과를 구조화해 표시합니다.",
  );
  const summaryItems = [
    ["상태", resultStatus.label],
    ["생성 후보", `${metadataResultCount(authoring, response)}건`],
    ["처리", metadataResultProcess(response, authoring, write, run)],
  ];

  // Do not use the Flow response as HTML.  It is external text and is rendered
  // only via textContent in this compact, human-readable overview.
  overview.replaceChildren();
  const messageElement = document.createElement("p");
  messageElement.textContent = message;
  const list = document.createElement("dl");
  list.className = "metadata-result-summary-list";
  summaryItems.forEach(([label, value]) => {
    const item = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = value;
    item.append(term, description);
    list.append(item);
  });
  overview.append(messageElement, list);
}

function renderRawMetadataResponse(content, response) {
  content.replaceChildren();

  const heading = document.createElement("div");
  heading.className = "result-section-heading";
  const headingCopy = document.createElement("div");
  const title = document.createElement("h4");
  title.textContent = "원본 응답";
  const description = document.createElement("p");
  description.textContent = "구조화된 원본 JSON은 필요할 때만 펼쳐서 확인할 수 있습니다.";
  const tag = document.createElement("span");
  tag.className = "mini-tag";
  tag.textContent = "api_response";
  headingCopy.append(title, description);
  heading.append(headingCopy, tag);

  const details = document.createElement("details");
  details.className = "raw-response-details";
  const summary = document.createElement("summary");
  summary.textContent = "원본 JSON 펼쳐 보기";
  const pre = document.createElement("pre");
  pre.className = "api-json";
  pre.textContent = JSON.stringify(response, null, 2);
  details.append(summary, pre);
  content.append(heading, details);
}

function renderMetadataResult() {
  const run = activeMetadataResult();
  const content = $("#metadata-result-content");
  if (!run || !content) return;

  const response = run.response || {};
  const authoring = response.metadata_authoring || {};
  const write = response.write_result || {};
  const validation = authoring.contract_validation || {};
  const resultStatus = flowResultStatus(response.status || write.status || authoring.status);
  const isSaveRequestPreview = run.previewOnly && !run.requestedDryRun;

  renderMetadataResultOverview(response, authoring, write, run, resultStatus);
  const statusElement = $("#metadata-result-status");
  statusElement.className = `status-pill ${resultStatus.tone}`;
  statusElement.textContent = resultStatus.label;
  $("#metadata-result-run").textContent = `${run.runId} · ${run.requestedAt}`;

  if (state.metadataResultTab === "candidates") {
    content.innerHTML = `
      <div class="result-section-heading"><div><h4>생성 후보</h4><p>Flow의 <code>data.columns</code>, <code>data.rows</code>를 그대로 표 형식으로 표시합니다.</p></div><span>${Number(response.data?.row_count || 0)}건</span></div>
      ${renderResultTable(response.data)}
      <div class="candidate-footnote">후보 표는 저장될 문서 전체가 아니라, 검토에 필요한 안전한 표시용 projection입니다.</div>`;
  } else if (state.metadataResultTab === "storage") {
    const operations = Array.isArray(write.operation_by_key) ? write.operation_by_key : [];
    content.innerHTML = `
      <div class="result-section-heading"><div><h4>저장 처리 계획</h4><p>Writer가 반환한 <code>write_result</code>를 기준으로 저장 대상과 중복 처리 방식을 확인합니다.</p></div>${statusPill(write.dry_run ? "검토 전용" : "저장 요청")}</div>
      ${isSaveRequestPreview ? `<div class="preview-disclaimer"><strong>더미 화면 안내</strong><span>이번 입력은 실제 저장 요청으로 선택했지만, 이 미리보기 서버는 MongoDB를 변경하지 않고 결과 위치만 보여 줍니다.</span></div>` : ""}
      <div class="storage-grid">
        <div><span>대상 DB</span><strong>${escapeHtml(write.database || "-")}</strong></div>
        <div><span>대상 컬렉션</span><strong>${escapeHtml(write.collection_name || "-")}</strong></div>
        <div><span>저장 예정</span><strong>${escapeHtml(write.would_save_count ?? 0)}건</strong></div>
        <div><span>실제 저장</span><strong>${escapeHtml(write.saved_count ?? 0)}건</strong></div>
      </div>
      <div class="operation-list">${operations.length ? operations.map((operation) => `<div><strong>${escapeHtml(operation.key || "-")}</strong><span>${escapeHtml(operation.operation || "-")}</span></div>`).join("") : `<p class="result-empty">처리 계획이 없습니다.</p>`}</div>
      <p class="storage-note">${escapeHtml(write.message || "저장 결과 메시지가 없습니다.")}</p>`;
  } else if (state.metadataResultTab === "raw") {
    renderRawMetadataResponse(content, response);
  } else {
    const resolved = authoring.resolved_references || [];
    const notices = response.answer_sections?.notices || [];
    content.innerHTML = `
      <div class="result-section-heading"><div><h4>처리 과정</h4><p>원문과 Flow 정제안, 계약 검증 결과를 분리해 확인합니다.</p></div><span class="mini-tag">${escapeHtml(authoring.contract_version || "rev_2")}</span></div>
      <div class="process-text-grid">
        <section><span>사용자 입력 원문</span><pre>${escapeHtml(authoring.original_text || response.trace?.raw_text_preview || "-")}</pre></section>
        <section><span>Flow 정제안</span><pre>${escapeHtml(authoring.refined_text || "정제 결과가 없습니다.")}</pre></section>
      </div>
      <div class="validation-grid">
        <div><span>계약 검증</span><strong>${escapeHtml(validation.status || "확인 필요")}</strong></div>
        <div><span>생성 후보</span><strong>${escapeHtml(authoring.generated_count ?? response.data?.row_count ?? 0)}건</strong></div>
        <div><span>확정 참조</span><strong>${escapeHtml(resolved.length)}건</strong></div>
        <div><span>중복 후보</span><strong>${escapeHtml(authoring.existing_match_count ?? 0)}건</strong></div>
      </div>
      <div class="process-detail-grid">
        <section><h5>확정된 계약 변환</h5>${resolved.length ? `<ul class="result-text-list">${resolved.map((item) => `<li><code>${escapeHtml(item.input || "-")}</code> → <code>${escapeHtml(item.target || "-")}</code></li>`).join("")}</ul>` : `<p class="result-empty">확정 변환이 없습니다.</p>`}</section>
        <section><h5>보완 요청 · 가정</h5>${textList([...(authoring.missing_information || []), ...(authoring.assumptions || []), ...notices], "추가 보완이 필요한 항목이 없습니다.")}</section>
      </div>`;
  }

  $$('[data-metadata-result-tab]').forEach((button) => {
    const selected = button.dataset.metadataResultTab === state.metadataResultTab;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
  });
}

function renderMetadataAuthoring() {
  const example = activeAuthoringExample();
  if (!example) return;
  const contract = authoringData().contract || {};
  $("#metadata-contract-version").textContent = contract.version || "rev_2";
  $("#metadata-example-flow-title").textContent = example.flow_label || "등록 Flow 예시";
  $("#metadata-example-fields").textContent = `입력에 포함: ${metadataExampleRequiredInput(example)}`;
  $("#metadata-example-raw").textContent = metadataExampleRawText(example);
  renderMetadataResult();
}

function renderSettings() {
  if (!isAdmin()) return;
  const settings = state.portal?.settings || {};
  const usagePolicy = Object.keys(state.adminSettings?.usage_policy || {}).length
    ? state.adminSettings.usage_policy
    : (settings.usage_policy || {
    active_user_min_distinct_days: 3,
    active_user_min_chat_count: 10,
    history_window_days: 21,
    });
  const admins = state.adminSettings && Array.isArray(state.adminSettings.admins)
    ? state.adminSettings.admins
    : (Array.isArray(settings.admins) ? settings.admins : []);
  const apiItems = metadataConnectionItems();
  $("#api-status-list").innerHTML = apiItems
    .map((item) => `
      <div class="api-status-item api-status-${escapeHtml(item.tone)}">
        <span class="api-status-icon" aria-hidden="true">${svgIcon(item.icon)}</span>
        <div class="api-status-copy"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.detail)}</span></div>
        ${statusPill(item.status)}
      </div>`)
    .join("");
  $("#admin-list").innerHTML = admins
    .map((admin) => `
      <tr><td>${escapeHtml(admin.employee_id || "-")}</td><td><strong>${escapeHtml(admin.name || admin.employee_name || "-")}</strong></td><td><span class="mini-tag">${escapeHtml(admin.role || "관리자")}</span></td><td>${escapeHtml(admin.scope || "포털 운영 관리")}</td><td>${statusPill(admin.status || "활성")}</td><td><span class="admin-row-note">사번 기준</span></td></tr>`)
    .join("") || `<tr><td colspan="6" class="empty-cell">등록된 관리자 정보가 없습니다.</td></tr>`;
  const adminAddButton = $("#admin-add-button");
  if (adminAddButton) {
    const isBusy = state.adminAdding;
    adminAddButton.disabled = isBusy;
    adminAddButton.classList.toggle("is-loading", isBusy);
    adminAddButton.setAttribute("aria-busy", String(isBusy));
    adminAddButton.title = isBusy ? "관리자 정보를 저장하고 있습니다." : "사번과 이름을 입력해 관리자를 등록합니다.";
    adminAddButton.innerHTML = isBusy
      ? `${svgIcon("clock", "button-spinner")}<span>등록 중…</span>`
      : '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg><span>관리자 추가</span>';
  }
  $("#active-user-min-days").value = usagePolicy.active_user_min_distinct_days;
  $("#active-user-min-chats").value = usagePolicy.active_user_min_chat_count;
  $("#active-policy-summary").textContent = `최근 ${usagePolicy.history_window_days}일 중 서로 다른 일자 ${usagePolicy.active_user_min_distinct_days}일 이상, 누적 채팅 ${usagePolicy.active_user_min_chat_count}건 이상 사용자를 활성 사용자로 집계합니다.`;

  const callerInput = $("#gaia-api-caller-employee-id");
  if (callerInput && document.activeElement !== callerInput) {
    callerInput.value = gaiaApiCallerEmployeeId();
  }
  const callerSummary = $("#gaia-api-caller-summary");
  if (!callerSummary) return;
  if (state.adminSettingsLoading) {
    callerSummary.textContent = "현재 설정을 불러오는 중입니다.";
  } else if (state.adminSettingsError) {
    callerSummary.textContent = state.adminSettingsError;
  } else if (gaiaApiCallerEmployeeId()) {
    const updated = state.adminSettings?.updated_at ? ` · 마지막 변경 ${state.adminSettings.updated_at}` : "";
    callerSummary.textContent = `현재 ${gaiaApiCallerEmployeeId()} 사번이 GAIA API 호출에 사용됩니다.${updated}`;
  } else if (state.adminSettings?.storage?.persistent === false) {
    callerSummary.textContent = "현재는 미리보기 설정입니다. MongoDB 연결 후 변경 값을 저장할 수 있습니다.";
  } else {
    callerSummary.textContent = "등록된 GAIA API 호출 권한 사번이 없습니다.";
  }
}

function renderAccessControls() {
  const admin = isAdmin();
  document.body.classList.toggle("is-standard-user", !admin);
  ["metadata", "settings"].forEach((view) => {
    const nav = $(`[data-nav="${view}"]`);
    if (!nav) return;
    nav.classList.toggle("restricted-nav", !admin);
    nav.dataset.restricted = String(!admin);
  });
  $$("[data-requires-admin]").forEach((control) => {
    control.classList.toggle("restricted-control", !admin);
    control.dataset.restricted = String(!admin);
  });
  $("#metadata-access-note").hidden = admin;
  $("#viewer-name").textContent = state.portal.viewer.name;
  $("#viewer-role").textContent = `${state.portal.viewer.role} · ${state.portal.viewer.employee_id}`;
  $(".avatar").textContent = state.portal.viewer.name.slice(0, 1);
  $(".mobile-user").textContent = state.portal.viewer.name.slice(0, 1);
}

function switchView(viewName) {
  if (!viewTitles[viewName]) return false;
  if (viewName === "metadata" && !isAdmin()) {
    showToast("관리자만 등록 가능합니다");
    return false;
  }
  if (viewName === "settings" && !isAdmin()) {
    showToast("관리자만 설정을 확인할 수 있습니다.");
    return false;
  }
  $$(".content-view").forEach((view) => view.classList.toggle("active", view.dataset.view === viewName));
  $$("[data-nav]").forEach((button) => button.classList.toggle("active", button.dataset.nav === viewName));
  $("#breadcrumb").textContent = viewTitles[viewName][0];
  $("#page-title").textContent = viewTitles[viewName][1];
  $(".sidebar").classList.remove("open");
  window.scrollTo({ top: 0, behavior: "smooth" });
  return true;
}

function isIntervalRepeat(repeat) {
  return String(repeat || "").trim() === "interval";
}

function intervalMinutes(value) {
  const minutes = Number(value);
  return Number.isInteger(minutes) && minutes > 0 && minutes <= 1440 ? minutes : 10;
}

function intervalLabel(value) {
  const minutes = intervalMinutes(value);
  return minutes === 60 ? "1시간마다" : `${minutes}분마다`;
}

function formattedTime(value) {
  const matched = String(value || "").match(/^(\d{1,2}):(\d{2})$/);
  if (!matched) return "시간 미설정";
  const hour = Number(matched[1]);
  const minute = matched[2];
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) return "시간 미설정";
  const meridiem = hour >= 12 ? "오후" : "오전";
  const hour12 = String(hour % 12 || 12).padStart(2, "0");
  return `${meridiem} ${hour12}:${minute}`;
}

function isIntervalSchedule(schedule) {
  return isIntervalRepeat(schedule?.repeat)
    || (Number.isInteger(Number(schedule?.interval_minutes)) && Number(schedule?.interval_minutes) > 0);
}

function scheduleIntervalMinutes(schedule) {
  return intervalMinutes(schedule?.interval_minutes);
}

function scheduleWindowLabel(schedule) {
  const start = formattedTime(schedule?.start_time);
  const end = formattedTime(schedule?.end_time);
  return `${start} ~ ${end}`;
}

function scheduleLabel(repeat, time, intervalMinutesValue = null, startTime = "", endTime = "") {
  if (isIntervalRepeat(repeat)) {
    const start = formattedTime(startTime);
    const end = formattedTime(endTime);
    return `${intervalLabel(intervalMinutesValue)} · ${start} ~ ${end}`;
  }
  const labels = { "평일": "평일", "매일": "매일", "매주": "매주 월요일", "매월": "매월 1일", "한 번만": "한 번만" };
  return `${labels[repeat] || repeat} · ${formattedTime(time)}`;
}

function scheduleRuleLabel(schedule) {
  if (isIntervalSchedule(schedule)) {
    return intervalLabel(scheduleIntervalMinutes(schedule));
  }
  return schedule?.rule_label || scheduleLabel(schedule?.repeat, schedule?.time);
}

function nextRunLabel(repeat, time, intervalMinutesValue = null, startTime = "", endTime = "") {
  if (isIntervalRepeat(repeat)) {
    return `오늘 ${formattedTime(startTime)}부터 ${formattedTime(endTime)}까지 · ${intervalLabel(intervalMinutesValue)}`;
  }
  return `다음 ${scheduleLabel(repeat, time)}`;
}

function scheduleNextRun(schedule) {
  return nextRunLabel(
    isIntervalSchedule(schedule) ? "interval" : schedule?.repeat,
    schedule?.time,
    schedule?.interval_minutes,
    schedule?.start_time,
    schedule?.end_time,
  );
}

function scheduleTimingFromForm(form) {
  const repeat = String(form.elements.repeat.value || "").trim();
  const interval = isIntervalRepeat(repeat);
  return {
    repeat,
    time: interval ? "" : String(form.elements.time.value || "").trim(),
    interval_minutes: interval ? intervalMinutes(form.elements.interval_minutes.value) : null,
    start_time: interval ? String(form.elements.start_time.value || "").trim() : "",
    end_time: interval ? String(form.elements.end_time.value || "").trim() : "",
  };
}

function validateScheduleTiming(timing, form) {
  const startInput = form.elements.start_time;
  const endInput = form.elements.end_time;
  startInput.setCustomValidity("");
  endInput.setCustomValidity("");

  if (!isIntervalRepeat(timing.repeat)) return Boolean(timing.time);
  if (!timing.start_time || !timing.end_time) {
    showToast("간격 반복은 시작 시간과 종료 시간을 모두 입력해 주세요.");
    return false;
  }
  if (timing.start_time >= timing.end_time) {
    endInput.setCustomValidity("종료 시간은 시작 시간보다 늦어야 합니다.");
    endInput.reportValidity();
    return false;
  }
  return true;
}

function syncScheduleTimingFields() {
  const form = $("#schedule-form");
  const interval = isIntervalRepeat(form.elements.repeat.value);
  const singleTime = $("#schedule-single-time-field");
  const intervalFields = $("#interval-schedule-fields");
  const timeInput = form.elements.time;
  const intervalInputs = [form.elements.interval_minutes, form.elements.start_time, form.elements.end_time];

  singleTime.hidden = interval;
  intervalFields.hidden = !interval;
  timeInput.disabled = interval;
  timeInput.required = !interval;
  intervalInputs.forEach((input) => {
    input.disabled = !interval;
    input.required = interval;
  });
  $("#interval-mode-label").textContent = `${intervalLabel(form.elements.interval_minutes.value)} 반복 실행`;
  updateSchedulePreview();
}

function updateSchedulePreview() {
  const form = $("#schedule-form");
  form.elements.start_time.setCustomValidity("");
  form.elements.end_time.setCustomValidity("");
  const timing = scheduleTimingFromForm(form);
  $("#next-preview").textContent = nextRunLabel(
    timing.repeat,
    timing.time,
    timing.interval_minutes,
    timing.start_time,
    timing.end_time,
  );
}

function prepareScheduleDrawer(scheduleId = "") {
  const form = $("#schedule-form");
  const schedule = state.portal.schedules.find((item) => item.id === scheduleId);
  state.editingScheduleId = schedule?.id || null;
  form.reset();
  if (schedule) {
    $("#schedule-drawer-kicker").textContent = "EDIT AUTOMATION";
    $("#schedule-drawer-title").textContent = "스케줄 수정";
    $("#schedule-submit").textContent = "변경 저장";
    form.elements.title.value = schedule.title;
    form.elements.question.value = schedule.question;
    form.elements.repeat.value = isIntervalSchedule(schedule) ? "interval" : (schedule.repeat || "매일");
    form.elements.time.value = schedule.time || "09:30";
    form.elements.interval_minutes.value = String(scheduleIntervalMinutes(schedule));
    form.elements.start_time.value = schedule.start_time || "09:00";
    form.elements.end_time.value = schedule.end_time || "18:00";
  } else {
    $("#schedule-drawer-kicker").textContent = "NEW AUTOMATION";
    $("#schedule-drawer-title").textContent = "새 스케줄 등록";
    $("#schedule-submit").textContent = "스케줄 등록";
    form.elements.repeat.value = "평일";
    form.elements.time.value = "09:30";
    form.elements.interval_minutes.value = "10";
    form.elements.start_time.value = "09:00";
    form.elements.end_time.value = "18:00";
  }
  $("#schedule-drawer").setAttribute("aria-label", schedule ? "스케줄 수정" : "새 스케줄 등록");
  syncScheduleTimingFields();
}

function scheduleRequestPayload(form) {
  const timing = scheduleTimingFromForm(form);
  return {
    title: String(form.elements.title.value || "").trim(),
    question: String(form.elements.question.value || "").trim(),
    repeat: timing.repeat,
    time: timing.time,
    interval_minutes: timing.interval_minutes,
    start_time: timing.start_time,
    end_time: timing.end_time,
  };
}

async function saveSchedule(form) {
  if (!state.portal || state.scheduleSubmitting) return;
  const timing = scheduleTimingFromForm(form);
  if (!validateScheduleTiming(timing, form)) return;

  const editingSchedule = state.portal.schedules.find((item) => item.id === state.editingScheduleId);
  if (editingSchedule && !canEditSchedule(editingSchedule)) {
    showToast("등록자 본인 또는 관리자만 스케줄을 수정할 수 있습니다.");
    return;
  }

  const payload = scheduleRequestPayload(form);
  if (!payload.title || !payload.question) {
    showToast("스케줄 이름과 실행 질문을 입력해 주세요.");
    return;
  }

  const isEdit = Boolean(editingSchedule);
  const url = isEdit
    ? `/api/schedules/${encodeURIComponent(editingSchedule.id)}`
    : "/api/schedules";
  const submitButton = $("#schedule-submit");
  state.scheduleSubmitting = true;
  submitButton.disabled = true;

  try {
    const response = await fetch(url, {
      method: isEdit ? "PATCH" : "POST",
      headers: portalRequestHeaders({ Accept: "application/json", "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const responsePayload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageFromResponse(responsePayload, isEdit
        ? "스케줄을 수정하지 못했습니다."
        : "스케줄을 등록하지 못했습니다."));
    }
    replaceScheduleInState(scheduleFromResponse(responsePayload));
    if (!isEdit) state.scheduleScope = "mine";
    closeDrawers();
    renderSchedules();
    switchView("schedules");
    showToast(isEdit ? "스케줄 변경 사항을 저장했습니다." : "새 스케줄을 등록했습니다.");
  } catch (error) {
    console.error("schedule save request failed", error);
    showToast(error?.message || "스케줄을 저장하지 못했습니다.");
  } finally {
    state.scheduleSubmitting = false;
    submitButton.disabled = false;
  }
}

async function updateScheduleStatus(schedule) {
  if (!schedule || !canEditSchedule(schedule) || state.scheduleMutationId) return;
  const nextStatus = scheduleStatusCode(schedule) === "active" ? "inactive" : "active";
  state.scheduleMutationId = schedule.id;
  renderSchedules();
  showToast(`${schedule.title} 스케줄 상태를 변경하고 있습니다.`);

  try {
    const response = await fetch(`/api/schedules/${encodeURIComponent(schedule.id)}/status`, {
      method: "PATCH",
      headers: portalRequestHeaders({ Accept: "application/json", "Content-Type": "application/json" }),
      body: JSON.stringify({ status: nextStatus }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageFromResponse(payload, "스케줄 상태를 변경하지 못했습니다."));
    }
    const updated = scheduleFromResponse(payload);
    replaceScheduleInState(updated);
    renderSchedules();
    showToast(`${updated.title} 스케줄을 ${scheduleStatusCode(updated) === "active" ? "재개" : "일시중지"}했습니다.`);
  } catch (error) {
    console.error("schedule status request failed", error);
    showToast(error?.message || "스케줄 상태를 변경하지 못했습니다.");
  } finally {
    state.scheduleMutationId = "";
    renderSchedules();
  }
}

async function deleteSchedule(schedule) {
  if (!schedule || !canEditSchedule(schedule) || state.scheduleMutationId) return;
  state.scheduleMutationId = schedule.id;
  renderSchedules();

  try {
    const response = await fetch(`/api/schedules/${encodeURIComponent(schedule.id)}`, {
      method: "DELETE",
      headers: portalRequestHeaders({ Accept: "application/json" }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageFromResponse(payload, "스케줄을 삭제하지 못했습니다."));
    }
    state.portal.schedules = state.portal.schedules.filter((item) => item.id !== schedule.id);
    if (state.editingScheduleId === schedule.id) closeDrawers();
    renderSchedules();
    showToast(`${schedule.title} 스케줄을 삭제했습니다.`);
  } catch (error) {
    console.error("schedule delete request failed", error);
    showToast(error?.message || "스케줄을 삭제하지 못했습니다.");
  } finally {
    state.scheduleMutationId = "";
    renderSchedules();
  }
}

function metadataFormMarkup() {
  const example = activeAuthoringExample();
  const rawText = escapeHtml(metadataExampleRawText(example));
  const rawTextRows = state.metadataType === "table_catalog" ? 18 : 9;
  const requiredInput = metadataExampleRequiredInput(example);
  const api = metadataApiState();
  const apiNotice = api.preview_only || api.mode === "preview"
    ? "현재는 미리보기 모드입니다. 외부 메타데이터 API와 MongoDB에는 요청하지 않습니다. 실제 연동은 서버의 .env에서 API 모드를 설정한 뒤 시작됩니다."
    : api.ready
      ? "입력은 포털 서버에서 외부 메타데이터 API로 전달됩니다. API 키와 MongoDB 연결 정보는 브라우저에 노출되지 않습니다."
      : "메타데이터 API 연결 설정이 아직 준비되지 않았습니다. 서버의 .env 값을 확인한 뒤 다시 시도해 주세요.";
  return `
    <div class="drawer-flow-note"><strong>${escapeHtml(example?.flow_label || "메타데이터 등록 Flow")}</strong><span>Chat Input의 <code>input_value</code>가 등록 원문으로 전달됩니다.</span></div>
    <label><span>등록 요청 원문</span><textarea name="raw_text" rows="${rawTextRows}" required>${rawText}</textarea><small>포함하면 좋은 정보: ${escapeHtml(requiredInput)}</small></label>
    <label><span>중복 처리 방식</span><select name="duplicate_action"><option value="skip">skip · 기존 항목 유지</option><option value="merge">merge · 기존 항목에 병합</option><option value="replace">replace · 기존 항목 교체</option><option value="create_new">create_new · 새 키 생성</option></select><small>실제 Flow에서는 요청 로더의 <code>duplicate_action</code>에 전달됩니다.</small></label>
    <label class="dry-run-control"><input name="dry_run" type="checkbox" checked /><span><strong>먼저 테스트 실행으로 검토</strong><small>후보와 저장 계획만 확인하고 MongoDB에는 저장하지 않습니다.</small></span></label>
    <div class="drawer-contract-note"><span>i</span><p>${escapeHtml(apiNotice)}</p></div>`;
}

function updateMetadataSubmitLabel() {
  if (state.metadataSubmitting) {
    $("#metadata-submit").textContent = "Flow 실행 중…";
    return;
  }
  const dryRun = $("#metadata-form [name='dry_run']")?.checked !== false;
  const api = metadataApiState();
  if (api.preview_only || api.mode === "preview") {
    $("#metadata-submit").textContent = dryRun ? "테스트 실행 미리보기" : "저장 요청 미리보기";
    return;
  }
  $("#metadata-submit").textContent = dryRun ? "테스트 실행" : "저장 요청 실행";
}

function renderMetadataForm() {
  const type = metadataTypes[state.metadataType];
  $("#metadata-drawer-kicker").textContent = type.kicker;
  $("#metadata-drawer-title").textContent = `${type.label} 등록 요청`;
  $("#metadata-drawer").setAttribute("aria-label", `${type.label} 등록 요청`);
  $("#metadata-form-fields").innerHTML = metadataFormMarkup();
  $("#metadata-form [name='dry_run']")?.addEventListener("change", updateMetadataSubmitLabel);
  updateMetadataSubmitLabel();
}

function openDrawer(kind, itemId = "") {
  if (kind === "metadata" && !isAdmin()) {
    showToast("관리자만 등록 가능합니다");
    return;
  }
  if (kind === "schedule") prepareScheduleDrawer(itemId);
  if (kind === "metadata") renderMetadataForm();
  const drawer = $(`#${kind}-drawer`);
  if (!drawer) return;
  $("#drawer-backdrop").hidden = false;
  window.requestAnimationFrame(() => drawer.classList.add("open"));
  drawer.setAttribute("aria-hidden", "false");
  drawer.querySelector("input, textarea, select")?.focus();
}

function closeDrawers() {
  $$(".drawer").forEach((drawer) => {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  });
  state.editingScheduleId = null;
  window.setTimeout(() => { $("#drawer-backdrop").hidden = true; }, 220);
}

function normalizeMetadataRun(payload) {
  if (!payload || typeof payload !== "object" || !payload.response || typeof payload.response !== "object") {
    throw new Error("메타데이터 API 응답 형식을 확인하지 못했습니다.");
  }
  return {
    metadataType: payload.metadata_type || state.metadataType,
    runId: payload.run_id || "METADATA-RUN",
    requestedAt: payload.requested_at || "방금 전",
    requestedBy: payload.requested_by || `${state.portal.viewer.name} (${viewerId()})`,
    previewOnly: Boolean(payload.preview_only),
    requestedDryRun: Boolean(payload.requested_dry_run),
    response: payload.response,
  };
}

async function submitMetadataAuthoring(form) {
  const formData = new FormData(form);
  const rawText = String(formData.get("raw_text") || "").trim();
  if (!rawText) {
    showToast("등록 요청 원문을 입력해 주세요.");
    form.elements.raw_text?.focus();
    return;
  }

  const requestBody = {
    metadata_type: state.metadataType,
    raw_text: rawText,
    duplicate_action: String(formData.get("duplicate_action") || "skip"),
    dry_run: Boolean(form.elements.dry_run?.checked),
  };
  const submitButton = $("#metadata-submit");
  state.metadataSubmitting = true;
  submitButton.disabled = true;
  updateMetadataSubmitLabel();

  try {
    const response = await fetch("/api/metadata-authoring", {
      method: "POST",
      headers: portalRequestHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(requestBody),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageFromResponse(payload, "메타데이터 등록 API 호출에 실패했습니다."));
    }

    state.metadataResult = normalizeMetadataRun(payload);
    state.metadataResultTab = "process";
    if (!state.metadataResult.previewOnly) {
      await loadLiveMetadata();
    }
    closeDrawers();
    renderMetadata();
    switchView("metadata");
    const resultLabel = state.metadataResult.previewOnly ? "미리보기 결과" : "Flow 실행 결과";
    showToast(`${metadataTypes[state.metadataType].label} ${resultLabel}를 받았습니다.`);
  } catch (error) {
    console.error(error);
    showToast(error?.message || "메타데이터 등록 요청을 완료하지 못했습니다.");
  } finally {
    state.metadataSubmitting = false;
    submitButton.disabled = false;
    updateMetadataSubmitLabel();
  }
}

function bindEvents() {
  if (state.eventsBound) return;
  state.eventsBound = true;

  document.addEventListener("click", async (event) => {
    const nav = event.target.closest("[data-nav]");
    if (nav) {
      event.preventDefault();
      switchView(nav.dataset.nav);
      return;
    }

    if (event.target.closest("[data-refresh-dashboard-usage]")) {
      void loadDashboardUsage({ notifyOnError: true });
      return;
    }

    if (event.target.closest("[data-refresh-dashboard-usage-full]")) {
      void refreshDashboardUsageFull();
      return;
    }

    if (event.target.closest("[data-dashboard-usage-export]")) {
      void downloadDashboardUsageCsv();
      return;
    }

    if (event.target.closest("[data-refresh-admin-settings]")) {
      void refreshAdminConfiguration();
      return;
    }

    if (event.target.closest("[data-open-admin-add]")) {
      openAdminAddModal(event.target.closest("[data-open-admin-add]"));
      return;
    }

    if (event.target.closest("[data-close-admin-add]") || event.target.id === "admin-add-modal") {
      closeAdminAddModal();
      return;
    }

    if (event.target.closest("[data-close-metadata-status]") || event.target.id === "metadata-status-modal") {
      closeMetadataStatusModal();
      return;
    }

    if (event.target.closest("[data-close-metadata-detail]") || event.target.id === "metadata-detail-modal") {
      closeMetadataDetailModal();
      return;
    }

    if (event.target.closest("[data-copy-metadata-detail]")) {
      void copyMetadataDetailJson();
      return;
    }

    if (event.target.closest("#metadata-status-confirm")) {
      void confirmMetadataStatusUpdate();
      return;
    }

    const scheduleScope = event.target.closest("[data-schedule-scope]");
    if (scheduleScope) {
      state.scheduleScope = scheduleScope.dataset.scheduleScope;
      renderSchedules();
      return;
    }

    const metadataType = event.target.closest("[data-metadata-type]");
    if (metadataType) {
      if (!isAdmin()) {
        showToast("관리자만 등록 가능합니다");
        return;
      }
      state.metadataType = metadataType.dataset.metadataType;
      state.metadataSearch = "";
      state.metadataPage = 1;
      state.metadataResultTab = "process";
      closeMetadataDetailModal();
      closeMetadataStatusModal();
      $("#metadata-search").value = "";
      renderMetadata();
      return;
    }

    const metadataPageButton = event.target.closest("[data-metadata-page]");
    if (metadataPageButton) {
      const pagination = paginateMetadata(filteredMetadata());
      const requested = metadataPageButton.dataset.metadataPage;
      const nextPage = requested === "previous"
        ? pagination.page - 1
        : requested === "next"
          ? pagination.page + 1
          : Number(requested);
      if (Number.isInteger(nextPage) && nextPage >= 1 && nextPage <= pagination.totalPages) {
        state.metadataPage = nextPage;
        renderMetadata();
      }
      return;
    }

    const resultTab = event.target.closest("[data-metadata-result-tab]");
    if (resultTab) {
      state.metadataResultTab = resultTab.dataset.metadataResultTab;
      renderMetadataResult();
      return;
    }

    if (event.target.closest("[data-load-metadata-example]")) {
      if (!isAdmin()) {
        showToast("관리자만 등록 가능합니다");
        return;
      }
      openDrawer("metadata");
      return;
    }

    const drawerTrigger = event.target.closest("[data-open-drawer]");
    if (drawerTrigger) {
      openDrawer(drawerTrigger.dataset.openDrawer);
      return;
    }

    if (event.target.closest("[data-close-drawer]") || event.target.id === "drawer-backdrop") {
      closeDrawers();
      return;
    }
    if (event.target.matches(".mobile-menu")) {
      $(".sidebar").classList.toggle("open");
      return;
    }
    if (event.target.closest(".close-tip")) {
      $(".schedule-tip")?.remove();
      return;
    }

    const editButton = event.target.closest("[data-edit-schedule]");
    if (editButton) {
      const schedule = state.portal.schedules.find((item) => item.id === editButton.dataset.editSchedule);
      if (!schedule || !canEditSchedule(schedule)) {
        showToast("등록자 본인 또는 관리자만 스케줄을 수정할 수 있습니다.");
        return;
      }
      openDrawer("schedule", schedule.id);
      return;
    }

    const pauseButton = event.target.closest("[data-toggle-schedule]");
    if (pauseButton) {
      const schedule = state.portal.schedules.find((item) => item.id === pauseButton.dataset.toggleSchedule);
      if (!schedule || !canEditSchedule(schedule)) {
        showToast("등록자 본인 또는 관리자만 스케줄을 수정하거나 상태를 변경할 수 있습니다.");
        return;
      }
      await updateScheduleStatus(schedule);
      return;
    }

    const deleteButton = event.target.closest("[data-delete-schedule]");
    if (deleteButton) {
      const schedule = state.portal.schedules.find((item) => item.id === deleteButton.dataset.deleteSchedule);
      if (!schedule || !canEditSchedule(schedule)) {
        showToast("등록자 본인 또는 관리자만 스케줄을 삭제할 수 있습니다.");
        return;
      }
      await deleteSchedule(schedule);
      return;
    }

    if (event.target.closest("[data-schedule-restricted]")) {
      showToast("전체 스케줄은 열람할 수 있습니다. 수정·일시중지·삭제는 등록자 본인 또는 관리자만 가능합니다.");
      return;
    }

    const metadataStatusButton = event.target.closest("[data-metadata-status]");
    if (metadataStatusButton) {
      openMetadataStatusModal(
        metadataStatusButton.dataset.metadataStatus,
        metadataStatusButton.dataset.nextMetadataStatus,
        metadataStatusButton,
      );
      return;
    }

    const metadataDetailButton = event.target.closest("[data-metadata-detail]");
    if (metadataDetailButton) {
      openMetadataDetailModal(metadataDetailButton.dataset.metadataDetail, metadataDetailButton);
      return;
    }
  });

  document.addEventListener("keydown", (event) => {
    const detailModal = $("#metadata-detail-modal");
    const statusModal = $("#metadata-status-modal");
    const adminModal = $("#admin-add-modal");
    const modal = detailModal && !detailModal.hidden
      ? detailModal
      : statusModal && !statusModal.hidden
        ? statusModal
        : adminModal;
    if (!modal || modal.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      if (modal.id === "metadata-detail-modal") closeMetadataDetailModal();
      else if (modal.id === "metadata-status-modal") closeMetadataStatusModal();
      else closeAdminAddModal();
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = modal.id === "metadata-detail-modal"
      ? $(".metadata-detail-dialog", modal)
      : modal.id === "metadata-status-modal"
        ? $(".metadata-status-dialog", modal)
        : $(".admin-add-dialog", modal);
    const focusable = dialog
      ? [...dialog.querySelectorAll("button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])")]
      : [];
    if (!focusable.length) {
      event.preventDefault();
      dialog?.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  $$(".filter-chip").forEach((button) => button.addEventListener("click", () => {
    state.scheduleFilter = button.dataset.filter;
    $$(".filter-chip").forEach((chip) => chip.classList.toggle("active", chip === button));
    renderSchedules();
  }));
  $("#schedule-search").addEventListener("input", (event) => {
    state.scheduleSearch = event.target.value;
    renderSchedules();
  });
  $("#metadata-search").addEventListener("input", (event) => {
    state.metadataSearch = event.target.value;
    state.metadataPage = 1;
    renderMetadata();
  });

  $("#schedule-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await saveSchedule(event.currentTarget);
  });

  $("#metadata-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!isAdmin()) {
      showToast("관리자만 등록 가능합니다");
      return;
    }
    if (state.metadataSubmitting) return;
    await submitMetadataAuthoring(event.currentTarget);
  });

  $("#activity-policy-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await saveUsagePolicy(event.currentTarget);
  });

  $("#gaia-api-caller-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await saveGaiaApiCaller(event.currentTarget);
  });

  $("#admin-add-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitAdministrator(event.currentTarget);
  });

  $("#schedule-form [name='repeat']").addEventListener("change", syncScheduleTimingFields);
  $("#schedule-form [name='time']").addEventListener("input", updateSchedulePreview);
  $("#schedule-form [name='interval_minutes']").addEventListener("change", () => {
    $("#interval-mode-label").textContent = `${intervalLabel($("#schedule-form [name='interval_minutes']").value)} 반복 실행`;
    updateSchedulePreview();
  });
  $("#schedule-form [name='start_time']").addEventListener("input", updateSchedulePreview);
  $("#schedule-form [name='end_time']").addEventListener("input", updateSchedulePreview);
}

async function initialize() {
  try {
    const response = await fetch("/api/portal", { cache: "no-store" });
    if (!response.ok) throw new Error("portal data request failed");
    state.portal = await response.json();
  } catch (error) {
    console.error(error);
    document.body.innerHTML = `<main class="fatal-error"><h1>포털 정보를 불러오지 못했습니다.</h1><p>로그인 상태와 서버 실행 상태를 확인한 뒤 새로고침해 주세요.</p></main>`;
    return;
  }

  // Bind user actions as soon as the Portal identity and initial data exist.
  // Phoenix/MongoDB reads below can be slow; they must not delay button handling.
  renderAccessControls();
  renderDashboard();
  renderSchedules();
  if (isAdmin()) {
    renderMetadata();
    renderSettings();
  }
  bindEvents();

  const startupTasks = [loadDashboardUsage(), loadSchedules()];
  if (isAdmin()) {
    startupTasks.push(
      loadMetadataApiStatus(),
      loadAdminSettings(),
      loadLiveMetadata(),
    );
  }
  await Promise.all(startupTasks);

  renderAccessControls();
  renderMetadataApiIndicator();
  renderDashboard();
  renderSchedules();
  if (isAdmin()) {
    renderMetadata();
    renderSettings();
  }
}

initialize();
