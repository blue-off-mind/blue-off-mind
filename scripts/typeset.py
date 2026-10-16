"""排字：把文字转成 SVG 轮廓。

GitHub 用 <img> 显示 README 里的 SVG，这种模式下 SVG 加载不了任何外部字体，
写 font-family 只会落到访客系统的默认字体上。所以这里用 HarfBuzz 排版、
fontTools 取字形轮廓，直接输出路径，保证所有人看到的都是同一套字。
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from html import escape
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / ".cache" / "fonts"

# 名字 → (Google Fonts 家族, 字重, 是否斜体)。gstatic 的下载地址带版本号会变，每次从 CSS API 现查。
FONTS = {
    "song-900": ("Noto Serif SC", 900, False),
    "song-700": ("Noto Serif SC", 700, False),
    "song-500": ("Noto Serif SC", 500, False),
    "play-400": ("Playfair Display", 400, False),
    "play-700": ("Playfair Display", 700, False),
    "play-900": ("Playfair Display", 900, False),
    "play-400i": ("Playfair Display", 400, True),
    "grot-500": ("Space Grotesk", 500, False),
    "grot-700": ("Space Grotesk", 700, False),
}

# 字体里都没有的字符（比如 🈚 这类 emoji）交给访客系统的彩色 emoji 字体
EMOJI_FAMILY = "'Apple Color Emoji','Segoe UI Emoji','Noto Color Emoji',sans-serif"

# 不能出现在行首 / 行尾的标点（避头尾）
NO_LINE_START = set("，。、；：？！）》」』】〕…—”’,.;:!?)]}%·")
NO_LINE_END = set("（《「『【〔“‘([{")


def _get(url: str) -> bytes:
    # 非浏览器 UA：CSS API 才会返回整套 TTF，而不是按 unicode-range 切片的 woff2
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def font_file(name: str) -> Path:
    path = FONT_DIR / f"{name}.ttf"
    if path.exists():
        return path
    family, weight, italic = FONTS[name]
    query = f"{family.replace(' ', '+')}:ital,wght@{int(italic)},{weight}"
    css = _get(f"https://fonts.googleapis.com/css2?family={query}").decode()
    url = re.search(r"url\((https://[^)]+?\.ttf)\)", css).group(1)
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    part.write_bytes(_get(url))
    part.rename(path)
    return path


def num(v: float, digits: int = 2) -> str:
    s = f"{v:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


class Face:
    _loaded: dict[str, Face] = {}

    @classmethod
    def get(cls, name: str) -> Face:
        if name not in cls._loaded:
            cls._loaded[name] = cls(name)
        return cls._loaded[name]

    def __init__(self, name: str):
        path = font_file(name)
        self.name = name
        self.id = re.sub(r"[^a-z0-9]", "", name)
        face = hb.Face(path.read_bytes())
        self.hb_font = hb.Font(face)
        self.upem = face.upem
        tt = TTFont(path, lazy=True)
        self.cmap = tt.getBestCmap()
        self.glyph_set = tt.getGlyphSet()
        self.glyph_names = tt.getGlyphOrder()
        os2 = tt["OS/2"]
        self.cap_height = getattr(os2, "sCapHeight", 0) or 0.7 * self.upem
        self._outlines: dict[int, str] = {}

    def covers(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def shape(self, text: str, features: tuple[str, ...] = ()) -> list[tuple[int, int, int, int, int]]:
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb_font, buf, {"kern": True, "liga": True, **{f: True for f in features}})
        return [
            (info.codepoint, pos.x_advance, pos.x_offset, pos.y_offset, info.cluster)
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions)
        ]

    def outline(self, gid: int) -> str:
        if gid not in self._outlines:
            pen = SVGPathPen(self.glyph_set, ntos=lambda v: num(v, 1))
            self.glyph_set[self.glyph_names[gid]].draw(pen)
            self._outlines[gid] = pen.getCommands()
        return self._outlines[gid]


@dataclass(frozen=True)
class Style:
    faces: tuple[str, ...]  # 按顺序回退：第一个有这个字的字体负责它
    size: float
    tracking: float = 0.0  # 字距，单位 em
    upper: bool = False
    features: tuple[str, ...] = ()  # 额外打开的 OpenType 特性，比如 Playfair 的 lnum（等高数字）


@dataclass
class Glyph:
    face: Face | None  # None：交给系统 emoji 字体
    gid: int
    char: str
    x: float
    dy: float
    size: float


@dataclass
class Line:
    glyphs: list[Glyph]
    width: float
    text: str


def _runs(text: str, faces: list[Face]) -> list[tuple[Face | None, str]]:
    runs: list[tuple[Face | None, str]] = []
    for ch in text:
        if ch in "️‍" or (ch == " " and runs):
            face = runs[-1][0]  # 变体选择符、连接符、空格跟着前一个字走
        else:
            face = next((f for f in faces if f.covers(ch)), None)
        if runs and runs[-1][0] is face:
            runs[-1] = (face, runs[-1][1] + ch)
        else:
            runs.append((face, ch))
    return runs


def set_line(text: str, style: Style) -> Line:
    if style.upper:
        text = text.upper()
    faces = [Face.get(n) for n in style.faces]
    track = style.tracking * style.size
    glyphs: list[Glyph] = []
    x = 0.0
    for face, run in _runs(text, faces):
        if face is None:
            for ch in run:
                if ch in "️‍":
                    continue
                glyphs.append(Glyph(None, 0, ch, x, 0.0, style.size))
                x += style.size + track
            continue
        scale = style.size / face.upem
        for gid, adv, xo, yo, cluster in face.shape(run, style.features):
            glyphs.append(Glyph(face, gid, run[cluster], x + xo * scale, yo * scale, style.size))
            x += adv * scale + track
    width = x - track if glyphs else 0.0
    return Line(glyphs, max(width, 0.0), text)


def _units(text: str) -> list[str]:
    """切成可换行的最小单位：汉字一字一个，西文一词一个，标点黏在相邻的字上。"""
    units: list[str] = []
    word = ""
    for ch in text:
        latin = ch.isascii() and not ch.isspace() and ch not in NO_LINE_START | NO_LINE_END
        if latin:
            word += ch
            continue
        if word:
            units.append(word)
            word = ""
        if ch.isspace():
            if units:
                units[-1] += " "
        elif ch in NO_LINE_START and units:
            units[-1] += ch
        else:
            units.append(ch)
    if word:
        units.append(word)
    # 开括号、开引号黏到后一个单位上
    merged: list[str] = []
    carry = ""
    for u in units:
        if u.strip() and all(c in NO_LINE_END for c in u.strip()):
            carry += u
            continue
        merged.append(carry + u)
        carry = ""
    if carry:
        merged.append(carry)
    return merged


def wrap(text: str, style: Style, width: float, max_lines: int | None = None) -> list[Line]:
    text = " ".join(text.split())
    lines: list[str] = []
    current = ""
    for unit in _units(text):
        candidate = current + unit
        if current and set_line(candidate.rstrip(), style).width > width:
            lines.append(current.rstrip())
            current = unit.lstrip()
        else:
            current = candidate
    if current.strip():
        lines.append(current.rstrip())
    if max_lines and len(lines) > max_lines:
        last = lines[max_lines - 1]
        while last and set_line(last + "…", style).width > width:
            last = last[:-1].rstrip()
        lines = lines[: max_lines - 1] + [last + "…"]
    return [set_line(line, style) for line in lines]


def fit(text: str, style: Style, width: float) -> Line:
    """单行放不下就截断加省略号。"""
    return wrap(text, style, width, max_lines=1)[0] if text.strip() else set_line("", style)


class Canvas:
    def __init__(self, width: float, height: float, title: str):
        self.width = width
        self.height = height
        self.title = title
        self.glyph_defs: dict[str, str] = {}
        self.defs: list[str] = []
        self.css: list[str] = []
        self.parts: list[str] = []

    def add(self, svg: str) -> None:
        self.parts.append(svg)

    def text(self, line: Line, x: float, y: float, fill: str, anchor: str = "start", attrs: str = "") -> tuple[float, float]:
        """在基线 (x, y) 处画一行字，返回实际的左右边界。"""
        if anchor == "middle":
            x -= line.width / 2
        elif anchor == "end":
            x -= line.width
        out = []
        for g in line.glyphs:
            gx, gy = x + g.x, y - g.dy
            if g.face is None:
                # emoji 的字面框按 1em 算，基线大约在下沿往上 0.12em
                out.append(
                    f'<text x="{num(gx)}" y="{num(gy)}" font-size="{num(g.size * 0.92)}" '
                    f'font-family="{EMOJI_FAMILY}">{escape(g.char)}</text>'
                )
                continue
            d = g.face.outline(g.gid)
            if not d:
                continue
            key = f"{g.face.id}-{g.gid}"
            self.glyph_defs.setdefault(key, d)
            s = g.size / g.face.upem
            out.append(f'<use href="#{key}" transform="translate({num(gx)} {num(gy)}) scale({num(s, 5)} {num(-s, 5)})"/>')
        self.parts.append(f'<g fill="{fill}"{attrs}>{"".join(out)}</g>')
        return x, x + line.width

    def render(self) -> str:
        glyphs = "".join(f'<path id="{k}" d="{d}"/>' for k, d in self.glyph_defs.items())
        css = f"<style>{''.join(self.css)}</style>" if self.css else ""
        w, h = num(self.width), num(self.height)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">'
            f"<title>{escape(self.title)}</title>{css}<defs>{glyphs}{''.join(self.defs)}</defs>"
            f"{''.join(self.parts)}</svg>\n"
        )
