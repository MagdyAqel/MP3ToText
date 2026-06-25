# MP3ToText

Arabic and English audio transcription web application powered by Faster-Whisper.

## Features

- Upload multiple audio files.
- Local Faster-Whisper transcription without an OpenAI API key.
- Arabic, English, or automatic language detection.
- Combine all transcripts into one file or export separate files in ZIP.
- Export Word (`.docx`) and text (`.txt`).
- Saved project library and transcript editor.
- Project title, source-file list, search, and automatic saving.
- Customer, package, duration, currency, order status, and quote calculation.
- Optional OpenAI transcription mode.

## Run Locally

Install Python 3.10 or newer, then:

```powershell
python -m pip install -r requirements.txt
python mp3_to_text_web_app.py
```

The application opens in the default browser. Faster-Whisper models are loaded from the local Hugging Face cache by default.

To allow the model to be downloaded automatically:

```powershell
$env:WHISPER_LOCAL_FILES_ONLY="0"
python mp3_to_text_web_app.py
```

## Deploy On Render

The repository includes:

- `Dockerfile`
- `render.yaml`
- `requirements.txt`

In Render:

1. Create a new **Web Service**.
2. Connect this GitHub repository.
3. Choose **Docker** as the runtime if it is not detected automatically.
4. Select at least the **Starter** plan.
5. Deploy the service.

Render supplies the `PORT` environment variable automatically. On the first transcription, Faster-Whisper downloads the selected model.

## Important Prototype Limitations

This repository is currently a single-server prototype:

- It does not include user accounts.
- All visitors share the same project library.
- Project storage uses a local JSON file and may be lost when a cloud instance is replaced or redeployed.
- Audio processing runs inside the web service and can be slow on CPU.
- Large public workloads need object storage, PostgreSQL, a job queue, and a separate GPU worker.

Do not use this version for confidential multi-user production data. The recommended production architecture is:

- Web/API service on Render.
- Authentication and PostgreSQL on Supabase or managed Postgres.
- Audio storage on Cloudflare R2 or S3.
- Faster-Whisper GPU worker on RunPod.
- Redis-compatible queue between the API and worker.

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | dynamic | Cloud web-service port |
| `MP3_TO_TEXT_HOST` | `127.0.0.1` | Bind address |
| `MP3_TO_TEXT_NO_BROWSER` | `0` | Disable automatic browser opening |
| `MP3_TO_TEXT_DATA_DIR` | `./mp3_to_text_data` | Project-data directory |
| `WHISPER_LOCAL_FILES_ONLY` | `1` | Prevent or allow model downloads |
| `WHISPER_DEVICE` | `cpu` | Faster-Whisper device |
| `WHISPER_COMPUTE_TYPE` | `int8` | Faster-Whisper compute type |

## Privacy

Local desktop use processes audio on the same computer. Cloud deployment uploads audio to the server, so add authentication, access controls, retention policies, and secure storage before accepting customer files.