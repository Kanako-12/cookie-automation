from flask import Flask, request, jsonify
import json, time, pathlib

app = Flask(__name__)
DATA = pathlib.Path.home() / "gamehub" / "data"

@app.post("/report/<game>")
def report(game):
    p = request.get_json()
    p["ts"] = time.time()
    d = DATA / game
    d.mkdir(parents=True, exist_ok=True)
    save = p.pop("save", None)
    (d / "latest.json").write_text(json.dumps(p))
    if save:
        (d / f"save_{time.strftime('%Y%m%d')}.txt").write_text(save)
    return jsonify(ok=True)

@app.get("/status")
def status():
    out = {}
    for g in DATA.iterdir():
        f = g / "latest.json"
        if f.exists():
            out[g.name] = json.loads(f.read_text())
    return jsonify(out)

@app.get("/")
def index():
    return """<meta name="viewport" content="width=device-width">
<body style="font-family:sans-serif;background:#1a1a2e;color:#eee;padding:1em">
<h2>Game Hub</h2><pre id="o">loading...</pre>
<script>
async function r(){const d=await(await fetch('/status')).json();
document.getElementById('o').textContent=JSON.stringify(d,null,2);}
r();setInterval(r,30000);
</script></body>"""

app.run(host="0.0.0.0", port=8090)
