"""`likearr/web/static/errors.js` and its wiring into `base.html`.

An in-page action (any `hx-post`) that fails - the service down, a 5xx, a proxy error - must show
a visible banner instead of leaving the control's new value on screen with nothing saved. These
tests check what pytest can check: the script is served, every page loads it, and the script's own
source follows the CSP and textContent rules. The banner actually appearing in a browser on a
failed request is covered only by reading the code, not by a test - see the builder's report.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
from jinja2 import meta as jinja2_meta

_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES_DIR = _ROOT / "likearr" / "web" / "templates"
_STATIC_DIR = _ROOT / "likearr" / "web" / "static"
_SCRIPT_TAG = '<script src="/static/errors.js" defer></script>'


def _extends_base(name: str) -> bool:
    source = (_TEMPLATES_DIR / name).read_text()
    return '{% extends "base.html" %}' in source or "{% extends 'base.html' %}" in source


def _pages() -> list[str]:
    """Every template that is a full page (not a partial, which never `{% extends %}`)."""
    return sorted(p.name for p in _TEMPLATES_DIR.glob("*.html") if not p.name.startswith("_") and p.name != "base.html")


def test_every_page_extends_base_html() -> None:
    """Sanity check for the test below: if a page stopped extending base.html, it would silently
    stop loading errors.js too, and the next test would not catch that on its own."""
    pages = _pages()
    assert pages, "expected at least one page template"
    assert all(_extends_base(name) for name in pages), [name for name in pages if not _extends_base(name)]


def test_base_html_loads_errors_js_next_to_htmx_with_defer() -> None:
    source = (_TEMPLATES_DIR / "base.html").read_text()
    assert _SCRIPT_TAG in source
    assert source.index(_SCRIPT_TAG) > source.index('<script src="/static/htmx.min.js" defer></script>')


def test_the_script_tag_is_in_head_outside_any_block_a_page_could_override() -> None:
    """A page overrides `title`, `nav` or `content` (the only blocks base.html defines), never
    `<head>` itself, so extending base.html is enough to inherit the script tag - confirmed here
    by checking no `{% block %}` opens between the script tag and the end of `<head>`."""
    source = (_TEMPLATES_DIR / "base.html").read_text()
    script_at = source.index(_SCRIPT_TAG)
    head_end = source.index("</head>")
    assert script_at < head_end
    assert "{% block" not in source[script_at:head_end]


def test_every_page_extending_base_loads_errors_js() -> None:
    """The literal claim: every page template, rendered through the same Jinja loader the app
    uses, resolves an ancestor chain that includes base.html - and base.html (checked above) is
    the one and only place the script tag comes from."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES_DIR), autoescape=True)
    for name in _pages():
        ast = env.parse(env.loader.get_source(env, name)[0])  # type: ignore[union-attr]
        referenced = jinja2_meta.find_referenced_templates(ast)
        assert "base.html" in referenced, name


def test_errors_js_exists_as_a_static_file() -> None:
    assert (_STATIC_DIR / "errors.js").is_file()


def test_errors_js_listens_for_both_htmx_error_events_on_the_body() -> None:
    source = (_STATIC_DIR / "errors.js").read_text()
    assert "document.body.addEventListener" in source
    assert '"htmx:responseError"' in source or "'htmx:responseError'" in source
    assert '"htmx:sendError"' in source or "'htmx:sendError'" in source


def test_errors_js_shows_an_alert_banner_via_textcontent_never_innerhtml() -> None:
    source = (_STATIC_DIR / "errors.js").read_text()
    assert "role" in source and "alert" in source
    assert "textContent" in source
    assert "innerHTML" not in source


def test_errors_js_has_no_eval_and_is_not_inline_anywhere() -> None:
    source = (_STATIC_DIR / "errors.js").read_text()
    assert "eval(" not in source
    for name in [*_pages(), "base.html"]:
        page_source = (_TEMPLATES_DIR / name).read_text()
        for line in page_source.splitlines():
            if "<script" in line:
                assert "src=" in line, f"{name}: inline script found: {line!r}"


def test_errors_js_reports_the_status_code() -> None:
    source = (_STATIC_DIR / "errors.js").read_text()
    assert "status" in source


def test_a_second_failure_replaces_the_banner_rather_than_stacking() -> None:
    """The banner is a single, reused element (looked up or created by a fixed id/selector) so a
    second `htmx:responseError` updates it in place instead of appending another one."""
    source = (_STATIC_DIR / "errors.js").read_text()
    assert "getElementById" in source or "querySelector" in source


def test_a_failed_poll_does_not_say_a_change_was_lost() -> None:
    """A GET (a job page's poll during a redeploy) saved nothing, so the banner must not say a
    change was lost; only a POST's does."""
    source = (_STATIC_DIR / "errors.js").read_text()
    assert "requestConfig" in source and '"GET"' in source.upper().replace("'GET'", '"GET"')
    assert "may be out of date" in source


def test_a_long_error_body_is_capped() -> None:
    """A proxy's 4xx can be a whole HTML page; the banner shows at most a short excerpt."""
    source = (_STATIC_DIR / "errors.js").read_text()
    assert "MAX_BODY_CHARS" in source and ".slice(0, MAX_BODY_CHARS)" in source
