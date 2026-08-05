# Device Control Release Evidence

Production device control remains disabled unless server startup verifies a release-evidence
manifest. The manifest is an approval record for exact external evidence; it does not perform or
replace notarization, outbound-policy activation, isolation observation, compatibility testing, or
independent security review.

## Manifest

Schema version 1 contains exactly these fields:

- `schema_version`: integer `1`.
- `release_version`: exact `agent-remote-server` version.
- `issued_at` and `expires_at`: timezone-aware timestamps with a lifetime of at most 30 days.
- `server_sha256`, `node_sha256`, `application_sha256`, and `proxy_sha256`: exact release artifact
  digests.
- `sbom_sha256` and `provenance_sha256`: SBOM and build-provenance evidence digests.
- `security_tests_sha256`, `security_review_sha256`, `signing_notarization_sha256`,
  `outbound_policy_sha256`, `local_claude_isolation_sha256`, `stop_revocation_sha256`, and
  `compatibility_sha256`: release-gate evidence digests.
- `computer_use_v2_evidence_sha256`: optional Apple-profile digest of the validated, artifact-bound
  Computer Use v2 acceptance record; required whenever production v2 rollout is non-zero.
- `ci_run_url`: HTTPS URL for the release run that assembled the evidence.
- `signature`: Base64-encoded Ed25519 signature.

Every present digest is 64 lowercase hexadecimal characters. Unknown fields are rejected, as are
missing fields required by the selected profile; the optional v2 digest may be `null` only while
rollout is zero.

Schema version 2 is restricted to the reduced `community-local-trust` production profile. It must
declare `production_ready=true`, `apple_notarized=false`, `public_distribution=false`, and
`manual_trust_required=true`. It also pins `community_signing_sha256`,
`automated_release_checks_sha256`, and `risk_acceptance_sha256`. Conflicting trust claims are
rejected, so a self-signed build cannot be represented as Apple notarized. Schema version 1 remains
restricted to `apple-developer-id` and its strict external gates.

Schema version 3 keeps the same `community-local-trust` claims and replaces the single
`node_sha256` and `proxy_sha256` values with `node_artifacts_sha256` and
`proxy_artifacts_sha256` mappings. Both mappings must cover `linux-amd64-glibc`,
`linux-arm64-glibc`, `linux-amd64-musl`, and `linux-arm64-musl`, allowing one signed manifest to
authorize a mixed-architecture Node fleet.

## Computer Use v2 rollout boundary

The Apple-profile release assembler validates the artifact-specific Computer Use v2 report and
binds its record digest as `computer_use_v2_evidence_sha256`. The Community schemas require this
field to be `null`. A valid general device-control manifest without the digest does not authorize a
non-zero `DEVICE_CONTROL_V2_ROLLOUT_PERCENT` in production.

That report must cover signed Safari, Chrome, Firefox, native application, and AX-incomplete
Electron runs; zero-content telemetry review; golden-prompt and current MCP/runtime compatibility;
zero wrong-target actions; no success-rate regression; the published image, latency, settle, and
coordinate-fallback thresholds; and a rehearsed new-generation rollback. A Community risk
acceptance digest cannot substitute for this report. The Apple assembler requires the structured
record's `report_sha256` to match a real file inside the bounded raw security-tests archive before
the record digest can become `computer_use_v2_evidence_sha256`.

Server startup rejects a non-zero rollout when the digest is absent. The same check runs on device
control progression and relay establishment, so an expired manifest or runtime configuration
change cannot bypass the startup decision.

## Signature

The signed bytes are UTF-8 JSON after removing `signature`, sorting keys, emitting ASCII escapes,
rejecting non-finite numbers, and using `,` and `:` without surrounding whitespace. Deployment
configuration pins the raw 32-byte Ed25519 public key as Base64 in
`DEVICE_CONTROL_RELEASE_PUBLIC_KEY`; the private key stays outside the server and repository.

Set `DEVICE_CONTROL_RELEASE_EVIDENCE_PATH` to the manifest path. With
`AGENT_REMOTE_ENV=production` and `DEVICE_CONTROL_ENABLED=true`, any missing configuration,
malformed manifest, version mismatch, invalid signature, future issue time, excessive lifetime, or
expiry stops application creation. Development can explicitly enable the capability without this
manifest only for synthetic, non-sensitive testing.

Create a manifest from an owner-only unsigned draft and an owner-only PKCS#8 Ed25519 private key:

```sh
uv run python scripts/create_device_control_release_evidence.py \
  --draft release-evidence-draft.json \
  --private-key /secure/release-evidence-key.pem \
  --output release-evidence.json
```

The command refuses symbolic links, group/world-readable inputs, oversized inputs, non-Ed25519
keys, signed drafts, and existing output paths. It verifies the completed manifest with the same
runtime verifier before succeeding. The private key must remain in the release environment and
must never be copied into a server image, deployment manifest, artifact, log, or repository.

The server release workflow publishes an immutable OCI image digest together with a downloadable
SPDX SBOM, GitHub build-provenance bundle, release metadata, and SHA-256 checksum file. Use the
exact deployed image digest as `server_sha256` after removing its `sha256:` prefix; use the
downloaded SBOM and provenance file digests for their corresponding evidence fields.
