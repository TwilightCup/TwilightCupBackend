"""系统消息本地化测试：!lang 切换 / 回退 / 权限 / LocaleCatalog 单元行为。

默认语言 en 下现有断言见各模块测试；本文件聚焦语言切换后的渲染变化。
"""

from __future__ import annotations

from pathlib import Path

from twilightcupbackend.i18n import LocaleCatalog

_LOCALES_DIR = Path(__file__).resolve().parents[1] / "locales"


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _skip_echo(ws) -> None:  # type: ignore[no-untyped-def]
    """跳过命令回显的那条普通 chat 消息。"""
    msg = ws.receive_json()
    assert msg["type"] == "chat"


def test_lang_switch_changes_messages(world) -> None:  # type: ignore[no-untyped-def]
    """裁判 !lang zh 后：切换确认、!roll、SrvError 均以中文渲染。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)

        # 切到中文：确认消息本身用新语言
        ws_r.send_json({"type": "chat", "text": "!lang zh"})
        _skip_echo(ws_r)
        msg = ws_r.receive_json()
        assert msg["type"] == "system" and "语言已切换为：zh" in msg["text"]

        # !roll → 中文 + 座位中文名
        ws_r.send_json({"type": "chat", "text": "!roll"})
        _skip_echo(ws_r)
        msg = ws_r.receive_json()
        assert msg["type"] == "system" and "裁判 掷出" in msg["text"]

        # 选手后连（5 条初始化 + 裁判侧无其积压）；其 !timer 的 SrvError 随比赛语言
        with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
            _drain(ws_r, 1)  # 选手上线 seat_state 广播
            _drain(ws_a, 5)
            ws_a.send_json({"type": "chat", "text": "!timer 30"})
            _skip_echo(ws_a)
            msg = ws_a.receive_json()
            assert msg["type"] == "error" and msg["code"] == 403
            assert msg["msg"] == "仅裁判可使用 !timer"
            _skip_echo(ws_r)  # 裁判收到该命令的聊天中转（error 只单播给选手）

        # 切回英文（选手退出产生一条下线 seat_state 需先消费）
        _drain(ws_r, 1)
        ws_r.send_json({"type": "chat", "text": "!lang en"})
        _skip_echo(ws_r)
        msg = ws_r.receive_json()
        assert msg["type"] == "system" and "Language switched to: en" in msg["text"]


def test_lang_list_and_unknown(world) -> None:  # type: ignore[no-untyped-def]
    """无参列出可用语言；未知 id 报 400 并列出可选项。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws:
        _drain(ws, 5)
        ws.send_json({"type": "chat", "text": "!lang"})
        _skip_echo(ws)
        msg = ws.receive_json()
        assert msg["type"] == "system" and "en" in msg["text"] and "zh" in msg["text"]

        ws.send_json({"type": "chat", "text": "!lang xx"})
        _skip_echo(ws)
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == 400
        assert "xx" in msg["msg"] and "zh" in msg["msg"]


def test_lang_referee_only(world) -> None:  # type: ignore[no-untyped-def]
    """非裁判用 !lang 被拒（SrvError 403）。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws:
        _drain(ws, 5)
        ws.send_json({"type": "chat", "text": "!lang zh"})
        _skip_echo(ws)
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == 403


# ----------------------------------------------------------------------
# LocaleCatalog 单元行为（不走 WS）
# ----------------------------------------------------------------------


def _catalog() -> LocaleCatalog:  # type: ignore[no-untyped-def]
    cat = LocaleCatalog()
    cat.load_dir(_LOCALES_DIR, default="en")
    return cat


def test_catalog_languages() -> None:
    assert _catalog().languages() == ["en", "zh"]


def test_catalog_fallback_chain() -> None:
    cat = _catalog()
    # zh 缺键（模拟）→ 回退 en → 仍缺 → 返回键名本身
    assert cat.translate("zh", "roll.result", player="选手A", value=42) == (
        "选手A 掷出 42（1-100）"
    )
    assert cat.translate("en", "roll.result", player="Referee", value=7) == (
        "Referee rolled 7 (1-100)"
    )
    assert cat.translate("zh", "no.such.key") == "no.such.key"
    assert (
        cat.translate("xx", "roll.result", player="A", value=1) == "A rolled 1 (1-100)"
    )


def test_catalog_missing_param_kept_literal() -> None:
    cat = _catalog()
    # 缺参数：占位符原样保留而非抛 KeyError
    assert cat.translate("en", "roll.result") == "{player} rolled {value} (1-100)"


def test_catalog_format_spec() -> None:
    cat = _catalog()
    assert cat.translate("en", "timer.remaining", m=1, s=5) == "Timer remaining 1:05"
    assert cat.translate("zh", "timer.remaining", m=0, s=9) == "剩余时间 0:09"
