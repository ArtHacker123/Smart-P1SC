## ADDED Requirements

### Requirement: 检测运行门控
系统 MUST 仅在打印机处于 `RUNNING` 时生成可处置的异常事件；在 `IDLE`、`PREPARE`、`PAUSE`、`FINISH`、`FAILED` 或未知状态时，系统 MAY 记录候选但 MUST NOT 触发自动暂停。首层检测 MUST 以 `layer_num <= 1` 为门控。

#### Scenario: 打印中运行检测
- **WHEN** `gcode_state` 为 `RUNNING` 且收到新帧
- **THEN** 系统对该帧运行检测管线

#### Scenario: 空闲或暂停时不触发暂停
- **WHEN** `gcode_state` 不是 `RUNNING` 且检测到可疑画面
- **THEN** 系统只记录候选，不创建可自动暂停的事件

#### Scenario: 首层门控
- **WHEN** `layer_num` 大于 1 且检测器只输出首层类候选
- **THEN** 系统忽略该首层候选

### Requirement: 本地候选生成
系统 MUST 使用本地 ONNX 推理生成 `spaghetti` 与 `first_layer_detachment` 候选；每个候选 MUST 包含类型、置信度、图像区域（bbox 或 mask）、帧号和模型版本。系统 MUST NOT 依赖云服务完成 v1 检测。

#### Scenario: 炒面候选
- **WHEN** 一帧被模型判定为包含炒面/拉丝
- **THEN** 系统生成至少一个 `spaghetti` 候选并写入有界环形缓冲

#### Scenario: 正常帧
- **WHEN** 一帧没有超过候选阈值的检测
- **THEN** 系统不生成候选，且不增加事件计数

### Requirement: 候选到事件的确认
系统 MUST 在滑动窗口内要求至少 K 帧正例，且候选在空间上一致（bbox IoU 或聚类），才将候选确认为事件。系统 MUST 对单帧闪烁、孤立噪声和不一致区域做抑制。

#### Scenario: 单帧误报
- **WHEN** 窗口内只有一帧出现候选，其余帧为正常
- **THEN** 系统不创建事件，只保留候选日志

#### Scenario: 持续且空间一致的炒面
- **WHEN** 窗口内有至少 K 帧炒面候选，且 bbox 空间一致
- **THEN** 系统创建一个 `spaghetti` 事件，并进入事件处置流程

#### Scenario: 分散的孤立检测
- **WHEN** 多帧候选出现在互不重叠的随机区域
- **THEN** 系统不确认事件，避免把噪点当成炒面

### Requirement: 遥测融合与门控
系统 MUST 将打印机遥测作为检测上下文，至少使用 `gcode_state`、`layer_num`、喷嘴/热床温度、`print_error` 和 `hms`。当打印机已报告堵头、断料、温度异常等可由硬件识别的故障时，系统 MUST 优先使用遥测告警，并 MUST NOT 重复创建同类视觉事件。

#### Scenario: 硬件已报告断料
- **WHEN** `hms` 或 `print_error` 指示断料，同时画面出现异常
- **THEN** 系统创建/更新遥测故障通知，不创建重复的炒面事件

#### Scenario: 温度未达到
- **WHEN** 喷嘴温度显著低于目标温度且处于预热阶段
- **THEN** 系统抑制视觉异常事件

### Requirement: 严重度与策略
系统 MUST 为每个确认事件分配严重度：`P1`（高置信炒面/首层大面积脱落，自动暂停）、`P2`（可疑，通知/观察，不自动暂停）、`P3`（轻微，仅记录）。严重度 MUST 由类型、置信度、持续帧数、增长趋势和打印机状态共同决定。

#### Scenario: 高置信炒面
- **WHEN** 确认事件类型为 `spaghetti` 且置信度超过 P1 阈值
- **THEN** 系统将严重度设为 `P1` 并请求自动暂停

#### Scenario: 低置信持续异常
- **WHEN** 事件持续但置信度处于 P2 区间
- **THEN** 系统发送通知并继续观察，不自动暂停

### Requirement: 恢复抑制与再次触发
用户恢复打印后，系统 MUST 对同一区域/类型的检测进入抑制窗口；系统 MUST 仅在面积显著增长、出现新区域或置信度显著上升时再次创建事件。

#### Scenario: 恢复后旧炒面仍在
- **WHEN** 用户恢复打印，画面中仍存在上次事件的炒面区域
- **THEN** 系统在抑制窗口内不因该区域立即再次暂停

#### Scenario: 炒面继续增长
- **WHEN** 抑制窗口内同一区域面积显著增长
- **THEN** 系统升级并创建新的处置事件

### Requirement: 支撑倒塌候选收集
v1 系统 MUST 将疑似支撑倒塌/结构突变记录为 `structural_anomaly` 候选，并 MUST NOT 因该类候选自动暂停。系统 MUST 保存对应帧和遥测，供后续标注与参考帧对比。

#### Scenario: 疑似支撑倒塌
- **WHEN** 检测器输出 `structural_anomaly` 候选并达到确认条件
- **THEN** 系统保存胶片条和遥测，但不暂停打印机

### Requirement: 模型与阈值管理
系统 MUST 记录每个事件使用的模型版本和阈值配置；系统 MUST 支持在不重启 daemon 的情况下更新模型版本（或通过受控重启），并 MUST 在事件中保留可追溯信息。

#### Scenario: 模型版本变更
- **WHEN** 运维更新检测模型
- **THEN** 新事件记录新的模型版本，旧事件保留原版本信息

#### Scenario: 阈值调整
- **WHEN** 运维调整某类候选/确认阈值
- **THEN** 系统在下一次检测周期生效，并将配置版本写入事件
