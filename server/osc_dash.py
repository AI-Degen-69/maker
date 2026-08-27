"""Oscillation dashboard for 5m/15m crypto — shows whether spread 2 can be captured.

Reads run/oscillation_summary.json + run/oscillation_windows.jsonl + snapshots.
Serves :8802
"""
from __future__ import annotations
import json, time
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.middleware.gzip import GZipMiddleware

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run"

app = FastAPI(title="Oscillation")
app.add_middleware(GZipMiddleware, minimum_size=1000)

def load_summary():
    f = RUN / "oscillation_summary.json"
    if not f.exists():
        return {"ts": 0, "per_series": {}}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except: return {"ts": 0, "per_series": {}}

def load_windows(limit=200):
    f = RUN / "oscillation_windows.jsonl"
    if not f.exists():
        return []
    rows=[]
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        try: rows.append(json.loads(line))
        except: continue
    rows.sort(key=lambda x: x.get("end_ts",0), reverse=True)
    return rows[:limit]

def load_live_snaps():
    f = RUN / "oscillation_snapshots.jsonl"
    if not f.exists():
        return {}
    # last snap per series - read only bounded tail to avoid loading full file
    last={}
    try:
        # Read only last 2000 lines efficiently without loading entire file
        with open(f, 'r', encoding='utf-8') as file:
            # Seek to end and read backwards to find last 2000 lines
            file.seek(0, 2)  # Go to end
            file_size = file.tell()

            # If file is small, just read it all
            if file_size < 500000:  # ~500KB threshold
                file.seek(0)
                lines = file.readlines()[-2000:]
            else:
                # Read last chunk and extract lines
                chunk_size = min(file_size, 200000)  # Read last ~200KB
                file.seek(max(0, file_size - chunk_size))
                # Skip partial first line
                file.readline()
                lines = file.readlines()[-2000:]

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    last[r["series"]] = r
                except:
                    continue
    except Exception:
        pass
    return last

@app.get("/api/oscillation")
def api():
    summary = load_summary()
    wins = load_windows(200)
    live = load_live_snaps()
    now=time.time()
    # per-series counts from windows for histogram
    return {"now": now, "summary": summary, "windows": wins, "live": live}

PAGE = r"""<!doctype html><html lang="he" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oscillation — 5m/15m Spread Capture</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0d12;--panel:#12161d;--panel2:#171c24;--line:#232a35;--tx:#e7ebf3;--dim:#8792a6;--faint:#535e70;--up:#33c9b5;--upS:#12302c;--down:#f0684d;--gold:#e8b84b;--proj:#7b9bf7;--r:10px;--disp:'Space Grotesk',system-ui;--mono:'IBM Plex Mono',monospace;--body:'IBM Plex Sans',system-ui}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--tx);font:13px/1.5 var(--body);-webkit-font-smoothing:antialiased}
a{color:var(--proj);text-decoration:none} a:hover{text-decoration:underline}
.mono{font-family:var(--mono)}
.hdr{padding:16px 20px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.hdr h1{margin:0;font:700 16px var(--disp)} .tag{border:1px solid var(--up);color:var(--up);border-radius:99px;padding:2px 8px;font-size:10px;font-weight:700}
.wrap{max-width:1400px;margin:0 auto;padding:16px 20px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:1000px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card h3{margin:0 0 6px;font:700 11px var(--disp);letter-spacing:.08em;text-transform:uppercase}
.kpi{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.kpi .box{flex:1;min-width:90px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 9px;text-align:center}
.box .lbl{font:600 9px var(--disp);letter-spacing:.07em;color:var(--faint);text-transform:uppercase}
.box .val{font:700 18px var(--mono);margin-top:2px}
.box .sub{font:400 10px var(--mono);color:var(--dim)}
.bar{height:6px;background:var(--panel2);border:1px solid var(--line);border-radius:99px;overflow:hidden;margin-top:6px}
.fill{height:100%;border-radius:99px}
.fill.up{background:var(--up)} .fill.warn{background:var(--proj)} .fill.gold{background:var(--gold)} .fill.down{background:var(--down)}
.tbl{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
.tbl th{font:700 11px var(--disp);letter-spacing:.06em;text-transform:uppercase;color:var(--faint);text-align:right;padding:8px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
.tbl td{padding:10px 8px;border-bottom:1px solid #1a2029;font-size:13px;vertical-align:middle}
.price-up{color:var(--up);font-weight:700;font-family:var(--mono)}
.price-down{color:var(--down);font-weight:700;font-family:var(--mono)}
.price-small{font-size:10px;font-weight:500;opacity:.85}
.candle-wrap{width:110px}
.candle-bar{height:10px;background:var(--panel2);border:1px solid var(--line);border-radius:99px;position:relative;overflow:hidden}
.candle-wick{position:absolute;top:50%;height:2px;background:var(--faint);transform:translateY(-50%)}
.candle-body{position:absolute;top:2px;bottom:2px;border-radius:3px}
.pill{font:700 9px var(--disp);letter-spacing:.06em;padding:2px 7px;border-radius:99px;border:1px solid var(--line);white-space:nowrap}
.pill-osc{background:rgba(51,201,181,.12);color:var(--up);border-color:rgba(51,201,181,.3)}
.pill-mono{background:rgba(240,104,77,.12);color:var(--down);border-color:rgba(240,104,77,.3)}
.pill-flat{background:var(--panel2);color:var(--dim)}
.note{font-size:11px;color:var(--dim);line-height:1.5;margin-top:8px;border-top:1px dashed var(--line);padding-top:8px}
.live-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
@media(max-width:900px){.live-grid{grid-template-columns:repeat(2,1fr)}}
.liveBox{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 9px}
</style></head><body>
<div class="hdr">
  <h1>◆ תצפיות תנודה — 5m / 15m ספרד 2¢</h1>
  <span class="tag">Spread Hunter · oscillation lab</span>
  <span style="flex:1"></span>
  <span id="updated" class="mono" style="font-size:11px;color:var(--dim)"></span>
</div>
<div class="wrap">
  <div class="card" style="margin-bottom:12px">
    <h3>איך לקרוא <span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--dim)">— מה נמדד</span></h3>
    <div style="font-size:11.5px;color:var(--dim);line-height:1.6">
      <b style="color:var(--tx)">SPREAD 2 = 2¢ מה-mid לשני הצדדים → resting_pair = 0.96</b> (תמיד &lt;1.00, רווח 4¢ אם שני הצדדים מתמלאים ו-merge). כל שנייה נדגם ה-book: <code>mid = (best_bid+best_ask)/2</code> מ-UP, <code>touch_pair = up_ask + down_ask</code> (≈1.01). <br>
      <span style="display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:2px 7px;margin-top:4px"><b style="color:var(--tx)">50/50 בפתיחה:</b> <span style="color:var(--faint)">התחיל ב</span> = ה-mid הראשון (≈50¢), <b style="color:var(--up)">עלה ל</b> = ה-high (+X ירוק), <b style="color:var(--down)">ירד ל</b> = ה-low (−X אדום) — שניהם נמדדים מ-50. למשל 50→60 = <span style="color:var(--up)">+10¢</span>, 50→40 = <span style="color:var(--down)">−10¢</span>.</span><br>
      <b>oscillating</b> = גם עלה ≥2¢ וגם ירד ≥2¢ מ-50 (הזוג השני יכול להתמלא). <b>monotonic</b> = רק צד אחד ≥2¢ (צריך יציאה). <b>flat</b> = לא זז 2¢. כל חלון לחיץ → Polymarket.
    </div>
  </div>

  <div id="liveBar" class="card" style="margin-bottom:12px"></div>
  <div id="grid" class="grid"></div>
  <div id="tables" style="margin-top:12px"></div>
</div>
<script>
const $=s=>document.getElementById(s);
const esc=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const pct=(a,b)=> b?Math.round(a/b*100):0;
const hms=s=>{s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?`${h}h ${String(m).padStart(2,'0')}m`:`${m}m ${String(x).padStart(2,'0')}s`;};
function pill(cls,txt){return `<span class="pill ${cls}">${txt}</span>`;}
function clsPill(c){return c==='oscillating'?pill('pill-osc','oscillating תנודתי'):c==='monotonic'?pill('pill-mono','monotonic חד-כיווני'):c==='flat'?pill('pill-flat','flat שטוח'):pill('pill-flat',esc(c));}
async function tick(){
  let data; try{data=await (await fetch('/api/oscillation',{cache:'no-store'})).json();}catch(e){return;}
  const sum=data.summary||{}, per=sum.per_series||{}, live=data.live||{}, wins=data.windows||[];
  $('updated').textContent = sum.ts? `עודכן לפני ${hms((data.now||Date.now()/1000)-sum.ts)} · ${wins.length} חלונות סגורים` : 'אין נתונים עדיין — המדידה רצה';
  // live bar
  let liveHtml = '<h3>חלונות חיים עכשיו — Live mids</h3><div class="live-grid">';
  const order=['btc-up-or-down-5m','eth-up-or-down-5m','bnb-up-or-down-5m','sol-up-or-down-5m','xrp-up-or-down-5m','btc-up-or-down-15m','eth-up-or-down-15m','bnb-up-or-down-15m','sol-up-or-down-15m','xrp-up-or-down-15m'];
  for(const k of order){
    const s=live[k];
    if(!s){ liveHtml+=`<div class="liveBox"><div style="font:700 10px var(--disp);color:var(--faint)">${k}</div><div style="color:var(--dim);font-size:11px">אין live / סגור</div></div>`; continue; }
    const mid=s.mid==null?'-':(s.mid*100).toFixed(1)+'¢';
    const tp=s.touch_pair==null?'-':s.touch_pair.toFixed(3);
    const rem=s.t_rem==null?'-':hms(s.t_rem);
    const q=s.queue_up==null?'-':Math.round(s.queue_up);
    const marketUrl = `https://polymarket.com/market/${encodeURIComponent(s.slug)}`;
    const displaySlug = esc(s.slug.slice(0,28));
    liveHtml+=`<div class="liveBox"><div style="font:700 10px var(--disp);color:var(--faint)">${k}</div><div class="mono" style="font-size:12px">mid ${mid} · touch ${tp}</div><div class="mono" style="font-size:10px;color:var(--dim)">queue @rest ${q} · נותר ${rem}</div><div style="font-size:10px"><a href="${esc(marketUrl)}" target="_blank" rel="noopener" title="${esc(s.slug)}">${displaySlug} ↗</a></div></div>`;
  }
  liveHtml+='</div>';
  $('liveBar').innerHTML=liveHtml;

  // per-series cards
  let grid='';
  for(const k of order){
    const s=per[k];
    if(!s) continue;
    const n=s.windows||0;
    const any2=s.any_2c||0, any3=s.any_3c||0, osc=s.oscillating||0, mono=s.monotonic||0, flat=s.flat||0;
    const p2=pct(any2,n), p3=pct(any3,n), po=pct(osc,n), pm=pct(mono,n);
    grid+=`<div class="card"><h3>${esc(s.label)} — ${s.duration===300?'5 דקות':'15 דקות'} <span style="font-weight:400;color:var(--dim);text-transform:none;letter-spacing:0">· ${n} חלונות</span></h3>
      <div class="kpi">
        <div class="box"><div class="lbl">כל תנודה ≥2¢</div><div class="val">${any2}/${n}</div><div class="sub">${p2}% מהחלונות זזו 2¢ לפחות</div><div class="bar"><div class="fill up" style="width:${p2}%"></div></div></div>
        <div class="box"><div class="lbl">≥3¢</div><div class="val">${any3}/${n}</div><div class="sub">${p3}%</div><div class="bar"><div class="fill gold" style="width:${p3}%"></div></div></div>
        <div class="box"><div class="lbl">oscillating (שני כיוונים)</div><div class="val" style="color:var(--up)">${osc}/${n}</div><div class="sub">${po}% — הזוג היה יכול להתמלא</div><div class="bar"><div class="fill up" style="width:${po}%"></div></div></div>
        <div class="box"><div class="lbl">monotonic</div><div class="val" style="color:var(--down)">${mono}/${n}</div><div class="sub">${pm}% — צריך יציאה</div><div class="bar"><div class="fill down" style="width:${pm}%"></div></div></div>
      </div>
      <div class="mono" style="font-size:10px;color:var(--dim)">מדד pair ב-touch חציוני: ${s.pair_cost_median==null?'-':s.pair_cost_median.toFixed(3)} (קרוב ל-1.01 = ספר צר) · flat ${flat}/${n}</div>
      <div class="note">אם <b>oscillating ≥50%</b> עם resting 0.96 → לכידת 4¢ אפשרית. אם <b>monotonic גבוה</b> → צריך להגדיר רף יציאה (נמדוד בהמשך איפה לצאת).</div>
    </div>`;
  }
  $('grid').innerHTML=grid||'<div class="card">אין חלונות סגורים עדיין — המדידה רצה, תן לה 5-15 דקות לסגור חלון ראשון.</div>';

  // recent windows table — v2: fonts bigger, UP/DOWN both shown, Japanese candle 1..99
  let tbl='<div class="card"><h3 style="font-size:13px">חלונות אחרונים — פתיחה 50/50 (לחיץ ל-Polymarket, חדש → ישן)</h3><div style="font-size:12px;color:var(--dim);margin-bottom:8px">כל חלון נמדד מהפתיחה. <span class="price-up">אפ ירוק</span> = מחיר UP, <span class="price-down">דאון אדום</span> = מחיר DOWN (משלימים ל-100¢). <span class="price-up">עלה ל</span> = שיא UP (ירוק גדול) ולידו DOWN באותו רגע (אדום קטן). <span class="price-down">ירד ל</span> = שיא DOWN (אדום גדול) ולידו UP קטן. הנר = פתיחה→סגירה, פתילה = high/low (בחרנו צד UP).</div><table class="tbl"><tr><th>סדרה</th><th>חלון</th><th>פתיחה<br><span style="font-weight:400;letter-spacing:0">UP / DOWN</span></th><th style="color:var(--up)">עלה ל<br><span style="font-weight:400;color:var(--dim)">UP high / DOWN low</span></th><th style="color:var(--down)">ירד ל<br><span style="font-weight:400;color:var(--dim)">DOWN high / UP low</span></th><th>נר יפני<br><span style="font-weight:400">1 ← 50 → 99</span></th><th>סיווג</th><th>קישור</th></tr>';
  for(const w of wins.slice(0,80)){
    const sm = w.start_mid, cm=w.close_mid, mx=w.max_mid, mn=w.min_mid;
    const fmt = v=> v==null?'-':(v*100).toFixed(1)+'¢';
    // opening: UP green / DOWN red
    const openUp = sm==null?'-':(sm*100).toFixed(1)+'¢';
    const openDown = sm==null?'-':((1-sm)*100).toFixed(1)+'¢';
    const delta = sm==null? '' : ((sm-0.50)*100).toFixed(1);
    const openHtml = sm==null?'-':`<div><span class="price-up" style="font-size:14px">${openUp} <span style="font-size:10px">אפ</span></span><span style="color:var(--faint);margin:0 4px">|</span><span class="price-down" style="font-size:14px">${openDown} <span style="font-size:10px">דאון</span></span></div><div style="font-size:10px;color:${Math.abs(sm-0.50)>=0.015?'var(--gold)':'var(--faint)'}">${delta>0?`+${delta}¢ מ-50`:`${delta}¢ מ-50`} ${Math.abs(sm-0.50)>=0.02?'⚠ לא 50':''}</div>`;
    // up column: UP high green big + DOWN low small red
    const upHigh = mx==null?'-':(mx*100).toFixed(1)+'¢';
    const upDownLow = mx==null?'-':((1-mx)*100).toFixed(1)+'¢';
    const upExc = (w.max_up*100).toFixed(1);
    const upHtml = mx==null?'<span style="color:var(--dim)">—</span>':`<div><span class="price-up" style="font-size:14px">${upHigh}</span> <span style="font-size:11px;color:var(--up)">+${upExc}¢</span></div><div class="price-down price-small">${upDownLow} דאון</div>`;
    // down column: DOWN high red big + UP low small green
    const downHigh = mn==null?'-':((1-mn)*100).toFixed(1)+'¢';
    const downUpLow = mn==null?'-':(mn*100).toFixed(1)+'¢';
    const downExc = (w.max_down*100).toFixed(1);
    const downHtml = mn==null?'<span style="color:var(--dim)">—</span>':`<div><span class="price-down" style="font-size:14px">${downHigh}</span> <span style="font-size:11px;color:var(--down)">+${downExc}¢</span></div><div class="price-up price-small">${downUpLow} אפ</div>`;
    // candle: UP side 1..99, open->close body, wick high-low
    const o = sm==null?50:sm*100, c = cm==null?o:cm*100, h = mx==null?o:mx*100, l = mn==null?o:mn*100;
    const lo = Math.min(o,c), hi = Math.max(o,c);
    const bodyLeft = Math.min(o,c), bodyW = Math.abs(c-o);
    const wickLeft = l, wickW = h-l;
    const bodyColor = c>=o ? 'var(--up)' : 'var(--down)';
    const candle = `<div class="candle-wrap"><div class="candle-bar"><div class="candle-wick" style="left:${wickLeft}%;width:${wickW}%;"></div><div class="candle-body" style="left:${bodyLeft}%;width:${Math.max(2,bodyW)}%;background:${bodyColor};border:1px solid ${bodyColor}"></div><div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--faint);opacity:.6"></div></div><div style="display:flex;justify-content:space-between;font-size:8px;color:var(--faint);margin-top:2px"><span>1</span><span>50</span><span>99</span></div><div style="font-size:10px;color:var(--dim);margin-top:1px">טווח ${((h-l)).toFixed(1)}¢ · סגירה ${(c).toFixed(1)}¢</div></div>`;
    tbl+=`<tr><td style="font-weight:700">${esc(w.label)}</td><td class="mono" title="${esc(w.slug)}" style="font-size:12px">${esc(w.slug.slice(-14))}<div style="font-size:10px;color:var(--faint)">${new Date(w.start_ts*1000).toLocaleTimeString('he-IL',{hour:'2-digit',minute:'2-digit'})}</div></td><td>${openHtml}</td><td>${upHtml}</td><td>${downHtml}</td><td>${candle}</td><td>${clsPill(w.class)}</td><td><a href="${w.url}" target="_blank" rel="noopener" style="font-size:13px;font-weight:700">פתח ↗</a></td></tr>`;
  }
  tbl+='</table><div class="note" style="font-size:12px"><b>התחיל ב</b> נמדד באמת — ה-mid הראשון שנדגם אחרי שהחלון נפתח (לכן לפעמים 47.5¢ ולא 50¢: החלון כבר רץ 10-20 שניות כשהתחלנו למדוד אותו). חלונות שייפתחו מעכשיו יתחילו קרוב ל-50¢. <b style="color:var(--up)">עלה ל</b> מודד מ-50: 50→60 = <span class="price-up">+10¢</span>, <b style="color:var(--down)">ירד ל</b> = 50→40 = <span class="price-down">−10¢</span>. הנר מראה פתיחה (קצה שמאלי של הגוף), סגירה (קצה ימין), ופתילות ל-high/low (בחרנו צד UP; DOWN הוא 100-UP).</div></div>';
  $('tables').innerHTML=tbl;
}
tick(); setInterval(tick,3000);
</script></body></html>
"""
@app.get("/", response_class=HTMLResponse)
@app.get("/oscillation", response_class=HTMLResponse)
def page():
    return HTMLResponse(PAGE, headers={"Cache-Control":"no-cache"})

