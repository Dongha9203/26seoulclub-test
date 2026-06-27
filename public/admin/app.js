// ── 인증/공통 API 클라이언트 ──────────────────────────────────────

const API_BASE = "/api/admin";

function getToken() {
  return sessionStorage.getItem("admin_token");
}

if (!getToken()) {
  window.location.replace("login.html");
}

async function api(path, options = {}) {
  const res = await fetch(API_BASE + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer " + getToken(),
      ...(options.headers || {}),
    },
  });

  if (res.status === 401) {
    sessionStorage.removeItem("admin_token");
    window.location.replace("login.html");
    throw new Error("인증이 만료되었습니다.");
  }

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || "요청 처리 중 오류가 발생했습니다.");
    err.status = res.status;
    throw err;
  }
  return data;
}

async function apiUpload(path, formData) {
  const res = await fetch(API_BASE + path, {
    method: "POST",
    headers: { Authorization: "Bearer " + getToken() },
    body: formData,
  });
  if (res.status === 401) {
    sessionStorage.removeItem("admin_token");
    window.location.replace("login.html");
    throw new Error("인증이 만료되었습니다.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || "업로드 중 오류가 발생했습니다.");
    err.status = res.status;
    throw err;
  }
  return data;
}

document.getElementById("logout-button").addEventListener("click", () => {
  sessionStorage.removeItem("admin_token");
  window.location.replace("login.html");
});

// ── 응답 캐시 (설정 화면 메뉴 전환 속도 개선) ────────────────────
const _apiCache = {};
const _CACHE_TTL_MS = {
  "/settings": 5 * 60 * 1000,
  "/kb/manual-source-guide": 60 * 60 * 1000,
};

async function cachedApi(path) {
  const ttl = _CACHE_TTL_MS[path];
  const entry = _apiCache[path];
  if (ttl && entry && Date.now() - entry.ts < ttl) return entry.data;
  const data = await api(path);
  if (ttl) _apiCache[path] = { data, ts: Date.now() };
  return data;
}

function invalidateCache(prefix) {
  Object.keys(_apiCache).forEach((k) => { if (k.startsWith(prefix)) delete _apiCache[k]; });
}

// ── 크론 동기화 상태 배너 ──────────────────────────────────────

function _isKstToday(utcIsoString) {
  const d = new Date(utcIsoString);
  const kst = d.toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul" });
  const now = new Date().toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul" });
  return kst === now;
}

async function renderCronStatus() {
  const el = document.getElementById("cron-status-banner");
  if (!el) return;
  try {
    const d = await api("/cron/status");
    let cls, icon, label, timeText = "";
    if (!d.status) {
      cls = "status-warn"; icon = "⚠️"; label = "동기화 기록 없음";
    } else if (d.status !== "ok") {
      cls = "status-error"; icon = "❌"; label = "노션 동기화 오류";
      if (d.started_at) {
        timeText = "최근: " + new Date(d.started_at).toLocaleString("ko-KR", {
          timeZone: "Asia/Seoul", month: "numeric", day: "numeric",
          hour: "2-digit", minute: "2-digit",
        });
      }
    } else if (_isKstToday(d.started_at)) {
      cls = "status-ok"; icon = "🟢"; label = "노션 동기화 정상";
      timeText = "오늘 " + new Date(d.started_at).toLocaleString("ko-KR", {
        timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit",
      }) + " 완료";
    } else {
      cls = "status-warn"; icon = "⚠️"; label = "오늘 동기화 미실행";
      timeText = "최근: " + new Date(d.started_at).toLocaleString("ko-KR", {
        timeZone: "Asia/Seoul", month: "numeric", day: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    }
    el.className = cls;
    el.innerHTML =
      `<span class="cron-label">${icon} ${label}</span>` +
      (timeText ? `<span class="cron-time">${timeText}</span>` : "");
  } catch {
    /* 배너 오류는 조용히 무시 */
  }
}

renderCronStatus();

// ── 공용 헬퍼 ──────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function cardWithDetail(title, subtitle, detailHtml, bodyHtml, extraHeaderHtml = "") {
  const id = "detail-" + Math.random().toString(36).slice(2);
  return `
    <div class="card">
      <div class="card-header-row">
        <div>
          <h2>${escapeHtml(title)}</h2>
          ${subtitle ? `<p class="card-subtitle">${escapeHtml(subtitle)}</p>` : ""}
        </div>
        <div style="display:flex; gap:8px; align-items:center;">
          ${extraHeaderHtml}
          <button class="detail-toggle" data-toggle="${id}">상세내용</button>
        </div>
      </div>
      <div class="accordion-panel" id="${id}">
        <div class="accordion-inner">${detailHtml}</div>
      </div>
      ${bodyHtml}
    </div>
  `;
}

function bindAccordions(root) {
  root.querySelectorAll(".detail-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const panel = document.getElementById(btn.dataset.toggle);
      panel.classList.toggle("open");
    });
  });
}

// ── 기간별 조회조건 (날짜 범위 필터) ────────────────────────────────
// 백엔드 api/admin.py의 _add_months와 동일한 말일 클램프 규칙을 따릅니다
// (예: 1/31 + 1개월 = 2/28). 여기서의 검사는 즉각적인 사용자 피드백용이고,
// 실제 검증은 항상 백엔드(_validate_date_range)가 한 번 더 수행합니다.
function addMonthsLocal(isoDate, months) {
  const d = new Date(isoDate + "T00:00:00");
  const day = d.getDate();
  d.setDate(1);
  d.setMonth(d.getMonth() + months);
  const lastDay = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
  d.setDate(Math.min(day, lastDay));
  return d;
}

function toISODate(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function dateRangeFilterHtml(maxMonths, startDate, endDate, idPrefix) {
  return `
    <div class="date-range-filter" style="margin:0 0 16px; padding:10px 12px; border:1px solid #e2e2e2; border-radius:6px; background:#fafafa;">
      <div class="date-range-row">
        <strong style="flex-shrink:0;">기간별 조회조건</strong>
        <span class="muted" style="flex-shrink:0;">(최대 ${maxMonths}개월)</span>
        <input type="date" id="${idPrefix}-start" value="${startDate || ""}" style="width:140px; flex-shrink:0;">
        <span style="flex-shrink:0;">~</span>
        <input type="date" id="${idPrefix}-end" value="${endDate || ""}" style="width:140px; flex-shrink:0;">
        <button id="${idPrefix}-search-btn" class="btn btn-secondary" style="flex-shrink:0;">조회</button>
        <button id="${idPrefix}-reset-btn" class="btn btn-secondary" style="flex-shrink:0;">초기화</button>
        <span class="muted" style="margin-left:8px; flex-shrink:0;">
          ${startDate && endDate ? `현재 조회 중: ${startDate} ~ ${endDate}` : "현재 전체 기간 조회 중"}
        </span>
      </div>
    </div>
  `;
}

function bindDateRangeFilter(idPrefix, maxMonths, onSearch, onReset) {
  const searchBtn = document.getElementById(`${idPrefix}-search-btn`);
  const resetBtn = document.getElementById(`${idPrefix}-reset-btn`);
  searchBtn.addEventListener("click", async () => {
    const startVal = document.getElementById(`${idPrefix}-start`).value;
    const endVal = document.getElementById(`${idPrefix}-end`).value;
    if (!startVal || !endVal) {
      alert("시작일과 종료일을 모두 입력해주세요.");
      return;
    }
    if (startVal > endVal) {
      alert("시작일은 종료일보다 늦을 수 없습니다.");
      return;
    }
    const maxEnd = toISODate(addMonthsLocal(startVal, maxMonths));
    if (endVal > maxEnd) {
      alert(`최대 ${maxMonths}개월까지 조회할 수 있습니다.`);
      return;
    }
    searchBtn.disabled = true;
    resetBtn.disabled = true;
    searchBtn.innerHTML = `<span class="spinner"></span> 조회 중...`;
    try {
      await onSearch(startVal, endVal);
    } catch (err) {
      alert("조회 중 오류가 발생했습니다: " + err.message);
      searchBtn.disabled = false;
      resetBtn.disabled = false;
      searchBtn.textContent = "조회";
    }
  });
  resetBtn.addEventListener("click", async () => {
    searchBtn.disabled = true;
    resetBtn.disabled = true;
    resetBtn.innerHTML = `<span class="spinner"></span> 초기화 중...`;
    try {
      await onReset();
    } catch (err) {
      alert("초기화 중 오류가 발생했습니다: " + err.message);
      searchBtn.disabled = false;
      resetBtn.disabled = false;
      resetBtn.textContent = "초기화";
    }
  });
}

// ── 라우터 ─────────────────────────────────────────────────────

const routes = {
  "daily-counts": renderDailyCounts,
  "qa-logs": renderQaLogs,
  "incomplete": () => renderActionList("incomplete", "불완전 답변 조회", "검색에 실패했거나, 질문이 너무 모호해서(예: '이거 뭐예요?'처럼 무엇에 대한 질문인지 알 수 있는 단어가 없는 경우) 챗봇이 직접 답하지 않고 운영팀 연락처만 안내한 건"),
  "unresolved": () => renderActionList("unresolved", "미해결 답변 조회", "지식 베이스에 내용이 없거나, 프로그램과 무관한 질문(예: '오늘 날씨 어때요?')이라 답변할 수 없어서, 챗봇이 직접 답하지 않고 운영팀 연락처만 안내한 건"),
  "failure-report": renderFailureReport,
  "operation-team": renderOperationTeam,
  "kb": renderKb,
  "tone": renderTone,
  "keywords": renderKeywords,
  "api-params": renderApiParams,
  "change-password": renderChangePassword,
};

function currentRoute() {
  return (window.location.hash || "#daily-counts").slice(1);
}

async function navigate() {
  const route = currentRoute();
  document.querySelectorAll(".nav-item[data-route]").forEach((el) => {
    el.classList.toggle("active", el.dataset.route === route);
  });
  const main = document.getElementById("main");
  const handler = routes[route] || routes["daily-counts"];
  main.innerHTML = `<p class="muted">불러오는 중...</p>`;
  try {
    await handler(main);
  } catch (err) {
    main.innerHTML = `<div class="card"><p class="error-text">${escapeHtml(err.message)}</p></div>`;
  }
}

document.querySelectorAll(".nav-item[data-route]").forEach((el) => {
  el.addEventListener("click", () => { window.location.hash = "#" + el.dataset.route; });
});
window.addEventListener("hashchange", navigate);

// ── ① 모니터링 ─────────────────────────────────────────────────

async function renderDailyCounts(main, page = 0, startDate = null, endDate = null) {
  const limit = 30;
  const maxMonths = 3;
  const dateQuery = (startDate && endDate) ? `&start_date=${startDate}&end_date=${endDate}` : "";
  const data = await api(`/monitoring/daily-counts?limit=${limit}&offset=${page * limit}${dateQuery}`);
  const rows = data.daily_counts.map(
    (d) => `<tr><td>${escapeHtml(d.day)}</td><td>${d.count}</td></tr>`
  ).join("");
  const hasMore = data.daily_counts.length === limit;
  main.innerHTML = `<h1>일별 질의/응답 건수</h1>` + cardWithDetail(
    "날짜별 집계",
    "최근 날짜부터 날짜별로 집계한 결과입니다. (자료가 최고 1년을 보관하고 이후 자동삭제 되어, 1년이 지난 건은 집계에서 빠집니다.)",
    "매일 챗봇에 들어온 질문 수를 날짜별로 보여줍니다. 운영 추이를 파악하는 데 사용합니다.",
    dateRangeFilterHtml(maxMonths, startDate, endDate, "daily-counts-filter")
    + (data.daily_counts.length
      ? `<div class="table-scroll"><table><thead><tr><th>날짜</th><th>건수</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<p class="muted">데이터가 없습니다.</p>`)
    + `<div class="pagination-row">
        <button id="daily-counts-prev" class="btn btn-secondary" ${page === 0 ? "disabled" : ""}>이전</button>
        <span>페이지 ${page + 1}</span>
        <button id="daily-counts-more" class="btn btn-secondary" ${hasMore ? "" : "disabled"}>더보기</button>
        <span class="muted pagination-info">(최대 30건/1페이지)</span>
        <button id="daily-counts-first" class="btn btn-secondary" ${page === 0 ? "disabled" : ""}>처음 페이지로 이동</button>
      </div>`
  );
  bindAccordions(main);
  bindDateRangeFilter("daily-counts-filter", maxMonths,
    (s, e) => renderDailyCounts(main, 0, s, e),
    () => renderDailyCounts(main, 0, null, null));

  document.getElementById("daily-counts-prev").addEventListener("click", () => {
    renderDailyCounts(main, page - 1, startDate, endDate);
  });
  document.getElementById("daily-counts-more").addEventListener("click", () => {
    renderDailyCounts(main, page + 1, startDate, endDate);
  });
  document.getElementById("daily-counts-first").addEventListener("click", () => {
    renderDailyCounts(main, 0, startDate, endDate);
  });
}

async function renderQaLogs(main, page = 0, startDate = null, endDate = null) {
  const limit = 30;
  const maxMonths = 1;
  const dateQuery = (startDate && endDate) ? `&start_date=${startDate}&end_date=${endDate}` : "";
  const data = await api(`/monitoring/qa-logs?limit=${limit}&offset=${page * limit}${dateQuery}`);
  const rows = data.logs.map((l) => `
    <tr>
      <td>${escapeHtml(new Date(l.timestamp).toLocaleString("ko-KR"))}</td>
      <td>${escapeHtml(l.question)}</td>
      <td>${escapeHtml((l.answer || "").slice(0, 80))}${(l.answer || "").length > 80 ? "…" : ""}</td>
      <td>${l.failure_cause ? escapeHtml(l.failure_cause) : "-"}</td>
    </tr>
  `).join("");
  const hasMore = data.logs.length === limit;
  main.innerHTML = `<h1>질의-답변 연계조회</h1>` + cardWithDetail(
    "최근 질의-답변",
    "사용자 질문과 챗봇 답변을 함께 보여줍니다. (이 자료는 최고 1년을 보관하고, 이후 자동삭제 됩니다.)",
    "사용자의 질문과 챗봇이 실제로 보낸 답변을 짝지어 확인할 수 있는 화면입니다.",
    dateRangeFilterHtml(maxMonths, startDate, endDate, "qa-logs-filter")
    + (data.logs.length
      ? `<div class="table-scroll"><table><thead><tr><th>시각</th><th>질문</th><th>답변</th><th>실패원인</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<p class="muted">아직 기록된 로그가 없습니다.</p>`)
    + `<div class="pagination-row">
        <button id="qa-logs-prev" class="btn btn-secondary" ${page === 0 ? "disabled" : ""}>이전</button>
        <span>페이지 ${page + 1}</span>
        <button id="qa-logs-more" class="btn btn-secondary" ${hasMore ? "" : "disabled"}>더보기</button>
        <span class="muted pagination-info">(최대 30건/1페이지)</span>
        <button id="qa-logs-first" class="btn btn-secondary" ${page === 0 ? "disabled" : ""}>처음 페이지로 이동</button>
      </div>`
  );
  bindAccordions(main);
  bindDateRangeFilter("qa-logs-filter", maxMonths,
    (s, e) => renderQaLogs(main, 0, s, e),
    () => renderQaLogs(main, 0, null, null));

  document.getElementById("qa-logs-prev").addEventListener("click", () => {
    renderQaLogs(main, page - 1, startDate, endDate);
  });
  document.getElementById("qa-logs-more").addEventListener("click", () => {
    renderQaLogs(main, page + 1, startDate, endDate);
  });
  document.getElementById("qa-logs-first").addEventListener("click", () => {
    renderQaLogs(main, 0, startDate, endDate);
  });
}

// ── ② 조치관리 ─────────────────────────────────────────────────

async function renderActionList(endpoint, title, subtitle, page = 0, startDate = null, endDate = null) {
  const main = document.getElementById("main");
  const limit = 30;
  const maxMonths = 3;
  const dateQuery = (startDate && endDate) ? `&start_date=${startDate}&end_date=${endDate}` : "";
  const data = await api(`/actions/${endpoint}?limit=${limit}&offset=${page * limit}${dateQuery}`);

  const rows = data.logs.map((l) => `
    <tr>
      <td>${escapeHtml(new Date(l.timestamp).toLocaleString("ko-KR"))}</td>
      <td>${escapeHtml(l.question)}</td>
      <td>${escapeHtml(l.failure_cause)}</td>
      <td><button class="btn btn-danger" data-resolve="${l.log_id}">삭제</button></td>
    </tr>
  `).join("");

  const hasMore = data.logs.length === limit;
  main.innerHTML = `<h1>${escapeHtml(title)}</h1>` + cardWithDetail(
    title, subtitle,
    "이 목록의 항목들은 운영자가 노션 페이지나 데이터를 직접 찾아 수정해야 해결되는 사항입니다. "
    + "노션/데이터 수정을 완료했다면 \"삭제\" 버튼을 눌러 이 목록에서 제거해 주세요 "
    + "(일별 질의/응답 건수, 원인별 집계 리포트 등 통계에는 계속 남습니다 — 이 목록에서만 사라집니다).",
    dateRangeFilterHtml(maxMonths, startDate, endDate, "action-list-filter")
    + (data.logs.length
      ? `<div class="table-scroll"><table><thead><tr><th>시각</th><th>질문</th><th>실패원인</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<p class="muted">해당 조건에 해당하는 항목이 없습니다.</p>`)
    + `<div class="pagination-row">
        <button id="action-list-prev" class="btn btn-secondary" ${page === 0 ? "disabled" : ""}>이전</button>
        <span>페이지 ${page + 1}</span>
        <button id="action-list-more" class="btn btn-secondary" ${hasMore ? "" : "disabled"}>더보기</button>
        <span class="muted pagination-info">(최대 30건/1페이지)</span>
        <button id="action-list-first" class="btn btn-secondary" ${page === 0 ? "disabled" : ""}>처음 페이지로 이동</button>
      </div>`
  );
  bindAccordions(main);
  bindDateRangeFilter("action-list-filter", maxMonths,
    (s, e) => renderActionList(endpoint, title, subtitle, 0, s, e),
    () => renderActionList(endpoint, title, subtitle, 0, null, null));

  document.getElementById("action-list-prev").addEventListener("click", () => {
    renderActionList(endpoint, title, subtitle, page - 1, startDate, endDate);
  });
  document.getElementById("action-list-more").addEventListener("click", () => {
    renderActionList(endpoint, title, subtitle, page + 1, startDate, endDate);
  });
  document.getElementById("action-list-first").addEventListener("click", () => {
    renderActionList(endpoint, title, subtitle, 0, startDate, endDate);
  });

  main.querySelectorAll("[data-resolve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("노션/데이터 수정을 완료해서 이 목록에서 삭제할까요?")) return;
      try {
        await api(`/actions/${btn.dataset.resolve}`, { method: "DELETE" });
        renderActionList(endpoint, title, subtitle, page);
      } catch (err) {
        alert("삭제 실패: " + err.message);
      }
    });
  });
}

async function renderFailureReport(main, startDate = null, endDate = null) {
  const maxMonths = 12;
  const dateQuery = (startDate && endDate) ? `?start_date=${startDate}&end_date=${endDate}` : "";
  const data = await api(`/actions/failure-report${dateQuery}`);
  const total = Object.values(data.counts).reduce((sum, cnt) => sum + cnt, 0);
  const rows = Object.entries(data.counts).map(
    ([cause, cnt]) => `<tr><td>${escapeHtml(cause)}</td><td>${cnt}</td></tr>`
  ).join("") + `<tr><td><strong>합계</strong></td><td><strong>${total}</strong></td></tr>`;
  main.innerHTML = `<h1>원인별 집계 리포트</h1>` + cardWithDetail(
    "실패 원인별 집계",
    "최근 1년간 누적된 건수입니다. (자료가 최고 1년을 보관하고 이후 자동삭제 되어, 1년이 지난 건은 집계에서 빠집니다.)",
    "검색 실패의 원인을 5가지로 분류해 건수를 보여줍니다." +
    `<ul style="margin:8px 0 0; padding-left:18px;">
      <li style="margin-bottom:6px;"><strong>지식DB공백</strong> — 동아리ON 운영과 관련은 있어 보이는 질문인데, 지식 베이스(Knowledge Base)에 그 내용을 다루는 문서가 아예 없어서 챗봇이 직접 답하지 못하고 운영팀 연락처를 안내한 경우입니다. 해당 내용을 지식 베이스에 새로 등록하면 다음부터는 답할 수 있습니다.</li>
      <li style="margin-bottom:6px;"><strong>검색실패</strong> — 동아리ON 운영과 명백히 관련된 질문인데, 지식 베이스에 관련 문서는 있지만 그 안에서 구체적인 답을 찾지 못해 운영팀 연락처를 안내한 경우입니다. 기존 문서 내용을 더 구체적으로 보강하면 도움이 됩니다.</li>
      <li style="margin-bottom:6px;"><strong>질문모호성</strong> — 질문에 무엇을 묻는지 알 수 있는 핵심 단어가 거의 없어(예: "이거 뭐예요?"처럼 대상이 빠진 질문) 챗봇이 바로 운영팀 연락처를 안내한 경우입니다. 사용자가 질문을 좀 더 구체적으로 다시 입력하면 해결되는 경우가 많습니다.</li>
      <li style="margin-bottom:6px;"><strong>정책밖요청</strong> — 날씨, 요리, 일반 상식, 다른 서비스 문의처럼 동아리ON 운영과 전혀 무관한 질문이라 챗봇이 답변 대상이 아니라고 안내한 경우입니다. 별도 조치가 필요 없는 정상적인 안내입니다.</li>
      <li><strong>API오류</strong> — 검색이나 질문 자체와는 무관하게, Claude API 호출이 일시적으로 실패해서(서버 과부하, 네트워크 오류 등) 답변을 만들지 못하고 운영팀 연락처를 안내한 경우입니다. 가끔 한두 건 보이는 건 정상이지만, 짧은 시간에 건수가 많이 쌓이면 API 사용량/네트워크 상태를 확인해 보세요.</li>
    </ul>`,
    dateRangeFilterHtml(maxMonths, startDate, endDate, "failure-report-filter")
    + `<div class="table-scroll"><table><thead><tr><th>원인</th><th>건수</th></tr></thead><tbody>${rows}</tbody></table></div>`
  );
  bindAccordions(main);
  bindDateRangeFilter("failure-report-filter", maxMonths,
    (s, e) => renderFailureReport(main, s, e),
    () => renderFailureReport(main, null, null));
}

// ── ③ 운영설정 ─────────────────────────────────────────────────

async function renderOperationTeam(main) {
  const settings = await cachedApi("/settings");
  const t = settings.operation_team;
  main.innerHTML = `<h1>담당자 연락처 문구</h1>` + cardWithDetail(
    "운영팀 연락처", "검색 실패/에스컬레이션 응답에 표시되는 연락처입니다.",
    "챗봇이 답변하지 못하거나 운영팀 연결이 필요할 때 사용자에게 보여주는 연락처 정보입니다. 여기서 수정하면 다음 질문부터 바로 반영됩니다.",
    `
    <form id="op-team-form">
      <label>운영팀 이름</label><input name="name" value="${escapeHtml(t.name)}" required>
      <label>주소</label><input name="address" value="${escapeHtml(t.address)}" required>
      <label>전화번호</label><input name="phone" value="${escapeHtml(t.phone)}" required>
      <label>이메일 (줄바꿈으로 여러 개 입력)</label>
      <textarea name="email_list" rows="2" required>${escapeHtml((t.email_list || []).join("\n"))}</textarea>
      <label>운영시간</label><input name="operating_hours" value="${escapeHtml(t.operating_hours)}" required>
      <div style="margin-top:14px; display:flex; align-items:center; gap:10px;">
        <button type="submit" class="btn">저장</button>
        <span id="op-team-result"></span>
      </div>
    </form>
    `
  );
  bindAccordions(main);

  document.getElementById("op-team-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const resultEl = document.getElementById("op-team-result");
    resultEl.textContent = "";
    try {
      await api("/settings/operation-team", {
        method: "PUT",
        body: JSON.stringify({
          name: f.name.value,
          address: f.address.value,
          phone: f.phone.value,
          email_list: f.email_list.value.split("\n").map((s) => s.trim()).filter(Boolean),
          operating_hours: f.operating_hours.value,
        }),
      });
      invalidateCache("/settings");
      resultEl.innerHTML = `<span class="success-text">저장되었습니다.</span>`;
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">${escapeHtml(err.message)}</span>`;
    }
  });
}

async function renderKb(main) {
  const [docs, lastSync, guide] = await Promise.all([
    api("/kb/documents"), api("/kb/notion/last-sync"), cachedApi("/kb/manual-source-guide"),
  ]);

  const guideRows = guide.guide.map(
    (g) => `<tr><td>${escapeHtml(g.source_type)}</td><td>${escapeHtml(g.처리방식)}</td></tr>`
  ).join("");

  const rows = docs.documents.map((d) => `
    <tr>
      <td>${escapeHtml(d.title)}</td>
      <td>${escapeHtml(d.source_type)}</td>
      <td>${escapeHtml(d.source_origin)}</td>
      <td>${escapeHtml(d.category)}</td>
      <td>
        ${d.is_editable ? `<button class="btn btn-secondary" data-embed="${d.doc_id}">갱신</button>` : ""}
        <button class="btn btn-danger" data-delete="${d.doc_id}" ${d.is_editable ? "" : "disabled"}>삭제</button>
      </td>
    </tr>
  `).join("");

  main.innerHTML = `<h1>Knowledge Base 조회/관리</h1>
    <div class="card">
      <div class="card-header-row">
        <div>
          <h2>노션 즉시 갱신</h2>
          <p class="card-subtitle" id="last-sync-text">
            ${lastSync.last_synced_at
              ? `마지막 갱신: ${escapeHtml(new Date(lastSync.last_synced_at).toLocaleString("ko-KR"))} (${escapeHtml(lastSync.mode)})`
              : "아직 갱신 기록이 없습니다."}
          </p>
        </div>
        <button id="notion-refresh-btn" class="btn">지금 갱신</button>
      </div>
      <div id="notion-refresh-result" style="margin-top: 10px;"></div>
      <p class="muted">노션을 수정한 직후 바로 반영하려면 이 버튼을 사용하세요.</p>
    </div>
    ` + cardWithDetail(
      "전체 문서 목록", "노션 소스는 조회 전용이며 삭제 버튼이 비활성화됩니다.",
      `수동 업로드 가능한 소스 타입별 처리 방식:
       <div class="table-scroll"><table><thead><tr><th>소스 타입</th><th>처리 방식</th></tr></thead><tbody>${guideRows}</tbody></table></div>
       <p style="margin-top:10px;">파일 업로드나 구글 스프레드시트로 추가한 문서는 목록에 바로 보이지만, "갱신" 버튼을 눌러 확인해줘야 챗봇이 실제로 그 내용을 찾아 답변할 수 있습니다.</p>
       <p style="margin-top:10px;">검색 정확도를 높이기 위해, 소제목 스타일이 없는 긴 문서(800자 이상)는 파일 1개를 업로드해도 "(파트 1)", "(파트 2)"처럼 여러 건으로 자동 분할되어 등록됩니다. 같은 출처(파일명)로 여러 줄이 보이는 건 오류가 아니라 정상 동작입니다.</p>
       <p style="margin-top:10px;">구글 스프레드시트는 시트의 행(row) 1개가 문서 1개로 등록됩니다. 시트에 10개 행이 있으면 문서도 10건 등록되는 게 정상이며, 각 문서의 제목은 시트의 "질문/제목" 컬럼 값을 그대로 사용합니다.</p>`,
      `
      <div style="display:flex; gap:24px; flex-wrap:wrap; margin-bottom:16px;">
        <div>
          <label>파일 업로드 (.docx/.pdf/.xlsx)</label>
          <input type="file" id="kb-file-input" accept=".docx,.pdf,.xlsx">
          <button id="kb-file-upload-btn" class="btn" style="margin-top:8px;">업로드</button>
          <div id="kb-file-result"></div>
        </div>
        <div>
          <label>구글 스프레드시트 URL</label>
          <input type="text" id="kb-sheet-input" placeholder="https://docs.google.com/spreadsheets/...">
          <button id="kb-sheet-upload-btn" class="btn" style="margin-top:8px;">가져오기</button>
          <div id="kb-sheet-result"></div>
        </div>
      </div>
      <div style="margin-bottom:16px; display:flex; align-items:center; gap:10px;">
        <button id="kb-embed-all-btn" class="btn btn-secondary">전체 갱신</button>
        <span class="muted">업로드한 문서·노션·캘린더를 통틀어 아직 반영되지 않은 것을 한 번에 재시도합니다 (행이 많은 시트 등록 후, 또는 "지금 갱신" 중 임베딩 실패가 있었을 때 추천).</span>
        <span id="kb-embed-all-result"></span>
      </div>
      ${docs.documents.length
        ? `<div class="table-scroll"><table><thead><tr><th>제목</th><th>유형</th><th>출처</th><th>카테고리</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="muted">등록된 문서가 없습니다.</p>`}
      `
    );
  bindAccordions(main);

  document.getElementById("notion-refresh-btn").addEventListener("click", async (e) => {
    const btn = e.target;
    const resultEl = document.getElementById("notion-refresh-result");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> 갱신 중...`;
    resultEl.innerHTML = "";
    try {
      const result = await api("/kb/notion/refresh", { method: "POST" });
      resultEl.innerHTML = `<span class="success-text">${escapeHtml(result.summary_text)}</span>`;
      document.getElementById("last-sync-text").textContent =
        `마지막 갱신: ${new Date(result.last_synced_at).toLocaleString("ko-KR")} (수동)`;
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">갱신 실패: ${escapeHtml(err.message)}</span>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "지금 갱신";
    }
  });

  main.querySelectorAll("[data-delete]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("이 문서를 삭제할까요?")) return;
      const originalText = btn.textContent;
      btn.disabled = true;
      btn.innerHTML = `<span class="spinner"></span> 삭제 중...`;
      try {
        await api(`/kb/documents/${btn.dataset.delete}`, { method: "DELETE" });
        renderKb(main);
      } catch (err) {
        alert("삭제 실패: " + err.message);
        btn.disabled = false;
        btn.textContent = originalText;
      }
    });
  });

  main.querySelectorAll("[data-embed]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("이 문서를 지식베이스에 반영하시겠습니까?")) return;
      btn.disabled = true;
      try {
        await api(`/kb/documents/${btn.dataset.embed}/embed`, { method: "POST" });
        alert("지식베이스에 반영되었습니다.");
      } catch (err) {
        alert("반영 실패: " + err.message);
      } finally {
        btn.disabled = false;
      }
    });
  });

  document.getElementById("kb-embed-all-btn").addEventListener("click", async (e) => {
    const btn = e.target;
    const resultEl = document.getElementById("kb-embed-all-result");
    if (!confirm("업로드한 문서 중 반영되지 않은 것을 모두 갱신합니다. 문서가 많으면 시간이 걸릴 수 있습니다. 계속할까요?")) return;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> 갱신 중입니다... (문서가 많으면 시간이 걸릴 수 있습니다)`;
    resultEl.textContent = "";
    try {
      const result = await api("/kb/documents/embed-all", { method: "POST" });
      const failNote = result.failed ? ` (실패 ${result.failed}건)` : "";
      resultEl.innerHTML = result.embedded
        ? `<span class="success-text">${result.embedded}건 반영 완료${failNote}</span>`
        : `<span class="muted">반영할 문서가 없습니다.</span>`;
      renderKb(main);
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">일괄 갱신 실패: ${escapeHtml(err.message)}</span>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "전체 갱신";
    }
  });

  document.getElementById("kb-file-upload-btn").addEventListener("click", async (e) => {
    const btn = e.target;
    const input = document.getElementById("kb-file-input");
    const resultEl = document.getElementById("kb-file-result");
    if (!input.files.length) {
      resultEl.innerHTML = `<span class="error-text">파일을 선택해주세요.</span>`;
      return;
    }
    const fd = new FormData();
    fd.append("file", input.files[0]);
    btn.disabled = true;
    resultEl.innerHTML = `<span class="spinner"></span> 업로드 중입니다... (문서가 많으면 시간이 걸릴 수 있습니다)`;
    try {
      const result = await apiUpload("/kb/upload", fd);
      const splitNote = result.inserted > 1
        ? ` (검색 정확도를 위해 긴 문서가 ${result.inserted}건으로 자동 분할되었습니다)`
        : "";
      resultEl.innerHTML = `<span class="success-text">${result.inserted}건 저장 완료${splitNote}</span>`;
      renderKb(main);
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">${escapeHtml(err.message)}</span>`;
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("kb-sheet-upload-btn").addEventListener("click", async (e) => {
    const btn = e.target;
    const input = document.getElementById("kb-sheet-input");
    const resultEl = document.getElementById("kb-sheet-result");
    btn.disabled = true;
    resultEl.innerHTML = `<span class="spinner"></span> 가져오는 중입니다... (문서가 많으면 시간이 걸릴 수 있습니다)`;
    try {
      const result = await api("/kb/google-sheet", {
        method: "POST", body: JSON.stringify({ url: input.value }),
      });
      resultEl.innerHTML =
        `<span class="success-text">${result.inserted}건 저장 완료 (시트의 행 1개 = 문서 1개로 등록됩니다)</span>`;
      renderKb(main);
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">${escapeHtml(err.message)}</span>`;
    } finally {
      btn.disabled = false;
    }
  });
}

const TONE_LABELS = {
  personality: "성격(personality)", language_purity: "언어순도(language_purity)",
  vip_consistency: "VIP 일관성(vip_consistency)", formality: "격식(formality)",
  channel: "채널(channel)", emotional_labor: "감정노동(emotional_labor)",
  persona: "역할(persona)", factuality: "사실성(factuality)",
};

async function renderTone(main) {
  const settings = await cachedApi("/settings");
  const fields = Object.entries(TONE_LABELS).map(([key, label]) => `
    <label>${escapeHtml(label)}</label>
    <textarea name="${key}" rows="2" required>${escapeHtml(settings.tone_elements[key])}</textarea>
  `).join("");

  main.innerHTML = `<h1>톤 설정 관리</h1>` + cardWithDetail(
    "브랜드 톤 8요소", "모든 챗봇 응답에 공통으로 적용되는 베이스 스타일입니다.",
    "이 8가지는 챗봇이 모든 답변에서 항상 지키는 기본 말투·태도 규칙입니다. (예: 존댓말만 쓴다, 친근하게 답한다, 추측하지 않고 문서 내용만 답한다 등) " +
    "수정 후 \"저장\"을 누르면 다음 사용자 질문부터 바로 적용됩니다.",
    `
    <form id="tone-form">
      ${fields}
      <div style="margin-top:14px; display:flex; align-items:center; gap:10px;">
        <button type="submit" class="btn">저장</button>
        <span id="tone-result"></span>
      </div>
    </form>
    `
  );
  bindAccordions(main);

  document.getElementById("tone-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const payload = {};
    Object.keys(TONE_LABELS).forEach((key) => { payload[key] = f[key].value; });
    const resultEl = document.getElementById("tone-result");
    try {
      await api("/settings/tone", { method: "PUT", body: JSON.stringify(payload) });
      invalidateCache("/settings");
      resultEl.innerHTML = `<span class="success-text">저장되었습니다.</span>`;
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">${escapeHtml(err.message)}</span>`;
    }
  });
}

const SITUATION_KEYWORD_LABELS = {
  policy_violation: "정책위반요청 (예: 대리 출석, 허위 작성 요청)",
  escalation_request: "상담원 연결 요청",
  gratitude: "감사 인사",
  simple_rejection: "단순 거절",
};

const FORBIDDEN_WORD_LABELS = {
  profanity: "욕설",
  hate_speech: "혐오표현",
  threats: "협박",
};

function _keywordsTextareaFields(labels, values) {
  return Object.entries(labels).map(([key, label]) => `
    <label>${escapeHtml(label)} (한 줄에 키워드 하나씩)</label>
    <textarea name="${key}" rows="3">${escapeHtml((values[key] || []).join("\n"))}</textarea>
  `).join("");
}

function _parseKeywordsForm(form, labels) {
  const payload = {};
  Object.keys(labels).forEach((key) => {
    payload[key] = form[key].value.split("\n").map((s) => s.trim()).filter(Boolean);
  });
  return payload;
}

async function renderKeywords(main) {
  const settings = await cachedApi("/settings");
  const situationKeywords = settings.situation_keywords || {};
  const forbiddenWords = settings.forbidden_words || {};

  main.innerHTML = `<h1>분류 키워드 관리</h1>` + cardWithDetail(
    "상황 분류 키워드 (부분일치)", "질문 안에 이 키워드가 포함되면 해당 상황으로 분류됩니다.",
    "7상황 분류 중 키워드 매칭으로 판단하는 4가지(정책위반요청/상담원 연결 요청/감사 인사/단순 거절)의 키워드 목록입니다. " +
    "질문 문장 안에 키워드가 부분적으로라도 포함되면 매칭됩니다(예: '대신 출석'을 등록하면 '친구가 대신 출석 체크해줘도 되나요?'에 매칭). " +
    "정책위반요청으로 분류되면 챗봇이 명확히 거절하는 톤으로 답변합니다. 여기서 수정하면 다음 질문부터 즉시 반영됩니다.",
    `
    <form id="situation-keywords-form">
      ${_keywordsTextareaFields(SITUATION_KEYWORD_LABELS, situationKeywords)}
      <div style="margin-top:14px; display:flex; align-items:center; gap:10px;">
        <button type="submit" class="btn">저장</button>
        <span id="situation-keywords-result"></span>
      </div>
    </form>
    `
  ) + cardWithDetail(
    "금지어 사전 (부분일치)", "질문 안에 이 단어가 포함되면 Claude 호출 없이 즉시 에스컬레이션됩니다.",
    "욕설/혐오표현/협박 키워드입니다. 검색·Claude 호출 전 가장 먼저 확인하므로, 매칭되면 곧바로 운영팀 안내 응답으로 처리되고 비용이 드는 API 호출은 발생하지 않습니다. " +
    "여기서 수정하면 다음 질문부터 즉시 반영됩니다.",
    `
    <form id="forbidden-words-form">
      ${_keywordsTextareaFields(FORBIDDEN_WORD_LABELS, forbiddenWords)}
      <div style="margin-top:14px; display:flex; align-items:center; gap:10px;">
        <button type="submit" class="btn">저장</button>
        <span id="forbidden-words-result"></span>
      </div>
    </form>
    `
  );
  bindAccordions(main);

  document.getElementById("situation-keywords-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const resultEl = document.getElementById("situation-keywords-result");
    try {
      await api("/settings/situation-keywords", {
        method: "PUT",
        body: JSON.stringify(_parseKeywordsForm(e.target, SITUATION_KEYWORD_LABELS)),
      });
      invalidateCache("/settings");
      resultEl.innerHTML = `<span class="success-text">저장되었습니다.</span>`;
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">${escapeHtml(err.message)}</span>`;
    }
  });

  document.getElementById("forbidden-words-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const resultEl = document.getElementById("forbidden-words-result");
    try {
      await api("/settings/forbidden-words", {
        method: "PUT",
        body: JSON.stringify(_parseKeywordsForm(e.target, FORBIDDEN_WORD_LABELS)),
      });
      invalidateCache("/settings");
      resultEl.innerHTML = `<span class="success-text">저장되었습니다.</span>`;
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">${escapeHtml(err.message)}</span>`;
    }
  });
}

async function renderApiParams(main) {
  const settings = await cachedApi("/settings");
  main.innerHTML = `<h1>챗봇 운영지침</h1>` + cardWithDetail(
    "남용 방지 설정", "",
    "챗봇 위젯으로 들어오는 질문의 글자수 제한과, 같은 사용자(세션) 기준으로 분당 몇 번까지 질문할 수 있는지를 조정합니다. " +
    "최대 질문 글자수는 2000자, 분당 요청 제한은 100회까지만 설정할 수 있습니다(그 이상은 챗봇이 비정상적으로 사용될 위험이 있어 제한해두었습니다).",
    `
    <form id="api-params-form">
      <label>최대 질문 글자수 (max_question_length)</label>
      <input type="number" name="max_question_length" min="1" max="2000" value="${settings.max_question_length}" required>
      <label>분당 요청 제한 (rate_limit_per_minute)</label>
      <input type="number" name="rate_limit_per_minute" min="1" max="100" value="${settings.rate_limit_per_minute}" required>
      <div style="margin-top:14px; display:flex; align-items:center; gap:10px;">
        <button type="submit" class="btn">저장</button>
        <span id="api-params-result"></span>
      </div>
    </form>
    `
  );
  bindAccordions(main);

  document.getElementById("api-params-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const resultEl = document.getElementById("api-params-result");
    try {
      await api("/settings/api-params", {
        method: "PUT",
        body: JSON.stringify({
          max_question_length: parseInt(f.max_question_length.value, 10),
          rate_limit_per_minute: parseInt(f.rate_limit_per_minute.value, 10),
        }),
      });
      invalidateCache("/settings");
      resultEl.innerHTML = `<span class="success-text">저장되었습니다.</span>`;
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">${escapeHtml(err.message)}</span>`;
    }
  });
}

async function renderChangePassword(main) {
  main.innerHTML = `<h1>비밀번호 변경</h1>
    <div class="card">
      <form id="pw-form">
        <label>현재 비밀번호</label><input type="password" name="current_password" required>
        <label>새 비밀번호 (8자 이상)</label><input type="password" name="new_password" minlength="8" required>
        <div style="margin-top:14px; display:flex; align-items:center; gap:10px;">
          <button type="submit" class="btn">변경</button>
          <span id="pw-result"></span>
        </div>
      </form>
    </div>`;

  document.getElementById("pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const resultEl = document.getElementById("pw-result");
    try {
      await api("/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: f.current_password.value,
          new_password: f.new_password.value,
        }),
      });
      resultEl.innerHTML = `<span class="success-text">변경되었습니다.</span>`;
      f.reset();
    } catch (err) {
      resultEl.innerHTML = `<span class="error-text">${escapeHtml(err.message)}</span>`;
    }
  });
}

navigate();
