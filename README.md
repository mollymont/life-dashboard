# Life Dashboard

Renders an 800x480 1-bit image on GitHub Actions every 15 min, publishes it to GitHub Pages,
and a TRMNL 7.5" DIY kit downloads and displays it.

- `render/` — HTML template + Python renderer (`python render/build.py --mock` to preview locally)
- `content.yaml` — focus line and to-dos (edit from the GitHub app)
- `training.yaml` — the 10K plan; today's session is derived from the date
- `firmware/LifeDashboard/` — Arduino sketch for the device
- `.github/workflows/render.yml` — schedule + deploy
- `test_build.py` — `python test_build.py`, no network or browser needed

## Day-to-day: the focus line and to-dos

Both live in `content.yaml`. Edit it on github.com or in the GitHub mobile app, commit,
and the `push` trigger re-renders the screen within a minute or two — no local setup:

```yaml
focus: "Ship one small thing today."
todos:
  - Water plants
  - Book dentist
```

Five to-dos fit; extras collapse into a `+N` next to the heading. Everything else on the
screen (weather, calendar, steps, training) comes from the APIs and env vars, so
`content.yaml` is the only file you touch daily.

## Local preview

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python render/build.py --mock   # writes out/preview.png
```

Frame format: 48,000 bytes, row-major, MSB-first, 1 = black.
