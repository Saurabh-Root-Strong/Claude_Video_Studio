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
  renderNext();
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
  if (e.code === "Space") { e.preventDefault(); SEQ.playing ? seqStop() : seqPlay(false); }
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
const lookName = (id) => { if (id === "none") return "None · simple"; const i = P.references.findIndex((r) => r.id === id); return i < 0 ? "?" : `R${i + 1}`; };
function curEditLook() {
  const ids = ["none", ...P.references.map((r) => r.id)];
  if (!editLook || !ids.includes(editLook)) editLook = store.get(`cve.editlook.${PID}`) || P.style_source || "none";
  if (!ids.includes(editLook)) editLook = P.style_source || "none";
  return editLook;
}
function renderLooks() {
  const refs = P.references || [], used = P.style_source || "none", ed = curEditLook();
  const chip = (id, label, title) => `<button class="lookchip ${ed === id ? "on" : ""} ${used === id ? "used" : ""}" data-look="${esc(id)}" title="${esc(title)}${used === id ? " – used by the video" : ""}">${used === id ? "▶ " : ""}${label}</button>`;
  $("#lookBar").innerHTML = `<span class="muted small">Edit look</span>` + chip("none", "None · simple", "Your own simple settings, no reference") +
    refs.map((r, i) => chip(r.id, `R${i + 1}`, r.file || r.url)).join("");
  $$("[data-look]", $("#lookBar")).forEach((b) => (b.onclick = () => setEditLook(b.dataset.look)));
  const i = refs.findIndex((r) => r.id === ed), r = refs[i];
  $("#lookTitle").innerHTML = `· editing ${r ? `look <b>R${i + 1}</b> (${esc((r.file || r.url || "").slice(0, 40))})` : "<b>your simple settings</b>"} ` +
    (ed === used ? `<span class="badge ok">▶ used by the video</span>` : `<span class="badge">not used by the video – choose it in Scenes → Look</span>`);
  const note = $("#lookNote");
  if (r) {
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
  const opts = [["none", "None · simple settings"], ...P.references.map((r, i) => [r.id, `R${i + 1} · ${(r.file || r.url || "").slice(0, 42)}`])];
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
    toast(`The video now uses ${lookName(id)}.${r.notes && r.notes.length ? " " + r.notes.join(", ") + "." : ""} Click Render preview to see it. (Undo reverts.)`);
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
/* ---------------- "?" help for every style setting: what it does, why/when, recommended ---------------- */
const HELP = {
  format: ["The shape of the finished video.",
    "Match where you post: 9:16 (tall) for Instagram Reels, YouTube Shorts and WhatsApp status; 16:9 (wide) for normal YouTube; 1:1 (square) for feeds. 'Original' keeps your camera's shape.",
    "9:16 for reels."],
  fit: ["What happens when your footage's shape differs from the video's shape.",
    "'Crop to fill' zooms in so there are no black bars (best for faces – you lose a little of the edges). 'Fit with bars' shows the whole frame with black bars – use it for screen recordings or charts where every part matters.",
    "Crop to fill."],
  captions: ["Burned-in subtitles on the video.",
    "Most people watch reels with the sound off – captions keep them watching. Scenes with no speech switch captions off by themselves.",
    "On."],
  caption_script: ["How Hindi speech is written in the captions.",
    "Hinglish (English letters: 'aap kaise ho') reads fastest for most Indian social media viewers. Hindi script (आप कैसे हो) suits a Hindi-reading audience. Wrong word? Type 'spell daval as double' in Scenes.",
    "Hinglish."],
  caption_position: ["Where the captions sit on screen.",
    "Bottom is classic; Center grabs attention; Top avoids covering things at the bottom. In the split layout this is ignored – captions sit on the orange line between the two halves, clear of the app's buttons.",
    "Bottom (full screen) – automatic in the split layout."],
  caption_case: ["Normal or ALL CAPITAL letters.",
    "UPPERCASE looks bold and energetic (hype / motivation reels) but is slower to read. Normal case is calmer and easier for longer sentences.",
    "Normal."],
  caption_words: ["How many words are on screen at once.",
    "Fewer words (2–3) = faster, punchier, easier on a phone. More words (5–6) = calmer, fewer caption changes. The spoken word is always highlighted.",
    "3–4."],
  caption_color: ["Colour of the caption words.",
    "White with a dark outline is readable on any background. Change it only for a brand look.",
    "White."],
  highlight_color: ["Colour of the word being spoken right now (also the line between the two panels).",
    "The moving highlight guides the eye and makes captions feel alive. Use your brand colour so every video is recognisably yours.",
    "Your brand orange #F97316."],
  caption_font: ["Font of the captions.",
    "Poppins is bundled with the studio and works for both English letters and Hindi script. Another font only works if it's installed on this PC.",
    "Poppins."],
  remove_silence: ["Automatically cut pauses (jump cuts).",
    "Removes dead air so the video feels tight and energetic – the biggest single improvement for talking-head reels. Turn it off for slow, emotional or storytelling pieces where pauses matter.",
    "Yes for reels."],
  silence_min: ["The shortest pause that gets cut, in seconds.",
    "Lower (0.3) = tighter, faster video. Higher (0.6–1.0) = more breathing room. A cut pause still keeps a short breath (~0.24 s) so it never sounds chopped.",
    "0.35."],
  language: ["The language spoken in your footage.",
    "Tells Whisper (the speech-to-text engine) what to listen for. 'Auto detect' can mistake Hindi for Urdu – choose Hindi for Hindi videos, then Re-analyze.",
    "Hindi."],
  logo: ["Your logo on top of the video (a PNG in the Brand Kit folder).",
    "Builds brand recognition and discourages reposting without credit. Keep it small – the studio sizes it automatically.",
    "Optional."],
  logo_position: ["Which corner the logo goes in.",
    "Top-right is the usual spot. Avoid bottom-right on reels – the app's like/comment buttons sit there.",
    "Top-right."],
  layout: ["How the screen is arranged.",
    "'Split' = a card or graphic on top, you below, captions on the line between – great for finance/explainer reels (the number or headline stays visible while you talk). 'Full screen' = only you, the classic talking-head look.",
    "Split (your Brand Kit)."],
  headline: ["The title card shown in the top panel when a scene has no card of its own (split layout).",
    "Put the main hook here, e.g. 'Bajaj *IPO* listing aaj' – a *word* in stars turns orange.",
    "Your hook, 2–5 words."],
  bg_mode: ["What happens to a green-screen background (split layout).",
    "'Replace green screen' swaps the green for a clean warm off-white so it looks like a studio. 'Keep as filmed' leaves it. It only acts when the clip really has a green background.",
    "Replace."],
  face_y: ["Where your eyes are in the camera frame (0 = top, 1 = bottom) – used to frame you in the split layout.",
    "Raise it if your head is cut off at the top, lower it if there's too much empty space above you. Or type 'face up' / 'face down' in Scenes.",
    "0.27 (eyes in the upper third)."],
  music: ["Quiet background music under your voice.",
    "Fills silences and adds energy; it automatically gets quieter while you speak. Put your own track in Brand Kit\\music – otherwise a soft generated tune plays. Use only music you have the rights to.",
    "Quiet, if it suits the mood."],
  audio_polish: ["Cleans and levels your voice.",
    "Removes low rumble and light background noise, evens out loud and soft words, and sets a consistent loudness – every video sounds equally loud and professional.",
    "Clean voice + loudness."],
  loudness: ["How loud your speech is in the final video, in LUFS (Loudness Units relative to Full Scale – the standard volume measure).",
    "-14 LUFS matches the loudest uploads on Reels/Shorts, so you don't sound quieter than the next video. -16 is the common standard; -18 is calm. Closer to 0 = louder.",
    "-14 for reels."],
  duck_db: ["How much quieter the music gets while you speak, in dB (decibels).",
    "A bigger number drops the music further under your voice, so words stay crystal clear. 18 dB is clear without the music disappearing; 0 = the music never dips.",
    "18."],
};
let helpOpen = null;
function setupHelp() {
  for (const lab of $$("#settingsForm label")) {
    const f = lab.querySelector("[name]"); if (!f || !HELP[f.name] || lab.querySelector(".qhelp")) continue;
    const b = document.createElement("button");
    b.type = "button"; b.className = "qhelp"; b.textContent = "?"; b.dataset.k = f.name;
    b.title = HELP[f.name][0];
    b.setAttribute("aria-label", `What is ${f.name.replace(/_/g, " ")}?`);
    lab.insertBefore(b, f);
    b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); helpOpen && helpOpen.btn === b ? closeHelp() : showHelp(b); };
  }
}
function showHelp(btn) {
  closeHelp();
  const k = btn.dataset.k, [what, why, rec] = HELP[k];
  const shared = !STYLE_KEYS.has(k);
  const pop = document.createElement("div");
  pop.className = "helppop"; pop.setAttribute("role", "dialog");
  pop.innerHTML = `<button class="hp-x" aria-label="Close">×</button>
    <div class="hp-what">${esc(what)}</div>
    <div class="hp-sec"><b>Why / when</b>${esc(why)}</div>
    <div class="hp-sec"><b>Recommended</b>${esc(rec)}</div>
    <div class="hp-tag ${shared ? "shared" : ""}">${shared ? "Same for every look – it belongs to your footage / this video" : "Part of the look – each reference can have its own value"}</div>`;
  document.body.appendChild(pop);
  const r = btn.getBoundingClientRect(), pw = Math.min(340, window.innerWidth - 24);
  pop.style.width = `${pw}px`;
  pop.style.left = `${Math.max(12, Math.min(window.innerWidth - pw - 12, r.left - 12))}px`;
  const below = r.bottom + 8 + pop.offsetHeight < window.innerHeight;
  pop.style.top = `${window.scrollY + (below ? r.bottom + 8 : Math.max(8, r.top - pop.offsetHeight - 8))}px`;
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
  $("#briefState").textContent = briefDirty ? "unsaved changes" : P.updated ? `saved` : "";
}
["#brief", "#lookBrief"].forEach((sel) => $(sel).addEventListener("input", () => { briefDirty = true; $("#briefState").textContent = "unsaved changes"; }));
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
  await api(`/api/projects/${PID}/brief`, { method: "POST", body: { brief: $("#brief").value, look_brief: $("#lookBrief").value, settings: collectSettings(), look: curEditLook() } });
  briefDirty = false; await refresh(true);
}
$("#saveBrief").onclick = () => saveBrief().then(() => toast("Saved.")).catch((e) => toast(e.message, true));
$("#brandBtn").onclick = async () => {
  try {
    const r = await api(`/api/projects/${PID}/apply_brand`, { method: "POST", body: { look: curEditLook() } });
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
        <div class="txt">${s.no_speech && s.captions === false ? `<span class="muted">(no speech – music / ambient sound)</span>` : esc(s.caption_view ?? s.transcript) || `<span class="muted">(no speech)</span>`}</div>
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
    const stale = pv.sig && P.edit_sig && pv.sig !== P.edit_sig;
    $("#playerInfo").innerHTML = `${pv.kind === "final" ? "Final export" : "Preview"} ${esc(pv.file)} · ${fmt(pv.duration)} · ${pv.created.slice(11, 16)}${s.keep ? "" : " · this scene is cut"}` +
      (stale || !pv.sig ? ` · <span class="stale" title="Scenes, clips or settings changed after this render">⚠ edited since this render – click Render preview</span>` : "");
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
