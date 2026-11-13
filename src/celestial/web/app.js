"use strict";

// ---- 小道具 ----
const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c);
  return el;
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "エラーが発生しました");
  return data;
}

let toastTimer;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 3500);
}

const num = (n) => Math.round(n).toLocaleString("ja-JP");
const secs = (s) => (s < 10 ? s.toFixed(1) : Math.round(s)) + "秒";
const pct = (x) => Math.round(x * 100) + "%";
const when = (ts) => {
  const d = new Date(ts * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString("ja-JP", { month: "numeric", day: "numeric" });
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

const ROUTE = {
  learned: ["学習済みの振り分け", "gold"], manager: ["管理AIが振り分け", ""], heuristic: ["キーワードで振り分け", ""],
  forced: ["担当を指定", ""], cache: ["キャッシュ", "gold"], plan: ["分担", ""],
};
let STATE = { models: [], roles: [], role_labels: {}, settings: {} };
const roleLabel = (r) => STATE.role_labels[r] || r;

// ---- 簡易Markdown(必ずエスケープしてから整形する) ----
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (m, pre, url) => `${pre}<a href="${url}" target="_blank" rel="noreferrer">${url}</a>`);
}

const DELIM_ROW = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const cellsOf = (line) => line.trim().replace(/^\|+|\|+$/g, "").split("|").map((c) => c.trim());

// 両端の | を省いた表(「月 | 売上」の次の行が「--- | ---」。GFM では正しい書き方)を、両端に | のある形にそろえる
function pipeTables(text) {
  const lines = text.split("\n"), out = [];
  let fenced = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i], next = lines[i + 1];
    if (line.trim().startsWith("```")) fenced = !fenced;
    else if (!fenced && line.includes("|") && next !== undefined && next.includes("|") && DELIM_ROW.test(next)
             && cellsOf(line).length === cellsOf(next).length) {
      let j = i + 2;
      while (j < lines.length && lines[j].includes("|") && !lines[j].trim().startsWith("```")) j++;
      for (const row of lines.slice(i, j)) out.push("| " + cellsOf(row).join(" | ") + " |");
      i = j - 1;
      continue;
    }
    out.push(line);
  }
  return out.join("\n");
}

// Excel にできる表(2行以上の Markdown の表、または CSV のコードブロック)があるか
function hasTable(text) {
  let rows = 0;
  for (const line of pipeTables(text).split("\n")) {
    if (!line.trim().startsWith("|")) { rows = 0; continue; }
    if (!DELIM_ROW.test(line)) rows += 1;
    if (rows >= 2) return true;
  }
  return /```(?:csv|tsv)[ \t]*\n/i.test(text);
}

// 会話の一覧に出す1行: Markdown の書式の記号と添付ファイルの中身を除き、表は「セル セル / セル セル」にする(スマホと同じ)
function previewText(content) {
  const body = (content || "")
    .replace(/\n\n\[添付ファイル: ([^\]\n]+)\]\n````\n[\s\S]*?\n````/g, " ")
    .replace(/```[\s\S]*?(```|$)/g, " ");
  return pipeTables(body).split("\n")
    .map((l) => (l.includes("|") && DELIM_ROW.test(l) ? "" : /^\s*\|.*\|\s*$/.test(l) ? cellsOf(l).join(" ") + " /" : l))
    .join("\n")
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+/gm, "")
    .replace(/\*\*|__|`/g, "")
    .replace(/(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])/g, "$1")
    .replace(/\[([^\]\n]+)\]\([^)\s]+\)/g, "$1")
    .replace(/\s+/g, " ").trim().replace(/\s*\/$/, "");
}

function renderTable(lines) {
  const rows = lines.filter((l, i) => !(i === 1 && DELIM_ROW.test(l))).map(cellsOf);
  const table = h("table", {});
  rows.forEach((cells, i) => {
    const tr = h("tr", {});
    for (const c of cells) { const cell = h(i === 0 ? "th" : "td", {}); cell.innerHTML = inline(c); tr.append(cell); }
    table.append(tr);
  });
  return h("div", { class: "table-wrap" }, table);
}

function renderMarkdown(target, text) {
  target.textContent = "";
  const parts = pipeTables(text).split(/```/);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const lang = (part.match(/^[^\n]*/) || [""])[0].trim();
      const body = part.replace(/^[^\n]*\n?/, "").replace(/\n$/, "");
      const code = h("code", {});
      code.textContent = body;
      target.append(h("div", { class: "codeblock" },
        h("div", { class: "code-head" }, h("span", {}, lang || "コード"),
          h("button", { type: "button", onclick: (e) => copy(body, e.target) }, "コピー")), h("pre", {}, code)));
      return;
    }
    let list = null, para = [], table = [];
    const flush = () => {
      if (para.length) { const p = h("p", {}); p.innerHTML = para.map(inline).join("<br>"); target.append(p); para = []; }
      if (table.length) { target.append(renderTable(table)); table = []; }
    };
    for (const raw of part.split("\n")) {
      const line = raw.trimEnd();
      const head = line.match(/^#{1,4}\s+(.*)/);
      const item = line.match(/^\s*(?:[-*・]|(\d+)[.)])\s+(.*)/);
      if (/^\s*\|.*\|\s*$/.test(line)) { if (para.length) { const keep = table; table = []; flush(); table = keep; } list = null; table.push(line); continue; }
      if (table.length) flush();
      if (head) { flush(); list = null; const el = h("h3", {}); el.innerHTML = inline(head[1]); target.append(el); }
      else if (item) {
        flush();
        const kind = item[1] ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== kind) { list = h(kind, {}); target.append(list); }
        const li = h("li", {}); li.innerHTML = inline(item[2]); list.append(li);
      }
      else if (!line.trim()) { flush(); list = null; }
      else { list = null; para.push(line); }
    }
    flush();
  });
}

async function copy(text, button) {
  try { await navigator.clipboard.writeText(text); if (button) { const t = button.textContent; button.textContent = "コピーしました"; setTimeout(() => (button.textContent = t), 1500); } }
  catch { toast("コピーできませんでした"); }
}

// ---- 表示の切り替え ----
const loaders = { learn: () => loadLearn(), bench: () => loadBench(), settings: () => loadSettings(), phone: () => loadPhone() };
let currentView = "chat";
function show(view) {
  currentView = view;
  for (const b of $("nav").children) b.classList.toggle("on", b.dataset.view === view);
  for (const s of document.querySelectorAll(".view")) s.hidden = s.id !== "view-" + view;
  if (loaders[view]) loaders[view]().catch((e) => toast(e.message));
  if (view === "chat") $("task").focus();
}
$("nav").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) show(b.dataset.view); });

const THEMES = [["", "自動"], ["light", "ライト"], ["dark", "ダーク"]];
function applyTheme(value) {
  if (value) document.documentElement.dataset.theme = value; else delete document.documentElement.dataset.theme;
  $("theme").textContent = "表示: " + (THEMES.find((t) => t[0] === value) || THEMES[0])[1];
}
let theme = "";
try { theme = localStorage.getItem("celestial-theme") || ""; } catch {}
applyTheme(theme);
$("theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.findIndex((t) => t[0] === theme) + 1) % THEMES.length][0];
  applyTheme(theme);
  try { localStorage.setItem("celestial-theme", theme); } catch {}
});

async function loadState() {
  STATE = await api("/api/state");
  const status = $("status");
  status.className = "status " + (STATE.error ? "ng" : "ok");
  status.lastElementChild.textContent = STATE.error ? "AIに接続できません" : `${STATE.models.length}個のAIが利用可能`;
  status.title = STATE.error || "";
  const role = $("opt-role");
  if (role.options.length === 1) for (const r of STATE.roles) role.append(h("option", { value: r.name }, r.label));
  const bench = $("bench-model"), keep = bench.value;
  bench.length = 1;
  for (const m of STATE.models) bench.append(h("option", { value: m.name }, m.name + " に固定"));
  bench.value = keep;
  return STATE;
}

async function refreshLinkStatus() {
  try {
    const d = await api("/api/devices");
    const ts = d.remote.tailscale, el = $("link-status");
    const on = d.remote.listening.length > 0;
    el.className = "status " + (on ? "ok" : "ng");
    el.lastElementChild.textContent = on ? `スマホ連携: 待受中(端末 ${d.devices.length})` : ts.state === "missing" ? "スマホ連携: Tailscale なし" : "スマホ連携: 未接続";
    el.title = on ? d.remote.urls.join("\n") : (d.remote.error || `Tailscale の状態: ${ts.state}`);
  } catch { /* 起動直後などは無視 */ }
}

// ---- 会話 ----
const EXAMPLES = [
  ["Pythonでリストの重複を取り除く関数を書いて", "コード"],
  ["りんごが3個で450円のとき、7個ではいくら?", "計算・推論"],
  ["次を英語に翻訳して: 明日の会議は10時からに変更になりました", "翻訳"],
  ["取引先の田中さんへ、打ち合わせのお礼メールを短く書いて", "文章作成"],
];
let currentId = null;
let threads = [];
let localRun = null;            // この画面から始めた実行 {runId, conversationId, assistantId}
let attachments = [];           // 送信前の添付 [{kind: "image", name, data, url} | {kind: "text", name, text}]
const approvalCards = new Map(); // 確認のカード id → 要素

function welcome() {
  const box = h("div", { class: "welcome" },
    h("h1", {}, "何をしましょうか"),
    h("p", {}, "依頼は管理AIが内容を見て、得意な小型AIに振り分けます。会話はスマホと同期され、回答に「良い」「悪い」を付けると次からの担当選びと回答に反映されます。"),
    h("div", { class: "examples" }, EXAMPLES.map(([text, kind]) =>
      h("button", { type: "button", onclick: () => { $("task").value = text; grow(); $("task").focus(); } }, h("small", {}, kind), text))));
  $("messages").replaceChildren(box);
}

function scrollDown(force) {
  const m = $("messages");
  if (force || m.scrollHeight - m.scrollTop - m.clientHeight < 160) m.scrollTop = m.scrollHeight;
}
function grow() { const t = $("task"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 220) + "px"; }
const pressed = (id) => $(id).getAttribute("aria-pressed") === "true";
for (const id of ["opt-verify", "opt-swarm", "opt-agent"]) {
  $(id).addEventListener("click", () => { $(id).setAttribute("aria-pressed", String(!pressed(id))); if (id === "opt-agent") showFolderPick(); });
}

// エージェントが作業するフォルダ(作業フォルダの中から選ぶ。Codex や Claude Code のように、そのフォルダの中で編集する)
async function showFolderPick() {
  const pick = $("opt-folder-pick");
  pick.hidden = !pressed("opt-agent");
  if (pick.hidden) return;
  try {
    const data = await api("/api/folders");
    const select = $("opt-folder"), keep = select.value;
    if (!data.root) { pick.hidden = true; return; }
    select.replaceChildren(h("option", { value: "" }, `${data.root}(作業フォルダ全体)`), ...data.folders.map((f) => h("option", { value: f }, f)));
    select.value = data.folders.includes(keep) ? keep : "";
  } catch { pick.hidden = true; }
}

async function loadThreads() {
  const q = $("thread-search").value.trim();
  const data = await api("/api/conversations" + (q ? "?q=" + encodeURIComponent(q) : ""));
  threads = data.conversations;
  renderThreads();
}

function renderThreads() {
  const list = $("thread-list");
  if (!threads.length) {
    list.replaceChildren(h("p", { class: "empty pad" }, $("thread-search").value.trim() ? "見つかりませんでした" : "まだ会話がありません"));
    return;
  }
  list.replaceChildren(...threads.map((t) => h("button", {
    type: "button", role: "listitem", class: "thread" + (t.id === currentId ? " on" : ""), onclick: () => openConversation(t.id),
  },
    h("div", { class: "thread-top" },
      t.pinned ? h("span", { class: "pin", title: "ピン留め" }) : null,
      h("b", {}, t.title || "無題の会話"),
      h("span", { class: "thread-time" }, when(t.updated))),
    h("div", { class: "thread-preview" },
      t.origin !== "pc" ? h("span", { class: "tag tiny" }, "スマホ") : null,
      previewText(t.preview)))));
}

$("thread-search").addEventListener("input", debounce(() => loadThreads().catch((e) => toast(e.message)), 250));

async function openConversation(id, opts = {}) {
  const data = await api("/api/conversations/" + id);
  if (currentId !== id && !opts.refresh) {
    for (const [, card] of approvalCards) card.remove();
    approvalCards.clear();
  }
  currentId = id;
  try { localStorage.setItem("celestial-conversation", id); } catch {}
  renderHeader(data.conversation);
  renderThreads();
  const box = $("messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  if (!data.messages.length) welcome();
  else box.replaceChildren(...data.messages.map(messageView));
  if (!opts.refresh || nearBottom) scrollDown(true);
}

function renderHeader(conv) {
  $("chat-title").textContent = conv ? (conv.title || "無題の会話") : "新しい会話";
  $("chat-sub").textContent = conv ? `${conv.count != null ? conv.count + " 件・" : ""}${conv.origin !== "pc" ? "スマホで開始・" : ""}${new Date(conv.created * 1000).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}` : "";
  const actions = $("chat-actions");
  if (!conv) { actions.replaceChildren(); return; }
  const rename = h("button", { type: "button", class: "ghost small", onclick: () => {
    const input = h("input", { type: "text", class: "title-input", value: conv.title || "", "aria-label": "会話の名前" });
    const save = async () => { try { await api("/api/conversations/" + conv.id, { title: input.value.trim() }); await loadThreads(); await openConversation(conv.id, { refresh: true }); } catch (e) { toast(e.message); } };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") renderHeader(conv); });
    input.addEventListener("blur", save);
    $("chat-title").replaceChildren(input); input.focus(); input.select();
  } }, "名前を変える");
  const pin = h("button", { type: "button", class: "ghost small", onclick: async () => {
    try { await api("/api/conversations/" + conv.id, { pinned: !conv.pinned }); conv.pinned = !conv.pinned; renderHeader(conv); loadThreads(); } catch (e) { toast(e.message); }
  } }, conv.pinned ? "ピン留めを外す" : "ピン留め");
  const exp = h("a", { class: "ghost small btn-link", href: `/api/conversations/${conv.id}/export`, download: (conv.title || "会話") + ".md" }, "書き出す");
  const del = h("button", { type: "button", class: "ghost small danger" }, "削除");
  let armed = false;
  del.addEventListener("click", async () => {
    if (!armed) { armed = true; del.textContent = "もう一度押すと削除"; setTimeout(() => { armed = false; del.textContent = "削除"; }, 3000); return; }
    try { await api("/api/conversations/" + conv.id, { deleted: true }); newConversation(); loadThreads(); toast("会話を削除しました"); } catch (e) { toast(e.message); }
  });
  actions.replaceChildren(rename, pin, exp, del);
}

function newConversation() {
  currentId = null;
  try { localStorage.removeItem("celestial-conversation"); } catch {}
  renderHeader(null);
  renderThreads();
  welcome();
  $("task").focus();
}
$("new-chat").addEventListener("click", newConversation);

function rateButtons(messageId, current) {
  const good = h("button", { type: "button", class: "rate good" + (current === 1 ? " on" : "") }, "良い");
  const bad = h("button", { type: "button", class: "rate bad" + (current === -1 ? " on" : "") }, "悪い");
  const send = async (isGood) => {
    try {
      await api("/api/feedback", { message_id: messageId, good: isGood });
      good.classList.toggle("on", isGood); bad.classList.toggle("on", !isGood);
      toast(isGood ? "手本として覚えました" : "この回答は今後使いません");
    } catch (e) { toast(e.message); }
  };
  good.addEventListener("click", () => send(true));
  bad.addEventListener("click", () => send(false));
  return [good, bad];
}

function metaRow(message) {
  const m = message.meta || {};
  const [label, tone] = ROUTE[m.route_source] || [m.route_source || "", ""];
  const tools = m.tool_calls || [];
  const row = h("div", { class: "meta" },
    m.agent || tools.length ? h("span", { class: "tag accent" }, "エージェント") : null,
    label ? h("span", { class: "tag " + tone }, label) : null,
    m.role ? h("span", { class: "tag accent" }, roleLabel(m.role)) : null,
    m.model ? h("span", { class: "tag" }, m.model) : null,
    m.examples_used ? h("span", { class: "tag gold" }, `手本 ${m.examples_used}件`) : null,
    tools.length ? h("span", { class: "tag", title: tools.map((c) => `${c.label}: ${c.detail}${c.ok ? "" : "(失敗)"}`).join("\n") },
      "道具: " + tools.map((c) => c.label + (c.ok ? "" : "(失敗)")).join(" → ")) : null,
    m.subresults && m.subresults.length ? h("span", { class: "tag" }, "分担: " + m.subresults.map((s) => roleLabel(s.role)).join("・")) : null,
    m.escalated ? h("span", { class: "tag", title: m.escalated.confidence != null ? `最初の回答の確からしさ ${m.escalated.confidence.toFixed(3)}` : "" },
      m.escalated.reason === "lang" ? "日本語で答え直し"
        : m.escalated.from === m.escalated.to ? "資料で確かめて答え直し" : `答え直し ${m.escalated.from} → ${m.escalated.to}`) : null,
    (m.sources || []).length ? h("span", { class: "sources" }, "出典",
      ...m.sources.map((s) => h("a", { class: "tag src", href: s.url, target: "_blank", rel: "noopener noreferrer" }, s.title))) : null,
    m.tokens != null ? h("span", {}, `${num(m.tokens)} トークン`) : null,
    m.latency_s != null ? h("span", {}, secs(m.latency_s)) : null,
    h("span", { class: "sep" }));
  if (m.status === "done" && (m.interaction_id || message.id)) row.append(...rateButtons(message.id, m.feedback));
  row.append(h("button", { type: "button", class: "rate", onclick: (e) => copy(message.content, e.target) }, "コピー"));
  if (m.status === "done" && message.content.trim()) {
    // 回答を文書として保存する(Excel は表を含む回答だけ)
    const link = (fmt, label) => h("a", { href: `/api/messages/${message.id}/export?format=${fmt}`, download: "" }, label);
    row.append(h("details", { class: "save-menu" }, h("summary", { class: "rate" }, "保存"),
      h("div", { class: "save-list" }, link("docx", "Word 文書"), hasTable(message.content) ? link("xlsx", "Excel(表)") : null, link("pptx", "PowerPoint"))));
  }
  if (["done", "error", "cancelled"].includes(m.status)) {
    row.append(h("button", { type: "button", class: "rate", title: "キャッシュを使わず、検証つきで答え直します", onclick: () => regenerate(message.id) }, "もう一度"));
  }
  return row;
}

async function regenerate(messageId) {
  try {
    await api(`/api/messages/${messageId}/regenerate`, { stream: false });
    toast("検証つきで、もう一度答えています…");
    refreshFromSync();
  } catch (e) { toast(e.message); }
}

function messageView(message) {
  const m = message.meta || {};
  if (message.role === "user") {
    const { text, files } = splitAttachments(message.content);
    return h("div", { class: "msg user", "data-id": message.id },
      m.images ? h("div", { class: "meta right" }, h("span", { class: "tag" }, `画像 ${m.images} 枚`)) : null,
      h("div", { class: "bubble" }, text,
        ...files.map((f) => h("details", { class: "attached" }, h("summary", {}, `添付ファイル: ${f.name}`), h("pre", {}, f.body)))),
      h("div", { class: "meta right user-actions" },
        h("button", { type: "button", class: "rate", title: "入力欄に戻して、直してから送り直します",
          onclick: () => { $("task").value = text.trim(); grow(); $("task").focus(); } }, "直して送る"),
        h("button", { type: "button", class: "rate", onclick: (e) => copy(message.content, e.target) }, "コピー")),
      m.device && m.device !== "pc" ? h("div", { class: "meta right" }, h("span", { class: "tag tiny" }, "スマホから")) : null);
  }
  const answer = h("div", { class: "answer" + (m.status === "error" ? " error" : "") });
  if (message.content) renderMarkdown(answer, message.content);
  const box = h("div", { class: "msg ai", "data-id": message.id });
  if (m.status === "running") box.append(h("div", { class: "stage" }, h("i", { class: "dot" }), h("span", {}, "応答中…")));
  box.append(answer);
  if ((m.changes || []).length) box.append(changesView(m.changes));
  if (m.memory_suggestion) box.append(memorySuggestion(message));
  if (m.status && m.status !== "running") box.append(metaRow(message));
  if (m.device && m.device !== "pc" && m.model && m.model.startsWith("local:")) box.prepend(h("div", { class: "meta" }, h("span", { class: "tag tiny" }, "スマホ内のAI")));
  return box;
}

// 「覚えますか」の提案(発言に利用者についての事実があると、判断の機能が見分けて出す)
function memorySuggestion(message) {
  const fact = message.meta.memory_suggestion;
  const answer = async (accept) => {
    try { await api(`/api/messages/${message.id}/memory`, { accept }); toast(accept ? `覚えました: ${fact}` : "覚えませんでした"); refreshFromSync(); }
    catch (e) { toast(e.message); }
  };
  return h("div", { class: "memory-suggest" },
    h("span", { class: "note" }, "覚えますか"), h("b", {}, fact),
    h("button", { type: "button", class: "rate", onclick: () => answer(true) }, "覚える"),
    h("button", { type: "button", class: "rate", onclick: () => answer(false) }, "いいえ"));
}

// エージェントが変えたファイル(Claude Code のように、ファイルごとの増減と差分を見せる)
function changesView(changes) {
  const added = changes.reduce((s, c) => s + (c.added || 0), 0), removed = changes.reduce((s, c) => s + (c.removed || 0), 0);
  const label = { added: "新規", modified: "変更", deleted: "削除" };
  return h("details", { class: "changes", open: changes.length <= 3 },
    h("summary", {}, h("b", {}, `変更したファイル ${changes.length}件`), h("span", { class: "plus" }, `+${added}`), h("span", { class: "minus" }, `−${removed}`)),
    changes.map((c) => h("details", { class: "change" },
      h("summary", {}, h("span", { class: "tag " + (c.status === "deleted" ? "bad" : c.status === "added" ? "good" : "") }, label[c.status] || c.status),
        h("code", {}, c.path), c.binary ? h("span", { class: "note" }, "(文書・画像など)") : [h("span", { class: "plus" }, `+${c.added}`), h("span", { class: "minus" }, `−${c.removed}`)]),
      c.diff ? h("pre", { class: "diff" }, c.diff.split("\n").map((line) => h("span", {
        class: line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "" }, line + "\n"))) : null)));
}

// ---- 画像・文書の添付 ----
const TEXT_EXT = /\.(txt|md|markdown|csv|tsv|json|jsonl|py|js|mjs|ts|tsx|jsx|html?|css|scss|log|xml|ya?ml|toml|ini|cfg|conf|c|cc|cpp|h|hpp|cs|java|kt|go|rs|rb|php|sql|sh|ps1|bat|cmd|dart|swift|r|tex|srt|vtt)$/i;
const TEXT_LIMIT = 30000;
const ATTACH_RE = /\n\n\[添付ファイル: ([^\]\n]+)\]\n````\n([\s\S]*?)\n````/g;

function renderAttachments() {
  const box = $("attachments");
  box.hidden = attachments.length === 0;
  const remove = (i) => { if (attachments[i].url) URL.revokeObjectURL(attachments[i].url); attachments.splice(i, 1); renderAttachments(); };
  box.replaceChildren(...attachments.map((a, i) => a.kind === "text"
    ? h("div", { class: "filechip" }, h("b", {}, a.name), h("span", {}, `${num(a.text.length)} 文字`),
      h("button", { type: "button", "aria-label": "外す", onclick: () => remove(i) }, "外す"))
    : h("div", { class: "thumb" }, h("img", { src: a.url, alt: a.name }),
      h("button", { type: "button", "aria-label": "外す", onclick: () => remove(i) }, "外す"))));
}
function readText(file, encoding) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file, encoding);
  });
}
async function addFiles(files) {
  for (const file of files) {
    if (file.type.startsWith("image/")) {
      if (attachments.filter((a) => a.kind === "image").length >= 4) { toast("画像は4枚までです"); continue; }
      if (file.size > 8_000_000) { toast("8MBを超える画像は添付できません"); continue; }
      const reader = new FileReader();
      reader.onload = () => { attachments.push({ kind: "image", name: file.name, data: String(reader.result).split(",")[1], url: URL.createObjectURL(file) }); renderAttachments(); };
      reader.readAsDataURL(file);
    } else if (TEXT_EXT.test(file.name) || file.type.startsWith("text/") || file.type === "application/json") {
      if (file.size > 2_000_000) { toast(`${file.name} は大きすぎます(2MBまで)`); continue; }
      let text = await readText(file, "utf-8");
      if (text.includes("�")) text = await readText(file, "shift_jis");  // 文字化けしたら Shift_JIS として読み直す
      if (text.length > TEXT_LIMIT) { text = text.slice(0, TEXT_LIMIT) + "\n…(長いため、ここで切りました)"; toast(`${file.name} は長いため、先頭の ${num(TEXT_LIMIT)} 文字だけを添えます`); }
      attachments.push({ kind: "text", name: file.name, text });
      renderAttachments();
    } else {
      toast(`${file.name} は添付できない種類です(画像と文字のファイルに対応)`);
    }
  }
}
function withAttachments(task, texts) {
  return task + texts.map((t) => `\n\n[添付ファイル: ${t.name}]\n\`\`\`\`\n${t.text.replace(/\n?$/, "")}\n\`\`\`\``).join("");
}
function splitAttachments(content) {
  const files = [];
  const text = content.replace(ATTACH_RE, (_, name, body) => { files.push({ name, body }); return ""; });
  return { text, files };
}
$("attach").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
$("task").addEventListener("paste", (e) => { const files = [...(e.clipboardData?.files || [])]; if (files.length) { e.preventDefault(); addFiles(files); } });
$("composer").addEventListener("dragover", (e) => { e.preventDefault(); $("composer").classList.add("drop"); });
$("composer").addEventListener("dragleave", () => $("composer").classList.remove("drop"));
$("composer").addEventListener("drop", (e) => { e.preventDefault(); $("composer").classList.remove("drop"); addFiles(e.dataTransfer.files); });

// ---- 確認(エージェントの影響のある操作の前) ----
function approvalCard(ev) {
  if (approvalCards.has(ev.id)) return approvalCards.get(ev.id);
  const card = h("div", { class: "panel approval" }, h("b", {}, `確認: ${ev.label}`), h("div", { class: "note" }, ev.summary || ""));
  if (ev.preview) { const pre = h("pre", {}); pre.textContent = ev.preview; card.append(h("div", { class: "codeblock" }, pre)); }
  const reply = async (allow, all) => {
    try { await api("/api/approve", { id: ev.id, allow, all }); } catch (e) { toast(e.message); }
    finish(allow);
  };
  const finish = (allow) => {
    card.replaceChildren(h("span", { class: "note" }, `${ev.label}(${ev.summary || ""}): ${allow ? "許可しました" : "拒否しました"}`));
    card.classList.add("answered");
    approvalCards.delete(ev.id);
    setTimeout(() => { if (card.parentElement && card.parentElement.id === "global-approvals") card.remove(); }, 4000);
  };
  card.finish = finish;
  card.append(h("div", { class: "composer-row" },
    h("button", { type: "button", class: "primary", onclick: () => reply(true, false) }, "許可"),
    h("button", { type: "button", class: "ghost", onclick: () => reply(true, true) }, "この依頼の間はすべて許可"),
    h("button", { type: "button", class: "ghost", onclick: () => reply(false, false) }, "拒否")));
  approvalCards.set(ev.id, card);
  return card;
}

// ---- 送信 ----
function stageText(ev) {
  const s = ev.stage;
  if (s === "cache") return "キャッシュから即答";
  if (s === "route") return `${roleLabel(ev.role)}の担当へ(${(ROUTE[ev.source] || [ev.source])[0]})`;
  if (s === "worker") return ev.agent ? `${ev.model} がエージェントとして作業中` : `${ev.model} が回答中` + (ev.examples ? `(手本 ${ev.examples}件を参照)` : "");
  if (s === "agent_step") return `[${ev.n}手目] ${ev.label}` + (ev.detail ? `(${ev.detail})` : "");
  if (s === "verify") return "管理AIが回答を検証中…";
  if (s === "retry") return ev.issue ? `${ev.model} が${ev.issue}中` : `検証で問題が見つかったため ${ev.model} が再回答中`;
  if (s === "escalate") return ev.reason === "lang" ? `言語を取り違えたため ${ev.model} が答え直し中` : `確からしさが低いため ${ev.model} が答え直し中`;
  if (s === "ground") return "Wikipedia で事実を確かめています…";
  if (s === "plan") return `${ev.subtasks.length}件に分解: ` + ev.subtasks.map((t) => roleLabel(t.role)).join("・");
  if (s === "sub_done") return "分担した作業が1件完了";
  if (s === "synth") return `${ev.model} が結果を統合中`;
  if (s === "project") {
    if (ev.phase === "plan") return "プロジェクトの計画を立てています…";
    if (ev.phase === "file") return `ファイルを書いています(${ev.n}/${ev.total}): ${ev.path}`;
    if (ev.phase === "check") return ev.attempt ? "直したものを確かめています…" : "構文とテストで確かめています…";
    if (ev.phase === "fix") return `問題のあったファイルを直しています: ${ev.path}`;
  }
  if (s === "memory") return "覚えていることを更新しています";
  if (s === "news") return ev.topic ? `「${ev.topic}」のニュースを探しています…` : "最新のニュースを取得しています…";
  if (s === "calc") return "日付を計算しています";
  return null;
}

async function sendMessage(task) {
  const box = $("messages");
  if (box.querySelector(".welcome")) box.replaceChildren();
  const images = attachments.filter((a) => a.kind === "image").map((a) => a.data);
  const texts = attachments.filter((a) => a.kind === "text");
  box.append(h("div", { class: "msg user" }, images.length ? h("div", { class: "meta right" }, h("span", { class: "tag" }, `画像 ${images.length} 枚`)) : null,
    h("div", { class: "bubble" }, task, ...texts.map((t) => h("details", { class: "attached" }, h("summary", {}, `添付ファイル: ${t.name}`), h("pre", {}, t.text))))));
  task = withAttachments(task, texts);
  attachments.forEach((a) => { if (a.url) URL.revokeObjectURL(a.url); }); attachments = []; renderAttachments();
  const stageLabel = h("span", {}, "キャッシュと担当を確認中…");
  const stage = h("div", { class: "stage" }, h("i", { class: "dot" }), stageLabel);
  const steps = h("ol", { class: "steps" });
  const answer = h("div", { class: "answer" });
  const msg = h("div", { class: "msg ai" }, stage, steps, answer);
  box.append(msg);
  scrollDown(true);

  const run = { runId: null, conversationId: currentId, assistantId: null };
  localRun = run;
  $("send").textContent = "停止"; $("send").classList.add("stop");
  let buffer = "", skipNext = false, pending = false, finished = false;
  const paint = () => { pending = false; renderMarkdown(answer, buffer); scrollDown(); };

  const onEvent = (ev) => {
    if (ev.type === "run") {
      run.runId = ev.run_id; run.conversationId = ev.conversation_id; run.assistantId = ev.assistant_message_id;
      if (currentId !== ev.conversation_id) { currentId = ev.conversation_id; try { localStorage.setItem("celestial-conversation", currentId); } catch {} }
      loadThreads().then(() => renderHeader(threads.find((t) => t.id === currentId) || null)).catch(() => {});
      if (ev.context) stageLabel.textContent = "会話の流れを踏まえて考えています…";
    } else if (ev.type === "token") {
      if (skipNext) { skipNext = false; return; }
      buffer += ev.text;
      if (!pending) { pending = true; requestAnimationFrame(paint); }
    } else if (ev.type === "stage") {
      if (ev.stage === "retry") { buffer = ""; skipNext = true; }
      if (ev.stage === "agent_step") steps.append(h("li", { "data-n": ev.n }, h("b", {}, ev.label), ev.detail ? h("span", {}, ev.detail) : null));
      const text = stageText(ev);
      if (text) stageLabel.textContent = text;
      scrollDown();
    } else if (ev.type === "approval") {
      msg.insertBefore(approvalCard(ev), answer); scrollDown(true);
    } else if (ev.type === "approval_done") {
      const card = approvalCards.get(ev.id); if (card) card.finish(ev.allowed);
    } else if (ev.type === "done") {
      finished = true;
      stage.remove();
      buffer = ev.message.content;
      renderMarkdown(answer, buffer);
      msg.dataset.id = ev.message.id;
      msg.append(metaRow(ev.message));
    } else if (ev.type === "error" || ev.type === "cancelled") {
      finished = true;
      stage.remove();
      if (ev.type === "error") { answer.classList.add("error"); answer.textContent = ev.message; }
      else msg.append(h("div", { class: "meta" }, "停止しました"));
    }
  };

  try {
    const res = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task, conversation_id: currentId, role: $("opt-role").value, context: $("opt-context").value,
        verify: pressed("opt-verify"), swarm: pressed("opt-swarm") ? "on" : "auto", agent: pressed("opt-agent"), images,
        folder: pressed("opt-agent") ? $("opt-folder").value : "",
      }),
    });
    if (!res.ok) throw new Error((await res.json()).error || "エラーが発生しました");
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let tail = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const lines = (tail + decoder.decode(value, { stream: true })).split("\n");
      tail = lines.pop();
      for (const line of lines) if (line.trim()) onEvent(JSON.parse(line));
    }
    if (!finished) { stage.remove(); msg.append(h("div", { class: "meta" }, "接続が切れました。実行は PC で続いており、終わると会話に保存されます。")); }
  } catch (e) {
    stage.remove();
    answer.classList.add("error"); answer.textContent = e.message;
  } finally {
    localRun = null;
    $("send").textContent = "送信"; $("send").classList.remove("stop");
    scrollDown();
    loadThreads().catch(() => {});
  }
}

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (localRun) {
    if (localRun.runId) { try { await api(`/api/runs/${localRun.runId}/cancel`, {}); } catch (err) { toast(err.message); } }
    return;
  }
  const task = $("task").value.trim();
  if (!task) return;
  $("task").value = ""; grow();
  sendMessage(task);
});
$("task").addEventListener("input", grow);
$("task").addEventListener("keydown", (e) => {
  // 日本語入力の変換確定の Enter では送信しない
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); $("composer").requestSubmit(); }
});

// ---- 他の端末で起きたことの反映 ----
const refreshFromSync = debounce(() => {
  loadThreads().catch(() => {});
  if (currentId && !(localRun && localRun.conversationId === currentId) && currentView === "chat") openConversation(currentId, { refresh: true }).catch(() => {});
}, 450);

const inbox = [];
function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }
    if (ev.type === "sync" || ev.type === "run_finished" || ev.type === "run_started") refreshFromSync();
    else if (ev.type === "approval") {
      if (approvalCards.has(ev.id)) return;
      if (localRun && localRun.runId === ev.run_id) return;  // この画面の実行は、流れの中で表示する
      $("global-approvals").append(approvalCard({ ...ev, label: `${ev.label}(別の端末からの依頼)` }));
    } else if (ev.type === "approval_done") {
      const card = approvalCards.get(ev.id); if (card) card.finish(ev.allowed);
    } else if (ev.type === "flows" || ev.type === "flow_run") {
      if (currentView === "flows") loadFlows().catch(() => {});
      if (ev.type === "flow_run" && ev.status === "error") toast("自動化が途中で失敗しました。「自動化」で記録を確かめてください");
    } else if (ev.type === "push" && !ev.only_phones) {
      inbox.unshift({ ...ev, at: Date.now() / 1000 });
      toast(`${ev.sender || "スマホ"}から: ${ev.kind === "clipboard" ? "クリップボードに入れました" : ev.text.slice(0, 60)}`);
      if (currentView === "phone") loadPhone().catch(() => {});
    } else if (ev.type === "devices") { refreshLinkStatus(); if (currentView === "phone") loadPhone().catch(() => {}); }
    else if (ev.type === "settings") loadState().catch(() => {});
  };
  source.onerror = () => { /* EventSource が自動で再接続する */ };
}

// ---- 学習状況 ----
function kpi(label, value, unit, note, tone) {
  return h("div", { class: "kpi " + (tone || "") }, h("span", {}, label), h("b", {}, value, unit ? h("small", {}, unit) : null), note ? h("em", {}, note) : null);
}

function barChart(series, key, unit) {
  const max = Math.max(1, ...series.map((p) => p[key]));
  const w = 100 / Math.max(series.length, 30);  // 回数が少ないうちは棒が太りすぎないようにする
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 100 100"); svg.setAttribute("preserveAspectRatio", "none"); svg.setAttribute("role", "img");
  let html = '<line class="grid-line" x1="0" x2="100" y1="0.5" y2="0.5"/><line class="grid-line" x1="0" x2="100" y1="50" y2="50"/><line class="grid-line" x1="0" x2="100" y1="99.5" y2="99.5"/>';
  series.forEach((p, i) => {
    const cached = p.source === "cache";
    const height = cached ? 3 : Math.max(1.5, (p[key] / max) * 100);
    html += `<rect class="${cached ? "bar-cache" : "bar-llm"}" x="${(i * w + w * 0.12).toFixed(3)}" y="${(100 - height).toFixed(2)}" width="${(w * 0.76).toFixed(3)}" height="${height.toFixed(2)}"><title>#${p.id}: ${unit === "秒" ? p[key].toFixed(1) : num(p[key])}${unit}${cached ? "(キャッシュ)" : ""}</title></rect>`;
  });
  svg.innerHTML = html;
  return h("div", { class: "chart" },
    h("div", { class: "axis" }, h("span", {}, `最大 ${unit === "秒" ? max.toFixed(1) : num(max)}${unit}`), h("span", {}, `直近 ${series.length} 回`)),
    svg, h("div", { class: "axis" }, h("span", {}, "古い"), h("span", {}, "新しい")));
}

function compareRows(a, b) {
  const rows = [
    ["平均トークン", num(a.avg_tokens), num(b.avg_tokens), b.avg_tokens - a.avg_tokens, a.avg_tokens, true],
    ["平均所要時間", secs(a.avg_latency_s), secs(b.avg_latency_s), b.avg_latency_s - a.avg_latency_s, a.avg_latency_s, true],
    ["キャッシュ利用", pct(a.cache_rate), pct(b.cache_rate), b.cache_rate - a.cache_rate, 0, false],
    ["学習済みの振り分け", pct(a.learned_rate), pct(b.learned_rate), b.learned_rate - a.learned_rate, 0, false],
  ];
  const cells = [h("span", { class: "hd" }, ""), h("span", { class: "hd num" }, `前半 ${a.n}回`), h("span", { class: "hd num" }, `後半 ${b.n}回`), h("span", { class: "hd delta" }, "")];
  for (const [label, x, y, diff, base, lowerIsBetter] of rows) {
    const better = lowerIsBetter ? diff < 0 : diff > 0;
    const text = Math.abs(diff) < 1e-9 ? "変化なし" : base ? `${diff > 0 ? "+" : "−"}${Math.round(Math.abs(diff) / base * 100)}%` : `${diff > 0 ? "+" : "−"}${Math.round(Math.abs(diff) * 100)}pt`;
    cells.push(h("span", {}, label), h("span", { class: "num" }, x), h("span", { class: "num" }, y),
      h("span", { class: "delta" }, h("span", { class: "tag " + (Math.abs(diff) < 1e-9 ? "" : better ? "good" : "bad") }, text)));
  }
  return h("div", { class: "compare" }, cells);
}

function historyRow(r) {
  const [label, tone] = ROUTE[r.route_source] || [r.route_source, ""];
  return h("div", { class: "hist-row" },
    h("div", { class: "task", title: r.task }, r.task),
    h("div", { class: "meta" }, h("span", { class: "tag " + tone }, label), h("span", { class: "tag accent" }, roleLabel(r.role)),
      h("span", { class: "tag" }, r.model), h("span", {}, `${num(r.tokens)} トークン`), h("span", {}, secs(r.latency_s))),
    h("div", { class: "acts" }, rateButtons(r.id, r.feedback)));
}

async function loadLearn() {
  const [s, hist] = await Promise.all([api("/api/stats"), api("/api/history?limit=30")]);
  const body = $("learn-body");
  if (!s.all.n) {
    body.replaceChildren(h("div", { class: "panel" }, h("p", { class: "empty" }, "まだ実行履歴がありません。「タスク」でいくつか実行すると、ここに学習の進み具合が表示されます。")));
    return;
  }
  const a = s.all, routerPct = Math.min(1, s.router_examples / s.router_needed);
  body.replaceChildren(
    h("div", { class: "kpis" },
      kpi("実行回数", num(a.n), "回"),
      kpi("平均トークン", num(a.avg_tokens), "", "1回あたりの計算量"),
      kpi("平均所要時間", a.avg_latency_s.toFixed(1), "秒"),
      kpi("キャッシュ利用", pct(a.cache_rate), "", "計算せずに即答した割合", "gold"),
      kpi("学習済みの振り分け", pct(a.learned_rate), "", "管理AIを呼ばずに済んだ割合", "gold"),
      kpi("節約できた計算", num(s.saved_tokens), "トークン", `約${secs(s.saved_seconds)}の短縮`, "gold")),
    h("div", { class: "cols" },
      h("div", { class: "panel" }, h("h2", {}, "1回ごとの計算量", h("small", {}, "トークン")), barChart(s.series, "tokens", ""),
        h("div", { class: "legend" }, h("span", {}, h("i", { class: "bar-llm", style: "background:var(--accent)" }), "AIが回答"), h("span", {}, h("i", { style: "background:var(--gold)" }), "キャッシュ"))),
      h("div", { class: "panel" }, h("h2", {}, "1回ごとの所要時間", h("small", {}, "秒")), barChart(s.series, "latency_s", "秒"))),
    h("div", { class: "cols" },
      h("div", { class: "panel" }, h("h2", {}, "前半と後半の比較"),
        s.first_half ? compareRows(s.first_half, s.second_half) : h("p", { class: "empty" }, `10回以上実行すると、前半と後半を比べて進化の度合いを表示します(現在 ${a.n}回)。`)),
      h("div", { class: "panel" }, h("h2", {}, "学習の蓄積"),
        h("div", {}, h("div", { class: "meta" }, h("span", {}, "振り分けの学習"), h("span", { class: "sep" }), h("span", {}, `${s.router_examples} / ${s.router_needed} 例`)),
          h("div", { class: "progress gold" }, h("i", { style: `width:${routerPct * 100}%` }))),
        h("p", { class: "note" }, routerPct < 1 ? "必要な例数に達すると、自信のあるタスクは管理AIを呼ばずに振り分けます。" : "学習済みです。自信のあるタスクは管理AIを呼ばずに振り分けます。"),
        h("div", { class: "meta" }, h("span", {}, "手本として覚えた回答"), h("span", { class: "sep" }), h("span", {}, `${s.memory_size} 件`)),
        h("div", { class: "meta" }, h("span", {}, "評価"), h("span", { class: "sep" }),
          h("span", { class: "tag good" }, `良い ${s.feedback.good}`), h("span", { class: "tag bad" }, `悪い ${s.feedback.bad}`), h("span", { class: "tag" }, `未評価 ${s.feedback.none}`)))),
    h("div", { class: "panel" }, h("h2", {}, "担当ごとのAIの実績", h("small", {}, "実績の良いAIが優先して選ばれます")),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "担当"), h("th", {}, "AI"), h("th", { class: "num" }, "回数"), h("th", {}, "評価点"), h("th", { class: "num" }, "平均秒"), h("th", { class: "num" }, "平均トークン"))),
        h("tbody", {}, s.role_model.map((r) => h("tr", {},
          h("td", {}, roleLabel(r.role)), h("td", {}, r.model), h("td", { class: "num" }, r.n),
          h("td", {}, h("div", { class: "meter" }, h("div", { class: "progress" }, h("i", { style: `width:${Math.max(0, Math.min(1, r.reward)) * 100}%` })), r.reward.toFixed(2))),
          h("td", { class: "num" }, r.latency.toFixed(1)), h("td", { class: "num" }, num(r.tokens)))))))),
    h("div", { class: "panel" }, h("h2", {}, "最近の実行", h("small", {}, "評価を付けると学習に反映されます")), h("div", { class: "hist" }, hist.map(historyRow))));
}

// ---- 計測 ----
let benchTimer = null;
$("bench-code").addEventListener("change", () => ($("bench-code-note").hidden = !$("bench-code").checked));
$("bench-start").addEventListener("click", async () => {
  try {
    await api("/api/bench", { model: $("bench-model").value, run_code: $("bench-code").checked, verify: $("bench-verify").checked });
    loadBench();
  } catch (e) { toast(e.message); }
});

function renderBenchJob(job) {
  const box = $("bench-progress");
  $("bench-start").disabled = job.running;
  $("bench-start").textContent = job.running ? "計測中…" : "計測を始める";
  if (!job.running && !job.report && !job.error) { box.replaceChildren(); return; }
  const done = job.outcomes.length, total = job.total || done || 1, ok = job.outcomes.filter((o) => o.ok).length;
  const title = job.running ? `計測中: ${job.label === "auto" ? "自動" : job.label}` : job.error ? "計測は中断されました" : `今回の結果: ${job.label === "auto" ? "自動" : job.label}`;
  const panel = h("div", { class: "panel" }, h("h2", {}, title, h("small", {}, `${done} / ${job.total || "?"} 件、正解 ${ok}`)),
    h("div", { class: "progress" }, h("i", { style: `width:${(done / total) * 100}%` })),
    h("div", { class: "dots" }, job.outcomes.map((o) => h("i", { class: o.ok ? "ok" : "ng", title: `${o.id}: ${o.ok ? "正解" : "不正解"}(${o.model}、${secs(o.latency_s)})` }))));
  if (job.error) panel.append(h("p", { class: "answer error" }, job.error));
  if (job.report) {
    const r = job.report;
    panel.append(h("div", { class: "kpis" },
      kpi("正解率", pct(r.accuracy), "", `${r.correct} / ${r.n} 件`),
      kpi("振り分けの正解", r.route_n ? pct(r.route_correct / r.route_n) : "-", "", `${r.route_correct} / ${r.route_n} 件`),
      kpi("平均トークン", num(r.avg_tokens)), kpi("平均所要時間", r.avg_latency_s.toFixed(1), "秒")),
      h("div", { class: "meta" }, Object.entries(r.by_role).map(([role, [c, n]]) => h("span", { class: "tag " + (c === n ? "good" : c / n < 0.5 ? "bad" : "") }, `${roleLabel(role)} ${c}/${n}`))));
    if (r.skipped) panel.append(h("p", { class: "note" }, `コードの課題 ${r.skipped} 件は対象外でした(「コードの課題も採点する」で含められます)。`));
  }
  box.replaceChildren(panel);
}

async function loadBench() {
  await loadState();
  const data = await api("/api/bench");
  renderBenchJob(data.job);
  clearTimeout(benchTimer);
  if (data.job.running && !$("view-bench").hidden) benchTimer = setTimeout(() => loadBench().catch(() => {}), 1200);
  const rows = data.history.slice().reverse();
  $("bench-history").replaceChildren(h("div", { class: "panel" }, h("h2", {}, "これまでの計測", h("small", {}, "課題集が同じもの同士で比べてください")),
    rows.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "日時"), h("th", {}, "対象"), h("th", {}, "正解率"), h("th", { class: "num" }, "振り分け"), h("th", { class: "num" }, "平均トークン"), h("th", { class: "num" }, "平均秒"), h("th", {}, "課題集"))),
      h("tbody", {}, rows.map((r) => h("tr", {},
        h("td", {}, new Date(r.ts * 1000).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })),
        h("td", {}, r.label === "auto" ? "自動" : r.label),
        h("td", {}, h("div", { class: "meter" }, h("div", { class: "progress" }, h("i", { style: `width:${(r.correct / r.n) * 100}%` })), `${pct(r.correct / r.n)} (${r.correct}/${r.n})`)),
        h("td", { class: "num" }, r.route_n ? `${r.route_correct}/${r.route_n}` : "-"),
        h("td", { class: "num" }, num(r.avg_tokens)), h("td", { class: "num" }, r.avg_latency_s.toFixed(1)),
        h("td", {}, h("span", { class: "path" }, r.suite)))))))
      : h("p", { class: "empty" }, "まだ計測結果がありません。「計測を始める」で最初の基準値を取りましょう。")));
}

// ---- 設定 ----
let ownTimer = null;
async function startWork(path, body) {
  try { await api(path, body); refreshOwn(); } catch (e) { toast(e.message); }
}

async function refreshOwn() {
  const panel = $("own-panel");
  if (!panel) return;
  let own;
  try { own = await api("/api/own"); } catch (e) { toast(e.message); return; }
  const job = own.job, busy = job.running;
  const perRole = h("select", { id: "own-per-role", "aria-label": "担当ごとの問題数" }, [10, 20, 30].map((n) => h("option", { value: n }, `担当ごとに${n}問`)));
  const parts = [
    h("h2", {}, "自作AI", h("small", {}, "小さな基礎モデルに、良い回答と自習の成果を教え込みます")),
    h("div", { class: "meta" }, h("span", {}, "学習に使えるデータ"), h("span", { class: "sep" }), h("span", {}, `${own.examples} 件(うち自習 ${own.study} 件)`)),
  ];
  if (!own.available) parts.push(h("p", { class: "note" }, "学習と自作AIの利用には追加のライブラリが必要です。"), h("pre", { class: "path" }, own.install_hint));
  parts.push(h("div", { class: "composer-row" },
    perRole,
    h("button", { type: "button", class: "ghost", disabled: busy, onclick: () => startWork("/api/study", { per_role: Number(perRole.value) }) }, "自習する"),
    h("span", { class: "grow" }),
    h("button", { type: "button", class: "primary", disabled: busy || !own.available || own.examples < 8, onclick: () => startWork("/api/train", {}) }, "学習を始める")));
  parts.push(h("p", { class: "note" }, "自習では、計測で最も正確だったAIが練習問題を作って解き、それが学習データになります。学習が終わると自動で精度を計測し、結果が担当選びに反映されます。"));
  if (busy) {
    parts.push(h("div", {}, h("div", { class: "meta" }, h("span", {}, (job.kind === "study" ? "自習中: " : "学習中: ") + job.message), h("span", { class: "sep" }), h("span", {}, job.total ? `${job.done} / ${job.total}` : "")),
      h("div", { class: "progress" }, h("i", { style: `width:${job.total ? (job.done / job.total) * 100 : 3}%` }))));
  } else if (job.error) {
    parts.push(h("p", { class: "answer error", style: "white-space:pre-wrap" }, job.error));
  } else if (job.result) {
    const r = job.result;
    parts.push(h("p", { class: "tag good", style: "align-self:flex-start" }, job.kind === "study"
      ? `${r.teacher} が ${r.added} 件の練習問題を解きました`
      : `${r.name} を作成しました(${num(r.seconds)}秒、計測 ${r.bench_correct}/${r.bench_n} 正解`
        + (r.base_n ? `、学習前の基礎モデルは ${r.base_correct}/${r.base_n}` : "") + ")"));
    if (job.kind === "train" && r.export_error) parts.push(h("p", { class: "note" }, "Ollama への書き出しはできませんでした: " + r.export_error));
  }
  if (own.models.length) {
    parts.push(h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "名前"), h("th", {}, "基礎モデル"), h("th", { class: "num" }, "学習データ"), h("th", { class: "num" }, "検証損失(前→後)"), h("th", { class: "num" }, "学習時間"), h("th", {}, "作成"))),
      h("tbody", {}, own.models.slice().reverse().map((m) => h("tr", {},
        h("td", {}, m.name, m.ollama ? h("span", { class: "tag", style: "margin-left:6px" }, "Ollama") : null), h("td", {}, h("span", { class: "path" }, m.base)),
        h("td", { class: "num" }, m.adapter === false ? "学習前" : `${m.examples} 件`),
        h("td", { class: "num" }, m.eval_loss == null ? "-" : `${m.base_eval_loss} → ${m.eval_loss}`), h("td", { class: "num" }, secs(m.seconds)),
        h("td", {}, new Date(m.created * 1000).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }))))))));
  }
  const keep = $("own-per-role") ? $("own-per-role").value : null;
  panel.replaceChildren(...parts);
  if (keep) perRole.value = keep;
  clearTimeout(ownTimer);
  if (busy && !$("view-settings").hidden) ownTimer = setTimeout(refreshOwn, 1500);
  else if (!busy && job.kind === "train" && job.result && !STATE.models.some((m) => m.name === job.result.model)) loadSettings();
}

function switchRow(key, title, desc) {
  const sw = h("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(!!STATE.settings[key]), "aria-label": title });
  sw.addEventListener("click", async () => {
    const next = sw.getAttribute("aria-checked") !== "true";
    try { await api("/api/settings", { [key]: next }); sw.setAttribute("aria-checked", String(next)); STATE.settings[key] = next; }
    catch (e) { toast(e.message); }
  });
  return h("div", { class: "setting" }, h("b", {}, title), h("p", {}, desc), h("div", { class: "ctl" }, sw));
}

// あなたに合わせる: AIへの指示と、覚えていること(スマホの中のAIにも同期される)
function personalPanel() {
  const limit = 1000;
  const text = h("textarea", { id: "set-instructions", rows: 4, maxlength: limit, "aria-label": "AIへの指示",
    placeholder: "例: 敬語は使わず、結論から短く答えて。私はエンジニアです。" });
  text.value = STATE.settings.instructions || "";
  const count = h("span", { class: "note" });
  const showCount = () => { count.textContent = `${text.value.length} / ${limit}`; };
  text.addEventListener("input", showCount);
  showCount();
  const saveText = async () => {
    try { await api("/api/settings", { instructions: text.value }); STATE.settings.instructions = text.value.trim(); toast("AIへの指示を保存しました"); }
    catch (e) { toast(e.message); }
  };
  const memories = STATE.settings.memories || [];
  const setMemories = async (next, message) => {
    try { await api("/api/settings", { memories: next }); STATE.settings.memories = next; toast(message); $("personal-panel").replaceWith(personalPanel()); }
    catch (e) { toast(e.message); }
  };
  const fact = h("input", { type: "text", id: "set-memory", maxlength: 200, placeholder: "例: 私の名前は佐藤です", "aria-label": "覚えてほしいこと" });
  const addFact = () => { const v = fact.value.trim(); if (v) setMemories([...memories, v], "覚えました"); };
  fact.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) addFact(); });
  return h("div", { class: "panel personal", id: "personal-panel" },
    h("h2", {}, "あなたに合わせる", h("small", {}, "すべての会話で使われます。スマホの中のAIにも同期されます")),
    h("div", { class: "field" }, h("b", {}, "AIへの指示"),
      h("p", { class: "note" }, "答え方の好みや、あなたについて知っておいてほしいことを書きます。"),
      text, h("div", { class: "field-row" }, count, h("button", { type: "button", class: "ghost", onclick: saveText }, "保存"))),
    h("div", { class: "field" }, h("b", {}, `覚えていること(${memories.length}件)`),
      h("p", { class: "note" }, "会話で「覚えて: 〜」と頼むと増えます。「忘れて: 〜」「何を覚えてる?」も使えます。"),
      memories.length ? h("ul", { class: "memory-list" }, memories.map((m, i) => h("li", {}, h("span", {}, m),
        h("button", { type: "button", class: "ghost", "aria-label": `「${m}」を消す`,
          onclick: () => setMemories(memories.filter((_, j) => j !== i), "消しました") }, "消す"))))
        : h("p", { class: "note empty" }, "まだありません"),
      h("div", { class: "field-row" }, fact, h("button", { type: "button", class: "ghost", onclick: addFact }, "覚える"))));
}

async function loadSettings() {
  await loadState();
  const tier = { small: "小型", medium: "中型", large: "大型" };
  const manager = h("select", { id: "set-manager", "aria-label": "管理AI" }, h("option", { value: "auto" }, "自動(最も小さいAI)"),
    STATE.models.map((m) => h("option", { value: m.name }, m.name)));
  manager.value = STATE.settings.manager_model;
  manager.addEventListener("change", async () => {
    try { await api("/api/settings", { manager_model: manager.value }); toast("管理AIを変更しました"); loadSettings(); } catch (e) { toast(e.message); }
  });
  const fewshot = h("input", { type: "number", id: "set-fewshot", min: 0, max: 8, value: STATE.settings.fewshot_k, "aria-label": "手本の件数" });
  fewshot.addEventListener("change", async () => {
    try { await api("/api/settings", { fewshot_k: Number.parseInt(fewshot.value, 10) }); toast("保存しました"); } catch (e) { toast(e.message); }
  });
  const exportOut = h("p", { class: "path" });
  const workspace = h("input", { type: "text", id: "set-workspace", value: STATE.settings.workspace || "", placeholder: "例: C:\\Users\\you\\Documents\\notes",
    "aria-label": "作業フォルダ", style: "flex:1;min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px" });
  const saveWorkspace = async () => {
    try { await api("/api/settings", { workspace: workspace.value.trim() }); toast(workspace.value.trim() ? "作業フォルダを設定しました" : "作業フォルダを解除しました"); loadSettings(); }
    catch (e) { toast(e.message); }
  };
  workspace.addEventListener("keydown", (e) => { if (e.key === "Enter") saveWorkspace(); });
  $("settings-body").replaceChildren(
    personalPanel(),
    h("div", { class: "panel" }, h("h2", {}, "利用できるAI", h("small", {}, "Ollama に入っているモデルを自動で検出します")),
      STATE.error ? h("p", { class: "answer error" }, STATE.error)
        : h("div", { class: "models" }, STATE.models.map((m) => h("div", { class: "model" }, h("b", {}, m.name),
          h("div", { class: "row" }, h("span", {}, `${m.size_gb.toFixed(1)} GB`), h("span", { class: "tag" }, tier[m.tier] || m.tier),
            m.own ? h("span", { class: "tag accent" }, "自作") : null,
            m.name === STATE.manager ? h("span", { class: "tag gold" }, "管理AI") : null))))),
    h("div", { class: "panel", id: "own-panel" }),
    h("div", { class: "panel" }, h("h2", {}, "学習と動作"),
      h("div", {},
        h("div", { class: "setting" }, h("b", {}, "管理AI"), h("p", {}, "タスクの振り分け・分解・検証を担当するAIです。"), h("div", { class: "ctl" }, manager)),
        switchRow("cache", "キャッシュ", "同じタスクには、以前の回答を計算なしで返します。"),
        switchRow("learned_routing", "振り分けの学習", "管理AIの振り分けを覚え、自信のあるタスクは管理AIを呼ばずに振り分けます。"),
        switchRow("semantic_search", "意味で手本を探す", STATE.embed_model
          ? `言い回しが違っても同じ趣旨の過去の回答を手本にします(埋め込みモデル: ${STATE.embed_model})。`
          : "埋め込みモデルが見つからないため、文字の近さで探しています。Ollama に bge-m3 などを入れると有効になります。"),
        switchRow("swarm_auto", "自動で分担", "長く複合的なタスクを、管理AIの判断で分解して複数のAIに分担させます。"),
        switchRow("cascade", "自信のない回答は答え直す", "速いAIの回答の出だしを確かめ、自信がない・言語を取り違えたときは、計測で最も正確なAIが答え直します。答え直す境目は、精度の計測結果から学びます。"),
        h("div", { class: "setting" }, h("b", {}, "手本の件数"), h("p", {}, "「良い」と評価した過去の回答を、似たタスクの手本として何件まで渡すか(0で無効)。"), h("div", { class: "ctl" }, fewshot)))),
    h("div", { class: "panel" }, h("h2", {}, "エージェントの道具", h("small", {}, "会話画面で「エージェント」をオンにしたときに使われます")),
      h("p", { class: "note" }, "計算と、専門のAIへの依頼はいつでも使えます。作業フォルダを設定すると、その中のファイルを扱えます(フォルダの外には届きません)。ファイルを書き換える・消す前には、必ずバックアップを取ります。"),
      h("div", { class: "composer-row" }, workspace, h("button", { type: "button", class: "ghost", onclick: saveWorkspace }, "設定")),
      h("div", {},
        switchRow("tools_write", "ファイルの変更を許可", "作業フォルダの中のファイルを作成・編集・移動・削除できるようにします。元の内容はバックアップに残り、元に戻せます。"),
        switchRow("tools_run", "コマンドと Python の実行を許可", "テストの実行やプログラムの動作確認などのため、コマンドや AI が書いたコードをこの PC で実行します。あなたの権限で動くため、内容を信頼できる用途でだけオンにしてください。"),
        switchRow("tools_pc", "PC の操作を許可", STATE.pc_supported ? "アプリを開く、文字を入力する、クリックする、画面を見るなどの操作をできるようにします。入力の前に送り先のウィンドウを確かめます。" : "この PC では使えません。"),
        switchRow("tools_net", "ネットの利用を許可", "Wikipedia で調べたり、指定されたウェブページを読んだりできるようにします。事実の質問で回答に自信がないときも、Wikipedia で確かめてから答えます(出典を表示します)。"),
        switchRow("tools_confirm", "影響のある操作の前に確認する", "ファイルの変更・コマンドの実行・PC の操作の前に、内容を見せて許可を求めます(スマホからも許可できます)。オフにすると確認せずに実行します。"))),
    h("div", { class: "panel" }, h("h2", {}, "スマホ連携"),
      h("div", {},
        switchRow("remote_enabled", "スマホからの接続を受け付ける", "Tailscale のアドレスで、登録したスマホからの接続を受け付けます。登録していない端末は使えません。"),
        switchRow("remote_control", "スマホからの PC 操作を許可", "登録したスマホから、この PC の画面を見たり、クリックや文字入力をしたりできるようにします。"),
        switchRow("keep_running", "ウィンドウを閉じても動き続ける", "画面を閉じても裏で動き続け、スマホから使えるようにします(次回の起動から有効)。"))),
    h("div", { class: "panel" }, h("h2", {}, "自作AIの学習用データ"),
      h("p", { class: "note" }, "悪い評価を除いた実行履歴を、モデルの学習に使えるチャット形式(JSONL)で書き出します。"),
      h("div", {}, h("button", { type: "button", class: "ghost", onclick: async () => {
        try { const r = await api("/api/export", {}); exportOut.textContent = `${r.count}件を書き出しました: ${r.path}`; } catch (e) { toast(e.message); }
      } }, "書き出す")),
      exportOut,
      h("p", { class: "note" }, "学習データの保存先: ", h("span", { class: "path" }, STATE.home))));
  refreshOwn();
}


// ---- スマホ連携 ----
let pairing = null, pairTimer = null;

function stateLabel(ts) {
  return { Running: "接続中", NeedsLogin: "ログインが必要", Stopped: "停止中", Starting: "起動中", NoState: "起動中", missing: "未インストール", error: "状態を取得できません" }[ts.state] || ts.state;
}

async function startPairing() {
  try { pairing = await api("/api/pairing/start", {}); loadPhone(); } catch (e) { toast(e.message); }
}

function pairingPanel(remote) {
  const panel = h("div", { class: "panel" }, h("h2", {}, "スマホを登録", h("small", {}, "登録した端末だけが、この PC につながります")));
  if (!pairing || pairing.expires * 1000 < Date.now()) {
    pairing = null;
    panel.append(h("p", { class: "note" }, "スマホの Celestial アプリと、この PC をつなぎます。コードは10分間・1回だけ有効です。"),
      h("div", {}, h("button", { type: "button", class: "primary", onclick: startPairing, disabled: !remote.listening.length }, "接続用のコードを出す")));
    if (!remote.listening.length) panel.append(h("p", { class: "note" }, "Tailscale がつながると使えるようになります。"));
    return panel;
  }
  const qr = h("div", { class: "qr" });
  if (pairing.qr_svg) qr.innerHTML = pairing.qr_svg;  // サーバーが生成した SVG(図形だけ)
  const left = h("span", { class: "note" });
  const tick = () => {
    const sec = Math.max(0, Math.round(pairing.expires - Date.now() / 1000));
    left.textContent = `残り ${Math.floor(sec / 60)}分${String(sec % 60).padStart(2, "0")}秒`;
    if (sec <= 0) { clearInterval(pairTimer); pairing = null; if (currentView === "phone") loadPhone(); }
  };
  clearInterval(pairTimer); pairTimer = setInterval(tick, 1000); tick();
  const address = (remote.urls[0] || "").replace("http://", "");
  panel.append(h("div", { class: "pair-grid" }, qr,
    h("div", { class: "pair-steps" },
      h("div", { class: "pair-code" }, pairing.code.slice(0, 3) + " " + pairing.code.slice(3)), left,
      h("ol", {},
        h("li", {}, "スマホのカメラでQRコードを読み取る(Tailscale をオンにしておく)"),
        h("li", {}, "開いたページで「Celestial アプリで接続する」を押す(アプリが未導入なら、そのページから入れられます)"),
        h("li", {}, "または、アプリの「PC と接続」でアドレス ", h("code", {}, address), " と上のコードを入力")),
      h("div", {}, h("button", { type: "button", class: "ghost small", onclick: startPairing }, "新しいコードを出す")))));
  return panel;
}

async function loadPhone() {
  const [dev, phoneModels] = await Promise.all([api("/api/devices"), api("/api/phone/models").catch(() => ({ models: [] }))]);
  const remote = dev.remote, ts = remote.tailscale;
  const online = (ts.peers || []).filter((p) => p.ip && p.os === "android");
  const statusPanel = h("div", { class: "panel" }, h("h2", {}, "接続の状態"),
    h("div", { class: "kv" },
      h("span", {}, "Tailscale"), h("span", {}, h("span", { class: "tag " + (ts.state === "Running" ? "good" : "bad") }, stateLabel(ts))),
      h("span", {}, "この PC のアドレス"), h("span", {}, ts.ips.length ? [...ts.ips, ts.dns].filter(Boolean).map((a) => h("code", {}, a)) : "なし"),
      h("span", {}, "スマホからの受付"), h("span", {}, remote.listening.length ? remote.listening.map((a) => h("code", {}, a)) : h("span", { class: "tag bad" }, "停止中")),
      h("span", {}, "Tailscale 上の Android 端末"), h("span", {}, online.length ? online.map((p) => h("span", { class: "tag " + (p.online ? "good" : "") }, `${p.name}${p.online ? "" : "(オフライン)"}`)) : "見つかりません")),
    remote.error ? h("p", { class: "answer error" }, remote.error) : null,
    !remote.enabled ? h("p", { class: "note" }, "「モデルと設定」でスマホからの接続が無効になっています。") : null,
    ts.state !== "Running" ? h("p", { class: "note" }, "Tailscale アプリを開いてログインし、接続をオンにしてください。つながると自動で受付を始めます。") : null,
    remote.listening.length ? h("p", { class: "note" }, "初回は Windows のファイアウォールの確認が出ることがあります。プライベート ネットワークで「許可」を選んでください。") : null);

  const apkUrl = remote.urls[0] ? remote.urls[0] + "/app/celestial.apk" : null;
  let apkPanel = null;
  await loadState().catch(() => {});
  if (STATE.apk) {
    apkPanel = h("div", { class: "panel" }, h("h2", {}, "スマホ用アプリ", h("small", {}, "Android")),
      h("p", { class: "note" }, "スマホで次のアドレスを開くと、アプリをダウンロードできます(提供元不明のアプリのインストールを許可する必要があります)。"),
      apkUrl ? h("code", {}, apkUrl) : h("p", { class: "note" }, "Tailscale がつながるとアドレスが表示されます。"));
  }

  const deviceRows = dev.devices.map((d) => {
    const revoke = h("button", { type: "button", class: "ghost small danger" }, "登録を取り消す");
    let armed = false;
    revoke.addEventListener("click", async () => {
      if (!armed) { armed = true; revoke.textContent = "もう一度押すと取り消し"; setTimeout(() => { armed = false; revoke.textContent = "登録を取り消す"; }, 3000); return; }
      try { await api(`/api/devices/${d.id}/revoke`, {}); toast(`${d.name} の登録を取り消しました`); loadPhone(); refreshLinkStatus(); } catch (e) { toast(e.message); }
    });
    return h("div", { class: "device" },
      h("div", {}, h("b", {}, d.name), h("div", { class: "note" }, `${d.platform || "端末"}・登録 ${new Date(d.created * 1000).toLocaleDateString("ja-JP")}・最終接続 ${d.last_seen ? new Date(d.last_seen * 1000).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "なし"}${d.last_ip ? "(" + d.last_ip + ")" : ""}`)),
      revoke);
  });
  const devicesPanel = h("div", { class: "panel" }, h("h2", {}, "登録済みの端末"),
    deviceRows.length ? h("div", { class: "devices" }, deviceRows) : h("p", { class: "empty" }, "まだ登録された端末はありません。"));

  const target = h("select", { id: "send-target", "aria-label": "送り先" }, h("option", { value: "" }, "すべての端末"), dev.devices.map((d) => h("option", { value: d.id }, d.name)));
  const kind = h("select", { id: "send-kind", "aria-label": "種類" }, h("option", { value: "text" }, "文字"), h("option", { value: "url" }, "URL"), h("option", { value: "clipboard" }, "クリップボードへ"));
  const text = h("textarea", { id: "send-text", rows: 3, class: "input", placeholder: "スマホに送る文字や URL" });
  const sendPanel = h("div", { class: "panel" }, h("h2", {}, "スマホに送る"),
    text, h("div", { class: "composer-row" }, target, kind, h("span", { class: "grow" }),
      h("button", { type: "button", class: "primary", onclick: async () => {
        if (!text.value.trim()) return;
        try { await api("/api/send", { target: target.value || null, kind: kind.value, text: text.value }); toast("送りました"); text.value = ""; } catch (e) { toast(e.message); }
      } }, "送る")));

  const inboxPanel = h("div", { class: "panel" }, h("h2", {}, "スマホから届いたもの", h("small", {}, "この画面を開いている間に届いたもの")),
    inbox.length ? h("div", { class: "hist" }, inbox.slice(0, 20).map((item) => h("div", { class: "hist-row" },
      h("div", { class: "task" }, item.kind === "url" ? h("a", { href: item.text, target: "_blank", rel: "noreferrer" }, item.text) : item.text),
      h("div", { class: "meta" }, h("span", { class: "tag" }, { text: "文字", url: "URL", clipboard: "クリップボード", notify: "通知" }[item.kind] || item.kind), h("span", {}, item.sender || ""), h("span", {}, when(item.at))),
      h("div", { class: "acts" }, h("button", { type: "button", class: "rate", onclick: (e) => copy(item.text, e.target) }, "コピー")))))
      : h("p", { class: "empty" }, "まだありません。スマホのアプリから文字や URL、クリップボードを送れます。"));

  const modelsPanel = h("div", { class: "panel" }, h("h2", {}, "スマホに入れられるAI", h("small", {}, "スマホのアプリからダウンロードし、スマホの中だけで動かします")),
    phoneModels.models.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "モデル"), h("th", { class: "num" }, "大きさ"), h("th", {}, "形式"), h("th", { class: "num" }, "計測の正解率"), h("th", {}, ""))),
      h("tbody", {}, phoneModels.models.map((m) => h("tr", {},
        h("td", {}, m.name), h("td", { class: "num" }, (m.file_size / 1e9).toFixed(2) + " GB"), h("td", {}, `${m.family} ${m.parameters} ${m.quantization}`),
        h("td", { class: "num" }, m.accuracy == null ? "-" : pct(m.accuracy)),
        h("td", {}, m.recommended ? h("span", { class: "tag gold" }, "おすすめ") : m.own ? h("span", { class: "tag accent" }, "自作") : null))))))
      : h("p", { class: "empty" }, "配れるモデルがありません。"));

  const filesPanel = h("div", { class: "panel" }, h("h2", {}, "共有フォルダ"),
    h("p", { class: "note" }, "スマホのアプリから、このフォルダのファイルを見たり、受け取ったり、送ったりできます(作業フォルダを設定すると、そちらが使われます)。"),
    h("code", {}, STATE.files_root || ""));

  $("phone-body").replaceChildren(statusPanel, pairingPanel(remote), devicesPanel, h("div", { class: "cols" }, sendPanel, inboxPanel), apkPanel, modelsPanel, filesPanel);
}

// ---- 起動 ----
async function boot() {
  try { await loadState(); } catch (e) {
    $("status").className = "status ng";
    $("status").lastElementChild.textContent = "接続できません";
    toast(e.message);
  }
  await loadThreads().catch((e) => toast(e.message));
  let last = null;
  try { last = localStorage.getItem("celestial-conversation"); } catch {}
  if (last && threads.some((t) => t.id === last)) await openConversation(last).catch(() => newConversation());
  else newConversation();
  connectEvents();
  refreshLinkStatus();
  setInterval(refreshLinkStatus, 30000);
}
boot();
