# Sandbox Alternatives

The in-process sandbox in `tools/sandbox.py` is **application-level**: it inspects
tool arguments in Python and refuses what looks dangerous. That is convenient and
portable, but it is only as strong as the checks we remembered to write — a clever
shell command can often evade a denylist, and a path-confinement check in the same
process as the attacker offers no real boundary if the attacker can run arbitrary
code.

The alternatives below move the boundary **out of the agent process**, into the
operating system, a separate runtime, or a separate machine. They are listed
roughly from lightest to strongest isolation. None is strictly "code" in the
sense of the current implementation — most are invoked as a command, a config
file, or a one-time system setup, with the agent simply spawning its tools
inside them.

A quick legend for the tradeoff columns:

- **Strength** — how hard it is for code running inside to break out.
- **Setup** — how much one-time work is required to use it.
- **Portability** — whether it works on the host this project targets
  (the harness is developed on macOS and runs against a local Ollama).
- **Fit** — a subjective rating for *this* coding-agent harness, where the
  agent reads/writes files and runs shell commands in the user's project.

---

## 1. OS-level filesystem confinement (no containers)

### Landlock (Linux ≥ 5.13)

Landlock is an unprivileged, in-kernel filesystem access-control LSM. A process
calls `landlock_restrict_self()` with a ruleset describing which paths it may
read/write, and the kernel enforces it for that process and all its children —
even if they later `exec` something malicious. No root, no container, no daemon.

- **Strength**: Strong (kernel-enforced, unforgeable by the sandboxed process).
- **Setup**: Low — a few dozen lines of C or the `pylandlock` / `landlock` PyPI
  binding, run once at agent startup before any tool executes.
- **Portability**: Linux only. Not available on macOS.
- **Fit**: Excellent on Linux. It does exactly what the Python path checks do,
  but correctly, and it also constrains child processes spawned by `run_bash`.
  This is arguably the single best drop-in replacement for the path half of
  the current sandbox on a Linux host.

### `chroot`

The classic: `chroot(2)` changes the root directory for a process and its
children, so absolute paths like `/etc/passwd` resolve inside the new root.

- **Strength**: Weak. It is **not** a security boundary on its own — a root
  process can escape trivially, and even non-root processes can escape in
  several well-known ways (e.g. via `chroot` + `mkdir` + file descriptors).
  It also does not restrict network, `/proc`, or `mknod`.
- **Setup**: Medium — you must populate the chroot with enough of a userspace
  (`/bin/sh`, coreutils, libs) for `run_bash` to work.
- **Portability**: POSIX, available on macOS, but even weaker there.
- **Fit**: Poor as a primary sandbox; reasonable as a *convenience* layer
  combined with something stronger (e.g. chroot + seccomp + drop privs).

### macOS Seatbelt (`sandbox-exec`)

macOS ships a kernel-enforced mandatory-access-control framework ("Seatbelt")
exposed via the `sandbox-exec` command and `.sb` policy files. You can write a
profile that permits reading/writing only under a given directory, blocks
`sudo`/`mount`/raw-disk access, and denies all network except specified hosts.

- **Strength**: Strong (kernel-enforced; used by Safari, App Store, etc.).
- **Setup**: Low — write a `.sb` profile and launch the agent under
  `sandbox-exec -f profile.sb`. Apple's built-in profiles (e.g.
  `no-network`) can be referenced directly.
- **Portability**: macOS only.
- **Fit**: Very good *for this project's development host*. It is the native
  macOS equivalent of Landlock + a network filter, and it requires no code
  changes — just a profile file and a wrapper command.

---

## 2. Namespaces and unprivileged containers

### Linux namespaces (via `bubblewrap` / `bwrap` / `unshare`)

Namespaces (`mount`, `pid`, `net`, `user`, `ipc`, `uts`) give a process its own
view of the filesystem, process list, network stack, etc. **Bubblewrap**
(`bwrap`, used by Flatpak) and **nsjail** are unprivileged wrappers that make
this practical: you declare a read-only root, a writable bind-mount for the
project, and an isolated network, then run the agent inside.

- **Strength**: Strong (kernel-enforced; the process literally cannot see the
  host filesystem outside the bind-mounts).
- **Setup**: Medium — install `bwrap`, declare bind-mounts and a rootfs.
  `unshare -r --net --pid --mount` is a one-liner for a quick test.
- **Portability**: Linux only (namespaces are a Linux kernel feature).
- **Fit**: Excellent on Linux. A `bwrap` invocation can replace both the path
  confinement *and* the command blocklist with a real boundary, and you can
  combine it with cgroups (below) for resource limits.

### systemd-nspawn

A thin container manager built around namespaces + cgroups. Think of it as
"chroot done right": it gives a near-complete OS view with proper isolation,
and integrates with `systemd` resource controls.

- **Strength**: Strong.
- **Setup**: Medium — needs a container rootfs (`debootstrap`, `dnf
  --installroot`, or a tarball).
- **Portability**: Linux + systemd.
- **Fit**: Good when you already run on a systemd box and want a long-lived
  project container. Heavier than `bwrap` for a single command.

### LXC / LXD

Full system containers. Overkill for a single agent process, but useful if you
want a persistent, snapshot-able "project VM" the agent always runs in.

- **Strength**: Strong.
- **Setup**: High (container image management, networking).
- **Portability**: Linux only.
- **Fit**: Low for a CLI agent; high if you want reproducible, throwaway project
  environments.

---

## 3. Full containers

### Docker / OCI runtimes (`runc`, `crun`, `podman`)

Run the agent (or just the `run_bash` tool) inside a container whose root
filesystem is a project image, with the project bind-mounted read-write and
everything else read-only. Network can be disabled (`--network none`) or
proxied.

- **Strength**: Strong, assuming a non-root container and a hardened runtime.
  (Docker historically had a weak default boundary for root containers;
  `podman` runs rootless by default.)
- **Setup**: Medium-High — image build, volume mounts, network policy. But
  tooling is mature and well-understood.
- **Portability**: Cross-platform via Docker Desktop / Podman Machine / colima.
  On macOS the container runs in a Linux VM, which adds latency.
- **Fit**: Good as a *tool-level* sandbox: keep the agent loop on the host, but
  route every `run_bash`/`write_file` call into a short-lived container. This
  is what most hosted coding agents (SWE-agent, OpenHands) do in practice.

### `podman` (rootless)

Same UX as Docker, but daemonless and rootless by default, so a container
breakout does not immediately imply host root.

- **Fit**: Strictly better than Docker for single-user local use.

---

## 4. Kernel syscall filtering

### seccomp-bpf

A Linux kernel feature that lets a process install a BPF filter restricting
which syscalls it (and its children) may call. You can ban `ptrace`, `mount`,
`reboot`, `keyctl`, `open` of specific paths (via path-based filters with
`SECCOMP_RET_ERRNO`), etc.

- **Strength**: Strong against syscall-based attacks; weak against logic bugs
  inside *allowed* syscalls (e.g. a permitted `unlink` can still delete
  everything writable).
- **Setup**: Medium — a filter program (libs like `pyseccomp` or hand-rolled
  BPF). Best combined with a filesystem sandbox, not used alone.
- **Portability**: Linux only.
- **Fit**: Good as a *second* layer on top of Landlock/namespaces. By itself
  it doesn't confine paths well; together with Landlock it is very strong.

### AppArmor / SELinux

Mandatory access-control LSMs configured by system packages. You write a
profile that says "this binary may only read/write these paths, may not
network, may not ptrace," and the kernel enforces it.

- **Strength**: Very strong (kernel-enforced; survives `exec`).
- **Setup**: High — profile authoring is fiddly and distribution-specific.
- **Portability**: Linux only, and AppArmor vs SELinux differ by distro.
- **Fit**: Low for a portable CLI tool, high for a centrally-managed
  deployment where a sysadmin owns the profile.

---

## 5. User-space kernels / VMs

### gVisor

A user-space kernel implemented in Go (`runsc`) that intercepts the sandboxed
program's syscalls and re-implements them against a restricted host API. The
sandboxed code never touches the host kernel directly. Compatible with the OCI
interface, so it drops into Docker/Podman.

- **Strength**: Very strong — defeats most kernel-exploit-based breakouts
  because the guest never issues real syscalls to the host kernel.
- **Setup**: Medium — install `runsc`, set it as the Docker runtime.
- **Portability**: Linux only.
- **Fit**: Excellent when you are already containerising tool execution and
  want a much harder boundary than plain `runc`. Some syscall-compatibility
  gaps; fine for typical dev tooling.

### Firecracker / Cloud Hypervisor / Kata Containers (microVMs)

Full KVM-based virtual machines with a tiny footprint and millisecond boot
times. Kata integrates them into Kubernetes/container runtimes; Firecracker is
used by AWS Lambda and Fargate.

- **Strength**: Maximum practical strength — hardware-isolated; a guest kernel
  exploit does not reach the host.
- **Setup**: High — needs KVM, a VM image, a network setup. Worth it only if
  you run untrusted agents at scale.
- **Portability**: Linux with virtualisation extensions.
- **Fit**: Low for a local single-user harness; **the** right choice for a
  multi-tenant hosted agent service.

---

## 6. Runtime / language-level confinement

### WebAssembly (WASI) runtimes

Compile tools (or the whole agent) to WASM and run them in `wasmtime`/`wasmer`
with a WASI capability-based filesystem: the runtime only sees directories you
explicitly pre-open, and there is no shell unless you implement one. `wasmtime`
also supports seccomp and per-instance resource limits.

- **Strength**: Strong — capability-based, no ambient authority, no `fork`/
  `exec` by default.
- **Setup**: High for Python tools (need to compile or rewrite), low for
  self-contained tools shipped as WASM.
- **Portability**: Cross-platform.
- **Fit**: Poor for the *existing* Python toolset (porting `run_bash` defeats
  the point), but attractive for a *new* tool layer written in Rust/Go that
  exposes safe primitives to the agent.

### RestrictedPython / sandboxed interpreters

Run agent-generated Python in a restricted interpreter that strips `open`,
`__import__`, `exec`, etc.

- **Strength**: Weak to moderate — sandboxed-Python escapes are a perennial
  CTF genre; RestrictedPython explicitly disclaims being a security sandbox.
- **Fit**: Relevant only if the agent emits Python rather than shell; not our
  case.

---

## 7. Resource limits (orthogonal but worth pairing)

These do not confine *what* a program can do, only *how much*. Pair them with
any of the above.

### cgroups v2 (Linux)

Limit CPU, memory, IO, and PID count for the agent process subtree. Prevents
fork bombs and runaway builds from taking down the host even when the command
blocklist is bypassed.

- **Fit**: Essential companion to any namespace/VM approach on Linux.

### `setrlimit` / `ulimit` (POSIX)

Per-process limits on file size, number of fds, CPU seconds, processes.
Available on macOS as well.

- **Fit**: Cheap baseline everywhere; weaker than cgroups but zero setup.

---

## 8. Network-level isolation

If the agent should not phone home, deny it network access entirely at the
boundary instead of trying to detect exfiltration in code.

- **Linux network namespace + `iptables`/`nftables` egress allowlist**: the
  sandboxed process gets its own netns with a veth pair and a proxy that
  allows only specific hosts (e.g. `localhost:11434` for Ollama).
- **macOS**: Seatbelt's `(deny network*)` or a `pfctl` rule on a dedicated
  interface.
- **`bubblewrap --unshare-net`**: the process gets a loopback-only netns —
  it can still reach the host via an explicit bind-mount/proxy.

For this harness, network egress should be limited to the Ollama endpoint
(`localhost:11434`) and any URL the user has allowlisted for `webfetch`.

---

## 9. Managed sandboxing services

If you do not want to run any of the above yourself, several services expose a
"sandboxed execution" API over the network:

- **E2B** — open-source microVM-based code sandboxes with an SDK; designed for
  exactly this use case (agent tool execution). You ship code, they return
  stdout/stderr/exit code; files live in the VM.
- **Modal / Fly Machines / Replicate** — ephemeral VMs/containers with an HTTP
  API; spin one up per session, tear it down when done.
- **Daytona / envd / Devcontainer** — dev-environment-as-code; less of a
  *security* boundary, more of a *reproducible workspace*, but still confines
  file writes to the workspace.

- **Strength**: Strong (the provider handles isolation; you get a remote
  boundary you cannot accidentally weaken).
- **Setup**: Low to medium (an SDK call), but adds a network dependency and
  latency to every tool call.
- **Portability**: Anywhere with network access.
- **Fit**: Great for a hosted version of this harness; awkward for a purely
  local one because every `read_file` becomes a round-trip.

---

## Comparison at a glance

| Approach                     | Layer        | Strength | Setup | macOS | Best for this harness? |
|------------------------------|--------------|----------|-------|-------|------------------------|
| Current in-process checks    | app          | weak     | low   | yes   | baseline / convenience  |
| Landlock                     | kernel       | strong   | low   | no    | ★ on Linux             |
| macOS Seatbelt (`sandbox-exec`) | kernel    | strong   | low   | yes   | ★ on macOS dev host    |
| `bubblewrap` / namespaces    | kernel       | strong   | med   | no    | ★★ on Linux            |
| seccomp-bpf                  | kernel       | strong*  | med   | no    | companion layer        |
| AppArmor / SELinux           | kernel       | very strong | high | no   | server deployments     |
| chroot                       | kernel       | weak     | med   | yes   | only with another layer|
| Docker / Podman              | container    | strong   | med   | VM    | tool-level sandbox     |
| gVisor (`runsc`)             | user-kernel  | very strong | med | no    | hardened container run |
| Firecracker / Kata (microVM) | VM           | max      | high  | no    | multi-tenant hosting   |
| WASI / Wasmtime              | runtime      | strong   | high  | yes   | new tool layer only    |
| cgroups v2 / rlimits         | kernel       | (resource) | low | no/yes | companion everywhere   |
| Network namespace / Seatbelt net | network | strong   | med   | yes   | companion everywhere   |
| E2B / Modal / Fly            | managed VM   | strong   | low   | n/a   | hosted version         |

\* seccomp is strong for syscalls but does not confine file paths on its own.

---

## Recommendation for this harness

Keep the in-process checks — they are cheap, portable, and catch the obvious
mistakes before they ever reach the OS. Then layer one of the following on top,
chosen by host:

1. **On the macOS dev machine**: launch the agent under `sandbox-exec` with a
   `.sb` profile that (a) restricts file writes to the project root, (b) denies
   network except `localhost:11434`, and (c) blocks `mount`, `sudo`, raw disk,
   and kernel-extension syscalls. Zero code changes; the Python sandbox becomes
   a second line of defence rather than the only one.

2. **On a Linux host**: `bwrap --ro-bind / / --bind $PROJECT $PROJECT
   --dev /dev --proc /proc --unshare-net` (with an explicit Ollama proxy) plus
   a Landlock ruleset applied from Python before the tool loop starts, plus a
   cgroup v2 slice for CPU/memory/PID limits. This gives kernel-enforced path,
   network, and resource confinement for both the agent and any `run_bash`
   children.

3. **If this ever becomes a hosted service**: run each session inside a
   Firecracker microVM (or gVisor-isolated container) with the project mounted
   read-write and network restricted to a allowlisted proxy. The in-process
   and namespace layers stay as defence-in-depth inside the VM.

In every case, the **audit log** (`tools/audit.py`) stays as-is: it is the
mechanism that lets you reconstruct *what happened inside the sandbox*, which
matters exactly as much as the boundary itself.
