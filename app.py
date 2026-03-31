"""
ClipBot v3 — Backend Flask
Soporta:
  1. Google Drive — lista y descarga videos del usuario
  2. Subida directa de archivos
Procesa con ffmpeg: vertical 9:16, texto, fade, concat.
"""

import os, uuid, threading, subprocess, shutil, tempfile, requests
from flask import Flask, request, jsonify, send_file, redirect
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app, origins="*")

UPLOAD_DIR = "uploads"
OUTPUT_DIR = "outputs"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

jobs = {}

# Google OAuth config
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI         = os.environ.get("REDIRECT_URI", "")  # ej: https://tuapp.railway.app/oauth/callback

ALLOWED = {"mp4","mov","avi","mkv","webm","m4v"}

def allowed(f): return "." in f and f.rsplit(".",1)[1].lower() in ALLOWED
def upd(jid, status, pct, msg):
    jobs[jid] = {**jobs.get(jid,{}), "status":status,"progress":pct,"message":msg}
    print(f"[{jid}] {pct}% {msg}")

# ── RUTAS PRINCIPALES ─────────────────────────────────────────

@app.route("/")
def index():
    return send_file("index.html")

@app.route("/ping")
def ping():
    return jsonify({"ok": True, "app": "ClipBot v3"})

# ── GOOGLE OAUTH ──────────────────────────────────────────────

@app.route("/auth/google")
def auth_google():
    """Redirige al usuario a Google para autorizar acceso a Drive."""
    scope = "https://www.googleapis.com/auth/drive.readonly"
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={scope}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return redirect(url)

@app.route("/oauth/callback")
def oauth_callback():
    """Recibe el código de Google y lo intercambia por un token."""
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "No se recibió código de autorización"}), 400

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })
    tokens = resp.json()
    access_token = tokens.get("access_token","")

    if not access_token:
        return jsonify({"error": "No se pudo obtener token", "details": tokens}), 400

    # Devolver HTML que cierra la ventana y pasa el token al padre
    return f"""
    <html><body>
    <script>
      // Si fue abierto como popup
      if (window.opener) {{
        window.opener.postMessage({{type:'drive_token', token:'{access_token}'}}, '*');
        window.close();
      }} else {{
        // Si fue redirect normal, guardar token y regresar
        localStorage.setItem('drive_token', '{access_token}');
        window.location.href = '/done';
      }}
    </script>
    <p>Conectado. Cerrando...</p>
    </body></html>
    """

@app.route("/done")
def done():
    return """
    <html><body>
    <script>
      const token = localStorage.getItem('drive_token');
      if (token && window.opener) {{
        window.opener.postMessage({{type:'drive_token', token:token}}, '*');
        window.close();
      }} else {{
        window.location.href = 'javascript:history.back()';
      }}
    </script>
    <p>Conectado exitosamente. Puedes cerrar esta ventana.</p>
    </body></html>
    """

@app.route("/drive/videos")
def drive_videos():
    """Lista videos de Google Drive del usuario."""
    token = request.headers.get("Authorization","").replace("Bearer ","")
    if not token:
        return jsonify({"error": "Sin token"}), 401

    query = "mimeType contains 'video/' and trashed=false"
    resp = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "q": query,
            "fields": "files(id,name,size,thumbnailLink,videoMediaMetadata)",
            "pageSize": 50,
            "orderBy": "modifiedTime desc",
        }
    )
    data = resp.json()
    files = data.get("files", [])

    videos = []
    for f in files:
        meta = f.get("videoMediaMetadata", {})
        videos.append({
            "id": f["id"],
            "name": f["name"],
            "size": int(f.get("size", 0)),
            "thumb": f.get("thumbnailLink", ""),
            "duration": int(meta.get("durationMillis", 0)) // 1000,
            "width": meta.get("width", 0),
            "height": meta.get("height", 0),
        })
    return jsonify({"videos": videos})

@app.route("/upload", methods=["POST"])
def upload():
    title     = request.form.get("title", "Mi Short")
    drive_ids = request.form.get("drive_ids", "")      # IDs separados por coma
    drive_tok = request.form.get("drive_token", "")
    files     = request.files.getlist("videos")

    if not files and not drive_ids:
        return jsonify({"error": "Sube videos o selecciona de Drive"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status":"pending","progress":0,"message":"Iniciando..."}

    saved = []
    for f in files:
        if f and allowed(f.filename):
            path = os.path.join(UPLOAD_DIR, f"{job_id}_{secure_filename(f.filename)}")
            f.save(path)
            saved.append(path)

    ids = [x.strip() for x in drive_ids.split(",") if x.strip()]

    t = threading.Thread(target=process_job,
                         args=(job_id, title, saved, ids, drive_tok))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({"error":"No encontrado"}), 404
    return jsonify(job)

@app.route("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error":"No listo"}), 404
    path = job.get("file")
    if not path or not os.path.exists(path):
        return jsonify({"error":"Archivo no encontrado"}), 404
    return send_file(path, mimetype="video/mp4", as_attachment=True,
                     download_name=f"short_{job_id}.mp4")

# ── PIPELINE ──────────────────────────────────────────────────

def download_from_drive(file_id, token, out_path):
    """Descarga un archivo de Google Drive."""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, stream=True, timeout=300)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

def process_job(job_id, title, uploaded_paths, drive_ids, drive_token):
    tmp = tempfile.mkdtemp(prefix=f"cb_{job_id}_")
    all_raw = list(uploaded_paths)

    try:
        # 1. Descargar desde Google Drive
        for i, fid in enumerate(drive_ids):
            upd(job_id,"processing", 5+i*8, f"Descargando de Drive {i+1}/{len(drive_ids)}...")
            out = os.path.join(tmp, f"drive_{i:02d}.mp4")
            try:
                download_from_drive(fid, drive_token, out)
                if os.path.exists(out):
                    all_raw.append(out)
            except Exception as e:
                print(f"Drive download error: {e}")

        if not all_raw:
            upd(job_id,"error",0,"No se pudo obtener ningún video.")
            return

        # 2. Procesar clips
        upd(job_id,"processing",30,f"Procesando {len(all_raw)} clips...")
        processed = []
        for idx, raw in enumerate(all_raw):
            base = os.path.join(tmp, f"p_{idx:02d}")
            pct  = 30 + int(idx/len(all_raw)*50)
            upd(job_id,"processing",pct,f"Procesando clip {idx+1}/{len(all_raw)}...")

            # a) Vertical 9:16
            vert = base+"_v.mp4"
            subprocess.run([
                "ffmpeg","-y","-i",raw,
                "-vf","scale='if(gt(iw/ih,9/16),trunc(ih*9/16/2)*2,iw)':'if(gt(iw/ih,9/16),ih,trunc(iw*16/9/2)*2)',pad=iw:ih:(ow-iw)/2:(oh-ih)/2,scale=1080:1920",
                "-c:v","libx264","-preset","fast","-crf","23",
                "-c:a","aac","-b:a","128k","-t","58",
                vert,"-loglevel","error"
            ], check=True)

            # b) Texto
            txt = base+"_t.mp4"
            safe = title.replace("'","").replace(":","")
            subprocess.run([
                "ffmpeg","-y","-i",vert,
                "-vf",(
                    f"drawtext=text='{safe}':"
                    f"fontsize=60:fontcolor=white:shadowcolor=black@0.8:shadowx=3:shadowy=3:"
                    f"x=(w-text_w)/2:y=100,"
                    f"drawtext=text='{idx+1} de {len(all_raw)}':"
                    f"fontsize=34:fontcolor=white@0.85:shadowcolor=black@0.6:shadowx=2:shadowy=2:"
                    f"x=50:y=h-80"
                ),
                "-c:v","libx264","-preset","fast","-crf","23",
                "-c:a","copy",txt,"-loglevel","error"
            ], check=True)

            # c) Fade
            fade = base+"_f.mp4"
            try:
                probe = subprocess.check_output([
                    "ffprobe","-v","error","-show_entries","format=duration",
                    "-of","default=noprint_wrappers=1:nokey=1",txt
                ], text=True).strip()
                dur = float(probe)
            except: dur = 30.0
            fo = max(0, dur-0.7)
            subprocess.run([
                "ffmpeg","-y","-i",txt,
                "-vf",f"fade=t=in:st=0:d=0.5,fade=t=out:st={fo:.2f}:d=0.7",
                "-af",f"afade=t=in:st=0:d=0.5,afade=t=out:st={fo:.2f}:d=0.7",
                "-c:v","libx264","-preset","fast","-crf","23",
                "-c:a","aac","-b:a","128k",
                fade,"-loglevel","error"
            ], check=True)
            processed.append(fade)

        # 3. Unir
        upd(job_id,"processing",85,"Uniendo clips en el short final...")
        list_f = os.path.join(tmp,"list.txt")
        with open(list_f,"w") as f:
            for p in processed: f.write(f"file '{p}'\n")

        out_file = os.path.join(OUTPUT_DIR, f"short_{job_id}.mp4")
        subprocess.run([
            "ffmpeg","-y","-f","concat","-safe","0","-i",list_f,
            "-c:v","libx264","-preset","fast","-crf","22",
            "-c:a","aac","-b:a","128k",
            out_file,"-loglevel","error"
        ], check=True)

        size_mb = round(os.path.getsize(out_file)/(1024*1024),1)
        jobs[job_id]["file"] = out_file
        upd(job_id,"done",100,f"Short listo — {len(processed)} clips · {size_mb} MB")

        for p in uploaded_paths:
            try: os.remove(p)
            except: pass

    except Exception as e:
        upd(job_id,"error",0,f"Error: {str(e)}")
        print(f"[{job_id}] FATAL: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port, debug=False)
