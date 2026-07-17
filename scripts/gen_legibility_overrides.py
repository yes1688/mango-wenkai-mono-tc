#!/usr/bin/env python3
"""
產生 legibility_overrides.py（可辨認性 override 表，單一事實源；align_symbols
與 verify_font_metrics 都吃它）。

表的語意（兩類，語意不同故分兩張表）：
  OVERRIDES_EXACT（P1 帶圈 U+2460-24FF / U+2776-2793 全 190 字）：
    cp -> (wr, hr, hc, vc) 四元組，**per-cp 量自 Iosevka Term Regular**
    （--iosevka 指定，SHA256 記錄於產出檔頭）。align 非等比雙向精準對齊——
    複刻 Iosevka Term 高橢圓（spec 20260717）：寬收回格內（① wr=0.872，
    連排不相黏）、高拉到數字全高（① hr=0.744，可辨認性）。第一輪等比
    wr=1.15 放大（連排相黏，Boss 反映）由此取代。
  OVERRIDES_MIN_H（P2 複合字母/分數清單）：cp -> (wr, hc, vc) 三元組，僅當
    bbox 高 < MIN_LEGIBLE_EM 才等比放大到 wr（可辨認性救援）；已達標字型
    （如 Sarasa 原生半形設計）不動，避免誤傷。從三字型「乾淨原版」推導
    （沿第一輪邏輯不變）。

P2 per-cp wr 的推導：基準 1.0 為底；寬形字（№℡ 等長寬比 AR > 1 者）單靠
基準到不了掃描驗收的 0.5em 高度下限，故逐字拉高到
  wr = MIN_TARGET_EM × AR × UPM / half_advance
（等比縮放下「寬度=wr×advance」⇔「高度=wr×advance/AR」，取 max(基準, 所需值)）。
AR 量自原版（原版高 ≥ ORIG_TALL_EM 者才視為全高設計、參與拉高）。

表健全性守門：validate_exact_table 驗 EXACT 覆蓋齊全 + 值域（wr ≤ WR_CAP
不溢格、hr/hc/vc 合理帶）——gen 寫表前驗、verify import 時驗，壞來源重生
的壞表兩頭都過不了（QA F2 事故防線的本輪版）。

用法：
  python3 gen_legibility_overrides.py --iosevka <IosevkaTerm-Regular.ttf>
                                                          # 覆寫 legibility_overrides.py
  python3 gen_legibility_overrides.py --iosevka <ttf> --originals-dir DIR
                                                          # P2 原版改從目錄讀（上游下載備援）
  python3 gen_legibility_overrides.py --scan              # 不產表：掃 production，報
                                                          # 「高<0.5em 且原版≥0.7em」flag
                                                          # （驗收判準；有 flag → exit 1）
"""

import hashlib
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

# 原版來源（與 rebuild-fonts.sh 同一套 git blob；P2 推導用。Mango 原版缺 P1
# 整段，P1 由 inject_symbol_glyphs 從 Sarasa 植入，不需原版）。
ORIGINALS = {
    "LXGW": ("LXGWWenKaiMonoTC-Regular.ttf", "8094ee4"),
    "Sarasa": ("SarasaMonoTC-Regular.ttf", "87917e0"),
}
PRODUCTION_REGULARS = [
    "MangoMono-NF-TC-Regular.ttf",
    "LXGWWenKaiMonoTC-Regular.ttf",
    "SarasaMonoTC-Regular.ttf",
]

# P1：帶圈字母數字 + 實心/空心帶圈數字（容器類，整段收，EXACT）。
P1_RANGES = [(0x2460, 0x24FF), (0x2776, 0x2793)]
# P2：字母符號 + 數字形式 block（逐字判定，僅收「被管線縮壞」者，MIN_H）。
P2_BLOCK = (0x2100, 0x218F)

P2_BASE_WR = 1.0           # P2 基準 wr（第一輪 spec 初值）
MIN_LEGIBLE_EM = 0.50      # 可辨認性高度下限（驗收：掃描 0 flag 的判準）
MIN_TARGET_EM = 0.52       # P2 放大的目標高度（留 0.02em 餘裕給下限）
ORIG_TALL_EM = 0.70        # 「原版是全高設計」判準（掃描 flag 的原版條件）

# EXACT 值域（validate_exact_table；量測來源異常時 fail fast，不產壞表）。
# WR_CAP 0.95：wr ≤ 0.95 時即使 hc 偏到容差邊（±0.03）bbox 仍完整落在
# [0, advance] 內 → 不溢格（連排不相黏）是表層級的內在保證。
# Iosevka Term 34.7.0 實測 P1 wr 全距 0.796-0.886，離 cap 有餘裕。
WR_CAP = 0.95
HR_RANGE = (0.3, 0.95)     # 實測 hr 全距 0.580-0.751
HC_RANGE = (0.4, 0.6)      # 實測 hc 全距 0.466-0.500
VC_RANGE = (0.2, 0.5)      # 實測 vc 全距 0.340-0.367

HEADER = '''\
#!/usr/bin/env python3
"""
容器類符號可辨認性 override 表 — **自動產生，勿手改**。
重生：python3 gen_legibility_overrides.py --iosevka <IosevkaTerm-Regular.ttf>

兩張表語意不同（單一事實源；align_symbols 與 verify_font_metrics 都吃這裡）：
  OVERRIDES_EXACT（P1 帶圈）：cp -> (wr, hr, hc, vc)，非等比雙向精準對齊——
    複刻 Iosevka Term 高橢圓（spec 20260717）：寬收回格內（連排不相黏）、
    高 per-cp 抄 Iosevka（① hr=0.744 數字全高）。
  OVERRIDES_MIN_H（P2 複合字母/分數）：cp -> (wr, hc, vc)，僅當 bbox 高 <
    MIN_LEGIBLE_EM 才等比放大到 wr；已達標的字型（Sarasa 原生半形設計）不動。
產生邏輯與參數見 gen_legibility_overrides.py docstring。
"""

# EXACT 量測來源（per-cp 逐字量；重生時核對 SHA256 確保同一版本）
IOSEVKA_SOURCE = {source!r}
IOSEVKA_SHA256 = {sha!r}

MIN_LEGIBLE_EM = {min_legible}   # 可辨認性高度下限（P2 救援判準 + verify golden floor）
MIN_TARGET_EM = {min_target}    # P2 放大的目標高度（留餘裕給下限）
P1_RANGES = {p1_ranges}  # 帶圈區段（inject_symbol_glyphs 植入範圍同此）

# (wr, hr, hc, vc) — per-cp 量自 Iosevka Term
OVERRIDES_EXACT = {{
'''

MID = '''}

# (wr, hc, vc)
OVERRIDES_MIN_H = {
'''

FOOTER = '''}

# override cp 集合（verify invariant 5 讓位判定用；兩表 shape 不同，值不合併）
OVERRIDE_CPS = frozenset(OVERRIDES_EXACT) | frozenset(OVERRIDES_MIN_H)
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


def validate_exact_table(table):
    """EXACT 表健全性守門：P1 覆蓋齊全 + 逐行值域。

    量測來源異常（錯版本、缺字、壞檔）重生出的壞表，align/verify 會與它
    「自洽通過」——故 gen 寫表前驗、verify import 時驗，兩頭擋。
    wr ≤ WR_CAP 是「不溢格 → 連排不相黏」的表層保證。"""
    expected = set()
    for lo, hi in P1_RANGES:
        expected |= set(range(lo, hi + 1))
    if set(table) != expected:
        missing = expected - set(table)
        extra = set(table) - expected
        raise ValueError(
            f"EXACT 表覆蓋異常：缺 {len(missing)} 字"
            + (f"（如 U+{min(missing):04X}）" if missing else "")
            + (f"、多 {len(extra)} 字" if extra else "")
        )
    for cp, row in table.items():
        if len(row) != 4:
            raise ValueError(f"U+{cp:04X}：EXACT 應為 (wr, hr, hc, vc)，收到 {row!r}")
        wr, hr, hc, vc = row
        if not (0 < wr <= WR_CAP):
            raise ValueError(f"U+{cp:04X} wr={wr} 超出 (0, {WR_CAP}]（溢格風險，連排會相黏）")
        if not (HR_RANGE[0] < hr <= HR_RANGE[1]):
            raise ValueError(f"U+{cp:04X} hr={hr} 超出 {HR_RANGE}")
        if not (HC_RANGE[0] <= hc <= HC_RANGE[1]):
            raise ValueError(f"U+{cp:04X} hc={hc} 超出 {HC_RANGE}")
        if not (VC_RANGE[0] <= vc <= VC_RANGE[1]):
            raise ValueError(f"U+{cp:04X} vc={vc} 超出 {VC_RANGE}")


def build_exact_rows(iosevka_path):
    """從 Iosevka Term 量 P1 每字 (wr, hr, hc, vc)。回傳 (source_desc, rows)，
    rows = [(cp, wr, hr, hc, vc)]，source_desc 含 name table 的家族與版本字串
    （可追溯性：檔名不帶版本）。缺字即 error（Iosevka Term 34.7.0 P1 覆蓋
    100%；缺字 = 拿錯包）。"""
    font = TTFont(iosevka_path)
    try:
        family = font["name"].getDebugName(4) or "?"
        version = font["name"].getDebugName(5) or "?"
        source_desc = f"{os.path.basename(iosevka_path)} ({family} {version})"
        cmap = font.getBestCmap() or {}
        upm = font["head"].unitsPerEm
        rows = []
        for lo, hi in P1_RANGES:
            for cp in range(lo, hi + 1):
                b = _bbox(font, cmap, cp)
                if b is None:
                    raise ValueError(
                        f"量測來源缺 U+{cp:04X}（{iosevka_path} 非完整 Iosevka Term？）"
                    )
                gn = cmap[cp]
                adv = font["hmtx"].metrics[gn][0]
                if adv <= 0:
                    raise ValueError(f"U+{cp:04X} advance={adv} 非法")
                w, h = b[2] - b[0], b[3] - b[1]
                rows.append((
                    cp,
                    round(w / adv, 3),
                    round(h / upm, 3),
                    round((b[0] + b[2]) / 2 / adv, 3),
                    round((b[1] + b[3]) / 2 / upm, 3),
                ))
        return source_desc, rows
    finally:
        font.close()


def _wr_needed(orig_w, orig_h, half_adv, upm):
    """等比縮放下，高度達 MIN_TARGET_EM 所需的 wr（寬/advance 覆蓋率）。"""
    ar = orig_w / orig_h
    return MIN_TARGET_EM * ar * upm / half_adv


def build_min_h_rows(originals):
    """P2 救援清單（沿第一輪邏輯）。回傳 [(cp, wr, hc, vc)]。"""
    measured = {}  # label -> (font, cmap, upm, half_adv)
    for label, font in originals.items():
        cmap = font.getBestCmap() or {}
        measured[label] = (font, cmap, font["head"].unitsPerEm, _half_advance(font, cmap))

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
    return min_h_rows


def generate(originals_dir, iosevka_path):
    with open(iosevka_path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    source_desc, exact_rows = build_exact_rows(iosevka_path)
    validate_exact_table({cp: (wr, hr, hc, vc) for cp, wr, hr, hc, vc in exact_rows})

    originals = _load_originals(originals_dir)
    try:
        min_h_rows = build_min_h_rows(originals)
    finally:
        for f in originals.values():
            f.close()

    out = os.path.join(SCRIPT_DIR, "legibility_overrides.py")
    with open(out, "w", encoding="utf-8") as f:
        p1_ranges_repr = "[" + ", ".join(
            f"(0x{lo:04X}, 0x{hi:04X})" for lo, hi in P1_RANGES
        ) + "]"
        f.write(HEADER.format(
            source=source_desc,
            sha=sha,
            min_legible=MIN_LEGIBLE_EM,
            min_target=MIN_TARGET_EM,
            p1_ranges=p1_ranges_repr,
        ))
        for cp, wr, hr, hc, vc in exact_rows:
            f.write(f"    0x{cp:04X}: ({wr}, {hr}, {hc}, {vc}),  # {chr(cp)}\n")
        f.write(MID)
        for cp, wr, hc, vc in min_h_rows:
            f.write(f"    0x{cp:04X}: ({wr}, {hc}, {vc}),  # {chr(cp)}\n")
        f.write(FOOTER)
    print(
        f"已產生 {out}：EXACT {len(exact_rows)} 字"
        f"（per-cp 量自 {source_desc}，sha256={sha[:12]}…）"
        f" + MIN_H {len(min_h_rows)} 字"
    )


def scan(originals_dir):
    """驗收掃描：production 對原版，P1/P2 報「高<MIN_LEGIBLE_EM 且原版≥ORIG_TALL_EM」flag。

    範圍 = P1 全區段 + P2 清單（由原版重推導，與表生成同一套邏輯）——**不是整個
    U+2100-218F block**：ℹ 等「對齊 Iosevka 屬正常」的小字（spec 不修名單）雖在
    block 內且高 <0.5em，但那是 Iosevka 自身的小設計，不是管線縮壞，不 flag。"""
    originals = _load_originals(originals_dir)
    min_h_rows = build_min_h_rows(originals)
    cps = [cp for lo, hi in P1_RANGES for cp in range(lo, hi + 1)]
    cps += [cp for cp, _, _, _ in min_h_rows]
    total_flags = 0
    try:
        orig_by_file = {fn: originals[label] for label, (fn, _) in ORIGINALS.items()}
        for fn in PRODUCTION_REGULARS:
            path = os.path.join(REPO_ROOT, FONTS_REL, fn)
            prod = TTFont(path)
            pc = prod.getBestCmap() or {}
            pu = prod["head"].unitsPerEm
            # Mango 原版缺整段（P1 由 Sarasa 植入），原版高以植入來源 Sarasa 為準。
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
    iosevka_path = None
    do_scan = False
    i = 0
    while i < len(args):
        if args[i] == "--originals-dir":
            originals_dir = args[i + 1]
            i += 2
        elif args[i] == "--iosevka":
            iosevka_path = args[i + 1]
            i += 2
        elif args[i] == "--scan":
            do_scan = True
            i += 1
        else:
            print(f"未知參數：{args[i]}（見 docstring）", file=sys.stderr)
            sys.exit(2)
    if do_scan:
        scan(originals_dir)
        return
    if iosevka_path is None:
        print("產表需要 --iosevka <IosevkaTerm-Regular.ttf>（EXACT 量測來源）", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(iosevka_path):
        print(f"檔案不存在：{iosevka_path}", file=sys.stderr)
        sys.exit(2)
    generate(originals_dir, iosevka_path)


if __name__ == "__main__":
    main()
