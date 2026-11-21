# Diagram sources

`build.py` generates the four blueprint-style sheets as self-contained HTML (`01-architecture.html` …).
The PNGs in `../img/` are rendered from them at 1600×1000 (@2x).

```bash
python docs/diagrams/build.py                       # regenerate the HTML
# render with any headless Chromium, e.g. Playwright:
#   page.set_viewport_size({"width": 1600, "height": 1000}); page.goto("file://…/01-architecture.html")
#   page.screenshot(path="docs/img/01-architecture.png")
```
Numbers on the sheets come from the README benchmark section (500k profiles × 259 KPIs on 2 vCPU).
Fonts: Saira Condensed + IBM Plex Mono via Google Fonts (set `FONT_DIR` to embed local woff2 files when offline).
