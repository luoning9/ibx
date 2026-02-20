# IBX 需求文档（个人单账户版）

## 1. 背景与目标

你希望这个 App 支持“条件触发交易”场景：
- 你输入一条交易策略（例如：`SLV 价格 <= 60 USD 时，买入 100 股 SLV`）。
- 系统持续监测价格。
- 条件满足时自动下单到 IB Gateway。
- 若到期前未满足，则策略自动中止。
- 所有价格统一按 USD 处理，仅交易 USD 计价产品。

目标是先实现个人可用、稳定、可追溯的最小闭环。

---

## 2. 适用范围

### 2.1 in scope（本期）
- 单用户。
- 单 IB 账户（固定，不支持多账户）。
- 条件类型：
  - 单产品条件（价格、流动性、回撤比例、上涨比例）。
  - 双产品组合条件（任意两个产品之间的流动性与价差关系）。
- 下单类型：支持市价单（`MKT`）和限价单（`LMT`）。
- 下单数量统一按 `QUANTITY`（不再支持 `NOTIONAL_USD`）。
- 到期机制：到期未触发自动 `EXPIRED`（支持绝对时间与相对时间，最长 1 周）。
- 支持 USD 计价期货交易。
- 支持按市场情况自动展期（roll）。
- 支持策略链式触发（一个策略最多触发一个下游策略）。

### 2.2 out of scope（本期不做）
- 通用 N 产品任意维度条件编排引擎（本期仅支持单产品与双产品）。
- 复杂订单策略（分批、跟踪止损、多腿组合）。
- 策略回测与绩效分析平台。

### 2.3 not planned（长期不考虑）
- 多账户路由与多账户管理。

---

## 3. 关键术语
- `Strategy`：用户提交的条件交易规则。
- `Trigger`：策略条件满足的瞬间。
- `Expire`：策略到达截止时间且未触发，自动失效。
- `Idempotency`：同一策略请求不会导致重复下单。

---

## 4. 功能需求

### 4.1 策略创建
系统必须支持创建策略，最小字段包括：
- `idempotency_key`
- `sec_type`（`STK` / `FUT`）
- `symbol`
- `condition_logic`（`AND` / `OR`）
- `conditions`（触发条件列表；“仅基础信息新建”阶段可为空，激活前至少 1 项）
- `currency`（固定为 `USD`）
- `trade_action`（交易动作对象，结构见 4.12）
- `expire_at` 或 `expire_in_seconds`（二选一）
- `next_strategy_id`（可选，下游策略 ID；为空表示无后续策略）
- `upstream_only_activation`（`true` 表示禁止手动激活，仅允许由上游策略激活）

`conditions` 列表项统一结构（两类）：
- `condition_id`：条件唯一标识（用于日志与审计追踪）
- `condition_nl`：条件自然语言描述（系统生成，只读；创建请求可省略）
- `condition_type`：`SINGLE_PRODUCT` 或 `PAIR_PRODUCTS`
- `metric`：例如 `PRICE` / `DRAWDOWN_PCT` / `RALLY_PCT` / `LIQUIDITY_RATIO` / `SPREAD`
- `trigger_mode`：`LEVEL` / `CROSS_UP` / `CROSS_DOWN`
- `evaluation_window`：滚动评估窗口（最小 `1m`，如 `1m` / `2m` / `5m`）
- `window_price_basis`：窗口价格基准（`CLOSE` / `HIGH` / `LOW` / `AVG`，默认 `CLOSE`）
- `operator`：`<=` / `>=` / `<` / `>` / `==`
- `value`：阈值
- `value` 类型由 `metric` 决定：`PRICE/SPREAD` 使用美元值，`DRAWDOWN_PCT/RALLY_PCT/LIQUIDITY_RATIO` 使用比例值

当 `condition_type=SINGLE_PRODUCT`：
- `metric` 仅允许：`PRICE` / `DRAWDOWN_PCT` / `RALLY_PCT`
- `product`（产品标识）
- `price_reference`（仅比例类指标需要，且与 `metric` 联动）：
- `DRAWDOWN_PCT` -> `HIGHEST_SINCE_ACTIVATION`
- `RALLY_PCT` -> `LOWEST_SINCE_ACTIVATION`

当 `condition_type=PAIR_PRODUCTS`：
- `metric` 仅允许：`LIQUIDITY_RATIO` / `SPREAD`
- `product_a`（产品 A 标识）
- `product_b`（产品 B 标识）

过期时间规则：
- 支持绝对时间：`expire_at`（ISO 8601，带时区）
- 支持相对时间：`expire_in_seconds`（从策略激活时间开始计时）
- 最长过期时长：`7 * 24 * 3600 = 604800` 秒
- 超过 1 周拒绝创建
- 策略创建后默认不立即激活，需手动激活或由上游策略激活
- 新建策略即使尚未设置触发条件与后续动作，状态仍为 `PENDING_ACTIVATION`。

### 4.2 策略查询与控制
系统必须支持：
- 查询策略列表与详情。
- 手动取消尚未终态的策略。
- 手动激活策略（仅当 `upstream_only_activation=false` 时允许）。
- 手动暂停策略（仅 `ACTIVE` 可暂停，暂停后转 `PAUSED`）。
- 手动恢复策略（仅 `PAUSED` 可恢复，恢复后转 `ACTIVE`）。

### 4.3 行情监测
系统必须按配置间隔轮询/获取行情，并评估所有已激活策略（`ACTIVE`）。
- `PAUSED` 策略不参与触发条件评估。

监测间隔要求：
- 配置项：`MONITOR_INTERVAL_SECONDS`
- 最小值：`20`
- 最大值：`300`（5分钟）
- 默认值：`60`
- 越界处理：自动夹紧到边界并记录 warning
- 说明：`MONITOR_INTERVAL_SECONDS` 是调度频率，`evaluation_window` 是条件计算窗口，二者独立配置。

### 4.4 触发与下单
当策略条件满足时，系统必须：
1. 执行下单前风控校验与交易核验。
2. 基于 `trade_action` 计算最终下单参数（统一按数量下单）。
3. 校验产品交易币种为 `USD`，非 `USD` 必须拒绝。
4. 提交订单到 IB Gateway。
5. 记录订单与回报状态。

### 4.12 交易动作结构（trade_action）
`trade_action` 必须独立定义，最小字段：
- `action_type`（`STOCK_TRADE` / `FUT_POSITION` / `FUT_ROLL`）
- `quantity`（统一数量下单，正整数）
- `tif`（当前版本固定 `DAY`）
- `allow_overnight`（是否允许隔夜时段成交，可选，默认 `false`）
- `cancel_on_expiry`（可选，默认 `false`）

当 `action_type=STOCK_TRADE`（股票买卖）：
- `symbol`
- `side`（`BUY` / `SELL`）
- `order_type`（`MKT` / `LMT`）
- `limit_price`（`order_type=LMT` 时必填）

当 `action_type=FUT_POSITION`（期货开平）：
- `symbol`
- `contract`（可选，建议填写具体合约）
- `position_effect`（`OPEN` / `CLOSE`）
- `side`（`BUY` / `SELL`）
- `order_type`（`MKT` / `LMT`）
- `limit_price`（`order_type=LMT` 时必填）

当 `action_type=FUT_ROLL`（期货展期）：
- `symbol`
- `close_contract`（待平合约）
- `open_contract`（目标合约）
- `close_order_type` / `open_order_type`（`MKT` / `LMT`）
- `close_limit_price` / `open_limit_price`（对应腿为 `LMT` 时必填）
- `max_leg_slippage_usd`（可选）

规则：
- `trade_action` 为空时，表示该策略仅负责激活下游策略。
- `trade_action` 不为空时，表示触发后先执行交易动作。
- 当同时配置 `trade_action` 和 `next_strategy_id` 时，允许“执行交易 + 激活下游策略”。
- 触发语义固定为 `ONCE`（本系统不提供 `on_trigger` 配置）。
- 当前版本仅发送 `TIF=DAY` 订单。
- `sec_type=STK` 仅允许 `action_type=STOCK_TRADE`。
- `sec_type=FUT` 仅允许 `action_type=FUT_POSITION` 或 `FUT_ROLL`。

### 4.14 全局条件配置
- `MAX_CONDITIONS_PER_STRATEGY`：单策略最大条件数（全局配置，默认建议 `5`）。
- 创建/更新策略时，`len(conditions)` 不得超过 `MAX_CONDITIONS_PER_STRATEGY`。
- `evaluation_window` 采用滚动窗口语义（每次评估时按“当前时刻向前 N 时间”取数据）。
- `evaluation_window` 最小值为 `1m`。

### 4.13 UI 页面与信息展示要求
主菜单仅包含：
- `策略列表`
- `运行事件`
- `交易日志`

说明：
- `策略编辑`不出现在主菜单中，仅通过“策略列表 -> 新建策略”进入。

策略编辑要求（拆分为三部分）：
- `基本信息编辑`：仅编辑策略基础字段（如 `id`、`sec_type`、`symbol`、描述、激活限制、过期方式）。
- `触发条件编辑`：编辑 `condition_id`、`trigger_mode`、`evaluation_window`，并支持按 `condition_type` 切换单产品/双产品字段；单产品比例类场景使用“指标（含基准）”组合选择，不单独暴露 `price_reference` 输入。
- `触发条件编辑`：支持 `window_price_basis` 下拉（收盘/最高/最低/平均），默认收盘价。
- `触发条件编辑`：`触发判定`选项需按 `metric` 动态约束，避免无效组合。
- `后续动作编辑`：编辑 `trade_action` 与 `next_strategy_id`（链式控制）。
- `触发条件编辑`与`后续动作编辑`仅在策略状态为 `PENDING_ACTIVATION` 或 `PAUSED` 时允许。
- 当状态为 `ACTIVE` 时，需先暂停后再编辑。
- “新增策略”时只进入 `基本信息编辑`；保存后进入该策略详情页，初始状态不含触发条件和后续动作。
- 在上述初始详情状态中：
- `触发条件`区块使用表头单一入口按钮（文案“设置触发条件”）并显示空态提示。
- `后续动作`区块使用表头单一入口按钮（文案“设置后续动作”）并显示空态提示。

策略列表页要求：
- 每条策略显示：`id`、`状态`、`自然语言描述`、`最近更新时间`、`过期时间`、操作按钮。
- 列表页不展示 `conditions`、`trade_action`、`next_strategy_id` 详细结构。
- 点击策略行进入策略详情页。

策略详情页要求：
- 已完成配置的策略：展示 `conditions`、`trade_action`、`next_strategy_id` 的完整结构。
- `conditions` 中每条条件需展示自然语言描述，来源为后端返回的 `condition_nl`（前端不自行拼装）。
- 仅完成基本信息的新策略：`触发条件`与`后续动作`区块分别仅展示设置按钮（无结构详情）。
- `触发条件`区块位于 `后续动作`区块上方。
- `后续动作`区块合并展示 `next_strategy` 与 `trade_action`：
- `next_strategy` 展示 `id`、说明、状态（状态置于小区域标题右侧）。
- `trade_action` 展示可读摘要与关键字段（动作类型、方向/开平类型、订单类型、TIF、隔夜开关、数量、限价）。
- `trade_action` 交易状态置于“交易指令”小区域标题右侧。
- `触发条件`与`后续动作`区块表头风格统一：左侧标题 / 中间状态文字 / 右侧编辑按钮。
- `后续动作`区块与`触发条件`区块一致，使用单一表头按钮；文案按是否已配置在“编辑/设置后续动作”间切换。
- 包含该策略的 `事件日志`（时间、事件类型、详情）。
- 在基础信息区域提供策略操作按钮：`取消执行` / `暂停执行` / `恢复执行` / `激活策略`（按当前状态控制可用性）。
- 编辑入口在 `PENDING_ACTIVATION` / `PAUSED` 可用，其它状态禁用。

运行事件页要求：
- 作为全局事件日志，格式与策略详情中的 `事件日志` 基本一致。
- 额外包含 `strategy_id` 列。

交易日志页要求：
- 汇总所有策略的“交易核验 + 执行”记录。
- 必须包含 `trade_id`，用于串联核验、下单、成交等记录。

### 4.11 可配置交易核验机制（Pre-Trade Verification）
系统必须支持可配置核验规则，并在“发往 IB 前”强制执行。

最小支持规则：
- 单笔交易金额上限（如：`max_notional_usd=5000`）。
- 允许的订单类型白名单（如：仅允许 `LMT`）。

行为要求：
- 任一核验规则不通过时，订单不得发送到 IB。
- 拒绝原因必须结构化记录（规则 ID、失败原因、原始订单摘要）。
- 核验规则支持热更新或重启生效（至少支持配置文件方式）。

配置示例（示意）：
```yaml
verification:
  max_notional_usd: 5000
  allowed_order_types: ["LMT"]
```

### 4.5 到期中止
系统必须定时检查策略到期时间：
- 对于未触发策略，`now >= expire_at` 时转为 `EXPIRED`。
- `EXPIRED` 后不得再触发下单。
- 若使用 `expire_in_seconds`，系统在激活时计算 `expire_at = activated_at + expire_in_seconds`。
- 对于已发单未成交策略（`ORDER_SUBMITTED`），到期时不得转 `EXPIRED`，应按订单收尾策略处理：
  - 若 `cancel_on_expiry=true`：发起撤单，撤单成功后转 `CANCELLED`；
  - 若 `cancel_on_expiry=false`：保持订单跟踪，等待 `FILLED/CANCELLED/FAILED` 最终回报。
- `cancel_on_expiry` 未配置时默认按 `false` 处理。
- `PAUSED` 状态下到期计时仍继续；若到期且尚未发单，按同规则转 `EXPIRED`。

### 4.6 幂等防重
系统必须保证：
- 相同 `idempotency_key` 的重复创建请求不产生重复策略。
- 同一策略最多触发一次下单。

### 4.7 重启恢复
系统重启后必须：
- 恢复 `ACTIVE` 策略监测。
- 保持 `PAUSED` 策略为暂停状态，不得误恢复为 `ACTIVE`。
- 对 `ORDER_SUBMITTED` 状态做订单状态补偿查询。
- 保留并恢复“待激活”策略，不得误激活。

### 4.8 期货与自动展期
当 `sec_type=FUT` 时，系统必须支持：
1. 识别当前持仓合约与下一可交易主力候选合约。
2. 在满足展期条件时自动执行“平近开远”。
3. 展期过程按同方向、同数量（或配置比例）执行，防止裸露风险敞口。

展期触发条件（至少支持以下组合）：
- 到期驱动：距离到期日小于等于 `ROLL_DAYS_BEFORE_EXPIRY`。
- 流动性驱动：目标合约间流动性比达到阈值（可配置）。
- 点差驱动：目标合约间价差达到阈值（可配置）。
- 组合关系：支持流动性条件与价差条件按 `AND/OR` 组合。

展期执行要求：
- 支持配置展期时间窗口（交易时段内）。
- 默认按“先平待切换合约，再开目标合约”执行；失败时记录 `FAILED` 并告警。
- 单次展期只执行一次，避免重复 roll。

比例条件说明：
- 回撤比例触发（`metric=DRAWDOWN_PCT`）：价格相对基准下跌达到阈值时触发（如回撤 10%）。
- 上涨比例触发（`metric=RALLY_PCT`）：价格相对基准上涨达到阈值时触发（如上涨 8%）。

### 4.10 策略链与下游激活
系统必须支持：
1. 一个策略触发后激活一个下游策略（1 -> 1）。
2. 下游策略共享上游上下文（如上游触发时记录的 `anchor_price`）。
3. 每个下游策略独立执行与独立状态管理。
4. 链式激活必须记录 `logical_activated_at`（上游触发时刻），用于 `HIGHEST_SINCE_ACTIVATION` / `LOWEST_SINCE_ACTIVATION` 这类基准计算起点。
5. 若下游策略存在实际激活延迟，系统必须基于行情缓存对区间 `[logical_activated_at, activated_at]` 做补偿计算，避免丢失该区间的最高/最低价。

一致性要求：
- 同一上游触发事件对同一下游只能激活一次（防重复）。
- 禁止策略环路（A->B->A）。
- `upstream_only_activation=true` 的策略只能由上游触发激活，任何手动激活请求必须拒绝。
- 对于使用 `price_reference=HIGHEST_SINCE_ACTIVATION/LOWEST_SINCE_ACTIVATION` 的策略，必须以 `logical_activated_at` 为统计起点，而非仅以数据库落库时间为起点。

---

## 5. 非功能需求

### 5.1 安全
- 默认仅允许 paper 交易。
- live 交易必须通过显式开关启用（如 `ENABLE_LIVE_TRADING=true`）。
- 敏感信息（账号、密码、token）不得写入日志。

### 5.2 可靠性
- 任何失败（校验失败、风控拒绝、下单异常）必须落库可追溯。
- 网络抖动或进程重启后可恢复到一致状态。
- 核验失败必须可追溯到具体规则版本与规则项。

### 5.3 可观测性
- 必须有结构化日志（至少包含 `strategy_id`、状态变化、错误原因）。
- 必须记录策略评估日志（便于回溯为何触发/未触发）。

### 5.4 可维护性
- 架构保持轻量，优先 SQLite + 单机 worker。
- 后续可平滑扩展到更复杂架构。

---

## 6. 约束与假设
- 永久单账户约束：系统仅服务一个固定 IB 账户，不提供账户切换、账户路由或账户间分配能力。
- 所有价格字段（触发价、限价）均为 USD。
- 仅允许交易币种为 USD 的产品；非 USD 产品直接拒绝。
- 不做任何汇率换算与跨币种处理。
- 若为限价单，`limit_price` 必须为正数且符合交易最小价格变动单位（tick）约束。
- 期货仅支持现金结算或可安全平仓品种；不做实物交割流程管理。

---

## 7. 状态定义（策略）
- `PENDING_ACTIVATION`（已创建，待激活）
- `ACTIVE`
- `PAUSED`（已暂停，不参与条件监测）
- `TRIGGERED`
- `ORDER_SUBMITTED`
- `FILLED`
- `EXPIRED`
- `CANCELLED`
- `FAILED`

---

## 8. 验收标准（DoD）
1. 可成功创建、查询、取消策略。
2. `SLV <= 60` 条件可被持续监测并正确触发。
3. 同一策略只触发一次下单，不会重复下单。
4. 到期未满足时，策略按时转 `EXPIRED`。
5. 重启后 `ACTIVE` 策略继续监测，状态不丢失。
6. paper 环境下至少一条端到端用例通过。
7. 至少一条期货自动展期用例通过（满足条件后完成“平近开远”且无重复执行）。
8. 至少一条“双产品组合条件”用例通过（任意两产品的流动性+价差按 `AND/OR` 触发正确）。
9. 至少一条“单产品流动性条件”用例通过（阈值触发正确）。
10. 至少一条“链式触发 + 回撤比例执行”用例通过（上游激活后，下游按比例条件执行正确）。
11. 至少一条“下游激活”用例通过（上游触发后下游被正确激活且无重复触发）。
12. 至少一条“回撤比例触发”与一条“上涨比例触发”用例通过。
13. 至少一条“核验拒绝”用例通过（如金额超限或 `MKT` 被禁止时正确拒绝且不发单）。
14. 至少一条“仅上游激活”用例通过（`upstream_only_activation=true` 时手动激活被拒绝）。
15. 至少一条“相对过期时间”用例通过（`expire_in_seconds` 从激活时刻开始计时）。
16. 至少一条“已发单到期”用例通过（`ORDER_SUBMITTED` 到期不转 `EXPIRED`，按收尾策略处理）。
17. UI 展示符合页面职责：列表仅展示摘要，详情展示完整结构；全局事件与交易日志字段齐全（含 `strategy_id` / `trade_id`）。
18. 至少一条“窗口评估”用例通过（`evaluation_window=5m` 且 `MONITOR_INTERVAL_SECONDS=20` 时按滚动窗口计算）。
19. 至少一条“条件数量上限”用例通过（超过 `MAX_CONDITIONS_PER_STRATEGY` 被拒绝）。
20. 至少一条“链式延迟激活补偿”用例通过（如下游延迟 1s 激活时，`HIGHEST_SINCE_ACTIVATION` 仍包含该 1s 内真实高点）。
21. 至少一条“暂停/恢复”用例通过（`ACTIVE->PAUSED->ACTIVE` 且暂停期间不触发新下单）。

---

## 9. 里程碑建议
1. M1：策略 CRUD + 监测 + 触发下单 + 到期中止 + 持久化恢复。
2. M2：期货支持与自动展期（含到期/流动性触发）。
3. M3：增强风控、通知与更高级订单能力。

---

## 10. 示例全集（来自当前需求讨论）

### 示例 A：单标的价格触发买入（你最初的 SLV 例子）
目标：当 `SLV` 价格低于阈值时买入固定数量，到期未触发则中止。

- 条件：`price(SLV) <= 60`
- 动作：`action_type=STOCK_TRADE`，`BUY 100` 股
- 订单：`MKT` 或 `LMT`
- 到期：`expire_at` 或 `expire_in_seconds`（最长 1 周）

### 示例 B：链式分档回撤卖出（你后续确认的简化方案）
目标：触及关键位后，先执行 10% 回撤卖出，并在执行时激活 20% 回撤策略。

1. 上游策略 `S0`
- 条件：`price(SLV) >= 100`
- `trade_action`：空（仅负责激活）
- `next_strategy_id`：`S1`，并写入 `anchor_price`

2. 中间策略 `S1`
- 条件：`drawdown_pct(SLV, HIGHEST_SINCE_ACTIVATION) >= 0.1`
- 标志：`upstream_only_activation=true`
- `trade_action`：`SELL 100` 股
- `next_strategy_id`：`S2`（同一触发事件同时执行）

3. 下游策略 `S2`
- 条件：`drawdown_pct(SLV, HIGHEST_SINCE_ACTIVATION) >= 0.2`
- 标志：`upstream_only_activation=true`
- `trade_action`：`SELL 100` 股
- `next_strategy_id`：空

说明：
- `S1` 与 `S2` 各自只允许触发一次。
- `S1` 触发后即使 `S2` 触发失败，也不回滚已成交的 `S1`。

### 示例 C：双产品调仓（任意两个 USD 产品）
目标：在两个产品之间按组合条件自动调仓。

- `product_a=SPY`
- `product_b=QQQ`
- 条件逻辑：`AND`
- 条件 1：`liquidity(QQQ) / liquidity(SPY) >= 1.1`
- 条件 2：`price(QQQ) - price(SPY) <= -120`
- 动作：卖出 `SPY` 固定数量并买入 `QQQ` 固定数量
- 订单：`MKT` 或 `LMT`

### 示例 D：期货自动展期（双产品组合条件）
目标：对期货持仓在满足市场条件时自动从当前合约切到目标合约。

- `sec_type=FUT`
- 待切换合约：`product_a`
- 目标合约：`product_b`
- 触发可组合：
  - 到期天数条件（`ROLL_DAYS_BEFORE_EXPIRY`）
  - 流动性比条件
  - 价差条件
- 动作：先平待切换合约，再开目标合约（受风控约束）

### 示例 F：期货开平仓
目标：对指定期货合约执行开仓或平仓。

- `sec_type=FUT`
- 动作类型：`action_type=FUT_POSITION`
- 示例 1：`position_effect=OPEN`, `side=BUY`, `quantity=2`, `contract=SIH7`
- 示例 2：`position_effect=CLOSE`, `side=SELL`, `quantity=1`, `contract=SIH7`

### 示例 E：上涨比例触发
目标：当价格相对基准上涨达到阈值时触发交易。

- 条件：`metric=RALLY_PCT`
- 参数：`metric=RALLY_PCT`, `value=0.08`（上涨 8%）
- 基准：`price_reference=LOWEST_SINCE_ACTIVATION`（`RALLY_PCT` 场景）
- 动作：可配置 `BUY` 或 `SELL`
