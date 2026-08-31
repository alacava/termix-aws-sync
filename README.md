# termix-aws-sync

One-way sync of running AWS EC2 instances into a [Termix](https://github.com/Termix-SSH/Termix)
host inventory. Termix has no native AWS integration (its built-in cloud
discovery covers Proxmox and Tailscale only), so this fills the same role
Termius's AWS integration plays: an auto-updating group of hosts that
tracks your cloud environment. It runs from Docker, cron, or a systemd
timer — diffs EC2 against Termix, and creates/updates/deletes Termix hosts
accordingly.

AWS is always the source of truth. Termix is never read to modify AWS.

## Docker quick start

```sh
git clone <this repo>
cd termix-aws-sync
cp .env.example .env        # edit: set TERMIX_API_KEY + TERMIX_URL, adjust SYNC_INTERVAL
cp config.toml.example config.toml   # or write your own, see "Configuration" below
$EDITOR config.toml          # set your AWS profile(s)/region(s), folder, credential_id
docker compose up -d
docker compose logs -f
```

`docker compose run --rm sync --dry-run` gives you a one-shot plan from the
same compose file without starting the loop — always do this first against
a new config (see "The --dry-run-first workflow" below).

AWS auth inside the container comes from the mounted `~/.aws` (read-only,
see `docker-compose.yml`) — every `profile` named in `config.toml` must
exist in that mounted directory — or from `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` set in `.env` as an alternative for a single
implicit profile.

Pre-built images are published to Docker Hub as
[`antlac1/termix-aws-sync`](https://hub.docker.com/r/antlac1/termix-aws-sync)
on every version tag. To use one instead of building locally, swap
`docker-compose.yml`'s `build: .` for `image: antlac1/termix-aws-sync:latest`
(or a specific version, e.g. `:0.1.0`).

## Bare-metal install

Requires Python 3.9+ and [awscli v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
Termix is accessed via its REST API directly (see "A note on the Termix
CLI vs. API" below) — no separate Termix CLI/Node.js install needed.

```sh
pipx install .
# or: pip install .
termix-aws-sync --dry-run
```

## Prerequisites

- **awscli v2** on `PATH` (`aws --version`), configured with a profile (or
  instance role / env-var credentials) for every AWS account you sync.
- A **Termix API key** (`TERMIX_API_KEY`) and your Termix server's base URL
  (`TERMIX_URL`, e.g. `http://termix.example.com:8080`) — both required,
  never put the key in `config.toml`.
- IAM permission for **`ec2:DescribeInstances` only** — this tool never
  calls anything else against AWS. Minimal policy:

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": "ec2:DescribeInstances",
        "Resource": "*"
      }
    ]
  }
  ```

## A note on the Termix CLI vs. API

Earlier versions of this tool drove Termix through its official CLI
(`@termix-cli/cli`), and AWS is still accessed the same way today (via
`aws ec2 describe-instances`). Termix itself was switched to its REST API
directly after finding a reproducible bug: `termix hosts create/update
--credential-id` fails server-side (`HTTP 500`, a Postgres `NOT NULL`
violation on `auth_type`), because the CLI doesn't send an explicit
`authType` field the server actually requires — confirmed by reading
Termix's own (open source) server code. Calling the API directly and
always sending `authType` explicitly is the fix, and was confirmed against
a live server before this switch was made. This is a deliberate,
evidence-based exception to "prefer official, versioned/documented
interfaces over undocumented ones" — not a default to reach for casually.

Practical implications: `TERMIX_URL` is now required (there's no
CLI-configured server to fall back on), and there's no Node.js/`termix`
CLI to install anywhere — one less runtime dependency.

## Internal vs. external IP

Each Termix host is registered with either the instance's **internal**
(private/VPC) or **external** (public) IP address:

- Use **internal** (`"internal"`/`"private"`) when the machine running
  Termix reaches your instances over VPC peering, a VPN, Direct Connect, or
  a mesh network like Tailscale.
- Use **external** (`"external"`/`"public"`) when Termix connects to
  instances over the public internet.

This is settable at three levels, in precedence order (most specific wins):

1. **Per-instance**, via the EC2 tag `termix:ip` = `private`/`public` (or
   `internal`/`external`).
2. **Per-target**, via `ip_source` in a `[[aws.targets]]` block.
3. **Globally**, via `[aws].ip_source` (or the `IP_SOURCE` env var, which
   overrides the global default — handy in Docker without editing the
   mounted config).

An instance missing the resolved field (e.g. `external` selected but the
instance has no public IP) is **skipped with a warning**, not synced with a
blank/wrong address.

The legacy config key `ip_field` (raw values `PrivateIpAddress` /
`PublicIpAddress`) is accepted as a synonym for `ip_source` at every level,
for direct compatibility with the original single-file reference script.

## Multi-environment example

Each `[[aws.targets]]` block is one AWS environment (an account profile +
region). Hosts from that target land in their own Termix folder, and can
use their own SSH credential — while still sharing one flat identity space
keyed on EC2 instance ID (instance IDs are globally unique across AWS
accounts), so an instance is never duplicated across environments.

```toml
[termix]
folder = "AWS"               # root folder; per-target default is "AWS / <name>"
managed_tag = "aws-sync"
extra_tags = ["aws"]
credential_id = 3             # global default SSH credential

[aws]
ip_source = "internal"
default_username = "ec2-user"
default_port = 22

[[aws.targets]]
name = "production"           # -> folder "AWS / production", tagged "production"
profile = "prod-account"
region = "us-east-1"
# credential_id = 7           # different key pair for this account

[[aws.targets]]
name = "staging"
profile = "staging-account"
region = "us-east-2"
# default_username = "ubuntu"  # this account's AMIs use a different login user
# ip_source = "external"       # and this one's reached over the public internet

[[aws.targets]]
region = "us-east-1"          # unnamed: goes to the global folder "AWS"
```

Folder resolution per target: explicit `folder` > `"<global folder> / <name>"`
when `name` is set > global `[termix].folder`. Termix nests folders by
splitting on the literal `" / "` (with spaces) — a bare `/` with no spaces
is stored as one flat folder whose name contains a slash character, not a
nested subfolder, so keep that delimiter in any explicit `folder` override
too. If a host is later found in the wrong folder for its target (e.g. you
renamed or moved a target), the sync treats that as drift and moves it.
`credential_id`/`key_file`/`ip_source`/`default_username` all resolve the
same way (per-target overrides global), so different environments can use
different SSH credentials, IP sources, and default login users — each
still overridable for a single instance via its own EC2 tags
(`termix:user`, `termix:ip`, `termix:port`).

**The mounted `~/.aws` (or env-var credentials) must cover every `profile`
named in `config.toml`** — a target whose profile isn't configured will
fail that target's `aws ec2 describe-instances` call.

Duplicate instance IDs across targets (e.g. the same account listed twice)
are detected and rejected with an error naming both targets, rather than
silently syncing the instance twice.

## Configuration reference

Config is TOML. Search order: `--config PATH`, then
`$TERMIX_AWS_SYNC_CONFIG`, then `/etc/termix-aws-sync.toml`, then
`~/.config/termix-aws-sync.toml`.

```toml
[termix]
# api key comes from TERMIX_API_KEY env, never from this file
folder = "AWS"               # root folder; per-target default is "AWS / <name>"
managed_tag = "aws-sync"
extra_tags = ["aws"]
credential_id = 3            # global default; overridable per target
# key_file = "/home/app/.ssh/aws-fleet.pem"

[aws]
ip_source = "internal"          # "internal"/"private" or "external"/"public" (global default)
default_username = "ec2-user"
default_port = 22
# required_tag = { key = "termix", value = "true" }

[[aws.targets]]
name = "production"
profile = "prod-account"
region = "us-east-1"
# folder = "Ops / Production"   # explicit folder override beats the name-derived default
# credential_id = 7
# ip_source = "external"
# default_username = "ubuntu"

[[aws.targets]]
name = "staging"
profile = "staging-account"
region = "us-east-2"

[[aws.targets]]
region = "us-east-1"
```

At least one `[[aws.targets]]` entry is required. Each target needs
`region`; `profile` and `name` are optional (omit `profile` to use the
default AWS credential chain — env vars or an instance role). Every target
must resolve an SSH auth method (`credential_id` or `key_file`, per-target
or inherited from `[termix]`) — config load fails with a clear error
otherwise. This is a hard requirement of Termix itself: its host create/
update endpoints reject the call outright without an explicit auth method
(confirmed against a real server — and there is no folder-level credential
fallback), so it's caught here at config load rather than failing on every
single host, every cycle, forever. `ip_source` and `default_username` are
also resolved per-target (target overrides global), and both can be
overridden again for a single instance via its EC2 tags
(`termix:ip`, `termix:user`).

If you want every host in one environment to share a single credential
without repeating it per instance, set `credential_id` once on that
target (or globally in `[termix]`) — that already applies to every host
synced from it, which is the practical equivalent of a "folder default."

**Env overrides** (highest precedence for the values they cover, primarily
for Docker):

| Variable | Purpose |
|---|---|
| `TERMIX_API_KEY` | **Required.** Termix auth. Never put this in the TOML file. |
| `TERMIX_URL` | **Required.** Base URL of your Termix server, e.g. `http://termix.example.com:8080`. |
| `SYNC_INTERVAL` | Loop-mode interval in seconds (same as `--interval`). Ignored by `--dry-run`. |
| `IP_SOURCE` | Overrides the global `[aws].ip_source`. Per-target/per-instance settings still take precedence over it. |
| `TERMIX_AWS_SYNC_CONFIG` | Config file path (second in the search order, after `--config`). |

Validation happens at load time: missing/invalid config (including an
invalid `ip_source` — the error names the accepted values) fails fast with
exit code 2 and a clear message, never a traceback.

## The `--dry-run`-first workflow

Always run with `--dry-run` before letting a new or changed config apply
anything:

```sh
termix-aws-sync --config config.toml --dry-run
# or: docker compose run --rm sync --dry-run
```

This prints the full create/update/delete plan and changes nothing (exit
0). Once the plan looks right, drop `--dry-run` (or just let the
container's loop apply it on its next cycle).

## CLI reference

```
termix-aws-sync [--config PATH] [--dry-run] [--debug] [--version] [--interval SECONDS]
```

- `--dry-run`: print the plan, change nothing, exit 0.
- `--interval SECONDS` (or `SYNC_INTERVAL` env): loop mode — sync, sleep,
  repeat. `SIGTERM`/`SIGINT` finish or abort the current sleep and exit 0.
  A failed cycle is logged and the loop continues; it never crash-loops.
  Omitting both (the default) is one-shot mode, for cron/systemd.
- `--debug`: verbose logging, including every AWS CLI command run and
  every Termix API request/response — see Troubleshooting below.
- Exit codes: `0` in sync or applied cleanly, `1` one or more operations
  failed (partial apply), `2` config/auth error (nothing was attempted, or
  the initial AWS/Termix state couldn't even be fetched).

## Cron

```sh
sudo install -o root -g root -m 0600 /dev/null /etc/termix-aws-sync.env
sudo $EDITOR /etc/termix-aws-sync.env    # TERMIX_API_KEY=... and TERMIX_URL=...
crontab -e                               # see contrib/crontab.example
```

## systemd timer

```sh
sudo install -o root -g root -m 0600 /dev/null /etc/termix-aws-sync.env
sudo $EDITOR /etc/termix-aws-sync.env
sudo cp contrib/termix-aws-sync.service contrib/termix-aws-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now termix-aws-sync.timer
journalctl -u termix-aws-sync -f
```

## Safety model

- **Identity is the EC2 instance ID.** Every host this tool creates carries
  a tag `aws-id-<instance-id>`, and the diff keys on that — not IP or name —
  so IP changes and renames are handled as updates, not create+delete.
- **Managed-tag marker.** Every host this tool creates also gets a
  `aws-sync` tag (configurable via `managed_tag`). The Termix API has no
  server-side tag filter, so this tool fetches every host visible to the
  API key and filters to the managed tag itself before touching anything
  — hosts without it are never listed, updated, or deleted. Hand-created
  Termix hosts are never touched, even on IP collision.
- **Deletes only fire when a managed host's instance ID is no longer in the
  set of running instances** — not on any other kind of drift.
- **Updates merge, they don't replace.** Termix's update endpoint is a
  full replace (confirmed directly against a live server), so a naive
  partial update would silently reset anything you configured by hand in
  the Termix UI after a managed host was created. This tool always merges
  the diff onto the host's existing full record first.
- **Secrets never appear in argv/URLs** beyond what's required.
  `TERMIX_API_KEY` is sent only as an `Authorization: Bearer` header, never
  as a URL parameter or CLI argument. `credential_id` (a saved Termix
  credential, referenced by ID) is the recommended SSH auth path for
  exactly this reason — prefer it over `key_file` when possible, since a
  key file path is just a filesystem reference, but it still avoids ever
  putting key *material* in this tool's own config or logs.

## ASG churn / opt-in tag

In environments with autoscaling groups launching and terminating
instances frequently, syncing everything can create a lot of host churn in
Termix. Set `[aws].required_tag = { key = "termix", value = "true" }` (or
the equivalent per your tagging scheme) to make sync **opt-in per
instance** — only instances carrying that exact tag/value are considered at
all; everything else is ignored by both the create and delete paths.

## Troubleshooting

**"host X has aws-sync tag but no aws-id-* tag; ignoring"** — this tool
only manages hosts it tagged itself; a hand-created host with the managed
tag but no identity tag is skipped and logged, by design.

**`termix API GET/POST/PUT/DELETE ... failed (401)`.** `TERMIX_API_KEY` is
wrong, expired, or missing the `Authorization: Bearer` prefix expectations
on the server side — verify with a direct call:

```sh
curl -i -H "Authorization: Bearer $TERMIX_API_KEY" "$TERMIX_URL/host/db/host"
```

A `200` with a JSON array back confirms the key and URL are both good.

**`could not reach termix API at ...`** means `TERMIX_URL` itself is wrong,
or the server isn't reachable from wherever this tool is running (check
Docker networking — `localhost` inside a container is the container
itself, not your host; see the internal-vs-external IP guidance above for
the same class of gotcha).

**Field-shape assumptions.** `fetch_termix_hosts()` in
`src/termix_aws_sync/termix.py` assumes each host object carries `id`,
`name`, `ip`, `port`, `username`, `tags`, and `folder` — confirmed against
a real server's `GET /host/db/host` response, but Termix could change this
in a future release. Running with `--debug` logs the raw HTTP request/
response for every call, which is the fastest way to compare against
reality if a sync starts behaving oddly (e.g. endlessly recreating or
updating hosts that look correct). Whatever the real shape turns out to
be, a wrong assumption here fails as a clean logged error and the sync
loop keeps running — it does not crash the process or crash-loop the
container.

**Exit 2 with a config/auth error and no traceback** on `--dry-run` (or any
run) means the initial AWS/Termix state fetch failed outright — check
`TERMIX_API_KEY`, `TERMIX_URL`, that `aws` is on `PATH` and authenticated,
and that AWS profiles named in `config.toml` exist. The curl command above
is a good standalone Termix auth smoke test.

## Development

```sh
pip install -e ".[dev]"
pytest
ruff check .
docker build -t termix-aws-sync .
```

## Releasing

Pushing a tag matching `v*.*.*` (e.g. `v0.1.0`) runs the full CI suite and,
if it passes, builds and pushes a `linux/amd64` image to Docker Hub as
`antlac1/termix-aws-sync:<version>`, `:<major.minor>`, and `:latest` — see
`.github/workflows/ci.yml`'s `publish` job. This job only runs on a tag
push (`github.ref` starting with `refs/tags/v`) — it shows as skipped on
ordinary branch pushes and PRs, by design.

It requires two repo secrets (Settings -> Secrets and variables ->
Actions):

- `DOCKERHUB_USERNAME` — your Docker Hub username.
- `DOCKERHUB_TOKEN` — a Docker Hub access token (Account Settings ->
  Security -> Access Tokens), not the account password.

## License

MIT — see [LICENSE](LICENSE).
