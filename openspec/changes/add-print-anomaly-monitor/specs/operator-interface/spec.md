## ADDED Requirements

### Requirement: 通知推送
系统 MUST 在事件创建、升级和自动停止时通过 OpenClaw 向飞书推送通知。通知 MUST 包含事件短码、类型、严重度、层号/总层数、置信度、发生时间、已执行动作、保持温度、允许的回复动作和胶片条图片。

#### Scenario: 事件通知
- **WHEN** 新 P1 事件进入 `HOLDING`
- **THEN** OpenClaw 向飞书发送包含短码、动作和胶片条的消息

#### Scenario: 通知失败
- **WHEN** OpenClaw 或飞书不可用
- **THEN** daemon 记录失败并重试；暂停/保持/超时停止不受影响

### Requirement: 用户回复解析
系统 MUST 接受飞书回复中的动作和事件短码，动作包括 `继续`、`停止`、`误报`、`查看`。当短码缺失且存在多个开放事件时，系统 MUST 要求用户澄清；当短码无效时，系统 MUST 返回明确错误。

#### Scenario: 有效回复
- **WHEN** 用户回复“继续 7F3A”
- **THEN** OpenClaw 调用 `incident decide INC-7F3A --action resume --code 7F3A --json`

#### Scenario: 缺少短码
- **WHEN** 用户回复“继续”且存在多个开放事件
- **THEN** OpenClaw 反问用户要操作哪个事件，不执行任何动作

#### Scenario: 无效动作
- **WHEN** 用户回复无法映射到允许动作
- **THEN** OpenClaw 返回可用动作列表，不调用 decide

### Requirement: CLI 契约
系统 MUST 提供 `status`、`incidents`、`incident show`、`incident decide`、`incident label`、`snapshot` 命令。所有命令 MUST 支持 `--json` 并返回稳定结构；系统 MUST 使用稳定退出码区分成功、状态不合法、需要确认、事件不存在、打印机不可达和已解决。

#### Scenario: 查询状态
- **WHEN** OpenClaw 执行 `smart-p1sc status --json`
- **THEN** 返回打印机状态、检测器健康、活动事件和计时信息

#### Scenario: 状态不合法
- **WHEN** OpenClaw 对已恢复的事件再次执行 resume
- **THEN** CLI 返回非零退出码和 `ALREADY_RESOLVED`，不发送打印机命令

### Requirement: 安全与授权
系统 MUST 通过本地 token 认证 CLI；系统 MUST 限制 OpenClaw 只能调用该 CLI，不得使用自由 shell 或原始打印机命令；系统 MUST 对控制命令限流并记录审计。系统 MUST NOT 在日志中输出 Access Code。

#### Scenario: 未认证调用
- **WHEN** CLI 请求缺少或携带错误 token
- **THEN** daemon 拒绝请求并返回授权错误

#### Scenario: 频率限制
- **WHEN** 短时间内控制命令超过配置上限
- **THEN** daemon 拒绝超额请求并返回限流错误

### Requirement: 幂等与竞态
系统 MUST 要求 `decide` 携带事件 ID、短码和幂等键；重复请求 MUST 返回相同结果；与超时自动停止竞态时 MUST 串行处理，晚到请求返回明确终态。

#### Scenario: 重复继续
- **WHEN** 同一 resume 请求被重复提交
- **THEN** 第一次执行，后续返回相同结果且不重复发送 MQTT

#### Scenario: 超时竞态
- **WHEN** resume 与 T+60 自动停止同时到达
- **THEN** daemon 串行处理；未赢得竞态的一方收到 `ALREADY_STOPPED` 或 `ALREADY_RESOLVED`

### Requirement: 降级与兜底
系统 MUST 在 OpenClaw/飞书不可用时继续执行暂停、保持和 60 分钟自动停止；系统 MUST 提供本机 CLI/状态页作为决策兜底。通知通道恢复后 MUST 补发未送达的关键事件。

#### Scenario: OpenClaw 宕机
- **WHEN** 事件发生但 OpenClaw 不可达
- **THEN** daemon 仍暂停并在 60 分钟后自动停止；本机 CLI 可继续/停止

#### Scenario: 通道恢复
- **WHEN** OpenClaw 恢复且存在未送达通知
- **THEN** daemon 补发事件摘要，不重复发送已确认送达的通知

### Requirement: 本地状态与实时画面
系统 MUST 在本机提供状态页和 MJPEG/快照接口，展示打印机状态、检测器健康、开放事件、剩余保持时间和最新胶片条；该接口 MUST 仅绑定本机或受信任局域网地址。

#### Scenario: 本机查看
- **WHEN** 用户在本机打开状态页
- **THEN** 可以看到实时画面、当前事件和可执行动作

#### Scenario: 非授权访问
- **WHEN** 未认证客户端尝试访问控制接口
- **THEN** 系统拒绝并记录审计
