"""准备前端联调数据并打印令牌/地址。

前置：Mongo 与后端服务（默认 http://localhost:8000）已启动。可重复运行——
已存在的账号会被复用，比赛按名字复用。

用法::

    uv run python scripts/seed_demo.py
"""

from __future__ import annotations

import sys

import httpx

from twilightcupbackend.auth import hash_password
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import Account, AccountType

BASE = "http://localhost:8000"
SESSION_NAME = "联调试玩"

# (username, password, display, type)
ACCOUNTS = [
    ("admin", "admin", "管理员", AccountType.ADMIN),
    ("playerA", "playerA", "选手A", AccountType.PLAYER),
    ("playerB", "playerB", "选手B", AccountType.PLAYER),
    ("referee", "referee", "裁判", AccountType.REFEREE),
    ("director", "director", "导播", AccountType.DIRECTOR),
]

SEED_LEVELS = ["L1", "L2", "S1"]  # 联调关卡（先入库，图池按 id 引用）


def seed_levels() -> dict[str, str]:
    """幂等入库关卡，返回 名字 → level_id。"""
    from twilightcupbackend.datatypes import Level

    db = DBController(settings)
    try:
        mapping: dict[str, str] = {}
        for name in SEED_LEVELS:
            lv = db.levels.get_by_name(name)
            if lv is None:
                lv = Level(name=name, display_name=name)
                db.levels.insert(lv)
            mapping[name] = lv.id
        return mapping
    finally:
        db.close()


def mappool_body(level_ids: dict[str, str]) -> dict:
    return {
        "categories": [
            {
                "name": "ML",
                "picks": [
                    {
                        "code": "ML1",
                        "name": "多关测试",
                        "type": 1,
                        "collection": {
                            "raw": {"levels": [level_ids["L1"], level_ids["L2"]]}
                        },
                        "category": "ML",
                    }
                ],
            },
            {
                "name": "SL",
                "picks": [
                    {
                        "code": "SL1",
                        "name": "单关测试",
                        "type": 2,
                        "retry_count": 2,
                        "collection": {"raw": {"level": level_ids["S1"]}},
                        "category": "SL",
                    }
                ],
            },
        ]
    }


def ensure_admin() -> None:
    db = DBController(settings)
    if db.accounts.get_by_username("admin") is None:
        db.accounts.insert(
            Account(
                username="admin",
                password_hash=hash_password("admin"),
                roles=[
                    AccountType.ADMIN,
                    AccountType.DIRECTOR,
                    AccountType.REFEREE,
                ],
                display_name="管理员",
            )
        )
        print("已创建管理员 admin/admin")
    db.close()


def login(client: httpx.Client, username: str, password: str) -> str:
    r = client.post("/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


def create_account(
    client: httpx.Client,
    admin_token: str,
    uname: str,
    pwd: str,
    disp: str,
    role: int,
) -> None:
    r = client.post(
        "/admin/accounts",
        json={
            "username": uname,
            "password": pwd,
            "display_name": disp,
            "roles": [int(role)],
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    if r.status_code == 201:
        print(f"已创建账号 {uname}")
    elif r.status_code == 409:
        print(f"账号 {uname} 已存在，复用")
    else:
        r.raise_for_status()


def get_or_create_match(
    client: httpx.Client, admin_token: str, level_ids: dict[str, str]
) -> dict:
    r = client.get("/admin/matches", headers={"Authorization": f"Bearer {admin_token}"})
    r.raise_for_status()
    for s in r.json():
        if s["name"] == SESSION_NAME:
            print(f"比赛“{SESSION_NAME}”已存在，复用 {s['id']}")
            return s
    body = {
        "name": SESSION_NAME,
        "bo_format": 3,
        "scoring_method": 1,
        "start_countdown_delay": 5,
        "mappool": mappool_body(level_ids),
        "player_a": "playerA",
        "player_b": "playerB",
        "referee": "referee",
        "director": "director",
    }
    r = client.post(
        "/admin/matches", json=body, headers={"Authorization": f"Bearer {admin_token}"}
    )
    r.raise_for_status()
    print(f"已创建比赛“{SESSION_NAME}”")
    return r.json()


def main() -> None:
    try:
        httpx.get(f"{BASE}/health", timeout=3).raise_for_status()
    except Exception as exc:
        sys.exit(f"后端服务未就绪（{BASE}/health）：{exc}")

    ensure_admin()
    level_ids = seed_levels()
    print(f"关卡库就绪：{len(level_ids)} 个联调关卡")
    client = httpx.Client(base_url=BASE, timeout=10)
    admin_token = login(client, "admin", "admin")
    for uname, pwd, disp, role in ACCOUNTS:
        if uname != "admin":
            create_account(client, admin_token, uname, pwd, disp, int(role))
    session = get_or_create_match(client, admin_token, level_ids)

    tokens = {uname: login(client, uname, pwd) for uname, pwd, *_ in ACCOUNTS}

    print("\n" + "=" * 60)
    print("前端联调信息")
    print("=" * 60)
    print(f"Swagger UI : {BASE}/docs")
    print(f"OpenAPI    : {BASE}/openapi.json")
    print(f"match_id : {session['id']}")
    print(f"赛制 BO{session['bo_format']}，先到 {session['win_threshold']} 分胜")
    print()
    seat_of = {
        "playerA": "PLAYER_A",
        "playerB": "PLAYER_B",
        "referee": "REFEREE",
        "director": "DIRECTOR",
    }
    for uname in ("playerA", "playerB", "referee", "director"):
        tok = tokens[uname]
        print(f"[{uname}] seat={seat_of[uname]}")
        print(f"  token : {tok}")
        print(f"  ws    : ws://localhost:8000/ws/{tok}")
    print()
    print("REST 调用带请求头：Authorization: Bearer <token>")
    print("裁判先 referee_select_pick → 选手 !ready → 自动倒计时 → 回合开始")
    print("=" * 60)
    client.close()


if __name__ == "__main__":
    main()
