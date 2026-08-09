"""存量迁移：把图池里的关卡名改写为 Level id（建立关卡库）。

背景：CollectionConfig.raw 原存关卡名（{"levels": ["Intro", ...]}）。
本脚本扫 mappools 集合 → 每个名字 upsert 到 levels 关卡库 → 把
raw 改写为对应 level_id（{"levels": [id, ...]}）。matches 历史快照
不迁（快照语义，保留比赛当时状态）。

幂等：已是 id 形态（能命中 Level.id）则跳过。可重复运行。

用法::

    uv run python scripts/migrate_levels.py           # 迁移
    uv run python scripts/migrate_levels.py --dry-run # 只看不动
"""

from __future__ import annotations

import sys

from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import Level


def collect_names(db: DBController) -> set[str]:
    """收集 mappools 里所有出现的、尚未迁移的关卡名。

    已是 level_id（能命中关卡库主键）的值不算名字——幂等关键：
    重复运行时已改写为 id 的引用不会被再次当名字建库。
    """
    names: set[str] = set()
    for doc in db.mappools.find():
        for pick in doc.mappool.all_picks():
            raw = pick.collection.raw
            for val in _raw_values(raw):
                if db.levels.get(val) is None:  # 不是已存在关卡 id
                    names.add(val)
    return names


def _raw_values(raw: dict) -> list[str]:
    """取 raw 里的多关 levels + 单关 level 字符串值。"""
    vals: list[str] = []
    if isinstance(raw.get("levels"), list):
        vals.extend(str(x) for x in raw["levels"] if isinstance(x, str))
    if isinstance(raw.get("level"), str):
        vals.append(raw["level"])
    return vals


def ensure_levels(db: DBController, names: set[str]) -> dict[str, str]:
    """每个名字 upsert Level，返回 名字 → level_id 映射。"""
    mapping: dict[str, str] = {}
    for name in sorted(names):
        existing = db.levels.get_by_name(name)
        if existing is not None:
            mapping[name] = existing.id
            continue
        lv = Level(name=name, display_name=name)
        db.levels.insert(lv)
        mapping[name] = lv.id
    return mapping


def rewrite_mappools(db: DBController, mapping: dict[str, str]) -> tuple[int, int]:
    """把 mappools 的 raw 从名字改写为 id；返回 (改写图池数, 改写 pick 数)。"""
    # 幂等跳过集用关卡库全量 id（不只本次 mapping——二次跑 mapping 为空，
    # 已迁移的引用仍须被识别为 id 而跳过）
    level_ids = {lv.id for lv in db.levels.find()}
    pools_changed = 0
    picks_changed = 0
    for doc in db.mappools.find():
        changed = False
        for pick in doc.mappool.all_picks():
            raw = pick.collection.raw
            if isinstance(raw.get("levels"), list):
                items = raw["levels"]
                # 已是 id 形态则跳过（幂等）
                if all(str(x) in level_ids for x in items):
                    continue
                new_items = [mapping.get(str(x), str(x)) for x in items]
                if new_items != list(items):
                    pick.collection.raw["levels"] = new_items
                    changed = True
                    picks_changed += 1
            if isinstance(raw.get("level"), str):
                val = raw["level"]
                if val in level_ids:
                    continue
                if val in mapping:
                    pick.collection.raw["level"] = mapping[val]
                    changed = True
                    picks_changed += 1
        if changed:
            db.mappools.replace(doc)
            pools_changed += 1
    return pools_changed, picks_changed


def verify(db: DBController) -> int:
    """校验：所有 raw 引用均命中关卡库 id；返回未命中数。"""
    level_ids = {lv.id for lv in db.levels.find()}
    bad = 0
    for doc in db.mappools.find():
        for pick in doc.mappool.all_picks():
            for v in _raw_values(pick.collection.raw):
                if v not in level_ids:
                    bad += 1
                    print(f"  ! 未命中的引用: {doc.name}/{pick.code} -> {v}")
    return bad


def main() -> None:
    dry = "--dry-run" in sys.argv
    db = DBController(settings)
    try:
        names = collect_names(db)
        print(f"发现关卡名 {len(names)} 个：{sorted(names)}")
        if dry:
            print("[dry-run] 未做任何改动")
            return
        mapping = ensure_levels(db, names)
        print(f"关卡库就绪（共 {len(mapping)} 个，含已存在）")
        pools, picks = rewrite_mappools(db, mapping)
        print(f"改写图池 {pools} 个、pick {picks} 个 → raw 已用 level_id")
        bad = verify(db)
        ok = "校验通过：所有引用均指向关卡库"
        print(ok if bad == 0 else f"校验：{bad} 个引用未命中")
    finally:
        db.close()


if __name__ == "__main__":
    main()
