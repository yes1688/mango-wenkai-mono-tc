#!/usr/bin/env python3
"""
字型後處理 pipeline 的「產出一致性」閘門。

rebuild-fonts.sh 跑完 strip→normalize→complete→fit 後，用本腳本對產物斷言
四條 golden invariant，任一不符即 fail fast（exit 1），確保重建不會悄悄改變
已驗證的字型行為（⌘ 置中、▰ 補字、advance 2:1）。

四條 invariant（per-font，以 Latin 'M' 的 advance 為基準動態判定，不寫死絕對值，
故三個 UPM/advance 不同的字型共用同一套規則）：

  1. [strip]     GSUB LookupType-4（Ligature Substitution）lookup 數 == 0
                 → strip_ligatures.py 的產出：連字 lookup 已清乾淨。
  2. [normalize] '中'(U+4E2D, EAW=W) 的 advance == 2 × 'M'(U+004D) 的 advance
                 → normalize_advances.py 的產出：全形 2:1 對齊 terminal EAW。
  3. [complete]  ▰(U+25B0) 存在，且其 bbox（含位置）== ▱(U+25B1) 的 bbox
                 → complete_glyph_pairs.py 的產出：實心由空心衍生，外框尺寸與
                  渲染位置都一致。比位置（非僅寬高）是防回歸：衍生實心的 lsb 須 =
                  其 xMin（與來源對齊），否則 ▰ 會相對 ▱ 左移、進度條 ▰▱ 並排錯位。
  4. [fit]       ⌘(U+2318) bbox 寬 ≤ advance（不爆框），且寬/advance ≈ MAX_RATIO
                 → fit_glyph_to_advance.py 的產出：爆框符號已縮進框並置中。
  5. [align]     contained 符號（TARGETS，標點/箭頭/技術/幾何）逐字對齊 Iosevka：
                 wr ≤ target+WR_TOL（不爆框超過覆蓋）、|hc−target|≤HC_TOL（水平置中）、
                 |vc−target|≤VC_TOL（垂直對齊）→ align_symbols.py 的產出。**三字型全驗**
                 （Mango/LXGW/Sarasa 皆可選為主字型）。
  6. [box]       box/block 自包含架構：LXGW box/block(U+2500-259F) 已植入 Sarasa
                 glyph（160 全有 cmap、advance 鎖半形），且 outline 與同目錄 Sarasa
                 對應 glyph 幾何等同 → 繼承 Sarasa 已驗的 tiling 觸界（不靠 fallback）。
                 Sarasa（植入來源）box/block 觸界簽章自身對齊 Iosevka tiling →
                 inject_box_glyphs.py 的產出。Mango box 原生（gpu 路徑），跳過。
                 為何比 outline 等同而非各自比 Iosevka 簽章：觸界判定用各字型自身
                 hhea ascent/descent 當 cell 界，LXGW(928/-241) 比 Sarasa(965/-215)
                 緊，6 條虛線豎線會在容差邊界差一線；植入的 outline 既與 Sarasa 逐點
                 相同，等同性即是「LXGW 框線 == 已驗 tiling 的 Sarasa 框線」的精確證明。
  7. [bold-lock] 僅 *-Bold：cell 網格與同目錄 *-Regular 逐字一致（終端等寬 Regular/Bold
                 同欄寬）—— M 相等 + cmap 交集逐字 advance 相等。非 Bold 字型跳過此條。

用法：
  python3 verify_font_metrics.py <font.ttf> [font2.ttf ...]
  全部通過 → exit 0；任一字型任一條失敗 → 印出失敗條目 + exit 1。
"""

import os
import sys

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
# DRY：fit 的縮放上限唯一定義在 fit_glyph_to_advance，驗證沿用同一常數。
from fit_glyph_to_advance import MAX_RATIO
# DRY：符號對齊的 target 表與容差唯一定義在 align_symbols / iosevka_targets，驗證沿用。
from align_symbols import TARGETS, WR_TOL, HC_TOL, VC_TOL
from box_tiling import check_tiling

# box drawing + block 範圍（invariant 6：LXGW 應移除 cmap、Sarasa 應 tiling）。
BOX_BLOCK_RANGE = range(0x2500, 0x25A0)

CP_M = 0x004D       # Latin 'M' — 半形 advance 基準
CP_WIDE = 0x4E2D    # '中' — EAW=W 全形代表字
CP_SOLID = 0x25B0   # ▰ 實心平行四邊形
CP_HOLLOW = 0x25B1  # ▱ 空心平行四邊形
CP_CMD = 0x2318     # ⌘ Place of Interest Sign

# ⌘ fit 後寬/advance 應落在此區間：上界 = MAX_RATIO（縮到上限），
# 下界留容差，確認確實縮過、而非停在舊 ratio 或根本沒處理。
CMD_RATIO_LO = MAX_RATIO - 0.04
CMD_RATIO_HI = MAX_RATIO + 0.01


def _bbox(font, glyph_name):
    """透過 glyphSet 量 bbox（會套 hmtx lsb 平移，反映實際渲染位置）。
    空 glyph 回 None。"""
    bp = BoundsPen(font.getGlyphSet())
    font.getGlyphSet()[glyph_name].draw(bp)
    return bp.bounds


def _outline_key(font, glyph_name):
    """回傳 glyph 的「幾何指紋」：decompose composite 後的 contour 操作序列（座標取整）。
    用來比對兩字型同一字的 outline 是否逐點等同，與 composite/simple 儲存方式無關。"""
    pen = DecomposingRecordingPen(font.getGlyphSet())
    font.getGlyphSet()[glyph_name].draw(pen)
    key = []
    for op, args in pen.value:
        pts = tuple((round(p[0]), round(p[1])) if p is not None else None for p in args)
        key.append((op, pts))
    return tuple(key)


def _ligature_lookup_count(font):
    """回傳 GSUB 中 LookupType-4（Ligature Substitution）lookup 數。"""
    if "GSUB" not in font:
        return 0
    table = font["GSUB"].table
    if table.LookupList is None:
        return 0
    return sum(1 for l in table.LookupList.Lookup if l.LookupType == 4)


def verify_font(path):
    """對單一字型驗四條 invariant。回傳 (ok, lines)：
    ok=True 全過；lines 為每條的 PASS/FAIL 說明。"""
    font = TTFont(path)
    try:
        cmap = font.getBestCmap() or {}
        hmtx = font["hmtx"]
        lines = []
        ok = True

        def check(cond, label, detail):
            nonlocal ok
            lines.append(f"    [{'PASS' if cond else 'FAIL'}] {label}：{detail}")
            if not cond:
                ok = False

        # 基準：Latin 'M' advance
        m_gn = cmap.get(CP_M)
        if m_gn is None:
            return False, [f"    [FAIL] 基準：找不到 'M'(U+004D)，無法判定 advance 基準"]
        m_adv = hmtx[m_gn][0]

        # 1. strip：無 ligature lookup
        lig = _ligature_lookup_count(font)
        check(lig == 0, "strip", f"GSUB Ligature(type4) lookup = {lig}（應 0）")

        # 2. normalize：中.advance == 2 × M.advance
        w_gn = cmap.get(CP_WIDE)
        if w_gn is None:
            check(False, "normalize", f"找不到 '中'(U+4E2D)")
        else:
            w_adv = hmtx[w_gn][0]
            check(
                w_adv == m_adv * 2,
                "normalize",
                f"'中' advance={w_adv} 應 == 2×M({m_adv})={m_adv * 2}",
            )

        # 3. complete：▰ 存在且 bbox == ▱
        solid_gn = cmap.get(CP_SOLID)
        hollow_gn = cmap.get(CP_HOLLOW)
        if hollow_gn is None:
            check(False, "complete", "找不到 ▱(U+25B1) 無法比對")
        elif solid_gn is None:
            check(False, "complete", "缺 ▰(U+25B0)")
        else:
            sb = _bbox(font, solid_gn)
            hb = _bbox(font, hollow_gn)
            if sb is None or hb is None:
                check(False, "complete", f"▰ 或 ▱ 為空 glyph（▰={_fmt(sb)} ▱={_fmt(hb)}）")
            else:
                # bbox 四座標都比（含位置）：寬高一致 + 左緣對齊（lsb 一致）。
                same = all(abs(a - b) <= 1 for a, b in zip(sb, hb))
                check(same, "complete", f"▰ bbox={_fmt(sb)} 應 == ▱ bbox={_fmt(hb)}")

        # 4. fit：⌘ 不爆框且寬/advance ≈ MAX_RATIO
        cmd_gn = cmap.get(CP_CMD)
        if cmd_gn is None:
            check(False, "fit", "找不到 ⌘(U+2318)")
        else:
            cmd_adv = hmtx[cmd_gn][0]
            cb = _bbox(font, cmd_gn)
            if cb is None:
                check(False, "fit", "⌘ 為空 glyph")
            else:
                w = cb[2] - cb[0]
                ratio = w / cmd_adv if cmd_adv else 0
                check(
                    w <= cmd_adv and CMD_RATIO_LO <= ratio <= CMD_RATIO_HI,
                    "fit",
                    f"⌘ bbox寬={w:.0f} advance={cmd_adv} ratio={ratio:.3f}"
                    f"（應 ≤advance 且 ∈[{CMD_RATIO_LO:.2f},{CMD_RATIO_HI:.2f}]）",
                )

        # 5. align：contained 符號逐字對齊 Iosevka（wr 上界 / hc / vc）。三字型全驗
        #    （Mango/LXGW/Sarasa 皆可選為主字型，contained 都要對齊）。
        upm = font["head"].unitsPerEm
        bad = []
        have = 0
        for cp, (target_wr, target_hc, target_vc) in sorted(TARGETS.items()):
            gn = cmap.get(cp)
            if gn is None:
                continue  # 該字型缺此符號，非本 task 補字範圍
            adv = hmtx[gn][0]
            b = _bbox(font, gn)
            if b is None or adv <= 0:
                continue
            have += 1
            wr = (b[2] - b[0]) / adv
            hc = (b[0] + b[2]) / 2 / adv
            vc = (b[1] + b[3]) / 2 / upm
            if (
                wr > target_wr + WR_TOL
                or abs(hc - target_hc) > HC_TOL
                or abs(vc - target_vc) > VC_TOL
            ):
                bad.append(f"U+{cp:04X}(wr={wr:.2f}/hc={hc:.2f}/vc={vc:.2f})")
        check(
            not bad,
            "align",
            f"contained 符號全對齊（{have} 字）"
            if not bad else f"{len(bad)}/{have} 未對齊：{', '.join(bad[:8])}",
        )

        # 6. box 自包含：LXGW box/block 已植入 Sarasa glyph（160 全有、advance 鎖半形、
        #    outline 與同目錄 Sarasa 幾何等同 → 繼承 Sarasa 已驗 tiling）；Sarasa（來源）
        #    自身觸界簽章對齊 Iosevka。Mango box 原生（gpu 路徑），跳過。
        base = os.path.basename(path)
        if "LXGW" in base:
            present = [cp for cp in BOX_BLOCK_RANGE if cp in cmap]
            weight = "Bold" if "-Bold" in base else "Regular"
            sib_path = os.path.join(os.path.dirname(path), f"SarasaMonoTC-{weight}.ttf")
            if not os.path.exists(sib_path):
                check(False, "box-inject", f"找不到植入來源 Sarasa：{os.path.basename(sib_path)}")
            else:
                sar = TTFont(sib_path)
                try:
                    scmap = sar.getBestCmap() or {}
                    shmtx = sar["hmtx"]
                    missing = [cp for cp in BOX_BLOCK_RANGE if cp not in cmap]
                    bad_adv = [
                        cp for cp in present if hmtx[cmap[cp]][0] != m_adv
                    ]
                    diff = []
                    for cp in present:
                        sgn = scmap.get(cp)
                        if sgn is None:
                            continue
                        if _outline_key(font, cmap[cp]) != _outline_key(sar, sgn):
                            diff.append(cp)
                    n_total = len(list(BOX_BLOCK_RANGE))
                    check(
                        not missing and not bad_adv and not diff,
                        "box-inject",
                        f"LXGW box/block 自包含：cmap {len(present)}/{n_total}、"
                        f"advance 鎖半形({m_adv}) 不符 {len(bad_adv)}、"
                        f"outline 與 Sarasa 不等同 {len(diff)}"
                        + (f"（如 U+{diff[0]:04X}…）" if diff else ""),
                    )
                finally:
                    sar.close()
        elif "Sarasa" in base:
            match, total, _mism = check_tiling(font)
            check(
                total > 0 and match == total,
                "box-tiling",
                f"Sarasa（植入來源）box/block 觸界簽章對齊 Iosevka {match}/{total}",
            )
        else:
            lines.append("    [SKIP] box：Mango box 原生（gpu 路徑），不在植入改動範圍")

        # 7. bold advance lock：*-Bold 的 cell 網格須與同目錄 *-Regular 逐字一致
        #    （終端等寬：Regular/Bold 同欄寬）。M 相等 + cmap 交集逐字 advance 相等。
        if "-Bold" in base:
            reg_path = path.replace("-Bold.ttf", "-Regular.ttf")
            if not os.path.exists(reg_path):
                check(False, "bold-lock", f"找不到對應 Regular：{os.path.basename(reg_path)}")
            else:
                reg = TTFont(reg_path)
                try:
                    rcmap = reg.getBestCmap() or {}
                    rhmtx = reg["hmtx"]
                    r_m = rcmap.get(CP_M)
                    if r_m is None or m_gn is None:
                        check(False, "bold-lock", "Regular 或 Bold 缺 'M' 基準")
                    else:
                        same_m = rhmtx[r_m][0] == m_adv
                        inter = [cp for cp in cmap if cp in rcmap]
                        mism = sum(
                            1 for cp in inter
                            if hmtx[cmap[cp]][0] != rhmtx[rcmap[cp]][0]
                        )
                        check(
                            same_m and mism == 0,
                            "bold-lock",
                            f"Bold.M={m_adv} vs Regular.M={rhmtx[r_m][0]}；"
                            f"cmap 交集 {len(inter)} 字 advance 不符 {mism}（應 0）",
                        )
                finally:
                    reg.close()

        return ok, lines
    finally:
        font.close()


def _fmt(bounds):
    if bounds is None:
        return "None"
    return "(" + ",".join(f"{v:.0f}" for v in bounds) + ")"


def main():
    paths = sys.argv[1:]
    if not paths:
        print(f"用法：{sys.argv[0]} <font.ttf> [font2.ttf ...]")
        sys.exit(2)

    all_ok = True
    for path in paths:
        if not os.path.exists(path):
            print(f"檔案不存在：{path}", file=sys.stderr)
            sys.exit(2)
        ok, lines = verify_font(path)
        print(f"\n## 驗證：{os.path.basename(path)} → {'✅ 通過' if ok else '❌ 失敗'}")
        for line in lines:
            print(line)
        all_ok = all_ok and ok

    print("\n" + "=" * 60)
    if all_ok:
        print("✅ 全部字型通過 golden invariant，產出與 production 一致。")
        sys.exit(0)
    else:
        print("❌ 有字型未通過，rebuild 產出與預期不符，請勿覆寫 production。")
        sys.exit(1)


if __name__ == "__main__":
    main()
