"""漫画源适配器集合（再漫画、拷贝漫画、Komiic）。"""

from .base import BaseMangaSource
from .copymanga import CopymangaSource
from .komiic import KomiicSource
from .zaimanhua import ZaimanhuaSource

__all__ = [
    "BaseMangaSource",
    "CopymangaSource",
    "KomiicSource",
    "ZaimanhuaSource",
]
