# Life Dashboard

Renders an 800x480 1-bit image on GitHub Actions every 15 min, publishes it to GitHub Pages,
and a TRMNL 7.5" DIY kit downloads and displays it.

- `render/` — HTML template + Python renderer (`python render/build.py --mock` to preview locally)
- `content.yaml` — focus line and to-dos (edit from the GitHub app)
- `firmware/LifeDashboard/` — Arduino sketch for the device
- `.github/workflows/render.yml` — schedule + deploy

Frame format: 48,000 bytes, row-major, MSB-first, 1 = black.
