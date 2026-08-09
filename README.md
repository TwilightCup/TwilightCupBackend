# 黄昏杯 (Twilight Cup) 后端服务

《人类一败涂地》(Human: Fall Flat) 1v1 速通比赛的中心中转服务端。中转选手端 /
裁判端 / 导播端之间的全部消息，管理多场相互隔离的比赛会话，解析 `!` 聊天命令，
驱动比赛生命周期，计算成绩并持久化日志。

需求文档：[`黄昏杯需求文档.md`](./黄昏杯需求文档.md)。

## 技术栈

Python 3.14 · uv + PEP 621 + hatchling · ruff + pyright · FastAPI + classy-fastapi
+ uvicorn[standard] + uvloop · orjson + pydantic v2 · MongoDB · 异步优先 + 无锁并发。

## 快速开始

```bash
# 安装依赖（含 dev）
uv sync --extra dev

# 配置环境变量
cp .env.example .env  # 编辑 .env

# 起一个本地 MongoDB（或用 docker compose）
docker compose -f docker-compose-example.yml up -d mongo

# 启动服务
uv run uvicorn twilightcupbackend.main:app --reload

# 代码检查
uv run ruff check src tests
uv run pyright
uv run pytest
```

## 项目结构

`src/twilightcupbackend/`：每个模块职责单一、全类型注解、无锁并发（内存状态仅在
asyncio 事件循环内变更）。详见各模块 docstring。

## 接口文档

服务端提供两类接口，文档各有出处：

### REST（OpenAPI）

启动服务后即可在线浏览自动生成的 OpenAPI 文档：

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 原始 schema：<http://localhost:8000/openapi.json>

导出为静态文件（交付前端 / 离线查看）：

```bash
uv run python scripts/export_openapi.py   # → docs/openapi.json
```

覆盖：账号管理、比赛会话管理、日志查询（详见 `/docs` 的标签分组）。所有非健康检查
接口均需 Bearer JWT（`POST /auth/login` 获取）。

**账号多角色**：`Account.roles` 为角色集合（一个账号可同时拥有选手/裁判/导播/管理员的
任意组合）；创建时选「管理员」会自动附带裁判 + 导播。权限按集合判定（如 `require_viewer`
要求 roles 含 ADMIN/REFEREE/DIRECTOR 任一）。多角色账号可同时开多条不同 `seat` 的
WebSocket 连接（如同时以裁判 + 导播身份在线）。

### WebSocket（实时通信协议）

选手 / 裁判 / 导播 三端的实时通信走 WebSocket（端点 `ws://<host>/ws/{token}`，可选
`?seat=PLAYER_A|PLAYER_B|REFEREE|DIRECTOR` 让多角色账号指定本连接身份），
FastAPI 不自动生成其文档，故维护一份**由代码自动生成**的协议参考：

- 文档：[`docs/ws-protocol.md`](./docs/ws-protocol.md)
- 重新生成（`protocol.py` 变更后）：`uv run python scripts/gen_ws_docs.py`

包含：连接与鉴权流程、枚举取值、全部 17 条客户端消息与 19 条服务端消息的字段表。

