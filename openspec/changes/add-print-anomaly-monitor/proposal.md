## Why

P1S 自带摄像头，但只有约 1 fps / 720p，且 P1 系列没有 X1 的本地 NPU 炒面检测。无人值守打印一旦出现炒面、首层脱落或支撑倒塌，往往要数小时后才发现，浪费材料和时间；同时 LAN Only 模式下 Bambu Handy 不可用，用户又需要局域网控制。需要一个本地常驻的监控与处置回路：持续看帧、确认异常、自动暂停、通过 OpenClaw/飞书通知，并在 60 分钟内等待用户决策，超时自动停止。

## What Changes

- 新增常驻 daemon，独占 P1S 摄像头（TCP 6000，MJPEG）与 MQTT（8883），提供本地 MJPEG 重播与状态接口。
- 新增本地 ONNX 异常检测：v1 覆盖炒面/拉丝与首层大面积脱落；候选帧与确认事件分离，融合层号、温度、HMS 等遥测做门控。
- 新增事件状态机：确认异常 -> 暂停 -> 安全保持（喷嘴降温、热床保持、灯保持）-> T+10/T+50 提醒 -> T+60 未回复自动停止；支持继续、停止、误报。
- 新增 OpenClaw 通知与决策通道：飞书推送文本 + 胶片条，事件短码；用户回复由 OpenClaw 解析后调用本地 CLI。
- 新增 operator CLI：只暴露 status/incidents/show/decide/label/snapshot 等高层动作，JSON 输出、稳定退出码、事件 ID + 短码校验、幂等与审计；不暴露原始 G-code/MQTT。
- 新增事件与数据存储：SQLite + 胶片条 + 遥测快照 + 用户决策/误报标签，支持后续模型迭代与同任务参考帧。
- 明确不做：从 PC 发起新打印/切片、支撑倒塌 v1、明火/烟雾安全认证、云推理、多打印机。

## Capabilities

### New Capabilities
- `printer-io`: P1S 摄像头采集、MQTT 遥测/控制、断线重连、LAN Only 前置条件、本地 MJPEG 重播。
- `anomaly-detection`: 本地检测管线、候选/确认两级、炒面与首层脱落、遥测门控、阈值与去抖。
- `incident-response`: 事件生命周期、60 分钟安全保持与自动停止、继续/停止/误报决策、恢复抑制、审计。
- `operator-interface`: OpenClaw/飞书推送与回复、事件短码、CLI 契约、JSON/退出码、幂等与权限。
- `print-data`: 事件/胶片条/遥测持久化、误报标注、保留策略、同任务参考帧与数据闭环。

### Modified Capabilities
（无。当前项目没有既有 spec。）

## Impact

- 新增本地服务（建议 Python + ONNX Runtime + paho-mqtt + OpenCV/Pillow + SQLite），以 Windows 服务/开机自启方式常驻。
- 依赖打印机 LAN Only Mode、Access Code、摄像头单连接限制；daemon 成为摄像头唯一持有者，需本地重播给用户。
- 依赖 OpenClaw Gateway 与飞书插件（同机），以及本地 CLI/HTTP 通道；OpenClaw 不在安全关键路径上。
- 无既有代码受影响；这是新项目的第一条 capability 基线。
