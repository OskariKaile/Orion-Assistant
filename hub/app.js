// Orion Universal Hub — read-only SPA
(() => {
  const $  = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
  const fmt = (n) => (n == null || isNaN(n)) ? "–" : n.toLocaleString();
  const fmtMs = (ms) => (ms == null) ? "–" : (ms >= 1000 ? (ms/1000).toFixed(2) + "s" : Math.round(ms) + "ms");
  const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString() : "–";
  const fmtDateTime = (iso) => iso ? new Date(iso).toLocaleString() : "–";

  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  }
  async function send(method, url, body) {
    const r = await fetch(url, {
      method, cache: "no-store",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await r.json(); } catch (_) {}
    if (!r.ok || data.ok === false) {
      console.warn(method, url, data);
    }
    return data;
  }

  // ---- tab routing --------------------------------------------------------
  function activate(tab) {
    $$(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    $$(".view").forEach(v => v.classList.toggle("hidden", v.dataset.view !== tab));
    const loader = LOADERS[tab];
    if (loader) loader();
  }
  $$(".tab").forEach(b => b.addEventListener("click", () => {
    location.hash = b.dataset.tab;
  }));
  window.addEventListener("hashchange", () => activate(location.hash.slice(1) || "dashboard"));

  // ---- live state (SSE) ---------------------------------------------------
  let _restarting = false;   // user-initiated restart: don't close the window
  function connectSSE() {
    let es;
    try { es = new EventSource("/events"); }
    catch (_) { return; }
    const dot = $("#conn-status");
    let downTimer = null;
    es.addEventListener("open",  () => {
      dot.classList.add("ok");
      if (downTimer) { clearTimeout(downTimer); downTimer = null; }
    });
    es.addEventListener("error", () => {
      dot.classList.remove("ok");
      // When Orion exits its HTTP server dies with it. EventSource then
      // sits in a permanent reconnect loop -- use that as the signal to
      // close this window. Confirm with a direct probe first so a
      // transient blip doesn't kill the hub. During a user-initiated
      // restart the restart handler polls and reloads instead.
      if (downTimer || _restarting) return;
      downTimer = setTimeout(async () => {
        downTimer = null;
        if (_restarting) return;
        try {
          const r = await fetch("/api/health", { cache: "no-store" });
          if (r.ok) return;
        } catch (_) {}
        try { window.close(); } catch (_) {}
      }, 4000);
    });
    es.addEventListener("ready", () => {});
    es.addEventListener("message", (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (!msg || !msg.event) return;
      if (msg.event === "state") setOrionState(msg.data.state);
      if (msg.event === "heard") {
        // Live: show the just-transcribed phrase on the dashboard before
        // the turn finishes. Replaced by the full row on the "turn" event.
        if (currentTab() === "dashboard") {
          const el = document.getElementById("d-last");
          if (el && msg.data && msg.data.text) {
            const t = new Date((msg.data.ts || Date.now()/1000) * 1000)
              .toLocaleTimeString();
            el.textContent = `[${t}] (in progress)\n› ${msg.data.text}`;
          }
        }
      }
      if (msg.event === "turn")  { if (currentTab() === "dashboard") loadDashboard(); }
      if (msg.event === "agenda_changed" && currentTab() === "agenda") loadAgenda();
      if (msg.event === "settings_changed" && currentTab() === "settings") loadSettings();
      if (msg.event === "memory_changed" && currentTab() === "memory") loadMemory();
      if (msg.event === "voice_state" && currentTab() === "dashboard") loadDashboard();
    });
  }
  function currentTab() { return (location.hash.slice(1) || "dashboard"); }

  function setOrionState(state) {
    // Canonical palette -- must stay in sync with orion.STATE_COLORS and
    // hud.STATES. See orion.py for the master copy.
    const map = {
      idle: "#3b82f6", wake: "#ec4899", listening: "#22c55e",
      transcribing: "#facc15", thinking: "#f97316",
      synthesizing: "#a855f7", speaking: "#06b6d4", muted: "#6b7280",
    };
    const dot = $("#state-dot");
    dot.style.background = map[state] || "#3b82f6";
    dot.style.boxShadow  = `0 0 10px ${map[state] || "#3b82f6"}`;
    $("#state-label").textContent = state || "ready";
  }

  // ---- DASHBOARD ----------------------------------------------------------
  async function loadDashboard() {
    try {
      const d = await getJSON("/api/dashboard");
      $("#d-turns").textContent   = fmt(d.turns_today);
      $("#d-latency").textContent = fmtMs(d.avg_latency_ms);
      $("#d-active").textContent  = fmtMs(d.avg_active_ms);
      $("#d-core").textContent    = fmtMs(d.avg_core_ms);
      $("#d-tokens").textContent  = `${fmt(d.tokens_in_today)} / ${fmt(d.tokens_out_today)}`;
      $("#d-voice").textContent   = (d.voice_enrolled ? "" : "(not enrolled) ") + (d.voice_state || "—");
      $("#d-model").textContent   = d.model || "—";
      $("#d-uptime").textContent  = humanDuration(d.uptime_seconds);
      const last = d.last_turn;
      $("#d-last").textContent = last
        ? `[${last.iso || ""}] ${last.category || ""}\n› ${last.query || ""}\n‹ ${last.reply || ""}`
        : "No turns yet.";
      setOrionState(d.voice_state === "locked" ? "muted" : "idle");
    } catch (e) { console.warn(e); }
  }
  function humanDuration(s) {
    if (!s) return "–";
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h}h ${m}m` : `${m}m`;
  }

  // Dashboard quick action pills
  let _muted = false, _locked = false;
  $("#d-mute")?.addEventListener("click", async () => {
    _muted = !_muted;
    await send("POST", "/api/actions/mute", { on: _muted });
    $("#d-mute").textContent = _muted ? "Unmute" : "Mute";
    $("#d-mute").classList.toggle("active", _muted);
  });
  $("#d-lock")?.addEventListener("click", async () => {
    _locked = !_locked;
    await send("POST", "/api/actions/voice_lock", { locked: _locked });
    $("#d-lock").textContent = _locked ? "Unlock voice" : "Lock voice";
    $("#d-lock").classList.toggle("active", _locked);
    loadDashboard();
  });
  $("#d-new-chat")?.addEventListener("click", async () => {
    if (!confirm("Start a new conversation? Orion will forget this session's context.")) return;
    await send("POST", "/api/actions/new_chat");
  });
  $("#d-restart")?.addEventListener("click", async () => {
    if (!confirm("Restart Orion? It saves the session and is back in a few seconds.")) return;
    const btn = $("#d-restart");
    _restarting = true;
    btn.textContent = "Restarting…";
    btn.disabled = true;
    await send("POST", "/api/actions/restart");
    // Poll until the new instance's server is up, then reload this page.
    const t0 = Date.now();
    const poll = setInterval(async () => {
      if (Date.now() - t0 > 90000) {   // give up after 90s
        clearInterval(poll);
        _restarting = false;
        btn.textContent = "Restart Orion";
        btn.disabled = false;
        return;
      }
      try {
        const r = await fetch("/api/health", { cache: "no-store" });
        if (r.ok) { clearInterval(poll); location.reload(); }
      } catch (_) { /* still down -- keep polling */ }
    }, 1500);
  });

  // ---- TELEMETRY ----------------------------------------------------------
  let chartLatency, chartCats, chartToks, chartMood;
  let _turnsCache = [];
  let _sortKey = "total_ms", _sortDir = "desc";
  const PALETTE = ["#7fb1ff","#f3a86b","#8de29a","#e58fbf","#c9a0ff","#f0d97a","#9aa0a6"];

  function mean(xs) { return xs.length ? Math.round(xs.reduce((a,b)=>a+b,0)/xs.length) : 0; }
  function pct(xs, p) {
    if (!xs.length) return 0;
    const s = [...xs].sort((a,b)=>a-b);
    const k = (s.length - 1) * p;
    const lo = Math.floor(k), hi = Math.min(lo + 1, s.length - 1);
    return Math.round(lo === hi ? s[lo] : s[lo] + (s[hi] - s[lo]) * (k - lo));
  }
  function median(xs) { return pct(xs, 0.5); }

  // Local-time YYYY-MM-DD so it matches telemetry.iso (which is local).
  function localDay(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }
  function dayRange(rows, days) {
    if (days <= 0) {
      const set = new Set(rows.map(r => (r.iso || "").slice(0,10)).filter(Boolean));
      return [...set].sort();
    }
    const today = new Date(); today.setHours(0,0,0,0);
    const out = [];
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today); d.setDate(d.getDate() - i);
      out.push(localDay(d));
    }
    return out;
  }

  async function loadTelemetry() {
    const days = +$("#t-range").value;
    const data = await getJSON(`/api/telemetry?days=${days}`);
    const rows = data.rows || [];
    _turnsCache = rows.map(r => {
      const synth = r.tts_synth_ms || r.tts_ms || 0;
      const play  = r.tts_play_ms || 0;
      return {
        iso: r.iso || "", speaker: r.speaker || "owner",
        source: r.source || "", category: r.category || "",
        mood: r.mood || "neutral",
        query: r.query || "", reply: r.reply || "",
        total_ms: r.total_ms || 0,
        listen_ms: r.listen_ms || 0, stt_ms: r.stt_ms || 0,
        llm_ms: r.llm_ms || 0, action_ms: r.action_ms || 0,
        synth_ms: synth, play_ms: play,
        active: (r.stt_ms||0) + (r.llm_ms||0) + (r.action_ms||0) + synth,
        core:   (r.stt_ms||0) + (r.llm_ms||0) + synth,
        tokens_in: r.tokens_in || 0, tokens_out: r.tokens_out || 0,
      };
    });
    $("#t-count").textContent = `· ${rows.length} rows in range · ${data.count} total`;

    // ---- headline cards ----
    const totals = _turnsCache.map(r => r.total_ms).filter(Boolean);
    const active = _turnsCache.map(r => r.active).filter(Boolean);
    $("#t-turns").textContent = fmt(_turnsCache.length);
    $("#t-avg").textContent   = mean(totals).toLocaleString() + " ms";
    $("#t-active").textContent = mean(active).toLocaleString() + " ms";
    const core = _turnsCache.map(r => r.core).filter(Boolean);
    $("#t-core").textContent = mean(core).toLocaleString() + " ms";
    $("#t-p").textContent = `${pct(totals,0.5).toLocaleString()} / ${pct(totals,0.95).toLocaleString()} ms`;
    const tokIn  = _turnsCache.map(r => r.tokens_in).filter(Boolean);
    const tokOut = _turnsCache.map(r => r.tokens_out).filter(Boolean);
    $("#t-tok").textContent = `${mean(tokIn).toLocaleString()} in · ${mean(tokOut).toLocaleString()} out`;
    $("#t-tok-hint").textContent = `across ${tokIn.length} LLM calls`;

    // ---- stage cards ----
    $("#t-s-listen").textContent = mean(_turnsCache.map(r=>r.listen_ms)) + " ms";
    $("#t-s-stt").textContent    = mean(_turnsCache.map(r=>r.stt_ms))    + " ms";
    $("#t-s-llm").textContent    = mean(_turnsCache.map(r=>r.llm_ms))    + " ms";
    $("#t-s-action").textContent = mean(_turnsCache.map(r=>r.action_ms)) + " ms";
    $("#t-s-synth").textContent  = mean(_turnsCache.map(r=>r.synth_ms))  + " ms";
    $("#t-s-play").textContent   = mean(_turnsCache.map(r=>r.play_ms))   + " ms";

    // ---- per-day buckets ----
    const dayLabels = dayRange(_turnsCache, days || 14);
    const byDay = {};
    dayLabels.forEach(d => byDay[d] = []);
    _turnsCache.forEach(r => {
      const d = r.iso.slice(0, 10);
      if (d in byDay) byDay[d].push(r);
    });
    const dailyMedian = dayLabels.map(d => median(byDay[d].map(r=>r.total_ms).filter(Boolean)));
    const dailyP95    = dayLabels.map(d => pct(byDay[d].map(r=>r.total_ms).filter(Boolean), 0.95));
    const dailyTokIn  = dayLabels.map(d => byDay[d].reduce((s,r)=>s+(r.tokens_in||0),0));
    const dailyTokOut = dayLabels.map(d => byDay[d].reduce((s,r)=>s+(r.tokens_out||0),0));

    // Top categories overall, others bucketed.
    const catCount = {};
    _turnsCache.forEach(r => catCount[r.category || "unknown"] = (catCount[r.category||"unknown"]||0)+1);
    const topCats = Object.entries(catCount).sort((a,b)=>b[1]-a[1]).slice(0,6).map(c=>c[0]);
    const catSeries = {}; topCats.forEach(c => catSeries[c] = dayLabels.map(()=>0));
    catSeries["other"] = dayLabels.map(()=>0);
    dayLabels.forEach((d, i) => {
      byDay[d].forEach(r => {
        const cat = r.category || "unknown";
        const key = topCats.includes(cat) ? cat : "other";
        catSeries[key][i] += 1;
      });
    });

    // ---- charts ----
    if (chartLatency) chartLatency.destroy();
    chartLatency = new Chart($("#chart-latency"), {
      type: "line",
      data: { labels: dayLabels, datasets: [
        { label: "median ms", data: dailyMedian,
          borderColor: "#7fb1ff", backgroundColor: "rgba(127,177,255,.15)", tension: .3, fill: true },
        { label: "p95 ms", data: dailyP95,
          borderColor: "#f3a86b", backgroundColor: "rgba(243,168,107,.15)", tension: .3, fill: false },
      ] }, options: chartOpts(),
    });

    if (chartCats) chartCats.destroy();
    chartCats = new Chart($("#chart-cats"), {
      type: "bar",
      data: { labels: dayLabels,
              datasets: Object.keys(catSeries).map((c,i) => ({
                label: c, data: catSeries[c],
                backgroundColor: PALETTE[i % PALETTE.length],
              })) },
      options: chartOpts({ scales: {
        x: { stacked: true, ticks: { color:"#6b7689" }, grid:{ color:"#1f2a3d" } },
        y: { stacked: true, beginAtZero: true, ticks: { color:"#6b7689" }, grid:{ color:"#1f2a3d" } },
      }}),
    });

    if (chartToks) chartToks.destroy();
    chartToks = new Chart($("#chart-toks"), {
      type: "bar",
      data: { labels: dayLabels, datasets: [
        { label: "prompt tokens",   data: dailyTokIn,  backgroundColor: "#7fb1ff" },
        { label: "response tokens", data: dailyTokOut, backgroundColor: "#f3a86b" },
      ] },
      options: chartOpts({ scales: {
        x: { stacked: true, ticks: { color:"#6b7689" }, grid:{ color:"#1f2a3d" } },
        y: { stacked: true, beginAtZero: true, ticks: { color:"#6b7689" }, grid:{ color:"#1f2a3d" } },
      }}),
    });

    const moodCount = {};
    _turnsCache.forEach(r => moodCount[r.mood] = (moodCount[r.mood]||0)+1);
    if (chartMood) chartMood.destroy();
    chartMood = new Chart($("#chart-mood"), {
      type: "doughnut",
      data: { labels: Object.keys(moodCount).length ? Object.keys(moodCount) : ["neutral"],
              datasets: [{ data: Object.values(moodCount).length ? Object.values(moodCount) : [0],
                           backgroundColor: ["#8de29a","#f3a86b","#7fb1ff","#e58fbf"] }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: "60%",
                 plugins: { legend: { labels: { color: "#98a2b3" } } } },
    });

    // ---- populate filter dropdowns + render table ----
    fillFilter("f-speaker", _turnsCache.map(r=>r.speaker));
    fillFilter("f-source",  _turnsCache.map(r=>r.source));
    fillFilter("f-category",_turnsCache.map(r=>r.category));
    fillFilter("f-mood",    _turnsCache.map(r=>r.mood));
    renderTurns();
  }

  function fillFilter(id, vals) {
    const sel = $("#" + id);
    if (!sel) return;
    const current = sel.value;
    const uniq = [...new Set(vals.filter(Boolean))].sort();
    sel.innerHTML = `<option value="">all</option>` +
      uniq.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
    if (uniq.includes(current)) sel.value = current;
  }

  const TCOLS = [
    ["iso","When",""], ["speaker","Who",""], ["source","Source",""],
    ["category","Category",""], ["mood","Mood",""],
    ["query","Query","q"], ["reply","Reply","q"],
    ["total_ms","total","ms total"], ["active","active","ms"],
    ["listen_ms","listen","ms"], ["stt_ms","stt","ms"], ["llm_ms","llm","ms"],
    ["action_ms","action","ms"], ["synth_ms","synth","ms"], ["play_ms","speaking","ms"],
  ];

  function renderTurns() {
    const sp = $("#f-speaker").value, sr = $("#f-source").value;
    const ct = $("#f-category").value, md = $("#f-mood").value;
    const q  = $("#f-q").value.toLowerCase();
    let rows = _turnsCache.filter(r =>
      (!sp || r.speaker === sp) && (!sr || r.source === sr) &&
      (!ct || r.category === ct) && (!md || r.mood === md) &&
      (!q  || r.query.toLowerCase().includes(q) || r.reply.toLowerCase().includes(q))
    );
    rows.sort((a, b) => {
      const av = a[_sortKey], bv = b[_sortKey];
      const cmp = (typeof av === "number" && typeof bv === "number")
        ? av - bv : String(av).localeCompare(String(bv));
      return _sortDir === "asc" ? cmp : -cmp;
    });
    $("#row-count").textContent = `${rows.length} / ${_turnsCache.length} turns`;

    const head = "<thead><tr>" + TCOLS.map(([k,label,cls]) =>
      `<th data-k="${k}" class="${cls.includes("ms") ? "ms" : ""}${k===_sortKey ? " active" : ""}">${label}${k===_sortKey ? (_sortDir==="asc" ? " ▲" : " ▼") : ""}</th>`
    ).join("") + "</tr></thead>";

    const body = "<tbody>" + (rows.length === 0
      ? `<tr><td colspan="${TCOLS.length}" class="empty">No turns match these filters.</td></tr>`
      : rows.map(r => "<tr>" + TCOLS.map(([k,_,cls]) => {
          if (k === "speaker") return `<td><span class="pill-sm ${r.speaker === "owner" ? "owner" : "other"}">${escapeHtml(r.speaker)}</span></td>`;
          if (k === "query")   return `<td class="q" title="${escapeHtml(r.query)}">${escapeHtml(r.query)}</td>`;
          if (k === "reply")   return `<td class="q" title="${escapeHtml(r.reply)}">${escapeHtml(r.reply)}</td>`;
          const isMs = cls.includes("ms");
          const cellCls = isMs ? (k === "total_ms" ? "ms total" : "ms") : "";
          const v = r[k];
          return `<td class="${cellCls}">${isMs ? (v||0).toLocaleString() : escapeHtml(v)}</td>`;
        }).join("") + "</tr>").join("")
    ) + "</tbody>";

    const wrap = $("#turn-table");
    wrap.innerHTML = "<table>" + head + body + "</table>";
    wrap.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (_sortKey === k) _sortDir = (_sortDir === "asc" ? "desc" : "asc");
      else { _sortKey = k;
             _sortDir = (typeof _turnsCache[0]?.[k] === "number") ? "desc" : "asc"; }
      renderTurns();
    }));
  }

  $("#t-range")?.addEventListener("change", loadTelemetry);
  ["f-speaker","f-source","f-category","f-mood"].forEach(id =>
    document.addEventListener("change", e => {
      if (e.target && e.target.id === id) renderTurns();
    }));
  document.addEventListener("change", e => {
    if (e.target && e.target.id === "f-sort") {
      const [k, d] = e.target.value.split(":");
      _sortKey = k; _sortDir = d;
      renderTurns();
    }
  });
  document.addEventListener("input", e => {
    if (e.target && e.target.id === "f-q") renderTurns();
  });

  function chartOpts(extra = {}) {
    return Object.assign({
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: "#98a2b3" } } },
      scales: {
        x: { ticks: { color: "#6b7689" }, grid: { color: "#1f2a3d" } },
        y: { ticks: { color: "#6b7689" }, grid: { color: "#1f2a3d" }, beginAtZero: true },
      },
    }, extra);
  }

  // ---- AGENDA -------------------------------------------------------------
  let agendaWhen = "today";
  async function loadAgenda() {
    const data = await getJSON(`/api/agenda?when=${encodeURIComponent(agendaWhen)}`);
    $("#a-spoken").textContent = data.spoken || "";
    $("#a-events").innerHTML = (data.events||[]).map(e => `
      <div class="item">
        <div><strong>${escapeHtml(e.title || "(untitled)")}</strong></div>
        <div class="when">${fmtDateTime(e.start)}</div>
        <button class="del-btn" data-cancel-event="${escapeHtml(e.title || "")}" title="Cancel event">✕</button>
      </div>`).join("") || `<div class="muted">No events.</div>`;
    $("#a-reminders").innerHTML = (data.reminders||[]).map(r => `
      <div class="item">
        <div>${escapeHtml(r.text || "")}</div>
        <div class="when">${fmtDateTime(r.fires_at)}</div>
        <button class="del-btn" data-cancel-rem="${escapeHtml(r.text || "")}" title="Cancel">✕</button>
      </div>`).join("") || `<div class="muted">No reminders.</div>`;
    $("#a-todos").innerHTML = (data.todos||[]).map(t => `
      <div class="item"><div>${escapeHtml(t)}</div>
        <button class="del-btn" data-todo-done="${escapeHtml(t)}" title="Mark done">✓</button>
      </div>
    `).join("") || `<div class="muted">No todos.</div>`;
  }
  $$('[data-when]').forEach(b => b.addEventListener("click", () => {
    $$('[data-when]').forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    agendaWhen = b.dataset.when;
    loadAgenda();
  }));

  $("#a-todo-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("#a-todo-input").value.trim();
    if (!text) return;
    await send("POST", "/api/agenda/todo", { text });
    $("#a-todo-input").value = "";
    loadAgenda();
  });
  $("#a-todo-clear")?.addEventListener("click", async () => {
    if (!confirm("Clear ALL todos?")) return;
    await send("POST", "/api/agenda/todo/clear");
    loadAgenda();
  });
  $("#a-snooze")?.addEventListener("click", async () => {
    await send("POST", "/api/reminders/snooze", { minutes: 10 });
    loadAgenda();
  });
  $("#a-rem-clear")?.addEventListener("click", async () => {
    if (!confirm("Clear ALL pending reminders?")) return;
    await send("POST", "/api/reminders/clear");
    loadAgenda();
  });
  document.addEventListener("click", async (e) => {
    const t = e.target;
    if (t.dataset?.todoDone) {
      await send("POST", "/api/agenda/todo/complete", { match: t.dataset.todoDone });
      loadAgenda();
    } else if (t.dataset?.cancelRem) {
      await send("DELETE", `/api/reminders/${encodeURIComponent(t.dataset.cancelRem)}`);
      loadAgenda();
    } else if (t.dataset?.cancelEvent) {
      if (!confirm(`Cancel event "${t.dataset.cancelEvent}"?`)) return;
      await send("POST", "/api/events/cancel", { match: t.dataset.cancelEvent });
      loadAgenda();
    }
  });

  // ---- VOICE DEBUG --------------------------------------------------------
  let debugKind = "audio";
  async function loadVoiceDebug() {
    const url = debugKind === "audio" ? "/api/debug/audio" : "/api/debug/tts";
    const fileBase = debugKind === "audio" ? "/api/debug/audio/file/" : "/api/debug/tts/file/";
    const data = await getJSON(url);
    const rows = (data.entries || []).slice().reverse();
    $("#v-rows").innerHTML = rows.map(e => `
      <tr>
        <td class="num">${e.idx}</td>
        <td class="num">${e.iso || ""}</td>
        <td>${e.source || ""}</td>
        <td>${escapeHtml(e.text || "")}</td>
        <td>${e.file ? `<audio controls preload="none" src="${fileBase}${encodeURIComponent(e.file)}"></audio>` : ""}</td>
        <td><button class="del-btn" data-del-debug="${e.idx}" title="Delete">✕</button></td>
      </tr>`).join("") || `<tr><td colspan="6" class="muted">No captures yet.</td></tr>`;
  }
  $$('[data-debug]').forEach(b => b.addEventListener("click", () => {
    $$('[data-debug]').forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    debugKind = b.dataset.debug;
    loadVoiceDebug();
  }));
  document.addEventListener("click", async (e) => {
    const idx = e.target?.dataset?.delDebug;
    if (idx == null) return;
    await send("DELETE", `/api/debug/${debugKind}/${idx}`);
    loadVoiceDebug();
  });
  $("#v-clear")?.addEventListener("click", async () => {
    if (!confirm("Clear ALL captured " + debugKind + " entries?")) return;
    await send("POST", `/api/debug/${debugKind}/clear`);
    loadVoiceDebug();
  });

  // ---- HEALTH -------------------------------------------------------------
  async function loadHealth() {
    const data = await getJSON("/api/health");
    $("#h-orion").innerHTML = (data.orion||[]).map(c => `
      <div class="item ${c.status === "ok" ? "ok" : "bad"}">
        <div><strong>${escapeHtml(c.label)}</strong>
          <span class="muted small"> — ${escapeHtml(c.value)}</span></div>
        <div class="badge">${c.status}</div>
      </div>`).join("");
    $("#h-pc").innerHTML = (data.pc||[]).map(c => `
      <div class="item">
        <div><strong>${escapeHtml(c.label)}</strong>
          <span class="muted small"> — ${escapeHtml(c.value)}</span></div>
      </div>`).join("");
  }
  $("#h-refresh")?.addEventListener("click", loadHealth);
  $("#h-mic")?.addEventListener("click", async () => {
    $("#h-mic-result").textContent = "listening…";
    const r = await send("POST", "/api/actions/mic_test");
    if (r.ok) {
      const bar = "█".repeat(Math.min(20, Math.round(r.level / 5)));
      $("#h-mic-result").textContent = `RMS ${r.rms}  ${bar}`;
    } else {
      $("#h-mic-result").textContent = "mic test failed: " + (r.error || "?");
    }
  });
  $("#h-enroll")?.addEventListener("click", async () => {
    const r = await send("POST", "/api/actions/enroll");
    alert(r.ok ? r.message : ("Could not launch enrollment: " + (r.error || "?")));
  });
  $("#h-calendar")?.addEventListener("click", async () => {
    const r = await send("POST", "/api/actions/reconnect_calendar");
    alert(r.ok ? r.message : ("Could not start reconnect: " + (r.error || "?")));
    loadHealth();
  });

  // ---- SKILLS -------------------------------------------------------------
  async function loadSkills() {
    const data = await getJSON("/api/skills");
    const total = data.tools.length;
    const off = data.tools.filter(t => !t.enabled).length;
    $("#s-note").textContent = `· ${total} built-in${off ? `, ${off} disabled` : ""}`;
    $("#s-rows").innerHTML = data.tools.map(t => `
      <tr data-name="${escapeHtml(t.name)}">
        <td><label class="switch"><input type="checkbox" ${t.enabled ? "checked" : ""} data-skill-toggle="${escapeHtml(t.name)}"><span class="slider"></span></label></td>
        <td><code>${escapeHtml(t.name)}</code></td>
        <td>${escapeHtml((t.description || "").slice(0, 240))}</td>
      </tr>`).join("");
  }
  document.addEventListener("change", async (e) => {
    if (!(e.target instanceof HTMLInputElement)) return;
    const name = e.target.dataset.skillToggle;
    if (!name) return;
    await send("POST", `/api/skills/${encodeURIComponent(name)}/toggle`,
               { enabled: e.target.checked });
    loadSkills();
  });

  // ---- SETTINGS -----------------------------------------------------------
  const GROUPS = [
    ["Behaviour", ["TELEMETRY","MOOD_AWARENESS","CONTEXT_AWARENESS","REPLAY_DEBUG","TTS_DEBUG",
                   "FUNCTION_CALLING","WEB_SEARCH","AUTO_MEMORY","DAILY_BRIEFING",
                   "PROACTIVE_ALERTS","PUSH_TO_TALK","PUSH_TO_TALK_HOTKEY",
                   "THINKING_FILLERS","BARGE_IN"]],
    ["Voice",     ["TTS_ENGINE","PIPER_VOICE","PIPER_LENGTH_SCALE",
                   "MOONSHINE_MODEL","STT_ENGINE","TTS_VOLUME"]],
    ["Security",  ["VOICE_VERIFICATION","VOICE_THRESHOLD"]],
    ["Agenda",    ["END_OF_DAY_NUDGE","END_OF_DAY_HOUR","END_OF_DAY_EARLY_HOUR",
                   "REMINDER_CALENDAR_SYNC"]],
    // LLM_BACKEND is the single switch: "gemini" puts everything back on
    // Gemini, "deepseek" uses DeepSeek for text and hands images and
    // web-grounded questions to LLM_IMAGE_BACKEND / LLM_SEARCH_BACKEND.
    ["Model",     ["LLM_BACKEND","DEEPSEEK_MODEL","GEMINI_MODEL","GROK_MODEL",
                   "LLM_IMAGE_BACKEND","LLM_SEARCH_BACKEND","LLM_FALLBACK"]],
    ["Hub",       ["HUB_AUTOSTART","HUB_PORT","HUB_OPEN_ON_START"]],
  ];
  async function loadSettings() {
    const payload = await getJSON("/api/settings");
    const byKey = {};
    payload.settings.forEach(s => byKey[s.key] = s);
    $("#set-groups").innerHTML = GROUPS.map(([title, keys]) => `
      <div class="set-group">
        <h3>${title}</h3>
        ${keys.map(k => settingRow(byKey[k])).filter(Boolean).join("")}
      </div>`).join("");
  }

  function settingRow(s) {
    if (!s) return "";
    const tag = s.hot
      ? `<span class="hot-tag" title="Applies immediately">live</span>`
      : `<span class="restart-tag" title="Restart Orion to apply">⟳ restart</span>`;
    return `<div class="set-row${s.overridden ? " overridden" : ""}" data-key="${s.key}">
      <div class="badge-row">
        <div><code>${s.key}</code>${tag}</div>
        ${s.overridden ? `<div class="meta">custom value · was overridden</div>` : ""}
      </div>
      <div class="ctrl">
        ${controlFor(s)}
        <span class="saved">saved ✓</span>
        ${s.overridden ? `<button class="reset" title="Reset to default" data-reset="${s.key}">↺</button>` : ""}
      </div>
    </div>`;
  }

  function controlFor(s) {
    const k = s.key;
    if (s.type === "bool") {
      return `<label class="switch"><input type="checkbox" data-set="${k}" ${s.value ? "checked" : ""}><span class="slider"></span></label>`;
    }
    if (s.enum && s.enum.length) {
      return `<select data-set="${k}">${s.enum.map(opt =>
        `<option value="${escapeHtml(opt)}"${opt === s.value ? " selected" : ""}>${escapeHtml(opt)}</option>`
      ).join("")}</select>`;
    }
    if (s.type === "number") {
      const step = (String(s.value).includes(".")) ? "0.01" : "1";
      return `<input type="number" step="${step}" data-set="${k}" value="${escapeHtml(s.value)}">`;
    }
    return `<input type="text" data-set="${k}" value="${escapeHtml(s.value == null ? "" : s.value)}">`;
  }

  function flashSaved(key) {
    const row = document.querySelector(`.set-row[data-key="${CSS.escape(key)}"]`);
    if (!row) return;
    row.classList.add("flash");
    setTimeout(() => row.classList.remove("flash"), 900);
  }

  async function patchSetting(key, value) {
    const r = await send("PATCH", "/api/settings", { key, value });
    if (r.ok) { flashSaved(key); loadSettings(); }
  }

  document.addEventListener("change", (e) => {
    const k = e.target?.dataset?.set;
    if (!k) return;
    let value;
    if (e.target.type === "checkbox") value = e.target.checked;
    else if (e.target.type === "number") value = e.target.value === "" ? null : Number(e.target.value);
    else value = e.target.value;
    patchSetting(k, value);
  });
  document.addEventListener("click", async (e) => {
    const k = e.target?.dataset?.reset;
    if (!k) return;
    await send("DELETE", `/api/settings/${encodeURIComponent(k)}`);
    loadSettings();
  });

  // ---- CONVERSATION -------------------------------------------------------
  async function loadConversation() {
    const data = await getJSON("/api/conversation");
    const recap = data.recap || {};
    $("#c-recap").textContent = recap.text || recap.summary || "(no recap saved yet)";
    const arch = (data.archive || []).slice().reverse();
    $("#c-archive").innerHTML = arch.map(a => `
      <div class="item">
        <div>
          <div class="muted small">${escapeHtml(a.date || a.iso || "")}</div>
          <div>${escapeHtml(a.summary || a.text || "")}</div>
        </div>
      </div>`).join("") || `<div class="muted">Nothing archived yet.</div>`;
  }

  // ---- MEMORY -------------------------------------------------------------
  async function loadMemory() {
    const data = await getJSON("/api/memory");
    const facts = data.facts || [];
    $("#m-facts").innerHTML = facts.length ? facts.map((f, i) => `
      <div class="item" data-fact-idx="${i}">
        <input class="fact-text" type="text" value="${escapeHtml(f)}" data-fact-edit="${i}">
        <button class="btn small" data-fact-save="${i}" title="Save">Save</button>
        <button class="del-btn" data-fact-del="${i}" title="Delete">✕</button>
      </div>`).join("") : `<div class="muted">No facts remembered yet.</div>`;
    // Only overwrite the recap box when the user isn't mid-edit, so a live
    // memory_changed event doesn't clobber what they're typing.
    const recap = $("#m-recap");
    if (recap && document.activeElement !== recap) recap.value = data.recap || "";
  }

  $("#m-fact-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("#m-fact-input").value.trim();
    if (!text) return;
    const r = await send("POST", "/api/memory/fact", { text });
    if (!r.ok && r.message === "duplicate") { /* already known */ }
    $("#m-fact-input").value = "";
    loadMemory();
  });
  document.addEventListener("click", async (e) => {
    const t = e.target;
    if (t.dataset?.factSave != null) {
      const i = +t.dataset.factSave;
      const input = document.querySelector(`[data-fact-edit="${i}"]`);
      if (!input) return;
      await send("PATCH", "/api/memory/fact", { index: i, text: input.value.trim() });
      loadMemory();
    } else if (t.dataset?.factDel != null) {
      if (!confirm("Forget this fact?")) return;
      await send("DELETE", `/api/memory/fact/${t.dataset.factDel}`);
      loadMemory();
    }
  });
  $("#m-recap-save")?.addEventListener("click", async () => {
    const r = await send("PATCH", "/api/memory/recap", { recap: $("#m-recap").value });
    if (r.ok) {
      const tag = $("#m-recap-saved");
      tag.classList.add("show");
      setTimeout(() => tag.classList.remove("show"), 1200);
    }
  });

  // ---- LOGS ---------------------------------------------------------------
  async function loadLogs() {
    try {
      const data = await getJSON("/api/logs?limit=300");
      const lines = data.lines || [];
      $("#l-count").textContent = `· ${lines.length} lines`;
      const pane = $("#l-pane");
      pane.innerHTML = lines.length ? lines.map(l => {
        const t = l.ts ? new Date(l.ts * 1000).toLocaleTimeString() : "";
        return `<div class="log-line ${escapeHtml(l.stream || "")}">` +
               `<span class="lt">${t}</span>` +
               `<span class="ls">${escapeHtml(l.stream || "")}</span>` +
               `<span class="ll">${escapeHtml(l.line || "")}</span></div>`;
      }).join("") : `<div class="muted">No log lines yet.</div>`;
      if ($("#l-follow")?.checked) pane.scrollTop = pane.scrollHeight;
    } catch (e) { console.warn(e); }
  }
  $("#l-refresh")?.addEventListener("click", loadLogs);
  $("#l-clear")?.addEventListener("click", async () => {
    await send("POST", "/api/logs/clear");
    loadLogs();
  });

  // ---- keyboard shortcuts ---------------------------------------------------
  // 1-9 and 0 switch tabs (nav order); / jumps to the telemetry search box.
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    const typing = (t instanceof HTMLInputElement)
      || (t instanceof HTMLTextAreaElement)
      || (t instanceof HTMLSelectElement)
      || (t && t.isContentEditable);
    if (typing) {
      if (e.key === "Escape") t.blur();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") {
      e.preventDefault();
      if (currentTab() !== "telemetry") location.hash = "telemetry";
      setTimeout(() => $("#f-q")?.focus(), 60);
      return;
    }
    const tabs = $$(".tab").map(b => b.dataset.tab);
    let idx = -1;
    if (e.key >= "1" && e.key <= "9") idx = +e.key - 1;
    else if (e.key === "0") idx = 9;
    if (idx >= 0 && idx < tabs.length) location.hash = tabs[idx];
  });

  // ---- helpers ------------------------------------------------------------
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
  }

  const LOADERS = {
    dashboard: loadDashboard,
    telemetry: loadTelemetry,
    agenda: loadAgenda,
    voice: loadVoiceDebug,
    health: loadHealth,
    skills: loadSkills,
    settings: loadSettings,
    memory: loadMemory,
    conversation: loadConversation,
    logs: loadLogs,
  };

  // ---- boot ---------------------------------------------------------------
  window.addEventListener("DOMContentLoaded", () => {
    connectSSE();
    activate(location.hash.slice(1) || "dashboard");
    // Light auto-refresh of the dashboard every 5s when it's open.
    setInterval(() => { if (currentTab() === "dashboard") loadDashboard(); }, 5000);
    // Logs tab follows live output while auto-refresh is ticked.
    setInterval(() => {
      if (currentTab() === "logs" && $("#l-follow")?.checked) loadLogs();
    }, 3000);
  });
})();
