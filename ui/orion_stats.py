"""Render `stats.jsonl` (written by telemetry.py) into a local HTML dashboard.

    python orion_stats.py            # rebuild stats.html and open it
    python orion_stats.py --no-open  # rebuild only

The HTML uses Chart.js from a CDN. Nothing is uploaded -- the dashboard
is rendered locally and just reads the JSONL file off disk.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _paths  # noqa: F401

import config
import telemetry


OUT = Path("stats.html")
DAYS = 14  # range shown in the trend charts


# ---------- aggregation ---------------------------------------------------

def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _day(iso: str) -> str:
    return iso[:10] if iso else ""


def aggregate(rows: list[dict]) -> dict:
    today = datetime.now().date()
    cutoff = today - timedelta(days=DAYS - 1)
    days = [(cutoff + timedelta(days=i)).isoformat() for i in range(DAYS)]

    in_range = [r for r in rows if r.get("iso", "")[:10] >= cutoff.isoformat()]

    totals_by_day: dict[str, list[int]] = defaultdict(list)
    cat_by_day: dict[str, Counter] = defaultdict(Counter)
    tokens_by_day: dict[str, dict[str, int]] = defaultdict(
        lambda: {"in": 0, "out": 0})
    mood_count = Counter()
    cat_count = Counter()
    all_totals: list[int] = []
    all_listen: list[int] = []
    all_stt: list[int] = []
    all_llm: list[int] = []
    all_action: list[int] = []
    all_tts_synth: list[int] = []
    all_tts_play: list[int] = []
    tok_in_per_call: list[int] = []
    tok_out_per_call: list[int] = []

    for r in in_range:
        d = _day(r.get("iso", ""))
        t = int(r.get("total_ms", 0))
        totals_by_day[d].append(t)
        cat_by_day[d][r.get("category", "unknown")] += 1
        cat_count[r.get("category", "unknown")] += 1
        mood_count[r.get("mood", "neutral")] += 1
        if r.get("tokens_in"):
            tokens_by_day[d]["in"] += int(r["tokens_in"] or 0)
            tok_in_per_call.append(int(r["tokens_in"]))
        if r.get("tokens_out"):
            tokens_by_day[d]["out"] += int(r["tokens_out"] or 0)
            tok_out_per_call.append(int(r["tokens_out"]))
        all_totals.append(t)
        all_listen.append(int(r.get("listen_ms", 0)))
        all_stt.append(int(r.get("stt_ms", 0)))
        all_llm.append(int(r.get("llm_ms", 0)))
        all_action.append(int(r.get("action_ms", 0)))
        # Old rows didn't split synth/play -- fall back to legacy tts_ms.
        synth = int(r.get("tts_synth_ms", 0))
        play = int(r.get("tts_play_ms", 0))
        if synth == 0 and play == 0:
            synth = int(r.get("tts_ms", 0))
        all_tts_synth.append(synth)
        all_tts_play.append(play)

    # Top categories overall, others bucketed as "other".
    top_cats = [c for c, _ in cat_count.most_common(6)]
    cat_series: dict[str, list[int]] = {c: [0] * DAYS for c in top_cats}
    cat_series["other"] = [0] * DAYS
    for i, d in enumerate(days):
        for cat, count in cat_by_day[d].items():
            key = cat if cat in cat_series else "other"
            cat_series[key][i] += count

    daily_median = [int(statistics.median(totals_by_day[d]))
                    if totals_by_day[d] else 0 for d in days]
    daily_p95 = [int(_pct(totals_by_day[d], 0.95))
                 if totals_by_day[d] else 0 for d in days]
    daily_count = [len(totals_by_day[d]) for d in days]
    daily_tokens_in = [tokens_by_day[d]["in"] for d in days]
    daily_tokens_out = [tokens_by_day[d]["out"] for d in days]

    # Active = everything except mic listening and Orion's own speaking time
    # (i.e. the part of the round-trip that's actual work, not human/audio length).
    all_active = [s + l + a + ts for s, l, a, ts in
                  zip(all_stt, all_llm, all_action, all_tts_synth)]

    # Recent turns for the interactive table (sorted slowest first, capped).
    slow = sorted(in_range, key=lambda r: int(r.get("total_ms", 0)),
                  reverse=True)[:50]

    return {
        "days": days,
        "headline": {
            "turns": len(in_range),
            "avg_ms": int(statistics.mean(all_totals)) if all_totals else 0,
            "active_avg": int(statistics.mean(all_active)) if all_active else 0,
            "p50_ms": int(_pct(all_totals, 0.5)),
            "p95_ms": int(_pct(all_totals, 0.95)),
            "listen_avg": int(statistics.mean(all_listen)) if all_listen else 0,
            "stt_avg": int(statistics.mean(all_stt)) if all_stt else 0,
            "llm_avg": int(statistics.mean(all_llm)) if all_llm else 0,
            "action_avg": int(statistics.mean(all_action)) if all_action else 0,
            "tts_synth_avg": int(statistics.mean(all_tts_synth))
                              if all_tts_synth else 0,
            "tts_play_avg": int(statistics.mean(all_tts_play))
                             if all_tts_play else 0,
            "tok_in_avg": int(statistics.mean(tok_in_per_call))
                           if tok_in_per_call else 0,
            "tok_out_avg": int(statistics.mean(tok_out_per_call))
                            if tok_out_per_call else 0,
            "tok_call_count": len(tok_in_per_call),
        },
        "daily_median": daily_median,
        "daily_p95": daily_p95,
        "daily_count": daily_count,
        "daily_tokens_in": daily_tokens_in,
        "daily_tokens_out": daily_tokens_out,
        "cat_series": cat_series,
        "mood": dict(mood_count),
        "slow": slow,
        "total_rows": len(rows),
    }


# ---------- rendering -----------------------------------------------------

HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Orion telemetry</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:24px;}
 h1{margin:0 0 4px;font-size:22px;font-weight:600;}
 .sub{color:#9aa0a6;font-size:13px;margin-bottom:24px;}
 .grid{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:14px;}
 .grid.stages{grid-template-columns:repeat(6,1fr);margin-bottom:24px;}
 .card{background:#1a1d24;border:1px solid #262a33;border-radius:8px;padding:14px 16px;}
 .card .label{color:#9aa0a6;font-size:12px;text-transform:uppercase;letter-spacing:.5px;}
 .card .val{font-size:24px;font-weight:600;margin-top:4px;}
 .card .hint{color:#9aa0a6;font-size:11px;margin-top:4px;}
 .card.stage .val{font-size:20px;color:#7fb1ff;}
 .footnote{color:#9aa0a6;font-size:12px;margin-top:24px;line-height:1.5;}
 .footnote b{color:#cfd2d6;}
 .row{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:24px;}
 .chart{background:#1a1d24;border:1px solid #262a33;border-radius:8px;padding:14px;}
 .chart h3{margin:0 0 10px;font-size:14px;font-weight:500;color:#cfd2d6;}
 .canvas-wrap{position:relative;height:280px;width:100%;}
 .canvas-wrap.short{height:220px;}
 table{width:100%;border-collapse:collapse;font-size:13px;}
 th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #262a33;}
 th{color:#9aa0a6;font-weight:500;user-select:none;cursor:pointer;position:sticky;top:0;background:#1a1d24;}
 th:hover{color:#e6e6e6;}
 th.active{color:#7fb1ff;}
 th.ms,td.ms{text-align:right;}
 td.q{color:#cfd2d6;font-family:Consolas,monospace;font-size:12px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
 td.ms{color:#f3a86b;font-variant-numeric:tabular-nums;}
 td.total{color:#ffb347;font-weight:600;}
 tbody tr:hover{background:#22262f;}
 .empty{color:#9aa0a6;font-style:italic;padding:24px;text-align:center;}
 .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;background:#262a33;color:#cfd2d6;}
 .pill.owner{background:#1e3a2a;color:#8de29a;}
 .pill.other{background:#3a1e1e;color:#e58fbf;}
 .filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;align-items:center;}
 .filters label{font-size:12px;color:#9aa0a6;display:flex;flex-direction:column;gap:3px;}
 .filters select,.filters input{background:#0f1115;color:#e6e6e6;border:1px solid #262a33;border-radius:6px;padding:6px 8px;font-size:13px;min-width:130px;}
 .filters input{min-width:200px;}
 .count{color:#9aa0a6;font-size:12px;margin-left:auto;}
 .scroll{max-height:560px;overflow:auto;border:1px solid #262a33;border-radius:6px;}
</style></head><body>
<h1>Orion telemetry</h1>
<div class="sub">__SUB__</div>

<div class="grid">
 <div class="card"><div class="label">Turns (last __DAYS__d)</div><div class="val">__TURNS__</div></div>
 <div class="card"><div class="label">Avg total</div><div class="val">__AVG__ ms</div><div class="hint">wake to reply finished</div></div>
 <div class="card"><div class="label">Avg active</div><div class="val">__ACTIVE__ ms</div><div class="hint">stt + llm + action + synth<br>(excludes listen &amp; speaking)</div></div>
 <div class="card"><div class="label">p50 / p95 total</div><div class="val">__P50__ / __P95__ ms</div><div class="hint">median / 95th percentile</div></div>
 <div class="card"><div class="label">Avg Gemini tokens / call</div><div class="val">__TOK_IN__ in &middot; __TOK_OUT__ out</div><div class="hint">across __TOK_CALLS__ LLM calls</div></div>
</div>

<div class="grid stages">
 <div class="card stage"><div class="label">1. Listen (mic)</div><div class="val">__LISTEN__ ms</div><div class="hint">time you spent talking</div></div>
 <div class="card stage"><div class="label">2. STT (transcribe)</div><div class="val">__STT__ ms</div><div class="hint">Whisper on your speech</div></div>
 <div class="card stage"><div class="label">3. Gemini round-trip</div><div class="val">__LLM__ ms</div><div class="hint">API call(s) total</div></div>
 <div class="card stage"><div class="label">4. Action work</div><div class="val">__ACTION__ ms</div><div class="hint">tools, song capture, etc.</div></div>
 <div class="card stage"><div class="label">5. TTS synth</div><div class="val">__TTS_SYNTH__ ms</div><div class="hint">Piper rendering audio</div></div>
 <div class="card stage"><div class="label">6. Orion speaking</div><div class="val">__TTS_PLAY__ ms</div><div class="hint">reply playback duration</div></div>
</div>

<div class="row">
 <div class="chart"><h3>Response time per day (median + p95)</h3><div class="canvas-wrap"><canvas id="lat"></canvas></div></div>
 <div class="chart"><h3>Mood</h3><div class="canvas-wrap short"><canvas id="mood"></canvas></div></div>
</div>

<div class="row">
 <div class="chart"><h3>Turns per category per day</h3><div class="canvas-wrap"><canvas id="cats"></canvas></div></div>
 <div class="chart"><h3>Gemini tokens per day</h3><div class="canvas-wrap"><canvas id="toks"></canvas></div></div>
</div>

<div class="chart">
 <h3>Recent turns</h3>
 <div class="filters">
  <label>Sort by
   <select id="f-sort">
    <option value="total_ms:desc">Total (slowest first)</option>
    <option value="total_ms:asc">Total (fastest first)</option>
    <option value="active:desc">Active (slowest first)</option>
    <option value="active:asc">Active (fastest first)</option>
    <option value="iso:desc">When (newest first)</option>
    <option value="iso:asc">When (oldest first)</option>
    <option value="llm_ms:desc">LLM ms (high→low)</option>
    <option value="stt_ms:desc">STT ms (high→low)</option>
    <option value="action_ms:desc">Action ms (high→low)</option>
   </select>
  </label>
  <label>Speaker
   <select id="f-speaker"><option value="">all</option></select>
  </label>
  <label>Source
   <select id="f-source"><option value="">all</option></select>
  </label>
  <label>Category
   <select id="f-category"><option value="">all</option></select>
  </label>
  <label>Mood
   <select id="f-mood"><option value="">all</option></select>
  </label>
  <label>Search query
   <input id="f-q" type="text" placeholder="contains…">
  </label>
  <span class="count" id="row-count"></span>
 </div>
 <div class="scroll" id="turn-table"></div>
</div>

<div class="footnote">
 <b>What these mean.</b>
 <b>total</b> is the full wake-to-reply round-trip.
 <b>listen</b> is how long the mic was open recording your speech (basically how long you spoke + the trailing-silence threshold).
 <b>stt</b> is Whisper turning that audio into text -- it does NOT include song-recognition capture (those seconds land in <b>action</b>).
 <b>llm</b> is the Gemini API round-trip (sums across multiple calls if a tool was invoked).
 <b>action</b> is work the handler did itself between getting the reply and speaking -- running a tool, capturing audio for song ID, etc.
 <b>synth</b> is Piper generating the audio file for the reply (the real "TTS cost").
 <b>speaking</b> is how long Orion actually talks out loud -- a long reply means a big number here, but it's not slowness, just length.
 <br><br>
 <b>p95</b> = the 95th-percentile latency. 95% of turns were faster than this number; only the slowest 5% were slower.
 It's the "worst-case feel" metric -- way more useful than average, which a couple of slow song lookups can drag up.
</div>

<script>
const DATA = __DATA__;
const opts = (extra={}) => Object.assign({
 responsive:true, maintainAspectRatio:false,
 plugins:{legend:{labels:{color:'#cfd2d6'}}},
 scales:{x:{ticks:{color:'#9aa0a6'},grid:{color:'#1f2229'}},
         y:{ticks:{color:'#9aa0a6'},grid:{color:'#1f2229'},beginAtZero:true}}
}, extra);

new Chart(document.getElementById('lat').getContext('2d'), {
 type:'line',
 data:{labels:DATA.days, datasets:[
  {label:'median ms', data:DATA.daily_median, borderColor:'#7fb1ff', backgroundColor:'#7fb1ff22', tension:.3},
  {label:'p95 ms', data:DATA.daily_p95, borderColor:'#f3a86b', backgroundColor:'#f3a86b22', tension:.3}
 ]}, options:opts()
});

const catLabels = Object.keys(DATA.cat_series);
const palette = ['#7fb1ff','#f3a86b','#8de29a','#e58fbf','#c9a0ff','#f0d97a','#9aa0a6'];
new Chart(document.getElementById('cats').getContext('2d'), {
 type:'bar',
 data:{labels:DATA.days, datasets:catLabels.map((c,i)=>({
  label:c, data:DATA.cat_series[c],
  backgroundColor:palette[i % palette.length]
 }))},
 options:opts({scales:{x:{stacked:true,ticks:{color:'#9aa0a6'},grid:{color:'#1f2229'}},
                       y:{stacked:true,beginAtZero:true,ticks:{color:'#9aa0a6'},grid:{color:'#1f2229'}}}})
});

new Chart(document.getElementById('toks').getContext('2d'), {
 type:'bar',
 data:{labels:DATA.days, datasets:[
  {label:'prompt tokens', data:DATA.daily_tokens_in, backgroundColor:'#7fb1ff'},
  {label:'response tokens', data:DATA.daily_tokens_out, backgroundColor:'#f3a86b'}
 ]}, options:opts({scales:{x:{stacked:true,ticks:{color:'#9aa0a6'},grid:{color:'#1f2229'}},
                           y:{stacked:true,beginAtZero:true,ticks:{color:'#9aa0a6'},grid:{color:'#1f2229'}}}})
});

const moodLabels = Object.keys(DATA.mood), moodVals = Object.values(DATA.mood);
new Chart(document.getElementById('mood').getContext('2d'), {
 type:'doughnut',
 data:{labels:moodLabels, datasets:[{data:moodVals,
        backgroundColor:['#8de29a','#f3a86b','#7fb1ff','#e58fbf']}]},
 options:{responsive:true, maintainAspectRatio:false,
          plugins:{legend:{labels:{color:'#cfd2d6'}}}}
});

// ---- Interactive turns table ----
(function(){
 const TURNS = (DATA.turns || []).map(r => ({...r, active: r.stt_ms + r.llm_ms + r.action_ms + r.synth_ms}));
 const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
 const uniq = key => [...new Set(TURNS.map(r => r[key]).filter(Boolean))].sort();
 const fill = (id, vals) => { const sel = document.getElementById(id);
   vals.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; sel.appendChild(o); }); };
 fill('f-speaker', uniq('speaker'));
 fill('f-source', uniq('source'));
 fill('f-category', uniq('category'));
 fill('f-mood', uniq('mood'));

 const COLS = [
  ['iso','When',''], ['speaker','Who',''], ['source','Source',''],
  ['category','Category',''], ['mood','Mood',''], ['query','Query','q'], ['reply','Reply','q'],
  ['total_ms','total','ms total'], ['active','active','ms'],
  ['listen_ms','listen','ms'], ['stt_ms','stt','ms'], ['llm_ms','llm','ms'],
  ['action_ms','action','ms'], ['synth_ms','synth','ms'], ['play_ms','speaking','ms'],
 ];
 let sortKey = 'total_ms', sortDir = 'desc';

 function render(){
  const sp = document.getElementById('f-speaker').value;
  const sr = document.getElementById('f-source').value;
  const ct = document.getElementById('f-category').value;
  const md = document.getElementById('f-mood').value;
  const q  = document.getElementById('f-q').value.toLowerCase();
  let rows = TURNS.filter(r =>
    (!sp || r.speaker === sp) && (!sr || r.source === sr) &&
    (!ct || r.category === ct) && (!md || r.mood === md) &&
    (!q  || (r.query || '').toLowerCase().includes(q) || (r.reply || '').toLowerCase().includes(q))
  );
  rows.sort((a,b) => {
    const av = a[sortKey], bv = b[sortKey];
    const cmp = (typeof av === 'number' && typeof bv === 'number')
      ? av - bv : String(av).localeCompare(String(bv));
    return sortDir === 'asc' ? cmp : -cmp;
  });
  document.getElementById('row-count').textContent =
    rows.length + ' / ' + TURNS.length + ' turns';
  const head = '<thead><tr>' + COLS.map(([k,label,cls]) =>
    `<th data-k="${k}" class="${cls.includes('ms')?'ms':''}${k===sortKey?' active':''}">${label}${k===sortKey?(sortDir==='asc'?' ▲':' ▼'):''}</th>`
  ).join('') + '</tr></thead>';
  const body = '<tbody>' + (rows.length === 0
    ? '<tr><td colspan="15" class="empty">No turns match these filters.</td></tr>'
    : rows.map(r => '<tr>' + COLS.map(([k,_,cls]) => {
        if (k === 'speaker') return `<td><span class="pill ${r.speaker==='owner'?'owner':'other'}">${esc(r.speaker)}</span></td>`;
        if (k === 'query')   return `<td class="q" title="${esc(r.query)}">${esc(r.query)}</td>`;
        if (k === 'reply')   return `<td class="q" title="${esc(r.reply)}">${esc(r.reply)}</td>`;
        const isMs = cls.includes('ms');
        const cellCls = isMs ? (k==='total_ms' ? 'ms total' : 'ms') : '';
        return `<td class="${cellCls}">${isMs ? r[k].toLocaleString() : esc(r[k])}</td>`;
      }).join('') + '</tr>').join('')
  ) + '</tbody>';
  const wrap = document.getElementById('turn-table');
  wrap.innerHTML = '<table>' + head + body + '</table>';
  wrap.querySelectorAll('th').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.k;
    if (sortKey === k) sortDir = (sortDir === 'asc' ? 'desc' : 'asc');
    else { sortKey = k; sortDir = (typeof TURNS[0]?.[k] === 'number') ? 'desc' : 'asc'; }
    render();
  }));
 }
 ['f-speaker','f-source','f-category','f-mood','f-sort'].forEach(id =>
   document.getElementById(id).addEventListener('change', e => {
     if (id === 'f-sort') { const [k,d] = e.target.value.split(':'); sortKey = k; sortDir = d; }
     render();
   }));
 document.getElementById('f-q').addEventListener('input', render);
 render();
})();
</script>
</body></html>
"""


def _turn_row(r: dict) -> dict:
    synth = int(r.get("tts_synth_ms", 0)) or int(r.get("tts_ms", 0))
    return {
        "iso": r.get("iso", ""),
        "speaker": r.get("speaker", "owner"),
        "source": r.get("source", ""),
        "category": r.get("category", ""),
        "mood": r.get("mood", ""),
        "query": r.get("query", ""),
        "reply": r.get("reply", ""),
        "total_ms": int(r.get("total_ms", 0)),
        "listen_ms": int(r.get("listen_ms", 0)),
        "stt_ms": int(r.get("stt_ms", 0)),
        "llm_ms": int(r.get("llm_ms", 0)),
        "action_ms": int(r.get("action_ms", 0)),
        "synth_ms": synth,
        "play_ms": int(r.get("tts_play_ms", 0)),
    }


def _slow_table(slow: list[dict]) -> str:
    if not slow:
        return '<div class="empty">No turns logged yet.</div>'
    rows = []
    for r in slow:
        q = (r.get("query") or "").replace("<", "&lt;").replace(">", "&gt;")
        rows.append(
            f"<tr><td>{r.get('iso','')}</td>"
            f"<td>{r.get('category','')}</td>"
            f"<td class='q'>{q}</td>"
            f"<td class='ms'>{r.get('total_ms',0)}</td>"
            f"<td class='ms'>{r.get('listen_ms',0)}</td>"
            f"<td class='ms'>{r.get('stt_ms',0)}</td>"
            f"<td class='ms'>{r.get('llm_ms',0)}</td>"
            f"<td class='ms'>{r.get('action_ms',0)}</td>"
            f"<td class='ms'>{r.get('tts_synth_ms', r.get('tts_ms',0))}</td>"
            f"<td class='ms'>{r.get('tts_play_ms',0)}</td></tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>When</th><th>Category</th><th>Query</th>"
        "<th>total</th><th>listen</th><th>stt</th><th>llm</th>"
        "<th>action</th><th>synth</th><th>speaking</th>"
        "</tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )


def render(rows: list[dict]) -> str:
    agg = aggregate(rows)
    h = agg["headline"]
    sub = (f"Last {DAYS} days · {agg['total_rows']} total turns on file · "
           f"source: {getattr(config, 'TELEMETRY_FILE', 'stats.jsonl')} · "
           f"generated {datetime.now().isoformat(timespec='seconds')}")
    return (HTML_TEMPLATE
            .replace("__SUB__", sub)
            .replace("__DAYS__", str(DAYS))
            .replace("__TURNS__", str(h["turns"]))
            .replace("__AVG__", str(h["avg_ms"]))
            .replace("__ACTIVE__", str(h["active_avg"]))
            .replace("__P50__", str(h["p50_ms"]))
            .replace("__P95__", str(h["p95_ms"]))
            .replace("__LISTEN__", str(h["listen_avg"]))
            .replace("__STT__", str(h["stt_avg"]))
            .replace("__LLM__", str(h["llm_avg"]))
            .replace("__ACTION__", str(h["action_avg"]))
            .replace("__TTS_SYNTH__", str(h["tts_synth_avg"]))
            .replace("__TTS_PLAY__", str(h["tts_play_avg"]))
            .replace("__TOK_IN__", f"{h['tok_in_avg']:,}")
            .replace("__TOK_OUT__", f"{h['tok_out_avg']:,}")
            .replace("__TOK_CALLS__", str(h["tok_call_count"]))
            .replace("__DATA__", json.dumps({
                "turns": [_turn_row(r) for r in agg["slow"]],
                "days": agg["days"],
                "daily_median": agg["daily_median"],
                "daily_p95": agg["daily_p95"],
                "daily_count": agg["daily_count"],
                "daily_tokens_in": agg["daily_tokens_in"],
                "daily_tokens_out": agg["daily_tokens_out"],
                "cat_series": agg["cat_series"],
                "mood": agg["mood"] or {"neutral": 0},
            })))


def main(argv: list[str]) -> int:
    rows = telemetry.read_all()
    OUT.write_text(render(rows), encoding="utf-8")
    print(f"Wrote {OUT.resolve()}  ({len(rows)} rows)")
    if "--no-open" not in argv:
        webbrowser.open(OUT.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
