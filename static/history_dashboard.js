

window.addEventListener('error',function(ev){const el=document.getElementById('updated');if(el)el.textContent='Overview JavaScript error: '+(ev.message||'script error');});
window.addEventListener('unhandledrejection',function(ev){const el=document.getElementById('updated');if(el)el.textContent='Overview promise error: '+(ev.reason&&ev.reason.message||ev.reason||'unknown');});
function setSnapshotIndicator(state){
 const el=document.getElementById('updated');if(el)el.dataset.snapshotState=state;
 const orb=document.getElementById('deck-status-orb');if(orb)orb.dataset.snapshotState=state;
 const label=document.getElementById('deck-health-label');
 if(label)label.textContent=({fresh:'SAVED SCAN · RECENT',old:'SAVED SCAN · AGED',error:'SAVED SCAN · UNAVAILABLE'})[state]||'SAVED SCAN · CONNECTING';
 if(state==='error'){const stamp=document.getElementById('deck-snapshot-at');if(stamp)stamp.textContent='Unavailable';}
}
const bootStatus=document.getElementById('updated');if(bootStatus)bootStatus.textContent='Dashboard script started · loading saved history…';
(async function loadHistoryDashboard(){
 const status=document.getElementById('updated');
 const controller=new AbortController();
 const timeout=setTimeout(()=>controller.abort(),25000);
 try{
  const response=await fetch('/history/overview-data',{cache:'no-store',signal:controller.signal});
  if(!response.ok)throw Error('History request failed: HTTP '+response.status);
  const data=await response.json();
  if(!data||typeof data.paper_tracker!=='object'||data.paper_tracker===null)throw Error('History API returned an invalid candidate ledger');
  const recordCount=Object.keys(data.paper_tracker).length;
  // Update and paint the connection state before the heavier DOM/table rendering work.
  if(status)status.textContent='Saved history received · preparing '+recordCount+' tracked records…';
  await new Promise(resolve=>requestAnimationFrame(()=>resolve()));
  try{
   render(data);
   renderScanReceipt(data);
   const snapshotMs=Date.parse(data.last_scan_completed_at||'');
   setSnapshotIndicator(Number.isFinite(snapshotMs)&&snapshotMs<=Date.now()&&Date.now()-snapshotMs<=45*60*1000?'fresh':'old');
   const snapshotTime=data.last_scan_completed_at?new Intl.DateTimeFormat('en-US',{timeZone:'America/Detroit',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(data.last_scan_completed_at)):'timestamp unavailable';
   if(status)status.textContent='Dashboard loaded · '+recordCount+' tracked records · Data snapshot: '+snapshotTime;
    const deckStamp=document.getElementById('deck-snapshot-at');if(deckStamp)deckStamp.textContent=snapshotTime;
  }catch(err){
   console.error('Jarvis history render error:',err);
   setSnapshotIndicator('error');
   if(status)status.textContent='Dashboard rendering failed: '+(err&&err.message||err);
  }
 }catch(err){
  console.error('Jarvis history data load error:',err);
  setSnapshotIndicator('error');
  if(status)status.textContent=(err&&err.name==='AbortError')?'History data timed out · reload to retry':'History data load failed: '+(err&&err.message||err);
  const pick=document.getElementById('action-top-pick');
  if(pick)pick.innerHTML='<div><div class="action-pick-title">TOP RADAR CANDIDATE</div><div class="action-pick-symbol">Dashboard data unavailable</div><div class="action-pick-reason">The saved scan could not be loaded. Reload to retry; no trade signal is implied.</div></div><span class="action-pick-status">DATA UNAVAILABLE</span>';
 }finally{clearTimeout(timeout);}
})()
const status=document.getElementById('updated');if(status)status.textContent='Dashboard loading…';
const n=v=>Number(v||0),hasPct=v=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v)),pct=v=>hasPct(v)?Number(v).toFixed(2)+'%':'—',cl=v=>!hasPct(v)?'':n(v)>0?'pos':n(v)<0?'neg':'',e=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;"),date=v=>v?new Date(v).toLocaleString():'—';
const rank={'BUY':8,'EARLY SETUP':6,'DEVELOPING RUNNER':5,'DEVELOPING SETUP':5,'ACTIONABLE SETUP':4,'EARLY WATCH':3,'LATE RUNNER — CHASE RISK':2,'LOW-CONVICTION WATCH':1};
const PREF_KEY='jarvisDashboardPrefsV1';const prefs={radarFilter:'all',assetFilter:'all',density:'comfortable',showTimeline:true,showPerformance:true,accent:'#4aa3df',font:'Inter,system-ui,sans-serif',fontSize:'14'};try{Object.assign(prefs,JSON.parse(localStorage.getItem(PREF_KEY)||'{}'));}catch(_){}
function applyPrefs(){document.body.classList.toggle('compact-mode',prefs.density==='compact');document.documentElement.style.setProperty('--accent',prefs.accent||'#4aa3df');document.body.style.fontFamily=prefs.font||'Inter,system-ui,sans-serif';document.body.style.fontSize=(Number(prefs.fontSize)||14)+'px';const timeline=document.getElementById('radar-timeline'),archive=document.getElementById('performance-archive');if(timeline)timeline.hidden=!prefs.showTimeline;if(archive)archive.hidden=!prefs.showPerformance;}
document.addEventListener('DOMContentLoaded',applyPrefs);


function elapsed(from,to){if(!from||!to)return '—';const ms=new Date(to)-new Date(from);if(!Number.isFinite(ms)||ms<0)return '—';const hours=Math.floor(ms/3600000);return hours<24?hours+'h':Math.floor(hours/24)+'d '+Math.floor(hours%24)+'h';}
const easternDateFormat=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'});
const etStamp=v=>v?easternDateFormat.format(new Date(v)):'—';

// Logo-based, read-only chart links. Ordinary HTTPS links let Android open the
// installed app when the user has enabled its supported app links.
function candidateChartUrl(x){
 const asset=String(x.asset_type||'').trim().toLowerCase();
 let symbol=String(x.symbol||'').trim().toUpperCase();
 if(!/^[A-Z0-9][A-Z0-9.\^\/-]{0,22}$/.test(symbol))return '';
 if(asset==='crypto'){
  symbol=symbol.replace(/[-\/]?(USDT|USDC|USD)$/,'');
  symbol=({XBT:'BTC',XDG:'DOGE'})[symbol]||symbol;
  if(!/^[A-Z0-9]{2,20}$/.test(symbol))return '';
  const quote=String(x.coinbase_quote_currency||'').toUpperCase()==='USDC'?'USDC':'USD';
  return 'https://www.coinbase.com/advanced-trade/spot/'+encodeURIComponent(symbol+'-'+quote);
 }
 if(asset==='stock'){
  const exchange=String(x.listing_exchange||x.primary_exchange||x.exchange||'').trim().toUpperCase();
  const prefix={NASDAQ:'NASDAQ',NYSE:'NYSE',AMEX:'AMEX',ARCA:'AMEX',NYSEARCA:'AMEX'}[exchange]||'';
  // A symbol page is a stable candidate-specific destination; Android may open
  // the installed TradingView app, but it decides whether to honor this path.
  return 'https://www.tradingview.com/symbols/'+(prefix?prefix+'-':'')+encodeURIComponent(symbol)+'/';
 }
 return '';
}
function candidateChartLogo(x){
 const url=candidateChartUrl(x);
 if(!url)return '';
 const crypto=String(x.asset_type||'').toLowerCase()==='crypto';
 const name=crypto?'Coinbase':'TradingView';
 const logo=crypto?'<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M12.95 22c-4.503 0-8.445-3.04-9.61-7.413-1.165-4.373.737-8.988 4.638-11.25a9.906 9.906 0 0 1 12.008 1.598l-3.335 3.367a5.185 5.185 0 0 0-7.354.013 5.252 5.252 0 0 0 0 7.393 5.185 5.185 0 0 0 7.354.013l3.349 3.367a9.887 9.887 0 0 1-7.05 2.912"/></svg>':'<svg aria-hidden="true" viewBox="0 0 24 24" fill="currentColor"><path d="M15.8654 8.2789c0 1.3541-1.0978 2.4519-2.452 2.4519-1.354 0-2.4519-1.0978-2.4519-2.452 0-1.354 1.0978-2.4518 2.452-2.4518 1.3541 0 2.4519 1.0977 2.4519 2.4519zM9.75 6H0v4.9038h4.8462v7.2692H9.75Zm8.5962 0H24l-5.1058 12.173h-5.6538z"/></svg>';
 return '<a class="op-chart-link '+(crypto?'coinbase':'tradingview')+'" href="'+e(url)+'" target="_blank" rel="noopener noreferrer" aria-label="View '+e(x.symbol||'candidate')+' chart on '+name+'" title="Open on '+name+' (app if supported)">'+logo+'<span>'+name+' chart</span></a>';
}

// SETUP QUALITY READINESS HELPER (research display only; official BUY rules are upstream).
// A 100/100 Setup Quality score is reserved for a fresh, independently verified,
// active official BUY signal. This is gate completion, not probability of profit.
function setupQualityReadiness(x, activeBuys, scanAgeMinutes){
 const readiness=x&&x.buy_readiness;
 const passed=readiness&&readiness.passed, total=readiness&&readiness.total;
 const valid=Number.isInteger(passed)&&Number.isInteger(total)&&total>0&&passed>=0&&passed<=total;
 if(!valid)return {score:null,label:'BUY CHECKS UNAVAILABLE',detail:'Saved BUY readiness is missing; technical evidence does not authorize a BUY.',isBuy:false,failedKeys:[]};
 const asset=String(x.asset_type||'').toLowerCase(),symbol=String(x.symbol||'').toUpperCase();
 const failedKeys=Array.isArray(readiness.failed_gate_keys)?readiness.failed_gate_keys.map(String).slice(0,16):[];
 const allGates=readiness.all_gates_passed===true&&passed===total&&failedKeys.length===0;
 const active=Array.isArray(activeBuys)&&activeBuys.some(s=>s.status==='active'&&String(s.asset_type||'').toLowerCase()===asset&&String(s.symbol||'').toUpperCase()===symbol);
 const verified=x.price_validation_status==='verified'&&x.data_quality&&x.data_quality.buy_eligible===true&&String(x.data_quality.data_type||'').toLowerCase()==='live';
 const recent=typeof scanAgeMinutes==='number'&&Number.isFinite(scanAgeMinutes)&&scanAgeMinutes>=0&&scanAgeMinutes<=20;
 const stageBuy=String(x.last_stage||'').toUpperCase()==='BUY';
 const isBuy=allGates&&stageBuy&&active&&verified&&recent;
 // A partial, inactive or stale setup never gets 100 even if all recorded
 // checks passed; it is not a current actionable BUY.
 const score=isBuy?100:Math.min(99,Math.round(passed/total*100));
 const failures=total-passed;
 let label,reason;
 if(isBuy){
  label='BUY SIGNAL · ENTRY REVIEW';
  reason='All '+total+' required BUY gates passed in a fresh scan with an active official signal. Confirm the live quote, spread, stop and order yourself.';
 }else if(!verified){
  label='WAIT · LIVE PRICE REQUIRED';
  reason='No independently verified BUY-eligible live price. '+passed+'/'+total+' saved BUY checks passed.';
 }else if(!recent){
  label='WAIT · SCAN OUTDATED';
  reason='Last scan is older than 20 minutes or its time is unavailable. Recheck before acting.';
 }else if(failures>0||!allGates){
  label='WAIT · '+failures+' BUY CHECK'+(failures===1?'':'S')+' MISSING';
  const gateNames={score_82:'Score 82+',volume_confirmed:'Volume',breakout_confirmed:'Breakout',two_technical_confirmations:'2 technical confirmations',
   market_not_risk_off:'Risk-Off regime',fresh_intraday_confirmation:'Fresh intraday',five_bar_intraday_trend_up:'Five-bar trend',
   no_risk_flags:'Risk flags',not_chase_risk:'Chase risk',price_verified:'Price verification',live_data_quality:'Live data'};
  const missing=failedKeys.map(key=>gateNames[key]||key.replaceAll('_',' ')).slice(0,3);
  reason=passed+'/'+total+' required BUY checks passed'+(missing.length?'. Missing: '+missing.join(', '):'.')+'.';
 }else{
  label='WAIT · NO ACTIVE BUY SIGNAL';
  reason='Saved BUY checks are complete, but the scanner has no matching active official BUY signal or BUY stage.';
 }
 return {score,label,detail:reason,isBuy,failedKeys,passed,total};
}
// END SETUP QUALITY READINESS HELPER

function priceTiming(x){const lastKnown=x.price_validation_status==='last_known'||x.price_timestamp_type==='daily_session_close';const sessionDate=x.price_session_date||(lastKnown&&x.price_as_of?new Date(x.price_as_of).toISOString().slice(0,10):null);const priceTime=lastKnown?(sessionDate?sessionDate+' · regular close scheduled 4:00 PM ET':'Daily session close · date unavailable'):etStamp(x.price_as_of);const checked=etStamp(x.price_checked_at||x.last_seen||(x.data_quality&&x.data_quality.received_at));const note=lastKnown?'Daily-bar source provides the session date, not the exact final-trade timestamp; early closes can differ.':'Exchange/provider timestamp';const delayed=x.data_quality&&x.data_quality.data_type==='delayed';const status=lastKnown?'LAST KNOWN · EOD':x.price_validation_status==='verified'?(delayed?'VERIFIED · DELAYED':'LIVE VERIFIED'):'UNVERIFIED';const baseStatus=x.discovery_baseline_status==='last_known_eod'?'LAST KNOWN EOD':x.discovery_baseline_status==='live_verified'?'LIVE VERIFIED':x.discovery_baseline_status==='first_verified_legacy'?'LEGACY FIRST-VERIFIED':x.discovery_baseline_status==='first_verified_fallback'?'FIRST VERIFIED FALLBACK':x.discovery_baseline_status==='unverified_at_discovery'?'FIRST CAPTURE · PROVISIONAL':'AWAITING RELIABLE PRICE';const baseTime=x.discovery_baseline_timestamp_type==='daily_session_close'?String(x.discovery_price_as_of||'').slice(0,10):etStamp(x.discovery_price_as_of);return '<div class="tiny"><b>'+e(status)+'</b></div><div class="tiny">PRICE TIME: '+e(priceTime)+'</div><div class="tiny">JARVIS CHECKED: '+e(checked)+'</div><div class="tiny">DISCOVERY BASELINE: '+e(baseStatus)+' · '+e(baseTime||'date unavailable')+'</div><div class="tiny">'+e(note)+'</div>';}function render(d){const t=d.paper_tracker||{},l=d.lifetime||{},all=Object.values(t),buySignals=d.buy_signals||[],activeBuys=buySignals.filter(x=>x.status==="active"),trackedReturns=all.filter(x=>hasPct(x.discovery_return_pct));
const integrity=l.integrity||{};const integrityNode=document.getElementById('ledger-integrity');if(integrityNode){const missing=n(integrity.retained_stock_uncounted_baselines);integrityNode.textContent='Ledger integrity · stock baselines '+n(integrity.retained_stock_baselines)+' · counted '+n(integrity.retained_stock_counted_baselines)+' · uncounted '+missing+' · unverified without baseline '+n(integrity.unverified_stock_without_baseline)+(missing?' · CHECK REQUIRED':' · OK');}
const fmtTime=d.last_scan_completed_at?new Intl.DateTimeFormat('en-US',{timeZone:'America/Detroit',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',timeZoneName:'short'}).format(new Date(d.last_scan_completed_at)):'No completed scan timestamp yet';document.getElementById('updated').textContent='Last scan completed: '+fmtTime;

const stageRank={'BUY':8,'EARLY SETUP':6,'DEVELOPING RUNNER':5,'DEVELOPING SETUP':5,'ACTIONABLE SETUP':4,'EARLY WATCH':3,'LATE RUNNER — CHASE RISK':2,'LOW-CONVICTION WATCH':1};
// Discovery radar retains every successfully scanned candidate; price verification controls entry eligibility, not visibility.
const surfaced=all.filter(x=>x.currently_surfaced!==false);
const earlyCount=surfaced.filter(x=>['EARLY SETUP','EARLY WATCH','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(x.last_stage)).length;const actionableCount=surfaced.filter(x=>x.last_stage==='ACTIONABLE SETUP').length;
const stockVerified=surfaced.filter(x=>x.asset_type==='stock'&&x.price_validation_status==='verified').length;
const cryptoVerified=surfaced.filter(x=>x.asset_type==='crypto'&&x.price_validation_status==='verified').length;
const regimeSet=new Set(surfaced.map(x=>String(x.market_regime||'').toUpperCase()).filter(v=>['RISK-OFF','RISK-ON','NEUTRAL'].includes(v)));
const regime=regimeSet.size===1?[...regimeSet][0]:'NOT RECORDED';
const regimeBox=document.getElementById('scan-regime-status');
if(regimeBox)regimeBox.dataset.regime=regime==='RISK-OFF'?'off':regime==='RISK-ON'?'on':'unknown';
const regimeNode=document.getElementById('scan-regime');
if(regimeNode)regimeNode.textContent=regime;
const regimeNote=document.getElementById('scan-regime-note');
if(regimeNote)regimeNote.textContent=regime==='RISK-OFF'?'Official BUY and early-entry paper signals blocked':regime==='RISK-ON'?'Market conditions supportive; other BUY checks still apply':'No reliable common regime saved for surfaced candidates';
const officialNode=document.getElementById('scan-official-buys');
if(officialNode)officialNode.textContent=String(activeBuys.length);
const verifiedNode=document.getElementById('scan-verified-prices');
if(verifiedNode)verifiedNode.textContent=(stockVerified+cryptoVerified)+' / '+surfaced.length;
const priceNote=document.getElementById('scan-price-note');
if(priceNote)priceNote.textContent=stockVerified+' stocks · '+cryptoVerified+' crypto · verified ≠ BUY-eligible';
const lastScanTime=Date.parse(d.last_scan_completed_at||'');
const scanAgeMinutes=Number.isFinite(lastScanTime)&&lastScanTime<=Date.now()?Math.floor((Date.now()-lastScanTime)/60000):null;
const ageNode=document.getElementById('scan-age');
if(ageNode)ageNode.textContent=scanAgeMinutes===null?'UNKNOWN':scanAgeMinutes<60?scanAgeMinutes+' min':Math.floor(scanAgeMinutes/60)+'h '+(scanAgeMinutes%60)+'m';
const ageNote=document.getElementById('scan-age-note');
if(ageNote)ageNote.textContent=scanAgeMinutes===null?'Scan timestamp unavailable':(scanAgeMinutes>45?'Older saved snapshot · ':'Saved scan · ')+fmtTime;


const radarPool=surfaced.filter(x=>(prefs.assetFilter==='all'||x.asset_type===prefs.assetFilter)&&(prefs.radarFilter==='all'||(prefs.radarFilter==='developing'&&['BUY','ACTIONABLE SETUP','DEVELOPING RUNNER','DEVELOPING SETUP','EARLY SETUP'].includes(x.last_stage))||(prefs.radarFilter==='actionable'&&['BUY','ACTIONABLE SETUP'].includes(x.last_stage))));const rankRadar=(a,b)=>(stageRank[b.last_stage]??0)-(stageRank[a.last_stage]??0)||n(b.daily_change_pct)-n(a.daily_change_pct);const rankedRadar=radarPool.sort(rankRadar);const candidates=[...rankedRadar.filter(x=>x.asset_type==='stock').slice(0,9),...rankedRadar.filter(x=>x.asset_type==='crypto').slice(0,9)].sort(rankRadar);
document.getElementById('opportunities').innerHTML=candidates.length?candidates.map((x,i)=>{
 const stage=x.last_stage||'WATCHLIST',s=String(stage),kind=s.includes('ACTIONABLE')?'action':s.includes('LATE RUNNER')?'late':'';
 const reason=(x.stage_reasons||x.discovery_reasons||[]).slice(0,2).join(' · ')||'Candidate retained for continued monitoring.';
 const risks=(x.risk_flags||[]).slice(0,2).join(' · ');
 const price=x.latest_price==null?'—':(Number.isFinite(Number(x.latest_price))?Number(x.latest_price).toPrecision(7):'—');
 const day=x.daily_change_pct==null?'—':pct(x.daily_change_pct);
 const ret=hasPct(x.discovery_return_pct)?(pct(x.discovery_return_pct)+(x.discovery_return_status==='provisional'?' · PROVISIONAL':'')):(x.discovery_baseline_status==='first_verified_fallback'?'From first verified':(x.price_validation_status==='unverified'?'Awaiting first usable quote':'—'));
 const discoveryCaptured=x.discovery_price_checked_at||x.first_seen||null;
 const discoveryPriceAsOf=x.discovery_price_as_of||null;
 const discoveryTime=discoveryCaptured?new Date(discoveryCaptured).toLocaleString():'Not recorded';
 const discoveryAsOf=discoveryPriceAsOf?new Date(discoveryPriceAsOf).toLocaleString():null;
 const lv=x.levels||{}, verified=x.price_validation_status==='verified';
 const activeSignal=((d.paper_trading&&Array.isArray(d.paper_trading.positions))?d.paper_trading.positions:[]).find(p=>p.asset_type===x.asset_type&&String(p.symbol||'').toUpperCase()===String(x.symbol||'').toUpperCase());
 const earlySignals=(x.stage_signals&&x.stage_signals.early)||[],lateSignals=(x.stage_signals&&x.stage_signals.late)||[];
 const riskFlags=x.risk_flags||[],components=x.score_components||{},levels=x.levels||{};
 const clamp=(v,max)=>Math.max(0,Math.min(1,(Number(v)||0)/max));
 const stock=x.asset_type==='stock';
 const momentum=stock?((clamp(components.momentum,25)+clamp(components.trend,15))/2)*20:clamp(components.momentum,30)*20;
 const volume=clamp(components.volume,25)*15;
 const breakout=clamp(components.breakout,stock?20:25)*15;
 const technical=stock?clamp(components.technical_confirmation,15)*25:clamp(components.trend,20)*25;
 const rr=Number(levels.reward_risk_ratio||0),riskPct=Number(levels.risk_pct);
 const riskReward=rr>=2&&Number.isFinite(riskPct)?(riskPct<=5?15:riskPct<=8?12:riskPct<=10?9:4):0;
 const activeFamilies=[components.momentum,components.volume,components.breakout,components.trend,...(stock?[components.technical_confirmation]:[])].filter(v=>Number(v)>0).length;
 const agreement=Math.min(10,activeFamilies/(stock?5:4)*10);
 const riskPenalty=Math.min(16,riskFlags.reduce((sum,flag)=>sum+( /offering|dilution|registration|financing/i.test(String(flag))?6:4),0));
 const latePenalty=s.includes('LATE RUNNER')?12:0;
 const regimePenalty=x.market_regime==='RISK-OFF'?8:0;
 const qualityParts={momentum_trend:Math.round(momentum),volume:Math.round(volume),breakout:Math.round(breakout),technical_confirmation:Math.round(technical),risk_reward:Math.round(riskReward),signal_agreement:Math.round(agreement)};
 const eodProvisional=stock&&x.price_validation_status==='last_known';
 const evidenceAvailable=stock&&['momentum','volume','breakout','trend','technical_confirmation'].some(k=>components[k]!==undefined&&components[k]!==null&&Number.isFinite(Number(components[k])));
 const evidenceProvisional=stock&&!verified&&!eodProvisional&&evidenceAvailable;
 const quality=(verified||eodProvisional||evidenceAvailable)?Math.max(0,Math.min(100,Math.round(Object.values(qualityParts).reduce((a,b)=>a+b,0)-riskPenalty-latePenalty-regimePenalty))):null;
 const confidence=quality; // Separate technical evidence score; never a BUY trigger.
 const setupReadiness=stock?setupQualityReadiness(x,activeBuys,scanAgeMinutes):null;
 const confidenceLabel=confidence===null?'Unavailable':confidence>=80?'Strong evidence':confidence>=65?'Good evidence':confidence>=50?'Developing evidence':'Weak evidence';
 const qualityBreakdown=quality===null?'Price verification required':(stock?'Momentum/trend '+qualityParts.momentum_trend+'/20':'Momentum '+qualityParts.momentum_trend+'/20')+' · Volume '+qualityParts.volume+'/15 · Breakout '+qualityParts.breakout+'/15 · '+(stock?'Technical':'Trend')+' '+qualityParts.technical_confirmation+'/25 · Risk/reward '+qualityParts.risk_reward+'/15 · Agreement '+qualityParts.signal_agreement+'/10'+(eodProvisional?' · EOD-based provisional estimate; not live entry confirmation':'');
 const fmtLevel=v=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toPrecision(7);
 const entry=lv.watch_entry_low!=null?fmtLevel(lv.watch_entry_low)+' – '+fmtLevel(lv.watch_entry_high):'Not available';
 const plan=verified&&lv.target_1!=null&&lv.target_2!=null;
 const calc=lv.calculation||{},riskPer=calc.risk_per_unit!=null?Number(calc.risk_per_unit):((Number(lv.watch_entry_high)||0)-(Number(lv.stop_loss||lv.invalidation_level)||0));
 const entryRef=Number(lv.watch_entry_high)||0,stopRef=Number(lv.stop_loss||lv.invalidation_level)||0;
 const fmtOrDash=v=>v==null||!Number.isFinite(Number(v))?'—':fmtLevel(v);
 const activePaperLevels=activeSignal?'<section class="target-math" style="margin-bottom:12px"><div class="target-math-head"><b>PAPER POSITION EXIT LEVELS</b><span>FROZEN AT BUY</span></div><div class="target-math-level"><div><small>PRICE TARGET 1 · SELL 50%</small></div><div class="target-math-result"><strong>'+e(fmtOrDash(activeSignal.target_1))+'</strong></div></div><div class="target-math-level level-two"><div><small>PRICE TARGET 2 · SELL REMAINING 50%</small></div><div class="target-math-result"><strong>'+e(fmtOrDash(activeSignal.target_2))+'</strong></div></div><div class="target-math-foot">These are the exact saved targets used by the open paper position. Stop: '+e(fmtOrDash(activeSignal.stop_loss))+'</div></section>':'';
 const calcLine=plan?'<section class="target-math"><div class="target-math-head"><b>CURRENT SETUP TARGET MATH</b><span>2R / 3R MODEL</span></div><div class="target-math-inputs"><div class="target-math-input"><span>ATR · 14 PERIOD</span><strong>'+e(calc.atr_14==null?'Not saved':fmtOrDash(calc.atr_14))+'</strong></div><div class="target-math-input"><span>RISK UNIT</span><strong>'+e(fmtOrDash(calc.risk_unit))+'</strong></div><div class="target-math-input"><span>ENTRY REFERENCE · HIGH</span><strong>'+e(fmtOrDash(entryRef))+'</strong></div><div class="target-math-input"><span>STOP · '+e((calc.stop_source||'SAVED STOP').toUpperCase())+'</span><strong>'+e(fmtOrDash(stopRef))+'</strong></div></div><div class="target-math-risk"><span>RISK PER SHARE / COIN<br>Entry reference − stop</span><strong>'+e(fmtOrDash(riskPer))+'</strong></div><div class="target-math-level"><div><small>PRICE TARGET 1 · 2R TARGET</small><div class="target-math-equation">'+e(fmtOrDash(entryRef))+' + (2 × '+e(fmtOrDash(riskPer))+')</div></div><div class="target-math-result"><strong>'+e(fmtOrDash(lv.target_1))+'</strong><span>PRICE TARGET · '+e(entryRef>0&&Number.isFinite(Number(lv.target_1))?((Number(lv.target_1)/entryRef-1)*100>=0?'+':'')+((Number(lv.target_1)/entryRef-1)*100).toFixed(2)+'% FROM RANGE HIGH':'GAIN N/A')+'</span></div></div><div class="target-math-level level-two"><div><small>PRICE TARGET 2 · 3R TARGET</small><div class="target-math-equation">'+e(fmtOrDash(entryRef))+' + (3 × '+e(fmtOrDash(riskPer))+')</div></div><div class="target-math-result"><strong>'+e(fmtOrDash(lv.target_2))+'</strong><span>PRICE TARGET · '+e(entryRef>0&&Number.isFinite(Number(lv.target_2))?((Number(lv.target_2)/entryRef-1)*100>=0?'+':'')+((Number(lv.target_2)/entryRef-1)*100).toFixed(2)+'% FROM RANGE HIGH':'GAIN N/A')+'</span></div></div><div class="target-exit-note" style="margin:12px 0;padding:11px;border:1px solid var(--border);border-radius:10px;font-size:12px;line-height:1.55"><b>PAPER EXIT RULES</b><br>Price Target 1: sell 50% of the paper position at the first verified scan price at or above this target. Price Target 2: sell the remaining 50% at the first verified scan price at or above this target. If the stop is reached first, exit the full position. For an open paper position, the frozen targets above take priority; current setup targets below may change as the setup is recalculated. These are scan-observed simulated exits, not guaranteed fills at the exact target price.</div><div class="target-math-foot"><b>How inputs are set:</b> Risk unit = greater of ATR(14) or 2% of signal price. Entry reference = upper entry range. Initial stop distance = 1.5 × risk unit, subject to a 10% price cap and a valid nearby-support adjustment. '+(calc.atr_14==null?'ATR inputs were not retained for this older saved plan.':'')+'</div></section>':'';



 const confidenceWhy=confidence===null?'Not enough saved technical evidence to calculate a setup score.':qualityBreakdown+'. Deductions: '+(riskPenalty+latePenalty+regimePenalty)+' points for risk flags, late-stage extension and/or risk-off conditions. Technical Evidence is an indicator breakdown for research, not a BUY signal or a probability of profit. The separate Setup Quality rating is based on official BUY-gate completion.'+(eodProvisional?' EOD-BASED · PROVISIONAL: calculated from the cross-checked prior-session close and saved indicators; wait for live price confirmation before considering an entry.':evidenceProvisional?' PROVISIONAL · PRICE PENDING: calculated from saved technical indicators only; price verification is still required before any BUY or trade plan.':'');
 const evidenceItems=[['MOMENTUM',qualityParts.momentum_trend,20],['VOLUME',qualityParts.volume,15],['BREAKOUT',qualityParts.breakout,15],[stock?'TECHNICAL':'TREND',qualityParts.technical_confirmation,25],['RISK / REWARD',qualityParts.risk_reward,15],['AGREEMENT',qualityParts.signal_agreement,10]];
 const evidenceHtml=evidenceItems.map(([label,score,max])=>'<div class="op-evidence-item"><div><span>'+label+'</span><b>'+score+'/'+max+'</b></div><i><em style="width:'+Math.max(0,Math.min(100,score/max*100))+'%"></em></i></div>').join('');
 const setupQuick=stock?'<div class="op-quick-quality" data-setup-status="'+(setupReadiness.isBuy?'buy':'wait')+'"><span>SETUP QUALITY · BUY READINESS</span><strong>'+e(setupReadiness.score===null?'—':setupReadiness.score) +(setupReadiness.score===null?'':'/100')+'</strong><em>'+e(setupReadiness.label)+'</em><small>'+e(setupReadiness.detail)+'</small></div>':'';
 const frontQuality=stock?confidence===null?'<div class="op-front-quality unavailable"><div><span>TECHNICAL EVIDENCE</span><strong>Insufficient data</strong></div><small>Technical evidence not available for calculation</small></div>':'<div class="op-front-quality"><div class="op-front-quality-top"><div><span>TECHNICAL EVIDENCE · '+(eodProvisional?'EOD-BASED · PROVISIONAL':evidenceProvisional?'PROVISIONAL · PRICE PENDING':'EVIDENCE SCORE')+'</span><strong>'+confidence+'<small>/100</small></strong></div><span class="op-quality-label">'+e(confidenceLabel.toUpperCase())+'</span></div><div class="op-quality-track"><i style="width:'+confidence+'%"></i></div><div class="op-evidence-grid">'+evidenceHtml+'</div>'+(eodProvisional?'<small>EOD research score only · BUY and trade-plan gates still require live verification</small>':evidenceProvisional?'<small>Provisional research score from saved technical evidence · BUY and trade-plan gates still require independent live price verification</small>':'')+'</div>' :'';
 const frontPlan=plan?'<div class="op-front-plan"><div><span>ENTRY ZONE</span><b>'+e(entry)+'</b></div><div><span>STOP / INVALIDATION</span><b>'+e(fmtLevel(lv.stop_loss||lv.invalidation_level))+'</b></div></div>':(verified?'':'<div class="op-front-plan-note">Price unverified · trade levels withheld</div>');
 const candidatePriceStatus=x.price_validation_status==='verified'?(x.data_quality&&x.data_quality.data_type==='delayed'?'VERIFIED · DELAYED':'VERIFIED PRICE'):x.price_validation_status==='last_known'?'LAST KNOWN · NOT LIVE':'UNVERIFIED · WATCH ONLY';
 const candidateStatusClass=x.price_validation_status==='verified'?'verified':x.price_validation_status==='last_known'?'last-known':'unverified';
 const frontExplanation=stock?'<div class="op-front-explanation"><div>'+e(confidenceWhy)+'</div><div>Method: '+e(lv.method||'ATR-based stop and 2R / 3R reward-to-risk targets where saved.')+'</div></div>' :'';

 return '<article data-asset-type="'+e(x.asset_type||'')+'" data-symbol="'+e(x.symbol||'')+'" data-stage="'+e(stage)+'" class="op-card op-flippable '+(i===0?'featured':'')+'"><div class="op-flip-inner"><div class="op-face op-face-front"><div class="op-card-top"><div><div class="op-kind">'+(i===0?'TOP RADAR CANDIDATE · ':'')+e((x.asset_type||'').toUpperCase())+'</div><div class="op-symbol">'+e(x.symbol||'—')+'</div></div><span class="op-stage '+kind+'">'+e(stage)+'</span></div><div class="op-price-status '+candidateStatusClass+'">'+e(candidatePriceStatus)+'</div><div class="op-metrics"><div class="op-metric"><strong>'+e(price)+'</strong><span>LATEST PRICE</span></div><div class="op-metric"><strong class="'+cl(x.daily_change_pct)+'">'+e(day)+'</strong><span>24H MOVE</span></div><div class="op-metric"><strong class="'+cl(x.discovery_return_pct)+'">'+e(ret)+'</strong><span>RETURN SINCE DISCOVERY</span></div></div><div class="op-discovery-time" style="font-size:11px;line-height:1.5;color:var(--muted);margin-top:-7px;margin-bottom:10px">FIRST CAPTURED: <b>'+e(discoveryTime)+'</b>'+(discoveryAsOf?' · PRICE AS OF: <b>'+e(discoveryAsOf)+'</b>':'')+'</div>'+(stock?setupQuick:'<div class="op-quick-quality"><span>CRYPTO RUNNER QUALITY</span><strong>'+e(x.crqs==null?'—':x.crqs)+'/74</strong><em>'+e(x.crqs_tier||'Score unavailable')+'</em></div>')+frontPlan+'<div class="op-reason"><b>WHY IT\'S ON RADAR</b><br>'+e(reason)+'</div><details class="op-details"><summary>Full analysis · price history · scoring · risks</summary><div class="op-details-body">'+priceTiming(x)+frontQuality+(stock?'':'<div class="op-front-quality"><div><span>CRYPTO RUNNER QUALITY · CRQS</span><strong>'+e(x.crqs==null?'—':x.crqs)+'<small>/74</small></strong></div><span class="op-quality-label">'+e(x.crqs_tier||'Score unavailable')+'</span><small>Entry timing score: '+e(x.crqs_entry_score==null?'—':x.crqs_entry_score)+'/74 · Separate from BUY signal</small><small>74-point calibrated ceiling; relative strength, order-book depth/spread and token-unlock risk are not scored yet.</small></div>')+frontExplanation+(risks?'<div class="op-risk">RISK FLAGS: '+e(risks)+'</div>':'')+'</div></details><div class="op-card-actions"><button type="button" data-card-action="analysis">View Analysis</button><button type="button" data-card-action="targets">View Price Targets</button>'+candidateChartLogo(x)+'</div></div><div class="op-face op-face-back" inert><div class="op-back-title">'+e((x.symbol||'CANDIDATE')+' · POTENTIAL TRADE PLAN')+'</div>'+activePaperLevels+calcLine+(calcLine||activePaperLevels?'':'<div class="op-front-plan-note">No independently verified price targets are available for this candidate yet.</div>')+'<button type="button" class="op-card-back-button" data-card-action="back">Back to Opportunity</button></div></div></article>';
}).join(''):'<div class="op-empty">Nothing has qualified for the radar yet. Jarvis will keep monitoring and show candidates after a successful scan.</div>';

const commandMetrics=document.getElementById('command-metrics');
if(commandMetrics){const verifiedCount=surfaced.filter(x=>x.price_validation_status==='verified').length;const combinedActionable=actionableCount;commandMetrics.innerHTML=[[''+all.length,'TRACKED ASSETS'],[''+surfaced.length,'ON RADAR NOW'],[''+earlyCount,'EARLY / BUILDING'],[''+combinedActionable,'ACTIONABLE SETUPS']].map(([v,label])=>'<div class="command-metric"><strong>'+e(v)+'</strong><span>'+label+'</span></div>').join('');}
const commandScan=document.getElementById('command-scan-status');if(commandScan)commandScan.textContent='Last completed scan · '+fmtTime;
const commandData=document.getElementById('command-data-status');if(commandData){const verifiedCount=surfaced.filter(x=>x.price_validation_status==='verified').length;commandData.textContent='Verified prices '+verifiedCount+' / '+surfaced.length+' · unverified assets remain visible for research only';}
const runnerLaneDefs=[
 {key:'emerging',title:'EMERGING',desc:'Early watch and early setup',stages:['EARLY WATCH','EARLY SETUP']},
 {key:'building',title:'BUILDING',desc:'Developing setup and runner',stages:['DEVELOPING SETUP','DEVELOPING RUNNER']},
 {key:'actionable',title:'ACTIONABLE',desc:'Qualified setup or BUY stage',stages:['ACTIONABLE SETUP','BUY']},
 {key:'cooling',title:'COOLING / RISK',desc:'Late extension or lower conviction',stages:['LATE RUNNER — CHASE RISK','LOW-CONVICTION WATCH']}
];
const runnerLanes=document.getElementById('runner-radar-lanes');
if(runnerLanes){runnerLanes.innerHTML=runnerLaneDefs.map(lane=>{const items=candidates.filter(x=>lane.stages.includes(String(x.last_stage||'').toUpperCase()));const chips=items.slice(0,5).map(x=>'<button type="button" class="runner-chip" data-runner-symbol="'+e(x.symbol||'')+'" data-runner-asset="'+e(x.asset_type||'')+'">'+e((x.asset_type||'').toUpperCase()+' · '+(x.symbol||'—'))+'</button>').join('');return '<div class="runner-lane"><div class="runner-lane-top"><span class="runner-lane-title">'+lane.title+'</span><span class="runner-lane-count">'+items.length+'</span></div><p>'+lane.desc+'</p><div class="runner-chip-list">'+(chips||'<span class="runner-empty">No candidates in this stage</span>')+'</div></div>';}).join('');}

document.querySelectorAll('[data-runner-symbol]').forEach(btn=>btn.addEventListener('click',()=>{const symbol=btn.dataset.runnerSymbol,asset=btn.dataset.runnerAsset;const card=[...document.querySelectorAll('.op-flippable')].find(c=>(c.dataset.symbol||'')===symbol&&(c.dataset.assetType||'')===asset);if(card){const library=document.getElementById('candidate-analysis-library');if(library)library.open=true;card.scrollIntoView({behavior:'smooth',block:'center'});openCandidateAnalysis(card);}}));
function updateActionTopPick(){const actionPick=document.getElementById('action-top-pick');if(!actionPick)return;const filteredCandidates=candidates.filter(x=>{const asset=(x.asset_type||'').toLowerCase(),stage=(x.last_stage||'WATCH').toUpperCase();const assetOK=jAsset==='all'||asset===jAsset;let stageOK=true;if(jStage==='buy')stageOK=stage==='BUY';else if(jStage==='setup')stageOK=['ACTIONABLE SETUP','DEVELOPING SETUP','EARLY SETUP'].includes(stage);else if(jStage==='early')stageOK=['EARLY SETUP','EARLY WATCH','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(stage);else if(jStage==='watch')stageOK=!['BUY','ACTIONABLE SETUP','EARLY SETUP','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(stage);return assetOK&&stageOK;});if(filteredCandidates.length){const top=filteredCandidates[0],stage=top.last_stage||'WATCH',kind=(top.asset_type||'').toUpperCase(),sym=top.symbol||'—';const stageLabel=stage==='BUY'?'BUY SIGNAL':stage==='ACTIONABLE SETUP'?'ACTIONABLE SETUP':stage.includes('EARLY')||stage.includes('RUNNER')?'EARLY RUNNER':'WATCH / DEVELOPING';actionPick.dataset.asset=String(top.asset_type||'').toLowerCase();actionPick.dataset.symbol=String(sym);actionPick.setAttribute('aria-label','Open full analysis for '+kind+' '+sym);actionPick.innerHTML='<div><div class="action-pick-title">TOP-RANKED OPPORTUNITY · '+e(kind)+'</div><div class="action-pick-symbol">'+e(sym)+'</div><div class="action-pick-reason">'+e((top.stage_reasons||top.discovery_reasons||[]).slice(0,3).join(' · ')||'Ranked first by the existing Jarvis radar.')+'</div><span class="action-pick-status">'+e(stageLabel)+'</span></div><span class="action-pick-status">OPEN FULL ANALYSIS ↓</span>';}else{delete actionPick.dataset.asset;delete actionPick.dataset.symbol;actionPick.setAttribute('aria-label','No candidate matches the selected filters');actionPick.innerHTML='<div><div class="action-pick-title">TOP-RANKED OPPORTUNITY</div><div class="action-pick-symbol">No candidates for this filter</div><div class="action-pick-reason">No candidate matches the selected asset and stage filters.</div></div><span class="action-pick-status">NO MATCH</span>';}}const actionPickControl=document.getElementById('action-top-pick');if(actionPickControl){const openTopAnalysis=()=>{const symbol=actionPickControl.dataset.symbol,asset=actionPickControl.dataset.asset;if(!symbol||!asset)return;const card=[...document.querySelectorAll('.op-flippable')].find(c=>(c.dataset.symbol||'')===symbol&&(c.dataset.assetType||'').toLowerCase()===asset);if(!card)return;const library=document.getElementById('candidate-analysis-library');if(library)library.open=true;card.scrollIntoView({behavior:'smooth',block:'center'});openCandidateAnalysis(card);};actionPickControl.addEventListener('click',openTopAnalysis);actionPickControl.addEventListener('keydown',ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();openTopAnalysis();}});}const jAssetButtons=document.querySelectorAll('[data-jasset]'),jStageButtons=document.querySelectorAll('[data-jstage]');let jAsset='all',jStage='all';function applyActionFilters(){const cards=[...document.querySelectorAll('.op-flippable')];cards.forEach(card=>{const asset=(card.dataset.assetType||'').toLowerCase(),stage=(card.dataset.stage||'').toUpperCase();const assetOK=jAsset==='all'||asset===jAsset;let stageOK=true;if(jStage==='buy')stageOK=stage==='BUY';else if(jStage==='setup')stageOK=['ACTIONABLE SETUP','DEVELOPING SETUP','EARLY SETUP'].includes(stage);else if(jStage==='early')stageOK=['EARLY SETUP','EARLY WATCH','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(stage);else if(jStage==='watch')stageOK=!['BUY','ACTIONABLE SETUP','EARLY SETUP','DEVELOPING RUNNER','DEVELOPING SETUP'].includes(stage);card.hidden=!(assetOK&&stageOK);});updateActionTopPick();const visible=cards.filter(c=>!c.hidden).length;const counts=document.getElementById('action-counts');if(counts)counts.innerHTML='<span><strong>'+visible+'</strong> shown / '+cards.length+' radar cards</span><span><strong>'+jAsset.toUpperCase()+'</strong> assets</span><span><strong>'+jStage.toUpperCase()+'</strong> stage</span>';}
jAssetButtons.forEach(btn=>btn.addEventListener('click',()=>{jAsset=btn.dataset.jasset;jAssetButtons.forEach(b=>b.classList.toggle('active',b===btn));applyActionFilters();}));jStageButtons.forEach(btn=>btn.addEventListener('click',()=>{jStage=btn.dataset.jstage;jStageButtons.forEach(b=>b.classList.toggle('active',b===btn));applyActionFilters();}));applyActionFilters();
function setCardFace(card,flipped){
  card.classList.toggle('is-flipped',flipped);
  const front=card.querySelector('.op-face-front'),back=card.querySelector('.op-face-back');
  if(front)front.inert=flipped;
  if(back)back.inert=!flipped;
}
function openCandidateAnalysis(card){
  const library=document.getElementById('candidate-analysis-library');
  if(library)library.open=true;
  card.hidden=false;
  setCardFace(card,false);
  const details=card.querySelector('.op-details');
  if(details)details.open=true;
  card.scrollIntoView({behavior:'smooth',block:'center'});
}
document.querySelectorAll('.op-flippable').forEach(card=>{
  setCardFace(card,false);
  card.querySelectorAll('[data-card-action]').forEach(button=>button.addEventListener('click',()=>{
    const action=button.dataset.cardAction;
    if(action==='analysis')openCandidateAnalysis(card);
    else setCardFace(card,action==='targets');
  }));
});
const timelineItems=surfaced.slice().sort((a,b)=>(stageRank[b.last_stage]??0)-(stageRank[a.last_stage]??0)).slice(0,12);document.getElementById('radar-timeline-cards').innerHTML=timelineItems.length?timelineItems.map(x=>{const history=(x.grade_history||[]).slice(-3).reverse();const events=history.length?history.map(h=>'<div class="timeline-event"><b>'+e(h.to||'Stage update')+'</b> · '+e(date(h.at))+'</div>').join(''):'<div class="timeline-event">First recorded stage: '+e(x.last_stage||'Watching')+'</div>';return '<article class="timeline-card"><div class="timeline-top"><div class="timeline-symbol">'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</div><span class="timeline-stage">'+e(x.last_stage||'WATCHING')+'</span></div><div class="timeline-events">'+events+'</div></article>';}).join(''):'<div class="op-empty">No candidates are currently on the radar.</div>';
const runnerPool=surfaced.filter(x=>x.asset_type==='stock').map(x=>{const gap=x.premarket_gap_pct==null?null:n(x.premarket_gap_pct),vol=n(x.premarket_volume),rvol=n(x.premarket_rvol_same_time),research=n(x.early_runner_research_score);const qualifies=(gap!==null&&gap>=2.5)||(vol>=25000&&rvol>=1.25);if(!qualifies)return null;const catalysts=Array.isArray(x.catalyst_labels)?x.catalyst_labels:(x.catalyst_labels?[String(x.catalyst_labels)]:[]);const dilution=Array.isArray(x.dilution_flags)?x.dilution_flags:(x.dilution_flags?[String(x.dilution_flags)]:[]);const score=Math.max(0,Math.min(100,Math.round(Math.min(Math.max(gap||0,0),25)*1.2+Math.min(rvol,10)*4+Math.min(vol/10000,10)+research*.25+(catalysts.length?12:0)-(dilution.length?15:0))));return {...x,_runnerScore:score,_runnerCatalysts:catalysts,_runnerDilution:dilution};}).filter(Boolean).sort((a,b)=>b._runnerScore-a._runnerScore).slice(0,40);
const runnerVerified=runnerPool.filter(x=>x.price_validation_status==='verified').length;const runnerLastKnown=runnerPool.filter(x=>x.price_validation_status==='last_known').length;
document.getElementById('early-runner-stats').innerHTML=[[''+runnerPool.length,'QUALIFIED RUNNERS'],[''+runnerVerified,'LIVE VERIFIED'],[''+runnerLastKnown,'LAST KNOWN · EOD'],[''+(runnerPool.length-runnerVerified-runnerLastKnown),'UNVERIFIED']].map(x=>'<div class="card"><div class="muted">'+e(x[1])+'</div><div class="value">'+e(x[0])+'</div></div>').join('');
document.getElementById('early-runner-rows').innerHTML=runnerPool.map((x,i)=>{const notes=[...x._runnerCatalysts,...x._runnerDilution.map(v=>'RISK: '+v)];const status=x.price_validation_status==='verified'?'<span class="pill pos">LIVE VERIFIED</span>':x.price_validation_status==='last_known'?'<span class="pill">LAST KNOWN · EOD</span>':'<span class="pill neg">UNVERIFIED</span>';const price=x.latest_price==null?'—':Number(x.latest_price).toPrecision(7);return '<tr><td>'+(i+1)+'</td><td><b>'+e(x.symbol)+'</b><div class="tiny">'+e(x.last_stage||'WATCHLIST')+'</div></td><td><b>'+e(x._runnerScore)+'/100</b><div class="tiny">Research '+e(x.early_runner_research_score??'—')+'</div></td><td>'+e(price)+'<div class="tiny">'+e(x.price_source||'Source not recorded')+'</div>'+priceTiming(x)+'</td><td>'+status+'</td><td>'+e(x.premarket_gap_pct==null?'—':pct(x.premarket_gap_pct))+'</td><td>'+e(x.premarket_rvol_same_time==null?'—':Number(x.premarket_rvol_same_time).toFixed(2)+'x')+'</td><td>'+e(x.premarket_volume==null?'—':Number(x.premarket_volume).toLocaleString())+'</td><td>'+e(notes.join(' · ')||'No catalyst / dilution flag recorded')+'</td></tr>';}).join('')||'<tr><td colspan="9" class="empty">No saved candidates currently meet the Early Runner display criteria. The board will populate from the next hourly scan that records qualifying premarket metrics.</td></tr>';
const observed=buySignals.filter(x=>x.target_1||x.target_2||x.stop_loss);const l1=observed.filter(x=>x.level_observations&&x.level_observations.target_1_observed),l2=observed.filter(x=>x.level_observations&&x.level_observations.target_2_observed),stops=observed.filter(x=>x.level_observations&&x.level_observations.stop_observed);document.getElementById('target-performance').innerHTML=[['BUY plans tracked',observed.length],['Price Target 1 observed',l1.length+' / '+observed.length],['Price Target 2 observed',l2.length+' / '+observed.length],['Stop observed',stops.length+' / '+observed.length]].map(x=>'<div class="card"><div class="muted">'+e(x[0])+'</div><div class="value">'+e(x[1])+'</div></div>').join('');document.getElementById('target-tracking').innerHTML=observed.slice().sort((a,b)=>new Date(b.at)-new Date(a.at)).slice(0,40).map(x=>{const obs=x.level_observations||{};return '<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+e(x.entry_price??'—')+'</td><td>'+e(x.target_1??'—')+(obs.target_1_observed?'<div class="tiny pos">Observed '+e(date(obs.target_1_observed.at))+'</div>':'<div class="tiny">Not yet observed</div>')+'</td><td>'+e(x.target_2??'—')+(obs.target_2_observed?'<div class="tiny pos">Observed '+e(date(obs.target_2_observed.at))+'</div>':'<div class="tiny">Not yet observed</div>')+'</td><td>'+e(x.stop_loss??'—')+(obs.stop_observed?'<div class="tiny neg">Observed '+e(date(obs.stop_observed.at))+'</div>':'')+'</td><td>'+e(obs.target_1_observed?elapsed(x.at,obs.target_1_observed.at):'—')+'</td><td>'+e(obs.target_2_observed?elapsed(x.at,obs.target_2_observed.at):'—')+'</td></tr>';}).join('')||'<tr><td colspan="7" class="empty">BUY target observations will appear as Jarvis records new scans.</td></tr>';

const thresholds=[['Positive now',trackedReturns.filter(x=>n(x.discovery_return_pct)>0).length],['At least +5%',trackedReturns.filter(x=>n(x.discovery_return_pct)>=5).length],['At least +10%',trackedReturns.filter(x=>n(x.discovery_return_pct)>=10).length],['At least +20%',trackedReturns.filter(x=>n(x.discovery_return_pct)>=20).length],['Down 5% or more',trackedReturns.filter(x=>n(x.discovery_return_pct)<=-5).length],['Ever reached +10%',trackedReturns.filter(x=>hasPct(x.discovery_max_gain_pct)&&n(x.discovery_max_gain_pct)>=10).length]];
document.getElementById('assets').innerHTML=['stock','crypto'].map(k=>{const a=trackedReturns.filter(x=>x.asset_type===k),m=a.length?a.reduce((s,x)=>s+n(x.discovery_return_pct),0)/a.length:null;return '<button type="button" class="panel asset-jump" data-asset="'+k+'" aria-label="Show '+(k==='stock'?'stock':'crypto')+' candidates"><div class="muted">'+(k==='stock'?'Stocks':'Crypto')+'</div><div class="value">'+a.length+' performance tracked</div><div class="'+cl(m)+'">Average return '+(m===null?'—':pct(m))+'</div><div class="tiny">'+a.filter(x=>n(x.discovery_return_pct)>0).length+' positive · '+a.filter(x=>x.last_stage==='ACTIONABLE SETUP').length+' actionable grade</div><div class="tiny" style="margin-top:8px;color:var(--accent)">View '+(k==='stock'?'stock':'crypto')+' list →</div></button>'}).join('');
document.getElementById('buy-history').innerHTML=[...buySignals].sort((a,b)=>new Date(b.at)-new Date(a.at)).map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'<div class="tiny">'+e(x.status||'')+'</div></td><td>'+date(x.at)+'</td><td>'+e(x.entry_price)+'</td><td class="'+cl(x.latest_return_pct)+'">'+(x.latest_return_pct==null?'—':pct(x.latest_return_pct))+'</td>'+['1h','4h','1d','3d','5d','7d','10d'].map(h=>'<td class="'+cl(x.outcomes&&x.outcomes[h]&&x.outcomes[h].return_pct)+'">'+(x.outcomes&&x.outcomes[h]?pct(x.outcomes[h].return_pct):'—')+'</td>').join('')+'<td>'+e(x.score)+'/100</td></tr>').join('')||'<tr><td colspan="12" class="empty">No BUY recommendations have been recorded yet.</td></tr>';
const signalHorizonRows=['1h','4h','1d','3d','5d','7d','10d'].map(h=>{
 const values=buySignals.map(x=>x.outcomes&&x.outcomes[h]?Number(x.outcomes[h].return_pct):NaN).filter(Number.isFinite);
 const wins=values.filter(v=>v>0).length,losses=values.filter(v=>v<0).length;
 const avg=values.length?values.reduce((sum,v)=>sum+v,0)/values.length:null;
 return '<tr><td>'+h+'</td><td>'+values.length+'</td><td>'+wins+'</td><td>'+losses+'</td><td class="'+cl(avg)+'">'+(avg===null?'—':pct(avg))+'</td></tr>';
}).join('');
document.getElementById('signal-checks').innerHTML=signalHorizonRows||'<tr><td colspan="5" class="empty">No matured BUY signal outcomes yet.</td></tr>';

let ledgerAssetFilter='all';function rows(){const q=document.getElementById('q').value.toUpperCase(),a=ledgerAssetFilter,g=document.getElementById('gradeFilter').value;const stocks=all.filter(x=>x.asset_type==='stock').length,crypto=all.filter(x=>x.asset_type==='crypto').length;const v=all.filter(x=>(a==='all'||x.asset_type===a)&&(g==='all'||x.last_stage===g)&&(!q||String(x.symbol).toUpperCase().includes(q)));const countNode=document.getElementById('ledger-candidate-counts');if(countNode)countNode.innerHTML='<span><strong>'+all.length+'</strong> total</span><span><strong>'+stocks+'</strong> stocks</span><span><strong>'+crypto+'</strong> crypto</span><span><strong>'+v.length+'</strong> shown</span>';document.querySelectorAll('[data-ledger-asset]').forEach(btn=>{const active=btn.dataset.ledgerAsset===a;btn.classList.toggle('active',active);btn.setAttribute('aria-pressed',String(active));});v.sort((x,y)=>(rank[y.last_stage]||0)-(rank[x.last_stage]||0)||(y.last_seen||'').localeCompare(x.last_seen||''));document.getElementById('rows').innerHTML=v.map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+date(x.first_seen)+'</td><td>'+n(x.discovery_initial_price??x.initial_price).toPrecision(7)+'</td><td><span class="pill">'+e(x.last_stage||'—')+'</span></td><td>'+e(x.price_source||'Legacy record — source not recorded')+'<div class="tiny">'+e(x.price_freshness||'Freshness unknown')+(x.price_validation_status==='unverified'?' · PRICE UNVERIFIED — excluded from returns':'')+'</div></td><td>'+date(x.price_as_of||x.last_seen)+'</td><td class="'+cl(x.session_change_pct)+'">'+(x.price_validation_status==='unverified'||x.session_change_pct==null?'—':pct(x.session_change_pct))+'</td><td class="'+cl(x.discovery_return_pct)+'">'+(hasPct(x.discovery_return_pct)?pct(x.discovery_return_pct):(['first_verified_fallback','first_reliable_fallback'].includes(x.discovery_baseline_status)?'DELAYED RELIABLE BASELINE':(x.price_validation_status==='unverified'?'AWAITING RELIABLE PRICE':'—')))+'</td><td class="pos">'+(x.price_validation_status==='unverified'||!hasPct(x.discovery_max_gain_pct??x.max_gain_pct)?'—':pct(x.discovery_max_gain_pct??x.max_gain_pct))+'</td><td class="neg">'+(x.price_validation_status==='unverified'||!hasPct(x.discovery_max_drawdown_pct??x.max_drawdown_pct)?'—':pct(x.discovery_max_drawdown_pct??x.max_drawdown_pct))+'</td><td>'+n(x.age_days).toFixed(1)+'d</td></tr>').join('')||'<tr><td colspan="11" class="empty">No matches.</td></tr>'}
const performanceArchive=document.getElementById('performance-archive');
const ledgerSearch=document.getElementById('q');
const ledgerGrade=document.getElementById('gradeFilter');
ledgerSearch.oninput=rows;
ledgerGrade.onchange=rows;
document.querySelectorAll('[data-ledger-asset]').forEach(btn=>{
  btn.onclick=()=>{ledgerAssetFilter=btn.dataset.ledgerAsset;rows();};
});
document.querySelectorAll('.asset-jump').forEach(card=>card.addEventListener('click',()=>{
  ledgerAssetFilter=card.dataset.asset;
  ledgerGrade.value='all';
  ledgerSearch.value='';
  rows();
  document.getElementById('candidate-ledger').scrollIntoView({behavior:'smooth',block:'start'});
}));
// Build the searchable table when its archive is first opened.
if(performanceArchive.open)rows();
else performanceArchive.ontoggle=()=>{
  if(!performanceArchive.open)return;
  performanceArchive.ontoggle=null;
  rows();
};
document.getElementById('thresholds').innerHTML=thresholds.map(x=>'<div style="display:flex;justify-content:space-between;padding:7px;border-bottom:1px solid #2b3b4d"><span>'+e(x[0])+'</span><b>'+x[1]+'</b></div>').join('');
const cps=l.checkpoints||{};document.getElementById('checks').innerHTML=Object.entries(cps).sort((a,b)=>n(a[0])-n(b[0])).map(([h,x])=>'<tr><td>'+e(h)+'</td><td>'+n(x.observations)+'</td><td>'+n(x.wins)+'</td><td>'+n(x.losses)+'</td><td>'+pct(x.observations?n(x.total_return_pct)/n(x.observations):0)+'</td></tr>').join('')||'<tr><td colspan="5">Checkpoints populate as records mature.</td></tr>';
document.getElementById('catalysts').innerHTML=all.filter(x=>(x.discovery_reasons||[]).length||(x.catalysts||[]).length||(x.risk_flags||[]).length).slice(0,40).map(x=>'<tr><td>'+e((x.asset_type||'').toUpperCase()+' '+x.symbol)+'</td><td>'+e((x.discovery_reasons||[]).join(' · ')||'—')+'</td><td>'+((x.catalysts||[]).slice(0,2).map(c=>c.url?'<a target="_blank" rel="noopener" href="'+e(c.url)+'">'+e(c.title)+'</a>':e(c.title)).join('<br>')||'—')+'</td><td class="neg">'+e((x.risk_flags||[]).join(' · ')||'—')+'</td></tr>').join('')||'<tr><td colspan="4">Discovery details will populate on the next scan.</td></tr>';
}
function renderScanReceipt(data) {
 const receipt = document.getElementById('scan-receipt');
 const changes = document.getElementById('scan-changes');
 const run = String(data.scan_run_id || '');
 const rows = Object.values(data.paper_tracker);
 const pair = data.completed_scan_pair || [];
 const current = pair.at(-1);
 const matched = current && current.completed_at === data.last_scan_completed_at &&
   (!run || current.run_id === run);
 const members = matched ? Object.keys(current.members) :
   rows.filter(row => row.currently_surfaced === true).map(row => row.asset_type + ':' + row.symbol);
 const count = asset => members.filter(key => key.startsWith(asset + ':')).length;
 const runLabel = /^\d+$/.test(run)
   ? '<a href="https://github.com/rileymagiera/jarvis-market-scanner/actions/runs/' + run + '">Run ' + run + '</a>'
   : 'Run ID unavailable';
 receipt.innerHTML = runLabel + ' · ' + e(etStamp(data.last_scan_completed_at)) +
   ' · ' + count('stock') + ' stocks · ' + count('crypto') + ' crypto · ' + rows.length + ' retained records';
 checkPublishedScan(data.last_scan_completed_at);
 if (!matched || pair.length < 2) {
   changes.textContent = 'A comparison needs two consecutive saved scans.';
   return;
 }
 const previous = pair.at(-2);
 const changesHtml = [];
 for (const key of [...new Set([...Object.keys(previous.members), ...members])].sort()) {
   const before = previous.members[key], after = current.members[key];
   const reasons = [];
   if (!before) reasons.push('Entered scan');
   else if (!after) reasons.push('Left scan');
   else {
     if (rank[after[0]] > rank[before[0]]) reasons.push('Stage advanced');
     if (before[1] === 'verified' && after[1] !== 'verified') reasons.push('Lost verification');
   }
   if (!reasons.length) continue;
   const describe = value => value ? value.map(part => part || 'Unknown').join(' · ') : 'Absent';
   changesHtml.push('<tr>' + [key, reasons.join(' · '), describe(before), describe(after)]
     .map(value => '<td>' + e(value) + '</td>').join('') + '</tr>');
 }
 changes.innerHTML = '<p>Compared with ' + e(etStamp(previous.completed_at)) + '</p>' +
   (changesHtml.length ? '<table><thead><tr><th>Candidate</th><th>Change</th><th>Previous</th><th>Current</th></tr></thead><tbody>' +
   changesHtml.join('') + '</tbody></table>' : '<p>No entries, departures, advances, or verification losses.</p>');
}

async function checkPublishedScan(displayedAt) {
 const label = document.getElementById('receipt-freshness');
 label.textContent = 'Checking latest published scan…';
 try {
   const response = await fetch('https://raw.githubusercontent.com/rileymagiera/jarvis-market-scanner/main/scan_metadata.json',
     {cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(6000)});
   if (!response.ok) throw Error();
   const published = Date.parse((await response.json()).last_scan_completed_at);
   const displayed = Date.parse(displayedAt);
   if (!Number.isFinite(published) || !Number.isFinite(displayed)) throw Error();
   label.textContent = published > displayed ? 'A newer scan is published on GitHub.' :
     published === displayed ? 'Matches the latest published scan.' : 'This snapshot is ahead of the published scan.';
 } catch {
   label.textContent = 'Latest published scan could not be checked.';
 }
}
