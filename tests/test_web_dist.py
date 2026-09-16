"""前端产物：只引相对资源、不引任何 CDN（hub 常在内网）。

2.3 起产物是独立交付物，在 web/dist（进版本库，但不再随 Python 包分发）——
部署时解包给 nginx，或者用 serve.webDir 让 hub 自己托管。
"""
import os
import re
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"

INDEX = WEB_DIR / "index.html"


@pytest.mark.skipif(not INDEX.is_file(), reason="还没构建前端（cd web && npm run build）")
def test_dist_is_self_contained():
    html = INDEX.read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs and all(r.startswith("./assets/") for r in refs), refs
    for r in refs:
        assert (WEB_DIR / r[2:]).is_file(), r
    for css in (WEB_DIR / "assets").glob("*.css"):
        text = css.read_text(encoding="utf-8")
        assert "url(http" not in text and "@import url(http" not in text
    assert not os.path.exists(WEB_DIR / "index.html.map")
