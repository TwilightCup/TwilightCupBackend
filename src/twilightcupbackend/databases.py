"""MongoDB 持久化层：通用 ``Repository[Document]`` + 各集合仓库。

参考 AShareGateway databases.py 的仓库模式，但改用原生 pymongo（不依赖
pydantic-mongo）以获得更好的 Python 3.14 兼容性。文档主键为字符串 UUID，
在 ``id``（模型）与 ``_id``（Mongo）之间映射。
"""

from __future__ import annotations

from typing import Any

from pymongo.collection import Collection
from pymongo.database import Database

from .datatypes import (
    Account,
    ChatMessage,
    CtTag,
    Document,
    Fixture,
    FixtureStatus,
    Level,
    MappoolDoc,
    Match,
    MatchLog,
    MatchStatus,
    RoundRecord,
    SpeedrunCacheDoc,
    SystemEvent,
    Tournament,
)


def model_to_doc(model: Document) -> dict[str, Any]:
    """pydantic 模型 -> Mongo 文档（``id`` 改名为 ``_id``）。"""
    doc: dict[str, Any] = model.model_dump(mode="python")
    doc["_id"] = doc.pop("id")
    return doc


def doc_to_model[D: Document](model_cls: type[D], doc: dict[str, Any]) -> D:
    """Mongo 文档 -> pydantic 模型（``_id`` 改回 ``id``）。"""
    data = dict(doc)
    data["id"] = data.pop("_id")
    return model_cls.model_validate(data)


class Repository[D: Document]:
    """通用 Mongo 仓库。绑定一个模型类与集合名。"""

    def __init__(
        self, database: Database, collection_name: str, model_cls: type[D]
    ) -> None:
        self._collection: Collection = database[collection_name]
        self._model_cls = model_cls

    @property
    def collection(self) -> Collection:
        return self._collection

    def get(self, id: str) -> D | None:
        doc = self._collection.find_one({"_id": id})
        return doc_to_model(self._model_cls, doc) if doc else None

    def find_one(self, filter: dict[str, Any] | None = None) -> D | None:
        doc = self._collection.find_one(filter or {})
        return doc_to_model(self._model_cls, doc) if doc else None

    def find(self, filter: dict[str, Any] | None = None) -> list[D]:
        cursor = self._collection.find(filter or {})
        return [doc_to_model(self._model_cls, doc) for doc in cursor]

    def insert(self, model: D) -> str:
        """插入新文档，返回其 id。"""
        self._collection.insert_one(model_to_doc(model))
        return model.id

    def replace(self, model: D) -> None:
        """按 id 整文档 upsert。"""
        self._collection.replace_one(
            {"_id": model.id}, model_to_doc(model), upsert=True
        )

    def update_fields(self, id: str, fields: dict[str, Any]) -> None:
        """按 id 局部更新（$set）。"""
        self._collection.update_one({"_id": id}, {"$set": fields})

    def delete(self, id: str) -> None:
        self._collection.delete_one({"_id": id})

    def count(self, filter: dict[str, Any] | None = None) -> int:
        return self._collection.count_documents(filter or {})


# 各集合仓库：薄封装，便于后续在类上追加集合专属查询。


class Accounts(Repository[Account]):
    def __init__(self, database: Database) -> None:
        super().__init__(database, "accounts", Account)

    def get_by_username(self, username: str) -> Account | None:
        return self.find_one({"username": username})


class Matches(Repository[Match]):
    def __init__(self, database: Database) -> None:
        super().__init__(database, "matches", Match)

    def find_by_member(self, account_id: str) -> list[Match]:
        """返回该账号参与（任一角色）的比赛。"""
        return self.find(
            {
                "$or": [
                    {"player_a_id": account_id},
                    {"player_b_id": account_id},
                    {"referee_id": account_id},
                    {"director_id": account_id},
                ]
            }
        )

    def find_running_for_player(self, account_id: str) -> list[Match]:
        """返回该账号作为选手且处于 RUNNING（进行中）的比赛——即其「当前活跃比赛」。"""
        return self.find(
            {
                "status": MatchStatus.RUNNING,
                "$or": [
                    {"player_a_id": account_id},
                    {"player_b_id": account_id},
                ],
            }
        )


class Rounds(Repository[RoundRecord]):
    def __init__(self, database: Database) -> None:
        super().__init__(database, "rounds", RoundRecord)

    def find_by_match(self, match_id: str) -> list[RoundRecord]:
        return self.find({"match_id": match_id})


class ChatMessages(Repository[ChatMessage]):
    def __init__(self, database: Database) -> None:
        super().__init__(database, "chat_messages", ChatMessage)

    def find_by_match(self, match_id: str) -> list[ChatMessage]:
        return self.find({"match_id": match_id})


class SystemEvents(Repository[SystemEvent]):
    def __init__(self, database: Database) -> None:
        super().__init__(database, "system_events", SystemEvent)

    def find_by_match(self, match_id: str) -> list[SystemEvent]:
        return self.find({"match_id": match_id})


class MatchLogs(Repository[MatchLog]):
    def __init__(self, database: Database) -> None:
        super().__init__(database, "match_logs", MatchLog)

    def get_by_match(self, match_id: str) -> MatchLog | None:
        return self.find_one({"match_id": match_id})


class Mappools(Repository[MappoolDoc]):
    """图池库：可复用图池定义，创建比赛时按 id 引用。"""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "mappools", MappoolDoc)


class Levels(Repository[Level]):
    """关卡库：图池选图的 collection.raw 按关卡 id 引用此处。"""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "levels", Level)

    def get_by_name(self, name: str) -> Level | None:
        return self.find_one({"name": name})


class CtTags(Repository[CtTag]):
    """词条库：CT 类别支持的词条由管理端维护。"""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "ct_tags", CtTag)

    def get_by_name(self, name: str) -> CtTag | None:
        return self.find_one({"name": name})


class Tournaments(Repository[Tournament]):
    """赛事仓库。"""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "tournaments", Tournament)

    def find_by_participant(self, account_id: str) -> list[Tournament]:
        """返回该选手参赛的赛事（participant_ids 多值查）。"""
        return self.find({"participant_ids": account_id})

    def find_by_referee(self, account_id: str) -> list[Tournament]:
        return self.find({"referee_ids": account_id})

    def find_by_director(self, account_id: str) -> list[Tournament]:
        return self.find({"director_ids": account_id})


class Fixtures(Repository[Fixture]):
    """对阵节点仓库。"""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "fixtures", Fixture)

    def find_by_tournament(self, tournament_id: str) -> list[Fixture]:
        return self.find({"tournament_id": tournament_id})

    def find_by_tournament_round(
        self, tournament_id: str, round_no: int
    ) -> list[Fixture]:
        return self.find({"tournament_id": tournament_id, "round_no": round_no})

    def find_by_match(self, match_id: str) -> Fixture | None:
        return self.find_one({"match_id": match_id})

    def find_pending_ready(self, tournament_id: str) -> list[Fixture]:
        """双方已定、待生成实战比赛的对阵。"""
        return self.find(
            {"tournament_id": tournament_id, "status": FixtureStatus.READY}
        )

    def find_by_member(self, account_id: str) -> list[Fixture]:
        """返回该账号被指派（选手/裁判/导播）的对阵。"""
        return self.find(
            {
                "$or": [
                    {"player_a_id": account_id},
                    {"player_b_id": account_id},
                    {"referee_id": account_id},
                    {"director_id": account_id},
                ]
            }
        )


class SpeedrunCaches(Repository[SpeedrunCacheDoc]):
    """speedrun.com 代理响应持久化缓存（主键为 key 的 sha256，见代理层）。"""

    def __init__(self, database: Database) -> None:
        super().__init__(database, "speedrun_cache", SpeedrunCacheDoc)
