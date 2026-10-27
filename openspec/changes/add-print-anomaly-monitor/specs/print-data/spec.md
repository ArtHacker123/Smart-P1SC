## ADDED Requirements

### Requirement: 事件持久化
系统 MUST 使用 SQLite 持久化事件、帧索引、遥测快照、命令、决策、标签和作业身份；时间 MUST 以 UTC 存储。系统 MUST 保证事件与帧、命令、决策之间的关联可查询。

#### Scenario: 事件落库
- **WHEN** 事件创建或状态迁移
- **THEN** 系统在同一事务中写入事件状态和审计记录

#### Scenario: 按事件查询
- **WHEN** 运维按事件 ID 查询
- **THEN** 返回事件详情、胶片条路径、命令历史和决策历史

### Requirement: 胶片条与环形缓冲
系统 MUST 维护有界环形缓冲，并在事件创建时保存前 N 帧与后 N 帧（默认前 10 + 后 10）为 JPEG；系统 MUST 在事件记录中保存胶片条路径和帧时间戳。环形缓冲 MUST 有内存上限，不得无限增长。

#### Scenario: 事件胶片条
- **WHEN** 事件被确认
- **THEN** 系统保存前 10 帧和后 10 帧，并将路径写入事件

#### Scenario: 缓冲上限
- **WHEN** 长时间无事件运行
- **THEN** 环形缓冲只保留最近 N 帧，旧帧被覆盖

### Requirement: 数据保留与磁盘健康
系统 MUST 支持可配置保留策略；系统 MUST 定期清理过期事件、胶片条和普通录制数据；系统 MUST NOT 删除未关闭事件。磁盘不足时，系统 MUST 优先停止普通录制、保留事件证据，并发出告警。

#### Scenario: 清理过期数据
- **WHEN** 数据超过保留期限
- **THEN** 系统删除对应文件和数据库记录，但保留开放事件

#### Scenario: 磁盘不足
- **WHEN** 可用磁盘低于阈值
- **THEN** 系统停止普通延时摄影、记录告警，并继续检测与事件保存

### Requirement: 标签与数据闭环
系统 MUST 记录用户决策和显式 `true_positive`/`false_positive` 标签；系统 MUST 记录打印最终结果（完成/失败/被停止/自动停止）；系统 MUST 支持导出训练清单（帧路径、标签、事件上下文）。

#### Scenario: 误报标签
- **WHEN** 用户回复“误报 7F3A”
- **THEN** 系统写入 `false_positive` 标签并关联该事件胶片条

#### Scenario: 导出训练数据
- **WHEN** 运维请求导出
- **THEN** 系统生成包含帧路径、标签、模型版本和上下文的清单，不上传任何数据

### Requirement: 作业身份与参考帧
系统 MUST 在可用时记录打印作业身份（如 3mf/gcode 哈希或文件名）；系统 MUST 保存成功打印的关键帧，供后续同任务参考对比。v1 MUST NOT 依赖参考帧完成检测。

#### Scenario: 记录作业身份
- **WHEN** 打印开始且作业身份可用
- **THEN** 系统将该身份关联到本次打印和事件

#### Scenario: 保存成功参考
- **WHEN** 打印完成且结果为成功
- **THEN** 系统保存关键帧和遥测摘要，供后续同任务使用

### Requirement: 隐私与凭据
系统 MUST 默认仅本地存储画面和数据；系统 MUST NOT 上传画面到云端；系统 MUST 对 Access Code 等凭据脱敏，不得写入日志、事件或导出文件。

#### Scenario: 默认不上传
- **WHEN** 系统正常运行
- **THEN** 除 OpenClaw/飞书通知所需的胶片条外，不上传任何画面

#### Scenario: 日志脱敏
- **WHEN** 系统记录连接错误或配置
- **THEN** 日志中不出现明文 Access Code
