# Deployment manifests

Declarative YAML descriptions of deployments. One manifest = one deployment's full
desired state. The orchestrator (`saorsa-deploy deployments up`/`down`/`plan`) turns a manifest
into the correct, strictly-ordered sequence of `saorsa-deploy` commands — replacing
the hand-edited bash runbooks in `../../runbooks/`.

## Layout: bundled profiles vs. local runs

```
deploy/saorsa_deploy/resources/profiles/   # SHIPPED IN THE WHEEL — reusable base
  reference.yaml                           #   profiles + the reference template
  staging.yaml

deploy/manifests/
  examples/        # COMMITTED — standalone copy-me templates (mainly the prod kinds)
    prod-uploaders.yaml
    prod-local-uploads.yaml
  runs/            # GIT-IGNORED — your concrete deployment manifests live here
```

- **The profiles** are package data under `deploy/saorsa_deploy/resources/profiles/`, so
  they are **versioned with the tool**: installing `saorsa-deploy==1.3.0` gets the
  `staging` profile exactly as it was at 1.3.0. These are the reusable *shapes* — they
  change rarely. A profile has no per-run identity; a run reaches one with
  `extends: profile:<name>` from **any** directory, including one outside this checkout
  (which is where an unattended runner writes its manifest). List what the installed
  tool ships with `saorsa-deploy deployments config profiles`.
- **`examples/`** is in version control, but serves a different purpose: each file is a
  complete, standalone manifest meant to be **copied** into `runs/` and edited — not a
  deep-merge base for `extends:`. The prod kinds are single-command deployments usually
  written standalone, so they live here rather than as profiles.
- **`runs/`** is git-ignored (same rationale as `runbooks/`). A concrete manifest like
  `runs/STG-01.yaml` pins specific versions/branches and churns every deployment, so it
  stays local and is never committed. Create your run manifests here.

```bash
# What the installed tool ships, and the profiles themselves:
saorsa-deploy deployments config profiles
saorsa-deploy deployments config validate --config deploy/saorsa_deploy/resources/profiles/reference.yaml
saorsa-deploy deployments plan            --config deploy/saorsa_deploy/resources/profiles/reference.yaml

# Your own runs (local, git-ignored):
saorsa-deploy deployments plan --config deploy/manifests/runs/STG-01.yaml   # preview the sequence
saorsa-deploy deployments up   --config deploy/manifests/runs/STG-01.yaml   # deploy
saorsa-deploy deployments down --name STG-01                                # destroy (state-driven; --config also works)
```

## Bundled profiles and committed examples

| File | `extends:` value | Kind | Notes |
|---|---|---|---|
| `resources/profiles/reference.yaml` | `profile:reference` | testnet | Every available option, populated. Copy it into `runs/` and delete what you don't need. |
| `resources/profiles/staging.yaml` | `profile:staging` | testnet | THE staging profile every release train's run extends (no `name`). ~990 nodes at **10% NAT**, the native client tier (10 uploaders + 2 downloaders) and the WASM browser-path tier added on top of it (3 uploaders + 1 downloader). |
| `resources/profiles/small.yaml` | `profile:small` | testnet | Small base profile (~100 nodes, a couple of 10 MB uploaders). |
| `examples/prod-uploaders.yaml` | — (copy, don't extend) | prod-uploaders | Standalone template: multi-VM prod upload run; shows per-uploader `log_level`. Copy into `runs/`. |
| `examples/prod-local-uploads.yaml` | — (copy, don't extend) | prod-local-uploads | Standalone template: upload to prod from the local machine; shows `log_level`. Copy into `runs/`. |

## Writing a run manifest

A testnet run that extends the staging profile (drop this in `runs/staging/STG-01.yaml`):

```yaml
extends: profile:staging   # a profile bundled with the tool, from any directory
name: STG-01
purpose: "RC sanity check"
testnet:
  binaries:
    ant_node: { version: 0.11.5-rc.4 }
    ant:      { version: 0.2.5-rc.4 }
    # The WASM (browser-path) tier. Branch form only — no WASM package is
    # published as a GitHub release — so the train's ant-client RC TAG goes
    # here, or the shipped tag on a train with no ant-client RC.
    ant_wasm:
      branch: ant-cli-v0.2.5-rc.4
      repo_owner: WithAutonomi   # REQUIRED: the default owner is maidsafe
```

Check the WASM tier will build before spending a build VM and ~10 minutes on it:

```bash
saorsa-deploy build ant-wasm-check --branch ant-cli-v0.2.5-rc.4 --repo-owner WithAutonomi
```

The prod kinds are single-command deployments — usually written standalone (no
`extends`). Minimal examples for each (drop in `runs/`):

```yaml
# prod-uploaders — real ETH on Arbitrum One
name: PROD-UL-01
kind: prod-uploaders
purpose: "weather check: larger files"
ssh_key_path: ~/.ssh/ant_deploy
prod_uploaders:
  binary: { version: 0.2.4 }
  secret_key_env: ARBITRUM_ONE_SECRET_KEY   # env var name only; up fails fast if unset
  uploaders:
    - { region: lon1, vm_size: s-2vcpu-4gb, urls: /path/to/urls.txt, payment: merkle }
    # optional per-uploader: log_level: debug — ant verbosity (info=default, debug, trace); see examples/prod-uploaders.yaml
    # optional per-uploader: clock_skew: 2h — disable time sync + shift that VM's clock by a signed Go-style duration (e.g. 2h, -2h, 30m; testing only)
```

```yaml
# prod-downloaders — read-only, no wallet key
name: PROD-DL-01
kind: prod-downloaders
purpose: "reproduce download timeouts"
ssh_key_path: ~/.ssh/ant_deploy
prod_downloaders:
  spec: lon1:s-1vcpu-2gb                     # single VM: region[:vm_size]
  addresses_path: /path/to/addresses.txt
  binary: { version: 0.2.4 }
  # optional: clock_skew: -2h — disable time sync + shift this VM's clock by a signed Go-style duration (e.g. 2h, -2h, 30m; testing only)
```

```yaml
# prod-canary — upload VM + probe VMs; keeps monitoring on destroy
name: CAN-01
kind: prod-canary
purpose: "baseline chunk-loss measurement"
ssh_key_path: ~/.ssh/ant_deploy
prod_canary:
  binary: { version: 0.2.4 }
  secret_key_env: ARBITRUM_ONE_SECRET_KEY
  upload_spec: lon1                          # REGION[:VM_SIZE]
  urls_file: /path/to/urls.txt
  probe_concurrency: 3
  probes:                                     # shard count = Σ count
    - { region: nyc1, count: 2 }
    - { region: sgp1, vm_size: s-2vcpu-4gb, count: 1 }
  keep_monitoring: true                       # preserve InfluxDB on destroy — the dataset is the product
```

The other kinds are `prod-continuous-downloader` (like `prod-downloaders` but
walking the list once a day, anchored to `daily_at`), `prod-local-uploads`,
`prod-local-downloads`, and `testnet-extension` (set `extension.source:` to the
source testnet).

## Reuse

- `extends: profile:<name>` deep-merges one of the profiles bundled with the tool under
  the child, resolved from the installed package rather than from the manifest's
  directory — so a run manifest works wherever it lives. `<name>` is a bare name:
  `profile:staging`, never `profile:staging.yaml` or a path.
- `extends: <relative-path>` still deep-merges a base manifest found relative to the
  child, which is how an operator's own private base manifests keep working. An old run
  manifest that said `extends: ../profiles/staging.yaml` resolves again once the path is
  corrected to the profiles' new home, but `profile:staging` is the form to use.
- Either way, **lists are replaced**, not appended — redefine `clients`/`uploaders` to
  swap them wholesale.
- `--set dotted.key=value` overrides a single value at run time
  (e.g. `--set testnet.nodes.nat_percent=0`).
- `--name NEW` overrides the deployment name (reuse one run manifest under a new name).

## Secrets

Never put wallet keys in a manifest. Prod kinds name the env var they read
(`secret_key_env:`, default `ARBITRUM_ONE_SECRET_KEY`); `up` fails fast if it's unset.
Testnet client uploads read `ANT_DEPLOY_SECRET_KEY` and the `ANT_CLIENT_AWS_*` creds
from the environment, same as before.

See `../README.md` for the full manifest schema reference.
