import asyncio
import base64
import copy
import hashlib
import json
import logging
import signal
import struct
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
        "monitor": 1,
        "left": 0,
        "top": 0,
        "width": 1920,
        "height": 1080,
    },
    "match": {
        "mode": "contains",
        "case_sensitive": False,
        "min_confidence": 0.5,
    },
    "overlay": {
        "stroke_color": "#ff3b30",
        "line_width": 3,
        "show_label": True,
    },
}

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


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
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
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


class RapidOCREngine:
    def __init__(self) -> None:
        self._engine: Any = None
        self._import_error_logged = False

    def _ensure_engine(self) -> bool:
        if self._engine is not None:
            return True
        try:
            from rapidocr import RapidOCR

            self._engine = RapidOCR()
            logging.info("RapidOCR 初始化完成")
            return True
        except Exception:
            if not self._import_error_logged:
                logging.exception("RapidOCR 初始化失败，将继续运行并发送空框")
                self._import_error_logged = True
            return False

    def recognize(self, image: np.ndarray) -> List[OCRItem]:
        if not self._ensure_engine():
            return []
        try:
            raw = self._engine(image)
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
        logging.info("收到退出信号 %s，正在停止 worker", signum)
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


async def recognition_loop(server: OverlayServer, stop_event: asyncio.Event) -> None:
    ocr = RapidOCREngine()
    with mss.MSS() as sct:
        while not stop_event.is_set():
            started = time.monotonic()
            config = load_config(write_if_missing=True)
            interval = get_interval_seconds(config)

            try:
                region, width, height = resolve_capture_region(sct, config)
                targets = read_targets()

                if not targets:
                    await server.broadcast(build_message(width, height, [], config))
                else:
                    screenshot = sct.grab(region)
                    image = np.asarray(screenshot)[:, :, :3]
                    items = ocr.recognize(image)
                    boxes: List[Dict[str, Any]] = []
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
                                "x": round(float(rect["x"]), 2),
                                "y": round(float(rect["y"]), 2),
                                "w": round(float(rect["w"]), 2),
                                "h": round(float(rect["h"]), 2),
                            }
                        )

                    await server.broadcast(build_message(width, height, boxes, config))
                    logging.debug("本轮 OCR=%s 命中=%s 目标=%s", len(items), len(boxes), len(targets))
            except Exception:
                logging.exception("识别循环失败，程序继续运行")
                try:
                    config = load_config(write_if_missing=True)
                    capture = config.get("capture", {})
                    width = int(capture.get("width", DEFAULT_CONFIG["capture"]["width"]))
                    height = int(capture.get("height", DEFAULT_CONFIG["capture"]["height"]))
                    await server.broadcast(build_message(width, height, [], config))
                except Exception:
                    logging.exception("发送空框失败")

            elapsed = time.monotonic() - started
            wait_seconds = max(0.0, interval - elapsed)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass


async def main_async() -> None:
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
        await server.stop()
        logging.info("worker 已停止")


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
