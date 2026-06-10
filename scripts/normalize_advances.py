#!/usr/bin/env python3
"""
統一 TTF 字型的 advance width，對齊 terminal East Asian Width 分類。

Terminal emulator 用 Unicode EAW 屬性決定字元佔幾格：
  W/F → 2 格、H/Na/N/A → 1 格
字型的 advance width 必須與此一致，否則會跑掉。

用法：python3 normalize_advances.py <font.ttf> [font2.ttf ...]
"""

import os
import sys
import unicodedata
from fontTools.ttLib import TTFont

EAW_WIDE = {"W", "F"}
EAW_NARROW = {"H", "Na", "N", "A"}


def normalize_advances(font):
    """依 Unicode EAW 屬性正規化所有 glyph 的 advance width。
    跳過 ASCII (U+0000-U+007F)。只改 advance，不動 lsb。
    回傳修改的 glyph 數量。
    """
    cmap = font.getBestCmap()
    if not cmap:
        return 0

    hmtx = font["hmtx"]
    m_glyph = cmap.get(0x4D)
    if m_glyph is None:
        print("  警告：找不到 U+004D 'M'，跳過", file=sys.stderr)
        return 0

    latin_advance = hmtx[m_glyph][0]
    wide_advance = latin_advance * 2

    print(f"  Latin 基準 advance（M）= {latin_advance}")
    print(f"  Wide 目標 advance = {wide_advance}")

    modified = 0
    for cp, glyph_name in sorted(cmap.items()):
        if cp <= 0x7F:
            continue

        eaw = unicodedata.east_asian_width(chr(cp))
        if eaw in EAW_WIDE:
            target = wide_advance
        elif eaw in EAW_NARROW:
            target = latin_advance
        else:
            continue

        current_advance, lsb = hmtx[glyph_name]
        if current_advance != target:
            hmtx[glyph_name] = (target, lsb)
            modified += 1

    return modified


def normalize_file(path):
    """開啟 TTF、正規化 advance width、原地覆寫。"""
    print(f"處理：{path}")
    font = TTFont(path)
    modified = normalize_advances(font)
    if modified:
        font.save(path)
        print(f"  修改 {modified} 個 glyph，已儲存")
    else:
        print(f"  無需修改，跳過")
    font.close()
    return modified


def main():
    if len(sys.argv) < 2:
        print(f"用法：{sys.argv[0]} <font.ttf> [font2.ttf ...]")
        sys.exit(1)
    for path in sys.argv[1:]:
        if not os.path.exists(path):
            print(f"檔案不存在：{path}", file=sys.stderr)
            sys.exit(1)
        normalize_file(path)


if __name__ == "__main__":
    main()
