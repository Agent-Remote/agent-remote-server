# Exact deployment preparation results

Deployment success uses dedicated authenticated Node routes, never generic task completion.
`POST /node/skill-deployments/{attempt_id}/result?task_id=UUID` submits a strict current poll attempt
and the original typed Helper preparation receipt. `POST` on `/result/inspect` observes that same
proposal without renewing authority or mutating task, attempt, content or release clocks.

The schema-1 Helper receipt fixes complete original identity, full input digest, directory epoch,
original plan generation and Helper receipt UUID. Server reconstructs the original manifest/plan/
members and verifies the same canonical input hash. The authenticated Node worker delegates this
Helper evidence; it is not a cryptographic Helper signature or evidence of tool use.

First acceptance requires the original active owner, exact canonical task binding, current unexpired
poll, unsuperseded configuration, matching state epochs and fresh deployment capability. Under the
user storage lock and exact task lock, the existing retention savepoint commits NodeTask success,
one `node_task_results` receipt, target-attempt ready and the operation projection together. No account
head, session snapshot or effective-use record changes. A lost-response exact replay returns only
the original saved result, even after lease expiry or input retirement. A changed proposal conflicts.

Inspection requires the original authenticated Node, immutable task/attempt binding and active owner.
It can recover an already accepted receipt after the feature gate closes. It exposes the submitted
proposal, accepted boolean, current poll attempt and task status, never file bytes or a new execution
grant. Missing/corrupt/duplicate terminal results cannot masquerade as absence. Observing absence
under a newer poll prevents an older poll from committing after that observation because both use
the same user/task lock order. Historical acceptance is not a claim about current account selection.

Generic task success/failure remains rejected for every deployment binding. Dedicated two-phase
revocation and drain confirmation now follow `docs/skill-deployment-termination.md`. Worker terminal
orchestration and bounded ordinary polling are connected; capability activation remains pending.
Lease expiry and
configuration supersession alone do not prove that Helper work stopped.
Retention explicitly protects inputs of `expired` tasks; unknown task states still fail closed.
The Node's separate permanent local drain receipt becomes terminal evidence only after exact Server
confirmation under its original saved revocation intent.
