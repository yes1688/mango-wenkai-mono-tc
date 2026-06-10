#!/usr/bin/env python3
"""
把 Sarasa 的 box drawing(U+2500-257F) + block(U+2580-259F) glyph 植入 LXGW，
讓 LXGW 自包含框線（不再靠 fallback）。取代 strip_lxgw_box.py。

為何植入而非用 LXGW 自家框線：LXGW 是楷體、box/block 畫在全形(1000)字身且原生高度
比 Iosevka 終端 box 矮，advance 又被 normalize 砍半 → 寬高雙錯，uniform scale 無法
同時滿足「縮進 1 格寬」與「垂直連 row 觸界」。Sarasa 的 box/block 已驗證 tiling 觸界
對齊 Iosevka（160/160），故直接把 Sarasa 的 outline + metrics 覆蓋上去，數學上等同。

為何植入後 LXGW tiling 簽章仍對齊：觸界判定（box_tiling）以各字型自身 hhea
ascent/descent 當 cell 上下界，容差 EDGE_V=0.06×UPM(=60)。Sarasa 垂直框 ink 跨
y∈[-285,965]（兩端 overshoot），足以吸收 LXGW(asc=928/desc=-241) 與
Sarasa(965/-215) 的 metric 差（≤37），故同一 outline 在 LXGW 仍判定觸界、簽章一致。
UPM 兩字型皆 1000 → ink/em 比例相同，實際渲染 tiling 行為與 Sarasa 等同。

做法（標準植入，與 complete_glyph_pairs.py 同路數）：
  1. 以 glyphSet.draw 取 Sarasa glyph outline（composite 在此被 decompose 成 contour）
  2. TTGlyphPen 重建為 simple glyph，植入 LXGW 的 glyf / hmtx /（有則）vmtx
  3. 重指 cmap：該 codepoint 的所有 Unicode subtable → 新 glyph（舊楷體框線變 orphan，無害）
  4. advance 鎖 = LXGW 半形 advance（M 的 advance，動態判定不寫死）；assert Sarasa 同值

用法：
  python3 inject_box_glyphs.py --source <Sarasa.ttf> <LXGW.ttf> [LXGW2.ttf ...]
"""

import os
import sys

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

# 植入範圍：box drawing + block elements（共 160 字，與 strip_lxgw_box 同範圍）。
INJECT_RANGES = [(0x2500, 0x257F), (0x2580, 0x259F)]
CP_M = 0x004D  # Latin 'M' — 半形 advance 基準（不寫死絕對值）


def _in_inject(cp):
    return any(lo <= cp <= hi for lo, hi in INJECT_RANGES)


def _copy_outline(src_font, src_gn):
    """從來源字型取 glyph outline，重建為 simple TTGlyph（decompose composite）。
    回傳 (glyph, lsb)；lsb = 重算 bbox 後的 xMin（渲染不平移）。"""
    glyph_set = src_font.getGlyphSet()
    rec = DecomposingRecordingPen(glyph_set)  # composite 在此 decompose 成 contour
    glyph_set[src_gn].draw(rec)
    pen = TTGlyphPen(glyph_set)
    for op, args in rec.value:
        getattr(pen, op)(*args)
    glyph = pen.glyph()
    glyph.recalcBounds(src_font["glyf"])
    lsb = glyph.xMin if glyph.numberOfContours > 0 else 0
    return glyph, lsb


def inject_box(target_font, src_font, target_label):
    """把 src_font 的 box/block glyph 植入 target_font。回傳植入字數。
    advance 鎖 = target 半形 advance；assert 來源同值。"""
    tcmap = target_font.getBestCmap() or {}
    m_gn = tcmap.get(CP_M)
    if m_gn is None:
        raise ValueError(f"{target_label} 找不到 'M'(U+004D)，無法判定半形 advance 基準")
    half_adv = target_font["hmtx"].metrics[m_gn][0]

    scmap = src_font.getBestCmap() or {}
    has_vmtx = "vmtx" in target_font
    src_has_vmtx = "vmtx" in src_font
    upm = target_font["head"].unitsPerEm

    injected = 0
    for cp in sorted(c for c in scmap if _in_inject(c)):
        src_gn = scmap[cp]
        src_adv = src_font["hmtx"].metrics[src_gn][0]
        if src_adv != half_adv:
            raise AssertionError(
                f"U+{cp:04X}：來源 advance={src_adv} ≠ 目標半形 advance={half_adv}（advance 鎖失敗）"
            )

        glyph, lsb = _copy_outline(src_font, src_gn)
        if glyph.numberOfContours <= 0:
            raise AssertionError(f"U+{cp:04X}：來源 glyph 為空，拒絕植入空框線")

        name = f"box{cp:04X}"
        if name in target_font["glyf"].glyphs:
            name = f"{name}.inj"

        target_font["glyf"][name] = glyph
        target_font["hmtx"].metrics[name] = (half_adv, lsb)
        if has_vmtx:
            # 來源 UPM 與目標一致（皆 1000）→ 直接沿用來源直排 metrics；無則退回 (UPM, 0)。
            target_font["vmtx"].metrics[name] = (
                src_font["vmtx"].metrics[src_gn] if src_has_vmtx else (upm, 0)
            )

        for table in target_font["cmap"].tables:
            if table.isUnicode():
                table.cmap[cp] = name
        injected += 1

    return injected


def inject_file(target_path, src_font):
    print(f"植入：{os.path.basename(target_path)} ← Sarasa box/block")
    font = TTFont(target_path)
    if "glyf" not in font:
        font.close()
        raise ValueError(f"{target_path} 非 glyf 字型，無法植入 TrueType outline")
    n = inject_box(font, src_font, os.path.basename(target_path))
    font.save(target_path)
    font.close()
    print(f"  植入 {n} 個 box/block glyph（自包含，已儲存）")
    return n


def main():
    args = sys.argv[1:]
    if len(args) < 3 or args[0] != "--source":
        print(f"用法：{sys.argv[0]} --source <Sarasa.ttf> <LXGW.ttf> [LXGW2.ttf ...]")
        sys.exit(1)
    src_path = args[1]
    targets = args[2:]
    for p in [src_path, *targets]:
        if not os.path.exists(p):
            print(f"檔案不存在：{p}", file=sys.stderr)
            sys.exit(1)

    src_font = TTFont(src_path)
    try:
        for t in targets:
            inject_file(t, src_font)
    finally:
        src_font.close()


if __name__ == "__main__":
    main()
