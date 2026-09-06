// ダッシュボード: /status を30秒ごとに取り、ゲームごとのカードを組み立てる
const CARDS = [
  ['cookies','🍪 cookies'], ['cps','⚡ CpS'],
  ['elderWrath','👵 elderWrath'], ['wrinklers','🐛 wrinklers'],
  ['lumps','🍬 lumps'], ['prestige','👼 prestige'],
  ['dragon','🐉 dragon Lv'], ['achievements','🏆 achievements'],
];
const WRATH = ['平穏','ざわめき','高まり','黙示録'];
// game名 -> Chart。'constructor'等のgame名がプロトタイプと衝突しないようプロトタイプなしオブジェクトを使う
const charts = Object.create(null);

function fmt(v){
  if (typeof v !== 'number' || !isFinite(v)) return v ?? '-';
  if (Math.abs(v) >= 1e15) return v.toExponential(2);
  return new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(v);
}
function el(tag, cls, text){
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function cssVar(name){ return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// gameごとのセクションをDOM APIで組み立てる(game名等をinnerHTMLに混ぜない)
function section(game){
  const id = 'sec-' + game;
  let sec = document.getElementById(id);
  if (sec) return sec;
  sec = el('section', 'card game'); sec.id = id;
  const head = el('div', 'card-title');
  head.append(el('h2', '', game), el('span', 'muted meta', ''));
  sec.appendChild(head);
  const stats = el('div', 'stats');
  for (const [key,label] of CARDS){
    const st = el('div', 'stat');
    const v = el('div', 'value num', '-'); v.dataset.key = key;
    st.append(el('div', 'label', label), v); stats.appendChild(st);
  }
  sec.appendChild(stats);
  const box = el('div', 'chartbox');
  const canvas = document.createElement('canvas');
  box.append(canvas, el('div', 'chartnote', 'グラフを表示できません(Chart.js 未読込)'));
  sec.appendChild(box);
  // 自動昇天トグル。変更はサーバに保存し、クライアントが60秒毎に拾う
  const settings = el('div', 'settings');
  const cfg = el('label', 'switch');
  const cb = document.createElement('input'); cb.type = 'checkbox';
  cb.addEventListener('change', async () => {
    cb.disabled = true;
    try {
      const res = await fetch('/config/' + encodeURIComponent(game), {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({autoAscend: cb.checked}),
      });
      if (res.ok) cb.checked = !!(await res.json()).autoAscend;
      else cb.checked = !cb.checked;  // 保存失敗時は表示を元に戻す
    } catch (e) { cb.checked = !cb.checked; console.warn(e); }
    cb.disabled = false;
  });
  cfg.append(cb, el('span', '', '⛪ 自動昇天(プレステージ2倍化で実行)'));
  settings.appendChild(cfg);
  sec.appendChild(settings);
  // 未取得実績の折りたたみリスト(実績ハント用チェックリスト)
  const ach = el('details', 'achievs'); ach.style.display = 'none';
  ach.append(el('summary'), el('div', 'chips'));
  sec.appendChild(ach);
  const img = el('img', 'shot'); img.alt = 'screenshot';
  // 読み込み成功時のみ表示(未送信・配信エラー時に壊れた画像アイコンを出さない)
  img.addEventListener('load', () => { img.style.display = 'block'; });
  img.addEventListener('error', () => { img.style.display = 'none'; delete img.dataset.src; });
  sec.appendChild(img);
  document.getElementById('games').appendChild(sec);
  return sec;
}

// Chart.js(defer/CDN)がまだ無ければ作らず、後続のrefreshで再試行する
function ensureChart(game, sec){
  if (charts[game]) return charts[game];
  if (typeof Chart === 'undefined') return null;
  const accent = cssVar('--accent') || '#1a73e8', muted = cssVar('--muted') || '#5f6368', border = cssVar('--border') || '#dadce0';
  charts[game] = new Chart(sec.querySelector('canvas'), {
    type:'line',
    data:{labels:[],datasets:[{data:[],borderColor:accent,backgroundColor:accent + '22',fill:true,tension:.3,pointRadius:0,borderWidth:2}]},
    options:{responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:false},title:{display:true,text:'ベースCpS (today, 対数目盛)',color:muted,font:{size:11,weight:'normal'}}},
      scales:{
        x:{ticks:{color:muted,maxTicksLimit:6},grid:{color:border}},
        // CpSは日内でも桁が跳ね上がり線形軸だと序盤が潰れるため対数軸にする
        y:{type:'logarithmic',ticks:{color:muted,maxTicksLimit:6,callback:v=>fmt(v)},grid:{color:border}}}}
  });
  return charts[game];
}

function updateCards(sec, rec){
  for (const e of sec.querySelectorAll('.value')){
    const key = e.dataset.key;
    let v = rec[key];
    if (key === 'achievements' && typeof v === 'number' && typeof rec.achievementsTotal === 'number'){
      v = fmt(v) + ' / ' + fmt(rec.achievementsTotal);
    }
    else if (key === 'elderWrath' && Number.isInteger(v) && WRATH[v]) v = WRATH[v];
    else v = fmt(v);
    e.textContent = v;
  }
  // トグルはPOST保存中(disabled)でなければサーバ値に同期する
  const cb = sec.querySelector('.switch input');
  if (!cb.disabled) cb.checked = !!(rec.config && rec.config.autoAscend);
  const ts = typeof rec.ts === 'number' ? new Date(rec.ts*1000) : null;
  let meta = ts ? '最終報告 ' + ts.toLocaleTimeString('ja-JP') : '';
  if (typeof rec.shotTs === 'number'){
    meta += (meta ? ' · ' : '') + 'スクショ ' + new Date(rec.shotTs*1000).toLocaleTimeString('ja-JP');
  }
  sec.querySelector('.meta').textContent = meta;
}

// 実績ハント用の未取得リスト。実績名はtextContentで挿入(HTMLに混ぜない)
function updateAchievements(sec, rec){
  const box = sec.querySelector('.achievs');
  const missing = Array.isArray(rec.missingAchievements) ? rec.missingAchievements : null;
  if (!missing){ box.style.display = 'none'; return; } // 旧クライアント
  box.style.display = 'block';
  const shadowMissing = Array.isArray(rec.missingShadow) ? rec.missingShadow : [];
  let label = '🏆 未取得の実績 ' + missing.length + '件';
  if (typeof rec.shadowOwned === 'number' && typeof rec.shadowTotal === 'number'){
    label += '(シャドウ ' + rec.shadowOwned + '/' + rec.shadowTotal + ')';
  }
  sec.querySelector('.achievs summary').textContent = label;
  // 内容が変わったときだけチップ群を組み直す(30秒ごとの全再構築を避ける)
  const chips = sec.querySelector('.achievs .chips');
  const sig = JSON.stringify([missing, shadowMissing]);
  if (chips.dataset.sig === sig) return;
  chips.dataset.sig = sig;
  chips.textContent = '';
  for (const name of missing) chips.appendChild(el('span', 'chip', name));
  if (shadowMissing.length){
    chips.appendChild(el('div', 'grouplabel', 'シャドウ実績(milk対象外・任意)'));
    for (const name of shadowMissing) chips.appendChild(el('span', 'chip shadow', name));
  }
}

function updateShot(sec, game, rec){
  const img = sec.querySelector('.shot');
  if (typeof rec.shotTs !== 'number'){
    img.style.display = 'none'; img.removeAttribute('src'); delete img.dataset.src; return;
  }
  // shotTsをキャッシュバスタに使い、画像が更新された時だけ再取得する
  const src = '/shot/' + encodeURIComponent(game) + '?t=' + rec.shotTs;
  if (img.dataset.src !== src){ img.dataset.src = src; img.src = src; }
}

async function updateChart(game, sec){
  const c = ensureChart(game, sec);
  sec.querySelector('.chartnote').style.display = c ? 'none' : 'block';
  if (!c) return;
  const res = await fetch('/history/' + encodeURIComponent(game));
  if (!res.ok) return;
  // Frenzy等のバフによるスパイクで暴れないよう、バフ抜きのbaseCpsを描く。
  // baseCps未対応の旧クライアントのreportはcpsで代用。対数軸は0以下を描画できないため除外する
  const points = (await res.json())
    .map(p => ({ts: p.ts, v: typeof p.baseCps === 'number' ? p.baseCps : p.cps}))
    .filter(p => typeof p.v === 'number' && p.v > 0);
  c.data.labels = points.map(p => new Date(p.ts*1000).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'}));
  c.data.datasets[0].data = points.map(p => p.v);
  c.update();
}

// game 0件時の空表示(初回のloading...置き換え/全ゲーム消滅時の復元)
function updateEmptyState(count){
  let empty = document.getElementById('empty');
  if (count){ if (empty) empty.remove(); return; }
  if (!empty){ empty = el('p', 'empty'); empty.id = 'empty'; document.getElementById('games').appendChild(empty); }
  empty.textContent = 'まだ報告がありません';
}

async function refresh(){
  let st;
  try { st = await (await fetch('/status')).json(); } catch { return; }
  const games = Object.keys(st);
  updateEmptyState(games.length);
  // /statusから消えたゲームのセクションを古い値のまま残さない
  for (const sec of document.querySelectorAll('section[id^="sec-"]')){
    const g = sec.id.slice(4);
    if (!games.includes(g)){ sec.remove(); if (charts[g]){ charts[g].destroy(); delete charts[g]; } }
  }
  for (const game of games){
    // 1ゲームの失敗(グラフ生成エラー等)で他ゲームの描画を止めない
    try {
      const sec = section(game);
      updateCards(sec, st[game]);
      updateAchievements(sec, st[game]);
      updateShot(sec, game, st[game]);
      await updateChart(game, sec);
    } catch (e) { console.warn(e); }
  }
}
refresh(); setInterval(refresh, 30000);
// defer読み込みのChart.jsが初回refresh後に間に合った場合の再描画
window.addEventListener('load', refresh);
