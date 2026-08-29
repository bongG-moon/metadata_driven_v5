const state = {
  portal: null,
  scheduleFilter: "all",
  scheduleScope: "mine",
  scheduleSearch: "",
  metadataSearch: "",
  metadataType: "table_catalog",
  metadataResultTab: "process",
  metadataResult: null,
  metadataApi: null,
  metadataSubmitting: false,
  editingScheduleId: null,
};

const viewTitles = {
  dashboard: ["활용 현황", "한눈에 보는 활용 현황"],
  schedules: ["스케줄링", "내 스케줄 관리"],
  metadata: ["메타데이터", "Agent 메타데이터 관리"],
  settings: ["설정", "관리자 설정"],
};

const metadataTypes = {
  table_catalog: {
    label: "데이터 카탈로그",
    kicker: "DATA CATALOG",
    totalLabel: "등록 데이터셋",
    description: "Agent가 어떤 데이터를 어디에서 어떤 조건으로 조회할 수 있는지 정의합니다.",
    filterHint: "데이터셋 키, 연결 소스, 필수 표준 Filter를 함께 관리합니다.",
    headers: ["데이터셋", "분류", "연결 소스", "필수 조건", "담당 조직", "최종 변경", "상태", ""],
  },
  main_flow_filters: {
    label: "Main Flow Filters",
    kicker: "MAIN FLOW FILTERS",
    totalLabel: "표준 Filter",
    description: "질문 속 일자·공정·LOT 같은 표현을 표준 키와 실제 데이터 컬럼으로 연결합니다.",
    filterHint: "표준 Filter 키, 의미 역할, 후보 컬럼과 사용자 표현을 관리합니다.",
    headers: ["표준 Filter", "의미 역할", "값 형식", "후보 컬럼", "사용자 표현", "담당 조직", "최종 변경", "상태"],
  },
  domain: {
    label: "도메인 정보",
    kicker: "DOMAIN KNOWLEDGE",
    totalLabel: "도메인 항목",
    description: "공정 그룹, 업무 용어, 분석 레시피처럼 Agent가 질문을 해석할 때 쓰는 업무 지식을 관리합니다.",
    filterHint: "도메인 구분, 키, 동의어, 질문 단서와 업무 설명을 관리합니다.",
    headers: ["도메인 항목", "구분", "동의어", "질문 단서", "업무 설명", "담당 조직", "최종 변경", "상태"],
  },
};

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusClass(status) {
  if (["성공", "활성", "정상", "연결됨"].includes(status)) return "status-success";
  if (["일시중지", "재시도 예정", "검토 필요", "설정 필요", "연결 확인 필요"].includes(status)) return "status-paused";
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

function canEditSchedule(schedule) {
  return isAdmin() || schedule.owner === viewerId();
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
  const { dashboard } = state.portal;
  $("#dashboard-period").textContent = dashboard.period_label;
  $("#dashboard-range").textContent = dashboard.range_label;
  $("#kpi-grid").innerHTML = dashboard.kpis
    .map((item, index) => `
      <article class="kpi-card" style="--card-glow:${index === 1 ? "#e9fbf7" : index === 2 ? "#fff5de" : "#edf3ff"}">
        <span class="card-label">${escapeHtml(item.label)}</span>
        <strong>${escapeHtml(item.value)}</strong>
        <footer><span class="metric-change ${item.tone === "accent" ? "accent" : ""}">${escapeHtml(item.change)}</span>${escapeHtml(item.detail)}</footer>
      </article>`)
    .join("");

  $("#usage-total").textContent = `누적 ${dashboard.total_chat_count.toLocaleString()}건`;
  $("#usage-chart").innerHTML = dashboard.usage_by_day
    .map((item) => `
      <div class="bar-wrap">
        <div class="bar-pair">
          <div class="chart-bar user-bar" data-value="사용자 ${item.unique_users}명" style="height:${Math.max(item.user_height, 12)}%"></div>
          <div class="chart-bar chat-bar" data-value="채팅 ${item.chat_count}건" style="height:${Math.max(item.chat_height, 12)}%"></div>
        </div>
        <span>${escapeHtml(item.label)}</span>
      </div>`)
    .join("");

  const rule = dashboard.active_user_rule;
  $("#active-user-summary").innerHTML = `
    <div class="active-user-number"><strong>${dashboard.active_user_count}</strong><span>명</span></div>
    <p>서로 다른 일자 <strong>${rule.min_distinct_days}일 이상</strong> · 누적 채팅 <strong>${rule.min_chat_count}건 이상</strong></p>`;
  $("#active-user-list").innerHTML = dashboard.active_users.length
    ? dashboard.active_users
        .map((user) => `
          <li><div><strong>${escapeHtml(user.user_name)}</strong><span>${escapeHtml(user.employee_id)}</span></div><div><b>${user.distinct_days}일</b><span>${user.chat_count}건</span></div></li>`)
        .join("")
    : `<li class="empty-list">현재 기준을 충족한 사용자가 없습니다.</li>`;

  $("#usage-history-list").innerHTML = dashboard.recent_usage_history
    .map((record) => `
      <tr>
        <td>${escapeHtml(record.date)}</td><td><strong>${escapeHtml(record.user_name)}</strong><span class="table-subtle">${escapeHtml(record.employee_id)}</span></td><td class="question-cell">${escapeHtml(record.question)}</td><td><span class="mini-tag">${escapeHtml(channelLabel(record.channel))}</span></td>
      </tr>`)
    .join("");

  $("#recent-runs").innerHTML = dashboard.recent_runs
    .map((run) => `
      <tr>
        <td>${escapeHtml(run.time)}</td><td><strong>${escapeHtml(run.name)}</strong></td><td>${escapeHtml(run.owner)}</td><td>${escapeHtml(run.target)}</td><td>${statusPill(run.status)}</td>
      </tr>`)
    .join("");
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

function filteredSchedules() {
  const search = state.scheduleSearch.toLowerCase().trim();
  return state.portal.schedules.filter((schedule) => {
    const matchesScope = state.scheduleScope === "all" || schedule.owner === viewerId();
    const matchesFilter = state.scheduleFilter === "all" || schedule.status === state.scheduleFilter;
    const haystack = `${schedule.title} ${schedule.question} ${schedule.target} ${schedule.owner}`.toLowerCase();
    return matchesScope && matchesFilter && (!search || haystack.includes(search));
  });
}

function scheduleActions(schedule) {
  if (!canEditSchedule(schedule)) {
    return `
      <span class="readonly-chip">열람 전용</span>
      <button class="restricted-action" type="button" data-schedule-restricted="${escapeHtml(schedule.id)}">권한 안내</button>`;
  }
  return `
    <button class="edit-action" type="button" data-edit-schedule="${escapeHtml(schedule.id)}">수정</button>
    <button class="pause-action" type="button" data-toggle-schedule="${escapeHtml(schedule.id)}">${schedule.status === "활성" ? "일시중지" : "재개"}</button>`;
}

function renderSchedules() {
  const schedules = filteredSchedules();
  const mineCount = state.portal.schedules.filter((schedule) => schedule.owner === viewerId()).length;
  const allCount = state.portal.schedules.length;
  $("#schedule-count").textContent = allCount;
  $("#my-schedule-count").textContent = mineCount;
  $("#all-schedule-count").textContent = allCount;
  $$("[data-schedule-scope]").forEach((button) => {
    const isSelected = button.dataset.scheduleScope === state.scheduleScope;
    button.classList.toggle("active", isSelected);
    button.setAttribute("aria-selected", String(isSelected));
  });
  $("#schedule-scope-note").textContent = state.scheduleScope === "mine"
    ? "본인이 등록한 스케줄만 표시합니다. 이 목록에서는 수정과 활성 상태 변경이 가능합니다."
    : isAdmin()
      ? "전체 스케줄을 보고 있습니다. 관리자는 모든 스케줄을 수정하거나 활성 상태를 변경할 수 있습니다."
      : "전체 스케줄은 열람할 수 있습니다. 수정과 활성 상태 변경은 본인이 등록한 스케줄에만 가능합니다.";

  $("#schedule-grid").innerHTML = schedules.length
    ? schedules
        .map((schedule) => `
        <article class="schedule-card ${canEditSchedule(schedule) ? "" : "readonly-schedule"}">
          <div class="schedule-card-top">
            <span class="schedule-symbol">◷</span>
            <div><h3>${escapeHtml(schedule.title)}</h3><span class="schedule-id">${escapeHtml(schedule.id)}</span></div>
            ${statusPill(schedule.status)}
          </div>
          <p class="schedule-question">${escapeHtml(schedule.question)}</p>
          <div class="schedule-meta">
            <div><span>반복</span><strong>${escapeHtml(schedule.rule_label)}</strong></div>
            <div><span>다음 실행</span><strong>${escapeHtml(schedule.next_run)}</strong></div>
            <div><span>발송 대상</span><strong>${escapeHtml(schedule.target)}</strong></div>
            <div><span>등록자</span><strong>${escapeHtml(schedule.owner)}</strong></div>
          </div>
          <div class="schedule-card-footer">
            <span class="last-run">최근 실행 · ${escapeHtml(schedule.last_run)}</span>
            <div class="schedule-actions">${scheduleActions(schedule)}</div>
          </div>
        </article>`)
        .join("")
    : `<div class="empty-state"><strong>조건에 맞는 스케줄이 없습니다.</strong><span>검색어 또는 상태 필터를 변경해 보세요.</span></div>`;
}

function activeMetadataItems() {
  return state.portal.metadata?.[state.metadataType] || [];
}

function metadataSearchText(item) {
  return Object.values(item)
    .flatMap((value) => Array.isArray(value) ? value : [value])
    .filter((value) => typeof value === "string" || typeof value === "number")
    .join(" ")
    .toLowerCase();
}

function filteredMetadata() {
  const search = state.metadataSearch.toLowerCase().trim();
  return activeMetadataItems().filter((item) => !search || metadataSearchText(item).includes(search));
}

function metadataTableRow(item) {
  if (state.metadataType === "table_catalog") {
    const filters = item.required_filters?.length ? item.required_filters.join(", ") : "없음";
    return `
      <tr>
        <td class="dataset-cell"><strong>${escapeHtml(item.display_name)}</strong><span>${escapeHtml(item.dataset_key)}</span></td>
        <td><span class="mini-tag">${escapeHtml(item.dataset_family)}</span></td>
        <td><strong>${escapeHtml(item.source_type)}</strong><span class="table-subtle">${escapeHtml(item.source_name)}</span></td>
        <td>${escapeHtml(filters)}</td><td>${escapeHtml(item.owner)}</td><td>${escapeHtml(item.updated_at)}</td><td>${statusPill(item.status)}</td>
        <td><button class="row-action" type="button" data-metadata-detail="${escapeHtml(item.id)}" aria-label="${escapeHtml(item.display_name)} 상세">⋯</button></td>
      </tr>`;
  }
  if (state.metadataType === "main_flow_filters") {
    return `
      <tr>
        <td class="dataset-cell"><strong>${escapeHtml(item.display_name)}</strong><span>${escapeHtml(item.filter_key)}</span></td>
        <td><span class="mini-tag">${escapeHtml(item.semantic_role)}</span></td><td>${escapeHtml(item.value_type)}</td>
        <td>${escapeHtml((item.column_candidates || []).join(", ") || "-")}</td><td>${escapeHtml((item.aliases || []).join(", ") || "-")}</td>
        <td>${escapeHtml(item.owner)}</td><td>${escapeHtml(item.updated_at)}</td><td>${statusPill(item.status)}</td>
      </tr>`;
  }
  return `
    <tr>
      <td class="dataset-cell"><strong>${escapeHtml(item.display_name)}</strong><span>${escapeHtml(item.key)}</span></td>
      <td><span class="mini-tag">${escapeHtml(item.section_label)}</span></td><td>${escapeHtml((item.aliases || []).join(", ") || "-")}</td>
      <td>${escapeHtml((item.question_cues || []).join(", ") || "-")}</td><td class="domain-summary">${escapeHtml(item.summary)}</td>
      <td>${escapeHtml(item.owner)}</td><td>${escapeHtml(item.updated_at)}</td><td>${statusPill(item.status)}</td>
    </tr>`;
}

function renderMetadata() {
  const type = metadataTypes[state.metadataType];
  const metadata = filteredMetadata();
  const allItems = activeMetadataItems();
  $("#metadata-type-title").textContent = type.label;
  $("#metadata-type-description").textContent = type.description;
  $("#metadata-total-label").textContent = type.totalLabel;
  $("#metadata-total").textContent = allItems.length;
  $("#metadata-active-total").textContent = allItems.filter((item) => item.status === "활성").length;
  $("#metadata-review-total").textContent = allItems.filter((item) => item.status === "검토 필요").length;
  $("#metadata-summary-note").textContent = `${type.label} 등록 전 필수 항목과 기존 항목을 확인하세요.`;
  $("#metadata-filter-hint").textContent = type.filterHint;
  $("#metadata-table-head").innerHTML = `<tr>${type.headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr>`;
  $("#metadata-list").innerHTML = metadata.length
    ? metadata.map(metadataTableRow).join("")
    : `<tr><td colspan="${type.headers.length}" class="empty-cell">조건에 맞는 ${escapeHtml(type.label)} 항목이 없습니다.</td></tr>`;
  $$("[data-metadata-type]").forEach((button) => {
    const isSelected = button.dataset.metadataType === state.metadataType;
    button.classList.toggle("active", isSelected);
    button.setAttribute("aria-selected", String(isSelected));
  });
  renderMetadataAuthoring();
}

function authoringData() {
  return state.portal?.metadata_authoring || { contract: {}, examples: {}, recent_results: [] };
}

function activeAuthoringExample() {
  return authoringData().examples?.[state.metadataType] || null;
}

function cloneValue(value) {
  return JSON.parse(JSON.stringify(value));
}

function metadataApiState() {
  return state.metadataApi || { mode: "unavailable", ready: false, preview_only: false, missing: [] };
}

function metadataApiLabel() {
  const api = metadataApiState();
  if (api.preview_only || api.mode === "preview") return "미리보기";
  if (api.ready) return "연결됨";
  return "설정 필요";
}

function metadataApiDetail() {
  const api = metadataApiState();
  if (api.preview_only || api.mode === "preview") {
    return "미리보기 모드 · 외부 API와 MongoDB에는 요청하지 않습니다.";
  }
  if (api.ready) {
    return "외부 메타데이터 API에 요청하고 구조화된 Flow 결과를 표시합니다.";
  }
  return "연결 설정을 확인한 뒤 등록 요청을 실행할 수 있습니다.";
}

function renderMetadataApiIndicator() {
  const label = $("#portal-runtime-label");
  const copy = $("#portal-runtime-copy");
  if (!label || !copy) return;
  const api = metadataApiState();
  if (api.preview_only || api.mode === "preview") {
    label.textContent = "메타데이터 미리보기 모드";
    copy.textContent = "대시보드·사번·이력·스케줄은 더미 화면이며, 메타데이터도 외부 API 없이 안전한 미리보기로 실행됩니다.";
    return;
  }
  if (api.ready) {
    label.textContent = "메타데이터 API 연결 준비됨";
    copy.textContent = "대시보드·사번·이력·스케줄은 현재 더미 화면입니다. 메타데이터 등록만 외부 Flow API로 실행됩니다.";
    return;
  }
  label.textContent = "메타데이터 API 설정 필요";
  copy.textContent = "대시보드·사번·이력·스케줄은 현재 더미 화면입니다. 메타데이터 API는 서버 .env 설정 후 실행할 수 있습니다.";
}

function errorMessageFromResponse(payload, fallback) {
  if (!payload || typeof payload !== "object") return fallback;
  const detail = payload.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object" && typeof detail.message === "string") return detail.message;
  if (typeof payload.message === "string") return payload.message;
  return fallback;
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
    dry_run: "테스트 실행",
    needs_input: "보완 필요",
    skipped: "저장 건너뜀",
    error: "오류",
    not_saved: "미저장",
  };
  const tone = {
    saved: "status-success",
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

function renderMetadataResult() {
  const run = activeMetadataResult();
  const content = $("#metadata-result-content");
  if (!run || !content) return;

  const response = run.response || {};
  const authoring = response.metadata_authoring || {};
  const write = response.write_result || {};
  const validation = authoring.contract_validation || {};
  const resultStatus = flowResultStatus(response.status);
  const isSaveRequestPreview = run.previewOnly && !run.requestedDryRun;

  $("#metadata-result-message").textContent = response.message || "등록 결과를 확인합니다.";
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
    content.innerHTML = `
      <div class="result-section-heading"><div><h4>API 응답</h4><p>실제 연동 시 포털 백엔드는 Chat Output 문자열이 아니라 이 구조화 응답을 보존해 표시합니다.</p></div><span class="mini-tag">api_response</span></div>
      <pre class="api-json">${escapeHtml(JSON.stringify(response, null, 2))}</pre>`;
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
  $("#metadata-example-fields").textContent = `입력에 포함: ${(example.required_input || []).join(" · ")}`;
  $("#metadata-example-raw").textContent = example.raw_text || "";
  renderMetadataResult();
}

function renderSettings() {
  const { settings } = state.portal;
  const metadataStatus = metadataApiLabel();
  const apiItems = [
    { label: settings.api.gaia_endpoint, detail: "Agent 응답 생성 및 결과 수신", status: settings.api.status },
    { label: settings.api.cube_endpoint, detail: "Rich Notification 결과 발송", status: settings.api.status },
    { label: `메타데이터 등록 API  /api/metadata-authoring`, detail: metadataApiDetail(), status: metadataStatus },
    { label: `Callback ${settings.api.callback_endpoint}`, detail: `최종 확인 · ${settings.api.last_checked}`, status: settings.api.status },
  ];
  $("#api-status-list").innerHTML = apiItems
    .map((item) => `
      <div class="api-status-item"><span class="api-status-icon">✓</span><div><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.detail)}</span></div>${statusPill(item.status)}</div>`)
    .join("");
  $("#admin-list").innerHTML = settings.admins
    .map((admin) => `
      <tr><td>${escapeHtml(admin.employee_id)}</td><td><strong>${escapeHtml(admin.name)}</strong></td><td><span class="mini-tag">${escapeHtml(admin.role)}</span></td><td>${escapeHtml(admin.scope)}</td><td>${statusPill(admin.status)}</td><td><button class="row-action" type="button" aria-label="${escapeHtml(admin.name)} 관리자 설정">⋯</button></td></tr>`)
    .join("");
  $("#active-user-min-days").value = settings.usage_policy.active_user_min_distinct_days;
  $("#active-user-min-chats").value = settings.usage_policy.active_user_min_chat_count;
  $("#active-policy-summary").textContent = `최근 ${settings.usage_policy.history_window_days}일 중 서로 다른 일자 ${settings.usage_policy.active_user_min_distinct_days}일 이상, 누적 채팅 ${settings.usage_policy.active_user_min_chat_count}건 이상 사용자를 활성 사용자로 집계합니다.`;
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

function scheduleLabel(repeat, time) {
  const labels = { "평일": "평일", "매일": "매일", "매주": "매주 월요일", "매월": "매월 1일", "한 번만": "한 번만" };
  return `${labels[repeat] || repeat} · ${time}`;
}

function updateSchedulePreview() {
  const form = $("#schedule-form");
  const repeat = form.elements.repeat.value;
  const time = form.elements.time.value;
  $("#next-preview").textContent = `다음 ${scheduleLabel(repeat, time)}`;
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
    form.elements.repeat.value = schedule.repeat || "매일";
    form.elements.time.value = schedule.time || "09:30";
    form.elements.target.value = schedule.target;
  } else {
    $("#schedule-drawer-kicker").textContent = "NEW AUTOMATION";
    $("#schedule-drawer-title").textContent = "새 스케줄 등록";
    $("#schedule-submit").textContent = "스케줄 등록";
    form.elements.repeat.value = "평일";
    form.elements.time.value = "09:30";
  }
  $("#schedule-drawer").setAttribute("aria-label", schedule ? "스케줄 수정" : "새 스케줄 등록");
  updateSchedulePreview();
}

function metadataFormMarkup() {
  const example = activeAuthoringExample();
  const rawText = escapeHtml(example?.raw_text || "");
  const requiredInput = (example?.required_input || []).join(" · ");
  const api = metadataApiState();
  const apiNotice = api.preview_only || api.mode === "preview"
    ? "현재는 미리보기 모드입니다. 외부 메타데이터 API와 MongoDB에는 요청하지 않습니다. 실제 연동은 서버의 .env에서 API 모드를 설정한 뒤 시작됩니다."
    : api.ready
      ? "입력은 포털 서버에서 외부 메타데이터 API로 전달됩니다. API 키와 MongoDB 연결 정보는 브라우저에 노출되지 않습니다."
      : "메타데이터 API 연결 설정이 아직 준비되지 않았습니다. 서버의 .env 값을 확인한 뒤 다시 시도해 주세요.";
  return `
    <div class="drawer-flow-note"><strong>${escapeHtml(example?.flow_label || "메타데이터 등록 Flow")}</strong><span>Chat Input의 <code>input_value</code>가 등록 원문으로 전달됩니다.</span></div>
    <label><span>등록 요청 원문</span><textarea name="raw_text" rows="9" required>${rawText}</textarea><small>포함하면 좋은 정보: ${escapeHtml(requiredInput)}</small></label>
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(errorMessageFromResponse(payload, "메타데이터 등록 API 호출에 실패했습니다."));
    }

    state.metadataResult = normalizeMetadataRun(payload);
    state.metadataResultTab = "process";
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
  document.addEventListener("click", (event) => {
    const nav = event.target.closest("[data-nav]");
    if (nav) {
      event.preventDefault();
      switchView(nav.dataset.nav);
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
      state.metadataResultTab = "process";
      $("#metadata-search").value = "";
      renderMetadata();
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
        showToast("본인이 등록한 스케줄만 수정하거나 상태를 변경할 수 있습니다.");
        return;
      }
      openDrawer("schedule", schedule.id);
      return;
    }

    const pauseButton = event.target.closest("[data-toggle-schedule]");
    if (pauseButton) {
      const schedule = state.portal.schedules.find((item) => item.id === pauseButton.dataset.toggleSchedule);
      if (!schedule || !canEditSchedule(schedule)) {
        showToast("본인이 등록한 스케줄만 수정하거나 상태를 변경할 수 있습니다.");
        return;
      }
      schedule.status = schedule.status === "활성" ? "일시중지" : "활성";
      schedule.next_run = schedule.status === "활성" ? `다음 ${schedule.rule_label}` : "일시중지됨";
      renderSchedules();
      showToast(`${schedule.title} 스케줄을 ${schedule.status === "활성" ? "재개" : "일시중지"}했습니다. (더미 화면)`);
      return;
    }

    if (event.target.closest("[data-schedule-restricted]")) {
      showToast("본인이 등록한 스케줄만 수정하거나 상태를 변경할 수 있습니다.");
      return;
    }
    if (event.target.closest("[data-metadata-detail]")) {
      showToast("상세 편집은 실제 메타데이터 API 연동 단계에서 제공합니다.");
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
    renderMetadata();
  });

  $("#schedule-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    const editingSchedule = state.portal.schedules.find((item) => item.id === state.editingScheduleId);
    if (editingSchedule) {
      if (!canEditSchedule(editingSchedule)) {
        showToast("본인이 등록한 스케줄만 수정하거나 상태를 변경할 수 있습니다.");
        return;
      }
      Object.assign(editingSchedule, {
        title: values.title,
        question: values.question,
        repeat: values.repeat,
        time: values.time,
        rule_label: scheduleLabel(values.repeat, values.time),
        next_run: editingSchedule.status === "활성" ? `다음 ${scheduleLabel(values.repeat, values.time)}` : "일시중지됨",
        target: values.target,
      });
      closeDrawers();
      renderSchedules();
      showToast("스케줄 변경 사항을 저장했습니다. 실제 MongoDB에는 아직 저장하지 않았습니다.");
      return;
    }

    const index = state.portal.schedules.length + 82;
    state.portal.schedules.unshift({
      id: `SCH-2026-${String(index).padStart(3, "0")}`,
      title: values.title,
      question: values.question,
      repeat: values.repeat,
      time: values.time,
      rule_label: scheduleLabel(values.repeat, values.time),
      next_run: `다음 ${scheduleLabel(values.repeat, values.time)}`,
      target: values.target,
      owner: viewerId(),
      status: "활성",
      last_run: "아직 실행 전",
    });
    state.scheduleScope = "mine";
    closeDrawers();
    renderSchedules();
    switchView("schedules");
    showToast("새 스케줄을 등록했습니다. 실제 저장은 아직 연결되지 않았습니다.");
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

  $("#activity-policy-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!isAdmin()) {
      showToast("관리자만 설정을 확인할 수 있습니다.");
      return;
    }
    const values = Object.fromEntries(new FormData(event.currentTarget));
    state.portal.settings.usage_policy.active_user_min_distinct_days = Math.max(1, Number(values.minDays) || 1);
    state.portal.settings.usage_policy.active_user_min_chat_count = Math.max(1, Number(values.minChats) || 1);
    state.portal.dashboard = buildDashboardFromHistory(
      state.portal.usage_history,
      state.portal.settings.usage_policy,
    );
    renderDashboard();
    renderSettings();
    showToast("활성 사용자 기준을 적용했습니다. 더미 이력을 다시 집계했습니다.");
  });

  $("#schedule-form [name='repeat']").addEventListener("change", updateSchedulePreview);
  $("#schedule-form [name='time']").addEventListener("input", updateSchedulePreview);
}

async function initialize() {
  try {
    const previewRole = new URLSearchParams(window.location.search).get("preview_role");
    const suffix = previewRole ? `?preview_role=${encodeURIComponent(previewRole)}` : "";
    const response = await fetch(`/api/mock/portal${suffix}`);
    if (!response.ok) throw new Error("dummy data request failed");
    state.portal = await response.json();
  } catch (error) {
    console.error(error);
    document.body.innerHTML = `<main class="fatal-error"><h1>미리보기 데이터를 불러오지 못했습니다.</h1><p>서버를 다시 실행한 뒤 새로고침해 주세요.</p></main>`;
    return;
  }

  try {
    const response = await fetch("/api/metadata-authoring/status");
    if (!response.ok) throw new Error("metadata authoring status request failed");
    state.metadataApi = await response.json();
  } catch (error) {
    // The existing dashboard, schedule, and employee-preview data must remain
    // available even when only the new metadata API status route is unavailable.
    console.warn("metadata authoring status unavailable", error);
    state.metadataApi = null;
  }

  renderAccessControls();
  renderMetadataApiIndicator();
  renderDashboard();
  renderSchedules();
  renderMetadata();
  renderSettings();
  bindEvents();
}

initialize();
