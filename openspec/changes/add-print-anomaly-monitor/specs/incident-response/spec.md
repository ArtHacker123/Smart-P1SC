## ADDED Requirements

### Requirement: 事件创建与幂等
系统 MUST 在确认异常时创建事件，事件 MUST 包含唯一 ID、短码、类型、严重度、置信度、开始时间、层号、胶片条、打印机快照和状态。系统 MUST 对同一活动区域/类型去重，避免重复事件和重复通知。

#### Scenario: 创建事件
- **WHEN** 检测模块确认一个 P1 炒面事件
- **THEN** 系统创建事件并生成唯一短码，状态进入 `PAUSING`

#### Scenario: 重复检测
- **WHEN** 同一区域/类型在已有活动事件期间再次被确认
- **THEN** 系统更新该事件证据，不创建新事件

### Requirement: 自动暂停
系统 MUST 对 P1 事件发送暂停；系统 MUST 在收到成功 ACK 且观察到 `gcode_state == PAUSE` 后将事件置为 `HOLDING`。系统 MUST 在暂停失败时重试并升级；按配置，系统 MUST 在重试耗尽后执行 fail-safe stop。

#### Scenario: 暂停成功
- **WHEN** 系统发送暂停且状态变为 `PAUSE`
- **THEN** 事件进入 `HOLDING`，启动 60 分钟计时，并发送通知

#### Scenario: 暂停被忽略
- **WHEN** 多次重试后 `gcode_state` 仍为 `RUNNING`
- **THEN** 系统发送控制失败通知，并按配置执行 stop 或保持升级状态

#### Scenario: 打印机已暂停
- **WHEN** 创建事件时打印机已经是 `PAUSE`
- **THEN** 系统不重复发送暂停，直接进入 `HOLDING` 并启动计时

### Requirement: 安全保持
进入 `HOLDING` 后，系统 MUST 将喷嘴温度降到配置的安全保持温度（默认 150°C）、保持热床打印温度和腔体灯开启。系统 MUST 记录保持命令结果，并在命令失败时通知用户。

#### Scenario: 保持命令成功
- **WHEN** 事件进入 `HOLDING`
- **THEN** 系统发送喷嘴降温命令并确认腔体灯状态，记录到审计

#### Scenario: 保持命令失败
- **WHEN** 喷嘴降温或开灯命令失败
- **THEN** 系统发送高优先级通知，并在状态接口标记降级

### Requirement: 60 分钟等待与自动停止
系统 MUST 在 `HOLDING` 的 T+10 和 T+50 发送提醒，并在 T+60 未收到用户决策时发送 `print.stop`。系统 MUST 验证停止状态，将事件结果标记为 `auto_stopped`，并 MUST NOT 允许之后 resume。计时器 MUST 独立于 OpenClaw、持久化并在 daemon 重启后恢复。

#### Scenario: 超时自动停止
- **WHEN** 事件在 T+60 仍为 `HOLDING` 且没有用户决策
- **THEN** 系统发送 stop、验证状态、关闭加热，并通知用户

#### Scenario: 用户及时回复
- **WHEN** 用户在 T+60 前发送“继续”或“停止”
- **THEN** 系统取消计时并执行对应决策

#### Scenario: daemon 重启
- **WHEN** daemon 在 `HOLDING` 期间重启
- **THEN** 系统从持久化截止时间恢复计时，不重置 60 分钟

### Requirement: 用户决策
系统 MUST 接受 `resume`、`stop` 和 `false_positive` 三种决策。系统 MUST 校验事件 ID、短码、事件状态和幂等键；`false_positive` MUST 等价于恢复打印并写入误报标签。系统 MUST 对重复决策返回相同结果，对晚到决策返回 `ALREADY_STOPPED` 或 `ALREADY_RESOLVED`。

#### Scenario: 继续打印
- **WHEN** 用户在 `HOLDING` 状态提交有效的 `resume`
- **THEN** 系统发送 resume、验证 `gcode_state == RUNNING`，事件进入 `MONITORING` 并启动抑制窗口

#### Scenario: 停止打印
- **WHEN** 用户在 `HOLDING` 状态提交有效的 `stop`
- **THEN** 系统发送 stop、验证停止状态，事件进入 `STOPPED`

#### Scenario: 误报
- **WHEN** 用户提交有效的 `false_positive`
- **THEN** 系统恢复打印并写入误报标签

#### Scenario: 晚到决策
- **WHEN** 事件已因超时自动停止后收到 resume
- **THEN** 系统拒绝并返回 `ALREADY_STOPPED`，不发送 resume

### Requirement: 恢复抑制
`resume` 成功后，系统 MUST 在配置的抑制窗口内抑制同一区域/类型的新事件；系统 MUST 在面积增长、新区域或置信度显著上升时允许再次触发。

#### Scenario: 恢复后立即复发
- **WHEN** 用户在恢复后数秒内，同一炒面区域仍未消失且没有增长
- **THEN** 系统不立即再次暂停

#### Scenario: 恢复后异常扩大
- **WHEN** 同一区域在抑制窗口内面积显著增长
- **THEN** 系统创建升级事件并按策略暂停

### Requirement: 手动操作对账
系统 MUST 通过 MQTT 遥测检测用户在打印机屏幕或其他客户端上的手动 resume/stop，并 MUST 关闭/更新事件、取消计时。

#### Scenario: 用户手动恢复
- **WHEN** 事件处于 `HOLDING` 而 `gcode_state` 变为 `RUNNING`
- **THEN** 系统将事件标记为 `resolved_by_user`，取消计时

#### Scenario: 用户手动停止
- **WHEN** 事件处于 `HOLDING` 而 `gcode_state` 变为 `FINISH`/`FAILED`/停止状态
- **THEN** 系统关闭事件并记录实际结果

### Requirement: 审计与恢复
系统 MUST 记录所有状态迁移、命令、ACK、状态对账、通知和用户决策，包含时间、来源、操作者和结果。daemon 重启后 MUST 与打印机实际状态对账，恢复或关闭未完成事件。

#### Scenario: 完整审计
- **WHEN** 一个事件从创建到结束
- **THEN** 审计记录可还原每一次暂停/恢复/停止尝试及其结果

#### Scenario: 重启恢复
- **WHEN** daemon 重启时存在未关闭事件
- **THEN** 系统读取事件和打印机状态，恢复计时或按实际状态关闭事件，并通知用户
