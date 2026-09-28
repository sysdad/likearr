"""Take the three README screenshots from a running demo server.

First write and start the demo server (see `scripts/demo_state.py`):

    uv run python scripts/demo_state.py /tmp/likearr-demo
    LIKEARR_UI_PASSWORD='a demo password, 16+ chars' LIKEARR_SPOTIFY_CLIENT_ID=demo \\
        uv run likearr start -c /tmp/likearr-demo/config.toml --port 8770

Then, in another terminal, run this script:

    uv run --with playwright python scripts/readme_screenshots.py <password file> <plan id>

`<password file>` holds the demo's `LIKEARR_UI_PASSWORD` and nothing else - never pass the
password on the command line, where it would end up in shell history and `ps`, and this script
never prints it. `<plan id>` is the "Review changes" plan to screenshot: take it from
`scripts/demo_state.py`'s own output line (`/plan/<plan id>  (Review changes)`), or from the demo
directory's job list (`ui/jobs/<plan id>/diff.json`).

Writes `docs/images/status.png`, `docs/images/review-changes.png` and `docs/images/look-up.png`
(override with `--out-dir`): 1280px wide, light theme, each cropped to its page's real content
height. Requires `playwright`, deliberately not a project dependency - installed for this one run
with `--with playwright` above. The first run on a machine also needs the browser binary:

    uv run --with playwright playwright install chromium
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8770"
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "images"

# (screenshot name, path (may hold "{plan_id}"), crop height in px)
SHOTS: list[tuple[str, str, int]] = [
    ("status", "/", 790),
    ("review-changes", "/plan/{plan_id}", 1310),
    ("look-up", "/explain?query=Abbey+Road", 605),
]


def take_screenshots(password: str, plan_id: str, out_dir: Path, *, base_url: str = DEFAULT_BASE_URL) -> None:
    """Log in to the demo server at `base_url` and write each of `SHOTS` under `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 900}, color_scheme="light", device_scale_factor=1
        )
        page = context.new_page()
        page.goto(f"{base_url}/login", timeout=15000)
        page.fill("input[type=password]", password)
        page.keyboard.press("Enter")
        page.wait_for_url(f"{base_url}/", timeout=15000)
        for name, path, height in SHOTS:
            page.goto(base_url + path.format(plan_id=plan_id), timeout=15000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.screenshot(
                path=str(out_dir / f"{name}.png"),
                full_page=True,
                clip={"x": 0, "y": 0, "width": 1280, "height": height},
            )
            print(name, page.title())
        browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Take the three README screenshots from a running demo server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("password_file", type=Path, help="a file holding LIKEARR_UI_PASSWORD, nothing else")
    parser.add_argument(
        "plan_id", help="the Review changes plan id to screenshot, from scripts/demo_state.py's own output"
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help=f"the demo server's URL (default: {DEFAULT_BASE_URL})"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help=f"where to write the screenshots (default: {DEFAULT_OUT_DIR})"
    )
    args = parser.parse_args(argv)

    password = args.password_file.read_text().strip()
    take_screenshots(password, args.plan_id, args.out_dir, base_url=args.base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
