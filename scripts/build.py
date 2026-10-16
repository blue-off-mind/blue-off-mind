"""排一期《星岛月报》：拉博客文章和 GitHub 数据，生成版面图片和 README.md。

本地运行：.venv/bin/python scripts/build.py
  博客的 Supabase publishable key 从环境变量或 .env.local 读，GitHub 令牌用 GH_TOKEN 或 `gh auth token`。
CI：.github/workflows/typeset.yml 每天跑一次，内容有变化才提交。
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import io
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape, unescape
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps

from typeset import ROOT, Canvas, Style, fit, num, set_line, wrap

ASSETS = ROOT / "assets"
TZ = ZoneInfo("Asia/Shanghai")
W = 824  # 版心宽度：1280 宽的窗口里 GitHub 主页 README 只有 831px，再宽也只到 846px
M = 8  # 每张图左右各留 8px：并排的卡片之间自然留出栏距，手机上单张显示时也左右对称

OWNER = "blue-off-mind"
EMAIL = "wuyuu0801@gmail.com"
MOTTO = "to define is to limit"
BLOG = "https://blog.wuyusblog.com"
SUPABASE_URL = "https://vcufajttjjflkmfkfikv.supabase.co"
FOUNDED = (2026, 1)  # 博客第一篇文章发在 2026 年 1 月，从那个月算创刊
SNAKE = f"https://raw.githubusercontent.com/{OWNER}/{OWNER}/output/snake"

# 编号沿用星岛入口站：01 作品集、02 社论日志，月报自己是 03
ISLES = [
    ("00 / GATEWAY", "星岛", "入口 · GATEWAY", "wuyusblog.com", "https://wuyusblog.com", "xingdao-gateway"),
    ("01 / MOVABLE TYPE", "碧水洗字", "作品集 · PORTFOLIO", "portfolio.wuyusblog.com", "https://portfolio.wuyusblog.com", "shui-yu-huozi"),
    ("02 / EDITORIAL", "社论日志", "博客 · JOURNAL", "blog.wuyusblog.com", BLOG, "WuYu-s-Blog"),
]
REPOS = ["youfu", "c-drive-cleaner"]
LANG_COLORS = {"Python": "#3572A5", "PowerShell": "#012456", "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Vue": "#41b883"}

# 与博客 LanguageContext 里的中文译名保持一致
CATEGORY = {"Design": "设计", "Coding": "编码", "Life": "生活", "Music": "音乐", "Tech": "科技"}
MOOD = {"Deep Focus": "深度专注", "Sunday Morning": "周日清晨", "Late Night": "深夜", "Nostalgic": "怀旧", "Sharp": "犀利", "Energetic": "活力"}
MONTHS = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
CN_DIGITS = "〇一二三四五六七八九"
CN_MONTHS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"]


@dataclass(frozen=True)
class Theme:
    name: str
    ink: str
    muted: str
    rule: str
    red: str
    star: str
    shadow: str  # 双色调照片的暗部
    light: str  # 双色调照片的亮部


THEMES = [
    Theme("light", ink="#191814", muted="#6b675c", rule="#d9d5c9", red="#b22922", star="#f2c515", shadow="#191814", light="#faf9f4"),
    Theme("dark", ink="#ece9e0", muted="#9d998d", rule="#34332e", red="#ee6b5f", star="#f2c515", shadow="#0d1117", light="#d6d2c6"),
]

LABEL = Style(("grot-500", "song-500"), 11, tracking=0.22, upper=True)
SMALL_CN = Style(("song-500",), 12, tracking=0.08)
LNUM = ("lnum",)  # Playfair 默认是旧式数字，0 像 o，邮箱和编号里必须换成等高数字


@dataclass
class Article:
    id: str
    title: str
    category: str
    mood: str
    published: datetime
    image: str
    note: str
    quote: str
    likes: int
    lead: str  # 正文开头，当导语用
    best: str  # 博客的「最佳阅读场景」

    @property
    def url(self) -> str:
        return f"{BLOG}/#/article/{self.id}"

    @property
    def date(self) -> str:
        return self.published.strftime("%Y.%m.%d")

    @property
    def kicker(self) -> str:
        cat = f"{CATEGORY.get(self.category, self.category)} {self.category}"
        mood = f"{MOOD.get(self.mood, self.mood)} {self.mood}"
        return f"{cat} · {mood}"


@dataclass
class Repo:
    name: str
    description: str
    language: str | None
    stars: int
    url: str


@dataclass
class Issue:
    today: datetime
    articles: list[Article]
    contributions: int | None

    @property
    def number(self) -> int:
        return self.today.month

    @property
    def total(self) -> int:
        return (self.today.year - FOUNDED[0]) * 12 + self.today.month - FOUNDED[1] + 1

    @property
    def volume(self) -> int:
        return self.today.year - FOUNDED[0] + 1

    @property
    def this_month(self) -> list[Article]:
        return [a for a in self.articles if (a.published.year, a.published.month) == (self.today.year, self.today.month)]


# ---------------------------------------------------------------- 数据


def env(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    local = ROOT / ".env.local"
    if local.exists():
        for line in local.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return None


def http(url: str, headers: dict | None = None, body: dict | None = None) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "starry-isle-monthly", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def github_token() -> str | None:
    token = env("GH_TOKEN") or env("GITHUB_TOKEN")
    if token:
        return token
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def plain(text: str) -> str:
    """正文可能是 HTML 或 Markdown，导语只要纯文字。"""
    text = unescape(re.sub(r"<[^>]+>", "\n", text))
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^[#>*\-\s]+", "", text, flags=re.M)
    text = re.sub(r"[*_`~]", "", text)
    out = ""
    for para in (p.strip() for p in text.splitlines()):
        if para:
            # 中文段落直接相接，西文段落之间补一个空格
            out += (" " if out and out[-1].isascii() and para[0].isascii() else "") + para
    return out[:240]


def fetch_articles() -> list[Article]:
    key = env("BLOG_SUPABASE_KEY")
    if not key:
        sys.exit("缺少 BLOG_SUPABASE_KEY（博客的 Supabase publishable key）")
    query = urllib.parse.urlencode(
        {
            "select": "id,title,category,mood,publishedAt,imageUrl,editorNote,pullQuote,likes,content,bestContext",
            "order": "publishedAt.desc",
            "limit": "30",
        }
    )
    rows = json.loads(http(f"{env('BLOG_SUPABASE_URL') or SUPABASE_URL}/rest/v1/articles?{query}", {"apikey": key}))
    return [
        Article(
            id=r["id"],
            title=r["title"].strip(),
            category=r["category"],
            mood=r["mood"],
            published=datetime.fromisoformat(r["publishedAt"].replace("Z", "+00:00")).astimezone(TZ),
            image=r.get("imageUrl") or "",
            note=(r.get("editorNote") or "").strip(),
            quote=(r.get("pullQuote") or "").strip(),
            likes=r.get("likes") or 0,
            lead=plain(r.get("content") or ""),
            best=(r.get("bestContext") or "").strip(),
        )
        for r in rows
    ]


def fetch_repos(token: str | None, names: list[str]) -> list[Repo]:
    headers = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})}
    repos = []
    for name in names:
        try:
            r = json.loads(http(f"https://api.github.com/repos/{OWNER}/{name}", headers))
        except urllib.error.HTTPError:
            continue  # 私有仓库在 CI 里看不到，跳过
        if r.get("private"):
            continue  # 本地用的是本人令牌，私有仓库也能读到，同样不上版面
        repos.append(Repo(r["name"], r.get("description") or "", r.get("language"), r["stargazers_count"], r["html_url"]))
    return repos


def fetch_contributions(token: str | None, today: datetime) -> int | None:
    if not token:
        return None
    start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    query = """query($login:String!,$from:DateTime!,$to:DateTime!){user(login:$login){contributionsCollection(from:$from,to:$to){
      contributionCalendar{totalContributions}}}}"""
    variables = {"login": OWNER, "from": start.astimezone(timezone.utc).isoformat(), "to": today.astimezone(timezone.utc).isoformat()}
    try:
        data = json.loads(http("https://api.github.com/graphql", {"Authorization": f"Bearer {token}"}, {"query": query, "variables": variables}))
        cal = data["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    except (urllib.error.URLError, KeyError, TypeError):
        return None
    return cal["totalContributions"]


# ---------------------------------------------------------------- 小工具


def luminance(hex_color: str) -> float:
    def channel(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def roman(n: int) -> str:
    out = ""
    for value, sym in [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]:
        while n >= value:
            out += sym
            n -= value
    return out


def rule(c: Canvas, x1: float, x2: float, y: float, color: str, width: float = 1) -> None:
    c.add(f'<rect x="{num(x1)}" y="{num(y - width / 2)}" width="{num(x2 - x1)}" height="{num(width)}" fill="{color}"/>')


def star(c: Canvas, x: float, y: float, theme: Theme, r: float = 3.2, cls: str = "") -> None:
    gid = f"glow-{theme.name}"
    if not any(gid in d for d in c.defs):
        c.defs.append(
            f'<radialGradient id="{gid}"><stop offset="0" stop-color="{theme.star}" stop-opacity=".75"/>'
            f'<stop offset=".35" stop-color="{theme.star}" stop-opacity=".28"/><stop offset="1" stop-color="{theme.star}" stop-opacity="0"/></radialGradient>'
        )
    klass = f' class="{cls}"' if cls else ""
    c.add(f'<g{klass}><circle cx="{num(x)}" cy="{num(y)}" r="{num(r * 6)}" fill="url(#{gid})"/><circle cx="{num(x)}" cy="{num(y)}" r="{num(r)}" fill="{theme.star}"/></g>')


def sparkle(c: Canvas, x: float, y: float, r: float, color: str) -> None:
    k = r * 0.22
    c.add(
        f'<path d="M{num(x)} {num(y - r)}Q{num(x + k)} {num(y - k)} {num(x + r)} {num(y)}Q{num(x + k)} {num(y + k)} {num(x)} {num(y + r)}'
        f'Q{num(x - k)} {num(y + k)} {num(x - r)} {num(y)}Q{num(x - k)} {num(y - k)} {num(x)} {num(y - r)}Z" fill="{color}"/>'
    )


# ---------------------------------------------------------------- 版面


def masthead(issue: Issue, t: Theme) -> Canvas:
    today = issue.today
    c = Canvas(W, 272, f"星岛月报 · Starry Isle Monthly · {today.year} 年第 {issue.number} 期")

    c.text(set_line("03 / MONTHLY", LABEL), M, 20, t.muted)
    c.text(set_line(f"{today.year} 年第 {issue.number} 期 · 总第 {issue.total} 期", SMALL_CN), W - M, 20, t.muted, "end")
    rule(c, M, W - M, 34, t.ink)

    # 报名拆成两座岛：「星岛」「月报」，中间留出星轨穿过的缝
    big = Style(("song-900",), 104, tracking=0.04)
    left, right = set_line("星岛", big), set_line("月报", big)
    gap, base = 100, 150
    lx = W / 2 - gap / 2 - left.width
    rx = W / 2 + gap / 2
    c.text(left, lx, base, t.ink)
    c.text(right, rx, base, t.ink)
    cap = Style(("play-400",), 14, tracking=0.42, upper=True)
    c.text(set_line("Starry Isle", cap), lx + left.width / 2, 190, t.ink, "middle")
    c.text(set_line("Monthly", cap), rx + right.width / 2, 190, t.ink, "middle")

    # 星轨：从左下的星点出发，压在「STARRY ISLE」下面往右走，到两座岛之间的缝里陡然升起，
    # 越过「月报」头顶冲出版面上沿。各段控制点首尾共线，曲线是连续光滑的。
    gx = W / 2
    sx, sy = 44, 212
    trail = (
        f"M{sx} {sy}C160 212 300 210 {num(gx - 42)} 204"
        f"C{num(gx - 22)} 202.3 {num(gx - 4)} 150 {num(gx)} 112"
        f"C{num(gx + 3.2)} 81.6 {num(gx + 18)} 52 {num(gx + 40)} 40"
        f"C{num(gx + 62)} 28 {num(gx + 98)} 12 {num(gx + 148)} -4"
    )
    c.css.append(
        ".trail{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 2.6s cubic-bezier(.6,0,.3,1) .4s forwards}"
        ".spark{transform-box:fill-box;transform-origin:center;animation:ignite 1s ease-out both,twinkle 5s ease-in-out 1.2s infinite}"
        "@keyframes draw{to{stroke-dashoffset:0}}"
        "@keyframes ignite{from{opacity:0;transform:scale(.3)}}"
        "@keyframes twinkle{50%{opacity:.6}}"
        "@media (prefers-reduced-motion:reduce){.trail{animation:none;stroke-dashoffset:0}.spark{animation:none}}"
    )
    c.add(f'<path class="trail" pathLength="1" d="{trail}" fill="none" stroke="{t.ink}" stroke-opacity=".82" stroke-width="1.6" stroke-linecap="round"/>')
    star(c, sx, sy, t, cls="spark")

    rule(c, M, W - M, 226, t.ink, 3)
    rule(c, M, W - M, 231.5, t.ink)
    c.text(set_line(f"Vol. {roman(issue.volume)} · No. {issue.number}", LABEL), M, 254, t.ink)
    cn_date = "".join(CN_DIGITS[int(d)] for d in str(today.year)) + f"年{CN_MONTHS[today.month - 1]}月"
    c.text(set_line(f"{MONTHS[today.month - 1]} {today.year} · {cn_date}", LABEL), W / 2, 254, t.ink, "middle")
    numbers = f"本月 {len(issue.this_month)} 篇"
    if issue.contributions is not None:
        numbers += f" · 贡献 {issue.contributions} 次"
    c.text(set_line(numbers, SMALL_CN), W - M, 254, t.ink, "end")
    rule(c, M, W - M, 266, t.ink)
    return c


def motto(t: Theme) -> Canvas:
    c = Canvas(W, 72, MOTTO)
    line = set_line(MOTTO, Style(("play-400i",), 30, tracking=0.01))
    x1, x2 = c.text(line, W / 2, 44, t.ink, "middle")
    rule(c, M, x1 - 40, 34, t.rule)
    rule(c, x2 + 40, W - M, 34, t.rule)
    sparkle(c, x1 - 22, 34, 6, t.red)
    sparkle(c, x2 + 22, 34, 6, t.red)
    return c


def flag(no: str, cn: str, en: str, t: Theme) -> Canvas:
    c = Canvas(W, 52, f"{no} {cn} {en}")
    rule(c, M, W - M, 4, t.ink, 2.4)
    _, x = c.text(set_line(no, Style(("play-900",), 24, features=LNUM)), M, 38, t.red)
    _, x = c.text(set_line(cn, Style(("song-900",), 21, tracking=0.06)), x + 12, 37, t.ink)
    c.text(set_line(en, LABEL), x + 12, 36, t.muted)
    return c


def headline_text(a: Article, t: Theme, width: float, pad: float) -> Canvas:
    c = Canvas(width, 344, f"头条：{a.title}")
    x, w = pad, width - pad - M
    c.text(fit(a.kicker, LABEL, w), x, 22, t.red)

    y = 22
    for i, line in enumerate(wrap(a.title, Style(("song-900",), 50, tracking=0.02), w, max_lines=2)):
        y += 64 if i == 0 else 60
        c.text(line, x, y, t.ink)
    y += 24
    rule(c, x, x + 40, y, t.red, 3)

    # 导语、编者按、引语依次往下排，排不下的从后往前舍弃
    footer = 330
    bottom = footer - 40
    if a.lead and y + 34 <= bottom:
        room = min(3, int((bottom - y - 34) // 26) + 1)
        lines = wrap(a.lead, Style(("song-500",), 15, tracking=0.04), w, max_lines=room)
        for i, line in enumerate(lines):
            c.text(line, x, y + 34 + i * 26, t.ink)
        y += 34 + (len(lines) - 1) * 26
    if a.note and y + 34 <= bottom:
        label = set_line("编者按", Style(("song-700",), 13, tracking=0.1))
        c.text(label, x, y + 34, t.red)
        c.text(fit(a.note, Style(("song-500",), 14, tracking=0.03), w - label.width - 14), x + label.width + 14, y + 34, t.muted)
        y += 34
    if a.quote and y + 46 <= bottom:
        room = min(2, int((bottom - y - 46) // 30) + 1)
        lines = wrap(a.quote, Style(("song-700", "play-400i"), 20, tracking=0.03), w - 36, max_lines=room)
        c.text(set_line("“", Style(("play-900",), 60)), x - 2, y + 66, t.red)
        for i, line in enumerate(lines):
            c.text(line, x + 36, y + 46 + i * 30, t.ink)

    meta = f"{a.date}  ·  ♥ {a.likes}" + (f"  ·  宜读 {a.best}" if a.best else "")
    c.text(fit(meta, Style(("grot-500", "song-500"), 12, tracking=0.08), w - 190), x, footer, t.muted)
    more = set_line("阅读全文 Read story ↗", Style(("song-700", "grot-700"), 13, tracking=0.08))
    x1, x2 = c.text(more, width - M, footer, t.ink, "end")
    rule(c, x1, x2, footer + 8, t.ink)
    return c


PHOTO = (300, 344)  # 头条配图那一栏的尺寸，图片本身左右各缩进 M


def headline_photo(a: Article, t: Theme, scale: int = 2) -> bytes:
    """下载头条配图，裁成竖幅，转成墨色和纸色的双色调，像印在报纸上一样。"""
    w, h = (PHOTO[0] - 2 * M) * scale, PHOTO[1] * scale
    url = a.image
    if "images.unsplash.com" in url:
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        query.update({"w": str(w * 2), "q": "80", "fm": "jpg"})
        query.pop("auto", None)
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    img = Image.open(io.BytesIO(http(url))).convert("L")
    img = ImageOps.fit(img, (w, h), method=Image.Resampling.LANCZOS, centering=(0.5, 0.42))
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageOps.colorize(img, black=t.shadow, white=t.light).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=78, optimize=True, progressive=True)
    return buf.getvalue()


def photo_card(a: Article, jpeg: bytes, t: Theme) -> Canvas:
    # 照片直接嵌进 SVG：左右的留白才能是透明的，和其他栏目对齐
    c = Canvas(*PHOTO, f"头条配图：{a.title}")
    data = base64.b64encode(jpeg).decode()
    c.add(f'<image x="{M}" y="0" width="{PHOTO[0] - 2 * M}" height="{PHOTO[1]}" href="data:image/jpeg;base64,{data}"/>')
    # 左上角贴一张和博客文章卡片一样的分类小标签
    tag = set_line(a.category, Style(("grot-700", "song-700"), 10, tracking=0.2, upper=True))
    bx, by = M + 14, 14
    c.add(f'<rect x="{bx}" y="{by}" width="{num(tag.width + 16)}" height="22" fill="{t.light if t.name == "light" else t.shadow}"/>')
    c.text(tag, bx + 8, by + 15, t.shadow if t.name == "light" else t.light)
    return c


def brief(a: Article, t: Theme, current: bool) -> Canvas:
    width = W / 2
    c = Canvas(width, 128, a.title)
    x0, x1 = M, width - M
    rule(c, x0, x1, 0.5, t.ink)
    c.text(fit(f"{a.published:%m.%d} · {MOOD.get(a.mood, a.mood)} {a.mood}", LABEL, x1 - x0 - 90), x0, 24, t.red)
    c.text(set_line("本期 This issue" if current else "往期 Archive", LABEL), x1, 24, t.muted, "end")
    c.text(fit(a.title, Style(("song-900",), 27, tracking=0.03), x1 - x0), x0, 64, t.ink)
    note = a.note or a.quote
    for i, line in enumerate(wrap(note, Style(("song-500",), 14, tracking=0.03), x1 - x0, max_lines=2)):
        c.text(line, x0, 94 + i * 22, t.muted)
    return c


def isle(i: int, t: Theme) -> Canvas:
    label, name, sub, host, _, _ = ISLES[i]
    widths = [275, 274, 275]
    width = widths[i]
    c = Canvas(width, 140, f"{name} · {host}")
    x0, x1 = M, width - M
    rule(c, x0, x1, 0.5, t.ink)
    c.text(set_line(label, LABEL), x0, 24, t.muted)
    c.text(set_line(name, Style(("song-900",), 34, tracking=0.04)), x0, 72, t.ink)
    c.text(set_line(sub, LABEL), x0, 96, t.red)
    c.text(fit(f"{host} ↗", Style(("grot-500",), 13, tracking=0.02), x1 - x0), x0, 128, t.ink)
    return c


def repo_card(r: Repo, t: Theme) -> Canvas:
    width = W / 2
    c = Canvas(width, 128, f"{r.name}: {r.description}")
    x0, x1 = M, width - M
    rule(c, x0, x1, 0.5, t.ink)
    x = x0
    if r.language:
        color = LANG_COLORS.get(r.language, t.muted)
        if t.name == "dark" and luminance(color) < 0.1:
            color = t.muted  # 像 PowerShell 的深蓝，放在深色底上就看不见了
        c.add(f'<circle cx="{num(x0 + 4)}" cy="20" r="4" fill="{color}"/>')
        x = x0 + 14
    c.text(set_line(f"{r.language or 'Repository'} · 开源 Open source", LABEL), x, 24, t.muted)
    c.text(set_line(f"★ {r.stars}", Style(("grot-500", "song-500"), 12, tracking=0.08)), x1, 24, t.muted, "end")
    c.text(fit(r.name, Style(("play-700",), 28), x1 - x0), x0, 64, t.ink)
    for i, line in enumerate(wrap(r.description, Style(("song-500",), 14, tracking=0.03), x1 - x0, max_lines=2)):
        c.text(line, x0, 94 + i * 22, t.muted)
    return c


def letters(issue: Issue, t: Theme) -> Canvas:
    c = Canvas(W, 124, f"来信请寄 {EMAIL}")
    c.text(set_line("来信请寄 · Letters to the editor", Style(("song-700", "grot-500"), 12, tracking=0.16, upper=True)), M, 34, t.red)
    c.text(set_line(EMAIL, Style(("play-400",), 38, tracking=0.01, features=LNUM)), M, 84, t.ink)
    c.text(set_line("编辑部收", Style(("song-500",), 13, tracking=0.3)), M, 112, t.muted)

    # 右边盖一枚邮戳：外圈绕排刊名，中间是本期年月
    cx, cy, r = W - M - 56, 64, 50
    stamp = f'<g fill="none" stroke="{t.red}" stroke-opacity=".9">'
    stamp += f'<circle cx="{cx}" cy="{cy}" r="{r}" stroke-width="2"/><circle cx="{cx}" cy="{cy}" r="{r - 15}" stroke-width="1"/>'
    for k in range(4):  # 邮戳左侧的波浪注销线
        y = cy - 21 + k * 14
        stamp += f'<path d="M{cx - r - 150} {y}q12.5 -7 25 0t25 0t25 0t25 0t25 0t25 0" stroke-width="1.6"/>'
    c.add(stamp + "</g>")
    ring = set_line("星岛月报 · STARRY ISLE MONTHLY · ", Style(("song-700", "grot-700"), 9, tracking=0.12))
    rad = r - 8
    start = -math.pi / 2 - ring.width / rad / 2
    for g in ring.glyphs:
        if g.face is None or not g.face.outline(g.gid):
            continue
        key = f"{g.face.id}-{g.gid}"
        c.glyph_defs.setdefault(key, g.face.outline(g.gid))
        ang = start + g.x / rad
        px, py = cx + rad * math.cos(ang), cy + rad * math.sin(ang)
        s = g.size / g.face.upem
        c.add(
            f'<use href="#{key}" fill="{t.red}" transform="translate({num(px)} {num(py)}) rotate({num(math.degrees(ang) + 90)}) '
            f'translate(0 {num(g.size * 0.36)}) scale({num(s, 5)} {num(-s, 5)})"/>'
        )
    c.text(set_line(f"{issue.today.year}", Style(("grot-700",), 15, tracking=0.1)), cx, cy - 1, t.red, "middle")
    c.text(set_line(f"{issue.today.month:02d} 月", Style(("song-700", "grot-700"), 12, tracking=0.1)), cx, cy + 16, t.red, "middle")
    return c


# ---------------------------------------------------------------- 输出


def picture(name: str, alt: str, width: float, href: str | None = None, ext: str = "svg") -> str:
    img = (
        f'<picture><source media="(prefers-color-scheme: dark)" srcset="assets/{name}-dark.{ext}">'
        f'<img src="assets/{name}-light.{ext}" width="{num(width)}" alt="{escape(alt)}"></picture>'
    )
    return f'<a href="{escape(href)}">{img}</a>' if href else img


def save(name: str, render) -> None:
    for t in THEMES:
        (ASSETS / f"{name}-{t.name}.svg").write_text(render(t).render())


def main() -> None:
    token = github_token()
    today = datetime.now(TZ)
    articles = fetch_articles()
    if not articles:
        sys.exit("博客一篇文章都没拉到，这一期不排")
    contributions = fetch_contributions(token, today)
    repos = fetch_repos(token, REPOS)
    sources = {r.name: r for r in fetch_repos(token, [isle[5] for isle in ISLES])}  # 没公开的站点仓库自动略过
    issue = Issue(today, articles, contributions)

    ASSETS.mkdir(exist_ok=True)
    for old in ASSETS.glob("*"):
        if old.suffix == ".svg" and old.name.startswith(("brief-", "repo-")):
            old.unlink()

    lead = articles[0]
    rest = [a for a in articles[1:5]]
    save("masthead", lambda t: masthead(issue, t))
    save("motto", motto)
    sections = [("01", "头条", "Headline"), ("02", "本期", "In this issue"), ("03", "群岛", "The isles"), ("04", "版面", "Contributions"), ("05", "来函", "Letters")]
    for no, cn, en in sections:
        save(f"flag-{no}", lambda t, no=no, cn=cn, en=en: flag(no, cn, en, t))

    # 头条配图只在换了头条、或者改了配图处理代码时才重做：每天重新下载压缩会产生没有意义的提交
    recipe = hashlib.sha1((inspect.getsource(headline_photo) + inspect.getsource(photo_card)).encode()).hexdigest()[:12]
    meta = {"id": lead.id, "image": lead.image, "recipe": recipe}
    stamp = ASSETS / "photo.json"
    has_photo = bool(lead.image)
    fresh = stamp.exists() and json.loads(stamp.read_text()) == meta and all((ASSETS / f"photo-{t.name}.svg").exists() for t in THEMES)
    if has_photo and not fresh:
        try:
            for t in THEMES:
                (ASSETS / f"photo-{t.name}.svg").write_text(photo_card(lead, headline_photo(lead, t), t).render())
            stamp.write_text(json.dumps(meta, ensure_ascii=False) + "\n")
        except (OSError, urllib.error.URLError) as exc:
            print(f"头条配图下载失败，改用纯文字头条：{exc}", file=sys.stderr)
            has_photo = False
    if not has_photo:
        for old in [stamp, *ASSETS.glob("photo-*.svg")]:
            old.unlink(missing_ok=True)
    text_width = W - PHOTO[0] if has_photo else W
    save("headline", lambda t: headline_text(lead, t, text_width, 20 if has_photo else M))

    current_ids = {a.id for a in issue.this_month}
    for i, a in enumerate(rest):
        save(f"brief-{i}", lambda t, a=a: brief(a, t, a.id in current_ids))
    for i in range(len(ISLES)):
        save(f"isle-{i}", lambda t, i=i: isle(i, t))
    for i, r in enumerate(repos):
        save(f"repo-{i}", lambda t, r=r: repo_card(r, t))
    save("letters", lambda t: letters(issue, t))

    head = picture("headline", f"头条：{lead.title}", text_width, lead.url)
    if has_photo:
        head = picture("photo", f"头条配图：{lead.title}", PHOTO[0], lead.url) + head
    briefs = "".join(picture(f"brief-{i}", f"{a.title} · {a.date}", W / 2, a.url) for i, a in enumerate(rest))
    isles = "".join(picture(f"isle-{i}", f"{name} · {host}", [275, 274, 275][i], href) for i, (_, name, _, host, href, _) in enumerate(ISLES))
    repo_row = "".join(picture(f"repo-{i}", f"{r.name}：{r.description}", W / 2, r.url) for i, r in enumerate(repos))
    source_line = ""
    links = [f'<a href="{sources[repo].url}">{name} · {repo}</a>' for _, name, _, _, _, repo in ISLES if repo in sources]
    if links:
        source_line = f'\n<p align="center"><sub>源码 Source：{"　".join(links)}</sub></p>\n'
    snake = (
        f'<picture><source media="(prefers-color-scheme: dark)" srcset="{SNAKE}-dark.svg">'
        f'<img src="{SNAKE}-light.svg" width="{W}" alt="贡献图被一条贪吃蛇吃掉的动画"></picture>'
    )

    readme = f"""<!--
  星岛月报 · Starry Isle Monthly
  这个文件由 scripts/build.py 生成，GitHub Actions 每天重新排版一次。要改版面请改脚本，别直接改这里。
-->

<p align="center">{picture("masthead", f"星岛月报 Starry Isle Monthly · {today.year} 年第 {issue.number} 期 · {MONTHS[today.month - 1].title()} {today.year}", W, "https://wuyusblog.com")}</p>

<p align="center">{picture("motto", MOTTO, W)}</p>

<p align="center">{picture("flag-01", "01 头条 Headline", W)}</p>

<p align="center">{head}</p>

<p align="center">{picture("flag-02", "02 本期 In this issue", W)}</p>

<p align="center">{briefs}</p>

<p align="center">{picture("flag-03", "03 群岛 The isles", W)}</p>

<p align="center">{isles}</p>

<p align="center">{repo_row}</p>
{source_line}
<p align="center">{picture("flag-04", "04 版面 Contributions", W)}</p>

<p align="center">{snake}</p>

<p align="center">{picture("flag-05", "05 来函 Letters", W)}</p>

<p align="center">{picture("letters", f"来信请寄 {EMAIL}", W, f"mailto:{EMAIL}")}</p>

<p align="center"><sub>本刊由 GitHub Actions 每日排版 · Typeset daily by GitHub Actions<br>
字体 Noto Serif SC · Playfair Display · Space Grotesk · 全部转成轮廓，所见即所印</sub></p>
"""
    (ROOT / "README.md").write_text(readme)
    print(f"排好了：{today.year} 年第 {issue.number} 期（总第 {issue.total} 期），头条《{lead.title}》，本月 {len(issue.this_month)} 篇，贡献 {contributions}")


if __name__ == "__main__":
    main()
