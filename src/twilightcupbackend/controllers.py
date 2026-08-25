"""数据库门面：持有 ``MongoClient`` / ``Database`` 与全部仓库，注入 logger。

参考 AShareGateway controllers.py 的 MongoDBController 风格。
``tz_aware=True`` 使读回的 ``datetime`` 带时区（与模型一致）。
测试时可注入 mongomock 的 ``client`` 以免依赖真实服务。
"""

from __future__ import annotations

from logging import Logger, getLogger
from typing import Any

from pymongo import MongoClient
from pymongo.database import Database

from .config import Settings
from .databases import (
    Accounts,
    ChatMessages,
    Fixtures,
    Levels,
    Mappools,
    Matches,
    MatchLogs,
    Rounds,
    SystemEvents,
    Tournaments,
)
from .datatypes import DEFAULT_TOURNAMENT_ID, Pick, Tournament, TournamentFormat
from .storage import Storage


class DBController:
    """聚合全部仓库的数据库门面。"""

    def __init__(
        self,
        settings: Settings,
        logger: Logger | None = None,
        client: MongoClient[dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger or getLogger("DBController")
        # client 为 None 时连真实 Mongo；测试可注入 mongomock 客户端
        self.client: MongoClient[dict[str, Any]] | None = client or MongoClient(
            settings.mongo_uri, tz_aware=True
        )
        self.database: Database = self.client[settings.db_name]
        self._init_repos()
        self.logger.info("Connected to MongoDB (db=%s).", settings.db_name)

    def _init_repos(self) -> None:
        db = self.database
        self.accounts = Accounts(db)
        self.matches = Matches(db)
        self.rounds = Rounds(db)
        self.chat_messages = ChatMessages(db)
        self.system_events = SystemEvents(db)
        self.match_logs = MatchLogs(db)
        self.mappools = Mappools(db)
        self.levels = Levels(db)
        self.tournaments = Tournaments(db)
        self.fixtures = Fixtures(db)

    def ensure_indexes(self) -> None:
        """创建常用索引（幂等）。"""
        self.accounts.collection.create_index("username", unique=True)
        self.matches.collection.create_index("status")
        self.rounds.collection.create_index([("match_id", 1), ("round_no", 1)])
        self.chat_messages.collection.create_index([("match_id", 1), ("ts", 1)])
        self.system_events.collection.create_index([("match_id", 1), ("ts", 1)])
        self.match_logs.collection.create_index("match_id", unique=True)
        self.mappools.collection.create_index("name", unique=True)
        self.levels.collection.create_index("name", unique=True)
        # 赛程管理
        self.tournaments.collection.create_index("participant_ids")
        self.tournaments.collection.create_index("referee_ids")
        self.tournaments.collection.create_index("director_ids")
        self.tournaments.collection.create_index("status")
        self.fixtures.collection.create_index(
            [
                ("tournament_id", 1),
                ("bracket_side", 1),
                ("round_no", 1),
                ("match_index", 1),
            ],
            unique=True,
        )
        self.fixtures.collection.create_index([("tournament_id", 1), ("status", 1)])
        self.fixtures.collection.create_index("match_id")

    def ensure_default_tournament(self) -> None:
        """确保默认赛事存在（固定主键，幂等），并回填存量孤立比赛。

        默认赛事是所有不经赛程直接创建的比赛的归属容器：不允许删除/修改、
        永不结束（format 仅占位，生成赛程端点已对其禁用）。
        存量 ``tournament_id=None`` 的比赛统一回填到默认赛事。
        """
        existing = self.tournaments.get(DEFAULT_TOURNAMENT_ID)
        if existing is None:
            self.tournaments.insert(
                Tournament(
                    id=DEFAULT_TOURNAMENT_ID,
                    name="黄昏杯",
                    format=TournamentFormat.SINGLE_ELIM,
                    created_by="system",
                )
            )
            self.logger.info("默认赛事已创建 (id=%s)。", DEFAULT_TOURNAMENT_ID)
        elif existing.name == "默认赛事":
            # 品牌名迁移：早期 seed 名为「默认赛事」→「黄昏杯」；管理员改过
            # 名的（≠旧名）不动（幂等，仅识别旧名）
            existing.name = "黄昏杯"
            self.tournaments.replace(existing)
            self.logger.info("默认赛事已更名「黄昏杯」(id=%s)。", DEFAULT_TOURNAMENT_ID)
        # Python 侧过滤 None（避免 Mongo 字段缺失 vs null 歧义，兼容 mongomock）
        for m in self.matches.find():
            if m.tournament_id is None:
                self.matches.update_fields(
                    m.id, {"tournament_id": DEFAULT_TOURNAMENT_ID}
                )

    def ping(self) -> bool:
        """探测数据库连通性。"""
        try:
            assert self.client is not None
            self.client.admin.command("ping")
            return True
        except Exception:
            self.logger.exception("MongoDB ping failed.")
            return False

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.logger.info("MongoDB connection closed.")


# ---------------------------------------------------------------------------
# 跨比赛选手占用校验
# ---------------------------------------------------------------------------


def player_running_conflict(
    db: DBController,
    player_a_id: str,
    player_b_id: str,
    *,
    self_id: str | None,
) -> str | None:
    """检查两名选手是否已处于另一场 RUNNING 比赛中。

    「每个选手同时只能在一个 RUNNING 比赛中」的统一校验：对被切到 RUNNING 的
    比赛的两名选手，查询是否存在**另一个** RUNNING 比赛（``id != self_id``）也含
    同一选手。命中则返回中文冲突文案（指明哪个选手、卡在哪场），供调用方以 409
    抛出；否则返回 None。

    ``self_id`` 为当前比赛 id，结果中排除自身（新建比赛也传其新生成的 id）。
    """
    for label, acc_id in (("选手A", player_a_id), ("选手B", player_b_id)):
        for other in db.matches.find_running_for_player(acc_id):
            if other.id == self_id:
                continue
            who = db.accounts.get(acc_id)
            who_name = who.display_name if who else acc_id
            return f"{label}（{who_name}）当前在比赛「{other.name}」中进行中"
    return None


# ---------------------------------------------------------------------------
# 选图展示图解析（REST 输出层与 WS 下发共用）
# ---------------------------------------------------------------------------


def resolve_pick_logo_url(
    pick: Pick, db: DBController | None, storage: Storage | None
) -> str | None:
    """选图展示图公开 URL：pick 自有 logo（图池编辑器逐选图上传）优先；无则按
    合集关卡回退取「终点关」的 Level.logo（关卡管理页配的展示图）。

    展示图是管理员在两处配的：选图自带（Pick.logo）与关卡库（Level.logo）。
    后者此前只在管理端表格显示、不流向任何下发口——本函数把它并进回退链，
    使图池 / 比赛详情 / 项目信息各场景的选图卡都能显示关卡展示图。
    """
    if storage is None:
        return None
    key = pick.logo
    if key is None and db is not None:
        key = _endpoint_level_logo(pick, db)
    return storage.public_url(key)


def _endpoint_level_logo(pick: Pick, db: DBController) -> str | None:
    """按合集关卡回退的展示图 key：取逆序第一个配了 logo 的关。

    多关即「终点关」口径，与前端官方图按名称解析的约定一致（Aztec% 显示
    Aztec；Any% 终点 Intro_Reprise 常无图，逆序退到 Ice）。关卡值可为库内
    id / 遗留名 / 工坊数字 id：id 与名各查一次，工坊 id 查不到自然跳过
    （前端再按名称回退官方关卡图）。全无图返回 None。
    """
    raw = pick.collection.raw if pick.collection is not None else {}
    vals: list[str] = []
    single = raw.get("level")
    if isinstance(single, str) and single:
        vals.append(single)
    seq = raw.get("levels")
    if isinstance(seq, list):
        vals.extend(v for v in seq if isinstance(v, str) and v)
    for v in reversed(vals):
        lv = db.levels.get(v) or db.levels.get_by_name(v)
        if lv is not None and lv.logo:
            return lv.logo
    return None
