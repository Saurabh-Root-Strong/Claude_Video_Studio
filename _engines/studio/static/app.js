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
$("#projectSelect").onchange = (e) => {
  PID = e.target.value; store.set("cve.pid", PID); lastVersion = -1; lastSig = {}; selected = null; briefDirty = false; lastRenderFile = null;
  seqStop(); SEQ.t = 0; SEQ.i = -1; editLook = null; TL.sel.clear(); TL.fit = true; vids().forEach((v) => { v.removeAttribute("src"); v.dataset.url = ""; v.load(); });
  refresh(true);
};

/* ---------------- tabs ---------------- */
function switchTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  store.set("cve.tab", name);
  // the timeline measures its width – it can only do that while visible; stop footage playback when leaving
  if (name === "footage") { if (typeof renderTimeline === "function" && P) setTimeout(renderTimeline, 0); }
  else if (typeof seqStop === "function") seqStop();
  if (name === "scenes" && P && typeof renderSceneTimeline === "function") setTimeout(renderSceneTimeline, 0);
  if (name === "media" && typeof loadMedia === "function") loadMedia();
}
$$("#tabs button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
$$("[data-open]").forEach((b) => (b.onclick = () => api(`/api/projects/${PID}/open`, { method: "POST", body: { which: b.dataset.open } })));

/* ---------------- polling ---------------- */
async function refresh(force = false) {
  if (!PID) return;
  let p;
  try { p = await api(`/api/projects/${encodeURIComponent(PID)}`); refresh.err = ""; }
  catch (e) {  // e.g. a damaged project file: say it ONCE (this runs every 2 s), don't leave a silent blank page
    const msg = e instanceof TypeError ? "Can't reach the studio – is the black Start Studio window still open?" : e.message;
    if (msg && msg !== refresh.err) { refresh.err = msg; toast(msg, true); }
    return;
  }
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
  renderNext();
  renderPlanBtn();
  // project picker label ("Name · N scenes") goes stale as clips/scenes change
  const opt = [...$("#projectSelect").options].find((o) => o.value === PID);
  if (opt) { const t = `${P.name} · ${P.scenes.length} scene${P.scenes.length === 1 ? "" : "s"}`; if (opt.textContent !== t) opt.textContent = t; }
  const aj = P.job && P.job.kind === "analyze" && P.job.status === "running" ? P.job.progress : "";
  if (!dragging && changed("src", [P.sources, P.footage, aj])) {
    renderSources();
    // clips added/removed/reordered: forget a selection that no longer exists, redraw the timeline
    const names = new Set(P.sources.map((s) => s.file));
    TL.sel = new Set([...TL.sel].filter((f) => names.has(f)));
    if (!tlDrag) { if (SEQ.playing && changed("seqOrder", P.sources.map((s) => s.file + s.include))) seqStop(); renderTimeline(); }
  }
  const refSig = [P.references, P.style_source, P.looks_view, P.commands.filter((c) => c.kind === "ref_style").map((c) => c.ref + c.status)];
  if (changed("refs", refSig)) { renderRefs(); renderLooks(); }
  renderBrief();
  renderAutoReport(); renderSuggest();
  if (changed("scenes", [P.scenes, P.settings, P.storyboard, P.tracks, P.commands.filter((c) => c.status === "claude").map((c) => c.scene), selected])) renderScenes();
  if (changed("detail", [curScene(), P.commands.length, P.commands.map((c) => c.status).join(), P.settings.layout,
    P.settings.remove_silence, P.settings.captions, P.settings.split_ratio, P.broll_clips, P.broll_assets, P.sources.map((s) => s.file)])) renderDetail();
  if (changed("renders", P.renders)) {
    renderRenders();
    // a render just finished while the page was open -> show it in the player
    const newest = latestPreview();
    if (newest && lastRenderFile !== null && newest.file !== lastRenderFile) {
      const show = SHOW && newest.sig === P.edit_sig && Date.now() - SHOW.at < 120000 ? SHOW : null;
      if (show && (playerMode === "preview" || show.switchView)) {
        SHOW = null;
        if (show.sid && P.scenes.some((x) => x.id === show.sid)) selected = show.sid;
        playerMode = "preview"; $$(".player-bar .seg button").forEach((x) => x.classList.toggle("active", x.dataset.mode === "preview"));
        curSrcUrl = ""; renderDetail(); loadPlayer(true);
        toast("Edited view updated – playing your change.");
      } else if (playerMode === "preview") reloadPreview();
      else if (AUTO.lastReq !== newest.sig) showRendered();  // a render you asked for: show it
      // auto update while you work in Original footage: stay there (the green pill says it is ready)
    }
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
  ["#analyzeBtn", "#finalBtn"].forEach((s) => ($(s).disabled = busy));
}

/* ---------------- footage ---------------- */
const fmtBytes = (b) => !b ? "" : b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${Math.round(b / 1e6)} MB`;
let dragging = null;  // file name while a clip is being dragged (don't redraw the list under the cursor)
function clipStatus(s) {
  const j = P.job || {};
  if (s.missing) return `<span class="badge bad" title="The file is no longer in Raw Footage/${esc(PID)}">file missing</span>`;
  if (j.kind === "analyze" && j.status === "running" && (j.progress || "").includes(s.file)) return `<span class="badge run">analyzing…</span>`;
  if (s.error && !s.analyzed) return `<span class="badge bad" title="${esc(s.error)}">failed – hover for why</span>`;
  if (s.analyzed) return `<span class="badge ok">analyzed · ${s.words ?? 0} words · ${s.scenes} scene${s.scenes === 1 ? "" : "s"}</span>`;
  return `<span class="badge warn">new – not analyzed</span>`;
}
function renderSources() {
  const el = $("#sourceList"), f = P.footage || {};
  $("#binSummary").textContent = P.sources.length
    ? `· ${f.clips} clip${f.clips === 1 ? "" : "s"}${f.included !== f.clips ? ` (${f.clips - f.included} skipped)` : ""} · ${fmt(f.duration || 0)} · ${fmtBytes(f.bytes)}` : "";
  const re = $("#reanalyze").checked;
  $("#analyzeBtn").textContent = re ? "Re-analyze all footage" : f.new ? `Analyze ${f.new} new clip${f.new === 1 ? "" : "s"}` : "Analyze footage";
  if (!P.sources.length) { el.innerHTML = `<p class="muted empty">No footage yet. Click <b>+ Add videos</b> or drop files above.</p>`; return; }
  const n = P.sources.length;
  el.innerHTML = P.sources.map((s, i) => {
    const thumb = s.thumb ? media("thumbs", s.thumb) : s.thumb_scene ? media("thumbs", s.thumb_scene) : "";
    const meta = [s.duration ? fmt(s.duration) : "", s.width ? `${s.width}×${s.height}` : "", s.fps ? `${Math.round(s.fps)} fps` : "",
      s.codec ? s.codec.toUpperCase() : "", fmtBytes(s.bytes), s.has_audio === false ? "no audio" : "",
      s.recorded ? `recorded ${esc(s.recorded.replace("T", " ").slice(0, 16))}` : ""].filter(Boolean).join(" · ");
    const inc = s.include !== false;
    return `
    <div class="clip ${inc ? "" : "skipped"} ${s.missing ? "missing" : ""}" draggable="true" data-file="${esc(s.file)}">
      <span class="grip" title="Drag to reorder">⠿</span>
      <span class="ord">${i + 1}</span>
      <div class="poster">${thumb ? `<img src="${thumb}" alt="" loading="lazy">` : `<span>🎬</span>`}
        ${s.duration ? `<em>${fmt(s.duration)}</em>` : ""}</div>
      <div class="cinfo">
        <div class="cname" title="${esc(s.file)}">${esc(s.file)}</div>
        <div class="muted small">${meta}</div>
        <div class="cbadges">${clipStatus(s)}${inc ? "" : `<span class="badge">skipped – not in the video</span>`}
          ${s.analyzed && inc ? `<span class="muted small">→ ${s.kept}/${s.scenes} scenes kept · ≈ ${fmt(s.est || 0)} in the edit</span>` : ""}</div>
      </div>
      <div class="cacts">
        <button class="btn ghost small" data-act="play" ${s.missing ? "disabled" : ""} title="Preview this clip">▶</button>
        <button class="btn ghost small" data-act="up" ${i === 0 ? "disabled" : ""} title="Move earlier">▲</button>
        <button class="btn ghost small" data-act="down" ${i === n - 1 ? "disabled" : ""} title="Move later">▼</button>
        <button class="btn small ${inc ? "ghost" : "warnbtn"}" data-act="include" title="${inc ? "Leave this clip out of the video (keeps it and its edits)" : "Put this clip back into the video"}">${inc ? "Skip" : "Include"}</button>
        <button class="btn ghost small danger" data-act="rm" title="Remove from the project (file stays on disk; Undo restores)">Remove</button>
      </div>
    </div>`;
  }).join("");
  $$(".clip", el).forEach((row) => {
    const file = row.dataset.file;
    $$("[data-act]", row).forEach((b) => (b.onclick = (e) => { e.stopPropagation(); clipAction(b.dataset.act, file); }));
    row.addEventListener("dragstart", (e) => { dragging = file; row.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/clip", file); });
    row.addEventListener("dragend", () => { dragging = null; row.classList.remove("dragging"); $$(".clip", el).forEach((x) => x.classList.remove("drop-before", "drop-after")); });
    row.addEventListener("dragover", (e) => {
      if (!dragging || dragging === file) return;
      e.preventDefault();
      const after = e.offsetY > row.offsetHeight / 2;
      row.classList.toggle("drop-after", after); row.classList.toggle("drop-before", !after);
    });
    row.addEventListener("dragleave", () => row.classList.remove("drop-before", "drop-after"));
    row.addEventListener("drop", (e) => {
      if (!dragging || dragging === file) return;
      e.preventDefault(); e.stopPropagation();
      const after = row.classList.contains("drop-after");
      const order = P.sources.map((s) => s.file).filter((x) => x !== dragging);
      order.splice(order.indexOf(file) + (after ? 1 : 0), 0, dragging);
      dragging = null; setClipOrder({ files: order });
    });
  });
}
async function setClipOrder(body) {
  try {
    const r = await fetch(`/api/projects/${PID}/sources/order`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 409 && j.confirm) {
      if (confirm(j.error + "\n\nReorder anyway?")) return setClipOrder({ ...body, force: true });
    } else if (!r.ok) toast(j.error || "Could not reorder", true);
    else if (!j.unchanged) toast("Clip order updated – scenes follow it. (Undo in the Scenes tab reverts.)");
  } finally { $("#sortClips").value = ""; lastSig.src = null; refresh(true); }
}
async function clipAction(act, file) {
  const files = P.sources.map((s) => s.file), i = files.indexOf(file);
  const s = P.sources[i];
  if (act === "up" || act === "down") {
    const j = act === "up" ? i - 1 : i + 1; if (j < 0 || j >= files.length) return;
    [files[i], files[j]] = [files[j], files[i]]; return setClipOrder({ files });
  }
  if (act === "include") {
    await api(`/api/projects/${PID}/source/include`, { method: "POST", body: { file, include: s.include === false } });
    toast(s.include === false ? `${file} is back in the video.` : `${file} is skipped – it stays in the project, out of the video.`);
    return refresh(true);
  }
  if (act === "rm") {
    if (!confirm(`Remove ${file} and its ${s.scenes || 0} scene(s) from this project?\nThe file stays on disk; Undo (Scenes tab) brings it back.`)) return;
    await api(`/api/projects/${PID}/source/remove`, { method: "POST", body: { file } }); return refresh(true);
  }
  if (act === "play") {
    TL.sel = new Set([file]); TL.anchor = file; renderTimeline();
    seqStop(); seqPlay(true);
    $("#seqEditor").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

/* ---------------- footage timeline (program monitor + video/audio tracks) ---------------- */
const STRIP_N = 24, ROW_H = 56;
const TL = { sel: new Set(), anchor: null, pps: 0, fit: true, assetsAsked: {} };
const SEQ = { segs: [], i: -1, act: 0, playing: false, t: 0 };
const vids = () => [$("#seqA"), $("#seqB")];
function clipUrl(s) { return s.proxy ? media("proxy", s.proxy) : media("raw", s.file); }
function layout() {  // every clip in clip order (skipped ones keep their place so the layout doesn't jump)
  let t = 0;
  return P.sources.map((s) => { const d = Math.max(0.1, s.duration || 0); const o = { s, start: t, dur: d }; t += d; return o; });
}
function totalDur() { return P.sources.reduce((a, s) => a + Math.max(0.1, s.duration || 0), 0); }
function tickStep(pps) { for (const st of [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]) if (st * pps >= 64) return st; return 1200; }
const tc = (t) => `${String(Math.floor(t / 60)).padStart(2, "0")}:${(t % 60).toFixed(1).padStart(4, "0")}`;

function renderTimeline() {
  const ed = $("#seqEditor");
  if (!P || !P.sources.length) { ed.classList.add("hidden"); return; }
  ed.classList.remove("hidden");
  const L = layout(), total = totalDur(), scroll = $("#tlScroll");
  const avail = Math.max(300, scroll.clientWidth - 16);
  if (TL.fit || !TL.pps) TL.pps = avail / total;
  const W = Math.max(avail, total * TL.pps);
  $("#tlCanvas").style.width = `${W}px`;
  // ruler
  const st = tickStep(TL.pps); let rh = "";
  for (let t = 0; t <= total + 0.001; t += st / (st >= 1 ? 5 : 2)) {
    const major = Math.abs(t / st - Math.round(t / st)) < 1e-6;
    rh += `<span class="tick ${major ? "major" : ""}" style="left:${t * TL.pps}px">${major ? `<b>${tc(t).replace(/\.0$/, "")}</b>` : ""}</span>`;
  }
  $("#tlRuler").innerHTML = rh;
  // tracks
  const blocks = L.map(({ s, start, dur }, i) => {
    const w = Math.max(18, dur * TL.pps), x = start * TL.pps, sel = TL.sel.has(s.file) ? "sel" : "", sk = s.include === false ? "skip" : "";
    let frames = "";
    if (s.strip) {
      const k = Math.max(1, Math.round(w / ROW_H));
      for (let j = 0; j < k; j++) {
        const idx = Math.min(STRIP_N - 1, Math.floor(((j + 0.5) / k) * STRIP_N));
        frames += `<i style="background-image:url('${media("thumbs", s.strip)}');background-size:${STRIP_N * ROW_H}px ${ROW_H}px;background-position:-${idx * ROW_H}px 0"></i>`;
      }
    } else if (s.thumb || s.thumb_scene) frames = `<i class="poster1" style="background-image:url('${media("thumbs", s.thumb || s.thumb_scene)}')"></i>`;
    const v = `<div class="blk ${sel} ${sk}" data-file="${esc(s.file)}" style="left:${x}px;width:${w}px" title="${esc(s.file)} · ${fmt(dur)}${sk ? " · skipped" : ""}">
        <div class="frames">${frames}</div><span class="lbl"><b>${i + 1}</b> ${esc(s.file)} · ${fmt(dur)}${sk ? " · skipped" : ""}</span></div>`;
    const a = `<div class="blk ablk ${sel} ${sk}" data-file="${esc(s.file)}" style="left:${x}px;width:${w}px">${s.wave ? `<img src="${media("thumbs", s.wave)}" alt="">` : `<span class="lbl muted">${s.has_audio === false ? "no audio" : ""}</span>`}</div>`;
    return [v, a];
  });
  $("#tlVideo").innerHTML = blocks.map((b) => b[0]).join("");
  $("#tlAudio").innerHTML = blocks.map((b) => b[1]).join("");
  drawHead();
  const n = TL.sel.size;
  $("#tlSel").textContent = n ? `${n} clip${n > 1 ? "s" : ""} selected · ▶ plays ${n > 1 ? "them back-to-back" : "it"} · Esc clears` : "Click a clip to select · Ctrl/Shift-click for several · drag to reorder";
  ["#tlStart", "#tlEnd", "#tlSkip"].forEach((s) => ($(s).disabled = !n));
  $("#seqWhat").textContent = n ? `selection (${n})` : "whole sequence";
  // missing film strips (clips added before the timeline existed): make them once
  if (P.sources.some((s) => !s.strip && !s.missing) && !TL.assetsAsked[PID]) {
    TL.assetsAsked[PID] = true;
    api(`/api/projects/${PID}/footage/assets`, { method: "POST" }).then(() => refresh(true)).catch(() => {});
  }
}
function drawHead() { $("#tlHead").style.transform = `translateX(${SEQ.t * TL.pps}px)`; $("#seqTime").textContent = `${tc(SEQ.t)} / ${tc(totalDur())}`; }

// selection + drag-to-reorder on the video/audio tracks (pointer events: works with mouse and touch)
let tlDrag = null;
$("#tlCanvas").addEventListener("pointerdown", (e) => {
  const blk = e.target.closest(".blk");
  if (!blk) {  // empty area / ruler: move the playhead there
    const r = $("#tlCanvas").getBoundingClientRect();
    seekTo(Math.max(0, Math.min(totalDur(), (e.clientX - r.left) / TL.pps)));
    return;
  }
  tlDrag = { file: blk.dataset.file, x0: e.clientX, moved: false, blk, id: e.pointerId, add: e.ctrlKey || e.metaKey, range: e.shiftKey };
  blk.setPointerCapture(e.pointerId);
});
$("#tlCanvas").addEventListener("pointermove", (e) => {
  if (!tlDrag) return;
  const dx = e.clientX - tlDrag.x0;
  if (!tlDrag.moved && Math.abs(dx) < 5) return;
  tlDrag.moved = true;
  $$(`.blk[data-file="${CSS.escape(tlDrag.file)}"]`).forEach((b) => { b.classList.add("dragging"); b.style.transform = `translateX(${dx}px)`; });
  // insertion point = gap nearest the pointer
  const r = $("#tlCanvas").getBoundingClientRect(), t = (e.clientX - r.left) / TL.pps, L = layout();
  let idx = L.length; for (let i = 0; i < L.length; i++) if (t < L[i].start + L[i].dur / 2) { idx = i; break; }
  tlDrag.idx = idx;
  const gx = (idx < L.length ? L[idx].start : totalDur()) * TL.pps;
  let ins = $("#tlIns"); if (!ins) { ins = document.createElement("div"); ins.id = "tlIns"; ins.className = "ins"; $("#tlCanvas").appendChild(ins); }
  ins.style.left = `${gx}px`;
});
function endDrag(e) {
  if (!tlDrag) return;
  const d = tlDrag; tlDrag = null;
  const ins = $("#tlIns"); if (ins) ins.remove();
  if (d.moved && d.idx !== undefined) {
    const files = P.sources.map((s) => s.file), from = files.indexOf(d.file);
    files.splice(from, 1); files.splice(d.idx > from ? d.idx - 1 : d.idx, 0, d.file);
    seqStop(); setClipOrder({ files });
    return;
  }
  // a click: select (Ctrl = toggle, Shift = range)
  const files = P.sources.map((s) => s.file);
  if (d.range && TL.anchor) {
    const a = files.indexOf(TL.anchor), b = files.indexOf(d.file);
    TL.sel = new Set(files.slice(Math.min(a, b), Math.max(a, b) + 1));
  } else if (d.add) { TL.sel.has(d.file) ? TL.sel.delete(d.file) : TL.sel.add(d.file); TL.anchor = d.file; }
  else { TL.sel = new Set([d.file]); TL.anchor = d.file; }
  seqStop(); const L = layout().find((x) => x.s.file === d.file); if (L) SEQ.t = L.start;
  renderTimeline();
}
$("#tlCanvas").addEventListener("pointerup", endDrag);
$("#tlCanvas").addEventListener("pointercancel", () => { tlDrag = null; renderTimeline(); });

function moveSel(toEnd) {
  const files = P.sources.map((s) => s.file), picked = files.filter((f) => TL.sel.has(f)), rest = files.filter((f) => !TL.sel.has(f));
  seqStop(); setClipOrder({ files: toEnd ? [...rest, ...picked] : [...picked, ...rest] });
}
$("#tlStart").onclick = () => moveSel(false);
$("#tlEnd").onclick = () => moveSel(true);
$("#tlSkip").onclick = async () => {
  const picked = P.sources.filter((s) => TL.sel.has(s.file)), inc = picked.some((s) => s.include === false);
  for (const s of picked) await api(`/api/projects/${PID}/source/include`, { method: "POST", body: { file: s.file, include: inc } });
  toast(inc ? "Selected clip(s) are back in the video." : "Selected clip(s) skipped."); refresh(true);
};
$("#tlIn").onclick = () => { TL.fit = false; TL.pps *= 1.5; renderTimeline(); };
$("#tlOut").onclick = () => { TL.fit = false; TL.pps /= 1.5; renderTimeline(); };
$("#tlFit").onclick = () => { TL.fit = true; renderTimeline(); };
window.addEventListener("resize", () => { if (TL.fit && P) renderTimeline(); });

// ---- sequence playback: two <video> elements swap so clip changes don't flash black
function buildSeq() {
  const L = layout().filter((x) => x.s.include !== false && !x.s.missing);
  const chosen = TL.sel.size ? L.filter((x) => TL.sel.has(x.s.file)) : L;
  return chosen.map((x) => ({ file: x.s.file, url: clipUrl(x.s), start: x.start, dur: x.dur }));
}
function loadInto(v, url, at, then) {
  const go = () => { try { v.currentTime = at; } catch {} then && then(); };
  if (v.dataset.url !== url) { v.dataset.url = url; v.src = url; v.addEventListener("loadedmetadata", go, { once: true }); }
  else go();
}
function playSeg(i, offset) {
  const seg = SEQ.segs[i]; if (!seg) return seqStop();
  SEQ.i = i;
  const [a, b] = vids(), cur = SEQ.act ? b : a, nxt = SEQ.act ? a : b;
  cur.classList.remove("off"); nxt.classList.add("off"); nxt.pause();
  $("#seqLabel").textContent = `${P.sources.findIndex((s) => s.file === seg.file) + 1} · ${seg.file}`;
  loadInto(cur, seg.url, offset, () => { if (SEQ.playing) cur.play().catch(() => {}); });
  const n = SEQ.segs[i + 1]; if (n) loadInto(nxt, n.url, 0);  // preload the next clip
}
function seqPlay(fromSel) {
  SEQ.segs = buildSeq();
  if (!SEQ.segs.length) return toast("Nothing to play – every clip is skipped or missing.", true);
  let i = SEQ.segs.findIndex((g) => SEQ.t >= g.start - 0.01 && SEQ.t < g.start + g.dur - 0.05);
  if (fromSel || i < 0) i = SEQ.segs.findIndex((g) => g.start >= SEQ.t - 0.01);
  if (i < 0) i = 0;
  const off = Math.max(0, SEQ.t - SEQ.segs[i].start);
  SEQ.playing = true; $("#seqPlay").textContent = "⏸ Pause";
  playSeg(i, off < SEQ.segs[i].dur - 0.05 ? off : 0);
}
function seqStop() {
  SEQ.playing = false; vids().forEach((v) => v.pause()); $("#seqPlay").textContent = "▶ Play";
}
function seekTo(t) {
  const wasPlaying = SEQ.playing; seqStop(); SEQ.t = t; drawHead();
  const L = layout().find((x) => t >= x.start && t < x.start + x.dur);
  if (L && L.s.include !== false && !L.s.missing) {
    const [a, b] = vids(), cur = SEQ.act ? b : a;
    cur.classList.remove("off"); (SEQ.act ? a : b).classList.add("off");
    $("#seqLabel").textContent = `${P.sources.indexOf(L.s) + 1} · ${L.s.file}`;
    loadInto(cur, clipUrl(L.s), t - L.start);
  }
  if (wasPlaying) seqPlay(false);
}
vids().forEach((v, k) => {
  v.addEventListener("timeupdate", () => {
    if (k !== SEQ.act || SEQ.i < 0 || !SEQ.playing) return;
    const seg = SEQ.segs[SEQ.i]; if (!seg) return;
    SEQ.t = seg.start + Math.min(v.currentTime, seg.dur); drawHead();
    if (v.currentTime >= seg.dur - 0.06) advance();
  });
  v.addEventListener("ended", () => { if (k === SEQ.act && SEQ.playing) advance(); });
  v.addEventListener("error", () => { if (k === SEQ.act && v.dataset.url) toast("This clip can't play in the browser yet (e.g. HEVC/ProRes) – Analyze it to get a preview copy.", true); });
});
function advance() {
  if (SEQ.i + 1 >= SEQ.segs.length) { seqStop(); return; }
  SEQ.act = 1 - SEQ.act; playSeg(SEQ.i + 1, 0);
}
$("#seqPlay").onclick = () => (SEQ.playing ? seqStop() : seqPlay(false));
document.addEventListener("keydown", (e) => {
  if (!$("#tab-footage").classList.contains("active") || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  if (e.code === "Space") { if (e.target.tagName === "VIDEO") return; e.preventDefault(); SEQ.playing ? seqStop() : seqPlay(false); }
  if (e.key === "Escape") { TL.sel.clear(); renderTimeline(); }
});
// ---- step flow: Footage -> Next: Scenes
function renderNext() {
  const j = P.job || {}, f = P.footage || {};
  const analyzing = j.kind === "analyze" && (j.status === "running" || j.status === "queued");
  const ready = P.scenes.length > 0 && !analyzing;
  const b = $("#nextScenes");
  b.disabled = !ready;
  b.classList.toggle("ready", ready && !f.new);
  b.title = analyzing ? "Wait for the analysis to finish" : !P.scenes.length ? (P.sources.length ? "Analyze the footage first" : "Add footage first")
    : f.new ? `${f.new} clip(s) not analyzed yet – their scenes won't be on the Scenes page` : "Go to the Scenes page";
  b.textContent = analyzing ? "Analyzing…" : "Next: Scenes →";
  const ban = $("#doneBanner"), clips = new Set(P.scenes.map((s) => s.src)).size;
  if (ready && !f.new) {
    const rendering = j.kind === "preview" && (j.status === "running" || j.status === "queued") || (j.pending || []).includes("preview");
    const html = `✓ Footage ready – <b>${P.scenes.length} scene${P.scenes.length === 1 ? "" : "s"}</b> from <b>${clips} clip${clips === 1 ? "" : "s"}</b>, ≈ ${fmt(P.totals.est || 0)} edited.
      ${rendering ? `<span class="muted">A preview of the complete video is rendering…</span>` : ""}
      <button class="btn next ready small" id="nextScenes2">Next: Scenes →</button>`;
    if (ban.dataset.h !== html) { ban.dataset.h = html; ban.innerHTML = html; $("#nextScenes2").onclick = goScenes; }  // redraw only on change
    ban.classList.remove("hidden");
  } else ban.classList.add("hidden");
}
function goScenes() {
  switchTab("scenes");
  if (P.scenes.length && !P.scenes.some((x) => x.id === selected)) selectScene(P.scenes[0].id, false);
  window.scrollTo({ top: 0, behavior: "smooth" });
}
$("#nextScenes").onclick = goScenes;
$("#addClips").onclick = () => $("#fileSource").click();
$("#sortClips").onchange = (e) => { if (e.target.value) setClipOrder({ sort: e.target.value }); };
$("#reanalyze").onchange = () => renderSources();
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
      else {
        const extra = (j.skipped && j.skipped.length ? ` Skipped (not a video): ${j.skipped.join(", ")}.` : "") +
          (j.duplicates && j.duplicates.length ? ` Already in the project: ${j.duplicates.join(", ")}.` : "");
        toast(`Added ${j.saved.length} file(s).${extra}` + (kind === "source" ? " Click Analyze when ready." : ""), !!extra);
      }
      refresh(true); resolve();
    };
    x.onerror = () => { bar.classList.add("hidden"); toast("Upload failed", true); resolve(); };
    x.send(fd);
  });
}
setupDrop("#dropSource", "#fileSource", "source");
// files dropped anywhere else would make the browser navigate away from the studio (losing the page);
// on the Footage card they're added as footage
["dragover", "drop"].forEach((ev) => window.addEventListener(ev, (e) => {
  if (e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files")) e.preventDefault();
}));
$("#binCard").addEventListener("drop", (e) => {
  if (e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); upload([...e.dataTransfer.files], "source", $("#dropSource")); }
});
setupDrop("#dropRef", "#fileRef", "reference");

/* ---------------- 📁 media (shared by every project) ---------------- */
var CAPDRAFT = null;  // ✏ Inspector caption-text draft {sid, text}
let MEDIA = null, MEDIA_OPEN = store.get("cve.medopen") || {};
const MEDIA_KINDS_ORDER = ["music", "sounds", "stickers", "videos", "transitions", "pictures"];
async function loadMedia() { loadExtract(); try { MEDIA = (await api("/api/media")).kinds; renderMedia(); fillXMedia(); } catch (e) { toast(e.message, true); } }
function fmtSize(b) { return b > 1 << 20 ? `${(b / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`; }
function renderMedia() {
  const box = $("#mediaKinds"); if (!box || !MEDIA) return;
  const q = ($("#mediaFind").value || "").trim().toLowerCase();
  const kindOpts = (k) => MEDIA_KINDS_ORDER.filter((x) => x !== k).map((x) => `<option value="${x}">${MEDIA[x].icon} ${esc(MEDIA[x].label)}</option>`).join("");
  box.innerHTML = MEDIA_KINDS_ORDER.map((k) => {
    const K = MEDIA[k], items = K.items.filter((it) => !q || it.name.toLowerCase().includes(q));
    const open = MEDIA_OPEN[k] !== false;
    const cards = items.map((it) => {
      const vis = k === "stickers" || k === "pictures" ? `<img loading="lazy" src="${it.url}" alt="">`
        : k === "videos" || k === "transitions" ? `<video preload="none" muted loop playsinline src="${it.url}" ${it.thumb ? `poster="${it.thumb}"` : ""}></video>`
        : `<div class="medaud">${k === "music" ? "🎵" : "🔊"}</div>`;
      const meta = [it.dur ? fmt(it.dur) : "", fmtSize(it.size)].filter(Boolean).join(" · ");
      return `<div class="medcard" data-k="${k}" data-n="${esc(it.name)}">
        <div class="medvis" data-play="1" data-tip="${k === "music" || k === "sounds" ? "Click to listen" : k === "videos" || k === "transitions" ? "Hover / click to play" : esc(it.name)}">${vis}</div>
        <div class="medname" title="${esc(it.name)}">${esc(it.name)}</div><div class="muted small">${meta}</div>
        <div class="medact">
          ${["videos", "transitions", "pictures"].includes(k) ? `<button class="btn small" data-use="1" data-tip="Copy into THIS project: then pick it in Cover with B-roll or on a timeline track">+ this project</button>` : ""}
          <select data-move="1" data-tip="Wrong kind? Move it"><option value="">move…</option>${kindOpts(k)}</select>
          <button class="btn ghost small" data-del="1" data-tip="Delete (kept in Library/.trash)">🗑</button>
        </div></div>`; }).join("");
    return `<div class="card medkind ${open ? "" : "closed"}">
      <div class="medhead" data-tog="${k}"><b>${K.icon} ${esc(K.label)}</b> <span class="badge">${K.items.length}</span>
        <span class="muted small">${esc(K.use)}</span><span class="grow"></span>
        <button class="lnk" data-openk="${k}" data-tip="${esc(K.folder)}">folder</button><span class="chev">${open ? "▾" : "▸"}</span></div>
      ${open ? (items.length ? `<div class="medgrid">${cards}</div>` : `<div class="muted small medempty">${q ? "Nothing matches." : "Nothing yet – drop files above."}</div>`) : ""}
    </div>`; }).join("");
  $$("[data-tog]", box).forEach((h) => (h.onclick = (e) => { if (e.target.closest("button")) return; MEDIA_OPEN[h.dataset.tog] = MEDIA_OPEN[h.dataset.tog] === false; store.set("cve.medopen", MEDIA_OPEN); renderMedia(); }));
  $$("[data-openk]", box).forEach((b) => (b.onclick = () => api("/api/media/open", { method: "POST", body: { kind: b.dataset.openk } })));
  $$(".medcard", box).forEach((c) => {
    const k = c.dataset.k, n = c.dataset.n;
    const v = $("video", c); if (v) { c.onmouseenter = () => v.play().catch(() => {}); c.onmouseleave = () => { v.pause(); v.currentTime = 0; }; }
    $("[data-play]", c).onclick = () => {
      if (k === "music" || k === "sounds") { const it = MEDIA[k].items.find((x) => x.name === n); if (window._medAudio) window._medAudio.pause(); window._medAudio = new Audio(it.url); window._medAudio.play().catch(() => {}); }
      else if (v) { v.muted = false; v.currentTime = 0; v.play().catch(() => {}); }
    };
    const use = $("[data-use]", c); if (use) use.onclick = async () => {
      if (!PID) return toast("Open a project first.", true);
      try { const r = await api(`/api/projects/${PID}/media/use`, { method: "POST", body: { kind: k, name: n } }); toast(r.reply); refresh(true); } catch (e) { toast(e.message, true); } };
    $("[data-move]", c).onchange = async (e) => { const to = e.target.value; if (!to) return;
      try { const r = await api("/api/media/move", { method: "POST", body: { kind: k, name: n, to } }); toast(r.reply); LIBCAT = null; loadMedia(); } catch (er) { toast(er.message, true); e.target.value = ""; } };
    $("[data-del]", c).onclick = async () => { if (!confirm(`Delete “${n}”? (kept in Library/.trash)\n\nIf a video uses it, it will be missing there.`)) return;
      try { const r = await api("/api/media/remove", { method: "POST", body: { kind: k, name: n } }); toast(r.reply); LIBCAT = null; loadMedia(); } catch (e) { toast(e.message, true); } };
  });
}
function uploadMedia(files) {
  if (!files.length) return Promise.resolve();
  const zone = $("#dropMedia"), fd = new FormData(); files.forEach((f) => fd.append("files", f));
  const bar = $(".progress", zone), fill = $(".progress div", zone), as = $("#mediaAs").value;
  bar.classList.remove("hidden"); fill.style.width = "0";
  return new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open("POST", `/api/media/upload${as ? `?kind=${as}` : ""}`);
    x.upload.onprogress = (e) => { if (e.lengthComputable) fill.style.width = `${(100 * e.loaded) / e.total}%`; };
    x.onload = () => {
      bar.classList.add("hidden");
      let j = {}; try { j = JSON.parse(x.responseText); } catch {}
      if (x.status >= 400) { toast(j.error || "Upload failed", true); return resolve(); }
      const ok = j.results.filter((r) => r.ok), res = $("#mediaResult");
      res.classList.remove("hidden");
      res.innerHTML = j.results.map((r) => `<div class="${r.ok ? "ok" : "bad"}">${r.ok ? "✓" : r.dup ? "=" : "✗"} <b>${esc(r.name)}</b> → ${r.ok ? `${MEDIA && MEDIA[r.kind] ? MEDIA[r.kind].icon + " " + esc(MEDIA[r.kind].label) : esc(r.kind)} <span class="muted">(${esc(r.why)})</span>` : esc(r.why)}</div>`).join("");
      toast(`${ok.length} of ${j.results.length} file(s) added` + (ok.length < j.results.length ? " – see the list for the others." : "."), ok.length < j.results.length);
      LIBCAT = null; MEDIA_OPEN = Object.assign(MEDIA_OPEN, Object.fromEntries(ok.map((r) => [r.kind, true]))); loadMedia(); if (PID) refresh(true); resolve();
    };
    x.onerror = () => { bar.classList.add("hidden"); toast("Upload failed", true); resolve(); };
    x.send(fd);
  });
}
(() => {
  const zone = $("#dropMedia"), input = $("#fileMedia");
  zone.onclick = (e) => { if (e.target.tagName !== "INPUT") input.click(); };
  input.onchange = () => uploadMedia([...input.files]).finally(() => (input.value = ""));
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", (e) => uploadMedia([...e.dataTransfer.files]));
  $("#tab-media").addEventListener("drop", (e) => { if (e.target.closest("#dropMedia, #xBox")) return; if (e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); uploadMedia([...e.dataTransfer.files]); } });
  $("#mediaFind").addEventListener("input", renderMedia);
  $("#mediaOpen").onclick = () => api("/api/media/open", { method: "POST", body: {} });
})();

/* ---------------- ⬇ extract & download: MP3 / MP4 / MP4 without sound, from a link, a file or a Media clip ---------------- */
let XD = { jobs: [], files: [] }, XFILE = null, XMODE = store.get("cve.xmode") || "mp3", XPOLL = null, XSEEN = new Set(), XINIT = false;
function fillXMedia() {
  const sel = $("#xMedia"); if (!sel || !MEDIA) return;
  const cur = sel.value;
  sel.innerHTML = `<option value="">or from Media…</option>` + ["videos", "transitions", "music"].filter((k) => MEDIA[k] && MEDIA[k].items.length)
    .map((k) => `<optgroup label="${MEDIA[k].icon} ${esc(MEDIA[k].label)}">${MEDIA[k].items.map((it) => `<option value="${esc(k + "/" + it.name)}">${esc(it.name)}</option>`).join("")}</optgroup>`).join("");
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "";
}
function xSource() {  // what the next Extract uses – exactly one of link / file / Media clip
  const url = $("#xUrl").value.trim(), m = $("#xMedia").value;
  return XFILE ? { file: XFILE } : m ? { media: m } : url ? { url } : null;
}
function renderXChosen() {
  const el = $("#xChosen"), m = $("#xMedia").value;
  const t = XFILE ? `📁 <b>${esc(XFILE.name)}</b> (${fmtSize(XFILE.size)})` : m ? `🎞 <b>${esc(m.split("/").slice(1).join("/"))}</b> from Media` : "";
  el.classList.toggle("hidden", !t);
  el.innerHTML = t ? `Using ${t} <button class="lnk" id="xClear">✕ clear</button>` : "";
  const c = $("#xClear"); if (c) c.onclick = () => { XFILE = null; $("#xMedia").value = ""; renderXChosen(); };
  $("#xUrl").disabled = !!t;
}
function setXMode(m) { XMODE = m; store.set("cve.xmode", m); $$("#xModes button").forEach((b) => b.classList.toggle("active", b.dataset.m === m)); }
async function loadExtract() {
  try { XD = await api("/api/extract"); } catch { return; }
  renderExtract();
  const busy = XD.jobs.some((j) => j.status === "queued" || j.status === "running");
  clearTimeout(XPOLL); if (busy) XPOLL = setTimeout(loadExtract, 1200);
}
function renderExtract() {
  const box = $("#xList"); if (!box) return;
  const live = XD.jobs.filter((j) => j.status !== "done" || !XD.files.some((f) => f.name === j.file));
  for (const j of XD.jobs) if (j.status === "done" && !XSEEN.has(j.id)) { XSEEN.add(j.id); if (XINIT) toast(`Ready: ${j.file} – ⬇ Download it below.`); }
  XINIT = true;  // the first look only learns what was already finished (no "Ready" toasts on opening the tab)
  const jobRow = (j) => j.status === "error"
    ? `<div class="xitem err"><span>✗</span><span class="xn"><b>${esc(j.title || "Extract")}</b> · ${esc(j.mode_label)}<br><span class="bad">${esc(j.error)}</span></span><button class="btn ghost small" data-xdis="${esc(j.id)}">OK</button></div>`
    : `<div class="xitem"><span class="spin-s">⟳</span><span class="xn"><b>${esc(j.title || "…")}</b> · ${esc(j.mode_label)}</span><span class="muted small">${esc(j.stage)} ${j.progress ? Math.round(j.progress) + "%" : ""}</span><div class="xbar"><i style="width:${j.progress || 2}%"></i></div></div>`;
  const fileRow = (f) => `<div class="xitem" data-xf="${esc(f.name)}">
      ${f.audio ? `<audio controls preload="none" src="${f.url}"></audio>` : `<video controls preload="metadata" src="${f.url}#t=0.5"></video>`}
      <span class="xn" title="${esc(f.name)}">${esc(f.name)} <span class="muted small">· ${fmtSize(f.size)}</span></span>
      <a class="btn small" href="/api/extract/download/${encodeURIComponent(f.name)}" download data-tip="Save it to your computer (Downloads folder)">⬇ Download</a>
      <select data-xkeep="1" data-tip="Keep a copy in Media so the editor can use it"><option value="">+ Save to Media…</option><option value="auto">Auto (sort it)</option>
        ${f.audio ? `<option value="music">🎵 Music</option><option value="sounds">🔊 Sound effects</option>` : `<option value="videos">🎞 Videos</option><option value="transitions">✨ Transitions</option>`}</select>
      <button class="btn ghost small" data-xdel="1" data-tip="Delete (kept in Library/.trash/extracted)">🗑</button></div>`;
  const dismissed = store.get("cve.xdis") || [];
  box.innerHTML = live.filter((j) => !dismissed.includes(j.id)).map(jobRow).join("") + XD.files.slice(0, 20).map(fileRow).join("")
    + (XD.files.length > 20 ? `<div class="muted small">${XD.files.length - 20} older file(s) – open the folder to see them.</div>` : "");
  $$("[data-xdis]", box).forEach((b) => (b.onclick = () => { store.set("cve.xdis", [...dismissed, b.dataset.xdis].slice(-50)); renderExtract(); }));
  $$("[data-xf]", box).forEach((row) => {
    const n = row.dataset.xf;
    $("[data-xdel]", row).onclick = async () => { if (!confirm(`Delete “${n}”? (kept in Library/.trash/extracted)`)) return;
      try { toast((await api("/api/extract/remove", { method: "POST", body: { name: n } })).reply); loadExtract(); } catch (e) { toast(e.message, true); } };
    $("[data-xkeep]", row).onchange = async (e) => { const k = e.target.value; if (!k) return;
      try { const r = (await api("/api/extract/to_media", { method: "POST", body: { name: n, kind: k === "auto" ? null : k } })).result;
        toast(r.ok ? `Saved to ${MEDIA && MEDIA[r.kind] ? MEDIA[r.kind].label : r.kind} as “${r.name}”.` : `Not saved: ${r.why}`, !r.ok);
        if (r.ok) { LIBCAT = null; MEDIA_OPEN[r.kind] = true; loadMedia(); }
      } catch (er) { toast(er.message, true); } e.target.value = ""; };
  });
}
async function startExtract() {
  const src = xSource();
  if (!src) return toast("Paste a link, choose a file, or pick a clip from Media first.", true);
  const go = $("#xGo"); go.disabled = true; go.textContent = XFILE ? "Uploading…" : "Starting…";
  try {
    let j;
    if (src.file) {
      const fd = new FormData(); fd.append("file", src.file); fd.append("mode", XMODE); fd.append("from", $("#xFrom").value); fd.append("to", $("#xTo").value);
      const r = await fetch("/api/extract", { method: "POST", body: fd }); j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || "Upload failed");
    } else j = await api("/api/extract", { method: "POST", body: { ...src, mode: XMODE, from: $("#xFrom").value, to: $("#xTo").value } });
    toast("Started – it shows below; you can keep working.");
    XFILE = null; $("#xUrl").value = ""; $("#xMedia").value = ""; renderXChosen(); loadExtract();
  } catch (e) { toast(e.message, true); }
  finally { go.disabled = false; go.textContent = "⬇ Extract"; }
}
(() => {
  const box = $("#xBox"); if (!box) return;
  setXMode(XMODE);
  $$("#xModes button").forEach((b) => (b.onclick = () => setXMode(b.dataset.m)));
  $("#xFileBtn").onclick = () => $("#xFile").click();
  $("#xFile").onchange = () => { const f = $("#xFile").files[0]; if (f) { XFILE = f; $("#xMedia").value = ""; renderXChosen(); } $("#xFile").value = ""; };
  $("#xMedia").onchange = () => { if ($("#xMedia").value) XFILE = null; renderXChosen(); };
  $("#xGo").onclick = startExtract;
  $("#xUrl").onkeydown = (e) => { if (e.key === "Enter") startExtract(); };
  $("#xOpen").onclick = () => api("/api/extract/open", { method: "POST", body: {} });
  ["dragenter", "dragover"].forEach((ev) => box.addEventListener(ev, (e) => { e.preventDefault(); box.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => box.addEventListener(ev, (e) => { e.preventDefault(); box.classList.remove("over"); }));
  box.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (f) { XFILE = f; $("#xMedia").value = ""; renderXChosen(); return; }
    const t = e.dataTransfer && (e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain"));
    if (t && /^https?:\/\//i.test(t.trim())) { $("#xUrl").value = t.trim().split(/\s/)[0]; XFILE = null; renderXChosen(); }
  });
})();

/* ---------------- references + brief ---------------- */
function renderRefs() {
  const el = $("#refList");
  if (!P.references.length) { el.innerHTML = `<p class="muted">No references yet. Add sample videos or images whose style you want.</p>`; return; }
  el.innerHTML = P.references.map((r, i) => {
    const pic = r.sheet ? `<img src="${media("sheets", r.sheet)}" alt="">` : r.kind === "image" ? `<img src="${media("refs", r.file)}" alt="">` : `<div class="refph">${r.url ? "🔗 link" : "🎞"}</div>`;
    // big reference number on the picture – the same name used in the Look pickers (page 2 and Scenes)
    const img = `<div class="refpic">${pic}<span class="reftag">R${i + 1}</span></div>`;
    const stats = r.kind === "video" ? `${fmt(r.duration)} · ${r.shots} shots · avg shot <b>${r.avg_shot}s</b>`
      : r.kind === "pending" ? "analysing…" : r.kind === "error" ? `<span style="color:var(--err)">could not analyse: ${esc(r.error)}</span>` : esc(r.kind);
    const active = P.style_source === r.id;
    const studying = P.commands.some((c) => c.kind === "ref_style" && c.ref === r.id && c.status === "claude");
    return `<div class="ref ${active ? "active" : ""}">${img}<div class="meta">
      <div class="name"><b>R${i + 1}</b> ${esc(r.file || r.url)}</div>
      <div class="muted">${stats}</div>
      <div class="row">
        ${active ? `<span class="badge ok" title="Chosen in Scenes → Look">▶ used by the video</span>` : ""}
        ${curEditLook() === r.id ? `<span class="badge acc">editing</span>` : `<button class="btn small primary" data-useref="${esc(r.id)}" title="Show and edit this reference's look below (doesn't change the video)">Edit look</button>`}
        <button class="btn ghost small" data-study="${esc(r.id)}" ${studying ? "disabled" : ""} title="Claude studies this reference in depth (fonts, captions, pacing, audio) and fills in its look">${studying ? "Claude will study it…" : "Ask Claude to study"}</button>
        <button class="btn ghost small danger" data-rmref="${esc(r.id)}">Remove</button>${r.url ? `<a class="small muted" href="${esc(r.url)}" target="_blank" rel="noopener">link</a>` : ""}</div>
    </div></div>`;
  }).join("");
  $$("[data-rmref]", el).forEach((b) => (b.onclick = async () => {
    const r = P.references.find((x) => x.id === b.dataset.rmref);
    if (!confirm(`Remove ${r.file || r.url} and its style?${P.style_source === r.id ? "\nIt's the active style – the video goes back to the simple settings." : ""}\n(Undo brings it back.)`)) return;
    await api(`/api/projects/${PID}/reference/remove`, { method: "POST", body: { id: r.id } }); refresh(true);
  }));
  $$("[data-useref]", el).forEach((b) => (b.onclick = () => setEditLook(b.dataset.useref)));
  $$("[data-study]", el).forEach((b) => (b.onclick = async () => {
    const r = await api(`/api/projects/${PID}/reference/${b.dataset.study}/study`, { method: "POST" });
    toast(r.already ? "Already in Claude's inbox." : `Sent to Claude. ${claudeHint()}`); refresh(true);
  }));
}
// ---- looks. Page 2 = the look LIBRARY: pick a look to edit (doesn't change the video).
//      Scenes page = the Look dropdown: which look the video USES (style_source).
let editLook = null;  // look shown in the page-2 form; null = follow the one the video uses
const BK = "brandkit";
const lookName = (id) => { if (id === "none") return "None · simple"; if (id === BK) return "★ Brand Kit"; const i = P.references.findIndex((r) => r.id === id); return i < 0 ? "?" : `R${i + 1}`; };
function curEditLook() {
  const ids = ["none", ...(P.brandkit?.available ? [BK] : []), ...P.references.map((r) => r.id)];
  if (!editLook || !ids.includes(editLook)) editLook = store.get(`cve.editlook.${PID}`) || P.style_source || "none";
  if (!ids.includes(editLook)) editLook = P.style_source || "none";
  return editLook;
}
function renderLooks() {
  const refs = P.references || [], used = P.style_source || "none", ed = curEditLook();
  const chip = (id, label, title) => `<button class="lookchip ${ed === id ? "on" : ""} ${used === id ? "used" : ""}" data-look="${esc(id)}" title="${esc(title)}${used === id ? " – used by the video" : ""}">${used === id ? "▶ " : ""}${label}</button>`;
  $("#lookBar").innerHTML = `<span class="muted small">Edit look</span>` + chip("none", "None · simple", "Plain simple settings, no reference") +
    (P.brandkit?.available ? chip(BK, "★ Brand Kit", "Your approved brand look – shared by all projects (read-only here)") : "") +
    refs.map((r, i) => chip(r.id, `R${i + 1}`, r.file || r.url)).join("");
  $$("[data-look]", $("#lookBar")).forEach((b) => (b.onclick = () => setEditLook(b.dataset.look)));
  const i = refs.findIndex((r) => r.id === ed), r = refs[i];
  $("#lookTitle").innerHTML = `· ${ed === BK ? "viewing <b>★ Brand Kit</b>" : `editing ${r ? `look <b>R${i + 1}</b> (${esc((r.file || r.url || "").slice(0, 40))})` : "<b>your simple settings</b>"}`} ` +
    (ed === used ? `<span class="badge ok">▶ used by the video</span>` : `<span class="badge">not used by the video – choose it in Scenes → Look</span>`);
  const note = $("#lookNote");
  if (ed === BK) {
    note.innerHTML = `<b>★ Brand Kit is shared by all your projects</b>, so it's read-only here (settings greyed out) – changing it would change every project. To change it, edit <code>Brand Kit\brand-kit.md</code> or ask Claude. Its brief below can still be written.`;
    note.classList.remove("hidden");
  } else if (r) {
    const auto = (r.look_notes || []).length ? `Auto-detected from the reference: ${r.look_notes.map(esc).join(" · ")}.` : "Started from your simple settings.";
    note.innerHTML = `${auto} ${r.studied ? "<b>Claude studied this reference.</b>" : "For the full look (fonts, captions, pacing, audio) click <b>Ask Claude to study</b> on its card."}`;
    note.classList.remove("hidden");
  } else note.classList.add("hidden");
  renderLookPick();
}
async function setEditLook(id) {
  if (curEditLook() === id) return;
  if (briefDirty) { try { await saveBrief(); } catch (e) { return toast(e.message, true); } }  // edits belong to the look being left
  editLook = id; store.set(`cve.editlook.${PID}`, id);
  briefDirty = false; renderLooks(); renderBrief(true); renderRefs();
}
// Scenes page: which look the video uses
function renderLookPick() {
  const sel = $("#lookSelect"), used = P.style_source || "none";
  const opts = [["none", "None · simple settings"], ...(P.brandkit?.available ? [[BK, "★ Brand Kit (your brand look)"]] : []),
    ...P.references.map((r, i) => [r.id, `R${i + 1} · ${(r.file || r.url || "").slice(0, 42)}`])];
  if (!opts.some(([id]) => id === used)) opts.push([used, used === BK ? "★ Brand Kit (file missing – settings kept)" : "(look no longer exists)"]);
  const html = opts.map(([id, l]) => `<option value="${esc(id)}">${esc(l)}</option>`).join("");
  if (sel.dataset.h !== html) { sel.innerHTML = html; sel.dataset.h = html; }
  sel.value = used;
  const v = P.looks_view?.[used] || {};
  $("#lookPickInfo").textContent = `${v.format || ""} · ${v.layout === "split" ? "split layout" : "full screen"} · captions ${v.captions === false ? "off" : v.caption_script === "hinglish" ? "Hinglish" : "on"}${v.ref_audio && v.ref_audio !== "none" ? " · reference background audio" : v.music && v.music !== "off" ? " · music" : ""}`;
}
async function switchLook(id) {
  if ((P.style_source || "none") === id) return;
  try {
    const r = await api(`/api/projects/${PID}/style_source`, { method: "POST", body: { source: id } });
    lastSig.refs = null; await refresh(true);
    toast(`The video now uses ${lookName(id)}.${r.notes && r.notes.length ? " " + r.notes.join(", ") + "." : ""} The edited view updates by itself. (Undo reverts.)`);
    SHOW = { sid: selected, switchView: true, at: Date.now() };  // a look is only visible in the edited view
    loadPlayer(false); autoPreviewTick(); renderPvBadge();
  } catch (e) { toast(e.message, true); renderLookPick(); }
}
$("#lookSelect").onchange = (e) => switchLook(e.target.value);
$("#lookEdit").onclick = () => { editLook = P.style_source || "none"; store.set(`cve.editlook.${PID}`, editLook); briefDirty = false; switchTab("refs"); renderLooks(); renderBrief(true); };
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
/* ---------------- "?" help: simple, short, with an example ---------------- */
// [title, what it is (1 line), how it works / why it matters (2-3 simple sentences), example, best for you]
const HELP = {
  format: ["Format", "The shape of your video.",
    "Every app shows videos in its own shape. If your video's shape matches the app, it fills the whole screen. If not, the app adds empty space around it and it looks small.",
    "9:16 = tall, fills a phone screen (Reels, Shorts).\n16:9 = wide, like TV (normal YouTube).\n1:1 = square.", "9:16"],
  fit: ["Fit", "What to do when your clip's shape doesn't match the video.",
    "Your phone clip may be a different shape from the video you're making. The studio can either zoom in until there's no gap, or show the full picture and fill the gap with black.",
    "Crop to fill → zooms in, no black bars (good for faces).\nFit with bars → whole picture, black bars on the sides (good for charts).", "Crop to fill"],
  captions: ["Captions", "Words of your speech shown on the video.",
    "Most people scroll with the sound off – in a bus, at work, late at night. Without captions they don't know what you're saying and scroll away. With captions they read along and keep watching.",
    "You say: \"Bajaj ka IPO aa chuka hai\"\nScreen shows: Bajaj ka IPO aa chuka hai", "On"],
  caption_script: ["Caption script", "How Hindi words are written.",
    "You speak Hindi, but captions can be written two ways. Hinglish uses English letters – most Indian Instagram users read it fastest. Hindi script is better if your viewers read Hindi daily.",
    "Hinglish → aap kaise ho\nHindi → आप कैसे हो", "Hinglish"],
  caption_position: ["Caption position", "Where captions sit on the screen.",
    "Captions should be easy to see but must not cover your face or the app's buttons. In the split layout this setting is ignored – captions always sit on the orange line in the middle.",
    "Bottom → classic.  Center → eye-catching.  Top → keeps the bottom clear.", "Bottom"],
  caption_case: ["Caption case", "Normal letters or ALL CAPS.",
    "CAPITAL letters look bold and loud, good for hype or motivation. But they're slower to read. Normal letters feel calm and are easier to read in longer sentences.",
    "Normal → aaj market upar hai\nUPPERCASE → AAJ MARKET UPAR HAI", "Normal"],
  caption_words: ["Words per caption", "How many words show at one time.",
    "The captions change as you talk. With fewer words, the eye reads them in one glance and the video feels fast. With more words, the captions change less often and feel calmer.",
    "2 → \"aaj market\"\n4 → \"aaj market upar hai\"\n6 → \"aaj market upar hai, Nifty bhi\"", "3–4"],
  caption_color: ["Caption colour", "Colour of the caption text.",
    "Captions sit on top of your video, which can be light or dark. White letters with a black outline stay readable on both. Change the colour only if your brand needs it.",
    "White → readable on a bright wall and on a dark shirt.", "White"],
  highlight_color: ["Highlight colour", "Colour of the word being spoken right now.",
    "While you talk, the word you're saying lights up in this colour. It guides the viewer's eye and makes the captions feel alive. Using your brand colour makes every video look like yours.",
    "Captions: aaj [market] upar hai\n→ \"market\" turns orange exactly while you say \"market\".", "Your orange #F97316"],
  caption_font: ["Caption font", "The letter style of captions.",
    "The font sets the mood: clean, bold, playful. Poppins comes with the studio and works for English letters and Hindi script. Another font only works if it's installed on this PC.",
    "Poppins → clean and modern.\nArial → plain and simple.", "Poppins"],
  remove_silence: ["Remove silences", "Cuts the pauses between your sentences.",
    "When we talk, we pause to think. On camera these pauses feel slow and people scroll away. This setting cuts them out automatically but keeps a tiny breath, so it still sounds natural.",
    "Before: \"IPO aaya … (1 sec) … aur list hua\"\nAfter:  \"IPO aaya, aur list hua\" – tighter and faster.", "Yes"],
  silence_min: ["Min silence (s)", "How long a pause must be before it's cut.",
    "Only pauses longer than this number of seconds get cut. A small number makes the video fast and tight. A bigger number keeps more natural breathing room.",
    "0.35 → cuts most pauses (fast reel).\n1.0 → only cuts long pauses (relaxed video).", "0.35"],
  language: ["Language", "The language you speak in the video.",
    "The studio listens to your voice and writes down every word. It works best when it knows the language. On 'Auto detect' it sometimes confuses Hindi with Urdu and writes the wrong script.",
    "Hindi → your words come out right: \"Bajaj ka IPO\".\nAuto → may come out in Urdu letters.", "Hindi"],
  logo: ["Logo", "Your logo on the video.",
    "A small logo in the corner tells people the video is yours, even when someone reposts it. Put a PNG file of your logo in the Brand Kit folder, then pick it here.",
    "logo.png in Brand Kit → choose it → it appears small in a corner of every video.", "Optional"],
  logo_position: ["Logo position", "Which corner the logo goes in.",
    "Instagram and YouTube put their own buttons on the screen (like, comment, share). Put the logo where those buttons won't cover it.",
    "Top-right → usual spot.\nBottom-right → avoid: the like/comment buttons cover it.", "Top-right"],
  layout: ["Layout", "How the screen is arranged.",
    "Split puts a card (title, number or chart) on top and you below – the key number stays on screen while you explain it. Full screen shows only you, the classic talking-head look.",
    "Split → top: \"2x+ listing gain\", bottom: you talking.\nFull screen → only you.", "Split"],
  headline: ["Headline card", "The big title on top (split layout).",
    "It shows whenever a scene has no card of its own. Write your main hook here. Put stars around a word to make it orange.",
    "Bajaj *IPO* listing aaj → shows \"Bajaj IPO listing aaj\" with IPO in orange.", "Your hook, 2–5 words"],
  bg_file: ["Behind me", "The picture or video that shows where the green screen was.",
    "Pick one from your Media tab (Pictures / Videos), or click ⬆ to upload one. A picture stays for the whole video; a video loops and runs on across cuts – it doesn't restart at every scene. Its sound is never used.",
    "Picture → a city skyline / office / stock chart behind you.\nVideo → a moving market screen / b-roll loop behind you.", "–"],
  bg_mode: ["Green screen", "What shows where the green screen behind you was.",
    "Film in front of a green cloth and the studio swaps the green for something else – in full screen and in the split layout. If there's no green behind you, nothing changes.",
    "Keep as filmed → stays green.\nClean wall → a clean off-white studio wall.\nPicture behind me → any photo (office, skyline, chart).\nVideo behind me → a moving clip that loops behind you while you talk.", "Clean wall"],
  face_y: ["Face position", "Moves you up or down in the bottom half.",
    "In the split layout you appear in the bottom half. This number tells the studio where your eyes are in your clip, so it can frame you nicely – not too high, not too low.",
    "Head cut off at the top? → make the number bigger (0.30).\nToo much space above your head? → smaller (0.22).", "0.27"],
  music: ["Music", "Soft background music under your voice.",
    "Music fills silences and adds energy. It automatically gets softer whenever you talk, so your words stay clear. Use your own song by putting it in the Brand Kit\\music folder.",
    "Quiet → music plays, and gets softer whenever you talk.\nOff → only your voice.", "Quiet"],
  ref_audio: ["Background audio", "Play a reference video's background music under your voice.",
    "Pick R1, R2… and the studio takes that reference's VOICE out (an AI voice-remover, about a minute the first time) and plays only its background music under you – softer whenever you talk, like Music. It replaces the Music setting while on. Careful: music inside someone else's video is usually copyrighted – fine to practise with, Instagram / YouTube may mute or claim a published reel.",
    "None → only your voice (and Music, if on).\nR2 → R2's background music, voice removed, under your voice.", "None"],
  audio_polish: ["Audio polish", "Makes your voice clean and even.",
    "Phone recordings often have fan or AC hum, and some words come out louder than others. Audio polish removes the hum, evens out your voice, and makes every video equally loud.",
    "Before: soft words hard to hear, fan hum.\nAfter: clear, even voice, no hum.", "On"],
  loudness: ["Speech loudness", "How loud your voice is in the final video.",
    "Apps play videos one after another. If yours is quieter than the next one, people think the sound is broken. -14 matches the loudest Reels and Shorts. Closer to 0 = louder.",
    "-14 → as loud as top Reels/Shorts.\n-16 → standard.  -18 → calm.", "-14"],
  duck_db: ["Music duck (dB)", "How much the music drops when you speak.",
    "When you start talking, the music automatically goes softer, and comes back up in your pauses. This number is how much softer. Bigger number = music drops more, words clearer.",
    "0 → music stays loud (words hard to hear).\n18 → music goes soft under your voice, words crystal clear.", "18"],
};
let helpOpen = null;
function setupHelp() {
  for (const lab of $$("#settingsForm label")) {
    const f = lab.querySelector("[name]"); if (!f || !HELP[f.name] || lab.querySelector(".qhelp")) continue;
    // label text + "?" on ONE row (the label is a vertical flex box, so wrap them together)
    const row = document.createElement("span");
    row.className = "lt";
    [...lab.childNodes].filter((n) => n !== f && n.nodeType === Node.TEXT_NODE).forEach((n) => row.appendChild(n));
    const b = document.createElement("button");
    b.type = "button"; b.className = "qhelp"; b.textContent = "?"; b.dataset.k = f.name;
    b.title = HELP[f.name][1];
    b.setAttribute("aria-label", `What is ${HELP[f.name][0]}?`);
    row.appendChild(b);
    // the field may sit inside a wrapper (select + ⬆ button): insert before that wrapper, not the nested field
    const anchor = f.parentNode === lab ? f : [...lab.children].find((c) => c.contains(f)) || null;
    lab.insertBefore(row, anchor);
    b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); helpOpen && helpOpen.btn === b ? closeHelp() : showHelp(b); };
  }
}
function showHelp(btn) {
  closeHelp();
  const k = btn.dataset.k, [title, what, how, example, best] = HELP[k];
  const shared = !STYLE_KEYS.has(k);
  const pop = document.createElement("div");
  pop.className = "helppop"; pop.setAttribute("role", "dialog");
  pop.innerHTML = `<button class="hp-x" aria-label="Close">×</button>
    <div class="hp-title">${esc(title)}</div>
    <div class="hp-what">${esc(what)}</div>
    <div class="hp-how">${esc(how)}</div>
    <div class="hp-ex"><span>Example</span>${esc(example).replace(/\n/g, "<br>")}</div>
    <div class="hp-best">Best for you: <b>${esc(best)}</b></div>
    <div class="hp-tag">${shared ? "Same for every look" : "Each look (R1, R2…) can have its own"}</div>`;
  document.body.appendChild(pop);
  const r = btn.getBoundingClientRect(), pw = Math.min(440, window.innerWidth - 24);
  pop.style.width = `${pw}px`;
  pop.style.left = `${Math.max(12, Math.min(window.innerWidth - pw - 12, r.left - 16))}px`;
  const below = r.bottom + 10 + pop.offsetHeight < window.innerHeight;
  pop.style.top = `${window.scrollY + (below ? r.bottom + 10 : Math.max(8, r.top - pop.offsetHeight - 10))}px`;
  pop.querySelector(".hp-x").onclick = closeHelp;
  helpOpen = { pop, btn }; btn.classList.add("on");
}
function closeHelp() { if (helpOpen) { helpOpen.pop.remove(); helpOpen.btn.classList.remove("on"); helpOpen = null; } }
document.addEventListener("click", (e) => { if (helpOpen && !helpOpen.pop.contains(e.target)) closeHelp(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeHelp(); });
window.addEventListener("scroll", closeHelp, { passive: true });
setupHelp();

const STYLE_KEYS = new Set(["format", "fit", "fps", "captions", "caption_script", "caption_font", "caption_color", "highlight_color",
  "caption_position", "caption_case", "caption_words", "remove_silence", "silence_min", "logo", "logo_position", "layout", "split_ratio",
  "bg_mode", "bg_color", "bg_file", "panel_color", "grade", "audio_polish", "music", "loudness", "duck_db", "ref_audio"]);
// 🟩 green screen: picture / video behind you – the file list follows the mode; ⬆ uploads into Media and picks it
function fillBgFile() {
  const mode = $('#settingsForm [name="bg_mode"]').value, sel = $('#settingsForm [name="bg_file"]');
  $("#bgFileLab").classList.toggle("hidden", !["picture", "video"].includes(mode));
  const list = ((P.bg_media || {})[mode === "picture" ? "pictures" : "videos"]) || [];
  const keep = sel.value || sel.dataset.want || "";
  const sig = mode + "|" + list.join("~");
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    sel.innerHTML = `<option value="">${list.length ? "– pick one –" : mode === "picture" ? "no pictures yet – ⬆ upload one" : "no videos yet – ⬆ upload one"}</option>` +
      list.map((f) => `<option value="${esc(f)}">${esc(f.split("/").pop())}</option>`).join("");
  }
  sel.value = list.includes(keep) ? keep : "";
}
$('#settingsForm [name="bg_mode"]').addEventListener("change", fillBgFile);
$("#bgUp").onclick = () => $("#bgUpFile").click();
$("#bgUpFile").onchange = async () => {
  const f = $("#bgUpFile").files[0]; $("#bgUpFile").value = ""; if (!f) return;
  const mode = $('#settingsForm [name="bg_mode"]').value, kind = mode === "picture" ? "pictures" : "videos";
  const fd = new FormData(); fd.append("files", f);
  toast("Uploading…");
  try {
    const r = await fetch(`/api/media/upload?kind=${kind}`, { method: "POST", body: fd }).then((x) => x.json());
    const res = (r.results || [])[0] || {};
    if (!res.ok && !res.dup) return toast(res.why || r.error || "Upload failed", true);
    await refresh(true);
    const name = res.ok ? `${kind === "pictures" ? "Pictures" : "Videos"}/${res.name}` : null;
    const sel = $('#settingsForm [name="bg_file"]');
    if (name) { sel.dataset.want = name; fillBgFile(); sel.value = name; briefDirty = true; $("#briefState").textContent = "unsaved changes"; }
    toast(res.ok ? `Uploaded – click “Save brief & settings” to use it.` : res.why, !res.ok);
  } catch (e) { toast(e.message, true); }
};
// 🎵 Background audio: None, or a reference's background music with its voice taken out (one entry per reference)
function fillRefAudio() {
  const sel = $('#settingsForm [name="ref_audio"]'); if (!sel) return;
  const opts = [["none", "None"]].concat((P.references || []).filter((r) => r.kind !== "error" && r.file).map((r) => {
    const i = P.references.indexOf(r) + 1;
    return [`ref:${r.id}`, `R${i} – its background music (voice removed)${r.bg_ready ? "" : P.ref_audio_tool ? " · prepared on save" : " · needs Setup"}`]; }));
  const want = opts.map((o) => o.join("|")).join("~");
  if (sel.dataset.sig === want) return;
  sel.dataset.sig = want;
  const keep = sel.value;
  sel.innerHTML = opts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
}
function renderBrief(force) {
  const ed = curEditLook(), nm = lookName(ed);
  $("#lookBriefTag").textContent = nm;
  $("#lookBriefLabel").innerHTML = `Brief for <b>${esc(nm)}</b> <span class="muted small">– how this look should feel (each look has its own)</span>`;
  if (!briefDirty || force) {
    $("#brief").value = P.brief || "";
    $("#lookBrief").value = (P.look_briefs || {})[ed] || "";
    const look = (P.looks_view || {})[curEditLook()] || P.settings;  // style keys come from the look being edited
    fillRefAudio();
    { const bf = $('#settingsForm [name="bg_file"]'); bf.dataset.want = String(((P.looks_view || {})[curEditLook()] || P.settings).bg_file || ""); }
    for (const el of $$("#settingsForm [name]")) {
      const v = STYLE_KEYS.has(el.name) && el.name in look ? look[el.name] : P.settings[el.name];
      const sv = v === null || v === undefined ? "" : String(v);
      // a value the dropdown doesn't list (e.g. music = "mytrack.mp3" set by a command) must survive a Save:
      // otherwise the select shows blank and saving silently changes the setting
      if (el.tagName === "SELECT" && sv && ![...el.options].some((o) => o.value === sv)) el.add(new Option(sv, sv));
      el.value = sv;
    }
    fillBgFile();
  }
  // ★ Brand Kit is read-only (shared by every project): grey out its style fields; footage fields stay editable
  for (const el of $$("#settingsForm [name]")) el.disabled = ed === BK && STYLE_KEYS.has(el.name);
  $("#briefState").textContent = briefDirty ? "unsaved changes" : P.updated ? `saved` : "";
}
["#brief", "#lookBrief"].forEach((sel) => $(sel).addEventListener("input", () => { briefDirty = true; $("#briefState").textContent = "unsaved changes"; }));
$$("#settingsForm [name]").forEach((el) => el.addEventListener("change", () => { briefDirty = true; $("#briefState").textContent = "unsaved changes"; }));
function collectSettings() {
  const s = {};
  for (const el of $$("#settingsForm [name]")) {
    if (el.disabled) continue;  // read-only (★ Brand Kit) – not sent
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
  await api(`/api/projects/${PID}/brief`, { method: "POST", body: { brief: $("#brief").value, look_brief: $("#lookBrief").value, settings: collectSettings(), look: curEditLook() } });
  briefDirty = false; await refresh(true);
}
$("#saveBrief").onclick = () => saveBrief().then(() => toast("Saved.")).catch((e) => toast(e.message, true));
// ---- ✨ Claude: plan the edit (Scenes page) -> Claude builds a STORYBOARD; nothing renders before you approve
function planPending() { return P.commands.some((c) => ["plan_edit", "first_cut"].includes(c.kind) && c.status === "claude"); }
function renderPlanBtn() {
  const b = $("#planBtn"), j = P.job || {};
  const analyzing = j.kind === "analyze" && (j.status === "running" || j.status === "queued");
  const pend = planPending();
  b.disabled = pend || analyzing || !P.scenes.length;
  b.textContent = pend ? (CBAUTO() ? "① Plan requested – Claude is on it" : "① Plan requested – say “process studio inbox”") : "① Plan story (Claude)";
  b.title = pend ? `Waiting for Claude: ${claudeHint()} (Cancel it in the Claude inbox.)`
    : analyzing ? "Wait for the analysis to finish – the scenes are about to change"
    : !P.scenes.length ? "Analyze your footage first (Footage tab)"
    : "Step ① (optional) – Claude plans the STORY beat by beat (keep / cut / reorder, hook, cards, B-roll) as a storyboard – you approve before anything changes. Then ② Auto-edit decorates the final cut.";
}
$("#planBtn").onclick = async () => {
  try {
    if (briefDirty) await saveBrief();  // unsaved brief on page 2 belongs in the plan
    const f = P.footage || {};
    if (f.new && !confirm(`${f.new} clip(s) are not analyzed yet – their scenes won't be in the plan.\n\nPlan the edit anyway?`)) return;
    if (P.storyboard && P.storyboard.status !== "applied" &&
        !confirm("A storyboard is already waiting for your review.\nMake a new plan? (Your reviews of beats that don't change are kept.)")) return;
    const r = await api(`/api/projects/${PID}/first_cut`, { method: "POST" });
    if (r.already) toast(`Already requested. ${claudeHint()}`);
    else toast(`Sent to Claude. ${claudeHint()} – a storyboard will appear for you to approve.` +
      (!(P.brief || "").trim() ? " Tip: fill “About this video” on page 2 for a better plan." : ""));
    refresh(true);
  } catch (e) { toast(e.message, true); }
};

/* ---------------- scenes ---------------- */
function sceneBadges(s) {
  const b = [];
  if (s.role === "broll") b.push(`<span class="badge broll" title="B-roll footage: covers speaker scenes, not played on its own">B-ROLL</span>`);
  if (s.broll_view) b.push(`<span class="badge ${s.broll_view.ok ? "broll" : "warn"}" title="${esc(s.broll_view.src)}">🎞 ${s.broll_view.where}</span>`);
  if (!s.keep) b.push(`<span class="badge cut">CUT</span>`);
  if (s.speed && s.speed !== 1) b.push(`<span class="badge acc">${s.speed}x</span>`);
  if (s.mute) b.push(`<span class="badge">muted</span>`);
  else if (s.volume !== undefined && s.volume !== 1) b.push(`<span class="badge">vol ${Math.round(s.volume * 100)}%</span>`);
  if (s.zoom && s.zoom > 1) b.push(`<span class="badge acc">zoom ${s.zoom}x</span>`);
  if ((s.punches || []).length) b.push(`<span class="badge acc" title="Zoom moments (punch-ins) inside this scene">🔍 ${s.punches.length}</span>`);
  if (s.text) b.push(`<span class="badge acc">text</span>`);
  if (s.transition) b.push(`<span class="badge acc" title="Transition into this scene">⇄ ${esc((LIBCAT?.transitions || []).find((t) => t.id === s.transition.type)?.name || s.transition.type)}</span>`);
  if ((s.effects || []).length) b.push(`<span class="badge acc" title="Effects">✨ ${s.effects.length}</span>`);
  if ((s.stickers || []).length) b.push(`<span class="badge acc" title="Stickers">🙂 ${s.stickers.length}</span>`);
  if ((s.sfx || []).length) b.push(`<span class="badge acc" title="Sound effects">🔊 ${s.sfx.length}</span>`);
  if ((s.lib_missing || []).length) b.push(`<span class="badge warn" title="Missing from the Library or can't be read – left out of the video: ${esc(s.lib_missing.join(", "))}">⚠ library file missing</span>`);
  if (s.card && s.card.kind !== "none") b.push(`<span class="badge acc">${esc(s.card.kind)} card</span>`);
  if (s.captions === false) b.push(s.captions_auto_off ? `<span class="badge" title="No speech detected (music / ambient sound) – captions switched off automatically. Type 'captions on' to bring them back.">no speech · captions off</span>` : `<span class="badge">no captions</span>`);
  else if (s.no_speech) b.push(`<span class="badge warn" title="Very few words for its length – maybe music or noise. You turned captions on for it.">few words</span>`);
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
  // clip dividers: show how the complete video is built when it uses more than one clip
  const multi = new Set(P.scenes.map((s) => s.src)).size > 1;
  const clipNo = (f) => P.sources.findIndex((x) => x.file === f) + 1;
  el.innerHTML = P.scenes.map((s, i) => `
    ${multi && (i === 0 || P.scenes[i - 1].src !== s.src) ? `<div class="clip-div ${s.src_skipped ? "skipped" : ""}"><b>Clip ${clipNo(s.src)}</b> ${esc(s.src)}${s.src_skipped ? " · skipped in Footage – not in the video" : ""}</div>` : ""}
    <div class="scene ${s.keep ? "" : "cut"} ${s.src_skipped ? "cut" : ""} ${s.id === selected ? "sel" : ""}" data-id="${s.id}">
      <img src="${media("thumbs", s.thumb)}" loading="lazy" alt="">
      <span class="no">${s.no}</span>
      <div class="info">
        <div class="times">${fmt(s.in)}–${fmt(s.out)} · ${s.dur}s${s.keep && Math.abs(s.est - s.dur) > 0.05 ? ` → ${s.est}s` : ""} ${sceneBadges(s)}</div>
        <div class="txt">${s.heard || (s.no_speech && s.captions === false) ? `<span class="muted">🎵 no speech – music / background sound</span>` : esc(s.caption_view ?? s.transcript) || `<span class="muted">(no speech)</span>`}</div>
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
  // re-analysis replaces scene ids: a stale selection must fall back to the first VIDEO scene, not "Select a scene."
  if ((!selected || !P.scenes.some((x) => x.id === selected)) && P.scenes.length)
    selectScene((P.scenes.find((x) => !isBroll(x)) || P.scenes[0]).id, false);
  if (!STL.drag && !STL.busy) renderSceneTimeline();  // never redraw under a drag in progress
}
function curScene() { return P?.scenes.find((s) => s.id === selected); }
function selectScene(id, play = true) {
  selected = id;
  $$(".scene").forEach((c) => c.classList.toggle("sel", c.dataset.id === id));
  $$(".sblk").forEach((c) => c.classList.toggle("sel", c.dataset.id === id));
  renderDetail(); loadPlayer(play);
}
/* ---- Inspector: direct controls for the selected scene. Every control sends a normal command (same grammar as the
   box below), so it lands in the history, is undoable, and Claude/CLI can do the same thing. */
const isBroll = (s) => s && s.role === "broll";
const inVid = (s) => s && s.keep && s.role !== "broll";  // plays in the edited video
const r2 = (x) => Math.round((+x || 0) * 100) / 100;
/* Hover help: every Inspector control says what it does (shown by the floating tip box, see initTips) */
const ITIP = {
  speed: { "0.75": "Slow motion – the scene plays at 3/4 speed (longer, slower voice).", "1": "Normal speed, as recorded.",
    "1.25": "A bit faster – removes dead time while the voice still sounds natural. Good for reels.", "1.5": "Fast – noticeably quicker speech. For less important parts.",
    "2": "Double speed – quick fillers or a fast-forward effect." },
  zoom: { "1": "No zoom – the full frame.", "1.1": "Slight punch-in (10 %) – subtle emphasis.", "1.2": "Punch-in 20 % – classic jump-cut zoom to highlight a point.",
    "1.3": "Close-up 30 % – strong emphasis on your face.", "1.5": "Tight close-up 50 % – very dramatic, can look soft on low-resolution footage." },
  captions: { auto: "Follow the whole video: captions on, except scenes with no speech (those turn off by themselves).",
    on: "Always show captions in this scene.", off: "Never show captions in this scene (e.g. when a card or B-roll already says it)." },
  cappos: { auto: "Follow the whole video: its exact height if you set one (the % box), else the look's position (split layout: on the line above your head, ≈ 44 %).",
    head: "Captions high – over your head (split layout: just under the line, on your forehead/hair). Use when something important is low.",
    center: "Captions in the middle of the picture – most eye-catching, but can cover your face.",
    below: "Captions low – near the bottom of the screen, under your chest. Keeps your face clear." },
  pauses: { auto: "Follow the whole-video setting for cutting pauses (shown on the right).",
    cut: "Cut the silent pauses out of this scene – jump cuts, faster pace.", keep: "Keep every pause in this scene – natural, calm pace." },
  bwhere: { top: "Top: split layout = the top panel (card area), you stay visible below. Full screen layout = the top half of the screen, you in the bottom half.",
    bottom: "Bottom: split layout = over YOU in the bottom panel, the card stays on top. Full screen layout = the bottom half, you in the top half.",
    full: "Show the B-roll over the whole screen (hides you and the card); your voice keeps playing." },
  bfit: { fill: "Fill its space – edges are cut off if the shape differs (a wide video in a tall space loses its sides).",
    fit: "Show the WHOLE picture – nothing cut off; the empty space is a soft blurred copy of it, not black bars. Best for charts and screenshots." },
  bcaps: { on: "Captions shown on top of the B-roll in this scene.", off: "No captions in this scene (the B-roll stays clean)." },
  role: { speaker: "Normal scene: plays in the video with its picture and sound.",
    broll: "Take this scene OUT of the video and use its picture only as B-roll over other scenes. Its own sound is not heard." },
};
// caption height in % from the top – mirrors pipeline.caption_center_y / caption_band (for the number box + mini map)
function capPctOf(s, cp) {
  const split = P.settings.layout === "split", seam = (+P.settings.split_ratio || 0.44) * 100;
  if (typeof cp === "number") return cp;
  if (cp === "auto" && typeof P.settings.caption_y === "number") return P.settings.caption_y;  // the video's exact height
  if (split) return { auto: seam, head: seam + (100 - seam) * 0.16, center: seam + (100 - seam) / 2, below: 78 }[cp] ?? seam;
  if (cp === "head") return 22; if (cp === "center") return 50; if (cp === "below") return 78;
  return { bottom: 75, center: 50, top: 25 }[P.settings.caption_position || "bottom"];
}
function capMini(s) {  // tiny phone: card area, Reels-covered bottom, where the captions are
  const split = P.settings.layout === "split", seam = (+P.settings.split_ratio || 0.44) * 100;
  const y = capPctOf(s, s.cap_pos || "auto");
  return `<span class="capmini" data-tip="Where the captions sit on the phone screen. Dark blue = top card, red = covered by the Reels/Shorts caption + buttons.">
    ${split ? `<i class="cm-card" style="height:${seam}%"></i>` : ""}<i class="cm-ui"></i><i class="cm-cap" style="top:${y}%"></i></span>`;
}
// tiny phone: where the B-roll covers the screen (orange) – split layout shows the card panel line
function brMini(where, split) {
  const seam = split ? (+P.settings.split_ratio || 0.44) * 100 : 50;
  const [t, hgt] = where === "full" ? [0, 100] : where === "top" ? [0, seam] : [seam, 100 - seam];
  return `<span class="capmini brmini" data-tip="Orange = where the B-roll shows on the phone screen."><i class="cm-br" style="top:${t}%;height:${hgt}%"></i>${where !== "full" ? `<i class="cm-seam" style="top:${seam}%"></i>` : ""}</span>`;
}
// upload a B-roll video/picture for one scene (kept in the project's Assets; the same file twice is reused)
let BRUP = null, BRMANAGE = false;
async function deleteBrollAsset(file) {
  const a = (P.broll_assets || []).find((x) => x.file === file);
  if (!a) return;
  if (!confirm(`Delete “${file}” from this project?${a.used_by.length ? `\nScene ${rangeTxt(a.used_by)} will no longer show it (you show there again).` : ""}\n(Undo brings it back.)`)) return;
  try {
    const r = await api(`/api/projects/${PID}/broll_asset/remove`, { method: "POST", body: { file } });
    toast(r.reply);
    await refresh(true); loadPlayer(false); autoPreviewTick(); renderPvBadge();
  } catch (e) { toast(e.message, true); }
}
function uploadBroll(file, sid) {
  if (BRUP) return toast("One upload at a time – wait for the current one to finish.", true);
  const fd = new FormData(); fd.append("files", file);
  BRUP = { sid, pct: 0 };
  const show = () => { const el = $("#brUpProg"); if (el && selected === sid) el.textContent = BRUP ? `uploading ${BRUP.pct} %…` : ""; };
  show();
  const x = new XMLHttpRequest();
  x.open("POST", `/api/projects/${PID}/upload?kind=broll&scene=${encodeURIComponent(sid)}`);
  x.upload.onprogress = (e) => { if (e.lengthComputable && BRUP) { BRUP.pct = Math.round((100 * e.loaded) / e.total); show(); } };
  const done = async (ok, j) => {
    BRUP = null; show();
    if (!ok) return toast(j.error || "Upload failed – is the black Start Studio window still open?", true);
    toast(j.reply || j.note || `Uploaded ${j.file}.`, !!j.note);
    if (j.reply) SHOW = { sid, switchView: true, at: Date.now() };
    await refresh(true); loadPlayer(false); autoPreviewTick(); renderPvBadge();
  };
  x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch {} done(x.status < 400, j); };
  x.onerror = () => done(false, {});
  x.send(fd);
}
/* ---- ✨ LIBRARY: transitions, effects, stickers, sound effects for the selected scene (Library/ folder on disk).
   Every click sends a normal scene command (undoable, in the history, same grammar for Claude / the CLI). */
let LIBCAT = null, LIBTAB = store.get("cve.libtab") || "transition", LIBPOLL = null;
const LIBQ = { sticker: "", sgroup: "picks", sound: "", scat: "whooshes" };
let LIBAUDIO = null;
async function loadLib(force) {
  if (!LIBCAT || force) { try { LIBCAT = await api("/api/library"); } catch { LIBCAT = null; } }
  return LIBCAT;
}
function libPlay(file, btn) {
  if (!LIBAUDIO) LIBAUDIO = new Audio();
  const url = media("library", file);
  if (!LIBAUDIO.paused && LIBAUDIO.dataset.f === file) { LIBAUDIO.pause(); if (btn) btn.textContent = "▶"; return; }
  $$("[data-play]").forEach((b) => (b.textContent = "▶"));
  LIBAUDIO.src = url; LIBAUDIO.dataset.f = file; LIBAUDIO.play().catch(() => toast("Can't play this sound in the browser.", true));
  if (btn) { btn.textContent = "■"; LIBAUDIO.onended = () => (btn.textContent = "▶"); }
}
function libTile(kind, it, active) {
  return `<button class="libtile ${active ? "on" : ""}" data-lib="${kind}" data-id="${esc(it.id)}" data-tip="${esc(it.desc || it.name)}${active ? " (click to remove)" : ""}">
    ${it.preview ? `<video muted loop playsinline preload="metadata" src="${media("library", it.preview)}#t=0.9"></video>` : `<i class="noprev">${kind === "transition" ? "⇄" : "✨"}</i>`}<span>${esc(it.name)}</span></button>`;
}
function renderLibSection(s) {
  const box = $("#libSec"); if (!box) return;
  const head = (inner) => `<div class="ilab hastip" data-tip="Transitions, effects, stickers and sound effects from your Library folder (free packs: Microsoft Fluent Emoji – MIT, Wyzowl SFX Kit – CC0). Your own: put PNG/GIF in Library\\Stickers\\My stickers, WAV/MP3 in Library\\Sounds\\My sounds.">✨ Library
    <span class="spacer"></span><button class="lnk" id="libOpen" data-tip="Open the Library folder (add your own stickers / sounds there)">📁 folder</button>
    <button class="lnk" id="libReload" data-tip="Read the Library folder again (after adding your own files)">↻</button></div>${inner}`;
  if (!LIBCAT) { box.innerHTML = head(`<span class="muted small">loading…</span>`); loadLib().then(() => { if (curScene()?.id === s.id) renderLibSection(s); }); return; }
  const L = LIBCAT;
  if (!L.ready) {
    const st = L.setup || {};
    box.innerHTML = head(`<div class="ibox">The Library isn't downloaded yet: ~1,600 3D stickers (Microsoft Fluent Emoji, MIT) and 320 sound effects (Wyzowl SFX Kit, CC0) – about 120 MB, once.
      ${st.running ? `<div class="muted small">⏳ ${esc((st.log || []).slice(-1)[0] || "working…")}</div>` : `<button class="btn small primary" id="libSetup">⬇ Download the free Library</button>`}
      <div class="muted small">Transitions and effects work without it.</div></div>`) + libTabs(s, L);
    if (st.running && !LIBPOLL) LIBPOLL = setInterval(async () => { await loadLib(true); if (!LIBCAT.setup?.running) { clearInterval(LIBPOLL); LIBPOLL = null; } if (curScene()) renderLibSection(curScene()); }, 2500);
  } else box.innerHTML = head(libTabs(s, L));
  bindLib(box, s);
}
function libTabs(s, L) {
  const n = { transition: s.transition ? 1 : 0, effect: (s.effects || []).length, sticker: (s.stickers || []).length, sound: (s.sfx || []).length };
  const tabs = [["transition", "⇄ Transition"], ["effect", "✨ Effects"], ["sticker", "🙂 Stickers"], ["sound", "🔊 Sounds"]];
  let body = "";
  if (LIBTAB === "transition") {
    const vid = P.scenes.filter(inVid), first = vid.length && vid[0].id === s.id, tr = s.transition;
    const tn = tr && (L.transitions.find((t) => t.id === tr.type) || {}).name;
    body = `<div class="muted small">How the video goes INTO this scene from the scene before. Lengths don't change; the next voice starts right at the cut.</div>
      ${first ? `<div class="warnline">⚠ This is the first scene – nothing comes before it. For an opening, use Effects → “Fade in from black”.</div>` : ""}
      ${tr ? `<div class="irow libcur"><b>⇄ ${esc(tn || tr.type)}</b>
        <select id="trDur" data-tip="How long the transition takes">${[0.25, 0.4, 0.6, 0.8, 1].map((v) => `<option value="${v}" ${Math.abs(v - tr.dur) < 0.01 ? "selected" : ""}>${v} s</option>`).join("")}${[0.25, 0.4, 0.6, 0.8, 1].some((v) => Math.abs(v - tr.dur) < 0.01) ? "" : `<option selected value="${tr.dur}">${tr.dur} s</option>`}</select>
        <label class="check" data-tip="A whoosh that peaks in the middle of the move (from the SFX Kit)"><input type="checkbox" id="trSnd" ${tr.sound ? "checked" : ""}> whoosh${tr.sound ? `: ${esc(tr.sound.name)}` : ""}</label>
        <button class="lnk" data-cmd="transition off">remove</button>
        <button class="lnk" data-gcmd="transitions ${esc(tr.type)} ${tr.dur}" data-tip="Use this transition at EVERY cut of the video">use at every cut</button></div>` : ""}
      <div class="libscroll">${[...new Set(L.transitions.map((t) => t.group))].map((g) => `<div class="libgrp">${esc(g)}</div><div class="libgrid">${L.transitions.filter((t) => t.group === g).map((t) => libTile("transition", t, tr && tr.type === t.id)).join("")}</div>`).join("")}</div>
      <div class="muted small">Hover a tile to preview it.</div>`;
  } else if (LIBTAB === "effect") {
    const on = s.effects || [];
    body = `<div class="muted small">Effects change this scene's picture (captions and stickers stay sharp and steady). Up to 4; one camera zoom at a time.</div>
      ${on.length ? `<div class="irow">${on.map((e) => `<span class="zchip">✨ ${esc((L.effects.find((x) => x.id === e) || {}).name || e)} <button class="lnk" data-cmd="effect remove ${esc(e)}" title="Remove">×</button></span>`).join(" ")}
        <button class="lnk" data-cmd="effects off">remove all</button></div>` : ""}
      <div class="libscroll">${[...new Set(L.effects.map((t) => t.group))].map((g) => `<div class="libgrp">${esc(g)}</div><div class="libgrid">${L.effects.filter((t) => t.group === g).map((t) => libTile("effect", t, on.includes(t.id))).join("")}</div>`).join("")}</div>`;
  } else if (LIBTAB === "sticker") {
    const on = s.stickers || [];
    const POS = ["top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom", "bottom-right"];
    const q = LIBQ.sticker.trim().toLowerCase();
    let items = L.stickers;
    if (q) items = items.filter((it) => it.id.includes(q.replace(/\s+/g, "_")) || it.name.toLowerCase().includes(q) || (it.keywords || []).some((k) => k.toLowerCase().includes(q)));
    else if (LIBQ.sgroup === "picks") { const m = new Map(L.stickers.map((it) => [it.id, it])); items = (L.finance_picks || []).map((id) => m.get(id)).filter(Boolean); }
    else items = items.filter((it) => it.group === LIBQ.sgroup);
    const total = items.length; items = items.slice(0, 160);
    body = `${on.length ? `<div class="librows">${on.map((st, i) => `<div class="librow"><img src="${media("library", st.file)}" alt=""><span class="nm" title="${esc(st.name)}">${esc(st.name)}</span>
        <span class="muted small">at</span><input type="number" step="0.1" min="0" class="stkSet" data-i="${i + 1}" data-k="at" value="${st.at}" data-tip="Seconds into this scene when it appears">
        <span class="muted small">for</span><input type="number" step="0.1" min="0.2" class="stkSet" data-i="${i + 1}" data-k="for" value="${st.dur || ""}" placeholder="end" data-tip="How long it stays (seconds)">
        <select class="stkSet" data-i="${i + 1}" data-k="pos" data-tip="Where on the screen">${POS.map((p) => `<option ${p === st.pos ? "selected" : ""}>${p}</option>`).join("")}</select>
        <select class="stkSet" data-i="${i + 1}" data-k="size" data-tip="Size (share of the screen width)">${[12, 16, 22, 30, 40, 55].map((v) => `<option value="${v}" ${Math.round((st.size || 0.22) * 100) === v ? "selected" : ""}>${v}%</option>`).join("")}${[12, 16, 22, 30, 40, 55].includes(Math.round((st.size || 0.22) * 100)) ? "" : `<option selected value="${Math.round(st.size * 100)}">${Math.round(st.size * 100)}%</option>`}</select>
        <select class="stkSet" data-i="${i + 1}" data-k="anim" data-tip="pop = slides up + fades in · bounce = keeps bobbing · still = just appears">${["pop", "bounce", "still"].map((a) => `<option ${a === (st.anim || "pop") ? "selected" : ""}>${a}</option>`).join("")}</select>
        <button class="lnk danger" data-cmd="sticker remove ${i + 1}" title="Remove">×</button></div>`).join("")}</div>` : ""}
      <div class="irow"><input id="stkQ" placeholder="Search 1,600 stickers: fire, rocket, money, chart, warning…" value="${esc(LIBQ.sticker)}">
        <select id="stkG" data-tip="Sticker group">${[["picks", "⭐ Finance picks"], ...(L.sticker_groups || []).map((g) => [g, g])].map(([v, l]) => `<option value="${esc(v)}" ${v === LIBQ.sgroup ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
      <div class="stkgrid">${items.map((it) => `<button class="stk" data-stk="${esc(it.id)}" title="${esc(it.name)} – click to add (appears 0.3 s into the scene for 2 s, top-right; change it above)"><img loading="lazy" src="${media("library", it.file)}" alt="${esc(it.name)}"></button>`).join("") || `<span class="muted small">No stickers found.</span>`}</div>
      ${total > items.length ? `<div class="muted small">Showing ${items.length} of ${total} – type to search.</div>` : ""}`;
  } else {
    const on = s.sfx || [];
    const q = LIBQ.sound.trim().toLowerCase();
    let items = L.sounds;
    if (q) items = items.filter((it) => it.id.includes(q.replace(/\s+/g, "-")) || it.name.toLowerCase().includes(q) || (it.tags || []).some((t) => t.includes(q)) || (it.use_for || []).some((u) => u.toLowerCase().includes(q)));
    else items = items.filter((it) => it.category === LIBQ.scat);
    body = `${on.length ? `<div class="librows">${on.map((e, i) => `<div class="librow"><button class="lnk" data-play="${esc(e.file)}" title="Listen">▶</button><span class="nm" title="${esc(e.name)}">🔊 ${esc(e.name)}</span>
        <span class="muted small">hits at</span><input type="number" step="0.05" min="0" class="sfxSet" data-i="${i + 1}" data-k="at" value="${e.at}" data-tip="Seconds into this scene where the sound's hit lands (it starts a little earlier so the hit is exactly there)">
        <span class="muted small">vol</span><input type="number" step="10" min="0" max="200" class="sfxSet" data-i="${i + 1}" data-k="vol" value="${Math.round((e.vol ?? 1) * 100)}"><span class="muted small">%</span>
        <button class="lnk danger" data-cmd="sfx remove ${i + 1}" title="Remove">×</button></div>`).join("")}</div>` : ""}
      ${LIBQ.scat === "meme sounds" ? `<div class="warnline">⚠ Meme sounds from myinstants.com have NO licence – many are clips from films, TV or songs. In monetised videos they can get YouTube Content ID claims or muted audio on Instagram. If a video gets a claim, swap that sound.</div>` : ""}
      <div class="irow"><input id="sndQ" placeholder="Search 320 sounds: whoosh, pop, cash, ding, riser, typing…" value="${esc(LIBQ.sound)}">
        <select id="sndC" data-tip="Sound category">${(L.sound_categories || []).map((c) => `<option ${c === LIBQ.scat ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></div>
      <div class="sndlist">${items.slice(0, 120).map((it) => `<div class="sndrow"><button class="lnk" data-play="${esc(it.file)}" title="Listen">▶</button>
        <span class="nm" title="${esc(it.desc || "")}">${it.risky ? `<b class="risky" title="${esc(it.desc)}">⚠</b> ` : ""}${esc(it.name)}</span><span class="muted small">${it.duration ? it.duration.toFixed(1) + " s" : ""}${(it.use_for || []).length ? " · " + esc(it.use_for.slice(0, 2).join(", ")) : ""}</span>
        <button class="btn small" data-snd="${esc(it.id)}" data-tip="Add to this scene – its hit lands at the start of the scene (change 'hits at' above)">+ add</button></div>`).join("") || `<span class="muted small">No sounds found.</span>`}</div>`;
  }
  return `<div class="seg iseg libtabs">${tabs.map(([k, l]) => `<button data-libtab="${k}" class="${LIBTAB === k ? "active" : ""}">${l}${n[k] ? ` <b>${n[k]}</b>` : ""}</button>`).join("")}</div>${body}`;
}
let LIBBUSY = false;
const libSend = (sid, cmd) => {  // one library action at a time: a double click must not add a sticker / sound twice
  if (LIBBUSY) return;
  LIBBUSY = true;
  sendCommand(sid, cmd).finally(() => setTimeout(() => (LIBBUSY = false), 250));
};
function bindLib(box, s) {
  const re = () => renderLibSection(curScene() || s);
  $$("[data-libtab]", box).forEach((b) => (b.onclick = () => { LIBTAB = b.dataset.libtab; store.set("cve.libtab", LIBTAB); re(); }));
  if ($("#libOpen", box)) $("#libOpen", box).onclick = () => api("/api/library/open", { method: "POST" }).catch((e) => toast(e.message, true));
  if ($("#libReload", box)) $("#libReload", box).onclick = async () => { await loadLib(true); re(); toast("Library read again."); };
  if ($("#libSetup", box)) $("#libSetup", box).onclick = async () => { await api("/api/library/setup", { method: "POST" }); await loadLib(true); re(); };
  $$(".libtile", box).forEach((b) => {
    const v = $("video", b);
    if (v) { b.onmouseenter = () => { v.currentTime = 0; v.play().catch(() => {}); }; b.onmouseleave = () => { v.pause(); v.currentTime = 0.9; }; }
    b.onclick = () => {
      if (b.dataset.lib === "transition") libSend(s.id, b.classList.contains("on") ? "transition off" : `transition ${b.dataset.id}${s.transition ? " " + s.transition.dur : ""}`);
      else libSend(s.id, b.classList.contains("on") ? `effect remove ${b.dataset.id}` : `effect ${b.dataset.id}`);
    };
  });
  if ($("#trDur", box)) $("#trDur", box).onchange = (e) => sendCommand(s.id, `transition ${s.transition.type} ${e.target.value}`);
  if ($("#trSnd", box)) $("#trSnd", box).onchange = (e) => sendCommand(s.id, `transition sound ${e.target.checked ? "on" : "off"}`);
  $$(".stkSet", box).forEach((i) => (i.onchange = () => {
    const k = i.dataset.k, v = i.value;
    if (k === "for" && !v) return;
    sendCommand(s.id, `sticker ${i.dataset.i} ${k === "at" ? `at ${v}` : k === "for" ? `for ${v}` : k === "size" ? `size ${v}%` : v}`);
  }));
  $$(".sfxSet", box).forEach((i) => (i.onchange = () => sendCommand(s.id, `sfx ${i.dataset.i} ${i.dataset.k === "at" ? `at ${i.value}` : `vol ${i.value}%`}`)));
  $$("[data-stk]", box).forEach((b) => (b.onclick = () => libSend(s.id, `sticker ${b.dataset.stk}`)));
  $$("[data-snd]", box).forEach((b) => (b.onclick = () => libSend(s.id, `sfx ${b.dataset.snd} at 0`)));
  $$("[data-play]", box).forEach((b) => (b.onclick = () => libPlay(b.dataset.play, b)));
  const keepFocus = (id, key, fn) => { const el = $(id, box); if (!el) return; el.oninput = () => { LIBQ[key] = el.value; clearTimeout(el._t); el._t = setTimeout(() => { fn(); const n = $(id); if (n) { n.focus(); n.setSelectionRange(n.value.length, n.value.length); } }, 250); }; };
  keepFocus("#stkQ", "sticker", re);
  keepFocus("#sndQ", "sound", re);
  if ($("#stkG", box)) $("#stkG", box).onchange = (e) => { LIBQ.sgroup = e.target.value; LIBQ.sticker = ""; re(); };
  if ($("#sndC", box)) $("#sndC", box).onchange = (e) => { LIBQ.scat = e.target.value; LIBQ.sound = ""; re(); };
  // data-cmd / data-gcmd buttons inside the section (renderDetail's binder ran before this section existed)
  $$("[data-cmd]", box).forEach((b) => (b.onclick = () => sendCommand(s.id, b.dataset.cmd)));
  $$("[data-gcmd]", box).forEach((b) => (b.onclick = () => sendCommand(null, b.dataset.gcmd)));
}
function segBtns(name, opts, cur) {
  return `<div class="seg iseg" data-name="${name}">${opts.map(([v, label, dis]) =>
    `<button data-v="${esc(v)}" class="${String(v) === String(cur) ? "active" : ""}" ${dis ? `disabled title="${esc(dis)}"` : `data-tip="${esc((ITIP[name] || {})[v] || "")}"`}>${label}</button>`).join("")}</div>`;
}
const lab = (text, tip) => `<span class="ilab hastip" data-tip="${esc(tip)}">${text}</span>`;
function initTips() {  // one floating tip box for every [data-tip] on the page – fast, never clipped by scrolling panels
  const box = document.createElement("div"); box.id = "tipbox"; box.hidden = true; document.body.appendChild(box);
  let cur = null, timer = null;
  const hide = () => { clearTimeout(timer); box.hidden = true; cur = null; };
  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (t === cur) return;
    hide(); if (!t || !t.dataset.tip) return;
    cur = t;
    timer = setTimeout(() => {
      if (!document.body.contains(t)) return;
      box.textContent = t.dataset.tip; box.hidden = false;
      const r = t.getBoundingClientRect(), bw = box.offsetWidth, bh = box.offsetHeight;
      const x = Math.min(window.innerWidth - bw - 8, Math.max(8, r.left + r.width / 2 - bw / 2));
      let y = r.bottom + 8;
      if (y + bh > window.innerHeight - 8) y = r.top - bh - 8;
      box.style.left = `${x}px`; box.style.top = `${y}px`;
    }, 300);
  });
  document.addEventListener("pointerdown", hide, true);
  window.addEventListener("scroll", hide, true);
}
initTips();
function brollUsers(file) { return P.scenes.filter((x) => x.broll && x.broll.src === file && x.keep).map((x) => x.no); }
const rangeTxt = (nos) => !nos.length ? "" : nos.length === 1 ? `scene ${nos[0]}` : `scenes ${nos[0]}–${nos[nos.length - 1]}`;
function renderDetail() {
  const s = curScene(), el = $("#sceneDetail");
  if (!s) { el.innerHTML = `<p class="muted">Select a scene on the timeline.</p>`; $("#sceneHistory").innerHTML = ""; return; }
  const split = P.settings.layout === "split";
  const tStart = r2(s.in - (s.orig_in ?? s.in)), tEnd = r2((s.orig_out ?? s.out) - s.out);
  const head = `
    <div class="title"><h2>Scene ${s.no}</h2> ${sceneBadges(s)}
      <span class="spacer"></span>
      <button class="btn small ghost" id="dupScene" data-tip="Copy this scene (with all its edits) and put the copy right after it – then drag it anywhere. Keys: Ctrl+D, or Ctrl+C, select another scene, Ctrl+V (the copy goes after that scene)">⧉ Duplicate</button>
      ${isBroll(s) ? "" : `<button class="btn small ${s.keep ? "ghost" : "primary"}" data-cmd="${s.keep ? "cut" : "keep"}" data-tip="${s.keep ? "Remove this scene from the video (it stays on the timeline, striped – Restore brings it back). Key: Delete" : "Put this scene back into the video. Key: Delete"}">${s.keep ? "✂ Cut scene" : "↺ Restore scene"}</button>`}</div>
    <div class="muted small">${esc(s.src)} · ${fmt(s.in)}–${fmt(s.out)} · ${s.dur}s${s.keep && !isBroll(s) ? ` → <b>${s.est}s</b> in the video` : ""}</div>
    ${!s.keep && s.broll_view && !isBroll(s) ? `<div class="warnline">⚠ This scene is cut, so its B-roll is not in the video either. <button class="lnk" data-cmd="keep">Restore scene</button></div>` : ""}`;
  // "what this scene IS" – deliberately at the bottom and worded so it can't be mistaken for "add B-roll here"
  const roleRow = `<div class="isec"><div class="ilab hastip" data-tip="Is this scene part of the video, or only footage used OVER other scenes?">This scene is…</div>
    <div class="irow">${segBtns("role", [["speaker", "🎤 Part of the video"], ["broll", "🎞 B-roll footage (not played itself)"]], isBroll(s) ? "broll" : "speaker")}</div>
    <div class="muted small">${isBroll(s) ? "Its picture is used to cover other scenes; its own sound and words are not in the video."
      : "To show B-roll OVER this scene, use “Cover with B-roll” above – not this."}</div></div>`;
  if (isBroll(s)) {
    const users = brollUsers(s.src);
    el.innerHTML = head + `<div class="ibox">
      <b>B-roll footage</b> – it never plays on its own and adds no length. It covers speaker scenes while your voice keeps playing.<br>
      ${users.length ? `Now covering <b>${rangeTxt(users)}</b>. Change that on a speaker scene (B-roll ▾).`
        : `Not used yet. <button class="btn small" data-gcmd="broll auto">Place it automatically</button> or pick a speaker scene → “Cover with B-roll”.`}
      ${(s.transcript || "").trim() && !s.no_speech ? `<div class="warnline">⚠ This scene has speech (“${esc((s.caption_view ?? s.transcript).split(/\s+/).slice(0, 6).join(" "))}…”) – as B-roll those words are NOT in the video.</div>` : ""}</div>` + roleRow;
  } else {
    const bv = s.broll_view;
    const clipOpts = [...(P.broll_clips || []).map((c) => c.file), ...P.sources.map((x) => x.file).filter((f) => f !== s.src && !(P.broll_clips || []).some((c) => c.file === f))];
    const assets = (P.broll_assets || []).filter((a) => !a.audio);  // sounds belong on Audio tracks
    const allUploads = P.broll_assets || [];
    const c = s.card;
    const cardKind = !c ? "auto" : c.kind;
    const words = (s.caption_view ?? s.transcript ?? "").split(/\s+/).filter(Boolean);
    const capsOn = words.length > 0 && (s.captions === true || (s.captions == null && P.settings.captions !== false));
    el.innerHTML = head + `
      <div class="isec bsec"><div class="ilab hastip" data-tip="Show another clip (market footage, charts…) OVER this scene while your voice keeps playing.">🎞 Cover with B-roll <span class="muted small">(footage shows over this scene, your voice keeps playing)</span></div>
        <div class="irow">
          <select id="brollSel" data-tip="Pick what covers this scene: a video/picture you uploaded for B-roll, or one of your clips. None = show yourself."><option value="">None</option>
            ${assets.length ? `<optgroup label="Uploaded B-roll">${assets.map((a) => `<option value="${esc(a.file)}" ${bv && bv.src === a.file ? "selected" : ""}>${a.still ? "🖼" : "🎞"} ${esc(a.file)}${a.used_by.length ? ` · scene ${rangeTxt(a.used_by)}` : ""}</option>`).join("")}</optgroup>` : ""}
            ${clipOpts.length ? `<optgroup label="Your clips">${clipOpts.map((f) => `<option value="${esc(f)}" ${bv && bv.src === f ? "selected" : ""}>${esc(f)}</option>`).join("")}</optgroup>` : ""}
            ${bv && ![...clipOpts, ...assets.map((a) => a.file)].includes(bv.src) ? `<option value="${esc(bv.src)}" selected>${esc(bv.src)} (missing)</option>` : ""}</select>
          <button class="btn small" id="brUpBtn" data-tip="Upload a video or a picture (chart, screenshot, logo…) from your computer to show over THIS scene. It is kept for this project and can be picked on other scenes too.">⬆ Upload video / picture</button>
          <input type="file" id="brUpFile" accept="video/*,image/*,.mkv,.mts" hidden>
          <span id="brUpProg" class="muted small">${BRUP && BRUP.sid === s.id ? `uploading ${BRUP.pct} %…` : ""}</span>
        </div>
        ${allUploads.length ? `<details class="brmanage" ${BRMANAGE ? "open" : ""}><summary class="muted small" data-tip="See and delete the videos, pictures and sounds you uploaded">Manage uploads (${allUploads.length})</summary>
          ${allUploads.map((a) => `<div class="brrow">${a.thumb ? `<img src="${media("thumbs", a.thumb)}" alt="">` : `<span class="brnothumb">${a.audio ? "🎵" : a.still ? "🖼" : "🎞"}</span>`}
            <span class="brname" title="${esc(a.file)}">${esc(a.file)}</span>
            <span class="muted small">${a.used_by.length ? `scene ${rangeTxt(a.used_by)}` : "not used"}</span>
            <button class="lnk danger" data-delasset="${esc(a.file)}" data-tip="Remove this upload from the project${a.used_by.length ? " – the scenes using it go back to showing you" : ""}. Undo brings it back.">🗑 delete</button></div>`).join("")}</details>` : ""}
        ${bv ? `<div class="irow">${lab("Placement", "Where the B-roll shows. Your voice keeps playing in every case.")}
          ${segBtns("bwhere", split ? [["full", "Full screen"], ["top", "Top panel"], ["bottom", "Over you (bottom)"]] : [["full", "Full screen"], ["top", "Top half"], ["bottom", "Bottom half"]], bv.where)}
          ${brMini(bv.where, split)}
          ${bv.chosen ? `<button class="lnk" data-cmd="broll layout" data-tip="Forget this choice – follow the layout (split: top panel, full screen layout: full screen)">auto</button>` : `<span class="muted small">(auto)</span>`}</div>
        <div class="irow">${lab("Picture", "Fill = the B-roll fills its space (edges cut off if its shape differs, e.g. a wide video in a tall space). Whole picture = nothing is cut off; the empty space is a soft blurred copy of it (no black bars).")}
          ${segBtns("bfit", [["fill", "Fill (crop)"], ["fit", "Whole picture"]], bv.fit || "fill")}</div>
        <div class="irow">${lab("Captions on it", "Show your captions on top of the B-roll in this scene, or hide them (same switch as Captions below).")}
          ${words.length ? segBtns("bcaps", [["on", "On"], ["off", "Off"]], capsOn ? "on" : "off") : `<span class="muted small">no speech in this scene – nothing to caption</span>`}</div>
        ${bv.still ? `<div class="muted small">🖼 Picture – shown for the whole scene.</div>` : `<div class="irow"><span class="muted small">Starts in the clip at</span>
          <input type="number" id="brollAt" step="0.5" min="0" placeholder="auto" value="${bv.at ?? ""}" title="Empty = continue from the previous scene's B-roll">
          <span class="muted small">s ${bv.at == null ? "(auto: continues)" : `<button class="lnk" data-cmd="broll continue">auto</button>`}</span></div>`}` : ""}
        ${bv && !bv.ok ? `<div class="warnline">⚠ Not shown: ${esc(bv.why)}.</div>` : ""}
        ${!clipOpts.length && !assets.length ? `<div class="muted small">Nothing to show yet – click “Upload video / picture”, or add another clip in Footage.</div>` : ""}</div>
      <div class="isec libsec" id="libSec"></div>
      <div class="isec"><div class="ilab hastip" data-tip="What you say in this scene. Click a word to change how the captions spell it, or to hide it. ✏ Edit text = type this scene's caption text yourself (add missing words, fix wrong ones).">Words <span class="muted small">(${s.caption_typed ? "your text – click a word or ✏ to change it" : "click a word to fix how it's written"})</span>
        ${inVid(s) ? `<button class="lnk" id="capEditBtn" data-tip="Type what the captions should say in this scene – add missing words, fix wrong ones. Save = it is timed to your voice and the edited view updates.">✏ Edit text</button>` : ""}
        ${s.caption_typed ? `<span class="badge okb" data-tip="These captions are YOUR text, each word timed to your voice.">your text</span> <button class="lnk" id="capAutoBtn" data-tip="Forget your text for this scene – back to the automatic captions from the audio.">↺ automatic</button>` : ""}</div>
        <div id="capEdit" class="capedit" ${CAPDRAFT && CAPDRAFT.sid === s.id ? "" : "hidden"}>
          <textarea id="capEditText" rows="3" placeholder="Type exactly what you say in this scene – e.g. Gold jo hai, woh ek safe haven commodity hai…">${esc(CAPDRAFT && CAPDRAFT.sid === s.id ? CAPDRAFT.text : words.join(" "))}</textarea>
          <div class="capeditrow"><button class="btn small primary" id="capEditSave">Save</button> <button class="btn ghost small" id="capEditCancel">Cancel</button>
            <span class="muted small">Ctrl+Enter saves · the words are placed on your voice in this scene, then the edited view updates by itself · Undo takes it back</span></div></div>
        ${s.heard ? `<div class="ibox">🎵 <b>No speech detected</b> – music or background sound, so captions stay off here.
            <button class="lnk" id="showHeard" data-tip="Speech recognition always 'hears' something in music. These are its guesses, not real words.">Show the ${s.heard.split(/\s+/).length} guessed word(s)</button>
            <div id="heardBox" class="muted small" hidden>“${esc(s.heard)}” – not real speech. To show captions anyway: Captions → On (then fix or hide the words here).</div></div>` : ""}
        ${s.no_speech && s.captions === true && words.length && !s.caption_typed ? `<div class="warnline">⚠ No real speech was detected here – these words are guesses and WILL appear as captions. Fix or hide them, or set Captions to Auto.</div>` : ""}
        <div class="words">${words.length ? words.map((w) => `<button class="wd" data-w="${esc(w)}" data-tip="Click to fix how “${esc(w)}” is written in the captions, or hide it.">${esc(w)}</button>`).join("") : s.heard ? "" : `<span class="muted">(no speech in this scene)</span>`}</div>
        <div id="wordFix" class="wordfix" hidden></div></div>
      <div class="isec"><div class="ilab hastip" data-tip="Remove seconds from the start or end of this scene (e.g. an “umm” before you start). You can also drag the block's edge on the timeline.">Trim</div>
        <div class="trim">
          <label>Start <span class="nud"><button data-nud="start" data-d="-0.1" data-tip="Remove 0.1 s less from the start (scene starts earlier).">−</button><input type="number" id="trimStart" step="0.05" min="0" value="${tStart}"><button data-nud="start" data-d="0.1" data-tip="Remove 0.1 s more from the start.">+</button></span> s</label>
          <label>End <span class="nud"><button data-nud="end" data-d="-0.1" data-tip="Remove 0.1 s less from the end (scene ends later).">−</button><input type="number" id="trimEnd" step="0.05" min="0" value="${tEnd}"><button data-nud="end" data-d="0.1" data-tip="Remove 0.1 s more from the end.">+</button></span> s</label>
          <button class="btn ghost small" data-cmd="reset trim" ${tStart || tEnd ? `data-tip="Undo all trimming – the scene gets its full original length back."` : `disabled title="Nothing trimmed yet"`}>Reset</button>
        </div>
        <div class="muted small">Seconds removed from the start / end. At the playhead: <button class="lnk" data-key="i" data-tip="Play the original footage, pause where the scene should START, then click (or press I).">I = start here</button> · <button class="lnk" data-key="o" data-tip="Pause where the scene should END, then click (or press O).">O = end here</button> (Original footage view)</div></div>
      <div class="irow">${lab("Speed", "How fast this scene plays. Faster = shorter video and a punchier pace.")}${segBtns("speed", [["0.75", "0.75×"], ["1", "1×"], ["1.25", "1.25×"], ["1.5", "1.5×"], ["2", "2×"]], s.speed || 1)}</div>
      <div class="irow">${lab("Zoom", "Zoom for the WHOLE scene. For a zoom on just a few words use a zoom moment below.")}${segBtns("zoom", [["1", "Off"], ["1.1", "1.1×"], ["1.2", "1.2×"], ["1.3", "1.3×"], ["1.5", "1.5×"]], s.zoom || 1)}</div>
      <div class="irow">${lab("Zoom moments", "A quick punch-in while you say something important – inside this scene, it stays ONE scene. Add one: play the original, then type e.g. punch 1.2-2.5 below.")}
        ${(s.punches || []).length ? [...s.punches].sort((a, b) => a[0] - b[0]).map((p, i) => `<span class="zchip" data-tip="Zoom ${p[2]}× from ${r2(p[0] - s.in)} s to ${r2(p[1] - s.in)} s of this scene">🔍 ${r2(p[0] - s.in)}–${r2(p[1] - s.in)} s · ${p[2]}× <button class="lnk" data-cmd="punch remove ${i + 1}" data-tip="Remove this zoom moment">✕</button></span>`).join(" ")
          : `<span class="muted small">none – type <code>punch 1.2-2.5</code> (seconds in this scene) to add one</span>`}</div>
      <div class="irow">${lab("Audio", "Sound of this scene: mute it, or make it louder / quieter.")}
        <button class="btn small ${s.mute ? "primary" : "ghost"}" data-cmd="${s.mute ? "unmute" : "mute"}" data-tip="${s.mute ? "Bring this scene's sound back." : "Silence this scene completely (background music still plays)."}">${s.mute ? "🔇 Muted" : "🔊 Mute"}</button>
        <input type="range" id="volRange" data-tip="Volume of this scene: 100 % = as recorded, 200 % = twice as loud. Applied when you let go." min="0" max="200" step="5" value="${Math.round((s.volume ?? 1) * 100)}" ${s.mute ? "disabled" : ""}>
        <span class="muted small" id="volVal">${Math.round((s.volume ?? 1) * 100)}%</span></div>
      <div class="irow">${lab("Captions", "Show or hide the spoken words on screen in this scene.")}${segBtns("captions", [["auto", "Auto"], ["on", "On"], ["off", "Off"]],
        s.captions === true ? "on" : s.captions === false && !s.captions_auto_off ? "off" : "auto")}
        ${s.captions_auto_off ? `<span class="muted small">no speech → off</span>` : ""}</div>
      <div class="irow ${capsOn ? "" : "dim"}">${lab("Position", "Where the captions sit in THIS scene: a preset, or an exact height in % from the top of the screen.")}${segBtns("cappos", [["auto", `Auto`], ["head", "Head"], ["center", "Center"], ["below", "Below"]], typeof s.cap_pos === "number" ? "" : s.cap_pos || "auto")}
        <span class="cappct" data-tip="Exact caption height for the WHOLE video: % from the TOP of the screen (0 = top edge, 100 = bottom edge). Type a number, press Enter – every scene follows it (also scenes you add later). Safe for Reels/Shorts: about 15–78.">
          <input type="number" id="capPct" min="5" max="95" step="1" value="${typeof s.cap_pos === "number" ? s.cap_pos : typeof P.settings.caption_y === "number" ? P.settings.caption_y : ""}" placeholder="${Math.round(capPctOf(s, s.cap_pos || "auto"))}"> %
          <button class="btn small" id="capPctSet" data-tip="Set this height for ALL scenes">Set</button>
          <button class="lnk" id="capPctOne" data-tip="Exception: use this height in THIS scene only (the other scenes keep the video's height)">only this scene</button></span>
        ${capMini(s)}
        <span class="muted small">${capsOn ? `now at ≈ ${Math.round(capPctOf(s, s.cap_pos || "auto"))} %${s.cap_pos != null ? ` · <b>own position</b> (other scenes: ${Math.round(capPctOf(s, "auto"))} %) – <button class="lnk" data-cmd="captions at auto" data-tip="Drop this scene's own position – follow the whole video again">follow the video</button>` : typeof P.settings.caption_y === "number" ? " · whole video" : ""}` : `captions are OFF here – ${!words.length ? "no speech in this scene" : "turn them On to see this"}`}</span></div>
      <div class="irow">${lab("Pauses", "Silent gaps while you think or breathe. Cutting them = jump cuts and a faster video.")}${segBtns("pauses", [["auto", "Auto"], ["cut", "Cut"], ["keep", "Keep"]],
        s.remove_silence === true ? "cut" : s.remove_silence === false ? "keep" : "auto")}
        <span class="muted small">Auto = ${P.settings.remove_silence ? "cut" : "keep"} (whole video)</span></div>
      ${split ? `<div class="isec"><div class="ilab hastip" data-tip="The graphic in the top panel (split layout): a title, a big number or a small chart about what you say.">Top card</div>
        <div class="irow"><select id="cardKind" data-tip="Previous card = keep showing the last card · Title = text (*word* = orange) · Number = big figure + label · Chart = mini line chart · Empty = no card.">
          ${[["auto", "Previous card (auto)"], ["title", "Title"], ["number", "Number"], ["chart", "Chart"], ["none", "Empty panel"]].map(([v, l]) => `<option value="${v}" ${v === cardKind ? "selected" : ""}>${l}</option>`).join("")}
        </select></div>
        <div id="cardFields" class="cardf"></div></div>` : ""}
      <div class="isec"><div class="ilab hastip" data-tip="A short line of text shown during this scene (separate from captions), e.g. a headline or a number.">Text on screen</div>
        <div class="irow"><input id="ovText" data-tip="Type the text to show, then click Set." placeholder="e.g. Nifty at record high" value="${esc(s.text || "")}">
          <select id="ovPos" data-tip="Where the text appears: top, center or bottom of the screen.">${["top", "center", "bottom"].map((p) => `<option ${p === (s.text_pos || "top") ? "selected" : ""}>${p}</option>`).join("")}</select>
          <button class="btn small" id="ovApply" data-tip="Show this text in this scene.">Set</button>${s.text ? `<button class="btn ghost small" data-cmd="remove text" data-tip="Remove the text from this scene.">Remove</button>` : ""}</div>
          ${split && s.text && (s.text_pos || "top") === "top" ? `<div class="warnline">⚠ In the split layout the top is the card – this text covers it. Choose center or bottom, or put it in the card.</div>` : ""}</div>` + roleRow;
    renderCardFields(c);
  }
  bindInspector(s);
  const hist = P.commands.filter((c) => c.scene === s.id).slice(-12).reverse();
  $("#sceneHistory").innerHTML = hist.map(cmdHtml).join("");
  bindCancel($("#sceneHistory"));
}
function renderCardFields(c) {
  const box = $("#cardFields"); if (!box) return;
  const k = $("#cardKind").value;
  const v = (f) => esc(c && c.kind === k ? (f === "points" ? (c.points || []).join(", ") : c[f] || "") : "");
  box.innerHTML = k === "title" ? `<input id="cfText" placeholder="bajaj *IPO* listing aaj  (*word* = orange)" value="${v("text")}">`
    : k === "number" ? `<input id="cfValue" placeholder="2x+" value="${v("value")}"><input id="cfLabel" placeholder="listing gain" value="${v("label")}">`
    : k === "chart" ? `<input id="cfPoints" placeholder="10, 12, 11, 15" value="${v("points")}"><input id="cfLabel" placeholder="Nifty 5 days" value="${v("label")}">`
    : `<span class="muted small">${k === "auto" ? "Keeps showing the card from the scene before (or the headline)." : "Top panel stays empty in this scene."}</span>`;
  box.innerHTML += `<button class="btn small" id="cardApply">Apply card</button>`;
  $("#cardApply").onclick = () => {
    const s = curScene(); if (!s) return;
    const val = (id) => ($(id)?.value || "").trim();
    const cmd = k === "auto" ? "card auto" : k === "none" ? "card off" : k === "title" ? `card: title ${val("#cfText")}`
      : k === "number" ? `card: number ${val("#cfValue")} | ${val("#cfLabel")}` : `card: chart ${val("#cfPoints")} | ${val("#cfLabel")}`;
    sendCommand(s.id, cmd);
  };
}
function bindInspector(s) {
  const el = $("#sceneDetail");
  $$("[data-cmd]", el).forEach((b) => (b.onclick = () => sendCommand(s.id, b.dataset.cmd)));
  $$("[data-gcmd]", el).forEach((b) => (b.onclick = () => sendCommand(null, b.dataset.gcmd)));
  $$("[data-key]", el).forEach((b) => (b.onclick = () => trimAtPlayhead(b.dataset.key)));
  // the % box sets the WHOLE video (what you expect from one number); "only this scene" makes an exception
  const cpSend = (all) => {
    const v = +($("#capPct", el)?.value || 0);
    if (!v || v < 5 || v > 95) return toast("Type a height between 5 and 95 (% from the top).", true);
    if (all) { SHOW = null; sendCommand(null, `captions at ${v}%`).then(() => { SHOW = { sid: s.id, switchView: true, at: Date.now() }; }); }
    else sendCommand(s.id, `captions at ${v}%`);
  };
  if ($("#capPctSet", el)) {
    $("#capPctSet", el).onclick = () => cpSend(true);
    $("#capPctOne", el).onclick = () => cpSend(false);
    $("#capPct", el).onkeydown = (e) => { if (e.key === "Enter") cpSend(true); };
  }
  if ($("#dupScene", el)) $("#dupScene", el).onclick = () => sceneClip("d");
  const sh = $("#showHeard", el); if (sh) sh.onclick = () => { const hb = $("#heardBox", el); hb.hidden = !hb.hidden; sh.textContent = hb.hidden ? sh.textContent.replace(/^Hide/, "Show") : sh.textContent.replace(/^Show/, "Hide"); };
  $$(".iseg", el).forEach((g) => $$("button", g).forEach((b) => (b.onclick = () => {
    if (b.classList.contains("active")) return;
    const v = b.dataset.v, n = g.dataset.name;
    if (n === "role" && v === "broll" && (s.transcript || "").trim() && !s.no_speech &&
        !confirm(`Scene ${s.no} has speech:\n“${(s.caption_view ?? s.transcript).split(/\s+/).slice(0, 10).join(" ")}…”\n\n` +
                 `As B-roll footage it LEAVES the video – these words won't be heard.\n\n` +
                 `Wanted B-roll footage to show OVER this scene instead? Cancel and use “Cover with B-roll”.`)) return;
    const cmd = { role: `role ${v === "broll" ? "broll" : "speaker"}`, speed: `speed ${v}`, zoom: v === "1" ? "no zoom" : `zoom ${v}`,
      captions: `captions ${v}`, cappos: `captions at ${v}`, pauses: v === "auto" ? "silences auto" : v === "cut" ? "remove silences" : "keep silences",
      bwhere: `broll ${v}`, bcaps: `captions ${v}`, bfit: `broll ${v}` }[n];
    if (cmd) sendCommand(s.id, cmd);
  })));
  const trimSet = (edge, x) => { x = Math.max(0, r2(x)); sendCommand(s.id, `trim ${edge} to ${x}`); };
  $$("[data-nud]", el).forEach((b) => (b.onclick = () => { const inp = b.dataset.nud === "start" ? $("#trimStart") : $("#trimEnd"); trimSet(b.dataset.nud, +inp.value + +b.dataset.d); }));
  [["#trimStart", "start"], ["#trimEnd", "end"]].forEach(([id, edge]) => { const i = $(id); if (i) i.onchange = () => trimSet(edge, +i.value); });
  const vol = $("#volRange");
  if (vol) { vol.oninput = () => ($("#volVal").textContent = `${vol.value}%`); vol.onchange = () => sendCommand(s.id, `volume ${vol.value}%`); }
  if ($("#libSec", el)) renderLibSection(s);
  const bm = $(".brmanage", el);
  if (bm) bm.ontoggle = () => (BRMANAGE = bm.open);
  $$("[data-delasset]", el).forEach((b) => (b.onclick = () => deleteBrollAsset(b.dataset.delasset)));
  const bu = $("#brUpBtn");
  if (bu) { bu.onclick = () => $("#brUpFile").click(); $("#brUpFile").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) uploadBroll(f, s.id); }; }
  const bs = $("#brollSel");
  if (bs) bs.onchange = () => sendCommand(s.id, bs.value ? `broll ${bs.value}` : "broll off");
  const ba = $("#brollAt");
  if (ba) ba.onchange = () => sendCommand(s.id, ba.value === "" ? "broll continue" : `broll from ${Math.max(0, +ba.value)}`);
  const ck = $("#cardKind");
  if (ck) ck.onchange = () => renderCardFields(s.card);
  const ov = $("#ovApply");
  if (ov) ov.onclick = () => {
    const t = $("#ovText").value.trim(); if (!t) return toast("Type the text first.", true);
    sendCommand(s.id, `text: ${t}`).then(() => { if ($("#ovPos").value !== (s.text_pos || "top")) sendCommand(s.id, `text ${$("#ovPos").value}`); });
  };
  // ✏ the scene's caption text, typed by you (draft survives the page's 2-second refresh)
  const ce = $("#capEdit"), ceT = $("#capEditText");
  const openCE = () => { CAPDRAFT = { sid: s.id, text: ceT.value }; ce.hidden = false; ceT.focus(); };
  const ceb = $("#capEditBtn"); if (ceb) ceb.onclick = () => (ce.hidden ? openCE() : ($("#capEditCancel").click()));
  if (ceT) {
    if (CAPDRAFT && CAPDRAFT.sid === s.id && CAPDRAFT.focus) {  // the panel was re-drawn while you typed: carry on
      ceT.focus(); const p = Math.min(CAPDRAFT.caret ?? ceT.value.length, ceT.value.length); ceT.setSelectionRange(p, p);
    }
    ceT.onfocus = () => { if (CAPDRAFT && CAPDRAFT.sid === s.id) CAPDRAFT.focus = true; };
    ceT.onblur = () => { if (CAPDRAFT && CAPDRAFT.sid === s.id) CAPDRAFT.focus = false; };
    ceT.oninput = () => { CAPDRAFT = { sid: s.id, text: ceT.value, focus: true, caret: ceT.selectionStart }; };
    ceT.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("#capEditSave").click(); } if (e.key === "Escape") $("#capEditCancel").click(); };
    $("#capEditCancel").onclick = () => { CAPDRAFT = null; ce.hidden = true; ceT.value = words.join(" "); };
    $("#capEditSave").onclick = async () => {
      const text = ceT.value.trim(); if (!text) return toast("Type the words first – or switch Captions off for this scene.", true);
      const btn = $("#capEditSave"); btn.disabled = true; btn.textContent = "Saving…";
      try { const r = await api(`/api/projects/${PID}/scene_caption`, { method: "POST", body: { scene: s.id, text } });
        CAPDRAFT = null; toast(r.reply); SHOW = { sid: s.id, switchView: true, at: Date.now() }; refresh(true); }
      catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = "Save"; }
    };
  }
  const cab = $("#capAutoBtn"); if (cab) cab.onclick = async () => {
    try { const r = await api(`/api/projects/${PID}/scene_caption`, { method: "POST", body: { scene: s.id, auto: true } }); CAPDRAFT = null; toast(r.reply); refresh(true); }
    catch (e) { toast(e.message, true); } };
  $$(".wd", el).forEach((b) => (b.onclick = () => {
    if (s.caption_typed && ce) { openCE(); return; }  // your own text: change it in the text box (spellings don't apply to it)
    const w = b.dataset.w.replace(/[.,!?।]+$/, ""), box = $("#wordFix");
    box.hidden = false;
    box.innerHTML = `Show “<b>${esc(w)}</b>” as <input id="wfVal" value="${esc(w)}"> <button class="btn small primary" id="wfSave">Save</button>
      <button class="btn ghost small" id="wfHide" title="Remove this word from the captions">Hide word</button> <span class="muted small">(everywhere in the video)</span>`;
    const inp = $("#wfVal"); inp.focus(); inp.select();
    const save = (val) => sendCommand(null, `spell ${w} as ${val}`);
    $("#wfSave").onclick = () => { if (inp.value.trim() && inp.value.trim() !== w) save(inp.value.trim()); else box.hidden = true; };
    inp.onkeydown = (e) => { if (e.key === "Enter") $("#wfSave").click(); if (e.key === "Escape") box.hidden = true; };
    $("#wfHide").onclick = () => save("-");
  }));
}
function trimAtPlayhead(k) {
  const s = curScene(); if (!s || isBroll(s)) return;
  if (playerMode !== "source") return toast("Switch to “Original footage” to trim at the playhead.", true);
  const t = player.currentTime;
  if (k === "i") { if (t >= s.out - 0.3 || t < (s.orig_in ?? s.in)) return toast("Playhead is outside this scene.", true); sendCommand(s.id, `trim start to ${r2(t - (s.orig_in ?? s.in))}`); }
  else { if (t <= s.in + 0.3 || t > (s.orig_out ?? s.out)) return toast("Playhead is outside this scene.", true); sendCommand(s.id, `trim end to ${r2((s.orig_out ?? s.out) - t)}`); }
}
$$(".rtabs button").forEach((b) => (b.onclick = () => {
  $$(".rtabs button").forEach((x) => x.classList.toggle("active", x === b));
  $("#inspPane").hidden = b.dataset.rt !== "insp"; $("#listPane").hidden = b.dataset.rt !== "list";
}));

/* ---- Scenes timeline: V2 = B-roll cutaways, V1 = the video's scenes (width = edited length), card row (split layout).
   Click = select · drag block = move · drag edge = trim · playhead follows the player. */
const STL = { blocks: [], drag: null };
function effCards(main) {
  if (P.settings.layout !== "split") return [];
  const head = (P.settings.headline || "").trim();
  let shown = head ? { kind: "title", text: head } : null;
  return main.map((s) => { const c = s.card; if (c && (c.kind === "none" || c.text || c.value || (c.points || []).length >= 2)) shown = c; return shown; });
}
const cardLabel = (c) => !c || c.kind === "none" ? "" : c.kind === "title" ? c.text.replace(/\*/g, "") : c.kind === "number" ? `${c.value} ${c.label || ""}` : `📈 ${c.label || "chart"}`;
function renderSceneTimeline() {
  const box = $("#stl"); if (!box || !P) return;
  const main = P.scenes.filter((s) => !s.src_skipped);  // EVERY scene is on the Video lane (B-roll ones as small 🎞 blocks)
  const bin = P.broll_clips || [];
  // ALL project clips as chips: drag one onto a scene in the Video lane to cover that scene with it.
  // B-roll clips first (blue); talking clips too (grey) – e.g. a second camera angle as a cutaway
  const usedBy = (f) => P.scenes.filter((x) => x.keep && !isBroll(x) && x.broll && x.broll.src === f).map((x) => x.no);
  const clipChip = (f, isB) => {
    const src = P.sources.find((x) => x.file === f) || {}, u = usedBy(f);
    const sc = P.scenes.find((x) => x.src === f && (isB ? isBroll(x) : true));
    return `<button class="bchip ${isB ? "" : "spk"} ${src.include === false ? "skip" : ""}" data-file="${esc(f)}" data-sel="${sc ? sc.id : ""}"
      title="Drag onto a scene below to show it OVER that scene (your voice keeps playing). Click = select.${src.include === false ? " This clip is set to Skip in Footage – B-roll from it is not shown." : ""}">
      ${isB ? "🎞" : "🎤"} ${esc(f)} · ${(src.duration || 0).toFixed(1)}s · ${u.length ? `covers ${rangeTxt(u)}` : `<i>${isB ? "not used" : "speaker clip"}</i>`}</button>`;
  };
  const others = P.sources.map((x) => x.file).filter((f) => !bin.some((c) => c.file === f));
  $("#brollBin").innerHTML = P.sources.length ? `<span class="muted small">Clips <span title="Drag a clip onto a scene to use it as B-roll there">ⓘ drag onto a scene:</span></span> ` +
    [...bin.map((c) => clipChip(c.file, true)), ...others.map((f) => clipChip(f, false)),
     ...(P.broll_assets || []).filter((a) => !a.audio).map((a) => { const u = usedBy(a.file); return `<button class="bchip" data-file="${esc(a.file)}" title="Uploaded B-roll – drag onto a scene below to show it OVER that scene (your voice keeps playing).">${a.still ? "🖼" : "⬆"} ${esc(a.file)}${a.still ? "" : ` · ${(+a.duration || 0).toFixed(1)}s`} · ${u.length ? `covers ${rangeTxt(u)}` : "<i>not used</i>"}</button>`; })].join(" ") : "";
  // B-roll scenes of clips that also have speaker scenes (e.g. a talking scene set to B-roll): off the timeline, so
  // list them here – every scene must stay findable
  const loose = [];
  if (loose.length) $("#brollBin").innerHTML += ` <span class="muted small">Not in the video:</span> ` + loose.map((x) =>
    `<button class="bchip warn" data-sel="${x.id}" title="Set to B-roll footage – select it and choose “Part of the video” to bring it back">Scene ${x.no} · B-roll footage${(x.transcript || "").trim() && !x.no_speech ? " · has speech!" : ""}</button>`).join(" ");
  $$(".bchip", $("#brollBin")).forEach((b) => {
    if (b.dataset.file) b.addEventListener("pointerdown", chipDown);
    else b.onclick = () => b.dataset.sel && selectScene(b.dataset.sel);
  });
  if (!main.length) { box.innerHTML = `<p class="muted small">No scenes yet.</p>`; STL.blocks = []; return; }
  const W = Math.max(300, box.clientWidth - 126), STUB = 30, GAP = 2;
  const kept = main.filter(inVid), total = kept.reduce((a, s) => a + s.est, 0) || 1;
  const stubs = main.length - kept.length;
  const pps = Math.max(1, (W - stubs * STUB - main.length * GAP) / total) * (STL.zoom || 1);
  let x = 0; STL.pps = pps;
  STL.blocks = main.map((s) => { const w = inVid(s) ? Math.max(6, s.est * pps) : STUB; const b = { s, x, w }; x += w + GAP; return b; });
  const cardsEff = effCards(main);
  const thumb = (f) => { const a = (P.broll_assets || []).find((x) => x.file === f); if (a) return a.thumb ? `background-image:url('${media("thumbs", a.thumb)}')` : "";
    const c = bin.find((b) => b.file === f); const t = c ? c.thumb : (P.scenes.find((x) => x.src === f) || {}).thumb; return t ? `background-image:url('${media("thumbs", t)}')` : ""; };
  // merge runs: B-roll by clip, cards by content
  const runs = (key) => { const out = []; STL.blocks.forEach((b, i) => { const k = key(b, i); if (!k) return; const last = out[out.length - 1];
    if (last && last.k === k && last.end === i - 1) { last.w = b.x + b.w - last.x; last.end = i; } else out.push({ k, x: b.x, w: b.w, start: i, end: i, b }); }); return out; };
  const bRuns = runs((b) => inVid(b.s) && b.s.broll_view ? JSON.stringify([b.s.broll_view.src, b.s.broll_view.where, b.s.broll_view.ok, b.s.broll.where || ""]) : "");
  STL.bRuns = bRuns;
  const cRuns = runs((b, i) => inVid(b.s) && cardsEff[i] && cardsEff[i].kind !== "none" && !(b.s.broll_view && b.s.broll_view.where === "full") ? JSON.stringify(cardsEff[i]) : "");
  // extra tracks: pictures/titles above the B-roll lane (top-most layer first), audio below the cards
  const allT = P.tracks || [];
  const trkVis = allT.filter((t) => t.kind !== "audio").slice().reverse(), trkAud = allT.filter((t) => t.kind === "audio");
  const step = tickStep(pps);
  let ticks = "";
  for (let t = 0; t <= total + 0.01; t += step / (step >= 1 ? 5 : 2)) {
    const major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
    ticks += `<span class="tick ${major ? "" : "minor"}" style="left:${tlX(t)}px">${major ? tc(t).replace(/\.0$/, "") : ""}</span>`;
  }
  const LK = P.ui_locks || {}, ST = P.settings;
  const laneHead = (cls, label, tip, btns) => `<div class="h ${cls} hastip" data-tip="${esc(tip)}"><span class="tn">${label}</span>${btns}</div>`;
  const lockBtn = (lane, on) => `<button class="tb ${on ? "on" : ""}" data-lane="${lane}" data-what="lock" title="${on ? "Unlock" : "Lock (no accidental changes)"}">${on ? "🔒" : "🔓"}</button>`;
  const swBtn = (lane, what, off, onIc, offIc, t1, t2) => `<button class="tb ${off ? "on" : ""}" data-lane="${lane}" data-what="${what}" title="${off ? t2 : t1}">${off ? offIc : onIc}</button>`;
  const film = (s, w) => {  // the scene's own frames (clip film strip) + its sound (waveform), like an editor's clip
    const src = P.sources.find((x) => x.file === s.src) || {}, D = +src.duration || 0, span = Math.max(0.05, s.out - s.in);
    let fr = "";
    if (src.strip && D > 0) {
      const k = Math.max(1, Math.round(w / 40));
      for (let j = 0; j < k; j++) {
        const idx = Math.min(STRIP_N - 1, Math.max(0, Math.floor(((s.in + ((j + 0.5) / k) * span) / D) * STRIP_N)));
        fr += `<i style="background-image:url('${media("thumbs", src.strip)}');background-position:${-idx * 40}px 0;background-size:${STRIP_N * 40}px 40px"></i>`;
      }
    } else fr = `<i class="one" style="background-image:url('${media("thumbs", s.thumb)}')"></i>`;
    const full = w * D / span;
    const wv = src.wave && D > 0 ? `<div class="wv ${ST.main_mute || s.mute ? "mut" : ""}" style="background-image:url('${media("thumbs", src.wave)}');background-size:${full}px 100%;background-position:${-(s.in / D) * full}px 0"></div>`
      : `<div class="wv none"></div>`;
    return `<div class="fr">${fr}</div>${wv}`;
  };
  box.innerHTML = `
    <div class="stl-heads"><div class="h ruler"></div>${trkVis.map(trkHead).join("")}${laneHead(`trk broll ${ST.broll_off ? "off" : ""}`, "🎞 B-roll", "Scene B-roll: footage shown OVER a scene while your voice plays. Drag a clip chip onto a scene to add one.", lockBtn("broll", LK.broll) + swBtn("broll", "hide", ST.broll_off, "👁", "🚫", "Hide all scene B-roll", "Show scene B-roll again"))}${laneHead(`trk main ${ST.main_mute ? "off" : ""}`, "🎬 Video 1", "Your scenes in playing order (the main line). Drag to reorder, drag an edge to trim, striped = cut. Green = its sound.", lockBtn("main", LK.main) + swBtn("main", "mute", ST.main_mute, "🔊", "🔇", "Mute the scenes' own sound (keep music / track audio)", "Turn the scenes' sound back on"))}${cardsEff.length ? laneHead(`trk ${ST.cards_off ? "off" : ""}`, "🃏 Cards", "The top-panel card (title / number / chart) shown during each scene – split layout.", lockBtn("cards", LK.cards) + swBtn("cards", "hide", ST.cards_off, "👁", "🚫", "Hide the top cards", "Show the top cards again")) : ""}${trkAud.map(trkHead).join("")}<div class="h newtrk"></div></div>
    <div class="stl-lanes" style="width:${x}px">
      <div class="lane ruler" data-tip="Time in the edited video. Click to jump there.">${ticks}</div>
      ${trkVis.map(trkLane).join("")}
      <div class="lane broll ${LK.broll ? "locked" : ""} ${ST.broll_off ? "off" : ""}">${bRuns.map((r, ri) => { const v = JSON.parse(r.k); return `<div class="bblk ${v[2] ? "" : "bad"}" data-run="${ri}" style="left:${r.x}px;width:${r.w}px;${thumb(v[0])}" title="${esc(v[0])} – ${v[1] === "full" ? "full screen" : v[1] === "bottom" ? "bottom" : "top"}${v[2] ? "" : " – NOT SHOWN (skipped or missing)"}
Drag = move to other scenes · drag an edge = cover more / fewer scenes · × = remove"><i class="hd l"></i><span>🎞 ${esc(v[0])} · ${v[1]}</span><b class="bx" title="Remove this B-roll">×</b><i class="hd r"></i></div>`; }).join("")}</div>
      <div class="lane main ${LK.main ? "locked" : ""}">${STL.blocks.map((b) => `<div class="sblk ${inVid(b.s) ? "film" : ""} ${isBroll(b.s) ? "cut brs" : b.s.keep ? "" : "cut"} ${b.s.id === selected ? "sel" : ""}" data-id="${b.s.id}" style="left:${b.x}px;width:${b.w}px" title="Scene ${b.s.no} · ${isBroll(b.s) ? "B-roll footage, not played – drag it to a place in the video to play it there" : b.s.keep ? `${b.s.est}s` : "cut – click to select, Del to restore"}">${inVid(b.s) ? film(b.s, b.w) : ""}
        ${isBroll(b.s) ? `${b.s.no}🎞` : b.s.keep ? `<i class="hd l"></i><span class="no">${b.s.no}</span>${b.w > 54 ? `<span class="du">${b.s.est}s</span>` : ""}<i class="hd r"></i>` : `${b.s.no}✂${b.s.broll_view ? "🎞" : ""}`}</div>`).join("")}</div>
      ${cardsEff.length ? `<div class="lane cards">${cRuns.map((r) => `<div class="cblk" style="left:${r.x}px;width:${r.w}px" title="${esc(cardLabel(JSON.parse(r.k)))}">${esc(cardLabel(JSON.parse(r.k)))}</div>`).join("")}</div>` : ""}
      ${trkAud.map(trkLane).join("")}
      <div class="lane newtrk" title="Drop a clip chip or drag a scene here – a new track is made for it">＋ drop a clip or a scene here for a new track</div>
      <div class="stl-head" id="stlHead"><b class="ph-knob" title="Drag to move the playhead"></b><button class="ph-cut" title="Split the scene at the playhead (S)">✂</button></div>
    </div>`;
  const nCut = main.length - kept.length, nBr = P.scenes.filter(isBroll).length, nSk = P.scenes.filter((x) => x.src_skipped && !isBroll(x)).length;
  $("#stlInfo").textContent = `${P.scenes.length} scenes: ${kept.length} in the video (≈ ${fmt(total)})` +
    (nCut ? ` · ${nCut} cut` : "") + (nBr ? ` · ${nBr} B-roll footage` : "") + (nSk ? ` · ${nSk} in skipped clips` : "") +
    (bRuns.length ? ` · ${bRuns.length} cutaway${bRuns.length > 1 ? "s" : ""}` : "");
  $$(".bblk", box).forEach((b) => b.addEventListener("pointerdown", bDown));
  $(".lane.ruler", box).onclick = (e) => seekEdit((e.clientX - $(".stl-lanes", box).getBoundingClientRect().left));
  $$(".sblk", box).forEach((el) => el.addEventListener("pointerdown", stlDown));
  $$("[data-lane]", box).forEach((b) => (b.onclick = () => trackOp({ op: "lane", lane: b.dataset.lane, what: b.dataset.what })));
  bindPlayhead(box);
  bindTracks(box);
  drawStlHead();
}
/* ---- extra TRACKS (Video / B-roll / Audio / Text): items are connected to a scene (anchor + offset), so they move
   with their scene. Drag = move · drag an edge = trim · double-click an empty spot = new item · × = remove. */
let TSEL = null;  // {track, item}
const TKIND = { video: ["🎬", "Video: an overlay clip WITH its own sound"], broll: ["🎞", "B-roll: a silent overlay"],
  audio: ["🎵", "Audio: music, voice-over or sounds"], text: ["🔤", "Text: titles, lower-thirds, banners"] };
function xToT(px) {  // timeline px -> edited time (inverse of tlX)
  const m = renderTimes(true);
  for (const b of STL.blocks) {
    if (px < b.x + b.w + 1) {
      if (!inVid(b.s)) { const nx = STL.blocks.slice(STL.blocks.indexOf(b)).find((k) => inVid(k.s)); return nx ? sAt(nx.s, m) : 0; }
      return sAt(b.s, m) + Math.max(0, Math.min(1, (px - b.x) / b.w)) * sDur(b.s, m);
    }
  }
  const last = [...STL.blocks].reverse().find((k) => inVid(k.s));
  return last ? sAt(last.s, m) + sDur(last.s, m) : 0;
}
function anchorAt(t) {  // the scene playing at edited time t -> {anchor, off}
  const m = renderTimes(true), vid = STL.blocks.map((b) => b.s).filter(inVid);
  if (!vid.length) return null;
  const s = vid.find((x) => t < sAt(x, m) + sDur(x, m) - 1e-6) || vid[vid.length - 1];
  return { anchor: s.id, off: Math.max(0, +(t - sAt(s, m)).toFixed(3)) };
}
function itemSpan(it) {  // -> {a, b, ok}: edited times; ok=false when its scene is cut / gone
  const m = renderTimes(true), s = P.scenes.find((x) => x.id === it.anchor);
  if (!s || !inVid(s)) return { a: s ? s.start_at : 0, b: (s ? s.start_at : 0) + it.dur, ok: false };
  const a = sAt(s, m) + it.off; return { a, b: a + it.dur, ok: true };
}
const srcName = (it) => (it.src && it.src.n ? it.src.n.split("/").pop() : "");
function trkHead(t) {
  const [ic, tip] = TKIND[t.kind] || ["", ""];
  return `<div class="h trk ${t.kind} ${t.off ? "off" : ""}" data-trk="${t.id}" data-tip="${esc(tip)} · double-click the name to rename">
    <span class="tn" data-ren="${t.id}">${ic} ${esc(t.name)}</span>
    <button class="tb ${t.locked ? "on" : ""}" data-tlock="${t.id}" title="${t.locked ? "Unlock" : "Lock (no accidental changes)"}">${t.locked ? "🔒" : "🔓"}</button>
    ${t.kind === "video" ? `<button class="tb ${t.mute ? "on" : ""}" data-tmute="${t.id}" title="${t.mute ? "Sound back on" : "Mute this track's sound (picture stays)"}">${t.mute ? "🔇" : "🔊"}</button>` : ""}
    <button class="tb" data-ttog="${t.id}" title="${t.kind === "audio" ? (t.off ? "Unmute" : "Mute") : (t.off ? "Show" : "Hide")} this track">${t.kind === "audio" ? (t.off ? "🔇" : "🔊") : (t.off ? "🚫" : "👁")}</button>
    <button class="tb" data-tdel="${t.id}" title="Delete this track">×</button></div>`;
}
function trkLane(t) {
  return `<div class="lane trk ${t.kind} ${t.off ? "off" : ""} ${t.locked ? "locked" : ""}" data-trk="${t.id}" title="Double-click an empty spot to add here${t.kind === "video" || t.kind === "broll" ? " (or drag a clip chip onto it)" : ""}">
    ${(t.items || []).map((it) => { const sp = itemSpan(it), x0 = tlX(sp.a), x1 = tlX(sp.b), w = Math.max(10, x1 - x0);
      const lab = t.kind === "text" ? `“${it.text}”` : `${srcName(it)}${t.kind === "audio" || t.kind === "video" ? ` · ${Math.round((it.vol ?? 1) * 100)}%` : ""}${it.where && it.where !== "full" ? ` · ${it.where}` : ""}`;
      const look = t.kind === "text" ? "" : tblkMedia(t, it, w);
      return `<div class="tblk ${t.kind} ${look ? "media" : ""} ${sp.ok ? "" : "bad"} ${TSEL && TSEL.item === it.id ? "sel" : ""}" data-trk="${t.id}" data-item="${it.id}" style="left:${x0}px;width:${w}px"
        title="${esc(lab)} · ${it.dur.toFixed(1)} s${sp.ok ? "" : " – its scene is cut, so this is not in the video"}">${look}<i class="hd l"></i><span class="tl">${esc(lab)}</span><b class="bx" title="Remove">×</b><i class="hd r"></i></div>`; }).join("")}</div>`;
}
// what a track block looks like: the frames it plays (film strip) and, for sound, its waveform – like an editor's clip
const MA = {};  // "t|name" -> {strip, wave, duration, thumb, still} | "wait"
function maOf(it) {
  const src = it.src || {}, key = `${src.t}|${src.n}`;
  if (src.t === "clip") { const s = P.sources.find((x) => x.file === src.n) || {}; return { strip: s.strip, wave: s.wave, duration: +s.duration || it.src_len || 0 }; }
  if (src.t === "asset") {
    const a = (P.broll_assets || []).find((x) => x.file === src.n) || {};
    if (a.still) return { thumb: a.thumb, still: true };
  }
  if (MA[key] && MA[key] !== "wait") return MA[key];
  if (!MA[key]) {
    MA[key] = "wait";
    api(`/api/projects/${PID}/media_assets`, { method: "POST", body: { src } })
      .then((r) => { MA[key] = r || {}; if (!STL.drag && !STL.busy) renderSceneTimeline(); })
      .catch(() => { MA[key] = {}; });
  }
  return null;
}
function tblkMedia(t, it, w) {
  const m = maOf(it);
  if (!m) return "";
  const D = +m.duration || +it.src_len || 0, inP = +it.in || 0, dur = Math.max(0.05, +it.dur || 0.05);
  const FH = t.kind === "video" ? 34 : t.kind === "broll" ? 40 : 0;
  let fr = "";
  if (FH && m.still && m.thumb) fr = `<div class="tfr still" style="height:${FH}px;background-image:url('${media("thumbs", m.thumb)}');background-size:${FH * 1.6}px ${FH}px"></div>`;
  else if (FH && m.strip && D > 0) {
    let tiles = "";
    const k = Math.max(1, Math.round(w / FH));
    for (let j = 0; j < k; j++) {
      const idx = Math.min(STRIP_N - 1, Math.max(0, Math.floor(((inP + ((j + 0.5) / k) * dur) / D) * STRIP_N)));
      tiles += `<i style="flex:0 0 ${FH}px;height:${FH}px;background-image:url('${media("thumbs", m.strip)}');background-position:${-idx * FH}px 0;background-size:${STRIP_N * FH}px ${FH}px"></i>`;
    }
    fr = `<div class="tfr">${tiles}</div>`;
  }
  let wv = "";
  const audible = t.kind === "audio" || (t.kind === "video" && (it.vol ?? 1) > 0.001);
  if ((t.kind === "audio" || t.kind === "video") && m.wave && D > 0) {
    const full = w * D / dur;
    wv = `<div class="twv ${audible && !t.off && !t.mute ? "" : "mut"}" style="background-image:url('${media("thumbs", m.wave)}');background-size:${full}px 100%;background-position:${-(inP / D) * full}px 0"></div>`;
  }
  return fr || wv ? `<div class="tmedia">${fr}${wv}</div>` : "";
}
async function trackOp(body, quiet) {
  try {
    const r = await api(`/api/projects/${PID}/tracks`, { method: "POST", body });
    if (r.reply && !quiet) toast(r.reply);
    await refresh(true); autoPreviewTick(); renderPvBadge();
    return r;
  } catch (e) { toast(e.message, true); await refresh(true); return null; }
}
function trkDefaults(t) {
  if (t.kind === "text") return { text: "Your text here", style: "lower", dur: 2.5 };
  if (t.kind === "audio") {
    const up = (P.broll_assets || []).find((a) => a.audio), mus = (P.music_files || [])[0];
    if (up) return { src: { t: "asset", n: up.file }, dur: Math.min(+up.duration || 5, 8), vol: (+up.duration || 0) > 15 ? 0.3 : 1 };
    if (mus) return { src: { t: "music", n: mus }, dur: 8, vol: 0.3, fade_in: 0.5, fade_out: 1 };
    const snd = LIBCAT && (LIBCAT.sounds || []).find((x) => x.category === "whooshes");
    if (snd) return { src: { t: "lib", n: snd.file }, dur: Math.max(0.3, snd.duration || 1), vol: 1 };
    return null;
  }
  const up = (P.broll_assets || []).find((a) => !a.audio), clip = P.sources[0];
  if (up) return { src: { t: "asset", n: up.file }, dur: 3, where: t.kind === "video" ? "pip" : "full" };
  if (clip) return { src: { t: "clip", n: clip.file }, dur: 3, where: t.kind === "video" ? "pip" : "full" };
  return null;
}
async function trkAddAt(t, time, over) {
  const an = anchorAt(time);
  if (!an) return toast("No scenes in the video yet.", true);
  if (t.kind === "audio" && !LIBCAT) await loadLib();
  const def = Object.assign(trkDefaults(t) || {}, over || {});
  if (t.kind !== "text" && !def.src) return toast(t.kind === "audio" ? "No sound yet – upload one in the editor below, or download the Library." : "No clip or upload to show yet.", true);
  const r = await trackOp({ op: "item_add", track: t.id, item: Object.assign(def, an) }, true);
  if (r && r.item) { TSEL = { track: t.id, item: r.item }; renderSceneTimeline(); renderTrkEdit(); }
}
function bindTracks(box) {
  $$("[data-ttog]", box).forEach((b) => (b.onclick = () => trackOp({ op: "toggle", track: b.dataset.ttog })));
  $$("[data-tlock]", box).forEach((b) => (b.onclick = () => trackOp({ op: "lock", track: b.dataset.tlock })));
  $$("[data-tmute]", box).forEach((b) => (b.onclick = () => trackOp({ op: "mute", track: b.dataset.tmute })));
  // the empty zone under the tracks: double-click = pick a track type (the + Track menu)
  const nz = $(".lane.newtrk", box);
  if (nz) nz.ondblclick = (e) => { e.stopPropagation(); $("#trkMenu").hidden = false; };
  $$("[data-tdel]", box).forEach((b) => (b.onclick = () => {
    const t = (P.tracks || []).find((x) => x.id === b.dataset.tdel);
    if (t && (!t.items.length || confirm(`Delete the track “${t.name}” and its ${t.items.length} item(s)?\n(Undo brings it back.)`))) {
      if (TSEL && TSEL.track === t.id) TSEL = null;
      trackOp({ op: "remove", track: t.id }).then(renderTrkEdit);
    }
  }));
  $$("[data-ren]", box).forEach((el) => (el.ondblclick = () => {
    const t = (P.tracks || []).find((x) => x.id === el.dataset.ren); if (!t) return;
    const n = prompt("Track name:", t.name); if (n && n.trim() && n.trim() !== t.name) trackOp({ op: "rename", track: t.id, name: n.trim() });
  }));
  $$(".lane.trk", box).forEach((lane) => (lane.ondblclick = (e) => {
    if (e.target.closest(".tblk")) return;
    const t = (P.tracks || []).find((x) => x.id === lane.dataset.trk);
    if (t && t.locked) return toast(`“${t.name}” is locked – click 🔒 on its header to unlock it.`, true);
    if (t) trkAddAt(t, xToT(e.clientX - $(".stl-lanes", box).getBoundingClientRect().left));
  }));
  $$(".tblk", box).forEach((el) => el.addEventListener("pointerdown", tblkDown));
  renderTrkEdit();
}
function snapT(t) {  // snap to scene cuts and the playhead within ~6 px
  const m = renderTimes(true), tol = 6 / (STL.pps || 50);
  const pts = STL.blocks.filter((b) => inVid(b.s)).flatMap((b) => [sAt(b.s, m), sAt(b.s, m) + sDur(b.s, m)]);
  if (playerMode === "preview") pts.push(player.currentTime);
  const best = pts.reduce((a, p) => (Math.abs(p - t) < Math.abs(a - t) ? p : a), Infinity);
  return Math.abs(best - t) <= tol ? best : t;
}
function tblkDown(e) {
  if (e.button !== 0) return;
  const el = e.currentTarget, t = (P.tracks || []).find((x) => x.id === el.dataset.trk), it = t && t.items.find((x) => x.id === el.dataset.item);
  if (!it) return;
  if (t.locked) { TSEL = { track: t.id, item: it.id }; renderTrkEdit(); return toast(`“${t.name}” is locked – click 🔒 on its header to unlock it.`, true); }
  if (e.target.classList.contains("bx")) { e.stopPropagation(); if (TSEL && TSEL.item === it.id) TSEL = null; return trackOp({ op: "item_remove", track: t.id, item: it.id }).then(renderTrkEdit); }
  const mode = e.target.classList.contains("hd") ? (e.target.classList.contains("l") ? "l" : "r") : "move";
  const sp = itemSpan(it), x0 = e.clientX, left0 = parseFloat(el.style.left), w0 = parseFloat(el.style.width);
  const top0 = el.getBoundingClientRect().top;
  let dest = null;  // {main: before-scene} | {track}
  TSEL = { track: t.id, item: it.id };
  $$("#stl .tblk").forEach((k) => k.classList.toggle("sel", k === el));
  renderTrkEdit();
  el.setPointerCapture(e.pointerId); STL.busy = true;
  let moved = false;
  el.onpointermove = (ev) => {
    const dx = ev.clientX - x0; if (!moved && Math.abs(dx) < 4 && Math.abs(ev.clientY - (top0 + 10)) < 14) return; moved = true;
    if (mode === "move") {
      // which lane is under the pointer (looking through the dragged block)?
      const under = document.elementsFromPoint(ev.clientX, ev.clientY).find((n) => n !== el && !el.contains(n) && n.closest) || null;
      const ml = under && under.closest("#stl .lane.main"), ol = under && under.closest("#stl .lane.trk");
      const otk = ol && ol.dataset.trk !== t.id ? (P.tracks || []).find((x) => x.id === ol.dataset.trk) : null;
      $$("#stl .sblk").forEach((x) => x.classList.remove("dropl"));
      $$("#stl .lane").forEach((x) => x.classList.remove("dropzone"));
      dest = null;
      if (ml && (t.kind === "video" || t.kind === "broll")) {
        const lanes = $("#stl .stl-lanes").getBoundingClientRect(), cx = ev.clientX - lanes.left;
        const bef = STL.blocks.find((k) => inVid(k.s) && k.x + k.w / 2 > cx);
        dest = { main: bef ? bef.s.id : "end" };
        if (bef) $(`#stl .sblk[data-id="${bef.s.id}"]`).classList.add("dropl");
        ml.classList.add("dropzone");
        el.style.transform = `translateY(${ml.getBoundingClientRect().top + 6 - top0}px)`;
        $("#stlInfo").textContent = `“${srcName(it)}” → back into Video 1 ${bef ? `before scene ${bef.s.no}` : "at the end"}`;
      } else if (otk) {
        dest = { track: otk };
        ol.classList.add("dropzone");
        el.style.transform = `translateY(${ol.getBoundingClientRect().top + 3 - top0}px)`;
        $("#stlInfo").textContent = `→ “${otk.name}”`;
      } else el.style.transform = "";
      el.style.left = `${left0 + dx}px`;
      return;
    }
    else if (mode === "r") el.style.width = `${Math.max(10, w0 + dx)}px`;
    else { el.style.left = `${left0 + dx}px`; el.style.width = `${Math.max(10, w0 - dx)}px`; }
  };
  el.onpointerup = () => {
    el.onpointermove = el.onpointerup = null; STL.busy = false;
    $$("#stl .sblk").forEach((x) => x.classList.remove("dropl"));
    $$("#stl .lane").forEach((x) => x.classList.remove("dropzone"));
    if (!moved) return;
    if (dest && dest.main) {
      if (TSEL && TSEL.item === it.id) TSEL = null;
      trackOp({ op: "item_to_scene", track: t.id, item: it.id, before: dest.main }).then((r) => { if (r && r.scene) selectScene(r.scene, false); renderTrkEdit(); });
      return;
    }
    if (dest && dest.track) {
      const an = anchorAt(snapT(xToT(parseFloat(el.style.left))));
      if (!an) return renderSceneTimeline();
      trackOp({ op: "item_move_track", track: t.id, item: it.id, to: dest.track.id, ...an }).then((r) => {
        if (r && r.item) { TSEL = { track: r.track, item: r.item }; renderSceneTimeline(); renderTrkEdit(); } });
      return;
    }
    const L = parseFloat(el.style.left), W = parseFloat(el.style.width);
    let a = snapT(xToT(L)), b = snapT(xToT(L + W));
    if (mode === "move") b = a + it.dur;
    if (b - a < 0.2) b = a + 0.2;
    const an = anchorAt(a); if (!an) return renderSceneTimeline();
    const set = { ...an };
    if (mode !== "move") set.dur = +(b - a).toFixed(3);
    if (mode === "l" && it.src && !(it.src_len === 0)) set.in = Math.max(0, +((it.in || 0) + (a - sp.a)).toFixed(3));  // trimming the head moves the in-point
    trackOp({ op: "item_set", track: t.id, item: it.id, set }, true);
  };
  el.onpointercancel = () => { el.onpointermove = el.onpointerup = null; STL.busy = false; renderSceneTimeline(); };
}
function renderTrkEdit() {
  const box = $("#trkEdit"); if (!box) return;
  const t = TSEL && (P.tracks || []).find((x) => x.id === TSEL.track), it = t && t.items.find((x) => x.id === TSEL.item);
  if (!it) { box.hidden = true; box.innerHTML = ""; TSEL = null; return; }
  box.hidden = false;
  const sp = itemSpan(it), ref = it.src ? `${it.src.t}|${it.src.n}` : "";
  const opt = (v, l) => `<option value="${esc(v)}" ${v === ref ? "selected" : ""}>${esc(l)}</option>`;
  const assets = P.broll_assets || [];
  let srcSel = "";
  if (t.kind === "audio") {
    const snd = (LIBCAT && LIBCAT.sounds) || [];
    srcSel = `<select id="teSrc">${assets.filter((a) => a.audio).length ? `<optgroup label="Uploaded sounds">${assets.filter((a) => a.audio).map((a) => opt(`asset|${a.file}`, a.file)).join("")}</optgroup>` : ""}
      ${(P.music_files || []).length ? `<optgroup label="Brand Kit music">${P.music_files.map((f) => opt(`music|${f}`, f)).join("")}</optgroup>` : ""}
      <optgroup label="Sound of a clip">${P.sources.filter((s) => s.has_audio !== false).map((s) => opt(`clip|${s.file}`, s.file)).join("")}</optgroup>
      ${snd.length ? `<optgroup label="Library sounds">${snd.map((x) => opt(`lib|${x.file}`, `${x.category}: ${x.name}`)).join("")}</optgroup>` : ""}
      ${ref && !snd.some((x) => `lib|${x.file}` === ref) && !assets.some((a) => `asset|${a.file}` === ref) && !(P.music_files || []).some((f) => `music|${f}` === ref) && !P.sources.some((s) => `clip|${s.file}` === ref) ? opt(ref, srcName(it) + " (missing)") : ""}</select>`;
  } else if (t.kind !== "text") {
    srcSel = `<select id="teSrc">${assets.filter((a) => !a.audio).length ? `<optgroup label="Uploads">${assets.filter((a) => !a.audio).map((a) => opt(`asset|${a.file}`, `${a.still ? "🖼" : "🎞"} ${a.file}`)).join("")}</optgroup>` : ""}
      <optgroup label="Your clips">${P.sources.map((s) => opt(`clip|${s.file}`, s.file)).join("")}</optgroup></select>`;
  }
  const num = (id, v, step, lab, tip) => `<label class="tef" data-tip="${esc(tip)}">${lab} <input type="number" id="${id}" step="${step}" min="0" value="${v}"></label>`;
  box.innerHTML = `<div class="tehead"><b>${(TKIND[t.kind] || [""])[0]} ${esc(t.name)}</b>
      <span class="muted small">${sp.ok ? `${fmt(sp.a)} → ${fmt(sp.b)} · connected to scene ${(P.scenes.find((x) => x.id === it.anchor) || {}).no || "?"} (moves with it)` : "⚠ its scene is cut – not in the video"}</span>
      <span class="spacer"></span><button class="lnk danger" id="teDel">🗑 remove</button><button class="lnk" id="teClose">close</button></div>
    <div class="terow">
      ${t.kind === "text" ? `<input id="teText" value="${esc(it.text)}" maxlength="200" placeholder="Text (put *one word* in stars – shown plain)">
        <select id="teStyle" data-tip="title = big, middle · lower = coloured bar above the captions · top = coloured banner at the top">${["title", "lower", "top"].map((v) => `<option ${v === it.style ? "selected" : ""}>${v}</option>`).join("")}</select>` : srcSel}
      ${t.kind !== "text" ? `<button class="btn small" id="teUp" data-tip="Upload a ${t.kind === "audio" ? "sound or music file (MP3 / WAV / M4A …)" : "video or picture"} for this item">⬆ upload</button><input type="file" id="teFile" hidden accept="${t.kind === "audio" ? "audio/*" : "video/*,image/*"}">` : ""}
    </div>
    <div class="terow">
      ${num("teDur", it.dur, 0.1, "length", "Seconds on the timeline")}
      ${t.kind !== "text" && it.src_len !== 0 ? num("teIn", it.in || 0, 0.1, "starts at", "Seconds into the clip / sound where it starts playing") : ""}
      ${t.kind === "video" || t.kind === "broll" ? `<label class="tef">where <select id="teWhere">${[["full", "full screen"], ["top", "top"], ["bottom", "bottom"], ["pip", "corner (picture-in-picture)"]].map(([v, l]) => `<option value="${v}" ${v === it.where ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label class="tef">picture <select id="teFit"><option value="fill" ${it.fit !== "fit" ? "selected" : ""}>fill (crop)</option><option value="fit" ${it.fit === "fit" ? "selected" : ""}>whole picture</option></select></label>` : ""}
      ${t.kind === "video" || t.kind === "audio" ? `<label class="tef" data-tip="${t.kind === "video" ? "Its own sound (0 = silent)" : "Volume – music under your voice sounds right around 20-35 %"}">volume <input type="number" id="teVol" step="5" min="0" max="200" value="${Math.round((it.vol ?? 1) * 100)}"> %</label>` : ""}
      ${t.kind === "audio" ? num("teFi", it.fade_in || 0, 0.1, "fade in", "Seconds to fade in") + num("teFo", it.fade_out || 0, 0.1, "fade out", "Seconds to fade out") : ""}
    </div>`;
  const set = (o) => trackOp({ op: "item_set", track: t.id, item: it.id, set: o }, true);
  $("#teClose", box).onclick = () => { TSEL = null; renderTrkEdit(); renderSceneTimeline(); };
  $("#teDel", box).onclick = () => { TSEL = null; trackOp({ op: "item_remove", track: t.id, item: it.id }).then(renderTrkEdit); };
  const on = (id, fn) => { const el = $(id, box); if (el) el.onchange = () => fn(el.value); };
  on("#teSrc", (v) => { const [tt, ...n] = v.split("|"); set({ src: { t: tt, n: n.join("|") }, in: 0, dur: it.dur }); });
  on("#teText", (v) => v.trim() && set({ text: v }));
  on("#teStyle", (v) => set({ style: v }));
  on("#teDur", (v) => set({ dur: +v }));
  on("#teIn", (v) => set({ in: +v }));
  on("#teWhere", (v) => set({ where: v }));
  on("#teFit", (v) => set({ fit: v }));
  on("#teVol", (v) => set({ vol: +v / 100 }));
  on("#teFi", (v) => set({ fade_in: +v }));
  on("#teFo", (v) => set({ fade_out: +v }));
  if ($("#teUp", box)) {
    $("#teUp", box).onclick = () => $("#teFile", box).click();
    $("#teFile", box).onchange = (e) => {
      const f = e.target.files[0]; e.target.value = ""; if (!f) return;
      const fd = new FormData(); fd.append("files", f);
      toast(`Uploading ${f.name}…`);
      fetch(`/api/projects/${PID}/upload?kind=asset`, { method: "POST", body: fd }).then((r) => r.json().then((j) => [r.ok, j]))
        .then(([ok, j]) => { if (!ok) return toast(j.error || "Upload failed", true); set({ src: { t: "asset", n: j.file }, in: 0, dur: it.dur }); })
        .catch(() => toast("Upload failed – is the black Start Studio window still open?", true));
    };
  }
  if (t.kind === "audio" && !LIBCAT) loadLib().then(() => { if (TSEL && TSEL.item === it.id) renderTrkEdit(); });
}
const ZOOMS = [1, 1.5, 2, 3, 4, 6, 8, 12];
function setZoom(z) {
  const box = $("#stl"), head = $("#stlHead");
  const at = head ? (parseFloat((head.style.transform.match(/translateX\(([-\d.]+)px/) || [0, 0])[1]) || 0) : 0;
  const frac = STL.pps ? at / STL.pps : 0;
  STL.zoom = z; renderSceneTimeline();
  if (box && STL.pps) box.scrollLeft = Math.max(0, frac * STL.pps - box.clientWidth / 2);  // keep the playhead in view
}
$("#zIn").onclick = () => setZoom(ZOOMS.find((z) => z > (STL.zoom || 1)) || ZOOMS[ZOOMS.length - 1]);
$("#zOut").onclick = () => setZoom([...ZOOMS].reverse().find((z) => z < (STL.zoom || 1)) || 1);
$("#zFit").onclick = () => setZoom(1);
$("#stl").addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  (e.deltaY < 0 ? $("#zIn") : $("#zOut")).click();
}, { passive: false });
// ✨ AUTO-EDIT: one click – the studio places everything, renders, and the report says what it did per scene
// the order matters: ① the plan changes WHICH scenes exist; ② auto-edit decorates them. Decorating first = lost work.
function storyWaiting() { const sb = P.storyboard; return !!(sb && sb.status && !/applied/.test(String(sb.status))); }
$("#autoBtn").onclick = async () => {
  const b = $("#autoBtn");
  if (planPending() && !confirm("Step ① is still running: Claude's story plan hasn't arrived yet (say “process studio inbox” in VS Code).\n\nThe plan may cut or reorder scenes – what auto-edit puts on those scenes would be lost.\n\nAuto-edit now anyway?")) return;
  if (storyWaiting() && !confirm("A Claude storyboard is waiting for your approval (Storyboard button).\n\nApprove it first – it may cut or reorder scenes, and auto-edit work on those scenes would be lost.\n\nAuto-edit now anyway?")) return;
  if (P.auto_edit && !confirm("Run auto-edit again? It replaces its own earlier stickers / sounds / zooms / transitions (your own edits stay).")) return;
  b.disabled = true; b.textContent = "② ✨ Editing…";
  try {
    const r = await api(`/api/projects/${PID}/autoedit`, { method: "POST", body: { level: $("#autoLevel").value || null } });
    toast(r.reply);
    SHOW = { sid: (P.scenes.find(inVid) || {}).id, switchView: true, at: Date.now() };
    await refresh(true); renderPvBadge();
    $("#aeReport").open = true;
  } catch (e) { toast(e.message, true); }
  b.disabled = false; b.textContent = "② ✨ Auto-edit";
};
$("#aeClear").onclick = async () => {
  try { const r = await api(`/api/projects/${PID}/autoedit`, { method: "POST", body: { action: "clear" } }); toast(r.reply); await refresh(true); autoPreviewTick(); }
  catch (e) { toast(e.message, true); }
};
// 💬 SUGGESTIONS: a plain-words note for many scenes -> applied, then the edited video updates
let SUG_BUSY = false;
// ONE status line that always says what is happening: applying → updating the video (scene x/y) → done ✓,
// plus anything still waiting for Claude. Redrawn on every poll, so it is never stale.
// 🤖 is Claude auto-reply on? (then nobody needs to type "process studio inbox")
function CBAUTO() { const c = (typeof P !== "undefined" && P && P.claude_bot) || {}; return !!(c.installed && c.enabled); }
function claudeHint() { return CBAUTO() ? "Claude starts on it by itself – the reply shows in the studio." : "Say “process studio inbox” in VS Code."; }
function renderSugStatus() {
  const el = $("#sugStatus"); if (!el || !P) return;
  const j = P.job || {}, rendering = ["running", "queued"].includes(j.status) && ["preview", "final", "ref_audio", "caption_align"].includes(j.kind);
  const waiting = (P.commands || []).filter((c) => c.status === "claude").length;
  const st = typeof editedState === "function" ? editedState().k : "";
  let html = "";
  if (SUG_BUSY) html = `<span class="spin"></span> Applying your note…`;
  else if (rendering) html = `<span class="spin"></span> Working – ${esc(j.kind === "final" ? "making the final video" : j.kind === "ref_audio" ? "taking the voice out of the reference" : j.kind === "caption_align" ? "lining up your captions with your voice" : "updating your edited video")}${j.progress && j.progress !== "Queued" ? ` · ${esc(j.progress)}` : j.status === "queued" ? " · starting…" : ""}`;
  else if (j.status === "error" && (P.suggestions || []).length) html = `<span class="bad">✗ The last update failed – ${esc((j.error || "").split("\n")[0].slice(0, 140))}</span>`;
  else if ((P.suggestions || []).length && st === "fresh") html = `<span class="okc">✓ Done – your edited video is up to date</span> <button class="lnk" id="sugPlay">▶ watch it</button>`;
  else if ((P.suggestions || []).length && st === "stale") html = `<span class="spin"></span> Your edited video is about to update…`;
  const cb = P.claude_bot || {}, sep = html ? " &nbsp;·&nbsp; " : "";
  const box = $("#cbAuto"); if (box && document.activeElement !== box) { box.checked = cb.enabled !== false; box.disabled = !cb.installed; }
  if (cb.running) {
    const mins = cb.started ? Math.max(0, Math.round((Date.now() - new Date(cb.started)) / 60000)) : 0;
    html += `${sep}<span class="clw"><span class="spin"></span> 🤖 Claude is working on ${waiting || "your"} request${waiting === 1 ? "" : "s"}${mins ? ` · ${mins} min` : ""} – the reply shows here</span>`;
  } else if (waiting) {
    const why = !cb.installed ? "Claude Code isn't installed on this PC" : !cb.enabled ? "auto-reply is off" : cb.busy_other ? "Claude is busy with another project – yours is next" : cb.last && cb.last.error ? cb.last.error : "";
    const auto = cb.installed && cb.enabled && !cb.busy_other && !(cb.last && cb.last.error);
    html += `${sep}<span class="clw"><span class="dot"></span> ${waiting} waiting for Claude${auto ? " – starting by itself…" : ` – ${esc(why)}`}</span>`
      + (cb.last && cb.last.error && cb.enabled ? ` <button class="lnk" id="cbRetry">↻ try again</button>` : "")
      + (cb.installed && !cb.enabled ? ` <button class="lnk" id="cbOn">turn auto-reply on</button>` : "")
      + (!auto ? ` <span class="muted small">(or say “process studio inbox” in VS Code)</span>` : "");
  }
  el.hidden = !html;
  if (el.dataset.h !== html) {
    el.dataset.h = html; el.innerHTML = html;
    const pb = $("#sugPlay"); if (pb) pb.onclick = () => $("#previewBtn").click();
    const rt = $("#cbRetry"); if (rt) rt.onclick = () => api("/api/claude_auto", { method: "POST", body: { retry: PID } }).then(() => refresh(true));
    const on = $("#cbOn"); if (on) on.onclick = () => api("/api/claude_auto", { method: "POST", body: { on: true, retry: PID } }).then(() => refresh(true));
  }
}
$("#cbAuto").onchange = (e) => api("/api/claude_auto", { method: "POST", body: { on: e.target.checked } })
  .then((r) => { toast(r.enabled ? "🤖 Claude auto-reply on – requests the studio can't do start Claude by themselves." : "Claude auto-reply off – say “process studio inbox” in VS Code for those."); refresh(true); });
$("#sugSend").onclick = async () => {
  const ta = $("#sugText"), text = ta.value.trim(), b = $("#sugSend");
  if (!text) return toast("Type a change first – e.g. scene 5 money sticker and cash sound, or remove silences.", true);
  if (SUG_BUSY) return;
  SUG_BUSY = true; renderSugStatus();
  b.disabled = true; b.innerHTML = `<span class="spin"></span>`;
  try {
    const r = await api(`/api/projects/${PID}/suggest`, { method: "POST", body: { text } });
    const ok = r.results.filter((x) => x.ok).length;
    toast(`${ok} of ${r.results.length} change(s) done` + (r.queued ? ` · ${r.queued} sent to Claude` : "") + (r.changed ? " – updating your edited video…" : "."), !ok && !r.queued);
    if (r.changed) { ta.value = ""; SHOW = { sid: (P.scenes.find(inVid) || {}).id, switchView: true, at: Date.now() }; }
    await refresh(true); renderPvBadge();
  } catch (e) { toast(e.message, true); }
  SUG_BUSY = false; renderSugStatus();
  b.disabled = false; b.textContent = "Send";
};
$("#sugText").addEventListener("keydown", (e) => {  // chat style: Enter sends, Shift+Enter = a new line
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#sugSend").click(); } });
function renderSuggest() {
  renderSugStatus();
  const ul = $("#sugResult"), last = (P.suggestions || [])[0];
  $("#sugBox").classList.toggle("ready", !!P.auto_edit);
  if (!last) { ul.hidden = true; return; }
  ul.hidden = false;
  ul.innerHTML = `<li class="muted small">Last note (${esc(new Date(last.at).toLocaleTimeString())}):</li>` + last.results.map((x) => {
    const ic = x.ok ? "✓" : x.ok === null ? "→" : "✗", cls = x.ok ? "ok" : x.ok === null ? "cl" : "bad";
    const sc = x.scene && P.scenes[x.scene - 1];
    const who = sc ? `<button class="lnk" data-sgsel="${sc.id}">Scene ${x.scene}</button>` : x.scene ? `Scene ${x.scene}` : "Whole video";
    return `<li class="${cls}"><b>${ic}</b> ${who} · <i>${esc(x.text)}</i> – ${esc(x.reply)}</li>`; }).join("");
  $$("[data-sgsel]", ul).forEach((b) => (b.onclick = () => selectScene(b.dataset.sgsel)));
}
function renderAutoReport() {
  const box = $("#aeReport"), ae = P && P.auto_edit;
  if (!box) return;
  if (!ae) { box.hidden = true; return; }
  box.hidden = false;
  const c = ae.counts || {};
  const sbAt = (P.storyboard || {}).applied_at;
  const stale = sbAt && ae.at && sbAt > ae.at;
  $("summary", box).innerHTML = (stale ? `<span class="aestale">⚠ The story changed after auto-edit (storyboard applied) – click ② Auto-edit again to fit the new cut.</span><br>` : "") + `✨ Auto-edit (${esc(ae.level)}) · ${c.stickers || 0} stickers · ${c.sfx || 0} sounds · ${c.punches || 0} zooms · ${c.transitions || 0} transitions · ${c.effects || 0} effects <span class="muted small">– click a scene to review it</span>`;
  $("ol", box).innerHTML = (ae.report || []).map((l) => { const m = l.match(/^Scene (\d+):\s*(.*)$/); const sc = m && P.scenes[+m[1] - 1];
    return `<li>${sc ? `<button class="lnk" data-aesel="${sc.id}">Scene ${m[1]}</button>: ${esc(m[2])}` : esc(l)}</li>`; }).join("");
  $$("[data-aesel]", box).forEach((b) => (b.onclick = () => selectScene(b.dataset.aesel)));
}
$("#addTrackBtn").onclick = (e) => { e.stopPropagation(); $("#trkMenu").hidden = !$("#trkMenu").hidden; };
document.addEventListener("click", (e) => { if (!e.target.closest(".trkadd")) $("#trkMenu").hidden = true; });
$$("[data-addtrk]").forEach((b) => (b.onclick = async () => {
  $("#trkMenu").hidden = true;
  const r = await trackOp({ op: "add", kind: b.dataset.addtrk });
  if (r) toast(`${r.reply} Double-click on it to add something${b.dataset.addtrk === "video" || b.dataset.addtrk === "broll" ? ", or drag a clip chip onto it" : ""}.`);
}));
function bindPlayhead(box) {
  const knob = $(".ph-knob", box), cut = $(".ph-cut", box), lanes = $(".stl-lanes", box);
  if (knob) knob.onpointerdown = (e) => {
    e.preventDefault(); e.stopPropagation(); knob.setPointerCapture(e.pointerId);
    let last = 0;
    knob.onpointermove = (ev) => { const now = Date.now(); if (now - last < 60) return; last = now; seekEdit(ev.clientX - lanes.getBoundingClientRect().left); };
    knob.onpointerup = () => { knob.onpointermove = knob.onpointerup = null; };
  };
  if (cut) cut.onclick = (e) => { e.stopPropagation(); splitAtPlayhead(); };
}
function splitAtPlayhead() {
  if (playerMode === "source") return $("#splitHere").click();
  // Edited view: find the scene under the playhead and the matching second inside it
  const m = renderTimes(false), t = player.currentTime;
  const s = P.scenes.filter(inVid).find((x) => t >= sAt(x, m) && t < sAt(x, m) + sDur(x, m));
  if (!s) return toast("Put the playhead on a scene first.", true);
  const into = (t - sAt(s, m)) * (s.speed || 1);
  if (into < 0.3 || into > (s.out - s.in) - 0.3) return toast("Too close to the scene's start or end to split there.", true);
  sendCommand(s.id, `split at ${into.toFixed(2)}`).then(() => {
    if (s.remove_silence || (s.cuts || []).length) toast("Split made – the scene has jump cuts, so the cut point is approximate. Check it in Original footage.");
  });
}
function renderTimes(fresh) {  // id -> [start, dur] measured in the rendered file (null if unknown / stale)
  const pv = P && latestPreview();
  if (!pv || !pv.times || (fresh && pv.sig !== P.edit_sig)) return null;
  return new Map(pv.times.map(([id, a, d]) => [id, [a, d]]));
}
const sAt = (s, m) => (m && m.has(s.id) ? m.get(s.id)[0] : s.start_at);
const sDur = (s, m) => (m && m.has(s.id) ? m.get(s.id)[1] : s.est);
function tlX(t) {  // edited time -> px (piecewise: blocks have minimum widths)
  const m = renderTimes(true);
  for (const b of STL.blocks) if (inVid(b.s) && t <= sAt(b.s, m) + sDur(b.s, m) + 1e-6) return b.x + Math.max(0, t - sAt(b.s, m)) / (sDur(b.s, m) || 1) * b.w;
  const l = STL.blocks[STL.blocks.length - 1]; return l ? l.x + l.w : 0;
}
function seekEdit(px) {
  const b = STL.blocks.find((k) => px >= k.x && px <= k.x + k.w + 2) || STL.blocks[STL.blocks.length - 1]; if (!b) return;
  if (!inVid(b.s)) return selectScene(b.s.id, false);
  const frac = Math.min(1, Math.max(0, (px - b.x) / b.w));
  if (playerMode === "preview" && latestPreview()) { const m = renderTimes(false); selected = b.s.id; renderDetail(); player.currentTime = sAt(b.s, m) + frac * sDur(b.s, m); drawStlHead(); return; }
  selectScene(b.s.id, false);
  const go = () => { player.currentTime = b.s.in + frac * (b.s.out - b.s.in); };
  if (player.readyState >= 1) setTimeout(go, 50); else player.addEventListener("loadedmetadata", go, { once: true });
}
function drawStlHead() {
  const h = $("#stlHead"); if (!h || !P) return;
  let t = null;
  if (playerMode === "preview") t = player.currentTime;
  else { const s = curScene(); if (s && s.keep && !isBroll(s) && STL.blocks.some((b) => b.s.id === s.id)) t = s.start_at + Math.min(s.est, Math.max(0, (player.currentTime - s.in) / (s.speed || 1))); }
  h.style.display = t == null ? "none" : "";
  if (t != null) h.style.transform = `translateX(${tlX(t)}px)`;
}
$("#player").addEventListener("timeupdate", drawStlHead);  // (`player` const is declared further down)
$("#player").addEventListener("seeked", drawStlHead);
function stlDown(e) {
  const el = e.currentTarget, id = el.dataset.id, b = STL.blocks.find((k) => k.s.id === id);
  if (!b || e.button !== 0) return;
  if ((P.ui_locks || {}).main) { selectScene(id); return toast("Video 1 is locked – click 🔒 on its header to unlock it.", true); }
  const edge = e.target.classList.contains("hd") ? (e.target.classList.contains("l") ? "l" : "r") : null;
  STL.drag = { id, b, el, edge, x0: e.clientX, moved: false, top0: el.getBoundingClientRect().top };
  el.setPointerCapture(e.pointerId);
  el.onpointermove = stlMove; el.onpointerup = stlUp; el.onpointercancel = () => { STL.drag = null; renderSceneTimeline(); };
}
function stlMove(e) {
  const d = STL.drag; if (!d) return;
  const dx = e.clientX - d.x0;
  if (!d.moved && Math.abs(dx) < 4) return;
  d.moved = true;
  const s = d.b.s, sp = s.speed || 1;
  if (d.edge) {  // trim: px -> source seconds, clamped to the scene's original range and a 0.3 s minimum
    const ds = dx / STL.pps * sp;
    if (d.edge === "l") { d.val = Math.min(s.out - 0.3, Math.max(s.orig_in ?? s.in, s.in + ds)); d.el.style.left = `${d.b.x + (d.val - s.in) / sp * STL.pps}px`; d.el.style.width = `${Math.max(6, d.b.w - (d.val - s.in) / sp * STL.pps)}px`; }
    else { d.val = Math.max(s.in + 0.3, Math.min(s.orig_out ?? s.out, s.out + ds)); d.el.style.width = `${Math.max(6, d.b.w + (d.val - s.out) / sp * STL.pps)}px`; }
    const diff = d.edge === "l" ? d.val - s.in : s.out - d.val;
    $("#stlInfo").textContent = `${d.edge === "l" ? "Start" : "End"}: ${diff >= 0 ? "trim" : "extend"} ${Math.abs(diff).toFixed(2)} s`;
  } else {
    // dragged UP onto the B-roll lane = make it B-roll footage; onto a Video / B-roll / Audio TRACK = lift it there
    // look THROUGH the dragged block (it moves under the pointer) to the lane underneath
    const over = document.elementsFromPoint(e.clientX, e.clientY).find((n) => n !== d.el && !d.el.contains(n) && n.closest) || null;
    const tl = over ? over.closest("#stl .lane.trk") : null;
    const nz = over ? over.closest("#stl .lane.newtrk") : null;
    nz && $(".lane.newtrk").classList.add("dropzone");
    if (!nz) { const z = $("#stl .lane.newtrk"); if (z) z.classList.remove("dropzone"); }
    if (nz && !isBroll(d.b.s) && d.b.s.keep) {
      d.toNew = true; d.toTrack = null; d.dropT = xToT(Math.max(0, d.b.x + dx));
      d.el.style.transform = `translate(${dx}px, ${nz.getBoundingClientRect().top + 3 - d.top0}px)`;
      d.el.classList.add("dragging");
      $("#stlInfo").textContent = `Scene ${d.b.s.no} → a NEW Video track at ${fmt(d.dropT)} – the main video closes the gap`;
      return;
    }
    d.toNew = false;
    const tk = tl && (P.tracks || []).find((x) => x.id === tl.dataset.trk);
    const okTrack = !!tk && tk.kind !== "text" && !isBroll(d.b.s) && d.b.s.keep;
    $$("#stl .lane.trk").forEach((x) => x.classList.toggle("dropzone", x === tl && okTrack));
    d.toTrack = okTrack ? tk : null;
    if (d.toTrack) {
      d.dropT = xToT(Math.max(0, d.b.x + dx));
      d.el.style.transform = `translate(${dx}px, ${tl.getBoundingClientRect().top + 3 - d.top0}px)`;
      d.el.classList.add("dragging");
      $$(".sblk", $("#stl")).forEach((x) => x.classList.remove("dropl"));
      $("#stlInfo").textContent = `Scene ${d.b.s.no} → “${tk.name}” at ${fmt(d.dropT)} (${tk.kind === "broll" ? "picture only" : tk.kind === "audio" ? "sound only" : "picture + sound"}) – the main video closes the gap`;
      return;
    }
    if (tk && tk.kind === "text") $("#stlInfo").textContent = "Scenes can't go on a Text track – drop it on a Video, B-roll or Audio track.";
    else if (tk && !d.b.s.keep) $("#stlInfo").textContent = `Scene ${d.b.s.no} is cut – restore it first.`;
    d.toBroll = !!(over && over.closest && over.closest("#stl .lane.broll"));
    const dy = d.toBroll ? -(d.el.offsetTop + d.el.offsetHeight) + 10 : 0;
    d.el.style.transform = `translate(${dx}px, ${dy}px)`; d.el.classList.add("dragging");
    d.el.classList.toggle("tobroll", d.toBroll);
    $("#stl .lane.broll").classList.toggle("dropzone", d.toBroll);
    if (d.toBroll) { $$(".sblk", $("#stl")).forEach((x) => x.classList.remove("dropl")); $("#stlInfo").textContent = `Scene ${d.b.s.no} → B-roll footage (leaves the video, its picture can cover other scenes)`; return; }
    const cx = d.b.x + d.b.w / 2 + dx;
    d.before = (STL.blocks.find((k) => k.s.id !== d.id && k.x + k.w / 2 > cx) || {}).s;
    $$(".sblk", $("#stl")).forEach((x) => x.classList.toggle("dropl", d.before && x.dataset.id === d.before.id));
  }
}
async function stlUp() {
  const d = STL.drag; STL.drag = null; if (!d) return;
  const s = d.b.s;
  if (!d.moved) { selectScene(s.id); return; }
  const bl = $("#stl .lane.broll"); if (bl) bl.classList.remove("dropzone");
  $$("#stl .lane.trk").forEach((x) => x.classList.remove("dropzone"));
  try {
    if (!d.edge && d.toNew) {
      const z = $("#stl .lane.newtrk"); if (z) z.classList.remove("dropzone");
      const r = await trackOp({ op: "scene_to_track", new_kind: "video", scene: s.id, t: +d.dropT.toFixed(3) });
      if (r && r.item) { TSEL = { track: r.track, item: r.item }; renderSceneTimeline(); renderTrkEdit(); }
      return;
    }
    if (!d.edge && d.toTrack) {
      const tk = d.toTrack, speech = (s.transcript || "").trim() && !s.no_speech;
      const words = (s.caption_view ?? s.transcript ?? "").split(/\s+/).slice(0, 10).join(" ");
      if (tk.kind === "broll" && speech && !confirm(`Scene ${s.no} has speech:\n“${words}…”\n\nA B-roll track is SILENT – these words won't be heard (a Video track keeps the sound). Continue?`)) { renderSceneTimeline(); return; }
      const r = await trackOp({ op: "scene_to_track", track: tk.id, scene: s.id, t: +d.dropT.toFixed(3) });
      if (r && r.item) { TSEL = { track: tk.id, item: r.item }; renderSceneTimeline(); renderTrkEdit(); }
      return;
    }
    if (!d.edge && d.toBroll && isBroll(s)) { /* already B-roll */ }
    else if (!d.edge && isBroll(s)) {
      // a B-roll scene dragged to a place in the video = it plays there as a normal scene (one undo step)
      const used = usedByFile(s.src);
      if (used.length && !confirm(`“${s.src}” now covers ${rangeTxt(used)}.
Playing it as its own scene removes that B-roll (the same footage would show twice). Continue?`)) { renderSceneTimeline(); return; }
      const r = await api(`/api/projects/${PID}/broll/insert`, { method: "POST", body: { file: s.src, before: d.before ? d.before.id : "end" } });
      toast(`${r.reply} (Undo reverts it)`); await refresh(true);
    } else if (!d.edge && d.toBroll) {
      if (!s.keep) toast(`Scene ${s.no} is cut – restore it first.`, true);
      else if ((s.transcript || "").trim() && !s.no_speech &&
               !confirm(`Scene ${s.no} has speech:\n“${(s.caption_view ?? s.transcript).split(/\s+/).slice(0, 10).join(" ")}…”\n\nAs B-roll footage it LEAVES the video – these words won't be heard. Continue?`)) { /* cancelled */ }
      else await sendCommand(s.id, "role broll");
    } else if (d.edge && d.val != null) {
      await sendCommand(s.id, d.edge === "l" ? `trim start to ${r2(d.val - (s.orig_in ?? s.in))}` : `trim end to ${r2((s.orig_out ?? s.out) - d.val)}`);
    } else if (!d.edge) {
      // drop position among VIDEO scenes -> the scene id it goes before in the full list (B-roll scenes keep their place)
      const to = d.before ? d.before.id : "end";
      await api(`/api/projects/${PID}/scene/${s.id}/move`, { method: "POST", body: { to } });
      await refresh(true);
    }
  } catch (err) { toast(err.message, true); }
  renderSceneTimeline();
}
/* ---- B-roll on the timeline: drag a clip chip onto a scene, drag a B-roll block to move it, drag its edge to cover
   more/fewer scenes, × removes. Snaps to whole scenes (what the render uses) – split a scene (S) for finer placement.
   Every drop is ONE server call = one undo step. */
function blockAt(clientX) {
  const lanes = $("#stl .stl-lanes"); if (!lanes || !STL.blocks.length) return -1;
  const px = clientX - lanes.getBoundingClientRect().left;
  let best = 0, bd = Infinity;
  STL.blocks.forEach((b, i) => { const d = px < b.x ? b.x - px : px > b.x + b.w ? px - b.x - b.w : 0; if (d < bd) { bd = d; best = i; } });
  return best;
}
function keptFrom(i0, n) {  // the next n scenes that are in the video, starting at block i0 (cut scenes skipped)
  const out = [];
  for (let i = Math.max(0, i0); i < STL.blocks.length && out.length < n; i++) if (inVid(STL.blocks[i].s)) out.push(i);
  return out;
}
function usedByFile(f) { return P.scenes.filter((x) => x.keep && x.role !== "broll" && x.broll && x.broll.src === f).map((x) => x.no); }
function showInsLine(px) {
  let l = $("#stl .insline");
  if (px == null) { if (l) l.remove(); return; }
  if (!l) { l = document.createElement("div"); l.className = "insline"; $("#stl .stl-lanes").appendChild(l); }
  l.style.left = `${px}px`;
}
function markTargets(idx) { $$("#stl .sblk").forEach((el, i) => el.classList.toggle("droptgt", idx.includes(i))); }
async function sendBroll(set) {
  try {
    const r = await api(`/api/projects/${PID}/broll`, { method: "POST", body: { set } });
    if (!r.unchanged) toast(`${r.reply} (Undo reverts it)`);
    await refresh(true);
  } catch (e) { toast(e.message, true); }
  renderSceneTimeline();
}
function chipDown(e) {
  if (e.button !== 0) return;
  const tl = (ev) => { const el = document.elementFromPoint(ev.clientX, ev.clientY); return el && el.closest ? el.closest("#stl .lane.trk") : null; };
  e.currentTarget.addEventListener("pointerup", (ev) => {  // dropped on a Video / B-roll track: add it there
    const lane = tl(ev);
    const nz = (() => { const el = document.elementFromPoint(ev.clientX, ev.clientY); return el && el.closest ? el.closest("#stl .lane.newtrk") : null; })();
    if (nz) {  // the empty zone: a new track for it – speech clips on a Video track (keeps the sound), the rest B-roll
      const f = e.currentTarget.dataset.file, asset = (P.broll_assets || []).find((a) => a.file === f), src = P.sources.find((x) => x.file === f);
      const kind = asset ? (asset.audio ? "audio" : "broll") : (src && src.has_audio !== false && P.scenes.some((x) => x.src === f && (x.transcript || "").trim() && !x.no_speech) ? "video" : "broll");
      const an = anchorAt(xToT(ev.clientX - $("#stl .stl-lanes").getBoundingClientRect().left));
      if (!an) return;
      const item = Object.assign({ src: { t: asset ? "asset" : "clip", n: f }, dur: asset && asset.still ? 3 : Math.min(5, +(asset ? asset.duration : src && src.duration) || 5), where: "full" }, an);
      trackOp({ op: "item_add", new_kind: kind, item }).then((r) => { if (r && r.item) { TSEL = { track: r.track, item: r.item }; renderSceneTimeline(); } });
      return;
    }
    if (!lane) return;
    const t = (P.tracks || []).find((x) => x.id === lane.dataset.trk);
    if (!t || (t.kind !== "video" && t.kind !== "broll")) return toast(t && t.kind === "audio" ? "Clips go on a Video or B-roll track – for an Audio track use “+ add” there." : "Drop clips on a Video or B-roll track.", true);
    const f = e.currentTarget.dataset.file, isAsset = (P.broll_assets || []).some((a) => a.file === f);
    trkAddAt(t, xToT(ev.clientX - $("#stl .stl-lanes").getBoundingClientRect().left), { src: { t: isAsset ? "asset" : "clip", n: f } });
  }, { once: true });
  const chip = e.currentTarget, file = chip.dataset.file, x0 = e.clientX, y0 = e.clientY;
  const isBclip = (P.broll_clips || []).some((c) => c.file === file);  // only B-roll clips can be inserted as scenes
  let ghost = null, tgt = null, insertAt = null;
  chip.setPointerCapture(e.pointerId);
  STL.busy = true;
  chip.onpointermove = (ev) => {
    if (!ghost && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 5) return;
    if (!ghost) { ghost = document.createElement("div"); ghost.className = "dragghost"; ghost.textContent = `🎞 ${file}`; document.body.appendChild(ghost); }
    ghost.style.transform = `translate(${ev.clientX + 12}px, ${ev.clientY + 8}px)`;
    const el = document.elementFromPoint(ev.clientX, ev.clientY);
    const blk = el && el.closest ? el.closest("#stl .sblk") : null;
    const inLane = el && el.closest ? el.closest("#stl .lane.main") : null;
    tgt = blk ? STL.blocks.findIndex((b) => b.s.id === blk.dataset.id) : -1;
    // near a scene's EDGE (or in a gap / past the end) = insert as its own scene; middle = cover that scene
    insertAt = null;
    if (isBclip && (blk || inLane)) {
      const lanes = $("#stl .stl-lanes").getBoundingClientRect(), px = ev.clientX - lanes.left;
      if (tgt >= 0) {
        const b = STL.blocks[tgt], frac = (px - b.x) / b.w, edge = Math.min(18, b.w * 0.25);
        if (px - b.x < edge || frac < 0.22) insertAt = tgt; else if (b.x + b.w - px < edge || frac > 0.78) insertAt = tgt + 1;
      } else insertAt = blockAt(ev.clientX) + (px > (STL.blocks[blockAt(ev.clientX)] || { x: 0 }).x ? 1 : 0);
    }
    if (insertAt != null) {
      tgt = -1;
      const b = STL.blocks[insertAt], last = STL.blocks[STL.blocks.length - 1];
      showInsLine(b ? b.x - 2 : last.x + last.w + 1);
      ghost.textContent = `➕ Insert “${file}” as its own scene ${b ? `before scene ${b.s.no}` : "at the end"}`;
    } else {
      showInsLine(null);
      ghost.textContent = tgt >= 0 ? `🎞 Cover scene ${STL.blocks[tgt].s.no} with “${file}” (voice keeps playing)` : `🎞 ${file}`;
    }
    markTargets(tgt >= 0 ? [tgt] : []);
    ghost.classList.toggle("ok", tgt >= 0 || insertAt != null);
  };
  const end = () => {
    chip.onpointermove = chip.onpointerup = chip.onpointercancel = null;
    STL.busy = false;
    markTargets([]);
    showInsLine(null);
    if (!ghost) { if (chip.dataset.sel) selectScene(chip.dataset.sel); return; }
    ghost.remove();
    if (insertAt != null) {
      const b = STL.blocks[insertAt];
      const used = usedByFile(file);
      if (used.length && !confirm(`“${file}” now covers ${rangeTxt(used)}.\nAs its own scene that B-roll is removed (the same footage would show twice). Continue?`)) return;
      return api(`/api/projects/${PID}/broll/insert`, { method: "POST", body: { file, before: b ? b.s.id : "end" } })
        .then((r) => { toast(`${r.reply} (Undo reverts it)`); return refresh(true); })
        .catch((err) => toast(err.message, true)).then(renderSceneTimeline);
    }
    if (tgt == null || tgt < 0) return toast("Drop the clip ON a scene (cover it) or on a scene's edge (insert it as its own scene).", true);
    const s = STL.blocks[tgt].s;
    if (!inVid(s)) return toast(`Scene ${s.no} ${isBroll(s) ? "is B-roll footage (not played)" : "is cut – restore it first"} – drop on another scene.`, true);
    if (s.src === file) return toast(`Scene ${s.no} comes from that clip – a scene can't cover itself.`, true);
    const old = s.broll && s.broll.src !== file ? ` (replaces ${s.broll.src})` : "";
    sendBroll({ [s.id]: { src: file } }).then(() => old && toast(`B-roll on scene ${s.no}${old}.`));
  };
  chip.onpointerup = end; chip.onpointercancel = () => { if (ghost) ghost.remove(); ghost = null; tgt = -1; insertAt = null; end(); };
}
function bDown(e) {
  if ((P.ui_locks || {}).broll) return toast("The B-roll lane is locked – click 🔒 on its header to unlock it.", true);
  if (e.button !== 0) return;
  const el = e.currentTarget, run = STL.bRuns[+el.dataset.run]; if (!run) return;
  const runIdx = []; for (let i = run.start; i <= run.end; i++) runIdx.push(i);
  const first = STL.blocks[run.start].s, raw = first.broll || {};
  const entry = (withAt) => ({ src: raw.src, ...(raw.where ? { where: raw.where } : {}), ...(raw.fit === "fit" ? { fit: "fit" } : {}), ...(withAt && raw.at != null ? { at: raw.at } : {}) });
  if (e.target.classList.contains("bx")) {  // remove the whole run
    e.stopPropagation();
    return sendBroll(Object.fromEntries(runIdx.map((i) => [STL.blocks[i].s.id, null])));
  }
  const mode = e.target.classList.contains("hd") ? (e.target.classList.contains("l") ? "l" : "r") : "move";
  const x0 = e.clientX; let moved = false, newIdx = runIdx;
  el.setPointerCapture(e.pointerId);
  STL.busy = true;
  const span = (idx) => { const a = STL.blocks[idx[0]], b = STL.blocks[idx[idx.length - 1]]; return [a.x, b.x + b.w - a.x]; };
  el.onpointermove = (ev) => {
    const dx = ev.clientX - x0;
    if (!moved && Math.abs(dx) < 4) return;
    moved = true;
    if (mode === "move") {
      const i0 = blockAt(run.x + dx + 4 + $("#stl .stl-lanes").getBoundingClientRect().left);
      newIdx = keptFrom(i0, runIdx.length);
      el.style.transform = `translateX(${dx}px)`; el.classList.add("dragging");
    } else {
      const i = blockAt(ev.clientX);
      const lo = mode === "l" ? Math.min(i, run.end) : run.start, hi = mode === "r" ? Math.max(i, run.start) : run.end;
      newIdx = []; for (let k = lo; k <= hi; k++) if (inVid(STL.blocks[k].s)) newIdx.push(k);
      if (newIdx.length) { const [l, w] = span(newIdx); el.style.left = `${l}px`; el.style.width = `${w}px`; }
    }
    markTargets(newIdx);
    $("#stlInfo").textContent = newIdx.length ? `B-roll → ${rangeTxt(newIdx.map((i) => STL.blocks[i].s.no))}` : "";
  };
  el.onpointerup = () => {
    el.onpointermove = el.onpointerup = el.onpointercancel = null;
    STL.busy = false;
    markTargets([]);
    if (!moved) return selectScene(first.id);
    if (!newIdx.length || newIdx.join() === runIdx.join()) return renderSceneTimeline();
    const set = {};
    runIdx.forEach((i) => { if (!newIdx.includes(i)) set[STL.blocks[i].s.id] = null; });
    newIdx.forEach((i, k) => {
      const s = STL.blocks[i].s;
      if (runIdx.includes(i) && mode !== "move") return;  // resize: scenes already covered stay as they are
      if (s.src === raw.src) return;                       // never cover a scene with its own clip
      set[s.id] = entry(k === 0);
    });
    const over = newIdx.filter((i) => !runIdx.includes(i)).map((i) => STL.blocks[i].s).filter((s) => s.broll && s.broll.src !== raw.src);
    if (over.length && !confirm(`Scene ${over.map((s) => s.no).join(", ")} already has other B-roll – replace it?`)) return renderSceneTimeline();
    sendBroll(set);
  };
  el.onpointercancel = () => { el.onpointermove = el.onpointerup = null; STL.busy = false; markTargets([]); renderSceneTimeline(); };
}
window.addEventListener("resize", () => { if ($("#tab-scenes").classList.contains("active")) renderSceneTimeline(); });
document.addEventListener("keydown", (e) => {
  if (!$("#tab-scenes").classList.contains("active") || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); $("#undoBtn").click(); return; }
  if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && ["c", "v", "d"].includes(e.key.toLowerCase())) {
    if (e.key.toLowerCase() === "c" && String(window.getSelection() || "").trim()) return;  // copying page text: leave it alone
    e.preventDefault(); if (!e.repeat) sceneClip(e.key.toLowerCase()); return;  // held key: one copy, not twenty
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const s = curScene();
  if (e.code === "Space") { if (e.target.tagName === "VIDEO") return;  // the player's own controls already toggle it
    e.preventDefault(); player.paused ? player.play().catch(() => {}) : player.pause(); }
  else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    e.preventDefault();
    const i = P.scenes.findIndex((x) => x.id === selected), j = i + (e.key === "ArrowRight" ? 1 : -1);
    if (P.scenes[j]) selectScene(P.scenes[j].id);
  }
  else if (e.key === "s" || e.key === "S") splitAtPlayhead();
  // Delete only – not Backspace: too easy to hit by accident
  else if (e.key === "Delete" && s && !isBroll(s)) { e.preventDefault(); sendCommand(s.id, s.keep ? "cut" : "keep"); }
  else if (e.key === "i" || e.key === "I") trimAtPlayhead("i");
  else if (e.key === "o" || e.key === "O") trimAtPlayhead("o");
});
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
// scene copy / paste: Ctrl+C copies the selected scene, Ctrl+V puts a copy AFTER the selected scene, Ctrl+D = copy right after itself.
// The clipboard holds the scene's id (per project), so a paste copies the scene as it is NOW (with all its edits).
let SCLIP = null, SCLIP_BUSY = false;
async function sceneClip(op) {
  const s = curScene();
  if (!s) return toast("Select a scene first.", true);
  if (op === "c") { SCLIP = { pid: PID, id: s.id }; toast(`Scene ${s.no} copied – select a scene and press Ctrl+V to put the copy after it.`); return; }
  let text = "duplicate";
  if (op === "v") {
    if (!SCLIP || SCLIP.pid !== PID) return toast("Nothing copied yet – select a scene and press Ctrl+C (or Ctrl+D to copy it right away).", true);
    const i = P.scenes.findIndex((x) => x.id === SCLIP.id);
    if (i < 0) { SCLIP = null; return toast("The copied scene doesn't exist any more (merged or undone) – copy again.", true); }
    text = `paste scene ${i + 1}`;
  }
  if (SCLIP_BUSY) return;  // one paste at a time (a double click must not make two copies by accident)
  SCLIP_BUSY = true;
  try {
    const r = await sendCommand(s.id, text);
    if (r && r.new && r.new.length) { SHOW = null; selectScene(r.new[0], false); }
  } finally { SCLIP_BUSY = false; }
}
async function sendCommand(scene, text) {
  if (!text.trim()) return;
  try {
    const r = await api(`/api/projects/${PID}/command`, { method: "POST", body: { scene, text } });
    const msg = [...r.replies.map((x) => x.replace(/^!/, "")), ...(r.queued.length ? [`Sent to Claude: “${r.queued.join("; ")}”`] : [])].join("  ·  ");
    toast(msg, r.replies.some((x) => x.startsWith("!")));
    if (r.replies.length && !r.replies.every((x) => x.startsWith("!"))) {
      // timeline edits (trim / split / cut) are often made watching the ORIGINAL footage – don't switch away then;
      // a look change (captions, zoom, card, text, speed…) is only visible in the edited view – go there when ready
      const timelineEdit = /^(trim|split|reset trim|untrim|cut|keep|restore|delete|remove|duplicate|paste)\b/i.test(text.trim()) && !/^remove (text|silences?|punch)/i.test(text.trim());
      SHOW = { sid: scene, switchView: !timelineEdit, at: Date.now() };
    }
    await refresh(true); loadPlayer(false); autoPreviewTick(); renderPvBadge();
    return r;
  } catch (e) { toast(e.message, true); }
}
$("#sceneSend").onclick = () => { const s = curScene(); if (!s) return toast("Select a scene first.", true); sendCommand(s.id, $("#sceneCmd").value).then(() => ($("#sceneCmd").value = "")); };
$("#sceneCmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#sceneSend").click(); });
const CHIP_TIP = { "cut": "Remove the selected scene from the video.", "keep": "Bring a cut scene back.",
  "trim start 0.5": "Remove half a second from the start of the scene.", "trim end 0.5": "Remove half a second from the end of the scene.",
  "speed 1.25": "Play the scene 25 % faster.", "slow mo": "Play the scene in slow motion.", "mute": "Silence the scene's sound.",
  "zoom in": "Punch-in zoom (1.2×) on the scene.", "remove silences": "Cut the pauses out of the scene (jump cuts).",
  "captions off": "Hide captions in this scene.", "remove text": "Remove the on-screen text from this scene." };
$$("#quickChips button").forEach((b) => { b.dataset.tip = CHIP_TIP[b.textContent] || "Send this command to the selected scene.";
  b.onclick = () => { const s = curScene(); if (s) sendCommand(s.id, b.textContent); }; });
$("#undoBtn").onclick = () => api(`/api/projects/${PID}/undo`, { method: "POST" }).then((r) => { toast(r.ok ? "Undone." : "Nothing to undo."); refresh(true); });
$("#sbBtn").onclick = () => window.open(`/storyboard/${PID}`, "_blank");
// "▶ Edited view": show the edited video in the player; make it first if it is missing or out of date
$("#previewBtn").onclick = async () => {
  if (!P) return;
  const st = editedState();
  if (st.k === "none" && !P.scenes.some((x) => inVid(x) && !x.src_skipped)) return toast("No scenes in the video yet.", true);
  if (st.k === "stale" || st.k === "none" || st.k === "failed") {
    try { await api(`/api/projects/${PID}/render`, { method: "POST", body: { kind: "preview" } }); AUTO.lastReq = P.edit_sig; await refresh(true); }
    catch (e) { return toast(e.message, true); }
  }
  if (latestPreview()) { playerMode = "preview"; $$(".player-bar .seg button").forEach((x) => x.classList.toggle("active", x.dataset.mode === "preview")); curSrcUrl = ""; loadPlayer(true); }
  else toast("Making the edited view… it plays here when ready.");
  PENDING_PLAY = st.k !== "fresh";
};
let PENDING_PLAY = false;
let SHOW = null;  // {sid, switchView, at}: the change you just made – shown + played when its update is ready
function renderPvBadge() {  // ON the video: an update is on its way (else a stale picture looks like "nothing happened")
  const b = $("#pvBadge"); if (!b || !P) return;
  const st = editedState();
  const busy = st.k === "updating" || (st.k === "stale" && AUTO.on);
  b.hidden = !busy || !(SHOW || playerMode === "preview");
  if (!b.hidden) b.textContent = SHOW && playerMode !== "preview" ? "⟳ Making the edited view with your change… it plays here in a few seconds"
    : "⟳ Updating the edited view with your change… a few seconds";
  if (st.k === "fresh" && SHOW && !SHOW.switchView && playerMode !== "preview") SHOW = null;  // timeline edit in Original view: done
}
function editedState() {  // fresh | updating | stale | failed | none
  const pv = latestPreview(), j = P.job || {};
  const rendering = (j.kind === "preview" || j.kind === "final") && (j.status === "running" || j.status === "queued") || (j.pending || []).includes("preview");
  if (rendering) return { k: "updating", t: `⟳ updating… ${j.kind === "preview" && j.progress ? esc(j.progress) : ""}` };
  if (j.kind === "preview" && j.status === "error" && (!pv || pv.sig !== P.edit_sig)) return { k: "failed", t: "⚠ last update failed – see Activity" };
  if (!pv) return { k: "none", t: "not made yet" };
  if (pv.sig && pv.sig === P.edit_sig) return { k: "fresh", t: "✓ up to date" };
  return { k: "stale", t: AUTO.on ? "⟳ will update…" : "⚠ has changes not shown yet" };
}
function renderEditedState() {
  const el = $("#evState"); if (!el || !P) return;
  const st = editedState();
  el.className = `evstate ${st.k}`; el.innerHTML = st.t;
  el.dataset.tip = { fresh: "The edited view shows all your changes.", updating: "Re-making the changed scenes – it plays automatically when ready.",
    stale: "You changed something after the last update.", failed: "The last update failed – open the Activity box (Footage tab) for the reason.",
    none: "Click ▶ Edited view to make it." }[st.k];
  // asked to watch while it was being made: start playing as soon as the new version is in
  if (PENDING_PLAY && st.k === "fresh") { PENDING_PLAY = false; playerMode = "preview"; $$(".player-bar .seg button").forEach((x) => x.classList.toggle("active", x.dataset.mode === "preview")); curSrcUrl = ""; loadPlayer(true); }
}
/* ---------------- export dialog ----------------
   Platform preset -> server fills frame / resolution / fps / file type (exportopts.normalize is the ONE source of
   truth); any field the user clicks overrides the preset. Remembered per browser: the platform and the fields picked
   by hand – never the frame (a 16:9 project must not inherit last week's 9:16 choice). */
const XP = { cat: null, o: null, n: null, seq: 0, plan: null };
function xpPrefs() { try { return JSON.parse(localStorage.getItem("cve_export") || "null") || {}; } catch (e) { return {}; } }
function xpSeconds() {
  const cur = (P.renders || []).filter((r) => !r.missing && r.sig === P.edit_sig && r.matches_edit !== false && !r.audio_only);
  return cur.length ? cur[cur.length - 1].duration : ((P.totals && P.totals.est) || 0);
}
function xpOpen() { return !$("#xpModal").classList.contains("hidden"); }
function closeExport() { $("#xpModal").classList.add("hidden"); }
async function openExport() {
  if (!PID) return;
  if (!P.scenes.some((x) => inVid(x) && !x.src_skipped)) return toast("Nothing to export yet – keep at least one scene.", true);
  try { XP.cat = await api(`/api/projects/${PID}/export_options`); } catch (e) { return toast(e.message, true); }
  const pr = xpPrefs(), o = { platform: XP.cat.platforms.some((p) => p.id === pr.platform) ? pr.platform : "edit" };
  for (const k of ["res", "fmt", "quality", "fps"]) if (pr[k]) o[k] = pr[k];
  XP.o = o; XP.n = null; XP.plan = null;
  $("#xpModal").classList.remove("hidden");
  renderExport(); xpPlan();
  setTimeout(() => $("#xpGo").focus(), 0);
}
async function xpPlan() {
  const seq = ++XP.seq;
  $("#xpGo").disabled = true;
  try {
    const j = await api(`/api/projects/${PID}/export_plan`, { method: "POST", body: { opts: XP.o, seconds: xpSeconds() } });
    if (seq !== XP.seq || !xpOpen()) return;  // an older answer arriving after a newer click
    XP.n = j.opts; XP.plan = j; renderExport();
  } catch (e) { if (seq === XP.seq) $("#xpSum").textContent = e.message; }
  finally { if (seq === XP.seq) $("#xpGo").disabled = !XP.n; }
}
function xpChips(el, items, cur, key) {
  el.innerHTML = items.map((x) => `<button type="button" class="${x.id === cur ? "on" : ""}" data-v="${esc(x.id)}"${x.tip ? ` title="${esc(x.tip)}"` : ""}>${x.html || esc(x.label)}</button>`).join("");
  $$("button", el).forEach((b) => (b.onclick = () => { XP.o[key] = b.dataset.v; xpPlan(); }));
}
function renderExport() {
  const c = XP.cat, n = XP.n || {}, ed = c.edit;
  const plat = c.platforms.find((p) => p.id === (n.platform || XP.o.platform)) || c.platforms[0];
  $("#xpPlat").innerHTML = c.platforms.map((p) => `<button type="button" class="xp-plat ${p.id === plat.id ? "on" : ""}" data-p="${p.id}"><span class="ic">${esc(p.icon)}</span>${esc(p.label)}</button>`).join("");
  $$("#xpPlat button").forEach((b) => (b.onclick = () => { XP.o = { platform: b.dataset.p }; xpPlan(); }));  // a preset resets every field
  $("#xpTip").textContent = plat.tip || "";
  const rec = plat.frames ? plat.frames[0] : null;
  xpChips($("#xpFrame"), c.frames.map((f) => ({ ...f, html: `${esc(f.label)}${f.id === ed.frame ? ` <i>your edit</i>` : f.id === rec ? ` <i>best here</i>` : ""}` })), n.frame, "frame");
  const srcP = Math.min(ed.src_w, ed.src_h);
  xpChips($("#xpRes"), c.res.map((r) => ({ ...r, label: r.id === "source" ? `As uploaded (${srcP}p)` : r.label })), n.res, "res");
  xpChips($("#xpFmt"), c.formats.map((f) => ({ ...f, tip: f.note })), n.fmt, "fmt");
  const fm = c.formats.find((f) => f.id === n.fmt);
  $("#xpFmtNote").textContent = fm ? fm.note : "";
  xpChips($("#xpQ"), c.quality.map((q) => ({ ...q, label: q.label.replace(/ – .*| \(.*/, ""), tip: q.label })), n.quality, "quality");
  xpChips($("#xpFps"), c.fps.map((f) => ({ id: f, label: f === "edit" ? `Same as edit (${ed.fps})` : `${f} fps` })), n.fps, "fps");
  const audio = n.fmt === "mp3";
  ["xpFrame", "xpRes", "xpQ", "xpFps"].forEach((id) => $("#" + id).classList.toggle("off", audio));
  const pl = XP.plan;
  $("#xpSum").innerHTML = pl ? `<b>${audio ? "Audio only" : `${pl.w}×${pl.h}`}</b>${audio ? "" : ` · ${pl.fps} fps`} · ${esc(fm ? fm.label : "")}` +
    ` · ≈ <b>${pl.mb >= 1024 ? (pl.mb / 1024).toFixed(1) + " GB" : Math.max(1, Math.round(pl.mb)) + " MB"}</b>` +
    ` · about ${pl.minutes < 1 ? "1 min" : Math.round(pl.minutes) + " min"} to make` : "Working out the size…";
  $("#xpWarn").innerHTML = pl ? pl.warnings.map((w) => `<li>${esc(w)}</li>`).join("") : "";
  $("#xpGo").textContent = `Export${plat.id !== "edit" && plat.id !== "custom" ? " for " + plat.label : ""}`;
}
$("#finalBtn").onclick = openExport;
$("#xpClose").onclick = $("#xpCancel").onclick = closeExport;
$("#xpModal").addEventListener("mousedown", (e) => { if (e.target === e.currentTarget) closeExport(); });
// while the dialog is open the timeline's keys (Space = play, Del = cut scene, S = split…) must not fire behind it
document.addEventListener("keydown", (e) => {
  if (!xpOpen()) return;
  if (e.key === "Escape") { e.preventDefault(); closeExport(); }
  e.stopPropagation();
}, true);
$("#xpGo").onclick = async () => {
  if (!XP.n) return;
  const btn = $("#xpGo"); btn.disabled = true;
  try {
    const j = await api(`/api/projects/${PID}/render`, { method: "POST", body: { kind: "final", opts: XP.n } });
    const keep = { platform: XP.n.platform };
    for (const k of ["res", "fmt", "quality", "fps"]) if (XP.o[k]) keep[k] = XP.o[k];
    try { localStorage.setItem("cve_export", JSON.stringify(keep)); } catch (e) { /* private mode */ }
    closeExport();
    toast(`Exporting: ${j.summary || "full quality"} – it appears in Exports (tab 5) when done.`);
    refresh(true);
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; }
};

/* ---------------- player ---------------- */
const player = $("#player");
let curSrcUrl = "";
// newest render of EITHER kind: after "Export final" the player must show that, not an older preview
// …but only an export that LOOKS like the edit: a 16:9 / 4:5 / other-fps export, an MP3 or a ProRes / H.265 / MKV file
// (the browser can't play those) would break the player and its scene jumps
function latestPreview() { const r = (P?.renders || []).filter((x) => !x.missing && (x.kind !== "final" || (x.matches_edit !== false && x.web !== false && !x.audio_only))); return r.length ? r[r.length - 1] : null; }
function renderUrl(r) { return r.kind === "final" ? media("exports", r.file) : media("work", r.file); }
function previewOffset(id) {
  const m = renderTimes(false); if (m && m.has(id)) return m.get(id)[0];
  let t = 0; for (const s of P.scenes) { if (s.id === id) return t; if (inVid(s) && !s.src_skipped) t += s.est; } return 0;
}
function loadPlayer(play, keepTime = null) {
  const s = curScene(); if (!s) return;
  let url, start;
  if (playerMode === "preview") {
    const pv = latestPreview();
    if (!pv) { $("#playerInfo").textContent = "Not made yet — click ▶ Edited view (above the player)."; return; }
    url = renderUrl(pv); start = inVid(s) ? previewOffset(s.id) : 0;
    const stale = pv.sig && P.edit_sig && pv.sig !== P.edit_sig;
    $("#playerInfo").innerHTML = `${pv.kind === "final" ? "Final export" : "Preview"} ${esc(pv.file)} · ${fmt(pv.duration)} · ${pv.created.slice(11, 16)}${s.keep ? "" : " · this scene is cut"}` +
      (stale || !pv.sig ? ` · <span class="stale" data-tip="Scenes, clips or settings changed after this render – this video does not show them yet.">⚠ ${AUTO.on ? "edited – updating…" : "has changes not shown yet – click ▶ Edited view"}</span>` : "");
    setRate(1, false);
  } else {
    const src = P.sources.find((x) => x.file === s.src);
    url = src && src.proxy ? media("proxy", src.proxy) : media("raw", s.src); start = s.in;
    setRate(s.speed || 1, !!s.mute);
    $("#playerInfo").textContent = `Original footage · plays at the scene's speed ${s.speed || 1}×${s.mute ? " · muted" : ""} · captions, zoom and cards show in “Edited view”`;
  }
  const rate = player.defaultPlaybackRate;
  const go = () => { player.playbackRate = rate; player.currentTime = keepTime != null ? Math.min(keepTime, Math.max(0, (player.duration || 1e9) - 0.1)) : start; if (play) player.play().catch(() => {}); };
  if (curSrcUrl !== url) { curSrcUrl = url; player.src = url; player.addEventListener("loadedmetadata", go, { once: true }); }
  else go();
}
let lastT = 0;
player.addEventListener("seeking", () => { lastT = player.currentTime; });
player.addEventListener("timeupdate", () => {
  const t = player.currentTime, prev = lastT; lastT = t;
  if (playerMode !== "source" || player.paused) return;
  const s = curScene(); if (!s) return;
  if (prev < s.out - 0.04 && t >= s.out - 0.04 && t - prev < 1) {
    if ($("#loopScene").checked) player.currentTime = s.in; else player.pause();
  }
});
let lastRenderFile = null;  // null = page just loaded (don't auto-switch on the first poll)
function setRate(r, muted) { player.defaultPlaybackRate = r; player.playbackRate = r; player.muted = muted; }
// a speed / mute change on the selected scene applies to the Original-footage player at once (no reload)
function syncSourcePlayer() {
  if (playerMode !== "source") return;
  const s = curScene(); if (!s) return;
  if (player.playbackRate !== (s.speed || 1) || player.muted !== !!s.mute) {
    setRate(s.speed || 1, !!s.mute);
    $("#playerInfo").textContent = `Original footage · plays at the scene's speed ${s.speed || 1}×${s.mute ? " · muted" : ""} · captions, zoom and cards show in “Edited view”`;
  }
}
/* Auto-update preview: every edit changes P.edit_sig; when it differs from the newest render, render again
   (only changed scenes are re-encoded, so a speed/caption change takes seconds). Debounced so a burst of clicks is
   ONE render; waits while any job runs or a drag is in progress; never retries the same edit twice (a failing
   render must not loop). The server also drops a second waiting preview. */
const AUTO = { on: (() => { try { return localStorage.getItem("cve_autoprev") !== "0"; } catch (e) { return true; } })(),
  timer: null, timerSig: null, lastReq: null };
function autoPreviewTick() {
  const pv = P && latestPreview(), sig = P && P.edit_sig;
  const want = AUTO.on && PID && sig && P.scenes.some((x) => inVid(x) && !x.src_skipped) && P.sources.some((x) => x.analyzed)
    && !(pv && pv.sig === sig) && AUTO.lastReq !== sig;
  if (!want) { clearTimeout(AUTO.timer); AUTO.timerSig = null; return; }
  const j = P.job || {};
  if (j.status === "running" || j.status === "queued" || STL.drag || STL.busy) return;  // checked again in 1 s
  if (AUTO.timerSig === sig) return;
  clearTimeout(AUTO.timer); AUTO.timerSig = sig;
  AUTO.timer = setTimeout(() => {
    AUTO.timerSig = null;
    const jj = P.job || {};
    if (P.edit_sig !== sig || jj.status === "running" || jj.status === "queued") return;
    AUTO.lastReq = sig;
    api(`/api/projects/${PID}/render`, { method: "POST", body: { kind: "preview" } }).then(() => refresh(true)).catch(() => {});
  }, 600);
}
setInterval(() => { if (P) { autoPreviewTick(); syncSourcePlayer(); renderEditedState(); renderPvBadge(); } }, 500);
function reloadPreview() {  // new render while watching "Edited video"
  const playing = !player.paused;
  curSrcUrl = ""; loadPlayer(playing, playing ? player.currentTime : null);
}
function showRendered() {
  playerMode = "preview";
  $$(".player-bar .seg button").forEach((x) => x.classList.toggle("active", x.dataset.mode === "preview"));
  curSrcUrl = ""; loadPlayer(false);
}
$$(".player-bar .seg button").forEach((b) => (b.onclick = () => {
  playerMode = b.dataset.mode; $$(".player-bar .seg button").forEach((x) => x.classList.toggle("active", x === b)); curSrcUrl = ""; loadPlayer(true);
}));
{
  const cb = $("#autoPrev");
  if (cb) { cb.checked = AUTO.on; cb.onchange = () => { AUTO.on = cb.checked; AUTO.lastReq = null; try { localStorage.setItem("cve_autoprev", cb.checked ? "1" : "0"); } catch (e) { /* private mode */ } autoPreviewTick(); loadPlayer(false); }; }
}
$("#splitHere").onclick = () => {
  const s = curScene(); if (!s) return;
  if (playerMode !== "source") return toast("Click “Original footage” under the player first – splits are made on the original clip.", true);
  sendCommand(s.id, `split at ${(player.currentTime - s.in).toFixed(2)}`);
};

/* ---------------- exports ---------------- */
function renderRenders() {
  const el = $("#renderList");
  const rs = [...(P.renders || [])].reverse();
  if (!rs.length) { el.innerHTML = `<p class="muted">Nothing rendered yet.</p>`; return; }
  const latestPv = latestPreview();
  const mbTxt = (b) => !b ? "" : b >= 2 ** 30 ? ` · ${(b / 2 ** 30).toFixed(2)} GB` : ` · ${Math.max(0.1, b / 2 ** 20).toFixed(b < 10 * 2 ** 20 ? 1 : 0)} MB`;
  el.innerHTML = rs.filter((r) => r.kind === "final" || r === latestPv).map((r) => {
    const area = r.kind === "final" ? "exports" : "work", url = media(area, r.file);
    const body = r.missing ? `<div class="muted small" style="padding:20px">This file is no longer in the ${r.kind === "final" ? "Exports" : "work"} folder (moved, renamed or deleted).</div>`
      : r.audio_only ? `<div class="xp-audio"><div class="muted small">🎧 Audio only</div><audio src="${url}" controls preload="none"></audio></div>`
      : r.web === false ? `<div class="xp-noplay muted small">▶ This file type doesn't play in the browser – open it in VLC, QuickTime, Premiere or your phone.<br><button class="btn ghost small" data-openx="1">📂 Show in Exports folder</button></div>`
      : `<video src="${url}" controls preload="metadata"></video>`;
    return `<div class="render">${body}
      <div class="meta"><div><b>${esc(r.file)}</b><div class="muted">${r.kind === "final" ? (r.summary ? esc(r.summary) : "Final export") : "Latest preview"}${r.summary ? "" : ` · ${esc(r.size)}`} · ${fmt(r.duration)}${mbTxt(r.bytes)} · ${esc(r.created.replace("T", " ").slice(0, 16))}</div></div>
      <div class="xp-acts">${r.kind === "final" && !r.missing ? `<a class="btn ghost small" href="${url}?dl=1" download="${esc(r.file)}" title="Save a copy (Downloads folder)">⬇ Download</a>` : ""}
      <span class="badge ${r.kind === "final" ? "ok" : ""}">${r.kind}</span>
      ${r.kind === "final" && r.sig ? (r.sig === P.edit_sig ? `<span class="badge ok" title="This export shows your edit exactly as it is now">✓ current edit</span>`
        : `<span class="badge warn" title="You changed the edit after this export – click Export final for a new one">edited since</span>`) : ""}</div></div>
    </div>`; }).join("");
  $$("[data-openx]", el).forEach((b) => (b.onclick = () => api(`/api/projects/${PID}/open`, { method: "POST", body: { which: "exports" } })));
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
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || !e.data || e.data.cve !== "storyboard-done") return;
  if (e.data.pid && e.data.pid !== PID && [...$("#projectSelect").options].some((o) => o.value === e.data.pid)) {
    $("#projectSelect").value = e.data.pid; $("#projectSelect").onchange({ target: $("#projectSelect") });
  }
  switchTab("scenes");
  refresh(true).then(() => { if (typeof autoPreviewTick === "function") autoPreviewTick(); });
  if (e.data.msg) toast(`${e.data.msg} (Undo takes it back)`);
});
const hashTab = location.hash.slice(1);
// ?widthdebug: logs the elements that make the page wider than the window (layout audits at phone width)
if (location.search.includes("widthdebug")) setTimeout(() => {
  const W = document.documentElement.clientWidth, bad = [];
  for (const el of document.querySelectorAll("body *")) {
    const r = el.getBoundingClientRect();
    if (r.width && r.right > W + 1 && el.offsetParent !== null && ![...el.children].some((c) => c.getBoundingClientRect().right > W + 1))
      bad.push(`[${(el.textContent || "").trim().slice(0, 40)}] ${el.parentElement ? el.parentElement.tagName.toLowerCase() + (el.parentElement.className ? "." + String(el.parentElement.className).split(" ")[0] : "") + ">" : ""}${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : ""} right=${Math.round(r.right)} w=${Math.round(r.width)}`);
  }
  console.log("WIDTHDEBUG " + W + " :: " + bad.slice(0, 25).join(" | "));
}, 2500);
const TABS = ["footage", "refs", "media", "scenes", "exports"];
switchTab(TABS.includes(hashTab) ? hashTab : hashTab === "export" ? "scenes" : TABS.includes(store.get("cve.tab")) ? store.get("cve.tab") : "footage");
// #export: open the Export dialog once the project is loaded (a bookmark / the layout check)
if (hashTab === "export") { const t0 = Date.now(), iv = setInterval(() => { if (P && PID) { clearInterval(iv); openExport(); } else if (Date.now() - t0 > 15000) clearInterval(iv); }, 300); }
loadBrandKit();
loadProjects().catch((e) => toast(e.message, true));
