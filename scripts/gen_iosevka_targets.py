#!/usr/bin/env python3
"""
從 Iosevka Term Regular 量測符號的佔格 target，產生 iosevka_targets.py 資料模組。

**只量 metrics、不複製 glyph**（OFL 量測無授權問題；產出純數字）。
Iosevka 不入 repo；要重生 target 需先下載對照版本：

  IosevkaTerm-Regular.ttf  (v34.6.1, OFL, be5invis/Iosevka
   PkgTTF-Unhinted-IosevkaTerm-34.6.1.zip 解出 Regular)

用法：
  python3 gen_iosevka_targets.py <IosevkaTerm-Regular.ttf>
  → 覆寫同目錄 iosevka_targets.py

範圍 = contained 符號（會 tiling 的 box/block 另案處理，不在此表）：
  Latin-1 符號 0080-00FF、標點 2000-206F、字母符號 2100-214F、數字形式 2150-218F、
  箭頭 2190-21FF、技術 2300-23FF、帶圈字母數字 2460-24FF、幾何 25A0-25FF、
  雜項符號 2600-26FF、Dingbats 2700-27BF、補充箭頭B 2900-297F、雜項符號箭頭 2B00-2BFF。
排除：
  - ASCII U+0000-007F（已知坑，不動）。
  - **所有字母**（unicodedata category L*）：字母坐 baseline，套「垂直置中對齊」會把它
    拉離基線 = regression。拉丁段重音字母、字母符號段的 Ω/K/Å/ℓ/ℵ 等數學字母全由此排除
    （task-72a4a59d，CTO 確認）。0100-024F 全字母 → 整段不入表，故 RANGES 收到 00FF 即可。
    例外 INCLUDE_LETTERLIKE：歸類為字母但實為符號者（ℹ U+2139 資訊符號）仍對齊。
  - ⌘ U+2318（由 fit 處理）、§5 大括號/積分延伸段 U+239B-23B1 / U+23A7-23AD。
"""

import os
import sys
import unicodedata

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen

# contained 範圍（box 2500-257F / block 2580-259F 另案，不收）。字母由下方 L 過濾排除。
RANGES = [
    (0x0080, 0x00FF),  # Latin-1 補充（©®°±§¶× 等真符號）
    (0x2000, 0x206F),  # 一般標點
    (0x2100, 0x214F),  # 字母符號（™ℹ№℃）
    (0x2150, 0x218F),  # 數字形式（½⅓Ⅻ）
    (0x2190, 0x21FF),  # 箭頭
    (0x2300, 0x23FF),  # 技術符號
    (0x2460, 0x24FF),  # 帶圈字母數字（①Ⓜ）
    (0x25A0, 0x25FF),  # 幾何圖形
    (0x2600, 0x26FF),  # 雜項符號（☀☁☂）
    (0x2700, 0x27BF),  # Dingbats（✓✗✂✉）
    (0x2900, 0x297F),  # 補充箭頭B
    (0x2B00, 0x2BFF),  # 雜項符號與箭頭
]
EXTRA = []                               # · U+00B7 已併入 Latin-1 範圍
# 字母例外：Unicode 歸類為 L* 但實為「符號」、多字型爆框疊字、需對齊者。
# ℹ U+2139（資訊符號）= 斜體 i 的字身被 Unicode 歸 Ll，但實際是 icon、不坐 baseline。
INCLUDE_LETTERLIKE = {0x2139}
EXCLUDE = {0x2318}                       # ⌘ 由 fit 處理
EXCLUDE |= set(range(0x239B, 0x23B2))    # §5 大括號/積分延伸段
EXCLUDE |= set(range(0x23A7, 0x23AE))

# box drawing + block elements：tiling 結構符號，量「觸界簽章」(L,R,T,B)。
BOX_RANGE = (0x2500, 0x259F)
EDGE_H = 0.05   # 水平觸界容差（× advance）
EDGE_V = 0.06   # 垂直觸界容差（× UPM）

HEADER = '''\
#!/usr/bin/env python3
"""
Iosevka Term Regular（v34.6.1, OFL）量測的符號佔格 target — **自動產生，勿手改**。
重生：python3 gen_iosevka_targets.py <IosevkaTerm-Regular.ttf>

TARGETS（contained）每筆 cp -> (wr, hc, vc)：
  wr = bbox寬 / advance              （水平覆蓋；對齊上界，不放大過小符號）
  hc = bbox水平中心 / advance        （0.5=置中；箭頭/角落類有設計偏移）
  vc = bbox垂直中心 / UPM            （垂直位置）
BOX_TILING（box+block）每筆 cp -> (L, R, T, B)：ink 是否觸 cell 左/右/上/下界，
  fallback 目標字型須對齊此簽章才能無縫接框。
只量 metrics、未複製任何 Iosevka glyph。
"""

# (wr, hc, vc)
TARGETS = {
'''

BOX_HEADER = '''
# box drawing + block 觸界簽章 (L, R, T, B)：ink 觸 cell 左/右/上/下界與否。
BOX_TILING = {
'''


def main():
    if len(sys.argv) != 2:
        print(f"用法：{sys.argv[0]} <IosevkaTerm-Regular.ttf>", file=sys.stderr)
        sys.exit(1)
    path = sys.argv[1]
    font = TTFont(path)
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    upm = font["head"].unitsPerEm

    name = font["name"].getDebugName(4) or ""
    ver = font["name"].getDebugName(5) or ""
    if "Iosevka" not in name or "Term" not in name:
        print(f"警告：字型名稱非 Iosevka Term（{name!r}），請確認對照版本", file=sys.stderr)

    def bounds(gn):
        bp = BoundsPen(font.getGlyphSet())
        font.getGlyphSet()[gn].draw(bp)
        return bp.bounds

    cps = []
    for lo, hi in RANGES:
        cps += range(lo, hi + 1)
    cps += EXTRA

    rows = []
    for cp in cps:
        if cp in EXCLUDE or 0x00 <= cp <= 0x7F:
            continue
        # 字母坐 baseline，垂直置中對齊會拉離基線 → 排除（task-72a4a59d）。
        # 例外：INCLUDE_LETTERLIKE 內的「歸類字母但實為符號」者（如 ℹ）仍要對齊。
        if (
            unicodedata.category(chr(cp)).startswith("L")
            and cp not in INCLUDE_LETTERLIKE
        ):
            continue
        gn = cmap.get(cp)
        if gn is None:
            continue
        adv = hmtx[gn][0]
        if not adv:
            continue
        b = bounds(gn)
        if b is None or (b[2] - b[0]) <= 0:
            continue
        wr = round((b[2] - b[0]) / adv, 3)
        hc = round((b[0] + b[2]) / 2 / adv, 3)
        vc = round((b[1] + b[3]) / 2 / upm, 3)
        rows.append((cp, wr, hc, vc))
    font.close()

    # box / block 觸界簽章
    asc = font["hhea"].ascent
    desc = font["hhea"].descent
    box_rows = []
    for cp in range(BOX_RANGE[0], BOX_RANGE[1] + 1):
        gn = cmap.get(cp)
        if gn is None:
            continue
        adv = hmtx[gn][0]
        if not adv:
            continue
        b = bounds(gn)
        if b is None:
            continue
        eh, ev = EDGE_H * adv, EDGE_V * upm
        sig = (b[0] <= eh, b[2] >= adv - eh, b[3] >= asc - ev, b[1] <= desc + ev)
        box_rows.append((cp, sig))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iosevka_targets.py")
    with open(out, "w", encoding="utf-8") as f:
        f.write(HEADER)
        f.write(f"    # 來源：{name}  {ver}\n")
        for cp, wr, hc, vc in rows:
            f.write(f"    0x{cp:04X}: ({wr}, {hc}, {vc}),  # {chr(cp)}\n")
        f.write("}\n")
        f.write(BOX_HEADER)
        for cp, sig in box_rows:
            f.write(f"    0x{cp:04X}: {sig},  # {chr(cp)}\n")
        f.write("}\n")
    print(
        f"已產生 {out}：{len(rows)} contained target + {len(box_rows)} box 簽章"
        f"（來源 {name} {ver}）"
    )


if __name__ == "__main__":
    main()
