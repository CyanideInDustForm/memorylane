#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сервер сайта-карты с метками (Flask + SQLite).

Локально:       python server.py          -> http://localhost:7100
Хостинг (Render): gunicorn server:app
"""
import io
import os
import re
import sqlite3
import uuid

from flask import Flask, jsonify, request, send_from_directory, abort

ROOT = os.path.dirname(os.path.abspath(__file__))
# На хостинге (Render) данные кладём на Persistent Disk через DATA_DIR,
# локально DATA_DIR не задана — всё лежит рядом с server.py.
DATA_DIR = os.environ.get("DATA_DIR", ROOT)
DB_PATH = os.path.join(DATA_DIR, "data.db")
UP_DIR = os.path.join(DATA_DIR, "uploads")
TH_DIR = os.path.join(DATA_DIR, "thumbs")
PORT = int(os.environ.get("PORT", "7100"))

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512 МБ на один запрос
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400       # статика (тайлы) — кэш 1 день

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    PIL_OK = True
except Exception:
    PIL_OK = False

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


# ------------------------------ база данных ------------------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    os.makedirs(UP_DIR, exist_ok=True)
    os.makedirs(TH_DIR, exist_ok=True)
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS markers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        x REAL NOT NULL,
        y REAL NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        marker_id INTEGER NOT NULL REFERENCES markers(id) ON DELETE CASCADE,
        name TEXT,
        stored TEXT NOT NULL,
        thumb TEXT,
        description TEXT DEFAULT ''
    );
    """)
    con.commit()
    con.close()


def markers_json():
    con = db()
    rows = con.execute("SELECT * FROM markers ORDER BY id").fetchall()
    out = []
    for r in rows:
        files = con.execute(
            "SELECT id,name,stored,thumb,description FROM files WHERE marker_id=? ORDER BY id",
            (r["id"],)).fetchall()
        out.append({
            "id": r["id"], "x": r["x"], "y": r["y"],
            "created_at": r["created_at"],
            "files": [dict(f) for f in files],
        })
    con.close()
    return out


# ------------------------------ файлы ------------------------------
def make_thumb(data, ext):
    if not PIL_OK or ext.lower() not in IMG_EXT:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.thumbnail((360, 360))
        name = uuid.uuid4().hex
        if im.mode in ("RGBA", "LA", "P"):
            name += ".png"
            im.save(os.path.join(TH_DIR, name))
        else:
            name += ".jpg"
            im.convert("RGB").save(os.path.join(TH_DIR, name), quality=80)
        return name
    except Exception:
        return None


def save_upload(storage):
    data = storage.read()
    ext = os.path.splitext(storage.filename or "")[1] or ".bin"
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)[:10]
    stored = uuid.uuid4().hex + ext
    with open(os.path.join(UP_DIR, stored), "wb") as f:
        f.write(data)
    return stored, make_thumb(data, ext), os.path.basename(storage.filename or stored)


def remove_file_row(row):
    for folder, name in ((UP_DIR, row["stored"]), (TH_DIR, row["thumb"])):
        if name:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


# ------------------------------ маршруты ------------------------------
@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html", max_age=0)  # не кэшировать


@app.route("/api/markers")
def api_markers():
    return jsonify(markers_json())


@app.route("/api/markers", methods=["POST"])
def create_marker():
    try:
        x, y = float(request.form["x"]), float(request.form["y"])
    except (KeyError, ValueError):
        abort(400)
    con = db()
    cur = con.execute("INSERT INTO markers(x,y) VALUES(?,?)", (x, y))
    mid = cur.lastrowid
    descs = request.form.getlist("desc")
    for i, f in enumerate(request.files.getlist("file")):
        desc = descs[i] if i < len(descs) else ""
        stored, thumb, name = save_upload(f)
        con.execute(
            "INSERT INTO files(marker_id,name,stored,thumb,description) VALUES(?,?,?,?,?)",
            (mid, name, stored, thumb, desc))
    con.commit()
    con.close()
    return jsonify({"ok": True, "id": mid})


@app.route("/api/markers/<int:mid>/edit", methods=["POST"])
def edit_marker(mid):
    con = db()
    if not con.execute("SELECT 1 FROM markers WHERE id=?", (mid,)).fetchone():
        con.close()
        abort(404)
    # описания существующих файлов: fdesc_<id>
    for key, desc in request.form.items():
        if key.startswith("fdesc_") and key[6:].isdigit():
            con.execute("UPDATE files SET description=? WHERE id=? AND marker_id=?",
                        (desc, int(key[6:]), mid))
    # удалённые файлы
    for fid in request.form.get("del", "").split(","):
        if fid.strip().isdigit():
            row = con.execute("SELECT stored,thumb FROM files WHERE id=? AND marker_id=?",
                              (int(fid), mid)).fetchone()
            if row:
                remove_file_row(row)
                con.execute("DELETE FROM files WHERE id=?", (int(fid),))
    # новые файлы (file + desc параллельными списками)
    descs = request.form.getlist("desc")
    for i, f in enumerate(request.files.getlist("file")):
        desc = descs[i] if i < len(descs) else ""
        stored, thumb, name = save_upload(f)
        con.execute(
            "INSERT INTO files(marker_id,name,stored,thumb,description) VALUES(?,?,?,?,?)",
            (mid, name, stored, thumb, desc))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.route("/api/markers/<int:mid>/delete", methods=["POST"])
def delete_marker(mid):
    con = db()
    rows = con.execute("SELECT stored,thumb FROM files WHERE marker_id=?", (mid,)).fetchall()
    con.execute("DELETE FROM markers WHERE id=?", (mid,))
    con.commit()
    con.close()
    for r in rows:
        remove_file_row(r)
    return jsonify({"ok": True})


@app.route("/<kind>/<name>")
def uploaded(kind, name):
    if kind not in ("uploads", "thumbs") or not SAFE_NAME.fullmatch(name):
        abort(404)
    folder = UP_DIR if kind == "uploads" else TH_DIR
    return send_from_directory(folder, name)


@app.errorhandler(413)
def too_large(e):
    return jsonify({"ok": False, "error": "Файл слишком большой (лимит 512 МБ на запрос)"}), 413


@app.errorhandler(500)
def server_err(e):
    return jsonify({"ok": False, "error": "Внутренняя ошибка сервера: " + str(e)}), 500


# Инициализация при старте — в том числе под gunicorn (импорт модуля),
# иначе папки uploads/thumbs и таблицы не создадутся на чистом сервере.
init_db()


if __name__ == "__main__":
    print("Сервер запущен: http://localhost:%d" % PORT)
    print("Pillow (миниатюры):", "есть" if PIL_OK else "нет")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
