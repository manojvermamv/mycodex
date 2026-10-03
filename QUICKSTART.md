# mycodex Quickstart

This path installs mycodex, logs in your ChatGPT accounts, launches the official Codex
TUI with automatic rotation, and connects the ChatGPT phone app through remote control.
The current version in this repository is `0.2.0`.

## 1. Check prerequisites

You need:

- a Linux host with a systemd user instance (Debian 13 tested);
- Python 3.10 or newer (`python3 --version`);
- the official [Codex CLI](https://developers.openai.com/codex/cli) 0.160.x;
- one or more ChatGPT accounts with Codex access.

Install Codex if it is missing:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

So that remote control keeps running after you log out or reboot, enable lingering once:

```bash
sudo loginctl enable-linger "$USER"
```

## 2. Install mycodex

```bash
git clone https://github.com/manojvermamv/mycodex.git ~/mycodex
~/mycodex/install.sh
```

`~/.local/bin` must be on your `PATH`. Then check the stack:

```bash
mycodex --version
mycodex doctor
```

`doctor` should report `overall ok` (or warnings that only concern accounts you have not
added yet).

## 3. Add your accounts

Log in each account once. On a server, the default device-code flow prints a link and a
one-time code to open on any device:

```bash
mycodex profile add
```

Repeat for every account. Profiles are named after the account email
(`you_example.com`); pass a name to choose your own (`mycodex profile add work`). Each login
is stored in its own Codex home, `~/.codex/profiles/<name>`, and reused, so you do not log
in again. The first account you add becomes the active one.

Already using prodex? Move its accounts instead of logging in again:

```bash
mycodex migrate --dry-run
mycodex migrate
```

When `mycodex doctor` passes, prodex can be uninstalled; the README's
[Coming from prodex](README.md#coming-from-prodex) lists the four commands.

Optional short names:

```bash
mycodex profile alias you_example.com work
```

## 4. Check quota and readiness

```bash
mycodex profile list
mycodex quota
```

`ROTATION READY` shows which accounts can take requests right now.

## 5. Launch Codex

```bash
mycodex
```

You are in the official Codex TUI, on the active account, with rotation on. Everything
Codex accepts works the same way:

```bash
mycodex --profile work              # start on a specific account
mycodex resume --last               # continue the latest thread (any account can resume it)
mycodex exec "review this repository"
mycodex --no-rotate                 # this account only for this session
```

All accounts share `~/.codex`, so threads, history, skills and settings are the same
whichever account you start on.

## 6. Tune rotation (optional)

```bash
mycodex rotation status
mycodex rotation order work personal    # which account is tried next
mycodex rotation disable personal       # never switch to it
mycodex rotation disable                # turn rotation off for every launch
mycodex rotation log -f                 # watch switches as they happen
```

When the active account hits a usage limit, the next request goes to the next ready
account, inside the same session and thread. The exhausted account is paused until its
limit resets.

## 7. Connect your phone

Choose the account your ChatGPT phone app is signed in to and start remote control:

```bash
mycodex --profile work remote-control
```

mycodex starts the `mycodex-remote` service, waits until the host is connected, and shows
the host name, paired devices and the rotation proxy. In the ChatGPT app, open Codex and
pick this host. If it does not appear, create a pairing code and enter it in the app:

```bash
mycodex remote pair
```

To start a new thread inside a project (the project is created if it does not exist yet):

```bash
mycodex remote seed ~/your-project --name "First task" --message "Describe the first task here"
```

Threads you start in the terminal show on the phone too, but outside any project; see
[docs/projects-and-threads.md](docs/projects-and-threads.md) for why, and for doing the
same from a script through `mycodex app-server proxy`.

Prefer one account with no rotation for the phone?

```bash
mycodex --profile work remote-control --pinned
```

The phone and your terminal sessions share one thread list. The service restarts on its
own after crashes and keeps running after you close SSH.

## 8. Everyday checks

```bash
mycodex status            # accounts, rotation, phone link, sessions, versions
mycodex remote status     # connection, relay account, paired phones, proxy
mycodex threads           # recent threads and their provider tag
mycodex processes         # every codex / mycodex process and its account
mycodex remote logs -f    # follow the remote-control service
```

## 9. Diagnose a problem

```bash
mycodex doctor
mycodex doctor --fix
```

Common fixes:

- `auth invalid` on an account: `mycodex profile reauth NAME`
- an account still paused after its reset: `mycodex rotation reset NAME`
- a thread missing on the phone: `mycodex threads adopt ID`
- phone shows no project: `mycodex remote seed ~/your-project`
- another remote-control server is running: stop it, or `mycodex remote-control --force`
- remote service restarting: `mycodex remote logs`

More in the README's [Troubleshooting](README.md#troubleshooting).

## 10. Update or uninstall

Update:

```bash
git -C ~/mycodex pull
~/mycodex/install.sh
mycodex remote restart
```

Uninstall. Your accounts in `~/.codex/profiles` and your threads in `~/.codex` stay where
they are, so plain `codex` with `CODEX_HOME=~/.codex/profiles/<name>` still works:

```bash
mycodex remote stop
rm ~/.local/bin/mycodex ~/.config/systemd/user/mycodex-remote.service
systemctl --user daemon-reload
rm -rf ~/mycodex
```

## Next references

- [README](README.md): features, commands and troubleshooting
- [docs/projects-and-threads.md](docs/projects-and-threads.md): project threads, with one command or through the app-server proxy
- [docs/architecture.md](docs/architecture.md): how accounts, the rotation proxy and the remote service work
- [docs/discovery-and-design.md](docs/discovery-and-design.md): the investigation behind the design
