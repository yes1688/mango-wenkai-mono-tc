#!/usr/bin/env python3
"""
移除 TTF 字型的 GSUB `calt`（Contextual Alternates）feature，並垃圾回收其專屬
lookup。可單獨執行，也可被 build-font.py / rebuild-fonts.sh import 使用。

為什麼需要這支（strip_ligatures 之外）：
  strip_ligatures.py 只清 GSUB LookupType-4（Ligature Substitution）。但等寬連字
  （`::`→箭頭、`->`、`!=`…）主要不是 type4，而是 `calt` feature 觸發的
  Type-6（Chaining Context）→ Type-1（Single）替換。終端字型不該有任何 contextual
  替換——兩個字元被換成一個寬字元會破壞等寬格線。

做法（GC 法，精準且三字型通用，不暴力清 type6/type1）：
  1. 從 FeatureList 移除所有 `calt` FeatureRecord，並同步修正 ScriptList 裡各
     LangSys 的 FeatureIndex / ReqFeatureIndex（feature 移除後 index 會位移）。
  2. 以「移除 calt 後、仍被任何 feature 觸達（含 chaining 鏈到的 single）」算 lookup
     可達閉包，回收不可達的孤兒 lookup，壓實 LookupList 並重映射所有 index 引用。

  關鍵：calt 與其他 feature **共用**的 lookup 不會被砍（例：Sarasa 的 calt 閉包多數
  與 dlig/script-specific feature 共用，只有少數是 calt 獨佔）。共用者仍被保留的
  feature 觸達 → 留在可達閉包內 → 不回收。功能上 calt FeatureRecord 一拿掉，shaper
  預設就不再跑連字；殘留的共用 lookup 只剩 default-off feature 會用到，終端不觸發。

用法：python3 strip_calt.py <font.ttf> [font2.ttf ...]
"""

import os
import sys

from fontTools.ttLib import TTFont

NO_REQ_FEATURE = 0xFFFF  # LangSys.ReqFeatureIndex「無必要 feature」哨兵


def _subst_records(lookup):
    """yield 一個 lookup 內所有 SubstLookupRecord（自動解開 Type-7 Extension 包裝）。
    只有 Context(5)/Chaining(6) 子表帶 SubstLookupRecord，藉它鏈到其他 lookup。"""
    for sub in getattr(lookup, "SubTable", []) or []:
        ext = getattr(sub, "ExtSubTable", None)
        target = ext if ext is not None else sub
        for rec in getattr(target, "SubstLookupRecord", []) or []:
            yield rec


def calt_feature_count(font):
    """回傳 GSUB FeatureList 中 `calt` FeatureRecord 的數量（0 = 已無 calt）。"""
    if "GSUB" not in font:
        return 0
    gsub = font["GSUB"].table
    if gsub.FeatureList is None:
        return 0
    return sum(1 for fr in gsub.FeatureList.FeatureRecord if fr.FeatureTag == "calt")


def _remap_langsys_features(gsub, feat_index_map):
    """feature 移除後，修正 ScriptList 各 LangSys 的 FeatureIndex / ReqFeatureIndex。
    feat_index_map：保留 feature 的 old→new index；被移除者不在 map 內。"""
    if gsub.ScriptList is None:
        return
    for sr in gsub.ScriptList.ScriptRecord:
        script = sr.Script
        langsyses = []
        if script.DefaultLangSys is not None:
            langsyses.append(script.DefaultLangSys)
        for lsr in getattr(script, "LangSysRecord", []) or []:
            langsyses.append(lsr.LangSys)
        for ls in langsyses:
            ls.FeatureIndex = [
                feat_index_map[i] for i in ls.FeatureIndex if i in feat_index_map
            ]
            ls.FeatureCount = len(ls.FeatureIndex)
            req = getattr(ls, "ReqFeatureIndex", NO_REQ_FEATURE)
            if req != NO_REQ_FEATURE:
                ls.ReqFeatureIndex = feat_index_map.get(req, NO_REQ_FEATURE)


def _reachable_lookups(lookup_list, seed_indices):
    """從 seed lookup 出發，沿 SubstLookupRecord 算可達閉包（含 chaining 鏈到的 single）。"""
    reachable = set()
    stack = list(seed_indices)
    while stack:
        idx = stack.pop()
        if idx in reachable:
            continue
        reachable.add(idx)
        for rec in _subst_records(lookup_list.Lookup[idx]):
            stack.append(rec.LookupListIndex)
    return reachable


def _remap_lookup_refs(lookup_list, features, lookup_index_map):
    """lookup 壓實後，重映射所有 LookupListIndex 引用：
    保留 feature 的 Feature.LookupListIndex + 保留 lookup 的 SubstLookupRecord。"""
    for feat in features:
        feat.LookupListIndex = [lookup_index_map[i] for i in feat.LookupListIndex]
        feat.LookupCount = len(feat.LookupListIndex)
    for lookup in lookup_list.Lookup:
        for rec in _subst_records(lookup):
            rec.LookupListIndex = lookup_index_map[rec.LookupListIndex]


def strip_calt(font):
    """移除 font GSUB 的 `calt` feature + GC 其專屬 lookup。
    回傳 (removed_features, removed_lookups)。"""
    if "GSUB" not in font:
        return 0, 0
    gsub = font["GSUB"].table
    feature_list = gsub.FeatureList
    lookup_list = gsub.LookupList
    if feature_list is None or lookup_list is None:
        return 0, 0

    old_features = list(feature_list.FeatureRecord)
    calt_feat_indices = {
        i for i, fr in enumerate(old_features) if fr.FeatureTag == "calt"
    }
    if not calt_feat_indices:
        return 0, 0

    # ── 1. 算「只回收 calt 獨佔」的 lookup（移除 calt feature 前算閉包）─────────────
    # removable = calt 可達閉包 − 其他 feature 可達閉包。如此精準鎖定 calt 的專屬
    # lookup：與 dlig 等共用者落在非 calt 閉包內 → 保留；既有孤兒（從不被任何 feature
    # 引用的死 lookup）兩個閉包都不含 → 不在 removable，原封不動（surgical，不掃別人的
    # dead code）。前提：removable 不被任何保留 lookup 引用（同字型已驗 orphan→calt=0），
    # 否則下方 remap 會以 KeyError fail fast。
    calt_seeds, noncalt_seeds = set(), set()
    for i, fr in enumerate(old_features):
        seeds = calt_seeds if i in calt_feat_indices else noncalt_seeds
        seeds.update(fr.Feature.LookupListIndex)
    removable = _reachable_lookups(lookup_list, calt_seeds) - _reachable_lookups(
        lookup_list, noncalt_seeds
    )

    # ── 2. 移除 calt FeatureRecord，建 feature old→new index map ──────────────
    feat_index_map = {}
    new_i = 0
    for old_i in range(len(old_features)):
        if old_i not in calt_feat_indices:
            feat_index_map[old_i] = new_i
            new_i += 1
    feature_list.FeatureRecord = [
        fr for i, fr in enumerate(old_features) if i not in calt_feat_indices
    ]
    feature_list.FeatureCount = len(feature_list.FeatureRecord)
    _remap_langsys_features(gsub, feat_index_map)

    # ── 3. 移除 removable lookup，壓實 LookupList 並重映射所有 index 引用 ──────────
    kept_features = [fr.Feature for fr in feature_list.FeatureRecord]
    old_lookups = list(lookup_list.Lookup)
    lookup_index_map = {}
    new_i = 0
    for old_i in range(len(old_lookups)):
        if old_i not in removable:
            lookup_index_map[old_i] = new_i
            new_i += 1
    lookup_list.Lookup = [l for i, l in enumerate(old_lookups) if i not in removable]
    lookup_list.LookupCount = len(lookup_list.Lookup)
    _remap_lookup_refs(lookup_list, kept_features, lookup_index_map)

    return len(calt_feat_indices), len(removable)


def strip_file(path):
    """開啟 TTF、strip calt、原地覆寫。"""
    print(f"處理：{path}")
    font = TTFont(path)
    feats, lookups = strip_calt(font)
    if feats:
        font.save(path)
        print(f"  移除 {feats} 個 calt feature、回收 {lookups} 個 calt 獨佔 lookup，已儲存")
    else:
        print(f"  無 calt feature，跳過")
    font.close()
    return feats, lookups


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
