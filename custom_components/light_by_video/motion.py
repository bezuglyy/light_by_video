"""Детектор движения по области кадра (без зависимости от Home Assistant).

Идея: держим медленно адаптирующийся фон кадра и считаем, какая доля пикселей
зоны от него отличается. Движение — это локальное изменение, смена освещения —
глобальное; их различаем по доле изменённых пикселей всего кадра.

Функции модуля чистые: им передают кадр/фон/настройки, они возвращают метрики.
Временная логика (подтверждение, тишина, продление) — в координаторе.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .const import DEFAULT_DETECTOR, ROLE_IGNORE, ROLE_MOTION

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def open_frame(raw: bytes) -> Image.Image:
    """Байты кадра -> RGB-изображение."""
    return Image.open(io.BytesIO(raw)).convert("RGB")


def prepare(img: Image.Image | np.ndarray, width: int) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Кадр (PIL или RGB-массив) -> (серое float32, насыщенность 0..1, (ш, в))."""
    rgb = Image.fromarray(np.asarray(img, dtype=np.uint8), mode="RGB") if isinstance(img, np.ndarray) else img
    w, h = rgb.size
    if width and w > int(width):
        nh = max(1, int(round(h * int(width) / w)))
        rgb = rgb.resize((int(width), nh), Image.BILINEAR)
    arr = np.asarray(rgb, dtype=np.float32)
    mx = arr.max(2)
    mn = arr.min(2)
    sat = float(np.mean((mx - mn) / 255.0)) if arr.size else 0.0
    gray = arr @ _LUMA
    return gray.astype(np.float32), sat, (rgb.size[0], rgb.size[1])


def blur(gray: np.ndarray, radius: int) -> np.ndarray:
    """Сглаживание серого кадра (шум ИК, артефакты JPEG)."""
    radius = int(radius or 0)
    if radius <= 0:
        return gray
    img = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8))
    img = img.filter(ImageFilter.GaussianBlur(radius=radius))
    return np.asarray(img, dtype=np.float32)


def zone_box(zone: dict[str, Any], width: int, height: int) -> tuple[int, int, int, int]:
    """Границы зоны (доли кадра) в пикселях."""
    x0 = max(0, min(width - 1, int(round(float(zone["x"]) * width))))
    y0 = max(0, min(height - 1, int(round(float(zone["y"]) * height))))
    x1 = max(x0 + 1, min(width, int(round((float(zone["x"]) + float(zone["w"])) * width))))
    y1 = max(y0 + 1, min(height, int(round((float(zone["y"]) + float(zone["h"])) * height))))
    return x0, y0, x1, y1


def polygon_mask(points: list[list[float]], width: int, height: int) -> np.ndarray:
    """Логическая маска многоугольника (вершины — доли кадра)."""
    from PIL import ImageDraw

    img = Image.new("1", (width, height), 0)
    xy = [(float(pt[0]) * width, float(pt[1]) * height) for pt in points if len(pt) >= 2]
    if len(xy) >= 3:
        ImageDraw.Draw(img).polygon(xy, fill=1)
    return np.asarray(img, dtype=bool)


def zone_mask(zone: dict[str, Any], width: int, height: int) -> np.ndarray:
    """Маска зоны: многоугольник (если заданы points), иначе прямоугольник."""
    points = zone.get("points")
    if points:
        return polygon_mask(points, width, height)
    x0, y0, x1, y1 = zone_box(zone, width, height)
    out = np.zeros((height, width), dtype=bool)
    out[y0:y1, x0:x1] = True
    return out


def _cleanup(mask: np.ndarray) -> np.ndarray:
    """Убрать одиночные пиксели и залить мелкие дырки."""
    if not mask.size:
        return mask
    img = Image.fromarray((mask.astype(np.uint8) * 255))
    img = img.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    return np.asarray(img) > 127


def _bbox(mask: np.ndarray) -> list[int] | None:
    """Ограничивающий прямоугольник изменений в пикселях зоны."""
    if not mask.any():
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    return [int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])]


def _ignore_mask(zones: list[dict[str, Any]], width: int, height: int) -> np.ndarray | None:
    """Общая маска областей-исключений (True = игнорировать)."""
    ignore = [z for z in zones if z.get("role") == ROLE_IGNORE]
    if not ignore:
        return None
    mask = np.zeros((height, width), dtype=bool)
    for zone in ignore:
        mask |= zone_mask(zone, width, height)
    return mask


def analyze(
    gray: np.ndarray,
    background: np.ndarray | None,
    detector: dict[str, Any],
    zones: list[dict[str, Any]],
    *,
    sat: float = 1.0,
) -> dict[str, Any]:
    """Сравнить кадр с фоном и вернуть метрики по зонам.

    ``background=None`` — фон ещё не набран: возвращаем кадр как новый фон.
    """
    cfg = {**DEFAULT_DETECTOR, **(detector or {})}
    height, width = gray.shape
    ir = sat < float(cfg.get("gray_sat", 0.02))
    diff_t = float(cfg.get("diff_t_ir" if ir else "diff_t", 20.0))

    result: dict[str, Any] = {
        "frame": f"{width}x{height}",
        "mean": round(float(gray.mean()), 1) if gray.size else 0.0,
        "sat": round(float(sat), 4),
        "ir": bool(ir),
        "diff_t": diff_t,
        "global_ratio": 0.0,
        "lighting": False,
        "initialized": background is not None,
        "zones": [],
        "background": None,
    }

    if float(result["mean"]) < float(cfg.get("frame_min_mean", 15.0)):
        result["reason"] = "кадр почти чёрный — источник или свет недоступны"
        result["background"] = background
        return result

    if background is None or getattr(background, "shape", None) != gray.shape:
        result["background"] = gray.copy()
        result["reason"] = "фон набран"
        return result

    delta = np.abs(gray - background)
    mask = delta > diff_t
    ignore = _ignore_mask(zones, width, height)
    if ignore is not None:
        mask &= ~ignore

    total = int(mask.size) or 1
    global_ratio = float(mask.sum()) / total
    result["global_ratio"] = round(global_ratio, 4)
    lighting = global_ratio >= float(cfg.get("global_change_ratio", 0.55))
    result["lighting"] = bool(lighting)

    if not lighting:
        mask = _cleanup(mask)
        for zone in zones:
            if zone.get("role") == ROLE_IGNORE:
                continue
            if zone.get("points"):
                area = zone_mask(zone, width, height)
                px = int(area.sum()) or 1
                patch = mask & area
                changed = int(patch.sum())
            else:
                x0, y0, x1, y1 = zone_box(zone, width, height)
                patch = mask[y0:y1, x0:x1]
                px = int(patch.size) or 1
                changed = int(patch.sum())
            ratio = changed / px
            sensitivity = max(0.2, min(5.0, float(zone.get("sensitivity") or 1.0)))
            need = max(float(cfg.get("min_ratio", 0.006)) / sensitivity, 1.0 / px)
            result["zones"].append(
                {
                    "id": zone.get("id"),
                    "name": zone.get("name"),
                    "role": zone.get("role", ROLE_MOTION),
                    "ratio": round(ratio, 4),
                    "need": round(need, 4),
                    "changed_px": changed,
                    "px": px,
                    "min_px": int(cfg.get("min_px", 60)),
                    "bbox": _bbox(patch),
                    "hit": bool(ratio >= need and changed >= int(cfg.get("min_px", 60))),
                }
            )
    else:
        # смена освещения: состояние зон не трогаем, но фон подтягиваем быстро
        for zone in zones:
            if zone.get("role") == ROLE_IGNORE:
                continue
            result["zones"].append(
                {
                    "id": zone.get("id"),
                    "name": zone.get("name"),
                    "role": zone.get("role", ROLE_MOTION),
                    "ratio": 0.0,
                    "need": float(cfg.get("min_ratio", 0.006)),
                    "changed_px": 0,
                    "px": 0,
                    "min_px": int(cfg.get("min_px", 60)),
                    "bbox": None,
                    "hit": False,
                }
            )

    alpha = float(cfg.get("bg_alpha", 0.03))
    if lighting:
        alpha = min(0.5, alpha * 5)
    result["background"] = (background * (1.0 - alpha) + gray * alpha).astype(np.float32)
    return result


def mask_png(gray: np.ndarray, background: np.ndarray | None, detector: dict[str, Any], zones: list[dict[str, Any]], sat: float) -> bytes:
    """PNG маски движения (для отладки в панели): белое — изменение, красный — зона."""
    cfg = {**DEFAULT_DETECTOR, **(detector or {})}
    height, width = gray.shape
    if background is None or getattr(background, "shape", None) != gray.shape:
        img = Image.new("RGB", (width, height), (24, 26, 30))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    ir = sat < float(cfg.get("gray_sat", 0.02))
    diff_t = float(cfg.get("diff_t_ir" if ir else "diff_t", 20.0))
    mask = _cleanup(np.abs(gray - background) > diff_t)
    ignore = _ignore_mask(zones, width, height)
    if ignore is not None:
        mask &= ~ignore

    canvas = np.stack([mask.astype(np.uint8) * 255] * 3, axis=2)
    img = Image.fromarray(canvas.astype(np.uint8), mode="RGB")
    draw = ImageDraw.Draw(img)
    for zone in zones:
        if zone.get("role") == ROLE_IGNORE:
            continue
        if zone.get("points"):
            poly = [(float(pt[0]) * width, float(pt[1]) * height) for pt in zone["points"]]
            if len(poly) >= 3:
                draw.line(poly + [poly[0]], fill=(255, 60, 60), width=2)
            continue
        x0, y0, x1, y1 = zone_box(zone, width, height)
        draw.rectangle([x0, y0, max(x0 + 1, x1 - 1), max(y0 + 1, y1 - 1)], outline=(255, 60, 60), width=2)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
