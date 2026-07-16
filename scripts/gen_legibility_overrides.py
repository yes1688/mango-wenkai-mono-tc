#!/usr/bin/env python3
"""
從三字型「乾淨原版」量測 P1/P2 容器類符號的長寬比，產生 legibility_overrides.py
（可辨認性 override 表，單一事實源；align_symbols 與 verify_font_metrics 都吃它）。

問題背景（docs/reports/20260716_cto_console_font_survey.md）：
  U+2460-24FF / U+2776-2793「容器類」符號（圈/框裡有數字或字母）被 normalize 砍半
  advance 後，align_symbols 依 Iosevka 小圈慣例縮到 0.436em，內容不可辨認。
  U+2100-218F 的 №/℅/分數等複合符號（LXGW 楷體畫在全形字身）同病。

表的語意（兩類，語意不同故分兩張表）：
  OVERRIDES_EXACT（P1 帶圈/括號/句點形全部）：無條件精準對齊 wr（放大或縮小），
    三字型該區段 wr/hc/vc 落同一 golden 容差 → 視覺一致。
  OVERRIDES_MIN_H（P2 複合字母/分數清單）：僅當 bbox 高 < MIN_LEGIBLE_EM 才放大到
    wr（可辨認性救援）；已達標字型（如 Sarasa 原生半形設計）不動，避免誤傷。

per-cp wr 的推導：類初值（帶圈 CIRCLED_WR / P2 1.0）為底；寬形字（⑽⒛№℡ 等
長寬比 AR > 1 者）單靠類初值到不了掃描驗收的 0.5em 高度下限，故逐字拉高到
  wr = MIN_TARGET_EM × AR × UPM / half_advance
（等比縮放下「寬度=wr×advance」⇔「高度=wr×advance/AR」，取 max(類初值, 所需值)）。
AR 量自原版（原版高 ≥ ORIG_TALL_EM 者才視為全高設計、參與拉高）。

用法：
  python3 gen_legibility_overrides.py                     # 原版取 git blob，覆寫 legibility_overrides.py
  python3 gen_legibility_overrides.py --circled-wr 1.25   # 帶圈類基準 wr 改 1.25（Boss 三檔比較用）
  python3 gen_legibility_overrides.py --originals-dir DIR # 原版改從目錄讀（上游下載備援）
  python3 gen_legibility_overrides.py --scan              # 不產表：掃 production，報「高<0.5em 且原版≥0.7em」flag
                                                          # （驗收判準；有 flag → exit 1）
"""

import os
import subprocess
import sys
import tempfile

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from iosevka_targets import TARGETS

REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
FONTS_REL = "crates/hive-native-gui/assets/fonts"

# 原版來源（與 rebuild-fonts.sh 同一套 git blob；Mango 原版缺 P1 整段，AR 量不到、
# 也不需要——Mango 的 P1 由 inject_symbol_glyphs 從 Sarasa 植入，AR 同 Sarasa）。
ORIGINALS = {
    "LXGW": ("LXGWWenKaiMonoTC-Regular.ttf", "8094ee4"),
    "Sarasa": ("SarasaMonoTC-Regular.ttf", "87917e0"),
}
PRODUCTION_REGULARS = [
    "MangoMono-NF-TC-Regular.ttf",
    "LXGWWenKaiMonoTC-Regular.ttf",
    "SarasaMonoTC-Regular.ttf",
]

# P1：帶圈字母數字 + 實心/空心帶圈數字（容器類，整段收）。
P1_RANGES = [(0x2460, 0x24FF), (0x2776, 0x2793)]
# P2：字母符號 + 數字形式 block（逐字判定，僅收「被管線縮壞」者）。
P2_BLOCK = (0x2100, 0x218F)

CIRCLED_WR_DEFAULT = 1.15  # 帶圈類基準 wr（spec 初值；Boss 三檔 1.0/1.15/1.25 挑定後鎖 golden）
CIRCLED_WR_MIN, CIRCLED_WR_MAX = 1.0, 1.5  # --circled-wr 合理範圍（QA F2：真實輸入面要驗證）
P2_BASE_WR = 1.0           # P2 基準 wr（spec 初值）
MIN_LEGIBLE_EM = 0.50      # 可辨認性高度下限（驗收：掃描 0 flag 的判準）
MIN_TARGET_EM = 0.52       # align 放大的目標高度（留 0.02em 餘裕給下限）
ORIG_TALL_EM = 0.70        # 「原版是全高設計」判準（掃描 flag 的原版條件）
P1_HC, P1_VC = 0.5, 0.34   # P1 置中目標（spec）

HEADER = '''\
#!/usr/bin/env python3
"""
容器類符號可辨認性 override 表 — **自動產生，勿手改**。
重生：python3 gen_legibility_overrides.py [--circled-wr {wr}]

兩張表語意不同（單一事實源；align_symbols 與 verify_font_metrics 都吃這裡）：
  OVERRIDES_EXACT（P1 帶圈/括號/句點形）：cp -> (wr, hc, vc)，無條件精準對齊
    （放大或縮小到 wr），三字型落同一容差 → 視覺一致。
  OVERRIDES_MIN_H（P2 複合字母/分數）：cp -> (wr, hc, vc)，僅當 bbox 高 <
    MIN_LEGIBLE_EM 才放大到 wr；已達標的字型（Sarasa 原生半形設計）不動。
寬形字（⑽⒛№℡ 等）wr 已按原版長寬比逐字拉高，保證等比放大後高 ≥ MIN_TARGET_EM。
產生邏輯與參數見 gen_legibility_overrides.py docstring。
"""

CIRCLED_WR = {circled_wr}      # 帶圈類基準 wr（Boss 三檔挑定後鎖定）
MIN_LEGIBLE_EM = {min_legible}   # 可辨認性高度下限（verify 的 golden floor）
MIN_TARGET_EM = {min_target}    # align 放大的目標高度（留餘裕給下限）
P1_RANGES = {p1_ranges}  # 帶圈區段（inject_symbol_glyphs 植入範圍同此）

# (wr, hc, vc)
OVERRIDES_EXACT = {{
'''

MID = '''}

# (wr, hc, vc)
OVERRIDES_MIN_H = {
'''

FOOTER = '''}

# 合併視圖（EXACT 優先；兩表 cp 不重疊，verify 逐字迭代用）
OVERRIDES = {**OVERRIDES_MIN_H, **OVERRIDES_EXACT}
'''


def _bbox(font, cmap, cp):
    gn = cmap.get(cp)
    if gn is None:
        return None
    bp = BoundsPen(font.getGlyphSet())
    font.getGlyphSet()[gn].draw(bp)
    return bp.bounds


def _half_advance(font, cmap):
    m_gn = cmap.get(0x004D)
    if m_gn is None:
        raise ValueError("找不到 'M'(U+004D)，無法判定半形 advance 基準")
    return font["hmtx"].metrics[m_gn][0]


def _load_originals(originals_dir):
    """回傳 {label: TTFont}。預設從 git blob 取（同 rebuild-fonts.sh），備援讀目錄。"""
    fonts = {}
    for label, (fn, blob) in ORIGINALS.items():
        if originals_dir:
            path = os.path.join(originals_dir, fn)
            if not os.path.exists(path):
                raise FileNotFoundError(f"原版目錄缺檔：{path}")
            fonts[label] = TTFont(path)
        else:
            raw = subprocess.run(
                ["git", "show", f"{blob}:{FONTS_REL}/{fn}"],
                cwd=REPO_ROOT, capture_output=True, check=True,
            ).stdout
            if not raw:
                raise RuntimeError(f"git blob 取出為空：{blob}:{fn}")
            tmp = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
            tmp.write(raw)
            tmp.close()
            fonts[label] = TTFont(tmp.name)
            os.unlink(tmp.name)
    return fonts


def validate_circled_wr(value):
    """驗證 --circled-wr 引數（QA F2，task-ce5c0a98）：這是 Boss 挑檔位的真實
    輸入面，範圍外或非數字一律 ValueError，不靜默接受（如打錯的 0）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"--circled-wr 需要數字，收到 {value!r}") from None
    if not (CIRCLED_WR_MIN <= v <= CIRCLED_WR_MAX):
        raise ValueError(
            f"--circled-wr {v} 超出合理範圍 [{CIRCLED_WR_MIN}, {CIRCLED_WR_MAX}]"
            "（帶圈 glyph 寬/半格覆蓋率；<1 縮回不可辨認、>1.5 溢出過半格）"
        )
    return v


def _wr_needed(orig_w, orig_h, half_adv, upm):
    """等比縮放下，高度達 MIN_TARGET_EM 所需的 wr（寬/advance 覆蓋率）。"""
    ar = orig_w / orig_h
    return MIN_TARGET_EM * ar * upm / half_adv


def build_tables(originals, circled_wr):
    """回傳 (exact_rows, min_h_rows)，皆為 [(cp, wr, hc, vc)]。"""
    measured = {}  # label -> (font, cmap, upm, half_adv)
    for label, font in originals.items():
        cmap = font.getBestCmap() or {}
        measured[label] = (font, cmap, font["head"].unitsPerEm, _half_advance(font, cmap))

    exact_rows = []
    for lo, hi in P1_RANGES:
        for cp in range(lo, hi + 1):
            wr = circled_wr
            for font, cmap, upm, half in measured.values():
                b = _bbox(font, cmap, cp)
                if b is None:
                    continue
                w, h = b[2] - b[0], b[3] - b[1]
                # 原版全高設計者才參與拉高（矮設計維持類基準，不無謂放大）
                if h <= 0 or h / upm < ORIG_TALL_EM:
                    continue
                wr = max(wr, _wr_needed(w, h, half, upm))
            exact_rows.append((cp, round(wr, 3), P1_HC, P1_VC))

    min_h_rows = []
    for cp in range(P2_BLOCK[0], P2_BLOCK[1] + 1):
        target = TARGETS.get(cp)
        if target is None:
            continue  # 字母等已被 iosevka_targets 排除者，不救援
        target_wr, _thc, tvc = target
        wr = P2_BASE_WR
        needy = False
        for font, cmap, upm, half in measured.values():
            b = _bbox(font, cmap, cp)
            if b is None:
                continue
            w, h = b[2] - b[0], b[3] - b[1]
            if w <= 0 or h <= 0 or h / upm < ORIG_TALL_EM:
                continue
            # 預測 align（只縮不放）後的高度：cur_wr 超標才縮，等比連高一起縮。
            predicted_h = (h / upm) * min(1.0, target_wr * half / w)
            if predicted_h < MIN_LEGIBLE_EM:
                needy = True
                wr = max(wr, _wr_needed(w, h, half, upm))
        if needy:
            min_h_rows.append((cp, round(wr, 3), 0.5, tvc))
    return exact_rows, min_h_rows


def generate(originals_dir, circled_wr):
    originals = _load_originals(originals_dir)
    try:
        exact_rows, min_h_rows = build_tables(originals, circled_wr)
    finally:
        for f in originals.values():
            f.close()

    out = os.path.join(SCRIPT_DIR, "legibility_overrides.py")
    with open(out, "w", encoding="utf-8") as f:
        p1_ranges_repr = "[" + ", ".join(
            f"(0x{lo:04X}, 0x{hi:04X})" for lo, hi in P1_RANGES
        ) + "]"
        f.write(HEADER.format(
            wr=circled_wr,
            circled_wr=circled_wr,
            min_legible=MIN_LEGIBLE_EM,
            min_target=MIN_TARGET_EM,
            p1_ranges=p1_ranges_repr,
        ))
        for cp, wr, hc, vc in exact_rows:
            f.write(f"    0x{cp:04X}: ({wr}, {hc}, {vc}),  # {chr(cp)}\n")
        f.write(MID)
        for cp, wr, hc, vc in min_h_rows:
            f.write(f"    0x{cp:04X}: ({wr}, {hc}, {vc}),  # {chr(cp)}\n")
        f.write(FOOTER)
    print(
        f"已產生 {out}：EXACT {len(exact_rows)} 字（帶圈基準 wr={circled_wr}）"
        f" + MIN_H {len(min_h_rows)} 字"
    )


def scan(originals_dir):
    """驗收掃描：production 對原版，P1/P2 報「高<MIN_LEGIBLE_EM 且原版≥ORIG_TALL_EM」flag。

    範圍 = P1 全區段 + P2 清單（由原版重推導，與表生成同一套邏輯）——**不是整個
    U+2100-218F block**：ℹ 等「對齊 Iosevka 屬正常」的小字（spec 不修名單）雖在
    block 內且高 <0.5em，但那是 Iosevka 自身的小設計，不是管線縮壞，不 flag。"""
    originals = _load_originals(originals_dir)
    exact_rows, min_h_rows = build_tables(originals, CIRCLED_WR_DEFAULT)
    cps = [cp for cp, _, _, _ in exact_rows] + [cp for cp, _, _, _ in min_h_rows]
    total_flags = 0
    try:
        orig_by_file = {fn: originals[label] for label, (fn, _) in ORIGINALS.items()}
        for fn in PRODUCTION_REGULARS:
            path = os.path.join(REPO_ROOT, FONTS_REL, fn)
            prod = TTFont(path)
            pc = prod.getBestCmap() or {}
            pu = prod["head"].unitsPerEm
            # Mango 原版缺整段（本 task 由 Sarasa 植入），原版高以植入來源 Sarasa 為準。
            orig = orig_by_file.get(fn, originals["Sarasa"])
            oc = orig.getBestCmap() or {}
            ou = orig["head"].unitsPerEm
            flags = []
            for cp in cps:
                ob = _bbox(orig, oc, cp)
                if ob is None or (ob[3] - ob[1]) / ou < ORIG_TALL_EM:
                    continue
                pb = _bbox(prod, pc, cp)
                if pb is None:
                    continue  # 缺字非本掃描對象（cmap 覆蓋由 verify invariant 把關）
                h = (pb[3] - pb[1]) / pu
                if h < MIN_LEGIBLE_EM:
                    flags.append(f"U+{cp:04X} {chr(cp)} h={h:.3f}em")
            state = "✅ 0 flag" if not flags else f"❌ {len(flags)} flag：{'；'.join(flags[:10])}"
            print(f"{fn}: {state}")
            total_flags += len(flags)
            prod.close()
    finally:
        for f in originals.values():
            f.close()
    if total_flags:
        print(f"共 {total_flags} 個可辨認性 flag（高<{MIN_LEGIBLE_EM}em 且原版≥{ORIG_TALL_EM}em）")
        sys.exit(1)
    print("✅ P1/P2 區段 0 個可辨認性 flag")


def main():
    args = sys.argv[1:]
    originals_dir = None
    circled_wr = CIRCLED_WR_DEFAULT
    do_scan = False
    i = 0
    while i < len(args):
        if args[i] == "--originals-dir":
            originals_dir = args[i + 1]
            i += 2
        elif args[i] == "--circled-wr":
            try:
                circled_wr = validate_circled_wr(args[i + 1])
            except ValueError as e:
                print(str(e), file=sys.stderr)
                sys.exit(2)
            i += 2
        elif args[i] == "--scan":
            do_scan = True
            i += 1
        else:
            print(f"未知參數：{args[i]}（見 docstring）", file=sys.stderr)
            sys.exit(2)
    if do_scan:
        scan(originals_dir)
    else:
        generate(originals_dir, circled_wr)


if __name__ == "__main__":
    main()
