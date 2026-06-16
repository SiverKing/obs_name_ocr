import asyncio
import base64
import copy
import hashlib
import io
import json
import logging
import os
import queue
import signal
import site
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import mss
import numpy as np


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
NAME_PATH = BASE_DIR / "name.txt"
OVERLAY_PATH = BASE_DIR / "overlay.html"
LOG_PATH = BASE_DIR / "worker.log"

DEFAULT_CONFIG: Dict[str, Any] = {
    "interval_ms": 1000,
    "host": "127.0.0.1",
    "port": 8765,
    "capture": {
        "source": "screen",
        "monitor": 1,
        "left": 0,
        "top": 0,
        "width": 1920,
        "height": 1080,
        "obs_websocket": {
            "url": "ws://127.0.0.1:4455",
            "password": "",
            "source_name": "",
            "source_uuid": "",
            "image_format": "png",
            "image_width": 0,
            "image_height": 0,
            "image_compression_quality": 80,
        },
    },
    "match": {
        "mode": "contains",
        "case_sensitive": False,
        "min_confidence": 0.5,
    },
    "ocr": {
        "use_cuda": False,
        "use_dml": False,
        "use_cls": False,
        "return_word_box": False,
        "reload_files_interval_ms": 2000,
        "log_performance": True,
        "log_performance_interval_ms": 3000,
    },
    "overlay": {
        "stroke_color": "#ff3b30",
        "color_mode": "single",
        "color_palette": [
            "#ff3b30",
            "#34c759",
            "#007aff",
            "#ffcc00",
            "#af52de",
            "#ff9500",
            "#00c7be",
            "#ff2d55",
        ],
        "line_width": 3,
        "show_label": True,
    },
    "desktop_overlay": {
        "enabled": False,
        "click_through": True,
        "hide_when_empty": True,
        "debug_border": False,
        "coordinate_mode": "capture",
        "screen_region": {
            "left": 0,
            "top": 0,
            "width": 1920,
            "height": 1080,
        },
        "topmost": True,
        "transparent_color": "#010101",
    },
}

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FORCE_EXIT_TIMER: Optional[threading.Timer] = None


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
    )


def deep_merge(defaults: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(write_if_missing: bool = True) -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        config = copy.deepcopy(DEFAULT_CONFIG)
        if write_if_missing:
            CONFIG_PATH.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            logging.info("已创建默认配置: %s", CONFIG_PATH)
        return config

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("config.json 顶层必须是 JSON 对象")
        return deep_merge(DEFAULT_CONFIG, data)
    except Exception:
        logging.exception("读取 config.json 失败，当前轮使用默认配置")
        return copy.deepcopy(DEFAULT_CONFIG)


def read_targets() -> List[str]:
    if not NAME_PATH.exists():
        return []

    try:
        targets: List[str] = []
        for raw_line in NAME_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            targets.append(line)
        return targets
    except Exception:
        logging.exception("读取 name.txt 失败，当前轮使用空目标列表")
        return []


def normalize_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def get_interval_seconds(config: Dict[str, Any]) -> float:
    try:
        interval_ms = int(config.get("interval_ms", 1000))
    except (TypeError, ValueError):
        interval_ms = 1000
    return max(100, interval_ms) / 1000.0


def resolve_capture_region(sct: mss.mss, config: Dict[str, Any]) -> Tuple[Dict[str, int], int, int]:
    capture = config.get("capture", {})
    monitors = sct.monitors
    try:
        monitor_index = int(capture.get("monitor", 1))
    except (TypeError, ValueError):
        monitor_index = 1
    if monitor_index < 0 or monitor_index >= len(monitors):
        logging.warning("monitor=%s 不存在，回退到 monitor=1", monitor_index)
        monitor_index = 1 if len(monitors) > 1 else 0

    monitor = monitors[monitor_index]

    def int_or_default(key: str, default: int) -> int:
        try:
            return int(capture.get(key, default))
        except (TypeError, ValueError):
            return default

    offset_left = int_or_default("left", 0)
    offset_top = int_or_default("top", 0)
    width = int_or_default("width", int(monitor["width"]))
    height = int_or_default("height", int(monitor["height"]))
    if width <= 0:
        width = int(monitor["width"])
    if height <= 0:
        height = int(monitor["height"])

    region = {
        "left": int(monitor["left"]) + offset_left,
        "top": int(monitor["top"]) + offset_top,
        "width": width,
        "height": height,
    }
    return region, width, height


def get_desktop_overlay_config(config: Dict[str, Any]) -> Dict[str, Any]:
    current = config.get("desktop_overlay", {})
    if not isinstance(current, dict):
        current = {}
    return deep_merge(DEFAULT_CONFIG["desktop_overlay"], current)


def rect_from_points(points: Any) -> Optional[Dict[str, float]]:
    if points is None:
        return None

    arr = np.asarray(points, dtype=float)
    if arr.size == 0:
        return None

    if arr.ndim == 1 and arr.size == 4:
        x1, y1, x2, y2 = arr.tolist()
        x = min(x1, x2)
        y = min(y1, y2)
        w = abs(x2 - x1)
        h = abs(y2 - y1)
    else:
        arr = arr.reshape(-1, 2)
        x = float(np.min(arr[:, 0]))
        y = float(np.min(arr[:, 1]))
        w = float(np.max(arr[:, 0]) - x)
        h = float(np.max(arr[:, 1]) - y)

    if w <= 0 or h <= 0:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


@dataclass
class OCRItem:
    text: str
    confidence: float
    rect: Dict[str, float]


@dataclass
class CaptureFrame:
    image: Optional[np.ndarray]
    region: Dict[str, int]
    width: int
    height: int
    source_info: Dict[str, Any]


class RapidOCREngine:
    def __init__(self) -> None:
        self._engine: Any = None
        self._engine_params: Dict[str, Any] = {}
        self._import_error_logged = False

    def _build_engine_params(self, config: Dict[str, Any]) -> Dict[str, Any]:
        ocr_config = config.get("ocr", {})
        return {
            "EngineConfig.onnxruntime.use_cuda": normalize_bool(
                ocr_config.get("use_cuda", False), False
            ),
            "EngineConfig.onnxruntime.use_dml": normalize_bool(
                ocr_config.get("use_dml", False), False
            ),
        }

    def _ensure_engine(self, config: Dict[str, Any]) -> bool:
        engine_params = self._build_engine_params(config)
        if self._engine is not None and engine_params == self._engine_params:
            return True

        old_engine = self._engine
        old_params = self._engine_params

        try:
            self._preload_onnxruntime_dlls(engine_params)
            from rapidocr import RapidOCR

            if old_engine is not None:
                logging.info("OCR 引擎配置变化，正在重新初始化 RapidOCR")

            new_engine = RapidOCR(params=engine_params)
            self._engine = new_engine
            self._engine_params = engine_params
            logging.info("RapidOCR 初始化完成")
            self._log_runtime_providers()
            return True
        except Exception:
            if old_engine is not None:
                self._engine = old_engine
                self._engine_params = old_params
                logging.exception("RapidOCR 重新初始化失败，继续使用上一个 OCR 引擎")
                return True

            if not self._import_error_logged:
                logging.exception("RapidOCR 初始化失败，将继续运行并发送空框")
                self._import_error_logged = True
            return False

    def _preload_onnxruntime_dlls(self, engine_params: Dict[str, Any]) -> None:
        if not (
            engine_params.get("EngineConfig.onnxruntime.use_cuda")
            or engine_params.get("EngineConfig.onnxruntime.use_dml")
        ):
            return

        nvidia_bin_dirs = self._add_nvidia_dll_directories()

        try:
            import onnxruntime as ort
        except Exception:
            return

        preload_dlls = getattr(ort, "preload_dlls", None)
        if not callable(preload_dlls):
            return

        try:
            logging.info("尝试预加载 ONNX Runtime CUDA/cuDNN DLL")
            preload_dlls()
            for bin_dir in nvidia_bin_dirs:
                preload_dlls(directory=str(bin_dir))
        except Exception:
            logging.exception("ONNX Runtime DLL 预加载失败，将继续尝试初始化 RapidOCR")

    def _add_nvidia_dll_directories(self) -> List[Path]:
        candidates: List[Path] = []
        for site_dir in site.getsitepackages():
            nvidia_dir = Path(site_dir) / "nvidia"
            if nvidia_dir.exists():
                candidates.extend(nvidia_dir.glob("*/bin"))

        add_dll_directory = getattr(os, "add_dll_directory", None)
        added: List[str] = []
        for bin_dir in candidates:
            if not bin_dir.is_dir():
                continue
            bin_dir_text = str(bin_dir)
            if callable(add_dll_directory):
                try:
                    add_dll_directory(bin_dir_text)
                except OSError:
                    logging.debug("无法加入 DLL 搜索路径: %s", bin_dir, exc_info=True)
            added.append(bin_dir_text)

        if added:
            old_path = os.environ.get("PATH", "")
            existing = {item.lower() for item in old_path.split(os.pathsep) if item}
            missing = [path for path in added if path.lower() not in existing]
            if missing:
                os.environ["PATH"] = os.pathsep.join(missing + [old_path])
            logging.info("已加入 NVIDIA DLL 搜索路径: %s", "; ".join(added))
        return [Path(path) for path in added]

    def _log_runtime_providers(self) -> None:
        providers: Dict[str, Any] = {}
        for name, attr_name in (
            ("det", "text_det"),
            ("cls", "text_cls"),
            ("rec", "text_rec"),
        ):
            component = getattr(self._engine, attr_name, None)
            infer_session = getattr(component, "session", None)
            ort_session = getattr(infer_session, "session", None)
            get_providers = getattr(ort_session, "get_providers", None)
            if callable(get_providers):
                providers[name] = get_providers()

        if providers:
            logging.info("RapidOCR ONNX Runtime providers: %s", providers)

    def recognize(self, image: np.ndarray, config: Dict[str, Any]) -> List[OCRItem]:
        if not self._ensure_engine(config):
            return []
        try:
            ocr_config = config.get("ocr", {})
            raw = self._engine(
                image,
                use_cls=normalize_bool(ocr_config.get("use_cls", False), False),
                return_word_box=normalize_bool(ocr_config.get("return_word_box", False), False),
            )
            return list(self._normalize(raw))
        except Exception:
            logging.exception("OCR 识别失败，当前轮发送空框")
            return []

    def _normalize(self, raw: Any) -> Iterable[OCRItem]:
        candidate = self._unwrap(raw)

        attr_items = self._from_attributes(candidate)
        if attr_items:
            yield from attr_items
            return

        if isinstance(candidate, dict):
            for item in self._from_dict_container(candidate):
                yield item
            return

        if isinstance(candidate, (list, tuple)):
            for entry in candidate:
                item = self._entry_to_item(entry)
                if item is not None:
                    yield item

    def _unwrap(self, raw: Any) -> Any:
        if isinstance(raw, tuple) and raw:
            first = raw[0]
            if isinstance(first, (list, tuple, dict)) or self._has_result_attributes(first):
                return first
        return raw

    def _has_result_attributes(self, value: Any) -> bool:
        names = {
            "boxes",
            "dt_boxes",
            "dt_polys",
            "txts",
            "texts",
            "rec_texts",
            "scores",
            "rec_scores",
        }
        return any(hasattr(value, name) for name in names)

    def _from_attributes(self, value: Any) -> List[OCRItem]:
        if not self._has_result_attributes(value):
            return []

        boxes = first_present(
            getattr(value, "boxes", None),
            getattr(value, "dt_boxes", None),
            getattr(value, "dt_polys", None),
        )
        texts = first_present(
            getattr(value, "txts", None),
            getattr(value, "texts", None),
            getattr(value, "rec_texts", None),
        )
        scores = first_present(getattr(value, "scores", None), getattr(value, "rec_scores", None))
        if boxes is None or texts is None:
            return []

        items: List[OCRItem] = []
        score_list = list(scores) if scores is not None else []
        for index, (box, text) in enumerate(zip(list(boxes), list(texts))):
            confidence = float(score_list[index]) if index < len(score_list) else 1.0
            rect = rect_from_points(box)
            if rect is None:
                continue
            items.append(OCRItem(text=str(text), confidence=confidence, rect=rect))
        return items

    def _from_dict_container(self, value: Dict[str, Any]) -> Iterable[OCRItem]:
        if "boxes" in value and ("texts" in value or "txts" in value):
            boxes = value.get("boxes")
            texts = value.get("texts", value.get("txts"))
            scores = first_present(value.get("scores"), value.get("confidences"), [])
            for index, (box, text) in enumerate(zip(boxes if boxes is not None else [], texts if texts is not None else [])):
                confidence = float(scores[index]) if index < len(scores) else 1.0
                rect = rect_from_points(box)
                if rect is not None:
                    yield OCRItem(text=str(text), confidence=confidence, rect=rect)
            return

        for key in ("results", "data", "items"):
            entries = value.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    item = self._entry_to_item(entry)
                    if item is not None:
                        yield item
                return

        item = self._entry_to_item(value)
        if item is not None:
            yield item

    def _entry_to_item(self, entry: Any) -> Optional[OCRItem]:
        if isinstance(entry, dict):
            text = first_present(entry.get("text"), entry.get("txt"), entry.get("label"))
            confidence = (
                entry.get("confidence")
                if entry.get("confidence") is not None
                else entry.get("score", entry.get("prob", 1.0))
            )
            box = first_present(
                entry.get("box")
                ,
                entry.get("bbox"),
                entry.get("points"),
                entry.get("poly"),
                entry.get("dt_box"),
            )
            if text is None or box is None:
                return None
            rect = rect_from_points(box)
            if rect is None:
                return None
            return OCRItem(text=str(text), confidence=float(confidence), rect=rect)

        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            box, text, confidence = entry[0], entry[1], entry[2]
            rect = rect_from_points(box)
            if rect is None:
                return None
            return OCRItem(text=str(text), confidence=float(confidence), rect=rect)

        return None


class OBSWebSocketScreenshotClient:
    def __init__(self) -> None:
        self._ws: Any = None
        self._url: Optional[str] = None
        self._password: str = ""
        self._rpc_version = 1
        self._request_counter = 0

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def capture(self, config: Dict[str, Any]) -> CaptureFrame:
        capture = config.get("capture", {})
        obs_config = capture.get("obs_websocket", {})
        if not isinstance(obs_config, dict):
            obs_config = {}

        url = str(obs_config.get("url") or "ws://127.0.0.1:4455")
        password = str(obs_config.get("password") or "")
        source_name = str(obs_config.get("source_name") or "")
        source_uuid = str(obs_config.get("source_uuid") or "")
        if not source_name and not source_uuid:
            source_name = await self._get_current_program_scene_name(url, password)
        source_info = await self._get_source_info(url, password, source_name, source_uuid)

        image_format = str(obs_config.get("image_format") or "png")
        width_hint = int(obs_config.get("image_width") or capture.get("width") or 0)
        height_hint = int(obs_config.get("image_height") or capture.get("height") or 0)
        quality = int(obs_config.get("image_compression_quality") or 80)

        request_data: Dict[str, Any] = {
            "imageFormat": image_format,
            "imageCompressionQuality": quality,
        }
        if source_uuid:
            request_data["sourceUuid"] = source_uuid
        else:
            request_data["sourceName"] = source_name
        if width_hint > 0:
            request_data["imageWidth"] = width_hint
        if height_hint > 0:
            request_data["imageHeight"] = height_hint

        response = await self._request(url, password, "GetSourceScreenshot", request_data)
        status = response.get("requestStatus", {})
        if not status.get("result"):
            raise RuntimeError(f"OBS GetSourceScreenshot 失败: {status}")

        image_data = response.get("responseData", {}).get("imageData", "")
        image = self._decode_image_data(image_data)
        height, width = image.shape[:2]
        region = {
            "left": int(capture.get("left", 0) or 0),
            "top": int(capture.get("top", 0) or 0),
            "width": width,
            "height": height,
        }
        return CaptureFrame(image=image, region=region, width=width, height=height, source_info=source_info)

    async def _get_source_info(self, url: str, password: str, source_name: str, source_uuid: str) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "source": "obs_websocket",
            "source_name": source_name,
            "source_uuid": source_uuid,
        }
        try:
            if source_uuid:
                response = await self._request(url, password, "GetInputSettings", {"inputUuid": source_uuid})
            else:
                response = await self._request(url, password, "GetInputSettings", {"inputName": source_name})
            status = response.get("requestStatus", {})
            if status.get("result"):
                data = response.get("responseData", {})
                info["input_kind"] = data.get("inputKind")
                info["input_settings"] = data.get("inputSettings", {})
        except Exception:
            logging.debug("读取 OBS input settings 失败，将只使用截图尺寸", exc_info=True)
        return info

    async def _get_current_program_scene_name(self, url: str, password: str) -> str:
        response = await self._request(url, password, "GetSceneList", {})
        status = response.get("requestStatus", {})
        if not status.get("result"):
            raise RuntimeError(f"OBS GetSceneList 失败: {status}")
        scene_name = response.get("responseData", {}).get("currentProgramSceneName")
        if not scene_name:
            raise RuntimeError("OBS 当前节目场景为空，请配置 source_name 或 source_uuid")
        return str(scene_name)

    async def _connect(self, url: str, password: str) -> Any:
        if self._ws is not None and self._url == url and self._password == password:
            return self._ws

        await self.close()
        import websockets

        self._ws = await websockets.connect(
            url,
            subprotocols=["obswebsocket.json"],
            max_size=None,
        )
        self._url = url
        self._password = password

        hello = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=5))
        data = hello.get("d", {})
        self._rpc_version = min(int(data.get("rpcVersion", 1)), 1)
        identify: Dict[str, Any] = {
            "rpcVersion": self._rpc_version,
            "eventSubscriptions": 0,
        }
        auth = data.get("authentication")
        if auth:
            if not password:
                raise RuntimeError("OBS WebSocket 需要密码，请填写 capture.obs_websocket.password")
            identify["authentication"] = self._build_auth(password, auth["salt"], auth["challenge"])

        await self._ws.send(json.dumps({"op": 1, "d": identify}, ensure_ascii=False))
        identified = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=5))
        if identified.get("op") != 2:
            raise RuntimeError(f"OBS WebSocket Identify 失败: {identified}")

        logging.info("OBS WebSocket 已连接: %s", url)
        return self._ws

    def _build_auth(self, password: str, salt: str, challenge: str) -> str:
        secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
        return base64.b64encode(hashlib.sha256(secret + challenge.encode("utf-8")).digest()).decode("ascii")

    async def _request(self, url: str, password: str, request_type: str, request_data: Dict[str, Any]) -> Dict[str, Any]:
        ws = await self._connect(url, password)
        self._request_counter += 1
        request_id = f"obs-name-ocr-{self._request_counter}"
        payload = {
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": request_id,
                "requestData": request_data,
            },
        }
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if msg.get("op") == 7 and msg.get("d", {}).get("requestId") == request_id:
                    return msg["d"]
        except Exception:
            await self.close()
            raise

    def _decode_image_data(self, image_data: str) -> np.ndarray:
        raw = image_data.split(",", 1)[1] if "," in image_data else image_data
        data = base64.b64decode(raw)
        try:
            import cv2

            array = np.frombuffer(data, dtype=np.uint8)
            image = cv2.imdecode(array, cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("cv2.imdecode 返回空图像")
            return image
        except Exception:
            from PIL import Image

            pil_image = Image.open(io.BytesIO(data)).convert("RGB")
            return np.asarray(pil_image)[:, :, ::-1].copy()


class DesktopOverlay:
    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._failed = False
        self._enabled = False

    def update(self, message: Dict[str, Any], config: Dict[str, Any], region: Dict[str, int]) -> None:
        overlay_config = get_desktop_overlay_config(config)
        enabled = normalize_bool(overlay_config.get("enabled", False), False)

        if not enabled or self._failed:
            self.stop()
            return

        self._ensure_thread()
        payload = {
            "message": message,
            "overlay": config.get("overlay", DEFAULT_CONFIG["overlay"]),
            "desktop_overlay": overlay_config,
            "region": {
                "left": int(region["left"]),
                "top": int(region["top"]),
                "width": int(region["width"]),
                "height": int(region["height"]),
            },
        }
        self._replace_latest(payload)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._replace_latest({"type": "stop"})
        self._thread.join(timeout=0.3)
        if self._thread.is_alive():
            logging.warning("桌面透明层线程未及时退出，将随进程退出")
        self._thread = None
        self._stop_event.clear()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="DesktopOverlay", daemon=True)
        self._thread.start()

    def _replace_latest(self, payload: Dict[str, Any]) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            pass

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            logging.exception("无法初始化 Win32 桌面透明层")
            self._failed = True
            return

        state: Dict[str, Any] = {
            "region": {"left": 0, "top": 0, "width": 1, "height": 1},
            "message": build_message(1, 1, [], DEFAULT_CONFIG),
            "overlay": DEFAULT_CONFIG["overlay"],
            "desktop_overlay": DEFAULT_CONFIG["desktop_overlay"],
            "visible": False,
        }

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        kernel32 = ctypes.windll.kernel32

        WM_DESTROY = 0x0002
        WM_PAINT = 0x000F
        WM_ERASEBKGND = 0x0014
        WM_NCHITTEST = 0x0084
        HTTRANSPARENT = -1
        WS_POPUP = 0x80000000
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOPMOST = 0x00000008
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        LWA_COLORKEY = 0x00000001
        SW_HIDE = 0
        SW_SHOWNOACTIVATE = 4
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        SWP_HIDEWINDOW = 0x0080
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        PM_REMOVE = 0x0001
        PS_SOLID = 0
        NULL_BRUSH = 5
        TRANSPARENT = 1
        DT_LEFT = 0x00000000
        DT_TOP = 0x00000000
        DT_SINGLELINE = 0x00000020

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [
                ("hdc", wintypes.HDC),
                ("fErase", wintypes.BOOL),
                ("rcPaint", RECT),
                ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32),
            ]

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", wintypes.POINT),
            ]

        WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        user32.BeginPaint.restype = wintypes.HDC
        user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = wintypes.LPARAM
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UpdateWindow.argtypes = [wintypes.HWND]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_byte, wintypes.DWORD]
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
        user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(RECT), wintypes.UINT]
        gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
        gdi32.CreatePen.restype = wintypes.HANDLE
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi32.SelectObject.restype = wintypes.HANDLE
        gdi32.GetStockObject.argtypes = [ctypes.c_int]
        gdi32.GetStockObject.restype = wintypes.HANDLE
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.Rectangle.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]

        def colorref(value: Any, fallback: str = "#ff3b30") -> int:
            text = str(value or fallback).strip()
            if text.startswith("#"):
                text = text[1:]
            if len(text) != 6:
                text = fallback[1:]
            try:
                r = int(text[0:2], 16)
                g = int(text[2:4], 16)
                b = int(text[4:6], 16)
            except ValueError:
                r, g, b = 255, 59, 48
            return r | (g << 8) | (b << 16)

        def get_boxes() -> List[Dict[str, Any]]:
            message = state["message"]
            boxes = message.get("boxes") if isinstance(message, dict) else []
            return boxes if isinstance(boxes, list) else []

        def paint(hwnd: Any) -> int:
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            try:
                region = state["region"]
                width = max(1, int(region["width"]))
                height = max(1, int(region["height"]))
                overlay_config = state["desktop_overlay"]
                bg = colorref(overlay_config.get("transparent_color"), "#010101")
                bg_brush = gdi32.CreateSolidBrush(bg)
                try:
                    full = RECT(0, 0, width, height)
                    user32.FillRect(hdc, ctypes.byref(full), bg_brush)
                finally:
                    gdi32.DeleteObject(bg_brush)

                boxes = get_boxes()
                debug_border = normalize_bool(overlay_config.get("debug_border", False), False)
                if not boxes and not debug_border:
                    return 0

                message = state["message"]
                msg_width = max(1, float(message.get("width", width)))
                msg_height = max(1, float(message.get("height", height)))
                scale_x = width / msg_width
                scale_y = height / msg_height
                overlay = state["overlay"]
                default_color = colorref(overlay.get("stroke_color"), DEFAULT_CONFIG["overlay"]["stroke_color"])
                line_width = max(1, int(overlay.get("line_width", DEFAULT_CONFIG["overlay"]["line_width"])))
                show_label = normalize_bool(overlay.get("show_label", True), True)

                pen = gdi32.CreatePen(PS_SOLID, line_width, default_color)
                old_pen = gdi32.SelectObject(hdc, pen)
                old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
                try:
                    if debug_border:
                        gdi32.Rectangle(hdc, 1, 1, width - 1, height - 1)
                    gdi32.SelectObject(hdc, old_pen)
                    gdi32.DeleteObject(pen)
                    pen = None
                    for box in boxes:
                        x = int(float(box.get("x", 0)) * scale_x)
                        y = int(float(box.get("y", 0)) * scale_y)
                        w = int(float(box.get("w", 0)) * scale_x)
                        h = int(float(box.get("h", 0)) * scale_y)
                        if w <= 0 or h <= 0:
                            continue
                        color = colorref(box.get("color"), overlay.get("stroke_color", DEFAULT_CONFIG["overlay"]["stroke_color"]))
                        box_pen = gdi32.CreatePen(PS_SOLID, line_width, color)
                        old_box_pen = gdi32.SelectObject(hdc, box_pen)
                        gdi32.Rectangle(hdc, x, y, x + w, y + h)
                        gdi32.SelectObject(hdc, old_box_pen)
                        gdi32.DeleteObject(box_pen)
                        if show_label:
                            label = str(box.get("matched") or box.get("text") or "")
                            if label:
                                label_rect = RECT(x, max(0, y - 22), x + max(120, len(label) * 18), max(22, y))
                                label_brush = gdi32.CreateSolidBrush(color)
                                try:
                                    user32.FillRect(hdc, ctypes.byref(label_rect), label_brush)
                                finally:
                                    gdi32.DeleteObject(label_brush)
                                gdi32.SetBkMode(hdc, TRANSPARENT)
                                gdi32.SetTextColor(hdc, colorref("#ffffff", "#ffffff"))
                                user32.DrawTextW(
                                    hdc,
                                    label,
                                    len(label),
                                    ctypes.byref(label_rect),
                                    DT_LEFT | DT_TOP | DT_SINGLELINE,
                                )
                finally:
                    gdi32.SelectObject(hdc, old_brush)
                    gdi32.SelectObject(hdc, old_pen)
                    if pen:
                        gdi32.DeleteObject(pen)
            finally:
                user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0

        def window_proc(hwnd: Any, msg: int, wparam: Any, lparam: Any) -> int:
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_PAINT:
                return paint(hwnd)
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wnd_proc = WNDPROC(window_proc)
        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"ObsNameOcrOverlay{threading.get_ident()}"
        wnd_class = WNDCLASSW()
        wnd_class.lpfnWndProc = wnd_proc
        wnd_class.hInstance = hinstance
        wnd_class.lpszClassName = class_name
        wnd_class.hbrBackground = gdi32.GetStockObject(NULL_BRUSH)
        atom = user32.RegisterClassW(ctypes.byref(wnd_class))
        if not atom:
            logging.error("注册 Win32 桌面透明层窗口类失败")
            return

        ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
        hwnd = user32.CreateWindowExW(
            ex_style,
            class_name,
            "OBS Name OCR Overlay",
            WS_POPUP,
            0,
            0,
            1,
            1,
            None,
            None,
            hinstance,
            None,
        )
        if not hwnd:
            logging.error("创建 Win32 桌面透明层窗口失败")
            return
        logging.info("桌面透明层窗口已创建: hwnd=%s", hwnd)

        last_log_at = 0.0

        def apply_window() -> None:
            nonlocal last_log_at
            region = state["region"]
            width = max(1, int(region["width"]))
            height = max(1, int(region["height"]))
            overlay_config = state["desktop_overlay"]
            bg = colorref(overlay_config.get("transparent_color"), "#010101")
            user32.SetLayeredWindowAttributes(hwnd, bg, 0, LWA_COLORKEY)
            topmost = normalize_bool(overlay_config.get("topmost", True), True)
            hide_when_empty = normalize_bool(
                overlay_config.get("hide_when_empty", True),
                True,
            )
            boxes = get_boxes()
            debug_border = normalize_bool(overlay_config.get("debug_border", False), False)
            if hide_when_empty and not boxes and not debug_border:
                if state["visible"]:
                    user32.ShowWindow(hwnd, SW_HIDE)
                    state["visible"] = False
                    logging.info("桌面透明层隐藏: 无命中框")
                return

            insert_after = HWND_TOPMOST if topmost else HWND_NOTOPMOST
            user32.SetWindowPos(
                hwnd,
                insert_after,
                int(region["left"]),
                int(region["top"]),
                width,
                height,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            state["visible"] = True
            user32.InvalidateRect(hwnd, None, True)
            user32.UpdateWindow(hwnd)
            now = time.monotonic()
            if now - last_log_at >= 5.0:
                logging.info(
                    "桌面透明层显示: left=%s top=%s width=%s height=%s boxes=%s debug_border=%s",
                    int(region["left"]),
                    int(region["top"]),
                    width,
                    height,
                    len(boxes),
                    debug_border,
                )
                last_log_at = now

        def drain_queue() -> bool:
            latest = None
            try:
                while True:
                    latest = self._queue.get_nowait()
            except queue.Empty:
                pass

            if latest is not None:
                if latest.get("type") == "stop":
                    return False
                state["region"] = latest["region"]
                state["message"] = latest["message"]
                state["overlay"] = latest["overlay"]
                state["desktop_overlay"] = latest["desktop_overlay"]
                apply_window()
            return True

        msg = MSG()
        try:
            while not self._stop_event.is_set():
                if not drain_queue():
                    break
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.016)
        except Exception:
            logging.exception("桌面透明层运行失败")
        finally:
            try:
                user32.ShowWindow(hwnd, SW_HIDE)
                user32.DestroyWindow(hwnd)
                user32.UnregisterClassW(class_name, hinstance)
            except Exception:
                pass


@dataclass(eq=False)
class WebSocketClient:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    lock: asyncio.Lock


class OverlayServer:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.clients: set[WebSocketClient] = set()
        self.connection_tasks: set[asyncio.Task] = set()
        self.last_message: Optional[str] = None
        self.server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.track_client, self.host, self.port)
        logging.info("HTTP/WebSocket 服务已启动: http://%s:%s/overlay.html", self.host, self.port)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

        clients = list(self.clients)
        for client in clients:
            await self.close_client(client)

        tasks = [task for task in self.connection_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
            except asyncio.TimeoutError:
                logging.warning("等待浏览器源连接任务退出超时，继续关闭 worker")

    async def broadcast(self, message: Dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        self.last_message = payload
        if not self.clients:
            return

        await asyncio.gather(
            *(self.send_text(client, payload) for client in list(self.clients)),
            return_exceptions=True,
        )

    async def track_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.connection_tasks.add(task)
        try:
            await self.handle_client(reader, writer)
        except asyncio.CancelledError:
            raise
        finally:
            if task is not None:
                self.connection_tasks.discard(task)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            await self.wait_writer_closed(writer)
            return

        try:
            method, path, headers = self.parse_request(request)
            if headers.get("upgrade", "").lower() == "websocket" and path == "/ws":
                await self.accept_websocket(reader, writer, headers)
                return
            await self.handle_http(method, path, writer)
        except Exception:
            logging.exception("处理客户端请求失败")
            await self.safe_write_http(writer, 500, b"Internal Server Error", "text/plain; charset=utf-8")
        finally:
            if not writer.is_closing():
                writer.close()
                try:
                    await self.wait_writer_closed(writer)
                except Exception:
                    pass

    def parse_request(self, request: bytes) -> Tuple[str, str, Dict[str, str]]:
        text = request.decode("iso-8859-1", errors="replace")
        lines = text.split("\r\n")
        parts = lines[0].split()
        if len(parts) < 2:
            raise ValueError("HTTP 请求行无效")
        method = parts[0].upper()
        path = parts[1].split("?", 1)[0]
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return method, path, headers

    async def handle_http(self, method: str, path: str, writer: asyncio.StreamWriter) -> None:
        if method not in {"GET", "HEAD"}:
            await self.safe_write_http(writer, 405, b"Method Not Allowed", "text/plain; charset=utf-8")
            return

        if path == "/":
            await self.write_redirect(writer, "/overlay.html")
            return

        if path == "/health":
            body = json.dumps({"ok": True, "clients": len(self.clients)}, ensure_ascii=False).encode("utf-8")
            await self.safe_write_http(writer, 200, body, "application/json; charset=utf-8", method == "HEAD")
            return

        if path == "/overlay.html":
            try:
                body = OVERLAY_PATH.read_bytes()
            except FileNotFoundError:
                await self.safe_write_http(writer, 404, b"overlay.html not found", "text/plain; charset=utf-8")
                return
            await self.safe_write_http(writer, 200, body, "text/html; charset=utf-8", method == "HEAD")
            return

        if path == "/favicon.ico":
            await self.safe_write_http(writer, 204, b"", "image/x-icon", method == "HEAD")
            return

        await self.safe_write_http(writer, 404, b"Not Found", "text/plain; charset=utf-8")

    async def write_redirect(self, writer: asyncio.StreamWriter, location: str) -> None:
        response = (
            "HTTP/1.1 302 Found\r\n"
            f"Location: {location}\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(response)
        await writer.drain()

    async def safe_write_http(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str,
        head_only: bool = False,
    ) -> None:
        reason = {
            200: "OK",
            204: "No Content",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
        }.get(status, "OK")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8")
        writer.write(headers)
        if not head_only and body:
            writer.write(body)
        await writer.drain()

    async def accept_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: Dict[str, str],
    ) -> None:
        key = headers.get("sec-websocket-key")
        if not key:
            await self.safe_write_http(writer, 400, b"Missing Sec-WebSocket-Key", "text/plain; charset=utf-8")
            return

        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(response)
        await writer.drain()

        client = WebSocketClient(reader=reader, writer=writer, lock=asyncio.Lock())
        self.clients.add(client)
        logging.info("浏览器源已连接，当前连接数: %s", len(self.clients))

        if self.last_message:
            await self.send_text(client, self.last_message)

        try:
            await self.read_websocket_until_close(client)
        finally:
            await self.close_client(client)
            logging.info("浏览器源已断开，当前连接数: %s", len(self.clients))

    async def read_websocket_until_close(self, client: WebSocketClient) -> None:
        while True:
            header = await client.reader.readexactly(2)
            first, second = header[0], header[1]
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F

            if length == 126:
                length = struct.unpack("!H", await client.reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await client.reader.readexactly(8))[0]

            mask = await client.reader.readexactly(4) if masked else b""
            payload = await client.reader.readexactly(length) if length else b""
            if masked and payload:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

            if opcode == 0x8:
                await self.send_close(client)
                return
            if opcode == 0x9:
                await self.send_frame(client, 0xA, payload)

    async def send_text(self, client: WebSocketClient, text: str) -> None:
        await self.send_frame(client, 0x1, text.encode("utf-8"))

    async def send_close(self, client: WebSocketClient) -> None:
        await self.send_frame(client, 0x8, b"")

    async def send_frame(self, client: WebSocketClient, opcode: int, payload: bytes) -> None:
        async with client.lock:
            try:
                header = bytearray([0x80 | opcode])
                length = len(payload)
                if length < 126:
                    header.append(length)
                elif length <= 0xFFFF:
                    header.extend((126, *struct.pack("!H", length)))
                else:
                    header.extend((127, *struct.pack("!Q", length)))
                client.writer.write(bytes(header) + payload)
                await client.writer.drain()
            except Exception:
                await self.close_client(client)

    async def close_client(self, client: WebSocketClient) -> None:
        self.clients.discard(client)
        if not client.writer.is_closing():
            client.writer.close()
        await self.wait_writer_closed(client.writer)

    async def wait_writer_closed(self, writer: asyncio.StreamWriter, timeout: float = 1.0) -> None:
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=timeout)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(signum: int, _frame: Any = None) -> None:
        global FORCE_EXIT_TIMER
        logging.info("收到退出信号 %s，正在停止 worker", signum)
        if FORCE_EXIT_TIMER is None:
            FORCE_EXIT_TIMER = threading.Timer(4.0, lambda: os._exit(0))
            FORCE_EXIT_TIMER.daemon = True
            FORCE_EXIT_TIMER.start()
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass


def find_match(text: str, targets: List[str], match_config: Dict[str, Any]) -> Optional[str]:
    if not text or not targets:
        return None

    case_sensitive = normalize_bool(match_config.get("case_sensitive", False), False)
    mode = str(match_config.get("mode", "contains")).strip().lower()
    source = text if case_sensitive else text.casefold()

    for target in targets:
        needle = target if case_sensitive else target.casefold()
        if mode == "exact":
            matched = source == needle
        else:
            matched = needle in source
        if matched:
            return target
    return None


def get_box_color(matched: str, config: Dict[str, Any]) -> str:
    overlay = config.get("overlay", {})
    default_color = str(overlay.get("stroke_color", DEFAULT_CONFIG["overlay"]["stroke_color"]))
    mode = str(overlay.get("color_mode", "single")).strip().lower()
    if mode not in {"by_target", "target", "matched", "per_target"}:
        return default_color

    palette = overlay.get("color_palette", DEFAULT_CONFIG["overlay"]["color_palette"])
    if not isinstance(palette, list) or not palette:
        return default_color
    normalized = str(matched or "").casefold().encode("utf-8")
    digest = hashlib.sha1(normalized).digest()
    index = int.from_bytes(digest[:4], "big") % len(palette)
    color = str(palette[index])
    return color if color.startswith("#") else default_color


def build_message(
    width: int,
    height: int,
    boxes: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "type": "boxes",
        "timestamp": int(time.time() * 1000),
        "width": width,
        "height": height,
        "boxes": boxes,
        "overlay": config.get("overlay", DEFAULT_CONFIG["overlay"]),
    }


def parse_obs_window_id(value: str) -> Dict[str, str]:
    parts = str(value or "").split(":")
    return {
        "title": parts[0] if len(parts) > 0 else "",
        "class": parts[1] if len(parts) > 1 else "",
        "exe": parts[2] if len(parts) > 2 else "",
    }


def get_window_rect_from_obs_setting(window_setting: str) -> Optional[Dict[str, int]]:
    target = parse_obs_window_id(window_setting)
    target_title = target.get("title", "")
    target_class = target.get("class", "")
    target_exe = target.get("exe", "").lower()
    if not any((target_title, target_class, target_exe)):
        return None

    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetModuleBaseNameW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    matches: List[Tuple[int, Dict[str, int]]] = []

    def get_text(hwnd: Any) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def get_class(hwnd: Any) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def get_exe(hwnd: Any) -> str:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid.value)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(260)
            if psapi.GetModuleBaseNameW(handle, None, buffer, len(buffer)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)

    def score_window(title: str, cls: str, exe: str) -> int:
        score = 0
        if target_exe and exe.lower() == target_exe:
            score += 10
        if target_class and cls == target_class:
            score += 10
        if target_title and title == target_title:
            score += 6
        elif target_title and (target_title in title or title in target_title):
            score += 3
        return score

    def enum_proc(hwnd: Any, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = get_text(hwnd)
        cls = get_class(hwnd)
        exe = get_exe(hwnd)
        score = score_window(title, cls, exe)
        if score <= 0:
            return True
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return True
        matches.append(
            (
                score,
                {
                    "left": int(rect.left),
                    "top": int(rect.top),
                    "width": width,
                    "height": height,
                },
            )
        )
        return True

    callback = EnumWindowsProc(enum_proc)
    user32.EnumWindows(callback, 0)
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def resolve_desktop_overlay_region(config: Dict[str, Any], frame: CaptureFrame) -> Dict[str, int]:
    overlay_config = get_desktop_overlay_config(config)
    mode = str(overlay_config.get("coordinate_mode", "capture")).strip().lower()
    if mode in {"screen", "screen_region", "display"}:
        screen_region = overlay_config.get("screen_region", {})
        if isinstance(screen_region, str) and screen_region.strip().lower() == "auto":
            screen_region = {"auto": True}
        if not isinstance(screen_region, dict):
            screen_region = {}

        auto = normalize_bool(screen_region.get("auto", False), False)
        if auto:
            source_info = frame.source_info or {}
            input_settings = source_info.get("input_settings", {})
            window_setting = input_settings.get("window") if isinstance(input_settings, dict) else None
            if window_setting:
                auto_region = get_window_rect_from_obs_setting(str(window_setting))
                if auto_region is not None:
                    return auto_region
                logging.warning("无法自动定位 OBS 窗口采集目标窗口，将使用 screen_region 手动配置")

        def int_value(key: str, default: int) -> int:
            try:
                return int(screen_region.get(key, default))
            except (TypeError, ValueError):
                return default

        width = int_value("width", frame.width)
        height = int_value("height", frame.height)
        return {
            "left": int_value("left", frame.region.get("left", 0)),
            "top": int_value("top", frame.region.get("top", 0)),
            "width": width if width > 0 else frame.width,
            "height": height if height > 0 else frame.height,
        }
    return frame.region


async def capture_frame(
    sct: mss.MSS,
    obs_client: OBSWebSocketScreenshotClient,
    config: Dict[str, Any],
) -> CaptureFrame:
    capture = config.get("capture", {})
    source = str(capture.get("source", "screen")).strip().lower()
    if source in {"obs", "obs_websocket", "obs-websocket"}:
        return await obs_client.capture(config)

    region, width, height = resolve_capture_region(sct, config)
    screenshot = sct.grab(region)
    image = np.asarray(screenshot)[:, :, :3]
    return CaptureFrame(
        image=image,
        region=region,
        width=width,
        height=height,
        source_info={"source": "screen"},
    )


async def recognition_loop(server: OverlayServer, stop_event: asyncio.Event) -> None:
    ocr = RapidOCREngine()
    desktop_overlay = DesktopOverlay()
    obs_client = OBSWebSocketScreenshotClient()
    config = load_config(write_if_missing=True)
    targets = read_targets()
    last_reload_at = 0.0
    last_perf_log_at = 0.0
    with mss.MSS() as sct:
        try:
            while not stop_event.is_set():
                started = time.monotonic()
                now = time.monotonic()
                reload_interval = int(config.get("ocr", {}).get("reload_files_interval_ms", 2000)) / 1000.0
                if now - last_reload_at >= max(0.2, reload_interval):
                    config = load_config(write_if_missing=True)
                    targets = read_targets()
                    last_reload_at = now
                interval = get_interval_seconds(config)

                try:
                    boxes: List[Dict[str, Any]] = []
                    capture_started = time.monotonic()
                    frame = await capture_frame(sct, obs_client, config)
                    capture_elapsed = time.monotonic() - capture_started
                    region, width, height = frame.region, frame.width, frame.height

                    ocr_elapsed = 0.0
                    if targets and frame.image is not None:
                        ocr_started = time.monotonic()
                        items = ocr.recognize(frame.image, config)
                        ocr_elapsed = time.monotonic() - ocr_started
                        match_config = config.get("match", {})
                        min_confidence = float(match_config.get("min_confidence", 0.5))

                        for item in items:
                            if item.confidence < min_confidence:
                                continue
                            matched = find_match(item.text, targets, match_config)
                            if matched is None:
                                continue

                            rect = item.rect
                            boxes.append(
                                {
                                    "text": item.text,
                                    "matched": matched,
                                    "confidence": round(float(item.confidence), 4),
                                    "color": get_box_color(matched, config),
                                    "x": round(float(rect["x"]), 2),
                                    "y": round(float(rect["y"]), 2),
                                    "w": round(float(rect["w"]), 2),
                                    "h": round(float(rect["h"]), 2),
                                }
                            )

                        logging.debug("本轮 OCR=%s 命中=%s 目标=%s", len(items), len(boxes), len(targets))

                    message = build_message(width, height, boxes, config)
                    await server.broadcast(message)
                    desktop_overlay.update(message, config, resolve_desktop_overlay_region(config, frame))
                    perf_interval = int(config.get("ocr", {}).get("log_performance_interval_ms", 3000)) / 1000.0
                    perf_now = time.monotonic()
                    if (
                        normalize_bool(config.get("ocr", {}).get("log_performance", True), True)
                        and perf_now - last_perf_log_at >= max(0.5, perf_interval)
                    ):
                        logging.info(
                            "性能: capture=%.0fms ocr=%.0fms total=%.0fms image=%sx%s boxes=%s",
                            capture_elapsed * 1000,
                            ocr_elapsed * 1000,
                            (time.monotonic() - started) * 1000,
                            width,
                            height,
                            len(boxes),
                        )
                        last_perf_log_at = perf_now
                except Exception:
                    logging.exception("识别循环失败，程序继续运行")
                    try:
                        config = load_config(write_if_missing=True)
                        capture = config.get("capture", {})
                        width = int(capture.get("width", DEFAULT_CONFIG["capture"]["width"]))
                        height = int(capture.get("height", DEFAULT_CONFIG["capture"]["height"]))
                        region = {
                            "left": int(capture.get("left", DEFAULT_CONFIG["capture"]["left"])),
                            "top": int(capture.get("top", DEFAULT_CONFIG["capture"]["top"])),
                            "width": width,
                            "height": height,
                        }
                        message = build_message(width, height, [], config)
                        await server.broadcast(message)
                        desktop_overlay.update(message, config, region)
                    except Exception:
                        logging.exception("发送空框失败")

                elapsed = time.monotonic() - started
                wait_seconds = max(0.0, interval - elapsed)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            desktop_overlay.stop()
            await obs_client.close()


async def main_async() -> None:
    global FORCE_EXIT_TIMER
    setup_logging()
    config = load_config(write_if_missing=True)
    host = str(config.get("host", DEFAULT_CONFIG["host"]))
    port = int(config.get("port", DEFAULT_CONFIG["port"]))

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    server = OverlayServer(host, port)
    await server.start()
    worker_task = asyncio.create_task(recognition_loop(server, stop_event))

    try:
        await stop_event.wait()
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        try:
            await asyncio.wait_for(server.stop(), timeout=2.0)
        except asyncio.TimeoutError:
            logging.warning("HTTP/WebSocket 服务停止超时，继续退出")
        if FORCE_EXIT_TIMER is not None:
            FORCE_EXIT_TIMER.cancel()
            FORCE_EXIT_TIMER = None
        logging.info("worker 已停止")


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
