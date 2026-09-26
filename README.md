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
- `GET /api/records/{id}`：记录详情（含`branch`归属），仅本分行可见。
- `GET /api/records/{id}/audit`：审计时间线，仅本分行可见。
- `GET /api/stats`：本分行状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，记录归入创建人所在分行。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`，仅本分行可执行。
- `POST /api/records/{id}/transfers`：管理员发起转办，请求体为`{"to_branch":"...","reason":"..."}`；确认前原分行继续办理。
- `GET /api/records/{id}/transfers`：转办记录，保留新旧分行、原因和经办人。
- `GET /api/transfers/incoming`：本分行的待确认转入列表（管理员）。
- `POST /api/transfers/{id}/confirm`：目标分行管理员确认转办，确认后归属与待办切换到新分行；并发确认仅最先提交生效，其余返回409。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，`X-Org`标识分行；列表、详情、统计、时间线和动作均按`X-Org`隔离，跨分行访问一律返回404。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
