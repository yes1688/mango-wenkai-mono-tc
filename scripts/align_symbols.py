#!/usr/bin/env python3
"""
把 contained 符號 glyph 的視覺佔格「對齊 Iosevka Term 基準」：uniform scale
（不變形）縮進 advance 框 + 水平置中 + 垂直對齊 Iosevka 中心。消除 normalize
砍半 advance 後符號爆框/落錯半格/垂直跑位。

問題背景：
  normalize_advances 依 EAW 把符號 advance 砍成半形（對齊 terminal 格子），但
  glyph 視覺大小/位置沒跟著調。LXGW 是閱讀楷體、整套符號畫在全形(1000)字身，
  advance 砍半後 → bbox 寬達 advance 的 1.7~2.0 倍，且整個 glyph 落在「右半格」
  （水平中心 hc≈1.0），壓到後一個字。Mango(Maple Mono) 多數已置中，但幾何實心
  形(■●◆)與大圓(◯)仍爆框偏右。

基準與目標（per-glyph，量自 Iosevka Term Regular，見 iosevka_targets.py）：
    target_wr = bbox寬 ÷ advance（水平覆蓋；對齊**上界**，不放大過小符號）
    target_hc = bbox水平中心 ÷ advance（0.5=置中；箭頭/角落類有設計偏移，逐字抄）
    target_vc = bbox垂直中心 ÷ UPM（垂直位置）
  **只量 Iosevka metrics、不複製其 glyph**（OFL 量測無授權問題）。

修法（uniform scale + 平移，絕不 non-uniform 拉伸 → 保留各字型字形）：
  - 僅當「水平爆框」(cur_wr > OVERFLOW) 才縮：scale = target_wr / cur_wr，縮到
    Iosevka 的水平覆蓋。未爆框者 scale=1（不放大過小符號，符合 · • 語意）。
  - 水平置中到 target_hc × advance。
  - 垂直對齊 target_vc × UPM。
  - advance 不動（保住 terminal 等寬）。

範圍 = contained 符號（標點/箭頭/技術/幾何）。**不含會 tiling 的 box/block**
  （U+2500-257F / U+2580-259F）——那是結構符號，uniform scale 救不了楷體全形
  字身（寬高雙錯，數學證明見 task da073d67），LXGW 走移除 cmap → fallback Sarasa；
  Mango box 原生已對齊不動。⌘(U+2318) 由 fit 既有階段處理。§5 大括號/積分延伸段
  排除。Sarasa 不套（fallback 字型）。target 表已在 iosevka_targets.py 排除以上。

Pipeline 順序（排在 normalize 之後，同 fit 區段）：
  strip → normalize → complete → fit(⌘) → align_symbols → inject_box_glyphs → verify

用法：
  python3 align_symbols.py <font.ttf> [font2.ttf ...]        # 對 TARGETS 對齊
  python3 align_symbols.py --check <font.ttf> [...]          # 只檢查不修改
"""

import os
import sys

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.recordingPen import DecomposingRecordingPen

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
# DRY：outline 替換 + lsb 更新邏輯與 fit 共用同一份。
from fit_glyph_to_advance import apply_outline_transform
# Iosevka per-glyph 佔格 target（自動產生，見 gen_iosevka_targets.py）。
from iosevka_targets import TARGETS

# golden / report 共用容差（DRY；verify_font_metrics 與 report 引用）。
WR_TOL = 0.05   # wr 上界容差：寬度 > target_wr + WR_TOL 才縮（下界不限 → 不放大過小符號）
HC_TOL = 0.03   # 水平中心容差
VC_TOL = 0.04   # 垂直中心容差


def compute_align_transform(
    xmin, ymin, xmax, ymax, advance, upm, target_wr, target_hc, target_vc
):
    """純函式：算把 glyph 對齊 Iosevka target 所需的 (scale, dx, dy)。

    - scale：當寬度覆蓋超過 Iosevka（cur_wr > target_wr + WR_TOL）才 = target_wr/
             cur_wr（縮到 Iosevka 覆蓋），否則 1.0。**只縮不放大**（過小符號維持，
             符合 · • 語意）；cur_wr > target_wr 時 scale 必 < 1，無需 clamp。
    - dx：水平置中到 target_hc × advance。
    - dy：垂直對齊 target_vc × upm（縮放後再平移到目標中心）。

    transform 語意：new_pt = (x*scale + dx, y*scale + dy)。
    """
    w = xmax - xmin
    if w <= 0 or advance <= 0:
        return (1.0, 0.0, 0.0)

    cur_wr = w / advance
    if cur_wr > target_wr + WR_TOL:
        scale = (target_wr * advance) / w   # = target_wr / cur_wr < 1
    else:
        scale = 1.0

    hcenter = (xmin + xmax) / 2.0
    vcenter = (ymin + ymax) / 2.0
    dx = target_hc * advance - scale * hcenter   # 水平置中到 target_hc
    dy = target_vc * upm - scale * vcenter        # 垂直對齊 target_vc
    return (scale, dx, dy)


def _glyph_bounds(font, glyph_name):
    bp = BoundsPen(font.getGlyphSet())
    font.getGlyphSet()[glyph_name].draw(bp)
    return bp.bounds


def _is_noop(scale, dx, dy):
    """scale≈1 且平移可忽略 → 不需改 outline（避免無謂改寫、保持冪等）。"""
    return scale == 1.0 and abs(dx) < 0.5 and abs(dy) < 0.5


def align_glyph(font, glyph_name, advance, upm, target_wr, target_hc, target_vc):
    """把 glyph_name 對齊 target（縮放 + 置中），原地替換 outline。

    回傳 (scale, dx, dy)；no-op 時回 (1.0, 0.0, 0.0) 且不改字型。
    """
    bounds = _glyph_bounds(font, glyph_name)
    if bounds is None:
        return (1.0, 0.0, 0.0)  # 空 glyph，不動

    xmin, ymin, xmax, ymax = bounds
    scale, dx, dy = compute_align_transform(
        xmin, ymin, xmax, ymax, advance, upm, target_wr, target_hc, target_vc
    )
    if _is_noop(scale, dx, dy):
        return (1.0, 0.0, 0.0)

    apply_outline_transform(font, glyph_name, scale, dx, dy)
    return (scale, dx, dy)


def align_codepoint(font, cp, targets=None):
    """對單一 codepoint 做對齊。回傳 True 表示有修改。"""
    if 0x00 <= cp <= 0x7F:
        raise ValueError(f"拒絕修改 ASCII 區 U+{cp:04X}")

    targets = targets if targets is not None else TARGETS
    target = targets.get(cp)
    if target is None:
        return False
    target_wr, target_hc, target_vc = target

    cmap = font.getBestCmap() or {}
    gn = cmap.get(cp)
    if gn is None:
        return False

    advance = font["hmtx"].metrics[gn][0]
    upm = font["head"].unitsPerEm
    bounds = _glyph_bounds(font, gn)
    if bounds is None:
        return False

    scale, dx, dy = align_glyph(
        font, gn, advance, upm, target_wr, target_hc, target_vc
    )
    return not _is_noop(scale, dx, dy)


def _flatten_target_composites(font, targets):
    """把 targets 內的 composite glyph 先 decompose 成簡單輪廓（讀原始、再寫回）。

    原因：部分符號（如 — emdash、‾ overline）是 composite，引用共用 base glyph。
    若該 base 被某 cp 先行縮放/平移，所有引用它的 composite 會跟著跑掉（交叉污染）。
    先把 composite **真正解構**成獨立輪廓（DecomposingRecordingPen 解析 component →
    contour），切斷引用，使每個 glyph 後續可獨立 transform。先全讀進 pens 再寫回，
    避免 read-after-write。
    """
    glyf = font["glyf"]
    cmap = font.getBestCmap() or {}
    glyph_set = font.getGlyphSet()
    pens = {}
    for cp in targets:
        gn = cmap.get(cp)
        if gn is None or gn in pens or gn not in glyf:
            continue
        if glyf[gn].isComposite():
            rpen = DecomposingRecordingPen(glyph_set)  # 解析 component 成 contour
            glyph_set[gn].draw(rpen)
            tpen = TTGlyphPen(None)
            rpen.replay(tpen)
            pens[gn] = tpen.glyph()
    for gn, g in pens.items():
        g.recalcBounds(glyf)
        glyf[gn] = g
    return len(pens)


def align_file(path, targets=None):
    """開啟 TTF、對 targets 內所有 codepoint 對齊、原地覆寫。"""
    targets = targets if targets is not None else TARGETS
    print(f"處理：{path}")
    font = TTFont(path)
    if "glyf" not in font:
        print("  非 glyf 字型，跳過（本腳本只處理 TrueType outline）")
        font.close()
        return 0

    flat = _flatten_target_composites(font, targets)
    if flat:
        print(f"  flatten {flat} 個 composite glyph")

    n = 0
    for cp in sorted(targets):
        if align_codepoint(font, cp, targets):
            n += 1

    if n:
        font.save(path)
        print(f"  對齊 {n} 個 glyph，已儲存")
    else:
        print("  無需修改，跳過")
    font.close()
    return n


def check_file(path, targets=None):
    """只檢查不修改：統計達標率（wr 上界 / hc / vc 對 target 誤差）。"""
    targets = targets if targets is not None else TARGETS
    print(f"檢查：{path}")
    font = TTFont(path)
    cmap = font.getBestCmap() or {}
    upm = font["head"].unitsPerEm
    have = ok = 0
    for cp in sorted(targets):
        target_wr, target_hc, target_vc = targets[cp]
        gn = cmap.get(cp)
        if gn is None:
            continue
        advance = font["hmtx"].metrics[gn][0]
        bounds = _glyph_bounds(font, gn)
        if bounds is None or advance <= 0:
            continue
        have += 1
        wr = (bounds[2] - bounds[0]) / advance
        hc = (bounds[0] + bounds[2]) / 2 / advance
        vc = (bounds[1] + bounds[3]) / 2 / upm
        good = (
            wr <= target_wr + WR_TOL
            and abs(hc - target_hc) <= HC_TOL
            and abs(vc - target_vc) <= VC_TOL
        )
        if good:
            ok += 1
        else:
            print(
                f"  U+{cp:04X}（{gn}）wr={wr:.2f}(≤{target_wr + WR_TOL:.2f}?) "
                f"hc={hc:.2f}(目標{target_hc:.2f}) vc={vc:.2f}(目標{target_vc:.2f})"
            )
    print(f"  達標 {ok}/{have}")
    font.close()
    return ok, have


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
            align_file(path)


if __name__ == "__main__":
    main()
