"""共通データモデル

VBAクラスモジュールの移植:
- `BoardModel.cls`        -> BoardModel
- `PlacedBoardModel.cls`  -> PlacedBoardModel

VBAの `Private Type` + `Property Get/Let` によるカプセル化は、
Pythonでは `@property` によるバリデーション付きセッターとして再現する。
"""
from __future__ import annotations

from dataclasses import dataclass


class BoardCategoryError(ValueError):
    """boardCategory に '下用'/'上用'/'' 以外が指定された場合の例外。

    VBA側は `Err.Raise vbObjectError + 1, "BoardModel", ...` で独自エラーを
    送出していた挙動を踏襲する。
    """


class BoardModel:
    """ボード(板材)1枚分の情報。MaterialMasterForm で使用。

    VBA版の `Clone()` メソッドはPython版では `clone()` として提供する。
    """

    VALID_CATEGORIES = ("下用", "上用", "")

    def __init__(
        self,
        *,
        id: int = 0,
        width: int = 0,
        length: int = 0,
        instance_id: str = "",
        is_rotated: bool = False,
        count: int = 0,
        board_category: str = "",
        stock_low: bool = False,
    ) -> None:
        self.id = id
        self.width = width
        self.length = length
        self.instance_id = instance_id
        self.is_rotated = is_rotated
        self.count = count
        self.stock_low = stock_low  # 在庫薄(看板が出ている)なら True
        self.board_category = board_category  # setter でバリデーション

    @property
    def board_category(self) -> str:
        return self._board_category

    @board_category.setter
    def board_category(self, value: str) -> None:
        if value not in self.VALID_CATEGORIES:
            raise BoardCategoryError("BoardCategoryは'下用'または'上用'を指定してください")
        self._board_category = value

    def clone(self) -> "BoardModel":
        return BoardModel(
            id=self.id,
            width=self.width,
            length=self.length,
            instance_id=self.instance_id,
            is_rotated=self.is_rotated,
            count=self.count,
            board_category=self.board_category,
            stock_low=self.stock_low,
        )

    def __repr__(self) -> str:  # デバッグ用
        return (
            f"BoardModel(id={self.id}, width={self.width}, length={self.length}, "
            f"instance_id={self.instance_id!r}, is_rotated={self.is_rotated}, "
            f"count={self.count}, board_category={self.board_category!r}, "
            f"stock_low={self.stock_low})"
        )


@dataclass
class PlacedBoardModel:
    """レイアウト上に配置済みのボード1枚分の情報。MaterialMasterForm で使用。

    VBA版の boardCategory は(BoardModelと異なり)バリデーション無しの
    単純な Property Let だったため、Python版でも単純な属性として扱う。
    """

    id: int = 0
    width: int = 0
    length: int = 0
    instance_id: str = ""
    x: int = 0
    y: int = 0
    board_category: str = ""
    original_width: int = 0
    original_length: int = 0
    is_fill_board: bool = False
