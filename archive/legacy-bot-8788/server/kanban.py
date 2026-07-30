"""Maker kanban page.

Same pipeline shape as the taker repo's server/kanban.py, with
maker-specific lanes (DECIDE -> REST -> FILL -> HOLD -> SETTLE) and
maker-specific metrics (fill rate, queue depth, spread capture vs adverse
selection, inventory balance, pair cost).
"""
from __future__ import annotations

PAGE = r"""
<style>
 :root{--bg:#0a0c0d;--pan:#121618;--pan2:#161b1e;--bd:#232a2e;--tx:#d6dbd8;
       --dim:#79847f;--am:#eda92c;--gn:#46c46a;--rd:#e2564f;--bl:#5b9bd5;
       --pu:#9b7fd4}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--tx);
      font:14.5px ui-monospace,SFMono-Regular,Menlo,monospace}
 .bar{display:flex;align-items:center;gap:12px;padding:7px 14px;
      border-bottom:1px solid var(--bd);background:var(--pan)}
 .bar b{color:var(--am);letter-spacing:1.4px;font-size:16px}
 .chip{border:1px solid var(--gn);color:var(--gn);padding:2px 9px;font-size:11.5px;letter-spacing:1.4px}

 /* ---------- sample-size bars (were broken: spans are inline, so width/height
    were ignored entirely and the bar never reflected progress) ---------- */
 .samp{display:flex;align-items:center;gap:18px;padding:7px 14px;
       border-bottom:1px solid var(--bd);background:#0d1113;flex-wrap:wrap}
 .lab{color:var(--dim);font-size:11.5px;letter-spacing:1.2px}
 .tgt{display:inline-flex;align-items:center;gap:7px;font-size:12.5px}
 .track{display:inline-block;width:150px;height:11px;background:#1b2124;
        border:1px solid var(--bd);position:relative;overflow:hidden;vertical-align:middle}
 .fillbar{display:block;height:100%;background:var(--bl);transition:width .6s ease}

 /* ---------- maker diagnostics ---------- */
 .diag{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
       gap:10px;margin:10px 0}
 .dg{background:#11161d;border:1px solid #1e2733;border-radius:8px;padding:10px 12px}
 .dg h3{margin:0 0 7px;font-size:10px;letter-spacing:.10em;color:#7d8899;font-weight:600}
 .dg table{width:100%;border-collapse:collapse;font-size:11px}
 .dg th{text-align:left;font-weight:500;color:#68727f;padding:2px 6px 4px 0;
        font-size:10px;border-bottom:1px solid #1e2733}
 .dg td{padding:3px 6px 3px 0;border-bottom:1px solid #161c24}
 .dg td.num{text-align:right;font-variant-numeric:tabular-nums}
 .dg .note{margin-top:7px;font-size:10px;color:#5d6673;line-height:1.4}
 /* NOTE: prefixed dg-. An earlier version named these .barw/.bar, but .bar was
    already the page header -- height:100% then stretched the header into a
    720px blue block that pushed the whole dashboard below the fold. */
 .dg-barw{display:inline-block;width:60px;height:5px;background:#1b222c;
       border-radius:3px;overflow:hidden;vertical-align:middle}
 .dg-bar{display:block;height:100%;background:#3f8cff}

 /* ---------- kpi strip ---------- */
 .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));
       gap:7px;padding:9px 12px;border-bottom:1px solid var(--bd)}
 .k{border:1px solid var(--bd);background:var(--pan);padding:6px 9px}
 .k .n{color:var(--dim);font-size:10.5px;letter-spacing:.8px}
 .k .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px}
 .k .s{color:var(--dim);font-size:11px}

 /* ---------- kanban ---------- */
 .kan{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;padding:10px 12px;
      align-items:start}
 @media(max-width:1250px){.kan{grid-template-columns:repeat(2,1fr)}}
 .lane{border:1px solid var(--bd);background:var(--pan);display:flex;
       flex-direction:column;min-height:150px}
 .lane h3{margin:0;padding:8px 11px;font-size:11.5px;letter-spacing:1.3px;
          border-bottom:1px solid var(--bd);display:flex;justify-content:space-between;
          align-items:center;font-weight:700}
 .lane .body{padding:6px;display:flex;flex-direction:column;gap:5px;
             max-height:520px;overflow-y:auto}
 .cnt{background:#1c2225;color:var(--dim);padding:1px 7px;border-radius:8px;font-size:10.5px}

 /* stage colours */
 .l1 h3{color:var(--dim)}   .l1{border-top:2px solid #3a4145}
 .l2 h3{color:var(--bl)}    .l2{border-top:2px solid var(--bl)}
 .l3 h3{color:var(--pu)}    .l3{border-top:2px solid var(--pu)}
 .l4 h3{color:var(--am)}    .l4{border-top:2px solid var(--am)}
 .l5 h3{color:var(--gn)}    .l5{border-top:2px solid var(--gn)}

 .card{background:var(--pan2);border:1px solid var(--bd);border-left:2px solid var(--bd);
       padding:7px 9px;font-size:12.5px;line-height:1.55}
 .card .top{display:flex;justify-content:space-between;gap:6px;align-items:baseline}
 .card .sub{color:var(--dim);font-size:11px}
 .card.up{border-left-color:var(--gn)} .card.dn{border-left-color:var(--rd)}
 .card.win{border-left-color:var(--gn)} .card.loss{border-left-color:var(--rd)}
 .card.skip{opacity:.62}
 .num{font-variant-numeric:tabular-nums}
 .g{color:var(--gn)}.r_{color:var(--rd)}.a{color:var(--am)}.d{color:var(--dim)}.bl{color:var(--bl)}.pu{color:var(--pu)}

 /* cards slide in from the previous lane as work advances */
 @keyframes flowin{
   0%{opacity:0;transform:translateX(-26px) scale(.97)}
   60%{opacity:1}
   100%{opacity:1;transform:none}
 }
 .enter{animation:flowin .55s cubic-bezier(.22,.9,.3,1)}
 @media (prefers-reduced-motion: reduce){ .enter{animation:none} }

 .note{color:var(--dim);font-size:11px;padding:6px 10px;line-height:1.5;
       border-top:1px solid var(--bd)}
 .livebar{display:flex;gap:18px;align-items:center;padding:7px 14px;
          border-bottom:1px solid var(--bd);background:var(--pan);flex-wrap:wrap}
</style>

<div class="bar">
  <b>MAKER_SIM</b><span class="d">·</span><span>BTC 5MIN</span>
  <span class="chip">PAPER · NO REAL ORDERS</span>
  <span id="live" class="d"></span>
  <span style="flex:1"></span><span id="clock" class="d"></span>
</div>
<div id="mkt" style="padding:9px 14px;border-bottom:1px solid var(--bd);
     background:#101619;font-size:14px;font-weight:600"></div>
<div class="samp" id="samp"></div>
<div class="livebar" id="livebar"></div>
<div id="exp" style="display:flex;align-items:center;gap:16px;padding:7px 14px;
     border-bottom:1px solid var(--bd);background:#0d1113;flex-wrap:wrap"></div>
<div id="explain" style="padding:10px 14px;border-bottom:1px solid var(--bd);
     background:#0b0f11;font-size:12.5px;line-height:1.75;color:#9fb0b5"></div>
<div class="kpis" id="kpis"></div>
<div class="diag" id="diag"></div>
<div class="kan" id="kan"></div>

<script>
const $=(x)=>document.getElementById(x);
const usd=(v,d=2)=>v==null?'—':(v<0?'-':'')+'$'+Math.abs(v).toFixed(d);
const pct=(v,d=1)=>v==null?'—':(v*100).toFixed(d)+'%';
const num=(v,d=0)=>v==null?'—':Number(v).toFixed(d);
const cls=(v)=>v==null?'':(v>=0?'g':'r_');
const hhmm=(t)=>t?new Date(t*1000).toLocaleTimeString():'—';
const seen={};                       // lane -> Set of ids already rendered

function lane(id,title,cls_,cards,note){
  const s=seen[id]=seen[id]||new Set();
  const html=cards.map(c=>{
    const isNew=!s.has(c.key); s.add(c.key);
    return `<div class="card ${c.cls||''} ${isNew?'enter':''}">${c.html}</div>`;
  }).join('');
  return `<div class="lane ${cls_}"><h3><span>${title}</span>
    <span class="cnt">${cards.length}</span></h3>
    <div class="body">${html||'<div class="d" style="padding:6px">—</div>'}</div>
    ${note?`<div class="note">${note}</div>`:''}</div>`;
}

function sampleBar(s){
  const sm=s.sample||{}, n=sm.n||0;
  if(!sm.targets||!Object.keys(sm.targets).length)
    return `<span class="lab">SAMPLE</span><span>${n} settled · need ≥2 to estimate</span>`;
  let h=`<span class="lab">SAMPLE SIZE</span><span><b class="bl">${n}</b> settled</span>
         <span class="d">mean ${usd(sm.mean)}/mkt · σ ${usd(sm.stdev)}</span>`;
  for(const [lvl,t] of Object.entries(sm.targets)){
    const need=t.need, prog=need?Math.min(100,100*n/need):0;
    h+=`<span class="tgt"><span class="d">${lvl}</span>
      <span class="track"><span class="fillbar" style="width:${prog.toFixed(1)}%;
        background:${t.reached?'var(--gn)':'var(--bl)'}"></span></span>
      <span class="${t.reached?'g':''}">${t.reached?'REACHED':n+'/'+(need==null?'∞':need)}</span>
      ${t.reached?'':`<span class="d">${t.eta_hours==null?'':'('+num(t.eta_hours,0)+'h)'}</span>`}</span>`;
  }
  return h;
}

async function tick(){
  let s; try{ s=await (await fetch('/api/state',{cache:'no-store'})).json(); }catch(e){ return; }
  $('clock').textContent=new Date().toLocaleTimeString();
  if(s.error){ $('kan').innerHTML='<div class="lane"><h3>ERROR</h3><div class="body">'+s.error+'</div></div>'; return; }
  const c=s.config||{}, L=s.live||{}, inv=L.inventory||{};
  // Market identity in the title bar. With four bots running side by side on
  // four ports, an unlabelled tab is unreadable -- you cannot tell which
  // market a number belongs to.
  if(c.market_title){
    document.title=c.market_title.slice(0,60);
    const link=c.market_url
      ? `<a href="${c.market_url}" target="_blank" style="color:var(--am);text-decoration:none">${c.market_title} ↗</a>`
      : c.market_title;
    $('mkt').innerHTML=`${link}<span class="chip" style="margin-left:10px">`
      + `PAYS $${(c.market_daily_rate||0).toFixed(0)}/DAY FOR RESTING</span>`;
  }
  const alive=(L._age!=null&&L._age<15);
  $('live').textContent=alive?'● bot running':'● bot idle';
  $('live').className=alive?'g':'r_';
  $('samp').innerHTML=sampleBar(s);

  /* ---- what the bot is doing, in plain words ----
     Replaces the old CENSUS banner. That banner tracked "does a sub-$1.00 pair
     exist at the touch", which was answered NO over 60 markets and is no
     longer the strategy. The bot now gets paid for leaving offers resting on
     the board, so the numbers that matter are: are we on the board, are we on
     both sides, and how big is our slice. */
  const R=s.rewards||{};
  const upt=R.uptime, shr=R.avg_share, two=R.two_sided_rate;
  // NOT `bar` -- that name is already taken further down this same function
  // scope, and a duplicate const is a PARSE error, which kills the entire
  // script tag and renders a blank page.
  const rbar=(v,good)=>`<span class="track"><span class="fillbar" style="width:${
      Math.min(100,100*(v||0)).toFixed(0)}%;background:${v>=good?'var(--gn)':'var(--am)'}"></span></span>`;
  $('exp').innerHTML =
    // Banner tells the truth per market: these funded markets DO pay for
    // resting, btc-updown-5m did not. Driven by the venue's own daily rate.
      (R.pays_for_resting
        ? `<span style="color:#46c46a;font-size:14px;font-weight:700">● PAID TO WAIT</span>`
          + `<span class="d">this market funds $${num(R.market_daily_rate)}/day for resting offers · `
          + `paid daily whether anyone takes them or not</span>`
          + `<span class="lab">OUR EST. SHARE</span>`
          + `<span class="g">${usd(R.est_resting_usd_per_day)}/day</span>`
        : `<span style="color:#e05c5c;font-size:14px;font-weight:700">● THIS MARKET DOES NOT PAY FOR WAITING</span>`
          + `<span class="d">rewards.rates=null here · resting earns $0</span>`)
    + `<span class="lab">ON THE BOARD</span>${rbar(upt,0.8)}`
    + `<span class="${(upt||0)>=0.8?'g':'a'}">${upt==null?'—':(upt*100).toFixed(0)+'%'}</span>`
    + `<span class="d">of the time (0% = earning nothing)</span>`
    + `<span class="lab">BOTH SIDES</span>`
    + `<span class="${(two||0)>=0.9?'g':'a'}">${two==null?'—':(two*100).toFixed(0)+'%'}</span>`
    + `<span class="d">(one side only pays 1/3 as much)</span>`
    + `<span class="lab">OUR SLICE</span>`
    + `<span class="bl">${shr==null?'—':(shr*100).toFixed(2)+'%'}</span>`
    + `<span class="d">of everyone's offers · sitting ${num(R.offset_cents,1)}¢ back from fair price</span>`;

  /* ---- how to read this page ---- */
  $('explain').innerHTML =
      `<b>What the bot is doing.</b> A market says "will Bitcoin be up or down in 5 minutes?" `
    + `You can bet YES or NO. The bot does not try to guess the answer. It just posts offers to buy `
    + `both sides slightly below the going price, and leaves them sitting there. `
    + `<b>Polymarket pays makers a daily cut just for having offers on the board</b> — money arrives `
    + `even if nobody ever takes the offer. That payment is the plan.`
    + `<br><b>Why the old plan failed.</b> It used to shop just under the <i>seller's</i> price, which is `
    + `above the fair middle, so buying both sides always cost about $1.01 for something that pays back `
    + `exactly $1.00. Guaranteed loss. It also sat out 7 of every 10 chances, waiting for perfect trades `
    + `that never came — and earned nothing while waiting. Now it shops from the <i>middle</i> price, so `
    + `both sides together cost about 96¢, and it stays on the board almost always.`
    + `<br><b>The one risk.</b> Leaving an offer up means people can take it — and they usually take it `
    + `when they know something we don't. That costs money. So watch these two numbers against each other:`
    + `<br>&nbsp;&nbsp;• <span class="g">REWARDS EARNED</span> — money for waiting. Should climb steadily.`
    + `<br>&nbsp;&nbsp;• <span class="r_">ADVERSE SELECTION</span> — money lost getting picked off.`
    + `<br><b>Rewards bigger → we're winning</b>, and we move closer to the middle to earn more. `
    + `<b>Losses bigger → back off</b> further from the middle. A few hours tells us which.`;

  /* ---- live market strip ---- */
  const u=L.up||{},d=L.down||{};
  $('livebar').innerHTML = L.market_slug ? `
    <span class="lab">LIVE</span>
    <a href="https://polymarket.com/event/${L.market_slug}" target="_blank"
       style="color:var(--am);text-decoration:none">${L.market_slug} ↗</a>
    <span class="a" style="font-size:20px;font-weight:700">${num(Math.max(0,L.t_remaining))}s</span>
    <span class="d">UP</span><span class="g">${u.best_bid==null?'—':u.best_bid.toFixed(2)}</span>
      <span class="d">/</span><span class="a">${u.best_ask==null?'—':u.best_ask.toFixed(2)}</span>
    <span class="d">DOWN</span><span class="g">${d.best_bid==null?'—':d.best_bid.toFixed(2)}</span>
      <span class="d">/</span><span class="a">${d.best_ask==null?'—':d.best_ask.toFixed(2)}</span>
    <span style="flex:1"></span>
    <span class="d">our book</span>
    <span class="g">UP ${num(inv.up_shares)}@${num(inv.up_avg,3)}</span>
    <span class="r_">DOWN ${num(inv.down_shares)}@${num(inv.down_avg,3)}</span>
    <span class="d">pair</span><span class="${(inv.pair_cost||9)<1?'g':'d'}">${num(inv.pair_cost,4)}</span>
    <span class="d">balance</span><span class="${(inv.balance||0)>=c.target_balance?'g':'a'}">${num(inv.balance,2)}</span>`
    : '<span class="d">waiting for the bot…</span>';

  /* ---- kpi strip ---- */
  const K=(n,v,sub,cl)=>`<div class="k"><div class="n">${n}</div>
      <div class="v ${cl||''}">${v}</div><div class="s">${sub||''}</div></div>`;
  // The two cards that decide the strategy come FIRST: money earned for
  // waiting, versus money lost getting picked off. Everything after them is
  // supporting detail.
  // The pool-guess estimate (est_usd_per_window x windows) was computed here.
  // It fed the retracted "$46/hr" tile and nothing else, so it is deleted
  // rather than left sitting one uncommented line away from being displayed.
  $('kpis').innerHTML =
    // Was "REWARDS EARNED $46/hr", multiplying our resting-score share by an
    // assumed pool. Fiction: this market has rewards.rates=null, so resting
    // pays nothing, and the rebate it DOES have is paid on matched volume.
    // Now shows money actually earned on fills, which is currently $0.00.
      K('PAID TO WAIT',R.pays_for_resting?usd(R.est_resting_usd_per_day)+'/day':'$0.00',
        R.pays_for_resting?'our slice of $'+num(R.market_daily_rate)+'/day funded'
                          :'this market funds nothing','g')
    + K('REBATE ON FILLS',usd(R.rebate_earned),
        R.rebate_per_share_cents==null?'paid only when an offer is taken'
        :num(R.rebate_per_share_cents,3)+'¢/share filled','g')
    + K('ADVERSE SELECTION',usd(s.adverse_selection),'cost of being picked off',
        cls(s.adverse_selection))
    + K('ON THE BOARD',pct(R.uptime),'0% earns nothing',(R.uptime||0)>=0.8?'g':'a')
    + K('OUR SLICE',pct(R.avg_share,2),'of everyone’s resting offers','bl')
    + K('EQUITY',usd(s.equity),'from '+usd(s.bankroll),cls(s.realized_pnl))
    + K('REALIZED P&L',usd(s.realized_pnl),pct(s.roi_on_cost,2)+' of turnover',cls(s.realized_pnl))
    + K('SPREAD CAPTURE',usd(s.spread_capture),num(s.avg_edge_cents,2)+'¢ avg edge','g')
    + K('FILL RATE',pct(s.fill_rate),'a fill is a side effect now','bl')
    + K('BALANCE',num(s.median_balance,3),'target '+c.target_balance,
        (s.median_balance||0)>=c.target_balance?'g':'a')
    + K('HEDGE X', String(s.balance_hedges||0), inv>0?'settlement crossings':'',
        (s.balance_hedges||0)>0?'w':'g_')
    + K('PAIR COST',num(s.median_pair_cost,4),'pays $1.00',
        (s.median_pair_cost||9)<1?'g':'r_')
    + K('WIN RATE',pct(s.win_rate),s.wins+'W / '+s.losses+'L')
    // HEDGE FILLABLE and MED PAIR@TOUCH lived here. Both measured the old
    // "buy a cheap pair" plan, which was answered NO over 60 markets. Replaced
    // by the two numbers that steer the current plan.
    + K('BOTH SIDES UP',pct(R.two_sided_rate),'one side pays 1/3 as much',
        (R.two_sided_rate||0)>=0.9?'g':'a')
    + K('DISTANCE BACK',num(R.offset_cents,1)+'¢','from fair price · closer = more pay','bl')
    + K('REBATE (est)',usd(s.rebate_est),'not counted in P&L','a')
    + K('TAKER FEES',usd(-(s.taker_fees_paid||0)),
        num(s.crossed_shares)+' sh crossed to hedge',
        (s.taker_fees_paid||0)>0?'r_':'g_')
    + K('QUOTE UPTIME',pct(s.quote_uptime),'cycles with a live quote',
        (s.quote_uptime||0)>0.5?'g':'a')
    + K('PARTIAL FILLS',String(s.partial_quotes||0),
        num(s.partial_fill_shares_missing)+' sh never filled',
        (s.partial_quotes||0)>0?'a':'g_');

  /* ---- maker diagnostics: fill rate vs queue depth, fill provenance, pair
     cost distribution, skip reasons. Every row is aggregated straight from
     quotes/fills/decisions -- nothing here is modelled or carried over. ---- */
  const bar=(f)=>{const w=Math.round(100*(f||0));
    return `<span class="dg-barw"><span class="dg-bar" style="width:${w}%"></span></span>`;};
  const fq=(s.fill_by_queue||[]).map(b=>
    `<tr><td>${b.label} sh</td><td class="num">${b.quotes}</td>
     <td class="num">${num(b.posted)}</td>
     <td class="num ${(b.fill_rate||0)>0.1?'g':'a'}">${pct(b.fill_rate)}</td>
     <td>${bar(b.fill_rate)}</td></tr>`).join('')
    || '<tr><td colspan="5" class="d">no quotes yet</td></tr>';

  /* Provenance matters more than the headline number: a rate carried by
     'sweep' is inferred (a mass cancel looks identical to a mass trade),
     while 'tape' is confirmed volume printed at our price. */
  const PROV={tape:['tape-confirmed','g'],queue:['book delta past queue','bl'],
              sweep:['level emptied — UNVERIFIED','a'],cross:['we took liquidity','w']};
  const pv=Object.entries(s.fill_provenance||{}).map(([k,v])=>
    `<tr><td class="${(PROV[k]||['',''])[1]}">${k}</td>
     <td class="num">${num(v)} sh</td>
     <td class="d">${(PROV[k]||['—'])[0]}</td></tr>`).join('')
    || '<tr><td colspan="3" class="d">no fills yet</td></tr>';

  const pcd=(s.pair_cost_distribution||[]);
  const under=pcd.filter(p=>p<1).length;
  const sk=(s.top_skip_reasons||[]).map(r=>
    `<tr><td class="d">${(r.reason||'').slice(0,54)}</td>
     <td class="num">${r.cycles}</td></tr>`).join('')
    || '<tr><td colspan="2" class="d">—</td></tr>';

  $('diag').innerHTML =
    `<div class="dg"><h3>FILL RATE vs QUEUE AHEAD</h3>
      <table><tr><th>queue at post</th><th>quotes</th><th>posted</th>
      <th>fill</th><th></th></tr>${fq}</table>
      <div class="note">If fills only land in the shallow buckets, the problem
        is queue position, not the strategy.</div></div>
     <div class="dg"><h3>WHERE FILLS CAME FROM</h3>
      <table><tr><th>source</th><th>shares</th><th>meaning</th></tr>${pv}</table>
      <div class="note">'sweep' is inferred, not observed. A fill rate built on
        it is an upper bound, not a measurement.</div></div>
     <div class="dg"><h3>PAIR COST</h3>
      <table><tr><th>markets with both legs</th><td class="num">${pcd.length}</td></tr>
      <tr><th>under $1.00</th><td class="num ${under===pcd.length?'g':'a'}">${under}</td></tr>
      <tr><th>median</th><td class="num">${num(s.median_pair_cost,4)}</td></tr>
      <tr><th>spread capture / share</th><td class="num">${num(100*(s.spread_capture_per_share||0),2)}¢</td></tr></table>
      <div class="note">The pair pays exactly $1.00, so anything at or above
        that is a guaranteed loss.</div></div>
     <div class="dg"><h3>WHY WE DIDN'T QUOTE</h3>
      <table><tr><th>reason</th><th>cycles</th></tr>${sk}</table>
      <div class="note">Uptime ${pct(s.quote_uptime)} — a maker that is off the
        book cannot be filled.</div></div>`;

  /* ---- kanban: DECIDE -> REST -> FILL -> HOLD -> SETTLE ---- */
  const decide=(s.decisions||[]).slice(0,14).map(x=>({
    key:'d'+x.id, cls:(x.action==='QUOTE'?'':'skip'),
    html:`<div class="top"><span class="${x.action==='QUOTE'?'bl':'d'}">${x.action}${x.count>1?' <span class="d">×'+x.count+'</span>':''}</span>
      <span class="d">${hhmm(x.ts)}</span></div>
      <div class="sub">${(x.reason||'').slice(0,42)}</div>`}));

  const rest=(L.open_quotes||[]).map((q,i)=>({
    key:'q'+q.side+q.price, cls:(q.side==='UP'?'up':'dn'),
    html:`<div class="top"><span class="${q.side==='UP'?'g':'r_'}">${q.side} @ ${q.price.toFixed(2)}</span>
      <span class="num">${num(q.size)} sh</span></div>
      <div class="sub">queue ahead <span class="${q.queue_ahead>0?'a':'g'}">${num(q.queue_ahead)}</span>
      · filled ${num(q.filled)}</div>`}));

  const fills=(s.recent_fills||[]).slice(0,14).map(f=>({
    key:'f'+f.id, cls:(f.side==='UP'?'up':'dn'),
    html:`<div class="top"><span class="${f.side==='UP'?'g':'r_'}">${f.side} @ ${(f.price||0).toFixed(2)}</span>
      <span class="num">${num(f.size)} sh</span></div>
      <div class="sub" style="display:flex; height:4px; margin:4px 0; background:#1b2124;">
        <div style="width:50%; display:flex; justify-content:flex-end;">
          <div style="height:100%; width:${f.edge_vs_mid < 0 ? Math.min(100, Math.abs(f.edge_vs_mid * 100) * 20) : 0}%; background:var(--rd);"></div>
        </div>
        <div style="width:50%; display:flex; justify-content:flex-start;">
          <div style="height:100%; width:${f.edge_vs_mid > 0 ? Math.min(100, (f.edge_vs_mid * 100) * 20) : 0}%; background:var(--gn);"></div>
        </div>
      </div>
      <div class="sub">edge <span class="${(f.edge_vs_mid||0) >= 0 ? 'g' : 'r_'}">${f.edge_vs_mid==null?'—':(f.edge_vs_mid*100).toFixed(2)+'¢'}</span>
      · waited ${num(f.queue_waited)} sh · ${hhmm(f.ts)}</div>`}));

  const hold=[];
  if(inv.fills){
    const risk=(inv.up_shares||0)-(inv.down_shares||0);
    hold.push({key:'h'+(L.condition_id||''),cls:(inv.balance>=c.target_balance?'win':''),
      html:`<div class="top"><span class="a">${(L.market_slug||'').slice(-8)}</span>
        <span class="num">${num(inv.fills)} fills</span></div>
        <div class="sub" style="display:flex; height:6px; margin:4px 0; background:#1b2124; border-radius:2px; overflow:hidden;">
          <div style="height:100%; width:${((inv.up_shares||0) / ((inv.up_shares||0) + (inv.down_shares||0) || 1)) * 100}%; background:var(--gn);"></div>
          <div style="height:100%; width:${((inv.down_shares||0) / ((inv.up_shares||0) + (inv.down_shares||0) || 1)) * 100}%; background:var(--rd);"></div>
        </div>
        <div class="sub">UP ${num(inv.up_shares)} · DOWN ${num(inv.down_shares)}</div>
        <div class="sub">pair <span class="${(inv.pair_cost||9)<1?'g':'d'}">${num(inv.pair_cost,4)}</span>
          · balance <span class="${(inv.balance||0)>=c.target_balance?'g':'a'}">${num(inv.balance,2)}</span></div>
        <div class="sub">unhedged <span class="${Math.abs(risk)>60?'r_':'d'}">${num(Math.abs(risk))} sh</span>
          · cost ${usd(inv.cost,0)}</div>`});
  }

  const settle=(s.settlements||[]).slice(0,14).map(x=>({
    key:'s'+x.slug, cls:(x.pnl>=0?'win':'loss'),
    html:`<div class="top"><span class="d">…${(x.slug||'').slice(-8)}</span>
      <span class="${x.pnl>=0?'g':'r_'}" style="font-weight:700">${x.pnl>=0?'+':''}${usd(x.pnl)}</span></div>
      <div class="sub">UP ${num(x.up_sh)} / DN ${num(x.dn_sh)} · bal ${num(x.balance,2)}</div>
      <div class="sub" style="margin-top:6px; display:flex; flex-direction:column; gap:3px;">
        <div style="display:flex; align-items:center; gap:6px;">
          <span style="width:25px;">Cost</span>
          <div style="flex:1; height:5px; background:#1b2124; border-radius:1px;">
            <div style="height:100%; width:${((x.cost||0) / (Math.max(x.cost||0, x.payout||0) || 1)) * 100}%; background:var(--dim);"></div>
          </div>
          <span class="num" style="width:35px; text-align:right;">${usd(x.cost,0)}</span>
        </div>
        <div style="display:flex; align-items:center; gap:6px;">
          <span style="width:25px;">Paid</span>
          <div style="flex:1; height:5px; background:#1b2124; border-radius:1px;">
            <div style="height:100%; width:${((x.payout||0) / (Math.max(x.cost||0, x.payout||0) || 1)) * 100}%; background:${x.pnl>=0?'var(--gn)':'var(--rd)'};"></div>
          </div>
          <span class="num" style="width:35px; text-align:right;">${usd(x.payout,0)}</span>
        </div>
      </div>`}));

  $('kan').innerHTML =
      lane('l1','① DECIDE','l1',decide,'why we quote or skip')
    + lane('l2','② REST ON BOOK','l2',rest,'our bids waiting in the queue')
    + lane('l3','③ FILL','l3',fills,'someone traded against us')
    + lane('l4','④ HOLD','l4',hold,'position carried into resolution')
    + lane('l5','⑤ SETTLE','l5',settle,'market resolved · $1.00 or $0.00');
}
tick(); setInterval(tick,2000);
</script>
"""