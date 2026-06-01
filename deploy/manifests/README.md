# Deployment manifests

Declarative YAML descriptions of deployments. One manifest = one deployment's full
desired state. The orchestrator (`saorsa-deploy deployments up`/`down`/`plan`) turns a manifest
into the correct, strictly-ordered sequence of `saorsa-deploy` commands — replacing
the hand-edited bash runbooks in `../../runbooks/`.

## Layout: committed profiles vs. local runs

```
deploy/manifests/
  profiles/        # COMMITTED — reusable base profiles + the reference template
    reference.yaml
    staging-rc.yaml
  runs/            # GIT-IGNORED — your concrete deployment manifests live here
```

- **`profiles/`** is in version control. These are the reusable *shapes* — they change
  rarely. A profile has no per-run identity; a run `extends:` it.
- **`runs/`** is git-ignored (same rationale as `runbooks/`). A concrete manifest like
  `runs/STG-01.yaml` pins specific versions/branches and churns every deployment, so it
  stays local and is never committed. Create your run manifests here.

```bash
# Anything in profiles/ is committed and always present:
saorsa-deploy deployments config validate --config deploy/manifests/profiles/reference.yaml
saorsa-deploy deployments plan            --config deploy/manifests/profiles/reference.yaml

# Your own runs (local, git-ignored):
saorsa-deploy deployments plan --config deploy/manifests/runs/STG-01.yaml   # preview the sequence
saorsa-deploy deployments up   --config deploy/manifests/runs/STG-01.yaml   # deploy
saorsa-deploy deployments down --name STG-01                                # destroy (state-driven; --config also works)
```

## Committed files

| File | Kind | Notes |
|---|---|---|
| `profiles/reference.yaml` | testnet | Every available option, populated. Copy it into `runs/` and delete what you don't need. |
| `profiles/staging-rc.yaml` | testnet | Reusable base profile (no `name`). A run `extends:` it. |
| `profiles/small.yaml` | testnet | Small base profile (~100 nodes, a couple of 10 MB uploaders). |

## Writing a run manifest

A testnet run that extends the staging-rc profile (drop this in `runs/STG-01.yaml`):

```yaml
extends: ../profiles/staging-rc.yaml   # path is relative to THIS file
name: STG-01
purpose: "RC sanity check"
testnet:
  binaries:
    ant_node: { version: 0.11.5-rc.4 }
    ant:      { version: 0.2.5-rc.4 }
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
    # optional per-uploader: clock_skew: true — disable time sync + shift that VM's clock (testing only)
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
  # optional: clock_skew: true — disable time sync + shift this VM's clock (testing only)
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

The other kinds are `prod-continuous-downloader` (like `prod-downloaders` plus
`sleep_interval_secs`), `prod-local-uploads`, `prod-local-downloads`, and
`testnet-extension` (set `extension.source:` to the source testnet).

## Reuse

- `extends: <relative-path>` deep-merges a base manifest under the child. **Lists are
  replaced**, not appended — redefine `clients`/`uploaders` to swap them wholesale.
- `--set dotted.key=value` overrides a single value at run time
  (e.g. `--set testnet.nodes.nat_percent=0`).
- `--name NEW` overrides the deployment name (reuse one run manifest under a new name).

## Secrets

Never put wallet keys in a manifest. Prod kinds name the env var they read
(`secret_key_env:`, default `ARBITRUM_ONE_SECRET_KEY`); `up` fails fast if it's unset.
Testnet client uploads read `ANT_DEPLOY_SECRET_KEY` and the `ANT_CLIENT_AWS_*` creds
from the environment, same as before.

See `../README.md` for the full manifest schema reference.
