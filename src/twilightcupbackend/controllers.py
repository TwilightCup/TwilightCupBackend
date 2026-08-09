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
