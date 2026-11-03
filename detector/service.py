#!/usr/bin/env python3
"""Детектор объектов (YOLOv5n ONNX, CPU) для интеграции «Свет по видео».

HTTP:
    GET  /health                     — состояние модели
    POST /detect?conf=0.35&classes=0,2&imgsz=640
         тело — JPEG/PNG-кадр
         ответ — {"objects":[{"cls":0,"name":"person","conf":0.83,"box":[x,y,w,h]}], "ms": 42.1}
         координаты box — доли кадра (0..1).

Запуск: python service.py --model models/yolov5n.onnx --host 0.0.0.0 --port 8099
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
from typing import Any

import numpy as np
import onnxruntime as ort
from aiohttp import web
from PIL import Image

COCO = [
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
]

LOGGER = logging.getLogger("lbv-detector")


def letterbox(img: Image.Image, size: int) -> tuple[np.ndarray, float, int, int]:
    """Пропорционально вписать в квадрат size, вернуть тензор NCHW и параметры обратного пересчёта."""
    w, h = img.size
    scale = min(size / w, size / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = img.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    pad_x, pad_y = (size - nw) // 2, (size - nh) // 2
    canvas.paste(resized, (pad_x, pad_y))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    tensor = np.ascontiguousarray(arr.transpose(2, 0, 1)[None, ...])
    return tensor, scale, pad_x, pad_y


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float = 0.45) -> list[int]:
    """Простое подавление пересечений (numpy)."""
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        area_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        area_r = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        iou = inter / np.maximum(1e-9, area_i + area_r - inter)
        order = rest[iou <= iou_thr]
    return keep


class Detector:
    """YOLOv5n: сессия ONNX Runtime (CPU) + разбор выходов."""

    def __init__(self, model_path: str, threads: int = 4) -> None:
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads))
        so.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            model_path, so, providers=["CPUExecutionProvider"]
        )
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.dtype = np.float16 if "float16" in (inp.type or "") else np.float32
        self.size = int(inp.shape[2] or 640)
        self.provider = self.session.get_providers()[0]
        self.lock_free = True

    def detect(
        self, raw: bytes, conf: float, classes: set[int] | None, size: int | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        t0 = time.perf_counter()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        src_w, src_h = img.size
        net_size = self.size  # модель со статическим входом — размер не меняем
        tensor, scale, pad_x, pad_y = letterbox(img, net_size)
        tensor = tensor.astype(self.dtype)
        preds = np.asarray(self.session.run(None, {self.input_name: tensor})[0], dtype=np.float32)
        t_infer = time.perf_counter()

        preds = np.asarray(preds)
        if preds.ndim == 3:
            preds = preds[0]
        if preds.shape[1] < 6:  # формат (85, 25200) — транспонируем
            preds = preds.T

        boxes_xywh = preds[:, :4]
        obj = preds[:, 4]
        cls_scores = preds[:, 5:]
        cls_idx = cls_scores.argmax(axis=1)
        cls_conf = cls_scores[np.arange(cls_scores.shape[0]), cls_idx]
        scores = obj * cls_conf

        mask = scores >= conf
        if classes:
            mask &= np.isin(cls_idx, list(classes))
        if not mask.any():
            return [], {
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "infer_ms": round((t_infer - t0) * 1000, 1),
                "raw": int(preds.shape[0]),
            }

        b = boxes_xywh[mask]
        s = scores[mask]
        c = cls_idx[mask]

        # xywh (центр) → xyxy, снимаем letterbox и масштаб
        x = (b[:, 0] - b[:, 2] / 2 - pad_x) / scale
        y = (b[:, 1] - b[:, 3] / 2 - pad_y) / scale
        w = b[:, 2] / scale
        h = b[:, 3] / scale
        xyxy = np.stack([x, y, x + w, y + h], axis=1)

        keep = nms(xyxy, s)
        objects: list[dict[str, Any]] = []
        for i in keep:
            bw = max(0.0, min(1.0, float(w[i] / src_w)))
            bh = max(0.0, min(1.0, float(h[i] / src_h)))
            bx = max(0.0, min(1.0, float(x[i] / src_w)))
            by = max(0.0, min(1.0, float(y[i] / src_h)))
            if bw <= 0.005 or bh <= 0.005:
                continue
            ci = int(c[i])
            objects.append(
                {
                    "cls": ci,
                    "name": COCO[ci] if ci < len(COCO) else str(ci),
                    "conf": round(float(s[i]), 3),
                    "box": [round(bx, 4), round(by, 4), round(bw, 4), round(bh, 4)],
                }
            )
        meta = {
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "infer_ms": round((t_infer - t0) * 1000, 1),
            "size": [src_w, src_h],
            "net": net_size,
            "raw": int(preds.shape[0]),
        }
        return objects, meta


DETECTOR: Detector | None = None


def _params(request: web.Request) -> tuple[float, set[int] | None, int | None]:
    conf = float(request.query.get("conf", 0.35))
    cls = request.query.get("classes", "0,1,2,3,5,7")
    classes = {int(x) for x in cls.split(",") if x.strip().isdigit()} or None
    size = request.query.get("imgsz")
    return conf, classes, (int(size) if size and size.isdigit() else None)


async def health(request: web.Request) -> web.Response:
    if DETECTOR is None:
        return web.json_response(
            {"ok": False, "error": "модель не загружена"}, status=503
        )
    return web.json_response(
        {
            "ok": True,
            "model": "yolov5n (COCO)",
            "provider": DETECTOR.provider,
            "net": DETECTOR.size,
            "dtype": str(DETECTOR.dtype),
            "classes": len(COCO),
            "version": "1.0.0",
        }
    )


async def detect(request: web.Request) -> web.Response:
    if DETECTOR is None:
        return web.json_response({"error": "модель не загружена"}, status=503)
    raw = await request.read()
    if len(raw) < 500:
        return web.json_response({"error": "пустой кадр"}, status=400)
    conf, classes, size = _params(request)
    try:
        objects, meta = await request.loop.run_in_executor(
            None, DETECTOR.detect, raw, conf, classes, size
        )
    except Exception as err:  # noqa: BLE001
        LOGGER.warning("detect error: %s", err)
        return web.json_response({"error": str(err)}, status=500)
    return web.json_response({"objects": objects, "count": len(objects), **meta})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="models/yolov5n.onnx")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(message)s"
    )
    global DETECTOR
    DETECTOR = Detector(args.model, args.threads)
    LOGGER.info(
        "модель загружена: %s (провайдер %s, вход %s)",
        args.model,
        DETECTOR.provider,
        DETECTOR.size,
    )

    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_post("/detect", detect)
    LOGGER.info("слушаю http://%s:%s", args.host, args.port)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
