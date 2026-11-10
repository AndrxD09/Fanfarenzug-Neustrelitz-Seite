import json
import mimetypes
import os
import sqlite3
import uuid
from email.parser import BytesParser
from email.policy import default
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AUDIO_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = Path(os.environ.get("AUDIO_UPLOAD_DIR", ROOT / "uploads")).resolve()
DATABASE = DATA_DIR / "audio.sqlite3"
MAX_REQUEST_SIZE = 100 * 1024 * 1024
MAX_FILES_PER_REQUEST = 20
AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".oga", ".ogg",
    ".opus", ".wav", ".weba", ".webm", ".wma",
}


def initialize_database():
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DATABASE) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


class AudioRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def translate_path(self, path):
        requested_path = urlsplit(path).path
        if requested_path.startswith("/uploads/"):
            stored_name = requested_path.removeprefix("/uploads/")
            if stored_name and Path(stored_name).name == stored_name:
                return str(UPLOAD_DIR / stored_name)
            return str(ROOT / ".not-found")

        resolved = Path(super().translate_path(path)).resolve()
        for protected in (DATA_DIR, ROOT / ".git"):
            try:
                resolved.relative_to(protected.resolve())
                return str(ROOT / ".not-found")
            except ValueError:
                pass
        return str(resolved)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/audio":
            with sqlite3.connect(DATABASE) as connection:
                rows = connection.execute(
                    "SELECT title, stored_name FROM audio ORDER BY id"
                ).fetchall()
            recordings = [
                {"title": title, "url": f"/uploads/{stored_name}"}
                for title, stored_name in rows
            ]
            return self.send_json(200, recordings)

        if path.startswith("/api/") or path.startswith("/data/"):
            return self.send_json(404, {"error": "Nicht gefunden."})

        if path.rstrip("/") == "/uploads":
            return self.send_json(404, {"error": "Nicht gefunden."})

        if path.startswith("/uploads/"):
            stored_name = path.removeprefix("/uploads/")
            if not stored_name or Path(stored_name).name != stored_name:
                return self.send_json(404, {"error": "Nicht gefunden."})
            with sqlite3.connect(DATABASE) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM audio WHERE stored_name = ?", (stored_name,)
                ).fetchone()
            if not exists:
                return self.send_json(404, {"error": "Nicht gefunden."})

        return super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path != "/api/audio":
            return self.send_json(404, {"error": "Nicht gefunden."})

        try:
            request_size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json(400, {"error": "Ungültige Anfragegröße."})

        if request_size <= 0:
            return self.send_json(400, {"error": "Es wurden keine Dateien ausgewählt."})
        if request_size > MAX_REQUEST_SIZE:
            return self.send_json(413, {"error": "Der Upload ist auf 100 MB begrenzt."})

        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            return self.send_json(400, {"error": "Es werden Audio-Dateien erwartet."})

        headers = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
        ).encode("utf-8")
        message = BytesParser(policy=default).parsebytes(
            headers + self.rfile.read(request_size)
        )
        if not message.is_multipart():
            return self.send_json(400, {"error": "Ungültiges Upload-Format."})

        files = []
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            if part.get_param("name", header="content-disposition") != "audio":
                continue

            original_name = part.get_filename() or ""
            safe_name = original_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
            extension = Path(safe_name).suffix.lower()
            content = part.get_payload(decode=True) or b""
            if not safe_name or extension not in AUDIO_EXTENSIONS or not content:
                return self.send_json(400, {"error": "Eine Datei ist kein unterstütztes Audioformat."})

            media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
            files.append((safe_name, extension, content, media_type))

        if not files:
            return self.send_json(400, {"error": "Es wurden keine Audiodateien gefunden."})
        if len(files) > MAX_FILES_PER_REQUEST:
            return self.send_json(413, {"error": "Pro Upload sind höchstens 20 Dateien erlaubt."})

        saved_paths = []
        try:
            with sqlite3.connect(DATABASE) as connection:
                for title, extension, content, media_type in files:
                    stored_name = f"{uuid.uuid4().hex}{extension}"
                    file_path = UPLOAD_DIR / stored_name
                    file_path.write_bytes(content)
                    saved_paths.append(file_path)
                    connection.execute(
                        "INSERT INTO audio (title, stored_name, content_type) VALUES (?, ?, ?)",
                        (title, stored_name, media_type),
                    )
        except (OSError, sqlite3.Error):
            for file_path in saved_paths:
                file_path.unlink(missing_ok=True)
            return self.send_json(500, {"error": "Die Audiodateien konnten nicht gespeichert werden."})

        self.send_json(201, {"uploaded": len(files)})


if __name__ == "__main__":
    initialize_database()
    port = int(os.environ.get("PORT", "5173"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AudioRequestHandler)
    print(f"Server läuft unter http://localhost:{port}")
    server.serve_forever()