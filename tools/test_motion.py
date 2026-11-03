#!/usr/bin/env python3
"""Офлайн-тесты детектора движения (без Home Assistant).

Запуск:  python3 tools/test_motion.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent.parent / "custom_components" / "light_by_video"


def _load_pkg():
    """Загрузить пакет без импорта __init__ (он тянет homeassistant)."""
    if "light_by_video" in sys.modules:
        return sys.modules["light_by_video.motion"], sys.modules["light_by_video.const"]
    pkg = types.ModuleType("light_by_video")
    pkg.__path__ = [str(BASE)]
    sys.modules["light_by_video"] = pkg
    for name in ("const", "motion"):
        spec = importlib.util.spec_from_file_location(
            f"light_by_video.{name}", BASE / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"light_by_video.{name}"] = module
        spec.loader.exec_module(module)
        setattr(pkg, name, module)
    return sys.modules["light_by_video.motion"], sys.modules["light_by_video.const"]


motion, const = _load_pkg()

W, H = 640, 360
ZONES = [
    {
        "id": "z1",
        "name": "Вход",
        "role": "motion",
        "x": 0.30,
        "y": 0.35,
        "w": 0.30,
        "h": 0.45,
        "sensitivity": 1.0,
    },
    {
        "id": "z2",
        "name": "Справа",
        "role": "motion",
        "x": 0.75,
        "y": 0.05,
        "w": 0.20,
        "h": 0.40,
        "sensitivity": 1.0,
    },
    {
        "id": "z3",
        "name": "Игнор",
        "role": "ignore",
        "x": 0.0,
        "y": 0.0,
        "w": 0.10,
        "h": 0.10,
    },
]


def frame(person: tuple[int, int] | None = None, brightness: int = 0) -> Image.Image:
    """Синтетический кадр: серый двор + необязательный «человек» + сдвиг яркости."""
    rng = np.random.default_rng(42)
    base = np.full((H, W, 3), 95, dtype=np.int16)
    base += rng.integers(-3, 4, size=(H, W, 1))
    base[:, :, 1] += 4
    img = Image.fromarray(
        np.clip(base + brightness, 0, 255).astype(np.uint8), mode="RGB"
    )
    if person is not None:
        draw = ImageDraw.Draw(img)
        x, y = person
        draw.rectangle([x, y, x + 55, y + 120], fill=(180, 160, 150))
    return img


def analyze(img: Image.Image, bg=None, detector=None):
    gray, sat, _ = motion.prepare(img, 480)
    result = motion.analyze(
        gray, bg, detector or const.DEFAULT_DETECTOR, ZONES, sat=sat
    )
    return result, result.pop("background", None)


failures: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    # 1. первый кадр — фон
    r0, bg = analyze(frame())
    check("первый кадр набирает фон", r0["initialized"] is False and bg is not None)

    # 2. фон без движения — движения нет
    r1, bg1 = analyze(frame(), bg)
    zones = {z["id"]: z for z in r1["zones"]}
    check(
        "пустая зона — нет движения",
        not zones["z1"]["hit"],
        f"ratio={zones['z1']['ratio']}",
    )
    check("смена света не распознана как движение", r1["lighting"] is False)

    # 3. человек в зоне — движение
    r2, bg2 = analyze(frame((230, 170)), bg1)
    zones2 = {z["id"]: z for z in r2["zones"]}
    check(
        "человек в зоне — движение",
        zones2["z1"]["hit"],
        f"ratio={zones2['z1']['ratio']}, px={zones2['z1']['changed_px']}",
    )
    check(
        "соседняя зона молчит",
        not zones2["z2"]["hit"],
        f"ratio={zones2['z2']['ratio']}",
    )

    # 4. человек вне зоны — движения нет
    r3, bg3 = analyze(frame((40, 10)), bg2)
    zones3 = {z["id"]: z for z in r3["zones"]}
    check(
        "человек вне зоны — нет движения",
        not zones3["z1"]["hit"] and not zones3["z2"]["hit"],
    )

    # 5. человек в правой зоне
    r4, bg4 = analyze(frame((520, 60)), bg3)
    zones4 = {z["id"]: z for z in r4["zones"]}
    check(
        "человек в правой зоне — движение",
        zones4["z2"]["hit"],
        f"ratio={zones4['z2']['ratio']}",
    )

    # 6. глобальная смена освещения (включили свет) — не движение
    r5, bg5 = analyze(frame(brightness=45), bg4)
    check(
        "смена освещения помечена как lighting",
        r5["lighting"] is True,
        f"global={r5['global_ratio']}",
    )
    check("при смене света зоны не активны", all(not z["hit"] for z in r5["zones"]))

    # 7. игнор-зона исключается из маски
    r6, bg6 = analyze(frame((5, 5)), bg5)
    check("игнор-зона не даёт движения", not any(z["hit"] for z in r6["zones"]))

    # 8. маска рисуется
    gray, sat, _ = motion.prepare(frame((230, 170)), 480)
    png = motion.mask_png(gray, bg6, const.DEFAULT_DETECTOR, ZONES, sat)
    check("маска отдаётся PNG", png[:8] == b"\x89PNG\r\n\x1a\n", f"{len(png)} байт")

    # 9. чувствительность влияет на порог
    det_low = {**const.DEFAULT_DETECTOR, "min_ratio": 0.30, "min_px": 10}
    r7, _ = analyze(frame((230, 170)), bg6, det_low)
    check("высокий порог отсекает движение", not any(z["hit"] for z in r7["zones"]))

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — " + ", ".join(failures))
        return 1
    print("Все проверки пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
