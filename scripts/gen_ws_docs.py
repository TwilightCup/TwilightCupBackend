"""生成 WebSocket 协议文档 ``docs/ws-protocol.md``。

从 ``twilightcupbackend/protocol.py`` 内省消息模型与字段，结合本文件内的中文描述，
产出稳定的协议参考（字段表自动同步代码，描述集中维护）。

用法::

    uv run python scripts/gen_ws_docs.py
"""

from __future__ import annotations

import types
import typing
from enum import Enum
from pathlib import Path
from typing import Literal, Union, get_args, get_origin

from twilightcupbackend import protocol
from twilightcupbackend.datatypes import (
    AccountType,
    AttemptStatus,
    MatchPhase,
    PickType,
    PlayerStatus,
    RoundVerdict,
    ScoringMethod,
    Seat,
)

# 消息描述（按模型类名），缺省则标记“待补充”。
DESCRIPTIONS: dict[str, str] = {
    # 客户端 -> 服务端
    "ClientChat": "选手/裁判发送的聊天文本（以 ``!`` 开头会被当作命令解析："
    "``!ready`` 仅选手、``!roll`` 所有人、``!timer [秒]|reset`` 与 "
    "``!lang [id]`` 仅裁判——切换比赛系统消息语言，见 docs/locales.md）。",
    "ClientReadyToggle": "预留消息：实际准备切换走 ClientChat !ready 命令（仅选手）。",
    "ClientLevelTimeUpload": "每关完成时上报用时（断线重连后用于幂等补传）。",
    "ClientAttemptSkip": "单关项目跳过某次尝试，记为 N/A。",
    "ClientProjectComplete": "本回合项目全部完成。",
    "ClientForfeitSignal": "弃权信号（多关退出 / 单关退出且 0 次有效成绩）。",
    "ClientReconnectResync": "断线重连后请求本回合权威快照。",
    "ClientRefereeMarkPrep": "裁判标记进入回合准备阶段。",
    "ClientRefereeSelectPick": "裁判从图池选定本回合选图；CT 类别可随消息提交词条"
    "（0-ct_tag_count 个，服务端校验枚举/互斥/数量）。",
    "ClientRefereeManualStart": "裁判手动发起开始（触发不可中断的倒计时）。",
    "ClientRefereeVerdict": "裁判判定本回合胜负。",
    "ClientRefereeEditVerdict": "裁判事后修改本回合判定（实时同步导播端）。",
    "ClientRefereeTerminateRound": "裁判强制终止当前回合（异常处置）。",
    "ClientRefereeEndMatch": "裁判手动结束比赛（胜方按比分自动判定；需已达到取胜"
    "分数）。常规流程下达到取胜分数时判定落定即自动结束，本消息用于兜底"
    "（改判后比分重回阈值、异常卡住的场次）。",
    "ClientCounterStart": "裁判启动独立倒计时器（由 ``!timer [秒]`` 触发）。",
    "ClientCounterReset": "裁判停止当前倒计时器（由 ``!timer reset`` 触发）。",
    "ClientDirectorSubscribe": "导播订阅（占位，导播连接天然只读）。",
    "ClientHeartbeat": "心跳保活（导播亦可用）。",
    "ClientDraftSync": "裁判上报 ban/pick 草稿（前端权威，后端存储+转发）",
    # 服务端 -> 客户端
    "SrvAuthOk": "连接鉴权成功，告知座位与比赛。",
    "SrvAuthError": "连接鉴权失败（令牌无效/未参与比赛等）。",
    "SrvChat": "广播一条聊天消息（含发送者自己的回声）。",
    "SrvSystem": "广播一条系统消息（命令回执、倒计时提示、回合信息等）。",
    "SrvReadyState": "双方准备状态变更。",
    "SrvSeatState": "座席连接状态（选手连入/断开广播；新连接初始化序列亦补发全量）。",
    "SrvPhaseChange": "比赛阶段切换。",
    "SrvCountdownTick": "开始倒计时逐秒提示。",
    "SrvCountdownAbort": "开始倒计时被中断（auto 倒计时下选手取消准备）。",
    "SrvRoundStart": "回合开始，向选手下发选图与关卡合集配置。"
    'pick.single_scoring 为本场单关计分方式快照（"fastest"/"average"，'
    "来自 Match.scoring_method；缺席或 null 时客户端按 fastest 处理，"
    "MULTI 回合忽略；见 backend-round-start-single-scoring）。",
    "SrvRoundStartedBroadcast": "回合开始广播（含项目编号与名称；tags 为 CT 词条）。",
    "SrvPlayerStatus": "选手单回合实时状态（重连快照亦复用此消息）。",
    "SrvLevelTimeUpdate": "某选手单关用时更新（裁判/导播）。",
    "SrvRoundResult": "本回合结算（判定与双方成绩）。",
    "SrvCumulativeScore": "累计比分。",
    "SrvMatchEnd": "比赛结束，宣告胜方（判定落定且比分达到取胜分数时自动触发）。",
    "SrvCounterState": "独立倒计时器状态（剩余秒数或 None）。",
    "SrvCounterAlert": "独立倒计时器告警（整分钟/30·20·10/5..1/0）。",
    "SrvVerdictEdit": "判定被修改的广播（导播端）。",
    "SrvDraftState": "广播 ban/pick 草稿给全员（含导播）；state 原样转发自裁判端。",
    "SrvMatchStatus": "比赛状态变更广播（pause/resume），导播/裁判多标签同步。",
    "SrvDisplaced": "本连接被同身份（账号+座位+比赛）且带 ``exclusive=1`` 的新连接"
    "顶掉：先于 close(4001) 送达。被顶掉 ≠ 鉴权失败（token 仍有效），"
    "前端应停止自动重连并提示「已在其他窗口打开」。",
    "SrvError": "错误回执（命令非法/权限不足/比赛已暂停等）。",
}

ENUMS: list[tuple[str, type[Enum]]] = [
    ("seat 座位", Seat),
    ("phase 比赛阶段", MatchPhase),
    ("player_status 选手状态", PlayerStatus),
    ("attempt_status 尝试状态", AttemptStatus),
    ("verdict 回合判定", RoundVerdict),
    ("pick_type 项目类型", PickType),
    ("scoring_method 单关计分", ScoringMethod),
    ("account_type 账号角色（account.roles 取值，一个账号可含多个）", AccountType),
]


def _fmt_type(annotation: object) -> str:
    if annotation is type(None):
        return "None"
    origin = get_origin(annotation)
    args = get_args(annotation)
    if isinstance(annotation, types.UnionType) or origin is Union:
        return " | ".join(_fmt_type(a) for a in args)
    if origin is Literal:
        return " | ".join(repr(a) for a in args)
    if origin is not None and args:
        # 泛型别名，如 list[LevelTime] / dict[str, object]
        return f"{_fmt_type(origin)}[{', '.join(_fmt_type(a) for a in args)}]"
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    return str(annotation).replace("typing.", "")


def _union_members(alias: object) -> list[type]:
    """从 ServerMessage(PEP604 联合) 或 ClientMessage(Annotated[Union, ...]) 取成员。"""
    origin = get_origin(alias)
    if origin is typing.Annotated:  # type: ignore[attr-defined]
        inner = get_args(alias)[0]
        return list(get_args(inner))
    return list(get_args(alias))


def _type_value(model: type) -> str:
    return repr(model.model_fields["type"].default)


def _fields_table(model: type) -> str:
    rows = ["| 字段 | 类型 | 必填 | 默认 | 说明 |", "| --- | --- | --- | --- | --- |"]
    for name, fi in model.model_fields.items():
        if name == "type":
            continue
        if fi.is_required():
            required, default = "是", "—"
        elif fi.default_factory is not None:
            required, default = "否", f"<{fi.default_factory.__qualname__}>"
        else:
            required, default = "否", repr(fi.default)
        cells = [
            f"`{name}`",
            _fmt_type(fi.annotation),
            required,
            default,
            fi.description or "",
        ]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _enum_table(enum: type[Enum]) -> str:
    rows = ["| 名称 | 值 |", "| --- | --- |"]
    for member in enum:
        rows.append(f"| `{member.name}` | {int(member.value)} |")
    return "\n".join(rows)


def main() -> None:
    client_models = _union_members(protocol.ClientMessage)
    server_models = _union_members(protocol.ServerMessage)

    out: list[str] = []
    out.append("# WebSocket 协议\n")
    out.append(
        "选手端 / 裁判端 / 导播端与服务端的实时通信协议。本文档由 "
        "`scripts/gen_ws_docs.py` 从 `src/twilightcupbackend/protocol.py` 自动生成"
        "（字段表与代码同步，描述集中维护于生成脚本）。\n"
    )
    out.append("## 连接与鉴权\n")
    out.append(
        "- 端点 `ws://<host>/ws/{token}`，token 为登录返回的 JWT。\n"
        "- 可选 ?seat=NAME（PLAYER_A/PLAYER_B/REFEREE/DIRECTOR）指定座位身份。\n"
        "- 可选 ?match=ID 连到指定比赛（裁判/导播多标签页选场）。\n"
        "- 可选 ?exclusive=1 要求独占身份 key（账号+座位+比赛）：同 key 既有连接"
        "先收 `displaced` 再被 close(4001) 顶掉，新连接照常 auth_ok + 快照；"
        "被顶掉连接的在途消息一律忽略。key 含 match，故裁判不同场多标签、"
        "多角色多座位互不影响；导播 OBS 多源不带 exclusive 仍并存"
        "（裁判端/选手端用，导播各场景页不用）。\n"
        "- 鉴权成功后先发 `auth_ok`，再推 `ready_state`、`phase_change`。\n"
        "- 导播连接只读：除 `director_subscribe`/`heartbeat` 外入站一律拒绝。\n"
        "- 多角色账号可开多条连接（不同 seat 各一条）；同 seat 重连替换旧连接"
        "（不带 exclusive 时为静默替换，关闭码 1000）。\n"
        "- 回合中发 `reconnect_resync` 取快照后幂等补传。\n"
        "- 不带 `seat` 时按比赛指派取首个匹配（选手 A/B 由此确定）。\n"
        "- 编码 JSON，带 `type` 判别字段（下表 type 列即其字面量）。\n"
    )

    out.append("## 枚举取值\n")
    for title, enum in ENUMS:
        out.append(f"**{title}**\n")
        out.append(_enum_table(enum) + "\n")

    out.append("## 客户端 → 服务端\n")
    for model in client_models:
        out.append(f"### `{model.__name__}`\n")
        out.append(f"- type：{_type_value(model)}\n")
        out.append(f"- {_desc(model)}\n")
        out.append(_fields_table(model) + "\n")

    out.append("## 服务端 → 客户端\n")
    for model in server_models:
        out.append(f"### `{model.__name__}`\n")
        out.append(f"- type：{_type_value(model)}\n")
        out.append(f"- {_desc(model)}\n")
        out.append(_fields_table(model) + "\n")

    target = Path(__file__).resolve().parent.parent / "docs" / "ws-protocol.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out), encoding="utf-8")
    print(f"已生成 {target}")
    print(f"  客户端 {len(client_models)} / 服务端 {len(server_models)} 条消息")


def _desc(model: type) -> str:
    text = DESCRIPTIONS.get(model.__name__)
    if not text:
        missing = DESCRIPTIONS.setdefault(model.__name__, "(待补充)")
        return missing
    return text


if __name__ == "__main__":
    main()
