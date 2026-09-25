# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/verification.py`：处置核验规则（回收八成、岸线复查、废弃物交接）。
- `src/ledger.py`：处置核验台账存储，核验记录更正留档、结论归档。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/verifications`：登记处置核验记录（recovery/shoreline/waste）
- `GET /api/items/{id}/verifications`：核验台账（含已更正留档记录）
- `GET /api/items/{id}/verification-status`：当前核验结论和缺失类别
- `POST /api/verifications/{id}/corrections`：更正核验原始记录
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级。

## 处置核验关闭条件

事件关闭（monitoring→closed）前必须同时满足，否则返回409并在`missing`中给出缺哪一类：

- 回收量（recovery记录合计）达到估算油量`quantity`的八成；
- 最近一次岸线复查（shoreline）结果为正常；
- 全部废弃物记录（waste）均已完成交接（handed_over）。

核验记录登记数量/结果/去向和经办人（handler）。原始记录更正时旧记录标记为`superseded`留档并生成新记录；若事件已关闭，自动回到`monitoring`待复核并归档一条`reopened`结论，旧`closed`结论保留。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
