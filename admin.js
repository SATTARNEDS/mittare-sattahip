"use strict";

const state = { user: null, csrfToken: "", questions: [], topics: [], page: 1, pagination: null, pdfQuestions: [], pdfFilename: "" };
const statusLabels = { draft: "ฉบับร่าง", pending: "รอตรวจ", published: "เผยแพร่", paused: "ระงับ" };
const audienceLabels = { agent: "ตัวแทน", general: "ความรู้ทั่วไป", broker: "นายหน้า" };
const actionLabels = { created: "สร้าง", updated: "แก้ไข", submitted: "ส่งตรวจ", published: "เผยแพร่", rejected: "ส่งกลับแก้ไข", paused: "ระงับ", restored: "กู้คืน" };
const loginView = document.getElementById("login-view");
const adminView = document.getElementById("admin-view");
const sessionLoader = document.getElementById("session-loader");
const questionDialog = document.getElementById("question-dialog");
const questionForm = document.getElementById("question-form");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

async function api(path, options = {}) {
  const requestOptions = { credentials: "same-origin", ...options, headers: { ...(options.headers || {}) } };
  if (requestOptions.body && typeof requestOptions.body !== "string") {
    requestOptions.headers["Content-Type"] = "application/json";
    requestOptions.body = JSON.stringify(requestOptions.body);
  }
  if (requestOptions.method && requestOptions.method !== "GET") requestOptions.headers["X-CSRF-Token"] = state.csrfToken;
  const response = await fetch(path, requestOptions);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "ระบบไม่สามารถดำเนินการได้");
  return data;
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 2800);
}

function showAdmin() {
  loginView.hidden = true;
  adminView.hidden = false;
  document.getElementById("admin-name").textContent = `${state.user.displayName} · ${state.user.role === "admin" ? "Admin" : "หัวหน้าทีม"}`;
  document.querySelectorAll(".admin-only").forEach(element => { element.hidden = state.user.role !== "admin"; });
}

async function restoreSession() {
  try {
    const data = await api("/exam/api/admin/me");
    state.user = data.user;
    state.csrfToken = data.csrfToken;
    showAdmin();
    try { await refreshWorkspace(); }
    catch (error) { showToast(error.message); }
  } catch (_error) {
    loginView.hidden = false;
    try {
      const setup = await api("/exam/api/admin/setup-status");
      if (!setup.configured) document.getElementById("login-message").textContent = "ยังไม่มีบัญชี Admin กรุณาตั้ง ADMIN_PASSWORD ใน Render แล้ว redeploy";
      else if (!setup.persistentStorage) document.getElementById("login-message").textContent = "คำเตือน: ฐานข้อมูล Render ยังเป็นพื้นที่ชั่วคราว ควรเพิ่ม Persistent Disk ก่อนบันทึกข้อมูลจริง";
    } catch (_setupError) { /* หน้าเข้าสู่ระบบยังใช้งานได้แม้ตรวจสถานะไม่สำเร็จ */ }
  } finally {
    sessionLoader.hidden = true;
  }
}

document.getElementById("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const message = document.getElementById("login-message");
  message.textContent = "";
  const form = new FormData(event.currentTarget);
  try {
    const data = await api("/exam/api/admin/login", { method: "POST", body: { username: form.get("username"), password: form.get("password") } });
    state.user = data.user;
    state.csrfToken = data.csrfToken;
    showAdmin();
    await refreshWorkspace();
  } catch (error) { message.textContent = error.message; }
});

document.getElementById("logout-button").addEventListener("click", async () => {
  try { await api("/exam/api/admin/logout", { method: "POST" }); } finally { window.location.reload(); }
});

async function loadDashboard() {
  const data = await api("/exam/api/admin/dashboard");
  ["total", "draft", "pending", "published", "paused", "agent", "general", "broker"].forEach(key => {
    document.getElementById(`stat-${key}`).textContent = data[key] || 0;
  });
}

async function loadQuestions() {
  const parameters = new URLSearchParams();
  const search = document.getElementById("search-input").value.trim();
  const status = document.getElementById("status-filter").value;
  const audience = document.getElementById("audience-filter").value;
  const topic = document.getElementById("topic-filter").value;
  const perPage = document.getElementById("per-page").value;
  if (search) parameters.set("search", search);
  if (status) parameters.set("status", status);
  if (audience) parameters.set("audience", audience);
  if (topic) parameters.set("topic", topic);
  parameters.set("page", state.page);
  parameters.set("perPage", perPage);
  const data = await api(`/exam/api/admin/questions?${parameters}`);
  state.questions = data.items;
  state.pagination = data.pagination;
  state.page = data.pagination.page;
  renderQuestions();
  renderPagination();
}

function renderQuestions() {
  const body = document.getElementById("questions-body");
  const firstItemOffset = (state.pagination.page - 1) * state.pagination.perPage;
  body.innerHTML = state.questions.map((question, index) => {
    const actions = [`<button class="action-button" data-action="edit" data-id="${question.id}">แก้ไข</button>`, `<button class="action-button" data-action="history" data-id="${question.id}">ประวัติ</button>`];
    if (question.status === "draft") actions.push(`<button class="action-button" data-action="submit" data-id="${question.id}">ส่งตรวจ</button>`);
    if (question.status === "pending" && state.user.role === "admin") actions.push(`<button class="action-button" data-action="publish" data-id="${question.id}">อนุมัติ</button>`, `<button class="action-button danger" data-action="reject" data-id="${question.id}">ส่งกลับ</button>`);
    if (question.status === "published" && state.user.role === "admin") actions.push(`<button class="action-button danger" data-action="pause" data-id="${question.id}">ระงับ</button>`);
    if (question.status === "paused" && state.user.role === "admin") actions.push(`<button class="action-button" data-action="restore" data-id="${question.id}">กู้คืน</button>`);
    const bankOrder = state.pagination.totalItems - firstItemOffset - index;
    return `<tr><td><strong>${bankOrder}</strong><small class="question-id">ID ${question.id}</small></td><td class="question-cell"><strong>${escapeHtml(question.q)}</strong><small>เฉลย: ${escapeHtml(question.a)}</small></td><td><span class="audience-tag audience-${question.audience}">${audienceLabels[question.audience] || escapeHtml(question.audience)}</span></td><td>${escapeHtml(question.topic)}</td><td><span class="badge badge-${question.status}">${statusLabels[question.status]}</span></td><td>${escapeHtml(formatDate(question.updatedAt))}</td><td><div class="row-actions">${actions.join("")}</div></td></tr>`;
  }).join("");
  document.getElementById("questions-empty").hidden = state.questions.length > 0;
}

async function loadTopics() {
  const parameters = new URLSearchParams();
  const audience = document.getElementById("audience-filter").value;
  const status = document.getElementById("status-filter").value;
  if (audience) parameters.set("audience", audience);
  if (status) parameters.set("status", status);
  state.topics = await api(`/exam/api/admin/topics?${parameters}`);
  const topics = state.topics.map(item => item.topic);
  document.getElementById("topic-list").innerHTML = topics.map(topic => `<option value="${escapeHtml(topic)}"></option>`).join("");
  const filter = document.getElementById("topic-filter");
  const selected = filter.value;
  filter.innerHTML = `<option value="">ทุกหมวด</option>${state.topics.map(item => `<option value="${escapeHtml(item.topic)}">${escapeHtml(item.topic)} (${item.questionCount})</option>`).join("")}`;
  filter.value = topics.includes(selected) ? selected : "";
}

function renderPagination() {
  const pagination = state.pagination;
  if (!pagination) return;
  const first = pagination.totalItems ? ((pagination.page - 1) * pagination.perPage) + 1 : 0;
  const last = Math.min(pagination.page * pagination.perPage, pagination.totalItems);
  document.getElementById("pagination-summary").textContent = pagination.totalItems ? `แสดง ${first}–${last} จาก ${pagination.totalItems} ข้อ` : "0 รายการ";
  document.getElementById("page-indicator").textContent = `หน้า ${pagination.page} / ${pagination.totalPages}`;
  document.getElementById("previous-page").disabled = !pagination.hasPrevious;
  document.getElementById("next-page").disabled = !pagination.hasNext;
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value.endsWith?.("Z") || value.includes("+") ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("th-TH", { dateStyle: "short", timeStyle: "short" }).format(date);
}

async function refreshWorkspace() {
  await Promise.all([loadDashboard(), loadTopics()]);
  await loadQuestions();
}
document.getElementById("refresh-button").addEventListener("click", refreshWorkspace);
async function applyBankFilters() { state.page = 1; await loadTopics(); await loadQuestions(); }
document.getElementById("status-filter").addEventListener("change", applyBankFilters);
document.getElementById("audience-filter").addEventListener("change", applyBankFilters);
document.getElementById("topic-filter").addEventListener("change", () => { state.page = 1; loadQuestions(); });
document.getElementById("per-page").addEventListener("change", () => { state.page = 1; loadQuestions(); });
document.getElementById("previous-page").addEventListener("click", () => { if (state.pagination?.hasPrevious) { state.page -= 1; loadQuestions(); } });
document.getElementById("next-page").addEventListener("click", () => { if (state.pagination?.hasNext) { state.page += 1; loadQuestions(); } });
let searchTimer;
document.getElementById("search-input").addEventListener("input", () => { clearTimeout(searchTimer); state.page = 1; searchTimer = setTimeout(loadQuestions, 300); });

function buildOptions(options = ["", "", "", ""], answer = "") {
  document.getElementById("options-editor").innerHTML = options.map((option, index) => `<label class="option-row"><input type="radio" name="correctIndex" value="${index}" ${option === answer ? "checked" : ""} required><input name="option${index}" value="${escapeHtml(option)}" required maxlength="500" aria-label="ตัวเลือก ${index + 1}"></label>`).join("");
}

function openQuestionEditor(question = null) {
  questionForm.reset();
  questionForm.elements.id.value = question?.id || "";
  questionForm.elements.audience.value = question?.audience || "agent";
  questionForm.elements.topic.value = question?.topic || "";
  questionForm.elements.q.value = question?.q || "";
  questionForm.elements.e.value = question?.e || "";
  questionForm.elements.difficulty.value = question?.difficulty || "medium";
  questionForm.elements.examFrequency.value = question?.examFrequency || "medium";
  questionForm.elements.sourceTitle.value = question?.sourceTitle || "";
  questionForm.elements.sourceUrl.value = question?.sourceUrl || "";
  questionForm.elements.verifiedAt.value = question?.verifiedAt || "";
  buildOptions(question?.o, question?.a);
  document.getElementById("question-dialog-title").textContent = question ? `แก้ไขข้อ ${question.id}` : "เพิ่มข้อสอบ";
  document.getElementById("question-message").textContent = "";
  questionDialog.showModal();
}

document.getElementById("new-question-button").addEventListener("click", () => openQuestionEditor());
document.getElementById("close-dialog").addEventListener("click", () => questionDialog.close());
document.getElementById("cancel-question").addEventListener("click", () => questionDialog.close());
document.getElementById("close-history").addEventListener("click", () => document.getElementById("history-dialog").close());

questionForm.addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(questionForm);
  const options = [0, 1, 2, 3].map(index => String(form.get(`option${index}`) || ""));
  const correctIndex = Number(form.get("correctIndex"));
  const payload = { audience: form.get("audience"), topic: form.get("topic"), q: form.get("q"), e: form.get("e"), o: options, a: options[correctIndex], difficulty: form.get("difficulty"), examFrequency: form.get("examFrequency"), sourceTitle: form.get("sourceTitle"), sourceUrl: form.get("sourceUrl"), verifiedAt: form.get("verifiedAt") };
  const id = form.get("id");
  try {
    await api(id ? `/exam/api/admin/questions/${id}` : "/exam/api/admin/questions", { method: id ? "PUT" : "POST", body: payload });
    questionDialog.close();
    showToast(id ? "บันทึกการแก้ไขแล้ว และเปลี่ยนเป็นฉบับร่าง" : "สร้างข้อสอบฉบับร่างแล้ว");
    await refreshWorkspace();
  } catch (error) { document.getElementById("question-message").textContent = error.message; }
});

document.getElementById("questions-body").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = Number(button.dataset.id);
  const question = state.questions.find(item => item.id === id);
  if (button.dataset.action === "edit") { openQuestionEditor(question); return; }
  if (button.dataset.action === "history") { await showHistory(id); return; }
  if (button.dataset.action === "reject") {
    const reason = window.prompt("ระบุเหตุผลที่ส่งกลับให้แก้ไข");
    if (!reason) return;
    try { await api(`/exam/api/admin/questions/${id}/reject`, { method: "POST", body: { reason } }); showToast("ส่งข้อสอบกลับไปแก้ไขแล้ว"); await refreshWorkspace(); } catch (error) { showToast(error.message); }
    return;
  }
  const confirmations = { submit: "ส่งข้อสอบนี้ให้ Admin ตรวจหรือไม่?", publish: "อนุมัติและนำข้อสอบนี้เข้าสู่การสุ่มหรือไม่?", pause: "ระงับข้อสอบนี้ทันทีหรือไม่?", restore: "กู้คืนข้อสอบนี้เข้าสู่การสุ่มหรือไม่?" };
  if (!window.confirm(confirmations[button.dataset.action])) return;
  try {
    await api(`/exam/api/admin/questions/${id}/${button.dataset.action}`, { method: "POST" });
    showToast("อัปเดตสถานะเรียบร้อยแล้ว");
    await refreshWorkspace();
  } catch (error) { showToast(error.message); }
});

async function showHistory(id) {
  const versions = await api(`/exam/api/admin/questions/${id}/versions`);
  document.getElementById("history-list").innerHTML = versions.length ? versions.map(version => `<article><strong>รุ่น ${version.version} · ${actionLabels[version.action] || escapeHtml(version.action)}</strong><small>${escapeHtml(version.changedBy || "ระบบ")} · ${escapeHtml(formatDate(version.createdAt))}</small><p>${escapeHtml(version.snapshot.question_text)}</p></article>`).join("") : "<p>ยังไม่มีประวัติ</p>";
  document.getElementById("history-dialog").showModal();
}

document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", async () => {
  document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === tab));
  document.querySelectorAll(".panel").forEach(panel => { panel.hidden = panel.id !== tab.dataset.panel; });
  if (tab.dataset.panel === "members-panel") await loadMembers();
  if (tab.dataset.panel === "users-panel") await loadUsers();
  if (tab.dataset.panel === "audit-panel") await loadAudit();
}));

const attemptModeLabels = {practice:"สุ่มทุกหมวด",topic:"ฝึกเฉพาะหมวด",simulation:"จำลองสอบจริง"};
const memberAttemptState = {memberId:null,page:1,totalItems:0};

async function loadMembers() {
  const search = document.getElementById("member-search").value.trim();
  const members = await api(`/exam/api/admin/members?search=${encodeURIComponent(search)}`);
  document.getElementById("members-empty").hidden = members.length > 0;
  document.getElementById("members-body").innerHTML = members.map(member => `<tr><td><div class="question-cell"><strong>${escapeHtml(member.display_name)}</strong><small>${member.username ? `@${escapeHtml(member.username)}` : "บัญชีเดิม"}</small></div></td><td>${member.attempts}</td><td>${member.simulation_attempts || 0}</td><td>${member.topic_attempts || 0}</td><td>${member.average_score}%</td><td><strong>${member.best_score}%</strong></td><td>${member.last_attempt_at ? escapeHtml(formatDate(member.last_attempt_at)) : "ยังไม่เคยทำ"}</td><td><button class="action-button" data-member-id="${member.id}" type="button">ดูประวัติ</button></td></tr>`).join("");
}

let memberSearchTimer;
document.getElementById("member-search").addEventListener("input", () => {
  clearTimeout(memberSearchTimer);
  memberSearchTimer = setTimeout(() => loadMembers().catch(error => showToast(error.message)), 250);
});

document.getElementById("members-body").addEventListener("click", async event => {
  const button = event.target.closest("[data-member-id]");
  if (!button) return;
  try {
    memberAttemptState.memberId = button.dataset.memberId;
    memberAttemptState.page = 1;
    const data = await loadMemberAttempts(false);
    document.getElementById("member-history-title").textContent = data.member.display_name;
    document.getElementById("member-history-summary").textContent = `${data.member.username ? `@${data.member.username} • ` : ""}${data.pagination.totalItems} รอบ`;
    document.getElementById("member-history-dialog").showModal();
  } catch (error) { showToast(error.message); }
});

function renderAttemptCard(attempt) {
  const percentage = Math.round(attempt.score * 100 / attempt.total_questions);
  const topicDetails = Object.entries(attempt.topicScores || {}).map(([topic, result]) => `<li><span>${escapeHtml(topic)}</span><strong>${Number(result.correct) || 0}/${Number(result.total) || 0}</strong></li>`).join("");
  return `<details class="attempt-card"><summary><span class="attempt-summary-main"><span class="badge badge-${percentage >= 60 ? "published" : "paused"}">${attemptModeLabels[attempt.exam_mode] || "ฝึกข้อสอบ"}</span><span><strong>${percentage}%</strong><small>${attempt.score}/${attempt.total_questions} คะแนน</small></span></span><span class="attempt-summary-meta">${escapeHtml(formatDate(attempt.completed_at))}</span></summary><div class="attempt-detail"><dl><div><dt>ชุดที่ทำ</dt><dd>${escapeHtml(attempt.selected_topic)}</dd></div><div><dt>เวลา</dt><dd>${Math.floor(attempt.duration_seconds / 60)} นาที ${attempt.duration_seconds % 60} วินาที</dd></div></dl>${topicDetails ? `<h3>คะแนนแยกรายหมวด</h3><ul>${topicDetails}</ul>` : "<p>ไม่มีข้อมูลคะแนนแยกรายหมวด</p>"}</div></details>`;
}

async function loadMemberAttempts(append) {
  const data = await api(`/exam/api/admin/members/${memberAttemptState.memberId}/attempts?page=${memberAttemptState.page}&perPage=10`);
  memberAttemptState.totalItems = data.pagination.totalItems;
  const list = document.getElementById("member-attempts-list");
  const markup = data.attempts.map(renderAttemptCard).join("");
  if (append) list.insertAdjacentHTML("beforeend", markup);
  else list.innerHTML = markup || "<p class=\"empty\">สมาชิกยังไม่เคยทำข้อสอบ</p>";
  const loadMore = document.getElementById("load-more-attempts");
  loadMore.hidden = !data.pagination.hasNext;
  loadMore.disabled = false;
  return data;
}

document.getElementById("load-more-attempts").addEventListener("click", async event => {
  event.currentTarget.disabled = true;
  memberAttemptState.page += 1;
  try { await loadMemberAttempts(true); }
  catch (error) { memberAttemptState.page -= 1; event.currentTarget.disabled = false; showToast(error.message); }
});

document.getElementById("close-member-history").addEventListener("click", () => document.getElementById("member-history-dialog").close());

function openPdfImport() {
  document.getElementById("pdf-upload-form").reset();
  document.getElementById("pdf-preview").hidden = true;
  document.getElementById("pdf-upload-message").textContent = "";
  document.getElementById("pdf-import-message").textContent = "";
  document.getElementById("pdf-import-dialog").showModal();
}

function renderPdfPreview() {
  const readyCount = state.pdfQuestions.filter(question => question.ready).length;
  document.getElementById("pdf-preview-summary").textContent = `พบ ${state.pdfQuestions.length} ข้อ • พร้อมนำเข้า ${readyCount} ข้อ`;
  document.getElementById("pdf-preview-source").textContent = state.pdfFilename;
  document.getElementById("pdf-question-preview").innerHTML = state.pdfQuestions.map((question, index) => {
    const options = [...(question.o || [])];
    while (options.length < 4) options.push("");
    const duplicate = Boolean(question.duplicateId);
    const warnings = question.warnings?.length ? `<ul>${question.warnings.map(warning => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>` : "<p>ข้อมูลครบ พร้อมนำเข้า</p>";
    return `<article class="pdf-preview-card ${question.ready ? "is-ready" : "has-warning"}" data-pdf-index="${index}"><header><label class="pdf-select-question"><input type="checkbox" ${question.ready ? "checked" : ""} ${duplicate ? "disabled" : ""}><span>ข้อ ${question.sourceNumber || index + 1}</span></label><span class="pdf-read-status">${question.ready ? "พร้อม" : "ต้องตรวจ"}</span></header><label>คำถาม<textarea data-field="q" rows="2">${escapeHtml(question.q)}</textarea></label><fieldset><legend>ตัวเลือกและคำตอบที่ถูก</legend>${options.map((option, optionIndex) => `<label class="pdf-option"><input type="radio" name="pdf-answer-${index}" value="${optionIndex}" ${question.a === option && option ? "checked" : ""}><span>${String.fromCharCode(65 + optionIndex)}</span><input data-option="${optionIndex}" value="${escapeHtml(option)}" aria-label="ตัวเลือก ${optionIndex + 1}"></label>`).join("")}</fieldset><label>คำอธิบายเฉลย<textarea data-field="e" rows="2">${escapeHtml(question.e)}</textarea></label><div class="pdf-warnings">${warnings}</div></article>`;
  }).join("");
  document.getElementById("pdf-preview").hidden = false;
}

document.getElementById("pdf-import-button").addEventListener("click", openPdfImport);
document.getElementById("close-pdf-import").addEventListener("click", () => document.getElementById("pdf-import-dialog").close());
document.getElementById("pdf-upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const message = document.getElementById("pdf-upload-message");
  button.disabled = true; button.textContent = "กำลังอ่าน PDF..."; message.textContent = "";
  try {
    const response = await fetch("/exam/api/admin/pdf-import/preview", {method:"POST",credentials:"same-origin",headers:{"X-CSRF-Token":state.csrfToken},body:new FormData(event.currentTarget)});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "อ่าน PDF ไม่สำเร็จ");
    state.pdfQuestions = result.questions;
    state.pdfFilename = result.filename;
    renderPdfPreview();
    if (result.truncated) message.textContent = "ไฟล์มีข้อสอบจำนวนมาก ระบบแสดงสูงสุด 500 ข้อต่อครั้ง";
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; button.textContent = "อ่านและแสดงตัวอย่าง"; }
});

document.getElementById("toggle-ready-questions").addEventListener("click", () => {
  document.querySelectorAll(".pdf-select-question input:not(:disabled)").forEach(checkbox => { checkbox.checked = true; });
});

document.getElementById("commit-pdf-import").addEventListener("click", async event => {
  const topic = document.getElementById("pdf-topic").value.trim();
  const message = document.getElementById("pdf-import-message");
  if (!topic) { message.textContent = "กรุณาเลือกหรือกรอกหมวดข้อสอบ"; document.getElementById("pdf-topic").focus(); return; }
  const selected = [...document.querySelectorAll(".pdf-preview-card")].filter(card => card.querySelector(".pdf-select-question input").checked).map(card => {
    const index = Number(card.dataset.pdfIndex);
    const options = [...card.querySelectorAll("[data-option]")].map(input => input.value.trim());
    const answerIndex = Number(card.querySelector('input[type="radio"]:checked')?.value ?? -1);
    return {sourceNumber:state.pdfQuestions[index].sourceNumber,q:card.querySelector('[data-field="q"]').value.trim(),o:options,a:options[answerIndex] || "",e:card.querySelector('[data-field="e"]').value.trim(),topic,audience:document.getElementById("pdf-audience").value,difficulty:document.getElementById("pdf-difficulty").value,examFrequency:"medium",sourceTitle:state.pdfFilename};
  });
  if (!selected.length) { message.textContent = "กรุณาเลือกข้อสอบอย่างน้อย 1 ข้อ"; return; }
  event.currentTarget.disabled = true; event.currentTarget.textContent = "กำลังนำเข้า..."; message.textContent = "";
  try {
    let importedCount = 0;
    let rejectedCount = 0;
    const batches = [];
    for (let start = 0; start < selected.length; start += 200) batches.push(selected.slice(start, start + 200));
    for (let batchIndex = 0; batchIndex < batches.length; batchIndex += 1) {
      event.currentTarget.textContent = `กำลังนำเข้าชุด ${batchIndex + 1}/${batches.length}...`;
      const response = await fetch("/exam/api/admin/pdf-import/commit", {method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrfToken},body:JSON.stringify({sourceTitle:state.pdfFilename,questions:batches[batchIndex]})});
      const result = await response.json().catch(() => ({}));
      if (!response.ok && !result.imported?.length) throw new Error(result.error || result.errors?.[0]?.error || `นำเข้าชุด ${batchIndex + 1} ไม่สำเร็จ`);
      importedCount += result.imported?.length || 0;
      rejectedCount += result.errors?.length || 0;
    }
    showToast(`นำเข้า ${importedCount} ข้อเป็นฉบับร่าง${rejectedCount ? ` • ไม่ผ่าน ${rejectedCount} ข้อ` : ""}`);
    document.getElementById("pdf-import-dialog").close();
    state.page = 1;
    await refreshWorkspace();
  } catch (error) { message.textContent = error.message; }
  finally { event.currentTarget.disabled = false; event.currentTarget.textContent = "นำข้อที่เลือกเข้าเป็นฉบับร่าง"; }
});

async function loadUsers() {
  const users = await api("/exam/api/admin/users");
  document.getElementById("users-list").innerHTML = users.map(user => `<div class="person-row"><div><strong>${escapeHtml(user.display_name)}</strong><small>@${escapeHtml(user.username)} · ${user.role === "admin" ? "Admin" : "หัวหน้าทีม"}</small></div><span class="badge ${user.is_active ? "badge-published" : "badge-paused"}">${user.is_active ? "ใช้งาน" : "ปิด"}</span></div>`).join("");
}

document.getElementById("user-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const message = document.getElementById("user-message");
  try {
    await api("/exam/api/admin/users", { method: "POST", body: Object.fromEntries(form) });
    event.currentTarget.reset(); message.textContent = ""; showToast("สร้างบัญชีแล้ว"); await loadUsers();
  } catch (error) { message.textContent = error.message; }
});

async function loadAudit() {
  const logs = await api("/exam/api/admin/audit-logs");
  document.getElementById("audit-list").innerHTML = logs.map(log => `<article><strong>${escapeHtml(log.adminName)} · ${escapeHtml(log.action)}</strong><small>${escapeHtml(log.entityType)} ${escapeHtml(log.entityId || "")} · ${escapeHtml(formatDate(log.createdAt))}</small></article>`).join("");
}

restoreSession();
