#!/usr/bin/env python3
"""把 .docx 逆成可读的结构化文本（段落样式 / 表格 / 图片清单）。

WHY 只用标准库：docx 本质是 zip + OOXML，``zipfile`` 与 ``ElementTree`` 足以取出
正文与结构，不引入三方依赖；这样在任意机器上都能先看清模板再决定后续渲染方案。

用法::

    python scripts/extract_docx.py <input.docx> [-o output.txt] [--max-images 40]

输出约定（便于人工与后续程序消费）：

- 段落：``<style>|text``，style 为空时表示正文 Normal；
- 表格：以 ``TBL`` 开头，单元格用 ``|`` 分隔，``ROW`` 每行一条；
- 图片：``IMG|<zip 内路径>|<字节数>|<在正文中的出现序号>``；
- 分节：``SEC`` 行给出页面尺寸与方向。
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_NS = {"w": _W[1:-1], "r": _R[1:-1], "a": _A[1:-1], "wp": _WP[1:-1]}

# 段落里的「非文字内容」：图片、分页符、文本框回退内容等，都要显式标出来，
# 否则逆向模板时会漏掉「这一节必须配一张现场照片」这类隐含要求。
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".emf", ".wmf", ".svg")


def _paragraph_text(p: ET.Element) -> str:
    """取段落全部可见文本（含 ``w:t`` 与制表符，忽略删除修订 ``w:delText``）。"""
    parts: list[str] = []
    for node in p.iter():
        tag = node.tag
        if tag == f"{_W}t":
            parts.append(node.text or "")
        elif tag == f"{_W}tab":
            parts.append("\t")
        elif tag == f"{_W}br":
            parts.append(" ")
    return "".join(parts).strip()


def _paragraph_style(p: ET.Element) -> str:
    ppr = p.find(f"{_W}pPr")
    if ppr is None:
        return ""
    pstyle = ppr.find(f"{_W}pStyle")
    if pstyle is None:
        return ""
    return pstyle.get(f"{_W}val") or ""


def _paragraph_images(p: ET.Element, rels: dict[str, str]) -> list[str]:
    """返回本段落引用的图片在 zip 内的路径。"""
    found: list[str] = []
    for blip in p.iter(f"{_A}blip"):
        embed = blip.get(f"{_R}embed") or blip.get(f"{_R}link")
        if not embed:
            continue
        target = rels.get(embed)
        if target:
            found.append(target)
    return found


def _has_page_break(p: ET.Element) -> bool:
    return p.find(f".//{_W}br[@{_W}type='page']") is not None or (
        p.find(f"{_W}pPr/{_W}pageBreakBefore") is not None
    )


def _load_rels(zf: zipfile.ZipFile) -> dict[str, str]:
    """读 document.xml.rels，建立 rId → 媒体路径 的映射。"""
    try:
        raw = zf.read("word/_rels/document.xml.rels")
    except KeyError:
        return {}
    root = ET.fromstring(raw)
    rels: dict[str, str] = {}
    for rel in root:
        rid = rel.get("Id")
        target = rel.get("Target")
        if not rid or not target:
            continue
        if not target.startswith("media/"):
            target = "word/" + target.lstrip("/")
        else:
            target = "word/" + target
        rels[rid] = target
    return rels


def _iter_block_elements(body: ET.Element):
    """按文档顺序产出块级元素（段落或表格），保持正文次序。"""
    for child in body:
        if child.tag == f"{_W}p":
            yield "p", child
        elif child.tag == f"{_W}tbl":
            yield "tbl", child


def _table_rows(tbl: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in tbl.findall(f"{_W}tr"):
        cells: list[str] = []
        for tc in tr.findall(f"{_W}tc"):
            texts = [_paragraph_text(p) for p in tc.findall(f"{_W}p")]
            cells.append(" ".join(t for t in texts if t).strip())
        rows.append(cells)
    return rows


def _sections(root: ET.Element) -> list[str]:
    out: list[str] = []
    for sect in root.iter(f"{_W}sectPr"):
        pg = sect.find(f"{_W}pgSz")
        if pg is None:
            continue
        w = pg.get(f"{_W}w")
        h = pg.get(f"{_W}h")
        orient = pg.get(f"{_W}orient") or "portrait"
        out.append(f"SEC|size={w}x{h}|orient={orient}")
    return out


def extract(path: Path, *, max_images: int = 40) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    if zipfile.is_zipfile(path) is False:
        raise ValueError(f"不是有效的 .docx（zip）文件：{path}")

    lines: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if "word/document.xml" not in names:
            raise ValueError("缺少 word/document.xml，不是 Word 文档")

        rels = _load_rels(zf)
        medias = sorted(n for n in names if n.startswith("word/media/"))
        sizes = {n: zf.getinfo(n).file_size for n in medias}

        root = ET.fromstring(zf.read("word/document.xml"))
        body = root.find(f"{_W}body")
        if body is None:
            raise ValueError("document.xml 缺少 body")

        lines.append(f"FILE|{path.name}")
        lines.append(f"MEDIA_COUNT|{len(medias)}")
        lines.append(f"MEDIA_BYTES|{sum(sizes.values())}")
        for line in _sections(root):
            lines.append(line)
        lines.append("")

        img_seq = 0
        for kind, node in _iter_block_elements(body):
            if kind == "p":
                style = _paragraph_style(node)
                text = _paragraph_text(node)
                for target in _paragraph_images(node, rels):
                    img_seq += 1
                    if img_seq <= max_images:
                        lines.append(f"IMG|{target}|{sizes.get(target, 0)}|#{img_seq}")
                if _has_page_break(node):
                    lines.append("PAGEBREAK")
                if text or style:
                    lines.append(f"{style}|{text}")
            else:
                rows = _table_rows(node)
                lines.append(f"TBL|rows={len(rows)}")
                for cells in rows:
                    lines.append("ROW|" + " | ".join(cells))
                lines.append("TBLEND")
        lines.append("")
        lines.append("--- 媒体清单 ---")
        for n in medias:
            lines.append(f"MEDIA|{n}|{sizes[n]}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 .docx 逆成结构化文本")
    parser.add_argument("input", type=Path, help="输入 .docx 路径")
    parser.add_argument("-o", "--output", type=Path, default=None, help="输出文件路径；缺省打印到标准输出")
    parser.add_argument("--max-images", type=int, default=40, help="正文里最多标注多少张图（默认 40）")
    args = parser.parse_args(argv)

    try:
        text = extract(args.input, max_images=args.max_images)
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"提取失败：{exc}", file=sys.stderr)
        return 1

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"已写出：{args.output}（{len(text.splitlines())} 行）")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
