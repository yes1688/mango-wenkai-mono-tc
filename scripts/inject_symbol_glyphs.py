#!/usr/bin/env python3
"""
把 Sarasa 的帶圈字母數字（U+2460-24FF）+ 實心/空心帶圈數字（U+2776-2793）glyph
植入 Mango，讓預設字型自包含（不再靠 fallback）。

為何植入：Mango 合成來源 NotoSansSymbols2 在此區段 0 glyph（帶圈數字收錄在
Noto Sans Symbols 一代，不在 Symbols2），production Mango 缺 187/190。缺字後兩條
渲染路徑 fallback 落點不同（egui → Sarasa 縮小版；終端 → macOS .SF NS 比例字型，
Linux 上更不存在），呈現不一致。植入同批已過管線（align 完）的 Sarasa glyph 一次
消除（spec: docs/plans/20260716_font_symbol_legibility_spec.md 修法 B）。

做法（比照 inject_box_glyphs.py 模式；差異 = 半格寬不同需等比縮放）：
  1. glyphSet.draw 取 Sarasa glyph outline（composite 在此 decompose 成 contour）
  2. 等比 ×(目標半格/來源半格)（Mango 600 / Sarasa 500 = 1.2；由 'M' advance 動態
     判定不寫死），並垂直平移使 bbox 垂直中心不變 → wr/hc/vc 三比率與來源一致
     （UPM 皆 1000）。尺寸**定點**不歸本腳本管——交給後續 align pass
  3. TTGlyphPen 重建 simple glyph，植入 glyf / hmtx /（有則）vmtx
  4. advance = 目標半格；已有 cp 不覆蓋（Mango 原生 Ⓘ Ⓜ ⓧ 保留）→ 冪等
     （重跑植入 0 字、結果不變）

Pipeline 順序：align_symbols **之前**（QA F1，task-ce5c0a98）——align 是唯一
尺寸權威，植入字與原生字一起由目標字型自己的 align pass 定點（legibility
override EXACT），production 重跑 align 才會「無需修改」。若 inject 放 align
之後，植入字 = 來源 align 定點 × 等比換算，rounding 落點與 align 自算不同，
重跑 align 會漂 1 unit（非冪等，QA F1 實測 115/188 字）。

用法：
  python3 inject_symbol_glyphs.py --source <Sarasa.ttf> <Mango.ttf> [Mango2.ttf ...]
"""

import os
import sys

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.boundsPen import BoundsPen

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
# 植入範圍 = legibility override 的 P1 帶圈區段（單一事實源）。
from legibility_overrides import P1_RANGES

CP_M = 0x004D  # Latin 'M' — 半形 advance 基準（不寫死絕對值）


def _in_ranges(cp):
    return any(lo <= cp <= hi for lo, hi in P1_RANGES)


def _copy_outline_scaled(src_font, src_gn, scale):
    """取來源 glyph outline，等比 ×scale 並垂直平移使 bbox 垂直中心不變，
    重建為 simple TTGlyph。回傳 (glyph, lsb)。

    水平不需補償：hcenter 與 advance 同乘 scale → hc 比率自動保持；
    垂直 advance 不變（UPM 相同）→ vcenter 純縮放會往原點跑，dy 補回。
    """
    glyph_set = src_font.getGlyphSet()
    bp = BoundsPen(glyph_set)
    glyph_set[src_gn].draw(bp)
    if bp.bounds is None:
        raise AssertionError(f"{src_gn}：來源 glyph 為空，拒絕植入")
    vcenter = (bp.bounds[1] + bp.bounds[3]) / 2.0
    dy = vcenter * (1.0 - scale)

    rec = DecomposingRecordingPen(glyph_set)  # composite 在此 decompose 成 contour
    glyph_set[src_gn].draw(rec)
    pen = TTGlyphPen(glyph_set)
    tpen = TransformPen(pen, (scale, 0, 0, scale, 0.0, dy))
    rec.replay(tpen)
    glyph = pen.glyph()
    glyph.recalcBounds(src_font["glyf"])
    lsb = glyph.xMin if glyph.numberOfContours > 0 else 0
    return glyph, lsb


def inject_symbols(target_font, src_font, target_label):
    """把 src_font 的 P1 帶圈 glyph 植入 target_font（已有 cp 跳過）。回傳植入字數。"""
    tcmap = target_font.getBestCmap() or {}
    m_gn = tcmap.get(CP_M)
    if m_gn is None:
        raise ValueError(f"{target_label} 找不到 'M'(U+004D)，無法判定半形 advance 基準")
    t_half = target_font["hmtx"].metrics[m_gn][0]

    scmap = src_font.getBestCmap() or {}
    s_m = scmap.get(CP_M)
    if s_m is None:
        raise ValueError("來源找不到 'M'(U+004D)，無法判定半形 advance 基準")
    s_half = src_font["hmtx"].metrics[s_m][0]
    if s_half <= 0:
        raise ValueError(f"來源半形 advance={s_half} 非法")

    t_upm = target_font["head"].unitsPerEm
    s_upm = src_font["head"].unitsPerEm
    if t_upm != s_upm:
        # 等比縮放的 vc 保持依賴兩字型 UPM 相同（三個 embed 字型皆 1000）。
        raise AssertionError(f"UPM 不一致（目標 {t_upm} ≠ 來源 {s_upm}），植入比率會失真")

    scale = t_half / s_half
    has_vmtx = "vmtx" in target_font
    src_has_vmtx = "vmtx" in src_font

    injected = 0
    for cp in sorted(c for c in scmap if _in_ranges(c)):
        if cp in tcmap:
            continue  # 已有 cp 不覆蓋（原生字保留；重跑冪等）

        glyph, lsb = _copy_outline_scaled(src_font, scmap[cp], scale)
        if glyph.numberOfContours <= 0:
            raise AssertionError(f"U+{cp:04X}：來源 glyph 為空，拒絕植入")

        name = f"sym{cp:04X}"
        if name in target_font["glyf"].glyphs:
            name = f"{name}.inj"

        target_font["glyf"][name] = glyph
        target_font["hmtx"].metrics[name] = (t_half, lsb)
        if has_vmtx:
            # UPM 一致 → 直排 metrics 直接沿用來源；無則退回 (UPM, 0)。
            target_font["vmtx"].metrics[name] = (
                src_font["vmtx"].metrics[scmap[cp]] if src_has_vmtx else (t_upm, 0)
            )

        for table in target_font["cmap"].tables:
            if table.isUnicode():
                table.cmap[cp] = name
        tcmap[cp] = name
        injected += 1

    return injected


def inject_file(target_path, src_font):
    print(f"植入：{os.path.basename(target_path)} ← Sarasa 帶圈字母數字")
    font = TTFont(target_path)
    if "glyf" not in font:
        font.close()
        raise ValueError(f"{target_path} 非 glyf 字型，無法植入 TrueType outline")
    n = inject_symbols(font, src_font, os.path.basename(target_path))
    if n:
        font.save(target_path)
    font.close()
    print(f"  植入 {n} 個帶圈 glyph（已有 cp 跳過，已儲存）" if n else "  無缺字，跳過")
    return n


def main():
    args = sys.argv[1:]
    if len(args) < 3 or args[0] != "--source":
        print(f"用法：{sys.argv[0]} --source <Sarasa.ttf> <Mango.ttf> [Mango2.ttf ...]")
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
