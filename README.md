# OBS Name OCR

这是一个用于 OBS/桌面画面 OCR 命中框显示的 Python 方案。

worker 会读取当前目录的 `name.txt`，按 `config.json` 配置截图并 OCR。命中目标文字后，会通过 WebSocket 推送给 `overlay.html`，也可以按配置开启 Windows 桌面透明覆盖层。

## 文件说明

- `worker.py`：OCR worker，负责截图、识别、匹配、HTTP/WebSocket 服务和可选桌面透明覆盖层。
- `overlay.html`：OBS 浏览器源使用的透明 canvas 画框页面。
- `obs_name_ocr.py`：OBS Python 脚本，只负责启动/停止 worker。
- `config.json`：运行配置。
- `name.txt`：目标文字列表，每行一个目标。
- `requirements.txt`：Python 依赖。

## 安装依赖

```powershell
cd D:\SiverKing\VSCode\python\obs_name_ocr
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

RapidOCR 默认需要 `onnxruntime`，已写入 `requirements.txt`。

## 启动 worker

```powershell
cd D:\SiverKing\VSCode\python\obs_name_ocr
.\venv\Scripts\python.exe .\worker.py
```

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
- `#` 开头的行会被当作注释忽略。
- worker 会运行中反复读取，改完文件后不需要重启。

示例：

```text
张三
目标玩家
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
  - `by_target`：不同目标文字使用不同颜色；同一个目标每次颜色稳定一致。
- `color_palette`：`by_target` 模式使用的颜色池。目标文字会被稳定映射到其中一个颜色。
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

## 能不能直接指定 OBS 某个捕获源

当前 `worker.py` 还不能直接指定 OBS 内部某一个捕获源作为 OCR 输入，它现在使用的是 Windows 屏幕区域截图。

如果指的是 OBS Python 脚本 API 直接拿实时源纹理像素，这不是一个适合本项目的常规路线。OBS 的源画面在 OBS 渲染管线内部，直接拿源画面通常需要走插件、渲染回调或 GPU 纹理读回，容易接近 C/C++ 插件方案。

如果可以接受通过 OBS WebSocket 取低频截图，OBS WebSocket 5.x 协议提供 `GetSourceScreenshot`，可以按 `sourceName` 或 `sourceUuid` 获取输入源或场景的 Base64 截图。这适合后续做“选择 OBS 源后每 1000ms 截图 OCR”的方案，但它不是高帧率实时视频帧流，频率太高会有性能和延迟成本。

当前项目第一版刻意不做 C++ 插件，也不阻塞 OBS 渲染线程，所以采用屏幕区域截图方案。

## 后续可做的捕获源选择 GUI 方案

可以做一个独立 GUI，让你像 OBS 添加捕获源一样选择：

- 显示器捕获：列出所有显示器。
- 窗口捕获：列出当前可见窗口标题。
- 区域捕获：手动输入或拖拽选择 `left/top/width/height`。
- OBS 源截图：连接 OBS WebSocket，列出 OBS 场景、输入源和场景项，选择后使用 `GetSourceScreenshot` 做低频 OCR。

第一版推荐实现路线：

1. 增加 `capture.source_type`：
   - `monitor`
   - `window`
   - `region`
2. 增加一个 `selector.py`：
   - 用 Tkinter 显示选择窗口。
   - 显示 monitor 列表。
   - 显示可见窗口标题列表。
   - 选择后写回 `config.json`。
3. `worker.py` 仍然使用 mss 截图：
   - monitor 模式截整块显示器。
   - region 模式截固定区域。
   - window 模式根据窗口句柄获取窗口矩形，然后按这个矩形截图。
   - obs_source 模式通过 OBS WebSocket 请求源截图。

这个 GUI 方案可以做到“体验接近 OBS 的选择源”。其中 monitor/window/region 本质仍是 Windows 屏幕/窗口区域截图；obs_source 模式则依赖 OBS WebSocket 的源截图接口。

如果确实要读取 OBS 内部源的实时帧流，建议另开一个 C++ OBS 插件；如果只是 1000ms 左右的 OCR，OBS WebSocket 截图模式可以作为下一版优先方案。
