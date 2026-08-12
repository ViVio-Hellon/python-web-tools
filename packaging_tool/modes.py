"""モード ── この画面は誰のためのものか

    現場 … ロットを引いて資材を選び、発注を出す
    資材 … 受け取った発注を確認する。取り消しもできる

**モードの唯一の出どころ**。画面・権限・ポート・ナビゲーションは
すべてここを読む。モードを増やすときはここに1行足す。

【以前は role と呼んでいました】
`field` / `warehouse` の2つで、**別プロセス・別ポート**で動かし、
資材側のエンドポイントは現場のサーバに登録しないことで権限を保っていました。
分けていたのは「どのショートカットを押したか」であって、誰かではありません。
`access_control` が身元で分けるようになったので、名前も実態に合わせて
`mode` にし、`warehouse` は `material` の旧名として受けるだけにしています
(現場の呼び名も「倉庫」から「資材」へ)。

【ポートはモードごとに分けたまま】
1つのプロセスで両方のモードを開けるようにはしていますが、ポートは
モードごとに別のままにしてあります。2つのモードを同時に開いて使う
場面があり、同じポートでは片方しか起動できないためです。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    detail: str
    # タブのアイコン。2つ開いていても取り違えないように色と文字で分ける
    color: str
    mark: str


FIELD = "field"
MATERIAL = "material"

ALL: tuple[Mode, ...] = (
    Mode(key=FIELD, label="現場",
         detail="ロットを引いて資材を選び、発注を出す",
         color="#1d4ed8", mark="現"),
    Mode(key=MATERIAL, label="資材",
         detail="受け取った発注を確認する。取り消しもできる",
         color="#b45309", mark="資"),
)

KEYS: tuple[str, ...] = tuple(m.key for m in ALL)

# 権限が無いときに渡すモード。いちばん狭いもの
DEFAULT = FIELD

# 旧名。ショートカット・設定ファイル・手順書がまだこちらで書かれている
LEGACY_NAMES = {"warehouse": MATERIAL}


def get(key: str) -> Mode:
    for mode in ALL:
        if mode.key == key:
            return mode
    raise ValueError(f"未知のモード: {key!r} (使えるのは {', '.join(KEYS)})")


def label(key: str) -> str:
    return get(key).label


def normalize(key: str) -> str:
    """旧名を受ける。知らない名前はそのまま返し、呼び手に判断させる。

    ここで既定へ倒してしまうと、打ち間違いが「現場モードで起動した」
    としか見えず、なぜそうなったのか分からなくなる。
    """
    text = (key or "").strip()
    return LEGACY_NAMES.get(text, text)

