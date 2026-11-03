#!/usr/bin/env python3
"""Синхронизация файлов проекта с GitHub (Contents API) и релиз."""

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
OWNER = "bezuglyy"
REPO = "light_by_video"
API = "https://api.github.com"
ROOT = Path("/opt/opencode/projects/light-by-video")
EXCLUDE_DIRS = {"frames", ".git", "__pycache__"}


def call(method: str, url: str, payload: dict | None = None, timeout: int = 40):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "opencode-sync",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")
    except Exception as err:  # noqa: BLE001
        return 0, str(err)


def remote_tree() -> dict[str, str]:
    status, data = call("GET", f"{API}/repos/{OWNER}/{REPO}/git/trees/main?recursive=1")
    if status != 200:
        print("tree error:", status, str(data)[:200])
        return {}
    return {x["path"]: x["sha"] for x in data.get("tree", []) if x["type"] == "blob"}


def local_files() -> list[Path]:
    out = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(p in EXCLUDE_DIRS for p in rel.parts) or path.suffix == ".pyc":
            continue
        if rel.name.endswith(".bak") or ".bak-" in rel.name:
            continue
        out.append(path)
    return out


def main() -> int:
    if not TOKEN:
        print("нет GITHUB_TOKEN")
        return 2
    remote = remote_tree()
    changed = 0
    for path in local_files():
        rel = str(path.relative_to(ROOT)).replace(os.sep, "/")
        payload = {
            "message": f"update {rel}",
            "content": base64.b64encode(path.read_bytes()).decode(),
            "branch": "main",
        }
        if rel in remote:
            payload["sha"] = remote[rel]
        status, res = call(
            "PUT",
            f"{API}/repos/{OWNER}/{REPO}/contents/{urllib.parse.quote(rel)}",
            payload,
        )
        mark = "ok " if status in (200, 201) else "ERR"
        if status in (200, 201):
            changed += 1
        print(
            f"  {mark} {rel} {'' if status in (200, 201) else str(res)[:120]}",
            flush=True,
        )
    print(f"обновлено: {changed}", flush=True)

    status, rel = call("GET", f"{API}/repos/{OWNER}/{REPO}/releases/tags/v0.2.0")
    if status == 200:
        print("релиз v0.2.0 уже есть:", rel.get("html_url"), flush=True)
        return 0

    print("создаю тег…", flush=True)
    status, ref = call("GET", f"{API}/repos/{OWNER}/{REPO}/git/ref/heads/main")
    sha = (ref or {}).get("object", {}).get("sha") if isinstance(ref, dict) else None
    if sha:
        call(
            "POST",
            f"{API}/repos/{OWNER}/{REPO}/git/refs",
            {"ref": "refs/tags/v0.2.0", "sha": sha},
        )

    body = (
        "## Свет по видео 0.2.0\n\n"
        "Home Assistant-интеграция: движение в **выбранной на кадре области** включает устройство "
        "(свет/реле/прожектор) на заданное время — с продлением, лимитом и паузой.\n\n"
        "### Возможности\n"
        "- зоны мышью на кадре: 4 угла, произвольный размер, точные X/Y/Ш/В, зоны-исключения;\n"
        "- детекция: разность кадров (EMA-фон, день/ИК, защита от смены света) **или объекты** "
        "(YOLOv5n ONNX на CPU-сервисе);\n"
        "- действия: держать N минут, продление, максимум, пауза, «не гасить, если горело», ручной карантин;\n"
        "- условия: **только после заката**, окно времени, дни недели;\n"
        "- панель: зоны, действия, детектор, источник камеры, журнал;\n"
        "- таймеры переживают перезапуск HA, сторож RTSP, откат на разность кадров при недоступности детектора.\n\n"
        "### Установка\n"
        "1. Скопировать `custom_components/light_by_video` в `/config/custom_components/` "
        "(или HACS → custom repository).\n"
        "2. Перезапустить HA, добавить интеграцию «Свет по видео», указать камеру/RTSP.\n"
        "3. Нарисовать область в панели и выбрать устройство.\n\n"
        "Документация — `docs/ОПИСАНИЕ-ПРОЕКТА.md` (PDF), `docs/FAQ.md` (PDF/XLSX); "
        "детектор объектов — `detector/README.md`.\n"
    )
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
        timeout=60,
    )
    print(
        "release:",
        status,
        (rel or {}).get("html_url") if isinstance(rel, dict) else rel,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
