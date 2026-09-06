from flask import Flask, abort, jsonify, render_template, request
import base64, collections, json, math, os, pathlib, re, secrets, subprocess, sys, tempfile, threading, time

app = Flask(__name__)
HERE = pathlib.Path(__file__).resolve().parent
STATIC_VERSION = str(int(time.time()))  # 起動ごとに変えて CSS/JS のキャッシュを更新させる
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
# 「今すぐ返事」: ダッシュボードから runner(board_agents.py)を起動する。
# 同時に走るのは1つだけ(runner 側の flock とも整合)。結果は直近分をメモリに残す
RUNNER = HERE / "board_agents.py"
RUNNER_CONFIG = os.environ.get("BOARD_AGENTS_CONFIG")  # テスト等で設定ファイルを差し替える
RUNNER_EXTRA_PATH = os.environ.get(
    "BOARD_AGENTS_PATH",
    ":".join(str(pathlib.Path.home() / d) for d in (".npm-global/bin", ".local/bin")))
JOBS = collections.deque(maxlen=20)
JOBS_LOCK = threading.Lock()
JOB_TAIL_LINES = 15


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
        "active": record.get("active") is True,      # AI の返信対象
        "archived": record.get("archived") is True,  # 一覧から隠す
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


def list_threads():
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
    return out


@app.get("/board/threads")
def board_threads():
    """?all=1 でアーカイブ済みも含める。?active=1 で AI の返信対象だけ"""
    threads = list_threads()
    if request.args.get("all") != "1":
        threads = [t for t in threads if not t["archived"]]
    if request.args.get("active") == "1":
        threads = [t for t in threads if t["active"]]
    return jsonify(threads)


@app.post("/board/threads")
def board_create_thread():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description="JSON object required")
    title = text_field(payload, "title", BOARD_TITLE_MAX)
    # 時刻+乱数のIDにして並行作成でも衝突しないようにする
    tid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    record = {"id": tid, "title": title, "created": time.time(), "posts": [], "active": True}
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


@app.post("/board/threads/<tid>")
def board_thread_set(tid):
    """active(AI の返信対象) / archived(一覧から隠す) の切り替え"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        abort(400, description="JSON object required")
    for key, value in payload.items():
        if key not in ("active", "archived"):
            abort(400, description=f"unknown key: {key}")
        if not isinstance(value, bool):
            abort(400, description=f"{key} must be boolean")
    with BOARD_LOCK:
        record = read_thread(tid)
        record.update(payload)
        if record.get("archived"):
            record["active"] = False  # アーカイブしたスレに AI が返信し続けないようにする
        write_atomic(thread_path(tid), json.dumps(record, ensure_ascii=False))
    return jsonify(thread_summary(record))


@app.delete("/board/threads/<tid>")
def board_thread_delete(tid):
    path = thread_path(tid)
    with BOARD_LOCK:
        if not path.is_file():
            abort(404)
        path.unlink()
    return jsonify(ok=True)


def job_env():
    env = dict(os.environ)
    # systemd 配下の PATH には CLI(~/.npm-global/bin 等)が無いので足す
    env["PATH"] = ":".join(p for p in (RUNNER_EXTRA_PATH, env.get("PATH", ""), "/usr/local/bin:/usr/bin:/bin") if p)
    return env


def watch_job(job, proc):
    out, _ = proc.communicate()
    lines = out.decode("utf-8", "replace").strip().splitlines()
    with JOBS_LOCK:
        job["finished"] = time.time()
        job["exit"] = proc.returncode
        job["status"] = "ok" if proc.returncode == 0 else "failed"
        job["tail"] = lines[-JOB_TAIL_LINES:]


@app.get("/board/jobs")
def board_jobs():
    with JOBS_LOCK:
        return jsonify(list(JOBS))


@app.post("/board/jobs")
def board_job_start():
    """「今すぐ返事」: 指定スレッドに対して runner を1回起動する(agent 省略時は順番の次の AI)"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description="JSON object required")
    tid = payload.get("thread")
    if not isinstance(tid, str) or not THREAD_ID.fullmatch(tid) or not thread_path(tid).is_file():
        abort(404, description="thread not found")
    agent = payload.get("agent")
    if agent is not None and (not isinstance(agent, str) or not AUTHOR.fullmatch(agent)):
        abort(400, description="agent must match [A-Za-z0-9_.-]{1,32}")
    if not RUNNER.is_file():
        abort(500, description="board_agents.py not found next to hub.py")
    argv = [sys.executable, str(RUNNER), "run", "--thread", tid, "--no-sync"]
    if RUNNER_CONFIG:
        argv += ["--config", RUNNER_CONFIG]
    argv += ["--agent", agent] if agent else ["--one"]  # 指定が無ければ順番の次の1人だけ
    with JOBS_LOCK:
        if any(j["status"] == "running" for j in JOBS):
            abort(409, description="another reply is already in progress")
        try:
            proc = subprocess.Popen(argv, cwd=HERE.parent, env=job_env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            abort(500, description=f"failed to start runner: {e}")
        job = {"id": secrets.token_hex(4), "thread": tid, "agent": agent, "started": time.time(),
               "finished": None, "status": "running", "exit": None, "tail": [],
               "command": " ".join(argv[1:])}  # 失敗時の切り分け用
        JOBS.appendleft(job)
    threading.Thread(target=watch_job, args=(job, proc), daemon=True).start()
    return jsonify(job), 202


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


@app.delete("/board/agents/<name>")
def board_agent_delete(name):
    """設定から外れた AI を runner が同期時に消す(存在しなければ何もしない)"""
    name = agent_name(name)
    with BOARD_LOCK:
        agents = read_agents()
        removed = agents.pop(name, None) is not None
        if removed:
            write_agents(agents)
    return jsonify(removed=removed)


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
    return render_template("board.html", v=STATIC_VERSION)


@app.get("/")
def index():
    return render_template("index.html", v=STATIC_VERSION)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
