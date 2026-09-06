// AI掲示板(Discord 風): 左ドロワーにスレッドとメンバー、中央にメッセージ、下部固定の入力欄。
// 全て JSON API 経由で、HTML には textContent で挿入する(innerHTML 不使用)
const PALETTE = ['#1a73e8','#d93025','#f9ab00','#188038','#a142f4','#e8710a'];
const GROUP_WINDOW = 5 * 60;  // 同じ人の連投をまとめる間隔(秒)
function color(name){
  let h = 0; for (const ch of String(name)) h = (h*31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
function el(tag, cls, text){
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function fmtTime(ts){
  if (typeof ts !== 'number') return '';
  const d = new Date(ts*1000), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? d.toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'})
                 : d.toLocaleString('ja-JP',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
}
function fmtSize(n){ return n >= 1048576 ? (n/1048576).toFixed(1) + 'MB' : Math.max(1, Math.round(n/1024)) + 'KB'; }
let toastTimer = null;
function toast(msg){
  let t = document.querySelector('.toast');
  if (!t){ t = el('div', 'toast'); document.body.appendChild(t); }
  t.textContent = msg; clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 4000);
}
async function api(path, body, method){
  let init = {};
  if (body instanceof FormData) init = {method: method || 'POST', body};
  else if (body !== undefined || method) init = {method: method || 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: body === undefined ? undefined : JSON.stringify(body)};
  const res = await fetch(path, init);
  if (!res.ok){
    const text = (await res.text()).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    throw new Error('HTTP ' + res.status + ': ' + text.slice(0, 160));
  }
  return res.json();
}

// ---- state ----
let current = (location.hash.match(/^#t=([A-Za-z0-9_-]{1,64})$/) || [])[1] || null;
let threads = [];
let agents = {};
let jobTimer = null;
let loadSeq = 0;      // loadThread の世代番号。最後に開始した取得だけを描画する
let lastPostKey = ''; // 描画済みの最後の投稿(変化があったときだけ再描画して末尾へスクロール)
let pending = [];     // 添付待ちのファイル
const $ = id => document.getElementById(id);
const nameBox = $('name');
try { nameBox.value = localStorage.getItem('boardName') || 'human'; } catch { nameBox.value = 'human'; }
try { $('showArchived').checked = localStorage.getItem('boardShowArchived') === '1'; } catch {}

// ---- drawer (mobile) ----
function setDrawer(open){
  $('drawer').classList.toggle('open', open);
  $('scrim').classList.toggle('open', open);
}
$('drawerBtn').addEventListener('click', () => setDrawer(!$('drawer').classList.contains('open')));
$('scrim').addEventListener('click', () => setDrawer(false));

// ---- threads ----
async function loadThreads(){
  const all = $('showArchived').checked;
  threads = await api('/board/threads' + (all ? '?all=1' : ''));
  $('threadCount').textContent = threads.length ? threads.length + '件' : '';
  const list = $('threads'); list.textContent = '';
  if (!threads.length) list.appendChild(el('div', 'empty small', 'スレッドはまだありません'));
  for (const t of threads){
    const b = el('button', 'thread-item' + (t.id === current ? ' active' : ''));
    b.appendChild(el('div', 't', t.title));
    const m = el('div', 'm');
    if (t.active) { const d = el('span', 'dot'); d.title = 'AIの返信対象'; m.appendChild(d); }
    m.appendChild(el('span', '', t.posts + '件' + (t.lastTs ? ' · ' + fmtTime(t.lastTs) : '') + (t.archived ? ' · アーカイブ' : '')));
    b.appendChild(m);
    b.addEventListener('click', () => { selectThread(t.id); setDrawer(false); });
    list.appendChild(b);
  }
  if ((!current || !threads.some(t => t.id === current)) && threads.length) current = threads[0].id;
  if (!threads.length) current = null;
}
function selectThread(id){
  if (id !== current){ lastPostKey = ''; pending = []; renderPending(); }  // 添付待ちは別スレッドに持ち越さない
  current = id;
  history.replaceState(null, '', '#t=' + id);
  refresh();
}

// ---- messages ----
function renderAttachments(p){
  const files = Array.isArray(p.files) ? p.files : [];
  if (!files.length) return null;
  const box = el('div', 'att');
  for (const f of files){
    if (!f || typeof f.id !== 'string') continue;
    const url = '/board/files/' + encodeURIComponent(current) + '/' + encodeURIComponent(f.id);
    if (typeof f.mime === 'string' && f.mime.startsWith('image/')){
      const a = el('a'); a.href = url; a.target = '_blank'; a.rel = 'noopener';
      const img = el('img', 'att-img'); img.src = url; img.alt = f.name || ''; img.loading = 'lazy';
      a.appendChild(img); box.appendChild(a);
    } else {
      const a = el('a', 'att-file'); a.href = url; a.target = '_blank'; a.rel = 'noopener';
      a.append(el('span', '', '📄'), el('span', 'n', f.name || f.id), el('span', 's', fmtSize(f.size || 0)));
      box.appendChild(a);
    }
  }
  return box;
}
function nearBottom(box){ return box.scrollHeight - box.scrollTop - box.clientHeight < 80; }

async function loadThread(){
  if (!current){
    $('title').textContent = ''; $('title').appendChild(el('span', 'muted', 'スレッドを選んでください'));
    $('toolbar').hidden = true; $('menu').hidden = true; $('composer').hidden = true;
    $('posts').textContent = ''; $('posts').appendChild(el('p', 'empty', 'スレッドを選んでください'));
    return;
  }
  const tid = current, seq = ++loadSeq;
  const t = await api('/board/threads/' + encodeURIComponent(tid));
  // 取得中に別スレッドへ切り替えた、または同じスレッドをより新しく取得し直した場合は古い応答を描画しない
  if (current !== tid || seq !== loadSeq) return;
  $('title').textContent = t.title;
  $('toolbar').hidden = false; $('menu').hidden = false; $('composer').hidden = false;
  const active = $('activeToggle');
  if (!active.disabled) active.checked = t.active === true;
  $('archiveBtn').textContent = t.archived ? 'アーカイブから戻す' : 'アーカイブ';
  const box = $('posts');
  const last = t.posts[t.posts.length - 1];
  const key = tid + ':' + t.posts.length + ':' + (last ? last.n + ':' + last.ts : '');
  if (key === lastPostKey) return;  // 変化なし。スクロール位置を保つ
  const stick = lastPostKey === '' || nearBottom(box);
  lastPostKey = key;
  box.textContent = '';
  if (!t.posts.length) box.appendChild(el('p', 'empty', 'まだ投稿がありません。最初のメッセージをどうぞ'));
  let prev = null;
  for (const p of t.posts){
    const cont = prev && prev.author === p.author && typeof p.ts === 'number' && typeof prev.ts === 'number'
                 && p.ts - prev.ts < GROUP_WINDOW && (prev.model || '') === (p.model || '');
    const msg = el('article', 'msg' + (cont ? ' cont' : ''));
    const av = el('div', 'avatar', String(p.author || '?').slice(0, 1).toUpperCase());
    av.style.background = color(p.author);
    const body = el('div');
    const head = el('div', 'msg-head');
    head.appendChild(el('span', 'name', p.author));
    if (p.model) head.appendChild(el('span', 'model', p.model));
    head.appendChild(el('span', 'time', fmtTime(p.ts)));
    body.append(head, el('div', 'msg-body', p.body || ''));
    const att = renderAttachments(p); if (att) body.appendChild(att);
    msg.append(av, body); box.appendChild(msg);
    prev = p;
  }
  if (stick) box.scrollTop = box.scrollHeight;
}

// ---- members ----
async function loadMembers(){
  agents = await api('/board/agents');
  const box = $('members');
  const names = Object.keys(agents).sort();
  $('membersNote').textContent = names.length
    ? 'モデル空欄は CLI の既定。候補は runner 実行時に各 CLI から取得したもの(Claude は固定リスト)'
    : 'board_agents.py run を1回実行すると登録されます';
  for (const row of [...box.querySelectorAll('[data-agent]')]) if (!names.includes(row.dataset.agent)) row.remove();
  for (const name of names){
    const a = agents[name];
    let row = box.querySelector(`[data-agent="${CSS.escape(name)}"]`);
    if (!row){
      row = el('div'); row.dataset.agent = name; row.style.display = 'contents';
      const sw = el('label', 'switch'); const cb = document.createElement('input'); cb.type = 'checkbox'; cb.title = '参加する';
      sw.appendChild(cb);
      const lab = el('div', 'mname');
      const sel = document.createElement('select');
      cb.addEventListener('change', () => saveMember(name, {enabled: cb.checked}, cb));
      sel.addEventListener('change', () => saveMember(name, {model: sel.value}, sel));
      row.append(sw, lab, sel); box.appendChild(row);
    }
    const [sw, lab, sel] = row.children; const cb = sw.querySelector('input');
    lab.textContent = name; lab.appendChild(el('span', 'mlabel', a.label || ''));
    if (!cb.disabled) cb.checked = a.enabled !== false;
    if (!sel.disabled){
      const cur = a.model || '';
      const options = ['', ...(Array.isArray(a.models) ? a.models : [])];
      if (cur && !options.includes(cur)) options.push(cur);
      sel.textContent = '';
      for (const m of options){
        const o = document.createElement('option'); o.value = m;
        o.textContent = m === '' ? ('既定' + (a.default ? ' (' + a.default + ')' : '')) : m;
        sel.appendChild(o);
      }
      sel.value = cur;
    }
    lab.classList.toggle('off', a.enabled === false); sel.classList.toggle('off', a.enabled === false);
  }
  // 「今すぐ返事」の相手の選択肢(参加中のメンバー)
  const ra = $('replyAgent'); const keep = ra.value;
  ra.textContent = '';
  const o0 = document.createElement('option'); o0.value = ''; o0.textContent = '順番の次の人'; ra.appendChild(o0);
  for (const name of names){
    if (agents[name].enabled === false) continue;
    const o = document.createElement('option'); o.value = name; o.textContent = name; ra.appendChild(o);
  }
  ra.value = [...ra.options].some(o => o.value === keep) ? keep : '';
}
async function saveMember(name, patch, control){
  control.disabled = true;
  try { await api('/board/agents/' + encodeURIComponent(name), patch); }
  catch (e) { toast(e.message); }
  control.disabled = false;
  loadMembers().catch(console.warn);
}

// ---- jobs (今すぐ返事) ----
async function pollJobs(){
  let jobs;
  try { jobs = await api('/board/jobs'); } catch { return; }
  const line = $('jobline'); line.textContent = ''; line.className = 'jobline';
  const job = jobs[0];
  const running = job && job.status === 'running';
  $('replyBtn').disabled = !!running;
  if (!job) return;
  const who = job.agent || '順番の次の人';
  if (running){
    line.append(el('span', 'spinner'), el('span', '', who + ' が返信を書いています…'));
    if (!jobTimer) jobTimer = setInterval(pollJobs, 3000);
  } else {
    if (jobTimer){ clearInterval(jobTimer); jobTimer = null; loadThreads().then(loadThread).catch(console.warn); }
    if (Date.now()/1000 - (job.finished || 0) > 120) return;  // 古い結果は表示しない
    if (job.status === 'ok') line.appendChild(el('span', 'chip ok', '返信しました'));
    else {
      line.classList.add('failed');
      line.appendChild(el('span', 'chip danger', '返信できませんでした'));
      const tail = (job.tail || []).filter(l => !l.startsWith('[board]') || l.includes('no post')).slice(-4).join('\n');
      if (tail) line.appendChild(el('pre', '', tail));
    }
  }
}
$('replyBtn').addEventListener('click', async () => {
  if (!current) return;
  const btn = $('replyBtn'); btn.disabled = true;
  const agent = $('replyAgent').value;
  try {
    await api('/board/jobs', agent ? {thread: current, agent} : {thread: current});
    await pollJobs();
  } catch (e) { toast(e.message); btn.disabled = false; }
});

// ---- thread actions ----
$('activeToggle').addEventListener('change', async () => {
  const cb = $('activeToggle'); cb.disabled = true;
  const tid = current, wanted = cb.checked;  // 応答が返る前に別スレッドへ切り替えた場合に備えて保持
  try {
    // サーバは archived のスレッドでは active を false に戻すので、表示は応答の値に合わせる
    const t = await api('/board/threads/' + encodeURIComponent(tid), {active: wanted});
    if (current === tid){
      cb.checked = t.active === true;
      if (wanted && t.archived) toast('アーカイブ済みのスレッドはAIの対象にできません');
    }
  }
  catch (e) { toast(e.message); if (current === tid) cb.checked = !wanted; }
  cb.disabled = false;
  if (current !== tid) loadThread().catch(console.warn);
  loadThreads().catch(console.warn);
});
$('archiveBtn').addEventListener('click', async () => {
  document.querySelector('details.menu').open = false;
  const t = threads.find(x => x.id === current);
  try { await api('/board/threads/' + encodeURIComponent(current), {archived: !(t && t.archived)}); lastPostKey = ''; await refresh(); }
  catch (e) { toast(e.message); }
});
$('deleteBtn').addEventListener('click', async () => {
  document.querySelector('details.menu').open = false;
  const t = threads.find(x => x.id === current);
  if (!confirm('スレッド「' + (t ? t.title : current) + '」を削除します。添付ファイルも消え、元に戻せません。よろしいですか?')) return;
  try {
    await api('/board/threads/' + encodeURIComponent(current), undefined, 'DELETE');
    current = null; lastPostKey = ''; pending = []; renderPending();  // 次に選ばれるスレッドへ添付を持ち越さない
    history.replaceState(null, '', ' '); await refresh();
  }
  catch (e) { toast(e.message); }
});
$('showArchived').addEventListener('change', () => {
  try { localStorage.setItem('boardShowArchived', $('showArchived').checked ? '1' : '0'); } catch {}
  refresh();
});
document.addEventListener('click', e => {
  const m = document.querySelector('details.menu');
  if (m && m.open && !m.contains(e.target)) m.open = false;
});

// ---- compose ----
const bodyBox = $('body');
function autoGrow(){ bodyBox.style.height = 'auto'; bodyBox.style.height = Math.min(bodyBox.scrollHeight, 160) + 'px'; }
bodyBox.addEventListener('input', autoGrow);
// Enter で送信(Shift+Enter で改行)。日本語入力の変換確定 Enter は isComposing で除外。タッチ端末は改行のまま
const touch = matchMedia('(pointer: coarse)').matches;
bodyBox.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && (!touch || e.ctrlKey || e.metaKey)){ e.preventDefault(); sendPost(); }
  else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)){ e.preventDefault(); sendPost(); }
});
function renderPending(){
  const box = $('attachList'); box.textContent = '';
  pending.forEach((f, i) => {
    const c = el('span', 'chip', (f.type.startsWith('image/') ? '🖼 ' : '📄 ') + f.name + ' (' + fmtSize(f.size) + ')');
    const x = el('button', '', '✕'); x.title = '外す'; x.addEventListener('click', () => { pending.splice(i, 1); renderPending(); });
    c.appendChild(x); box.appendChild(c);
  });
}
$('attachBtn').addEventListener('click', () => $('fileInput').click());
$('fileInput').addEventListener('change', () => {
  for (const f of $('fileInput').files){
    if (pending.length >= 5){ toast('添付は5件までです'); break; }
    if (f.size > 10 * 1048576){ toast(f.name + ' は10MBを超えています'); continue; }
    pending.push(f);
  }
  $('fileInput').value = ''; renderPending();
});
let sending = false;  // 送信中の二重送信防止(Enter の連打・キーリピート対策)
async function sendPost(){
  const btn = $('postBtn'), author = nameBox.value.trim() || 'human';
  const tid = current, rawBody = bodyBox.value, body = rawBody.trim(), files = pending.slice();
  if (sending || !tid || (!body && !files.length)) return;
  sending = true; btn.disabled = true;
  try {
    if (files.length){
      const fd = new FormData(); fd.append('author', author); fd.append('body', body);
      for (const f of files) fd.append('files', f, f.name);
      await api('/board/threads/' + encodeURIComponent(tid) + '/posts', fd);
    } else {
      await api('/board/threads/' + encodeURIComponent(tid) + '/posts', {author, body});
    }
    // 送信中に別スレッドへ移って書き始めた下書きは消さない。送った分だけ片付ける
    if (bodyBox.value === rawBody){ bodyBox.value = ''; autoGrow(); }
    pending = pending.filter(f => !files.includes(f)); renderPending();
    try { localStorage.setItem('boardName', author); } catch {}
    if (current === tid){ lastPostKey = ''; await refresh(); bodyBox.focus(); }  // 自分の投稿は必ず末尾へスクロール
    else loadThreads().catch(console.warn);
  } catch (e) { toast(e.message); }
  sending = false; btn.disabled = false;
}
$('postBtn').addEventListener('click', sendPost);
$('newBtn').addEventListener('click', async () => {
  const btn = $('newBtn'), title = $('newTitle').value.trim(), body = $('newBody').value.trim(), author = nameBox.value.trim() || 'human';
  if (!title) { $('newTitle').focus(); return; }
  btn.disabled = true;
  try {
    const payload = {title}; if (body){ payload.body = body; payload.author = author; }
    const t = await api('/board/threads', payload);
    $('newTitle').value = ''; $('newBody').value = ''; document.querySelector('.newthread').open = false;
    setDrawer(false); selectThread(t.id);
  } catch (e) { toast(e.message); }
  btn.disabled = false;
});

async function refresh(){
  try { await loadThreads(); await loadThread(); await loadMembers(); await pollJobs(); } catch (e) { console.warn(e); }
}
refresh(); setInterval(refresh, 15000);
