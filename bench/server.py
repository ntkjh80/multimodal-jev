"""로컬 웹/API 서버. 정적 화면과 평가 API를 같은 출처에서 제공합니다.

외부 패키지 없는 개발용 서버입니다. 루프백 바인딩, Host/Origin 검증,
변경 요청의 세션 토큰 검사로 다른 웹사이트의 로컬 API 접근을 제한합니다.
인터넷 공개 운영에는 별도 인증·TLS·역방향 프록시가 필요합니다.
"""
import base64
import binascii
import csv
import hashlib
import io
import json
import mimetypes
import re
import secrets
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import MAX_ASSET_BYTES, MAX_REQUEST_BYTES, ROOT
from .datasets import parse_dataset, seed_demo, validate_dataset
from .metrics import summarize
from .providers import public_model, validate_model
from .runner import Runner
from .storage import Store, dumps, new_id, now


def media_type(data):
    """확장자를 신뢰하지 않고 실행 가능한 HTML/SVG를 미디어로 공개하지 않습니다."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "image"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "image"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "image"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", "audio"
    if data.startswith(b"ID3") or (len(data) > 2 and data[0] == 255 and data[1] & 224 == 224):
        return "audio/mpeg", "audio"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "video/mp4", "video"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm", "video"
    raise ValueError("PNG/JPEG/WebP, WAV/MP3, MP4/WebM 파일을 사용하세요.")


class Application:
    def __init__(self, directory):
        self.store = Store(directory)
        seed_demo(self.store)
        self.runner = Runner(self.store)
        self.csrf = secrets.token_urlsafe(32)

    def upload(self, payload):
        try:
            raw = base64.b64decode(payload["data"], validate=True)
        except (KeyError, ValueError, binascii.Error):
            raise ValueError("파일 인코딩을 확인하세요.") from None
        if not raw or len(raw) > MAX_ASSET_BYTES:
            raise ValueError("파일은 1바이트~20MiB여야 합니다.")
        mime, kind = media_type(raw)
        # 이름은 표시·매칭에만 사용합니다. 실제 저장 이름은 서버가 발급합니다.
        name = str(payload.get("name", "media")).replace("\\", "/").split("/")[-1][:160]
        asset = {"id": new_id("asset"), "name": name, "mime": mime, "kind": kind,
                 "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "created_at": now(),
                 "duration_seconds": None}
        if mime == "audio/wav":
            try:
                with wave.open(io.BytesIO(raw)) as wav:
                    asset["duration_seconds"] = wav.getnframes() / wav.getframerate()
            except (wave.Error, EOFError, ZeroDivisionError):
                raise ValueError("유효한 PCM WAV 파일이 아닙니다.") from None
        (self.store.directory / "assets" / asset["id"]).write_bytes(raw)
        self.store.put("assets", asset)
        return asset


def handler_class(app):
    """네트워크 포트 없이도 실제 요청 파서를 테스트할 수 있도록 분리합니다."""
    class Handler(BaseHTTPRequestHandler):
        server_version = "MultimodalBench"

        def log_message(self, fmt, *args):
            # 질문, API 키, 원본 파일 이름을 접근 로그에 남기지 않습니다.
            return

        def allowed(self, mutation=False):
            expected = "127.0.0.1:%d" % self.server.server_port
            alternate = "localhost:%d" % self.server.server_port
            host = self.headers.get("Host", "")
            if host not in (expected, alternate):
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://" + expected, "http://" + alternate):
                return False
            return not mutation or secrets.compare_digest(self.headers.get("X-Bench-Token", ""), app.csrf)

        def send(self, data, status=200, content_type="application/json; charset=utf-8", extra=None):
            if not isinstance(data, bytes):
                data = dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.allowed():
                return self.send({"error": "허용되지 않는 출처입니다."}, 403)
            try:
                self.get_route()
            except (ValueError, KeyError) as exc:
                self.send({"error": str(exc)}, 400)
            except Exception:
                self.send({"error": "서버 처리 오류가 발생했습니다."}, 500)

        def get_route(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path == "/api/bootstrap":
                runs = [self.run_list_item(r) for r in app.store.list("runs")]
                datasets = [{k: v for k, v in d.items() if k != "samples"} | {"size": len(d["samples"])}
                            for d in app.store.list("datasets")]
                return self.send({"csrf": app.csrf, "models": [public_model(m) for m in app.store.list("models")],
                                  "datasets": datasets, "assets": app.store.list("assets"), "runs": runs,
                                  "version": "0.1.0"})
            if path == "/api/health":
                return self.send({"status": "ok"})
            if path == "/api/template":
                return self.send(app.store.get("datasets", "demo-dataset"), extra={"Content-Disposition": 'attachment; filename="dataset-template.json"'})
            if path.startswith("/api/datasets/"):
                value = app.store.get("datasets", path.split("/")[-1])
                return self.send(value if value else {"error": "없는 데이터셋"}, 200 if value else 404)
            if path.startswith("/api/runs/"):
                parts = path.split("/")
                run = app.store.get("runs", parts[3])
                if not run:
                    return self.send({"error": "없는 실험"}, 404)
                rows = app.store.results(run["id"])
                result = dict(run, results=rows, summary=summarize(run, rows))
                if len(parts) == 5 and parts[4] == "export":
                    if urllib.parse.parse_qs(parsed.query).get("format", ["json"])[0] == "csv":
                        fields = ["model_id", "sample_id", "repetition", "modality", "status", "label", "choice",
                                  "input_tokens", "output_tokens", "token_source", "preprocess_ms", "request_ms",
                                  "total_ms", "cost_estimate", "demo", "error"]
                        output = io.StringIO()
                        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
                        writer.writeheader()
                        for row in rows:
                            safe = dict(row)
                            # Excel에서 열어도 사용자 문자열을 수식으로 실행하지 않도록 합니다.
                            for k, value in safe.items():
                                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                                    safe[k] = "'" + value
                            writer.writerow(safe)
                        return self.send(("\ufeff" + output.getvalue()).encode(), content_type="text/csv; charset=utf-8",
                                         extra={"Content-Disposition": 'attachment; filename="benchmark-results.csv"'})
                    return self.send(result, extra={"Content-Disposition": 'attachment; filename="benchmark-run.json"'})
                return self.send(result)
            if path.startswith("/media/"):
                asset = app.store.get("assets", path.split("/")[-1])
                if not asset:
                    return self.send({"error": "없는 파일"}, 404)
                raw = (app.store.directory / "assets" / asset["id"]).read_bytes()
                headers = {"Accept-Ranges": "bytes"}
                range_header = self.headers.get("Range")
                if range_header:
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
                    if not match:
                        return self.send(b"", 416, extra={"Content-Range": "bytes */%d" % len(raw)})
                    start = int(match.group(1))
                    end = min(int(match.group(2)) if match.group(2) else len(raw) - 1, len(raw) - 1)
                    if start > end or start >= len(raw):
                        return self.send(b"", 416, extra={"Content-Range": "bytes */%d" % len(raw)})
                    headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, len(raw))
                    return self.send(raw[start:end + 1], 206, asset["mime"], headers)
                return self.send(raw, content_type=asset["mime"], extra=headers)
            # 경로 정규화 후 정적 폴더 안에 있는 파일만 제공합니다.
            relative = "index.html" if path == "/" else path.lstrip("/")
            target = (ROOT / "web" / relative).resolve()
            web = (ROOT / "web").resolve()
            if web not in target.parents or not target.is_file() or target.suffix not in (".html", ".css", ".js", ".svg"):
                return self.send({"error": "페이지를 찾을 수 없습니다."}, 404)
            mime = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml"}[target.suffix]
            return self.send(target.read_bytes(), content_type=mime + "; charset=utf-8")

        @staticmethod
        def run_list_item(run):
            return {k: run.get(k) for k in ("id", "name", "status", "created_at", "updated_at", "total", "completed")} | {
                "model_count": len(run["models"]), "dataset_name": run["dataset"]["name"],
                "demo": any(m["provider"] == "demo" for m in run["models"])}

        def do_POST(self):
            if not self.allowed(mutation=True):
                return self.send({"error": "세션이 만료되었거나 허용되지 않는 요청입니다. 새로고침하세요."}, 403)
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    return self.send({"error": "요청 크기 제한을 초과했습니다."}, 413)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON 객체를 보내세요.")
                path = urllib.parse.urlsplit(self.path).path
                if path == "/api/assets":
                    return self.send(app.upload(payload), 201)
                if path == "/api/models":
                    model = validate_model(payload)
                    model["id"] = model["id"] or new_id("model")
                    if model["id"] == "demo" and model["provider"] != "demo":
                        raise ValueError("기본 데모 모델의 종류는 변경할 수 없습니다.")
                    app.store.put("models", model)
                    return self.send(public_model(model), 201)
                if path == "/api/datasets":
                    data = parse_dataset(payload["text"], payload.get("filename", "data.json")) if "text" in payload else payload
                    if payload.get("name"):
                        data["name"] = payload["name"]
                    dataset = validate_dataset(data, app.store)
                    app.store.put("datasets", dataset)
                    return self.send(dataset, 201)
                if path == "/api/runs":
                    return self.send(app.runner.create(payload), 201)
                match = re.fullmatch(r"/api/runs/([^/]+)/(stop|resume)", path)
                if match:
                    run_id, action = match.groups()
                    if action == "stop":
                        app.runner.stop(run_id)
                    else:
                        app.runner.resume(run_id)
                    return self.send({"ok": True})
                self.send({"error": "없는 API입니다."}, 404)
            except (ValueError, KeyError, TypeError) as exc:
                self.send({"error": str(exc)}, 400)
            except Exception:
                self.send({"error": "서버 처리 오류가 발생했습니다."}, 500)

    return Handler


def create_server(app, port=8766):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_class(app))
    server.daemon_threads = True
    return server
