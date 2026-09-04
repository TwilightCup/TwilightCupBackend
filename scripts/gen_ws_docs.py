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
    "ClientPreloadReport": "选手端预载状态上报（仅 PLAYER_A/PLAYER_B，PREP 阶段有意义；"
    "场景级预载仅 MULTI 合集，SINGLE 报 ``na``）。``failed`` 不阻塞开局"
    "（round_start 时选手端回退标准加载），仅触发 kind=preload 告警；"
    "旧版客户端连接不带 ``cap=preload1`` 不上报，门控豁免。",
    "ClientSubsegmentSample": "选手端分段采样上报（仅 MULTI 回合、PLAYER 席位，"
    "每秒一次）：每关「角色从装死苏醒」到「触碰通关判定区」窗口内采样位置与"
    "运动向量，t_ms 为该选手计时器（TwilightTimer）时间线上的当前总时间"
    "（与官方计分同一时钟）；位移全 0 = 该秒近乎静止（照存不建检测平面）。"
    "plane_radius 为可选的本端检测平面半径（米），服务端用于陈旧平面回路识别，"
    "旧版客户端可缺省。纯内存回合级数据，不落库、回合结束清空。",
    "ClientSubsegmentHit": "选手穿越对手采样平面时上报（仅 MULTI 回合）；"
    "同一平面可多次上报（擦边往复/曲折路线绕回/失败折返均如实上报，客户端"
    "同平面防抖）。t_ms 为穿越时刻自己计时器时间线上的总时间。服务端按 "
    "settled-event 模型结算：某平面最后一次穿越后静默期（约 0.5s）无再穿越"
    "才广播，有效时刻取最后一次穿越；低于已结算进度游标的迟到乱序事件忽略，"
    "但距该席最近一次穿越 ≥3s 的低键穿越视为「失败折返重来」的真实重访，"
    "重开该键按当前时刻广播（计时器坠落不清零，数值自带罚时成本）。路线自然"
    "回路再次经过陈旧平面（穿越方早于采样方到过该点且轨迹无复活级跳变）"
    "不广播，避免领先被误播成大幅落后。",
    "ClientLiveTime": "选手端实时计时上报（每秒一次，随 subsegment 采样节拍）："
    "total_ms/segment_ms 取自其注册的真实计时器（TwilightTimer）的 "
    "RoundTotalMs/CurrentSegmentMs；real_time_ms 可选，为提供方 Real Time "
    "现实/墙钟计时（TwilightTimer 实现 IRealtimeTimerProvider 时附带）；"
    "level_index 为当前所在合集关卡。仅中转裁判/导播，选手间互不转发。",
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
    "ClientDirectorCommand": "导播控制台发往同账号其他导播连接（OBS 舞台）的操控"
    '指令：场景切换（``switch_scene``，payload ``{"scene": ...}``）、Coming '
    "Soon 倒计时操控（``soon_start``/``soon_pause``/``soon_reset``/"
    '``soon_set_target``，set_target payload ``{"target_ms": ...}``）与直播配置'
    '实时下发（``config_update``，payload ``{"config": {...}}``，八个字符串键'
    "rtmpA/rtmpB/hlsA/hlsB/pbA/pbB/histA/histB，可部分缺失，服务端不校验、"
    "原样透传）。服务端以 ``director_cmd`` 原样定向转发，不落库、不回执发送方。",
    "ClientUtcTimestamp": "选手端 UTC 时间戳周期上报（连接后按固定间隔发送，间隔在"
    "选手端配置中设置）：utc_ms 为 Unix UTC 毫秒时间戳。仅选手席位有效；"
    "服务端按席暂存最近一条并中转裁判/导播，不参与比赛判定。",
    "ClientHeartbeat": "心跳保活（导播亦可用）。",
    "ClientDraftSync": "裁判上报 ban/pick 草稿（前端权威，后端存储+转发）",
    # 服务端 -> 客户端
    "SrvAuthOk": "连接鉴权成功，告知座位与比赛。",
    "SrvAuthError": "连接鉴权失败（令牌无效/未参与比赛等）。",
    "SrvChat": "广播一条聊天消息（含发送者自己的回声）。",
    "SrvSystem": "系统消息：全场广播（命令回执、倒计时提示、回合信息等）或"
    "单席位定向提示。sender 为聊天展示前缀：广播 ``Twilight``（与落库 "
    "ChatMessage.sender_name 一致，全员逐字相同）；定向提示 ``System``"
    "（仅目标席位收到、不落库，如重连回 PREP 的补发提示）。",
    "SrvReadyState": "双方准备状态变更。",
    "SrvPreloadState": "双方预载状态广播（上报/重置时；取值 "
    "absent|in_progress|done|failed|na，absent=从未上报）。",
    "SrvSeatState": "座席连接状态（选手连入/断开广播；新连接初始化序列亦补发全量）。",
    "SrvPhaseChange": "比赛阶段切换。",
    "SrvCountdownTick": "开始倒计时逐秒提示。",
    "SrvCountdownAbort": "开始倒计时被中断（auto 倒计时下选手取消准备）。",
    "SrvPickAnnounced": "选图确定即向全体成员提前下发合集（预览性质；"
    "``round_start`` 仍是唯一权威，PREP 期间改图导致两者不同属正常流程，"
    "选手端自行作废旧预载）。裁判重新应用选图会重发，以最新一次为准；"
    "pick 与 collection 与 ``round_start`` 同构（含词条/重试/计分方式、"
    "关卡 id 已展开为显示名）。",
    "SrvRoundStart": "回合开始，向选手下发选图与关卡合集配置。"
    'pick.single_scoring 为本场单关计分方式快照（"fastest"/"average"，'
    "来自 Match.scoring_method；缺席或 null 时客户端按 fastest 处理，"
    "MULTI 回合忽略；见 backend-round-start-single-scoring）。",
    "SrvRoundStartedBroadcast": "回合开始广播（含项目编号与名称；tags 为 CT 词条）。",
    "SrvPlayerStatus": "选手单回合实时状态（重连快照亦复用此消息）。",
    "SrvLevelTimeUpdate": "某选手单关用时更新（裁判/导播）。",
    "SrvSubsegmentSample": "转发对手的采样点给对侧选手（其客户端据此建检测平面）；"
    "仅发对方 seat（裁判/导播不收），选手断线重连后按原序补放。",
    "SrvSubsegmentGap": "实时时间差广播（双方选手 + 裁判 + 导播；overlay 用）："
    "平面穿越结算（静默期无再穿越）后发出，``gap_ms = hit_ms - sample_ms``，"
    ">0 = 穿越方落后，可为负。同键或更低键可能再次收到——结算后再次穿越的"
    "修正（amend）或失败折返重来的重访（重穿时刻自带罚时成本，画面随真实"
    "进度更新）——前端展示取最新一条，进度类 UI 需自持最大 seq"
    "（服务端游标不回退）。路线自然回路的陈旧平面穿越不会结算广播。",
    "SrvLiveTime": "选手实时计时中转（每秒；仅裁判与导播席，overlay 计时显示用）："
    "服务端按席暂存最近一条，IN_ROUND 期间裁判/导播晚连时握手补发双方；"
    "real_time_ms 为可选字段，选手端提供方支持现实/墙钟计时时携带。",
    "SrvUtcTimestamp": "选手 UTC 时间戳中转（连接后按固定间隔；仅裁判与导播席）："
    "服务端按席暂存最近一条，裁判/导播（含晚连）连入时握手补发双方，"
    "用于时钟偏移/同步显示。",
    "SrvRoundResult": "本回合结算（判定与双方成绩）。",
    "SrvCumulativeScore": "累计比分。",
    "SrvMatchEnd": "比赛结束，宣告胜方（判定落定且比分达到取胜分数时自动触发）。",
    "SrvCounterState": "独立倒计时器状态（剩余秒数或 None）。",
    "SrvCounterAlert": "独立倒计时器告警（整分钟/30·20·10/5..1/0）。",
    "SrvVerdictEdit": "判定被修改的广播（导播端）。",
    "SrvDraftState": "广播 ban/pick 草稿给全员（含导播）；state 原样转发自裁判端。",
    "SrvDirectorCommand": "定向转发导播控制台操控指令（action/payload 原样来自 "
    "``director_command``）：仅发发送方之外的同账号 DIRECTOR 连接（OBS 舞台），"
    "每个导播只控自己的舞台；选手/裁判与其他账号导播均不收。另含服务端主动"
    "下发的 ``state_sync``：DIRECTOR 连接 ``auth_ok`` 后若有状态暂存，补发最近"
    '的场景/倒计时/直播配置（payload ``{"scene"/"soon"/"config"}``，soon 内'
    "时间戳均为服务器毫秒、附 ``now_ms`` 供时钟校正）。",
    "SrvMatchStatus": "比赛状态变更广播（pause/resume），导播/裁判多标签同步。",
    "SrvDisplaced": "本连接被同身份（账号+座位+比赛）且带 ``exclusive=1`` 的新连接"
    "顶掉：先于 close(4001) 送达。被顶掉 ≠ 鉴权失败（token 仍有效），"
    "前端应停止自动重连并提示「已在其他窗口打开」。",
    "SrvError": "错误回执（命令非法/权限不足/比赛已暂停等）。仅发给触发方"
    "（特定连接/席位，不广播、不落库）；客户端展示沿用 ``System`` 前缀，"
    "与全场广播的 ``system`` 消息（``Twilight``）区分。",
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
        "- 可选 ?cap=（逗号分隔的能力声明，如 `preload1`=会上报预载状态）；"
        "预载开局门控只对声明了能力的席位生效。\n"
        "- 可选 ?exclusive=1 要求独占身份 key（账号+座位+比赛）：同 key 既有连接"
        "先收 `displaced` 再被 close(4001) 顶掉，新连接照常 auth_ok + 快照；"
        "被顶掉连接的在途消息一律忽略。key 含 match，故裁判不同场多标签、"
        "多角色多座位互不影响；导播 OBS 多源不带 exclusive 仍并存"
        "（裁判端/选手端用，导播各场景页不用）。\n"
        "- 鉴权成功后先发 `auth_ok`，再推 `ready_state`、`phase_change`；"
        "PREP 阶段选手席补发 `pick_announced`（有待选图时），各席位补发"
        " `preload_state` 快照；选手席另收仅其可见的 System 前缀定向提示："
        "当前选图（有选图时）与未就绪时的 prep 提示。选手连入时向全员"
        "（含本人）广播 `seat.online` 系统消息（广播 system 消息 = Twilight "
        "前缀，各端逐字一致）。\n"
        "- 导播连接只读：除 `director_subscribe`/`heartbeat`/`director_command` 外"
        "入站一律拒绝；`director_command` 仅定向转发给同账号其他导播连接"
        "（OBS 舞台），不影响比赛状态。\n"
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
