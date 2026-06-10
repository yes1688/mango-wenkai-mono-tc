#!/usr/bin/env python3
"""
把字型 re-tag 成指定 family 的某個 RIBBI style 成員（改 name/OS2/head metadata，
不動 glyph/cmap/advance）。

動機：LXGW WenKai Mono TC 上游沒有 Bold 字重（最重只到 Medium 500），Boss 拍板用
Medium 頂替當 bold。Medium 原始 metadata 是「獨立 family」（nameID1=
'LXGW WenKai Mono TC Medium'、usWeightClass=500、無 macStyle bold bit），fontdb
會把它當成另一個家族、不會在請求 Weight::BOLD 時命中。本腳本把它 re-tag 成
Regular 的 RIBBI Bold 對：nameID1 改回主 family、subfamily=Bold、usWeightClass=700、
macStyle/fsSelection bold bit、移除 typographic nameID16/17 → 與該 family 的 Regular
共用 nameID1，fontdb 即以 weight 匹配選到此 face（與 Sarasa 真 Bold 同掛法）。

只改 metadata、不碰輪廓 → 不影響共同管線（strip/normalize/complete/fit/align）算出的
advance 與符號對齊。re-tag 應在共同管線之後跑（順序與 advance/glyph 無相依，但放最後
最直覺）。

用法：
  python3 retag_weight.py <font.ttf> --family "LXGW WenKai Mono TC" \
      --weight 700 --subfamily Bold

支援的 subfamily（RIBBI）：Regular / Bold / Italic / Bold Italic。bold/italic 的
macStyle 與 fsSelection bit 依 subfamily 名自動設定。
"""

import argparse
import os
import sys

from fontTools.ttLib import TTFont

# fsSelection bit（OpenType spec）：bit0 ITALIC、bit5 BOLD、bit6 REGULAR。
FS_ITALIC = 0x0001
FS_BOLD = 0x0020
FS_REGULAR = 0x0040
# head.macStyle bit：bit0 Bold、bit1 Italic。
MAC_BOLD = 0x01
MAC_ITALIC = 0x02

# 設這兩個 platform 的 name record，涵蓋 Windows(3,1,0x409) 與 Mac(1,0,0)。
_WIN = (3, 1, 0x409)
_MAC = (1, 0, 0)


def _postscript_name(family, subfamily):
    """組 nameID6（PostScript name）：family 去空白 + '-' + subfamily 去空白。"""
    return f"{family.replace(' ', '')}-{subfamily.replace(' ', '')}"


def _full_name(family, subfamily):
    """組 nameID4（full name）：Regular 只用 family，其餘 'family subfamily'。"""
    return family if subfamily == "Regular" else f"{family} {subfamily}"


def retag(font, family, weight, subfamily):
    """把 font re-tag 成 family 的 RIBBI `subfamily` 成員（in-place，不存檔）。

    改動：nameID 1/2/4/6 + 移除 typographic 16/17 + OS/2.usWeightClass +
    OS/2.fsSelection + head.macStyle 的 bold/italic bit。
    """
    is_bold = "bold" in subfamily.lower()
    is_italic = "italic" in subfamily.lower()

    name = font["name"]

    def setname(nid, val):
        name.setName(val, nid, *_WIN)
        name.setName(val, nid, *_MAC)

    setname(1, family)
    setname(2, subfamily)
    setname(4, _full_name(family, subfamily))
    setname(6, _postscript_name(family, subfamily))
    # 移除 typographic family/subfamily，塌縮成純 RIBBI（與只有 nameID1 的 Regular 對齊）。
    name.removeNames(nameID=16)
    name.removeNames(nameID=17)

    os2 = font["OS/2"]
    os2.usWeightClass = weight
    fs = os2.fsSelection & ~(FS_ITALIC | FS_BOLD | FS_REGULAR)
    if is_bold:
        fs |= FS_BOLD
    if is_italic:
        fs |= FS_ITALIC
    if not (is_bold or is_italic):
        fs |= FS_REGULAR
    os2.fsSelection = fs

    head = font["head"]
    ms = head.macStyle & ~(MAC_BOLD | MAC_ITALIC)
    if is_bold:
        ms |= MAC_BOLD
    if is_italic:
        ms |= MAC_ITALIC
    head.macStyle = ms


def retag_file(path, family, weight, subfamily):
    print(f"處理：{path}")
    font = TTFont(path)
    retag(font, family, weight, subfamily)
    font.save(path)
    print(
        f"  re-tag → family='{family}' subfamily='{subfamily}' "
        f"usWeightClass={weight} macStyle={font['head'].macStyle}，已儲存"
    )
    font.close()


def main():
    ap = argparse.ArgumentParser(description="re-tag 字型為指定 family 的 RIBBI style 成員")
    ap.add_argument("fonts", nargs="+", help="要 re-tag 的 .ttf（原地覆寫）")
    ap.add_argument("--family", required=True, help="目標 family（nameID1，須與 Regular 一致）")
    ap.add_argument("--weight", required=True, type=int, help="usWeightClass（如 700）")
    ap.add_argument(
        "--subfamily", required=True,
        choices=["Regular", "Bold", "Italic", "Bold Italic"],
        help="RIBBI subfamily",
    )
    args = ap.parse_args()
    for path in args.fonts:
        if not os.path.exists(path):
            print(f"檔案不存在：{path}", file=sys.stderr)
            sys.exit(1)
        retag_file(path, args.family, args.weight, args.subfamily)


if __name__ == "__main__":
    main()
