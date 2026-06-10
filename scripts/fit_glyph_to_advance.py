#!/usr/bin/env python3
"""
把「視覺 bbox 寬度 > advance」的 glyph 等比例縮進 advance 框內，消除爆框疊字。

問題背景：
  ⌘（U+2318 Place of Interest Sign）的 glyph 視覺 bbox 寬度大於它的 advance，
  在等寬 terminal / UI 渲染時，glyph 右半部會蓋到後一個字（pill 的 ⌘K、dropdown
  的 ⌘N）。實測（UPM 1000）：
    LXGW:   ⌘ bbox 寬 817，advance 500 → 爆框 317（最嚴重）
    Mango:  ⌘ bbox 寬 712，advance 600 → 爆框 112
    Sarasa: ⌘ bbox 寬 642，advance 500 → 爆框 142

修法（uniform scale + 置中）：
  對指定字元清單，若 glyph 視覺 bbox 寬度 > advance，對 outline 做**等比例縮放**
  （x/y 同比例，避免變形），縮到 bbox 寬度 ≤ advance × MAX_RATIO（留邊距），並
  **水平置中**在 advance 框內、**垂直保持原 bbox 中心**（不破壞與其他字的對齊）。
  advance 本身不改（保住 terminal 等寬）。

可單獨執行，也可被 build pipeline import 使用。

Pipeline 順序：
  fit_glyph 依賴 advance 已定，必須在 normalize_advances.py **之後**跑：
    strip_ligatures → normalize_advances → complete_glyph_pairs → fit_glyph_to_advance

用法：
  python3 fit_glyph_to_advance.py <font.ttf> [font2.ttf ...]   # 對預設清單縮放
  python3 fit_glyph_to_advance.py --check <font.ttf> [...]      # 只檢查不修改

注意：
  - 只操作 glyf（TrueType outline）字型；CFF 需另行處理。
  - composite glyph 在 draw() 時自動 decompose 成 contour 再縮放。
  - ASCII 區（U+0000-U+007F）不碰。
  - 三個 embed 字型（MangoMono / LXGW / Sarasa）都要跑，改完需重 build binary 生效。
"""

import os
import sys

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.transformPen import TransformPen

# ── 要縮的字元清單（可擴充）────────────────────────────────────────────────
# 目前只放 ⌘（唯一確認在 UI 爆框的）。其餘字元等實測爆框再加。
TARGET_CODEPOINTS = [
    0x2318,  # ⌘ Place of Interest Sign（pill ⌘K / dropdown ⌘N）
]

# 縮放目標：bbox 寬度 ≤ advance × MAX_RATIO（留 8% 邊距，左右各 ~4%）。
# 0.92：仍 < 1.0 不爆框，但符號比 0.85 大一點（uniform scale 連高度一起放大）。
MAX_RATIO = 0.92


def compute_fit_transform(xmin, ymin, xmax, ymax, advance, max_ratio=MAX_RATIO):
    """純函式：算把 bbox 縮進 advance 框所需的 (scale, dx, dy)。

    - scale：等比例縮放係數，使縮後 bbox 寬度 = advance × max_ratio。
             已在框內（bbox 寬 ≤ advance）則回 scale=1、dx/dy=0（不動）。
    - dx：水平置中於 [0, advance] 框內。
    - dy：垂直保持原 bbox 中心不變（純縮放會把中心往原點拉，dy 補回）。

    transform 語意：new_pt = (x*scale + dx, y*scale + dy)。
    """
    w = xmax - xmin
    if w <= 0:
        return (1.0, 0.0, 0.0)
    if w <= advance:
        return (1.0, 0.0, 0.0)  # 已在框內，不動

    scale = (advance * max_ratio) / w
    # 水平置中：縮後寬 w*scale，左邊距 = (advance - w*scale)/2
    new_w = w * scale
    dx = (advance - new_w) / 2.0 - xmin * scale
    # 垂直保持原中心：原中心 vc，縮後變 vc*scale，補回差值
    vc = (ymin + ymax) / 2.0
    dy = vc - vc * scale
    return (scale, dx, dy)


def _glyph_bounds(font, glyph_name):
    """回傳 glyph 的視覺 bbox (xmin, ymin, xmax, ymax)，空 glyph 回 None。"""
    bp = BoundsPen(font.getGlyphSet())
    font.getGlyphSet()[glyph_name].draw(bp)
    return bp.bounds


def apply_outline_transform(font, glyph_name, scale, dx, dy):
    """對 glyph outline 原地套 (scale, dx, dy) 並更新 lsb=新 xMin（advance 不變）。

    語意：new_pt = (x*scale + dx, y*scale + dy)。回傳新 xMin。

    關鍵：替換 outline 後必須把 hmtx 的 lsb（left side bearing）更新成新 xMin。
    否則 fontTools glyphSet 在 draw() 時會依「舊 lsb − 新 xMin」把 glyph 水平
    平移，使算好的置中失效（glyph 會偏移、貼到後字）。advance 不動，等寬不受影響。

    composite glyph 在 draw() 時被 decompose 成 contour，再過 transform。
    供 fit_glyph 與 align_symbols 共用（DRY：outline 替換邏輯只此一份）。
    """
    glyph_set = font.getGlyphSet()
    tt_pen = TTGlyphPen(glyph_set)
    transform_pen = TransformPen(tt_pen, (scale, 0, 0, scale, dx, dy))
    glyph_set[glyph_name].draw(transform_pen)
    new_glyph = tt_pen.glyph()

    glyf_table = font["glyf"]
    new_glyph.recalcBounds(glyf_table)  # 算出縮放後實際 xMin/yMin/xMax/yMax
    glyf_table[glyph_name] = new_glyph

    old_advance, _old_lsb = font["hmtx"].metrics[glyph_name]
    font["hmtx"].metrics[glyph_name] = (old_advance, new_glyph.xMin)
    return new_glyph.xMin


def fit_glyph(font, glyph_name, advance, max_ratio=MAX_RATIO):
    """把 glyph_name 縮進 advance 框（若爆框），原地替換 glyf 的 outline。

    回傳 (scale, dx, dy)；scale==1.0 代表沒爆框、未修改。
    """
    bounds = _glyph_bounds(font, glyph_name)
    if bounds is None:
        return (1.0, 0.0, 0.0)  # 空 glyph，不動

    xmin, ymin, xmax, ymax = bounds
    scale, dx, dy = compute_fit_transform(xmin, ymin, xmax, ymax, advance, max_ratio)
    if scale == 1.0:
        return (1.0, 0.0, 0.0)

    apply_outline_transform(font, glyph_name, scale, dx, dy)
    return (scale, dx, dy)


def fit_codepoint(font, cp, max_ratio=MAX_RATIO):
    """對單一 codepoint 做 fit。回傳 True 表示有修改，False 表示跳過。"""
    if 0x00 <= cp <= 0x7F:
        raise ValueError(f"拒絕修改 ASCII 區 U+{cp:04X}")

    cmap = font.getBestCmap() or {}
    gn = cmap.get(cp)
    if gn is None:
        print(f"  U+{cp:04X} 不存在，跳過")
        return False

    advance = font["hmtx"].metrics[gn][0]
    bounds = _glyph_bounds(font, gn)
    if bounds is None:
        print(f"  U+{cp:04X}（{gn}）空 glyph，跳過")
        return False
    w_before = bounds[2] - bounds[0]

    scale, dx, dy = fit_glyph(font, gn, advance, max_ratio)
    if scale == 1.0:
        print(f"  U+{cp:04X}（{gn}）bbox寬={w_before:.0f} ≤ advance={advance}，未爆框，跳過")
        return False

    after = _glyph_bounds(font, gn)
    w_after = after[2] - after[0]
    print(
        f"  U+{cp:04X}（{gn}）縮放 scale={scale:.4f}："
        f"bbox寬 {w_before:.0f}→{w_after:.0f}（advance={advance}, 上限={advance * max_ratio:.0f}）"
    )
    return True


def fit_file(path, codepoints=None):
    """開啟 TTF、對 codepoints 清單做 fit、原地覆寫。"""
    codepoints = codepoints if codepoints is not None else TARGET_CODEPOINTS
    print(f"處理：{path}")
    font = TTFont(path)
    if "glyf" not in font:
        print("  非 glyf 字型，跳過（本腳本只處理 TrueType outline）")
        font.close()
        return 0

    n = 0
    for cp in codepoints:
        if fit_codepoint(font, cp):
            n += 1

    if n:
        font.save(path)
        print(f"  縮放 {n} 個 glyph，已儲存")
    else:
        print("  無需修改，跳過")
    font.close()
    return n


def check_file(path, codepoints=None):
    """只檢查不修改：列印每個目標字元的 bbox寬 / advance / 是否爆框。"""
    codepoints = codepoints if codepoints is not None else TARGET_CODEPOINTS
    print(f"檢查：{path}")
    font = TTFont(path)
    cmap = font.getBestCmap() or {}
    overflow = 0
    for cp in codepoints:
        gn = cmap.get(cp)
        if gn is None:
            print(f"  U+{cp:04X} 不存在")
            continue
        advance = font["hmtx"].metrics[gn][0]
        bounds = _glyph_bounds(font, gn)
        if bounds is None:
            print(f"  U+{cp:04X}（{gn}）空 glyph")
            continue
        w = bounds[2] - bounds[0]
        limit = advance * MAX_RATIO
        status = "爆框" if w > advance else ("超上限" if w > limit else "OK")
        if w > advance:
            overflow += 1
        print(f"  U+{cp:04X}（{gn}）bbox寬={w:.0f} advance={advance} 上限={limit:.0f} → {status}")
    font.close()
    return overflow


def main():
    args = sys.argv[1:]
    if not args:
        print(f"用法：{sys.argv[0]} [--check] <font.ttf> [font2.ttf ...]")
        sys.exit(1)

    check_only = False
    if args[0] == "--check":
        check_only = True
        args = args[1:]
        if not args:
            print("--check 需指定至少一個字型檔")
            sys.exit(1)

    for path in args:
        if not os.path.exists(path):
            print(f"檔案不存在：{path}", file=sys.stderr)
            sys.exit(1)
        if check_only:
            check_file(path)
        else:
            fit_file(path)


if __name__ == "__main__":
    main()
