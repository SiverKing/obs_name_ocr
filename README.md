# OBS Name OCR

这是一个用于 OBS/桌面画面 OCR 命中框显示的 Python 方案。

当前 GUI 版本：`v11`。版本号集中定义在 `gui.py` 顶部的 `APP_VERSION`，修改该常量即可更新窗口标题和工具栏显示。

worker 会读取当前目录的 `name.txt`，按 `config.json` 配置截图并 OCR。命中目标文字后，会通过 WebSocket 推送给 `overlay.html`，也可以按配置开启 Windows 桌面透明覆盖层。

## 文件说明

- `worker.py`：OCR worker，负责截图、识别、匹配、HTTP/WebSocket 服务和可选桌面透明覆盖层。
- `gui.py`：本地桌面 UI，用于编辑 `name.txt`、修改 `config.json`、启动/停止 worker、测试 OBS WebSocket 和查看最近日志。
- `overlay.html`：OBS 浏览器源使用的透明 canvas 画框页面。
- `obs_name_ocr.py`：OBS Python 脚本，只负责启动/停止 worker。
- `config.json`：运行配置。
- `name.txt`：目标文字列表，每行一个目标。
- `requirements.txt`：Python 依赖。
- `logs/YYYYMMDD.log`：运行时按日期生成的统一日志文件（自动创建）。

## 安装依赖

```powershell
cd D:\SiverKing\VSCode\python\obs_name_ocr
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

RapidOCR 默认需要 CPU 版 `onnxruntime`，已写入 `requirements.txt`。项目同时支持 ONNX Runtime CUDA 和 RapidOCR 原生 TensorRT 两条 NVIDIA 加速路径；TensorRT 是可选依赖，因此没有写入 `requirements.txt`。

### NVIDIA 显卡加速

项目支持两种互斥的 NVIDIA 推理后端：

- `onnxruntime`：继续使用 ONNX Runtime，可通过 `use_cuda` 启用 CUDA provider。
- `tensorrt_fp32` / `tensorrt_fp16`：使用 RapidOCR 3.8.4 自带的原生 TensorRT 后端。

这里的“原生 TensorRT”不是 ONNX Runtime 的 `TensorrtExecutionProvider`。即使 `ort.get_available_providers()` 列出了该 provider，项目也不会选择它。

#### ONNX Runtime CUDA

默认安装的是 CPU 版 ONNX Runtime。要让 ONNX Runtime 调用 NVIDIA 显卡，需要换成 GPU 版 `onnxruntime-gpu`，并安装它需要的 CUDA/cuDNN 运行时。

先停止 worker，然后执行：

```powershell
cd D:\SiverKing\VSCode\python\obs_name_ocr
.\venv\Scripts\python.exe -m pip uninstall -y onnxruntime onnxruntime-gpu onnxruntime-directml
.\venv\Scripts\python.exe -m pip install --upgrade "onnxruntime-gpu[cuda,cudnn]"
```

检查 ONNX Runtime 是否能看到 CUDA provider：

```powershell
.\venv\Scripts\python.exe -c "import onnxruntime as ort; print(ort.__version__); print(ort.get_available_providers()); print(ort.get_device())"
```

正常至少应该看到：

```text
['CUDAExecutionProvider', 'CPUExecutionProvider']
```

列表里额外出现 `TensorrtExecutionProvider` 也没关系；本模式仍只使用 CUDA provider。

然后把 `config.json` 里的 OCR 配置改成：

```json
"ocr": {
  "backend": "onnxruntime",
  "use_cuda": true,
  "use_dml": false,
  "use_cls": false,
  "return_word_box": false,
  "reload_files_interval_ms": 2000,
  "log_performance": true,
  "log_performance_interval_ms": 3000
}
```

启动 worker 后，日志里应该出现类似：

```text
已加入 NVIDIA DLL 搜索路径: ...\nvidia\cublas\bin; ...\nvidia\cudnn\bin; ...
尝试预加载 ONNX Runtime CUDA/cuDNN DLL
RapidOCR ONNX Runtime providers: {'det': ['CUDAExecutionProvider', 'CPUExecutionProvider'], 'cls': ['CUDAExecutionProvider', 'CPUExecutionProvider'], 'rec': ['CUDAExecutionProvider', 'CPUExecutionProvider']}
```

如果只看到 `CPUExecutionProvider`，说明当前仍是 CPU 推理。

常见问题：

- `onnxruntime is not installed`：没有安装 ONNX Runtime，执行 `pip install -r requirements.txt` 或安装 GPU 版。
- `CUDAExecutionProvider is not in available providers`：装的是 CPU 版 `onnxruntime`，需要卸载后安装 `onnxruntime-gpu[cuda,cudnn]`。
- `cublasLt64_13.dll is missing`：GPU 版 ONNX Runtime 已安装，但 CUDA 运行库缺失或不在路径里。优先用 `pip install --upgrade "onnxruntime-gpu[cuda,cudnn]"` 补齐。
- `Could not locate cudnn_engines_tensor_ir64_9.dll` 或 `CUDNN_STATUS_SUBLIBRARY_LOADING_FAILED`：cuDNN 子 DLL 没被加载。当前 worker 会自动把 `venv\Lib\site-packages\nvidia\*\bin` 加入进程 DLL 搜索路径和 `PATH`，请确认你运行的是最新 `worker.py`。
- `CUDAExecutionProvider` 后面仍带着 `CPUExecutionProvider` 是正常的，CPU 是 fallback；只要 provider 列表里 CUDA 在前面，说明会优先走显卡。

实测 2560x1440 原图从数秒级 OCR 降到约 `ocr=470-580ms`。如果仍然慢，优先检查输入分辨率、OBS WebSocket 截图耗时和命中框数量。

#### RapidOCR 原生 TensorRT

本项目支持：

| 配置值 | RapidOCR 后端 | 精度 |
| --- | --- | --- |
| `tensorrt_fp32` | Det、Rec 使用 `EngineType.TENSORRT` | FP32，结果与当前 ONNX Runtime 最接近 |
| `tensorrt_fp16` | Det、Rec 使用 `EngineType.TENSORRT` | FP16，通常更快，阈值附近可能有微小浮点差异 |

当 `use_cls=true` 时，Cls 也切换到 TensorRT；`use_cls=false` 时，未使用的 Cls 保持默认 ONNX Runtime，避免额外构建 Cls Engine。

已实测成功的环境：

- Python `3.12.9`
- RapidOCR `3.8.4`
- TensorRT cu12 `10.16.1.11`
- cuda-python / cuda-bindings `12.9.7`
- CUDA Runtime `12.9`
- NVIDIA GeForce RTX 2070 8GB，Compute Capability `7.5`（`sm75`）

停止 worker 后，由用户手动安装可选依赖：

```powershell
cd D:\SiverKing\VSCode\python\obs_name_ocr
.\venv\Scripts\python.exe -m pip install "tensorrt-cu12>=10,<11" "cuda-python>=12.9,<13"
```

不需要卸载当前可用的 RapidOCR、`onnxruntime-gpu` 或 CUDA 12 Python 运行库。安装后做只读导入验证：

```powershell
.\venv\Scripts\python.exe -m pip check
.\venv\Scripts\python.exe -c "import tensorrt as trt; from cuda.bindings import runtime as cudart; print('TensorRT:', trt.__version__); print('CUDA Runtime:', cudart.cudaRuntimeGetVersion())"
.\venv\Scripts\python.exe -c "from rapidocr import EngineType; print(EngineType.TENSORRT, type(EngineType.TENSORRT))"
```

可以在 GUI 的“OCR 设置”里选择：

- 关闭 TensorRT（ONNX Runtime）
- TensorRT FP32（准确度优先）
- TensorRT FP16（速度优先）

也可以直接修改配置：

```json
"ocr": {
  "backend": "tensorrt_fp32",
  "use_cuda": true,
  "use_dml": false,
  "use_cls": false,
  "return_word_box": false,
  "reload_files_interval_ms": 2000,
  "log_performance": true,
  "log_performance_interval_ms": 3000
}
```

TensorRT 模式不会使用或传递 `use_cuda` / `use_dml`；GUI 会禁用这两个 ONNX Runtime 专属控件，但不会清空原勾选值，切回 ONNX Runtime 后会恢复。

首次启动会同步构建 Det、Rec Engine。TensorRT 在 `Building TensorRT engine (this may take a few minutes)...` 后可能较长时间没有进度输出，这不是必然卡死。不要提前终止：Engine 只有完整构建成功后才写入磁盘，中断当前阶段后，下次仍会从该阶段重新构建。

RapidOCR 默认把缓存写到：

```text
venv\Lib\site-packages\rapidocr\models\models\
```

FP32 和 FP16 使用不同文件，例如：

```text
ch_PP-OCRv4_det_mobile_sm75_fp32.engine
ch_PP-OCRv4_rec_mobile_sm75_fp32.engine
ch_PP-OCRv4_det_mobile_sm75_fp16.engine
ch_PP-OCRv4_rec_mobile_sm75_fp16.engine
```

同一模型、GPU 架构和精度再次启动时会直接加载缓存。Engine 不应提交到 Git，也不建议复制到不同 GPU、TensorRT 或 CUDA 环境复用。

TensorRT 常见问题和本次实际踩坑：

- **每次都停在 Building**：通常是上一次在构建完成前手动终止，尚未产生 Engine。让 Det、Rec 至少完整构建一次；TensorRT 不支持断点续编。
- **FP32 已构建，切换 FP16 又开始 Building**：正常。两种精度使用独立缓存，首次都要分别构建。
- **`engine_type` 不能传字符串**：RapidOCR 3.8.4 要求 `rapidocr.EngineType.TENSORRT` 枚举。worker 已统一处理，配置文件只填写上述 `backend` 字符串。
- **`TRTInferSession object has no attribute model_root_dir`**：RapidOCR 3.8.4 原生 TensorRT 初始化顺序缺陷。worker 会仅在当前进程为 3.8.4 应用兼容初始化，不修改 `site-packages`，也不覆盖 RapidOCR 的缓存、workspace 或动态 shape 配置。
- **缺少 `tensorrt` 或 `cuda.bindings.runtime`**：分别检查 `tensorrt-cu12` 和 `cuda-python`。程序只会输出中文错误，不会自动安装或伪装成 TensorRT 已启用。
- **把它误认为 ONNX Runtime TensorRT EP**：本项目不使用该 EP。TensorRT 模式由 RapidOCR 的 `TRTInferSession` 直接构建和执行 Engine。
- **热切换失败**：已有 OCR 引擎仍可用时，worker 会恢复旧引擎，并记录用户请求的后端、失败原因和继续运行的实际后端；首次初始化失败时则发送空框。
- **源码更新后仅保存配置没有生效**：`config.json` 会热重载，但 Python 源码不会。修改 `worker.py` 后必须重启 worker。

## 启动桌面 UI

```powershell
cd D:\SiverKing\VSCode\python\obs_name_ocr
.\venv\Scripts\python.exe .\gui.py
```

桌面 UI 不需要浏览器页面板。它可以直接编辑 `name.txt`，用中文表单修改 `config.json`，启动或停止 `worker.py`，测试 OBS WebSocket 连接，获取 OBS 场景和输入源并回填 `source_name` / `source_uuid`，底部会自动刷新运行目录 `logs/YYYYMMDD.log` 的最近 5 行。

worker、GUI 启动的子进程输出以及 OBS 脚本启动的 worker 输出统一写入运行目录的 `logs` 文件夹，并按本地日期生成文件，例如 `logs/20260806.log`。

如果没有使用项目自带虚拟环境，也可以用当前 Python 运行：

```powershell
python .\gui.py
```

## 启动 worker

```powershell
cd D:\SiverKing\VSCode\python\obs_name_ocr
.\venv\Scripts\python.exe .\worker.py
```

运行日志会写入当前目录的 `logs/YYYYMMDD.log`，例如 `logs/20260806.log`；跨日期运行时 worker 日志会切换到当天文件。

停止时按 `Ctrl+C`。如果终端仍未释放，可用下面命令查占用端口的 PID：

```powershell
netstat -ano | findstr :8765
taskkill /PID 进程号 /F
```

## name.txt

规则：

- UTF-8 编码。
- 每行一个目标。
- 空行会被忽略。
- `#` 开头的行是分组标签；目标使用它上方最近的分组。
- 有分组时，识别框标签显示为 `分组-目标`；没有分组时仍显示原目标名。
- worker 会运行中反复读取，改完文件后不需要重启。

示例：

```text
# 管理员
张三
目标玩家
# 黑名单
某个ID
```

## config.json 参数

### 基础服务

```json
"interval_ms": 1000,
"host": "127.0.0.1",
"port": 8765
```

- `interval_ms`：识别间隔，单位毫秒。默认 `1000`，即每秒一次。
- `host`：HTTP/WebSocket 监听地址。只给本机 OBS 用时建议 `127.0.0.1`。
- `port`：服务端口。OBS 浏览器源 URL 要对应这个端口。

如果 `host` 填 `0.0.0.0`，局域网其他设备也可能访问这个服务；不需要远程访问时不要这样填。

### 截图区域 capture

```json
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
    "image_width": 1920,
    "image_height": 1080,
    "image_compression_quality": 80
  }
}
```

- `source`：截图来源。
  - `screen`：使用 mss 截 Windows 屏幕区域，默认值。
  - `obs_websocket`：通过 OBS WebSocket 的 `GetSourceScreenshot` 获取 OBS 源或场景截图。
- `monitor`：第几块屏幕。通常 `1` 是第一块显示器，`2` 是第二块显示器，`0` 是所有显示器合成的大虚拟桌面。
- `left`：在指定显示器内部向右偏移的像素。
- `top`：在指定显示器内部向下偏移的像素。
- `width`：截图宽度。
- `height`：截图高度。

当 `source` 是 `screen` 时，使用 `monitor/left/top/width/height`。

当 `source` 是 `obs_websocket` 时，使用 `obs_websocket` 配置：

- `url`：OBS WebSocket 地址，默认 `ws://127.0.0.1:4455`。
- `password`：OBS WebSocket 密码。没有密码就留空。
- `source_name`：OBS 源名或场景名，例如 `显示器采集` 或 `场景`。
- `source_uuid`：OBS 源 UUID。优先级高于 `source_name`，可留空。
- `image_format`：截图格式，建议 `png`。
- `image_width` / `image_height`：请求 OBS 输出的截图尺寸。填 `0` 时由 OBS 按源尺寸返回。
- `image_compression_quality`：截图质量，主要影响 jpg，png 影响较小。

OBS WebSocket 模式示例：

```json
"capture": {
  "source": "obs_websocket",
  "left": 0,
  "top": 0,
  "width": 2560,
  "height": 1440,
  "obs_websocket": {
    "url": "ws://127.0.0.1:4455",
    "password": "",
    "source_name": "显示器采集",
    "source_uuid": "",
    "image_format": "png",
    "image_width": 2560,
    "image_height": 1440,
    "image_compression_quality": 80
  }
}
```

OBS WebSocket 模式下，OCR 坐标基于 OBS 返回图片的尺寸。OBS 浏览器源宽高建议和 `image_width/image_height` 保持一致。

列出 OBS 场景、输入源和 UUID：

```powershell
.\venv\Scripts\python.exe .\list_obs_sources.py
```

如果 OBS WebSocket 有密码：

```powershell
.\venv\Scripts\python.exe .\list_obs_sources.py ws://127.0.0.1:4455 你的密码
```

查看 mss 识别到的屏幕列表：

```powershell
.\venv\Scripts\python.exe -c "import mss, json; s=mss.MSS(); print(json.dumps(s.monitors, ensure_ascii=False, indent=2))"
```

输出数组里：

- 第 `0` 项对应 `monitor: 0`，是所有屏幕合成区域。
- 第 `1` 项对应 `monitor: 1`。
- 第 `2` 项对应 `monitor: 2`。

### Windows 分辨率和缩放

配置里填写的是截图像素，不是 Windows UI 缩放后的逻辑尺寸。

常见填写：

- Windows 分辨率 `1920 x 1080`，缩放 `100% / 125% / 150%`，通常仍填 `1920 x 1080`。
- Windows 分辨率 `2560 x 1440`，通常填 `2560 x 1440`。
- Windows 分辨率 `3840 x 2160`，通常填 `3840 x 2160`。

Windows 缩放会让文字和窗口变大，但截图区域仍按实际像素处理。

### 匹配规则 match

```json
"match": {
  "mode": "contains",
  "case_sensitive": false,
  "min_confidence": 0.5
}
```

- `mode`：
  - `contains`：OCR 结果包含目标文字就算命中，推荐默认值。
  - `exact`：OCR 结果必须和目标完全一致。
- `case_sensitive`：是否大小写敏感。
- `min_confidence`：最低 OCR 置信度。漏识别可降到 `0.3`，误框多可升到 `0.7`。

### 匹配容错 match_tolerance

```json
"match_tolerance": {
  "enabled": true,
  "normalize_confusable": true,
  "collapse_repeated_chars": true,
  "ignore_separators": true,
  "max_edit_distance": 1,
  "fuzzy_enabled": true,
  "fuzzy_threshold": 0.88,
  "fuzzy_min_length": 4
}
```

这组配置只在原始 `match` 没命中时生效，用来处理 OCR 把字符读错或漏掉重复字符的情况。

- `enabled`：是否启用匹配容错。
- `normalize_confusable`：归一化易混字符，例如 `1/l/I`、`0/O`、`5/S`。
- `collapse_repeated_chars`：压缩连续重复字符，例如 `asdopjkkf` 和 `asdopjkf` 会按同一结果比较。
- `ignore_separators`：忽略 `_`、`-`、空格等分隔符，适合 OCR 漏读尾部符号的情况。
- `max_edit_distance`：允许的最大编辑距离，默认 `1`，可处理 `Nuo1i_` 被识别成 `Nuoi_` 这类少读一个字符的情况。
- `fuzzy_enabled`：是否启用相似度匹配兜底。
- `fuzzy_threshold`：相似度命中阈值，默认 `0.88`。误报多就调高，仍漏识别就小幅调低。
- `fuzzy_min_length`：目标文字长度小于该值时不做相似度匹配，避免短目标误报。

### OCR 原始输出 ocr_output

```json
"ocr_output": {
  "enabled": true
}
```

- `enabled`：是否输出 `ocr_output.txt`。开启后 worker 每轮覆盖写入最近一次 OCR 原始识别内容，用于排查“识别错了”还是“没识别到”。

### OCR 性能 ocr

```json
"ocr": {
  "backend": "onnxruntime",
  "use_cuda": false,
  "use_dml": false,
  "use_cls": false,
  "return_word_box": false,
  "reload_files_interval_ms": 2000,
  "log_performance": true,
  "log_performance_interval_ms": 3000
}
```

- `backend`：OCR 推理后端。合法值只有 `onnxruntime`、`tensorrt_fp32`、`tensorrt_fp16`。旧配置缺少该字段或填写非法值时会安全回退到 `onnxruntime`；非法值会记录明确警告。
- `use_cuda`：仅在 `backend=onnxruntime` 时生效。让 RapidOCR 的 ONNX Runtime 优先使用 NVIDIA CUDA；需要安装 GPU 版 `onnxruntime`，并且 provider 列表里出现 `CUDAExecutionProvider`。
- `use_dml`：仅在 `backend=onnxruntime` 时生效。使用 Windows DirectML provider，需要安装 `onnxruntime-directml`。如果你是 NVIDIA 显卡，通常优先尝试 CUDA。
- `use_cls`：是否启用文字方向分类。水平文字场景建议 `false`，速度更快。
- `return_word_box`：是否返回词级框。当前匹配不需要，建议 `false`。
- `reload_files_interval_ms`：重新读取 `config.json` 和 `name.txt` 的间隔。默认 `2000`。
- `log_performance`：是否在终端输出每轮耗时，例如截图、OCR、总耗时。
- `log_performance_interval_ms`：性能日志输出间隔，避免每轮刷屏。

`backend`、TensorRT 精度、TensorRT 模式下的 `use_cls`，以及 ONNX Runtime 的 CUDA/DirectML 参数发生变化时，worker 会按热重载重新初始化 OCR 引擎。FP32 与 FP16 参数不同，因此互相切换一定会重建或加载对应精度的 Engine。

性能优化建议：

- OBS WebSocket 模式下优先降低 `capture.obs_websocket.image_width/image_height`，例如 `1280x720` 或 `960x540`。
- 可以把 `image_format` 改成 `jpg`，`image_compression_quality` 设置 `60-80`。
- 截图尺寸越大，OCR 越慢。2K/4K 原图通常明显慢于 720p。
- 如果小字识别变差，再把 `image_width/image_height` 调高。

### 画框样式 overlay

```json
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
    "#ff2d55"
  ],
  "line_width": 3,
  "show_label": true
}
```

- `stroke_color`：框颜色。
- `color_mode`：颜色模式。
  - `single`：所有命中框使用同一个 `stroke_color`。
  - `by_target`：根据当前 `name.txt` 动态生成高区分度颜色，每个不同目标分配一个不重复颜色；目标列表不变时颜色保持稳定。
- `color_palette`：保留用于旧配置兼容；当前动态 `by_target` 模式不再受固定颜色池数量限制。
- `line_width`：框线宽度。
- `show_label`：是否显示命中的文字标签。

这个配置同时影响 OBS 浏览器源和桌面透明覆盖层。

### 桌面透明覆盖层 desktop_overlay

```json
"desktop_overlay": {
  "enabled": false,
  "click_through": true,
  "hide_when_empty": true,
  "debug_border": false,
  "coordinate_mode": "capture",
  "screen_region": {
    "left": 0,
    "top": 0,
    "width": 1920,
    "height": 1080
  },
  "topmost": true,
  "transparent_color": "#010101"
}
```

- `enabled`：是否开启 Windows 桌面透明覆盖层。
  - `false`：只推送给 OBS 浏览器源。
  - `true`：额外创建一个置顶透明窗口，直接覆盖到 Windows 屏幕上的截图区域。
- `click_through`：是否点击穿透。建议 `true`，这样鼠标不会点到透明层。
- `hide_when_empty`：没有命中框时隐藏透明层窗口。建议 `true`，便于排查点击穿透问题。
- `debug_border`：调试边框。设为 `true` 时，即使没有识别命中也会画出透明层外框，用来检查覆盖位置和点击穿透。
- `coordinate_mode`：桌面透明层坐标模式。
  - `capture`：默认模式。透明层直接使用截图来源尺寸和 `capture.left/top` 定位。适合屏幕截图，或 OBS 源本身就对应整块屏幕。
  - `screen_region`：屏幕映射模式。把 OCR 坐标按比例映射到 `screen_region` 指定的 Windows 屏幕区域。适合 OBS WebSocket 的窗口采集源没有全屏显示在你的屏幕上的情况。
- `screen_region`：当 `coordinate_mode` 为 `screen_region` 时使用。可以填 `"auto"` 自动获取，或者手动填写目标画面在 Windows 屏幕上的实际位置和大小。
- `topmost`：是否置顶。建议 `true`。
- `transparent_color`：透明背景色。保持默认即可，除非你的画面正好需要显示这个颜色。

如果你想让红框直接显示在自己的屏幕上，打开：

```json
"desktop_overlay": {
  "enabled": true,
  "click_through": true,
  "hide_when_empty": true,
  "debug_border": false,
  "coordinate_mode": "capture",
  "screen_region": {
    "left": 0,
    "top": 0,
    "width": 1920,
    "height": 1080
  },
  "topmost": true,
  "transparent_color": "#010101"
}
```

这不是 OBS 浏览器源，而是 worker 自己创建的桌面透明层。

### OBS 源和桌面坐标偏移

当 `capture.source` 是 `obs_websocket` 时，OCR 看到的是 OBS 返回的源截图。这个坐标不一定等于 Windows 屏幕坐标。

例如你选择 OBS 的“窗口采集”作为来源：

- OBS 返回的截图坐标：从被采集窗口图像左上角开始。
- 桌面透明层需要的坐标：从 Windows 屏幕左上角开始。

如果被采集窗口不是全屏铺在屏幕上，直接用 `capture` 坐标会发生偏移。此时把桌面透明层改成屏幕映射模式：

自动获取窗口位置：

```json
"desktop_overlay": {
  "enabled": true,
  "coordinate_mode": "screen_region",
  "screen_region": "auto"
}
```

自动模式适合 OBS WebSocket 来源是 `window_capture`，worker 会读取 OBS 的窗口采集设置，例如 `微信:Qt51514QWindowIcon:Weixin.exe`，然后在 Windows 当前可见窗口里匹配这个窗口并获取实际屏幕矩形。

如果自动匹配不准，再手动填写：

```json
"desktop_overlay": {
  "enabled": true,
  "coordinate_mode": "screen_region",
  "screen_region": {
    "left": 320,
    "top": 180,
    "width": 1280,
    "height": 720
  }
}
```

列出当前 Windows 可见窗口，辅助判断自动匹配为什么失败：

```powershell
.\venv\Scripts\python.exe .\list_windows.py
```

这里的 `left/top/width/height` 填“这个窗口画面实际显示在 Windows 屏幕上的区域”。worker 会把 OBS 源截图里的命中框按比例映射到这个区域。

## OBS 浏览器源用法

1. 启动 worker。
2. 在 OBS 添加浏览器源。
3. URL 填：

```text
http://127.0.0.1:8765/overlay.html
```

4. 浏览器源宽高填成截图区域大小，例如 `1920 x 1080` 或 `2560 x 1440`。
5. 把浏览器源放在目标捕获源上方。

示例源顺序：

```text
浏览器源 overlay.html
游戏捕获 / 窗口捕获 / 显示器捕获
```

红框会出现在 OBS 合成画面里，也会进入录制和直播输出。

## 桌面透明层和 OBS 浏览器源的区别

OBS 浏览器源：

- 只显示在 OBS 画布里。
- 会进入 OBS 录制/直播。
- 不会直接覆盖你的 Windows 桌面或游戏窗口。

桌面透明覆盖层：

- 直接显示在 Windows 屏幕上。
- 方便你自己看命中框。
- 不一定会被游戏捕获源捕获到。
- 如果 OBS 使用显示器捕获，通常能捕获到这个透明层；如果 OBS 使用游戏捕获，可能捕获不到。

## OBS 捕获源选择

`worker.py` 支持两类截图来源：

- `screen`：使用 Windows 屏幕区域截图，按 `monitor/left/top/width/height` 读取。
- `obs_websocket`：通过 OBS WebSocket 5.x 的 `GetSourceScreenshot` 按 `source_name` 或 `source_uuid` 获取 OBS 场景或输入源截图。

推荐用 `gui.py` 选择 OBS 来源：

1. 启动 OBS，并确认“工具 > WebSocket 服务器设置”已开启。
2. 运行 `python .\gui.py`。
3. 在“截图来源”里把 `source` 设为 `obs_websocket`。
4. 填写 OBS WebSocket `url` 和 `password`。
5. 点击“测试 OBS 连接”。
6. 点击“获取 OBS 捕获对象”，选择场景或输入源后会自动回填 `source_name` 和 `source_uuid`。
7. 点击“保存配置”。

`source_uuid` 优先级高于 `source_name`。如果 OBS 里重命名了来源但 UUID 没变，保留 UUID 通常更稳定。

OBS WebSocket 截图适合 500ms 到数秒级的 OCR 轮询，不是高帧率实时帧流。频率太高时，优先降低 `interval_ms` 的压力、降低 `image_width/image_height`，或改用 `jpg` 并适当调低 `image_compression_quality`。
