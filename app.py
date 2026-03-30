"""
ClipBot v2 — Backend Flask
Soporta:
  1. Subida de videos propios (multipart/form-data)
  2. Link de YouTube (yt-dlp)
Procesa con ffmpeg: vertical 9:16, texto, fade, concat.
"""

import os, uuid, threading, subprocess, shutil, tempfile
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

jobs = {}  # job_id -> {status, progress, message, file}

ALLOWED = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}

def allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED

def upd(job_id, status, pct, msg):
    jobs[job_id] = {**jobs.get(job_id, {}), "status": status, "progress": pct, "message": msg}
    print(f"[{job_id}] {pct}% {msg}")

# ── RUTAS ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({"ok": True, "app": "ClipBot v2"})

@app.route("/upload", methods=["POST"])
def upload():
    """Recibe videos subidos por el usuario y lanza el procesamiento."""
    title = request.form.get("title", "Mi Short")
    files = request.files.getlist("videos")
    yt_links = [l.strip() for l in request.form.get("yt_links", "").split("\n") if l.strip()]

    if not files and not yt_links:
        return jsonify({"error": "Sube al menos un video o pega un link"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "pending", "progress": 0, "message": "Recibiendo archivos..."}

    # Guardar archivos subidos
    saved_paths = []
    for f in files:
        if f and allowed(f.filename):
            fname = secure_filename(f.filename)
            path = os.path.join(UPLOAD_DIR, f"{job_id}_{fname}")
            f.save(path)
            saved_paths.append(path)

    t = threading.Thread(target=process_job,
                         args=(job_id, title, saved_paths, yt_links))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job no encontrado"}), 404
    return jsonify(job)

@app.route("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "No listo"}), 404
    path = job.get("file")
    if not path or not os.path.exists(path):
        return jsonify({"error": "Archivo no encontrado"}), 404
    return send_file(path, mimetype="video/mp4", as_attachment=True,
                     download_name=f"short_{job_id}.mp4")

# ── PIPELINE ──────────────────────────────────────────────────

def process_job(job_id, title, uploaded_paths, yt_links):
    tmp = tempfile.mkdtemp(prefix=f"cb_{job_id}_")
    all_raw = list(uploaded_paths)  # videos subidos

    try:
        total_sources = len(all_raw) + len(yt_links)

        # 1. Descargar links de YouTube
        for i, link in enumerate(yt_links):
            upd(job_id, "processing", 5 + i * 8, f"Descargando link {i+1}/{len(yt_links)}...")
            out = os.path.join(tmp, f"yt_{i:02d}.mp4")
            cmd = [
                "yt-dlp", link, "-o", out,
                "-f", "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "--no-playlist", "--quiet", "--no-warnings",
            ]
            try:
                subprocess.run(cmd, check=True, timeout=180)
                if os.path.exists(out):
                    all_raw.append(out)
            except Exception as e:
                print(f"YT download error: {e}")

        if not all_raw:
            upd(job_id, "error", 0, "No se pudo obtener ningún video.")
            return

        # 2. Procesar cada clip
        upd(job_id, "processing", 30, f"Procesando {len(all_raw)} clips...")
        processed = []
        for idx, raw in enumerate(all_raw):
            base = os.path.join(tmp, f"p_{idx:02d}")
            pct = 30 + int(idx / len(all_raw) * 50)
            upd(job_id, "processing", pct, f"Procesando clip {idx+1}/{len(all_raw)}...")

            # a) Vertical 9:16
            vert = base + "_v.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", raw,
                "-vf", "scale='if(gt(iw/ih,9/16),trunc(ih*9/16/2)*2,iw)':'if(gt(iw/ih,9/16),ih,trunc(iw*16/9/2)*2)',pad=iw:ih:(ow-iw)/2:(oh-ih)/2,scale=1080:1920",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-t", "58",
                vert, "-loglevel", "error"
            ], check=True)

            # b) Texto
            txt = base + "_t.mp4"
            safe_title = title.replace("'", "").replace(":", "")
            subprocess.run([
                "ffmpeg", "-y", "-i", vert,
                "-vf", (
                    f"drawtext=text='{safe_title}':"
                    f"fontsize=60:fontcolor=white:shadowcolor=black@0.8:shadowx=3:shadowy=3:"
                    f"x=(w-text_w)/2:y=100,"
                    f"drawtext=text='{idx+1} de {len(all_raw)}':"
                    f"fontsize=34:fontcolor=white@0.85:shadowcolor=black@0.6:shadowx=2:shadowy=2:"
                    f"x=50:y=h-80"
                ),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy", txt, "-loglevel", "error"
            ], check=True)

            # c) Fade in/out
            fade = base + "_f.mp4"
            try:
                probe = subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", txt
                ], text=True).strip()
                dur = float(probe)
            except Exception:
                dur = 30.0
            fo = max(0, dur - 0.7)
            subprocess.run([
                "ffmpeg", "-y", "-i", txt,
                "-vf", f"fade=t=in:st=0:d=0.5,fade=t=out:st={fo:.2f}:d=0.7",
                "-af", f"afade=t=in:st=0:d=0.5,afade=t=out:st={fo:.2f}:d=0.7",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                fade, "-loglevel", "error"
            ], check=True)

            processed.append(fade)

        # 3. Unir todos
        upd(job_id, "processing", 85, "Uniendo clips en el short final...")
        list_f = os.path.join(tmp, "list.txt")
        with open(list_f, "w") as f:
            for p in processed:
                f.write(f"file '{p}'\n")

        out_file = os.path.join(OUTPUT_DIR, f"short_{job_id}.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_f,
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k",
            out_file, "-loglevel", "error"
        ], check=True)

        size_mb = round(os.path.getsize(out_file) / (1024 * 1024), 1)
        jobs[job_id]["file"] = out_file
        upd(job_id, "done", 100, f"Short listo — {len(processed)} clips · {size_mb} MB")

        # Limpiar uploads
        for p in uploaded_paths:
            try: os.remove(p)
            except: pass

    except Exception as e:
        upd(job_id, "error", 0, f"Error: {str(e)}")
        print(f"[{job_id}] FATAL: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
