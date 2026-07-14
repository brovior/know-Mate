/* ===== KnowMate 프런트엔드 ===== */
'use strict';

let bridge = null;
let currentMode = "knowledge";
let waiting = false;
let currentThread = null;   // 현재 대화 스레드 {id, title, mode, created_at, messages}

// QWebEngineView 환경에서 .checked 동적 읽기가 불안정한 경우를 방어하기 위해 JS 변수로 별도 추적
let scopeLocal  = true;
let scopeShared = true;
let scopeMail   = true;

/* -- QWebChannel 초기화 -- */
new QWebChannel(qt.webChannelTransport, function(channel) {
  bridge = channel.objects.bridge;
  bridge.responseReady.connect(onResponse);
  bridge.indexProgress.connect(onIndexProgress);
  bridge.indexFinished.connect(onIndexFinished);
  bridge.indexAlert.connect(onIndexAlert);
  bridge.statusUpdated.connect(onStatusUpdated);
  onBridgeReady();
});

function onBridgeReady() {
  renderEmptyState();
  initTitlebarDrag();
  initScopeCheckboxes();
  bridge.getIndexStatus().then(json => {
    let data;
    try { data = JSON.parse(json); } catch { return; }
    onStatusUpdated(data);
  });
  loadRecentQuestions();
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
function switchMode(mode) {
  currentMode = mode;
  document.getElementById("segKnow").classList.toggle("active", mode === "knowledge");
  document.getElementById("segMes").classList.toggle("active",  mode === "mes");
  document.getElementById("panelKnow").style.display = mode === "knowledge" ? "" : "none";
  document.getElementById("panelMes").style.display  = mode === "mes"       ? "" : "none";
  loadRecentQuestions();
}

/* -- 입력 전송 -- */
function handleKey(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMsg(); }
}

function sendMsg() {
  if (waiting || !bridge) return;
  const inp = document.getElementById("inputPill");
  const text = inp.value.trim();
  if (!text) return;

  // 첫 메시지면 스레드 초기화
  if (!currentThread) {
    currentThread = {
      id: _genId(),
      title: text.substring(0, 30),
      mode: currentMode,
      created_at: new Date().toISOString(),
      messages: [],
    };
  }
  currentThread.messages.push({ role: "user", content: text });

  appendUserMsg(text);
  inp.value = "";
  setWaiting(true);

  const scopes = [];
  if (currentMode === "knowledge") {
    if (scopeLocal)  scopes.push("local");
    if (scopeShared) scopes.push("shared");
    if (scopeMail)   scopes.push("mail");

    if (scopes.length === 0) {
      setWaiting(false);
      removeLoading();
      appendAiBlocks([{ type: "text", content: "검색 범위를 하나 이상 선택해주세요. (내 PC 문서 또는 공유 폴더)" }]);
      return;
    }
  }

  bridge.sendQuery(JSON.stringify({ query: text, mode: currentMode, scopes }));
}

function _genId() {
  return Date.now().toString(36) + Math.random().toString(36).substring(2, 7);
}

function setWaiting(on) {
  waiting = on;
  const inp = document.getElementById("inputPill");
  inp.disabled = on;
  if (on) showLoading(true);
}

/* -- Python 응답 수신 -- */
function onResponse(json) {
  showLoading(false);
  setWaiting(false);

  let data;
  try { data = JSON.parse(json); } catch { return; }

  removeLoading();
  appendAiBlocks(data.blocks || []);
  document.getElementById("inputPill").focus();

  // 스레드에 AI 응답 저장
  if (currentThread && bridge) {
    currentThread.messages.push({ role: "ai", blocks: data.blocks || [] });
    bridge.saveThread(currentThread.mode, JSON.stringify(currentThread));
    loadRecentQuestions();
  }
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
    row.innerHTML =
      `<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml(path)}</span>` +
      `<button onclick="removeFolder(this,'${escHtml(path)}')" style="background:none;border:none;color:#888;cursor:pointer;font-size:16px;padding:0 4px;">✕</button>`;
    list.appendChild(row);
  });
}

function removeFolder(btn, path) {
  if (!bridge) return;
  bridge.removeWatchFolder(path).then(json => renderFolderList(JSON.parse(json)));
}

function addFolder() {
  if (!bridge) return;
  bridge.selectFolder().then(path => {
    if (!path) return;
    bridge.addWatchFolder(path).then(json => renderFolderList(JSON.parse(json)));
  });
}

/* -- 인덱싱 버튼 -- */
function startFullIndex() {
  closeOnboarding();
  if (bridge) {
    bridge.startReindex();
  } else {
    showToast("브리지가 준비되지 않았습니다.");
  }
}

/* -- 증분 재인덱싱 -- */
function startReindex() {
  if (!bridge) { showToast("브리지가 준비되지 않았습니다."); return; }
  // 클릭 즉시 로딩 상태 표시 (progress 시그널 오기 전에도 보이도록)
  showIndexRunning();
  bridge.startReindex();
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
  if (bridge) bridge.cancelReindex();
}

function newThread() {
  currentThread = null;
  renderEmptyState();
  document.getElementById("inputPill").focus();
  loadRecentQuestions();
}

/* -- 최근 질문 -- */
function loadRecentQuestions() {
  if (!bridge) return;
  bridge.getThreads(currentMode).then(json => {
    let threads;
    try { threads = JSON.parse(json); } catch { return; }
    renderRecentList(threads);
  });
}

function renderRecentList(threads) {
  const list = document.getElementById("recentList");
  list.innerHTML = '<div class="panel-title" style="margin-top:4px;">최근 질문</div>';
  (threads || []).slice(0, 15).forEach(t => {
    const div = document.createElement("div");
    div.className = "recent-item" + (currentThread && currentThread.id === t.id ? " active" : "");
    div.title = t.title;
    div.onclick = () => restoreThread(t);

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

function deleteQuestion(thread) {
  if (!bridge) return;
  bridge.deleteThread(currentMode, thread.id);
  // 현재 열려 있는 스레드를 삭제하면 화면을 비우고 목록 갱신
  if (currentThread && currentThread.id === thread.id) {
    newThread();              // 내부에서 loadRecentQuestions 호출
  } else {
    loadRecentQuestions();
  }
}

function restoreThread(thread) {
  currentThread = thread;
  const scroll = document.getElementById("chatScroll");
  scroll.innerHTML = "";
  (thread.messages || []).forEach(msg => {
    if (msg.role === "user") {
      appendUserMsg(msg.content);
    } else if (msg.role === "ai") {
      appendAiBlocks(msg.blocks || []);
    }
  });
  loadRecentQuestions();  // active 상태 반영
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
