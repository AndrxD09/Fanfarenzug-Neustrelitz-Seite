import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from email.parser import BytesParser
from email.policy import default
from http.cookies import CookieError, SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def load_local_environment():
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == "AUDIO_UPLOAD_PASSWORD":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault("AUDIO_UPLOAD_PASSWORD", value)


load_local_environment()

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AUDIO_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = Path(os.environ.get("AUDIO_UPLOAD_DIR", ROOT / "uploads")).resolve()
DATABASE = DATA_DIR / "audio.sqlite3"
MAX_REQUEST_SIZE = 100 * 1024 * 1024
MAX_FILES_PER_REQUEST = 20
SESSION_DURATION_SECONDS = 8 * 60 * 60
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".oga", ".ogg",
    ".opus", ".wav", ".weba", ".webm", ".wma",
}
VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".flv", ".m2ts", ".m4v",
    ".mkv", ".mov", ".mp4", ".mpe", ".mpeg", ".mpg", ".mts", ".ogv",
    ".ts", ".vob", ".webm", ".wmv",
}
REGISTERS = {
    "Blaeser 1. Stimme",
    "Blaeser 2. Stimme",
    "Blaeser 3. Stimme",
    "Blaeser 4. Stimme",
    "Trommel",
    "Hochtrommel",
}


def normalize_register(value):
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def canonical_register(value):
    candidates = [value]
    for encoding in ("latin-1", "cp1252"):
        try:
            candidates.append(value.encode(encoding).decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    for candidate in candidates:
        normalized = normalize_register(candidate)
        canonical = REGISTER_BY_NORMALIZED_VALUE.get(normalized)
        if canonical:
            return canonical
        canonical = REGISTER_BY_NORMALIZED_VALUE.get(
            normalize_register(candidate.replace("ä", "ae").replace("Ä", "Ae"))
        )
        if canonical:
            return canonical
    return None


REGISTER_BY_NORMALIZED_VALUE = {
    normalize_register(register): register for register in REGISTERS
}


class VideoProcessingError(Exception):
    pass


def extract_audio(content, extension):
    with tempfile.TemporaryDirectory(dir=UPLOAD_DIR) as temp_dir:
        source = Path(temp_dir) / f"video{extension}"
        destination = Path(temp_dir) / "audio.mp3"
        source.write_bytes(content)
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
                    "-map", "0:a:0", "-vn", "-codec:a", "libmp3lame",
                    "-q:a", "2", "-f", "mp3", "-y", str(destination),
                ],
                check=False,
                capture_output=True,
                timeout=300,
            )
        except FileNotFoundError as error:
            raise RuntimeError("FFmpeg ist nicht installiert.") from error
        except subprocess.TimeoutExpired as error:
            raise VideoProcessingError("Die Videoverarbeitung hat zu lange gedauert.") from error

        if result.returncode != 0 or not destination.is_file():
            raise VideoProcessingError(
                "Aus dem Video konnte keine Audiospur extrahiert werden."
            )
        return destination.read_bytes()


def initialize_database():
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DATABASE) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                track_number INTEGER,
                register TEXT NOT NULL DEFAULT '',
                stored_name TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(audio)")
        }
        if "track_number" not in columns:
            connection.execute(
                "ALTER TABLE audio ADD COLUMN track_number INTEGER"
            )
        if "register" not in columns:
            connection.execute(
                "ALTER TABLE audio ADD COLUMN register TEXT NOT NULL DEFAULT ''"
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

    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def session_token(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookie.get("audio_session")
        return morsel.value if morsel else None

    def is_authenticated(self):
        token = self.session_token()
        if not token:
            return False
        now = time.time()
        with SESSIONS_LOCK:
            expired = [key for key, expiry in SESSIONS.items() if expiry <= now]
            for key in expired:
                del SESSIONS[key]
            return SESSIONS.get(token, 0) > now

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/session":
            return self.send_json(200, {"authenticated": self.is_authenticated()})

        if path == "/api/audio":
            with sqlite3.connect(DATABASE) as connection:
                rows = connection.execute(
                    """
                    SELECT id, title, track_number, register, stored_name
                    FROM audio
                    ORDER BY
                        CASE WHEN track_number IS NULL THEN 1 ELSE 0 END,
                        track_number,
                        title COLLATE NOCASE,
                        id
                    """
                ).fetchall()
            recordings = [
                {
                    "id": recording_id,
                    "title": title,
                    "number": track_number,
                    "register": canonical_register(register) or register,
                    "url": f"/uploads/{stored_name}",
                }
                for recording_id, title, track_number, register, stored_name in rows
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
                recording = connection.execute(
                    "SELECT content_type FROM audio WHERE stored_name = ?", (stored_name,)
                ).fetchone()
            if not recording:
                return self.send_json(404, {"error": "Nicht gefunden."})
            if self.headers.get("Range"):
                return self.send_upload_range(UPLOAD_DIR / stored_name, recording[0])

        return super().do_GET()

    def send_upload_range(self, file_path, content_type):
        try:
            file_size = file_path.stat().st_size
            file = file_path.open("rb")
        except OSError:
            return self.send_json(404, {"error": "Nicht gefunden."})

        range_header = self.headers.get("Range", "")
        unit, separator, requested_range = range_header.partition("=")
        if not separator or unit.strip().lower() != "bytes" or "," in requested_range:
            file.close()
            return self.send_unsatisfied_range(file_size)

        first, separator, last = requested_range.strip().partition("-")
        if not separator:
            file.close()
            return self.send_unsatisfied_range(file_size)

        if not first:
            if not last.isdecimal() or int(last) == 0 or file_size == 0:
                file.close()
                return self.send_unsatisfied_range(file_size)
            start = max(file_size - int(last), 0)
            end = file_size - 1
        else:
            if not first.isdecimal() or (last and not last.isdecimal()):
                file.close()
                return self.send_unsatisfied_range(file_size)
            start = int(first)
            end = min(int(last), file_size - 1) if last else file_size - 1
            if start >= file_size or end < start:
                file.close()
                return self.send_unsatisfied_range(file_size)

        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            file.seek(start)
            remaining = length
            while remaining:
                chunk = file.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        finally:
            file.close()

    def send_unsatisfied_range(self, file_size):
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{file_size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/login":
            return self.handle_login()
        if path == "/api/logout":
            token = self.session_token()
            if token:
                with SESSIONS_LOCK:
                    SESSIONS.pop(token, None)
            return self.send_json(
                200,
                {"authenticated": False},
                {"Set-Cookie": "audio_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"},
            )
        if path != "/api/audio":
            return self.send_json(404, {"error": "Nicht gefunden."})
        if not self.is_authenticated():
            return self.send_json(401, {"error": "Bitte zuerst anmelden."})

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
            return self.send_json(400, {"error": "Es werden Audio- oder Video-Dateien erwartet."})

        headers = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
        ).encode("utf-8")
        message = BytesParser(policy=default).parsebytes(
            headers + self.rfile.read(request_size)
        )
        if not message.is_multipart():
            return self.send_json(400, {"error": "Ungültiges Upload-Format."})

        files = []
        metadata = None
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            part_name = part.get_param("name", header="content-disposition")
            if part_name == "metadata":
                try:
                    metadata = json.loads(part.get_content())
                except (TypeError, json.JSONDecodeError):
                    return self.send_json(400, {"error": "Die Titelangaben sind ungültig."})
                continue
            if part_name != "audio":
                continue

            original_name = part.get_filename() or ""
            safe_name = original_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
            extension = Path(safe_name).suffix.lower()
            content = part.get_payload(decode=True) or b""
            is_video = extension in VIDEO_EXTENSIONS
            if (
                not safe_name
                or (extension not in AUDIO_EXTENSIONS and not is_video)
                or not content
            ):
                return self.send_json(
                    400,
                    {"error": "Eine Datei ist kein unterstütztes Audio- oder Videoformat."},
                )

            media_type = "audio/mpeg" if is_video else (
                mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
            )
            files.append({
                "filename": safe_name,
                "extension": extension,
                "content": content,
                "media_type": media_type,
                "is_video": is_video,
            })

        if not files:
            return self.send_json(
                400, {"error": "Es wurden keine Audio- oder Videodateien gefunden."}
            )
        if len(files) > MAX_FILES_PER_REQUEST:
            return self.send_json(413, {"error": "Pro Upload sind höchstens 20 Dateien erlaubt."})
        if metadata is None:
            metadata = [
                {
                    "title": Path(file["filename"]).stem,
                    "number": None,
                    "register": "",
                }
                for file in files
            ]
        if isinstance(metadata, list):
            for item in metadata:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("number"), int)
                    and not isinstance(item["number"], bool)
                    and item["number"] > 30
                ):
                    item["number"] = 30
        if (
            not isinstance(metadata, list)
            or len(metadata) != len(files)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("title"), str)
                or not item["title"].strip()
                or len(item["title"]) > 200
                or (
                    item.get("number") is not None
                    and (
                        isinstance(item["number"], bool)
                        or not isinstance(item["number"], int)
                        or item["number"] < 1
                    )
                )
                for item in metadata
            )
        ):
            return self.send_json(
                400,
                {"error": "Jede Datei benötigt einen Titel und eine Titelnummer von 1 bis 30 oder keine Nummer."},
            )

        invalid_register_files = []
        for index, item in enumerate(metadata):
            register = item.get("register")
            normalized_register = (
                canonical_register(register) if isinstance(register, str) else None
            )
            if normalized_register is None:
                invalid_register_files.append(
                    (files[index]["filename"], register)
                )
            else:
                item["register"] = normalized_register
        if invalid_register_files:
            return self.send_json(
                400,
                {
                    "error": (
                        "Für "
                        + ", ".join(
                            f"„{filename}“ (empfangen: {register!r})"
                            for filename, register in invalid_register_files
                        )
                        + " bitte im Feld „Register“ eines dieser Register auswählen: "
                        + ", ".join(sorted(REGISTERS))
                        + "."
                    )
                },
            )

        saved_paths = []
        converted = 0
        try:
            with sqlite3.connect(DATABASE) as connection:
                for file, item in zip(files, metadata):
                    title = item["title"].strip()
                    track_number = item.get("number")
                    register = item["register"]
                    extension = file["extension"]
                    content = file["content"]
                    media_type = file["media_type"]
                    is_video = file["is_video"]
                    if is_video:
                        content = extract_audio(content, extension)
                        extension = ".mp3"
                        converted += 1
                    stored_name = f"{uuid.uuid4().hex}{extension}"
                    file_path = UPLOAD_DIR / stored_name
                    file_path.write_bytes(content)
                    saved_paths.append(file_path)
                    connection.execute(
                        "INSERT INTO audio (title, track_number, register, stored_name, content_type) VALUES (?, ?, ?, ?, ?)",
                        (title, track_number, register, stored_name, media_type),
                    )
        except RuntimeError as error:
            for file_path in saved_paths:
                file_path.unlink(missing_ok=True)
            return self.send_json(503, {"error": str(error)})
        except VideoProcessingError as error:
            for file_path in saved_paths:
                file_path.unlink(missing_ok=True)
            return self.send_json(400, {"error": str(error)})
        except (OSError, sqlite3.Error):
            for file_path in saved_paths:
                file_path.unlink(missing_ok=True)
            return self.send_json(
                500, {"error": "Die Audio- oder Videodateien konnten nicht gespeichert werden."}
            )

        self.send_json(201, {"uploaded": len(files), "converted": converted})

    def handle_login(self):
        configured_password = os.environ.get("AUDIO_UPLOAD_PASSWORD")
        if not configured_password:
            return self.send_json(
                503,
                {"error": "Das Upload-Passwort ist auf dem Server nicht konfiguriert."},
            )
        try:
            request_size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json(400, {"error": "Ungültige Anfragegröße."})
        if request_size <= 0 or request_size > 4096:
            return self.send_json(400, {"error": "Ungültige Anmeldung."})
        try:
            credentials = json.loads(self.rfile.read(request_size))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self.send_json(400, {"error": "Ungültige Anmeldung."})
        password = credentials.get("password") if isinstance(credentials, dict) else None
        if not isinstance(password, str) or not hmac.compare_digest(
            password.encode("utf-8"), configured_password.encode("utf-8")
        ):
            return self.send_json(401, {"error": "Das Passwort ist nicht richtig."})

        redirect_to = credentials.get("next", "/")
        if (
            not isinstance(redirect_to, str)
            or not redirect_to.startswith("/")
            or redirect_to.startswith("//")
            or "\\" in redirect_to
            or any(ord(character) < 32 for character in redirect_to)
        ):
            redirect_to = "/"
        token = secrets.token_urlsafe(32)
        with SESSIONS_LOCK:
            SESSIONS[token] = time.time() + SESSION_DURATION_SECONDS
        cookie = (
            f"audio_session={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_DURATION_SECONDS}"
        )
        if self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https":
            cookie += "; Secure"
        return self.send_json(
            200, {"redirect": redirect_to}, {"Set-Cookie": cookie}
        )

    def do_DELETE(self):
        if not self.is_authenticated():
            return self.send_json(401, {"error": "Bitte zuerst anmelden."})
        path = urlsplit(self.path).path
        prefix = "/api/audio/"
        recording_id = path.removeprefix(prefix) if path.startswith(prefix) else ""
        if not recording_id.isdecimal():
            return self.send_json(404, {"error": "Aufnahme nicht gefunden."})

        try:
            with sqlite3.connect(DATABASE) as connection:
                row = connection.execute(
                    "SELECT stored_name FROM audio WHERE id = ?", (recording_id,)
                ).fetchone()
                if not row:
                    return self.send_json(404, {"error": "Aufnahme nicht gefunden."})
                connection.execute("DELETE FROM audio WHERE id = ?", (recording_id,))
            try:
                (UPLOAD_DIR / row[0]).unlink(missing_ok=True)
            except OSError:
                self.log_error("Audiodatei %s konnte nicht gelöscht werden.", row[0])
                return self.send_json(
                    500,
                    {"error": "Der Eintrag wurde entfernt, die Audiodatei konnte aber nicht gelöscht werden."},
                )
        except sqlite3.Error:
            return self.send_json(500, {"error": "Die Aufnahme konnte nicht gelöscht werden."})
        return self.send_json(200, {"deleted": True})


if __name__ == "__main__":
    initialize_database()
    port = int(os.environ.get("PORT", "5173"))
    server = ThreadingHTTPServer(("0.0.0.0", port), AudioRequestHandler)
    print(f"Server läuft unter http://localhost:{port}")
    server.serve_forever()