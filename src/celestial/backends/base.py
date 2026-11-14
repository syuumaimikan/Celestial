"""モデルバックエンドの共通インターフェース。

自作AIを組み込むときは Backend を継承し @register("名前") を付けるだけでよい。
celestial.toml の [backends.xxx] type = "名前" で切り替えられる。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

Message = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}
TIERS = ("small", "medium", "large")


class BackendError(RuntimeError):
    pass


def loop_cut(text: str, span: int = 80, times: int = 3) -> str | None:
    """末尾が同じ文の繰り返しになっていたら、繰り返しの手前までの文字列を返す(なっていなければ None)。

    小さいモデルは同じ文を上限まで書き続けることがあり、時間と電力を無駄にする。
    末尾の span 文字が直近で times 回以上現れたら繰り返しとみなし、1回分だけを残す。
    """
    if len(text) < span * times:
        return None
    tail = text[-span:]
    if tail.strip() == "" or text[-4000:].count(tail) < times:
        return None
    first = text.find(tail)
    second = text.find(tail, first + 1)
    if second <= 0:
        return None
    period = second - first
    start = first  # 繰り返しの始まりまで戻り、ちょうど1回分を残す
    while start > 0 and text[start - 1] == text[start - 1 + period]:
        start -= 1
    end = start + period
    rest = text[end:end + period]  # 文の途中で切れないように、次の文の区切りまでは残す
    stop = min((i for i in (rest.find(c) for c in "。\n.!?！？") if i >= 0), default=-1)
    return text[:end + stop + 1 if stop >= 0 else end].rstrip()


@dataclass
class ModelInfo:
    name: str
    backend: str
    size_bytes: int = 0
    tier_override: str | None = None
    can_manage: bool = True  # 管理AI(振り分け・検証)を任せられるか
    vision: bool = False     # 画像を入力として受け取れるか
    specialties: tuple[str, ...] = ()  # 得意な役割 ("code", "reason", "translate", "agent", "judge" など)
    profile: str = ""                  # 自作AIのプロファイル名 ("nano", "coder", "agent" など)

    @property
    def tier(self) -> str:
        if self.tier_override in TIERS:
            return self.tier_override
        gb = self.size_bytes / 1e9
        return "small" if gb < 2.5 else "medium" if gb < 7 else "large"


@dataclass
class Generation:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Backend(ABC):
    def __init__(self, name: str, **options):
        self.name = name
        self.options = options

    @abstractmethod
    def list_models(self) -> list[ModelInfo]:
        """このバックエンドで利用可能なモデル一覧。"""

    def embedding_models(self) -> list[str]:
        """埋め込み(文章 → ベクトル)に使えるモデルの一覧。対応しないバックエンドは空。"""
        return []

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        raise BackendError(f"{self.name} は埋め込みに対応していません")

    def release(self) -> None:
        """GPUメモリを空ける(学習の前などに呼ばれる)。既定では何もしない。"""

    def loaded_models(self) -> set[str] | None:
        """いまメモリに読み込まれているモデル名(読み込みの待ち時間がないもの)。分からなければ None。"""
        return None

    @abstractmethod
    def generate(
        self,
        model: str,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_schema: dict | None = None,
        on_token: Callable[[str], None] | None = None,
        images: list[bytes] | None = None,
        logprobs: list[float] | None = None,
        top_logprobs: list[dict[str, float]] | None = None,
    ) -> Generation:
        """チャット補完を1回行う。json_schema 指定時はそのスキーマに沿ったJSON文字列を返す。

        images は最後の発話に添える画像(PNG/JPEG のバイト列)。対応しないモデルでは BackendError。
        logprobs にリストを渡すと、出した各トークンの対数確率を、on_token を呼ぶ前に順に書き足す
        (回答の確からしさを測るため。対応しないバックエンドは何も書かない)。
        on_token が例外を投げたら、生成をそこで打ち切ってその例外をそのまま伝える。
        """


_REGISTRY: dict[str, type[Backend]] = {}


def register(type_name: str):
    def deco(cls: type[Backend]) -> type[Backend]:
        _REGISTRY[type_name] = cls
        return cls

    return deco


def create_backend(name: str, type: str | None = None, **options) -> Backend:
    type_name = type or name
    if type_name not in _REGISTRY:
        raise BackendError(f"未知のバックエンド種別: {type_name}(登録済み: {', '.join(sorted(_REGISTRY))})")
    return _REGISTRY[type_name](name, **options)
