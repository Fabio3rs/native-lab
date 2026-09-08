# NativeLab

[Português](README.md) | [English](README.en.md)

**Current platform: Linux only.** Isolation depends on user, PID, IPC, network,
and UTS namespaces, as well as bubblewrap.

> [!CAUTION]
> **NativeLab is an experimental, unaudited, and potentially dangerous proof
> of concept. Do not treat it as a security boundary for malware, unknown
> installers, hostile dependencies, or valuable data.**

## Why NativeLab exists

NativeLab started from a practical problem in using coding agents for
development and testing.

An agent may be sandboxed while editing a project, but real development
workflows frequently require processes that outlive a single command:

- Vite/Astro development servers;
- browser automation with Playwright;
- GUI applications under Xvfb;
- test databases and local services;
- native applications that need to communicate over localhost.

A common solution is to start these processes outside the agent sandbox. This
creates an authority gap:

```text
sandboxed agent
    |
    | modifies project files
    v
user approves "npm run dev" outside the sandbox
    |
    v
dev server / hot reload executes project code
with the user's normal host permissions
```

This is especially undesirable when project contents may be untrusted or
influenced by external input. A prompt injection, malicious dependency,
generated file, or compromised repository could affect code that is later
executed through an apparently legitimate development action. Approval is also
weak evidence of intent: starting a development server is a normal and expected
action during testing.

NativeLab moves the development workload into a persistent sandbox:

```text
                 NativeLab session
             ┌────────────────────────┐
Codex ───────►│ dev server             │
              │ browser / Playwright   │
              │ Xvfb / native app      │
              │ test processes         │
              │ private localhost      │
             └────────────────────────┘
                        │
                 no host network
                 no real user home
                 restricted filesystem
```

Processes inside the same session can communicate normally, including through
localhost, while remaining isolated from the host and the Internet. The
intended property is roughly:

```text
Authority(workload) <= Authority(NativeLab session)
```

Running another test process should not implicitly grant additional host
authority. Operations that genuinely expand authority — such as exposing
another host directory or enabling external networking — belong in trusted
NativeLab policy rather than project-controlled configuration.

This motivation drives the project's main design decisions:

| Decision | Intended property |
| --- | --- |
| host-only configuration | the project cannot grant powers to itself |
| current workspace RW | development remains functional |
| other trusted projects RO | references and dependencies without modification |
| real HOME absent | browsers and applications do not inherit personal credentials |
| private `/run` | desktop-session sockets and agents are not exposed |
| private localhost | Vite and Playwright communicate without reaching the host |
| no Internet | the process under test receives no implicit egress |

### Threat model

The primary goal is to prevent accidental capability escalation and capability
laundering in agent-driven development workflows. NativeLab is not intended to
be a hardened virtual-machine boundary for running arbitrary malware.

The design deliberately prefers:

```text
"this program does not work inside the sandbox"
```

over:

```text
"run it outside the sandbox so the test works"
```

The latter fallback is exactly what NativeLab was created to avoid.

### Example workflow

```text
Codex
  ├─ native-lab run -- bash -c 'cd web && npm run dev'
  │      └─ Astro/Vite :4321
  │
  └─ Playwright MCP
         └─ native-lab run playwright-mcp
                └─ Chromium
                     └─ http://localhost:4321
```

Both commands enter the workspace's same persistent `bubblewrap` session, so
they share the same namespaces and private localhost. They enter through
OpenSSH over a Unix socket:

```text
native-lab run npm run dev
                    │
                    │ same session / 127.0.0.1
                    ▼
native-lab run npx @playwright/mcp ...
```

SSH transports stdin, stdout, stderr, EOF, and exit status. There is no custom
daemon protocol, pipe multiplexer, or tmux.

## The security warning, without euphemisms

This PoC substantially reduces what a process can see, but it still shares the
kernel and several selected host resources. A bug in the kernel, bubblewrap,
the parser, the scripts, or the mount composition can break its assumptions.

In particular:

- the current workspace is persistent and read-write; executed code can modify
  or delete nearly any file in it, except for masked metadata;
- `.env`, `*.pem`, `*.key`, and other secrets in the current workspace are
  deliberately accessible;
- `~/.npm-global` and `~/.npm`, when present, are mounted read-only. This
  prevents sandbox persistence but allows reading and executing content that
  already exists in those directories;
- `extra_read_only` can expose any data the user places there;
- `/etc` is a read-only view of the host and may contain information readable
  by the user;
- trusted projects are filtered by names/globs, not by content classification.
  A secret with an unexpected name remains visible;
- read-only access prevents writes from the sandbox but does not create an
  immutable snapshot. A host process may still change a mounted source during
  the session;
- lack of Internet access reduces exfiltration paths, but data can still be
  written to the workspace, sent to another process on the session's localhost,
  or printed;
- the shared control plane is writable by the sandbox itself. Code inside it
  can take down the socket, destroy ephemeral keys, or cause denial of service;
- there are no cgroups, quotas, CPU/memory/process limits, custom seccomp,
  custom Landlock, pidfd, or fork-bomb protection;
- there is no VM isolation: every process uses the host kernel;
- the implementation is shell + Python and has not received a security audit.

Use it only with disposable or version-controlled workspaces, keep backups, and
run only code whose risk you accept. Failure to create any essential property
aborts startup; there is no fallback to direct host execution.

## Filesystem policy v2

The root is no longer `--ro-bind / /`. It starts empty with `--tmpfs /`, and
only explicitly allowed sources are mounted:

| View inside the session | Policy |
| --- | --- |
| `/bin`, `/sbin`, `/usr`, `/etc`, `/lib`, `/lib64` | host RO, when present |
| `/nix/store`, `/run/current-system/sw` | host RO, when present |
| `~/.npm-global`, `~/.npm` | host RO, when present |
| canonical current workspace | host RW |
| `.git`, `.agents`, `.codex` in the workspace | DENY |
| imported trusted projects | host RO with additional masks |
| remainder of the real HOME | not mounted |
| HOME's logical pathname | private RW tmpfs |
| `/tmp`, `/run`, `/dev/shm` | private RW |
| `/proc` | new procfs for the PID namespace |
| `/dev` | minimal set created by bubblewrap |
| `/var`, `/home`, and other trees | absent unless explicitly mounted |

HOME keeps the pathname returned by `getpwuid(3)`, for example
`/home/fabio`, but its contents start private and empty. The npm mounts are
applied on top of this HOME. Therefore symlinks such as
`~/.npm-global/bin/npm -> ../lib/node_modules/...` continue to work without
exposing the rest of HOME.

`XDG_RUNTIME_DIR` inside the session is `/run/user/$UID`, privately created
with mode `0700`. It is not the host's `$XDG_RUNTIME_DIR`. Therefore D-Bus,
Wayland, PipeWire/PulseAudio, gpg-agent, ssh-agent, and host X11 sockets do not
cross the boundary. `/dev/shm` is also private, allowing a browser/Xvfb to run
without sharing the host's POSIX memory.

### Current workspace

The result of `realpath "$PWD"` is the only persistent RW root. Secrets that
belong to the active work remain available:

```text
.env                 accessible
cert.pem              accessible
private.key           accessible
.git                  denied
.agents               denied
.codex                denied
```

For existing protected paths, NativeLab validates type and identity and
applies an opaque mount. Symlinks at these locations cause startup to fail
closed.

When `.git`, `.agents`, or `.codex` does not yet exist, bubblewrap needs a real
mountpoint beneath the RW bind. The solution chosen for this PoC is visible on
the host: NativeLab creates an **empty temporary directory** with that name and
keeps it for the duration of the session. Inside the sandbox it is covered by a
mode `000`, read-only tmpfs, so it cannot be read or receive persistent content.

Ownership of these placeholders is recorded under
`$XDG_RUNTIME_DIR/native-lab/synthetic-mounts/`. `stop` removes a placeholder
only if it is still empty, has the expected identity, and has no other active
holder. Multiple sessions are reference-counted. After `SIGKILL`, the next
policy resolution discards markers for dead PIDs and recovers the state. If the
path changed type or gained content, NativeLab fails closed and does not delete
it.

Operational consequence: while a session is alive, host tools will see these
empty directories in the workspace. This is an accepted and documented PoC
limitation, not an invisible implementation detail.

### Trusted Codex projects

By default, NativeLab reads the configured global config and recognizes this
format:

```toml
[projects."/mnt/projects/Projects/project-a"]
trust_level = "trusted"

[projects."/mnt/projects/Projects/project-b"]
trust_level = "untrusted"
```

Only tables under `projects` whose `trust_level` is exactly `"trusted"` are
imported. Paths are expanded, canonicalized, deduplicated, and must exist. The
current workspace retains RW precedence. `/`, HOME, and any ancestor that
would expose all of HOME are ignored with a warning.

Each imported project is RO. The following paths are denied:

```text
.git
.agents
.codex
**/.env
**/.env.*
**/*.pem
**/*.key
**/*.p12
**/*.pfx
```

Matches are expanded on the host before `bwrap` with `rg --files --hidden
--no-ignore`. A match that is a symlink or another unexpected object aborts the
session. There is no silent fallback that broadens access.

## Trusted configuration

The only NativeLab config it looks for is:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/native-lab/config.toml
```

Workspace configuration is not read, there is no `--config`, and
`NATIVE_LAB_CONFIG` does not exist. When present, the file must be regular,
owned by the user, not be a symlink, and not be writable by group/others.

Equivalent defaults:

```toml
version = 1

[codex]
import_trusted_projects = true
config_path = "~/.codex/config.toml"

[filesystem]
extra_read_only = []
extra_trusted_project_deny_globs = []
```

`extra_read_only` adds paths to the defaults; it does not replace them. There
is no `extra_read_write`: the workspace remains the only persistent RW root.
`extra_trusted_project_deny_globs` adds globs relative to each trusted project.
`~` and `~/...` are expanded without `eval` using the real HOME.

Example:

```toml
version = 1

[codex]
import_trusted_projects = true
config_path = "~/.codex/config.toml"

[filesystem]
extra_read_only = ["~/sdk-reference"]
extra_trusted_project_deny_globs = ["**/*.secret", "**/credentials.json"]
```

Both this file and the Codex config are parsed by `python3` with `tomllib`. The
helper produces canonical JSON and NUL-delimited arguments; it never generates
shell code for `eval`.

## Policy digest and session identity

`POLICY_VERSION=2`. Before choosing a session, the launcher resolves and
sorts:

- RW and RO roots;
- trusted projects;
- concrete masks and DENY rules;
- protected metadata;
- relevant config options.

The canonical JSON is hashed with SHA-256 to form `POLICY_DIGEST`. The session
ID uses:

```text
workspace + NUL + profile + NUL + policy_version + NUL + policy_digest
```

Therefore adding/removing Codex trust or changing `extra_read_only` produces a
different ID and does not reuse a sandbox created under previous authorization.
The resolved manifest and summary remain in the runtime directory; `status`
shows the digest, number of RO roots, and trusted projects.

An already-running old session does not retroactively lose mounts when config
changes. Stop the session before revoking access if it contains long-lived
processes; changing config prevents reuse by subsequent calls, but does not
revoke an existing process.

## Environment and host-side TCB

`bwrap` uses `--clearenv` and sets only a basic environment. `SSH_AUTH_SOCK`,
D-Bus, display, Xauthority, gpg-agent, cloud tokens, and credentials are not
forwarded automatically. The remote command receives the caller's original
PATH, a private HOME, private TMPDIR, and private XDG_RUNTIME_DIR.

The caller's PATH is not used to build the sandbox. `bwrap`, `ssh`, `socat`,
`python3`, `rg`, `nohup`, and `flock` are resolved through a fixed host-side
PATH, canonicalized, and rejected if they are inside the workspace. SSH ignores
the client's personal config with `-F /dev/null`.

Preserving the remote PATH allows `~/.npm-global/bin/npm` and `npx` to run, but
does not guarantee that every component on that PATH has been mounted. An
`npm install -g` into the host prefix failing read-only is expected behavior.
Without Internet access, `npx -y` only works when the required package is
already available.

## Namespaces, networking, and capabilities

The holder requires:

```text
--unshare-user --unshare-pid --unshare-ipc
--unshare-net  --unshare-uts --new-session
--cap-drop ALL --die-with-parent
```

Loopback works between processes in the same session. The host's localhost is
not visible, the session is not visible through the host's localhost, and
there is no route to the Internet.

Capabilities are currently always dropped. GDB may work in some cases without
`CAP_SYS_PTRACE`, depending on UID, parent/child relationship, Yama, and
seccomp. Programs that truly require ptrace or another capability will need an
explicit future profile and a new policy fingerprint; this PoC does not yet
offer that customization.

## Requirements and installation

- Linux with user namespaces and network namespaces enabled;
- Bash;
- Python 3.11+ (`tomllib` in the standard library);
- bubblewrap, socat, and ripgrep;
- OpenSSH client and server;
- `flock`, `realpath`, `sha256sum`, `stat`, `awk`, and standard Unix tools;
- a valid `$XDG_RUNTIME_DIR`.

Keep these files together and executable:

```text
native-lab
native-labd
native-lab-session
native-lab-policy.py
```

`XDG_RUNTIME_DIR` must be absolute, exist, belong to the user, have mode `0700`,
and be writable/searchable. Absence or different permissions cause an explicit
error.

## Usage

In the exact directory that should be RW:

```bash
native-lab run COMMAND [ARGS...]
native-lab status
native-lab stop
```

Examples:

```bash
native-lab run npm run dev
native-lab run curl http://127.0.0.1:5173
native-lab run -- sh -c 'exit 37'
echo "$?"
# 37
```

The default mode uses `ssh -T`: there is no PTY, stdin passes through, stdout
and stderr remain separate, and the exit status is preserved. Arguments use
careful POSIX quoting. A future `native-lab-exec` could replace this layer with
serialized argv and `execve(2)`.

TTYs, full-screen applications, and password prompts are outside this stage.

### The boundary is `native-lab`, not the shell line

The `native-lab` launcher is part of the TCB and must be started on the host.
After validating/creating the session, it sends the SSH process only the
arguments received after `native-lab run`. This does not automatically make
the rest of a compound shell line run inside the sandbox.

> [!WARNING]
> Unprotected metacharacters are interpreted by the host shell before
> NativeLab receives the arguments. Therefore, this command is dangerous:
>
> ```bash
> native-lab run npm test && npm run build
> ```
>
> Only `npm test` goes through NativeLab; if it succeeds, `npm run build` is
> started directly by the host shell.

To run the entire compound operation inside the session, pass the shell
program and its script as arguments to `native-lab run`:

```bash
native-lab run -- sh -c 'npm test && npm run build'
```

The same caution applies to `||`, `;`, pipes, redirections, command
substitutions, and globs. In the examples below, `consumer`, the redirection,
`probe`, and the `*.js` expansion belong to the outer shell, not NativeLab:

```bash
native-lab run producer | consumer
native-lab run command > /tmp/result.log
native-lab run echo "$(probe)"
native-lab run tool *.js
```

Prefer direct argv for a single process. When shell syntax is needed, place the
entire expression in a protected string passed to `sh -c` or `bash -c`. Host
approval should cover the trusted NativeLab launcher, with no adjacent commands
outside it.

### Configuring Playwright MCP in Codex

The Playwright MCP server can be started directly inside the workspace session.
Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.native_lab_playwright]
command = "native-lab"
args = [
    "run",
    "playwright-mcp",
    "--isolated",
    "--output-dir", ".playwright-mcp",
    "--viewport-size", "1920x1080",
    "--allowed-hosts", "127.0.0.1,localhost",
]
startup_timeout_sec = 20.0
tool_timeout_sec = 60.0
env_vars = ["XDG_RUNTIME_DIR"]
default_tools_approval_mode = "prompt"
```

`native-lab run` makes the MCP server enter the session associated with Codex's
working directory. `XDG_RUNTIME_DIR` must be forwarded so the client can find
the NativeLab runtime and Unix socket. The MCP Chromium instance and a server
started separately with `native-lab run npm run dev`, for example, then share
the same private localhost.

This configuration assumes that `native-lab` and `playwright-mcp` are on the
PATH received by the process and that their required files are visible inside
the sandbox.

If a Google Chrome installation at `/opt/google/chrome` is needed, expose it
read-only in NativeLab's trusted config,
`$HOME/.config/native-lab/config.toml`:

```toml
[filesystem]
extra_read_only = [
    "/opt/google/chrome",
]
```

Changing this file changes the policy digest and creates a session with the
new policy; an old session that is already running does not receive the mount
retroactively.

### Generic MCP server

The Python adapter exposes asynchronous process management without invoking a
host shell:

| Tool | Operation |
| --- | --- |
| `run` | starts structured `argv` and immediately returns a `process_id` |
| `head` / `tail` | queries the bounded stdout/stderr ring buffer |
| `expect` | waits for future or retained literal output without polling |
| `write` | writes UTF-8 to stdin and can send EOF |
| `kill` | signals the SSH client group (`TERM`, or `KILL` with `force`) |
| `processes` | lists handles owned by this MCP server instance |

Create the environment and install the repository lock file:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

A Codex configuration using absolute paths looks like this:

```toml
[mcp_servers.native_lab]
command = "/path/to/native-lab/.venv/bin/python"
args = ["/path/to/native-lab/native-lab-mcp"]
env_vars = ["XDG_RUNTIME_DIR"]
startup_timeout_sec = 20.0
tool_timeout_sec = 3600.0
```

The server must start with the agent workspace as its current directory. It
uses that directory to select the NativeLab session; `NATIVE_LAB_BIN` may point
to another trusted launcher. Each process runs as:

```text
native-lab run -- argv[0] argv[1] ...
```

`expect` performs literal matching, including across read boundaries.
`from_position="now"` is the default and observes only new events;
`from_position="start"` searches retained history. `after_cursor` resumes
precisely after an opaque cursor returned by the tools and takes precedence
over `from_position`. The default buffer is 1 MiB of characters per process;
the trusted environment can change it with `NATIVE_LAB_MCP_BUFFER_CHARS`.
Each buffer also retains at most 4096 events. An instance retains up to 64
handles by default; at the limit, the oldest completed handle is discarded,
but concurrent processes are never removed. The trusted environment can change
the quota with `NATIVE_LAB_MCP_MAX_PROCESSES`.

`mcp-types` 2.2.0 still contains types-only models for the old 2025 Tasks, but
the official SDK does not implement the wire-incompatible 2026 Tasks
extension. This first adapter therefore keeps `expect` as a blocking,
cancellable call with a timeout. The registry has no MCP dependency, so a
future Tasks extension can wrap the same wait without changing processes,
buffers, or cursors.

Processes belong to the MCP instance lifetime. On shutdown, the adapter sends
`SIGTERM` to active groups and uses `SIGKILL` after a grace period.

### Instructing the agent without the generic MCP server

When the adapter above is not configured, instruct the agent to prefix
development and test processes with `native-lab run`. For example, add this to
`AGENTS.md` or the prompt:

```text
Run project code, servers, and tests inside NativeLab. For one process, pass
argv directly: `native-lab run <program> <arguments>`. For compound
expressions, put the entire expression inside the session, for example:
`native-lab run -- sh -c '<command 1> && <command 2>'`. Never leave `&&`,
`||`, `;`, pipes, redirections, command substitutions, or globs to the outer
shell. Processes started in the same workspace share localhost. If something
does not work in the sandbox, do not run it directly on the host as a fallback;
report the limitation.
```

## Runtime and lifecycle

```text
$XDG_RUNTIME_DIR/native-lab/
├── policy-mount.lock
├── synthetic-mounts/
└── <session-id>/
    ├── start.lock
    ├── holder.pid
    ├── session.info
    ├── resolved-policy.json
    ├── deny-mask
    ├── daemon.log
    └── control/
        ├── lab-ssh.sock
        ├── client_ed25519
        ├── ssh_host_ed25519_key
        ├── authorized_keys
        ├── known_hosts
        ├── sshd_config
        └── ...
```

Concurrent startup is serialized with `flock`. A PID or socket alone does not
indicate readiness: the client runs an SSH `true` probe. The holder supervises
bubblewrap, and `--die-with-parent` terminates the sandbox after an abrupt
death.

Before signaling a holder, `stop` validates its PID, `/proc` start time, argv,
workspace, session directory, and policy digest. It then sends `SIGTERM`, waits,
and uses `SIGKILL` only if necessary. Stale state is recovered on the next run.

The control plane remains intentionally simple. The client and sandbox can
still see ephemeral keys in the shared directory; separating host-private,
shared, and sandbox-private material is future hardening work.

## Tests

Tests must run on a host that permits user/network namespaces; an outer sandbox
may return `EPERM` before NativeLab starts.

```bash
./native-lab-smoke.sh
./native-lab-policy-smoke.sh
```

The first covers lifecycle, concurrency, namespaces, localhost, lack of
Internet access, streaming, stdio, exit status, quoting, and stale-state
recovery. The second creates Codex/NativeLab configs and temporary projects to
cover RW/RO, secrets, metadata, private HOME/runtime/shm, RO npm, trusted and
untrusted projects, digest, and TCB.

The policy smoke test creates two uniquely named probe files in
`~/.npm-global` and `~/.npm` to verify the default mounts and removes them
during cleanup. If the directories do not exist, it creates them and later
attempts to remove them only if they remain empty.

The GitHub Actions workflow runs both on `ubuntu-24.04`, installs dependencies,
enables user namespaces in the ephemeral VM, validates Bash/Python/ShellCheck,
executable modes, and the absence of versioned runtime/private keys. Relaxing
AppArmor on the disposable runner is not a recommendation for a real machine.

## What belongs in Git

The four executables/helper, smoke tests, documentation, `.gitignore`, and
`.github/workflows/ci.yml` should be versioned. Sockets, logs, SSH keys,
`resolved-policy.json`, runtimes, temporary fixtures, and `__pycache__` should
not. `.gitignore` covers known local artifacts, and CI rejects old runtime data
and tracked private keys.

## Troubleshooting

```bash
native-lab status
```

Startup failures display the first lines of `daemon.log`. Common errors:

- `XDG_RUNTIME_DIR is not set`: the user runtime is unavailable;
- `missing dependency`: a host-side tool is missing;
- `Operation not permitted`: the kernel, an LSM, or an outer sandbox blocked
  namespaces;
- `failed to resolve filesystem policy`: invalid config, unsafe path, scan
  error, or a mask that cannot be built fail-closed;
- `Read-only file system`/`Permission denied`: a write outside allowed roots;
- socket path too long: use a runtime with a shorter pathname.

The probes and commands behind the design decisions are documented in
[DISCOVERIES.md](DISCOVERIES.md).

## Out of scope

There is no custom daemon protocol, tmux, C/C++, cgroups, Internet proxy, MCP
proxy, integrated Xvfb, host display access, Docker, pidfd, custom seccomp, or
capability profiles. Before any use as a security tool, NativeLab needs a
formal threat model, specialist review, adversarial tests, and additional
hardening.

## Roadmap

- integrate `expect` with MCP Tasks when the official SDK provides the
  corresponding server-side lifecycle;
- keep any expansion of filesystem, networking, or capabilities under the
  host's trusted policy control.
