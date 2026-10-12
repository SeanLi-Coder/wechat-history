"use strict";

const icons = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  archive: '<rect x="3" y="3" width="18" height="5" rx="1.5"/><path d="M5 8v11a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8M9 12h6"/>',
  chat: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z"/><path d="M8 11h8M8 14h5"/>',
  settings: '<path d="m9.5 3-.6 2a8 8 0 0 0-1.8 1L5 5.5 2.5 9l1.6 1.5a8 8 0 0 0 0 3L2.5 15 5 18.5l2.1-.5a8 8 0 0 0 1.8 1l.6 2h5l.6-2a8 8 0 0 0 1.8-1l2.1.5 2.5-3.5-1.6-1.5a8 8 0 0 0 0-3L21.5 9 19 5.5l-2.1.5a8 8 0 0 0-1.8-1l-.6-2h-5Z"/><circle cx="12" cy="12" r="3"/>',
  monitor: '<rect x="3" y="3" width="18" height="13" rx="2"/><path d="M8 21h8M12 16v5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  "arrow-right": '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  "arrow-left": '<path d="M20 12H4m6-6-6 6 6 6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5M6.1 6.1a8 8 0 0 1 13.2 3M4.7 14.9a8 8 0 0 0 13.2 3"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
  folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/><path d="M3 8h18"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Z"/><path d="M14 2v6h6M9 12h6M9 16h4"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m21 15-5-5L5 21"/>',
  audio: '<path d="M12 3v12M12 6l7-2v10"/><circle cx="8.5" cy="17.5" r="3.5"/><circle cx="15.5" cy="16.5" r="3.5"/>',
  video: '<rect x="3" y="5" width="13" height="14" rx="2"/><path d="m16 10 5-3v10l-5-3"/>',
};

const $ = (id) => document.getElementById(id);
const state = {
  status: null,
  page: "overview",
  token: "",
  settingsInitialized: false,
  settingsDirty: false,
  polling: null,
  refreshing: false,
  refreshAgain: false,
  conversations: [],
  globalSearch: null,
  conversationId: null,
  conversationLoaded: false,
  conversationRequest: 0,
  messagesRequest: 0,
  messages: [],
  messagesTotal: 0,
  messagesOffset: 0,
  messageContext: null,
  pendingMessageSearch: null,
  pendingGlobalSearch: null,
  messagesFilterKey: "",
  sourceSignature: "",
  lastRunningJob: null,
  completedJobs: new Set(),
  verifiedSnapshots: new Set(),
  verificationResults: new Map(),
  importing: false,
};

function icon(name, className = "icon") {
  const span = document.createElement("span");
  span.className = className;
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${icons[name] || icons.file}</svg>`;
  return span;
}

function hydrateIcons() {
  document.querySelectorAll("[data-icon]").forEach((element) => {
    const name = element.dataset.icon;
    element.replaceChildren(...icon(name).childNodes);
    element.setAttribute("aria-hidden", "true");
    element.removeAttribute("data-icon");
  });
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function actionButton(label, action, className = "text-button", iconName, data = {}) {
  const button = element("button", className);
  button.type = "button";
  button.dataset.action = action;
  Object.assign(button.dataset, data);
  if (iconName) button.append(icon(iconName));
  button.append(element("span", "", label));
  return button;
}

function emptyState(iconName, title, description, button) {
  const wrapper = element("div", "empty-state");
  wrapper.append(icon(iconName, "empty-icon"), element("h3", "", title), element("p", "", description));
  if (button) wrapper.append(button);
  return wrapper;
}

function parseTime(value) {
  if (!value) return null;
  const normalized = typeof value === "number" && value < 1e12 ? value * 1000 : value;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDate(value, short = false) {
  const date = parseTime(value);
  if (!date) return value ? String(value) : "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", ...(short
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) }).format(date);
}

function conversationTime(value) {
  const date = parseTime(value);
  if (!date) return "";
  const today = new Date();
  const sameDay = beijingTime(date).slice(0, 10) === beijingTime(today).slice(0, 10);
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", ...(sameDay
    ? { hour: "2-digit", minute: "2-digit", hour12: false }
    : { ...(beijingTime(date).slice(0, 4) !== beijingTime(today).slice(0, 4) ? { year: "2-digit" } : {}), month: "numeric", day: "numeric" }) }).format(date);
}

function beijingTime(value) {
  const date = parseTime(value);
  if (!date) return "时间未知";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

function searchFilters(scope) {
  return { query: $(scope === "global" ? "conversation-search" : "message-search").value.trim(),
    dateFrom: $(`${scope}-date-from`).value, dateTo: $(`${scope}-date-to`).value };
}

function filterKey(filters) {
  return JSON.stringify([filters.query, filters.dateFrom, filters.dateTo]);
}

function hasFilters(filters) {
  return Boolean(filters.query || filters.dateFrom || filters.dateTo);
}

function searchParameters(filters, offset, limit) {
  const parameters = new URLSearchParams({ q: filters.query, offset: String(offset), limit: String(limit) });
  if (filters.dateFrom) parameters.set("date_from", filters.dateFrom);
  if (filters.dateTo) parameters.set("date_to", filters.dateTo);
  return parameters.toString();
}

function validateSearchFilters(filters) {
  if (filters.dateFrom && filters.dateTo && filters.dateFrom > filters.dateTo) {
    throw new Error("结束日期不能早于开始日期，请调整日期范围。");
  }
}

function updateDateFilter(scope) {
  const { dateFrom, dateTo } = searchFilters(scope);
  const active = Boolean(dateFrom || dateTo);
  $(`${scope}-date-filter`).classList.toggle("is-filtered", active);
  $(`${scope}-date-label`).textContent = active ? "日期 · 已选" : "日期";
  $(`${scope}-date-filter`).querySelector('[data-action="clear-dates"]').disabled = !active;
  $(`${scope}-date-filter`).querySelector("summary").title = active
    ? `${dateFrom || "不限开始"} 至 ${dateTo || "不限结束"} · 按北京时间` : "按北京时间筛选日期";
}

function setDateRange(scope, range = {}) {
  $(`${scope}-date-from`).value = range.dateFrom || "";
  $(`${scope}-date-to`).value = range.dateTo || "";
  updateDateFilter(scope);
}

async function clearMessageSearch() {
  state.pendingMessageSearch?.cancel();
  $("message-search").value = "";
  setDateRange("message");
  await loadMessages(false);
}

function avatarTone(value) {
  return `avatar-tone-${Array.from(String(value)).reduce((sum, character) => sum + character.codePointAt(0), 0) % 5}`;
}

function avatarText(value) {
  return Array.from(String(value || "聊").trim()).slice(0, 2).join("") || "聊";
}

function setAvatar(node, label, url) {
  node.classList.remove("has-avatar");
  node.setAttribute("aria-hidden", "true");
  node.replaceChildren(element("span", "avatar-fallback", avatarText(label)));
  if (typeof url !== "string" || url.length !== 75 || !/^\/api\/media\/[a-f0-9]{64}$/.test(url)) return node;
  const image = element("img", "avatar-image");
  image.alt = "";
  image.loading = "lazy";
  image.decoding = "async";
  image.addEventListener("load", () => {
    if (image.parentNode === node) node.classList.add("has-avatar");
  }, { once: true });
  image.addEventListener("error", () => {
    if (image.parentNode !== node) return;
    image.remove();
    node.classList.remove("has-avatar");
  }, { once: true });
  image.src = url;
  node.append(image);
  return node;
}

function createAvatar(tag, className, label, url) {
  return setAvatar(element(tag, `${className} ${avatarTone(label)}`), label, url);
}

function highlighted(tag, className, value, query) {
  const node = element(tag, className);
  const text = String(value ?? "");
  if (!query) { node.textContent = text; return node; }
  const pattern = new RegExp(String(query).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    node.append(document.createTextNode(text.slice(offset, match.index)), element("mark", "search-highlight", match[0]));
    offset = match.index + match[0].length;
  }
  node.append(document.createTextNode(text.slice(offset)));
  return node;
}

function searchSnippet(value, query) {
  const text = String(value || "[非文本消息]").replace(/\s+/g, " ");
  const match = query ? text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase()) : 0;
  const start = Math.max(0, match - 35);
  return `${start ? "…" : ""}${text.slice(start, start + 180)}${text.length > start + 180 ? "…" : ""}`;
}

function renderConversationCount() {
  $("conversation-count").textContent = state.globalSearch
    ? `${count(state.globalSearch.conversationTotal)} 个用户 / 会话 · ${count(state.globalSearch.total)} 条聊天记录`
    : `${count(state.conversations.length)} 个会话 · ${state.status?.reader_source?.archived ? "归档阅读" : "本机阅读"}`;
}

function formatBytes(bytes) {
  const value = Math.max(0, Number(bytes) || 0);
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  return `${amount.toLocaleString("zh-CN", { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`;
}

function count(value) {
  return (Number(value) || 0).toLocaleString("zh-CN");
}

function localizeError(message, code) {
  const errors = {
    wechat_running: "请从微信菜单退出微信后重试，关闭窗口不等于退出。",
    process_check_failed: "暂时无法确认微信是否已退出，请检查系统权限后重试。",
    source_changed: "备份期间源文件发生变化，请退出微信以及正在修改文件的程序后重试。",
    insufficient_space: "备份目录的可用空间不足，请释放磁盘空间或选择其他位置。",
    permission_denied: "无法读取或写入文件，请在 macOS 设置中允许拾聊访问相应目录。",
    source_missing: "微信数据目录或其中的文件已不存在，请重新选择数据目录。",
    corrupt_archive: "归档无法读取或文件清单异常，请运行校验或创建新的备份。",
    backup_busy: "已有备份任务正在运行，请等待完成后重试。",
    nested_destination: "备份保存目录必须位于微信数据目录之外。",
    invalid_source: "请选择一个存在的微信数据文件夹。",
    invalid_destination: "备份保存位置必须是文件夹。",
    invalid_path: "请先选择数据来源和备份保存目录。",
    symlink_source: "数据来源不能是符号链接，请选择实际文件夹。",
    symlink_destination: "备份保存目录不能是符号链接，请选择实际文件夹。",
    unsafe_source: "不能备份整个磁盘根目录，请选择微信数据文件夹。",
    unsupported_file: "数据目录含有不支持的特殊文件，请选择微信账号的数据文件夹。",
    unsupported_path: "数据目录含有无法存入 ZIP 的文件名，请检查源目录。",
    invalid_snapshot: "归档标识无效，请刷新档案列表后重试。",
    pending_wal: "微信还有未完成保存的记录，请先正常退出微信，再重新读取。",
    invalid_key: "数据库密钥格式不正确，请导入本人账号的有效密钥文件。",
    wrong_key: "密钥与数据库不匹配，请重新初始化本机读取。",
    authentication_failed: "数据库完整性校验未通过，可能密钥不匹配或文件已损坏。",
    decrypt_failed: "本机聊天读取未完成，请确认微信已正常退出后重试。",
    sqlcipher_unavailable: "本机读取组件尚未准备好，请重新启动拾聊后重试。",
    pending_journal: "微信还有未完成保存的记录，请正常退出微信后重试。",
    key_or_database_invalid: "数据库副本无法读取，可能密钥不匹配或文件已损坏。",
    checkpoint_failed: "部分记录尚未完整保存，请正常退出微信后重新读取。",
    integrity_failed: "数据库完整性校验未通过，本次没有保存不完整的记录。",
    export_failed: "数据库副本导出未完成，请检查可用空间和文件访问权限。",
  };
  if (errors[code]) return errors[code];
  const exact = {
    "Choose source and destination folders": "请先选择数据来源和备份保存目录。",
    "Folder paths must be absolute": "请填写完整的文件夹路径，或点击「选择文件夹」。",
    "Source must be an existing folder, not a symbolic link": "请选择一个存在的实际数据文件夹，不要选择符号链接。",
    "Backup destination cannot be inside the source folder": "备份保存目录必须位于数据来源目录之外。",
    "Destination must be a folder": "备份保存位置必须是文件夹。",
    "Source must not contain the application state folder": "来源目录不能包含拾聊自身的数据，请选择微信账号的数据文件夹。",
    "Another operation is already running": "已有任务正在执行，请等待完成后重试。",
    "JSON requires a messages array": "JSON 文件需要包含 messages 数组，请检查聊天导出格式。",
    "JSONL requires a ChatLab header": "JSONL 文件需要 ChatLab 格式的 header，请使用 ChatLab 导出文件。",
    "CSV requires text/content, sender and timestamp columns": "CSV 文件需要 text 或 content 列，并建议包含 sender 和 timestamp 列。",
    "Import must contain 1 byte to 100 MB": "请选择有内容且不超过 100 MB 的聊天记录文件。",
    "Message is missing text/content": "导入记录中有消息缺少 text 或 content 字段，请检查文件格式。",
    "Not found": "未找到该文件或会话，请刷新后重试。",
    "Select a regular readable ZIP archive": "请选择拾聊保存的可读 ZIP 归档。",
    "This ZIP is not a supported readable archive": "这份 ZIP 不是拾聊可读归档，请选择「保存可读归档」生成的文件。",
    "A media object is missing; the readable archive was not created": "部分媒体副本缺失，请重新读取聊天后再保存归档。",
    "A media object changed while archiving": "保存归档时媒体发生变化，请等当前读取完成后重试。",
    "The readable archive changed after opening": "归档文件已发生变化，请重新打开这份归档。",
    "Archived conversation library is damaged": "这份归档中的聊天记录无法完整读取，请尝试其他备份。",
    "Readable archive entry failed its integrity check": "归档内容未通过完整性校验，请尝试其他备份。",
  };
  if (exact[message]) return exact[message];
  if (!message) return "操作未完成，请检查目录权限或文件格式后重试。";
  return /[\u3400-\u9fff]/.test(message) ? message : `操作未完成，请检查目录权限或文件格式。详细信息：${message}`;
}

function toast(message, error = false) {
  const box = element("div", `toast${error ? " error" : ""}`);
  box.setAttribute("role", error ? "alert" : "status");
  const close = element("button", "toast-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "关闭提示");
  close.addEventListener("click", () => box.remove());
  box.append(icon(error ? "info" : "check"), element("span", "", message), close);
  $("toast-stack").append(box);
  window.setTimeout(() => box.remove(), error ? 16000 : 6500);
}

function getToken() {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const token = params.get("token");
  if (token) {
    state.token = token;
    try { sessionStorage.setItem("chatkeeper-token", token); } catch (_) { /* Storage can be unavailable in private contexts. */ }
    history.replaceState(null, "", window.location.pathname + window.location.search);
  } else {
    try { state.token = sessionStorage.getItem("chatkeeper-token") || ""; } catch (_) { state.token = ""; }
  }
}

async function api(path, options = {}) {
  if (!state.token) throw new Error("当前窗口缺少本机访问凭证，请通过「拾聊」启动器重新打开。");
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${state.token}`);
  let body = options.body;
  if (body !== undefined && !(body instanceof Blob)) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, { ...options, body, headers, cache: "no-store" });
  } catch (_) {
    throw new Error("暂时无法连接本地服务。请确认「拾聊」仍在运行，再重试。");
  }
  const responseText = await response.text();
  let data;
  try { data = responseText ? JSON.parse(responseText) : {}; } catch (_) { data = {}; }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) throw new Error("本机访问凭证已失效，请通过「拾聊」启动器重新打开。");
    const detail = data.error || data.message || data.detail;
    throw new Error(localizeError(typeof detail === "string" ? detail : `HTTP ${response.status}`, data.code));
  }
  return data;
}

async function withBusy(button, task, pendingText = "处理中…") {
  if (button?.dataset.loading) return;
  let originalNodes;
  if (button) {
    originalNodes = Array.from(button.childNodes);
    button.dataset.loading = "true";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.replaceChildren(element("span", "", pendingText));
  }
  try {
    return await task();
  } catch (error) {
    toast(error.message || "操作未完成，请重试。", true);
    return null;
  } finally {
    if (button) {
      button.replaceChildren(...originalNodes);
      button.disabled = false;
      button.removeAttribute("aria-busy");
      delete button.dataset.loading;
    }
    updateJobButtons();
  }
}

function showPage(name) {
  if (!["overview", "archives", "reader", "settings"].includes(name)) name = "overview";
  state.page = name;
  document.body.classList.toggle("reader-active", name === "reader");
  $("main").classList.toggle("is-reader", name === "reader");
  $("reader-management").open = false;
  document.querySelectorAll(".page").forEach((page) => {
    const active = page.id === `page-${name}`;
    page.hidden = !active;
    page.classList.toggle("active", active);
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.page === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $("page-breadcrumb").textContent = { overview: "概览", archives: "备份档案", reader: "聊天阅读", settings: "设置" }[name];
  if (name === "reader" && !state.conversationLoaded) loadConversations();
}

function renderArchiveTable(snapshots, compact = false) {
  if (!snapshots.length) {
    return emptyState("archive", "还没有备份，故事正等待被收藏", "选择微信数据目录，创建第一份本地备份。以后回头看，它们都会在这里。", actionButton("开始第一次备份", "backup", "text-button", "plus"));
  }
  const table = element("table", "archive-table");
  table.setAttribute("aria-label", compact ? "最近的备份" : "全部备份档案");
  const head = element("thead");
  const headRow = element("tr");
  [["备份文件", ""], ["归档大小", "size-column"], ["状态", "status-column"], ["操作", "archive-action-column"]].forEach(([title, cls]) => {
    const cell = element("th", cls, title);
    cell.scope = "col";
    headRow.append(cell);
  });
  head.append(headRow);
  table.append(head);
  const body = element("tbody");
  snapshots.forEach((snapshot) => {
    const row = element("tr");
    const nameCell = element("td");
    const name = element("div", "archive-name");
    const summary = element("div");
    const snapshotName = snapshot.name || `微信备份 ${formatDate(snapshot.created_at)}`;
    summary.append(element("div", "archive-title", snapshotName));
    summary.append(element("div", "archive-detail", `${formatDate(snapshot.created_at)} · ${count(snapshot.file_count)} 个文件 · ZIP`));
    name.append(icon("archive", "archive-file-icon"), summary);
    nameCell.append(name);
    const size = element("td", "size-column archive-size", formatBytes(snapshot.archive_bytes));
    size.append(element("small", "", `原始 ${formatBytes(snapshot.original_bytes)}`));
    const status = element("td", "status-column");
    const verified = state.verifiedSnapshots.has(snapshot.id) || ["verified", "ok"].includes(snapshot.status) || Boolean(snapshot.verified_at);
    const failed = ["error", "corrupt", "failed"].includes(snapshot.status) || state.verificationResults.get(snapshot.id)?.ok === false;
    status.append(element("span", `badge ${failed ? "badge-red" : "badge-green"}`, failed ? "校验异常" : verified ? "已校验" : "已归档"));
    const actionsCell = element("td", "archive-action-column");
    const actions = element("div", "archive-actions");
    const details = actionButton("详情", "details", "text-button", null, { id: snapshot.id });
    details.setAttribute("aria-label", `查看 ${snapshotName} 的详情`);
    const verify = actionButton("校验", "verify", "text-button", null, { id: snapshot.id });
    verify.setAttribute("aria-label", `校验 ${snapshotName}`);
    const reveal = actionButton("", "reveal", "icon-button", "folder", { id: snapshot.id });
    reveal.setAttribute("aria-label", `在 Finder 中显示 ${snapshotName}`);
    reveal.title = "在 Finder 中显示";
    actions.append(details, verify, reveal);
    actionsCell.append(actions);
    row.append(nameCell, size, status, actionsCell);
    body.append(row);
  });
  table.append(body);
  return table;
}

function renderJob(job) {
  for (const id of ["overview-job", "archives-job", "reader-job"]) {
    const panel = $(id);
    panel.replaceChildren();
    panel.hidden = !job || !["running", "error"].includes(job.status);
    if (panel.hidden) continue;
    panel.classList.toggle("job-error", job.status === "error");
    const top = element("div", "job-top");
    if (job.status === "error") {
      top.append(element("strong", "", "上次操作未完成"));
      panel.append(top, element("p", "job-detail", localizeError(job.error, job.code)));
      continue;
    }
    const progress = job.progress || {};
    const total = Number(progress.total_bytes) || Number(progress.total_files) || 0;
    const done = Number(progress.total_bytes) ? Number(progress.done_bytes) || 0 : Number(progress.done_files) || 0;
    const percent = total ? Math.min(100, Math.max(0, Math.round(done / total * 100))) : 0;
    const jobLabels = { verify: "正在校验归档完整性…", wechat_import: "正在整理聊天和媒体…", reader_export: "正在保存聊天与媒体归档…", reader_open: "正在打开压缩归档…" };
    top.append(element("strong", "", jobLabels[job.type] || "正在把对话好好收藏…"), element("span", "", total ? `${percent}%` : progress.media ? `已处理 ${count(done)} 条` : "准备中"));
    const track = element("div", "progress-track");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", job.type === "verify" ? "校验进度" : ["wechat_import", "reader_export", "reader_open"].includes(job.type) ? "读取与归档进度" : "备份进度");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    if (total) track.setAttribute("aria-valuenow", String(percent));
    const fill = element("div", "progress-fill");
    fill.style.width = `${total ? percent : 5}%`;
    track.append(fill);
    const currentFile = ["wechat_import", "reader_export", "reader_open"].includes(job.type) ? readerProgressLabel(progress.current_file) : progress.current_file;
    const detail = progress.media ? currentFile || "正在整理媒体文件…" : job.type === "verify" && !progress.total_files ? "正在读取归档并计算每个文件的 SHA-256…" : !progress.total_files ? currentFile || "正在检查数据目录…" : `${count(progress.done_files)} / ${count(progress.total_files)} 个文件${currentFile ? ` · ${currentFile}` : " · 正在检查数据目录"}`;
    panel.append(top, track, element("p", "job-detail", detail));
    if (progress.snapshot_ready) {
      panel.append(element("p", "job-detail", "聊天快照已完成，可以继续使用微信；媒体整理期间请暂勿清理微信文件。"));
    }
  }
}

function updateJobButtons() {
  const running = state.status?.job?.status === "running";
  document.querySelectorAll('[data-action="backup"], [data-action="verify"], [data-action="read-wechat"], [data-action="reader-export"], [data-action="reader-open"], [data-action="reader-live"]').forEach((button) => {
    button.disabled = running || Boolean(button.dataset.loading);
    if (running) button.title = "请等待当前任务完成";
    else if (button.title === "请等待当前任务完成") button.removeAttribute("title");
  });
}

function renderSources(sources) {
  sources = (sources || []).filter((source) => source.exists !== false && source.readable !== false);
  const signature = JSON.stringify(sources || []);
  if (state.sourceSignature === signature) return;
  state.sourceSignature = signature;
  const selector = $("detected-source");
  const defaultOption = element("option", "", sources?.length ? "选择检测到的目录，或在下方手动填写" : "暂未检测到微信目录，请在下方选择文件夹");
  defaultOption.value = "";
  selector.replaceChildren(defaultOption);
  (sources || []).forEach((source) => {
    const labels = { "WeChat 4.x data": "微信 4.x 数据", "WeChat legacy data": "微信旧版数据", "WeChat container documents": "微信文档目录", "WeChat shared data": "微信共享数据" };
    const option = element("option", "", `${labels[source.label] || source.label || "微信数据"} — ${source.path}`);
    option.value = source.path;
    selector.append(option);
  });
  selector.value = $("source-path").value;
}

function fillSettings(config) {
  $("source-path").value = config.source || "";
  $("destination-path").value = config.destination || "";
  $("schedule-enabled").checked = Boolean(config.schedule_enabled);
  $("schedule-time").value = config.schedule_time || "22:00";
  $("schedule-interval").value = config.interval || "daily";
  $("schedule-time").disabled = $("schedule-interval").value === "hourly";
  $("detected-source").value = config.source || "";
  state.settingsInitialized = true;
  state.settingsDirty = false;
}

function renderStatus(status) {
  const config = status.config || {};
  const snapshots = [...(status.snapshots || [])].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const totalBytes = snapshots.reduce((sum, snapshot) => sum + (Number(snapshot.archive_bytes) || 0), 0);
  $("stat-snapshots").textContent = count(snapshots.length);
  $("stat-messages").textContent = count(status.library?.messages);
  $("wechat-key-status").textContent = status.wechat_reader?.configured
    ? "本机读取已准备好。退出微信后，可读取或更新聊天与媒体；完成备份时也会更新阅读库。本机缺失或暂不支持的媒体会保留文字提示。"
    : "需要先完成一次本地密钥初始化，或导入已有密钥文件。密钥只保存在本机；读取前请正常退出微信。";
  const archived = Boolean(status.reader_source?.archived);
  $("reader-source-chip").textContent = archived ? "归档阅读" : "本机记录";
  $("reader-source-title").textContent = archived ? `正在阅读归档：${status.reader_source.name || "可读归档"}` : "本机记录";
  $("reader-source-description").textContent = archived ? "对话与媒体从这份归档中读取。返回本机记录后可查看最近更新。" : "聊天和可用媒体保存在本机，备份时一并保存可读归档。";
  const readyMedia = (status.media?.items || []).filter(item => item.status === "ready" && ["image", "audio", "video"].includes(item.kind));
  if (readyMedia.length) $("reader-source-description").textContent += ` 已保存 ${readyMedia.map(item => `${count(item.count)} 条${mediaLabel(item.kind)}`).join("、")}。`;
  document.querySelector('[data-action="reader-live"]').hidden = !archived;
  document.querySelector('[data-action="reader-export"]').hidden = archived;
  renderConversationCount();
  const formatted = formatBytes(totalBytes).split(" ");
  $("stat-storage").replaceChildren(document.createTextNode(formatted[0]), element("small", "", formatted[1]));
  $("nav-archive-count").textContent = count(snapshots.length);
  $("archives-total").textContent = `${count(snapshots.length)} 份`;
  $("recent-archives").replaceChildren(renderArchiveTable(snapshots.slice(0, 3), true));
  $("all-archives").replaceChildren(renderArchiveTable(snapshots));
  $("app-version").textContent = `拾聊 · ${status.version ? `v${status.version}` : "本地版"}`;
  const enabled = Boolean(config.schedule_enabled);
  const installed = Boolean(status.schedule?.installed);
  const scheduleError = status.schedule?.error;
  const badge = $("schedule-badge");
  badge.textContent = enabled ? (scheduleError || !installed ? "待检查" : "已开启") : "未开启";
  badge.className = `badge ${enabled ? (scheduleError || !installed ? "badge-orange" : "badge-green") : "badge-neutral"}`;
  $("schedule-headline").textContent = enabled ? "给回忆，安排一个时间" : "按自己的节奏，收藏";
  $("schedule-description").textContent = enabled ? (scheduleError ? "计划保存后未能启动，请到设置中检查。" : "退出微信后，让每次备份安静完成。") : "设置一个方便的时间，让备份成为习惯。";
  $("schedule-time-display").textContent = enabled ? (config.interval === "hourly" ? "每小时" : config.schedule_time || "22:00") : "— : —";
  $("schedule-interval-label").textContent = enabled ? (config.interval === "hourly" ? "自动归档" : "每天一次") : "等待设置";
  $("schedule-action-text").textContent = enabled ? "管理备份计划" : "设置备份计划";
  $("hero-backup-note").textContent = status.wechat_running ? "检测到微信正在运行，请先退出微信再备份" : !config.source ? "先选择微信数据目录，就可以开始收藏" : "备份前请退出微信，以保证文件一致性";
  let scheduleState = scheduleError ? `计划状态：${scheduleError}` : enabled ? (installed ? "macOS 定时任务已启用。" : "计划尚未安装，请重新保存设置。") : "自动备份未开启。";
  if (status.schedule?.next_run) scheduleState += ` 下次执行：${formatDate(status.schedule.next_run)}。`;
  if (status.last_scheduled_result?.error) scheduleState += ` 最近执行：${localizeError(status.last_scheduled_result.error, status.last_scheduled_result.code)}`;
  if (status.last_scheduled_result?.reader_error) scheduleState += ` 备份已保存，阅读库更新未完成：${localizeError(status.last_scheduled_result.reader_error)}`;
  if (status.last_scheduled_result?.status === "deferred") scheduleState += " 最近一次因微信正在运行而暂缓，约 15 分钟后重试。";
  $("schedule-state").textContent = scheduleState;
  if (!state.settingsInitialized) fillSettings(config);
  renderSources(status.sources);
  renderJob(status.job);
  updateJobButtons();
}

function observeJob(job) {
  if (job?.status === "running") {
    if (state.lastRunningJob?.id === job.id && state.lastRunningJob.snapshotId) job.snapshotId = state.lastRunningJob.snapshotId;
    state.lastRunningJob = job;
    return;
  }
  if (!job || state.lastRunningJob?.id !== job.id || state.completedJobs.has(job.id)) return;
  state.completedJobs.add(job.id);
  if (job.status === "error") {
    if (job.type === "verify") {
      const id = job.result?.snapshot_id || state.lastRunningJob.snapshotId;
      if (id) {
        state.verifiedSnapshots.delete(id);
        state.verificationResults.set(id, { ...(job.result || {}), ok: false });
      }
    }
    toast(localizeError(job.error, job.code), true);
  } else if (job.status === "done") {
    if (job.type === "verify") {
      const result = job.result || {};
      const valid = result.ok !== false && result.valid !== false && !result.error && !(result.errors?.length);
      const snapshotId = result.snapshot_id || state.lastRunningJob.snapshotId;
      if (snapshotId) state.verificationResults.set(snapshotId, { ...result, ok: valid });
      if (valid && (result.snapshot_id || state.lastRunningJob.snapshotId)) state.verifiedSnapshots.add(result.snapshot_id || state.lastRunningJob.snapshotId);
      toast(valid ? "归档校验完成，文件完整。" : "归档校验发现异常，请查看归档详情。", !valid);
    } else if (job.type === "wechat_import") {
      toast(`微信记录已导入，新增 ${count(job.result?.messages)} 条消息。`);
      refreshReaderContents();
    } else if (job.type === "reader_export") {
      toast("可读归档已保存到备份目录，聊天和可用媒体都已收藏。");
    } else if (job.type === "reader_open") {
      toast("压缩归档已打开，可以翻阅对话和媒体。");
      resetReaderSource();
    } else {
      toast("备份完成，这一段日常已经好好收藏。");
      if (job.result?.reader_error) toast(`原始备份已保存，阅读库更新未完成：${job.result.reader_error}`, true);
      else if (!state.status?.reader_source?.archived) refreshReaderContents();
    }
  }
}

async function refreshStatus({ silent = false } = {}) {
  if (state.refreshing) { state.refreshAgain = true; return; }
  state.refreshing = true;
  try {
    const status = await api("/api/status");
    observeJob(status.job);
    if (state.lastRunningJob?.snapshotId && status.job?.id === state.lastRunningJob.id && status.job.status === "running") {
      status.job.snapshotId = state.lastRunningJob.snapshotId;
    }
    state.status = status;
    renderStatus(status);
    $("connection-banner").hidden = true;
  } catch (error) {
    $("connection-banner").hidden = false;
    $("connection-banner").textContent = error.message;
    if (!silent && state.status) toast(error.message, true);
    if (!state.status) {
      $("recent-archives").replaceChildren(emptyState("archive", "等待连接本地服务", "从「拾聊」启动器打开后，即可查看本机的备份档案。"));
      $("all-archives").replaceChildren(emptyState("archive", "等待连接本地服务", "本机服务连接成功后，归档会显示在这里。"));
    }
  } finally {
    state.refreshing = false;
    window.clearTimeout(state.polling);
    const delay = state.refreshAgain ? 0 : state.status?.job?.status === "running" ? 3000 : 15000;
    state.refreshAgain = false;
    if (state.token) state.polling = window.setTimeout(() => refreshStatus({ silent: true }), delay);
  }
}

async function startBackup(button) {
  await withBusy(button, async () => {
    if (!state.status?.config?.source) {
      showPage("settings");
      $("source-path").focus();
      toast("请先选择微信数据目录并保存设置。");
      return;
    }
    const result = await api("/api/backup", { method: "POST", body: {} });
    state.lastRunningJob = { id: result.job_id, type: "backup", status: "running" };
    toast("已开始备份，可在这里查看进度。");
    await refreshStatus({ silent: true });
  }, "正在准备…");
}

async function verifySnapshot(id, button) {
  await withBusy(button, async () => {
    state.verifiedSnapshots.delete(id);
    state.verificationResults.delete(id);
    const result = await api(`/api/snapshots/${encodeURIComponent(id)}/verify`, { method: "POST", body: {} });
    state.lastRunningJob = { id: result.job_id, type: "verify", status: "running", snapshotId: id };
    toast("正在逐个核对归档中的文件，请稍候。");
    await refreshStatus({ silent: true });
  }, "校验中…");
}

async function revealSnapshot(id, button) {
  await withBusy(button, async () => {
    await api(`/api/snapshots/${encodeURIComponent(id)}/reveal`, { method: "POST", body: {} });
    toast("已在 Finder 中显示备份文件。");
  }, "打开中…");
}

async function showManifest(id, button) {
  await withBusy(button, async () => {
    const response = await api(`/api/snapshots/${encodeURIComponent(id)}`);
    const manifest = response.manifest || response;
    const snapshot = state.status?.snapshots?.find((item) => item.id === id) || {};
    const content = $("manifest-content");
    content.replaceChildren();
    const metadata = element("dl", "manifest-meta");
    const verified = state.verifiedSnapshots.has(id) || manifest.verified_at || snapshot.verified_at || snapshot.status === "verified";
    const verification = state.verificationResults.get(id) || manifest.verification || response.verification;
    const verificationText = verification?.ok === false || verification?.valid === false ? "校验异常" : verified ? "SHA-256 校验通过" : "可运行 SHA-256 完整性校验";
    const files = Array.isArray(manifest.files) ? manifest.files : [];
    [["归档格式", "ZIP · 本机原始文件备份"], ["创建时间", formatDate(manifest.created_at || snapshot.created_at)], ["文件数量", `${count(manifest.file_count ?? snapshot.file_count ?? files.length)} 个`], ["完整性", verificationText], ["原始大小", formatBytes(manifest.original_bytes ?? snapshot.original_bytes)], ["归档大小", formatBytes(manifest.archive_bytes ?? snapshot.archive_bytes)]].forEach(([label, value]) => {
      const item = element("div");
      item.append(element("dt", "", label), element("dd", "", value));
      metadata.append(item);
    });
    content.append(metadata);
    if (verification?.errors?.length) content.append(element("p", "info-note", `校验发现异常：${verification.errors.join("；")}`));
    if (manifest.skipped_symlinks?.length) content.append(element("p", "info-note", `已跳过 ${count(manifest.skipped_symlinks.length)} 个符号链接；链接指向的外部文件不在本归档中。`));
    if (files.length) {
      const list = element("div", "manifest-files", `文件清单${files.length > 40 ? "（显示前 40 项）" : ""}`);
      files.slice(0, 40).forEach((file) => {
        const line = element("div", "manifest-file", file.path || file.name || file.relative_path || "文件");
        line.append(element("small", "", `SHA-256: ${file.sha256 || file.hash || "未提供"}`));
        list.append(line);
      });
      content.append(list);
    } else {
      content.append(element("pre", "manifest-raw", JSON.stringify(manifest, null, 2)));
    }
    $("manifest-dialog").showModal();
  }, "读取中…");
}

async function pickFolder(purpose, button) {
  await withBusy(button, async () => {
    const result = await api("/api/pick-folder", { method: "POST", body: { purpose } });
    if (result.path) {
      $(purpose === "source" ? "source-path" : "destination-path").value = result.path;
      state.settingsDirty = true;
      $("settings-save-status").textContent = "目录已选择，保存后生效";
    }
  }, "请选择文件夹…");
}

async function saveSettings(event) {
  event.preventDefault();
  await withBusy($("save-settings"), async () => {
    const config = {
      source: $("source-path").value.trim(),
      destination: $("destination-path").value.trim(),
      schedule_enabled: $("schedule-enabled").checked,
      schedule_time: $("schedule-time").value || "22:00",
      interval: $("schedule-interval").value,
    };
    await api("/api/config", { method: "POST", body: config });
    state.settingsDirty = false;
    $("settings-save-status").textContent = "设置已保存";
    toast(config.schedule_enabled ? "设置已保存，自动备份计划已更新。" : "设置已保存。");
    await refreshStatus({ silent: true });
  }, "保存中…");
}

async function loadConversations() {
  if (hasFilters(searchFilters("global"))) return loadGlobalSearch();
  const request = ++state.conversationRequest;
  state.globalSearch = null;
  updateGlobalSearchChrome();
  if (!state.conversationLoaded) $("conversation-list").replaceChildren(element("div", "loading-placeholder", "正在读取会话…"));
  try {
    const response = await api("/api/conversations");
    if (request !== state.conversationRequest) return;
    state.conversations = response.conversations || [];
    state.conversationLoaded = true;
    renderConversations();
    if (!state.conversationId) renderReaderEmpty();
  } catch (error) {
    if (request !== state.conversationRequest) return;
    $("conversation-list").replaceChildren(emptyState("chat", "会话暂时无法读取", error.message, actionButton("重新加载", "load-conversations")));
    if (!state.conversationId) renderReaderEmpty();
  }
}

function updateGlobalSearchChrome() {
  const active = hasFilters(searchFilters("global"));
  $("conversation-pane-title").textContent = active ? "搜索结果" : "会话";
  document.querySelector('[data-action="clear-global-search"]').hidden = !active;
  $("global-search-status").hidden = !active;
  updateDateFilter("global");
  if (!active) {
    $("global-search-status").textContent = "";
    delete $("global-search-status").dataset.query;
    delete $("global-search-status").dataset.filters;
  }
}

async function loadGlobalSearch(append = false, button = null) {
  const filters = searchFilters("global");
  const { query } = filters;
  const key = filterKey(filters);
  if (!hasFilters(filters)) return loadConversations();
  const request = ++state.conversationRequest;
  const previous = state.globalSearch?.key === key ? state.globalSearch : null;
  const offset = append && previous ? previous.messages.length : 0;
  updateGlobalSearchChrome();
  if (!append) {
    $("global-search-status").textContent = "正在搜索本机聊天…";
    $("conversation-list").replaceChildren(element("div", "loading-placeholder", "正在查找用户、会话和聊天内容…"));
  }
  const task = async () => {
    validateSearchFilters(filters);
    const response = await api(`/api/search?${searchParameters(filters, offset, 50)}`);
    if (request !== state.conversationRequest || key !== filterKey(searchFilters("global"))) return;
    state.globalSearch = {
      ...filters, key,
      conversations: response.conversations || [],
      conversationTotal: Number(response.conversation_total) || (response.conversations || []).length,
      conversationsHasMore: Boolean(response.conversations_has_more),
      messages: append && previous ? [...previous.messages, ...(response.messages || [])] : response.messages || [],
      total: Number(response.total) || 0,
    };
    renderGlobalSearch(append);
  };
  const failed = (error) => {
    if (request !== state.conversationRequest) return;
    $("global-search-status").textContent = "搜索暂时无法完成";
    $("conversation-list").replaceChildren(emptyState("search", "搜索暂时无法完成", error.message, actionButton("重新搜索", "load-conversations")));
  };
  if (button) await withBusy(button, async () => { try { await task(); } catch (error) { failed(error); } }, "搜索中…");
  else { try { await task(); } catch (error) { failed(error); } }
}

function conversationButton(conversation, query = "", inSearch = false) {
  const button = element("button", `conversation-button${state.conversationId === String(conversation.id) ? " selected" : ""}`);
  button.classList.toggle("is-search-result", inSearch);
  button.type = "button";
  button.dataset.action = "select-conversation";
  button.dataset.id = conversation.id;
  button.setAttribute("aria-pressed", String(state.conversationId === String(conversation.id)));
  const title = conversation.title || "未命名会话";
  const avatar = createAvatar("span", "conversation-avatar", title, conversation.avatar_url);
  const summary = element("span", "conversation-summary");
  const heading = element("span", "conversation-line");
  const timestamp = element("time", "", inSearch ? beijingTime(conversation.last_message_at) : conversationTime(conversation.last_message_at));
  const date = parseTime(conversation.last_message_at);
  if (date) timestamp.dateTime = date.toISOString();
  timestamp.title = `${beijingTime(conversation.last_message_at)} · 北京时间`;
  heading.append(highlighted("strong", "", title, query), timestamp);
  const senderMatch = query && !title.toLocaleLowerCase().includes(query.toLocaleLowerCase());
  summary.append(heading, senderMatch
    ? element("p", "search-match-reason", "会话内发送者匹配 · 查看此会话")
    : highlighted("p", "", conversation.preview || "打开查看聊天记录", query));
  button.append(avatar, summary);
  return button;
}

function renderConversations() {
  if (state.globalSearch?.key === filterKey(searchFilters("global"))) return renderGlobalSearch(true);
  const list = $("conversation-list");
  list.replaceChildren();
  if (!state.conversations.length) list.append(emptyState("chat", "对话，等待被拾起", "导入聊天记录后，会话会显示在这里。"));
  state.conversations.forEach(conversation => list.append(conversationButton(conversation)));
  updateGlobalSearchChrome();
  renderConversationCount();
}

function renderGlobalSearch(preserveScroll = false) {
  const search = state.globalSearch;
  if (!search) return;
  const list = $("conversation-list");
  const scroll = list.scrollTop;
  list.replaceChildren();
  $("global-search-status").textContent = `${count(search.conversationTotal)} 个用户 / 会话，${count(search.total)} 条聊天记录`;
  $("global-search-status").dataset.query = search.query;
  $("global-search-status").dataset.filters = search.key;
  if (!search.conversations.length && !search.total) {
    list.append(emptyState("search", "没有找到匹配结果", "换一个关键词或日期范围，或清空条件后重试。", actionButton("清空搜索", "clear-global-search")));
  }
  if (search.conversations.length) {
    list.append(element("h3", "search-group-heading", `用户 / 会话 · ${count(search.conversationTotal)}`));
    search.conversations.forEach(conversation => list.append(conversationButton(conversation, search.query, true)));
    if (search.conversationsHasMore) list.append(element("p", "search-limit-note", `已显示前 ${count(search.conversations.length)} 个，请补充关键词缩小范围。`));
  }
  if (search.total) {
    list.append(element("h3", "search-group-heading", `聊天记录 · ${count(search.total)}`));
    search.messages.forEach(message => {
      const button = element("button", "search-message-result");
      button.type = "button";
      button.dataset.action = "open-search-message";
      button.dataset.id = String(message.id);
      button.dataset.conversationId = String(message.conversation_id);
      const heading = element("span", "search-result-heading");
      const timestamp = element("time", "", beijingTime(message.timestamp));
      const date = parseTime(message.timestamp);
      if (date) timestamp.dateTime = date.toISOString();
      timestamp.title = `${beijingTime(message.timestamp)} · 北京时间`;
      heading.append(highlighted("strong", "", message.conversation_title || "未命名会话", search.query), timestamp);
      const senderName = message.sender || (message.is_self ? "我" : "对方");
      const avatar = createAvatar("span", "conversation-avatar search-result-avatar", senderName, message.avatar_url);
      const content = element("span", "search-result-content");
      const sender = highlighted("span", "search-result-sender", senderName, search.query);
      const preview = highlighted("span", "search-result-preview", searchSnippet(message.text, search.query), search.query);
      content.append(heading, sender, preview);
      button.append(avatar, content);
      if (state.messageContext?.targetId === String(message.id) && state.conversationId === String(message.conversation_id)) {
        button.classList.add("selected");
        button.setAttribute("aria-current", "true");
      }
      list.append(button);
    });
    if (search.messages.length < search.total) list.append(actionButton(`更多聊天记录（${count(search.messages.length)} / ${count(search.total)}）`, "search-more", "button button-outline search-more"));
  }
  list.scrollTop = preserveScroll ? scroll : 0;
  renderConversationCount();
}

async function clearGlobalSearch() {
  state.pendingGlobalSearch?.cancel();
  $("conversation-search").value = "";
  setDateRange("global");
  state.globalSearch = null;
  state.conversationRequest += 1;
  updateGlobalSearchChrome();
  $("conversation-search").focus();
  await loadConversations();
}

async function resetReaderSource() {
  state.pendingMessageSearch?.cancel();
  state.pendingGlobalSearch?.cancel();
  state.conversationId = null;
  state.conversationLoaded = false;
  state.messages = [];
  state.messagesTotal = 0;
  state.messagesOffset = 0;
  state.messageContext = null;
  state.globalSearch = null;
  state.messagesRequest += 1;
  $("conversation-search").value = "";
  $("message-search").value = "";
  setDateRange("global");
  setDateRange("message");
  $("global-date-filter").open = false;
  $("message-date-filter").open = false;
  $("reader-dropzone").classList.remove("has-selection");
  $("reader-management").open = false;
  if ($("media-preview-dialog").open) $("media-preview-dialog").close();
  await loadConversations();
}

async function refreshReaderContents() {
  state.conversationLoaded = false;
  await loadConversations();
  if (state.conversationId) await loadMessages(false);
}

async function startReaderArchive(action, button) {
  await withBusy(button, async () => {
    if (action === "live") {
      await api("/api/reader/live", { method: "POST", body: {} });
      await refreshStatus({ silent: true });
      await resetReaderSource();
      toast("已返回本机聊天记录。");
      return;
    }
    let path;
    if (action === "open") {
      const selection = await api("/api/pick-archive", { method: "POST", body: {} });
      if (!selection.path) return;
      path = selection.path;
    }
    const result = await api(action === "open" ? "/api/reader/archive" : "/api/reader/export", { method: "POST", body: action === "open" ? { path } : {} });
    state.lastRunningJob = { id: result.job_id, type: action === "open" ? "reader_open" : "reader_export", status: "running" };
    await refreshStatus({ silent: true });
  }, action === "open" ? "选择归档…" : action === "live" ? "切换中…" : "正在保存…");
}

function renderReaderEmpty() {
  $("messages-header").hidden = true;
  $("message-context-bar").hidden = true;
  const hasConversations = Boolean(state.conversations.length);
  $("message-list").replaceChildren(emptyState("chat", hasConversations ? "选一段对话，慢慢重温" : "聊天记录，妥善收藏", hasConversations ? "从左侧选择会话，查看聊天记录与媒体。这里是只读归档，不能发送消息。" : "打开一份可读压缩归档，或在右上角「管理」中读取本机聊天、导入聊天记录。", hasConversations ? null : actionButton("管理聊天记录", "open-reader-management", "button button-outline", "folder")));
}

function prepareConversation(id) {
  state.pendingMessageSearch?.cancel();
  state.conversationId = String(id);
  state.messages = [];
  state.messagesTotal = 0;
  state.messagesOffset = 0;
  state.messageContext = null;
  $("message-context-bar").hidden = true;
  $("message-search").value = "";
  setDateRange("message");
  $("message-date-filter").open = false;
  $("reader-dropzone").classList.add("has-selection");
  $("reader-management").open = false;
  $("messages-header").classList.remove("search-open");
  document.querySelector('[data-action="toggle-message-search"]').setAttribute("aria-expanded", "false");
  const selected = [...state.conversations, ...(state.globalSearch?.conversations || [])].find(item => String(item.id) === state.conversationId);
  $("messages-header").hidden = false;
  $("conversation-title").textContent = selected?.title || "聊天记录";
  $("conversation-meta").textContent = "正在读取…";
  $("conversation-header-avatar").className = `conversation-avatar header-avatar ${avatarTone(selected?.title || "")}`;
  setAvatar($("conversation-header-avatar"), selected?.title, selected?.avatar_url);
}

async function selectConversation(id) {
  prepareConversation(id);
  setDateRange("message", searchFilters("global"));
  renderConversations();
  await loadMessages(false);
}

async function openSearchMessage(conversationId, messageId, button) {
  const query = state.globalSearch?.query || "";
  prepareConversation(conversationId);
  state.messageContext = { targetId: String(messageId), query };
  const request = ++state.messagesRequest;
  $("message-list").replaceChildren(element("div", "loading-placeholder", "正在定位消息与前后文…"));
  const task = async () => {
    const response = await api(`/api/conversations/${encodeURIComponent(conversationId)}/context?message_id=${encodeURIComponent(messageId)}&limit=100`);
    if (request !== state.messagesRequest || state.conversationId !== String(conversationId)) return;
    state.messages = response.messages || [];
    state.messagesOffset = Number(response.offset) || 0;
    state.messagesTotal = Number(response.total) || 0;
    setConversationHeading(response.conversation || {});
    renderMessages(false);
    renderConversations();
    requestAnimationFrame(() => {
      if (request !== state.messagesRequest) return;
      const target = Array.from($("message-list").querySelectorAll(".message-item")).find(row => row.dataset.messageId === String(messageId));
      target?.scrollIntoView({ block: "center" });
      target?.focus({ preventScroll: true });
    });
  };
  await withBusy(button, async () => {
    try { await task(); } catch (error) {
      if (request !== state.messagesRequest) return;
      $("message-list").replaceChildren(emptyState("search", "暂时无法定位消息", error.message,
        actionButton("重新定位", "open-search-message", "button button-outline", null, { id: String(messageId), conversationId: String(conversationId) })));
    }
  }, "定位中…");
}

function setConversationHeading(conversation) {
  $("messages-header").hidden = false;
  $("conversation-title").textContent = conversation.title || "未命名会话";
  $("conversation-header-avatar").className = `conversation-avatar header-avatar ${avatarTone(conversation.title || "")}`;
  setAvatar($("conversation-header-avatar"), conversation.title, conversation.avatar_url);
  const filtered = hasFilters(searchFilters("message"));
  $("conversation-meta").textContent = `${filtered ? "找到 " : ""}${count(state.messagesTotal)} 条消息 · 最新在前${filtered ? " · 北京时间" : ""}${conversation.is_demo ? " · 示例会话" : ""}`;
}

function returnToConversations() {
  $("reader-dropzone").classList.remove("has-selection");
  (document.querySelector(".search-message-result.selected, .conversation-button.selected") || $("conversation-search"))?.focus({ preventScroll: true });
}

function toggleMessageSearch() {
  const expanded = $("messages-header").classList.toggle("search-open");
  document.querySelector('[data-action="toggle-message-search"]').setAttribute("aria-expanded", String(expanded));
  if (expanded) $("message-search").focus();
  else if (hasFilters(searchFilters("message"))) clearMessageSearch();
}

async function loadMessages(append = false, button = null) {
  if (!state.conversationId) return;
  const filters = searchFilters("message");
  const key = filterKey(filters);
  append = append && (state.messagesFilterKey === key || Boolean(state.messageContext));
  const request = ++state.messagesRequest;
  const id = state.conversationId;
  const offset = append ? state.messagesOffset + state.messages.length : 0;
  if (!append) {
    state.messagesOffset = 0;
    state.messageContext = null;
    $("message-context-bar").hidden = true;
    $("message-list").replaceChildren(element("div", "loading-placeholder", "正在翻开这段对话…"));
  }
  const task = async () => {
    validateSearchFilters(filters);
    const response = await api(`/api/conversations/${encodeURIComponent(id)}/messages?${searchParameters(filters, offset, 100)}`);
    if (request !== state.messagesRequest || id !== state.conversationId || key !== filterKey(searchFilters("message"))) return;
    const incoming = response.messages || [];
    state.messages = append ? [...state.messages, ...incoming] : incoming;
    state.messagesTotal = Number(response.total) || 0;
    state.messagesFilterKey = key;
    const conversation = response.conversation || state.conversations.find((item) => String(item.id) === id) || {};
    setConversationHeading(conversation);
    renderMessages(append);
  };
  if (button) await withBusy(button, task, "加载中…");
  else {
    try { await task(); } catch (error) {
      if (request !== state.messagesRequest) return;
      $("message-list").replaceChildren(emptyState("info", "消息暂时无法读取", error.message, actionButton("重新加载", "load-messages")));
    }
  }
}

async function loadNewerMessages(button) {
  if (!state.messageContext || !state.messagesOffset) return;
  const id = state.conversationId;
  const request = ++state.messagesRequest;
  const offset = Math.max(0, state.messagesOffset - 100);
  const limit = state.messagesOffset - offset;
  await withBusy(button, async () => {
    const response = await api(`/api/conversations/${encodeURIComponent(id)}/messages?offset=${offset}&limit=${limit}`);
    if (request !== state.messagesRequest || id !== state.conversationId) return;
    const scroller = $("messages-scroll");
    const previousHeight = scroller.scrollHeight;
    const previousTop = scroller.scrollTop;
    state.messages = [...(response.messages || []), ...state.messages];
    state.messagesOffset = offset;
    renderMessages(true);
    scroller.scrollTop = previousTop + scroller.scrollHeight - previousHeight;
  }, "加载中…");
}

function renderMessages(append) {
  const scrollTop = $("messages-scroll").scrollTop;
  const list = $("message-list");
  list.dataset.filters = filterKey(searchFilters("message"));
  const highlightQuery = $("message-search").value.trim() || state.messageContext?.query || "";
  list.replaceChildren();
  $("message-context-bar").hidden = !state.messageContext;
  if (state.messageContext) {
    $("message-context-caption").textContent = `搜索结果的前后文 · 不受日期限制 · 最新在前 · 第 ${count(state.messagesOffset + 1)}–${count(state.messagesOffset + state.messages.length)} 条`;
    if (state.messagesOffset > 0) list.append(actionButton("加载更新的消息", "load-earlier", "button button-outline message-more"));
  }
  if (!state.messages.length) {
    const filtered = hasFilters(searchFilters("message"));
    list.append(emptyState("search", filtered ? "没有找到相关消息" : "这段会话还没有消息", filtered ? "换一个关键词或日期范围，或者清空条件查看全部消息。" : "可以继续导入聊天记录文件。"));
  }
  let previousTimestamp = null;
  state.messages.forEach((message) => {
    const timestamp = parseTime(message.timestamp);
    if (timestamp && (!previousTimestamp || beijingTime(timestamp).slice(0, 10) !== beijingTime(previousTimestamp).slice(0, 10) || Math.abs(timestamp - previousTimestamp) >= 5 * 60 * 1000)) {
      const divider = element("time", "message-time-divider", formatDate(message.timestamp, beijingTime(timestamp).slice(0, 4) === beijingTime(new Date()).slice(0, 4)));
      divider.dateTime = timestamp.toISOString();
      list.append(divider);
    }
    previousTimestamp = timestamp;
    const sender = message.sender || (message.is_self ? "我" : "对方");
    const row = element("div", `message-item${message.is_self ? " self" : ""}`);
    row.dataset.messageId = String(message.id);
    if (state.messageContext?.targetId === String(message.id)) {
      row.classList.add("is-target");
      row.tabIndex = -1;
      row.setAttribute("aria-label", "定位到的消息");
    }
    const avatar = createAvatar("div", "message-avatar", sender, message.avatar_url);
    const content = element("div", "message-content");
    const meta = element("div", "message-meta");
    const timeLabel = hasFilters(searchFilters("message")) || state.messageContext ? beijingTime(message.timestamp)
      : timestamp ? new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(timestamp) : "时间未知";
    meta.append(highlighted("span", "", sender, highlightQuery), element("time", "", timeLabel));
    meta.lastChild.title = `${beijingTime(message.timestamp)} · 北京时间`;
    if (timestamp) meta.lastChild.dateTime = timestamp.toISOString();
    const bubble = element("div", "message-bubble");
    bubble.append(highlighted("div", "message-text", message.text || "[非文本消息]", highlightQuery));
    content.append(meta, bubble);
    if (Array.isArray(message.media) && message.media.length) {
      row.classList.add("has-media");
      const mediaList = element("div", "message-media-list");
      message.media.filter((media) => media && typeof media === "object").forEach((media) => mediaList.append(renderMedia(media)));
      bubble.append(mediaList);
    }
    row.append(avatar, content);
    list.append(row);
  });
  if (state.messagesOffset + state.messages.length < state.messagesTotal) {
    list.append(actionButton(state.messageContext ? "加载更早的消息" : `加载更早的消息（${count(state.messages.length)} / ${count(state.messagesTotal)}）`, "load-more", "button button-outline message-more"));
  }
  $("messages-scroll").scrollTop = append ? scrollTop : 0;
}

function readerProgressLabel(value) {
  const label = String(value || "").trim();
  if (!label) return "正在准备聊天记录";
  if (/[\u3400-\u9fff]/.test(label) && !/sql|cipher|pragma|key|密钥|数据库|\/Users\//i.test(label)) return label;
  if (/media|image|audio|video|attachment/i.test(label)) return "正在整理图片、语音和视频";
  if (/contact/i.test(label)) return "正在整理会话信息";
  return "正在整理聊天记录";
}

function mediaUrl(value) {
  if (typeof value !== "string" || !value.startsWith("/api/media/")) return null;
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin || url.search || url.hash || !/^\/api\/media\/[A-Za-z0-9_-]+$/.test(url.pathname)) return null;
    return url.pathname;
  } catch (_) { return null; }
}

function mediaLabel(kind) {
  return { image: "图片", audio: "语音", video: "视频" }[kind] || "附件";
}

function mediaFallback(media, status = media.status) {
  const kind = mediaLabel(media.kind);
  const descriptions = {
    missing: `这条${kind}的原文件不在本机，暂时无法查看。`,
    locked: `这条${kind}暂时无法打开，可在更新聊天后再试。`,
    unsupported: `暂不支持这条${kind}的格式，已保留原消息。`,
    error: `这条${kind}未能加载，可稍后重新读取聊天。`,
  };
  const fallback = element("div", "media-fallback");
  fallback.setAttribute("role", "status");
  fallback.append(icon(["image", "audio", "video"].includes(media.kind) ? media.kind : "file"));
  const description = element("div", "");
  description.append(element("strong", "", `${kind}暂不可用`), element("p", "", descriptions[status] || descriptions.unsupported));
  fallback.append(description);
  return fallback;
}

function renderMedia(media) {
  const card = element("div", `media-card media-${["image", "audio", "video"].includes(media.kind) ? media.kind : "attachment"}`);
  card.dataset.mediaStatus = media.status || "unsupported";
  const url = media.status === "ready" ? mediaUrl(media.url) : null;
  const supported = ["image", "audio", "video"].includes(media.kind);
  if (!url || !supported) {
    card.append(mediaFallback(media, media.status === "ready" ? "error" : media.status));
    return card;
  }
  const fail = () => {
    card.dataset.mediaStatus = "error";
    card.replaceChildren(mediaFallback(media, "error"));
  };
  if (media.kind === "image") {
    const button = element("button", "media-image-button");
    button.type = "button";
    button.setAttribute("aria-label", "打开图片预览");
    const image = element("img", "message-image");
    image.alt = "聊天图片";
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("error", fail, { once: true });
    image.src = url;
    button.append(image, element("span", "media-image-caption", media.detail ? "点击查看图片" : "点击查看原图"));
    button.addEventListener("click", () => openImagePreview(media, url));
    card.append(button);
  } else {
    const header = element("div", "media-heading");
    header.append(icon(media.kind), element("span", "", mediaLabel(media.kind)));
    if (Number(media.byte_size) > 0) header.append(element("small", "", formatBytes(media.byte_size)));
    const player = element(media.kind, "media-player");
    player.controls = true;
    player.preload = "metadata";
    player.setAttribute("aria-label", `播放聊天${mediaLabel(media.kind)}`);
    if (media.kind === "video") player.playsInline = true;
    player.addEventListener("error", fail, { once: true });
    player.src = url;
    card.append(header, player);
  }
  if (media.detail) card.append(element("p", "media-note", media.detail));
  return card;
}

function openImagePreview(media, url) {
  const dialog = $("media-preview-dialog");
  const viewport = $("media-preview-viewport");
  const image = element("img", "media-preview-image");
  image.alt = media.detail || "聊天图片原图";
  $("media-preview-title").textContent = media.detail || "图片预览";
  $("media-preview-meta").textContent = Number(media.byte_size) > 0 ? formatBytes(media.byte_size) : "保存在本机的图片";
  viewport.classList.remove("is-original");
  $("media-preview-size").textContent = "原始尺寸";
  $("media-preview-size").setAttribute("aria-pressed", "false");
  $("media-preview-size").disabled = false;
  image.addEventListener("load", () => {
    $("media-preview-meta").textContent = `${image.naturalWidth} × ${image.naturalHeight}${Number(media.byte_size) > 0 ? ` · ${formatBytes(media.byte_size)}` : ""}`;
  }, { once: true });
  image.addEventListener("error", () => {
    viewport.replaceChildren(mediaFallback(media, "error"));
    $("media-preview-size").disabled = true;
  }, { once: true });
  image.src = url;
  viewport.replaceChildren(image);
  dialog.showModal();
}

async function importFile(file) {
  if (!file || state.importing) return;
  if (!/\.(json|jsonl|csv|txt)$/i.test(file.name)) {
    toast("请选择 JSON、JSONL、CSV 或 TXT 格式的聊天记录。", true);
    return;
  }
  if (file.size > 100 * 1024 * 1024) {
    toast("文件超过 100 MB，请拆分后再导入。", true);
    return;
  }
  state.importing = true;
  const buttons = Array.from(document.querySelectorAll('[data-action="import"]'));
  const originalButtonNodes = new Map(buttons.map((button) => [button, Array.from(button.childNodes)]));
  buttons.forEach((button) => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.replaceChildren(element("span", "", "导入中…"));
  });
  toast(`正在导入「${file.name}」…`);
  try {
    const result = await api("/api/import", { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) }, body: file });
    toast(result.duplicate ? "这个文件已经导入过，无需重复导入。" : `导入完成：${count(result.conversations)} 个会话，${count(result.messages)} 条消息。`);
    $("conversation-search").value = "";
    state.pendingGlobalSearch?.cancel();
    setDateRange("global");
    state.conversationLoaded = false;
    showPage("reader");
    await loadConversations();
    if (state.conversations.length) await selectConversation(state.conversations[0].id);
    await refreshStatus({ silent: true });
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.importing = false;
    originalButtonNodes.forEach((nodes, button) => button.replaceChildren(...nodes));
    document.querySelectorAll('[data-action="import"]').forEach((button) => { button.disabled = false; button.removeAttribute("aria-busy"); });
    $("import-file").value = "";
  }
}

async function createDemo(button) {
  await withBusy(button, async () => {
    const result = await api("/api/demo", { method: "POST", body: {} });
    toast("示例会话已准备好，可以体验阅读与搜索。");
    $("conversation-search").value = "";
    state.pendingGlobalSearch?.cancel();
    setDateRange("global");
    await loadConversations();
    const conversation = state.conversations.find((item) => item.is_demo || String(item.title).includes("示例")) || state.conversations.find((item) => item.id === result.conversation_id) || state.conversations[0];
    if (conversation) await selectConversation(conversation.id);
    await refreshStatus({ silent: true });
  }, "准备示例…");
}

function debounce(callback, delay = 250) {
  let timer;
  const debounced = (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => callback(...args), delay);
  };
  debounced.cancel = () => window.clearTimeout(timer);
  return debounced;
}

function bindEvents() {
  document.addEventListener("click", (event) => {
    const pageButton = event.target.closest("[data-page]");
    if (pageButton) { showPage(pageButton.dataset.page); return; }
    const button = event.target.closest("[data-action]");
    if (!button || button.disabled) return;
    const id = button.dataset.id;
    const actions = {
      backup: () => startBackup(button),
      verify: () => verifySnapshot(id, button),
      details: () => showManifest(id, button),
      reveal: () => revealSnapshot(id, button),
      refresh: () => withBusy(button, () => refreshStatus(), "刷新中…"),
      "pick-source": () => pickFolder("source", button),
      "pick-destination": () => pickFolder("destination", button),
      import: () => $("import-file").click(),
      "import-keys": () => $("wechat-key-file").click(),
      "read-wechat": () => withBusy(button, async () => {
        const result = await api("/api/wechat/import", { method: "POST", body: {} });
        state.lastRunningJob = { id: result.job_id, type: "wechat_import", status: "running" };
        await refreshStatus({ silent: true });
      }, "开始读取…"),
      "reader-export": () => startReaderArchive("export", button),
      "reader-open": () => startReaderArchive("open", button),
      "reader-live": () => startReaderArchive("live", button),
      "reader-back": returnToConversations,
      "toggle-message-search": toggleMessageSearch,
      "open-reader-management": () => { $("reader-management").open = true; $("reader-management").querySelector("summary").focus(); },
      "close-reader-management": () => { $("reader-management").open = false; $("reader-management").querySelector("summary").focus(); },
      demo: () => createDemo(button),
      "select-conversation": () => selectConversation(id),
      "open-search-message": () => openSearchMessage(button.dataset.conversationId, id, button),
      "search-more": () => loadGlobalSearch(true, button),
      "clear-global-search": clearGlobalSearch,
      "clear-message-search": clearMessageSearch,
      "clear-dates": () => {
        const scope = button.dataset.scope;
        (scope === "global" ? state.pendingGlobalSearch : state.pendingMessageSearch)?.cancel();
        setDateRange(scope);
        if (scope === "global") loadConversations();
        else loadMessages(false);
      },
      "load-earlier": () => loadNewerMessages(button),
      "exit-message-context": clearMessageSearch,
      "load-more": () => loadMessages(true, button),
      "load-messages": () => loadMessages(false, button),
      "load-conversations": () => loadConversations(),
    };
    actions[button.dataset.action]?.();
  });
  document.querySelector(".brand").addEventListener("click", (event) => { event.preventDefault(); showPage("overview"); });
  $("settings-form").addEventListener("submit", saveSettings);
  $("settings-form").addEventListener("input", () => { state.settingsDirty = true; $("settings-save-status").textContent = "有未保存的更改"; });
  $("schedule-interval").addEventListener("change", () => { $("schedule-time").disabled = $("schedule-interval").value === "hourly"; });
  $("detected-source").addEventListener("change", () => {
    if ($("detected-source").value) $("source-path").value = $("detected-source").value;
    state.settingsDirty = true;
    $("settings-save-status").textContent = "目录已选择，保存后生效";
  });
  const searchConversations = debounce(loadConversations);
  state.pendingGlobalSearch = searchConversations;
  const globalSearchChanged = () => {
    state.conversationRequest += 1;
    updateGlobalSearchChrome();
    searchConversations.cancel();
    searchConversations();
  };
  const globalSearchKeydown = (event) => {
    if (event.key === "Escape") { event.preventDefault(); searchConversations.cancel(); clearGlobalSearch(); }
    if (event.key === "Enter") { event.preventDefault(); searchConversations.cancel(); loadConversations(); }
    if (event.key === "ArrowDown" && event.target.id === "conversation-search") {
      const first = $("conversation-list").querySelector("button:not(:disabled)");
      if (first) { event.preventDefault(); first.focus(); }
    }
  };
  ["conversation-search", "global-date-from", "global-date-to"].forEach(id => {
    $(id).addEventListener("input", globalSearchChanged);
    $(id).addEventListener("keydown", globalSearchKeydown);
  });
  $("conversation-list").addEventListener("keydown", (event) => {
    if (event.key === "Escape") { $("conversation-search").focus(); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const buttons = [...$("conversation-list").querySelectorAll("button:not(:disabled)")];
    const index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : Math.max(0, Math.min(buttons.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
    buttons[next]?.focus();
  });
  const searchMessages = debounce(() => loadMessages(false));
  state.pendingMessageSearch = searchMessages;
  const messageSearchChanged = () => {
    state.messagesRequest += 1;
    state.messageContext = null;
    updateDateFilter("message");
    $("message-context-bar").hidden = true;
    searchMessages.cancel();
    searchMessages();
  };
  const messageSearchKeydown = (event) => {
    if (event.key === "Enter") { event.preventDefault(); searchMessages.cancel(); loadMessages(false); }
    if (event.key === "Escape") {
      event.preventDefault();
      searchMessages.cancel();
      if (hasFilters(searchFilters("message"))) clearMessageSearch();
      else if ($("messages-header").classList.contains("search-open")) {
        toggleMessageSearch();
        document.querySelector('[data-action="toggle-message-search"]').focus();
      }
      $("message-date-filter").open = false;
    }
  };
  ["message-search", "message-date-from", "message-date-to"].forEach(id => {
    $(id).addEventListener("input", messageSearchChanged);
    $(id).addEventListener("keydown", messageSearchKeydown);
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest("#reader-management") && !event.target.closest('[data-action="open-reader-management"]')) $("reader-management").open = false;
    if (!event.target.closest("#message-date-filter")) $("message-date-filter").open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("reader-management").open) {
      $("reader-management").open = false;
      $("reader-management").querySelector("summary").focus();
    }
  });
  $("import-file").addEventListener("change", (event) => importFile(event.target.files[0]));
  $("wechat-key-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    await withBusy(document.querySelector('[data-action="import-keys"]'), async () => {
      const result = await api("/api/wechat/keys", { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } });
      toast(`已验证并保存在本机：${count(result.verified_databases)} 个数据库密钥。`);
      await refreshStatus({ silent: true });
    }, "验证密钥…");
    event.target.value = "";
  });
  $("close-dialog").addEventListener("click", () => $("manifest-dialog").close());
  $("manifest-dialog").addEventListener("click", (event) => {
    if (event.target !== $("manifest-dialog")) return;
    const bounds = event.target.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.target.close();
  });
  $("close-media-preview").addEventListener("click", () => $("media-preview-dialog").close());
  $("media-preview-dialog").addEventListener("close", () => $("media-preview-viewport").replaceChildren());
  $("media-preview-dialog").addEventListener("click", (event) => {
    if (event.target !== $("media-preview-dialog")) return;
    const bounds = event.target.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.target.close();
  });
  $("media-preview-size").addEventListener("click", () => {
    const original = $("media-preview-viewport").classList.toggle("is-original");
    $("media-preview-size").textContent = original ? "适应窗口" : "原始尺寸";
    $("media-preview-size").setAttribute("aria-pressed", String(original));
  });
  let dragDepth = 0;
  const dropzone = $("reader-dropzone");
  dropzone.addEventListener("dragenter", (event) => {
    if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
    event.preventDefault();
    dragDepth += 1;
    $("drop-overlay").hidden = false;
  });
  dropzone.addEventListener("dragover", (event) => {
    if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = state.importing ? "none" : "copy";
  });
  dropzone.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) $("drop-overlay").hidden = true;
  });
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dragDepth = 0;
    $("drop-overlay").hidden = true;
    if (event.dataTransfer.files.length > 1) toast("每次导入一个文件，其余文件可以稍后继续导入。");
    importFile(event.dataTransfer.files[0]);
  });
  document.addEventListener("dragover", (event) => {
    if (Array.from(event.dataTransfer?.types || []).includes("Files")) event.preventDefault();
  });
  document.addEventListener("drop", (event) => {
    if (Array.from(event.dataTransfer?.types || []).includes("Files")) event.preventDefault();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshStatus({ silent: true });
  });
}

getToken();
hydrateIcons();
bindEvents();
$("today-label").textContent = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" }).format(new Date());
renderReaderEmpty();
refreshStatus({ silent: true });
