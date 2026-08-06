from flask import Flask, abort, jsonify, request
import collections, json, os, pathlib, re, tempfile, time

app = Flask(__name__)
DATA = pathlib.Path.home() / "gamehub" / "data"

# URLの<game>はディレクトリ名になるため、パストラバーサル対策として英数等に制限
GAME_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")

# 1日分のreport上限(60秒毎=1440件)より余裕を持った読み込み上限
HISTORY_LIMIT = 3000


def game_dir(game):
    if not GAME_NAME.fullmatch(game):
        abort(404)
    return DATA / game


def today():
    return time.strftime("%Y%m%d")


def write_atomic(path, text):
    """並行リクエストで書きかけ同士が混ざらないよう一時ファイル経由で置き換える"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    """壊れたファイルやdict以外はNone扱いで握りつぶす(表示側の耐性)"""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


@app.post("/report/<game>")
def report(game):
    d = game_dir(game)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description="JSON object required")
    payload["ts"] = time.time()
    d.mkdir(parents=True, exist_ok=True)

    # typeを送らない旧クライアントはsaveキーの有無で振り分ける。
    # タイポ等の未知typeをreport扱いに落とすとsave便がlatest.jsonを潰す
    # 事故が再発するため、save/report以外は明示的に拒否する
    kind = payload.get("type") or ("save" if "save" in payload else "report")
    if kind not in ("save", "report"):
        abort(400, description="type must be 'save' or 'report'")

    if kind == "save":
        # save便はセーブファイルの退避のみ。latest.json/historyには触れない
        # (save便の痩せたpayloadでlatest.jsonが上書きされるバグの修正)
        save = payload.get("save")
        if not isinstance(save, str) or not save:
            abort(400, description="save string required")
        write_atomic(d / f"save_{today()}.txt", save)
        return jsonify(ok=True)

    payload.pop("save", None)
    line = json.dumps(payload, ensure_ascii=False)
    write_atomic(d / "latest.json", line)
    with (d / f"history_{today()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return jsonify(ok=True)


@app.get("/status")
def status():
    out = {}
    if DATA.is_dir():
        for g in sorted(DATA.iterdir()):
            record = read_json(g / "latest.json") if g.is_dir() else None
            if record is not None:
                out[g.name] = record
    return jsonify(out)


@app.get("/history/<game>")
def history(game):
    """当日のreport履歴(ダッシュボードのグラフ用に間引いた形で返す)"""
    d = game_dir(game)
    # 上限超過時はファイル先頭ではなく直近側を残す(グラフが凍らないように)
    points = collections.deque(maxlen=HISTORY_LIMIT)
    path = d / f"history_{today()}.jsonl"
    if path.is_file():
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    points.append({
                        "ts": record.get("ts"),
                        "cps": record.get("cps"),
                        "cookies": record.get("cookies"),
                    })
    return jsonify(list(points))


@app.get("/")
def index():
    return """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Game Hub</title>
<!-- deferで読み込み、CDN不通/低速でもカード表示をブロックしない -->
<script defer src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body{font-family:system-ui,sans-serif;background:#1a1a2e;color:#eee;
       margin:0;padding:1em;max-width:640px;margin-inline:auto}
  h1{font-size:1.3em;margin:.2em 0 .8em}
  h2{font-size:1.05em;margin:1.2em 0 .5em;color:#ffd166;text-transform:capitalize}
  .cards{display:grid;grid-template-columns:repeat(2,1fr);gap:.6em}
  .card{background:#16213e;border-radius:.6em;padding:.7em .9em}
  .card .label{font-size:.72em;color:#9aa4c7}
  .card .value{font-size:1.5em;font-weight:700;margin-top:.15em;
               font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
  .chartbox{background:#16213e;border-radius:.6em;padding:.7em;margin-top:.6em;height:200px}
  .chartnote{display:none;color:#9aa4c7;font-size:.8em;padding:.5em}
  .meta{font-size:.72em;color:#9aa4c7;margin-top:.4em}
  #empty{color:#9aa4c7}
</style></head><body>
<h1>🎮 Game Hub</h1>
<div id="games"><p id="empty">loading...</p></div>
<script>
const CARDS = [
  ['cookies','🍪 cookies'], ['cps','⚡ CpS'],
  ['elderWrath','👵 elderWrath'], ['wrinklers','🐛 wrinklers'],
];
const WRATH = ['平穏','ざわめき','高まり','黙示録'];
const charts = {};   // game名 -> Chart

function fmt(v){
  if (typeof v !== 'number' || !isFinite(v)) return v ?? '-';
  if (Math.abs(v) >= 1e15) return v.toExponential(2);
  return new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(v);
}

// gameごとのセクションをDOM APIで組み立てる(game名等をinnerHTMLに混ぜない)
function section(game){
  const id = 'sec-' + game;
  let sec = document.getElementById(id);
  if (sec) return sec;
  sec = document.createElement('section');
  sec.id = id;
  const h2 = document.createElement('h2');
  h2.textContent = game;
  sec.appendChild(h2);
  const cards = document.createElement('div');
  cards.className = 'cards';
  for (const [key,label] of CARDS){
    const card = document.createElement('div');
    card.className = 'card';
    const l = document.createElement('div');
    l.className = 'label'; l.textContent = label;
    const v = document.createElement('div');
    v.className = 'value'; v.dataset.key = key; v.textContent = '-';
    card.append(l,v); cards.appendChild(card);
  }
  sec.appendChild(cards);
  const box = document.createElement('div');
  box.className = 'chartbox';
  const canvas = document.createElement('canvas');
  const note = document.createElement('div');
  note.className = 'chartnote';
  note.textContent = 'グラフを表示できません(Chart.js 未読込)';
  box.append(canvas, note); sec.appendChild(box);
  const meta = document.createElement('div');
  meta.className = 'meta'; sec.appendChild(meta);
  document.getElementById('games').appendChild(sec);
  return sec;
}

// Chart.js(defer/CDN)がまだ無ければ作らず、後続のrefreshで再試行する
function ensureChart(game, sec){
  if (charts[game]) return charts[game];
  if (typeof Chart === 'undefined') return null;
  charts[game] = new Chart(sec.querySelector('canvas'), {
    type:'line',
    data:{labels:[],datasets:[{data:[],borderColor:'#4cc9f0',
      backgroundColor:'rgba(76,201,240,.15)',fill:true,tension:.3,
      pointRadius:0,borderWidth:2}]},
    options:{responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:false},title:{display:true,text:'CpS (today)',
        color:'#9aa4c7',font:{size:11}}},
      scales:{
        x:{ticks:{color:'#9aa4c7',maxTicksLimit:6},grid:{color:'#26305c'}},
        y:{ticks:{color:'#9aa4c7',callback:v=>fmt(v)},grid:{color:'#26305c'}}}}
  });
  return charts[game];
}

function updateCards(sec, rec){
  for (const el of sec.querySelectorAll('.value')){
    const key = el.dataset.key;
    let v = rec[key];
    if (key === 'elderWrath' && Number.isInteger(v) && WRATH[v]) v = WRATH[v];
    else v = fmt(v);
    el.textContent = v;
  }
  const ts = typeof rec.ts === 'number' ? new Date(rec.ts*1000) : null;
  sec.querySelector('.meta').textContent =
    ts ? '最終報告: ' + ts.toLocaleTimeString('ja-JP') : '';
}

async function updateChart(game, sec){
  const c = ensureChart(game, sec);
  sec.querySelector('.chartnote').style.display = c ? 'none' : 'block';
  if (!c) return;
  const res = await fetch('/history/' + encodeURIComponent(game));
  if (!res.ok) return;
  const points = (await res.json()).filter(p => typeof p.cps === 'number');
  c.data.labels = points.map(p => new Date(p.ts*1000)
    .toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'}));
  c.data.datasets[0].data = points.map(p => p.cps);
  c.update();
}

async function refresh(){
  let st;
  try { st = await (await fetch('/status')).json(); } catch { return; }
  const games = Object.keys(st);
  const empty = document.getElementById('empty');
  if (empty && games.length) empty.remove();
  for (const game of games){
    // 1ゲームの失敗(グラフ生成エラー等)で他ゲームの描画を止めない
    try {
      const sec = section(game);
      updateCards(sec, st[game]);
      await updateChart(game, sec);
    } catch (e) { console.warn(e); }
  }
}
refresh(); setInterval(refresh, 30000);
// defer読み込みのChart.jsが初回refresh後に間に合った場合の再描画
window.addEventListener('load', refresh);
</script></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
