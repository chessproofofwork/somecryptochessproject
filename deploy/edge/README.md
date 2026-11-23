# The second node: a public read-only mirror on its own VM

The testnet's primary VM runs the only authoritative node. Everything the chain
does — mining, matches, settlement, the faucet — happens there. That is one
failure domain for reads as well as writes, and a single node proves nothing
about replication.

This deployment is the second failure domain: an `e2-micro` running one node in
a separate Compose project, pull-syncing the primary, serving the same JSON API
at its own HTTPS name, and refusing every write. It is the smallest host that can
hold the chain, and it is deliberately boring: no miner, no relay, no faucet, no
Docker socket, no inbound port from the internet except 443 (and 80 for the ACME
challenge).

## What it is for

- **Replication is tested, not assumed.** `--peer` with `--peer-poll-seconds 30`
  keeps pulling the primary's blocks; `sync_peers` checks the genesis hash, the
  parent chain and the proof rules before adopting anything.
- **Reads survive the primary.** Blocks, matches, proofs and status stay
  answerable while the primary is down or being redeployed.
- **Public exposure is one narrow surface.** The proxy speaks TLS, allows GET,
  caps request bodies and answers every other method — HEAD and all writes
  included — with 405 and `Allow: GET`.

It is *not* a second authority: it never mines, never signs, and never accepts a
transaction. Writes go to the primary node (the app already reaches it through
`https://app.proofofchess.com/v1`).

## Create the VM

The firewall for this VM is separate from the primary's: only 80/443 are open and
SSH is over IAP, so there is no inbound `22`, `8645` or `8646`. Pass `--network`
*and* `--subnet` explicitly: the rules below are created in `$NETWORK`, and an
instance created without them lands in `default`, where none of them apply — the
symptom is a mirror that is up, healthy on loopback and unreachable from the
internet. The VPC is custom-mode (one subnet per region), so naming the network
alone is not enough either.

```bash
export PROJECT_ID="chessproofofwork"   # the project the primary VM runs in
export NETWORK="sccp-testnet"          # the VPC the primary VM and these rules use
export REGION="us-central1"
export ZONE="us-central1-a"
export SUBNET="sccp-testnet-${REGION}" # custom-mode VPC: one subnet per region
export EDGE="sccp-edge"
export EDGE_TAG="sccp-edge"
export EDGE_IP="sccp-edge-ip"

gcloud compute addresses create "$EDGE_IP" --region="$REGION" --project="$PROJECT_ID"

gcloud compute instances create "$EDGE" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --network="$NETWORK" \
    --subnet="$SUBNET" \
    --machine-type=e2-micro \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size=20GB \
    --tags="$EDGE_TAG" \
    --address="$(gcloud compute addresses describe "$EDGE_IP" \
        --region="$REGION" --format='value(address)' --project="$PROJECT_ID")" \
    --shielded-secure-boot \
    --shielded-vtpm \
    --shielded-integrity-monitoring \
    --no-service-account \
    --no-scopes

# 443 for the mirror, 80 for the certificate challenge and its redirect.
gcloud compute firewall-rules create sccp-edge-web \
    --project="$PROJECT_ID" \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:80,tcp:443 \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$EDGE_TAG"

# No 22 from the internet: IAP tunnels SSH, so there is no SSH port to find.
gcloud compute firewall-rules create sccp-edge-iap-ssh \
    --project="$PROJECT_ID" \
    --network="$NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 \
    --target-tags="$EDGE_TAG"

gcloud compute ssh "$EDGE" --zone="$ZONE" --project="$PROJECT_ID" --tunnel-through-iap
```

Point an `A` record for the mirror's name at `EDGE_IP` before starting the stack:
Caddy cannot finish the HTTP-01 challenge until the name resolves to this VM.

### Give it swap

The stack fits in an `e2-micro`, but not with room to spare: the node, Caddy,
Docker and the guest agent sit at roughly 930 MB of the 969 MB the kernel sees,
and the image ships no swap. A host that runs out does not exit — it stops
scheduling userland, so the kernel still completes TCP handshakes from the accept
queue while sshd never sends a banner. That is what a wedged mirror
looks like: `https://<domain>/v1/status` times out, the instance is still
`RUNNING`, and the serial console is full of failed container health checks.
Create the swap file once:

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=20' | sudo tee /etc/sysctl.d/60-sccp-swap.conf
sudo sysctl vm.swappiness=20
free -m          # the Swap line must be non-zero, and must survive a reboot
```

Swap on a small disk is not a performance feature here; it is what keeps `sshd`
able to answer while the node replays its chain, which is the only job this host
has. If it wedges again with swap in place, move the VM to `e2-small`.

## Deploy

The VM needs Docker Engine and the Compose plugin (the primary's
`docs/setup/GCP_RESOURCE_SETUP.md` section 5 has the Debian commands). The
stack itself is two files — `docker-compose.yml` and `Caddyfile` — plus a `.env`
that names the image, the mirror's domain and the primary URL.

### On the VM

Copy this directory's `docker-compose.yml`, `Caddyfile` and `.env.example` to
`/srv/sccp-edge` on the VM; `gcloud compute scp --tunnel-through-iap` works
because SSH is IAP-only. The repository checkout is not needed after those three
files are in place. Set `SCCP_IMAGE` in `.env` to the tag the primary deploy
pushed (`:testnet` or the commit sha).

```bash
# on the edge VM, as the deploy user, in /srv/sccp-edge
cp .env.example .env && $EDITOR .env   # image, domain, primary URL
set -a && . ./.env && set +a           # the same values, for the checks below
# If the registry package is private, log in first with a token that can read
# packages:
#   printf '%s' "$TOKEN" | docker login ghcr.io -u <user> --password-stdin
docker compose pull node
docker compose up -d --no-build
docker compose ps
curl -fsS "https://$SCCP_EDGE_DOMAIN/v1/status"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
    "https://$SCCP_EDGE_DOMAIN/v1/tx" -d '{}'   # 405, by design
curl -sS -o /dev/null -D - -X POST \
    "https://$SCCP_EDGE_DOMAIN/v1/tx" -d '{}' | grep -i '^allow: GET'
```

The mirror does not need the source tree: `docker compose pull` is the only step
that talks to the registry, and `--no-build` keeps Compose from trying to build a
500 MB image on a 1 GB VM if the tag is missing. `docker compose up -d` after a
`SCCP_IMAGE` change recreates the node container and keeps the volume.

Keeping the volume is only right while the new build can read it. The node
replays its stored chain at startup, so an image that changes a consensus
parameter cannot apply blocks written by the previous one and exits instead of
serving. After changing `SCCP_IMAGE`, wait for health; if the node cannot replay
the store, stop it, delete `/data/*` and start once more. Case 1 of
[`docs/operations/MIRROR_OUTAGE.md`](../../docs/operations/MIRROR_OUTAGE.md) has
the exact commands.

### What to watch

```bash
docker compose logs -f node      # "replicated <peer>: +N blocks to height H"
docker compose logs -f tls       # certificate and request log
docker compose exec node sccp --data-dir /data chain info   # if it stops advancing
```

`--data-dir` is a global option, so it goes before `chain`; `chain status` is not
a command.

A mirror whose height trails the primary by more than a couple of blocks is
either failing to reach `SCCP_PRIMARY_URL` or being refused by it (the log line
names which). It is safe to delete `node-data` and start over: the chain is
rebuildable from the primary in one sync.

### Roll back

The image tag is the only moving part. Pin the previous one in `.env` and
recreate the node; the volume and the certificate are untouched:

```bash
# Edit SCCP_IMAGE in .env to the previous tag, then:
docker compose pull node && docker compose up -d --no-build
```

Docker keeps the previous image locally, so a rollback on the VM does not need
the registry at all. `caddy_data` is what stops a redeploy from re-issuing a
certificate and burning Let's Encrypt's weekly limit.

## Why the API is read-only here

`POST /v1/tx` is how a wallet submits a transaction, and an open write endpoint
on a small VM is an invitation to fill its disk with mempool entries, however
well the node validates them. The primary already terminates TLS for the app and
takes writes; the mirror's job is reads. If that changes, it is one matcher in
`Caddyfile` plus a rate limit and a bigger disk — not a config flag to flip
casually.
