# Changelog

Newest first. Releases are tags (`vX.Y.Z`).

## v0.2.0 -- 2026-11-07

- **A new look** (Vyom 0.6): a summary at the top (running, unhealthy in red,
  stopped); the containers that need you (unhealthy, restarting, failed) come
  first, in red, with "restarted 3× in 10 min" when Docker's events say so;
  each compose project says how it's doing ("needs you", "all running").
- **Live CPU and memory sparklines** on every running container.
- **Logs in colour** in the preview: ERROR red, WARN yellow, INFO green,
  times dimmed; the image and ports above them.
- **Keys moved to match the buttons**: `Ctrl+S` opens a shell (was `Ctrl+T`),
  `Ctrl+X` stops or starts (was `Ctrl+S`), `Ctrl+L` follows the logs.
- **Remove asks first**: `Ctrl+Backspace` once shows a red note, a second
  time within 5 seconds removes the container.
- Needs Vyom 0.6.0.

- Every corner is rounded: the website's callouts.
- A logo and a shloka at the top of the README and the website: every Vayu tool has them now.
- Screenshots in the README and on the website.

## v0.1.4 -- 2026-10-21

- A website (GitHub Pages, https://theyashsawarkar.github.io/patra/): the README as a home page, the
  changelog, and architecture decision records (`docs/adr/`), built from these files by
  `scripts/build-site.py`; CI checks the site is up to date.

## v0.1.3 -- 2026-10-21

- Documentation for open source: a logo and banner, a full README (features,
  install, usage, configuration, troubleshooting, the whole Vyom family),
  CONTRIBUTING, SECURITY and CODE_OF_CONDUCT, issue and pull request
  templates, and CI (compiles, checks plugin.toml and that every key is
  documented).
- plugin.toml has its icon again (it was empty).

## v0.1.2 -- 2026-10-19

- Opens at Vyom's shared panel size (Vyom 0.2.5) like every other tool:
  its own width/height are gone.

## v0.1.1 -- 2026-10-19

- Keys are spelled out (Ctrl+Enter, Shift+Enter, Backspace) instead of symbols
  (⌃↵, ⇧↵, ⌫) in the footer and on rows and buttons.

## v0.1.0 -- 2026-10-19

- First release, as its own repository: docker containers. Replaces docker-picker.py in Vayu.
- Every container, **grouped by compose project**, running ones first, failed ones flagged.
- **Live CPU and memory** per container, and its ports.
- The preview shows the **last log lines**, and restart / stop / start / shell buttons.
- `Enter` follows the logs in a terminal; `Ctrl+T` opens a shell inside the container.
- Never wakes a stopped Docker daemon (it's often socket-activated): it says it's stopped instead.
