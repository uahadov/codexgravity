#!/usr/bin/env python3
"""
Render the repository's editorial images (paper collage style).

Outputs, both exactly 1280x640 PNG (GitHub's recommended social preview size):
    docs/social-preview.png   hero + GitHub social preview
    docs/how-it-works.png     the four-step plate

The look follows the reference style: warm paper, hairline grid, Manrope
ExtraBold headlines with an orange underlined word, black-and-white cut-out
plates with soft shadows, orange stamps. No mascots, no icons, no gimmicks.

Fonts: reads Manrope + IBM Plex Sans from --fonts-dir when available (both OFL),
otherwise falls back to the system stack. Uses headless Chromium to rasterise.

    python scripts/render_social_images.py
    python scripts/render_social_images.py --fonts-dir ~/fonts --chrome /usr/bin/chromium
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
WIDTH, HEIGHT, SCALE = 1280, 640, 1

DEFAULT_FONT_DIRS = (
    Path.home() / "Desktop/video-edit-plan/materiallar/fontlar",
    Path.home() / ".local/share/fonts",
    Path("/usr/share/fonts"),
)

CHROME_CANDIDATES = (
    Path.home() / ".gemini/antigravity/bin/chromium",
    Path.home() / ".cache/puppeteer/chrome/linux-156.0.8078.47/chrome-linux64/chrome",
    Path("/usr/bin/chromium"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium-browser"),
)


def find_chrome(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chrome")
    if not found:
        raise SystemExit("No Chromium/Chrome binary found. Pass --chrome /path/to/chrome.")
    return Path(found)


def find_font(fonts_dir: Path | None, *names: str) -> Path | None:
    dirs = [fonts_dir] if fonts_dir else []
    dirs += list(DEFAULT_FONT_DIRS)
    for directory in dirs:
        if not directory or not directory.is_dir():
            continue
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def font_css(fonts_dir: Path | None) -> str:
    faces = [
        ("Manrope", 800, ("Manrope-ExtraBold.ttf",)),
        ("Manrope", 600, ("Manrope-SemiBold.ttf",)),
        ("IBM Plex Sans", 400, ("IBMPlexSans-Regular.ttf",)),
        ("IBM Plex Sans", 500, ("IBMPlexSans-Medium.ttf",)),
    ]
    rules = []
    for family, weight, names in faces:
        path = find_font(fonts_dir, *names)
        if path is None:
            continue
        data = base64.b64encode(path.read_bytes()).decode()
        rules.append(
            "@font-face{font-family:'%s';font-style:normal;font-weight:%d;"
            "src:url(data:font/ttf;base64,%s) format('truetype');font-display:block}"
            % (family, weight, data)
        )
    return "\n".join(rules)


BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1280px;height:640px;overflow:hidden}
body{
  background:#f4f0e9;color:#14120f;
  font-family:'IBM Plex Sans','Fira Sans','DejaVu Sans',sans-serif;
  -webkit-font-smoothing:antialiased;
}
.grain{
  position:absolute;inset:0;pointer-events:none;opacity:.5;mix-blend-mode:multiply;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/><feColorMatrix type='saturate' values='0'/></filter><rect width='180' height='180' filter='url(%23n)' opacity='.16'/></svg>");
}
.grid{
  position:absolute;inset:0;pointer-events:none;
  background-image:linear-gradient(to right, rgba(20,18,15,.045) 1px, transparent 1px),
                   linear-gradient(to bottom, rgba(20,18,15,.045) 1px, transparent 1px);
  background-size:64px 64px;
}
.rule{position:absolute;height:1px;background:rgba(20,18,15,.14)}
.eyebrow{
  font-family:'IBM Plex Sans',sans-serif;font-weight:500;font-size:11.5px;
  letter-spacing:.18em;text-transform:uppercase;color:#7a7264;
}
.mono{font-family:'Fira Sans','DejaVu Sans Mono',monospace;letter-spacing:.02em}
.shard{position:absolute;background:#14120f}
.card{
  position:absolute;background:#fffdf8;border:1px solid rgba(20,18,15,.10);
  box-shadow:0 34px 60px -22px rgba(40,30,15,.38), 0 2px 0 rgba(20,18,15,.05);
}
.plate{
  position:absolute;background:#0d0c0b;color:#f7f3ea;
  box-shadow:0 40px 70px -26px rgba(30,22,10,.55), 0 0 0 1px rgba(0,0,0,.4);
}
.stamp{
  position:absolute;border:2.5px solid #f26419;color:#f26419;background:rgba(244,240,233,.86);
  font-family:'IBM Plex Sans',sans-serif;font-weight:500;letter-spacing:.16em;
  text-transform:uppercase;text-align:center;
}
.hl{position:relative;color:#f26419;display:inline-block}
.hl i{
  position:absolute;left:-4px;right:-6px;bottom:4px;height:10px;background:rgba(242,100,25,.30);
  border-radius:3px;transform:rotate(-.8deg) skewX(-9deg);z-index:-1;
}
"""


HERO_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
@FONT_CSS@
@BASE_CSS@
.head{position:absolute;left:56px;top:132px;width:640px}
.title{
  font-family:'Manrope','Fira Sans',sans-serif;font-weight:800;font-size:104px;
  line-height:.9;letter-spacing:-.038em;
}
.sub{position:absolute;left:56px;top:404px;width:560px;font-size:19px;line-height:1.5;color:#4a4438}
.sub b{font-weight:500;color:#14120f}
.flow{position:absolute;left:56px;top:520px;display:flex;align-items:center;gap:10px}
.flow span{font-family:'IBM Plex Sans',sans-serif;font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#4a4438}
.flow em{color:#f26419;font-style:normal}
.foot{position:absolute;left:56px;bottom:36px;font-size:11.5px;letter-spacing:.06em;color:#8b8272}
.tiny{position:absolute;top:40px}
.plate-q{left:800px;top:92px;width:400px;height:190px;transform:rotate(-1.8deg);padding:26px 28px;z-index:2}
.plate-q .eyebrow{color:#f26419}
.plate-q .line{margin-top:22px;font-size:19px;line-height:1.35;color:#f7f3ea;font-weight:500}
.plate-q .meta{margin-top:16px;font-size:11.5px;letter-spacing:.1em;color:#9a9284;text-transform:uppercase}
.plate-v{left:924px;top:322px;width:336px;height:226px;transform:rotate(1.3deg);padding:24px 26px;z-index:1}
.plate-v .big{font-family:'Manrope',sans-serif;font-weight:800;font-size:52px;letter-spacing:-.03em;margin-top:18px}
.plate-v .big em{font-style:normal;color:#f26419}
.plate-v .cmd{margin-top:6px;font-size:13.5px;color:#b9b1a2}
.plate-v .time{position:absolute;right:26px;bottom:22px;font-size:12px;letter-spacing:.12em;color:#7f7768;text-transform:uppercase}
.plate-v .suite{position:absolute;left:26px;bottom:22px;font-size:12px;letter-spacing:.12em;color:#7f7768;text-transform:uppercase}
.stamp-v{left:908px;top:280px;padding:10px 18px 9px;transform:rotate(-6deg);font-size:12.5px;z-index:3;box-shadow:0 8px 18px -10px rgba(40,30,15,.4)}
.shard-b{left:1204px;top:556px;width:28px;height:28px;transform:rotate(28deg);clip-path:polygon(50% 0,62% 38%,100% 50%,62% 62%,50% 100%,38% 62%,0 50%,38% 38%)}
</style></head>
<body>
  <div class="grid"></div>
  <div class="grain"></div>

  <div class="eyebrow tiny" style="left:56px">Codex + Gemini · two-way review</div>
  <div class="eyebrow tiny" style="right:56px">github.com/uahadov/codexgravity</div>
  <div class="rule" style="left:56px;right:56px;top:78px"></div>

  <div class="head">
    <div class="title">Two coders.<br>One <span class="hl"><i></i>review.</span></div>
  </div>

  <div class="sub">
    Codex writes the plan. Gemini tries to break it: <b>blocking issues, questions,
    a second opinion</b>. Then your tests decide. Nothing is called “done” on vibes.
  </div>

  <div class="flow mono">
    <span>plan</span><em>→</em><span>challenge</span><em>→</em><span>revise</span><em>→</em><span>verify</span>
  </div>

  <div class="rule" style="left:56px;right:56px;bottom:64px"></div>
  <div class="foot mono">every session is saved · transcript.jsonl · report.md · result.json</div>

  <div class="shard shard-b shard"></div>

  <div class="plate plate-q">
    <div class="eyebrow">Gemini → Codex · blocked</div>
    <div class="line">No test for the 429 path.<br>Which backoff did you pick?</div>
    <div class="meta">2 blocking issues · round 1</div>
  </div>

  <div class="plate plate-v">
    <div class="eyebrow" style="color:#f26419">verification</div>
    <div class="big">exit <em>0</em></div>
    <div class="cmd mono">$ pytest -q</div>
    <div class="suite">3 files · 0 failures</div>
    <div class="time">4.31s · verified</div>
  </div>

  <div class="stamp stamp-v">approved by both</div>
</body></html>
"""


FLOW_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
@FONT_CSS@
@BASE_CSS@
.head{position:absolute;left:56px;top:92px;width:1080px}
.title{font-family:'Manrope',sans-serif;font-weight:800;font-size:54px;line-height:1.0;letter-spacing:-.032em}
.sub{position:absolute;left:56px;top:232px;font-size:17px;color:#4a4438}
.plates{position:absolute;left:56px;right:56px;top:296px;display:grid;grid-template-columns:repeat(4,1fr);gap:18px}
.plate-4{position:relative;height:236px;background:#fffdf8;border:1px solid rgba(20,18,15,.10);
  box-shadow:0 28px 50px -26px rgba(40,30,15,.35);padding:22px 22px}
.plate-4 .n{font-size:11.5px;letter-spacing:.18em;color:#a49b8a}
.plate-4 .w{font-family:'Manrope',sans-serif;font-weight:800;font-size:34px;letter-spacing:-.03em;margin-top:40px}
.plate-4 .c{margin-top:14px;font-size:14.5px;line-height:1.45;color:#4a4438}
.plate-4 .tag{position:absolute;left:22px;bottom:18px;font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#a49b8a}
.plate-4 .bar{position:absolute;left:22px;right:22px;top:0;height:3px;background:#14120f}
.plate-4.dark{background:#0d0c0b;color:#f7f3ea;border-color:#0d0c0b}
.plate-4.dark .n{color:#8a8172}
.plate-4.dark .c{color:#bdb5a6}
.plate-4.dark .tag{color:#8a8172}
.plate-4.dark .bar{background:#f26419}
.plate-4.dark .w em{font-style:normal;color:#f26419}
.foot{position:absolute;left:56px;bottom:34px;font-size:11.5px;letter-spacing:.06em;color:#8b8272}
.shard-a{right:56px;top:94px;width:34px;height:34px;transform:rotate(14deg);
  clip-path:polygon(50% 0,62% 38%,100% 50%,62% 62%,50% 100%,38% 62%,0 50%,38% 38%)}
</style></head>
<body>
  <div class="grid"></div>
  <div class="grain"></div>

  <div class="eyebrow" style="position:absolute;left:56px;top:40px">How a round works</div>
  <div class="eyebrow" style="position:absolute;right:56px;top:40px">no mascots · no vibes · just checks</div>
  <div class="rule" style="left:56px;right:56px;top:78px"></div>
  <div class="shard shard-a"></div>

  <div class="head"><div class="title">Two opinions. One decision.<br>Then <span style="color:#f26419">the tests decide.</span></div></div>
  <div class="sub">Both agents can ask, answer and block. The loop ends only when every reviewer approves.</div>

  <div class="plates">
    <div class="plate-4"><div class="bar"></div><div class="n mono">01</div>
      <div class="w">Plan</div>
      <div class="c">Codex writes the approach: files, edge cases, test strategy.</div>
      <div class="tag mono">codex</div></div>
    <div class="plate-4"><div class="bar" style="background:#f26419"></div><div class="n mono">02</div>
      <div class="w">Challenge</div>
      <div class="c">Gemini lists real, blocking problems — or the change stays blocked.</div>
      <div class="tag mono">gemini</div></div>
    <div class="plate-4"><div class="bar"></div><div class="n mono">03</div>
      <div class="w">Revise</div>
      <div class="c">Questions answered, weak parts rewritten, dissent kept on record.</div>
      <div class="tag mono">both</div></div>
    <div class="plate-4 dark"><div class="bar"></div><div class="n mono">04</div>
      <div class="w"><em>Verify</em></div>
      <div class="c">Real command, real exit code. Not 0? Not verified.</div>
      <div class="tag mono">pytest -q</div></div>
  </div>

  <div class="foot mono">codex-gravity · plan → challenge → revise → verify</div>
</body></html>
"""


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(26)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path} is not a PNG")
    return struct.unpack(">II", header[16:24])


def render(chrome: Path, html: str, out: Path, fonts: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cg-shots-") as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html, encoding="utf-8")
        command = [
            str(chrome), "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--no-sandbox", "--disable-dev-shm-usage",
            f"--force-device-scale-factor={SCALE}",
            f"--window-size={WIDTH},{HEIGHT}",
            "--virtual-time-budget=2500",
            f"--screenshot={out}",
            page.as_uri(),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if not out.is_file():
        raise SystemExit(f"Chromium produced no screenshot:\n{result.stdout}\n{result.stderr}")
    width, height = png_size(out)
    print(f"wrote {out.relative_to(ROOT)} · {width}x{height} · {out.stat().st_size // 1024} KB")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the editorial repo images")
    parser.add_argument("--fonts-dir", default=None, help="Directory with Manrope/IBM Plex TTFs")
    parser.add_argument("--chrome", default=None, help="Chromium/Chrome binary")
    args = parser.parse_args()

    chrome = find_chrome(args.chrome)
    fonts_dir = Path(args.fonts_dir).expanduser() if args.fonts_dir else None
    fonts_css = font_css(fonts_dir)
    if not fonts_css:
        print("warning: Manrope/IBM Plex not found, falling back to the system stack", file=sys.stderr)

    for name, template, out in (
        ("social-preview", HERO_HTML, DOCS / "social-preview.png"),
        ("how-it-works", FLOW_HTML, DOCS / "how-it-works.png"),
    ):
        html = template.replace("@FONT_CSS@", fonts_css).replace("@BASE_CSS@", BASE_CSS)
        render(chrome, html, out, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
