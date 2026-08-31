# Changelog

## 0.3.0

- Bundle and materialize the reviewed versioned Docker seccomp profile with
  every generated country operator bundle.
- Derive worker enrollment grants from the topology, including the runtime
  capabilities reported by remote workers, and preserve grants at node join.
- Require explicit K3s worker node selectors and render them into worker pods.
- Record non-secret sandbox, socket-proxy, runtime, and seccomp pins after a
  successful deployment so Compose rollback uses the previous release's
  runtime rather than subsequently edited environment values.
- Reject authenticated HTTP redirects and bound deployment API response reads.

## 0.2.0

- Introduced the Compose/K3s country deployment, enrollment, backup, and
  lifecycle tooling.
