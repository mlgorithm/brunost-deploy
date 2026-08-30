# Security policy

Please do not report suspected vulnerabilities in public issues. Contact the
maintainers privately with the affected component, deployment backend, impact,
and a reproducible description. Do not include credentials, private task
bundles, or participant data.

The deployment layer is designed to fail closed in production: images are
digest-pinned, worker credentials are permission-locked, callbacks are signed,
and worker sandboxes use an allowlisted runtime plus a restricted Docker
socket proxy. Operators must still supply the seccomp profile, sandbox images,
TLS, and shared durable storage.

Supported versions are the current `main` release and the latest tagged
release. Security fixes are backported only when the affected release remains
in active operational use.
