#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.15"
# dependencies = ["fonttools>=4.50", "skia-pathops>=0.8", "resvg-py>=0.5"]
# ///
"""Follow the links between the source files and write self-contained art into dist/."""

import math
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from enum import StrEnum, auto
from functools import cache, cached_property, reduce
from itertools import batched, groupby, product
from pathlib import Path
from shutil import rmtree
from string.templatelib import Interpolation, Template
from typing import NamedTuple, Self
from xml.sax.saxutils import escape

lazy import pathops
lazy import resvg_py
lazy from fontTools.pens.basePen import decomposeQuadraticSegment
lazy from fontTools.pens.transformPen import TransformPen
lazy from fontTools.svgLib.path import parse_path

ROOT = Path(__file__).resolve().parent
SVG = "{http://www.w3.org/2000/svg}"
NOT_DRAWN = frozenset({"title", "desc", "defs", "mask", "symbol", "style", "metadata"})
BLACK = frozenset({"#000", "#000000", "black"})

type Layer = tuple[str, pathops.Path]
type Point = tuple[float, float]
type Points = tuple[Point, ...]
type Theme = dict[str, str]


class Role(StrEnum):
    """The colours a theme supplies."""

    INK = auto()
    GROUND = auto()


class BuildError(SystemExit):
    """A source file needs fixing."""


class Affine(NamedTuple):
    """x' = a*x + c*y + e, y' = b*x + d*y + f."""

    a: float = 1
    b: float = 0
    c: float = 0
    d: float = 1
    e: float = 0
    f: float = 0

    def __matmul__(self, o: Self) -> Self:
        return Affine(
            self.a * o.a + self.c * o.b,
            self.b * o.a + self.d * o.b,
            self.a * o.c + self.c * o.d,
            self.b * o.c + self.d * o.d,
            self.a * o.e + self.c * o.f + self.e,
            self.b * o.e + self.d * o.f + self.f,
        )

    @classmethod
    def parse(cls, transform: str | None) -> Self:
        matrix = cls()
        for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", transform or ""):
            match name, [float(v) for v in re.split(r"[\s,]+", args.strip()) if v]:
                case "translate", [x, *y]:
                    matrix @= cls(e=x, f=y[0] if y else 0)
                case "scale", [x, *y]:
                    matrix @= cls(a=x, d=y[0] if y else x)
                case "rotate", [degrees, *pivot]:
                    r = math.radians(degrees)
                    turn = cls(math.cos(r), math.sin(r), -math.sin(r), math.cos(r))
                    if pivot:
                        turn = cls(e=pivot[0], f=pivot[1]) @ turn @ cls(e=-pivot[0], f=-pivot[1])
                    matrix @= turn
                case "matrix", values:
                    matrix @= cls(*values)
                case _:
                    raise BuildError(f"unsupported transform: {name}")
        return matrix


IDENTITY = Affine()


def union(one: pathops.Path, two: pathops.Path) -> pathops.Path:
    return pathops.op(one, two, pathops.PathOp.UNION, fix_winding=True)


def difference(one: pathops.Path, two: pathops.Path) -> pathops.Path:
    return pathops.op(one, two, pathops.PathOp.DIFFERENCE, fix_winding=True)


def view_box(el: ET.Element) -> list[float]:
    return [float(v) for v in el.get("viewBox").split()]


class Art:
    """One source file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.key = path.relative_to(ROOT).with_suffix("").as_posix()
        self.root = ET.parse(path).getroot()
        self.ids = {el.get("id"): el for el in self.root.iter() if el.get("id")}
        self.title = self.root.findtext(f"{SVG}title") or self.key
        *_, self.width, self.height = view_box(self.root)

    def follow(self, href: str) -> tuple[Self, ET.Element]:
        file, _, fragment = href.partition("#")
        path = (self.path.parent / file).resolve() if file else self.path
        if not path.is_file():
            raise BuildError(f"{self.key}: {href} points at a file that does not exist")
        art = load(path)
        if (target := art.ids.get(fragment) if fragment else art.root) is None:
            raise BuildError(f"{self.key}: {href} points at an id that {art.key} does not have")
        return art, target

    def links(self) -> list[str]:
        keys = (self.follow(use.get("href"))[0].key for use in self.root.iter(f"{SVG}use"))
        return list(dict.fromkeys(key for key in keys if key != self.key))

    def paint(self, el: ET.Element, ctm: Affine = IDENTITY, color: str = Role.INK, fill: str = "#000") -> list[Layer]:
        tag = el.tag.removeprefix(SVG)
        if tag in NOT_DRAWN:
            return []
        ctm @= Affine.parse(el.get("transform"))
        color, fill = el.get("color", color), el.get("fill", fill)
        mask = el.get("mask")
        if mask and tag != "g":
            raise BuildError(f"{self.key}: put mask= on a <g> around the art, not on <{tag}>")
        match tag:
            case "g":
                drawn = [*self.paint(child, ctm, color, fill) for child in el]
                if mask:
                    if (shape := self.ids.get(mask.removeprefix("url(#").removesuffix(")"))) is None:
                        raise BuildError(f"{self.key}: {mask} is not in this file")
                    masked = [*self.paint(child, ctm, fill=fill) for child in shape]
                    hole = reduce(union, (cut for fill, cut in masked if fill.lower() in BLACK), pathops.Path())
                    drawn = [(fill, difference(outline, hole)) for fill, outline in drawn]
                return drawn
            case "use":
                art, target = self.follow(el.get("href"))
                ctm @= Affine(e=float(el.get("x", 0)), f=float(el.get("y", 0)))
                if target.tag.removeprefix(SVG) not in ("svg", "symbol"):
                    return art.paint(target, ctm, color, fill)
                fill = target.get("fill", fill)
                *_, vw, vh = view_box(target)
                w, h = float(el.get("width", vw)), float(el.get("height", vh))
                s = min(w / vw, h / vh)
                ctm @= Affine(a=s, d=s, e=(w - vw * s) / 2, f=(h - vh * s) / 2)
                return [*art.paint(child, ctm, color, fill) for child in target]
            case "path" | "rect":
                if "ground" in el.get("class", "").split():
                    fill = Role.GROUND
                outline = pathops.Path()
                pen = TransformPen(outline.getPen(), ctm)
                if tag == "path":
                    parse_path(el.get("d"), pen)
                else:
                    round_rect(pen, *(float(el.get(k, 0)) for k in ("x", "y", "width", "height", "rx")))
                return [(color if fill == "currentColor" else fill, pathops.simplify(outline, fix_winding=True))]
            case _:
                raise BuildError(f"{self.key}: <{tag}> is not supported; use <path>, <rect>, <use> or <g>")

    @cached_property
    def layers(self) -> list[Layer]:
        """One outline per run of the same fill, in paint order."""
        drawn = [*self.paint(child, fill=self.root.get("fill", "#000")) for child in self.root]
        return [(fill, reduce(union, (o for _, o in run))) for fill, run in groupby(drawn, lambda layer: layer[0])]

    @property
    def has_ground(self) -> bool:
        return any(fill == Role.GROUND for fill, _ in self.layers)


@cache
def load(path: Path) -> Art:
    return Art(path)


def round_rect(pen: TransformPen, x: float, y: float, w: float, h: float, r: float) -> None:
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


def num(value: float, places: int = 2) -> str:
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def xml(template: Template) -> str:
    """Render a t-string as markup: values are escaped unless marked :raw."""
    out = []
    for part in template:
        match part:
            case str():
                out.append(part)
            case Interpolation(value=value, format_spec="raw"):
                out.append(value)
            case Interpolation(value=float() as value):
                out.append(num(value))
            case Interpolation(value=value):
                out.append(escape(str(value), {'"': "&quot;"}))
    return "".join(out)


def rejoin(start: Point, first: Points, second: Points, slack: float = 0.01) -> Points | None:
    """The cubic that two cubics are the halves of, if they are."""
    a0, a1, a2, a3, b1, b2, b3 = (complex(*point) for point in (start, *first, *second))
    if not (span := abs(b1 - a2)) or not 0.02 < (t := abs(a3 - a2) / span) < 0.98:
        return None
    p1, p2 = a0 + (a1 - a0) / t, b3 + (b2 - b3) / (1 - t)
    middle = p1 + (p2 - p1) * t
    r0, r1 = a1 + (middle - a1) * t, middle + (b2 - middle) * t
    if max(abs(r0 - a2), abs(r1 - b1), abs(r0 + (r1 - r0) * t - a3)) > slack:
        return None
    return (p1.real, p1.imag), (p2.real, p2.imag), second[-1]


def segments(outline: pathops.Path) -> list[tuple[str, Points]]:
    """The outline, with the cubics that pathops cut in two put back together."""
    out, start, at = [], None, None
    for verb, points in outline.segments:
        if verb == "curveTo" and out and out[-1][0] == verb and (whole := rejoin(start, out[-1][1], points)):
            out[-1] = (verb, whole)
        else:
            start = at
            out.append((verb, points))
        at = points[-1] if points else at
    return out


def commands(outline: pathops.Path, places: int) -> Iterator[str]:
    def points(*pairs: Point) -> str:
        return " ".join(f"{num(x, places)},{num(y, places)}" for x, y in pairs)

    for verb, pairs in segments(outline):
        match verb:
            case "moveTo":
                yield f"M{points(*pairs)}"
            case "lineTo":
                yield f"L{points(*pairs)}"
            case "curveTo":
                yield f"C{points(*pairs)}"
            case "qCurveTo":
                yield from (f"Q{points(*quad)}" for quad in decomposeQuadraticSegment(pairs))
            case "closePath" | "endPath":
                yield "Z"


def path_d(outline: pathops.Path, places: int, indent: str, width: int = 100) -> str:
    """Each subpath starts a line and wraps between commands."""
    lines = []
    for command in commands(outline, places):
        if command.startswith("M"):
            lines.append(indent + command)
        elif len(lines[-1]) + len(command) < width:
            lines[-1] += f" {command}"
        else:
            lines.append(f"{indent}  {command}")
    return "\n".join(["", *lines])


def paths(art: Art, theme: Theme, indent: str = "  ") -> str:
    places = 2 if max(art.width, art.height) < 3000 else 1
    return "\n".join(
        xml(t'{indent}<path fill="{theme.get(fill, fill)}" d="{path_d(outline, places, f"{indent}  ")}"/>')
        for fill, outline in art.layers
    )


def standalone(art: Art, theme: Theme) -> str:
    w, h = art.width, art.height
    return xml(
        t'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">\n'
        t"  <title>{art.title}</title>\n{paths(art, theme):raw}\n</svg>\n"
    )


def sheet(folders: dict[str, str], arts: dict[str, list[Art]], themes: dict[str, Theme]) -> str:
    """Every file once; anything that paints a ground, once per theme."""
    page, tallest, sans, mono = 1400, 196, "system-ui,sans-serif", "ui-monospace,monospace"
    first = next(iter(themes))
    out, y = [], 0.0

    def text(x: float, y: float, s: str, *, anchor: str = "middle", weight: int = 400, family: str = sans) -> None:
        out.append(
            xml(
                t'<text x="{x}" y="{y}" font-family="{family}" font-size="12" font-weight="{weight}" '
                t'fill="#50586a" text-anchor="{anchor}">{s}</text>'
            )
        )

    for folder, note in folders.items():
        cells = [(art, name) for art in arts[folder] for name in (themes if art.has_ground else [first])]
        out.append(xml(t'<rect y="{y}" width="{page}" height="48" fill="#E9ECF3"/>'))
        text(40, y + 30, folder.upper(), anchor="start", weight=700)
        text(page - 40, y + 30, note, anchor="end")
        y += 48
        columns = math.ceil(len(cells) / math.ceil(len(cells) / 5))
        width = page / columns
        for row in batched(cells, columns, strict=False):
            scales = [min((width - 68) / art.width, tallest / art.height) for art, _ in row]
            height = max(art.height * s for (art, _), s in zip(row, scales, strict=True)) + 94
            for i, ((art, name), s) in enumerate(zip(row, scales, strict=True)):
                middle = (i + 0.5) * width
                dx, dy = middle - art.width * s / 2, y + 30 + (height - 94 - art.height * s) / 2
                out.append(
                    xml(
                        t'<g transform="translate({dx} {dy}) scale({num(s, 5)})">\n'
                        t"{paths(art, themes[name], '    '):raw}\n  </g>"
                    )
                )
                text(
                    middle,
                    y + height - 34,
                    f"{art.key} · {name}" if art.has_ground else art.key,
                    weight=600,
                    family=mono,
                )
                text(middle, y + height - 16, " + ".join(art.links()) or "drawn here")
            y += height
    body = "\n".join(f"  {element}" for element in out)
    return xml(
        t'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {page} {y}" width="{page}" height="{y}">\n'
        t'  <rect width="{page}" height="{y}" fill="#F5F7FB"/>\n{body:raw}\n</svg>\n'
    )


def main() -> None:
    config = tomllib.loads((ROOT / "bailey.toml").read_text())
    folders, themes, sizes = config["folders"], config["themes"], config.get("png", {})
    arts = {folder: [load(path) for path in sorted((ROOT / folder).glob("*.svg"))] for folder in folders}

    dist = ROOT / "dist"
    rmtree(dist, ignore_errors=True)
    for (name, theme), (folder, group) in product(themes.items(), arts.items()):
        (out := dist / name / folder).mkdir(parents=True)
        for art in group:
            (out / f"{art.path.stem}.svg").write_text(svg := standalone(art, theme))
            for size in sizes.get(folder, []):
                scale = size / max(art.width, art.height)
                width, height = (max(1, round(side * scale)) for side in (art.width, art.height))
                png = resvg_py.svg_to_bytes(svg_string=svg, width=width, height=height)
                (out / f"{art.path.stem}-{size}.png").write_bytes(bytes(png))
    (dist / "sheet.svg").write_text(sheet(folders, arts, themes))
    print(f"built {sum(map(len, arts.values()))} files in {len(themes)} themes")


if __name__ == "__main__":
    main()
