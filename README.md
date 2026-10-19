# patra

**patra** (पात्र, "vessel") — Docker containers. A tool for [Vyom](https://github.com/Theyashsawarkar/vyom), the keyboard-driven panels of [Vayu](https://github.com/Theyashsawarkar/vayu); in Vayu it replaces docker-picker.py.

> Docker containers: grouped by compose project, live CPU and memory, logs in the preview; restart, stop, start, shell, logs.

## Features

- Every container, **grouped by compose project**, running ones first, failed ones flagged.
- **Live CPU and memory** per container, and its ports.
- The preview shows the **last log lines**, and restart / stop / start / shell buttons.
- `Enter` follows the logs in a terminal; `Ctrl+T` opens a shell inside the container.
- Never wakes a stopped Docker daemon (it's often socket-activated): it says it's stopped instead.

## Keys

| Key | Action |
|---|---|
| `Enter` | Follow the selected container's logs in a terminal |
| `Ctrl+R` | Restart the selected container |
| `Ctrl+S` | Stop or start the selected container |
| `Ctrl+T` | Open a shell in the selected container |
| `Ctrl+Backspace` | Remove the selected stopped container |
| `Esc` | close |

Every Vyom panel also has: `Up`/`Down` (or `Ctrl+K`/`Ctrl+J`) to move, `Page Up`/`Page Down`, `Tab` to switch to the next tool.

## Requirements

- [Vyom](https://github.com/Theyashsawarkar/vyom) 0.2 or newer (Python 3.11+, GTK 4, gtk4-layer-shell, a layer-shell compositor)
- `docker`

## Install

```sh
git clone https://github.com/Theyashsawarkar/patra ~/.local/share/vyom/plugins/patra
vyom reload        # or log out and in
vyom patra         # open it (again: close it)
```

Bind it to a key, e.g. in sway:

```
bindsym $mod+<key> exec vyom patra
```

Update: `git -C ~/.local/share/vyom/plugins/patra pull && vyom reload`. `vyom list` shows every installed tool and why one would be skipped.

## Configuration

In `~/.config/vyom/config.toml` (all optional):

```toml
[plugin.patra]
# terminal: terminal for logs and shells (default: $TERMINAL, else kitty/foot/alacritty/wezterm)
```

## License

MIT.
