# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：本分行记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（限本分行）。
- `GET /api/records/{id}/audit`：审计时间线（限本分行）。
- `GET /api/records/{id}/transfers`：该记录的转办历史（限本分行）。
- `GET /api/stats`：本分行状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，记录归入创建人所在分行。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/transfer`：管理员发起转办，请求体为`{"to_org":"...","reason":"..."}`；同一记录仅允许一条待确认转办。
- `POST /api/transfers/{id}/confirm`：目标分行管理员确认转入；确认后记录归属与待办切换到新分行，并发确认仅先提交的生效，其余返回409。
- `GET /api/transfers/incoming`：本分行的转入转办单（待办）。
- `GET /api/transfers/outgoing`：本分行的转出转办单。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。`X-Org`决定数据归属：列表、详情、统计和动作只返回本分行的记录，跨分行访问一律返回404；目标分行确认前原分行可继续办理。转办记录永久保留新旧分行、原因和经办人。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
