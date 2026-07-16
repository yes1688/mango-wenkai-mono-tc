#!/usr/bin/env python3
"""
box drawing / block 的「觸界簽章」量化判定 — 驗證 fallback 目標字型能無縫接框。

CTO/Boss 指示：對齊好壞用程式量化判定，不靠肉眼。box 無縫的必要條件 =「端點觸界」：
每個 box 字的 ink 是否觸到 cell 的左/右/上/下界，要與 Iosevka（終端 box 標竿）的
簽章一致；相鄰格才接得起來。

簽章 = (L, R, T, B)：
  L = bbox x0 ≤ EDGE_H×advance              （觸左界）
  R = bbox x1 ≥ advance − EDGE_H×advance     （觸右界）
  T = bbox y1 ≥ ascent − EDGE_V×UPM          （觸上界，含行高）
  B = bbox y0 ≤ descent + EDGE_V×UPM          （觸下界）
以各字型自身 hhea ascent/descent 當 cell 上下界（跨字型可比：問的是「ink 是否觸到
這個字型自己的行框邊」）。target 簽章量自 Iosevka，baked 在 iosevka_targets.BOX_TILING。

用法：python3 box_tiling.py <font.ttf> [font2.ttf ...]   # 報各字型對齊 Iosevka 簽章的比率
"""

import os
import sys

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from iosevka_targets import BOX_TILING

EDGE_H = 0.05   # 水平觸界容差（× advance）— 與 gen_iosevka_targets 一致
EDGE_V = 0.06   # 垂直觸界容差（× UPM）


def box_signature(font, gn, advance, asc, desc, upm):
    """回傳 glyph 的觸界簽章 (L, R, T, B)；空 glyph 回 None。"""
    bp = BoundsPen(font.getGlyphSet())
    font.getGlyphSet()[gn].draw(bp)
    b = bp.bounds
    if b is None:
        return None
    eh, ev = EDGE_H * advance, EDGE_V * upm
    return (b[0] <= eh, b[2] >= advance - eh, b[3] >= asc - ev, b[1] <= desc + ev)


def check_tiling(font, targets=None):
    """比對字型 box/block 簽章與 Iosevka target。

    回傳 (match, total, mismatches)：total = 字型實際有且 target 也有的字數，
    match = 簽章一致數，mismatches = [(cp, target_sig, font_sig), ...]。
    """
    targets = targets if targets is not None else BOX_TILING
    cmap = font.getBestCmap() or {}
    hmtx = font["hmtx"]
    asc = font["hhea"].ascent
    desc = font["hhea"].descent
    upm = font["head"].unitsPerEm
    match = 0
    total = 0
    mismatches = []
    for cp, tsig in sorted(targets.items()):
        gn = cmap.get(cp)
        if gn is None:
            continue
        adv = hmtx[gn][0]
        if not adv:
            continue
        sig = box_signature(font, gn, adv, asc, desc, upm)
        if sig is None:
            continue
        total += 1
        if sig == tuple(tsig):
            match += 1
        else:
            mismatches.append((cp, tuple(tsig), sig))
    return match, total, mismatches


def main():
    if len(sys.argv) < 2:
        print(f"用法：{sys.argv[0]} <font.ttf> [font2.ttf ...]")
        sys.exit(1)
    for path in sys.argv[1:]:
        font = TTFont(path)
        match, total, mism = check_tiling(font)
        print(f"{os.path.basename(path)}: box/block 觸界簽章對齊 {match}/{total}")
        for cp, tsig, sig in mism[:12]:
            print(f"  U+{cp:04X} {chr(cp)} Iose{tsig} vs {sig}")
        font.close()


if __name__ == "__main__":
    main()
