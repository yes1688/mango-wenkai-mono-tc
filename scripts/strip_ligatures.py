#!/usr/bin/env python3
"""
移除 TTF 字型的 GSUB Ligature Substitution（LookupType 4）lookup。
可單獨執行，也可被 build-font.py import 使用。

用法：python3 strip-ligatures.py <font.ttf> [font2.ttf ...]
"""

import os
import sys
from fontTools.ttLib import TTFont


def strip_ligature_lookups(font):
    """移除 font 的 GSUB table 中所有 LookupType 4（Ligature Substitution）lookup。
    同時清理 FeatureList 中指向被移除 lookup 的 index。
    回傳移除的 lookup 數量。
    """
    if "GSUB" not in font:
        return 0

    gsub = font["GSUB"].table
    lookup_list = gsub.LookupList
    if lookup_list is None:
        return 0

    old_lookups = list(lookup_list.Lookup)
    lig_indices = {i for i, l in enumerate(old_lookups) if l.LookupType == 4}
    if not lig_indices:
        return 0

    index_map = {}
    new_idx = 0
    for old_idx in range(len(old_lookups)):
        if old_idx not in lig_indices:
            index_map[old_idx] = new_idx
            new_idx += 1

    lookup_list.Lookup = [l for i, l in enumerate(old_lookups) if i not in lig_indices]
    lookup_list.LookupCount = len(lookup_list.Lookup)

    if gsub.FeatureList:
        for feat in gsub.FeatureList.FeatureRecord:
            old_indices = feat.Feature.LookupListIndex
            feat.Feature.LookupListIndex = [
                index_map[i] for i in old_indices if i in index_map
            ]
            feat.Feature.LookupCount = len(feat.Feature.LookupListIndex)

    for lookup in lookup_list.Lookup:
        if hasattr(lookup, "SubTable"):
            for sub in lookup.SubTable:
                if hasattr(sub, "SubstLookupRecord"):
                    for rec in sub.SubstLookupRecord:
                        if rec.LookupListIndex in index_map:
                            rec.LookupListIndex = index_map[rec.LookupListIndex]
                if hasattr(sub, "BacktrackLookupRecord"):
                    for rec in sub.BacktrackLookupRecord:
                        if rec.LookupListIndex in index_map:
                            rec.LookupListIndex = index_map[rec.LookupListIndex]
                if hasattr(sub, "LookAheadLookupRecord"):
                    for rec in sub.LookAheadLookupRecord:
                        if rec.LookupListIndex in index_map:
                            rec.LookupListIndex = index_map[rec.LookupListIndex]

    return len(lig_indices)


def strip_file(path):
    """開啟 TTF、strip ligature、原地覆寫。"""
    print(f"處理：{path}")
    font = TTFont(path)
    removed = strip_ligature_lookups(font)
    if removed:
        font.save(path)
        print(f"  移除 {removed} 個 Ligature lookup，已儲存")
    else:
        print(f"  無 Ligature lookup，跳過")
    font.close()
    return removed


def main():
    if len(sys.argv) < 2:
        print(f"用法：{sys.argv[0]} <font.ttf> [font2.ttf ...]")
        sys.exit(1)
    for path in sys.argv[1:]:
        if not os.path.exists(path):
            print(f"檔案不存在：{path}", file=sys.stderr)
            sys.exit(1)
        strip_file(path)


if __name__ == "__main__":
    main()
