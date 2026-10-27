## ADDED Requirements

### Requirement: 摄像头流采集
系统 MUST 通过 TCP 6000 以 TLS 连接 P1S 摄像头，使用用户名 `bblp` 和 LAN Access Code 完成认证，并按 16 字节帧头读取 JPEG 帧。系统 MUST 校验 JPEG 起止标记，并支持跨 TCP 分片重组。

#### Scenario: 成功读取一帧
- **WHEN** 摄像头连接已认证并收到帧头与完整 JPEG 数据
- **THEN** 系统产生一条带单调递增序号、单调到达时间和墙上时间的帧记录

#### Scenario: 认证失败
- **WHEN** Access Code 错误或认证包被拒绝
- **THEN** 系统标记摄像头为不可用，记录脱敏错误，并按退避策略重连

#### Scenario: 帧数据损坏
- **WHEN** 帧头声明的长度与 JPEG 起止标记不一致
- **THEN** 系统丢弃该帧、记录告警，并继续读取下一帧

### Requirement: 摄像头单连接与本地重播
系统 MUST 作为打印机摄像头的唯一客户端；系统 MUST 在本机提供 MJPEG 流和最新快照接口，供用户或本地页面查看，且 MUST NOT 为每个查看者建立新的打印机摄像头连接。

#### Scenario: 本地查看者连接
- **WHEN** 用户在本地页面打开摄像头视图
- **THEN** 系统从已有帧缓冲重播，不新增到打印机的 TCP 连接

#### Scenario: 打印机已有其他摄像头客户端
- **WHEN** 系统连接摄像头时发现连接被拒绝或流被抢占
- **THEN** 系统标记摄像头为降级，重试连接，并在状态接口中报告原因

### Requirement: 摄像头健康与重连
系统 MUST 持续监测摄像头帧到达情况；当连续超过配置阈值未收到帧时，系统 MUST 将摄像头状态标记为 `DEGRADED`，并通过事件/通知暴露。系统 MUST 使用有界退避重连，且 MUST NOT 因重连阻塞 MQTT 或事件处理。

#### Scenario: 短暂断流后恢复
- **WHEN** 摄像头流中断后在阈值内恢复
- **THEN** 系统记录一次短暂降级，不终止当前打印事件

#### Scenario: 打印中长时间断流
- **WHEN** `gcode_state` 为 `RUNNING` 且摄像头连续超过配置时长无帧
- **THEN** 系统发送一次高优先级通知，并按配置决定是否暂停打印

### Requirement: MQTT 遥测
系统 MUST 通过 TLS 连接 8883，使用 `bblp` 和 LAN Access Code 认证，订阅 `device/{serial}/report`。系统 MUST 维护最新的打印机状态快照，至少包含 `gcode_state`、`layer_num`、`total_layer_num`、喷嘴温度、热床温度、`print_error`、`hms` 和摄像头信息。

#### Scenario: 收到状态上报
- **WHEN** 打印机在 report 主题发布状态
- **THEN** 系统更新最新快照，并向检测与事件模块发布结构化状态事件

#### Scenario: MQTT 断线
- **WHEN** MQTT 连接断开
- **THEN** 系统标记控制通道不可用、按退避重连，并在恢复后重新订阅和拉取最新状态

### Requirement: 打印机控制与状态对账
系统 MUST 通过 `device/{serial}/request` 发送 `print.pause`、`print.resume` 和 `print.stop`，使用唯一 `sequence_id` 和 QoS 1。系统 MUST 等待相同 `sequence_id` 的 ACK，并 MUST 通过后续 `gcode_state` 验证实际状态；仅 ACK 成功不视为命令成功。

#### Scenario: 暂停成功
- **WHEN** 系统发送 `print.pause`
- **THEN** 系统在收到成功 ACK 且观察到 `gcode_state` 变为 `PAUSE` 后，将命令标记为成功

#### Scenario: ACK 成功但状态未变
- **WHEN** 系统收到成功 ACK，但在超时内 `gcode_state` 仍未变为 `PAUSE`
- **THEN** 系统重试命令；达到重试上限后升级为控制失败事件

#### Scenario: 控制命令超时
- **WHEN** 系统在超时时间内没有收到对应 `sequence_id` 的 ACK
- **THEN** 系统按幂等键重试，并记录每次尝试的审计记录

### Requirement: LAN Only 控制前置条件
系统 MUST 在启动和打印开始时检查控制通道是否可用，并在写命令被静默丢弃时向用户报告“需要 LAN Only Mode/Developer Mode”的明确诊断。系统 MUST NOT 假装控制成功。

#### Scenario: 云模式下写命令被丢弃
- **WHEN** 系统能读取 report 但 pause/resume/stop 的 `gcode_state` 始终不变
- **THEN** 系统在状态接口和通知中报告控制不可用，并停止自动处置重试

#### Scenario: LAN Only 模式下控制可用
- **WHEN** 打印机处于 LAN Only Mode 且命令收到 ACK 并改变状态
- **THEN** 系统将控制通道标记为可用

### Requirement: 腔体灯保持
系统 MUST 在监控和异常保持期间确保腔体灯开启；如果暂停或打印机操作导致灯关闭，系统 MUST 尝试通过 MQTT 重新开启。

#### Scenario: 暂停后灯被关闭
- **WHEN** 事件进入 `HOLDING` 且检测到腔体灯关闭
- **THEN** 系统发送开灯命令，并将结果记录到事件审计中

### Requirement: 控制接口最小权限
系统 MUST 只向 operator 接口暴露高层动作；系统 MUST NOT 暴露任意 G-code、任意 MQTT 主题或任意打印机命令。系统 MUST 对控制命令做状态校验、限流和审计。

#### Scenario: 非法原始命令
- **WHEN** operator 接口收到包含原始 G-code 或未知 MQTT 命令的请求
- **THEN** 系统拒绝请求并返回明确的“不支持”错误码
