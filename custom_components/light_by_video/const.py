"""Константы интеграции light_by_video («Свет по видео»)."""

from __future__ import annotations

DOMAIN = "light_by_video"
NAME = "Свет по видео"
MANUFACTURER = "techlan.su"
VERSION = "0.11.0"

# --- ключи настроек (config entry data/options) ---
CONF_CAMERA_ENTITY = "camera_entity"
CONF_SNAPSHOT_URL = "snapshot_url"
CONF_GO2RTC_BASE = "go2rtc_base"
CONF_SOURCE_USER = "source_user"
CONF_SOURCE_PASS = "source_pass"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DETECTOR = "detector"
CONF_ZONES = "zones"
CONF_ENABLED = "enabled"
CONF_NOTIFY = "notify"
CONF_MUTE_UNTIL = "mute_until"  # режим «не беспокоить» до указанного времени (ISO)
CONF_INTERLOCKS = "interlocks"  # запреты по сенсорам
CONF_SUN_ENTITY = "sun_entity"  # сенсор «темно» (по умолчанию sun.sun)
CONF_SUN_OFFSET = "sun_offset_min"  # поправка к закату, минуты (минус — раньше)
CONF_CALIBRATION = "calibration"  # данные калибровки масштаба (эталонный объект на кадре)
CONF_SCHEDULES = "schedules"  # расписания принудительного вкл/выкл
CONF_CATCHUP_MIN = "catchup_min"  # догон пропущенного после простоя, минуты

DEFAULT_GO2RTC_BASE = "http://127.0.0.1:1984"
DEFAULT_SCAN_INTERVAL = 1.5
MIN_SCAN_INTERVAL = 1.0
MAX_SCAN_INTERVAL = 30.0

# --- детектор ---
DEFAULT_DETECTOR: dict[str, float | int | str] = {
    "width": 480,  # до какой ширины ужимаем кадр
    "blur": 1,  # радиус сглаживания (0..3), применяется к кадру и фону
    "bg_alpha": 0.03,  # скорость адаптации фона
    "diff_t": 26.0,  # порог разницы яркости, цветной кадр
    "diff_t_ir": 14.0,  # порог для ч/б (ИК)
    "min_ratio": 0.015,  # доля изменённых пикселей зоны (подобрано по реальному потоку)
    "min_px": 150,  # абсолютный минимум пикселей
    "confirm_frames": 2,  # из скольких кадров подтверждаем движение
    "confirm_window": 3,
    "quiet_frames": 4,  # сколько тихих кадров = «движение закончилось»
    "global_change_ratio": 0.55,  # >55 % кадра изменилось — это смена освещения
    "night_sensitivity": 1.4,  # множитель чувствительности в ИК
    "mode": "frame",  # frame — разность кадров | objects — детектор объектов (ONNX-сервис)
    "objects_url": "http://192.168.103.202:8099/detect",
    "objects_conf": 0.35,  # порог уверенности детектора
    "objects_classes": "0,1,2,3,5,7",  # COCO: person, bicycle, car, motorcycle, bus, truck
    "objects_cache_ms": 700,
    "objects_require_motion": True,  # объект засчитывается только вместе с движением в зоне
    "objects_only_on_motion": True,  # инференс только когда в кадре есть движение (экономия CPU)
    "objects_idle_cache_ms": 3000,  # как долго держим прошлый результат без движения
    "objects_tiling": 1,  # 1 — весь кадр, 2 — 2×2 фрагмента, 3 — 3×3 (дальние объекты)
    "objects_width": 0,  # ширина кадра для детектора (0 — как у источника)
    "objects_min_h": 0.0,  # мин. высота объекта в долях кадра (отсечь мелочь)  # не чаще одного запроса к детектору
    "objects_timeout": 4.0,
    "gray_sat": 0.02,  # ниже — ч/б кадр
    "frame_min_mean": 15.0,  # ниже — кадр почти чёрный
}

ZONE_ROLES = ("motion", "ignore")
# Условие по солнцу: any — всегда, night — только после заката, day — только до заката
SUN_MODES = ("any", "night", "day")
ROLE_MOTION = "motion"
ROLE_IGNORE = "ignore"

DAYS_ALL = [0, 1, 2, 3, 4, 5, 6]  # 0 = понедельник
MAX_ZONES = 20
ZONE_MIN_POINTS = 3  # многоугольник: минимум вершин
ZONE_MAX_POINTS = 24
HOLD_MIN = 5  # секунд
HOLD_MAX = 86400
MAX_ON_MIN = 5
MANUAL_HOLD_SEC = 600  # не трогаем устройство после ручного вмешательства
MIN_ACTION_INTERVAL = 5.0  # сек между командами

DEFAULT_ZONE: dict = {
    "id": "z1",
    "name": "Зона 1",
    "role": ROLE_MOTION,
    "x": 0.25,
    "y": 0.25,
    "w": 0.35,
    "h": 0.45,
    "enabled": True,
    "sensitivity": 1.0,
    "target_entity": "",
    "hold_sec": 300,
    "cooldown_sec": 0,  # пауза между включениями после выключения
    "extend": True,
    "max_on_sec": 1800,
    "restore": False,
    "sun": "any",
    "lux_sensor": "",
    "lux_below": 0.0,
    "time_from": "",
    "time_to": "",
    "days": [0, 1, 2, 3, 4, 5, 6],
    "entity": True,
}

# --- события ---
EVENT_DETECTED = "detected"
EVENT_CLEARED = "cleared"
EVENT_ACTION_ON = "action_on"
EVENT_ACTION_OFF = "action_off"
EVENT_SKIPPED = "skipped"
EVENT_CAMERA_LOST = "camera_lost"
EVENT_CAMERA_BACK = "camera_back"
EVENT_LIGHTING = "lighting"
EVENT_SCHEDULE = "schedule"
EVENT_BUS = f"{DOMAIN}_event"

EVENT_TITLES = {
    EVENT_DETECTED: "Движение в зоне",
    EVENT_CLEARED: "Движение закончилось",
    EVENT_ACTION_ON: "Устройство включено",
    EVENT_ACTION_OFF: "Устройство выключено",
    EVENT_SKIPPED: "Действие пропущено",
    EVENT_CAMERA_LOST: "Источник кадра недоступен",
    EVENT_CAMERA_BACK: "Источник кадра снова доступен",
    EVENT_LIGHTING: "Смена освещения (не движение)",
    EVENT_SCHEDULE: "Сработало расписание",
}

# --- запреты по сенсорам ---
INTERLOCK_OPS = {
    "is_on": "включён / открыт",
    "is_off": "выключен / закрыт",
    "above": "больше",
    "below": "меньше",
    "equal": "равно",
    "not_equal": "не равно",
}
INTERLOCK_MODES = {"block": "запрещать", "warn": "только предупреждать"}

# состояния, которые считаем «включено» / «выключено» в запретах
ISH_ON_STATES = {
    "on", "true", "1", "yes", "home", "open", "opened", "включено", "открыто", "тревога",
}
IS_OFF_STATES = {
    "off", "false", "0", "no", "not_home", "closed", "выключено", "закрыто", "норма",
}
MAX_INTERLOCKS = 20

# предустановки детектора (кнопки в панели)
DETECTOR_PRESETS: dict[str, dict] = {
    "street": {
        "mode": "objects",
        "objects_conf": 0.25,
        "objects_tiling": 2,
        "objects_require_motion": True,
        "objects_only_on_motion": True,
        "min_ratio": 0.012,
        "diff_t": 24.0,
    },
    "yard": {
        "mode": "objects",
        "objects_conf": 0.3,
        "objects_tiling": 1,
        "objects_require_motion": True,
        "objects_only_on_motion": True,
        "min_ratio": 0.015,
        "diff_t": 26.0,
    },
    "room": {
        "mode": "frame",
        "objects_tiling": 1,
        "min_ratio": 0.02,
        "diff_t": 28.0,
        "blur": 1,
    },
}

# --- калибровка масштаба (по эталонному объекту на кадре) ---
DEFAULT_CALIBRATION: dict = {
    "object_name": "",
    "object_m": 0.0,      # реальный размер эталона, м
    "object_px": 0.0,     # его размер на кадре, px
    "plate_m": 0.520,     # второй эталон (номерной знак РФ), м
    "plate_px": 0.0,      # он же на кадре, px
    "px_per_m": 0.0,      # итоговый масштаб
    "person_m": 1.70,     # средний рост человека
    "frame_w": 1920,      # ширина кадра, на котором мерили
    "frame_h": 1080,
    "measured_at": "",
    "note": "",
}

# --- расписания ---
SCHEDULE_ACTIONS = ("on", "off")
# расписание — это ПЕРИОД, в котором разрешены действия (движение включает свет)
SCHEDULE_TRIGGERS = ("time", "sunset", "sunrise")
SCHEDULE_TRIGGER_TITLES = {
    "time": "по времени",
    "sunset": "с заката до рассвета",
    "sunrise": "с рассвета до заката",
}
MAX_SCHEDULES = 20
DEFAULT_CATCHUP_MIN = 360  # 6 часов, как в «Времени света»
DEFAULT_SCHEDULE: dict = {
    "id": "s1",
    "name": "Ночной период",
    "enabled": True,
    "trigger": "time",
    "time_from": "19:00",
    "time_to": "07:00",
    "offset_min": 0,
    "offset_end_min": 0,
    "days": [0, 1, 2, 3, 4, 5, 6],
    "zone_id": "",  # пусто — все зоны
    "seconds": 0,  # 0 — держать как задано в зоне
    "last_start": "",
    "last_end": "",
}

MAX_EVENT_LOG = 200
FETCH_RETRIES = 3
FETCH_TIMEOUT = 12
UNKNOWN_GRACE = 6

# --- HTTP/панель ---
URL_STATIC = f"/{DOMAIN}_static"
URL_FRAME = f"/api/{DOMAIN}/frame"
URL_MASK = f"/api/{DOMAIN}/mask"
URL_STATE = f"/api/{DOMAIN}/state"
URL_SETTINGS = f"/api/{DOMAIN}/settings"
URL_ZONE = f"/api/{DOMAIN}/zone"
URL_TEST = f"/api/{DOMAIN}/test"
URL_EVENTS = f"/api/{DOMAIN}/events"
PANEL_URL = "light-by-video"
PANEL_TITLE = "Свет по видео"
PANEL_ICON = "mdi:lightbulb-on-outline"

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.state"
