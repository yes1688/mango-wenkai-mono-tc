#!/usr/bin/env python3
"""
補齊「實心/空心成對」字元中缺失的一半，確保兩者來自同一字型、bbox 一致。

問題背景：
  LXGW WenKai Mono TC 有 ▱（U+25B1 空心平行四邊形）但缺 ▰（U+25B0 實心）。
  terminal 進度條 ▰▱▱▱▱ 的實心部分掉到 fallback 字型渲染，與空心部分高度差一倍多，
  綠色實心看起來像「沒填滿方格的細線」。

衍生方法（空心 → 實心）：
  空心 glyph = 外框 contour + 內部挖空 contour（兩 contour winding 相反，
  non-zero 填充規則下外框填、內框挖 → 形成環）。
  取「絕對面積最大」的外框 contour、丟掉內部挖空 contour，即得實心版。
  外框 contour 的點順序（winding direction）原樣保留 → 填充方向天然正確，
  不需手動 reverse。

可單獨執行，也可被 build pipeline import 使用（與 strip_ligatures.py /
normalize_advances.py 並列，於字型 vendoring 後處理階段呼叫）。

用法：
  python3 complete_glyph_pairs.py <font.ttf>           # 補齊已確認的成對字元
  python3 complete_glyph_pairs.py --scan <font.ttf>...  # 只掃描，不修改

注意：
  - 只操作 glyf（TrueType outline）字型；CFF 需另行處理。
  - advance 沿用配對字元既有值（已被 normalize_advances.py 正規化）。
  - ASCII 區（U+0000-U+007F）不碰。
"""

import os
import sys
import unicodedata

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.boundsPen import BoundsPen

# ── 已確認要補的成對字元 ────────────────────────────────────────────────
# (target_cp 缺的那個, source_cp 既有的那個, mode)
# mode="solid_from_hollow"：從空心衍生實心（保留外框、丟內框挖空）。
# 只列「CTO 已確認」要補的；其餘成對字元等掃描報告審完再加。
CONFIRMED_COMPLETIONS = {
    "LXGWWenKaiMonoTC": [
        (0x25B0, 0x25B1, "solid_from_hollow"),  # ▰ ← ▱ 平行四邊形（terminal 進度條）
    ],
}


def _split_contours(recording):
    """把 RecordingPen 的 value 依 closePath/endPath 切成多段 contour。
    回傳 list[list[(op, args)]]。
    """
    contours = []
    cur = []
    for op, args in recording:
        cur.append((op, args))
        if op in ("closePath", "endPath"):
            contours.append(cur)
            cur = []
    if cur:  # 沒有顯式 closePath 的殘段（理論上不該發生）
        contours.append(cur)
    return contours


def _contour_signed_area(contour):
    """以 shoelace 公式算 contour 的有號面積。
    qCurveTo 僅取終點近似（成對幾何字元為直線多邊形，足夠精確）。
    """
    pts = []
    for op, args in contour:
        if op == "moveTo":
            pts.append(args[0])
        elif op == "lineTo":
            pts.append(args[0])
        elif op == "qCurveTo":
            pts.append(args[-1])
        elif op == "curveTo":
            pts.append(args[-1])
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def derive_solid_from_hollow(font, hollow_glyph_name):
    """從空心 glyph 衍生實心 glyph。
    取絕對面積最大的外框 contour、原樣保留其 winding，丟掉內部挖空 contour。
    回傳新的 TTGlyph 物件（單 contour 實心）。
    """
    glyph_set = font.getGlyphSet()
    src = glyph_set.get(hollow_glyph_name)
    if src is None:
        raise ValueError(f"找不到來源 glyph：{hollow_glyph_name}")

    rec = RecordingPen()
    src.draw(rec)  # composite 也會在此被 decompose 成 contour

    contours = _split_contours(rec.value)
    if not contours:
        raise ValueError(f"來源 glyph {hollow_glyph_name} 無 contour，無法衍生實心")

    # 外框 = 絕對面積最大的 contour
    outer = max(contours, key=lambda c: abs(_contour_signed_area(c)))

    pen = TTGlyphPen(font.getGlyphSet())
    for op, args in outer:
        getattr(pen, op)(*args)
    return pen.glyph()


_DERIVERS = {
    "solid_from_hollow": derive_solid_from_hollow,
}


def _glyph_height(font, glyph_name):
    """回傳 glyph bbox 高度（ymax-ymin），空 glyph 回 0。"""
    bp = BoundsPen(font.getGlyphSet())
    font.getGlyphSet()[glyph_name].draw(bp)
    if bp.bounds is None:
        return 0
    _, ymin, _, ymax = bp.bounds
    return ymax - ymin


def _inject_glyph(font, cp, glyph, advance, lsb, glyph_name, vmetrics=None):
    """把 glyph 加進 glyf / hmtx / glyphOrder / 所有 Unicode cmap subtable。
    lsb（left side bearing）須 = glyph 實際 xMin，否則 glyphSet 渲染時會依
    「lsb − xMin」把 glyph 水平平移，使衍生字相對來源字錯位。
    若字型有 vmtx（CJK 直排 metrics），vmetrics 必填，一併寫入，否則
    glyphSet 取用與存檔會因缺直排 metrics 而失敗。
    """
    if glyph_name in font["glyf"].glyphs:
        raise ValueError(f"glyph name 衝突：{glyph_name} 已存在")

    # glyf.__setitem__ 會自動把 glyph_name 加進共享的 glyphOrder，不需手動 append
    font["glyf"][glyph_name] = glyph
    font["hmtx"].metrics[glyph_name] = (advance, lsb)
    if "vmtx" in font:
        if vmetrics is None:
            raise ValueError(f"字型含 vmtx，植入 {glyph_name} 必須提供 vmetrics")
        font["vmtx"].metrics[glyph_name] = vmetrics

    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap[cp] = glyph_name


def complete_glyph(font, target_cp, source_cp, mode):
    """若 target_cp 缺失且 source_cp 存在，衍生並植入 target_cp。
    回傳 True 表示有植入，False 表示跳過（target 已存在或 source 缺失）。
    """
    if 0x00 <= target_cp <= 0x7F:
        raise ValueError(f"拒絕修改 ASCII 區 U+{target_cp:04X}")

    cmap = font.getBestCmap() or {}
    if target_cp in cmap:
        print(f"  U+{target_cp:04X} 已存在，跳過")
        return False
    source_gn = cmap.get(source_cp)
    if source_gn is None:
        print(f"  來源 U+{source_cp:04X} 不存在，無法衍生 U+{target_cp:04X}，跳過")
        return False

    deriver = _DERIVERS.get(mode)
    if deriver is None:
        raise ValueError(f"未知衍生模式：{mode}")

    glyph = deriver(font, source_gn)
    advance = font["hmtx"].metrics[source_gn][0]  # 沿用配對字元 advance（已 normalize）

    glyph_name = f"uni{target_cp:04X}"
    if glyph_name in font.getGlyphOrder():
        glyph_name = f"{glyph_name}.derived"

    # lsb 設為衍生 glyph 的實際 xMin（= 渲染不平移）。外框沿用來源 outline 座標，
    # 故 ▰ xMin == ▱ 外框 xMin，與來源水平對齊。若停留在 0 而 xMin≠0，glyphSet
    # 會把實心相對空心左移（LXGW ▰ 曾因此左移 ~11%、進度條 ▰▱ 並排對不齊）。
    glyph.recalcBounds(font["glyf"])
    lsb = glyph.xMin

    # 有 vmtx 的字型沿用來源字元的直排 metrics
    vmetrics = font["vmtx"].metrics[source_gn] if "vmtx" in font else None
    _inject_glyph(font, target_cp, glyph, advance, lsb, glyph_name, vmetrics)

    src_h = _glyph_height(font, source_gn)
    new_h = _glyph_height(font, glyph_name)
    print(
        f"  植入 U+{target_cp:04X}（{glyph_name}）← U+{source_cp:04X}："
        f"advance={advance}, bbox高={new_h}（來源={src_h}）"
    )
    return True


def _family_key(font):
    """從 name table 取家族名（去空白），用來對照 CONFIRMED_COMPLETIONS。"""
    name = font["name"]
    rec = name.getName(1, 3, 1, 0x409) or name.getName(1, 1, 0, 0)
    if rec is None:
        return ""
    return rec.toUnicode().replace(" ", "")


def complete_file(path):
    """開啟 TTF、補齊已確認的成對字元、原地覆寫。"""
    print(f"處理：{path}")
    font = TTFont(path)
    if "glyf" not in font:
        print("  非 glyf 字型，跳過（本腳本只處理 TrueType outline）")
        font.close()
        return 0

    family = _family_key(font)
    completions = CONFIRMED_COMPLETIONS.get(family)
    if not completions:
        print(f"  家族「{family}」無已確認補字項目，跳過")
        font.close()
        return 0

    print(f"  家族：{family}，待補 {len(completions)} 項")
    n = 0
    for target_cp, source_cp, mode in completions:
        if complete_glyph(font, target_cp, source_cp, mode):
            n += 1

    if n:
        font.save(path)
        print(f"  補齊 {n} 個 glyph，已儲存")
    else:
        print("  無需修改，跳過")
    font.close()
    return n


# ── 掃描模式 ────────────────────────────────────────────────────────────
# terminal / TUI / status line 會用到的成對或系列字元範圍。
SCAN_RANGES = [
    ("Geometric Shapes", 0x25A0, 0x25FF),
    ("Block Elements", 0x2580, 0x259F),
    ("Box Drawing", 0x2500, 0x257F),
    ("Stars/Marks", 0x2605, 0x2606),
    ("Dingbat stars", 0x2730, 0x273F),
    ("Arrows (common)", 0x2190, 0x21FF),
]


def scan_fonts(paths):
    """掃描多個字型在 SCAN_RANGES 的 glyph 完整性，列印報告。
    對每個 codepoint 回報各字型：有/缺 + bbox 高度。
    重點標記「成對只有一半」「同系列 bbox 高度不一致」。
    """
    fonts = []
    for p in paths:
        f = TTFont(p)
        fonts.append((os.path.basename(p), f, f.getBestCmap() or {}, f.getGlyphSet()))

    def height(cmap, gs, cp):
        gn = cmap.get(cp)
        if gn is None:
            return None
        bp = BoundsPen(gs)
        gs[gn].draw(bp)
        if bp.bounds is None:
            return 0
        _, ymin, _, ymax = bp.bounds
        return int(ymax - ymin)

    names = [n for n, _, _, _ in fonts]
    print("=" * 72)
    print("字型成對 / 系列字元完整性掃描")
    print("字型：" + " | ".join(names))
    print("=" * 72)

    issues = []  # (severity, line)
    for label, lo, hi in SCAN_RANGES:
        print(f"\n## {label}  U+{lo:04X}–U+{hi:04X}")
        for cp in range(lo, hi + 1):
            try:
                ch = chr(cp)
                uname = unicodedata.name(ch, "")
            except ValueError:
                continue
            if not uname:
                continue
            cells = []
            heights = []
            present = []
            for n, f, cmap, gs in fonts:
                h = height(cmap, gs, cp)
                if h is None:
                    cells.append("缺")
                    present.append(False)
                else:
                    cells.append(f"h={h}")
                    heights.append(h)
                    present.append(True)
            # 只列至少一個字型有、或全缺但屬已知成對的
            n_present = sum(present)
            if n_present == 0:
                continue
            row = f"  U+{cp:04X} {ch} {uname[:34]:34} " + " | ".join(
                f"{n.split('-')[0][:6]:6}={c}" for (n, *_), c in zip(fonts, cells)
            )
            # 嚴重度判定
            if 0 < n_present < len(fonts):
                issues.append((0, f"[缺字] U+{cp:04X} {ch} {uname}：" + " ".join(
                    f"{n.split('-')[0][:8]}={c}" for (n, *_), c in zip(fonts, cells))))
            elif heights and (max(heights) - min(heights)) > max(heights) * 0.25:
                issues.append((1, f"[高度不一致] U+{cp:04X} {ch} {uname}：" + " ".join(
                    f"{n.split('-')[0][:8]}=h{c.split('=')[-1]}" for (n, *_), c in zip(fonts, cells))))
            print(row)

    print("\n" + "=" * 72)
    print("問題彙整（嚴重度：缺字 > bbox 高度不一致）")
    print("=" * 72)
    issues.sort(key=lambda x: x[0])
    if not issues:
        print("  無問題，三字型成對/系列字元完整且高度一致。")
    for _, line in issues:
        print("  " + line)

    for _, f, _, _ in fonts:
        f.close()


def main():
    args = sys.argv[1:]
    if not args:
        print(f"用法：{sys.argv[0]} [--scan] <font.ttf> [font2.ttf ...]")
        sys.exit(1)

    if args[0] == "--scan":
        paths = args[1:]
        if not paths:
            print("--scan 需指定至少一個字型檔")
            sys.exit(1)
        for p in paths:
            if not os.path.exists(p):
                print(f"檔案不存在：{p}", file=sys.stderr)
                sys.exit(1)
        scan_fonts(paths)
        return

    for path in args:
        if not os.path.exists(path):
            print(f"檔案不存在：{path}", file=sys.stderr)
            sys.exit(1)
        complete_file(path)


if __name__ == "__main__":
    main()
