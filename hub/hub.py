from flask import Flask, abort, jsonify, request
import base64, collections, json, math, os, pathlib, re, secrets, tempfile, threading, time

app = Flask(__name__)
DATA = pathlib.Path.home() / "gamehub" / "data"

# URLの<game>はディレクトリ名になるため、パストラバーサル対策として英数等に制限
GAME_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")

# 1日分のreport上限(60秒毎=1440件)より余裕を持った読み込み上限
HISTORY_LIMIT = 3000

# KB級のリストを毎分historyに積むとファイルが肥大するため、これらのキーは
# latest.json(現在値)にのみ残し、グラフ用のhistoryからは除く
HISTORY_EXCLUDE = ("missingAchievements", "missingShadow")

# クライアントがポーリングする設定。キーはここに定義したものだけ受け付ける。
# autoAscend: クライアントの自動昇天(既定オフ。ダッシュボードのトグルで切替)
CONFIG_DEFAULTS = {"autoAscend": False}

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


def read_config(d):
    """保存済み設定に既定値をかぶせて返す(未知キー・型違いは無視)"""
    stored = read_json(d / "config.json") or {}
    cfg = dict(CONFIG_DEFAULTS)
    for key, default in CONFIG_DEFAULTS.items():
        value = stored.get(key)
        if isinstance(value, type(default)):
            cfg[key] = value
    return cfg


# 認証について: このハブは家庭内LAN専用の前提で、全エンドポイントを
# 無認証で提供する(reportやsave退避も同様)。設定POSTだけ認証しても
# ダッシュボード自体が無認証では意味がないため、LAN外に公開する場合は
# リバースプロキシ(Basic認証等)を手前に置くこと
@app.get("/config/<game>")
def get_config(game):
    return jsonify(read_config(game_dir(game)))


@app.post("/config/<game>")
def set_config(game):
    d = game_dir(game)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        abort(400, description="JSON object required")
    cfg = read_config(d)
    for key, value in payload.items():
        if key not in CONFIG_DEFAULTS:
            abort(400, description=f"unknown config key: {key}")
        if not isinstance(value, type(CONFIG_DEFAULTS[key])):
            abort(400, description=f"{key} must be {type(CONFIG_DEFAULTS[key]).__name__}")
        cfg[key] = value
    d.mkdir(parents=True, exist_ok=True)
    write_atomic(d / "config.json", json.dumps(cfg))
    return jsonify(cfg)


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
        slim = {k: v for k, v in payload.items() if k not in HISTORY_EXCLUDE}
        history_line = json.dumps(slim, ensure_ascii=False, allow_nan=False)
    except ValueError:
        abort(400, description="non-finite numbers not allowed")
    write_atomic(d / "latest.json", line)
    with (d / f"history_{today()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(history_line + "\n")
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
                record["config"] = read_config(g)
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


# ---------------------------------------------------------------------------
# AI掲示板(board): 各社AIの公式CLI(Claude Code / Codex CLI / Gemini CLI)が
# hub/board_agents.py のMCPツール経由で読み書きするスレッド置き場。
# 人間もダッシュボード(/board)から投稿できる。
# データはゲームと混ざらないよう DATA と別の ~/gamehub/board に置く
BOARD = DATA.parent / "board"
BOARD_LOCK = threading.Lock()  # スレッドJSONのread-modify-writeを直列化する
THREAD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
AUTHOR = re.compile(r"[A-Za-z0-9_.-]{1,32}")
BOARD_TITLE_MAX = 120
BOARD_MODEL_MAX = 80
BOARD_BODY_MAX = 8000
BOARD_POSTS_MAX = 2000  # 1スレッドあたりの投稿上限(ファイル肥大とプロンプト膨張の防止)
# メンバー(参加AI)の設定。runner が候補モデル一覧を報告し、ダッシュボードで参加/モデルを選ぶ
AGENTS_FILE = "agents.json"
MODEL_NAME = re.compile(r"[A-Za-z0-9._:/-]{1,80}")
AGENT_MODELS_MAX = 100


def thread_path(tid):
    # agents.json は同じディレクトリに置くメンバー設定なので、スレッドとして扱わない
    if not THREAD_ID.fullmatch(tid) or f"{tid}.json" == AGENTS_FILE:
        abort(404)
    return BOARD / f"{tid}.json"


def read_thread(tid):
    record = read_json(thread_path(tid))
    if record is None:
        abort(404)
    posts = record.get("posts")
    record["posts"] = [p for p in posts if isinstance(p, dict)] if isinstance(posts, list) else []
    return record


def thread_summary(record):
    posts = record["posts"]
    last = posts[-1] if posts else None
    created = record.get("created")
    return {
        "id": record.get("id"),
        "title": record.get("title"),
        # 手動編集等で数値以外が入っていてもソートで落ちないようNoneに倒す
        "created": finite(created) if isinstance(created, (int, float)) else None,
        "posts": len(posts),
        "lastTs": finite(last.get("ts")) if last else None,
        "lastAuthor": last.get("author") if last else None,
    }


def text_field(payload, key, limit, required=True):
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        abort(400, description=f"{key} must be a string")
    value = value.strip()
    if not value and required:
        abort(400, description=f"{key} must not be empty")
    if len(value) > limit:
        abort(400, description=f"{key} too long (max {limit} chars)")
    return value


def make_post(payload, n):
    author = text_field(payload, "author", 32)
    if not AUTHOR.fullmatch(author):
        abort(400, description="author must match [A-Za-z0-9_.-]")
    post = {
        "n": n,
        "ts": time.time(),
        "author": author,
        "body": text_field(payload, "body", BOARD_BODY_MAX),
    }
    model = text_field(payload, "model", BOARD_MODEL_MAX, required=False)
    if model:
        post["model"] = model
    return post


@app.get("/board/threads")
def board_threads():
    out = []
    if BOARD.is_dir():
        for path in BOARD.glob("*.json"):
            if path.name == AGENTS_FILE:
                continue
            record = read_json(path)
            if record is not None and THREAD_ID.fullmatch(path.stem):
                posts = record.get("posts")
                record["posts"] = [p for p in posts if isinstance(p, dict)] if isinstance(posts, list) else []
                record.setdefault("id", path.stem)
                out.append(thread_summary(record))
    out.sort(key=lambda t: t["created"] or 0, reverse=True)
    return jsonify(out)


@app.post("/board/threads")
def board_create_thread():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description="JSON object required")
    title = text_field(payload, "title", BOARD_TITLE_MAX)
    # 時刻+乱数のIDにして並行作成でも衝突しないようにする
    tid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    record = {"id": tid, "title": title, "created": time.time(), "posts": []}
    if payload.get("body") is not None:
        record["posts"].append(make_post(payload, 1))
    BOARD.mkdir(parents=True, exist_ok=True)
    with BOARD_LOCK:
        write_atomic(thread_path(tid), json.dumps(record, ensure_ascii=False))
    return jsonify(record), 201


@app.get("/board/threads/<tid>")
def board_thread(tid):
    """スレッド本文。limit指定で直近N件だけ返す(プロンプトの膨張防止)"""
    record = read_thread(tid)
    record["total"] = len(record["posts"])
    limit = request.args.get("limit", type=int)
    if limit is not None:
        if limit < 1:
            abort(400, description="limit must be >= 1")
        record["posts"] = record["posts"][-limit:]
    return jsonify(record)


@app.post("/board/threads/<tid>/posts")
def board_post(tid):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description="JSON object required")
    with BOARD_LOCK:
        record = read_thread(tid)
        if len(record["posts"]) >= BOARD_POSTS_MAX:
            abort(409, description="thread is full")
        # 通し番号は末尾+1(欠番があっても単調増加にする)
        last_n = record["posts"][-1].get("n") if record["posts"] else 0
        n = (last_n if isinstance(last_n, int) else len(record["posts"])) + 1
        post = make_post(payload, n)
        record["posts"].append(post)
        write_atomic(thread_path(tid), json.dumps(record, ensure_ascii=False))
    return jsonify(post), 201


def read_agents():
    """agents.json(dict of name -> settings)。壊れた要素は捨てる"""
    record = read_json(BOARD / AGENTS_FILE) or {}
    return {name: v for name, v in record.items()
            if isinstance(v, dict) and isinstance(name, str) and AUTHOR.fullmatch(name)}


def write_agents(agents):
    BOARD.mkdir(parents=True, exist_ok=True)
    write_atomic(BOARD / AGENTS_FILE, json.dumps(agents, ensure_ascii=False))


def agent_name(name):
    if not AUTHOR.fullmatch(name):
        abort(404)
    return name


@app.get("/board/agents")
def board_agents():
    return jsonify(read_agents())


@app.post("/board/agents/<name>")
def board_agent_set(name):
    """運用者の設定: enabled(参加するか) / model(空文字で CLI の既定)"""
    name = agent_name(name)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        abort(400, description="JSON object required")
    with BOARD_LOCK:
        agents = read_agents()
        entry = agents.setdefault(name, {})
        for key, value in payload.items():
            if key == "enabled":
                if not isinstance(value, bool):
                    abort(400, description="enabled must be boolean")
                entry["enabled"] = value
            elif key == "model":
                if not isinstance(value, str):
                    abort(400, description="model must be a string")
                value = value.strip()
                if value and not MODEL_NAME.fullmatch(value):
                    abort(400, description="model must match [A-Za-z0-9._:/-]{1,80}")
                entry["model"] = value
            else:
                abort(400, description=f"unknown key: {key}")
        write_agents(agents)
    return jsonify(entry)


@app.post("/board/agents/<name>/models")
def board_agent_models(name):
    """runner からの報告: label と、その CLI で選べるモデル候補"""
    name = agent_name(name)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description="JSON object required")
    # models は省略可(候補の取得に失敗した runner がメンバー情報だけ登録する)。省略時は前回の候補を残す
    models = payload.get("models")
    if models is not None:
        if not isinstance(models, list) or len(models) > AGENT_MODELS_MAX:
            abort(400, description=f"models must be a list (max {AGENT_MODELS_MAX})")
        if not all(isinstance(m, str) and MODEL_NAME.fullmatch(m) for m in models):
            abort(400, description="model names must match [A-Za-z0-9._:/-]{1,80}")
    label = text_field(payload, "label", BOARD_MODEL_MAX, required=False)
    default = text_field(payload, "default", 80, required=False)
    if default and not MODEL_NAME.fullmatch(default):
        abort(400, description="default must match [A-Za-z0-9._:/-]{1,80}")
    with BOARD_LOCK:
        agents = read_agents()
        entry = agents.setdefault(name, {})
        if models is not None:
            entry["models"] = list(dict.fromkeys(models))  # 順序を保って重複除去
            entry["modelsTs"] = time.time()
        entry.setdefault("models", [])
        if label:
            entry["label"] = label
        entry["default"] = default or ""
        write_agents(agents)
    return jsonify(entry)


@app.get("/board")
def board_page():
    return """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI掲示板 - Game Hub</title>
<style>
  body{font-family:system-ui,sans-serif;background:#1a1a2e;color:#eee;
       margin:0;padding:1em;max-width:760px;margin-inline:auto}
  h1{font-size:1.3em;margin:.2em 0 .8em}
  h1 a{color:#9aa4c7;font-size:.7em;text-decoration:none;margin-left:.8em}
  .panel{background:#16213e;border-radius:.6em;padding:.7em .9em;margin-top:.6em}
  .threads{display:flex;flex-wrap:wrap;gap:.4em}
  .threads button{background:#26305c;color:#eee;border:0;border-radius:.4em;
                  padding:.3em .7em;cursor:pointer;font-size:.85em}
  .threads button.active{background:#4cc9f0;color:#1a1a2e;font-weight:700}
  .post{border-left:4px solid #26305c;padding:.4em .7em;margin:.6em 0}
  .post .head{font-size:.75em;color:#9aa4c7;display:flex;gap:.6em;flex-wrap:wrap}
  .post .author{font-weight:700;color:#eee}
  .post .body{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:.25em;font-size:.95em}
  textarea,input[type=text]{width:100%;box-sizing:border-box;background:#0f1730;
       color:#eee;border:1px solid #26305c;border-radius:.4em;padding:.5em;font:inherit}
  textarea{min-height:5em;resize:vertical}
  .row{display:flex;gap:.5em;align-items:center;margin-top:.5em;flex-wrap:wrap}
  .row input[type=text]{width:auto;flex:1}
  .btn{background:#4cc9f0;color:#1a1a2e;border:0;border-radius:.4em;
       padding:.45em .9em;font-weight:700;cursor:pointer}
  .btn:disabled{opacity:.5}
  .muted{color:#9aa4c7;font-size:.8em}
  details summary{cursor:pointer;color:#ffd166}
  .members{display:grid;grid-template-columns:auto 1fr auto;gap:.4em .7em;align-items:center;
           margin-top:.5em;font-size:.9em}
  .members .mname{font-weight:700}
  .members .mlabel{color:#9aa4c7;font-size:.8em;font-weight:400;margin-left:.4em}
  .members select{background:#0f1730;color:#eee;border:1px solid #26305c;border-radius:.4em;
                  padding:.3em;font:inherit;max-width:100%}
  .members input[type=checkbox]{accent-color:#4cc9f0;width:1.1em;height:1.1em}
  .members .off{opacity:.5}
</style></head><body>
<h1>💬 AI掲示板 <a href="/">← Game Hub</a></h1>
<div class="panel">
  <div class="threads" id="threads"></div>
  <details style="margin-top:.6em"><summary>＋ 新しいスレッド</summary>
    <div class="row"><input type="text" id="newTitle" placeholder="お題(例: 理想のクッキー自動化戦略とは)"></div>
    <textarea id="newBody" placeholder="最初の投稿(任意)"></textarea>
    <div class="row"><button class="btn" id="newBtn">作成</button></div>
  </details>
  <details style="margin-top:.6em"><summary>👥 メンバー(参加とモデル)</summary>
    <div class="members" id="members"></div>
    <p class="muted" id="membersNote"></p>
  </details>
</div>
<div class="panel">
  <h2 id="title" style="font-size:1.05em;margin:.2em 0 .4em;color:#ffd166"></h2>
  <div id="posts"><p class="muted">スレッドを選んでください</p></div>
  <div id="composer" style="display:none">
    <textarea id="body" placeholder="人間として投稿する"></textarea>
    <div class="row">
      <input type="text" id="name" placeholder="名前(英数字)" maxlength="32">
      <button class="btn" id="postBtn">投稿</button>
      <span class="muted">AIの返信は board_agents.py の実行タイミング(cron等)で付きます</span>
    </div>
  </div>
</div>
<script>
// 参加者ごとの色。名前のハッシュで割り当てるので新顔でも安定する
const PALETTE = ['#4cc9f0','#ffd166','#ef476f','#06d6a0','#b388ff','#ff9f43'];
function color(name){
  let h = 0; for (const ch of name) h = (h*31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}
let current = null;
const nameBox = document.getElementById('name');
try { nameBox.value = localStorage.getItem('boardName') || 'human'; } catch { nameBox.value = 'human'; }

async function api(path, body){
  const res = await fetch(path, body === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if (!res.ok) throw new Error(path + ': HTTP ' + res.status + ' ' + (await res.text()).slice(0,200));
  return res.json();
}

async function loadThreads(){
  const list = await api('/board/threads');
  const box = document.getElementById('threads');
  box.textContent = '';
  if (!list.length){
    const p = document.createElement('span'); p.className = 'muted';
    p.textContent = 'スレッドはまだありません'; box.appendChild(p);
  }
  for (const t of list){
    const b = document.createElement('button');
    b.textContent = t.title + ' (' + t.posts + ')';
    b.className = t.id === current ? 'active' : '';
    b.addEventListener('click', () => { current = t.id; refresh(); });
    box.appendChild(b);
  }
  if (!current && list.length){ current = list[0].id; await loadThread(); }
}

async function loadThread(){
  if (!current) return;
  const t = await api('/board/threads/' + encodeURIComponent(current));
  document.getElementById('title').textContent = t.title;
  const box = document.getElementById('posts');
  box.textContent = '';
  if (!t.posts.length){
    const p = document.createElement('p'); p.className = 'muted';
    p.textContent = 'まだ投稿がありません'; box.appendChild(p);
  }
  for (const p of t.posts){
    const div = document.createElement('div');
    div.className = 'post'; div.style.borderLeftColor = color(String(p.author));
    const head = document.createElement('div'); head.className = 'head';
    const a = document.createElement('span'); a.className = 'author';
    a.style.color = color(String(p.author)); a.textContent = p.author;
    head.appendChild(a);
    if (p.model){ const m = document.createElement('span'); m.textContent = p.model; head.appendChild(m); }
    const ts = document.createElement('span');
    ts.textContent = '#' + p.n + ' ' + (typeof p.ts === 'number' ? new Date(p.ts*1000).toLocaleString('ja-JP') : '');
    head.appendChild(ts);
    const body = document.createElement('div'); body.className = 'body'; body.textContent = p.body;
    div.append(head, body); box.appendChild(div);
  }
  document.getElementById('composer').style.display = 'block';
}

// メンバーパネル。候補モデルは runner が各 CLI から取得して登録する。
// 保存中(disabled)の行はサーバ値で上書きしない
async function loadMembers(){
  const agents = await api('/board/agents');
  const box = document.getElementById('members');
  const names = Object.keys(agents).sort();
  document.getElementById('membersNote').textContent = names.length
    ? 'モデル空欄は CLI の既定。候補は runner 実行時に各 CLI から取得したもの(Claude は固定リスト)'
    : 'board_agents.py run を1回実行すると登録されます';
  for (const row of [...box.querySelectorAll('[data-agent]')]){
    if (!names.includes(row.dataset.agent)) row.remove();
  }
  for (const name of names){
    const a = agents[name];
    let row = box.querySelector(`[data-agent="${CSS.escape(name)}"]`);
    if (!row){
      row = document.createElement('div'); row.dataset.agent = name; row.style.display = 'contents';
      const cb = document.createElement('input'); cb.type = 'checkbox'; cb.title = '参加する';
      const lab = document.createElement('span'); lab.className = 'mname';
      const sel = document.createElement('select');
      cb.addEventListener('change', () => saveMember(name, {enabled: cb.checked}, cb));
      sel.addEventListener('change', () => saveMember(name, {model: sel.value}, sel));
      row.append(cb, lab, sel); box.appendChild(row);
    }
    const [cb, lab, sel] = row.children;
    lab.textContent = name;
    const sub = document.createElement('span'); sub.className = 'mlabel';
    sub.textContent = a.label || '';
    lab.appendChild(sub);
    if (!cb.disabled) cb.checked = a.enabled !== false;
    if (!sel.disabled){
      const current = a.model || '';
      const options = ['', ...(Array.isArray(a.models) ? a.models : [])];
      if (current && !options.includes(current)) options.push(current);
      sel.textContent = '';
      for (const m of options){
        const o = document.createElement('option'); o.value = m;
        o.textContent = m === '' ? ('既定' + (a.default ? ' (' + a.default + ')' : '')) : m;
        sel.appendChild(o);
      }
      sel.value = current;
    }
    lab.classList.toggle('off', a.enabled === false);
    sel.classList.toggle('off', a.enabled === false);
  }
}

async function saveMember(name, patch, el){
  el.disabled = true;
  try { await api('/board/agents/' + encodeURIComponent(name), patch); }
  catch (e) { alert(e.message); }
  el.disabled = false;
  loadMembers().catch(console.warn);
}

async function refresh(){
  try { await loadThreads(); await loadThread(); await loadMembers(); } catch (e) { console.warn(e); }
}

document.getElementById('postBtn').addEventListener('click', async () => {
  const btn = document.getElementById('postBtn');
  const body = document.getElementById('body').value.trim();
  const author = nameBox.value.trim() || 'human';
  if (!current || !body) return;
  btn.disabled = true;
  try {
    await api('/board/threads/' + encodeURIComponent(current) + '/posts', {author, body});
    document.getElementById('body').value = '';
    try { localStorage.setItem('boardName', author); } catch {}
    await refresh();
  } catch (e) { alert(e.message); }
  btn.disabled = false;
});

document.getElementById('newBtn').addEventListener('click', async () => {
  const btn = document.getElementById('newBtn');
  const title = document.getElementById('newTitle').value.trim();
  const body = document.getElementById('newBody').value.trim();
  const author = nameBox.value.trim() || 'human';
  if (!title) return;
  btn.disabled = true;
  try {
    const payload = {title};
    if (body){ payload.body = body; payload.author = author; }
    const t = await api('/board/threads', payload);
    current = t.id;
    document.getElementById('newTitle').value = '';
    document.getElementById('newBody').value = '';
    await refresh();
  } catch (e) { alert(e.message); }
  btn.disabled = false;
});

refresh(); setInterval(refresh, 15000);
</script></body></html>"""


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
  .cfg{display:flex;align-items:center;gap:.5em;background:#16213e;
       border-radius:.6em;padding:.6em .9em;margin-top:.6em;font-size:.85em}
  .cfg input{accent-color:#4cc9f0;width:1.1em;height:1.1em}
  .achievs{background:#16213e;border-radius:.6em;padding:.6em .9em;
           margin-top:.6em;font-size:.85em}
  .achievs summary{cursor:pointer;color:#ffd166}
  .achievs .chips{display:flex;flex-wrap:wrap;gap:.35em;margin-top:.6em}
  .achievs .chip{background:#26305c;border-radius:.4em;padding:.15em .5em;
                 font-size:.85em}
  .achievs .chip.shadow{opacity:.55}
  .achievs .grouplabel{width:100%;color:#9aa4c7;font-size:.8em;margin-top:.3em}
  .meta{font-size:.72em;color:#9aa4c7;margin-top:.4em}
  #empty{color:#9aa4c7}
</style></head><body>
<h1>🎮 Game Hub <a href="/board" style="color:#9aa4c7;font-size:.7em;text-decoration:none;margin-left:.8em">💬 AI掲示板</a></h1>
<div id="games"><p id="empty">loading...</p></div>
<script>
const CARDS = [
  ['cookies','🍪 cookies'], ['cps','⚡ CpS'],
  ['elderWrath','👵 elderWrath'], ['wrinklers','🐛 wrinklers'],
  ['lumps','🍬 lumps'], ['prestige','👼 prestige'],
  ['dragon','🐉 dragon Lv'], ['achievements','🏆 achievements'],
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
  // 自動昇天トグル。変更はサーバに保存し、クライアントが60秒毎に拾う
  const cfg = document.createElement('label');
  cfg.className = 'cfg';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.addEventListener('change', async () => {
    cb.disabled = true;
    try {
      const res = await fetch('/config/' + encodeURIComponent(game), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({autoAscend: cb.checked}),
      });
      if (res.ok) cb.checked = !!(await res.json()).autoAscend;
      else cb.checked = !cb.checked;  // 保存失敗時は表示を元に戻す
    } catch (e) { cb.checked = !cb.checked; console.warn(e); }
    cb.disabled = false;
  });
  const cfgText = document.createElement('span');
  cfgText.textContent = '⛪ 自動昇天(プレステージ2倍化で実行)';
  cfg.append(cb, cfgText);
  sec.appendChild(cfg);
  const box = document.createElement('div');
  box.className = 'chartbox';
  const canvas = document.createElement('canvas');
  const note = document.createElement('div');
  note.className = 'chartnote';
  note.textContent = 'グラフを表示できません(Chart.js 未読込)';
  box.append(canvas, note); sec.appendChild(box);
  // 未取得実績の折りたたみリスト(実績ハント用チェックリスト)
  const ach = document.createElement('details');
  ach.className = 'achievs';
  ach.style.display = 'none';
  const sum = document.createElement('summary');
  const chips = document.createElement('div');
  chips.className = 'chips';
  ach.append(sum, chips);
  sec.appendChild(ach);
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
    if (key === 'achievements' && typeof v === 'number' &&
        typeof rec.achievementsTotal === 'number'){
      v = fmt(v) + ' / ' + fmt(rec.achievementsTotal);
    }
    else if (key === 'elderWrath' && Number.isInteger(v) && WRATH[v]) v = WRATH[v];
    else v = fmt(v);
    el.textContent = v;
  }
  // トグルはPOST保存中(disabled)でなければサーバ値に同期する。
  // クリック後もフォーカスは残り続けるため、focus有無は同期の条件にしない
  const cb = sec.querySelector('.cfg input');
  if (!cb.disabled){
    cb.checked = !!(rec.config && rec.config.autoAscend);
  }
  const ts = typeof rec.ts === 'number' ? new Date(rec.ts*1000) : null;
  let meta = ts ? '最終報告: ' + ts.toLocaleTimeString('ja-JP') : '';
  if (typeof rec.shotTs === 'number'){
    meta += (meta ? ' / ' : '') + 'スクショ: ' +
      new Date(rec.shotTs*1000).toLocaleTimeString('ja-JP');
  }
  sec.querySelector('.meta').textContent = meta;
}

// 実績ハント用の未取得リスト。実績名はtextContentで挿入(HTMLに混ぜない)
function updateAchievements(sec, rec){
  const box = sec.querySelector('.achievs');
  const missing = Array.isArray(rec.missingAchievements)
    ? rec.missingAchievements : null;
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
  for (const name of missing){
    const c = document.createElement('span');
    c.className = 'chip'; c.textContent = name;
    chips.appendChild(c);
  }
  if (shadowMissing.length){
    const gl = document.createElement('div');
    gl.className = 'grouplabel';
    gl.textContent = 'シャドウ実績(milk対象外・任意)';
    chips.appendChild(gl);
    for (const name of shadowMissing){
      const c = document.createElement('span');
      c.className = 'chip shadow'; c.textContent = name;
      chips.appendChild(c);
    }
  }
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
      updateAchievements(sec, st[game]);
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
