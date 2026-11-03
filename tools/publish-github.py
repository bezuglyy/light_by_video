#!/usr/bin/env python3
"""Публикация проекта «Свет по видео» на GitHub (Contents API, git push не используется)."""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
OWNER = os.environ.get("GH_OWNER", "bezuglyy")
REPO = "light_by_video"
API = "https://api.github.com"
ROOT = Path("/opt/opencode/projects/light-by-video")

EXCLUDE_DIRS = {"frames", ".git", "__pycache__"}
EXCLUDE_NAMES = {"MD5SUMS.txt"}  # заливаем отдельно после генерации

DESCRIPTION = (
    "Home Assistant: свет по видео — движение в выбранной области камеры включает устройство на время "
    "(зоны, закат, ONNX-детектор объектов, панель)"
)
TOPICS = [
    "home-assistant",
    "hacs",
    "integration",
    "camera",
    "motion-detection",
    "zone-detection",
    "onnx",
    "yolov5",
    "object-detection",
    "russian",
    "smart-home",
]


def call(
    method: str,
    url: str,
    payload: dict | None = None,
    raw: bytes | None = None,
    ctype: str | None = None,
):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "opencode-publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if raw is not None:
        data = raw
        headers["Content-Type"] = ctype or "application/octet-stream"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
        return err.code, {"error": body}


def files_to_upload() -> list[Path]:
    out: list[Path] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.name in EXCLUDE_NAMES or rel.suffix == ".pyc":
            continue
        if rel.name.endswith(".bak") or ".bak-" in rel.name:
            continue
        out.append(path)
    return out


def main() -> int:
    if not TOKEN:
        print("нет GITHUB_TOKEN")
        return 2

    status, me = call("GET", f"{API}/user")
    print("token:", status, me.get("login"))

    status, repo = call(
        "POST",
        f"{API}/user/repos",
        {
            "name": REPO,
            "description": DESCRIPTION,
            "private": False,
            "has_issues": True,
            "has_wiki": False,
            "has_projects": False,
            "auto_init": False,
        },
    )
    if status == 422:
        print("репозиторий уже существует — обновляю файлы")
    elif status >= 300:
        print("не создан:", status, repo)
        return 1
    else:
        print("создан:", repo.get("html_url"))

    files = files_to_upload()
    uploaded = 0
    for path in files:
        rel = str(path.relative_to(ROOT)).replace(os.sep, "/")
        content = base64.b64encode(path.read_bytes()).decode()
        status, res = call(
            "PUT",
            f"{API}/repos/{OWNER}/{REPO}/contents/{urllib.parse.quote(rel)}",
            {"message": f"add {rel}", "content": content, "branch": "main"},
        )
        if status in (200, 201):
            uploaded += 1
            print(f"  ok  {rel}")
        else:
            print(f"  ERR {rel}: {status} {str(res)[:160]}")
    print(f"загружено файлов: {uploaded}/{len(files)}")

    status, _ = call("PUT", f"{API}/repos/{OWNER}/{REPO}/topics", {"names": TOPICS})
    print("topics:", status)

    body = """## Свет по видео 0.2.0

Home Assistant-интеграция: движение в **выбранной на кадре области** включает устройство (свет/реле/прожектор)
на заданное время, с продлением, лимитом и паузой.

### Возможности
- зоны мышью на кадре: 4 угла, произвольный размер, точные X/Y/Ш/В, зоны-исключения;
- детекция: **разность кадров** (EMA-фон, день/ИК, защита от смены света) или **объекты** (YOLOv5n ONNX, CPU-сервис);
- действия: держать N минут, продление по движению, максимум, пауза, «не гасить, если горело», ручной карантин;
- условия: **только после заката** (`sun.sun`), окно времени, дни недели;
- панель: зоны, действия, детектор, источник камеры, журнал;
- устойчивость: таймеры переживают перезапуск HA, сторож RTSP, откат на разность кадров при недоступности детектора.

### Установка
1. Скопировать `custom_components/light_by_video` в `/config/custom_components/` (или HACS → custom repository).
2. Перезапустить HA, добавить интеграцию «Свет по видео», указать камеру/RTSP.
3. Нарисовать область в панели и выбрать устройство.

Документация: `docs/ОПИСАНИЕ-ПРОЕКТА.md` (PDF), `docs/FAQ.md` (PDF/XLSX).
Детектор объектов: `detector/README.md`.
"""
    status, rel = call(
        "POST",
        f"{API}/repos/{OWNER}/{REPO}/releases",
        {
            "tag_name": "v0.2.0",
            "target_commitish": "main",
            "name": "Свет по видео 0.2.0",
            "body": body,
            "draft": False,
            "prerelease": False,
        },
    )
    print("release:", status, rel.get("html_url"))

    if status in (200, 201) and rel.get("upload_url"):
        upload_url = rel["upload_url"].split("{")[0]
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(
                (ROOT / "custom_components" / "light_by_video").rglob("*")
            ):
                if path.is_file() and "__pycache__" not in path.parts:
                    zf.write(path, str(path.relative_to(ROOT)))
        data = buf.getvalue()
        url = f"{upload_url}?name=light_by_video-0.2.0.zip"
        status, asset = call("POST", url, raw=data, ctype="application/zip")
        print("asset:", status, asset.get("browser_download_url"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
