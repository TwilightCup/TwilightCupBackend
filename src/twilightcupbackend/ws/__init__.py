"""WebSocket 端点注册（原生 add_api_websocket_route，委托 ConnectionManager）。"""

from __future__ import annotations

from logging import getLogger

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..connection_manager import ConnectionManager

_logger = getLogger("WS")


def register_ws(app: FastAPI) -> None:
    """注册 ``/ws/{token}`` 端点。"""

    async def ws_endpoint(
        websocket: WebSocket,
        token: str,
        seat: str | None = None,
        match: str | None = None,
        cap: str | None = None,
    ) -> None:
        """seat 为可选 query 参数，多角色账号用以指定本连接的座位身份；
        match 为可选 query 参数，连到指定比赛（裁判多标签页选场）；
        cap 为可选 query 参数，逗号分隔的客户端能力声明（如 preload1=会上报预载）。"""
        cm: ConnectionManager = app.state.connection_manager
        conn = await cm.connect(websocket, token, seat, match, cap)
        if conn is None:
            return
        try:
            while True:
                raw = await websocket.receive_text()
                await cm.handle(conn, raw)
        except WebSocketDisconnect:
            _logger.info("WebSocket 正常断开（seat=%s）。", conn.seat.name)
        except Exception:
            _logger.exception("WebSocket 处理异常。")
        finally:
            await cm.disconnect(conn)

    app.add_api_websocket_route("/ws/{token}", ws_endpoint)
