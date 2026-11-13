"use strict";
// ---- 判断(Jev のような System 1): 文章を作らずに、選択肢から1つを確率つきで選ぶ ----
// app.js の h / $ / api / toast / when / secs / pct / loaders を使う。

let decideTimer = null;

async function loadDecide() {
  const body = $("decide-body");
  if (!body.dataset.ready) {
    body.dataset.ready = "1";
    body.replaceChildren(decideTryPanel(), h("div", { class: "panel", id: "decide-calibration" }));
  }
  await refreshCalibration();
}

function decideTryPanel() {
  const question = h("input", { type: "text", id: "decide-q", placeholder: "質問(例: この問い合わせの緊急度は?)", "aria-label": "質問" });
  const options = h("textarea", { rows: 3, id: "decide-options", placeholder: "選択肢(1行に1つ。空なら はい/いいえ)", "aria-label": "選択肢" });
  const context = h("textarea", { rows: 3, id: "decide-context", placeholder: "判断の材料(メール・文章など)", "aria-label": "判断の材料" });
  const out = h("div", { class: "decide-out" });
  const go = h("button", { type: "button", class: "primary" }, "判断する");
  go.addEventListener("click", async () => {
    if (!question.value.trim()) return toast("質問を書いてください");
    go.disabled = true;
    try {
      const r = await api("/api/decide", { question: question.value, options: options.value, context: context.value });
      const entries = Object.entries(r.distribution).sort((a, b) => b[1] - a[1]);
      out.replaceChildren(
        h("p", {}, h("b", {}, r.answer), `(確率 ${pct(r.probability)}・${r.model}・${secs(r.latency_s)})`,
          r.calibrated ? null : h("span", { class: "note" }, " このAIは確率の分布を返さないため、選んだものを 100% としています")),
        h("div", { class: "decide-bars" }, entries.map(([name, p], i) => h("div", { class: "decide-bar" + (i === 0 ? " top" : "") },
          h("b", {}, name), h("div", { class: "track" }, h("div", { class: "fill", style: `width:${(p * 100).toFixed(1)}%` })), h("span", {}, pct(p))))));
    } catch (e) { toast(e.message); }
    finally { go.disabled = false; }
  });
  return h("div", { class: "panel" },
    h("h2", {}, "判断を試す", h("small", {}, "文章を作らずに、選択肢から1つを確率つきで選びます(AIは記号を1語だけ答えるので速い)")),
    h("div", { class: "decide-form" }, h("div", { class: "wide" }, question), options, context),
    h("div", { class: "field-row" }, h("span", { class: "note" }, "自動化の「判断する」手順や、POST /api/decide からも使えます。"), go),
    out);
}

async function refreshCalibration() {
  const panel = $("decide-calibration");
  if (!panel) return;
  const r = await api("/api/decide/calibration");
  const job = r.job, busy = job.running && job.kind === "calibrate";
  const rows = Object.entries(r.models).sort((a, b) => b[1].accuracy - a[1].accuracy);
  const start = h("button", { type: "button", class: "ghost", disabled: busy || job.running }, busy ? "較正しています…" : "較正する");
  start.addEventListener("click", async () => {
    try { await api("/api/decide/calibrate", {}); toast("較正を始めました"); refreshCalibration(); } catch (e) { toast(e.message); }
  });
  panel.replaceChildren(...[
    h("div", { class: "field-row" }, h("h2", {}, "確率の較正", h("small", {}, `いま判断に使うAI: ${r.current || "-"}`)), start),
    h("p", { class: "note" }, "AIの確率は、そのままだと自信過剰になりがちです。正解の分かっている判断(文章の意味を読み取る30問と、担当の振り分け55問)をAIごとに解かせ、正答率と温度を学びます。以後の判断は、意味の判断で最も正確だったAIを、学んだ温度で使います。"),
    busy ? h("p", { class: "note" }, `${job.done} / ${job.total}  ${job.message || ""}`) : null,
    job.kind === "calibrate" && job.error ? h("p", { class: "answer error" }, job.error) : null,
    rows.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "AI"), h("th", { class: "num" }, "意味の判断"), h("th", { class: "num" }, "振り分け"), h("th", { class: "num" }, "較正誤差(前→後)"),
        h("th", { class: "num" }, "温度"), h("th", { class: "num" }, "1回の時間"), h("th", {}, "較正した日時"))),
      h("tbody", {}, rows.map(([name, e]) => h("tr", {},
        h("td", {}, name, name === r.current ? h("span", { class: "tag gold", style: "margin-left:6px" }, "使用中") : null),
        h("td", { class: "num" }, `${pct(e.accuracy)}(${Math.round(e.accuracy * e.n)}/${e.n})`),
        h("td", { class: "num" }, e.routing_n ? `${pct(e.routing_accuracy)}(${Math.round(e.routing_accuracy * e.routing_n)}/${e.routing_n})` : "-"),
        h("td", { class: "num" }, `${(e.ece_raw ?? 0).toFixed(3)} → ${(e.ece ?? 0).toFixed(3)}`),
        h("td", { class: "num" }, (e.temperature ?? 1).toFixed(2)),
        h("td", { class: "num" }, secs(e.latency_s || 0)),
        h("td", {}, e.at ? when(e.at) : "-")))))) : h("p", { class: "note empty" }, "まだ較正していません。"),
  ].filter(Boolean));  // replaceChildren は null を「null」という文字にしてしまうので除く
  clearTimeout(decideTimer);
  if (busy && currentView === "decide") decideTimer = setTimeout(() => refreshCalibration().catch(() => {}), 1200);
}

loaders.decide = () => loadDecide();
