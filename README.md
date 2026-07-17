# DOKI Factory Automation — Simulators

Static site, no build step. Deploy as-is on Vercel (framework preset: **Other** / static).

- `/` — landing page linking to both simulators
- `/mincecraft/` — MINCECRAFT guided-weighing station simulator
- `/schedule1/` — Schedule 1 dough-mixer timer simulator

The two simulators cross-link to each other via relative paths (`../mincecraft/`, `../schedule1/`), so this folder structure must be preserved.
