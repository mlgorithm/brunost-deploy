# Brunost Judge disaster recovery runbook

This runbook restores Judge data only. Premium data and the Premium callback
consumer remain owned and backed up by the Premium operator.

## Verify backup integrity

Run the non-destructive check on the data node:

```bash
/usr/local/sbin/brunost-judge-dr-check /srv/brunost/backups/judge/latest
```

It verifies every file checksum and that PostgreSQL can inspect the custom
format dump. A successful check is not a recovery test until it has been
restored into an isolated target.

## Isolated recovery drill

1. Provision an isolated PostgreSQL and S3-compatible object store at the
   intended recovery versions.
2. Copy one timestamped backup directory to the isolated recovery host.
3. Run `BRUNOST_RESTORE_CONFIRM=YES restore-judge.sh <backup-dir>` with an
   environment file that points only at the isolated targets.
4. Start the Judge control plane and callback dispatcher against those
   targets. Do not point Premium at the recovery instance yet.
5. Run `brunostctl verify` against the recovery Judge URL and check readiness,
   callback signing configuration, and every worker's `node doctor` result.
6. Perform one immutable artifact, one submission, and one signed callback
   smoke test using a disposable Premium tenant or test environment.
7. Record restore duration, data-loss window, failed checks, and operator
   actions. Keep the latest successful drill with the backup evidence.

## Production recovery

Production restore is destructive. Obtain the incident commander’s approval,
stop new contest traffic, drain workers, and confirm the backup timestamp.
Set `BRUNOST_RESTORE_CONFIRM=YES` only in the one-shot restore process. Restore
the database first, then objects, then start Judge and wait for `/readyz`.
Re-enroll or rotate workers if their credentials were compromised. Re-enable
Premium traffic only after the signed callback smoke test succeeds.

The deployment does not provide automatic PostgreSQL promotion, object-store
failover, DNS/load-balancer failover, or a cross-region copy. Those remain
environment-dependent recovery steps.
