from flask import Flask, abort, jsonify, request
import base64, collections, json, math, os, pathlib, re, tempfile, time

app = Flask(__name__)
DATA = pathlib.Path.home() / "gamehub" / "data"

# URLの<game>はディレクトリ名になるため、パストラバーサル対策として英数等に制限
GAME_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")

# 1日分のreport上限(60秒毎=1440件)より余裕を持った読み込み上限
HISTORY_LIMIT = 3000

# shot便(base64画像)が最大。デコード後上限+base64膨張分(4/3)より広めに取る
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

SHOT_DATAURL = re.compile(r"data:image/(?:png|jpeg);base64,([A-Za-z0-9+/=]+)")
SHOT_MAX_BYTES = 4 * 1024 * 1024
# 形式ごとに別ファイル名にすると並行アップロード同士が互いのファイルを
# 消し合えるため、単一の正準ファイルに書き、形式はマジックバイトで判定する
SHOT_FILE = "shot.img"
SHOT_FORMATS = ((b"\xff\xd8\xff", "image/jpeg"),
                (b"\x89PNG\r\n\x1a\n", "image/png"))


def shot_mime(raw):
    """実データのマジックバイトからMIMEを決める(申告MIMEの偽装対策)"""
    for magic, mime in SHOT_FORMATS:
        if raw.startswith(magic):
            return mime
    return None


def game_dir(game):
    if not GAME_NAME.fullmatch(game):
        abort(404)
    return DATA / game


def today():
    return time.strftime("%Y%m%d")


def write_atomic(path, data):
    """並行リクエストで書きかけ同士が混ざらないよう一時ファイル経由で置き換える"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def finite(value):
    """非有限float(旧コードが書き残したInfinity/NaN)は標準JSONとして
    再出力できず/statusごと壊すため、Noneに落とす"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def read_json(path):
    """壊れたファイル・dict以外・非有限数値入りはNone扱い(表示側の耐性)"""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        json.dumps(record, allow_nan=False)  # 旧データのInfinity/NaN検出
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

    # typeキー自体を送らない旧クライアントのみsaveキーの有無で振り分ける。
    # タイポや空文字などの不正typeをreport扱いに落とすとsave便が
    # latest.jsonを潰す事故が再発するため、既知のtype以外は明示的に拒否する
    if "type" in payload:
        kind = payload["type"]
    else:
        kind = "save" if "save" in payload else "report"
    if kind not in ("save", "report", "shot"):
        abort(400, description="type must be 'save', 'report' or 'shot'")

    if kind == "save":
        # save便はセーブファイルの退避のみ。latest.json/historyには触れない
        # (save便の痩せたpayloadでlatest.jsonが上書きされるバグの修正)
        save = payload.get("save")
        if not isinstance(save, str) or not save:
            abort(400, description="save string required")
        write_atomic(d / f"save_{today()}.txt", save)
        return jsonify(ok=True)

    if kind == "shot":
        # shot便はスクショの保存のみ。latest.json/historyには触れない
        shot = payload.get("shot")
        m = SHOT_DATAURL.fullmatch(shot) if isinstance(shot, str) else None
        if not m:
            abort(400, description="shot must be a png/jpeg data URL")
        try:
            raw = base64.b64decode(m.group(1), validate=True)
        except ValueError:
            abort(400, description="invalid base64")
        if len(raw) > SHOT_MAX_BYTES:
            abort(413, description="image too large")
        if shot_mime(raw) is None:
            abort(400, description="image data is not png/jpeg")
        # write_atomicのos.replaceで後勝ちになるため並行アップロードでも安全
        write_atomic(d / SHOT_FILE, raw)
        return jsonify(ok=True)

    payload.pop("save", None)
    try:
        # Infinity/NaN/1e999等はそのまま書くとlatest.jsonが不正JSONになり
        # /statusの応答ごと壊れてダッシュボード全体が止まるため拒否する
        line = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except ValueError:
        abort(400, description="non-finite numbers not allowed")
    write_atomic(d / "latest.json", line)
    with (d / f"history_{today()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return jsonify(ok=True)


def shot_mtime(d):
    try:
        return (d / SHOT_FILE).stat().st_mtime
    except OSError:
        return None


@app.get("/status")
def status():
    out = {}
    if DATA.is_dir():
        for g in sorted(DATA.iterdir()):
            record = read_json(g / "latest.json") if g.is_dir() else None
            if record is not None:
                # クライアント申告でなくファイル実体から算出(表示側のキャッシュ更新判定用)
                mtime = shot_mtime(g)
                if mtime is not None:
                    record["shotTs"] = mtime
                out[g.name] = record
    return jsonify(out)


@app.get("/shot/<game>")
def shot(game):
    d = game_dir(game)
    try:
        # 判定と送信の間で置換されても不整合にならないよう一度読み切る(上限4MB)
        raw = (d / SHOT_FILE).read_bytes()
    except OSError:
        abort(404)
    mime = shot_mime(raw)
    if mime is None:
        abort(404)  # 手動操作等で壊れたファイルが置かれていた場合
    return app.response_class(raw, mimetype=mime)


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
                        "ts": finite(record.get("ts")),
                        "cps": finite(record.get("cps")),
                        "baseCps": finite(record.get("baseCps")),
                        "cookies": finite(record.get("cookies")),
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
  .shot{display:none;width:100%;max-height:70vh;object-fit:contain;
        background:#16213e;border-radius:.6em;margin-top:.6em}
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
// game名 -> Chart。'constructor'等のgame名がプロトタイプと衝突しないよう
// プロトタイプなしオブジェクトを使う
const charts = Object.create(null);

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
  const img = document.createElement('img');
  img.className = 'shot';
  img.alt = 'screenshot';
  // 読み込み成功時のみ表示(未送信・配信エラー時に壊れた画像アイコンを出さない)
  img.addEventListener('load', () => { img.style.display = 'block'; });
  img.addEventListener('error', () => {
    img.style.display = 'none';
    // 一時的な取得失敗を次回refreshで再試行できるよう読込済み判定を破棄
    delete img.dataset.src;
  });
  sec.appendChild(img);
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
      plugins:{legend:{display:false},title:{display:true,
        text:'ベースCpS (today, 対数目盛)',color:'#9aa4c7',font:{size:11}}},
      scales:{
        x:{ticks:{color:'#9aa4c7',maxTicksLimit:6},grid:{color:'#26305c'}},
        // CpSは日内でも桁が跳ね上がり線形軸だと序盤が潰れるため対数軸にする
        y:{type:'logarithmic',ticks:{color:'#9aa4c7',maxTicksLimit:6,
          callback:v=>fmt(v)},grid:{color:'#26305c'}}}}
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
  let meta = ts ? '最終報告: ' + ts.toLocaleTimeString('ja-JP') : '';
  if (typeof rec.shotTs === 'number'){
    meta += (meta ? ' / ' : '') + 'スクショ: ' +
      new Date(rec.shotTs*1000).toLocaleTimeString('ja-JP');
  }
  sec.querySelector('.meta').textContent = meta;
}

function updateShot(sec, game, rec){
  const img = sec.querySelector('.shot');
  if (typeof rec.shotTs !== 'number'){
    img.style.display = 'none';
    img.removeAttribute('src');
    delete img.dataset.src;
    return;
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
  // baseCps未対応の旧クライアントのreportはcpsで代用。
  // 対数軸は0以下を描画できないため除外する
  const points = (await res.json())
    .map(p => ({ts: p.ts, v: typeof p.baseCps === 'number' ? p.baseCps : p.cps}))
    .filter(p => typeof p.v === 'number' && p.v > 0);
  c.data.labels = points.map(p => new Date(p.ts*1000)
    .toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'}));
  c.data.datasets[0].data = points.map(p => p.v);
  c.update();
}

// game 0件時の空表示(初回のloading...置き換え/全ゲーム消滅時の復元)
function updateEmptyState(count){
  let empty = document.getElementById('empty');
  if (count){ if (empty) empty.remove(); return; }
  if (!empty){
    empty = document.createElement('p');
    empty.id = 'empty';
    document.getElementById('games').appendChild(empty);
  }
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
    if (!games.includes(g)){
      sec.remove();
      if (charts[g]){ charts[g].destroy(); delete charts[g]; }
    }
  }
  for (const game of games){
    // 1ゲームの失敗(グラフ生成エラー等)で他ゲームの描画を止めない
    try {
      const sec = section(game);
      updateCards(sec, st[game]);
      updateShot(sec, game, st[game]);
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
