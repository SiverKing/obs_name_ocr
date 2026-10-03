# 版权所有 © 2026 www.siver.top
# 修改这里即可更新 GUI 显示的版本号
APP_VERSION = "v16"

import asyncio
import base64
import copy
import hashlib
import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import name_store

try:
    from PySide6.QtCore import QItemSelectionModel, QPoint, QPointF, QRectF, Qt, QThread, QTimer, Signal
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QCloseEvent,
        QFont,
        QIcon,
        QLinearGradient,
        QPainter,
        QPen,
        QPixmap,
    )
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QColorDialog,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPushButton,
        QPlainTextEdit,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QStyle,
        QStyledItemDelegate,
        QTableWidget,
        QTableWidgetItem,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    print("缺少 PySide6。请先执行：python -m pip install -r requirements.txt")
    raise SystemExit(1) from exc


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
NAME_PATH = BASE_DIR / "name.txt"
WORKER_PATH = BASE_DIR / "worker.py"
LOG_DIR = BASE_DIR / "logs"

GROUP_STYLE_HELP = (
    ("#R", "rainbow 彩虹流光", "彩虹沿边框流动，约 2 秒一圈；标签为彩虹底色白字描边。"),
    ("#F", "flash 红白闪烁", "边框与标签在红、白之间交替闪烁，每秒 2 次。"),
    ("#P", "pulse 呼吸脉冲", "边框亮度按 1.2 秒周期在 55% ~ 100% 之间呼吸。"),
    ("#M", "march 黄黑警戒", "黄黑相间的警戒段沿边框滚动，像跑马灯。"),
    ("#N", "neon 霓虹光晕", "保留自动分色，边框外圈同色柔光按 1.6 秒周期呼吸。"),
    ("#D", "duotone 双色渐变", "青色 #00c7be 与紫色 #af52de 沿边框循环流动。"),
)

STYLE_DISPLAY_NAMES: Dict[str, str] = {
    "": "默认自动分色",
    "rainbow": "彩虹流光",
    "flash": "红白闪烁",
    "pulse": "呼吸脉冲",
    "march": "黄黑警戒",
    "neon": "霓虹光晕",
    "duotone": "双色渐变",
}
"""特效名 → 中文显示名，与 worker.GROUP_STYLE_SUFFIXES 的取值一一对应。"""

GROUP_STYLE_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("", "默认自动分色"),
    ("rainbow", "彩虹流光 #R"),
    ("flash", "红白闪烁 #F"),
    ("pulse", "呼吸脉冲 #P"),
    ("march", "黄黑警戒 #M"),
    ("neon", "霓虹光晕 #N"),
    ("duotone", "双色渐变 #D"),
)
"""分组特效下拉框的取值与文案。"""

NAME_TREE_GROUP_ROLE = "group"
NAME_TREE_TARGET_ROLE = "target"

PREVIEW_LABEL_HEIGHT = 16
PREVIEW_ANIM_INTERVAL_MS = 33
# 与 overlay.html / worker.DesktopOverlay 的动画参数保持一致（彩虹取 worker 的 0.5，即文档里的“约 2 秒一圈”）。
PREVIEW_RAINBOW_SPEED = 0.5
PREVIEW_FLASH_HZ = 2.0
PREVIEW_PULSE_PERIOD = 1.2
PREVIEW_NEON_PERIOD = 1.6
PREVIEW_MARCH_SPEED = 40.0
PREVIEW_MARCH_SEGMENT = 8
PREVIEW_DUOTONE_SPEED = 0.1
PREVIEW_MARCH_COLORS = ("#ffcc00", "#1a1a1a")
PREVIEW_DUOTONE_COLORS = ("#00c7be", "#af52de")


def scale_hex_color(value: str, factor: float) -> QColor:
    """按比例缩放颜色亮度，等价于 overlay.html 的 scaleColor()。"""
    color = QColor(value)
    if not color.isValid():
        return QColor("#ff3b30")
    clamp = lambda channel: max(0, min(255, int(round(channel * factor))))  # noqa: E731
    return QColor(clamp(color.red()), clamp(color.green()), clamp(color.blue()))


def mix_hex_color(left: str, right: str, ratio: float) -> QColor:
    """按比例混合两个颜色，等价于 overlay.html 的 mixColor()。"""
    color_a = QColor(left)
    color_b = QColor(right)
    if not color_a.isValid() or not color_b.isValid():
        return QColor("#ff3b30")
    ratio = max(0.0, min(1.0, float(ratio)))
    return QColor(
        int(round(color_a.red() + (color_b.red() - color_a.red()) * ratio)),
        int(round(color_a.green() + (color_b.green() - color_a.green()) * ratio)),
        int(round(color_a.blue() + (color_b.blue() - color_a.blue()) * ratio)),
    )


def hue_color(position: float) -> QColor:
    """0~1 的色相位置 → 高饱和颜色，用于彩虹流光。"""
    wrapped = ((float(position) % 1.0) + 1.0) % 1.0
    return QColor.fromHsvF(wrapped, 1.0, 1.0)


def perimeter_point(rect: QRectF, position: float) -> QPointF:
    """矩形周长上 0~1 位置对应的点，等价于 overlay.html 的 rectPoint()。"""
    wrapped = ((float(position) % 1.0) + 1.0) % 1.0
    if wrapped < 0.25:
        return QPointF(rect.left() + rect.width() * (wrapped / 0.25), rect.top())
    if wrapped < 0.5:
        return QPointF(rect.right(), rect.top() + rect.height() * ((wrapped - 0.25) / 0.25))
    if wrapped < 0.75:
        return QPointF(rect.right() - rect.width() * ((wrapped - 0.5) / 0.25), rect.bottom())
    return QPointF(rect.left(), rect.bottom() - rect.height() * ((wrapped - 0.75) / 0.25))


STYLE_ICON_CACHE: Dict[str, QIcon] = {}
"""特效色块图标缓存：树重建时会被反复用到，避免每次都重画 QPixmap。"""


def build_style_icon(style: str, size: int = 16) -> QIcon:
    """生成下拉框里的特效色块图标（带缓存）。"""
    cached = STYLE_ICON_CACHE.get(style)
    if cached is not None:
        return cached

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rect = QRectF(0.5, 0.5, size - 1, size - 1)

    if style == "rainbow":
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        for index in range(7):
            gradient.setColorAt(index / 6, hue_color(index / 6))
        painter.setBrush(QBrush(gradient))
        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.drawRoundedRect(rect, 3, 3)
    elif style == "flash":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#ff3b30")))
        painter.drawRoundedRect(QRectF(0.5, 0.5, (size - 1) / 2 + 1, size - 1), 3, 3)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRoundedRect(QRectF(size / 2 - 0.5, 0.5, (size - 1) / 2, size - 1), 3, 3)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawRoundedRect(rect, 3, 3)
    elif style == "pulse":
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, scale_hex_color("#34c759", 0.55))
        gradient.setColorAt(1.0, scale_hex_color("#34c759", 1.0))
        painter.setBrush(QBrush(gradient))
        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.drawRoundedRect(rect, 3, 3)
    elif style == "march":
        painter.setBrush(QBrush(QColor(PREVIEW_MARCH_COLORS[0])))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 3, 3)
        painter.setPen(QPen(QColor(PREVIEW_MARCH_COLORS[1]), 3))
        for offset in range(-size, size * 2, 6):
            painter.drawLine(QPointF(offset, size), QPointF(offset + size, 0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawRoundedRect(rect, 3, 3)
    elif style == "neon":
        painter.setBrush(QBrush(QColor("#0f172a")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 3, 3)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(34, 211, 238, 90), 4))
        painter.drawRoundedRect(rect.adjusted(2.5, 2.5, -2.5, -2.5), 2, 2)
        painter.setPen(QPen(QColor("#22d3ee"), 1.5))
        painter.drawRoundedRect(rect.adjusted(2.5, 2.5, -2.5, -2.5), 2, 2)
    elif style == "duotone":
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(PREVIEW_DUOTONE_COLORS[0]))
        gradient.setColorAt(1.0, QColor(PREVIEW_DUOTONE_COLORS[1]))
        painter.setBrush(QBrush(gradient))
        painter.setPen(QPen(QColor("#cbd5e1"), 1))
        painter.drawRoundedRect(rect, 3, 3)
    else:
        painter.setBrush(QBrush(QColor("#cbd5e1")))
        painter.setPen(QPen(QColor("#94a3b8"), 1))
        painter.drawRoundedRect(rect, 3, 3)

    painter.end()
    icon = QIcon(pixmap)
    STYLE_ICON_CACHE[style] = icon
    return icon


def daily_log_path() -> Path:
    return LOG_DIR / f"{time.strftime('%Y%m%d')}.log"


def append_daily_log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with daily_log_path().open("a", encoding="utf-8") as file:
        file.write(message)
        file.write("\n")

OCR_BACKEND_ONNXRUNTIME = "onnxruntime"
OCR_BACKEND_TENSORRT_FP32 = "tensorrt_fp32"
OCR_BACKEND_TENSORRT_FP16 = "tensorrt_fp16"
OCR_BACKEND_OPTIONS = (
    ("关闭 TensorRT（ONNX Runtime）", OCR_BACKEND_ONNXRUNTIME),
    ("TensorRT FP32（准确度优先）", OCR_BACKEND_TENSORRT_FP32),
    ("TensorRT FP16（速度优先）", OCR_BACKEND_TENSORRT_FP16),
)
OCR_BACKEND_VALUES = tuple(value for _, value in OCR_BACKEND_OPTIONS)


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


def deep_merge(defaults: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_dict(parent: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def set_combo_data(combo: QComboBox, value: str) -> None:
    for index in range(combo.count()):
        if combo.itemData(index) == value:
            combo.setCurrentIndex(index)
            return
    if combo.isEditable():
        combo.setCurrentText(value)


def bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def normalize_ocr_backend(value: Any) -> str:
    if isinstance(value, str) and value in OCR_BACKEND_VALUES:
        return value
    return OCR_BACKEND_ONNXRUNTIME


def parse_screen_region(text: str) -> Any:
    raw = text.strip()
    if not raw or raw.lower() == "auto":
        return "auto"

    if raw.startswith("{"):
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("screen_region JSON 必须是对象")
        return value

    parts = [part.strip() for part in raw.split(",")]
    if len(parts) == 4:
        left, top, width, height = [int(part) for part in parts]
        if width < 0 or height < 0:
            raise ValueError("screen_region 的 width/height 不能小于 0")
        return {"left": left, "top": top, "width": width, "height": height}

    raise ValueError('screen_region 请填写 auto、JSON 对象，或 "left,top,width,height"')


def format_screen_region(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "auto"


def read_tail_lines(path: Path, line_count: int = 5) -> str:
    if not path.exists():
        return "暂无日志"

    try:
        with path.open("rb") as file:
            file.seek(0, 2)
            size = file.tell()
            file.seek(max(0, size - 65536), 0)
            text = file.read().decode("utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip()]
        return "\n".join(lines[-line_count:]) if lines else "暂无日志"
    except Exception as exc:
        return f"读取日志失败：{exc}"


def build_obs_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode("utf-8")).digest()).decode("ascii")


async def obs_request(ws: Any, request_type: str, request_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    await ws.send(
        json.dumps(
            {
                "op": 6,
                "d": {
                    "requestType": request_type,
                    "requestId": request_id,
                    "requestData": request_data or {},
                },
            },
            ensure_ascii=False,
        )
    )
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if msg.get("op") == 7 and msg.get("d", {}).get("requestId") == request_id:
            response = msg["d"]
            status = response.get("requestStatus", {})
            if status and not status.get("result"):
                comment = status.get("comment") or status
                raise RuntimeError(f"{request_type} 请求失败：{comment}")
            return response


async def connect_obs(url: str, password: str) -> Any:
    import websockets

    ws = await asyncio.wait_for(
        websockets.connect(url, subprotocols=["obswebsocket.json"], max_size=None),
        timeout=5,
    )
    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        data = hello.get("d", {})
        identify: Dict[str, Any] = {
            "rpcVersion": min(int(data.get("rpcVersion", 1)), 1),
            "eventSubscriptions": 0,
        }
        auth = data.get("authentication")
        if auth:
            if not password:
                raise RuntimeError("OBS WebSocket 需要密码，请填写 password")
            identify["authentication"] = build_obs_auth(password, auth["salt"], auth["challenge"])

        await ws.send(json.dumps({"op": 1, "d": identify}, ensure_ascii=False))
        identified = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if identified.get("op") != 2:
            raise RuntimeError(f"OBS Identify 失败：{identified}")
        return ws
    except Exception:
        await ws.close()
        raise


async def run_obs_test(url: str, password: str) -> str:
    ws = await connect_obs(url, password)
    try:
        response = await obs_request(ws, "GetVersion")
        data = response.get("responseData", {})
        obs_version = data.get("obsVersion", "unknown")
        ws_version = data.get("obsWebSocketVersion", "unknown")
        return f"OBS 连接正常：OBS {obs_version} / WebSocket {ws_version}"
    finally:
        await ws.close()


async def run_obs_sources(url: str, password: str) -> Dict[str, Any]:
    ws = await connect_obs(url, password)
    try:
        scenes_response = await obs_request(ws, "GetSceneList")
        inputs_response = await obs_request(ws, "GetInputList")

        scenes_data = scenes_response.get("responseData", {})
        inputs_data = inputs_response.get("responseData", {})
        items: List[Dict[str, str]] = []

        for scene in scenes_data.get("scenes", []):
            items.append(
                {
                    "kind": "场景",
                    "name": str(scene.get("sceneName", "")),
                    "type": "scene",
                    "uuid": str(scene.get("sceneUuid", "")),
                }
            )

        for item in inputs_data.get("inputs", []):
            items.append(
                {
                    "kind": "输入源",
                    "name": str(item.get("inputName", "")),
                    "type": str(item.get("inputKind", "")),
                    "uuid": str(item.get("inputUuid", "")),
                }
            )

        return {
            "current_scene": str(scenes_data.get("currentProgramSceneName", "")),
            "items": items,
        }
    finally:
        await ws.close()


def obs_error_message(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "连接超时，请确认 OBS 已启动并开启 WebSocket。"
    if "connect call failed" in lowered or "connection refused" in lowered:
        return "连接失败，请确认 OBS WebSocket 地址和端口正确。"
    if "authentication" in lowered or "password" in lowered or "4009" in lowered:
        return "认证失败，请检查 OBS WebSocket 密码。"
    return text


class OBSTaskThread(QThread):
    succeeded = Signal(str, object)
    failed = Signal(str)

    def __init__(self, action: str, url: str, password: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.action = action
        self.url = url
        self.password = password

    def run(self) -> None:
        try:
            if self.action == "test":
                result = asyncio.run(run_obs_test(self.url, self.password))
            else:
                result = asyncio.run(run_obs_sources(self.url, self.password))
            self.succeeded.emit(self.action, result)
        except Exception as exc:
            self.failed.emit(obs_error_message(exc))


class SourcePickerDialog(QDialog):
    def __init__(self, payload: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择 OBS 捕获对象")
        self.resize(760, 460)
        self.items: List[Dict[str, str]] = list(payload.get("items", []))
        self.selected_item: Optional[Dict[str, str]] = None

        layout = QVBoxLayout(self)
        current_scene = payload.get("current_scene") or "未知"
        intro = QLabel(f"当前节目场景：{current_scene}。选择后会同时回填 source_name 和 source_uuid。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["类型", "名称", "子类型", "UUID"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.table, 1)

        for item in self.items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [item.get("kind", ""), item.get("name", ""), item.get("type", ""), item.get("uuid", "")]
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setData(Qt.ItemDataRole.UserRole, item)
                self.table.setItem(row, column, table_item)

        if self.table.rowCount() > 0:
            self.table.selectRow(0)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "未选择", "请先选择一个 OBS 场景或输入源。")
            return
        item = self.table.item(row, 0)
        self.selected_item = item.data(Qt.ItemDataRole.UserRole) if item else None
        super().accept()


class StyleHelpDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("分组特效说明")
        self.resize(780, 420)
        self.setMinimumSize(660, 340)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "左侧列表里每个分组右边的下拉框就是特效开关，选中即写入 name.txt 的 “# 分组名#X” 后缀，"
            "该分组下的目标全部使用对应特效，标签仍显示“分组：目标 - 百分比”。\n"
            "后缀大小写不敏感；不带后缀的分组保持默认自动分色；同一目标重复出现时，"
            "以第一次出现的分组和特效为准。OBS 浏览器源与桌面透明层均已支持。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        table = QTableWidget(len(GROUP_STYLE_HELP), 3, self)
        table.setHorizontalHeaderLabels(["后缀", "特效", "效果"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, values in enumerate(GROUP_STYLE_HELP):
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))
        table.resizeRowsToContents()
        layout.addWidget(table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class NameTreeDelegate(QStyledItemDelegate):
    """只允许第 0 列（分组名 / 目标名）进入编辑状态，特效列和备注列不可编辑。"""

    def createEditor(self, parent: QWidget, option: Any, index: Any) -> Optional[QWidget]:
        if index.column() != 0:
            return None
        return super().createEditor(parent, option, index)


class EffectPreviewWidget(QWidget):
    """在 GUI 里近似复现分组特效：边框 + 标签，参数与 overlay.html / 桌面层保持一致。

    只是选特效时的直观参考，真正的渲染仍然由 worker 负责，这里不参与识别流程。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._style = ""
        self._label = "示例目标 - 92%"
        self._color = "#ff3b30"
        self._line_width = 3
        self._started_at = time.monotonic()

        self.setFixedSize(420, 56)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._timer = QTimer(self)
        self._timer.setInterval(PREVIEW_ANIM_INTERVAL_MS)
        self._timer.timeout.connect(self.update)
        self._sync_timer()

    def set_effect(
        self,
        style: str,
        label: Optional[str] = None,
        color: Optional[str] = None,
        line_width: Optional[int] = None,
    ) -> None:
        self._style = style if style in name_store.ANIMATED_STYLES else ""
        if label is not None:
            self._label = label
        if color:
            self._color = color
        if line_width:
            self._line_width = max(1, min(12, int(line_width)))
        self._started_at = time.monotonic()
        self._sync_timer()
        self.update()

    def _sync_timer(self) -> None:
        if self._style in name_store.ANIMATED_STYLES:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        now = time.monotonic() - self._started_at
        box = QRectF(10, PREVIEW_LABEL_HEIGHT + 6, max(60, self.width() - 20), max(14, self.height() - PREVIEW_LABEL_HEIGHT - 14))

        label_background = QColor(self._color)
        label_foreground = QColor("#ffffff")
        rainbow_label = False
        line_width = self._line_width

        if self._style == "rainbow":
            segments = 96
            for index in range(segments):
                position = index / segments
                painter.setPen(QPen(hue_color(position + now * PREVIEW_RAINBOW_SPEED), line_width))
                painter.drawLine(perimeter_point(box, position), perimeter_point(box, (index + 1) / segments))
            # 桌面透明层：标签是彩虹底色 + 白字黑描边（浏览器层用的是深色底，这里跟桌面层对齐）。
            rainbow_label = True
        elif self._style == "flash":
            flash_on = int(now * PREVIEW_FLASH_HZ * 2) % 2 == 0
            color = QColor("#ff3b30") if flash_on else QColor("#ffffff")
            painter.setPen(QPen(color, line_width))
            painter.drawRect(box)
            label_background = color
            label_foreground = QColor("#ffffff") if flash_on else QColor("#000000")
        elif self._style == "pulse":
            factor = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(now * 2 * math.pi / PREVIEW_PULSE_PERIOD))
            color = scale_hex_color(self._color, factor)
            painter.setPen(QPen(color, line_width))
            painter.drawRect(box)
            label_background = color
        elif self._style == "march":
            perimeter = max(1.0, 2 * (box.width() + box.height()))
            start = -((now * PREVIEW_MARCH_SPEED) % (PREVIEW_MARCH_SEGMENT * 2))
            count = int(perimeter // PREVIEW_MARCH_SEGMENT) + 3
            for index in range(count):
                segment_start = start + index * PREVIEW_MARCH_SEGMENT
                segment_end = segment_start + PREVIEW_MARCH_SEGMENT
                if segment_end <= 0 or segment_start >= perimeter:
                    continue
                low = max(0.0, segment_start) / perimeter
                high = min(perimeter, segment_end) / perimeter
                painter.setPen(
                    QPen(
                        QColor(PREVIEW_MARCH_COLORS[index % 2]),
                        line_width,
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.FlatCap,
                    )
                )
                painter.drawLine(perimeter_point(box, low), perimeter_point(box, high))
            label_background = QColor(PREVIEW_MARCH_COLORS[0])
            label_foreground = QColor(PREVIEW_MARCH_COLORS[1])
        elif self._style == "neon":
            breath = 0.5 + 0.5 * math.sin(now * 2 * math.pi / PREVIEW_NEON_PERIOD)
            main_factor = 0.70 + 0.30 * breath
            for width_scale, brightness in ((3, 0.30), (2, 0.55), (1, main_factor)):
                painter.setPen(QPen(scale_hex_color(self._color, brightness), max(1, line_width * width_scale)))
                painter.drawRect(box)
            label_background = QColor(self._color)
        elif self._style == "duotone":
            steps = 64
            offset = now * PREVIEW_DUOTONE_SPEED
            for index in range(steps):
                position = (index / steps + offset) % 1.0
                triangle = position * 2 if position < 0.5 else (1.0 - position) * 2
                painter.setPen(
                    QPen(
                        mix_hex_color(PREVIEW_DUOTONE_COLORS[0], PREVIEW_DUOTONE_COLORS[1], triangle),
                        line_width,
                    )
                )
                painter.drawLine(perimeter_point(box, index / steps), perimeter_point(box, (index + 1) / steps))
            label_background = QColor(PREVIEW_DUOTONE_COLORS[0])
        else:
            painter.setPen(QPen(QColor(self._color), line_width))
            painter.drawRect(box)

        self._draw_label(painter, box, label_background, label_foreground, rainbow_label, now)
        painter.end()

    def _draw_label(
        self,
        painter: QPainter,
        box: QRectF,
        background: QColor,
        foreground: QColor,
        rainbow: bool,
        now: float,
    ) -> None:
        """标签宽度按文字宽度算（和桌面层一致：文字宽 + 10px），不是整行铺满。"""
        font = QFont("Microsoft YaHei", 8)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        padding = 5
        text_width = metrics.horizontalAdvance(self._label)
        label_width = max(24, min(int(box.width()), text_width + padding * 2))
        label_rect = QRectF(box.left(), max(0.0, box.top() - PREVIEW_LABEL_HEIGHT), label_width, PREVIEW_LABEL_HEIGHT)

        if rainbow:
            segments = max(8, int(label_rect.width() // 3))
            for index in range(segments):
                position = index / segments
                strip = QRectF(
                    label_rect.left() + label_rect.width() * position,
                    label_rect.top(),
                    label_rect.width() / segments + 1,
                    label_rect.height(),
                )
                painter.fillRect(strip, hue_color(position + now * PREVIEW_RAINBOW_SPEED))
        else:
            painter.fillRect(label_rect, background)

        text = metrics.elidedText(self._label, Qt.TextElideMode.ElideRight, max(20, int(label_rect.width()) - padding * 2))
        text_rect = label_rect.adjusted(padding, 0, -padding, 0)
        flags = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        if rainbow:
            painter.setPen(QPen(QColor("#000000")))
            for offset_x, offset_y in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
                painter.drawText(text_rect.translated(offset_x, offset_y), flags, text)
            painter.setPen(QPen(QColor("#ffffff")))
        else:
            painter.setPen(QPen(foreground))
        painter.drawText(text_rect, flags, text)


class AddTargetsDialog(QDialog):
    """批量添加目标：选择分组 + 一次粘贴多行。"""

    def __init__(self, group_labels: List[str], group_indices: List[int], current_index: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("添加识别目标")
        self.resize(520, 420)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.group_combo = QComboBox(self)
        for label, index in zip(group_labels, group_indices):
            self.group_combo.addItem(label, index)
        if current_index in group_indices:
            self.group_combo.setCurrentIndex(group_indices.index(current_index))
        form.addRow("添加到分组", self.group_combo)
        layout.addLayout(form)

        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setFont(QFont("Consolas", 10))
        self.text_edit.setPlaceholderText("每行一个目标文字，可一次粘贴多行")
        layout.addWidget(self.text_edit, 1)

        self.skip_existing_check = QCheckBox("跳过该分组中已存在的目标（不区分大小写）", self)
        self.skip_existing_check.setChecked(True)
        layout.addWidget(self.skip_existing_check)

        hint = QLabel("空行会被忽略；以 # 开头的行会被忽略（那是分组表头）。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("添加")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_group_index(self) -> int:
        data = self.group_combo.currentData()
        return int(data) if data is not None else -1

    def input_text(self) -> str:
        return self.text_edit.toPlainText()

    def skip_existing(self) -> bool:
        return self.skip_existing_check.isChecked()


class NameSourceDialog(QDialog):
    """name.txt 源码模式：直接看/改文本，应用后按 worker 的规则重新解析。"""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("name.txt 源码")
        self.resize(560, 520)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "这里是 name.txt 的真实内容（未保存时显示的是界面里的最新版本）。"
            "改完点“应用到列表”会按 worker 的解析规则重新载入，不会立刻写盘。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.editor = QPlainTextEdit(self)
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.setPlainText(text)
        layout.addWidget(self.editor, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用到列表")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def source_text(self) -> str:
        return self.editor.toPlainText()


class DuplicateTargetsDialog(QDialog):
    """重复目标报告：worker 只按第一次出现的位置生效，这里给出清理入口。"""

    def __init__(self, name_list: name_store.NameList, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("重复目标")
        self.cleanup_requested = False
        duplicates = name_list.duplicate_locations()

        self.resize(620, 420)
        layout = QVBoxLayout(self)
        hint = QLabel(
            f"发现 {len(duplicates)} 个目标重复出现，共 {name_list.duplicate_extra_total()} 条多余记录。"
            "worker 匹配时只认第一次出现的位置，后面的重复项不会生效，建议清理。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(len(duplicates), 3, self)
        self.table.setHorizontalHeaderLabels(["目标", "生效分组", "其它位置"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        for row, (key, locations) in enumerate(duplicates.items()):
            first_group = name_list.groups[locations[0][0]].label if locations[0][0] < len(name_list.groups) else ""
            other_groups = "、".join(
                name_list.groups[group_index].label
                for group_index, _ in locations[1:]
                if group_index < len(name_list.groups)
            )
            values = [key, f"{first_group}（第 1 次出现）", other_groups]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        cleanup_button = buttons.addButton("删除后出现的重复项", QDialogButtonBox.ButtonRole.AcceptRole)
        cleanup_button.clicked.connect(self._request_cleanup)
        layout.addWidget(buttons)

    def _request_cleanup(self) -> None:
        self.cleanup_requested = True
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"OBS Name OCR 控制台 {APP_VERSION}")
        self.resize(1180, 820)

        self.config: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.worker_process: Optional[subprocess.Popen[Any]] = None
        self.worker_output_file: Optional[Any] = None
        self.obs_thread: Optional[OBSTaskThread] = None

        self.name_list = name_store.NameList()
        self.name_dirty = False
        self._name_rendering = False
        self._toc_syncing = False

        self._build_ui()
        self._apply_style()
        self.load_name_file()
        self.load_config_file()
        self.refresh_log()
        self.update_worker_state()

        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.refresh_log)
        self.log_timer.start(1000)

        self.worker_timer = QTimer(self)
        self.worker_timer.timeout.connect(self.update_worker_state)
        self.worker_timer.start(1000)

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        self.setCentralWidget(central)

        root.addWidget(self._build_toolbar())

        # 配置面板默认收进弹窗，整页高度都留给目标列表和日志；两栏之间可以拖动分配。
        body = QSplitter(Qt.Orientation.Vertical, self)
        body.setChildrenCollapsible(False)
        body.addWidget(self._build_name_panel())
        body.addWidget(self._build_log_panel())
        body.setStretchFactor(0, 8)
        body.setStretchFactor(1, 2)
        body.setSizes([720, 190])
        self.body_splitter = body
        root.addWidget(body, 1)

        # 先建好（不显示），这样 load_config_file() 回填表单时控件都已存在。
        self.config_dialog = self._build_config_dialog()

    def _build_toolbar(self) -> QWidget:
        toolbar = QFrame(self)
        toolbar.setObjectName("toolbar")
        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        title = QLabel("OCR 控制台")
        title.setObjectName("appTitle")
        layout.addWidget(title)

        version_label = QLabel(f"版本 {APP_VERSION}", self)
        version_label.setObjectName("hint")
        layout.addWidget(version_label)

        copyright_label = QLabel(
            '版权所有 <a href="https://www.siver.top" style="color:#2563eb;">www.siver.top</a>',
            self,
        )
        copyright_label.setObjectName("hint")
        copyright_label.setOpenExternalLinks(True)
        copyright_label.setToolTip("https://www.siver.top")
        layout.addWidget(copyright_label)
        layout.addStretch(1)

        self.config_button = QPushButton("配置 config.json")
        self.config_button.setObjectName("neutralButton")
        self.config_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.config_button.setToolTip("打开配置窗口（config.json）")
        self.config_button.clicked.connect(self.show_config_dialog)
        layout.addWidget(self.config_button)

        self.start_button = QPushButton("启动 worker")
        self.start_button.setObjectName("successButton")
        self.start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.start_button.clicked.connect(self.start_worker)
        layout.addWidget(self.start_button)

        self.stop_button = QPushButton("停止 worker")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_button.clicked.connect(self.stop_worker)
        layout.addWidget(self.stop_button)

        self.worker_status_label = QLabel("状态：未运行")
        self.worker_status_label.setObjectName("statusBadge")
        layout.addWidget(self.worker_status_label)

        return toolbar

    def _build_name_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.name_title_label = QLabel("目标文字 name.txt")
        self.name_title_label.setObjectName("sectionTitle")
        self.name_title_label.setToolTip("双击列表项可改名；特效按分组生效；保存后 worker 会在下一次热重载自动应用。")
        header.addWidget(self.name_title_label)
        header.addStretch(1)
        self.name_summary_label = QLabel("")
        self.name_summary_label.setObjectName("hint")
        header.addWidget(self.name_summary_label)
        layout.addLayout(header)

        self.name_search_edit = QLineEdit(self)
        self.name_search_edit.setPlaceholderText("搜索目标文字")
        self.name_search_edit.setClearButtonEnabled(True)
        self.name_search_edit.textChanged.connect(self.filter_name_tree)
        layout.addWidget(self.name_search_edit)

        self.name_tree = QTreeWidget(self)
        self.name_tree.setColumnCount(3)
        self.name_tree.setHeaderLabels(["分组 / 目标", "特效", "备注"])
        self.name_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.name_tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.name_tree.setExpandsOnDoubleClick(False)
        self.name_tree.setUniformRowHeights(False)
        self.name_tree.setItemDelegate(NameTreeDelegate(self.name_tree))
        name_header = self.name_tree.header()
        name_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        name_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        name_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.name_tree.setColumnWidth(1, 150)
        self.name_tree.setColumnWidth(2, 86)
        self.name_tree.itemChanged.connect(self._on_name_item_changed)
        self.name_tree.itemSelectionChanged.connect(self._on_name_selection_changed)

        # 左侧分组目录：只列分组，点一下跳到对应分组。
        toc_column = QWidget(self)
        toc_layout = QVBoxLayout(toc_column)
        toc_layout.setContentsMargins(0, 0, 0, 0)
        toc_layout.setSpacing(4)
        toc_title = QLabel("分组目录")
        toc_title.setObjectName("hint")
        toc_layout.addWidget(toc_title)

        self.name_toc = QListWidget(toc_column)
        self.name_toc.setObjectName("nameToc")
        self.name_toc.setMinimumWidth(120)
        self.name_toc.setToolTip("点一下分组名就能跳到列表里对应的位置")
        self.name_toc.currentRowChanged.connect(self._on_name_toc_row_changed)
        toc_layout.addWidget(self.name_toc, 1)

        self.name_list_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.name_list_splitter.setChildrenCollapsible(False)
        self.name_list_splitter.addWidget(toc_column)
        self.name_list_splitter.addWidget(self.name_tree)
        self.name_list_splitter.setStretchFactor(0, 0)
        self.name_list_splitter.setStretchFactor(1, 1)
        self.name_list_splitter.setSizes([200, 1200])
        layout.addWidget(self.name_list_splitter, 1)

        # 预览和按钮挤在一行里，把纵向空间尽量留给列表。
        preview_row = QHBoxLayout()
        preview_row.setSpacing(10)
        self.name_preview_caption = QLabel("效果预览：默认自动分色")
        self.name_preview_caption.setObjectName("hint")
        self.name_preview_caption.setWordWrap(True)
        preview_row.addWidget(self.name_preview_caption, 1)
        self.name_preview = EffectPreviewWidget(self)
        preview_row.addWidget(self.name_preview)
        layout.addLayout(preview_row)

        ops_row = QHBoxLayout()
        ops_row.setSpacing(6)
        self.add_group_button = QPushButton("＋分组")
        self.add_group_button.setObjectName("neutralButton")
        self.add_group_button.setToolTip("新建一个分组（写成 “# 分组名” 表头）")
        self.add_group_button.clicked.connect(self.add_name_group)
        ops_row.addWidget(self.add_group_button)

        self.add_target_button = QPushButton("＋目标")
        self.add_target_button.setObjectName("neutralButton")
        self.add_target_button.setToolTip("向选中的分组添加目标，可一次粘贴多行")
        self.add_target_button.clicked.connect(self.add_name_targets)
        ops_row.addWidget(self.add_target_button)

        self.rename_name_button = QPushButton("改名")
        self.rename_name_button.setObjectName("neutralButton")
        self.rename_name_button.setToolTip("重命名选中的分组或目标（也可以直接双击列表项）")
        self.rename_name_button.clicked.connect(self.rename_name_selection)
        ops_row.addWidget(self.rename_name_button)

        self.delete_name_button = QPushButton("删除")
        self.delete_name_button.setObjectName("dangerButton")
        self.delete_name_button.setToolTip("删除选中的分组（连同组内目标）或目标")
        self.delete_name_button.clicked.connect(self.delete_name_selection)
        ops_row.addWidget(self.delete_name_button)

        self.name_move_up_button = QPushButton("上移")
        self.name_move_up_button.setObjectName("neutralButton")
        self.name_move_up_button.setToolTip("把选中的分组或目标往上移一位")
        self.name_move_up_button.clicked.connect(lambda: self.move_name_selection(-1))
        ops_row.addWidget(self.name_move_up_button)

        self.name_move_down_button = QPushButton("下移")
        self.name_move_down_button.setObjectName("neutralButton")
        self.name_move_down_button.setToolTip("把选中的分组或目标往下移一位")
        self.name_move_down_button.clicked.connect(lambda: self.move_name_selection(1))
        ops_row.addWidget(self.name_move_down_button)

        self.name_move_button = QPushButton("移动到…")
        self.name_move_button.setObjectName("neutralButton")
        self.name_move_button.setToolTip("把选中的目标移动到另一个分组，可选放到该分组顶部或末尾")
        self.name_move_button.clicked.connect(self.move_name_targets_to_group)
        ops_row.addWidget(self.name_move_button)

        self.name_duplicate_button = QPushButton("查重")
        self.name_duplicate_button.setObjectName("neutralButton")
        self.name_duplicate_button.setToolTip("检查重复目标并一键清理")
        self.name_duplicate_button.clicked.connect(self.show_duplicate_report)
        ops_row.addWidget(self.name_duplicate_button)

        ops_row.addStretch(1)

        self.style_help_button = QPushButton("特效说明")
        self.style_help_button.setObjectName("neutralButton")
        self.style_help_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.style_help_button.clicked.connect(self.show_style_help)
        ops_row.addWidget(self.style_help_button)

        self.name_source_button = QPushButton("源码")
        self.name_source_button.setObjectName("neutralButton")
        self.name_source_button.setToolTip("以纯文本方式查看 / 编辑 name.txt 内容")
        self.name_source_button.clicked.connect(self.open_name_source_dialog)
        ops_row.addWidget(self.name_source_button)

        self.save_name_button = QPushButton("保存")
        self.save_name_button.setObjectName("primaryButton")
        self.save_name_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self.save_name_button.setToolTip("保存 name.txt（原子写入，worker 不会读到半截内容）")
        self.save_name_button.clicked.connect(self.save_name_file)
        ops_row.addWidget(self.save_name_button)

        self.reload_name_button = QPushButton("重载")
        self.reload_name_button.setObjectName("neutralButton")
        self.reload_name_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.reload_name_button.setToolTip("丢弃界面上的修改，重新从磁盘读取 name.txt")
        self.reload_name_button.clicked.connect(self.load_name_file)
        ops_row.addWidget(self.reload_name_button)
        layout.addLayout(ops_row)

        return panel

    def _build_config_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("配置管理 config.json")
        dialog.resize(760, 780)
        dialog.setMinimumSize(620, 480)

        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.setContentsMargins(16, 16, 16, 16)
        dialog_layout.setSpacing(10)

        hint = QLabel("配置调试好之后一般不用再改；保存后 worker 会在下一次热重载自动应用。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        dialog_layout.addWidget(hint)

        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_container = QWidget(scroll)
        self.config_layout = QVBoxLayout(form_container)
        self.config_layout.setContentsMargins(0, 0, 0, 0)
        self.config_layout.setSpacing(12)

        self._add_service_group()
        self._add_capture_group()
        self._add_match_group()
        self._add_match_tolerance_group()
        self._add_debug_output_group()
        self._add_ocr_group()
        self._add_overlay_group()
        self.config_layout.addStretch(1)

        scroll.setWidget(form_container)
        dialog_layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.save_config_button = QPushButton("保存配置")
        self.save_config_button.setObjectName("primaryButton")
        self.save_config_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self.save_config_button.clicked.connect(self.save_config_file)
        buttons.addWidget(self.save_config_button)

        self.reload_config_button = QPushButton("重载配置")
        self.reload_config_button.setObjectName("neutralButton")
        self.reload_config_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.reload_config_button.clicked.connect(self.load_config_file)
        buttons.addWidget(self.reload_config_button)

        close_button = QPushButton("关闭")
        close_button.setObjectName("neutralButton")
        close_button.clicked.connect(dialog.hide)
        buttons.addWidget(close_button)
        dialog_layout.addLayout(buttons)

        return dialog

    def show_config_dialog(self) -> None:
        self.config_dialog.show()
        self.config_dialog.raise_()
        self.config_dialog.activateWindow()

    def _build_log_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("logPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("最近 5 行日志")
        title.setObjectName("sectionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.log_updated_label = QLabel("未刷新")
        self.log_updated_label.setObjectName("hint")
        header.addWidget(self.log_updated_label)
        layout.addLayout(header)

        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(8)
        self.log_view.setMinimumHeight(72)
        self.log_view.setFont(QFont("Consolas", 10))
        layout.addWidget(self.log_view)

        return panel

    def _group(self, title: str) -> QGroupBox:
        group = QGroupBox(title, self)
        group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        return group

    def _form(self, group: QGroupBox) -> QFormLayout:
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        return form

    def _add_service_group(self) -> None:
        group = self._group("基础服务")
        form = self._form(group)

        self.interval_spin = QSpinBox(self)
        self.interval_spin.setRange(100, 3_600_000)
        self.interval_spin.setSingleStep(100)
        self.interval_spin.setSuffix(" ms")
        form.addRow("识别间隔 interval_ms", self.interval_spin)

        self.host_edit = QLineEdit(self)
        form.addRow("监听地址 host", self.host_edit)

        self.port_spin = QSpinBox(self)
        self.port_spin.setRange(1, 65535)
        form.addRow("端口 port", self.port_spin)

        self.config_layout.addWidget(group)

    def _add_capture_group(self) -> None:
        group = self._group("截图来源")
        layout = QVBoxLayout(group)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        self.capture_source_combo = QComboBox(self)
        self.capture_source_combo.addItem("screen", "screen")
        self.capture_source_combo.addItem("obs_websocket", "obs_websocket")
        self.capture_source_combo.currentIndexChanged.connect(self.update_obs_controls)
        form.addRow("来源选择 source", self.capture_source_combo)

        self.monitor_spin = QSpinBox(self)
        self.monitor_spin.setRange(0, 32)
        form.addRow("显示器 monitor", self.monitor_spin)

        region_widget = QWidget(self)
        region_layout = QGridLayout(region_widget)
        region_layout.setContentsMargins(0, 0, 0, 0)
        region_layout.setHorizontalSpacing(8)
        region_layout.setVerticalSpacing(8)
        self.left_spin = self._region_spin(-100_000, 100_000)
        self.top_spin = self._region_spin(-100_000, 100_000)
        self.width_spin = self._region_spin(0, 100_000)
        self.height_spin = self._region_spin(0, 100_000)
        region_layout.addWidget(QLabel("left"), 0, 0)
        region_layout.addWidget(self.left_spin, 0, 1)
        region_layout.addWidget(QLabel("top"), 0, 2)
        region_layout.addWidget(self.top_spin, 0, 3)
        region_layout.addWidget(QLabel("width"), 1, 0)
        region_layout.addWidget(self.width_spin, 1, 1)
        region_layout.addWidget(QLabel("height"), 1, 2)
        region_layout.addWidget(self.height_spin, 1, 3)
        form.addRow("屏幕区域", region_widget)

        self.obs_url_edit = QLineEdit(self)
        form.addRow("OBS WebSocket url", self.obs_url_edit)

        self.obs_password_edit = QLineEdit(self)
        self.obs_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.obs_password_edit.setPlaceholderText("无密码则留空")
        form.addRow("OBS password", self.obs_password_edit)

        self.obs_source_name_edit = QLineEdit(self)
        form.addRow("source_name", self.obs_source_name_edit)

        self.obs_source_uuid_edit = QLineEdit(self)
        form.addRow("source_uuid", self.obs_source_uuid_edit)

        self.image_format_combo = QComboBox(self)
        self.image_format_combo.setEditable(True)
        self.image_format_combo.addItem("png", "png")
        self.image_format_combo.addItem("jpg", "jpg")
        self.image_format_combo.addItem("jpeg", "jpeg")
        form.addRow("image_format", self.image_format_combo)

        image_size_widget = QWidget(self)
        image_size_layout = QHBoxLayout(image_size_widget)
        image_size_layout.setContentsMargins(0, 0, 0, 0)
        image_size_layout.setSpacing(8)
        self.image_width_spin = self._region_spin(0, 100_000)
        self.image_height_spin = self._region_spin(0, 100_000)
        image_size_layout.addWidget(QLabel("宽"))
        image_size_layout.addWidget(self.image_width_spin)
        image_size_layout.addWidget(QLabel("高"))
        image_size_layout.addWidget(self.image_height_spin)
        form.addRow("image_width / image_height", image_size_widget)

        self.image_quality_spin = QSpinBox(self)
        self.image_quality_spin.setRange(0, 100)
        form.addRow("image_compression_quality", self.image_quality_spin)
        layout.addLayout(form)

        hint = QLabel("source_uuid 优先级高于 source_name。OBS 相关按钮仅在来源为 obs_websocket 时启用。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        self.test_obs_button = QPushButton("测试 OBS 连接")
        self.test_obs_button.setObjectName("neutralButton")
        self.test_obs_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.test_obs_button.clicked.connect(self.test_obs_connection)
        button_layout.addWidget(self.test_obs_button)

        self.fetch_obs_button = QPushButton("获取 OBS 捕获对象")
        self.fetch_obs_button.setObjectName("neutralButton")
        self.fetch_obs_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.fetch_obs_button.clicked.connect(self.fetch_obs_sources)
        button_layout.addWidget(self.fetch_obs_button)
        layout.addLayout(button_layout)

        self.config_layout.addWidget(group)

    def _add_match_group(self) -> None:
        group = self._group("匹配规则")
        form = self._form(group)

        self.match_mode_combo = QComboBox(self)
        self.match_mode_combo.addItem("包含匹配", "contains")
        self.match_mode_combo.addItem("完全匹配", "exact")
        form.addRow("mode", self.match_mode_combo)

        self.case_sensitive_check = QCheckBox("区分大小写", self)
        form.addRow("case_sensitive", self.case_sensitive_check)

        self.min_confidence_spin = QDoubleSpinBox(self)
        self.min_confidence_spin.setRange(0.0, 1.0)
        self.min_confidence_spin.setSingleStep(0.05)
        self.min_confidence_spin.setDecimals(2)
        form.addRow("min_confidence", self.min_confidence_spin)

        self.config_layout.addWidget(group)

    def _add_match_tolerance_group(self) -> None:
        group = self._group("匹配容错 match_tolerance")
        form = self._form(group)

        self.match_tolerance_enabled_check = QCheckBox("启用容错匹配", self)
        form.addRow("enabled", self.match_tolerance_enabled_check)

        self.normalize_confusable_check = QCheckBox("兼容 1/l/I、0/O、5/S 等易混字符", self)
        form.addRow("normalize_confusable", self.normalize_confusable_check)

        self.collapse_repeated_chars_check = QCheckBox("压缩连续重复字符，例如 kk -> k", self)
        form.addRow("collapse_repeated_chars", self.collapse_repeated_chars_check)

        self.ignore_separators_check = QCheckBox("忽略 _、-、空格等分隔符", self)
        form.addRow("ignore_separators", self.ignore_separators_check)

        self.max_edit_distance_spin = QSpinBox(self)
        self.max_edit_distance_spin.setRange(0, 8)
        form.addRow("max_edit_distance", self.max_edit_distance_spin)

        self.fuzzy_enabled_check = QCheckBox("启用相似度匹配", self)
        form.addRow("fuzzy_enabled", self.fuzzy_enabled_check)

        self.fuzzy_threshold_spin = QDoubleSpinBox(self)
        self.fuzzy_threshold_spin.setRange(0.0, 1.0)
        self.fuzzy_threshold_spin.setSingleStep(0.01)
        self.fuzzy_threshold_spin.setDecimals(2)
        form.addRow("fuzzy_threshold", self.fuzzy_threshold_spin)

        self.fuzzy_min_length_spin = QSpinBox(self)
        self.fuzzy_min_length_spin.setRange(1, 128)
        form.addRow("fuzzy_min_length", self.fuzzy_min_length_spin)

        self.config_layout.addWidget(group)

    def _add_debug_output_group(self) -> None:
        group = self._group("诊断输出 ocr_output")
        form = self._form(group)

        self.ocr_output_enabled_check = QCheckBox("输出 ocr_output.txt", self)
        form.addRow("enabled", self.ocr_output_enabled_check)

        hint = QLabel("开启后每轮覆盖写入最近一次 OCR 原始识别内容，用于排查漏识别。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        form.addRow("", hint)

        self.config_layout.addWidget(group)

    def _add_ocr_group(self) -> None:
        group = self._group("OCR 设置")
        form = self._form(group)

        self.backend_combo = QComboBox(self)
        for label, value in OCR_BACKEND_OPTIONS:
            self.backend_combo.addItem(label, value)
        form.addRow("backend", self.backend_combo)

        self.use_cuda_check = QCheckBox("启用 CUDA", self)
        form.addRow("use_cuda", self.use_cuda_check)

        self.use_dml_check = QCheckBox("启用 DirectML", self)
        form.addRow("use_dml", self.use_dml_check)

        self.backend_combo.currentIndexChanged.connect(
            self.update_ocr_backend_controls
        )

        backend_hint = QLabel(
            "TensorRT 仅适用于 NVIDIA GPU。首次初始化会编译并缓存 Engine，"
            "可能耗时数分钟。FP32 与当前结果最接近；FP16 更快，但阈值附近"
            "可能出现微小浮点差异。"
        )
        backend_hint.setObjectName("hint")
        backend_hint.setWordWrap(True)
        form.addRow("", backend_hint)

        self.use_cls_check = QCheckBox("启用方向分类", self)
        form.addRow("use_cls", self.use_cls_check)

        self.reload_files_spin = QSpinBox(self)
        self.reload_files_spin.setRange(200, 3_600_000)
        self.reload_files_spin.setSingleStep(100)
        self.reload_files_spin.setSuffix(" ms")
        form.addRow("reload_files_interval_ms", self.reload_files_spin)

        self.log_performance_check = QCheckBox("记录性能日志", self)
        form.addRow("log_performance", self.log_performance_check)

        self.config_layout.addWidget(group)

    def _add_overlay_group(self) -> None:
        group = self._group("叠加层设置")
        form = self._form(group)

        color_widget = QWidget(self)
        color_layout = QHBoxLayout(color_widget)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(8)
        self.stroke_color_edit = QLineEdit(self)
        self.stroke_color_edit.setPlaceholderText("#ff3b30")
        color_layout.addWidget(self.stroke_color_edit, 1)
        color_button = QPushButton("选择")
        color_button.setObjectName("neutralButton")
        color_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        color_button.clicked.connect(self.choose_stroke_color)
        color_layout.addWidget(color_button)
        form.addRow("stroke_color", color_widget)

        self.color_mode_combo = QComboBox(self)
        self.color_mode_combo.addItem("单色", "single")
        self.color_mode_combo.addItem("按目标动态分色", "by_target")
        form.addRow("color_mode", self.color_mode_combo)

        self.line_width_spin = QSpinBox(self)
        self.line_width_spin.setRange(1, 64)
        form.addRow("line_width", self.line_width_spin)

        self.show_label_check = QCheckBox("显示标签", self)
        form.addRow("show_label", self.show_label_check)

        self.desktop_overlay_enabled_check = QCheckBox("启用桌面透明覆盖层", self)
        form.addRow("desktop_overlay.enabled", self.desktop_overlay_enabled_check)

        self.coordinate_mode_combo = QComboBox(self)
        self.coordinate_mode_combo.addItem("capture", "capture")
        self.coordinate_mode_combo.addItem("screen_region", "screen_region")
        form.addRow("coordinate_mode", self.coordinate_mode_combo)

        self.screen_region_edit = QLineEdit(self)
        self.screen_region_edit.setPlaceholderText('auto 或 {"left":0,"top":0,"width":1920,"height":1080}')
        form.addRow("screen_region", self.screen_region_edit)

        self.config_layout.addWidget(group)

    def _region_spin(self, minimum: int, maximum: int) -> QSpinBox:
        spin = QSpinBox(self)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(10)
        return spin

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f5f7fb;
                color: #1f2937;
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
            }
            QFrame#toolbar, QFrame#panel, QFrame#logPanel {
                background: #ffffff;
                border: 1px solid #d9e0ea;
                border-radius: 8px;
            }
            QLabel#appTitle {
                font-size: 18px;
                font-weight: 700;
                color: #111827;
            }
            QLabel#sectionTitle {
                font-size: 15px;
                font-weight: 700;
                color: #111827;
            }
            QLabel#hint {
                color: #64748b;
                font-size: 12px;
            }
            QLabel#statusBadge {
                border-radius: 999px;
                padding: 6px 12px;
                background: #e5e7eb;
                color: #374151;
                font-weight: 600;
            }
            QGroupBox {
                background: #fbfcfe;
                border: 1px solid #dce3ee;
                border-radius: 8px;
                margin-top: 14px;
                padding: 14px 12px 12px 12px;
                font-weight: 700;
                color: #0f172a;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                background: #fbfcfe;
            }
            QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 6px 8px;
                selection-background-color: #bfdbfe;
            }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                min-height: 30px;
            }
            QTextEdit, QPlainTextEdit {
                font-family: Consolas, "Cascadia Mono", monospace;
            }
            QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
            QSpinBox:focus, QDoubleSpinBox:focus {
                border-color: #2563eb;
            }
            QPushButton {
                border: 0;
                border-radius: 6px;
                padding: 8px 12px;
                font-weight: 700;
                min-height: 18px;
            }
            QPushButton#primaryButton {
                background: #2563eb;
                color: #ffffff;
            }
            QPushButton#successButton {
                background: #16a34a;
                color: #ffffff;
            }
            QPushButton#dangerButton {
                background: #dc2626;
                color: #ffffff;
            }
            QPushButton#neutralButton {
                background: #475569;
                color: #ffffff;
            }
            QFrame#panel QPushButton {
                padding: 6px 10px;
                min-height: 16px;
            }
            QPushButton:disabled {
                background: #cbd5e1;
                color: #64748b;
            }
            QCheckBox {
                spacing: 8px;
            }
            QSplitter::handle {
                background: #e2e8f0;
                width: 8px;
            }
            QScrollArea {
                background: transparent;
            }
            QTableWidget {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                gridline-color: #e2e8f0;
                selection-background-color: #dbeafe;
                selection-color: #111827;
            }
            QTreeWidget {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                selection-background-color: #dbeafe;
                selection-color: #111827;
                outline: 0;
            }
            QTreeWidget::item {
                min-height: 26px;
                border-bottom: 1px solid #f1f5f9;
            }
            QTreeWidget::item:selected {
                background: #dbeafe;
                color: #111827;
            }
            QTreeWidget::branch {
                background: transparent;
            }
            QListWidget#nameToc {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                outline: 0;
            }
            QListWidget#nameToc::item {
                min-height: 24px;
                padding: 2px 4px;
                border-bottom: 1px solid #f1f5f9;
            }
            QListWidget#nameToc::item:selected {
                background: #dbeafe;
                color: #111827;
            }
            QComboBox#styleCombo {
                min-height: 20px;
                padding: 1px 4px;
                margin: 2px 4px;
                font-size: 12px;
            }
            QHeaderView::section {
                background: #eef2f7;
                border: 0;
                border-right: 1px solid #d9e0ea;
                padding: 7px;
                font-weight: 700;
            }
            """
        )

    def show_style_help(self) -> None:
        StyleHelpDialog(self).exec()

    # ---------------- 目标清单 name.txt：读 / 写 ----------------

    def load_name_file(self) -> None:
        if self.name_dirty:
            result = QMessageBox.question(
                self,
                "未保存的修改",
                "name.txt 还有未保存的修改，重载会丢弃它们。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                return

        try:
            self.name_list = name_store.load_name_list(NAME_PATH)
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", f"读取 name.txt 失败：{exc}")
            return

        self.name_dirty = False
        self._render_name_tree(keep_state=False)
        if self.name_list.had_bom:
            QMessageBox.warning(
                self,
                "文件带 UTF-8 BOM",
                "name.txt 开头有 UTF-8 BOM，而 worker 用 utf-8 读取，会把第一行表头当成普通目标。\n"
                "界面已按容错方式解析；点一次“保存”即可去掉 BOM，让 worker 恢复正常。",
            )
        self.statusBar().showMessage(
            f"name.txt 已重载：{len(self.name_list.groups)} 个分组 / {self.name_list.target_total()} 个目标",
            3000,
        )

    def save_name_file(self) -> None:
        try:
            if self.name_list.source_mtime_ns is not None and NAME_PATH.exists():
                if NAME_PATH.stat().st_mtime_ns != self.name_list.source_mtime_ns:
                    result = QMessageBox.question(
                        self,
                        "文件已被外部修改",
                        "name.txt 在载入之后被其它程序改过，继续保存会用界面里的内容覆盖磁盘文件。是否覆盖？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if result != QMessageBox.StandardButton.Yes:
                        return
            name_store.save_name_list(NAME_PATH, self.name_list)
        except PermissionError as exc:
            QMessageBox.critical(
                self,
                "保存失败",
                "name.txt 正被其它程序占用（可能是 worker 正在读取或杀软在扫描），请稍后重试。\n\n"
                f"详细信息：{exc}",
            )
            return
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"保存 name.txt 失败：{exc}")
            return

        self.name_dirty = False
        self._refresh_name_header()
        self.statusBar().showMessage("name.txt 已保存，worker 会在下一次热重载时应用", 3500)

    def _mark_name_dirty(self) -> None:
        self.name_dirty = True
        self._refresh_name_header()

    def _refresh_name_header(self) -> None:
        self.name_title_label.setText("目标文字 name.txt" + ("　● 未保存" if self.name_dirty else ""))
        self.name_title_label.setStyleSheet("color: #b45309;" if self.name_dirty else "")

        duplicates = self.name_list.duplicate_extra_total()
        summary = f"{len(self.name_list.groups)} 个分组 · {self.name_list.target_total()} 个目标"
        if duplicates:
            summary += f" · ⚠ {duplicates} 条重复记录"
        self.name_summary_label.setText(summary)
        self.name_summary_label.setStyleSheet("color: #b45309;" if duplicates else "")

    # ---------------- 目标清单：树渲染 ----------------

    def _render_name_tree(self, keep_state: bool = True) -> None:
        collapsed = set()
        selected_key: Optional[Tuple[str, int, int]] = None
        if keep_state:
            for index in range(self.name_tree.topLevelItemCount()):
                item = self.name_tree.topLevelItem(index)
                if item is not None and not item.isExpanded():
                    collapsed.add(index)
            selected_key = self._name_item_key(self.name_tree.currentItem())

        self._name_rendering = True
        self.name_tree.blockSignals(True)
        try:
            self.name_tree.clear()
            duplicates = self.name_list.duplicate_locations()
            for group_index, group in enumerate(self.name_list.groups):
                self._append_name_group_item(group_index, group, duplicates)
        finally:
            self.name_tree.blockSignals(False)
            self._name_rendering = False

        for index in range(self.name_tree.topLevelItemCount()):
            item = self.name_tree.topLevelItem(index)
            if item is not None:
                item.setExpanded(index not in collapsed)

        self._render_name_toc()
        self._apply_name_filter(self.name_search_edit.text())
        self._restore_name_selection(selected_key)
        self._refresh_name_header()
        self._update_name_preview()

    # ---------------- 目标清单：左侧分组目录 ----------------

    def _render_name_toc(self) -> None:
        self._toc_syncing = True
        try:
            self.name_toc.clear()
            for index, group in enumerate(self.name_list.groups):
                item = QListWidgetItem(f"{group.label} ({len(group.targets)})")
                item.setData(Qt.ItemDataRole.UserRole, index)
                if group.style:
                    # 只有带特效的分组才显示色块，普通分组不挂灰色方块。
                    item.setIcon(build_style_icon(group.style))
                style_name = STYLE_DISPLAY_NAMES.get(group.style, STYLE_DISPLAY_NAMES[""])
                item.setToolTip(f"{group.label}\n{len(group.targets)} 个目标 · {style_name}")
                if group.is_ungrouped:
                    item.setForeground(QBrush(QColor("#64748b")))
                self.name_toc.addItem(item)
        finally:
            self._toc_syncing = False
        self._sync_name_toc_selection()

    def _sync_name_toc_selection(self) -> None:
        group_index = self._current_group_index()
        self._toc_syncing = True
        try:
            if group_index is None or not 0 <= group_index < self.name_toc.count():
                self.name_toc.setCurrentRow(-1)
                return
            self.name_toc.setCurrentRow(group_index)
            item = self.name_toc.item(group_index)
            if item is not None:
                self.name_toc.scrollToItem(item)
        finally:
            self._toc_syncing = False

    def _on_name_toc_row_changed(self, row: int) -> None:
        if self._toc_syncing or row < 0 or not 0 <= row < len(self.name_list.groups):
            return
        self._select_name_group(row)

    def _append_name_group_item(
        self,
        group_index: int,
        group: name_store.NameGroup,
        duplicates: Dict[str, List[Tuple[int, int]]],
    ) -> None:
        # 未分组用“（未分组）”占位显示，它不是真实分组名，所以不给内联编辑。
        item = QTreeWidgetItem([group.label, "", self._group_note(group)])
        item.setData(0, Qt.ItemDataRole.UserRole, (NAME_TREE_GROUP_ROLE, group_index, -1))
        if not group.is_ungrouped:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        item.setForeground(2, QBrush(QColor("#94a3b8")))
        if group.is_ungrouped:
            item.setForeground(0, QBrush(QColor("#64748b")))
        item.setToolTip(0, self._group_tooltip(group))
        self.name_tree.addTopLevelItem(item)
        self.name_tree.setItemWidget(item, 1, self._build_style_combo(group_index, group.style))

        for target_index, target in enumerate(group.targets):
            self._append_name_target_item(item, group_index, target_index, target, group, duplicates)

    def _append_name_target_item(
        self,
        parent: QTreeWidgetItem,
        group_index: int,
        target_index: int,
        target: str,
        group: name_store.NameGroup,
        duplicates: Dict[str, List[Tuple[int, int]]],
    ) -> None:
        locations = duplicates.get(target.casefold(), [])
        first = locations[0] if locations else (group_index, target_index)
        is_duplicate = bool(locations) and (group_index, target_index) != first

        item = QTreeWidgetItem([target, "", "⚠ 重复" if is_duplicate else ""])
        item.setData(0, Qt.ItemDataRole.UserRole, (NAME_TREE_TARGET_ROLE, group_index, target_index))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        item.setFont(0, QFont("Consolas", 10))
        if is_duplicate:
            first_group = self.name_list.groups[first[0]].label if first[0] < len(self.name_list.groups) else ""
            item.setForeground(0, QBrush(QColor("#b45309")))
            item.setForeground(2, QBrush(QColor("#b45309")))
            item.setToolTip(0, f"重复目标：worker 只认第一次出现的位置（{first_group}），这一条不会参与匹配。")
        else:
            item.setToolTip(0, f"分组：{group.label}")
        parent.addChild(item)

    def _build_style_combo(self, group_index: int, style: str) -> QComboBox:
        combo = QComboBox(self.name_tree)
        combo.setObjectName("styleCombo")
        combo.setFixedHeight(24)
        for value, label in GROUP_STYLE_OPTIONS:
            combo.addItem(build_style_icon(value), label, value)
        set_combo_data(combo, style)

        group = self.name_list.groups[group_index] if 0 <= group_index < len(self.name_list.groups) else None
        if group is not None and group.is_ungrouped:
            # 未分组在文件里没有表头行，写不出特效后缀，直接禁用避免“选了却不生效”。
            combo.setEnabled(False)
            combo.setToolTip("“未分组”没有分组表头，只能用默认自动分色；需要特效请先新建分组再把这些目标移进去")
        else:
            combo.setToolTip("该分组下所有目标的识别框特效；保存后 worker 自动生效")
            combo.currentIndexChanged.connect(
                lambda _index, index=group_index, widget=combo: self._on_name_style_changed(index, widget)
            )
        return combo

    @staticmethod
    def _group_note(group: name_store.NameGroup) -> str:
        return f"{len(group.targets)} 个目标" if group.targets else "空分组"

    @staticmethod
    def _group_tooltip(group: name_store.NameGroup) -> str:
        lines = [
            f"分组：{group.label}",
            f"特效：{STYLE_DISPLAY_NAMES.get(group.style, STYLE_DISPLAY_NAMES[''])}",
            f"目标数：{len(group.targets)}",
        ]
        if group.is_ungrouped:
            lines.append("这些目标不属于任何分组（文件里排在表头之前，或用一行裸 # 标记）")
        else:
            letter = name_store.STYLE_LETTERS.get(group.style, "")
            lines.append(f"文件表头：# {group.name}" + (f"#{letter}" if letter else ""))
        return "\n".join(lines)

    @staticmethod
    def _name_item_key(item: Optional[QTreeWidgetItem]) -> Optional[Tuple[str, int, int]]:
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or len(data) != 3:
            return None
        return (str(data[0]), int(data[1]), int(data[2]))

    def _find_name_item(self, key: Tuple[str, int, int]) -> Optional[QTreeWidgetItem]:
        for index in range(self.name_tree.topLevelItemCount()):
            group_item = self.name_tree.topLevelItem(index)
            if group_item is None:
                continue
            if self._name_item_key(group_item) == key:
                return group_item
            for child_index in range(group_item.childCount()):
                child = group_item.child(child_index)
                if self._name_item_key(child) == key:
                    return child
        return None

    def _first_visible_name_item(self) -> Optional[QTreeWidgetItem]:
        for index in range(self.name_tree.topLevelItemCount()):
            item = self.name_tree.topLevelItem(index)
            if item is not None and not item.isHidden():
                return item
        return None

    def _restore_name_selection(self, key: Optional[Tuple[str, int, int]]) -> None:
        self.name_tree.clearSelection()
        candidate = self._find_name_item(key) if key is not None else None
        if candidate is None or candidate.isHidden():
            candidate = self._first_visible_name_item()
        if candidate is not None:
            self.name_tree.setCurrentItem(candidate)
            candidate.setSelected(True)
            self.name_tree.scrollToItem(candidate)

    def _select_name_group(self, group_index: int) -> None:
        item = self.name_tree.topLevelItem(group_index)
        if item is None:
            return
        if item.isHidden():
            # 当前搜索词把这个分组过滤掉了，清空搜索让操作结果可见（clear() 会触发重新过滤）。
            self.name_search_edit.clear()
        item.setExpanded(True)
        self.name_tree.clearSelection()
        self.name_tree.setCurrentItem(item)
        item.setSelected(True)
        self.name_tree.scrollToItem(item)

    def _select_name_targets(self, names: List[str]) -> None:
        wanted = {name.casefold() for name in names}
        self.name_tree.clearSelection()
        last_item: Optional[QTreeWidgetItem] = None
        for index in range(self.name_tree.topLevelItemCount()):
            group_item = self.name_tree.topLevelItem(index)
            if group_item is None:
                continue
            for child_index in range(group_item.childCount()):
                child = group_item.child(child_index)
                value = str(child.data(0, Qt.ItemDataRole.EditRole) or "")
                if value.casefold() in wanted:
                    child.setSelected(True)
                    last_item = child
        if last_item is not None:
            if last_item.isHidden():
                self.name_search_edit.clear()
            # 注意：PySide6 的 setCurrentItem(item) 等价于 ClearAndSelect，会把多选清掉，
            # 所以这里显式传 NoUpdate，只改当前项、保留已经选中的其它目标。
            self.name_tree.setCurrentItem(last_item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.name_tree.scrollToItem(last_item)

    def _current_group_index(self) -> Optional[int]:
        key = self._name_item_key(self.name_tree.currentItem())
        if key is None:
            return 0 if self.name_list.groups else None
        group_index = key[1]
        return group_index if 0 <= group_index < len(self.name_list.groups) else None

    # ---------------- 目标清单：交互 ----------------

    def _on_name_style_changed(self, group_index: int, combo: QComboBox) -> None:
        if self._name_rendering or not 0 <= group_index < len(self.name_list.groups):
            return
        group = self.name_list.groups[group_index]
        if group.is_ungrouped:
            return
        style = str(combo.currentData() or "")
        if group.style == style:
            return
        group.style = style
        self._mark_name_dirty()
        item = self.name_tree.topLevelItem(group_index)
        if item is not None:
            item.setToolTip(0, self._group_tooltip(group))
        self._update_name_preview()
        self.statusBar().showMessage(
            f"分组「{group.label}」特效已改为：{STYLE_DISPLAY_NAMES.get(style, STYLE_DISPLAY_NAMES[''])}",
            3000,
        )

    def _on_name_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._name_rendering or item is None:
            return
        if column != 0:
            # 备注列不可编辑（见 NameTreeDelegate），这里兜底把显示恢复成模型内容。
            self._revert_name_item()
            return
        key = self._name_item_key(item)
        if key is None:
            return
        kind, group_index, target_index = key
        if not 0 <= group_index < len(self.name_list.groups):
            return
        group = self.name_list.groups[group_index]
        new_text = str(item.data(0, Qt.ItemDataRole.EditRole) or "")

        if kind == NAME_TREE_GROUP_ROLE:
            if group.is_ungrouped:
                self._revert_name_item()
                return
            error = name_store.validate_group_name(new_text)
            if error:
                QMessageBox.warning(self, "分组名不可用", error)
                self._revert_name_item()
                return
            value = new_text.strip()
            if value == group.name:
                self._revert_name_item()
                return
            group.name = value
        elif kind == NAME_TREE_TARGET_ROLE:
            if not 0 <= target_index < len(group.targets):
                return
            error = name_store.validate_target_name(new_text)
            if error:
                QMessageBox.warning(self, "目标文字不可用", error)
                self._revert_name_item()
                return
            value = new_text.strip()
            if value == group.targets[target_index]:
                self._revert_name_item()
                return
            key_value = value.casefold()
            if any(
                other.casefold() == key_value for index, other in enumerate(group.targets) if index != target_index
            ):
                QMessageBox.warning(self, "目标已存在", f"分组「{group.label}」里已经有「{value}」了。")
                self._revert_name_item()
                return
            group.targets[target_index] = value
        else:
            return

        self._mark_name_dirty()
        # 在 itemChanged 处理里直接重建树会删掉正在提交的编辑器，延到事件循环下一轮再渲染。
        QTimer.singleShot(0, lambda: self._render_name_tree(keep_state=True))

    def _revert_name_item(self) -> None:
        """把显示恢复成模型内容。

        校验失败的路径模型没有被改动，所以只需要重建一次；这里刻意不直接操作 item：
        上面的模态提示会跑嵌套事件循环，期间可能已经执行过一次延迟重建并销毁了 item。
        """
        QTimer.singleShot(0, lambda: self._render_name_tree(keep_state=True))

    def _on_name_selection_changed(self) -> None:
        if self._name_rendering:
            return
        self._sync_name_toc_selection()
        self._update_name_preview()

    def _preview_color(self) -> str:
        overlay = self.config.get("overlay", {})
        if not isinstance(overlay, dict):
            return "#ff3b30"
        palette = overlay.get("color_palette") or []
        if str(overlay.get("color_mode", "")) == "by_target" and palette:
            return str(palette[0])
        return str(overlay.get("stroke_color", "#ff3b30"))

    def _preview_line_width(self) -> int:
        overlay = self.config.get("overlay", {})
        if not isinstance(overlay, dict):
            return 3
        try:
            return max(1, min(12, int(overlay.get("line_width", 3))))
        except (TypeError, ValueError):
            return 3

    def _update_name_preview(self) -> None:
        group_index = self._current_group_index()
        if group_index is None:
            self.name_preview_caption.setText("效果预览：默认自动分色\n先在左侧添加分组，再给分组选特效。")
            self.name_preview.set_effect("", "示例目标 - 92%", self._preview_color(), self._preview_line_width())
            return

        group = self.name_list.groups[group_index]
        sample = group.targets[0] if group.targets else "示例目标"
        label = f"{sample} - 92%" if group.is_ungrouped else f"{group.label}：{sample} - 92%"
        style_name = STYLE_DISPLAY_NAMES.get(group.style, STYLE_DISPLAY_NAMES[""])
        self.name_preview_caption.setText(
            f"效果预览：{style_name}\n按桌面透明层的画法近似；OBS 浏览器源的彩虹标签底色略有不同。"
        )
        self.name_preview.set_effect(group.style, label, self._preview_color(), self._preview_line_width())

    def filter_name_tree(self, text: str) -> None:
        self._apply_name_filter(text)

    def _apply_name_filter(self, text: str) -> None:
        keyword = str(text).strip().casefold()
        for index in range(self.name_tree.topLevelItemCount()):
            group_item = self.name_tree.topLevelItem(index)
            if group_item is None:
                continue
            group_name = str(group_item.data(0, Qt.ItemDataRole.EditRole) or "").casefold()
            group_matches = bool(keyword) and keyword in group_name
            visible_children = 0
            for child_index in range(group_item.childCount()):
                child = group_item.child(child_index)
                if child is None:
                    continue
                target_name = str(child.data(0, Qt.ItemDataRole.EditRole) or "")
                matched = (not keyword) or group_matches or keyword in target_name.casefold()
                child.setHidden(not matched)
                if matched:
                    visible_children += 1
            hidden = bool(keyword) and not group_matches and visible_children == 0
            group_item.setHidden(hidden)
            # 目录跟着一起过滤，避免点到已经被过滤掉的分组。
            toc_item = self.name_toc.item(index)
            if toc_item is not None:
                toc_item.setHidden(hidden)

    # ---------------- 目标清单：增删改 ----------------

    def add_name_group(self) -> None:
        name, accepted = QInputDialog.getText(self, "新建分组", "分组名（会写成 “# 分组名” 表头）：")
        if not accepted:
            return
        error = name_store.validate_group_name(name)
        if error:
            QMessageBox.warning(self, "分组名不可用", error)
            return

        value = str(name).strip()
        self.name_list.groups.append(name_store.NameGroup(name=value, style="", blank_before=True))
        self._mark_name_dirty()
        self._render_name_tree(keep_state=True)
        self._select_name_group(len(self.name_list.groups) - 1)
        self.statusBar().showMessage(f"已新建分组「{value}」，点“＋目标”往里添加目标", 4000)

    def add_name_targets(self) -> None:
        # 不要在弹框前就创建“未分组”：用户取消时会留下一个磁盘上并不存在的空分组。
        labels = [group.label for group in self.name_list.groups]
        indices = list(range(len(self.name_list.groups)))
        if self.name_list.ungrouped_index() is None:
            labels.append(name_store.UNGROUPED_LABEL)
            indices.append(-1)

        current = self._current_group_index()
        dialog = AddTargetsDialog(labels, indices, current if current is not None else 0, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        group_index = dialog.selected_group_index()
        if group_index < 0:
            group_index = self.name_list.ensure_ungrouped()
            self._render_name_tree(keep_state=False)
        if not 0 <= group_index < len(self.name_list.groups):
            return

        names, ignored, merged = name_store.split_target_input(dialog.input_text())
        if not names:
            QMessageBox.information(self, "没有可添加的目标", "没有解析到有效的目标文字。")
            return

        group = self.name_list.groups[group_index]
        existing = {target.casefold() for target in group.targets}
        skip_existing = dialog.skip_existing()
        added = 0
        skipped = 0
        for name in names:
            key = name.casefold()
            if skip_existing and key in existing:
                skipped += 1
                continue
            group.targets.append(name)
            existing.add(key)
            added += 1

        if added == 0:
            QMessageBox.information(self, "没有可添加的目标", "这些目标在该分组里都已经存在了。")
            return

        self._mark_name_dirty()
        self._render_name_tree(keep_state=True)
        self._select_name_group(group_index)

        message = f"已向「{group.label}」添加 {added} 个目标"
        details = []
        if skipped:
            details.append(f"跳过已存在 {skipped} 个")
        if merged:
            details.append(f"合并输入内重复 {merged} 个")
        if ignored:
            details.append(f"忽略 # 行 {ignored} 行")
        if details:
            message += "（" + "，".join(details) + "）"
        self.statusBar().showMessage(message, 4000)

    def rename_name_selection(self) -> None:
        item = self.name_tree.currentItem()
        key = self._name_item_key(item)
        if item is None or key is None:
            self.statusBar().showMessage("请先选择一个分组或目标", 2500)
            return
        if item.isHidden():
            # 过滤会保留 currentItem，先清空搜索，避免改到看不见的那一行。
            self.name_search_edit.clear()

        kind, group_index, target_index = key
        if not 0 <= group_index < len(self.name_list.groups):
            return
        group = self.name_list.groups[group_index]

        if kind == NAME_TREE_GROUP_ROLE:
            if group.is_ungrouped:
                QMessageBox.information(
                    self,
                    "未分组不能改名",
                    "“未分组”不是真实分组，文件里没有对应的表头。\n"
                    "如果想给它起名字，请新建一个分组，再把里面的目标用“移动到…”移过去。",
                )
                return
            text, accepted = QInputDialog.getText(
                self, "重命名分组", "分组名：", QLineEdit.EchoMode.Normal, group.name
            )
            if not accepted:
                return
            error = name_store.validate_group_name(text)
            if error:
                QMessageBox.warning(self, "分组名不可用", error)
                return
            value = str(text).strip()
            if value == group.name:
                return
            if any(other.name == value for index, other in enumerate(self.name_list.groups) if index != group_index):
                QMessageBox.information(self, "同名分组", f"已经有一个叫「{value}」的分组了，两者会显示相同标签。")
            group.name = value
        elif kind == NAME_TREE_TARGET_ROLE:
            if not 0 <= target_index < len(group.targets):
                return
            current = group.targets[target_index]
            text, accepted = QInputDialog.getText(
                self, "重命名目标", "目标文字：", QLineEdit.EchoMode.Normal, current
            )
            if not accepted:
                return
            error = name_store.validate_target_name(text)
            if error:
                QMessageBox.warning(self, "目标文字不可用", error)
                return
            value = str(text).strip()
            if value == current:
                return
            key_value = value.casefold()
            if any(
                other.casefold() == key_value for index, other in enumerate(group.targets) if index != target_index
            ):
                QMessageBox.warning(self, "目标已存在", f"分组「{group.label}」里已经有「{value}」了。")
                return
            group.targets[target_index] = value
        else:
            return

        self._mark_name_dirty()
        self._render_name_tree(keep_state=True)

    def delete_name_selection(self) -> None:
        keys = [key for key in (self._name_item_key(item) for item in self.name_tree.selectedItems()) if key]
        if not keys:
            self.statusBar().showMessage("请先选择要删除的分组或目标", 2500)
            return

        group_indices = sorted({key[1] for key in keys if key[0] == NAME_TREE_GROUP_ROLE})
        removed_groups = set(group_indices)
        target_keys = {
            (key[1], key[2]) for key in keys if key[0] == NAME_TREE_TARGET_ROLE and key[1] not in removed_groups
        }

        if group_indices:
            total = sum(
                len(self.name_list.groups[index].targets)
                for index in group_indices
                if 0 <= index < len(self.name_list.groups)
            )
            message = f"确定删除 {len(group_indices)} 个分组" + (f"及其 {total} 个目标" if total else "") + "？"
            result = QMessageBox.question(
                self,
                "删除分组",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                return

        for group_index, target_index in sorted(target_keys, reverse=True):
            if not 0 <= group_index < len(self.name_list.groups):
                continue
            targets = self.name_list.groups[group_index].targets
            if 0 <= target_index < len(targets):
                del targets[target_index]
        for group_index in sorted(group_indices, reverse=True):
            if 0 <= group_index < len(self.name_list.groups):
                del self.name_list.groups[group_index]

        self.name_list.normalize_ungrouped()
        self._mark_name_dirty()
        self._render_name_tree(keep_state=True)
        self.statusBar().showMessage(
            f"已删除 {len(group_indices)} 个分组、{len(target_keys)} 个目标（保存后生效）",
            4000,
        )

    def move_name_selection(self, delta: int) -> None:
        keys = [key for key in (self._name_item_key(item) for item in self.name_tree.selectedItems()) if key]
        if not keys:
            self.statusBar().showMessage("请先选择要移动的分组或目标", 2500)
            return

        group_keys = [key for key in keys if key[0] == NAME_TREE_GROUP_ROLE]
        if group_keys:
            group_index = group_keys[0][1]
            new_index = group_index + delta
            if not 0 <= new_index < len(self.name_list.groups):
                self.statusBar().showMessage("已经在最前面 / 最后面了", 2000)
                return
            self.name_list.groups.insert(new_index, self.name_list.groups.pop(group_index))
            moved = self.name_list.groups[new_index]
            self.name_list.normalize_ungrouped()
            self._mark_name_dirty()
            self._render_name_tree(keep_state=False)
            try:
                new_position = self.name_list.groups.index(moved)
            except ValueError:  # 理论上不会发生：normalize 只合并、不重建对象
                new_position = min(max(new_index, 0), len(self.name_list.groups) - 1)
            self._select_name_group(new_position)
            return

        target_keys = [key for key in keys if key[0] == NAME_TREE_TARGET_ROLE]
        group_indices = {key[1] for key in target_keys}
        if len(group_indices) != 1:
            self.statusBar().showMessage("一次只能移动同一个分组内的目标", 2500)
            return

        group_index = target_keys[0][1]
        group = self.name_list.groups[group_index]
        moved_names: List[str] = []
        moved = 0
        for target_index in sorted({key[2] for key in target_keys}, reverse=delta > 0):
            new_index = target_index + delta
            if not 0 <= new_index < len(group.targets):
                continue
            moved_names.append(group.targets[target_index])
            group.targets.insert(new_index, group.targets.pop(target_index))
            moved += 1

        if not moved:
            self.statusBar().showMessage("已经在最前面 / 最后面了", 2000)
            return

        self._mark_name_dirty()
        self._render_name_tree(keep_state=False)
        self._select_name_targets(moved_names)

    def move_name_targets_to_group(self) -> None:
        selected_keys = [
            key
            for key in (self._name_item_key(item) for item in self.name_tree.selectedItems())
            if key and key[0] == NAME_TREE_TARGET_ROLE
        ]
        if not selected_keys:
            self.statusBar().showMessage("请先选择要移动的目标（分组请用上移 / 下移）", 2500)
            return

        target_keys = [(key[1], key[2]) for key in selected_keys]
        source_groups = {key[0] for key in target_keys}
        menu = QMenu(self)
        for index, group in enumerate(self.name_list.groups):
            submenu = menu.addMenu(group.label)
            if len(source_groups) == 1 and index in source_groups:
                submenu.setEnabled(False)
                continue
            top_action = submenu.addAction("移到顶部")
            top_action.setData((index, "top"))
            end_action = submenu.addAction("移到最后")
            end_action.setData((index, "end"))
        chosen = menu.exec(self.name_move_button.mapToGlobal(QPoint(0, self.name_move_button.height())))
        if chosen is None:
            return

        payload = chosen.data()
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        destination, position = int(payload[0]), str(payload[1])
        if not 0 <= destination < len(self.name_list.groups):
            return

        before_duplicates = self.name_list.duplicate_total()
        names = [
            self.name_list.groups[group_index].targets[target_index]
            for group_index, target_index in sorted(target_keys)
            if 0 <= group_index < len(self.name_list.groups)
            and 0 <= target_index < len(self.name_list.groups[group_index].targets)
        ]
        for group_index, target_index in sorted(target_keys, reverse=True):
            if not 0 <= group_index < len(self.name_list.groups):
                continue
            targets = self.name_list.groups[group_index].targets
            if 0 <= target_index < len(targets):
                del targets[target_index]

        destination_group = self.name_list.groups[destination]
        if position == "top":
            destination_group.targets[0:0] = names
        else:
            destination_group.targets.extend(names)
        after_duplicates = self.name_list.duplicate_total()

        self._mark_name_dirty()
        self._render_name_tree(keep_state=False)
        self._select_name_targets(names)

        where = "顶部" if position == "top" else "末尾"
        message = f"已把 {len(names)} 个目标移到「{destination_group.label}」{where}"
        if after_duplicates > before_duplicates:
            message += f"（新增 {after_duplicates - before_duplicates} 个重复目标，已用 ⚠ 标记）"
        self.statusBar().showMessage(message, 4000)

    def show_duplicate_report(self) -> None:
        if not self.name_list.duplicate_locations():
            QMessageBox.information(self, "查重", "没有发现重复目标。")
            return
        dialog = DuplicateTargetsDialog(self.name_list, self)
        dialog.exec()
        if not dialog.cleanup_requested:
            return

        removed = self.name_list.remove_duplicates()
        self._mark_name_dirty()
        self._render_name_tree(keep_state=False)
        self.statusBar().showMessage(f"已删除后出现的重复目标 {removed} 个（保存后生效）", 4000)

    def open_name_source_dialog(self) -> None:
        text = name_store.serialize_name_text(self.name_list.groups, self.name_list.trailing_newline)
        dialog = NameSourceDialog(text, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        groups, trailing_newline = name_store.parse_name_text(dialog.source_text())
        self.name_list = name_store.NameList(
            groups,
            newline=self.name_list.newline,
            trailing_newline=trailing_newline,
            source_mtime_ns=self.name_list.source_mtime_ns,
        )
        self._mark_name_dirty()
        self._render_name_tree(keep_state=False)
        self.statusBar().showMessage("已按源码重新载入列表（点“保存”才写入 name.txt）", 4000)

    def load_config_file(self) -> None:
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise ValueError("config.json 顶层必须是 JSON 对象")
                self.config = deep_merge(DEFAULT_CONFIG, data)
            else:
                self.config = copy.deepcopy(DEFAULT_CONFIG)
            self.populate_config_form()
            self._update_name_preview()
            self.statusBar().showMessage("config.json 已重载", 2500)
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", f"读取 config.json 失败：{exc}")

    def populate_config_form(self) -> None:
        capture = self.config.get("capture", {})
        obs = capture.get("obs_websocket", {}) if isinstance(capture.get("obs_websocket"), dict) else {}
        match = self.config.get("match", {})
        match_tolerance = self.config.get("match_tolerance", {})
        if not isinstance(match_tolerance, dict):
            match_tolerance = {}
        ocr_output = self.config.get("ocr_output", {})
        if not isinstance(ocr_output, dict):
            ocr_output = {}
        ocr = self.config.get("ocr", {})
        overlay = self.config.get("overlay", {})
        desktop_overlay = self.config.get("desktop_overlay", {})

        self.interval_spin.setValue(int(self.config.get("interval_ms", 1000)))
        self.host_edit.setText(str(self.config.get("host", "127.0.0.1")))
        self.port_spin.setValue(int(self.config.get("port", 8765)))

        set_combo_data(self.capture_source_combo, str(capture.get("source", "screen")))
        self.monitor_spin.setValue(int(capture.get("monitor", 1)))
        self.left_spin.setValue(int(capture.get("left", 0)))
        self.top_spin.setValue(int(capture.get("top", 0)))
        self.width_spin.setValue(max(0, int(capture.get("width", 0))))
        self.height_spin.setValue(max(0, int(capture.get("height", 0))))

        self.obs_url_edit.setText(str(obs.get("url", "ws://127.0.0.1:4455")))
        self.obs_password_edit.setText(str(obs.get("password", "")))
        self.obs_source_name_edit.setText(str(obs.get("source_name", "")))
        self.obs_source_uuid_edit.setText(str(obs.get("source_uuid", "")))
        set_combo_data(self.image_format_combo, str(obs.get("image_format", "png")))
        self.image_width_spin.setValue(max(0, int(obs.get("image_width", 0))))
        self.image_height_spin.setValue(max(0, int(obs.get("image_height", 0))))
        self.image_quality_spin.setValue(max(0, min(100, int(obs.get("image_compression_quality", 80)))))

        set_combo_data(self.match_mode_combo, str(match.get("mode", "contains")))
        self.case_sensitive_check.setChecked(bool_value(match.get("case_sensitive"), False))
        self.min_confidence_spin.setValue(float(match.get("min_confidence", 0.5)))

        self.match_tolerance_enabled_check.setChecked(bool_value(match_tolerance.get("enabled"), True))
        self.normalize_confusable_check.setChecked(bool_value(match_tolerance.get("normalize_confusable"), True))
        self.collapse_repeated_chars_check.setChecked(bool_value(match_tolerance.get("collapse_repeated_chars"), True))
        self.ignore_separators_check.setChecked(bool_value(match_tolerance.get("ignore_separators"), True))
        self.max_edit_distance_spin.setValue(int(match_tolerance.get("max_edit_distance", 1)))
        self.fuzzy_enabled_check.setChecked(bool_value(match_tolerance.get("fuzzy_enabled"), True))
        self.fuzzy_threshold_spin.setValue(float(match_tolerance.get("fuzzy_threshold", 0.88)))
        self.fuzzy_min_length_spin.setValue(int(match_tolerance.get("fuzzy_min_length", 4)))

        self.ocr_output_enabled_check.setChecked(bool_value(ocr_output.get("enabled"), True))

        set_combo_data(
            self.backend_combo,
            normalize_ocr_backend(ocr.get("backend", OCR_BACKEND_ONNXRUNTIME)),
        )
        self.use_cuda_check.setChecked(bool_value(ocr.get("use_cuda"), False))
        self.use_dml_check.setChecked(bool_value(ocr.get("use_dml"), False))
        self.use_cls_check.setChecked(bool_value(ocr.get("use_cls"), False))
        self.reload_files_spin.setValue(int(ocr.get("reload_files_interval_ms", 2000)))
        self.log_performance_check.setChecked(bool_value(ocr.get("log_performance"), True))

        self.stroke_color_edit.setText(str(overlay.get("stroke_color", "#ff3b30")))
        set_combo_data(self.color_mode_combo, str(overlay.get("color_mode", "single")))
        self.line_width_spin.setValue(max(1, int(overlay.get("line_width", 3))))
        self.show_label_check.setChecked(bool_value(overlay.get("show_label"), True))

        self.desktop_overlay_enabled_check.setChecked(bool_value(desktop_overlay.get("enabled"), False))
        set_combo_data(self.coordinate_mode_combo, str(desktop_overlay.get("coordinate_mode", "capture")))
        self.screen_region_edit.setText(format_screen_region(desktop_overlay.get("screen_region", "auto")))

        self.update_obs_controls()
        self.update_ocr_backend_controls()

    def build_config_from_form(self) -> Dict[str, Any]:
        if self.interval_spin.value() < 100:
            raise ValueError("interval_ms 必须大于等于 100")
        if not 1 <= self.port_spin.value() <= 65535:
            raise ValueError("port 必须在 1-65535 之间")
        if self.width_spin.value() < 0 or self.height_spin.value() < 0:
            raise ValueError("width/height 不能小于 0")
        if self.image_width_spin.value() < 0 or self.image_height_spin.value() < 0:
            raise ValueError("image_width/image_height 不能小于 0")
        if not 0.0 <= self.min_confidence_spin.value() <= 1.0:
            raise ValueError("min_confidence 必须在 0-1 之间")
        if not 0.0 <= self.fuzzy_threshold_spin.value() <= 1.0:
            raise ValueError("fuzzy_threshold 必须在 0-1 之间")

        config = copy.deepcopy(self.config)
        config["interval_ms"] = self.interval_spin.value()
        config["host"] = self.host_edit.text().strip() or "127.0.0.1"
        config["port"] = self.port_spin.value()

        capture = ensure_dict(config, "capture")
        capture["source"] = str(self.capture_source_combo.currentData() or "screen")
        capture["monitor"] = self.monitor_spin.value()
        capture["left"] = self.left_spin.value()
        capture["top"] = self.top_spin.value()
        capture["width"] = self.width_spin.value()
        capture["height"] = self.height_spin.value()

        obs = ensure_dict(capture, "obs_websocket")
        obs["url"] = self.obs_url_edit.text().strip() or "ws://127.0.0.1:4455"
        obs["password"] = self.obs_password_edit.text()
        obs["source_name"] = self.obs_source_name_edit.text().strip()
        obs["source_uuid"] = self.obs_source_uuid_edit.text().strip()
        obs["image_format"] = self.image_format_combo.currentText().strip() or "png"
        obs["image_width"] = self.image_width_spin.value()
        obs["image_height"] = self.image_height_spin.value()
        obs["image_compression_quality"] = self.image_quality_spin.value()

        match = ensure_dict(config, "match")
        match["mode"] = str(self.match_mode_combo.currentData() or "contains")
        match["case_sensitive"] = self.case_sensitive_check.isChecked()
        match["min_confidence"] = self.min_confidence_spin.value()

        match_tolerance = ensure_dict(config, "match_tolerance")
        match_tolerance["enabled"] = self.match_tolerance_enabled_check.isChecked()
        match_tolerance["normalize_confusable"] = self.normalize_confusable_check.isChecked()
        match_tolerance["collapse_repeated_chars"] = self.collapse_repeated_chars_check.isChecked()
        match_tolerance["ignore_separators"] = self.ignore_separators_check.isChecked()
        match_tolerance["max_edit_distance"] = self.max_edit_distance_spin.value()
        match_tolerance["fuzzy_enabled"] = self.fuzzy_enabled_check.isChecked()
        match_tolerance["fuzzy_threshold"] = self.fuzzy_threshold_spin.value()
        match_tolerance["fuzzy_min_length"] = self.fuzzy_min_length_spin.value()

        ocr_output = ensure_dict(config, "ocr_output")
        ocr_output["enabled"] = self.ocr_output_enabled_check.isChecked()

        ocr = ensure_dict(config, "ocr")
        ocr["backend"] = normalize_ocr_backend(self.backend_combo.currentData())
        ocr["use_cuda"] = self.use_cuda_check.isChecked()
        ocr["use_dml"] = self.use_dml_check.isChecked()
        ocr["use_cls"] = self.use_cls_check.isChecked()
        ocr["reload_files_interval_ms"] = self.reload_files_spin.value()
        ocr["log_performance"] = self.log_performance_check.isChecked()

        overlay = ensure_dict(config, "overlay")
        overlay["stroke_color"] = self.stroke_color_edit.text().strip() or "#ff3b30"
        overlay["color_mode"] = str(self.color_mode_combo.currentData() or "single")
        overlay["line_width"] = self.line_width_spin.value()
        overlay["show_label"] = self.show_label_check.isChecked()

        desktop_overlay = ensure_dict(config, "desktop_overlay")
        desktop_overlay["enabled"] = self.desktop_overlay_enabled_check.isChecked()
        desktop_overlay["coordinate_mode"] = str(self.coordinate_mode_combo.currentData() or "capture")
        desktop_overlay["screen_region"] = parse_screen_region(self.screen_region_edit.text())

        return config

    def save_config_file(self) -> None:
        try:
            self.config = self.build_config_from_form()
            CONFIG_PATH.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._update_name_preview()  # 预览用的描边颜色/线宽可能刚改过
            self.statusBar().showMessage("config.json 已保存，worker 会按热重载间隔读取", 3500)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"保存 config.json 失败：{exc}")

    def choose_stroke_color(self) -> None:
        current = QColor(self.stroke_color_edit.text().strip() or "#ff3b30")
        color = QColorDialog.getColor(current, self, "选择框线颜色")
        if color.isValid():
            self.stroke_color_edit.setText(color.name())

    def update_obs_controls(self) -> None:
        enabled = self.capture_source_combo.currentData() == "obs_websocket"
        obs_widgets = [
            self.obs_url_edit,
            self.obs_password_edit,
            self.obs_source_name_edit,
            self.obs_source_uuid_edit,
            self.image_format_combo,
            self.image_width_spin,
            self.image_height_spin,
            self.image_quality_spin,
            self.test_obs_button,
            self.fetch_obs_button,
        ]
        busy = self.obs_thread is not None and self.obs_thread.isRunning()
        for widget in obs_widgets:
            widget.setEnabled(enabled and not busy)

    def update_ocr_backend_controls(self) -> None:
        use_onnxruntime = (
            normalize_ocr_backend(self.backend_combo.currentData())
            == OCR_BACKEND_ONNXRUNTIME
        )
        self.use_cuda_check.setEnabled(use_onnxruntime)
        self.use_dml_check.setEnabled(use_onnxruntime)

    def current_obs_credentials(self) -> Dict[str, str]:
        return {
            "url": self.obs_url_edit.text().strip() or "ws://127.0.0.1:4455",
            "password": self.obs_password_edit.text(),
        }

    def test_obs_connection(self) -> None:
        self._start_obs_task("test")

    def fetch_obs_sources(self) -> None:
        self._start_obs_task("sources")

    def _start_obs_task(self, action: str) -> None:
        if self.obs_thread is not None and self.obs_thread.isRunning():
            return
        credentials = self.current_obs_credentials()
        self.obs_thread = OBSTaskThread(action, credentials["url"], credentials["password"], self)
        self.obs_thread.succeeded.connect(self.on_obs_success)
        self.obs_thread.failed.connect(self.on_obs_failure)
        self.obs_thread.finished.connect(self.on_obs_finished)
        self.statusBar().showMessage("正在连接 OBS WebSocket...")
        self.obs_thread.start()
        self.update_obs_controls()

    def on_obs_success(self, action: str, result: object) -> None:
        if action == "test":
            QMessageBox.information(self, "OBS 连接", str(result))
            self.statusBar().showMessage(str(result), 4000)
            return

        payload = result if isinstance(result, dict) else {"items": []}
        if not payload.get("items"):
            QMessageBox.information(self, "OBS 捕获对象", "没有获取到 OBS 场景或输入源。")
            return

        dialog = SourcePickerDialog(payload, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_item:
            item = dialog.selected_item
            self.capture_source_combo.setCurrentIndex(1)
            self.obs_source_name_edit.setText(item.get("name", ""))
            self.obs_source_uuid_edit.setText(item.get("uuid", ""))
            self.statusBar().showMessage("已回填 OBS source_name 和 source_uuid", 3500)

    def on_obs_failure(self, message: str) -> None:
        QMessageBox.critical(self, "OBS 操作失败", message)
        self.statusBar().showMessage(f"OBS 操作失败：{message}", 5000)

    def on_obs_finished(self) -> None:
        self.obs_thread = None
        self.update_obs_controls()

    def choose_python_executable(self) -> str:
        venv_python = BASE_DIR / "venv" / "Scripts" / "python.exe"
        return str(venv_python) if venv_python.exists() else sys.executable

    def start_worker(self) -> None:
        if self.worker_process is not None and self.worker_process.poll() is None:
            self.statusBar().showMessage("worker 已在运行", 2500)
            return
        if not WORKER_PATH.exists():
            QMessageBox.critical(self, "启动失败", f"找不到 worker.py：{WORKER_PATH}")
            return
        if self.name_dirty:
            self.statusBar().showMessage("提示：name.txt 有未保存的修改，worker 会先读取磁盘上的旧内容", 5000)

        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            append_daily_log(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] GUI 启动 worker"
            )
            self.worker_output_file = daily_log_path().open("a", encoding="utf-8")
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.worker_process = subprocess.Popen(
                [self.choose_python_executable(), str(WORKER_PATH)],
                cwd=str(BASE_DIR),
                stdout=self.worker_output_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            self.statusBar().showMessage("worker 已启动", 3000)
            self.update_worker_state()
        except Exception as exc:
            self._close_worker_output()
            QMessageBox.critical(self, "启动失败", f"启动 worker 失败：{exc}")

    def stop_worker(self) -> None:
        if self.worker_process is None or self.worker_process.poll() is not None:
            self.statusBar().showMessage("worker 未运行", 2500)
            self.update_worker_state()
            return

        try:
            self.worker_process.terminate()
            self.statusBar().showMessage("正在停止 worker...")
            QTimer.singleShot(3000, self.kill_worker_if_needed)
        except Exception as exc:
            QMessageBox.critical(self, "停止失败", f"停止 worker 失败：{exc}")

    def kill_worker_if_needed(self) -> None:
        if self.worker_process is not None and self.worker_process.poll() is None:
            self.worker_process.kill()
            self.statusBar().showMessage("worker 未按时退出，已强制停止", 3500)
        self.update_worker_state()

    def update_worker_state(self) -> None:
        running = self.worker_process is not None and self.worker_process.poll() is None
        if running:
            self.worker_status_label.setText("状态：运行中")
            self.worker_status_label.setStyleSheet("background: #dcfce7; color: #166534;")
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
        else:
            if self.worker_process is not None:
                return_code = self.worker_process.poll()
                if return_code is not None:
                    self.statusBar().showMessage(f"worker 已退出，退出码 {return_code}", 3500)
                self.worker_process = None
                self._close_worker_output()
            self.worker_status_label.setText("状态：未运行")
            self.worker_status_label.setStyleSheet("background: #e5e7eb; color: #374151;")
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)

    def refresh_log(self) -> None:
        self.log_view.setPlainText(read_tail_lines(daily_log_path(), 5))
        self.log_updated_label.setText(time.strftime("最后刷新 %H:%M:%S"))

    def _close_worker_output(self) -> None:
        if self.worker_output_file is not None:
            try:
                self.worker_output_file.close()
            except Exception:
                pass
            self.worker_output_file = None

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.name_dirty:
            result = QMessageBox.question(
                self,
                "name.txt 未保存",
                "name.txt 还有未保存的修改。是否保存后退出？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if result == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if result == QMessageBox.StandardButton.Yes:
                self.save_name_file()
                if self.name_dirty:
                    event.ignore()
                    return

        if self.worker_process is not None and self.worker_process.poll() is None:
            result = QMessageBox.question(
                self,
                "worker 仍在运行",
                "worker 仍在运行。是否停止 worker 后退出？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if result == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if result == QMessageBox.StandardButton.Yes:
                self.worker_process.terminate()
                try:
                    self.worker_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.worker_process.kill()
        # 配置窗口是非模态子窗口，留着会挡住“最后一个窗口关闭”的退出判定，先收起来。
        self.config_dialog.hide()
        self._close_worker_output()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
