"""Small authenticated browser UI for uploading and running OCR jobs."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from datetime import timedelta
from urllib.parse import quote

from flask import (Flask, abort, jsonify, request, send_file, session,
                   url_for)
from waitress import serve

from .config import SUPPORTED_EXT, Settings
from . import db
from .csv_writer import set_link_resolver
from .otp_auth import (MAX_ATTEMPTS, OTP_TTL_SECONDS,
                       RESEND_COOLDOWN_SECONDS, OtpChallenge, OtpMailError,
                       generate_otp, is_valid_email, send_otp_email)
from .smtp_config import ADMIN_EMAIL
from .pipeline import Pipeline
from .secrets import PROVIDERS, describe

app = Flask(__name__)
app.secret_key = os.environ.get("SHARE_OCR_WEB_SECRET") or secrets.token_bytes(32)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

UPLOADS_ROOT = Path.home() / "scans" / "uploads"

_job_lock = threading.Lock()
_job: dict = {"state": "idle", "pipeline": None, "error": ""}
_otp_lock = threading.Lock()
_challenges: dict[str, OtpChallenge] = {}
_last_request: dict[str, float] = {}
_ip_requests: dict[str, list[float]] = {}
# Base URL (e.g. http://13.235.223.180:8000) written into the CSV links.
_public_base = os.environ.get("SHARE_OCR_PUBLIC_URL", "").rstrip("/")


def _clean_name(filename: str) -> str:
    """The uploaded file's own name, unchanged except for anything that
    could escape the upload folder (path parts, control characters)."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if name in {"", ".", ".."}:
        return ""
    return name[:200]


def _sign(rel: str) -> str:
    key = app.secret_key if isinstance(app.secret_key, bytes) else app.secret_key.encode()
    return hmac.new(key, rel.encode("utf-8"), "sha256").hexdigest()[:32]


def _scan_link(path: str) -> str | None:
    """Signed http:// link to an uploaded scan, so the file name in the
    downloaded CSV opens from Excel on the user's PC (a server path cannot)."""
    if not _public_base:
        return None
    try:
        rel = Path(path).resolve().relative_to(UPLOADS_ROOT.resolve()).as_posix()
    except ValueError:
        return None
    return f"{_public_base}/scan/{quote(rel)}?k={_sign(rel)}"


set_link_resolver(_scan_link)


def _remember_base_url() -> None:
    global _public_base
    if not os.environ.get("SHARE_OCR_PUBLIC_URL"):
        _public_base = request.host_url.rstrip("/")


@app.before_request
def _protect():
    if request.endpoint in {"index", "request_otp", "verify_otp", "logout", "signed_scan"}:
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
    _remember_base_url()
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
        if pipeline and state in {"running", "stopping"} and not pipeline.running:
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


@app.get("/api/rows")
def rows():
    """Return extracted results or failed files for the desktop-style table."""
    s = Settings.load()
    q = db.Queue(s.db_path)
    try:
        if request.args.get("view") == "failed":
            found = q.failures(400)
            output = [{
                "id": f'f{row["id"]}', "file": row["name"],
                "file_url": url_for("failure_source", file_id=row["id"]),
                "flags": f'{row["error"] or "Extraction failed"}  (attempt {row["attempts"]}/{s.max_attempts})',
                "tag": "flagged",
            } for row in found]
            revision = f'failed:{found[0]["id"]}:{len(found)}' if found else "failed:0:0"
            return jsonify({"rows": output, "total": len(output),
                            "revision": revision, "view": "failed"})
        found = q.recent_rows(2000)
        output = []
        for row in reversed(found):
            try:
                rec = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                rec = {}
            distinctive = ""
            if rec.get("distinctive_from") or rec.get("distinctive_to"):
                distinctive = f'{rec.get("distinctive_from", "")} - {rec.get("distinctive_to", "")}'
            output.append({
                "idx": len(output) + 1,
                "id": str(row["row_id"]),
                "file": row["name"],
                "file_url": url_for("source_file", row_id=row["row_id"]),
                "company": rec.get("company_name", ""),
                "folio": rec.get("folio_no", ""),
                "regfolio": rec.get("registered_folio_no", ""),
                "cert": rec.get("certificate_no", ""),
                "holder": rec.get("share_holder_name", ""),
                "shares": rec.get("no_of_shares", ""),
                "facevalue": rec.get("face_value_per_share", ""),
                "sharetype": rec.get("share_type", ""),
                "distinctive": distinctive,
                "date": rec.get("date_of_issue", ""),
                "latest": rec.get("latest_share_holder_name", ""),
                "latestfolio": rec.get("latest_folio_no", ""),
                "foliohistory": rec.get("folio_no_history", ""),
                "holderhistory": rec.get("share_holder_history", ""),
                "remarks": rec.get("remarks", ""),
                "flags": rec.get("validation_flags", ""),
                "tag": ("addon" if str(rec.get("validation_flags", "")).startswith("Add-on not captured")
                        else "flagged" if rec.get("validation_flags") else ""),
            })
        revision = f'{found[0]["row_id"]}:{len(found)}' if found else "0:0"
        return jsonify({"rows": output, "total": len(output),
                        "revision": revision, "view": "all"})
    finally:
        q.close()


@app.get("/api/source/<int:row_id>")
def source_file(row_id: int):
    """Open the uploaded scan linked from a result row."""
    s = Settings.load()
    q = db.Queue(s.db_path)
    try:
        row = q.conn.execute(
            "SELECT f.path, f.name FROM results r JOIN files f ON f.id=r.file_id "
            "WHERE r.row_id=?", (row_id,)).fetchone()
    finally:
        q.close()
    if not row:
        abort(404)
    path = Path(row[0]).resolve()
    try:
        path.relative_to(UPLOADS_ROOT.resolve())
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=False, download_name=row[1])


@app.get("/api/failure-source/<int:file_id>")
def failure_source(file_id: int):
    s = Settings.load()
    q = db.Queue(s.db_path)
    try:
        row = q.conn.execute("SELECT path,name FROM files WHERE id=?", (file_id,)).fetchone()
    finally:
        q.close()
    if not row:
        abort(404)
    path = Path(row[0]).resolve()
    try:
        path.relative_to(UPLOADS_ROOT.resolve())
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=False, download_name=row[1])


@app.get("/scan/<path:rel>")
def signed_scan(rel: str):
    """Open an uploaded scan from a CSV/Excel link. No login needed: the
    link carries an HMAC signature, so only links this server wrote work."""
    if not hmac.compare_digest(request.args.get("k", ""), _sign(rel)):
        abort(404)
    path = (UPLOADS_ROOT / rel).resolve()
    try:
        path.relative_to(UPLOADS_ROOT.resolve())
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=False, download_name=path.name)


@app.get("/api/settings")
def settings_status():
    s = Settings.load()
    pools = {}
    for provider, info in PROVIDERS.items():
        details = describe(s, provider)
        pools[provider] = {"label": info.label, "count": details["count"],
                           "source": details["source_label"]}
    return jsonify({"keys": pools, "output_dir": str(s.csv_dir)})


@app.get("/api/output")
def output_files():
    s = Settings.load()
    files = sorted(p.name for p in s.csv_dir.glob("certificates*.csv") if p.is_file())
    return jsonify({"directory": str(s.csv_dir), "files": files})


@app.post("/api/actions/<action>")
def job_action(action: str):
    if not _csrf_ok():
        abort(403)
    with _job_lock:
        pipeline = _job.get("pipeline")
        running = bool(pipeline and pipeline.running)
        if action in {"pause", "resume", "stop"} and not running:
            return jsonify({"error": "No OCR job is currently running."}), 409
        if action == "pause":
            pipeline.pause(True)
            _job["state"] = "paused"
            return jsonify({"state": "paused"})
        if action == "resume":
            pipeline.pause(False)
            _job["state"] = "running"
            return jsonify({"state": "running"})
        if action == "stop":
            pipeline.stop()
            _job["state"] = "stopping"
            return jsonify({"state": "stopping"})
        if action in {"delete", "clear"}:
            if running:
                return jsonify({"error": "Pause or stop the OCR job before changing results."}), 409
            if pipeline is None:
                pipeline = Pipeline(Settings.load())
                _job["pipeline"] = pipeline
            if action == "clear":
                pipeline.clear()
                _job.update(state="idle", error="")
                return jsonify({"ok": True})
            data = request.get_json(silent=True) or {}
            row_ids = data.get("row_ids", [])
            if not isinstance(row_ids, list) or not row_ids or len(row_ids) > 2000:
                return jsonify({"error": "Select one or more result rows first."}), 400
            try:
                deleted = pipeline.delete_rows([int(value) for value in row_ids])
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid result selection."}), 400
            return jsonify({"deleted": deleted})
    return jsonify({"error": "Unknown action."}), 404


@app.post("/api/run")
def run_job():
    if not _csrf_ok():
        abort(403)
    with _job_lock:
        current = _job.get("pipeline")
        if _job["state"] == "running" and current and current.running:
            return jsonify({"error": "An OCR job is already running."}), 409

        uploads = request.files.getlist("files") + request.files.getlist("folder_files")
        allowed = {ext.lower() for ext in SUPPORTED_EXT}
        uploads = [f for f in uploads if f.filename and
                   Path(f.filename).suffix.lower() in allowed]
        if not uploads:
            return jsonify({"error": "Choose one or more PDF or image files."}), 400

        _remember_base_url()
        job_id = uuid.uuid4().hex
        folder = UPLOADS_ROOT / job_id
        folder.mkdir(parents=True, exist_ok=True)
        saved_paths = []
        for item in uploads:
            name = _clean_name(item.filename)
            if not name:
                continue
            # Keep the exact original name; a second file with the same name
            # in one upload goes into its own numbered sub-folder instead.
            target, n = folder / name, 1
            while target.exists():
                n += 1
                target = folder / str(n) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            item.save(target)
            saved_paths.append(str(target))
        if not saved_paths:
            return jsonify({"error": "No supported documents were uploaded."}), 400

        s = Settings.load()
        engine = request.form.get("engine", "openai")
        if engine not in ("openai", "tesseract"):
            return jsonify({"error": "Unsupported OCR engine."}), 400
        if engine == "openai" and not any(
                describe(s, provider)["count"] for provider in PROVIDERS):
            return jsonify({"error": "No OpenAI or NVIDIA API key is configured on this server."}), 400
        s.engine = engine
        try:
            s.workers = max(1, min(16, int(request.form.get("workers", "8"))))
        except ValueError:
            return jsonify({"error": "Workers must be a number from 1 to 16."}), 400
        s.ensure_dirs()
        pipeline = Pipeline(s)
        added = pipeline.ingest(saved_paths)
        if not added:
            return jsonify({"error": "No supported documents were uploaded."}), 400
        scope_ids = pipeline.scope_ids_for(saved_paths)
        _job.update(state="running", pipeline=pipeline, error="", id=job_id)
        pipeline.start(scope_ids=scope_ids)

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
*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#152238;font:16px system-ui,Segoe UI,sans-serif}.wrap{max-width:1500px;margin:32px auto;padding:0 20px}.brand{color:#2864dc;font-weight:700;letter-spacing:.08em;font-size:13px}.card{background:#fff;border:1px solid #e3e9f2;border-radius:14px;padding:22px;margin-top:16px;box-shadow:0 8px 30px #182a4710}h1{font-size:32px;margin:8px 0}p{color:#63718a}.drop{border:2px dashed #b8c7dc;border-radius:10px;padding:30px 18px;text-align:center;background:#f9fbff;cursor:pointer}.drop:hover{border-color:#2864dc}.controls{display:flex;gap:14px;align-items:end;flex-wrap:wrap;margin-top:16px}label{display:grid;gap:7px;color:#52627b;font-size:14px}select,input[type=number]{padding:10px;border:1px solid #ccd6e4;border-radius:9px;background:white;color:#152238}button{border:0;border-radius:9px;background:#2864dc;color:white;font-weight:650;padding:12px 20px;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.muted{color:#75839a;font-size:13px}.bar{height:10px;border-radius:9px;background:#e7edf6;overflow:hidden}.fill{height:100%;width:0;background:#2864dc;transition:width .3s}.row{display:flex;justify-content:space-between;gap:16px;align-items:center}.pill{border-radius:99px;padding:5px 11px;background:#edf2fa;font-size:13px;text-transform:capitalize}.files a{display:inline-block;margin:8px 10px 0 0;color:#2864dc}.error{color:#b42318}.ok{color:#147d50}.table-wrap{overflow:auto;max-height:68vh;border:1px solid #d8e0ec;border-radius:8px;margin-top:14px}table{border-collapse:separate;border-spacing:0;table-layout:fixed;min-width:2600px;width:100%;font-size:13px}th{position:sticky;top:0;background:#f1f5fb;z-index:3;text-align:left;white-space:nowrap;color:#43536b;font-weight:700}th,td{padding:10px 12px;border-bottom:1px solid #e3e9f1;border-right:1px solid #edf0f5;vertical-align:top}td{white-space:normal;overflow-wrap:anywhere;line-height:1.45}th:nth-child(1),td:nth-child(1){width:54px;position:sticky;left:0;z-index:2;background:#f8fafd}th:nth-child(1){z-index:5;background:#eaf0f8}th:nth-child(2),td:nth-child(2){width:190px;position:sticky;left:54px;z-index:2;background:#fff;box-shadow:2px 0 4px #15223812}th:nth-child(2){z-index:5;background:#f1f5fb}th:nth-child(3),td:nth-child(3){width:190px}th:nth-child(4),td:nth-child(4){width:120px}th:nth-child(5),td:nth-child(5){width:155px}th:nth-child(6),td:nth-child(6){width:130px}th:nth-child(7),td:nth-child(7){width:230px}th:nth-child(8),td:nth-child(8){width:105px}th:nth-child(9),td:nth-child(9){width:135px}th:nth-child(10),td:nth-child(10){width:120px}th:nth-child(11),td:nth-child(11){width:160px}th:nth-child(12),td:nth-child(12){width:125px}th:nth-child(13),td:nth-child(13){width:230px}th:nth-child(14),td:nth-child(14){width:135px}th:nth-child(15),td:nth-child(15){width:220px}th:nth-child(16),td:nth-child(16){width:240px}th:nth-child(17),td:nth-child(17){width:190px}th:nth-child(18),td:nth-child(18){width:240px}tbody tr:nth-child(even) td{background:#fafbfd}tbody tr:nth-child(even) td:nth-child(-n+2){background:#f4f7fb} .table-wrap tbody tr.addon td,.table-wrap tbody tr.addon td:nth-child(-n+2){background:#eaf1ff}.table-wrap tbody tr.flagged td,.table-wrap tbody tr.flagged td:nth-child(-n+2){background:#fff3d6}td a{color:#2864dc;text-decoration:underline;overflow-wrap:anywhere}.cell-text{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:6;overflow:hidden;overflow-wrap:anywhere;line-height:1.45}.table-title{margin:0}.export-links a{color:#2864dc;margin-left:14px}@media(max-width:600px){.wrap{margin:18px auto}.card{padding:16px}h1{font-size:27px}}
<style>
.toolbar{display:flex;gap:10px;align-items:center}.toolbar button,.actions button{margin:0}.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.actions .spacer{flex:1}.secondary{background:#e9eef6;color:#24344c}.danger{background:#d92d20}.drop input[type=file]{display:block;margin:14px auto 0}#folder-files{display:none}.modal{position:fixed;inset:0;background:#10182888;display:grid;place-items:center;z-index:20;padding:20px}.modal[hidden]{display:none}.modal-card{background:white;border-radius:14px;padding:24px;max-width:560px;width:100%;box-shadow:0 20px 70px #0004}.modal-card h2{margin-top:0}.modal-card p{line-height:1.55}.key-counts{display:grid;grid-template-columns:1fr 1fr;gap:12px}.key-count{background:#f4f7fb;border:1px solid #e1e7f0;border-radius:10px;padding:12px}.result-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.result-controls label{display:flex;align-items:center;gap:5px}.result-controls input{width:auto}.legend{display:inline-block;width:13px;height:13px;border:1px solid #cbd5e1;vertical-align:-2px;margin-right:5px}.legend.addon{background:#eaf1ff}.legend.flagged{background:#fff3d6}tbody tr.addon,tbody tr.addon td{background:#eaf1ff}tbody tr.flagged,tbody tr.flagged td{background:#fff3d6}.selected-row{outline:2px solid #2864dc;outline-offset:-2px}.previews{display:flex;gap:12px;overflow-x:auto;padding:4px 2px 8px;margin-top:16px}.previews[hidden]{display:none}.thumb-wrap{position:relative;flex:0 0 auto}.thumb-x{position:absolute;top:4px;right:4px;width:24px;height:24px;padding:0;margin:0;border-radius:50%;background:#152238cc;color:#fff;font-size:16px;line-height:24px;text-align:center}.thumb-x:hover{background:#d92d20}.thumb-clear{align-self:center;flex:0 0 auto}.thumb{width:112px;display:flex;flex-direction:column;border:1px solid #d8e0ec;border-radius:6px;overflow:hidden;background:#fff;text-decoration:none}.thumb:hover{outline:2px solid #2864dc}.thumb img,.thumb canvas,.thumb-ph{width:112px;height:150px;object-fit:cover;display:block;background:#f4f7fb}.thumb-ph{display:grid;place-items:center;color:#63718a;font-weight:700;font-size:14px}.thumb span{background:#2864dc;color:#fff;text-align:center;font-size:12px;font-weight:700;padding:4px}
</style></head><body><main class="wrap"><div class="row"><div><div class="brand">SHARE CERTIFICATE OCR</div><h1>Share Certificate OCR</h1><p>Upload scans, extract details and review results.</p></div><div class="toolbar"><button class="secondary" id="api-keys" type="button">API keys</button><button class="secondary" id="output-folder" type="button">Output folder</button><button id="logout" type="button">Sign out</button></div></div>
<div id="modal" class="modal" hidden><section class="modal-card"><div class="row"><h2 id="modal-title">Settings</h2><button class="secondary" id="modal-close" type="button">Close</button></div><div id="modal-content"></div></section></div>
<section class="card"><form id="form"><div class="drop" id="drop"><strong>Choose certificates</strong><p>PDF, PNG, JPG, TIFF, WEBP or BMP. You can select multiple files.</p><input id="files" name="files" type="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp,.bmp" multiple><input id="folder-files" name="folder_files" type="file" webkitdirectory directory multiple hidden></div>
<div class="controls"><label>OCR engine<select name="engine"><option value="openai">OpenAI vision (OpenAI + NVIDIA key pool)</option><option value="tesseract">Offline Tesseract</option></select></label><label>Workers<input name="workers" type="number" min="1" max="16" value="8"></label><button id="submit">Extract</button><button class="secondary" id="pick-folder" type="button">Select folder</button><button class="secondary" id="pause" type="button" disabled>Pause</button><button class="danger" id="stop" type="button" disabled>Stop</button><span class="muted" id="chosen">Nothing selected</span></div><div class="previews" id="previews" hidden></div></form><p class="muted">OpenAI mode uses server-configured API keys. Scans stay on this EC2 server.</p><div id="message"></div></section>
<section class="card"><div class="row"><h2>Job status</h2><span class="pill" id="state">Ready</span></div><div class="bar"><div class="fill" id="fill"></div></div><p id="counts">No job started yet.</p><div class="files export-links" id="results"></div></section>
<section class="card"><div class="row"><h2 class="table-title">Results <span class="pill" id="record-count">0 records</span></h2><div class="result-controls"><label><input type="radio" name="view" value="all" checked>All</label><label><input type="radio" name="view" value="failed">Failed</label></div></div><div class="row muted" style="justify-content:flex-start;margin:10px 0"><span><i class="legend addon"></i> add-on not captured</span><span><i class="legend flagged"></i> needs review</span><span id="selected-count" style="margin-left:auto">Nothing selected</span></div><div class="actions"><button class="secondary" id="select-all" type="button">Select all</button><button class="danger" id="delete-selected" type="button">Delete selected</button><button class="danger" id="clear-all" type="button">Clear all</button><span class="spacer"></span><span class="muted">Click a file name to open its uploaded scan.</span></div><div class="table-wrap"><table><thead><tr><th>#</th><th>File</th><th>Name of Share</th><th>Folio No</th><th>Registered Folio No</th><th>Certificate No</th><th>Name of Share Holder</th><th>No of Shares</th><th>Face Value / Share</th><th>Share Type</th><th>Distinctive No</th><th>Date of Issue</th><th>Latest Share Holder</th><th>Latest Folio No</th><th>Folio No History</th><th>Share Holder History</th><th>Remarks</th><th>Flags</th></tr></thead><tbody id="result-rows"></tbody></table></div></section>
</main><script>const csrf=document.querySelector('meta[name=csrf-token]').content,msg=document.querySelector('#message');
const form=document.querySelector('#form'),files=document.querySelector('#files'),folderFiles=document.querySelector('#folder-files'),selectedIds=new Set();
let rowsRevision=null,currentView='all';
async function post(url,data={}){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)})}
function showModal(title){document.querySelector('#modal-title').textContent=title;document.querySelector('#modal').hidden=false}
document.querySelector('#modal-close').addEventListener('click',()=>document.querySelector('#modal').hidden=true);
document.querySelector('#modal').addEventListener('click',e=>{if(e.target.id==='modal')e.currentTarget.hidden=true});
document.querySelector('#logout').addEventListener('click',async()=>{await post('/api/auth/logout');location.reload()});
document.querySelector('#api-keys').addEventListener('click',async()=>{showModal('API keys');const box=document.querySelector('#modal-content');box.textContent='Loading key status…';try{const r=await fetch('/api/settings'),d=await r.json();box.replaceChildren();const grid=document.createElement('div');grid.className='key-counts';for(const p of Object.values(d.keys)){const card=document.createElement('div');card.className='key-count';const b=document.createElement('strong');b.textContent=p.label;const line=document.createElement('p');line.textContent=`${p.count} key(s) configured · ${p.source}`;card.append(b,line);grid.append(card)}const note=document.createElement('p');note.textContent='Multiple keys are supported. For safety, enter or change API keys on the EC2 server over SSH (or AWS Systems Manager), not in this HTTP browser page. Put keys in OPENAI_API_KEYS separated by commas; NVIDIA_API_KEYS is also supported. No code edit is needed.';box.append(grid,note)}catch(_){box.textContent='Could not read API key status.'}});
document.querySelector('#output-folder').addEventListener('click',async()=>{showModal('Output folder');const box=document.querySelector('#modal-content');box.textContent='Loading output files…';try{const r=await fetch('/api/output'),d=await r.json();box.replaceChildren();if(!d.files.length){const empty=document.createElement('p');empty.textContent='No output files yet.';box.append(empty)}for(const f of d.files){const a=document.createElement('a');a.href='/api/results/'+encodeURIComponent(f);a.textContent='Download '+f;a.style.display='block';a.style.margin='10px 0';box.append(a)}}catch(_){box.textContent='Could not list output files.'}});
function updateChosen(){const n=files.files.length+folderFiles.files.length;document.querySelector('#chosen').textContent=n?`${n} file(s) selected`:'Nothing selected';renderPreviews()}
const PDFJS='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/';let pdfjsReady=null,previewUrls=[],previewToken=0;
function loadPdfJs(){if(!pdfjsReady)pdfjsReady=new Promise((res,rej)=>{const sc=document.createElement('script');sc.src=PDFJS+'pdf.min.js';sc.onload=()=>{pdfjsLib.GlobalWorkerOptions.workerSrc=PDFJS+'pdf.worker.min.js';res(pdfjsLib)};sc.onerror=()=>{pdfjsReady=null;rej(Error('pdf.js'))};document.head.append(sc)});return pdfjsReady}
async function pdfThumb(url,canvas){const lib=await loadPdfJs(),pdf=await lib.getDocument(url).promise;try{const page=await pdf.getPage(1),base=page.getViewport({scale:1}),vp=page.getViewport({scale:300/base.height});canvas.width=vp.width;canvas.height=vp.height;await page.render({canvasContext:canvas.getContext('2d'),viewport:vp}).promise}finally{pdf.destroy()}}
function removeFile(input,index){const dt=new DataTransfer();[...input.files].forEach((f,i)=>{if(i!==index)dt.items.add(f)});input.files=dt.files;updateChosen()}
function placeholder(text){const d=document.createElement('div');d.className='thumb-ph';d.textContent=text;return d}
async function renderPreviews(){const token=++previewToken,strip=document.querySelector('#previews');for(const u of previewUrls)URL.revokeObjectURL(u);previewUrls=[];strip.replaceChildren();const list=[files,folderFiles].flatMap(input=>[...input.files].map((f,i)=>({f,input,i}))).filter(e=>/\.(pdf|png|jpe?g|tiff?|webp|bmp)$/i.test(e.f.name)),MAX=200;strip.hidden=!list.length;for(let i=0;i<Math.min(list.length,MAX);i++){const f=list[i].f,url=URL.createObjectURL(f);previewUrls.push(url);const a=document.createElement('a');a.className='thumb';a.href=url;a.target='_blank';a.rel='noopener';a.title=f.name;let media;if(/\.pdf$/i.test(f.name))media=document.createElement('canvas');else if(/\.tiff?$/i.test(f.name))media=placeholder('TIFF');else{media=document.createElement('img');media.src=url;media.alt=f.name;media.loading='lazy'}const cap=document.createElement('span');cap.textContent='#'+(i+1);a.append(media,cap);const x=document.createElement('button');x.type='button';x.className='thumb-x';x.textContent='×';x.title='Remove '+f.name;x.addEventListener('click',()=>removeFile(list[i].input,list[i].i));const wrap=document.createElement('div');wrap.className='thumb-wrap';wrap.append(a,x);strip.append(wrap)}if(list.length>MAX){const more=document.createElement('p');more.className='muted';more.textContent=`+${list.length-MAX} more file(s)`;strip.append(more)}if(list.length){const clr=document.createElement('button');clr.type='button';clr.className='secondary thumb-clear';clr.textContent='Clear selection';clr.addEventListener('click',()=>{files.value='';folderFiles.value='';updateChosen()});strip.append(clr)}for(const a of strip.querySelectorAll('a.thumb')){const c=a.querySelector('canvas');if(!c)continue;if(token!==previewToken)return;try{await pdfThumb(a.href,c)}catch(_){if(token===previewToken)c.replaceWith(placeholder('PDF'))}}}
files.addEventListener('change',()=>{if(files.files.length)folderFiles.value='';updateChosen()});folderFiles.addEventListener('change',()=>{if(folderFiles.files.length)files.value='';updateChosen()});
document.querySelector('#pick-folder').addEventListener('click',()=>folderFiles.click());
document.querySelector('#drop').addEventListener('dragover',e=>e.preventDefault());document.querySelector('#drop').addEventListener('drop',e=>{e.preventDefault();folderFiles.value='';files.files=e.dataTransfer.files;updateChosen()});
form.addEventListener('submit',async e=>{e.preventDefault();if(!files.files.length&&!folderFiles.files.length){msg.textContent='Select files or a folder first.';msg.className='error';return}const b=document.querySelector('#submit');b.disabled=true;msg.textContent='Uploading files…';msg.className='muted';try{const r=await fetch('/api/run',{method:'POST',headers:{'X-CSRF-Token':csrf},body:new FormData(form)}),d=await r.json();if(!r.ok)throw Error(d.error||'Upload failed');msg.textContent=`${d.queued} document(s) queued.`;msg.className='ok';document.querySelector('#pause').disabled=false;document.querySelector('#stop').disabled=false;refresh()}catch(err){msg.textContent=err.message;msg.className='error'}finally{b.disabled=false}});
async function control(action){try{const r=await post('/api/actions/'+action),d=await r.json();if(!r.ok)throw Error(d.error);refresh()}catch(err){msg.textContent=err.message;msg.className='error'}}
document.querySelector('#pause').addEventListener('click',()=>control(document.querySelector('#pause').textContent==='Pause'?'pause':'resume'));
document.querySelector('#stop').addEventListener('click',()=>control('stop'));
for(const radio of document.querySelectorAll('input[name=view]'))radio.addEventListener('change',()=>{currentView=radio.value;rowsRevision=null;selectedIds.clear();refreshRows(true);updateSelected()});
document.querySelector('#select-all').addEventListener('click',()=>{for(const tr of document.querySelectorAll('#result-rows tr')){selectedIds.add(tr.dataset.id);tr.classList.add('selected-row')}updateSelected()});
function updateSelected(){const n=selectedIds.size;document.querySelector('#selected-count').textContent=n?`${n} selected`:'Nothing selected'}
document.querySelector('#result-rows').addEventListener('click',e=>{if(e.target.closest('a'))return;const tr=e.target.closest('tr');if(!tr)return;const id=tr.dataset.id;if(selectedIds.has(id)){selectedIds.delete(id);tr.classList.remove('selected-row')}else{selectedIds.add(id);tr.classList.add('selected-row')}updateSelected()});
document.querySelector('#delete-selected').addEventListener('click',async()=>{if(currentView==='failed'){msg.textContent='Failed files have no extracted rows to delete. They will be listed in the failed CSV.';msg.className='error';return}const ids=[...selectedIds];if(!ids.length){msg.textContent='Select one or more rows first.';msg.className='error';return}if(!confirm(`Delete ${ids.length} selected row(s)? The source files remain on the server and the rows will be removed from the results and CSV.`))return;try{const r=await post('/api/actions/delete',{row_ids:ids}),d=await r.json();if(!r.ok)throw Error(d.error);msg.textContent=`Deleted ${d.deleted} row(s).`;msg.className='ok';selectedIds.clear();rowsRevision=null;refreshRows(true);refresh()}catch(err){msg.textContent=err.message;msg.className='error'}});
document.querySelector('#clear-all').addEventListener('click',async()=>{if(!confirm('Clear the queue, all extracted rows and CSV outputs? Uploaded original scans will remain on the server.'))return;try{const r=await post('/api/actions/clear'),d=await r.json();if(!r.ok)throw Error(d.error);selectedIds.clear();rowsRevision=null;msg.textContent='Cleared.';msg.className='ok';refreshRows(true);refresh()}catch(err){msg.textContent=err.message;msg.className='error'}});
async function refresh(){try{const r=await fetch('/api/status');if(!r.ok)return;const d=await r.json();document.querySelector('#state').textContent=d.state;document.querySelector('#fill').style.width=d.progress+'%';const c=d.counts;document.querySelector('#counts').textContent=`${c.done||0} done · ${c.pending||0} waiting · ${c.dead||0} failed · ${d.rate||0} files/sec`;const links=document.querySelector('#results');links.replaceChildren();for(const f of d.files){const a=document.createElement('a');a.href='/api/results/'+encodeURIComponent(f);a.textContent='Download '+f;links.append(a)}const active=['running','paused','stopping'].includes(d.state);document.querySelector('#pause').disabled=!['running','paused'].includes(d.state);document.querySelector('#pause').textContent=d.state==='paused'?'Resume':'Pause';document.querySelector('#stop').disabled=!['running','paused'].includes(d.state);if(d.error){msg.textContent=d.error;msg.className='error'}}catch(_){}}
async function refreshRows(force=false){try{const r=await fetch('/api/rows?view='+currentView);if(!r.ok)return;const d=await r.json(),revision=currentView+':'+d.revision;if(!force&&revision===rowsRevision)return;rowsRevision=revision;selectedIds.clear();updateSelected();const body=document.querySelector('#result-rows');body.replaceChildren();document.querySelector('#record-count').textContent=currentView==='failed'?`${d.total} failed files`:`${d.total} records`;for(let i=0;i<d.rows.length;i++){const x=d.rows[i],tr=document.createElement('tr');tr.dataset.id=x.id||'';if(x.tag)tr.classList.add(x.tag);const vals={idx:x.idx||i+1,file:x.file,company:x.company,folio:x.folio,regfolio:x.regfolio,cert:x.cert,holder:x.holder,shares:x.shares,facevalue:x.facevalue,sharetype:x.sharetype,distinctive:x.distinctive,date:x.date,latest:x.latest,latestfolio:x.latestfolio,foliohistory:x.foliohistory,holderhistory:x.holderhistory,remarks:x.remarks,flags:x.flags};for(const key of ['idx','file','company','folio','regfolio','cert','holder','shares','facevalue','sharetype','distinctive','date','latest','latestfolio','foliohistory','holderhistory','remarks','flags']){const td=document.createElement('td'),content=document.createElement('div');content.className='cell-text';if(key==='file'){const a=document.createElement('a');a.href=x.file_url;a.target='_blank';a.rel='noopener';a.textContent=vals[key]||'';content.append(a)}else content.textContent=vals[key]??'';content.title=content.textContent;td.append(content);tr.append(td)}body.append(tr)}}catch(_){}}
setInterval(refresh,2500);setInterval(()=>refreshRows(),2500);refresh();refreshRows();</script></body></html>'''


def main() -> None:
    host = os.environ.get("SHARE_OCR_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("SHARE_OCR_WEB_PORT", "8000"))
    print(f"Share OCR browser UI listening on http://{host}:{port}")
    serve(app, host=host, port=port, threads=8, max_request_body_size=500 * 1024 * 1024)


if __name__ == "__main__":
    main()
