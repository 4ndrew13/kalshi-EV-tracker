# Deployment

Everything needed to get the collector running on an OCI Always Free VM, and how code
changes reach it afterwards.

---

## 1. Creating the instance

Choices that are painful to change later are marked ⚠.

| Setting | Value | Why |
|---|---|---|
| **Shape** | `VM.Standard.A1.Flex`, **2 OCPU / 12 GB** | Preferred. The memory lever is the cheapest defence if you are ever flagged as idle — memory is a reclamation criterion on A1 shapes **only**. |
| *fallback* | `VM.Standard.E2.1.Micro` | Use if A1 reports "out of capacity". Try a different availability domain first; capacity fluctuates. Micro is a *separate* allowance, so it does not consume the A1 grant. |
| **Image** | Canonical **Ubuntu 24.04 LTS** (22.04 fine) | `setup.sh` assumes `apt`. |
| ⚠ **Home region** | Nearest to you | **Permanent — cannot be changed.** Also governs A1 capacity availability. |
| **Boot volume** | Default (~50 GB) | Wildly oversized. The full ladder is ~1 MB/day, ~380 MB/year. |
| **Public IPv4** | Assign | Needed for SSH. |
| **Ingress rules** | Leave default | The collector is outbound-only. Default VCN already allows SSH on 22. Open nothing else. |

### ⚠ SSH keys

Generate the pair **on your laptop** and paste only the public half into the console:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/kalshi_oci -C kalshi-collector
cat ~/.ssh/kalshi_oci.pub          # paste this into "Add SSH keys"
```

Oracle can generate a pair for you, but then the private key travels through a browser
download. Generating locally means the private key never leaves your machine.

**Login user is `ubuntu`** on Ubuntu images (`opc` is Oracle Linux — a common 10-minute
confusion).

```bash
ssh -i ~/.ssh/kalshi_oci ubuntu@<public-ip>
```

### After it boots

Consider upgrading the tenancy to **Pay As You Go**. Always Free resources stay free, but
the tenancy becomes exempt from idle reclamation. Held in reserve per SPEC.md — the
decision there is to ship and react — but it is the one lever that removes the risk
outright.

---

## 2. What I need from you once it exists

1. **Public IP**, and confirmation the user is `ubuntu`.
2. **Which shape you got** — A1 or Micro. It determines whether the memory lever exists if
   you are ever flagged idle.
3. **SSH access, or not.** Either add my key / share `~/.ssh/kalshi_oci`, or run the
   commands yourself and paste back output. The box holds no Kalshi credentials and can
   place no orders, so either is defensible.
4. **`KALSHI_SHEETS_ID`** — confirm it is filled in your `.env` (the long string in the
   sheet URL between `/d/` and `/edit`), and that the sheet is shared with the service
   account's `client_email` as **Editor**.
5. **Backup target** — `rsync` destination or a git remote for `deploy/backup.sh`. The
   Sheet carries only the ~11 live buckets; without this the other ~177 exist solely on
   the VM's disk.

---

## 3. First install

```bash
# on the VM
sudo apt-get update && sudo apt-get install -y git
git clone <your-repo-url> /tmp/kalshi && sudo /tmp/kalshi/deploy/setup.sh <your-repo-url>
```

Then place the two secrets (see §5), and **gate on the self-test**:

```bash
sudo -u kalshi /opt/kalshi-collector/.venv/bin/python -m kalshi_collector.main selftest
```

This is the first time the Coinbase / Kraken / Bitstamp composite and the basis-vs-BRTI
check run anywhere — those hosts were unreachable from the build machine. Do not start the
service until it passes.

```bash
sudo systemctl enable --now kalshi-collector
journalctl -u kalshi-collector -f
```

---

## 4. How code changes reach the instance

**You never edit files on the box.** Two options; pick one.

### Option A — git (recommended)

Private repo is fine; the code contains no secrets. One-time, give the VM read-only access
via a **deploy key**:

```bash
# on the VM
sudo -u kalshi ssh-keygen -t ed25519 -f /home/kalshi/.ssh/id_ed25519 -N ""
sudo cat /home/kalshi/.ssh/id_ed25519.pub
```

Paste that into **GitHub → repo → Settings → Deploy keys → Add**, leaving *Allow write
access* **unchecked**. A read-only key means a compromised VM cannot rewrite your code.

Thereafter, every change is:

```bash
git push                                                    # laptop
ssh <vm> 'sudo /opt/kalshi-collector/deploy/update.sh'      # deploy
```

### Option B — rsync, no GitHub at all

```bash
./deploy/push.sh ubuntu@<public-ip>
```

Syncs the working tree and deploys in one step.

### What `update.sh` does

1. Pulls (or uses the rsynced files).
2. Installs any new dependencies.
3. **Runs the self-test against live APIs — and aborts if it fails**, leaving the working
   collector running rather than replacing it with a broken one.
4. Restarts the service.

Two guards worth knowing:

- **It refuses to restart between HH:53 and HH:58.** The ladder is perishable: a restart
  through `HH:56` costs that hour permanently and no retry recovers it. Settlements are
  durable and repopulate via the startup backfill, so that window is the *only* time a
  deploy can actually destroy data. `FORCE=1` overrides.
- `SKIP_SELFTEST=1` if you know why it fails — most often the prior hour has not settled
  yet, which the basis check needs.

Deploy any time outside the last ten minutes of the hour and the worst case is a few
seconds of downtime that heals itself.

---

## 5. Where your real values live

**Yes — on the Ubuntu server, and nowhere else.**

| File | Contents | Mode |
|---|---|---|
| `/opt/kalshi-collector/.env` | `KALSHI_SHEETS_ID`, `KALSHI_HEALTHCHECK_URL`, backup targets | `600`, owner `kalshi` |
| `/opt/kalshi-collector/service-account.json` | Google service-account key | `600`, owner `kalshi` |

Both are:

- **Untracked** — listed in `.gitignore`, so `git push` cannot carry them.
- **Excluded from every sync path** — `push.sh` and `update.sh` both skip them explicitly,
  so a deploy can never overwrite or leak them.
- **Loaded by systemd** via `EnvironmentFile=`, so the values reach the process as
  environment variables and never appear in the code or in a command line.

`data/` is treated identically: excluded from every sync, so a deploy can never clobber the
system of record.

### What is actually at risk

The collector holds **no Kalshi credentials by design** — it cannot place an order. The
only real secret is the Google key, and it is scoped to a single spreadsheet. The
Healthchecks URL is a ping token: someone with it could send false "OK" pings, which would
mask an outage but expose nothing.

### Two practical consequences

1. **Keep a copy of `service-account.json` in a password manager.** If the VM is reclaimed
   or rebuilt, `.env` and the key are gone with it. Everything else — code and data — is
   recoverable from git and the backups; these two are not.
2. **Never paste either into a chat, an issue, or a commit.** `scp` the key directly:

   ```bash
   scp -i ~/.ssh/kalshi_oci service-account.json ubuntu@<ip>:/tmp/
   ssh <vm> 'sudo mv /tmp/service-account.json /opt/kalshi-collector/ && \
             sudo chown kalshi:kalshi /opt/kalshi-collector/service-account.json && \
             sudo chmod 600 /opt/kalshi-collector/service-account.json'
   ```
