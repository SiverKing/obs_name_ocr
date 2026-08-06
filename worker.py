import asyncio
import base64
import colorsys
import copy
import errno
import hashlib
import io
import json
import logging
import os
import queue
import signal
import site
import struct
import sys
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import mss
import numpy as np


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
NAME_PATH = BASE_DIR / "name.txt"
OVERLAY_PATH = BASE_DIR / "overlay.html"
LOG_DIR = BASE_DIR / "logs"
OCR_OUTPUT_PATH = BASE_DIR / "ocr_output.txt"

OCR_BACKEND_ONNXRUNTIME = "onnxruntime"
OCR_BACKEND_TENSORRT_FP32 = "tensorrt_fp32"
OCR_BACKEND_TENSORRT_FP16 = "tensorrt_fp16"
OCR_BACKEND_VALUES = (
    OCR_BACKEND_ONNXRUNTIME,
    OCR_BACKEND_TENSORRT_FP32,
    OCR_BACKEND_TENSORRT_FP16,
)
OCR_BACKEND_LABELS = {
    OCR_BACKEND_ONNXRUNTIME: "ONNX Runtime",
    OCR_BACKEND_TENSORRT_FP32: "RapidOCR 原生 TensorRT FP32",
    OCR_BACKEND_TENSORRT_FP16: "RapidOCR 原生 TensorRT FP16",
}

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
    "match_tolerance": {
        "enabled": True,
        "normalize_confusable": True,
        "collapse_repeated_chars": True,
        "ignore_separators": True,
        "max_edit_distance": 1,
        "fuzzy_enabled": True,
        "fuzzy_threshold": 0.88,
        "fuzzy_min_length": 4,
    },
    "ocr_output": {
        "enabled": True,
    },
    "ocr": {
        "backend": OCR_BACKEND_ONNXRUNTIME,
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


class DailyFileHandler(logging.Handler):
    """按日志记录日期写入 logs/YYYYMMDD.log。"""

    def __init__(self, log_dir: Path) -> None:
        super().__init__()
        self.log_dir = log_dir

    def emit(self, record: logging.LogRecord) -> None:
        try:
            date_name = time.strftime("%Y%m%d", time.localtime(record.created))
            log_path = self.log_dir / f"{date_name}.log"
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as file:
                file.write(self.format(record))
                file.write("\n")
        except Exception:
            self.handleError(record)


def _terminal_logging_enabled() -> bool:
    try:
        return bool(sys.stderr and sys.stderr.isatty())
    except Exception:
        return False


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = DailyFileHandler(LOG_DIR)
    file_handler.setFormatter(formatter)
    handlers: List[logging.Handler] = [file_handler]
    if _terminal_logging_enabled():
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        handlers.insert(0, stream_handler)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def _configure_rapidocr_logging() -> None:
    rapidocr_logger = logging.getLogger("RapidOCR")
    daily_handler = next(
        (
            handler
            for handler in rapidocr_logger.handlers
            if getattr(handler, "_obs_name_ocr_daily", False)
        ),
        None,
    )
    if daily_handler is None:
        daily_handler = DailyFileHandler(LOG_DIR)
        daily_handler._obs_name_ocr_daily = True
        daily_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] [RapidOCR] %(message)s")
        )
        rapidocr_logger.addHandler(daily_handler)

    if not _terminal_logging_enabled():
        for handler in list(rapidocr_logger.handlers):
            if handler is daily_handler:
                continue
            rapidocr_logger.removeHandler(handler)
            handler.close()
    rapidocr_logger.propagate = False


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


def normalize_startup_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = int(DEFAULT_CONFIG["port"])
        logging.warning("config.json port 无效，启动时回退到默认端口: %s", port)

    if port < 1 or port > 65535:
        default_port = int(DEFAULT_CONFIG["port"])
        logging.warning("config.json port=%s 超出 1-65535，启动时回退到默认端口: %s", port, default_port)
        return default_port
    return port


def is_address_in_use_error(exc: OSError) -> bool:
    return (
        exc.errno == errno.EADDRINUSE
        or getattr(exc, "winerror", None) == 10048
        or "address already in use" in str(exc).lower()
        or "通常每个套接字地址" in str(exc)
    )


def save_config_port(port: int, previous_port: int) -> None:
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("config.json 顶层必须是 JSON 对象")
        else:
            data = {}

        data["port"] = port
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logging.info("已将 config.json 端口从 %s 更新为 %s", previous_port, port)
    except Exception:
        logging.exception("写入 config.json 端口失败，请手动把 port 改为 %s", port)


def parse_targets(content: str) -> Tuple[List[str], Dict[str, str]]:
    targets: List[str] = []
    target_groups: Dict[str, str] = {}
    current_group = ""

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            current_group = line[1:].strip()
            continue

        targets.append(line)
        # 匹配按 name.txt 顺序取第一个目标，重复目标的分组也保持相同规则。
        target_groups.setdefault(line.casefold(), current_group)

    return targets, target_groups


def read_targets_and_groups() -> Tuple[List[str], Dict[str, str]]:
    if not NAME_PATH.exists():
        return [], {}

    try:
        return parse_targets(NAME_PATH.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("读取 name.txt 失败，当前轮使用空目标列表")
        return [], {}


def read_targets() -> List[str]:
    targets, _ = read_targets_and_groups()
    return targets


def build_target_label(target: str, target_groups: Dict[str, str]) -> str:
    group = target_groups.get(str(target).casefold(), "")
    return f"{group}-{target}" if group else target


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


@lru_cache(maxsize=32)
def _warn_invalid_ocr_backend(value_repr: str) -> None:
    logging.warning(
        "ocr.backend=%s 非法；合法值为 %s，已安全回退到 %s",
        value_repr,
        ", ".join(OCR_BACKEND_VALUES),
        OCR_BACKEND_ONNXRUNTIME,
    )


def normalize_ocr_backend(value: Any, warn: bool = True) -> str:
    if isinstance(value, str) and value in OCR_BACKEND_VALUES:
        return value
    if warn:
        _warn_invalid_ocr_backend(repr(value))
    return OCR_BACKEND_ONNXRUNTIME


def describe_ocr_backend(backend: str) -> str:
    normalized = normalize_ocr_backend(backend, warn=False)
    return OCR_BACKEND_LABELS[normalized]


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
class MatchResult:
    target: str
    method: str
    score: float = 1.0


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
        self._engine_backend: Optional[str] = None
        self._last_initialization_failure: Optional[
            Tuple[str, str, str, Optional[str]]
        ] = None
        self._nvidia_dll_directories_ready = False

    def _build_engine_params(self, config: Dict[str, Any]) -> Dict[str, Any]:
        ocr_config = config.get("ocr", {})
        backend = normalize_ocr_backend(
            ocr_config.get("backend", OCR_BACKEND_ONNXRUNTIME)
        )
        if backend == OCR_BACKEND_ONNXRUNTIME:
            return {
                "EngineConfig.onnxruntime.use_cuda": normalize_bool(
                    ocr_config.get("use_cuda", False), False
                ),
                "EngineConfig.onnxruntime.use_dml": normalize_bool(
                    ocr_config.get("use_dml", False), False
                ),
            }

        from rapidocr import EngineType

        engine_params = {
            "Det.engine_type": EngineType.TENSORRT,
            "Rec.engine_type": EngineType.TENSORRT,
            "EngineConfig.tensorrt.use_fp16": (
                backend == OCR_BACKEND_TENSORRT_FP16
            ),
            "EngineConfig.tensorrt.use_int8": False,
        }
        if normalize_bool(ocr_config.get("use_cls", False), False):
            engine_params["Cls.engine_type"] = EngineType.TENSORRT
        return engine_params

    def _ensure_engine(self, config: Dict[str, Any]) -> bool:
        old_engine = self._engine
        old_params = self._engine_params
        old_backend = self._engine_backend
        ocr_config = config.get("ocr", {})
        requested_backend = normalize_ocr_backend(
            ocr_config.get("backend", OCR_BACKEND_ONNXRUNTIME)
        )

        try:
            engine_params = self._build_engine_params(config)
            if (
                self._engine is not None
                and requested_backend == self._engine_backend
                and engine_params == self._engine_params
            ):
                self._last_initialization_failure = None
                return True

            if requested_backend == OCR_BACKEND_ONNXRUNTIME:
                self._preload_onnxruntime_dlls(engine_params)
            else:
                if not self._nvidia_dll_directories_ready:
                    self._add_nvidia_dll_directories()
                    self._nvidia_dll_directories_ready = True
                self._check_tensorrt_dependencies(requested_backend)
                self._apply_rapidocr_tensorrt_compatibility()

            from rapidocr import RapidOCR
            _configure_rapidocr_logging()

            if old_engine is not None:
                logging.info(
                    "OCR 引擎配置变化，正在按用户请求重新初始化：%s",
                    describe_ocr_backend(requested_backend),
                )

            new_engine = RapidOCR(params=engine_params)
            self._engine = new_engine
            self._engine_params = engine_params
            self._engine_backend = requested_backend
            self._last_initialization_failure = None
            logging.info(
                "RapidOCR 初始化完成；当前实际后端：%s",
                describe_ocr_backend(requested_backend),
            )
            self._log_runtime_backend(
                requested_backend,
                normalize_bool(ocr_config.get("use_cls", False), False),
            )
            return True
        except Exception as exc:
            failure_key = (
                requested_backend,
                repr(locals().get("engine_params", {})),
                f"{type(exc).__name__}: {exc}",
                old_backend,
            )
            if old_engine is not None:
                self._engine = old_engine
                self._engine_params = old_params
                self._engine_backend = old_backend
                if failure_key != self._last_initialization_failure:
                    logging.exception(
                        "用户请求的 OCR 后端 %s 初始化失败；失败原因：%s；"
                        "当前继续运行的实际后端：%s",
                        describe_ocr_backend(requested_backend),
                        exc,
                        describe_ocr_backend(old_backend or OCR_BACKEND_ONNXRUNTIME),
                    )
                    self._last_initialization_failure = failure_key
                return True

            if failure_key != self._last_initialization_failure:
                logging.exception(
                    "用户请求的 OCR 后端 %s 初始化失败；失败原因：%s；"
                    "当前继续运行的实际后端：无（OCR 暂不可用，将发送空框）",
                    describe_ocr_backend(requested_backend),
                    exc,
                )
                self._last_initialization_failure = failure_key
            return False

    def _check_tensorrt_dependencies(self, backend: str) -> None:
        backend_label = describe_ocr_backend(backend)
        import_errors: List[str] = []
        try:
            import tensorrt  # noqa: F401
        except Exception as exc:
            import_errors.append(f"tensorrt ({type(exc).__name__}: {exc})")

        try:
            from cuda.bindings import runtime as cudart  # noqa: F401
        except Exception as exc:
            import_errors.append(
                f"cuda.bindings.runtime ({type(exc).__name__}: {exc})"
            )

        if import_errors:
            raise RuntimeError(
                f"请求 {backend_label}，但原生 TensorRT 依赖不可用："
                f"{'; '.join(import_errors)}；请手动安装与 CUDA 12 匹配的 "
                "tensorrt-cu12 和 cuda-python；程序不会自动安装或伪装为已启用 "
                "TensorRT"
            )

    def _apply_rapidocr_tensorrt_compatibility(self) -> None:
        if importlib_metadata.version("rapidocr") != "3.8.4":
            return

        from rapidocr.inference_engine.tensorrt import TRTInferSession

        if "model_root_dir" in TRTInferSession.__dict__:
            return

        # RapidOCR 3.8.4 reads this attribute before assigning the configured
        # model root. A class-level default restores its intended lazy setup
        # without changing RapidOCR files or overriding its cache configuration.
        setattr(TRTInferSession, "model_root_dir", None)
        logging.warning(
            "检测到 RapidOCR 3.8.4 原生 TensorRT 的 model_root_dir 初始化缺陷；"
            "已仅在当前进程应用兼容处理，未修改 RapidOCR 包源码"
        )

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

    def _log_runtime_backend(self, backend: str, use_cls: bool) -> None:
        if backend == OCR_BACKEND_ONNXRUNTIME:
            self._log_runtime_providers()
            return

        precision = "FP16" if backend == OCR_BACKEND_TENSORRT_FP16 else "FP32"
        if use_cls:
            logging.info(
                "RapidOCR 原生 TensorRT：精度=%s；实际后端 "
                "Det=TensorRT、Rec=TensorRT、Cls=TensorRT",
                precision,
            )
        else:
            logging.info(
                "RapidOCR 原生 TensorRT：精度=%s；实际后端 "
                "Det=TensorRT、Rec=TensorRT；Cls 未启用（保持默认 ONNX Runtime）",
                precision,
            )

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
                            label = str(
                                box.get("label") or box.get("matched") or box.get("text") or ""
                            )
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


async def start_overlay_server_with_available_port(host: str, requested_port: Any) -> OverlayServer:
    first_port = normalize_startup_port(requested_port)
    port = first_port

    while port <= 65535:
        server = OverlayServer(host, port)
        try:
            await server.start()
        except OSError as exc:
            if not is_address_in_use_error(exc):
                raise

            if port >= 65535:
                raise RuntimeError("端口 65535 已被占用，无法继续自动递增端口") from exc

            next_port = port + 1
            logging.warning("端口 %s 已被占用，自动尝试下一个端口 %s", port, next_port)
            port = next_port
            continue

        if port != first_port:
            save_config_port(port, first_port)
            logging.info("端口自动调整完成，worker 当前监听端口: %s", port)
        return server

    raise RuntimeError(f"从端口 {first_port} 到 65535 都不可用，worker 无法启动")


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


def get_match_tolerance_config(config: Dict[str, Any]) -> Dict[str, Any]:
    current = config.get("match_tolerance", {})
    if not isinstance(current, dict):
        current = {}
    return deep_merge(DEFAULT_CONFIG["match_tolerance"], current)


def get_ocr_output_config(config: Dict[str, Any]) -> Dict[str, Any]:
    current = config.get("ocr_output", {})
    if not isinstance(current, dict):
        current = {}
    return deep_merge(DEFAULT_CONFIG["ocr_output"], current)


def match_text(source: str, needle: str, mode: str) -> bool:
    if mode == "exact":
        return source == needle
    return needle in source


def normalize_confusable_text(text: str, case_sensitive: bool) -> str:
    replacements = {
        "0": "o",
        "1": "l",
        "I": "l",
        "|": "l",
        "!": "l",
        "5": "s",
        "$": "s",
    }
    normalized = "".join(replacements.get(char, char) for char in text)
    return normalized if case_sensitive else normalized.casefold()


def collapse_repeated_chars(text: str) -> str:
    if not text:
        return text

    result = [text[0]]
    for char in text[1:]:
        if char != result[-1]:
            result.append(char)
    return "".join(result)


def strip_match_separators(text: str) -> str:
    return "".join(char for char in text if char not in {"_", "-", " ", "\t"})


def prepare_tolerant_text(text: str, case_sensitive: bool, tolerance_config: Dict[str, Any]) -> str:
    prepared = text if case_sensitive else text.casefold()
    if normalize_bool(tolerance_config.get("normalize_confusable", True), True):
        prepared = normalize_confusable_text(text, case_sensitive)
    if normalize_bool(tolerance_config.get("collapse_repeated_chars", True), True):
        prepared = collapse_repeated_chars(prepared)
    if normalize_bool(tolerance_config.get("ignore_separators", True), True):
        prepared = strip_match_separators(prepared)
    return prepared


def similarity_score(source: str, needle: str) -> float:
    if not source or not needle:
        return 0.0
    return SequenceMatcher(None, source, needle).ratio()


def limited_edit_distance(left: str, right: str, max_distance: int) -> int:
    if max_distance < 0:
        return max_distance + 1
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    if left == right:
        return 0

    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        row_min = current[0]
        for right_index, right_char in enumerate(right, 1):
            cost = 0 if left_char == right_char else 1
            value = min(
                previous[right_index] + 1,
                current[right_index - 1] + 1,
                previous[right_index - 1] + cost,
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def find_match_detail(
    text: str,
    targets: List[str],
    match_config: Dict[str, Any],
    tolerance_config: Optional[Dict[str, Any]] = None,
) -> Optional[MatchResult]:
    if not text or not targets:
        return None

    case_sensitive = normalize_bool(match_config.get("case_sensitive", False), False)
    mode = str(match_config.get("mode", "contains")).strip().lower()
    source = text if case_sensitive else text.casefold()

    for target in targets:
        needle = target if case_sensitive else target.casefold()
        if match_text(source, needle, mode):
            return MatchResult(target=target, method="原始匹配", score=1.0)

    tolerance = deep_merge(DEFAULT_CONFIG["match_tolerance"], tolerance_config or {})
    if not normalize_bool(tolerance.get("enabled", True), True):
        return None

    tolerant_source = prepare_tolerant_text(text, case_sensitive, tolerance)
    for target in targets:
        tolerant_needle = prepare_tolerant_text(target, case_sensitive, tolerance)
        if match_text(tolerant_source, tolerant_needle, mode):
            return MatchResult(target=target, method="归一化/重复字符容错", score=1.0)

    try:
        max_edit_distance = int(tolerance.get("max_edit_distance", 1))
    except (TypeError, ValueError):
        max_edit_distance = 1
    max_edit_distance = max(0, max_edit_distance)

    try:
        min_length = int(tolerance.get("fuzzy_min_length", 4))
    except (TypeError, ValueError):
        min_length = 4
    min_length = max(1, min_length)

    if max_edit_distance > 0 and len(tolerant_source) >= min_length:
        best_result: Optional[MatchResult] = None
        for target in targets:
            tolerant_needle = prepare_tolerant_text(target, case_sensitive, tolerance)
            if len(tolerant_needle) < min_length:
                continue
            distance = limited_edit_distance(tolerant_source, tolerant_needle, max_edit_distance)
            if distance <= max_edit_distance:
                score = 1.0 - (distance / max(len(tolerant_source), len(tolerant_needle), 1))
                if best_result is None or score > best_result.score:
                    best_result = MatchResult(target=target, method="编辑距离容错", score=score)
        if best_result is not None:
            return best_result

    if not normalize_bool(tolerance.get("fuzzy_enabled", True), True):
        return None

    try:
        threshold = float(tolerance.get("fuzzy_threshold", 0.88))
    except (TypeError, ValueError):
        threshold = 0.88
    threshold = min(1.0, max(0.0, threshold))

    best_result: Optional[MatchResult] = None
    for target in targets:
        tolerant_needle = prepare_tolerant_text(target, case_sensitive, tolerance)
        if len(tolerant_source) < min_length or len(tolerant_needle) < min_length:
            continue

        score = similarity_score(tolerant_source, tolerant_needle)
        if score >= threshold and (best_result is None or score > best_result.score):
            best_result = MatchResult(target=target, method="相似度容错", score=score)

    return best_result


def find_match(
    text: str,
    targets: List[str],
    match_config: Dict[str, Any],
    tolerance_config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    result = find_match_detail(text, targets, match_config, tolerance_config)
    if result is not None:
        return result.target
    return None


def rgb_distance_squared(left: Tuple[int, int, int], right: Tuple[int, int, int]) -> int:
    return sum((left_channel - right_channel) ** 2 for left_channel, right_channel in zip(left, right))


@lru_cache(maxsize=64)
def generate_distinct_colors(count: int) -> Tuple[str, ...]:
    if count <= 0:
        return ()

    candidates: List[Tuple[int, int, int]] = []
    seen_candidates: set[Tuple[int, int, int]] = set()
    hue_steps = max(72, min(360, count * 4))
    for hue_index in range(hue_steps):
        hue = hue_index / hue_steps
        for saturation in (0.68, 0.82, 0.96):
            for value in (0.78, 0.90, 1.0):
                red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
                rgb = (round(red * 255), round(green * 255), round(blue * 255))
                luminance = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
                if luminance < 75 or rgb in seen_candidates:
                    continue
                seen_candidates.add(rgb)
                candidates.append(rgb)

    selected: List[Tuple[int, int, int]] = [(242, 48, 48)]
    remaining = [candidate for candidate in candidates if candidate != selected[0]]
    while len(selected) < count and remaining:
        best = max(
            remaining,
            key=lambda candidate: (
                min(rgb_distance_squared(candidate, current) for current in selected),
                sum(candidate),
            ),
        )
        selected.append(best)
        remaining.remove(best)

    golden_ratio = 0.618033988749895
    while len(selected) < count:
        index = len(selected)
        red, green, blue = colorsys.hsv_to_rgb((index * golden_ratio) % 1.0, 0.86, 0.92)
        candidate = (round(red * 255), round(green * 255), round(blue * 255))
        if candidate not in selected:
            selected.append(candidate)

    return tuple(f"#{red:02x}{green:02x}{blue:02x}" for red, green, blue in selected)


def build_target_color_map(targets: List[str], config: Dict[str, Any]) -> Dict[str, str]:
    overlay = config.get("overlay", {})
    mode = str(overlay.get("color_mode", "single")).strip().lower()
    if mode not in {"by_target", "target", "matched", "per_target"}:
        return {}

    unique_targets: List[str] = []
    seen: set[str] = set()
    for target in targets:
        key = str(target).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        unique_targets.append(key)

    colors = generate_distinct_colors(len(unique_targets))
    return dict(zip(unique_targets, colors))


def get_box_color(
    matched: str,
    config: Dict[str, Any],
    target_color_map: Optional[Dict[str, str]] = None,
) -> str:
    overlay = config.get("overlay", {})
    default_color = str(overlay.get("stroke_color", DEFAULT_CONFIG["overlay"]["stroke_color"]))
    mode = str(overlay.get("color_mode", "single")).strip().lower()
    if mode not in {"by_target", "target", "matched", "per_target"}:
        return default_color

    if target_color_map:
        color = target_color_map.get(str(matched or "").casefold())
        if color:
            return color
    return default_color


def clean_ocr_debug_text(text: Any) -> str:
    return str(text).replace("\r", "\\r").replace("\n", "\\n")


def write_ocr_output(
    items: List[OCRItem],
    targets: List[str],
    match_config: Dict[str, Any],
    tolerance_config: Dict[str, Any],
    min_confidence: float,
    frame: CaptureFrame,
    ocr_elapsed: float,
    skipped_reason: str = "",
) -> None:
    lines = [
        "OCR 原始识别结果",
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"截图来源: {frame.source_info.get('source', 'unknown') if isinstance(frame.source_info, dict) else 'unknown'}",
        f"图像尺寸: {frame.width}x{frame.height}",
        f"截图区域: left={frame.region.get('left', 0)} top={frame.region.get('top', 0)} width={frame.region.get('width', frame.width)} height={frame.region.get('height', frame.height)}",
        f"目标数量: {len(targets)}",
        f"匹配模式: {match_config.get('mode', DEFAULT_CONFIG['match']['mode'])}",
        f"大小写敏感: {normalize_bool(match_config.get('case_sensitive', False), False)}",
        f"min_confidence: {min_confidence:.4f}",
        f"容错匹配: {normalize_bool(tolerance_config.get('enabled', True), True)}",
        f"字符归一化: {normalize_bool(tolerance_config.get('normalize_confusable', True), True)}",
        f"重复字符压缩: {normalize_bool(tolerance_config.get('collapse_repeated_chars', True), True)}",
        f"忽略分隔符: {normalize_bool(tolerance_config.get('ignore_separators', True), True)}",
        f"最大编辑距离: {int(tolerance_config.get('max_edit_distance', 1))}",
        f"相似度匹配: {normalize_bool(tolerance_config.get('fuzzy_enabled', True), True)}",
        f"相似度阈值: {float(tolerance_config.get('fuzzy_threshold', 0.88)):.4f}",
        f"OCR 耗时: {ocr_elapsed * 1000:.0f}ms",
        f"原始条目数: {len(items)}",
    ]

    if skipped_reason:
        lines.extend(["", skipped_reason])
    elif not items:
        lines.extend(["", "未识别到任何文本"])
    else:
        lines.append("")
        for index, item in enumerate(items, 1):
            rect = item.rect
            match_result = find_match_detail(item.text, targets, match_config, tolerance_config)
            if item.confidence < min_confidence:
                status = "低于置信度阈值"
            elif match_result is not None:
                status = f"命中目标: {match_result.target} ({match_result.method}"
                if match_result.score < 1.0:
                    status += f", score={match_result.score:.4f}"
                status += ")"
            else:
                status = "未命中目标"
            lines.append(
                f"{index:03d} | {status} | conf={float(item.confidence):.4f} "
                f"| rect=x={float(rect['x']):.2f},y={float(rect['y']):.2f},w={float(rect['w']):.2f},h={float(rect['h']):.2f} "
                f"| text={clean_ocr_debug_text(item.text)}"
            )

    try:
        OCR_OUTPUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        logging.exception("写入 OCR 原始识别结果失败: %s", OCR_OUTPUT_PATH)


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
    targets, target_groups = read_targets_and_groups()
    target_color_map = build_target_color_map(targets, config)
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
                    targets, target_groups = read_targets_and_groups()
                    target_color_map = build_target_color_map(targets, config)
                    last_reload_at = now
                interval = get_interval_seconds(config)

                try:
                    boxes: List[Dict[str, Any]] = []
                    capture_started = time.monotonic()
                    frame = await capture_frame(sct, obs_client, config)
                    capture_elapsed = time.monotonic() - capture_started
                    region, width, height = frame.region, frame.width, frame.height

                    items: List[OCRItem] = []
                    ocr_elapsed = 0.0
                    match_config = config.get("match", {})
                    tolerance_config = get_match_tolerance_config(config)
                    ocr_output_config = get_ocr_output_config(config)
                    min_confidence = float(match_config.get("min_confidence", 0.5))
                    skipped_reason = ""
                    if frame.image is None:
                        skipped_reason = "未执行 OCR：当前截图为空"
                    elif not targets:
                        skipped_reason = "未执行 OCR：name.txt 没有有效目标"
                    else:
                        ocr_started = time.monotonic()
                        items = ocr.recognize(frame.image, config)
                        ocr_elapsed = time.monotonic() - ocr_started

                        for item in items:
                            if item.confidence < min_confidence:
                                continue
                            matched = find_match(item.text, targets, match_config, tolerance_config)
                            if matched is None:
                                continue

                            rect = item.rect
                            boxes.append(
                                {
                                    "text": item.text,
                                    "matched": matched,
                                    "label": build_target_label(matched, target_groups),
                                    "confidence": round(float(item.confidence), 4),
                                    "color": get_box_color(matched, config, target_color_map),
                                    "x": round(float(rect["x"]), 2),
                                    "y": round(float(rect["y"]), 2),
                                    "w": round(float(rect["w"]), 2),
                                    "h": round(float(rect["h"]), 2),
                                }
                            )

                        logging.debug("本轮 OCR=%s 命中=%s 目标=%s", len(items), len(boxes), len(targets))
                    if normalize_bool(ocr_output_config.get("enabled", True), True):
                        write_ocr_output(
                            items,
                            targets,
                            match_config,
                            tolerance_config,
                            min_confidence,
                            frame,
                            ocr_elapsed,
                            skipped_reason,
                        )

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
    port = config.get("port", DEFAULT_CONFIG["port"])

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    server = await start_overlay_server_with_available_port(host, port)
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
