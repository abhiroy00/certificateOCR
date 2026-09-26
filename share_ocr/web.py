"""Small authenticated browser UI for uploading and running OCR jobs."""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from datetime import timedelta

from flask import (Flask, abort, jsonify, make_response, request, send_file,
                   session)
from waitress import serve
from werkzeug.utils import secure_filename

from .config import SUPPORTED_EXT, Settings
from .otp_auth import (MAX_ATTEMPTS, OTP_TTL_SECONDS,
                       RESEND_COOLDOWN_SECONDS, OtpChallenge, OtpMailError,
                       generate_otp, is_valid_email, send_otp_email)
from .smtp_config import ADMIN_EMAIL
from .pipeline import Pipeline

app = Flask(__name__)
app.secret_key = os.environ.get("SHARE_OCR_WEB_SECRET") or secrets.token_bytes(32)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

_job_lock = threading.Lock()
_job: dict = {"state": "idle", "pipeline": None, "error": ""}
_otp_lock = threading.Lock()
_challenges: dict[str, OtpChallenge] = {}
_last_request: dict[str, float] = {}
_ip_requests: dict[str, list[float]] = {}


@app.before_request
def _protect():
    if request.endpoint in {"index", "request_otp", "verify_otp", "logout"}:
        return None
    if not session.get("authenticated"):
        return jsonify({"error": "Please sign in with your email OTP."}), 401


def _csrf_ok() -> bool:
    return bool(session.get("csrf") and hmac.compare_digest(
        request.headers.get("X-CSRF-Token", ""), session["csrf"]))


@app.get("/")
def index():
    token = secrets.token_urlsafe(24)
    session["csrf"] = token
    page = PAGE if session.get("authenticated") else LOGIN_PAGE
    return page.replace("__CSRF__", token)


@app.post("/api/auth/request-otp")
def request_otp():
    if not _csrf_ok():
        abort(403)
    email = (request.json or {}).get("email", "").strip()
    if not is_valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400

    now = time.time()
    sid = session.setdefault("otp_sid", secrets.token_urlsafe(24))
    client_ip = request.remote_addr or "unknown"
    with _otp_lock:
        last = _last_request.get(sid, 0)
        if now - last < RESEND_COOLDOWN_SECONDS:
            return jsonify({"error": f"Wait {int(RESEND_COOLDOWN_SECONDS - (now-last))} seconds before requesting another code."}), 429
        recent = [t for t in _ip_requests.get(client_ip, []) if now - t < 3600]
        if len(recent) >= 20:
            return jsonify({"error": "Too many access requests from this network. Try again later."}), 429
        recent.append(now)
        _ip_requests[client_ip] = recent

    code = generate_otp()
    challenge = OtpChallenge(email=email, code=code)
    try:
        send_otp_email(email, code)
    except OtpMailError as exc:
        return jsonify({"error": str(exc)}), 503
    with _otp_lock:
        _challenges[sid] = challenge
        _last_request[sid] = now
    return jsonify({"message": f"Access code sent to the administrator ({ADMIN_EMAIL}). Ask them for it. It expires in {OTP_TTL_SECONDS // 60} minutes."})


@app.post("/api/auth/verify-otp")
def verify_otp():
    if not _csrf_ok():
        abort(403)
    sid = session.get("otp_sid", "")
    with _otp_lock:
        challenge = _challenges.get(sid)
        if not challenge:
            return jsonify({"error": "Request an access code first."}), 400
        ok, message = challenge.check((request.json or {}).get("code", ""))
        if ok:
            _challenges.pop(sid, None)
            _last_request.pop(sid, None)
    if not ok:
        return jsonify({"error": message}), 400
    session.clear()
    session["authenticated"] = True
    session["permanent"] = True
    session["csrf"] = secrets.token_urlsafe(24)
    return jsonify({"ok": True})


@app.post("/api/auth/logout")
def logout():
    if not _csrf_ok():
        abort(403)
    session.clear()
    return jsonify({"ok": True})


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


LOGIN_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="__CSRF__"><title>Share Certificate OCR - Sign in</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#152238;font:16px system-ui,Segoe UI,sans-serif}.wrap{max-width:520px;margin:9vh auto;padding:0 20px}.card{background:#fff;border:1px solid #e3e9f2;border-radius:14px;padding:34px;box-shadow:0 8px 30px #182a4710}h1{font-size:25px;margin:0 0 8px}p{color:#63718a;line-height:1.5}label{display:block;font-weight:600;margin:20px 0 7px}input{width:100%;padding:12px;border:1px solid #ccd6e4;border-radius:8px;font:inherit}button{margin-top:16px;border:0;border-radius:8px;background:#2864dc;color:white;font-weight:650;padding:12px 18px;cursor:pointer}button:disabled{opacity:.55}.muted{font-size:14px;color:#63718a}.error{color:#b42318}.ok{color:#147d50}#verify{display:none;border-top:1px solid #dce4ef;margin-top:24px;padding-top:8px}
</style></head><body><main class="wrap"><section class="card"><h1>Share Certificate OCR</h1><p>Enter your email to request access.</p>
<form id="request"><label for="email">Email</label><input id="email" type="email" autocomplete="email" required><button id="send">Send access code</button></form>
<div id="verify"><p>Code sent to the administrator. Ask them for it.</p><label for="code">Access code</label><input id="code" inputmode="numeric" maxlength="6" autocomplete="one-time-code"><button id="check">Verify and open OCR</button></div><p id="message" class="muted"></p>
</section></main><script>
const csrf=document.querySelector('meta[name=csrf-token]').content,msg=document.querySelector('#message');
document.querySelector('#request').addEventListener('submit',async e=>{e.preventDefault();const b=document.querySelector('#send');b.disabled=true;msg.textContent='Sending request…';try{const r=await fetch('/api/auth/request-otp',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({email:document.querySelector('#email').value})});const d=await r.json();if(!r.ok)throw Error(d.error);document.querySelector('#verify').style.display='block';msg.textContent=d.message;msg.className='ok'}catch(err){msg.textContent=err.message;msg.className='error'}finally{b.disabled=false}});
document.querySelector('#check').addEventListener('click',async()=>{const b=document.querySelector('#check');b.disabled=true;try{const r=await fetch('/api/auth/verify-otp',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({code:document.querySelector('#code').value})});const d=await r.json();if(!r.ok)throw Error(d.error);location.reload()}catch(err){msg.textContent=err.message;msg.className='error'}finally{b.disabled=false}});
</script></body></html>'''


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="__CSRF__"><title>Share Certificate OCR</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#152238;font:16px system-ui,Segoe UI,sans-serif}.wrap{max-width:900px;margin:48px auto;padding:0 20px}.brand{color:#2864dc;font-weight:700;letter-spacing:.08em;font-size:13px}.card{background:#fff;border:1px solid #e3e9f2;border-radius:18px;padding:26px;margin-top:20px;box-shadow:0 8px 30px #182a4710}h1{font-size:32px;margin:8px 0}p{color:#63718a}.drop{border:2px dashed #b8c7dc;border-radius:14px;padding:36px 18px;text-align:center;background:#f9fbff;cursor:pointer}.drop:hover{border-color:#2864dc}.controls{display:flex;gap:16px;align-items:end;flex-wrap:wrap;margin-top:18px}label{display:grid;gap:7px;color:#52627b;font-size:14px}select,input[type=number]{padding:10px;border:1px solid #ccd6e4;border-radius:9px;background:white;color:#152238}button{border:0;border-radius:10px;background:#2864dc;color:white;font-weight:650;padding:12px 20px;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.muted{color:#75839a;font-size:13px}.bar{height:10px;border-radius:9px;background:#e7edf6;overflow:hidden}.fill{height:100%;width:0;background:#2864dc;transition:width .3s}.row{display:flex;justify-content:space-between;gap:16px;align-items:center}.pill{border-radius:99px;padding:5px 11px;background:#edf2fa;font-size:13px;text-transform:capitalize}.files a{display:inline-block;margin:8px 10px 0 0;color:#2864dc}.error{color:#b42318}.ok{color:#147d50}@media(max-width:600px){.wrap{margin:20px auto}.card{padding:19px}h1{font-size:27px}}
</style></head><body><main class="wrap"><div class="row"><div><div class="brand">SHARE CERTIFICATE OCR</div><h1>Upload and extract</h1><p>Upload certificate scans and review job progress and CSV results here.</p></div><button id="logout" type="button">Sign out</button></div>
<section class="card"><form id="form"><div class="drop" id="drop"><strong>Choose certificates</strong><p>PDF, PNG, JPG, TIFF, WEBP or BMP. You can select multiple files.</p><input id="files" name="files" type="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp,.bmp" multiple required></div>
<div class="controls"><label>OCR engine<select name="engine"><option value="openai">OpenAI vision</option><option value="tesseract">Offline Tesseract</option></select></label><label>Workers<input name="workers" type="number" min="1" max="16" value="8"></label><button id="submit">Upload and start OCR</button><span class="muted" id="chosen">No files selected</span></div></form><p class="muted">OpenAI mode needs the server API key configured. Scans stay on this EC2 server.</p><div id="message"></div></section>
<section class="card"><div class="row"><h2>Job status</h2><span class="pill" id="state">Ready</span></div><div class="bar"><div class="fill" id="fill"></div></div><p id="counts">No job started yet.</p><div class="files" id="results"></div></section>
</main><script>
document.querySelector('#logout').addEventListener('click',async()=>{await fetch('/api/auth/logout',{method:'POST',headers:{'X-CSRF-Token':document.querySelector('meta[name=csrf-token]').content}});location.reload()});
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
