# Changelog

Newest first. Releases are tags (`vX.Y.Z`).

## v0.3.0 -- 2026-11-09

- `Space u p` (show or hide the preview) now comes from Vyom core, which also remembers it per tool; the tool's own copy is gone.
- **Vim keys** (Vyom 0.7): patra opens in NORMAL mode, where letters are keys
  and typing changes nothing; `/` (or `i`) types a filter, `Esc` goes back.
  `Space` is the leader, with a which-key strip; `?` lists every key and
  `Space Space` is a palette of every action.
- **New keys**: `x` stops or starts, `R` restarts, `o` opens a shell, `yy`
  copies the name, `r` refreshes; `Space c` acts on the container,
  `Space y i` / `y p` copy its ID / ports, `Space f r` / `f s` / `f t` show only
  running / stopped / the ones that need you, `Space s c` / `s m` / `s n` sort
  by CPU / memory / name, `Space u p` hides the preview.
- **Remove asks with one question**: `dd` (or `Space d d`) asks "Remove …?",
  `y` removes and any other key keeps it, instead of pressing twice.
- **Typing is safe**: `Ctrl+Backspace` deletes a word in the filter again
  (it removed a container); it now says remove moved to `dd`, and `Ctrl+L`
  says logs are on `Enter`. `Ctrl+R`, `Ctrl+S` and `Ctrl+X` still work.
- Keys are data (`patra/vim.py`); `scripts/keybinds.py` writes the
  `# keybind:` lines and the README table from it (`--check` in CI).
- Needs Vyom 0.7.0.

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
