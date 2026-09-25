"""Small authenticated browser UI for uploading and running OCR jobs."""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from flask import (Flask, abort, jsonify, make_response, request, send_file,
                   session)
from waitress import serve
from werkzeug.utils import secure_filename

from .config import SUPPORTED_EXT, Settings
from .pipeline import Pipeline

app = Flask(__name__)
app.secret_key = os.environ.get("SHARE_OCR_WEB_SECRET") or secrets.token_bytes(32)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

_job_lock = threading.Lock()
_job: dict = {"state": "idle", "pipeline": None, "error": ""}


def _password() -> str:
    value = os.environ.get("SHARE_OCR_WEB_PASSWORD", "")
    if len(value) < 12:
        raise RuntimeError("Set SHARE_OCR_WEB_PASSWORD to a password of at least 12 characters.")
    return value


@app.before_request
def _protect():
    user = os.environ.get("SHARE_OCR_WEB_USER", "admin")
    auth = request.authorization
    if not auth or auth.username != user or not hmac.compare_digest(
            auth.password or "", _password()):
        return make_response("Login required", 401,
                             {"WWW-Authenticate": 'Basic realm="Share OCR"'})


def _csrf_ok() -> bool:
    return bool(session.get("csrf") and hmac.compare_digest(
        request.headers.get("X-CSRF-Token", ""), session["csrf"]))


@app.get("/")
def index():
    token = secrets.token_urlsafe(24)
    session["csrf"] = token
    return PAGE.replace("__CSRF__", token)


@app.get("/api/status")
def status():
    s = Settings.load()
    counts = Pipeline(s).counts()
    with _job_lock:
        pipeline = _job.get("pipeline")
        state = _job["state"]
        error = _job.get("error", "")
        if pipeline and state == "running" and not pipeline.running:
            state = "complete"
            _job["state"] = state
        stats = pipeline.stats if pipeline else None
    files = sorted(p.name for p in s.csv_dir.glob("certificates*.csv"))
    total = max(1, counts.get("total", 0))
    finished = counts.get("done", 0) + counts.get("dead", 0)
    return jsonify({"state": state, "error": error, "counts": counts,
                    "progress": min(100, round(finished * 100 / total)),
                    "rate": round(stats.rate, 2) if stats else 0,
                    "files": files})


@app.post("/api/run")
def run_job():
    if not _csrf_ok():
        abort(403)
    with _job_lock:
        current = _job.get("pipeline")
        if _job["state"] == "running" and current and current.running:
            return jsonify({"error": "An OCR job is already running."}), 409

        uploads = request.files.getlist("files")
        allowed = {ext.lower() for ext in SUPPORTED_EXT}
        uploads = [f for f in uploads if f.filename and
                   Path(f.filename).suffix.lower() in allowed]
        if not uploads:
            return jsonify({"error": "Choose one or more PDF or image files."}), 400

        job_id = uuid.uuid4().hex
        folder = Path.home() / "scans" / "uploads" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        for item in uploads:
            safe = secure_filename(Path(item.filename).name)
            if not safe:
                continue
            item.save(folder / f"{uuid.uuid4().hex[:8]}_{safe}")

        s = Settings.load()
        engine = request.form.get("engine", "openai")
        if engine not in ("openai", "tesseract"):
            return jsonify({"error": "Unsupported OCR engine."}), 400
        s.engine = engine
        try:
            s.workers = max(1, min(16, int(request.form.get("workers", "8"))))
        except ValueError:
            return jsonify({"error": "Workers must be a number from 1 to 16."}), 400
        s.ensure_dirs()
        pipeline = Pipeline(s)
        added = pipeline.ingest([str(folder)])
        if not added:
            return jsonify({"error": "No supported documents were uploaded."}), 400
        _job.update(state="running", pipeline=pipeline, error="", id=job_id)
        pipeline.start()

        def monitor():
            try:
                while pipeline.running:
                    time.sleep(1)
                pipeline.join()
                with _job_lock:
                    if _job.get("id") == job_id:
                        _job["state"] = "complete"
            except Exception as exc:  # keep the service alive and show a brief error
                with _job_lock:
                    if _job.get("id") == job_id:
                        _job.update(state="error", error=str(exc)[:300])

        threading.Thread(target=monitor, daemon=True, name="ocr-web-monitor").start()
    return jsonify({"queued": added, "job": job_id}), 202


@app.get("/api/results/<name>")
def result_file(name: str):
    if Path(name).name != name or not name.endswith(".csv"):
        abort(404)
    s = Settings.load()
    path = s.csv_dir / name
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="__CSRF__"><title>Share Certificate OCR</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#152238;font:16px system-ui,Segoe UI,sans-serif}.wrap{max-width:900px;margin:48px auto;padding:0 20px}.brand{color:#2864dc;font-weight:700;letter-spacing:.08em;font-size:13px}.card{background:#fff;border:1px solid #e3e9f2;border-radius:18px;padding:26px;margin-top:20px;box-shadow:0 8px 30px #182a4710}h1{font-size:32px;margin:8px 0}p{color:#63718a}.drop{border:2px dashed #b8c7dc;border-radius:14px;padding:36px 18px;text-align:center;background:#f9fbff;cursor:pointer}.drop:hover{border-color:#2864dc}.controls{display:flex;gap:16px;align-items:end;flex-wrap:wrap;margin-top:18px}label{display:grid;gap:7px;color:#52627b;font-size:14px}select,input[type=number]{padding:10px;border:1px solid #ccd6e4;border-radius:9px;background:white;color:#152238}button{border:0;border-radius:10px;background:#2864dc;color:white;font-weight:650;padding:12px 20px;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.muted{color:#75839a;font-size:13px}.bar{height:10px;border-radius:9px;background:#e7edf6;overflow:hidden}.fill{height:100%;width:0;background:#2864dc;transition:width .3s}.row{display:flex;justify-content:space-between;gap:16px;align-items:center}.pill{border-radius:99px;padding:5px 11px;background:#edf2fa;font-size:13px;text-transform:capitalize}.files a{display:inline-block;margin:8px 10px 0 0;color:#2864dc}.error{color:#b42318}.ok{color:#147d50}@media(max-width:600px){.wrap{margin:20px auto}.card{padding:19px}h1{font-size:27px}}
</style></head><body><main class="wrap"><div class="brand">SHARE CERTIFICATE OCR</div><h1>Upload and extract</h1><p>Upload certificate scans and review job progress and CSV results here.</p>
<section class="card"><form id="form"><div class="drop" id="drop"><strong>Choose certificates</strong><p>PDF, PNG, JPG, TIFF, WEBP or BMP. You can select multiple files.</p><input id="files" name="files" type="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp,.bmp" multiple required></div>
<div class="controls"><label>OCR engine<select name="engine"><option value="openai">OpenAI vision</option><option value="tesseract">Offline Tesseract</option></select></label><label>Workers<input name="workers" type="number" min="1" max="16" value="8"></label><button id="submit">Upload and start OCR</button><span class="muted" id="chosen">No files selected</span></div></form><p class="muted">OpenAI mode needs the server API key configured. Scans stay on this EC2 server.</p><div id="message"></div></section>
<section class="card"><div class="row"><h2>Job status</h2><span class="pill" id="state">Ready</span></div><div class="bar"><div class="fill" id="fill"></div></div><p id="counts">No job started yet.</p><div class="files" id="results"></div></section>
</main><script>
const form=document.querySelector('#form'), files=document.querySelector('#files'), msg=document.querySelector('#message');
files.addEventListener('change',()=>document.querySelector('#chosen').textContent=files.files.length?`${files.files.length} file(s) selected`:'No files selected');
document.querySelector('#drop').addEventListener('dragover',e=>{e.preventDefault()});document.querySelector('#drop').addEventListener('drop',e=>{e.preventDefault();files.files=e.dataTransfer.files;files.dispatchEvent(new Event('change'))});
form.addEventListener('submit',async e=>{e.preventDefault();if(!files.files.length)return;const b=document.querySelector('#submit');b.disabled=true;msg.textContent='Uploading files…';msg.className='muted';try{const r=await fetch('/api/run',{method:'POST',headers:{'X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content},body:new FormData(form)});const d=await r.json();if(!r.ok)throw new Error(d.error||'Upload failed');msg.textContent=`${d.queued} document(s) queued.`;msg.className='ok';refresh()}catch(err){msg.textContent=err.message;msg.className='error'}finally{b.disabled=false}});
async function refresh(){try{const r=await fetch('/api/status');if(!r.ok)return;const d=await r.json();document.querySelector('#state').textContent=d.state;document.querySelector('#fill').style.width=d.progress+'%';const c=d.counts;document.querySelector('#counts').textContent=`${c.done||0} done · ${c.pending||0} waiting · ${c.dead||0} failed · ${d.rate||0} files/sec`;document.querySelector('#results').innerHTML=d.files.map(f=>`<a href="/api/results/${encodeURIComponent(f)}">Download ${f}</a>`).join('');if(d.error){msg.textContent=d.error;msg.className='error'}}catch(_){}}setInterval(refresh,2500);refresh();
</script></body></html>'''


def main() -> None:
    _password()  # fail fast with a clear setup message
    host = os.environ.get("SHARE_OCR_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("SHARE_OCR_WEB_PORT", "8000"))
    print(f"Share OCR browser UI listening on http://{host}:{port}")
    serve(app, host=host, port=port, threads=8, max_request_body_size=500 * 1024 * 1024)


if __name__ == "__main__":
    main()
