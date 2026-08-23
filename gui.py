# 版权所有 © 2026 www.siver.top
# 修改这里即可更新 GUI 显示的版本号
APP_VERSION = "v11"

import asyncio
import base64
import copy
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from PySide6.QtCore import Qt, QThread, QTimer, Signal
    from PySide6.QtGui import QColor, QCloseEvent, QFont
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
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QPlainTextEdit,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QStyle,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"OBS Name OCR 控制台 {APP_VERSION}")
        self.resize(1180, 820)

        self.config: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.worker_process: Optional[subprocess.Popen[Any]] = None
        self.worker_output_file: Optional[Any] = None
        self.obs_thread: Optional[OBSTaskThread] = None

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
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)
        self.setCentralWidget(central)

        root.addWidget(self._build_toolbar())

        body = QSplitter(Qt.Orientation.Horizontal, self)
        body.setChildrenCollapsible(False)
        body.addWidget(self._build_name_panel())
        body.addWidget(self._build_config_panel())
        body.setStretchFactor(0, 4)
        body.setStretchFactor(1, 6)
        root.addWidget(body, 1)

        root.addWidget(self._build_log_panel())

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
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("目标文字 name.txt")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.name_editor = QTextEdit(self)
        self.name_editor.setAcceptRichText(False)
        self.name_editor.setFont(QFont("Consolas", 11))
        self.name_editor.setPlaceholderText("每行一个目标文字")
        layout.addWidget(self.name_editor, 1)

        hint = QLabel("# 开头行设置后续目标的分组；空行会被忽略。保存后无需重启 worker。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.save_name_button = QPushButton("保存")
        self.save_name_button.setObjectName("primaryButton")
        self.save_name_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self.save_name_button.clicked.connect(self.save_name_file)
        buttons.addWidget(self.save_name_button)

        self.reload_name_button = QPushButton("重载")
        self.reload_name_button.setObjectName("neutralButton")
        self.reload_name_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.reload_name_button.clicked.connect(self.load_name_file)
        buttons.addWidget(self.reload_name_button)
        layout.addLayout(buttons)

        return panel

    def _build_config_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 18, 18, 18)
        panel_layout.setSpacing(12)

        title = QLabel("配置管理 config.json")
        title.setObjectName("sectionTitle")
        panel_layout.addWidget(title)

        scroll = QScrollArea(self)
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
        panel_layout.addWidget(scroll, 1)

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
        panel_layout.addLayout(buttons)

        return panel

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
        self.log_view.setFixedHeight(118)
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
            QHeaderView::section {
                background: #eef2f7;
                border: 0;
                border-right: 1px solid #d9e0ea;
                padding: 7px;
                font-weight: 700;
            }
            """
        )

    def load_name_file(self) -> None:
        try:
            text = NAME_PATH.read_text(encoding="utf-8") if NAME_PATH.exists() else ""
            self.name_editor.setPlainText(text)
            self.statusBar().showMessage("name.txt 已重载", 2500)
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", f"读取 name.txt 失败：{exc}")

    def save_name_file(self) -> None:
        try:
            NAME_PATH.write_text(self.name_editor.toPlainText(), encoding="utf-8")
            self.statusBar().showMessage("name.txt 已保存", 3000)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"保存 name.txt 失败：{exc}")

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
