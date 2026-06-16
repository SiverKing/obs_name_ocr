import copy
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import obspython as obs


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

worker_process: Optional[subprocess.Popen] = None
worker_log_handle = None
script_settings = None


def script_dir() -> Path:
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


def default_worker_path() -> str:
    return str(script_dir() / "worker.py")


def default_python_path() -> str:
    return str(script_dir() / "venv" / "Scripts" / "python.exe")


def config_path_for_worker(worker_path: str) -> Path:
    if worker_path:
        return Path(worker_path).resolve().parent / "config.json"
    return script_dir() / "config.json"


def deep_merge(defaults: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def log(level: int, message: str) -> None:
    obs.script_log(level, f"[obs_name_ocr] {message}")


def get_setting_string(settings, key: str, default: str) -> str:
    if settings is None:
        return default
    value = obs.obs_data_get_string(settings, key)
    return value or default


def get_interval_ms(settings) -> int:
    if settings is None:
        return 1000
    value = obs.obs_data_get_int(settings, "interval_ms")
    if value <= 0:
        return 1000
    return int(value)


def update_config_interval(worker_path: str, interval_ms: int) -> None:
    path = config_path_for_worker(worker_path)
    config = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                config = deep_merge(config, existing)
        except Exception as exc:
            log(obs.LOG_WARNING, f"读取 config.json 失败，将用默认配置覆盖 interval_ms: {exc}")

    config["interval_ms"] = interval_ms
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(obs.LOG_INFO, f"已更新识别频率: {interval_ms}ms")


def is_worker_running() -> bool:
    return worker_process is not None and worker_process.poll() is None


def start_worker(props=None, prop=None) -> bool:
    global worker_process, worker_log_handle

    if is_worker_running():
        log(obs.LOG_INFO, "worker 已在运行")
        return True

    settings = script_settings
    worker_path = get_setting_string(settings, "worker_path", default_worker_path())
    python_path = get_setting_string(settings, "venv_python", default_python_path())
    interval_ms = get_interval_ms(settings)

    if not Path(worker_path).is_file():
        log(obs.LOG_ERROR, f"worker.py 不存在: {worker_path}")
        return True
    if not Path(python_path).is_file():
        log(obs.LOG_ERROR, f"venv python 不存在: {python_path}")
        return True

    try:
        update_config_interval(worker_path, interval_ms)
        log_path = Path(worker_path).resolve().parent / "obs_worker.log"
        worker_log_handle = log_path.open("a", encoding="utf-8")
        worker_log_handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] start worker\n")
        worker_log_handle.flush()

        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        worker_process = subprocess.Popen(
            [python_path, worker_path],
            cwd=str(Path(worker_path).resolve().parent),
            stdout=worker_log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        log(obs.LOG_INFO, f"worker 已启动，PID={worker_process.pid}")
    except Exception as exc:
        log(obs.LOG_ERROR, f"启动 worker 失败: {exc}")
        close_worker_log()

    return True


def close_worker_log() -> None:
    global worker_log_handle
    if worker_log_handle is not None:
        try:
            worker_log_handle.close()
        except Exception:
            pass
        worker_log_handle = None


def stop_worker(props=None, prop=None) -> bool:
    global worker_process

    process = worker_process
    if process is None:
        close_worker_log()
        log(obs.LOG_INFO, "worker 未运行")
        return True

    if process.poll() is None:
        log(obs.LOG_INFO, "正在停止 worker")
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log(obs.LOG_WARNING, "worker 未及时退出，执行 kill")
            process.kill()
            process.wait(timeout=5)
        except Exception as exc:
            log(obs.LOG_ERROR, f"停止 worker 失败: {exc}")

    worker_process = None
    close_worker_log()
    log(obs.LOG_INFO, "worker 已停止")
    return True


def script_description() -> str:
    return (
        "OBS Name OCR 控制脚本。\n"
        "只负责启动/停止当前目录 venv 下的 worker.py，不在 OBS 渲染线程中执行 OCR。"
    )


def script_defaults(settings) -> None:
    obs.obs_data_set_default_int(settings, "interval_ms", 1000)
    obs.obs_data_set_default_string(settings, "worker_path", default_worker_path())
    obs.obs_data_set_default_string(settings, "venv_python", default_python_path())


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_int(props, "interval_ms", "识别频率 interval_ms (ms)", 100, 60000, 100)
    obs.obs_properties_add_path(
        props,
        "worker_path",
        "worker.py 路径",
        obs.OBS_PATH_FILE,
        "Python (*.py);;All files (*.*)",
        str(script_dir()),
    )
    obs.obs_properties_add_path(
        props,
        "venv_python",
        "venv python 路径",
        obs.OBS_PATH_FILE,
        "python.exe (*.exe);;All files (*.*)",
        str(script_dir() / "venv" / "Scripts"),
    )
    obs.obs_properties_add_button(props, "start_worker", "启动 worker", start_worker)
    obs.obs_properties_add_button(props, "stop_worker", "停止 worker", stop_worker)
    return props


def script_update(settings) -> None:
    global script_settings
    script_settings = settings


def script_unload() -> None:
    stop_worker()
