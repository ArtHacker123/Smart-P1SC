# Smart-P1SC

Bambu Lab P1S 本地打印异常监控与处置回路。程序常驻读取打印机摄像头与 MQTT 遥测，在本地运行 YOLO/ONNX 异常检测，确认异常后自动暂停并进入安全保持，通过 OpenClaw/飞书通知用户；用户在限定时间内决定继续、停止或标记误报。

> 当前版本 `0.1.0`。核心监控、事件状态机、CLI、本地 HTTP、通知与模型工具已实现并有自动化测试覆盖。仓库不包含真实数据集和训练后的 ONNX 权重，需自行训练或部署模型。

## 功能

- 独占 P1S 摄像头（TCP 6000，TLS MJPEG），提供本地 MJPEG 重播和快照。
- 通过 MQTT（TCP 8883，TLS）读取打印状态、层号、温度、HMS 等遥测，并发送暂停/继续/停止命令。
- 本地 ONNX 推理，支持 YOLOv8 等单输出检测模型；无模型时 `auto` 后端回退到空检测器，不影响摄像头与遥测。
- 候选帧 + 多帧确认两级检测，结合层号、喷嘴温度等遥测做门控，降低单帧误报。
- 事件状态机：确认异常 -> 暂停 -> 安全保持 -> 定时提醒 -> 超时自动停止。
- 支持继续、停止、误报三种处置，要求事件 ID/短码双重校验，支持幂等与审计。
- SQLite 持久化事件、事件流水、人工标签和通知发件箱；保存异常前后胶片条。
- OpenClaw/飞书通知，通知失败不影响本地暂停/停止安全路径。
- Operator CLI、本地状态页、JSON 输出和稳定退出码。
- 模型训练/导出/检查工具，统一生成 ONNX 和 sidecar 元数据。

## 工作原理

```text
P1S Camera :6000 --TLS--> CameraStream --ring buffer--> FrameAnalyzer
P1S MQTT   :8883 --TLS--> telemetry / command executor

FrameAnalyzer -> DetectionConfirmer -> IncidentManager -> SQLite + filmstrip
                                             |-> pause / resume / stop
                                             |-> notification outbox -> OpenClaw -> Feishu
                                             |-> local API 127.0.0.1:8787 -> browser / CLI
```

默认安全策略：

- P1 级异常：暂停打印，喷嘴降温到 150 C，热床按配置保持。
- 保持期间在 T+10、T+50 分钟提醒，T+60 分钟仍未处置则自动停止。
- 暂停失败时默认执行失败安全停止。
- P2 先记录观察，可在置信度/面积升级后转为 P1。
- P3（包括 `structural_anomaly`）仅记录，不自动暂停。
- OpenClaw 不在安全关键路径上；通知失败不会阻止本地暂停/停止。

## 安全边界

本项目是辅助监控工具，不是消防、烟雾、电气或人身安全设备，也未经过安全认证。不要仅依赖它进行无人值守打印。保持打印机原有安全措施、现场看护和保险有效。

- 仅支持单台 P1S，不发起新打印，不切片，不暴露原始 G-code/MQTT。
- 不做云推理，不上传摄像头画面。
- 检测效果受光照、遮挡、摄像头分辨率（约 1 fps）和模型质量影响。
- `structural_anomaly` 当前只记录，不触发暂停。

## 环境要求

- Windows 10/11（当前主要开发和自启动脚本面向 Windows）。
- Python 3.8 或更高版本。
- 打印机与运行主机在同一局域网。
- P1S 开启 LAN Only Mode，并准备 Access Code。
- 可选：OpenClaw Gateway、飞书插件，用于通知和回复处置。

P1S 摄像头通常只允许一个客户端连接。daemon 运行时会独占摄像头；此时不要再让 Bambu Handy、Bambu Studio 或其他程序连接摄像头。安装检查 `probe` 应在 daemon 停止时运行。

## 安装

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[mqtt,vision]"
```

开发环境：

```powershell
python -m pip install -e ".[mqtt,vision,dev]"
```

训练环境（仅训练/导出模型时需要）：

```powershell
python -m pip install -e ".[training]"
```

依赖说明：

- `mqtt`：`paho-mqtt`，连接打印机 MQTT。
- `vision`：Pillow、NumPy、ONNX Runtime，运行检测与快照。
- `training`：Ultralytics、ONNX、ONNX Runtime，训练与导出。
- `dev`：pytest、ruff、mypy、PyYAML 类型存根。

## 配置

复制示例配置并填写打印机信息：

```powershell
Copy-Item config.example.json config.json
```

至少修改：

```json
{
  "printer": {
    "host": "192.168.1.42",
    "serial": "01P00A000000000",
    "lan_only": true
  }
}
```

`host` 是打印机局域网 IP，`serial` 是打印机序列号。`access_code` 不要写进配置文件，使用环境变量或受 ACL 保护的 `access_code_file`。

### 密钥

PowerShell 当前会话：

```powershell
$env:SMART_P1SC_ACCESS_CODE = "打印机 Access Code"
$env:SMART_P1SC_API_TOKEN = "本地 API Token"
$env:SMART_P1SC_OPENCLAW_TOKEN = "OpenClaw Hook Token"
```

持久化到当前 Windows 用户（计划任务需要用户级环境变量）：

```powershell
[Environment]::SetEnvironmentVariable("SMART_P1SC_ACCESS_CODE", "打印机 Access Code", "User")
[Environment]::SetEnvironmentVariable("SMART_P1SC_API_TOKEN", "本地 API Token", "User")
[Environment]::SetEnvironmentVariable("SMART_P1SC_OPENCLAW_TOKEN", "OpenClaw Hook Token", "User")
```

生成随机 token：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

建议 API token 与 OpenClaw token 分别生成。设置用户级环境变量后，需要重新打开终端或重新登录，计划任务才会读取到新值。

配置文件查找顺序：

1. `--config <path>`
2. 环境变量 `SMART_P1SC_CONFIG`
3. 当前工作目录下的 `config.json`

### 主要配置节

| 配置节 | 用途 |
| --- | --- |
| `printer` | 打印机 IP、序列号、Access Code 来源、LAN Only |
| `camera` | 摄像头端口、超时、重连、帧缓冲 |
| `mqtt` | MQTT 端口、保活、命令超时与重试 |
| `detection` | 检测后端、模型、阈值、输入尺寸、类别映射 |
| `hold` | 暂停后喷嘴温度、热床保持、等待时间、提醒 |
| `storage` | SQLite、媒体目录、保留期限、最低磁盘空间 |
| `http` | 本地 API 监听地址、端口、token 环境变量名 |
| `openclaw` | Hook URL、token、重试、飞书目标 |
| `logging` | 日志级别、JSON 格式、日志文件 |

检测关键字段：

```json
{
  "detection": {
    "enabled": true,
    "backend": "auto",
    "model_path": "models/spaghetti.onnx",
    "model_version": "spaghetti-v1",
    "input_width": 640,
    "input_height": 640,
    "input_layout": "nchw",
    "channel_order": "rgb",
    "normalize": true,
    "output_format": "auto",
    "class_count": 3,
    "class_map": {
      "0": "spaghetti",
      "1": "first_layer_detachment",
      "2": "structural_anomaly"
    },
    "box_coordinates": "pixels",
    "providers": ["CPUExecutionProvider"]
  }
}
```

`backend` 可选值：

- `auto`：有可用 ONNX Runtime 和模型时使用 ONNX，否则回退到空检测器。
- `onnx`：强制使用 ONNX；模型、运行时或 `class_map` 缺失会报错。
- `null`：关闭视觉检测，仅保留摄像头、遥测与事件接口。

`box_coordinates` 可选 `auto`、`normalized`、`pixels`。当前 3 类自训练 YOLO 导出建议使用 `pixels`，具体以 `smart-p1sc-model inspect` 的输出为准。

## 快速检查

首次启动前先做配置检查：

```powershell
smart-p1sc-probe doctor
```

检查摄像头与 MQTT（先停止 daemon）：

```powershell
smart-p1sc-probe probe --timeout 15
```

JSON 输出：

```powershell
smart-p1sc-probe doctor --json
smart-p1sc-probe probe --json
```

`doctor` 检查打印机信息、Access Code、API token、存储目录和模型路径。模型缺失是警告，不阻止启动；此时 `auto` 后端会回退为空检测器。

## 运行

前台运行：

```powershell
smart-p1scd --config .\config.json
```

默认读取当前目录的 `config.json`。daemon 使用单实例锁，重复启动会失败。按 `Ctrl+C` 停止。

启动后检查：

```powershell
smart-p1sc --json health
smart-p1sc --json status
```

本地状态页：

```text
http://127.0.0.1:8787/
```

浏览器会弹出 Basic Auth，用户名可任意填写，密码为 `SMART_P1SC_API_TOKEN`。状态页包含实时 MJPEG、当前状态和事件列表。

## CLI

所有命令都使用 `config.json` 或 `--config` 指定的配置，并从环境变量读取 token。

```powershell
smart-p1sc status
smart-p1sc health
smart-p1sc incidents --open --limit 20
smart-p1sc incident show <事件ID或4位短码>
smart-p1sc incident decide <事件ID或4位短码> --action resume --code <4位短码>
smart-p1sc incident decide <事件ID或4位短码> --action stop --code <4位短码>
smart-p1sc incident decide <事件ID或4位短码> --action false_positive --code <4位短码>
smart-p1sc incident label <事件ID或4位短码> --label true_positive
smart-p1sc snapshot --output .\snapshot.jpg
```

稳定 JSON 输出：

```powershell
smart-p1sc --json status
smart-p1sc --json incidents --open
smart-p1sc --json incident show <reference>
```

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 事件不存在 |
| `2` | 参数或配置错误 |
| `3` | 无法连接 daemon |
| `4` | 远端 HTTP 错误 |
| `5` | token 缺失或鉴权失败 |
| `6` | 请求被限流 |

`incident decide` 必须同时提供事件引用和 4 位短码。`false_positive` 会继续打印并写入误报标签。

## 本地 HTTP API

默认监听 `127.0.0.1:8787`，不要暴露到公网。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 存活检查，无需 token |
| `GET` | `/` | 状态页 |
| `GET` | `/status` | daemon、打印机、摄像头、检测、存储状态 |
| `GET` | `/incidents?open=1&limit=20` | 事件列表 |
| `GET` | `/incidents/{id或短码}` | 事件详情、事件流水、标签、胶片条 |
| `GET` | `/incidents/{id}/frames/{index}` | 单张胶片条 JPEG |
| `GET` | `/snapshot` | 当前帧 JPEG |
| `GET` | `/stream.mjpg` | 本地 MJPEG 重播 |
| `POST` | `/incidents/{id或短码}/decide` | 继续/停止/误报 |
| `POST` | `/incidents/{id或短码}/label` | 写入人工标签 |

除 `/health` 外，请求需要以下任一鉴权方式：

```text
Authorization: Bearer <SMART_P1SC_API_TOKEN>
Authorization: Basic <任意用户名:SMART_P1SC_API_TOKEN>
X-API-Token: <SMART_P1SC_API_TOKEN>
```

决策接口有速率限制，并支持 `idempotency_key` 防重复提交。

## OpenClaw / 飞书

相关文件：

- `openclaw/openclaw.example.json5`：OpenClaw 工具和 skill 配置示例。
- `openclaw/exec-approvals.example.json`：CLI 执行白名单。
- `skills/smart-p1sc/SKILL.md`：OpenClaw skill 说明。

集成步骤：

1. 安装并配置 OpenClaw Gateway 与飞书插件。
2. 将 `skills/smart-p1sc` 放入 OpenClaw 的 skills 目录。
3. 参考 `openclaw/openclaw.example.json5` 和 `openclaw/exec-approvals.example.json` 合并到现有配置，不要直接覆盖已有配置。
4. 确保 Gateway 进程能找到 `smart-p1sc` 可执行文件，并能读取 `SMART_P1SC_API_TOKEN`。
5. 如 Hook 启用鉴权，将 daemon 的 `SMART_P1SC_OPENCLAW_TOKEN` 设置为对应 token。
6. 按需调整 `openclaw.hook_url`、`openclaw.feishu_target` 和超时/重试。

通知内容包含事件类型、4 位短码、置信度、证据路径和建议动作。用户回复由 OpenClaw 解析后调用受限 CLI。执行白名单只允许 `status`、`health`、`incidents`、`incident show`、`incident decide`、`incident label` 等固定形式；`decide` 仍必须带 4 位短码。

OpenClaw 不可用时，事件仍会保存在 SQLite，通知进入发件箱并在后续重试；本地暂停/停止逻辑不受影响。

## 模型训练与部署

仓库不包含数据集和训练后的 ONNX 模型。`models/yolov8n.pt` 是基础权重，不能直接作为最终检测模型。

### 数据集

准备 YOLO 格式数据集：

```text
datasets/3d-printer-failure/
  data.yaml
  images/
    train/
    val/
    test/
  labels/
    train/
    val/
    test/
```

`data.yaml` 示例：

```yaml
path: .
train: images/train
val: images/val
test: images/test
nc: 3
names:
  0: spaghetti
  1: first_layer_detachment
  2: structural_anomaly
```

也可从 `training/dataset.example.yaml` 开始修改。数据集相对路径会优先按 YAML 所在目录解析，不存在时再按当前工作目录解析。

### 训练并导出

```powershell
smart-p1sc-model train `
  --data datasets\3d-printer-failure\data.yaml `
  --model models\yolov8n.pt `
  --epochs 100 `
  --batch 8 `
  --device 0 `
  --output models\spaghetti.onnx
```

CPU 训练使用 `--device cpu`。默认类别顺序为：

```text
spaghetti,first_layer_detachment,structural_anomaly
```

训练会校验数据集类别顺序与 `--class-names` 是否一致，并自动导出 ONNX 与 sidecar 元数据。

只导出已有权重：

```powershell
smart-p1sc-model export `
  --weights runs\train\spaghetti\weights\best.pt `
  --output models\spaghetti.onnx
```

### 检查模型

```powershell
smart-p1sc-model inspect --model models\spaghetti.onnx
```

检查单张图片推理：

```powershell
smart-p1sc-model check `
  --model models\spaghetti.onnx `
  --image .\sample.jpg `
  --class-map "0=spaghetti,1=first_layer_detachment,2=structural_anomaly"
```

`inspect` 会输出输入/输出形状和建议配置。当前 YOLOv8 3 类模型常见输出为 `[1, 7, 84]`，推荐 `output_format=cxcywh_class`、`box_coordinates=pixels`、`class_count=3`。

### 部署

1. 将 ONNX 放到 `models\spaghetti.onnx`，sidecar 元数据放在同目录。
2. 在 `config.json` 中设置 `detection.model_path`、`class_count`、`class_map`。
3. 用 `box_coordinates=pixels` 或按 `inspect` 建议调整。
4. 重启 daemon，或等待 daemon 在打印机空闲时自动重载模型。
5. 用 `smart-p1sc --json status` 确认 `detection.loaded` 和 `providers`。

`models/*.onnx`、`models/*.pt` 和数据集默认被 `.gitignore` 忽略。

## 数据与日志

默认路径：

```text
data/smart_p1sc.sqlite3
data/media/
data/smart_p1sc.log
```

SQLite 保存事件、事件流水、探索标签、决策和通知发件箱；媒体目录保存异常前后胶片条。默认保留策略：

- 已解决事件保留 30 天。
- 正常数据保留 7 天。
- 磁盘剩余空间低于 512 MB 时产生告警。

日志默认 JSON 格式，并会对 Access Code、API token、OpenClaw token 等敏感字段做脱敏。

## Windows 开机自启

脚本会注册当前用户的登录计划任务，并优先使用 `pythonw.exe`：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1 `
  -ProjectPath "G:\OpenCode\Smart-P1SC"
```

可选参数：

```powershell
-ConfigPath "G:\OpenCode\Smart-P1SC\config.json"
-PythonPath "C:\Python38\python.exe"
-TaskName "Smart-P1SC"
```

启动/停止任务：

```powershell
Start-ScheduledTask -TaskName "Smart-P1SC"
Stop-ScheduledTask -TaskName "Smart-P1SC"
```

卸载：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_autostart.ps1
```

计划任务使用用户级环境变量读取密钥，因此不要把密钥只设置在某个临时终端会话中。

## 开发与测试

```powershell
python -m pytest
```

测试覆盖摄像头协议、MQTT、配置、检测确认、事件状态机、SQLite、HTTP、CLI、通知、模型工具和 daemon 生命周期。

Lint：

```powershell
python -m ruff check smart_p1_sc tests
```

类型检查：

```powershell
python -m mypy smart_p1_sc
```

当前全仓 ruff/mypy 仍有历史告警，新增代码应保持无新增问题。

## 故障排查

| 现象 | 检查 |
| --- | --- |
| `daemon_unreachable` | daemon 是否运行；`http.host`/`http.port` 是否正确 |
| `missing_token` / 401 | 是否设置 `SMART_P1SC_API_TOKEN`；CLI 与 daemon 是否一致 |
| 摄像头探测失败 | 是否开启 LAN Only；daemon 是否已停止；是否被其他客户端占用 |
| MQTT 超时 | Access Code、序列号、IP；LAN Only 是否开启 |
| 检测未加载 | 模型路径是否存在；`auto` 是否回退到空检测器；安装 `vision` extra |
| 模型输出异常 | 运行 `smart-p1sc-model inspect`，按建议设置 `output_format`/`box_coordinates` |
| 通知未送达 | OpenClaw Hook URL、token、飞书插件；查看 `data/smart_p1sc.log` 与通知发件箱 |
| 启动即退出 | 查看日志；确认没有重复 daemon 实例；确认配置文件是合法 JSON |

## 目录结构

```text
smart_p1_sc/
  camera.py          摄像头 TLS/MJPEG 客户端与帧缓冲
  mqtt.py            P1S MQTT 遥测与命令
  detector.py        候选帧确认与遥测门控
  onnx_detector.py   ONNX Runtime 检测后端
  incidents.py       事件状态机与安全保持
  notifications.py   OpenClaw 通知与回复解析
  http_server.py     本地 HTTP/MJPEG/状态页
  cli.py             operator CLI
  diagnostics.py     安装前检查
  model_tool.py      训练/导出/检查工具
  daemon.py          常驻服务与组件编排
  db.py              SQLite 持久化
  media.py           胶片条存储
tests/               自动化测试
training/            数据集 YAML 模板
openclaw/            OpenClaw 配置示例
scripts/             Windows 自启动脚本
skills/              OpenClaw skill
openspec/            需求、设计与任务记录
```

## 许可证

尚未指定。公开发布前请补充合适的 `LICENSE` 文件。
