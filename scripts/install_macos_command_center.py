#!/usr/bin/env python3
"""Install the Finder-launchable JOÃO.AI app bundle for THIS worktree.

The launcher opens an instant local splash ("JOÃO.AI démarre…") while the
server boots on a fixed port, exports a provider-capable PATH, logs
unbuffered to a stable file, and always points at the repository this
script is run from. A temporary .icns is generated from the mascot SVG when
the macOS image tools are available (the final AI mascot lands later).
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_NAME = "JOÃO.AI"
APP = Path(f"/Applications/{APP_NAME}.app")
PORT = 4747

MASCOT_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>
<rect width='64' height='64' rx='14' fill='#11141c'/>
<circle cx='32' cy='36' r='21' fill='#f2c49b'/>
<path d='M11 32 Q11 12 32 12 Q53 12 53 32 L53 34 L11 34 Z' fill='#ffcf3f'/>
<rect x='8' y='31' width='48' height='6' rx='3' fill='#f5b91e'/>
<rect x='28' y='8' width='8' height='8' rx='2' fill='#ffcf3f'/>
<circle cx='24' cy='42' r='6.5' fill='#fff'/><circle cx='40' cy='42' r='6.5' fill='#fff'/>
<circle cx='24' cy='42' r='4.6' fill='#1a1208'/><circle cx='40' cy='42' r='4.6' fill='#1a1208'/>
<circle cx='25.5' cy='40.5' r='1.4' fill='#fff'/><circle cx='41.5' cy='40.5' r='1.4' fill='#fff'/>
<path d='M22 52 Q27 49 32 51.5 Q37 49 42 52 Q37 56.5 32 54.5 Q27 56.5 22 52 Z' fill='#4a2f1a'/>
</svg>"""

SPLASH = """<!doctype html>
<meta charset="utf-8"><title>JOÃO.AI démarre…</title>
<style>
body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;
 background:linear-gradient(160deg,#0b0e1a,#1a1440);color:#e8eaf0;
 font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
@keyframes shine{0%{background-position:0% 50%}100%{background-position:200% 50%}}
.wordmark{font-size:34px;font-weight:800;letter-spacing:.5px;
 background:linear-gradient(110deg,#7dd3fc,#c084fc,#f0abfc,#67e8f9,#a5f3fc,#7dd3fc);
 background-size:200% auto;-webkit-background-clip:text;background-clip:text;color:transparent;
 animation:shine 5s linear infinite}
.dot{color:#9aa1b5}
@keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}
.status{color:#9aa1b5;animation:pulse 1.6s ease infinite}
</style>
__MASCOT__
<div class="wordmark">JOÃO.AI</div>
<div class="status">JOÃO.AI démarre… la Bulle s'ouvre dès que le serveur est prêt</div>
<div class="dot" id="tries"></div>
<script>
const url="http://127.0.0.1:__PORT__/";let tries=0;
async function poll(){tries++;document.getElementById('tries').textContent=tries>4?tries+'s':'';
 try{await fetch(url,{mode:'no-cors',cache:'no-store'});location.href=url}
 catch(_){setTimeout(poll,1000)}}
poll();
</script>"""


def write(path: Path, value: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    if mode is not None:
        os.chmod(path, mode)


def build_icns(target: Path) -> bool:
    """Best-effort temporary icon from the mascot SVG (qlmanage + sips + iconutil)."""
    if not all(shutil.which(tool) for tool in ("qlmanage", "sips", "iconutil")):
        return False
    with tempfile.TemporaryDirectory(prefix="joao-icon-") as tmp:
        tmpdir = Path(tmp)
        svg = tmpdir / "mascot.svg"
        svg.write_text(MASCOT_SVG)
        render = subprocess.run(
            ["qlmanage", "-t", "-s", "1024", "-o", str(tmpdir), str(svg)],
            capture_output=True, text=True)
        png = tmpdir / "mascot.svg.png"
        if render.returncode != 0 or not png.is_file():
            return False
        iconset = tmpdir / "joao.iconset"
        iconset.mkdir()
        for size in (16, 32, 64, 128, 256, 512, 1024):
            name = f"icon_{size}x{size}.png" if size <= 512 else "icon_512x512@2x.png"
            resample = subprocess.run(
                ["sips", "-z", str(size), str(size), str(png),
                 "--out", str(iconset / name)], capture_output=True)
            if resample.returncode != 0:
                return False
        result = subprocess.run(["iconutil", "-c", "icns", str(iconset),
                                 "-o", str(target)], capture_output=True)
        return result.returncode == 0 and target.is_file()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, default=APP)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    app = args.app.expanduser().resolve()
    repo = args.repo.expanduser().resolve()
    cli = repo / "src/joao_orchestrator/cli/joao.py"
    if not cli.is_file():
        parser.error(f"JOAO CLI not found: {cli}")
    executable_name = app.stem
    contents = app / "Contents"
    resources = contents / "Resources"
    splash = resources / "splash.html"
    info = {
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": executable_name,
        "CFBundleIdentifier": "ai.joao.command-center",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.3",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    }
    icon = resources / "joao.icns"
    if build_icns(icon):
        info["CFBundleIconFile"] = "joao.icns"
    log = Path("~/.local/share/joao/command-center.log").expanduser()
    launcher = (
        "#!/bin/zsh\n"
        "export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin\"\n"
        f"mkdir -p {shlex.quote(str(log.parent))}\n"
        # Instant feedback: the splash opens immediately and hands over to the
        # Bubble as soon as the fixed-port server answers.
        f"open {shlex.quote(str(splash))}\n"
        f"exec {shlex.quote(sys.executable)} -u {shlex.quote(str(cli))} ui "
        f"--port {args.port} --no-open "
        f">>{shlex.quote(str(log))} 2>&1\n"
    )
    write(contents / "Info.plist", plistlib.dumps(info))
    write(splash, SPLASH.replace("__PORT__", str(args.port))
          .replace("__MASCOT__", MASCOT_SVG.replace("<svg ", "<svg width='96' height='96' ", 1))
          .encode("utf-8"))
    write(contents / "MacOS" / executable_name, launcher.encode("utf-8"), 0o755)
    print(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
