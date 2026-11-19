/* CVE Studio front-end (vanilla JS). State lives on disk in project.json; this page polls it. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (t) => { t = Math.max(0, +t || 0); const m = Math.floor(t / 60); const s = t - m * 60; return `${m}:${s.toFixed(1).padStart(4, "0")}`; };
const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };

let PID = store.get("cve.pid");
let P = null;            // current project
let lastVersion = -1;
let selected = null;     // selected scene id
let playerMode = "source";
let briefDirty = false;

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 3800);
}
const media = (area, name) => `/media/${encodeURIComponent(PID)}/${area}/${encodeURIComponent(name)}`;

/* ---------------- projects ---------------- */
async function loadProjects() {
  const list = await api("/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = list.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}${p.scenes ? ` · ${p.scenes} scenes` : ""}</option>`).join("");
  if (!list.length) { showEmpty(true); return; }
  if (!PID || !list.some((p) => p.id === PID)) PID = list[0].id;
  sel.value = PID; showEmpty(false); await refresh(true);
}
function showEmpty(on) {
  $("#empty").classList.toggle("hidden", !on);
  ["#tabs", "#app"].forEach((s) => $(s).classList.toggle("hidden", on));
  $("#projectSelect").classList.toggle("hidden", on);
}
async function createProject(name) {
  if (!name) return toast("Type a project name first.", true);
  const p = await api("/api/projects", { method: "POST", body: { name } });
  PID = p.id; store.set("cve.pid", PID); lastVersion = -1; await loadProjects(); switchTab("footage");
}
$("#newProjectBtn").onclick = () => { const n = prompt("Project name"); if (n) createProject(n.trim()).catch((e) => toast(e.message, true)); };
$("#emptyCreate").onclick = () => createProject($("#emptyName").value.trim()).catch((e) => toast(e.message, true));
$("#projectSelect").onchange = (e) => { PID = e.target.value; store.set("cve.pid", PID); lastVersion = -1; lastSig = {}; selected = null; briefDirty = false; lastRenderFile = null; refresh(true); };

/* ---------------- tabs ---------------- */
function switchTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  store.set("cve.tab", name);
}
$$("#tabs button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
$$("[data-open]").forEach((b) => (b.onclick = () => api(`/api/projects/${PID}/open`, { method: "POST", body: { which: b.dataset.open } })));

/* ---------------- polling ---------------- */
async function refresh(force = false) {
  if (!PID) return;
  let p;
  try { p = await api(`/api/projects/${encodeURIComponent(PID)}`); } catch (e) { return; }
  const jobChanged = JSON.stringify(p.job) !== JSON.stringify(P?.job);
  if (!force && p.version === lastVersion && !jobChanged) return;
  P = p; lastVersion = p.version;
  renderAll();
}
setInterval(() => refresh(false), 2000);

let lastSig = {};
function changed(key, value) {
  // job progress bumps the project version every few seconds during renders; redrawing lists that didn't change
  // replaces the DOM under the cursor and swallows clicks – only redraw a section when ITS data changed
  const sig = JSON.stringify(value);
  if (lastSig[key] === sig) return false;
  lastSig[key] = sig; return true;
}
function renderAll() {
  renderJob();
  if (changed("src", P.sources)) renderSources();
  if (changed("refs", P.references)) renderRefs();
  renderBrief();
  if (changed("scenes", [P.scenes, P.settings, P.storyboard, P.commands.filter((c) => c.status === "claude").map((c) => c.scene), selected])) renderScenes();
  if (changed("detail", [curScene(), P.commands.length, P.commands.map((c) => c.status).join()])) renderDetail();
  if (changed("renders", P.renders)) {
    renderRenders();
    // a render just finished while the page was open -> show it in the player
    const newest = latestPreview();
    if (newest && lastRenderFile !== null && newest.file !== lastRenderFile) showRendered();
    lastRenderFile = newest ? newest.file : "";
  }
  if (changed("inbox", P.commands.map((c) => c.id + c.status + c.reply).join())) renderInbox();
}

/* ---------------- job ---------------- */
function renderJob() {
  const j = P.job, pill = $("#jobPill");
  if (!j || !j.status) { pill.classList.add("hidden"); $("#jobLog").textContent = "No activity yet."; return; }
  pill.classList.remove("hidden");
  pill.className = "pill " + (j.status === "running" || j.status === "queued" ? "running" : j.status);
  const label = { analyze: "Analyze", preview: "Preview", final: "Export", ref_download: "Download", ref_analyze: "References" }[j.kind] || j.kind;
  const waiting = (j.pending || []).length;
  pill.textContent = `${label}: ${j.status === "error" ? "failed" : j.progress || j.status}${waiting ? ` · ${waiting} more queued` : ""}`;
  pill.title = j.error || "";
  $("#jobLog").textContent = (j.log || []).join("\n") + (j.error ? `\n\n${j.error}` : "");
  const busy = j.status === "running" || j.status === "queued";
  ["#analyzeBtn", "#previewBtn", "#finalBtn"].forEach((s) => ($(s).disabled = busy));
}

/* ---------------- footage ---------------- */
function renderSources() {
  const el = $("#sourceList");
  if (!P.sources.length) { el.innerHTML = `<p class="muted">No footage yet.</p>`; return; }
  el.innerHTML = P.sources.map((s) => `
    <div class="item">
      <div>${esc(s.file)}</div>
      <span class="muted small">${s.duration ? fmt(s.duration) : "…"}</span>
      <span class="muted small">${s.width ? `${s.width}×${s.height}` : ""}${s.has_audio === false ? " · no audio" : ""}</span>
      <span class="badge ${s.analyzed ? "ok" : "warn"}">${s.analyzed ? `analyzed · ${s.words ?? 0} words` : "not analyzed"}</span>
      <button class="btn ghost small" data-rm="${esc(s.file)}">Remove</button>
    </div>`).join("");
  $$("[data-rm]", el).forEach((b) => (b.onclick = async () => {
    if (!confirm(`Remove ${b.dataset.rm} and its scenes from this project? (the file stays on disk)`)) return;
    await api(`/api/projects/${PID}/source/remove`, { method: "POST", body: { file: b.dataset.rm } }); refresh(true);
  }));
}
$("#analyzeBtn").onclick = async () => {
  try { await api(`/api/projects/${PID}/analyze`, { method: "POST", body: { reanalyze: $("#reanalyze").checked } }); toast("Analysis started – this can take a few minutes."); refresh(true); }
  catch (e) { toast(e.message, true); }
};

/* ---------------- uploads ---------------- */
function setupDrop(zoneSel, inputSel, kind) {
  const zone = $(zoneSel), input = $(inputSel);
  zone.onclick = (e) => { if (e.target.tagName !== "INPUT") input.click(); };
  input.onchange = () => upload([...input.files], kind, zone).finally(() => (input.value = ""));
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", (e) => upload([...e.dataTransfer.files], kind, zone));
}
function upload(files, kind, zone) {
  if (!files.length) return Promise.resolve();
  const fd = new FormData(); files.forEach((f) => fd.append("files", f));
  const bar = $(".progress", zone), fill = $(".progress div", zone);
  bar.classList.remove("hidden"); fill.style.width = "0";
  return new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open("POST", `/api/projects/${PID}/upload?kind=${kind}`);
    x.upload.onprogress = (e) => { if (e.lengthComputable) fill.style.width = `${(100 * e.loaded) / e.total}%`; };
    x.onload = () => {
      bar.classList.add("hidden");
      let j = {}; try { j = JSON.parse(x.responseText); } catch {}
      if (x.status >= 400) toast(j.error || "Upload failed", true);
      else toast(`Uploaded ${j.saved.length} file(s).` + (j.skipped && j.skipped.length ? ` Skipped (not a video): ${j.skipped.join(", ")}` : ""), !!(j.skipped && j.skipped.length));
      refresh(true); resolve();
    };
    x.onerror = () => { bar.classList.add("hidden"); toast("Upload failed", true); resolve(); };
    x.send(fd);
  });
}
setupDrop("#dropSource", "#fileSource", "source");
setupDrop("#dropRef", "#fileRef", "reference");

/* ---------------- references + brief ---------------- */
function renderRefs() {
  const el = $("#refList");
  if (!P.references.length) { el.innerHTML = `<p class="muted">No references yet. Add sample videos or images whose style you want.</p>`; return; }
  el.innerHTML = P.references.map((r, i) => {
    const img = r.sheet ? `<img src="${media("sheets", r.sheet)}" alt="">` : r.kind === "image" ? `<img src="${media("refs", r.file)}" alt="">` : "";
    const stats = r.kind === "video" ? `${fmt(r.duration)} · ${r.shots} shots · avg shot <b>${r.avg_shot}s</b>`
      : r.kind === "pending" ? "analysing…" : r.kind === "error" ? `<span style="color:var(--err)">could not analyse: ${esc(r.error)}</span>` : esc(r.kind);
    return `<div class="ref">${img}<div class="meta">
      <div class="name"><b>R${i + 1}</b> ${esc(r.file || r.url)}</div>
      <div class="muted">${stats}</div>
      <div class="row"><button class="btn ghost small" data-rmref="${i}">Remove</button>${r.url ? `<a class="small muted" href="${esc(r.url)}" target="_blank" rel="noopener">link</a>` : ""}</div>
    </div></div>`;
  }).join("");
  $$("[data-rmref]", el).forEach((b) => (b.onclick = async () => {
    const r = P.references[+b.dataset.rmref];
    await api(`/api/projects/${PID}/reference/remove`, { method: "POST", body: { file: r.file, url: r.url } }); refresh(true);
  }));
}
async function addRefUrl(download) {
  const url = $("#refUrl").value.trim();
  try { await api(`/api/projects/${PID}/reference_url`, { method: "POST", body: { url, download } }); $("#refUrl").value = ""; toast(download ? "Download started." : "Link saved."); refresh(true); }
  catch (e) { toast(e.message, true); }
}
$("#refSaveLink").onclick = () => addRefUrl(false);
$("#refDownload").onclick = () => addRefUrl(true);

async function loadBrandKit() {
  const files = await api("/api/brandkit").catch(() => []);
  $("#logoSelect").innerHTML = `<option value="">None</option>` + files.filter((f) => /\.(png|jpe?g|webp)$/i.test(f)).map((f) => `<option>${esc(f)}</option>`).join("");
}
function renderBrief() {
  if (!briefDirty) {
    $("#brief").value = P.brief || "";
    for (const el of $$("#settingsForm [name]")) {
      const v = P.settings[el.name];
      const sv = v === null || v === undefined ? "" : String(v);
      // a value the dropdown doesn't list (e.g. music = "mytrack.mp3" set by a command) must survive a Save:
      // otherwise the select shows blank and saving silently changes the setting
      if (el.tagName === "SELECT" && sv && ![...el.options].some((o) => o.value === sv)) el.add(new Option(sv, sv));
      el.value = sv;
    }
  }
  $("#briefState").textContent = briefDirty ? "unsaved changes" : P.updated ? `saved` : "";
}
$("#brief").addEventListener("input", () => { briefDirty = true; $("#briefState").textContent = "unsaved changes"; });
$$("#settingsForm [name]").forEach((el) => el.addEventListener("change", () => { briefDirty = true; $("#briefState").textContent = "unsaved changes"; }));
function collectSettings() {
  const s = {};
  for (const el of $$("#settingsForm [name]")) {
    let v = el.value;
    if (["captions", "remove_silence", "audio_polish"].includes(el.name)) v = v === "true";
    else if (["caption_words"].includes(el.name)) v = parseInt(v || "4", 10);
    else if (["silence_min"].includes(el.name)) v = parseFloat(v || "0.6");
    else if (["face_y"].includes(el.name)) v = parseFloat(v || "0.27");
    else if (["loudness", "duck_db"].includes(el.name)) { if (v === "") continue; v = parseFloat(v); }  // blank = keep
    else if (el.name === "logo") v = v || null;
    s[el.name] = v;
  }
  return s;
}
async function saveBrief() {
  await api(`/api/projects/${PID}/brief`, { method: "POST", body: { brief: $("#brief").value, settings: collectSettings() } });
  briefDirty = false; await refresh(true);
}
$("#saveBrief").onclick = () => saveBrief().then(() => toast("Saved.")).catch((e) => toast(e.message, true));
$("#brandBtn").onclick = async () => {
  try {
    const r = await api(`/api/projects/${PID}/apply_brand`, { method: "POST" });
    briefDirty = false; await refresh(true);
    toast(`Brand Kit applied (${r.applied.length} settings). Undo to revert.`);
  } catch (e) { toast(e.message, true); }
};
$("#firstCutBtn").onclick = async () => {
  try {
    await saveBrief();
    if (!P.scenes.length) return toast("Analyze the footage first (tab 1).", true);
    await api(`/api/projects/${PID}/first_cut`, { method: "POST" });
    toast("Sent to Claude. Say “process studio inbox” in VS Code.");
    refresh(true); openInbox();
  } catch (e) { toast(e.message, true); }
};

/* ---------------- scenes ---------------- */
function sceneBadges(s) {
  const b = [];
  if (!s.keep) b.push(`<span class="badge cut">CUT</span>`);
  if (s.speed && s.speed !== 1) b.push(`<span class="badge acc">${s.speed}x</span>`);
  if (s.mute) b.push(`<span class="badge">muted</span>`);
  else if (s.volume !== undefined && s.volume !== 1) b.push(`<span class="badge">vol ${Math.round(s.volume * 100)}%</span>`);
  if (s.zoom && s.zoom > 1) b.push(`<span class="badge acc">zoom ${s.zoom}x</span>`);
  if (s.text) b.push(`<span class="badge acc">text</span>`);
  if (s.card && s.card.kind !== "none") b.push(`<span class="badge acc">${esc(s.card.kind)} card</span>`);
  if (s.captions === false) b.push(`<span class="badge">no captions</span>`);
  if (s.remove_silence === true || (s.remove_silence === null && P.settings.remove_silence)) b.push(`<span class="badge">jump cuts</span>`);
  if (s.in !== s.orig_in || s.out !== s.orig_out) b.push(`<span class="badge">trimmed</span>`);
  return b.join(" ");
}
function renderScenes() {
  const t = P.totals || {};
  const sbb = $("#sbBtn");
  sbb.style.display = P.storyboard ? "" : "none";
  if (P.storyboard) sbb.textContent = `Storyboard (${P.storyboard.reviewed}/${P.storyboard.beats} reviewed)`;
  $("#totals").innerHTML = P.scenes.length
    ? `<b>${t.count}</b> scenes · <b>${t.kept}</b> kept · original <b>${fmt(t.orig)}</b> → edit ≈ <b>${fmt(t.est)}</b> · format <b>${esc(P.settings.format)}</b> · captions <b>${P.settings.captions ? "on" : "off"}</b>${P.undo_depth ? ` · ${P.undo_depth} undo steps` : ""}`
    : `No scenes yet — upload footage and click <b>Analyze footage</b> in tab 1.`;
  const pending = new Set(P.commands.filter((c) => c.status === "claude" && c.scene).map((c) => c.scene));
  const el = $("#sceneList");
  const scroll = el.scrollTop;
  el.innerHTML = P.scenes.map((s) => `
    <div class="scene ${s.keep ? "" : "cut"} ${s.id === selected ? "sel" : ""}" data-id="${s.id}">
      <img src="${media("thumbs", s.thumb)}" loading="lazy" alt="">
      <span class="no">${s.no}</span>
      <div class="info">
        <div class="times">${fmt(s.in)}–${fmt(s.out)} · ${s.dur}s${s.keep && Math.abs(s.est - s.dur) > 0.05 ? ` → ${s.est}s` : ""} ${sceneBadges(s)}</div>
        <div class="txt">${esc(s.caption_view ?? s.transcript) || `<span class="muted">(no speech)</span>`}</div>
      </div>
      <div class="acts">
        <button data-act="up" title="Move up">▲</button><button data-act="down" title="Move down">▼</button>
        <button data-act="toggle" title="${s.keep ? "Cut" : "Restore"}">${s.keep ? "✂" : "↺"}</button>
      </div>
      ${pending.has(s.id) ? `<span class="pending" title="Waiting for Claude"></span>` : ""}
    </div>`).join("");
  el.scrollTop = scroll;
  $$(".scene", el).forEach((card) => {
    card.onclick = (e) => {
      const act = e.target.dataset.act;
      if (act === "up" || act === "down") return api(`/api/projects/${PID}/scene/${card.dataset.id}/move`, { method: "POST", body: { dir: act } }).then(() => refresh(true));
      if (act === "toggle") { const s = P.scenes.find((x) => x.id === card.dataset.id); return sendCommand(s.id, s.keep ? "cut" : "keep"); }
      selectScene(card.dataset.id);
    };
  });
  // re-analysis replaces scene ids: a stale selection must fall back to scene 1, not "Select a scene."
  if ((!selected || !P.scenes.some((x) => x.id === selected)) && P.scenes.length) selectScene(P.scenes[0].id, false);
}
function curScene() { return P?.scenes.find((s) => s.id === selected); }
function selectScene(id, play = true) {
  selected = id;
  $$(".scene").forEach((c) => c.classList.toggle("sel", c.dataset.id === id));
  renderDetail(); loadPlayer(play);
}
function renderDetail() {
  const s = curScene(), el = $("#sceneDetail");
  if (!s) { el.innerHTML = `<p class="muted">Select a scene.</p>`; $("#sceneHistory").innerHTML = ""; return; }
  el.innerHTML = `
    <div class="title"><h2>Scene ${s.no}</h2><span class="muted">${esc(s.src)} · ${fmt(s.in)}–${fmt(s.out)} (${s.dur}s${s.keep ? ` → ${s.est}s` : ""})</span> ${sceneBadges(s)}</div>
    <div class="transcript">${esc(s.transcript) || `<span class="muted">(no speech in this scene)</span>`}</div>
    ${s.caption_view !== undefined && s.caption_view !== s.transcript ? `<div class="transcript capview"><span class="muted">Captions:</span> ${esc(s.caption_view)}
      <div class="muted small">Wrong word? Type in the box below:  spell ${esc((s.caption_view.split(" ").find(Boolean)) || "word")} as ...</div></div>` : ""}
    ${s.text ? `<div class="props"><span class="badge acc">Text: ${esc(s.text)} (${esc(s.text_pos)})</span></div>` : ""}
    ${s.card ? `<div class="props"><span class="badge acc">Top card: ${esc(s.card.kind === "title" ? s.card.text : s.card.kind === "number" ? `${s.card.value} ${s.card.label || ""}` : s.card.kind === "chart" ? `chart · ${s.card.label || ""}` : "empty")}</span></div>` : ""}`;
  const hist = P.commands.filter((c) => c.scene === s.id).slice(-12).reverse();
  $("#sceneHistory").innerHTML = hist.map(cmdHtml).join("");
  bindCancel($("#sceneHistory"));
}
function cmdHtml(c) {
  const who = c.status === "claude" ? `<span class="by-claude">waiting for Claude</span>` : c.by === "claude" ? `<span class="by-claude">Claude</span>` : c.status === "cancelled" ? "cancelled" : "instant";
  return `<div class="cmd ${c.status}">
    <div class="meta">${c.scene_no ? `Scene ${c.scene_no}` : "Whole video"} · ${esc((c.created || "").slice(11, 16))} · ${who}
      ${c.status === "claude" ? `<button class="btn ghost small" data-cancel="${c.id}">cancel</button>` : ""}</div>
    <div class="q">› ${esc(c.text)}</div>${c.reply ? `<div class="a">${esc(c.reply)}</div>` : ""}</div>`;
}
function bindCancel(root) {
  $$("[data-cancel]", root).forEach((b) => (b.onclick = () => api(`/api/projects/${PID}/command/${b.dataset.cancel}/cancel`, { method: "POST" }).then(() => refresh(true))));
}
async function sendCommand(scene, text) {
  if (!text.trim()) return;
  try {
    const r = await api(`/api/projects/${PID}/command`, { method: "POST", body: { scene, text } });
    const msg = [...r.replies.map((x) => x.replace(/^!/, "")), ...(r.queued.length ? [`Sent to Claude: “${r.queued.join("; ")}”`] : [])].join("  ·  ");
    toast(msg, r.replies.some((x) => x.startsWith("!")));
    await refresh(true); loadPlayer(false);
  } catch (e) { toast(e.message, true); }
}
$("#sceneSend").onclick = () => { const s = curScene(); if (!s) return toast("Select a scene first.", true); sendCommand(s.id, $("#sceneCmd").value).then(() => ($("#sceneCmd").value = "")); };
$("#sceneCmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#sceneSend").click(); });
$("#globalSend").onclick = () => sendCommand(null, $("#globalCmd").value).then(() => ($("#globalCmd").value = ""));
$("#globalCmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#globalSend").click(); });
$$("#quickChips button").forEach((b) => (b.onclick = () => { const s = curScene(); if (s) sendCommand(s.id, b.textContent); }));
$("#undoBtn").onclick = () => api(`/api/projects/${PID}/undo`, { method: "POST" }).then((r) => { toast(r.ok ? "Undone." : "Nothing to undo."); refresh(true); });
$("#sbBtn").onclick = () => window.open(`/storyboard/${PID}`, "_blank");
$("#previewBtn").onclick =() => api(`/api/projects/${PID}/render`, { method: "POST", body: { kind: "preview" } }).then(() => toast("Rendering preview… (only changed scenes are re-rendered)")).catch((e) => toast(e.message, true));
$("#finalBtn").onclick = () => api(`/api/projects/${PID}/render`, { method: "POST", body: { kind: "final" } }).then(() => toast("Exporting full quality to the Exports folder…")).catch((e) => toast(e.message, true));

/* ---------------- player ---------------- */
const player = $("#player");
let curSrcUrl = "";
// newest render of EITHER kind: after "Export final" the player must show that, not an older preview
function latestPreview() { const r = P?.renders || []; return r.length ? r[r.length - 1] : null; }
function renderUrl(r) { return r.kind === "final" ? media("exports", r.file) : media("work", r.file); }
function previewOffset(id) {
  let t = 0; for (const s of P.scenes) { if (s.id === id) return t; if (s.keep) t += s.est; } return 0;
}
function loadPlayer(play) {
  const s = curScene(); if (!s) return;
  let url, start;
  if (playerMode === "preview") {
    const pv = latestPreview();
    if (!pv) { $("#playerInfo").textContent = "Nothing rendered yet — click Render preview."; return; }
    url = renderUrl(pv); start = s.keep ? previewOffset(s.id) : 0;
    $("#playerInfo").textContent = `${pv.kind === "final" ? "Final export" : "Preview"} ${esc(pv.file)} · ${fmt(pv.duration)} · ${pv.created.slice(11, 16)}${s.keep ? "" : " · this scene is cut"}`;
    player.playbackRate = 1; player.muted = false;
  } else {
    const src = P.sources.find((x) => x.file === s.src);
    url = src && src.proxy ? media("proxy", src.proxy) : media("raw", s.src); start = s.in;
    player.playbackRate = s.speed || 1; player.muted = !!s.mute;
    $("#playerInfo").textContent = `Source · speed ${s.speed || 1}x${s.mute ? " · muted" : ""}`;
  }
  const go = () => { player.currentTime = start; if (play) player.play().catch(() => {}); };
  if (curSrcUrl !== url) { curSrcUrl = url; player.src = url; player.addEventListener("loadedmetadata", go, { once: true }); }
  else go();
}
player.addEventListener("timeupdate", () => {
  if (playerMode !== "source") return;
  const s = curScene(); if (!s) return;
  if (player.currentTime >= s.out - 0.04) {
    if ($("#loopScene").checked) player.currentTime = s.in; else player.pause();
  }
});
let lastRenderFile = null;  // null = page just loaded (don't auto-switch on the first poll)
function showRendered() {
  playerMode = "preview";
  $$(".seg button").forEach((x) => x.classList.toggle("active", x.dataset.mode === "preview"));
  curSrcUrl = ""; loadPlayer(false);
}
$$(".seg button").forEach((b) => (b.onclick = () => {
  playerMode = b.dataset.mode; $$(".seg button").forEach((x) => x.classList.toggle("active", x === b)); curSrcUrl = ""; loadPlayer(true);
}));
$("#splitHere").onclick = () => {
  const s = curScene(); if (!s) return;
  if (playerMode !== "source") return toast("Switch to “Source scene” to split.", true);
  sendCommand(s.id, `split at ${(player.currentTime - s.in).toFixed(2)}`);
};

/* ---------------- exports ---------------- */
function renderRenders() {
  const el = $("#renderList");
  const rs = [...(P.renders || [])].reverse();
  if (!rs.length) { el.innerHTML = `<p class="muted">Nothing rendered yet.</p>`; return; }
  const latestPv = latestPreview();
  el.innerHTML = rs.filter((r) => r.kind === "final" || r === latestPv).map((r) => `
    <div class="render">
      <video src="${media(r.kind === "final" ? "exports" : "work", r.file)}" controls preload="metadata"></video>
      <div class="meta"><div><b>${esc(r.file)}</b><div class="muted">${r.kind === "final" ? "Final export" : "Latest preview"} · ${esc(r.size)} · ${fmt(r.duration)} · ${esc(r.created.replace("T", " ").slice(0, 16))}</div></div>
      <span class="badge ${r.kind === "final" ? "ok" : ""}">${r.kind}</span></div>
    </div>`).join("");
}

/* ---------------- inbox ---------------- */
function renderInbox() {
  const waiting = P.commands.filter((c) => c.status === "claude");
  const btn = $("#inboxBtn");
  btn.classList.toggle("hidden", !waiting.length);
  btn.textContent = `Claude inbox · ${waiting.length}`;
  const recent = P.commands.filter((c) => c.status === "claude" || c.by === "claude").slice(-20).reverse();
  $("#inboxList").innerHTML = recent.length ? recent.map(cmdHtml).join("") : `<p class="muted">Nothing waiting.</p>`;
  bindCancel($("#inboxList"));
}
function openInbox() { $("#inbox").classList.remove("hidden"); }
$("#inboxBtn").onclick = openInbox;
$("#inboxClose").onclick = () => $("#inbox").classList.add("hidden");
$("#copyPhrase").onclick = () => navigator.clipboard?.writeText($("#inboxPhrase").textContent).then(() => toast("Copied."));

/* ---------------- boot ---------------- */
const hashTab = location.hash.slice(1);
switchTab(["footage", "refs", "scenes", "exports"].includes(hashTab) ? hashTab : store.get("cve.tab") || "footage");
loadBrandKit();
loadProjects().catch((e) => toast(e.message, true));
