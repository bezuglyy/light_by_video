# Детектор объектов (CPU, ONNX) для «Свет по видео»

Отдельный лёгкий сервис: принимает JPEG-кадр, возвращает bbox объектов (COCO: person, car, truck, bus, …).
Живёт на **ARM (192.168.103.202:8099)**, потому что в контейнер HA (Alpine/musl, Python 3.14) `onnxruntime`
не устанавливается — нет musllinux-колёс.

## Состав

| Файл | Что |
|:--|:--|
| `service.py` | HTTP-сервис (aiohttp + onnxruntime): `GET /health`, `POST /detect?conf=0.35&classes=0,1,2,3,5,7` |
| `lbv-detector.service` | systemd-юнит (автозапуск, Restart=always, 4 потока ORT) |
| модель | `/opt/light-by-video-detector/models/yolov5n_fp32.onnx` (YOLOv5n, COCO-80, вход 640×640, FP32) |

## Установка (ARM)

```bash
mkdir -p /opt/light-by-video-detector/models && cd /opt/light-by-video-detector
python3 -m venv venv && ./venv/bin/pip install onnxruntime numpy pillow aiohttp onnx
# модель: yolov5n.onnx (v7.0, FP16) → конвертация в FP32 (скрипт в истории), либо готовый yolov5n_fp32.onnx
cp service.py /opt/light-by-video-detector/
cp lbv-detector.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now lbv-detector
curl -s http://127.0.0.1:8099/health
```

## Замеры (03.11.2026, Xeon Silver 4108, 4 потока)

| Кадр | infer | Комментарий |
|:--|--:|:--|
| 640×360 (субпоток камеры) | **175–290 мс** | рабочий режим (интеграция шлёт кадр субпотока) |
| 1920×1080 | 350–470 мс | дороже из-за декодирования/ресайза |

## Интеграция

В HA: «Свет по видео» → «Детектор» → режим **«объекты: человек/машина (ONNX)»**, адрес
`http://192.168.103.202:8099/detect`, порог 0.3, классы `0,1,2,3,5,7`, галочка **«Только вместе с движением»**
(иначе припаркованная в зоне машина удерживала бы свет постоянно). При недоступности сервиса интеграция
автоматически возвращается к разности кадров.
