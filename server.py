# -*- coding: utf-8 -*-
"""Cloud entrypoint for the MP3 to Text web service."""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import app


STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
ORIGINAL_DO_GET = app.AppHandler.do_GET


def build_health_payload() -> dict[str, Any]:
    data_dir = os.environ.get("MP3_TO_TEXT_DATA_DIR") or os.path.join(os.getcwd(), "mp3_to_text_data")
    os.makedirs(data_dir, exist_ok=True)
    return {
        "ok": True,
        "service": "mp3-to-text-arabic",
        "startedAt": STARTED_AT,
        "dataDir": data_dir,
        "dataDirWritable": os.access(data_dir, os.W_OK),
    }


def send_json(handler: app.AppHandler, payload: dict[str, Any], status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def cloud_do_get(handler: app.AppHandler) -> None:
    parsed = urllib.parse.urlparse(handler.path)
    if parsed.path == "/healthz":
        send_json(handler, build_health_payload())
        return
    ORIGINAL_DO_GET(handler)


app.AppHandler.do_GET = cloud_do_get


if __name__ == "__main__":
    app.main()
