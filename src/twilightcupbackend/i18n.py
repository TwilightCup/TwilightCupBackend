"""系统消息本地化：从语言文件加载键值对并渲染。

语言文件放在 ``settings.locales_dir`` 目录下，文件名（去 ``.txt`` 后缀）
即语言 id（如 ``en.txt`` / ``zh.txt``），格式见 docs/locales.md。
启动时在 main.py lifespan 中调用 :meth:`LocaleCatalog.load_dir` 一次性加载，
不做热加载。

回退链：当前语言 → 默认语言 → 键名本身；缺参数时保留 ``{name}`` 字面量，
保证渲染永不抛异常。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .datatypes import Seat

logger = logging.getLogger(__name__)


class _SafeDict(dict[str, object]):
    """``str.format_map`` 用的字典：缺参数时保留 ``{key}`` 字面量。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class LocaleCatalog:
    """语言目录：语言 id → (键 → 模板) 的内存映射。"""

    def __init__(self) -> None:
        self._langs: dict[str, dict[str, str]] = {}
        self._default: str = "en"

    def load_dir(self, path: Path, default: str) -> None:
        """扫描目录下所有 ``*.txt``（文件名去后缀 = 语言 id）并加载。"""
        self._langs = {}
        self._default = default
        if not path.is_dir():
            logger.warning("语言目录不存在：%s", path)
            return
        for file in sorted(path.glob("*.txt")):
            locale = file.stem
            self._langs[locale] = self._parse_file(file)
            logger.info(
                "已加载语言 %s（%d 条消息）。", locale, len(self._langs[locale])
            )

    @staticmethod
    def _parse_file(file: Path) -> dict[str, str]:
        entries: dict[str, str] = {}
        for lineno, raw in enumerate(
            file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if "=" not in line:
                logger.warning("%s:%d 缺少 =，已跳过：%s", file.name, lineno, line)
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not key:
                logger.warning("%s:%d 键为空，已跳过。", file.name, lineno)
                continue
            if key in entries:
                logger.warning(
                    "%s:%d 重复键 %s，后者覆盖前者。", file.name, lineno, key
                )
            entries[key] = value
        return entries

    def languages(self) -> list[str]:
        """已加载的语言 id 列表（按文件名排序）。"""
        return sorted(self._langs)

    def has(self, locale: str) -> bool:
        return locale in self._langs

    def translate(self, locale: str, key: str, **kw: object) -> str:
        """渲染消息：locale → 默认语言 → 键名本身。"""
        template = self._langs.get(locale, {}).get(key)
        if template is None:
            template = self._langs.get(self._default, {}).get(key)
        if template is None:
            logger.warning("消息键缺失（%s / %s）：%s", locale, self._default, key)
            return key
        return template.format_map(_SafeDict(kw))


#: 模块级单例；main.py 启动时 load_dir。
catalog = LocaleCatalog()


def t(locale: str, key: str, **kw: object) -> str:
    """渲染指定语言的系统消息（见 :data:`catalog`）。"""
    return catalog.translate(locale, key, **kw)


def seat_name(seat: Seat, locale: str) -> str:
    """按语言返回座位显示名（复用 Seat.name_zh / name_en）。"""
    return seat.name_zh if locale == "zh" else seat.name_en
