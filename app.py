# -*- coding: utf-8 -*-
"""Self-contained local web desktop app for MP3 transcription."""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any


APP_TITLE = "محول MP3 إلى نص"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
JOBS: dict[str, "JobState"] = {}
JOBS_LOCK = threading.Lock()
PROJECTS_LOCK = threading.Lock()


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    data: bytes


@dataclass(frozen=True)
class JobOptions:
    engine: str
    api_key: str
    language: str
    model: str
    local_model: str
    prompt: str
    output_format: str
    output_mode: str
    include_headers: bool


@dataclass
class JobState:
    job_id: str
    options: JobOptions
    files: list[UploadedFile]
    progress: float = 0.0
    status: str = "في الانتظار"
    logs: list[str] = field(default_factory=list)
    preview: str = ""
    done: bool = False
    error: str = ""
    output_bytes: bytes = b""
    output_filename: str = ""
    content_type: str = "application/octet-stream"
    project_id: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "jobId": self.job_id,
                "progress": self.progress,
                "status": self.status,
                "logs": self.logs[-80:],
                "preview": self.preview,
                "done": self.done,
                "error": self.error,
                "filename": self.output_filename,
                "contentType": self.content_type,
                "projectId": self.project_id,
            }

    def set_status(self, status: str, progress: float | None = None) -> None:
        with self.lock:
            self.status = status
            if progress is not None:
                self.progress = max(0.0, min(100.0, progress))

    def add_log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)

    def complete(self, data: bytes, filename: str, content_type: str, preview: str, project_id: str = "") -> None:
        with self.lock:
            self.output_bytes = data
            self.output_filename = filename
            self.content_type = content_type
            self.preview = preview
            self.project_id = project_id
            self.progress = 100.0
            self.status = "اكتمل التحويل"
            self.done = True

    def fail(self, message: str) -> None:
        with self.lock:
            self.error = message
            self.status = "حدث خطأ"


class AppHandler(BaseHTTPRequestHandler):
    server_version = "MP3ToTextLocal/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_html(render_index())
            return
        if parsed.path == "/status":
            job = self.get_job(parsed)
            if not job:
                self.send_json({"error": "لم يتم العثور على المهمة."}, status=404)
                return
            self.send_json(job.snapshot())
            return
        if parsed.path == "/download":
            job = self.get_job(parsed)
            if not job:
                self.send_json({"error": "لم يتم العثور على المهمة."}, status=404)
                return
            with job.lock:
                if not job.done or not job.output_bytes:
                    self.send_json({"error": "الملف غير جاهز بعد."}, status=409)
                    return
                data = job.output_bytes
                filename = job.output_filename
                content_type = job.content_type
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f"attachment; filename={filename}")
            self.send_header("X-Filename", filename)
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/projects":
            self.send_json({"projects": list_project_summaries()})
            return
        if parsed.path == "/project":
            project = self.get_project(parsed)
            if not project:
                self.send_json({"error": "لم يتم العثور على المشروع."}, status=404)
                return
            self.send_json({"project": project})
            return
        if parsed.path == "/project/download":
            project = self.get_project(parsed)
            if not project:
                self.send_json({"error": "لم يتم العثور على المشروع."}, status=404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            output_format = query.get("format", [project.get("output_format", "docx")])[0]
            output_mode = query.get("mode", [project.get("output_mode", "combined")])[0]
            data, filename, content_type = build_project_download(project, output_format, output_mode)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f"attachment; filename={filename}")
            self.send_header("X-Filename", filename)
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/start":
            self.handle_start()
            return
        if parsed.path == "/shutdown":
            self.send_json({"ok": True})
            threading.Thread(target=self.shutdown_server, daemon=True).start()
            return
        if parsed.path == "/project/update":
            payload = self.read_json_body()
            project = update_project(
                str(payload.get("id", "")),
                str(payload.get("transcript", "")),
                str(payload.get("title", "")).strip() or None,
                payload.get("order") if isinstance(payload.get("order"), dict) else None,
            )
            if not project:
                self.send_json({"error": "لم يتم العثور على المشروع."}, status=404)
                return
            self.send_json({"project": project})
            return
        if parsed.path == "/project/delete":
            payload = self.read_json_body()
            ok = delete_project(str(payload.get("id", "")))
            self.send_json({"ok": ok})
            return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def handle_start(self) -> None:
        try:
            fields, files = self.parse_multipart_request()
            options = JobOptions(
                engine=fields.get("engine", "local").strip(),
                api_key=fields.get("apiKey", "").strip(),
                language=fields.get("language", "").strip(),
                model=fields.get("model", "gpt-4o-transcribe").strip(),
                local_model=fields.get("localModel", "small").strip(),
                prompt=fields.get("prompt", "").strip(),
                output_format=fields.get("format", "docx").strip(),
                output_mode=fields.get("outputMode", "combined").strip(),
                include_headers=fields.get("includeHeaders", "true") == "true",
            )
            if options.engine == "api" and not options.api_key:
                self.send_json({"error": "أدخل مفتاح OpenAI API."}, status=400)
                return
            if options.engine not in {"api", "local"}:
                self.send_json({"error": "اختر طريقة تحويل صحيحة."}, status=400)
                return
            if options.output_mode not in {"combined", "separate"}:
                self.send_json({"error": "اختر طريقة إخراج صحيحة."}, status=400)
                return
            if not files:
                self.send_json({"error": "اختر ملفًا صوتيًا واحدًا على الأقل."}, status=400)
                return
            job_id = uuid.uuid4().hex
            job = JobState(job_id=job_id, options=options, files=files)
            with JOBS_LOCK:
                JOBS[job_id] = job
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            self.send_json({"jobId": job_id})
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, status=400)

    def parse_multipart_request(self) -> tuple[dict[str, str], list[UploadedFile]]:
        content_type = self.headers.get("Content-Type", "")
        boundary_match = re.search(r"boundary=(.+)", content_type)
        if not boundary_match:
            raise ValueError("طلب الرفع غير صحيح.")
        boundary = boundary_match.group(1).strip().strip('"').encode("utf-8")
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        fields: dict[str, str] = {}
        files: list[UploadedFile] = []
        for raw_part in body.split(b"--" + boundary):
            part = raw_part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            if part.endswith(b"--"):
                part = part[:-2].strip(b"\r\n")
            if b"\r\n\r\n" not in part:
                continue
            header_bytes, content = part.split(b"\r\n\r\n", 1)
            header_text = header_bytes.decode("utf-8", errors="replace")
            name = find_header_value(header_text, "name")
            filename = find_header_value(header_text, "filename")
            if not name:
                continue
            if filename:
                safe_name = os.path.basename(filename.replace("\\", "/")) or "audio.mp3"
                files.append(UploadedFile(filename=safe_name, data=content))
            else:
                fields[name] = content.decode("utf-8", errors="replace")
        return fields, files

    def get_job(self, parsed: urllib.parse.ParseResult) -> JobState | None:
        job_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
        with JOBS_LOCK:
            return JOBS.get(job_id)

    def get_project(self, parsed: urllib.parse.ParseResult) -> dict[str, Any] | None:
        project_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
        return get_project(project_id)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("طلب JSON غير صحيح.") from exc
        return payload if isinstance(payload, dict) else {}

    def send_html(self, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def shutdown_server(self) -> None:
        time.sleep(0.3)
        self.server.shutdown()


def run_job(job: JobState) -> None:
    try:
        results: list[tuple[str, str]] = []
        total = len(job.files)
        local_model: Any | None = None
        if job.options.engine == "local":
            job.set_status(f"تحميل النموذج المحلي: {job.options.local_model}", 1)
            local_model = load_local_whisper_model(job.options.local_model)
            job.add_log(f"تم تحميل النموذج المحلي {job.options.local_model}.")
        for index, uploaded in enumerate(job.files, start=1):
            job.set_status(f"جاري تحويل {index} من {total}: {uploaded.filename}", ((index - 1) / total) * 100)
            if job.options.engine == "api" and len(uploaded.data) > MAX_UPLOAD_BYTES:
                job.add_log(f"تم تخطي {uploaded.filename}: الحجم أكبر من 25MB.")
                job.set_status(job.status, (index / total) * 100)
                continue
            text = (
                transcribe_local_file(local_model, uploaded, job, index, total)
                if job.options.engine == "local"
                else transcribe_api_file(uploaded, job)
            )
            if not text:
                job.add_log(f"لم يتم العثور على كلام واضح في {uploaded.filename}.")
                text = "[لم يتم العثور على كلام واضح في هذا الملف]"
            results.append((uploaded.filename, text))
            job.add_log(f"تم تحويل {uploaded.filename}.")
            job.set_status(job.status, (index / total) * 100)
        if not results:
            raise RuntimeError("لم يتم تحويل أي ملف.")
        now = datetime.now().strftime("%Y%m%d_%H%M")
        preview = build_combined_text(results, True)
        if job.options.output_mode == "separate":
            data = build_separate_outputs_zip(results, job.options.output_format, job.options.language)
            filename = f"transcripts_{now}.zip"
            content_type = "application/zip"
        else:
            combined = build_combined_text(results, job.options.include_headers)
            if job.options.output_format == "txt":
                data = combined.encode("utf-8-sig")
                filename = f"transcript_{now}.txt"
                content_type = "text/plain; charset=utf-8"
            else:
                data = build_docx_bytes(combined, rtl=(job.options.language != "en"))
                filename = f"transcript_{now}.docx"
                content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            preview = combined
        project = create_project_record(job, results, preview)
        job.complete(data, filename, content_type, preview, str(project.get("id", "")))
    except Exception as exc:  # noqa: BLE001
        job.fail(str(exc))


def get_data_dir() -> str:
    path = os.environ.get("MP3_TO_TEXT_DATA_DIR") or os.path.join(os.getcwd(), "mp3_to_text_data")
    os.makedirs(path, exist_ok=True)
    return path


def get_projects_path() -> str:
    return os.path.join(get_data_dir(), "projects.json")


def load_projects() -> list[dict[str, Any]]:
    path = get_projects_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def save_projects(projects: list[dict[str, Any]]) -> None:
    path = get_projects_path()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(projects, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def list_project_summaries() -> list[dict[str, Any]]:
    with PROJECTS_LOCK:
        projects = load_projects()
    projects.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    summaries: list[dict[str, Any]] = []
    for project in projects:
        order = project.get("order") if isinstance(project.get("order"), dict) else {}
        summaries.append({
            "id": project.get("id", ""),
            "title": project.get("title", "مشروع"),
            "createdAt": project.get("created_at", ""),
            "updatedAt": project.get("updated_at", ""),
            "engine": project.get("engine", ""),
            "language": project.get("language", ""),
            "fileCount": len(project.get("items", [])),
            "customer": order.get("customer", ""),
            "orderStatus": order.get("status", ""),
            "orderTotal": order.get("total", 0),
            "currency": order.get("currency", "ILS"),
            "preview": str(project.get("transcript", ""))[:180],
        })
    return summaries


def get_project(project_id: str) -> dict[str, Any] | None:
    if not project_id:
        return None
    with PROJECTS_LOCK:
        return next((p for p in load_projects() if str(p.get("id")) == project_id), None)


def create_project_record(job: JobState, results: list[tuple[str, str]], transcript: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    title = safe_output_stem(results[0][0] if results else "transcript")
    if len(results) > 1:
        title = f"{title} + {len(results) - 1}"
    project = {
        "id": uuid.uuid4().hex,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "engine": job.options.engine,
        "language": job.options.language,
        "output_format": job.options.output_format,
        "output_mode": job.options.output_mode,
        "source_files": [uploaded.filename for uploaded in job.files],
        "transcript": transcript,
        "items": [{"filename": filename, "text": text} for filename, text in results],
        "order": default_order_record(),
    }
    with PROJECTS_LOCK:
        projects = load_projects()
        projects.append(project)
        save_projects(projects)
    return project


def default_order_record() -> dict[str, Any]:
    return {"customer": "", "package": "auto", "minutes": 0, "currency": "ILS", "status": "new", "total": 0}


def sanitize_order_record(order: dict[str, Any]) -> dict[str, Any]:
    package = str(order.get("package", "auto"))
    if package not in {"auto", "formatted", "reviewed", "urgent"}:
        package = "auto"
    currency = str(order.get("currency", "ILS"))
    if currency not in {"ILS", "USD"}:
        currency = "ILS"
    status = str(order.get("status", "new"))
    if status not in {"new", "quoted", "paid", "delivered", "archived"}:
        status = "new"
    try:
        minutes = max(0.0, float(order.get("minutes", 0)))
    except (TypeError, ValueError):
        minutes = 0.0
    try:
        total = max(0.0, float(order.get("total", 0)))
    except (TypeError, ValueError):
        total = 0.0
    return {
        "customer": str(order.get("customer", "")).strip()[:160],
        "package": package,
        "minutes": round(minutes, 2),
        "currency": currency,
        "status": status,
        "total": round(total, 2),
    }


def update_project(project_id: str, transcript: str, title: str | None = None, order: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not project_id:
        return None
    with PROJECTS_LOCK:
        projects = load_projects()
        for project in projects:
            if str(project.get("id")) == project_id:
                project["transcript"] = transcript
                if title:
                    project["title"] = title
                if order is not None:
                    project["order"] = sanitize_order_record(order)
                project["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                save_projects(projects)
                return project
    return None


def delete_project(project_id: str) -> bool:
    if not project_id:
        return False
    with PROJECTS_LOCK:
        projects = load_projects()
        kept = [p for p in projects if str(p.get("id")) != project_id]
        if len(kept) == len(projects):
            return False
        save_projects(kept)
        return True


def build_project_download(project: dict[str, Any], output_format: str, output_mode: str) -> tuple[bytes, str, str]:
    title = safe_output_stem(str(project.get("title") or "transcript"))
    language = str(project.get("language") or "")
    if output_mode == "separate" and project.get("items"):
        results = [
            (str(item.get("filename") or "transcript"), str(item.get("text") or ""))
            for item in project.get("items", []) if isinstance(item, dict)
        ]
        return build_separate_outputs_zip(results, output_format, language), f"{title}.zip", "application/zip"
    transcript = str(project.get("transcript") or "")
    if output_format == "txt":
        return (transcript.strip() + "\n").encode("utf-8-sig"), f"{title}.txt", "text/plain; charset=utf-8"
    return build_docx_bytes(transcript.strip() + "\n", rtl=(language != "en")), f"{title}.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def transcribe_api_file(uploaded: UploadedFile, job: JobState) -> str:
    params: dict[str, Any] = {
        "model": job.options.model,
        "response_format": "diarized_json" if job.options.model == "gpt-4o-transcribe-diarize" else "json",
    }
    if job.options.language:
        params["language"] = job.options.language
    if job.options.prompt and job.options.model != "gpt-4o-transcribe-diarize":
        params["prompt"] = job.options.prompt
    if job.options.model == "gpt-4o-transcribe-diarize":
        params["chunking_strategy"] = "auto"
    return extract_text(call_openai_transcription_api(job.options.api_key, uploaded, params)).strip()


def load_local_whisper_model(model_name: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("مكتبة faster-whisper غير موجودة.") from exc
    if model_name not in {"tiny", "base", "small", "medium", "large-v3"}:
        raise RuntimeError("النموذج المحلي المختار غير مدعوم.")
    try:
        local_only = os.environ.get("WHISPER_LOCAL_FILES_ONLY", "1").strip().lower() not in {"0", "false", "no"}
        return WhisperModel(
            model_name,
            device=os.environ.get("WHISPER_DEVICE", "cpu"),
            compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
            local_files_only=local_only,
            cpu_threads=max(1, min(8, os.cpu_count() or 2)),
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"تعذر تحميل النموذج المحلي {model_name}.") from exc


def transcribe_local_file(model: Any, uploaded: UploadedFile, job: JobState, file_index: int, total_files: int) -> str:
    suffix = os.path.splitext(uploaded.filename)[1] or ".mp3"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        temp_file.write(uploaded.data)
        temp_path = temp_file.name
    try:
        segments, info = model.transcribe(
            temp_path,
            language=job.options.language or None,
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=True,
        )
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        start_progress = ((file_index - 1) / total_files) * 100
        file_share = 100 / total_files
        lines: list[str] = []
        for segment in segments:
            text = str(getattr(segment, "text", "") or "").strip()
            if text:
                lines.append(text)
            if duration > 0:
                end_time = float(getattr(segment, "end", 0.0) or 0.0)
                job.set_status(
                    f"جاري تحويل {file_index} من {total_files}: {uploaded.filename}",
                    start_progress + min(0.98, end_time / duration) * file_share,
                )
        return "\n".join(lines).strip()
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def call_openai_transcription_api(api_key: str, uploaded: UploadedFile, params: dict[str, Any]) -> dict[str, Any]:
    boundary = f"----mp3totext{uuid.uuid4().hex}"
    body = build_api_multipart_body(boundary, uploaded, params)
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(parse_openai_error(detail) or f"فشل الطلب إلى OpenAI: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"تعذر الاتصال بخدمة OpenAI: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("رجع رد غير مفهوم من خدمة OpenAI.") from exc


def build_api_multipart_body(boundary: str, uploaded: UploadedFile, fields: dict[str, Any]) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            f"{value}\r\n".encode(),
        ])
    content_type = mimetypes.guess_type(uploaded.filename)[0] or "audio/mpeg"
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{uploaded.filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        uploaded.data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks)


def extract_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        segments = response.get("segments")
        return format_segments(segments) if segments else str(response.get("text", ""))
    return ""


def format_segments(segments: Any) -> str:
    lines: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        if text:
            speaker = segment.get("speaker")
            lines.append(f"{speaker}: {text}" if speaker else text)
    return "\n".join(lines)


def build_combined_text(results: list[tuple[str, str]], include_headers: bool) -> str:
    parts = [f"===== {filename} =====\n{text.strip()}" if include_headers else text.strip() for filename, text in results]
    return "\n\n".join(parts).strip() + "\n"


def build_separate_outputs_zip(results: list[tuple[str, str]], output_format: str, language: str) -> bytes:
    output = BytesIO()
    used_names: set[str] = set()
    extension = "txt" if output_format == "txt" else "docx"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_filename, text in results:
            stem = unique_stem(safe_output_stem(source_filename), used_names)
            data = (text.strip() + "\n").encode("utf-8-sig") if output_format == "txt" else build_docx_bytes(text.strip() + "\n", rtl=(language != "en"))
            archive.writestr(f"{stem}.{extension}", data)
    return output.getvalue()


def safe_output_stem(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0].strip()
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem or "transcript"


def unique_stem(stem: str, used_names: set[str]) -> str:
    candidate = stem
    index = 2
    while candidate.lower() in used_names:
        candidate = f"{stem}_{index}"
        index += 1
    used_names.add(candidate.lower())
    return candidate


def build_docx_bytes(text: str, rtl: bool = True) -> bytes:
    document_xml = build_document_xml(text, rtl)
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""
    package_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""
    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
  xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Transcript</dc:title><dc:creator>MP3 to Text Arabic</dc:creator>
  <cp:lastModifiedBy>MP3 to Text Arabic</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>
</cp:coreProperties>"""
    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
 <Application>MP3 to Text Arabic</Application>
</Properties>"""
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types)
        docx.writestr("_rels/.rels", package_rels)
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("docProps/core.xml", core_xml)
        docx.writestr("docProps/app.xml", app_xml)
    return output.getvalue()


def build_document_xml(text: str, rtl: bool) -> str:
    paragraph_xml = "\n".join(build_paragraph(line, rtl) for line in (text.splitlines() or [""]))
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:xml="http://www.w3.org/XML/1998/namespace">
<w:body>{paragraph_xml}<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body>
</w:document>"""


def build_paragraph(line: str, rtl: bool) -> str:
    alignment = "right" if rtl else "left"
    bidi = "<w:bidi/>" if rtl else ""
    rtl_run = "<w:rtl/>" if rtl else ""
    lang = "ar-SA" if rtl else "en-US"
    return f'<w:p><w:pPr>{bidi}<w:jc w:val="{alignment}"/></w:pPr><w:r><w:rPr>{rtl_run}<w:lang w:val="{lang}"/></w:rPr><w:t xml:space="preserve">{html.escape(line)}</w:t></w:r></w:p>'


def parse_openai_error(detail: str) -> str:
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return detail.strip()
    error = payload.get("error")
    return str(error.get("message") or "").strip() if isinstance(error, dict) else detail.strip()


def find_header_value(header_text: str, key: str) -> str:
    match = re.search(rf'{key}="([^"]*)"', header_text)
    if match:
        return match.group(1)
    match = re.search(rf"{key}=([^;\r\n]+)", header_text)
    return match.group(1).strip() if match else ""


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def render_index() -> str:
    return Path(__file__).with_name("index.html").read_text(encoding="utf-8")


def main() -> None:
    port = int(os.environ.get("PORT") or os.environ.get("MP3_TO_TEXT_PORT") or find_free_port())
    host = os.environ.get("MP3_TO_TEXT_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    server = ThreadingHTTPServer((host, port), AppHandler)
    browser_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{browser_host}:{port}/"
    if os.environ.get("MP3_TO_TEXT_NO_BROWSER") != "1" and host != "0.0.0.0":
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"{APP_TITLE}: {url}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
