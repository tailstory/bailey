#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools>=4.50", "skia-pathops>=0.8", "resvg-py>=0.5"]
# ///
"""Build Bailey's ready-to-use art into dist/.

    uv run build.py

Source files link to each other with <use href="../parts/tail.svg#art">. Browsers follow those links only
over http, and GitHub's preview and design apps do not follow them at all, so this script follows them
instead. Every source file becomes one self-contained SVG per theme (plain filled paths, nothing linked),
PNGs at the sizes bailey.toml asks for, and a place on dist/sheet.svg.
"""
from __future__ import annotations

import math
import re
import shutil
import tomllib
import xml.etree.ElementTree as ET
from functools import cache
from pathlib import Path
from xml.sax.saxutils import escape

import pathops
import resvg_py
from fontTools.pens.basePen import decomposeQuadraticSegment
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path import parse_path

ROOT = Path(__file__).resolve().parent
SVG = "{http://www.w3.org/2000/svg}"
INK, GROUND = "ink", "ground"  # the colours a theme supplies
NOT_DRAWN = {"title", "desc", "defs", "mask", "symbol", "style", "metadata"}
BLACK = {"#000", "#000000", "black"}


class BuildError(SystemExit):
    """A source file is wrong in a way the person editing it can fix."""


# ---- 2x3 affine matrices as (a, b, c, d, e, f): x' = a*x + c*y + e, y' = b*x + d*y + f ----
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(m, n):
    a, b, c, d, e, f = m
    g, h, i, j, k, l = n
    return (a * g + c * h, b * g + d * h, a * i + c * j, b * i + d * j, a * k + c * l + e, b * k + d * l + f)


def parse_transform(text):
    m = IDENTITY
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = [float(x) for x in re.split(r"[\s,]+", args.strip()) if x]
        if name == "translate":
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            r = math.radians(v[0])
            t = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(v) == 3:
                t = mul(mul((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "matrix":
            t = tuple(v)
        else:
            raise BuildError(f"unsupported transform: {name}")
        m = mul(m, t)
    return m


def view_box(el):
    return tuple(float(v) for v in el.get("viewBox").split())


class Art:
    """One source file."""

    def __init__(self, path: Path):
        self.path = path
        self.key = path.relative_to(ROOT).with_suffix("").as_posix()  # "marks/t-peek"
        self.root = ET.parse(path).getroot()
        self.by_id = {el.get("id"): el for el in self.root.iter() if el.get("id")}
        self.title = self.root.findtext(SVG + "title") or self.key
        _, _, self.width, self.height = view_box(self.root)

    def follow(self, href: str) -> tuple[Art, ET.Element]:
        """The file and element a link such as "../parts/tail.svg#art" points at."""
        file, _, fragment = href.partition("#")
        target = (self.path.parent / file).resolve() if file else self.path
        if not target.is_file():
            raise BuildError(f"{self.key}: {href} points at a file that does not exist")
        art = load(target)
        element = art.by_id.get(fragment) if fragment else art.root
        if element is None:
            raise BuildError(f'{self.key}: {href} points at an id that {art.key} does not have')
        return art, element

    def links(self) -> list[str]:
        """Other files this one places, in order, once each."""
        found = []
        for el in self.root.iter(SVG + "use"):
            key = self.follow(el.get("href"))[0].key
            if key != self.key and key not in found:
                found.append(key)
        return found

    def paint(self, el, ctm, color) -> list[tuple[str, pathops.Path]]:
        """What one element draws: (fill, outline) pairs in paint order, every link followed."""
        tag = el.tag.removeprefix(SVG)
        if tag in NOT_DRAWN:
            return []
        ctm = mul(ctm, parse_transform(el.get("transform")))
        color = el.get("color", color)
        if el.get("mask") and tag != "g":
            raise BuildError(f"{self.key}: put mask= on a <g> around the art, not on <{tag}>")
        if tag == "g":
            drawn = [op for child in el for op in self.paint(child, ctm, color)]
            if el.get("mask"):
                mask = self.by_id.get(re.search(r"#([^)]+)", el.get("mask")).group(1))
                if mask is None:
                    raise BuildError(f'{self.key}: {el.get("mask")} is not in this file')
                cut = self.knock_out(mask, ctm)
                drawn = [(f, pathops.op(p, cut, pathops.PathOp.DIFFERENCE, fix_winding=True)) for f, p in drawn]
            return drawn
        if tag == "use":
            art, target = self.follow(el.get("href"))
            ctm = mul(ctm, (1, 0, 0, 1, float(el.get("x", 0)), float(el.get("y", 0))))
            if target.tag not in (SVG + "svg", SVG + "symbol"):
                return art.paint(target, ctm, color)
            _, _, vw, vh = view_box(target)
            w, h = float(el.get("width", vw)), float(el.get("height", vh))
            s = min(w / vw, h / vh)  # fit the whole drawing inside the box, centred
            ctm = mul(ctm, (s, 0, 0, s, (w - vw * s) / 2, (h - vh * s) / 2))
            return [op for child in target for op in art.paint(child, ctm, color)]
        fill = el.get("fill", "#000")
        if "ground" in el.get("class", "").split():
            fill = GROUND
        elif fill == "currentColor":
            fill = color
        outline = pathops.Path()
        pen = TransformPen(outline.getPen(), ctm)
        if tag == "path":
            parse_path(el.get("d"), pen)
        elif tag == "rect":
            round_rect(pen, *(float(el.get(k, 0)) for k in ("x", "y", "width", "height", "rx")))
        else:
            raise BuildError(f"{self.key}: <{tag}> is not supported; use <path>, <rect>, <use> or <g>")
        return [(fill, pathops.simplify(outline, fix_winding=True))]

    def knock_out(self, mask, ctm) -> pathops.Path:
        """Everything a mask paints black: the area it removes."""
        cut = pathops.Path()
        for child in mask:
            for fill, outline in self.paint(child, ctm, INK):
                if fill.lower() in BLACK:
                    cut = pathops.op(cut, outline, pathops.PathOp.UNION, fix_winding=True)
        return cut

    def flatten(self) -> list[tuple[str, pathops.Path]]:
        """The whole file as one outline per run of the same fill, in paint order."""
        layers: list[list] = []
        for child in self.root:
            for fill, outline in self.paint(child, IDENTITY, INK):
                if layers and layers[-1][0] == fill:
                    layers[-1][1] = pathops.op(layers[-1][1], outline, pathops.PathOp.UNION, fix_winding=True)
                else:
                    layers.append([fill, outline])
        return [(fill, outline) for fill, outline in layers]


@cache
def load(path: Path) -> Art:
    return Art(path)


def round_rect(pen, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    k = r * 0.5522847498
    pen.moveTo((x + r, y))
    pen.lineTo((x + w - r, y))
    if r:
        pen.curveTo((x + w - r + k, y), (x + w, y + r - k), (x + w, y + r))
    pen.lineTo((x + w, y + h - r))
    if r:
        pen.curveTo((x + w, y + h - r + k), (x + w - r + k, y + h), (x + w - r, y + h))
    pen.lineTo((x + r, y + h))
    if r:
        pen.curveTo((x + r - k, y + h), (x, y + h - r + k), (x, y + h - r))
    pen.lineTo((x, y + r))
    if r:
        pen.curveTo((x, y + r - k), (x + r - k, y), (x + r, y))
    pen.closePath()


def num(v, places=2):
    s = f"{v:.{places}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def path_d(outline: pathops.Path, places) -> str:
    out = []
    for verb, pts in outline.segments:
        if verb in ("moveTo", "lineTo"):
            out.append(("M" if verb == "moveTo" else "L") + " ".join(num(c, places) for c in pts[0]))
        elif verb == "curveTo":
            out.append("C" + " ".join(num(c, places) for pt in pts for c in pt))
        elif verb == "qCurveTo":  # pathops chains quadratics the way TrueType does: split them back up
            for control, end in decomposeQuadraticSegment(pts):
                out.append("Q" + " ".join(num(c, places) for c in (*control, *end)))
        elif verb in ("closePath", "endPath"):
            out.append("Z")
    return "".join(out)


def paths(art: Art, layers, theme) -> str:
    places = 2 if max(art.width, art.height) < 3000 else 1
    return "".join(f'<path fill="{theme.get(fill, fill)}" d="{path_d(outline, places)}"/>' for fill, outline in layers)


def standalone(art: Art, layers, theme) -> str:
    w, h = num(art.width), num(art.height)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
            f"<title>{escape(art.title)}</title>{paths(art, layers, theme)}</svg>\n")


def sheet(folders, arts, flat, themes) -> str:
    """Everything on one page: each file once, and anything with a ground once per theme."""
    page, row_art, paper, band, rule = 1400, 196, "#F5F7FB", "#E9ECF3", "#E3E7EF"
    sans = "system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
    mono = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
    first = next(iter(themes))
    out, y = [], 0

    def text(x, ty, s, size, fill, weight=400, anchor="start", family=sans, spacing=""):
        out.append(f'<text x="{num(x)}" y="{num(ty)}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
                   f'fill="{fill}" text-anchor="{anchor}"{spacing}>{escape(s)}</text>')

    text(48, 78, "Bailey", 44, themes[first][INK], 800)
    text(page - 48, 62, "Every file in this repo, built. Drawings live in parts/ and type/;", 13, "#50586a", anchor="end")
    text(page - 48, 82, "everything else only places them. Generated by build.py: do not edit.", 13, "#50586a", anchor="end")
    y = 120
    for folder, note in folders.items():
        cells = []
        for art in arts[folder]:
            themed = any(fill == GROUND for fill, _ in flat[art.key])
            cells += [(art, name) for name in (themes if themed else [first])]
        out.append(f'<rect x="0" y="{y}" width="{page}" height="48" fill="{band}"/>')
        text(40, y + 30, folder.upper(), 13, "#50586a", 700, spacing=' letter-spacing="1.6"')
        text(page - 40, y + 30, note, 12, "#50586a", anchor="end")
        y += 48
        columns = math.ceil(len(cells) / math.ceil(len(cells) / 5))
        cell_w = page / columns
        for start in range(0, len(cells), columns):
            row = cells[start:start + columns]
            scales = [min((cell_w - 68) / art.width, row_art / art.height) for art, _ in row]
            height = max(art.height * s for (art, _), s in zip(row, scales)) + 94
            for i, ((art, name), s) in enumerate(zip(row, scales)):
                x = i * cell_w
                if i:
                    out.append(f'<line x1="{num(x)}" y1="{num(y)}" x2="{num(x)}" y2="{num(y + height)}" stroke="{rule}"/>')
                ax, ay = x + (cell_w - art.width * s) / 2, y + 30 + (height - 94 - art.height * s) / 2
                out.append(f'<g transform="translate({num(ax)} {num(ay)}) scale({num(s, 5)})">{paths(art, flat[art.key], themes[name])}</g>')
                themed = len([c for c in cells if c[0] is art]) > 1
                text(x + 16, y + 22, name if themed else art.title.split(" · ")[0], 12, "#667085", 700)
                text(x + cell_w / 2, y + height - 34, art.key, 12, themes[first][INK], 600, "middle", mono)
                text(x + cell_w / 2, y + height - 16, " + ".join(art.links()) or "drawn here", 11.5, "#50586a", anchor="middle")
            y += height
            out.append(f'<line x1="0" y1="{num(y)}" x2="{page}" y2="{num(y)}" stroke="{rule}"/>')
    body = "\n".join(out)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {page} {num(y)}" width="{page}" height="{num(y)}">\n'
            f'<title>Bailey: every file, built</title>\n<rect width="{page}" height="{num(y)}" fill="{paper}"/>\n{body}\n</svg>\n')


def main():
    config = tomllib.loads((ROOT / "bailey.toml").read_text(encoding="utf-8"))
    folders, themes, png_sizes = config["folders"], config["themes"], config.get("png", {})
    arts = {folder: [load(p) for p in sorted((ROOT / folder).glob("*.svg"))] for folder in folders}
    flat = {art.key: art.flatten() for group in arts.values() for art in group}

    dist = ROOT / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    written = 0
    for name, theme in themes.items():
        for folder, group in arts.items():
            target = dist / name / folder
            target.mkdir(parents=True, exist_ok=True)
            for art in group:
                svg = standalone(art, flat[art.key], theme)
                (target / f"{art.path.stem}.svg").write_text(svg, encoding="utf-8")
                written += 1
                for size in png_sizes.get(folder, []):
                    scale = size / max(art.width, art.height)
                    png = resvg_py.svg_to_bytes(svg_string=svg, width=max(1, round(art.width * scale)),
                                                height=max(1, round(art.height * scale)))
                    (target / f"{art.path.stem}-{size}.png").write_bytes(bytes(png))
                    written += 1
    (dist / "sheet.svg").write_text(sheet(folders, arts, flat, themes), encoding="utf-8")
    print(f"built {sum(len(g) for g in arts.values())} source files in {len(themes)} themes: {written + 1} files in dist/")


if __name__ == "__main__":
    main()
