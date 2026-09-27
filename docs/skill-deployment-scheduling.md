# Pending deployment acceptance and polling

New configuration acceptance classifies an unbound account as stored and an active bound Native
account as pending only when the explicit server policy, tool/backend allowlist and complete saved
deployment protocol report agree. A known compatible offline Node may remain pending. Missing,
malformed or incompatible reports remain unsupported. Acceptance never creates a task or marks
content ready; historical operations keep their original classification.

After acceptance, missing fresh heartbeat on a still compatible Node keeps the original target
pending. Actual withdrawal of compatible protocol becomes unsupported before a task is bound;
account/owner/binding invalidation and materialization failures have explicit terminal error codes.
Quota failure is retryable through the existing original-operation retry protocol.

Authenticated Node polling schedules a bounded batch of the original Node's latest unbound pending
or needs-resolution attempts before acquiring poll task locks. Each candidate is reloaded under its
owner content lock and committed independently. Original selection, discovery, account binding,
active owner and fresh capability are rechecked by the existing reservation service. First use
reserves the original takeover; later polls prepare the resolved complete input. Migration conflicts
remain needs_resolution until a later poll observes their resolution. Existing task bindings are
excluded: leases, redelivery, success and permanent drain retain their dedicated protocols.

The attempt's existing updated_at orders candidates and advances after each bounded examination,
so waiting takeover or conflict candidates do not indefinitely hide later accounts. Superseded
attempts without a task may become terminal under the same lock; this is not evidence of Helper
drain. Attempts with any deployment task binding may terminate only through the dedicated protocol.
Expected pre-dispatch failures change only the original attempt projection and retention clocks;
unknown failures roll back and surface rather than silently consuming work.

This integration does not advertise runtime support or prove systemd/Claude or Docker acceptance.

## Cross-repository first-use acceptance

`AGENT_REMOTE_RUN_SKILL_FIRST_USE_TEST=1 uv run pytest -q tests/test_skill_first_use_live.py`
runs the opt-in real HTTP → ordinary polling → systemd writer → Worker/Helper → takeover/discovery
→ deployment proof with normal and lost post-commit takeover confirmation. The Node checkout is
expected at `../agent-remote-node`, or set `AGENT_REMOTE_TEST_NODE_REPO` to its absolute path.
Docker and Go are required; the harness owns a privileged systemd container with private cgroups.
No task, lease, takeover, directory, fence or capture is fixture-seeded. The legacy account, package
and compatible Node report are test inputs. Credentials and runtime resources are disposable.

This verifies capture/deployment, not Claude launch, full daemon heartbeat, Docker/sbx support or
capability activation. Both Go Worker and Helper execute in a root test process. The Node
`docs/skill-first-use-acceptance.md` describes the reproducible command and exact evidence boundary.
