"""账号管理控制器（管理员）：创建/查询/修改/删除账号。"""

from __future__ import annotations

from classy_fastapi import Routable, delete, get, patch, post
from fastapi import Depends, HTTPException, status
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from ..auth import hash_password, require_admin
from ..controllers import DBController
from ..datatypes import Account, AccountType
from .schemas import AccountCreate, AccountOut


class AccountUpdate(BaseModel):
    display_name: str | None = None
    password: str | None = None
    roles: list[AccountType] | None = None
    # speedrun.com 绑定；空串 = 解绑（传 None = 不改）
    speedrun_id: str | None = None


def _normalize_roles(roles: list[AccountType]) -> list[AccountType]:
    """去重保序；管理员默认兼裁判 + 导播。"""
    result = list(dict.fromkeys(roles))
    if AccountType.ADMIN in result:
        for r in (AccountType.REFEREE, AccountType.DIRECTOR):
            if r not in result:
                result.append(r)
    return result


class AccountController(Routable):
    def __init__(self, db: DBController) -> None:
        super().__init__(prefix="/admin/accounts", tags=["accounts"])
        self.db = db

    def _admin_count(self) -> int:
        """系统中 ADMIN 角色账号数（末位管理员保护用）。"""
        return sum(1 for a in self.db.accounts.find() if AccountType.ADMIN in a.roles)

    def _reference_conflicts(self, account_id: str) -> list[str]:
        """账号仍被业务数据引用的冲突清单（删除前完整性检查）。

        口径与 /me/matches 一致：未归档比赛（含 ENDED 未归档）算引用，
        已归档比赛视为收纳完毕不再阻挡；赛事成员池与对阵节点恒算引用
        （历史赛程数据完整性）。
        """
        refs: list[str] = []
        member_matches = self.db.matches.find_by_member(account_id)
        matches = [m for m in member_matches if m.archived_at is None]
        if matches:
            refs.append(f"{len(matches)} 场未归档比赛")
        tournaments = [
            t
            for t in self.db.tournaments.find()
            if account_id in (*t.participant_ids, *t.referee_ids, *t.director_ids)
        ]
        if tournaments:
            refs.append(f"{len(tournaments)} 个赛事")
        fixtures = [
            f
            for f in self.db.fixtures.find()
            if account_id in (f.player_a_id, f.player_b_id, f.winner_id)
        ]
        if fixtures:
            refs.append(f"{len(fixtures)} 个对阵节点")
        return refs

    @post(
        "",
        response_model=AccountOut,
        status_code=status.HTTP_201_CREATED,
        summary="创建账号",
        description="管理员预创建选手/裁判/导播/管理员账号并分发。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            409: {"description": "用户名已存在"},
        },
    )
    def create(
        self,
        body: AccountCreate,
        _: Account = Depends(require_admin),
    ) -> AccountOut:
        account = Account(
            username=body.username,
            password_hash=hash_password(body.password),
            roles=_normalize_roles(body.roles),
            display_name=body.display_name,
            speedrun_id=body.speedrun_id.strip() or None if body.speedrun_id else None,
        )
        try:
            self.db.accounts.insert(account)
        except DuplicateKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在") from exc
        return AccountOut.from_account(account)

    @get(
        "",
        response_model=list[AccountOut],
        summary="账号列表",
        description="返回全部账号（不含口令哈希）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
        },
    )
    def list(
        self,
        _: Account = Depends(require_admin),
    ) -> list[AccountOut]:
        return [AccountOut.from_account(a) for a in self.db.accounts.find()]

    @get(
        "/{account_id}",
        response_model=AccountOut,
        summary="查询单个账号",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "账号不存在"},
        },
    )
    def get_one(
        self,
        account_id: str,
        _: Account = Depends(require_admin),
    ) -> AccountOut:
        account = self.db.accounts.get(account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
        return AccountOut.from_account(account)

    @patch(
        "/{account_id}",
        response_model=AccountOut,
        summary="修改账号",
        description="按字段局部更新（display_name / password / roles / speedrun_id，"
        "传哪个改哪个）。保护：不能移除系统中最后一个管理员的角色"
        "（防先降级再删除绕过账号删除守卫）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限 / 移除最后一个管理员角色"},
            404: {"description": "账号不存在"},
            409: {"description": "用户名冲突"},
        },
    )
    def update(
        self,
        account_id: str,
        body: AccountUpdate,
        _: Account = Depends(require_admin),
    ) -> AccountOut:
        account = self.db.accounts.get(account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
        if body.display_name is not None:
            account.display_name = body.display_name
        if body.password is not None:
            account.password_hash = hash_password(body.password)
        if body.roles is not None:
            new_roles = _normalize_roles(body.roles)
            if (
                AccountType.ADMIN in account.roles
                and AccountType.ADMIN not in new_roles
                and self._admin_count() <= 1
            ):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "不能移除最后一个管理员角色"
                )
            account.roles = new_roles
        if body.speedrun_id is not None:
            account.speedrun_id = body.speedrun_id.strip() or None
        try:
            self.db.accounts.replace(account)
        except DuplicateKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在") from exc
        return AccountOut.from_account(account)

    @delete(
        "/{account_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="删除账号",
        description="按序校验（命中即拒）：不能删当前登录账号；不能删 ADMIN 角色"
        "账号（管理员只能改角色/改密码）；账号仍被引用则 409（未归档比赛 / "
        "赛事成员池 / 对阵节点，msg 列出冲突类型，先处理引用再删）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限 / 删自己 / 删管理员账号"},
            404: {"description": "账号不存在"},
            409: {"description": "账号仍被比赛/赛事/对阵引用"},
        },
    )
    def remove(
        self,
        account_id: str,
        admin: Account = Depends(require_admin),
    ) -> None:
        account = self.db.accounts.get(account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
        if account.id == admin.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "不能删除当前登录账号")
        if AccountType.ADMIN in account.roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "不能删除管理员账号")
        refs = self._reference_conflicts(account.id)
        if refs:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"账号仍被引用：{' / '.join(refs)}"
            )
        self.db.accounts.delete(account_id)
