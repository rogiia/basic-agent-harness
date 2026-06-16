Where Sandboxing Fits
The previous breakdown was mostly about semantic security — controlling what the model decides to do. Sandboxing is execution security — limiting the blast radius if something bad does get executed. They're complementary layers:
[User] → [Harness: semantic controls] → [Model] → [Harness: validates output]
                                                           ↓
                                              [Sandbox: execution controls]
                                                           ↓
                                                    [Tool execution]
Even with perfect prompt injection defense and tool gating, you still want sandboxing because:

The model might find a novel exploit path you didn't anticipate
A tool implementation might have its own vulnerability
Supply chain attacks on tool dependencies


Is Docker Enough?
Short answer: Docker alone is not sufficient for a hostile workload. Here's why:
What Docker actually gives you

Filesystem isolation — container has its own root, can't see host FS (unless you mount volumes carelessly)
Process isolation — processes inside can't see host PIDs
Network namespacing — you can firewall egress
Resource limits — cgroups for CPU/memory caps

What Docker does not give you

Kernel isolation — containers share the host kernel. A kernel exploit (e.g., a container escape via runc, cgroups, overlayfs) gives the attacker host root. This has happened repeatedly with real CVEs.
Syscall filtering by default — without a seccomp profile, the container can make most Linux syscalls, including dangerous ones
Protection against a privileged container — if your agent needs to run docker run --privileged or mount the Docker socket, it's essentially root on the host
GPU isolation — if you expose a GPU, the attack surface widens significantly

The real threat model
If the agent is running untrusted code (e.g., a code-execution tool where the model generates arbitrary Python/bash), Docker alone is a weak boundary. You're one kernel CVE away from host compromise.

What Strong Sandboxing Actually Looks Like
Layer 1 — Seccomp + AppArmor/SELinux

Restrict which syscalls the container can make. Docker has a default seccomp profile but it's permissive; write a custom one that whitelists only what your tool actually needs.
Layer 2 — gVisor (runsc)

Google's gVisor interposes on syscalls in user space — the container's kernel calls are intercepted by a Go process, not passed directly to the host kernel. This eliminates most kernel exploit paths. Drop-in replacement for runc in Docker/Kubernetes. Real performance cost (~20–30% slower for syscall-heavy workloads), but strong isolation.
Layer 3 — Firecracker / MicroVMs

What AWS Lambda and Fly.io use. Each workload gets a real hardware-virtualized VM with its own kernel, booting in ~125ms. No shared kernel at all — true VM-level isolation with container-like startup speed. This is the gold standard for untrusted code execution.
Layer 4 — Ephemeral environments

Spin up a fresh sandbox per tool invocation, destroy it immediately after. No persistent state means no lateral movement between calls. Firecracker makes this practical latency-wise.

Practical Tiers by Risk Level
WorkloadAppropriate sandboxAgent calls your own predefined tools (no code exec)Docker + seccomp + network egress controlAgent calls tools that fetch external dataDocker + gVisor + strict egress firewallAgent executes model-generated codeFirecracker MicroVM or dedicated service like E2BAgent executes code with GPU accessVery hard — consider hosted sandboxes only

The Docker Socket Problem
One specific footgun: if your harness runs inside Docker and has access to /var/run/docker.sock so it can spin up tool containers — that's immediate host escape. Anyone with access to the Docker socket is root on the host. Either:

Use a Docker-out-of-Docker proxy that rate-limits and validates image/config before passing through
Move to Kubernetes and use proper RBAC instead
Use a dedicated sandbox API (E2B, Modal, Dagger) that handles this for you


Managed Sandbox Services Worth Knowing
If you don't want to operate this yourself:

E2B — purpose-built for AI agent code execution, Firecracker-backed, good SDK
Modal — ephemeral containers with strong isolation, great for Python tool execution
Cloudflare Workers — V8 isolate-based, very strong isolation, but JS/WASM only


The mental model shift: Docker is a dev tool that happens to provide some isolation. Firecracker/gVisor are security tools designed from the ground up with hostile workloads in mind. For an agent that executes anything the model generates, you want the latter.
