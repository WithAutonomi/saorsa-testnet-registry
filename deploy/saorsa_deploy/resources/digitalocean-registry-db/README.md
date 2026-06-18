# digitalocean-registry-db

Standalone Terraform module for the **shared** DigitalOcean Managed Postgres
cluster that backs `saorsa-testnet-registry` (deployment history shared across
people).

This is **completely independent** of the App-Platform-attached database
`dev-db-722203` defined in `app.yaml` — this module never references it. The two
clusters are decoupled; the App-Platform DB can be decommissioned separately
later with no impact here.

Like `aws-client-s3`, it is a one-off application-infra module: a fixed S3 state
key (`saorsa-deploy/digitalocean-registry-db.tfstate`), applied manually.

## What it creates

- `digitalocean_database_cluster` — PG **18** (matches the local source server),
  `db-s-1vcpu-1gb`, single node, region `lon1`.
- `digitalocean_database_db` — `saorsa_testnet_registry`.
- `digitalocean_database_user` — `saorsa_testnet` (the app user handed to local
  tools + collaborators; **not** the cluster's `doadmin` superuser).

Access is **open** (no trusted-sources firewall): reachable from any IP, gated by
the generated password over TLS. The emitted `DATABASE_URL` pins
`?sslmode=require` (mandatory for DO managed PG). To lock it down later, add a
`digitalocean_database_firewall` resource.

## Provision

```bash
cd deploy/saorsa_deploy/resources/digitalocean-registry-db
terraform init
terraform apply -var "do_token=$DO_TOKEN"   # needs AWS creds for the S3 backend too
```

## Get the connection string

```bash
terraform -chdir=deploy/saorsa_deploy/resources/digitalocean-registry-db output -raw database_url
```

Paste it into your local `.env` as `DATABASE_URL`. **Never commit the real value.**

## Sharing with collaborators

Send each collaborator the `database_url` output **out-of-band** (password
manager / 1Password / direct message — not in git, not in Slack history you'd
regret). They paste it into their own `.env`. Everyone shares one app user
(`saorsa_testnet`); rotate the password by tainting the user
(`terraform apply -replace=digitalocean_database_user.registry`) and re-sharing
if it leaks.

## One-time post-restore step (already done for the initial migration)

DO managed PG (PG15+) does not grant `CREATE` on `public` to non-owner users, so
the app user needs schema ownership before a restore/migration can create
objects. Run once as `doadmin`:

```sql
ALTER SCHEMA public OWNER TO saorsa_testnet;
GRANT  ALL ON SCHEMA public TO saorsa_testnet;
```
