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
  if (changed("scenes", [P.scenes, P.settings, P.storyboard, P.commands.filter((c) => c.status === "claude").map((c) => c.scene), selected])) renderScenes();
  if (changed("detail", [curScene(), P.commands.length, P.commands.map((c) => c.status).join(), P.settings.layout,
    P.settings.remove_silence, P.broll_clips, P.sources.map((s) => s.file)])) renderDetail();
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
    toast(r.already ? "Already in Claude's inbox." : "Sent to Claude. Say “process studio inbox” in VS Code."); refresh(true);
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
  $("#lookPickInfo").textContent = `${v.format || ""} · ${v.layout === "split" ? "split layout" : "full screen"} · captions ${v.captions === false ? "off" : v.caption_script === "hinglish" ? "Hinglish" : "on"}${v.music && v.music !== "off" ? " · music" : ""}`;
}
async function switchLook(id) {
  if ((P.style_source || "none") === id) return;
  try {
    const r = await api(`/api/projects/${PID}/style_source`, { method: "POST", body: { source: id } });
    lastSig.refs = null; await refresh(true);
    toast(`The video now uses ${lookName(id)}.${r.notes && r.notes.length ? " " + r.notes.join(", ") + "." : ""} Click ▶ Edited view to see it. (Undo reverts.)`);
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
  bg_mode: ["Background", "What to do with a green screen behind you.",
    "If you film in front of a green cloth, the studio can swap the green for a clean off-white wall so it looks like a real studio. If there's no green behind you, nothing changes.",
    "Replace → green becomes a clean off-white wall.\nKeep as filmed → stays green.", "Replace"],
  face_y: ["Face position", "Moves you up or down in the bottom half.",
    "In the split layout you appear in the bottom half. This number tells the studio where your eyes are in your clip, so it can frame you nicely – not too high, not too low.",
    "Head cut off at the top? → make the number bigger (0.30).\nToo much space above your head? → smaller (0.22).", "0.27"],
  music: ["Music", "Soft background music under your voice.",
    "Music fills silences and adds energy. It automatically gets softer whenever you talk, so your words stay clear. Use your own song by putting it in the Brand Kit\\music folder.",
    "Quiet → music plays, and gets softer whenever you talk.\nOff → only your voice.", "Quiet"],
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
    lab.insertBefore(row, f);
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
  "bg_mode", "bg_color", "panel_color", "grade", "audio_polish", "music", "loudness", "duck_db"]);
function renderBrief(force) {
  const ed = curEditLook(), nm = lookName(ed);
  $("#lookBriefTag").textContent = nm;
  $("#lookBriefLabel").innerHTML = `Brief for <b>${esc(nm)}</b> <span class="muted small">– how this look should feel (each look has its own)</span>`;
  if (!briefDirty || force) {
    $("#brief").value = P.brief || "";
    $("#lookBrief").value = (P.look_briefs || {})[ed] || "";
    const look = (P.looks_view || {})[curEditLook()] || P.settings;  // style keys come from the look being edited
    for (const el of $$("#settingsForm [name]")) {
      const v = STYLE_KEYS.has(el.name) && el.name in look ? look[el.name] : P.settings[el.name];
      const sv = v === null || v === undefined ? "" : String(v);
      // a value the dropdown doesn't list (e.g. music = "mytrack.mp3" set by a command) must survive a Save:
      // otherwise the select shows blank and saving silently changes the setting
      if (el.tagName === "SELECT" && sv && ![...el.options].some((o) => o.value === sv)) el.add(new Option(sv, sv));
      el.value = sv;
    }
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
  b.textContent = pend ? "✨ Plan requested – say “process studio inbox”" : "✨ Claude: plan the edit";
  b.title = pend ? "Waiting for Claude: type “process studio inbox” in VS Code. (Cancel it in the Claude inbox.)"
    : analyzing ? "Wait for the analysis to finish – the scenes are about to change"
    : !P.scenes.length ? "Analyze your footage first (Footage tab)"
    : "Claude plans the whole edit beat by beat (cuts, cards, zooms) as a storyboard – you approve before anything renders";
}
$("#planBtn").onclick = async () => {
  try {
    if (briefDirty) await saveBrief();  // unsaved brief on page 2 belongs in the plan
    const f = P.footage || {};
    if (f.new && !confirm(`${f.new} clip(s) are not analyzed yet – their scenes won't be in the plan.\n\nPlan the edit anyway?`)) return;
    if (P.storyboard && P.storyboard.status !== "applied" &&
        !confirm("A storyboard is already waiting for your review.\nMake a new plan? (Your reviews of beats that don't change are kept.)")) return;
    const r = await api(`/api/projects/${PID}/first_cut`, { method: "POST" });
    if (r.already) toast("Already requested – say “process studio inbox” in VS Code.");
    else toast("Sent to Claude. Say “process studio inbox” in VS Code – a storyboard will appear for you to approve." +
      (!(P.brief || "").trim() ? " Tip: fill “About this video” on page 2 for a better plan." : ""));
    refresh(true);
  } catch (e) { toast(e.message, true); }
};

/* ---------------- scenes ---------------- */
function sceneBadges(s) {
  const b = [];
  if (s.role === "broll") b.push(`<span class="badge broll" title="B-roll footage: covers speaker scenes, not played on its own">B-ROLL</span>`);
  if (s.broll_view) b.push(`<span class="badge ${s.broll_view.ok ? "broll" : "warn"}" title="${esc(s.broll_view.src)}">🎞 ${s.broll_view.where === "full" ? "full" : "top"}</span>`);
  if (!s.keep) b.push(`<span class="badge cut">CUT</span>`);
  if (s.speed && s.speed !== 1) b.push(`<span class="badge acc">${s.speed}x</span>`);
  if (s.mute) b.push(`<span class="badge">muted</span>`);
  else if (s.volume !== undefined && s.volume !== 1) b.push(`<span class="badge">vol ${Math.round(s.volume * 100)}%</span>`);
  if (s.zoom && s.zoom > 1) b.push(`<span class="badge acc">zoom ${s.zoom}x</span>`);
  if ((s.punches || []).length) b.push(`<span class="badge acc" title="Zoom moments (punch-ins) inside this scene">🔍 ${s.punches.length}</span>`);
  if (s.text) b.push(`<span class="badge acc">text</span>`);
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
  cappos: { auto: "Use the look's position. Split layout: on the line between the card and you, just above your head.",
    head: "Captions high – over your head (split layout: just under the line, on your forehead/hair). Use when something important is low.",
    center: "Captions in the middle of the picture – most eye-catching, but can cover your face.",
    below: "Captions low – near the bottom of the screen, under your chest. Keeps your face clear." },
  pauses: { auto: "Follow the whole-video setting for cutting pauses (shown on the right).",
    cut: "Cut the silent pauses out of this scene – jump cuts, faster pace.", keep: "Keep every pause in this scene – natural, calm pace." },
  bwhere: { top: "Show the B-roll in the top panel (card area); you stay visible below.", full: "Show the B-roll over the whole screen; your voice keeps playing." },
  role: { speaker: "Normal scene: plays in the video with its picture and sound.",
    broll: "Take this scene OUT of the video and use its picture only as B-roll over other scenes. Its own sound is not heard." },
};
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
    const c = s.card;
    const cardKind = !c ? "auto" : c.kind;
    const words = (s.caption_view ?? s.transcript ?? "").split(/\s+/).filter(Boolean);
    const capsOn = words.length > 0 && (s.captions === true || (s.captions == null && P.settings.captions !== false));
    el.innerHTML = head + `
      <div class="isec bsec"><div class="ilab hastip" data-tip="Show another clip (market footage, charts…) OVER this scene while your voice keeps playing.">🎞 Cover with B-roll <span class="muted small">(footage shows over this scene, your voice keeps playing)</span></div>
        <div class="irow">
          <select id="brollSel" data-tip="Pick the clip that covers this scene. None = show yourself."><option value="">None</option>${clipOpts.map((f) => `<option ${bv && bv.src === f ? "selected" : ""}>${esc(f)}</option>`).join("")}</select>
          ${bv ? segBtns("bwhere", [["top", "Top panel", split ? "" : "Needs the split layout"], ["full", "Full screen"]], bv.where) : ""}
        </div>
        ${bv ? `<div class="irow"><span class="muted small">Starts in the clip at</span>
          <input type="number" id="brollAt" step="0.5" min="0" placeholder="auto" value="${bv.at ?? ""}" title="Empty = continue from the previous scene's B-roll">
          <span class="muted small">s ${bv.at == null ? "(auto: continues)" : `<button class="lnk" data-cmd="broll continue">auto</button>`}</span></div>` : ""}
        ${bv && !bv.ok ? `<div class="warnline">⚠ Not shown: ${esc(bv.why)}.</div>` : ""}
        ${!clipOpts.length ? `<div class="muted small">No other clips. Add another clip in Footage (e.g. market footage) to show it over this scene.</div>` : ""}</div>
      <div class="isec"><div class="ilab hastip" data-tip="What you say in this scene. Click a word to change how the captions spell it, or to hide it.">Words <span class="muted small">(click a word to fix how it's written)</span></div>
        ${s.heard ? `<div class="ibox">🎵 <b>No speech detected</b> – music or background sound, so captions stay off here.
            <button class="lnk" id="showHeard" data-tip="Speech recognition always 'hears' something in music. These are its guesses, not real words.">Show the ${s.heard.split(/\s+/).length} guessed word(s)</button>
            <div id="heardBox" class="muted small" hidden>“${esc(s.heard)}” – not real speech. To show captions anyway: Captions → On (then fix or hide the words here).</div></div>` : ""}
        ${s.no_speech && s.captions === true && words.length ? `<div class="warnline">⚠ No real speech was detected here – these words are guesses and WILL appear as captions. Fix or hide them, or set Captions to Auto.</div>` : ""}
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
      <div class="irow ${capsOn ? "" : "dim"}">${lab("Position", "Where the captions sit in THIS scene. Auto = the look's position.")}${segBtns("cappos", [["auto", "Auto"], ["head", "Head"], ["center", "Center"], ["below", "Below"]], s.cap_pos || "auto")}
        <span class="muted small">${capsOn ? `Auto = ${split ? "on the seam" : { bottom: "below", center: "center", top: "head" }[P.settings.caption_position || "bottom"]}`
          : `captions are OFF here – ${!words.length ? "no speech in this scene" : "turn them On to see this"}`}</span></div>
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
      bwhere: `broll ${v}` }[n];
    if (cmd) sendCommand(s.id, cmd);
  })));
  const trimSet = (edge, x) => { x = Math.max(0, r2(x)); sendCommand(s.id, `trim ${edge} to ${x}`); };
  $$("[data-nud]", el).forEach((b) => (b.onclick = () => { const inp = b.dataset.nud === "start" ? $("#trimStart") : $("#trimEnd"); trimSet(b.dataset.nud, +inp.value + +b.dataset.d); }));
  [["#trimStart", "start"], ["#trimEnd", "end"]].forEach(([id, edge]) => { const i = $(id); if (i) i.onchange = () => trimSet(edge, +i.value); });
  const vol = $("#volRange");
  if (vol) { vol.oninput = () => ($("#volVal").textContent = `${vol.value}%`); vol.onchange = () => sendCommand(s.id, `volume ${vol.value}%`); }
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
  $$(".wd", el).forEach((b) => (b.onclick = () => {
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
    [...bin.map((c) => clipChip(c.file, true)), ...others.map((f) => clipChip(f, false))].join(" ") : "";
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
  const W = Math.max(300, box.clientWidth - 70), STUB = 30, GAP = 2;
  const kept = main.filter(inVid), total = kept.reduce((a, s) => a + s.est, 0) || 1;
  const stubs = main.length - kept.length;
  const pps = Math.max(1, (W - stubs * STUB - main.length * GAP) / total);
  let x = 0; STL.pps = pps;
  STL.blocks = main.map((s) => { const w = inVid(s) ? Math.max(6, s.est * pps) : STUB; const b = { s, x, w }; x += w + GAP; return b; });
  const cardsEff = effCards(main);
  const thumb = (f) => { const c = bin.find((b) => b.file === f); const t = c ? c.thumb : (P.scenes.find((x) => x.src === f) || {}).thumb; return t ? `background-image:url('${media("thumbs", t)}')` : ""; };
  // merge runs: B-roll by clip, cards by content
  const runs = (key) => { const out = []; STL.blocks.forEach((b, i) => { const k = key(b, i); if (!k) return; const last = out[out.length - 1];
    if (last && last.k === k && last.end === i - 1) { last.w = b.x + b.w - last.x; last.end = i; } else out.push({ k, x: b.x, w: b.w, start: i, end: i, b }); }); return out; };
  const bRuns = runs((b) => inVid(b.s) && b.s.broll_view ? JSON.stringify([b.s.broll_view.src, b.s.broll_view.where, b.s.broll_view.ok, b.s.broll.where || ""]) : "");
  STL.bRuns = bRuns;
  const cRuns = runs((b, i) => inVid(b.s) && cardsEff[i] && cardsEff[i].kind !== "none" && !(b.s.broll_view && b.s.broll_view.where === "full") ? JSON.stringify(cardsEff[i]) : "");
  const step = tickStep(pps);
  let ticks = "";
  for (let t = 0; t <= total + 0.01; t += step) ticks += `<span class="tick" style="left:${tlX(t)}px">${tc(t)}</span>`;
  box.innerHTML = `
    <div class="stl-heads"><div class="h ruler"></div><div class="h hastip" data-tip="Footage shown OVER the scenes below while your voice plays. Optional – drag a clip chip onto a scene to add one.">B-roll</div><div class="h main hastip" data-tip="Your scenes in playing order. Drag to reorder, drag an edge to trim, striped = cut.">Video</div>${cardsEff.length ? `<div class="h hastip" data-tip="The top-panel card (title / number / chart) shown during each scene – split layout.">Cards</div>` : ""}</div>
    <div class="stl-lanes" style="width:${x}px">
      <div class="lane ruler" data-tip="Time in the edited video. Click to jump there.">${ticks}</div>
      <div class="lane broll">${bRuns.map((r, ri) => { const v = JSON.parse(r.k); return `<div class="bblk ${v[2] ? "" : "bad"}" data-run="${ri}" style="left:${r.x}px;width:${r.w}px;${thumb(v[0])}" title="${esc(v[0])} – ${v[1] === "full" ? "full screen" : "top panel"}${v[2] ? "" : " – NOT SHOWN (skipped or missing)"}
Drag = move to other scenes · drag an edge = cover more / fewer scenes · × = remove"><i class="hd l"></i><span>🎞 ${esc(v[0])} · ${v[1] === "full" ? "full" : "top"}</span><b class="bx" title="Remove this B-roll">×</b><i class="hd r"></i></div>`; }).join("")}</div>
      <div class="lane main">${STL.blocks.map((b) => `<div class="sblk ${isBroll(b.s) ? "cut brs" : b.s.keep ? "" : "cut"} ${b.s.id === selected ? "sel" : ""}" data-id="${b.s.id}" style="left:${b.x}px;width:${b.w}px;${inVid(b.s) ? `background-image:url('${media("thumbs", b.s.thumb)}')` : ""}" title="Scene ${b.s.no} · ${isBroll(b.s) ? "B-roll footage, not played – drag it to a place in the video to play it there" : b.s.keep ? `${b.s.est}s` : "cut – click to select, Del to restore"}">
        ${isBroll(b.s) ? `${b.s.no}🎞` : b.s.keep ? `<i class="hd l"></i><span class="no">${b.s.no}</span>${b.w > 54 ? `<span class="du">${b.s.est}s</span>` : ""}<i class="hd r"></i>` : `${b.s.no}✂${b.s.broll_view ? "🎞" : ""}`}</div>`).join("")}</div>
      ${cardsEff.length ? `<div class="lane cards">${cRuns.map((r) => `<div class="cblk" style="left:${r.x}px;width:${r.w}px" title="${esc(cardLabel(JSON.parse(r.k)))}">${esc(cardLabel(JSON.parse(r.k)))}</div>`).join("")}</div>` : ""}
      <div class="stl-head" id="stlHead"></div>
    </div>`;
  const nCut = main.length - kept.length, nBr = P.scenes.filter(isBroll).length, nSk = P.scenes.filter((x) => x.src_skipped && !isBroll(x)).length;
  $("#stlInfo").textContent = `${P.scenes.length} scenes: ${kept.length} in the video (≈ ${fmt(total)})` +
    (nCut ? ` · ${nCut} cut` : "") + (nBr ? ` · ${nBr} B-roll footage` : "") + (nSk ? ` · ${nSk} in skipped clips` : "") +
    (bRuns.length ? ` · ${bRuns.length} cutaway${bRuns.length > 1 ? "s" : ""}` : "");
  $$(".bblk", box).forEach((b) => b.addEventListener("pointerdown", bDown));
  $(".lane.ruler", box).onclick = (e) => seekEdit((e.clientX - $(".stl-lanes", box).getBoundingClientRect().left));
  $$(".sblk", box).forEach((el) => el.addEventListener("pointerdown", stlDown));
  drawStlHead();
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
  const edge = e.target.classList.contains("hd") ? (e.target.classList.contains("l") ? "l" : "r") : null;
  STL.drag = { id, b, el, edge, x0: e.clientX, moved: false };
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
    // dragged UP onto the B-roll lane = make it B-roll footage (handled on release)
    const over = document.elementFromPoint(e.clientX, e.clientY);
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
  try {
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
  if (e.button !== 0) return;
  const el = e.currentTarget, run = STL.bRuns[+el.dataset.run]; if (!run) return;
  const runIdx = []; for (let i = run.start; i <= run.end; i++) runIdx.push(i);
  const first = STL.blocks[run.start].s, raw = first.broll || {};
  const entry = (withAt) => ({ src: raw.src, ...(raw.where ? { where: raw.where } : {}), ...(withAt && raw.at != null ? { at: raw.at } : {}) });
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
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const s = curScene();
  if (e.code === "Space") { if (e.target.tagName === "VIDEO") return;  // the player's own controls already toggle it
    e.preventDefault(); player.paused ? player.play().catch(() => {}) : player.pause(); }
  else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    e.preventDefault();
    const i = P.scenes.findIndex((x) => x.id === selected), j = i + (e.key === "ArrowRight" ? 1 : -1);
    if (P.scenes[j]) selectScene(P.scenes[j].id);
  }
  else if (e.key === "s" || e.key === "S") $("#splitHere").click();
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
async function sendCommand(scene, text) {
  if (!text.trim()) return;
  try {
    const r = await api(`/api/projects/${PID}/command`, { method: "POST", body: { scene, text } });
    const msg = [...r.replies.map((x) => x.replace(/^!/, "")), ...(r.queued.length ? [`Sent to Claude: “${r.queued.join("; ")}”`] : [])].join("  ·  ");
    toast(msg, r.replies.some((x) => x.startsWith("!")));
    if (r.replies.length && !r.replies.every((x) => x.startsWith("!"))) {
      // timeline edits (trim / split / cut) are often made watching the ORIGINAL footage – don't switch away then;
      // a look change (captions, zoom, card, text, speed…) is only visible in the edited view – go there when ready
      const timelineEdit = /^(trim|split|reset trim|untrim|cut|keep|restore|delete|remove)\b/i.test(text.trim()) && !/^remove (text|silences?|punch)/i.test(text.trim());
      SHOW = { sid: scene, switchView: !timelineEdit, at: Date.now() };
    }
    await refresh(true); loadPlayer(false); autoPreviewTick(); renderPvBadge();
  } catch (e) { toast(e.message, true); }
}
$("#sceneSend").onclick = () => { const s = curScene(); if (!s) return toast("Select a scene first.", true); sendCommand(s.id, $("#sceneCmd").value).then(() => ($("#sceneCmd").value = "")); };
$("#sceneCmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#sceneSend").click(); });
$("#globalSend").onclick = () => sendCommand(null, $("#globalCmd").value).then(() => ($("#globalCmd").value = ""));
$("#globalCmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#globalSend").click(); });
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
$("#finalBtn").onclick = () => api(`/api/projects/${PID}/render`, { method: "POST", body: { kind: "final" } }).then(() => toast("Exporting full quality to the Exports folder…")).catch((e) => toast(e.message, true));

/* ---------------- player ---------------- */
const player = $("#player");
let curSrcUrl = "";
// newest render of EITHER kind: after "Export final" the player must show that, not an older preview
function latestPreview() { const r = (P?.renders || []).filter((x) => !x.missing); return r.length ? r[r.length - 1] : null; }
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
  el.innerHTML = rs.filter((r) => r.kind === "final" || r === latestPv).map((r) => `
    <div class="render">
      ${r.missing ? `<div class="muted small" style="padding:20px">This file is no longer in the ${r.kind === "final" ? "Exports" : "work"} folder (moved, renamed or deleted).</div>`
        : `<video src="${media(r.kind === "final" ? "exports" : "work", r.file)}" controls preload="metadata"></video>`}
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
switchTab(["footage", "refs", "scenes", "exports"].includes(hashTab) ? hashTab : store.get("cve.tab") || "footage");
loadBrandKit();
loadProjects().catch((e) => toast(e.message, true));
