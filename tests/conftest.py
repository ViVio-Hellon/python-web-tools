"""試験全体の約束。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _import_diag_to_tmp(tmp_path, monkeypatch):
    """取り込み診断(`import_diag`)を試験ごとの一時フォルダへ書く。

    書き先は利用者のログフォルダなので、試験が現場の記録に混ざらないようにする。
    """
    from packaging_tool import import_diag
    monkeypatch.setattr(import_diag, "path_for",
                        lambda day=None: tmp_path / "取り込み診断.log")
