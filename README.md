# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/disposal_ledger.py`：处置核验台账，三类记录登记、更正留档和结论留档。
- `src/disposal_verification.py`：处置核验，关闭条件和缺失类别判定。
- `src/disposal_api.py`：处置核验接口路由。
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
- `GET /api/items/{id}/disposal-records`，`POST`登记处置记录
- `POST /api/disposal-records/{id}/correct`，更正原始记录
- `GET /api/items/{id}/disposal-verification`，当前核验状态
- `GET /api/items/{id}/disposal-conclusions`，历史核验结论
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级。

## 处置核验

处置台账分三类记录，均关联事件并登记经办人：回收数量（`recovery`，登记`quantity`）、岸线复查（`shoreline`，登记`result`为`normal`/`abnormal`）、废弃物去向（`waste`，登记`destination`和`transfer_status`为`pending`/`completed`）。事件关闭必须同时满足：回收量合计达到估算油量的八成、最近一次岸线复查正常、废弃物全部完成交接；不满足时返回缺哪一类。更正原始记录会作废旧记录并写入新记录，旧记录留档；若事件已关闭则自动回到`pending_review`待复核，历史核验结论继续留档，复核通过后才能重新关闭。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
