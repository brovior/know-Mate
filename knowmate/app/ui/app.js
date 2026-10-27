/* ===== KnowMate 프런트엔드 ===== */
'use strict';

let bridge = null;
let currentMode = "knowledge";
let waiting = false; // visible response animation only
let frontendSubmitting = false;
let backendBusy = true; // remain conservative until the backend state is known
let queryState = "IDLE";
let stateRequestId = null;
let queryStateEpoch = 0;
let queryStateReadSeq = 0;
let activeRequest = null;
let viewGeneration = 0;
let recentListSeq = 0;
let historyMutationEpoch = 0;
let restoreSeq = 0;
let navigationSeq = 0;
let navigationPending = false;
let querySafetyTimer = null;
let queryTimeoutMs = 0;
let _failurePolicyState = {job_id: "", revision: 0, active: false, terminal: false, policy_unknown: false};
let _failurePolicySubmitting = false;
let _failurePolicyPendingAction = "exclude";
let _failurePolicyRequestSeq = 0;
let _failurePolicyReadSeq = 0;
let _failurePolicyEpoch = 0;
let _failurePolicyTransportUnknown = false;
let _failurePolicyAwaitingStart = false;
let _failurePolicyPriorId = "";
const _failurePolicyRetired = new Set();
const _failurePolicyToasts = new Set();
const threadVersions = new WeakMap();
const savedThreadVersions = new WeakMap();
const threadSaves = new Map();
const retainedThreads = new Map(); // pending requests and answers awaiting a successful save
const deletingThreads = new Set();
const unsentUserMessages = new WeakMap();
let currentThread = null;   // 현재 대화 스레드 {id, title, mode, created_at, messages}

// QWebEngineView 환경에서 .checked 동적 읽기가 불안정한 경우를 방어하기 위해 JS 변수로 별도 추적
let scopeLocal  = true;
let scopeShared = true;
let scopeMail   = true;

/* -- QWebChannel 초기화 -- */
new QWebChannel(qt.webChannelTransport, function(channel) {
  bridge = channel.objects.bridge;
  bridge.responseReady.connect(onResponse);
  bridge.queryStateChanged.connect(onQueryStateChanged);
  bridge.indexProgress.connect(onIndexProgress);
  bridge.indexFinished.connect(onIndexFinished);
  bridge.indexAlert.connect(onIndexAlert);
  bridge.statusUpdated.connect(onStatusUpdated);
  if (bridge.failuresChanged) bridge.failuresChanged.connect(refreshFailAttention);
  if (bridge.failurePolicyJobChanged) bridge.failurePolicyJobChanged.connect(onFailurePolicyJobChanged);
  onBridgeReady();
});

function onBridgeReady() {
  renderEmptyState();
  refreshQueryState();
  initTitlebarDrag();
  initScopeCheckboxes();
  bridge.getIndexStatus().then(json => {
    let data;
    try { data = JSON.parse(json); } catch { return; }
    onStatusUpdated(data);
  });
  bridge.getVersion().then(v => {
    const el = document.getElementById("appVersion");
    if (el) el.textContent = "v" + v;
  }).catch(() => {});
  loadRecentQuestions();
  refreshFailAttention();
  refreshFailurePolicyStatus();
}

/* -- 커스텀 타이틀바 -- */
function initTitlebarDrag() {
  const bar = document.getElementById("titlebar");
  bar.addEventListener("mousedown", function(e) {
    if (e.button !== 0 || e.target.closest(".wc-btn")) return;
    if (bridge) bridge.startWindowDrag();
  });
  bar.addEventListener("dblclick", function(e) {
    if (e.target.closest(".wc-btn")) return;
    windowMaximize();
  });
}

let _maximized = false;

function initScopeCheckboxes() {
  const elLocal  = document.getElementById("chkLocal");
  const elShared = document.getElementById("chkShared");
  if (elLocal) {
    scopeLocal = elLocal.checked;
    elLocal.addEventListener("change", function() {
      scopeLocal = this.checked;
      this.closest(".check-item")?.classList.toggle("selected", this.checked);
    });
  }
  if (elShared) {
    scopeShared = elShared.checked;
    elShared.addEventListener("change", function() {
      scopeShared = this.checked;
      this.closest(".check-item")?.classList.toggle("selected", this.checked);
    });
  }
  const elMail = document.getElementById("chkMail");
  if (elMail) {
    scopeMail = elMail.checked;
    elMail.addEventListener("change", function() {
      scopeMail = this.checked;
      this.closest(".check-item")?.classList.toggle("selected", this.checked);
    });
  }
}

function windowMinimize() { if (bridge) bridge.minimizeWindow(); }

function windowMaximize() {
  if (!bridge) return;
  bridge.maximizeWindow();
  _maximized = !_maximized;
  const icon = document.getElementById("wcMaxIcon");
  if (icon) {
    icon.textContent = _maximized ? "⧉" : "□";
  }
  document.getElementById("wcMax").title = _maximized ? "복원" : "최대화";
}

function windowClose()    { if (bridge) bridge.closeWindow(); }

/* -- 모드 전환 -- */
async function switchMode(mode) {
  if (mode === currentMode) return;
  if (!await prepareNavigation()) return;
  currentMode = mode;
  currentThread = null;
  document.getElementById("segKnow").classList.toggle("active", mode === "knowledge");
  document.getElementById("segMes").classList.toggle("active", mode === "mes");
  document.getElementById("panelKnow").style.display = mode === "knowledge" ? "" : "none";
  document.getElementById("panelMes").style.display = mode === "mes" ? "" : "none";
  renderEmptyState();
  loadRecentQuestions();
}

/* -- 입력 전송 / 질문 수명 -- */
function handleKey(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMsg(); }
}

function queryLocked() {
  return failurePolicyBlocked() || frontendSubmitting || backendBusy || navigationPending || Boolean(currentThread &&
    deletingThreads.has(currentThread.mode + ":" + currentThread.id));
}

function updateQueryControls() {
  const inp = document.getElementById("inputPill");
  if (inp) {
    inp.disabled = false;
    inp.placeholder = queryState === "RETIRED_PENDING_EXIT"
      ? "이전 요청을 정리하는 중입니다. 다음 질문을 작성할 수 있습니다."
      : "질문을 입력하세요";
  }
  document.querySelectorAll(".btn-send").forEach(button => {
    button.disabled = queryLocked();
    button.title = failurePolicyBlocked() ? failurePolicyBusyMessage() : queryLocked() ? "이전 질문 처리가 끝난 뒤 전송할 수 있습니다." : "전송";
  });
  document.querySelectorAll(".btn-settings-save, .btn-add-folder, #folderList button, " +
    "#failBody button[data-act='exclude'], #failBody button[data-act='unexclude']").forEach(button => {
    button.disabled = frontendSubmitting || backendBusy || failurePolicyBlocked();
  });
  document.querySelectorAll("#btnReindex, #btnRetryAll, #obStartBtn, #failBody button[data-act='retry']").forEach(button => {
    button.disabled = failurePolicyBlocked();
    if (failurePolicyBlocked()) button.title = failurePolicyBusyMessage();
  });
  const bulk = document.getElementById("btnBulkFailAction");
  if (bulk && (frontendSubmitting || backendBusy || failurePolicyBlocked())) bulk.disabled = true;
}

function policyWriteBlocked() {
  if (failurePolicyBlocked()) { showToast(failurePolicyBusyMessage()); return true; }
  if (!frontendSubmitting && !backendBusy) return false;
  showToast(queryState === "RETIRED_PENDING_EXIT"
    ? "이전 요청을 정리하는 중입니다. 완료 후 다시 시도해 주세요."
    : "질문 처리가 끝난 뒤 다시 시도해 주세요.");
  return true;
}

async function refreshQueryState() {
  if (!bridge) return;
  const request = activeRequest;
  const epoch = queryStateEpoch;
  const seq = ++queryStateReadSeq;
  try {
    const state = await bridge.getQueryState();
    if (activeRequest === request && epoch === queryStateEpoch && seq === queryStateReadSeq) {
      onQueryStateChanged(state, true);
    }
  }
  catch { showToast("질문 처리 상태를 확인하지 못했습니다."); }
}

function armQuerySafetyTimer(request) {
  clearTimeout(querySafetyTimer);
  if (!Number.isFinite(queryTimeoutMs) || queryTimeoutMs <= 0) return;
  querySafetyTimer = setTimeout(async () => {
    if (activeRequest !== request || !backendBusy) return;
    await refreshQueryState();
    if (activeRequest === request && backendBusy) {
      bridge.cancelQuery(request.request_id);
      setWaiting(false);
      showToast("이전 요청을 정리하는 중입니다.");
    }
  }, Math.min(queryTimeoutMs + 1000, 2147483647));
}

function onQueryStateChanged(json, authoritative = false) {
  let data;
  try { data = typeof json === "string" ? JSON.parse(json) : json; } catch { return; }
  if (!data || typeof data.busy !== "boolean" || typeof data.state !== "string") return;
  if (!authoritative && stateRequestId && data.request_id !== stateRequestId) return;
  queryStateEpoch++;
  stateRequestId = data.request_id || null;
  backendBusy = data.busy;
  queryState = data.state;
  if (Number.isFinite(data.timeout_ms) && data.timeout_ms > 0) queryTimeoutMs = data.timeout_ms;
  if (!backendBusy) {
    clearTimeout(querySafetyTimer);
    setWaiting(false);
  }
  else if (activeRequest && !activeRequest.timerArmed) {
    activeRequest.timerArmed = true;
    armQuerySafetyTimer(activeRequest);
  }
  if (queryState === "RETIRED_PENDING_EXIT" || queryState === "CLOSING") setWaiting(false);
  updateQueryControls();
  if (!backendBusy) renderFailuresPanel();
}

function touchHistory(invalidateRestore = false) {
  historyMutationEpoch++;
  recentListSeq++;
  if (invalidateRestore) restoreSeq++;
}

function threadKey(thread) {
  return thread.mode + ":" + thread.id;
}

function releaseSavedThread(thread) {
  if (!thread || (threadVersions.get(thread) || 0) !== (savedThreadVersions.get(thread) || 0)) return;
  if (activeRequest && activeRequest.thread === thread && !activeRequest.terminalSeen && !activeRequest.invalidated) return;
  if (retainedThreads.get(threadKey(thread)) === thread) retainedThreads.delete(threadKey(thread));
}

function requestIsVisible(request) {
  return currentThread && currentMode === request.mode && currentThread.mode === request.mode &&
    currentThread.id === request.thread_id;
}

function markThreadDirty(thread) {
  threadVersions.set(thread, (threadVersions.get(thread) || 0) + 1);
}

async function persistThread(thread = currentThread) {
  if (!thread || !bridge) return true;
  while (threadSaves.has(thread.id)) await threadSaves.get(thread.id);
  if (deletingThreads.has(threadKey(thread))) return false;
  const version = threadVersions.get(thread) || 0;
  if ((savedThreadVersions.get(thread) || 0) === version) return true;
  touchHistory();
  const pending = (async () => {
    try {
      const result = JSON.parse(await bridge.saveThread(thread.mode, JSON.stringify(thread)));
      if (!result || result.ok !== true) throw new Error("save failed");
      savedThreadVersions.set(thread, version);
      loadRecentQuestions();
      return true;
    } catch {
      showToast("대화를 저장하지 못했습니다. 현재 대화를 유지하고 다시 시도해 주세요.");
      return false;
    }
  })();
  threadSaves.set(thread.id, pending);
  const ok = await pending;
  if (threadSaves.get(thread.id) === pending) threadSaves.delete(thread.id);
  if (ok) releaseSavedThread(thread);
  return ok;
}

async function prepareNavigation() {
  const seq = ++navigationSeq;
  navigationPending = true;
  viewGeneration++;
  restoreSeq++;
  recentListSeq++;
  setWaiting(false);
  const saved = await persistThread();
  if (seq !== navigationSeq) return false;
  navigationPending = false;
  if (!saved) syncWaitingToView();
  updateQueryControls();
  return saved;
}

async function sendMsg() {
  if (failurePolicyBlocked()) { showToast(failurePolicyBusyMessage()); return; }
  if (queryLocked() || !bridge) return;
  const inp = document.getElementById("inputPill");
  const inputValue = inp.value;
  const text = inputValue.trim();
  if (!text) return;
  const scopes = [];
  if (currentMode === "knowledge") {
    if (scopeLocal) scopes.push("local");
    if (scopeShared) scopes.push("shared");
    if (scopeMail) scopes.push("mail");
    if (scopes.length === 0) {
      showToast("검색 범위를 하나 이상 선택해주세요.");
      return;
    }
  }
  frontendSubmitting = true;
  const submissionGeneration = viewGeneration;
  updateQueryControls();
  try {
    if (!await persistThread()) return;
    if (submissionGeneration !== viewGeneration || navigationPending) return;
    if (!currentThread) {
      currentThread = { id: _genId(), title: text.substring(0, 30), mode: currentMode,
        created_at: new Date().toISOString(), messages: [] };
    }
    const thread = currentThread;
    const generation = viewGeneration;
    const pendingMessage = unsentUserMessages.get(thread);
    if (pendingMessage && thread.messages[thread.messages.length - 1] === pendingMessage) {
      if (pendingMessage.content !== text) {
        pendingMessage.content = text;
        markThreadDirty(thread);
        renderThreadMessages(thread);
      }
    } else {
      const message = { role: "user", content: text };
      thread.messages.push(message);
      unsentUserMessages.set(thread, message);
      markThreadDirty(thread);
      appendUserMsg(text);
    }
    if (!await persistThread(thread)) return;
    const request = { request_id: _genId(), mode: thread.mode, thread_id: thread.id,
      view_generation: generation, thread, terminalSeen: false, invalidated: false, timerArmed: false };
    if (activeRequest) {
      activeRequest.invalidated = true;
      releaseSavedThread(activeRequest.thread);
    }
    activeRequest = request;
    retainedThreads.set(threadKey(thread), thread);
    backendBusy = true;
    stateRequestId = request.request_id;
    queryState = "RUNNING";
    setWaiting(Boolean(requestIsVisible(request) && !navigationPending));
    // Reserve before calling: a fast Qt response may arrive during this invocation.
    await bridge.sendQuery(JSON.stringify({ query: text, scopes, request_id: request.request_id,
      mode: request.mode, thread_id: request.thread_id, view_generation: generation }));
    unsentUserMessages.delete(thread);
    if (generation === viewGeneration && currentThread === thread && inp.value === inputValue) inp.value = "";
    armQuerySafetyTimer(request);
  } catch {
    setWaiting(false);
    showToast("질문 전송 상태를 확인하지 못했습니다.");
    refreshQueryState();
  } finally {
    frontendSubmitting = false;
    updateQueryControls();
  }
}

function _genId() {
  return Date.now().toString(36) + Math.random().toString(36).substring(2, 7);
}

function setWaiting(on) {
  waiting = on;
  showLoading(on);
  updateQueryControls();
}

function syncWaitingToView() {
  setWaiting(Boolean(activeRequest && !activeRequest.terminalSeen && !activeRequest.invalidated &&
    backendBusy && requestIsVisible(activeRequest)));
}

/* -- Python 응답 수신: 실행권은 상태 신호의 실제 종료 확인으로만 해제 -- */
async function onResponse(json) {
  let data;
  try { data = JSON.parse(json); } catch { return; }
  const request = activeRequest;
  if (!request || !data || data.request_id !== request.request_id || request.terminalSeen ||
      data.mode !== request.mode || data.thread_id !== request.thread_id ||
      data.view_generation !== request.view_generation) return;
  if (!["success", "error", "cancelled", "timeout", "rejected"].includes(data.status)) return;
  if (data.status === "rejected") {
    showToast("이전 질문 처리가 끝난 뒤 다시 시도해 주세요.");
    refreshQueryState();
    return;
  }
  request.terminalSeen = true;
  if (request.invalidated) return;
  const thread = request.thread;
  if (requestIsVisible(request)) setWaiting(false);
  const blocks = Array.isArray(data.blocks) ? data.blocks : [];
  if (data.status === "cancelled" || data.status === "timeout") {
    releaseSavedThread(thread);
    showToast(data.status === "timeout" ? "응답 시간이 초과되었습니다. 이전 요청을 정리합니다." : "질문을 취소했습니다.");
    return;
  }
  thread.messages.push({ role: "ai", blocks });
  markThreadDirty(thread);
  if (requestIsVisible(request)) {
    currentThread = thread;
    appendAiBlocks(blocks);
  }
  await persistThread(thread);
}

/* -- 인덱싱 시그널 핸들러 -- */
function onIndexProgress(json) {
  let data;
  try { data = JSON.parse(json); } catch { return; }
  updateIndexProgressUI(data.current, data.total, data.filename);
}

function onIndexFinished(message) {
  hideIndexProgress();
  showToast(message.length > 60 ? message.substring(0, 60) + "…" : message);
  refreshFailAttention();
}

function onStatusUpdated(dataOrJson) {
  let data = (typeof dataOrJson === "string") ? JSON.parse(dataOrJson) : dataOrJson;

  const idxTime   = document.getElementById("idxTime");
  const idxDetail = document.getElementById("idxDetail");
  const badgeLocal  = document.getElementById("badgeLocal");
  const badgeShared = document.getElementById("badgeShared");
  const badgeMail   = document.getElementById("badgeMail");

  if (idxTime)   idxTime.textContent   = data.last_indexed || "-";
  if (idxDetail) idxDetail.textContent = `문서 ${data.doc_count ?? 0}건`;
  if (badgeLocal)  badgeLocal.textContent  = data.local_count  ?? "-";
  if (badgeShared) badgeShared.textContent = data.shared_count ?? "-";
  if (badgeMail)   badgeMail.textContent   = data.mail_count   ?? "-";
}

function onIndexAlert(message) {
  showToast(message);
}

/* -- 인덱싱 진행률 UI (사이드바 기존 요소 활용) -- */
function updateIndexProgressUI(current, total, filename) {
  const progWrap = document.getElementById("progWrap");
  const progBar  = document.getElementById("progBar");
  const progText = document.getElementById("progText");
  const progFile = document.getElementById("progFile");
  const idxIcon  = document.getElementById("idxIcon");

  if (progWrap) progWrap.style.display = "block";
  if (progText) progText.style.display = "block";
  if (progFile) progFile.style.display = "block";

  // total === -2 : 스트리밍 인덱싱 중 (총계 미정, 처리 건수 카운터)
  if (total === -2) {
    if (idxIcon)  idxIcon.textContent = "⟳ 인덱싱 중...";
    if (progBar)  progBar.style.width = "0%";
    if (progText) progText.textContent = `인덱싱 중... ${current}건 처리`;
    if (progFile) progFile.textContent = filename || "";
    return;
  }

  // total < 0 : 스캔 단계 (총 건수 미정, 발견 건수만 표시)
  if (total < 0) {
    if (idxIcon)  idxIcon.textContent = "⟳ 스캔 중...";
    if (progBar)  progBar.style.width = "0%";
    if (progText) progText.textContent = `폴더 스캔 중... ${current}건 발견`;
    if (progFile) progFile.textContent = "";
    return;
  }

  if (idxIcon)  idxIcon.textContent = "⟳ 인덱싱 중...";
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  if (progBar)  progBar.style.width = pct + "%";
  if (progText) progText.textContent = `${current}/${total}건`;
  if (progFile) progFile.textContent = filename || "";
}

function hideIndexProgress() {
  const progWrap = document.getElementById("progWrap");
  const progText = document.getElementById("progText");
  const progFile = document.getElementById("progFile");
  const idxIcon  = document.getElementById("idxIcon");

  if (progWrap) progWrap.style.display = "none";
  if (progText) { progText.style.display = "none"; progText.textContent = ""; }
  if (progFile) { progFile.style.display = "none"; progFile.textContent = ""; }
  if (idxIcon)  idxIcon.textContent = "✅ 마지막 인덱싱";
}

/* -- DOM 헬퍼 -- */
function renderEmptyState() {
  const scroll = document.getElementById("chatScroll");
  scroll.innerHTML = `
    <div class="empty-state">
      <i class="ti ti-message-search"></i>
      <p>질문을 입력하면 관련 문서를 검색해 드립니다.</p>
    </div>`;
}

function appendUserMsg(text) {
  const scroll = document.getElementById("chatScroll");
  const empty = scroll.querySelector(".empty-state");
  if (empty) empty.remove();

  const div = document.createElement("div");
  div.className = "msg-user";
  div.innerHTML = `<div class="bubble-user">${escHtml(text)}</div>`;
  scroll.appendChild(div);
  scrollBottom();
}

function showLoading(show) {
  let el = document.getElementById("_loadingMsg");
  if (show && !el) {
    const scroll = document.getElementById("chatScroll");
    el = document.createElement("div");
    el.id = "_loadingMsg";
    el.className = "msg-ai";
    el.innerHTML = `<div class="avatar">K</div>
      <div class="bubble-ai"><div class="loading">
        <div class="dot"></div><div class="dot"></div><div class="dot"></div>
      </div></div>`;
    scroll.appendChild(el);
    scrollBottom();
  } else if (!show && el) {
    el.remove();
  }
}

function removeLoading() { showLoading(false); }

function appendAiBlocks(blocks) {
  const scroll = document.getElementById("chatScroll");
  const wrap = document.createElement("div");
  wrap.className = "msg-ai";
  wrap.innerHTML = `<div class="avatar">K</div>`;
  const inner = document.createElement("div");

  blocks.forEach(block => {
    inner.appendChild(renderBlock(block));
  });

  wrap.appendChild(inner);
  scroll.appendChild(wrap);
  scrollBottom();
}

function renderBlock(block) {
  switch (block.type) {
    case "text":    return renderText(block);
    case "sources": return renderSources(block);
    case "table":   return renderTable(block);
    default:        return renderUnsupported(block);
  }
}

function renderText(block) {
  const div = document.createElement("div");
  div.className = "bubble-ai";
  div.innerHTML = renderMarkdown(block.content);
  return div;
}

/* ── 경량 마크다운 렌더러 ── */

function renderMarkdown(text) {
  const lines = text.split('\n');
  let html = '';
  let i = 0;
  while (i < lines.length) {
    if (_isMdTableRow(lines[i]) && i + 1 < lines.length && _isMdSeparator(lines[i + 1])) {
      const tableLines = [];
      while (i < lines.length && _isMdTableRow(lines[i])) {
        tableLines.push(lines[i]);
        i++;
      }
      html += _renderMdTable(tableLines);
    } else {
      const line = lines[i].trim();
      html += line ? '<p>' + _renderInline(line) + '</p>' : '<br>';
      i++;
    }
  }
  return html;
}

function _isMdTableRow(line) {
  return /^\|.+\|$/.test(line.trim());
}

function _isMdSeparator(line) {
  return /^\|[\s|:-]+\|$/.test(line.trim());
}

function _renderMdTable(lines) {
  const rows = lines
    .filter(l => !_isMdSeparator(l))
    .map(l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim()));

  if (rows.length === 0) return '';
  const [head, ...body] = rows;

  const th = head.map(c => `<th>${_renderInline(c)}</th>`).join('');
  const tr = body.map(r =>
    '<tr>' + r.map(c => `<td>${_renderInline(c)}</td>`).join('') + '</tr>'
  ).join('');

  return `<table class="md-table"><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
}

function _renderInline(text) {
  return escHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/~~(.+?)~~/g, '<s>$1</s>')
    .replace(/`(.+?)`/g, '<code>$1</code>');
}

function renderSources(block) {
  const frag = document.createDocumentFragment();
  const title = document.createElement("div");
  title.className = "sources-title";
  title.textContent = block.title || "관련 문서";
  frag.appendChild(title);

  (block.items || []).forEach(item => {
    const card = document.createElement("div");
    card.className = "source-card";
    const badgeClass = item.badge === "메일" ? "badge-mail" : "badge-doc";
    const scorePct = Math.round((item.score || 0) * 100);
    card.innerHTML = `
      <span class="source-badge ${badgeClass}">${escHtml(item.badge)}</span>
      <div class="source-info">
        <div class="source-title">${escHtml(item.title)}</div>
        <div class="source-sub">${escHtml(item.subtitle)}</div>
      </div>
      <span class="source-score">일치율 ${scorePct}%</span>`;
    card.onclick = () => {
      if (bridge) {
        bridge.openFile(item.path).then(result => {
          if (result === "not_found") showToast("원본을 찾을 수 없음");
        }).catch(() => showToast("파일 열기 실패"));
      }
    };
    frag.appendChild(card);
  });

  const wrapper = document.createElement("div");
  wrapper.appendChild(frag);
  return wrapper;
}

function renderTable(block) {
  const wrap = document.createElement("div");
  wrap.className = "block-table-wrap";
  const titleEl = document.createElement("div");
  titleEl.className = "block-title";
  titleEl.textContent = block.title || "";
  wrap.appendChild(titleEl);

  const table = document.createElement("table");
  table.className = "block-table";
  const thead = `<thead><tr>${(block.columns||[]).map(c=>`<th>${escHtml(String(c))}</th>`).join("")}</tr></thead>`;
  const tbody = `<tbody>${(block.rows||[]).map(row=>`<tr>${row.map(c=>`<td>${escHtml(String(c))}</td>`).join("")}</tr>`).join("")}</tbody>`;
  table.innerHTML = thead + tbody;
  wrap.appendChild(table);
  return wrap;
}

function renderUnsupported(block) {
  const div = document.createElement("div");
  div.className = "unsupported-block";
  div.innerHTML = `<i class="ti ti-alert-circle" style="font-size:14px;vertical-align:-2px;"></i> 지원하지 않는 응답 형식 (type: "${escHtml(block.type || "?")}")`;
  return div;
}

/* -- 온보딩 오버레이 -- */
function openOnboarding() {
  document.getElementById("overlay").classList.add("show");
  document.getElementById("obTitle").textContent = "폴더 관리";
  document.getElementById("obSub").textContent = "인덱싱할 폴더를 추가하거나 제거하세요";
  if (bridge) {
    bridge.getFolders().then(json => renderFolderList(JSON.parse(json)));
  }
}

function closeOnboarding() {
  document.getElementById("overlay").classList.remove("show");
}

function renderFolderList(folders) {
  const list = document.getElementById("folderList");
  list.innerHTML = "";
  (folders || []).forEach(path => {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #2a2a2a;font-size:13px;";
    // 경로를 인라인 onclick 문자열에 넣지 않는다 — escHtml은 HTML 특수문자만 다루고
    // JS 문자열 이스케이프(역슬래시·작은따옴표)는 하지 않아, Windows 경로나 작은따옴표가
    // 든 폴더명("Kim's docs")이 그대로 깨지거나 코드로 실행될 수 있다. 리스너로 실제
    // 문자열을 클로저에 담아 전달한다.
    row.innerHTML =
      `<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></span>` +
      `<button style="background:none;border:none;color:#888;cursor:pointer;font-size:16px;padding:0 4px;">✕</button>`;
    row.firstElementChild.textContent = path;
    const removeBtn = row.lastElementChild;
    removeBtn.addEventListener("click", () => removeFolder(removeBtn, path));
    list.appendChild(row);
  });
  updateQueryControls();
}

function removeFolder(btn, path) {
  if (!bridge || policyWriteBlocked()) return;
  bridge.removeWatchFolder(path).then(json => renderFolderList(JSON.parse(json)));
}

function addFolder() {
  if (!bridge || policyWriteBlocked()) return;
  bridge.selectFolder().then(path => {
    if (!path || policyWriteBlocked()) return;
    bridge.addWatchFolder(path).then(json => renderFolderList(JSON.parse(json)));
  });
}

/* -- 인덱싱 버튼 -- */
function startFullIndex() {
  closeOnboarding();
  if (bridge) {
    startReindex();
  } else {
    showToast("브리지가 준비되지 않았습니다.");
  }
}

/* -- 증분 재인덱싱 -- */
function startReindex() {
  if (failurePolicyBlocked()) { showToast(failurePolicyBusyMessage()); return; }
  if (!bridge) { showToast("브리지가 준비되지 않았습니다."); return; }
  Promise.resolve(bridge.startReindex()).then(result => {
    if (result === "ok") showIndexRunning();
    else showToast(_failureRequestMessage(result));
  }).catch(() => showToast("인덱싱을 시작하지 못했습니다."));
}

function showIndexRunning() {
  const progWrap = document.getElementById("progWrap");
  const progText = document.getElementById("progText");
  const progFile = document.getElementById("progFile");
  const progBar  = document.getElementById("progBar");
  const idxIcon  = document.getElementById("idxIcon");
  if (progWrap) progWrap.style.display = "block";
  if (progText) { progText.style.display = "block"; progText.textContent = "스캔 중..."; }
  if (progFile) { progFile.style.display = "block"; progFile.textContent = ""; }
  if (progBar)  progBar.style.width = "0%";
  if (idxIcon)  idxIcon.textContent = "⟳ 인덱싱 중...";
}

function cancelReindex() {
  if (failurePolicyBlocked()) { showToast(failurePolicyBusyMessage()); return; }
  if (bridge) bridge.cancelReindex();
}

async function newThread() {
  if (!await prepareNavigation()) return;
  currentThread = null;
  renderEmptyState();
  document.getElementById("inputPill").focus();
  loadRecentQuestions();
}

/* -- 최근 질문: 변경 및 조회 순서가 지난 응답은 반영하지 않음 -- */
function loadRecentQuestions() {
  if (!bridge) return;
  const seq = ++recentListSeq;
  const epoch = historyMutationEpoch;
  const mode = currentMode;
  const generation = viewGeneration;
  bridge.getThreads(mode).then(json => {
    if (seq !== recentListSeq || epoch !== historyMutationEpoch ||
        mode !== currentMode || generation !== viewGeneration) return;
    let threads;
    try { threads = JSON.parse(json); } catch { return; }
    if (Array.isArray(threads)) renderRecentList(threads);
  }).catch(() => showToast("최근 질문을 불러오지 못했습니다."));
}

function renderRecentList(threads) {
  const list = document.getElementById("recentList");
  list.innerHTML = '<div class="panel-title" style="margin-top:4px;">최근 질문</div>';
  (threads || []).slice(0, 15).forEach(t => {
    const div = document.createElement("div");
    div.className = "recent-item" + (currentThread && currentThread.id === t.id ? " active" : "");
    div.title = t.title;
    div.onclick = () => restoreThread({ id: t.id, mode: currentMode });

    const label = document.createElement("span");
    label.className = "recent-label";
    label.textContent = t.title;

    const del = document.createElement("button");
    del.className = "recent-del";
    del.title = "삭제";
    del.innerHTML = '<i class="ti ti-x"></i>';
    del.onclick = (e) => { e.stopPropagation(); deleteQuestion(t); };

    div.appendChild(label);
    div.appendChild(del);
    list.appendChild(div);
  });
}

async function deleteQuestion(thread) {
  if (!bridge) return;
  const mode = thread.mode || currentMode;
  const isCurrent = currentThread && currentThread.id === thread.id && currentThread.mode === mode;
  if (isCurrent && !await prepareNavigation()) return;
  const key = mode + ":" + thread.id;
  if (deletingThreads.has(key)) return;
  deletingThreads.add(key);
  const retained = retainedThreads.get(key);
  if (activeRequest && activeRequest.mode === mode && activeRequest.thread_id === thread.id) {
    activeRequest.invalidated = true;
    if (!activeRequest.terminalSeen) bridge.cancelQuery(activeRequest.request_id);
    if (requestIsVisible(activeRequest)) setWaiting(false);
  }
  updateQueryControls();
  touchHistory(true);
  try {
    // Finish any already-dispatched save before deletion, so its ACK cannot recreate the thread.
    while (threadSaves.has(thread.id)) await threadSaves.get(thread.id);
    const result = JSON.parse(await bridge.deleteThread(mode, thread.id));
    if (!result || result.ok !== true) throw new Error("delete failed");
    retainedThreads.delete(key);
    if (currentThread && currentThread.id === thread.id && currentThread.mode === mode) {
      currentThread = null;
      renderEmptyState();
    }
    loadRecentQuestions();
  } catch { showToast("대화를 삭제하지 못했습니다."); }
  finally { deletingThreads.delete(key); releaseSavedThread(retained); updateQueryControls(); }
}

async function restoreThread(thread) {
  if (!bridge) return;
  const mode = thread.mode || currentMode;
  if (deletingThreads.has(mode + ":" + thread.id)) {
    showToast("대화를 삭제하는 중입니다.");
    return;
  }
  if (!await prepareNavigation()) return;
  const seq = ++restoreSeq;
  const generation = viewGeneration;
  const retained = retainedThreads.get(mode + ":" + thread.id);
  navigationPending = true;
  updateQueryControls();
  try {
    const restored = JSON.parse(await bridge.getThread(mode, thread.id));
    if (seq !== restoreSeq || generation !== viewGeneration || mode !== currentMode) return;
    if (!restored || restored.id !== thread.id || restored.mode !== mode) {
      showToast("대화를 찾을 수 없습니다.");
      return;
    }
    currentThread = retained || retainedThreads.get(mode + ":" + thread.id) || restored;
    renderThreadMessages(currentThread);
    loadRecentQuestions();
  } catch { showToast("대화를 불러오지 못했습니다."); }
  finally {
    if (generation === viewGeneration) {
      navigationPending = false;
      syncWaitingToView();
      updateQueryControls();
    }
  }
}

function renderThreadMessages(thread) {
  document.getElementById("chatScroll").innerHTML = "";
  (thread.messages || []).forEach(msg => {
    if (msg.role === "user") appendUserMsg(msg.content);
    else if (msg.role === "ai") appendAiBlocks(msg.blocks || []);
  });
  syncWaitingToView();
}

/* -- 유틸 -- */
function scrollBottom() {
  const s = document.getElementById("chatScroll");
  s.scrollTop = s.scrollHeight;
}

function escHtml(str) {
  return String(str)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

let toastTimer = null;
function showToast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2500);
}

/* -- 설정 패널 -- */
let _currentSettings = null;

function openSettings() {
  if (!bridge) { showToast("브리지가 준비되지 않았습니다."); return; }
  bridge.getSettings().then(json => {
    let data;
    try { data = JSON.parse(json); } catch { showToast("설정을 불러오지 못했습니다."); return; }
    _currentSettings = data;
    _fillSettingsForm(data);
    document.getElementById("settingsOverlay").classList.add("show");
  }).catch(() => showToast("설정을 불러오지 못했습니다."));
}

function closeSettings() {
  document.getElementById("settingsOverlay").classList.remove("show");
  document.getElementById("testResults").innerHTML = "";
}

function _fillSettingsForm(data) {
  document.getElementById("setLlmUrl").value   = data.llm?.base_url ?? "";
  document.getElementById("setLlmModel").value = data.llm?.model ?? "";
  document.getElementById("setEmbedUrl").value = data.embedding?.base_url ?? "";
  document.getElementById("setEmbedModel").textContent = data.embedding?.model ?? "-";

  const threshold = data.search?.score_threshold ?? 0.3;
  document.getElementById("setScoreThreshold").value = threshold;
  document.getElementById("valScoreThreshold").textContent = parseFloat(threshold).toFixed(2);

  const topK = data.search?.top_k_max ?? 10;
  document.getElementById("setTopK").value = topK;
  document.getElementById("valTopK").textContent = topK + "건";

  document.getElementById("setIdleEnabled").checked = data.collector?.idle_enabled ?? true;
  const idleSec = data.collector?.idle_seconds ?? 60;
  const idleSelect = document.getElementById("setIdleMinutes");
  idleSelect.querySelector("option[data-custom]")?.remove();
  const idleOption = Array.from(idleSelect.options).find(option => Number(option.value) * 60 === idleSec);
  if (idleOption) {
    idleSelect.value = idleOption.value;
  } else {
    const idleLabel = idleSec % 60 === 0 ? (idleSec / 60) + "분" : idleSec + "초";
    const custom = new Option("직접 설정한 값: " + idleLabel, "custom", true, true);
    custom.disabled = true;
    custom.dataset.custom = "true";
    idleSelect.prepend(custom);
    idleSelect.value = "custom";
  }
  idleSelect.dataset.changed = "false";
  idleSelect.onchange = () => { idleSelect.dataset.changed = "true"; };

  document.getElementById("setMailEnabled").checked = data.mail?.enabled ?? true;

  const maxSize = data.chunking?.max_file_size_mb ?? 30;
  document.getElementById("setMaxFileSize").value = maxSize;
  document.getElementById("valMaxFileSize").textContent = maxSize + "MB";

  document.getElementById("setAutoDelete").checked = data.cleanup?.auto_delete ?? false;

  selectSeg("segCloseAction", data.ui?.close_action ?? "tray");
  selectSeg("segLogLevel", data.log_level ?? "INFO");

  bridge.getVersion().then(v => {
    document.getElementById("settingsVersionNotice").textContent = "v" + v;
  }).catch(() => {});
}

function selectSeg(groupId, value) {
  const group = document.getElementById(groupId);
  if (!group) return;
  group.querySelectorAll(".settings-seg-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.value === value);
  });
}

function _segValue(groupId) {
  const active = document.querySelector(`#${groupId} .settings-seg-btn.active`);
  return active ? active.dataset.value : null;
}

function saveSettings() {
  if (policyWriteBlocked()) return;
  if (!bridge) { showToast("브리지가 준비되지 않았습니다."); return; }

  const patch = {
    llm: {
      base_url: document.getElementById("setLlmUrl").value.trim(),
      model: document.getElementById("setLlmModel").value.trim(),
    },
    embedding: {
      base_url: document.getElementById("setEmbedUrl").value.trim(),
    },
    search: {
      score_threshold: parseFloat(document.getElementById("setScoreThreshold").value),
      top_k_max: parseInt(document.getElementById("setTopK").value, 10),
    },
    collector: {
      idle_enabled: document.getElementById("setIdleEnabled").checked,
    },
    mail: {
      enabled: document.getElementById("setMailEnabled").checked,
    },
    chunking: {
      max_file_size_mb: parseInt(document.getElementById("setMaxFileSize").value, 10),
    },
    cleanup: {
      auto_delete: document.getElementById("setAutoDelete").checked,
    },
    ui: {
      close_action: _segValue("segCloseAction") || "tray",
    },
    log_level: _segValue("segLogLevel") || "INFO",
  };

  const idleSelect = document.getElementById("setIdleMinutes");
  if (idleSelect.dataset.changed === "true") {
    patch.collector.idle_seconds = Number(idleSelect.value) * 60;
  }

  bridge.saveSettings(JSON.stringify(patch)).then(json => {
    let result;
    try { result = JSON.parse(json); } catch { showToast("설정 저장 실패"); return; }
    if (result.ok) {
      showToast("설정이 저장되었습니다. 일부 항목은 재시작 후 적용됩니다.");
      closeSettings();
    } else {
      showToast(result.error === "policy_busy" || result.code === "policy_busy" ? failurePolicyBusyMessage() : result.error === "query_busy" || result.code === "query_busy" ? "질문 처리가 끝난 뒤 다시 시도해 주세요." : "설정 저장 실패");
    }
  }).catch(() => showToast("설정 저장 실패"));
}

function testConnection() {
  if (!bridge) { showToast("브리지가 준비되지 않았습니다."); return; }
  const box = document.getElementById("testResults");
  box.innerHTML = '<div class="test-result">연결 확인 중...</div>';

  bridge.testConnection().then(json => {
    let result;
    try { result = JSON.parse(json); } catch { box.innerHTML = '<div class="test-result test-fail">❌ 테스트 실패</div>'; return; }

    const rows = [];
    if (result.llm) {
      rows.push(_renderTestRow("LLM 서버", result.llm));
    }
    if (result.embedding) {
      rows.push(_renderTestRow("임베딩 서버", result.embedding));
    }
    box.innerHTML = rows.join("");
  }).catch(() => {
    box.innerHTML = '<div class="test-result test-fail">❌ 테스트 실패</div>';
  });
}

function _renderTestRow(label, r) {
  if (r.ok) {
    return `<div class="test-result test-ok">✅ ${escHtml(label)} 연결 정상</div>`;
  }
  return `<div class="test-result test-fail">❌ ${escHtml(label)} 연결 실패 — ${escHtml(r.detail || "")}</div>`;
}

function openConfigFile() {
  if (!bridge) { showToast("브리지가 준비되지 않았습니다."); return; }
  bridge.openConfigFile().then(result => {
    if (result !== "ok") showToast("설정 파일을 열지 못했습니다.");
  }).catch(() => showToast("설정 파일을 열지 못했습니다."));
}

/* ===== 확인 필요한 문서 (5차) ===== */
let _failCards = [];
let _failFilter = "all";
let _failBulkRunning = false;
let _failRefreshTask = null;
let _failRefreshPending = false;
let _failRequestGeneration = 0;
let _failWaitTimer = null;

/** Keep policy writes blocked until the backend confirms native completion. */
function failurePolicyBlocked() {
  return _failurePolicySubmitting || _failurePolicyTransportUnknown || _failurePolicyState.active || _failurePolicyState.policy_unknown;
}

function failurePolicyBusyMessage() {
  if (_failurePolicyTransportUnknown) return "문서 제외 처리 상태를 확인하지 못했습니다. 상태를 다시 확인해 주세요.";
  return _failurePolicyState.policy_unknown && !_failurePolicyState.active
    ? "제외 설정 상태를 확인하지 못했습니다. 상태를 다시 확인해 주세요."
    : "문서 제외 작업이 진행 중입니다. 완료 후 다시 시도해 주세요.";
}

function _failurePolicySummary(state) {
  const result = state.result || {};
  if (state.policy_unknown || state.settings_saved == null) return "제외 설정 상태를 확인하지 못했습니다. 상태를 다시 확인해 주세요.";
  if (!state.settings_saved) return result.cancelled ? "요청이 취소됐습니다. 제외 설정은 변경되지 않았습니다." : "제외 설정을 저장하지 못했습니다. 변경되지 않은 상태로 종료했습니다.";
  if (state.action === "reconcile") return "제외 설정 상태를 확인했습니다. 필요한 작업을 다시 요청해 주세요.";
  const changed = Number.isFinite(result.changed) ? result.changed : 0;
  if (state.action === "unexclude") return changed ? `${changed}건을 인덱싱 제외에서 해제했습니다.` : "이미 제외 해제된 문서입니다.";
  const pending = (result.failed || 0) + (result.not_attempted || 0);
  let text = changed ? `${changed}건 제외 설정 적용` : "제외 설정 적용 확인";
  if (pending) text += ` · 검색 데이터 ${pending}건 정리 미완료`;
  else if (result.cancelled) text += " · 데이터 정리가 중단됐습니다.";
  else if (result.error_code || result.status === "error") text += " · 처리 결과를 확인해 주세요.";
  else text += " · 검색 데이터 정리 완료";
  if (result.state_cleanup_failed) text += " · 처리 상태 저장 실패";
  if (result.mail_cleanup_failed) text += " · 메일 정리 실패";
  else if (result.mail_pending_chunks) text += ` · 메일 데이터 ${result.mail_pending_chunks}개 정리 대기`;
  if ((Object.hasOwn(state, "counts") && state.counts === null) || (Object.hasOwn(result, "counts") && result.counts === null)) text += " · 검색 건수 갱신 미확인";
  return text;
}

/** Update the popup and persistent sidebar from one scalar job snapshot. */
function renderFailurePolicyUI() {
  const state = _failurePolicyState;
  const busy = failurePolicyBlocked();
  const terminal = state.terminal && !state.active && !_failurePolicySubmitting;
  const result = state.result || {};
  const warning = _failurePolicyTransportUnknown || state.policy_unknown || (terminal && (!state.settings_saved || result.cancelled || result.failed || result.not_attempted || result.state_cleanup_failed || result.mail_cleanup_failed || result.mail_pending_chunks || result.error_code || result.status === "error"));
  const titles = {reserved: "제외 요청 중…", settings: state.action === "unexclude" ? "제외 해제 설정 저장 중" : "제외 설정 저장 중", documents: "검색 데이터 정리 중", mail: "메일 검색 데이터 정리 중", counts: "검색 건수 집계 중", finishing: "마무리 중"};
  const requesting = {exclude: "제외 요청 중…", unexclude: "제외 해제 요청 중…", reconcile: "제외 설정 확인 요청 중…"};
  let title = _failurePolicySubmitting ? requesting[_failurePolicyPendingAction] : state.action === "reconcile" && state.active ? "제외 설정 상태 확인 중" : titles[state.stage] || "마무리 중";
  if (terminal) title = state.policy_unknown ? "제외 설정 확인 필요" : warning ? "제외 처리 결과 확인" : state.action === "unexclude" ? "제외 해제 완료" : state.action === "reconcile" ? "제외 설정 확인 완료" : "제외 처리 완료";
  if (_failurePolicyTransportUnknown) title = "제외 처리 상태 확인 필요";
  const done = Number.isFinite(state.done) ? Math.max(0, state.done) : 0;
  const total = Number.isFinite(state.total) ? Math.max(0, state.total) : 0;
  const determinate = !_failurePolicySubmitting && !warning && state.stage === "documents" && total > 0 && done < total;
  const showPanel = busy || Boolean(state.job_id && terminal);
  const showSidebar = busy || Boolean(terminal && warning);
  const detail = _failurePolicyTransportUnknown ? "요청을 중복 실행하지 않고 현재 처리 상태를 확인합니다." : terminal ? _failurePolicySummary(state) : state.stage === "documents" && !_failurePolicySubmitting ? `${done}/${total}건 처리 시도` : state.stage === "mail" ? "기존 메일 삭제 대기도 함께 정리하고 있습니다." : "처리가 끝날 때까지 기다려 주세요.";
  for (const where of ["Panel", "Sidebar"]) {
    const box = document.getElementById("failurePolicy" + where);
    if (!box) continue;
    box.hidden = where === "Panel" ? !showPanel : !showSidebar;
    box.dataset.state = warning ? "warning" : terminal ? "terminal" : "active";
    const label = document.getElementById("failurePolicy" + where + "Title");
    if (label) label.textContent = where === "Sidebar" && !terminal && !_failurePolicyTransportUnknown ? state.action === "unexclude" ? "문서 제외 해제 처리 중" : "문서 제외 처리 중" : title;
    const count = document.getElementById("failurePolicy" + where + "Count");
    if (count) count.textContent = state.stage === "documents" && !terminal && !_failurePolicySubmitting ? `${done} / ${total}건` : "";
    const note = document.getElementById("failurePolicy" + where + "Detail");
    if (note) note.textContent = detail;
    const help = document.getElementById("failurePolicy" + where + "Help");
    if (help) help.textContent = where === "Sidebar" ? busy ? "문서 제외 처리 중에는 인덱싱과 제외 설정을 변경할 수 없습니다." : "일부 검색 데이터 정리 결과를 확인해 주세요." : busy ? "이 창을 닫아도 처리는 계속됩니다." : "원본 파일은 그대로 보존됩니다.";
    const track = document.getElementById("failurePolicy" + where + "Track");
    if (track) {
      track.hidden = terminal;
      track.classList.toggle("indeterminate", !determinate);
      if (determinate) track.setAttribute("aria-valuenow", String(done / total * 100));
      else track.removeAttribute("aria-valuenow");
    }
    const fill = document.getElementById("failurePolicy" + where + "Fill");
    if (fill) fill.style.width = determinate ? `${done / total * 100}%` : "";
    const recheck = document.getElementById("failurePolicy" + where + "Recheck");
    if (recheck) { recheck.hidden = !state.policy_unknown && !_failurePolicyTransportUnknown; recheck.disabled = _failurePolicySubmitting || (state.active && !_failurePolicyTransportUnknown); }
  }
  _failBulkRunning = busy;
  updateQueryControls();
  _renderBulkFailureAction(_failCards.filter(card => _failFilter === "all" || _failCategory(card) === _failFilter));
}

function _applyFailurePolicyState(value, live = false) {
  let state;
  try { state = typeof value === "string" ? JSON.parse(value) : value; } catch { return false; }
  if (!state || typeof state.active !== "boolean" || typeof state.terminal !== "boolean" || typeof state.policy_unknown !== "boolean" || typeof state.job_id !== "string" || !Number.isInteger(state.revision)) return false;
  const current = _failurePolicyState;
  if (live && _failurePolicyAwaitingStart && state.job_id === _failurePolicyPriorId) return false;
  if (_failurePolicyRetired.has(state.job_id)) return false;
  if (state.job_id === current.job_id && state.revision < current.revision) return false;
  if (state.job_id === current.job_id && current.terminal && !current.active && (state.active || !state.terminal)) return false;
  if (!state.job_id && current.job_id) return false;
  if (state.job_id !== current.job_id && current.active) return false;
  if (state.job_id !== current.job_id && current.job_id) _failurePolicyRetired.add(current.job_id);
  _failurePolicyState = state;
  _failurePolicyTransportUnknown = false;
  if (!live || state.job_id !== _failurePolicyPriorId) _failurePolicyAwaitingStart = false;
  if (live) _failurePolicyEpoch++;
  renderFailurePolicyUI();
  if (state.terminal && !state.active) {
    if (live && !_failurePolicyToasts.has(state.job_id)) {
      _failurePolicyToasts.add(state.job_id);
      showToast(_failurePolicySummary(state));
    }
    if (state.job_id !== current.job_id || !current.terminal) _refreshFailures(true).catch(() => {});
  }
  return true;
}

function onFailurePolicyJobChanged(json) { _applyFailurePolicyState(json, true); }

function _failurePolicyReply(promise) {
  let timer;
  const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("policy reply timeout")), 3000); });
  return Promise.race([Promise.resolve(promise), timeout]).finally(() => clearTimeout(timer));
}

function refreshFailurePolicyStatus() {
  if (!bridge || typeof bridge.getFailurePolicyJobStatus !== "function") return Promise.resolve();
  const seq = ++_failurePolicyReadSeq;
  const epoch = _failurePolicyEpoch;
  return _failurePolicyReply(bridge.getFailurePolicyJobStatus()).then(value => {
    if (seq === _failurePolicyReadSeq && epoch === _failurePolicyEpoch) {
      if (!_applyFailurePolicyState(value)) throw new Error("invalid policy state");
      return true;
    }
    return false;
  }).catch(() => {
    if (seq === _failurePolicyReadSeq && epoch === _failurePolicyEpoch) {
      _failurePolicyTransportUnknown = true;
      renderFailurePolicyUI();
      showToast("처리 상태를 확인하지 못했습니다. 진행 상황을 다시 열어 확인해 주세요.");
    }
    return false;
  });
}

function _failurePolicyRequestError(error) {
  if (error === "policy_busy" || error === "policy_unconfirmed") return failurePolicyBusyMessage();
  if (error === "query_busy") return "질문 처리가 끝난 뒤 다시 시도해 주세요.";
  if (error === "busy") return "인덱싱이 끝난 후 다시 시도해 주세요.";
  if (error === "persistence_pending") return _failureRequestMessage(error);
  if (error === "not_ready") return "프로그램 준비가 끝난 뒤 다시 시도해 주세요.";
  return "제외 작업을 시작하지 못했습니다.";
}

/** Submit one policy job; an uncertain acknowledgement never resubmits writes. */
async function _startFailurePolicyJob(action, paths) {
  if (!bridge || typeof bridge.startFailurePolicyJob !== "function") { showToast("제외 작업을 시작할 준비가 되지 않았습니다."); return; }
  if (failurePolicyBlocked() && !(action === "reconcile" && !_failurePolicyState.active && !_failurePolicySubmitting)) { showToast(failurePolicyBusyMessage()); return; }
  if (action !== "reconcile" && policyWriteBlocked()) return;
  const seq = ++_failurePolicyRequestSeq;
  _failurePolicyEpoch++;
  _failurePolicySubmitting = true;
  _failurePolicyAwaitingStart = true;
  _failurePolicyPriorId = _failurePolicyState.job_id;
  _failurePolicyPendingAction = action;
  renderFailurePolicyUI();
  try {
    const response = await _failurePolicyReply(bridge.startFailurePolicyJob(action, JSON.stringify(paths)));
    if (seq !== _failurePolicyRequestSeq) return;
    const result = typeof response === "string" ? JSON.parse(response) : response;
    if (!result || result.ok !== true) {
      _failurePolicySubmitting = false;
      _failurePolicyAwaitingStart = false;
      renderFailurePolicyUI();
      showToast(_failurePolicyRequestError(result?.error));
      if (result?.error === "policy_busy" || result?.error === "policy_unconfirmed") await refreshFailurePolicyStatus();
      return;
    }
    _failurePolicySubmitting = false;
    _failurePolicyAwaitingStart = false;
    if (result.accepted && result.job_id && _failurePolicyState.job_id !== result.job_id) {
      _applyFailurePolicyState({job_id: result.job_id, revision: -1, action, stage: "reserved", done: 0, total: paths.length, active: true, terminal: false, policy_unknown: false});
    }
    renderFailurePolicyUI();
    await refreshFailurePolicyStatus();
  } catch {
    if (seq !== _failurePolicyRequestSeq) return;
    showToast("요청이 접수됐는지 확인하고 있습니다. 중복 요청하지 말고 잠시 기다려 주세요.");
    const known = await refreshFailurePolicyStatus();
    _failurePolicySubmitting = false;
    if (!known && _failurePolicyAwaitingStart) _failurePolicyTransportUnknown = true;
    renderFailurePolicyUI();
  }
}

function recheckFailurePolicy() {
  return _failurePolicyTransportUnknown ? refreshFailurePolicyStatus() : _startFailurePolicyJob("reconcile", []);
}

function refreshFailAttention() {
  return _refreshFailures().catch(() => {});
}

function _failuresPanelOpen() {
  const overlay = document.getElementById("failuresOverlay");
  return !!overlay && overlay.classList.contains("show");
}

function _refreshFailures(invalidate = false) {
  if (!bridge) return Promise.resolve();
  if (invalidate) _failRequestGeneration++;
  _failRefreshPending = true;
  if (_failRefreshTask) return _failRefreshTask;
  _failRefreshTask = (async () => {
    while (_failRefreshPending) {
      _failRefreshPending = false;
      const generation = _failRequestGeneration;
      const cards = JSON.parse(await bridge.getFailures());
      if (!Array.isArray(cards)) throw new Error("invalid failures");
      if (generation !== _failRequestGeneration) continue;
      _failCards = cards;
      _renderFailAttention(cards);
      if (_failuresPanelOpen()) {
        const body = document.getElementById("failBody");
        const scroll = body ? body.scrollTop : 0;
        renderFailuresPanel();
        if (body) body.scrollTop = scroll;
      }
    }
  })().finally(() => { _failRefreshTask = null; });
  return _failRefreshTask;
}

function _refreshFailureWaitLabels() {
  if (!_failuresPanelOpen()) return;
  document.querySelectorAll("#failBody [data-next-retry]").forEach(label => {
    const value = label.dataset.nextRetry;
    label.textContent = _formatFailWait(value === "" ? null : Number(value));
  });
}

function _renderFailAttention(cards) {
  const btn = document.getElementById("btnFailAttention");
  const text = document.getElementById("failAttentionText");
  const count = cards.filter(c => !c.excluded).length;
  if (!btn || !text) return;
  if (count > 0) {
    text.textContent = `확인 필요한 문서 ${count}건`;
    btn.style.display = "flex";
  } else {
    btn.style.display = "none";
  }
}

function openFailuresPanel() {
  if (!bridge) { showToast("브리지가 준비되지 않았습니다."); return; }
  _failFilter = "all";
  document.getElementById("failuresOverlay").classList.add("show");
  if (_failWaitTimer === null) _failWaitTimer = setInterval(_refreshFailureWaitLabels, 30000);
  renderFailurePolicyUI();
  refreshFailurePolicyStatus();
  return _refreshFailures(true).catch(() => showToast("실패 목록을 불러오지 못했습니다."));
}

function closeFailuresPanel() {
  document.getElementById("failuresOverlay").classList.remove("show");
  if (_failWaitTimer !== null) clearInterval(_failWaitTimer);
  _failWaitTimer = null;
}

if (window.addEventListener) window.addEventListener("unload", () => {
  if (_failWaitTimer !== null) clearInterval(_failWaitTimer);
});

function _failCategory(card) {
  if (card.excluded) return "excluded";
  // 6a: "조치 필요"는 kind(NEEDS_USER_ACTION류)뿐 아니라 반복 승격(escalation)
  // 으로도 진입한다 — 같은 원인으로 3회 이상 실패하면 원인이 무엇이든 표시된다.
  if (card.kind === "NEEDS_USER_ACTION" || card.escalation === "NEEDS_ACTION") return "action";
  return "waiting";
}

function setFailFilter(cat) {
  _failFilter = cat;
  renderFailuresPanel();
}

function renderFailuresPanel() {
  const filters = document.getElementById("failFilters");
  const counts = { all: _failCards.length, action: 0, waiting: 0, excluded: 0 };
  _failCards.forEach(c => counts[_failCategory(c)]++);
  if (filters) {
    filters.querySelectorAll(".filter-chip").forEach(chip => {
      const cat = chip.dataset.cat;
      chip.textContent = `${_failFilterLabel(cat)} ${counts[cat]}`;
      chip.classList.toggle("active", cat === _failFilter);
      chip.onclick = () => setFailFilter(cat);
    });
  }

  const visible = _failCards.filter(c => _failFilter === "all" || _failCategory(c) === _failFilter);
  _renderBulkFailureAction(visible);

  const body = document.getElementById("failBody");
  if (!body) return;
  if (visible.length === 0) {
    body.innerHTML = '<div class="fail-empty">해당하는 문서가 없습니다.</div>';
    return;
  }
  body.innerHTML = visible.map(_renderFailCard).join("");
  _bindFailCardActions(body, visible);
  updateQueryControls();
}

function _bulkFailureTargets(visible) {
  return _failFilter === "excluded" ? visible : visible.filter(card => !card.excluded);
}

function _renderBulkFailureAction(visible) {
  const button = document.getElementById("btnBulkFailAction");
  const retryAll = document.getElementById("btnRetryAll");
  if (!button || !retryAll) return;
  const isUnexclude = _failFilter === "excluded";
  const targets = _bulkFailureTargets(visible);
  button.textContent = failurePolicyBlocked() ? _failurePolicyState.policy_unknown && !_failurePolicyState.active ? "설정 확인 필요" : _failurePolicySubmitting ? "요청 중…" : "제외 처리 중…" : isUnexclude ? `${targets.length}건 제외 해제` : `${targets.length}건 제외`;
  button.classList.toggle("unexclude", isUnexclude);
  button.disabled = failurePolicyBlocked() || _failBulkRunning || frontendSubmitting || backendBusy || targets.length === 0;
  retryAll.style.display = isUnexclude ? "none" : "";
}

function runBulkFailureAction() {
  if (!bridge || policyWriteBlocked()) return;
  const visible = _failCards.filter(c => _failFilter === "all" || _failCategory(c) === _failFilter);
  const targets = _bulkFailureTargets(visible);
  if (targets.length === 0) return;

  const isUnexclude = _failFilter === "excluded";
  const message = isUnexclude
    ? `${targets.length}개 문서를 인덱싱 제외에서 해제합니다.\n다음 인덱싱부터 다시 대상이 됩니다. 기존 실패 이력과 재시도 대기는 유지됩니다.\n계속하시겠습니까?`
    : `${targets.length}개 문서를 인덱싱에서 제외합니다.\n기존 검색 데이터는 삭제되지만 원본 파일은 삭제되지 않습니다.\n계속하시겠습니까?`;
  if (!window.confirm(message)) return;

  return _startFailurePolicyJob(isUnexclude ? "unexclude" : "exclude", targets.map(card => card.path));
}

// data-act → 실행할 함수. 경로는 HTML을 거치지 않고 카드 객체에서 직접 꺼내
// 클로저로 전달하므로, 경로에 역슬래시·작은따옴표가 있어도 원본 그대로 넘어간다.
const _FAIL_ACTIONS = {
  retry: (path) => retryFailureFile(path),
  reveal: (path) => revealFailureFile(path),
  exclude: (path) => excludeFailureFile(path),
  unexclude: (path) => unexcludeFailureFile(path),
};

function _bindFailCardActions(body, visible) {
  body.querySelectorAll("button[data-act]").forEach(btn => {
    const card = visible[Number(btn.dataset.idx)];
    const handler = _FAIL_ACTIONS[btn.dataset.act];
    if (!card || !handler) return;
    btn.addEventListener("click", () => handler(card.path));
  });
}

function _failFilterLabel(cat) {
  return { all: "전체", action: "조치 필요", waiting: "재시도 대기", excluded: "제외됨" }[cat] || cat;
}

function _formatFailTimestamp(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const now = new Date();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (d.toDateString() === now.toDateString()) return `오늘 ${hh}:${mm}`;
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return `어제 ${hh}:${mm}`;
  const days = Math.floor((now - d) / 86400000);
  if (days > 0 && days < 7) return `${days}일 전`;
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function _formatFailWait(nextTs) {
  if (nextTs == null) return "다음 사이클에 재시도 예정";
  const remaining = Math.max(nextTs - Date.now() / 1000, 0);
  if (remaining <= 0) return "다음 사이클에 재시도 예정";
  const days = Math.floor(remaining / 86400);
  const hours = Math.floor((remaining % 86400) / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  if (days > 0) return `${days}일 ${hours}시간 후`;
  if (hours > 0) return `${hours}시간 ${minutes}분 후`;
  return `${minutes}분 후`;
}

function _renderFailCard(card, index) {
  // 경로는 HTML에 넣지 않는다 — data-idx로 배열 위치만 넘기고, 실제 문자열은
  // _bindFailCardActions가 클로저로 잡아 전달한다. 인라인 onclick에 경로를
  // 문자열로 끼워 넣으면 Windows 경로(C:\Users\...)의 역슬래시가 JS 이스케이프로
  // 해석돼 값이 깨지고(\t→탭, \U→U), 작은따옴표가 든 이름은 문자열을 조기
  // 종료시켜 임의 코드 실행까지 가능하다.
  const act = (name, label, cls) =>
    `<button class="${cls}" data-act="${name}" data-idx="${index}">${label}</button>`;
  if (card.excluded) {
    return `
      <div class="f-card excluded">
        <div class="f-top">
          <span class="f-name">${escHtml(card.name)}</span>
          <span class="f-badge b-gray">제외됨</span>
        </div>
        <div class="f-path">${escHtml(card.dir)}</div>
        <div class="f-meta">
          <span><span class="k">단계</span> ${escHtml(card.stage_label)}</span>
          <span><span class="k">실패</span> ${card.consecutive_failures}회</span>
          <span><span class="k">마지막</span> ${_formatFailTimestamp(card.last_failed_ts)}</span>
          <span class="k">다음 자동 재시도</span><span class="next none">사용자가 제외함</span>
        </div>
        <div class="f-actions">
          ${act("unexclude", "제외 해제", "f-btn primary")}
          ${act("reveal", "🗀 파일 위치 열기", "f-btn")}
        </div>
      </div>`;
  }
  const waitLabel = _formatFailWait(card.next_retry_ts);
  // 6a: 같은 원인으로 2회째 반복되면(아직 조치 필요 임계 전) 배지에 "반복"을
  // 덧붙인다 — kind 배지(원인)와 escalation(누적)은 서로 다른 축이라 별도 표시.
  const repeatSuffix = card.escalation === "REPEATED" ? " · 반복" : "";
  return `
    <div class="f-card">
      <div class="f-top">
        <span class="f-name">${escHtml(card.name)}</span>
        <span class="f-badge ${escHtml(card.badge_class)}">${escHtml(card.badge_label)}${repeatSuffix}</span>
      </div>
      <div class="f-path">${escHtml(card.dir)}</div>
      <div class="f-meta">
        <span><span class="k">단계</span> ${escHtml(card.stage_label)}</span>
        <span><span class="k">실패</span> ${card.consecutive_failures}회</span>
        <span><span class="k">마지막</span> ${_formatFailTimestamp(card.last_failed_ts)}</span>
        <span class="k">다음 자동 재시도</span><span class="next" data-next-retry="${Number.isFinite(card.next_retry_ts) ? card.next_retry_ts : ""}">${waitLabel}</span>
      </div>
      <div class="f-actions">
        ${act("retry", "⟳ 지금 다시 시도", "f-btn primary")}
        ${act("reveal", "🗀 파일 위치 열기", "f-btn")}
        ${act("exclude", "인덱싱 제외", "f-btn danger spacer")}
      </div>
    </div>`;
}

function retryFailureFile(path) {
  if (!bridge) return;
  if (failurePolicyBlocked()) { showToast(failurePolicyBusyMessage()); return; }
  bridge.retryFile(path).then(result => {
    if (result !== "ok") { showToast(_failureRequestMessage(result)); return; }
    showIndexRunning();
  }).catch(() => showToast("재시도 요청에 실패했습니다."));
}

function _failureRequestMessage(result) {
  if (result === "policy_busy") return failurePolicyBusyMessage();
  if (result === "busy") return "인덱싱 또는 종료 정리가 이미 진행 중입니다.";
  if (result === "persistence_pending") return "실패 이력을 저장하지 못했습니다. 저장 후 다시 시도해 주세요.";
  if (result === "not_found") return "재시도할 파일의 실패 이력을 찾을 수 없습니다.";
  return "재시도 요청을 시작하지 못했습니다.";
}

function excludeFailureFile(path) {
  if (!bridge || policyWriteBlocked()) return;
  return _startFailurePolicyJob("exclude", [path]);
}

function unexcludeFailureFile(path) {
  if (!bridge || policyWriteBlocked()) return;
  return _startFailurePolicyJob("unexclude", [path]);
}

function revealFailureFile(path) {
  if (!bridge) return;
  bridge.revealFile(path).then(result => {
    if (result === "not_found") showToast("원본을 찾을 수 없음");
  }).catch(() => showToast("파일 위치 열기 실패"));
}
