"""Guards for the config page, which can read the dashboard login token."""

import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"


def test_page_policy_blocks_other_origins():
    html = (UI / "index.html").read_text()
    policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "http" not in policy and "*" not in policy


def test_page_has_no_html_injection_sinks_or_external_urls():
    code = (UI / "app.js").read_text() + (UI / "index.html").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in code, sink
    assert not re.search(r"https?://", code)


def test_token_only_goes_to_same_origin_api_paths():
    js = (UI / "app.js").read_text()
    urls = re.findall(r'fetch\(\s*("[^"]*"|SETTINGS_URL)', js)
    assert urls and all(u == "SETTINGS_URL" or u.startswith('"/api/plugins/') for u in urls)


def test_license_present():
    root = UI.parent
    assert "BSD Zero Clause License" in (root / "LICENSE").read_text()
    assert 'license = "0BSD"' in (root / "pyproject.toml").read_text()
