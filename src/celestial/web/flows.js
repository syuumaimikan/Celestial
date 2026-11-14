"use strict";
// ---- 自動化(n8n のようなワークフロー): 一覧・つないだ手順の編集・AIで作る・実行の記録 ----
// app.js の h / $ / api / toast / when / secs / currentView を使う。

const FLOW_ICONS = {
  manual: "M7 5l8 5-8 5z",
  schedule: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM10 6v4l3 2",
  interval: "M4 10a6 6 0 0 1 10.5-4M16 10a6 6 0 0 1-10.5 4M14.5 3v3h-3M5.5 17v-3h3",
  webhook: "M8 12a3 3 0 1 1 1.5-5.6L12 11M12 8a3 3 0 1 1 2.6 4.5H9M6.5 14.5A3 3 0 1 1 9 10",
  file: "M3 6.5A1.5 1.5 0 0 1 4.5 5H8l1.5 2h6A1.5 1.5 0 0 1 17 8.5v6a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 3 14.5z",
  ai: "M10 2.5l1.6 4.4L16 8.5l-4.4 1.6L10 14.5l-1.6-4.4L4 8.5l4.4-1.6zM15.5 13l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z",
  decide: "M10 3v14M5 17h10M4 7h12M4 7l-2 5h4zM16 7l-2 5h4z",
  news: "M3 5h11v10a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1V7h-3M6 8h5M6 11h5M6 14h3",
  web_search: "M8.5 3.5a5 5 0 1 0 0 10a5 5 0 0 0 0-10zM12.5 12.5L17 17",
  fetch: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM3 10h14M10 3c2 2 2.8 4.4 2.8 7S12 15 10 17M10 3C8 5 7.2 7.4 7.2 10S8 15 10 17",
  post: "M3 10l14-6-5 14-2.5-5.5zM9.5 12.5L17 4",
  kb_search: "M4 4h5a2 2 0 0 1 2 2v10a1.5 1.5 0 0 0-1.5-1.5H4zM16 4h-5a2 2 0 0 0-2 2v10a1.5 1.5 0 0 1 1.5-1.5H16z",
  tool: "M13.5 3.5a3 3 0 0 0-4 3.8L4 12.8V16h3.2l5.5-5.5a3 3 0 0 0 3.8-4l-2 2-1.5-1.5z",
  transform: "M3.5 6h5l-2-2m2 2-2 2M16.5 14h-5l2 2m-2-2 2-2",
  read_file: "M5 2.5h6.5L15 6v11.5H5zM11.5 2.5V6H15M7.5 10h5M7.5 13h5",
  write_file: "M4 16l1-4 8.5-8.5a1.4 1.4 0 0 1 2 2L7 14zM12.5 4.5l3 3",
  document: "M5 2.5h6.5L15 6v11.5H5zM11.5 2.5V6H15M7.5 9.5h5M7.5 12h5M7.5 14.5h3",
  command: "M3 4.5h14v11H3zM6 8l2.5 2L6 12M10 12.5h4",
  python: "M7 4.5h5a2 2 0 0 1 2 2V9H6v2.5H4V6.5a2 2 0 0 1 2-2zM13 15.5H8a2 2 0 0 1-2-2V11h8V8.5h2v5a2 2 0 0 1-2 2z",
  for_each: "M4 5.5h12M4 10h12M4 14.5h7M14.5 12l2.5 2.5-2.5 2.5",
  notify: "M6 8a4 4 0 0 1 8 0c0 4 1.5 5 1.5 5h-11S6 12 6 8zM8.5 15.5a1.5 1.5 0 0 0 3 0",
  condition: "M10 2.5l7.5 7.5-7.5 7.5L2.5 10z",
  template: "M4 5h12M4 8.5h12M4 12h8M4 15.5h10",
  wait: "M6 3h8M6 17h8M6.5 3c0 4 7 4 7 7s-7 3-7 7M13.5 3c0 4-7 4-7 7s7 3 7 7",
};
const FLOW_STATUS = { done: ["成功", "good"], error: ["失敗", "bad"], stopped: ["条件で停止", ""], running: ["実行中", "accent"] };
let FLOWS = null;       // /api/flows の結果
let flowEditing = null; // 編集中の流れ(null なら一覧)
let flowHistory = null; // 記録を見ている流れの id

function flowIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("class", "flow-icon");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", FLOW_ICONS[name] || FLOW_ICONS.template);
  svg.append(path);
  return svg;
}

async function loadFlows() {
  FLOWS = await api("/api/flows");
  renderFlows();
}

function renderFlows() {
  const body = $("flows-body");
  if (!FLOWS) return;
  if (flowEditing) return body.replaceChildren(flowEditor(flowEditing));
  if (flowHistory) return body.replaceChildren(flowRunsPanel(flowHistory));
  body.replaceChildren(flowBuilderPanel(), flowListPanel());
}

// ---- AIで作る ----
function flowBuilderPanel() {
  const text = h("textarea", { rows: 2, id: "flow-describe", placeholder: "例: 毎朝8時にニュースのページを読んで、3行にまとめてスマホに通知して", "aria-label": "作りたい自動化" });
  const go = h("button", { type: "button", class: "primary" }, "AIで下書きを作る");
  go.addEventListener("click", async () => {
    if (!text.value.trim()) return toast("作りたい自動化を書いてください");
    go.disabled = true; go.textContent = "作っています…";
    try { const r = await api("/api/flows/generate", { description: text.value }); flowEditing = r.flow; renderFlows(); toast("下書きを作りました。内容を確かめて保存してください"); }
    catch (e) { toast(e.message); }
    finally { go.disabled = false; go.textContent = "AIで下書きを作る"; }
  });
  const tplPick = (FLOWS && FLOWS.templates && FLOWS.templates.length)
    ? h("div", { class: "flow-templates" }, h("span", { class: "note" }, "ひな形から作る:"), " ",
        FLOWS.templates.map((t) => h("button", { type: "button", class: "chip", onclick: () => {
          flowEditing = JSON.parse(JSON.stringify({ name: t.name, enabled: false, trigger: t.trigger, steps: t.steps }));
          renderFlows();
        } }, t.name)))
    : null;
  return h("div", { class: "panel flow-builder" },
    h("h2", {}, "AIで作る", h("small", {}, "やりたいことを書くと、きっかけと手順の下書きを作ります")),
    text,
    tplPick,
    h("div", { class: "field-row" },
      h("span", { class: "note" }, "下書きは止めた状態で作ります。内容を確かめてから有効にしてください。"),
      h("span", {}, h("button", { type: "button", class: "ghost", onclick: () => { flowEditing = blankFlow(); renderFlows(); } }, "自分で作る"), " ", go)));
}

function blankFlow() {
  return { name: "", enabled: true, trigger: { type: "manual" }, steps: [{ type: "ai", label: "", prompt: "" }] };
}

// ---- 一覧 ----
function flowListPanel() {
  const flows = FLOWS.flows;
  const panel = h("div", { class: "panel" }, h("h2", {}, "自動化", h("small", {}, `${flows.length}件`)));
  if (!flows.length) {
    panel.append(h("p", { class: "note empty" }, "まだありません。上の「AIで作る」か「自分で作る」から始めてください。"));
    return panel;
  }
  for (const f of flows) {
    const last = f.last_run ? FLOW_STATUS[f.last_run.status] || [f.last_run.status, ""] : null;
    const sw = h("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(!!f.enabled), "aria-label": `${f.name}を有効にする` });
    sw.addEventListener("click", async () => {
      try { await api(`/api/flows/${f.id}/enabled`, { enabled: !f.enabled }); await loadFlows(); } catch (e) { toast(e.message); }
    });
    panel.append(h("div", { class: "flow-row" },
      h("div", { class: "flow-row-main" },
        h("div", { class: "flow-row-title" }, flowIcon(f.trigger.type), h("b", {}, f.name)),
        h("div", { class: "flow-row-meta" },
          h("span", { class: "tag" }, f.summary),
          h("span", { class: "flow-chain" }, f.steps.map((s) => flowIcon(s.type))),
          last ? h("span", { class: "tag " + last[1] }, `${last[0]}・${when(f.last_run.started)}`) : h("span", { class: "note" }, "まだ動かしていません"),
          f.running ? h("span", { class: "tag accent" }, "実行中") : null,
          f.next_run ? h("span", { class: "note" }, "次: " + new Date(f.next_run * 1000).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })) : null)),
      h("div", { class: "flow-row-actions" },
        sw,
        h("button", { type: "button", class: "ghost", onclick: () => runFlow(f) }, "実行"),
        h("button", { type: "button", class: "ghost", onclick: () => { flowEditing = JSON.parse(JSON.stringify(f)); renderFlows(); } }, "編集"),
        h("button", { type: "button", class: "ghost", onclick: () => { flowHistory = f.id; renderFlows(); } }, "記録"))));
  }
  return panel;
}

async function runFlow(f, input = "") {
  try {
    await api(`/api/flows/${f.id}/run`, { input });
    toast(`「${f.name}」を動かしています`);
    setTimeout(() => loadFlows().catch(() => {}), 400);
  } catch (e) { toast(e.message); }
}

// ---- 編集(きっかけと手順を上から順につないだ形) ----
function flowEditor(flow) {
  const allowed = FLOWS.allowed || {};
  const name = h("input", { type: "text", value: flow.name || "", placeholder: "自動化の名前", "aria-label": "自動化の名前", class: "flow-name" });
  name.addEventListener("input", () => (flow.name = name.value));
  let focused = null;
  const vars = h("div", { class: "flow-vars" }, h("span", { class: "note" }, "差し込める値:"),
    ["{{prev}}", "{{trigger}}", "{{date}}", "{{time}}", ...flow.steps.map((_, i) => `{{step${i + 1}}}`)].map((v) =>
      h("button", { type: "button", class: "chip", onclick: () => insertVar(v) }, v)));
  function insertVar(v) {
    if (!focused) return toast("差し込む欄を先に選んでください");
    const { selectionStart: a, selectionEnd: b, value } = focused;
    focused.value = value.slice(0, a) + v + value.slice(b);
    focused.dispatchEvent(new Event("input"));
    focused.focus();
    focused.selectionStart = focused.selectionEnd = a + v.length;
  }
  function field(target, key, kind, label, options) {
    let el;
    if (kind === "select") {
      el = h("select", { "aria-label": label }, Object.entries(options).map(([v, t]) => h("option", { value: v }, t)));
      el.value = target[key] || Object.keys(options)[0];
      target[key] = el.value;
      el.addEventListener("change", () => { target[key] = el.value; });
    } else if (kind === "area") {
      el = h("textarea", { rows: 3, "aria-label": label, placeholder: label });
      el.value = target[key] || "";
    } else {
      el = h("input", { type: kind === "number" ? "number" : "text", "aria-label": label, placeholder: label, value: target[key] ?? "" });
    }
    if (kind !== "select") {
      el.addEventListener("input", () => { target[key] = kind === "number" ? el.value : el.value; });
      el.addEventListener("focus", () => (focused = el));
    }
    return h("label", { class: "flow-field" }, h("span", {}, label), el);
  }

  // きっかけ
  const trigger = flow.trigger;
  const triggerFields = h("div", { class: "flow-fields" });
  function drawTrigger() {
    const parts = [];
    if (trigger.type === "schedule") {
      trigger.time = trigger.time || "08:00";
      trigger.days = trigger.days || [0, 1, 2, 3, 4, 5, 6];
      const time = h("input", { type: "time", value: trigger.time, "aria-label": "時刻" });
      time.addEventListener("input", () => (trigger.time = time.value));
      parts.push(h("label", { class: "flow-field" }, h("span", {}, "時刻"), time),
        h("div", { class: "flow-field" }, h("span", {}, "曜日"), h("div", { class: "day-chips" }, [..."月火水木金土日"].map((d, i) => {
          const on = trigger.days.includes(i);
          return h("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": String(on), onclick: () => {
            trigger.days = on ? trigger.days.filter((x) => x !== i) : [...trigger.days, i].sort();
            drawTrigger();
          } }, d);
        }))));
    } else if (trigger.type === "interval") {
      trigger.minutes = trigger.minutes || 60;
      parts.push(field(trigger, "minutes", "number", "何分ごと"));
    } else if (trigger.type === "file") {
      parts.push(field(trigger, "folder", "text", "見張るフォルダ(作業フォルダの中)"), field(trigger, "pattern", "text", "ファイル名の形(例: *.txt)"));
    } else if (trigger.type === "webhook") {
      parts.push(flow.hook_urls ? h("div", { class: "flow-field" }, h("span", {}, "送り先のアドレス(この PC と Tailscale から)"),
        flow.hook_urls.map((u) => h("code", { class: "path" }, u)),
        h("span", { class: "note" }, "POST で送った本文が {{trigger}} に入ります(JSON なら {{trigger.項目}} も)。?wait=1 を付けると結果を返します。"))
        : h("p", { class: "note" }, "保存すると、送り先のアドレスが表示されます。"));
    } else {
      parts.push(h("p", { class: "note" }, "一覧の「実行」や、スマホのアプリから動かします。"));
    }
    triggerFields.replaceChildren(...parts);
  }
  const triggerType = h("select", { "aria-label": "きっかけ" }, Object.entries(FLOWS.triggers).map(([v, t]) => h("option", { value: v }, t)));
  triggerType.value = trigger.type;
  triggerType.addEventListener("change", () => { flow.trigger = { type: triggerType.value }; Object.assign(trigger, flow.trigger); for (const k of Object.keys(trigger)) if (k !== "type") delete trigger[k]; trigger.type = triggerType.value; drawTrigger(); });
  drawTrigger();
  const nodes = [h("div", { class: "flow-node trigger" },
    h("div", { class: "flow-node-head" }, flowIcon(trigger.type), h("b", {}, "きっかけ"), triggerType), triggerFields)];

  // 手順
  const FIELDS = {
    ai: [["prompt", "area", "AIへの依頼(例: 次の文章を3行に要約して: {{prev}})"]],
    decide: [["question", "text", "質問(例: この問い合わせの緊急度は?)"], ["options", "area", "選択肢(1行に1つ。空なら はい/いいえ)"],
      ["context", "area", "判断の材料(空なら前の手順の結果。確率は {{stepN.probability}})"]],
    news: [["topic", "text", "話題(空なら主要ニュース)"], ["lang", "select", "言語", { ja: "日本語", en: "English" }], ["limit", "number", "件数(1〜15)"]],
    web_search: [["query", "text", "検索する言葉(例: {{trigger}})"], ["lang", "select", "言語", { ja: "日本語", en: "English" }], ["limit", "number", "件数(1〜15)"]],
    fetch: [["url", "text", "読むページのアドレス"]],
    post: [["url", "text", "送り先のアドレス(Slack・Discord の Webhook など)"], ["body", "area", "送る内容(JSON も可)"]],
    kb_search: [["query", "text", "知識ベースの検索語(例: {{trigger}})"], ["top_k", "number", "件数(1〜12)"]],
    tool: [["name", "text", "ツール名(例: calc, code_check, convert_file, image_edit, qr_barcode, web_extract)"],
      ["args", "area", "引数(JSON形式。例: {\"path\": \".\"})"]],
    transform: [["action", "select", "処理", { json_get: "JSONキー取得", regex: "正規表現抽出", lines: "空行除去・整形", split: "区切り分割", trim: "前後空白削除", upper: "大文字化", lower: "小文字化", count: "文字/行数カウント" }],
      ["input", "area", "入力(空なら前の手順の結果 {{prev}})"],
      ["arg", "text", "キーパス(a.b.0)・正規表現パターン・区切り文字"]],
    read_file: [["path", "text", "ファイル(作業フォルダの中)"]],
    write_file: [["path", "text", "ファイル(作業フォルダの中)"], ["content", "area", "書く内容"], ["mode", "select", "書き方", { overwrite: "上書き", append: "末尾に追記" }]],
    document: [["kind", "select", "種類", { docx: "Word", xlsx: "Excel", pptx: "PowerPoint" }], ["path", "text", "ファイル名"],
      ["mode", "select", "書き方(Word/PPTX)", { overwrite: "上書き", append: "末尾に追記" }], ["content", "area", "内容(Markdown。Excel は CSV や表)"]],
    command: [["command", "text", "コマンド(作業フォルダで実行)"]],
    python: [["code", "area", "Python コード(作業フォルダで実行)"]],
    for_each: [["items", "area", "処理する一覧(1行1件。空なら {{prev}})"], ["prompt", "area", "1件ごとの指示({{item}} に各行が入ります)"]],
    notify: [["title", "text", "題"], ["text", "area", "本文(空なら前の手順の結果)"], ["target", "select", "送り先", { all: "スマホと PC", phone: "スマホ", pc: "PC" }]],
    condition: [["value", "text", "調べる文字(例: {{prev}})"], ["op", "select", "比べ方", FLOWS.ops], ["match", "text", "比べる文字"]],
    template: [["text", "area", "文章({{prev}} などを差し込めます)"]],
    wait: [["seconds", "number", "秒(最大600)"]],
  };
  flow.steps.forEach((step, i) => {
    const spec = FLOWS.steps[step.type];
    const need = spec.needs && !allowed[spec.needs];
    const typeSel = h("select", { "aria-label": "手順の種類" }, Object.entries(FLOWS.steps).map(([v, s]) => h("option", { value: v }, s.label)));
    typeSel.value = step.type;
    typeSel.addEventListener("change", () => { flow.steps[i] = { type: typeSel.value, label: step.label || "" }; renderFlows(); });
    const label = h("input", { type: "text", value: step.label || "", placeholder: spec.label, "aria-label": "手順の名前", class: "flow-step-label" });
    label.addEventListener("input", () => (step.label = label.value));
    const move = (d) => { const j = i + d; if (j < 0 || j >= flow.steps.length) return; [flow.steps[i], flow.steps[j]] = [flow.steps[j], flow.steps[i]]; renderFlows(); };
    nodes.push(addBetween(i), h("div", { class: "flow-node step-" + step.type + (need ? " blocked" : "") },
      h("div", { class: "flow-node-head" }, h("span", { class: "flow-num" }, String(i + 1)), flowIcon(step.type), typeSel, label,
        h("span", { class: "grow" }),
        h("button", { type: "button", class: "icon-btn", title: "上へ", "aria-label": "上へ", onclick: () => move(-1) }, "↑"),
        h("button", { type: "button", class: "icon-btn", title: "下へ", "aria-label": "下へ", onclick: () => move(1) }, "↓"),
        h("button", { type: "button", class: "icon-btn", title: "この手順を消す", "aria-label": "この手順を消す", onclick: () => { flow.steps.splice(i, 1); renderFlows(); } }, "×")),
      need ? h("p", { class: "note warn" }, `この手順を動かすには、設定で${{ net: "「ネットの利用を許可」", files: "作業フォルダ", write: "「ファイルの変更を許可」", run: "「コマンドと Python の実行を許可」" }[spec.needs]}が必要です。`) : null,
      h("div", { class: "flow-fields" }, (FIELDS[step.type] || []).map(([key, kind, text, options]) => field(step, key, kind, text, options)))));
  });
  nodes.push(addBetween(flow.steps.length));
  function addBetween(index) {
    const sel = h("select", { "aria-label": "手順を足す" }, h("option", { value: "" }, "+ 手順を足す"),
      Object.entries(FLOWS.steps).map(([v, s]) => h("option", { value: v }, s.label)));
    sel.addEventListener("change", () => { if (!sel.value) return; flow.steps.splice(index, 0, { type: sel.value, label: "" }); renderFlows(); });
    return h("div", { class: "flow-link" }, sel);
  }

  const save = async (andRun) => {
    try {
      const saved = await api("/api/flows", flow);
      toast("保存しました");
      flowEditing = null;
      await loadFlows();
      if (andRun) { await runFlow(saved); flowHistory = saved.id; renderFlows(); }
    } catch (e) { toast(e.message); }
  };
  const remove = async () => {
    if (!flow.id || !confirm(`「${flow.name}」を消しますか?(実行の記録も消えます)`)) return;
    try { await api(`/api/flows/${flow.id}/delete`, {}); flowEditing = null; await loadFlows(); toast("消しました"); } catch (e) { toast(e.message); }
  };
  return h("div", { class: "panel flow-editor" },
    h("div", { class: "field-row" }, name,
      h("label", { class: "check" }, (() => { const c = h("input", { type: "checkbox" }); c.checked = flow.enabled !== false; c.addEventListener("change", () => (flow.enabled = c.checked)); return c; })(), " 有効")),
    vars,
    h("div", { class: "flow-canvas" }, nodes),
    h("div", { class: "field-row" },
      flow.id ? h("button", { type: "button", class: "ghost danger", onclick: remove }, "消す") : h("span", {}),
      h("span", {},
        h("button", { type: "button", class: "ghost", onclick: () => { flowEditing = null; renderFlows(); } }, "やめる"), " ",
        h("button", { type: "button", class: "ghost", onclick: () => save(true) }, "保存して試す"), " ",
        h("button", { type: "button", class: "primary", onclick: () => save(false) }, "保存"))));
}

// ---- 実行の記録 ----
function flowRunsPanel(flowId) {
  const flow = FLOWS.flows.find((f) => f.id === flowId);
  const panel = h("div", { class: "panel" },
    h("div", { class: "field-row" }, h("h2", {}, `記録: ${flow ? flow.name : ""}`),
      h("span", {}, flow ? h("button", { type: "button", class: "ghost", onclick: () => runFlow(flow) }, "実行") : null, " ",
        h("button", { type: "button", class: "ghost", onclick: () => { flowHistory = null; renderFlows(); } }, "一覧へ"))));
  const list = h("div", { class: "flow-runs" }, h("p", { class: "note" }, "読み込み中…"));
  panel.append(list);
  api(`/api/flows/${flowId}/runs`).then((r) => {
    if (!r.runs.length) return list.replaceChildren(h("p", { class: "note empty" }, "まだ記録がありません"));
    list.replaceChildren(...r.runs.map((run) => {
      const st = FLOW_STATUS[run.status] || [run.status, ""];
      const source = { manual: "手動", phone: "スマホから", schedule: "時刻", interval: "間隔", webhook: "Webhook", file: "ファイル" }[run.source] || run.source;
      return h("details", { class: "flow-run", open: run === r.runs[0] },
        h("summary", {}, h("span", { class: "tag " + st[1] }, st[0]), h("span", {}, when(run.started)), h("span", { class: "note" }, source),
          run.finished ? h("span", { class: "note" }, secs(run.finished - run.started)) : null),
        run.log.map((s) => h("div", { class: "flow-run-step " + s.status },
          h("div", { class: "flow-run-head" }, h("span", { class: "flow-num" }, String(s.n)), flowIcon(s.type), h("b", {}, s.label),
            h("span", { class: "note" }, secs(s.seconds || 0))),
          s.output ? h("pre", {}, s.output) : null)));
    }));
  }).catch((e) => list.replaceChildren(h("p", { class: "answer error" }, e.message)));
  return panel;
}

loaders.flows = () => { flowEditing = flowEditing || null; return loadFlows(); };
