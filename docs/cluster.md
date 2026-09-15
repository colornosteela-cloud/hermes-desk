# LAN cluster

Clone this repo onto each host. Each `hermes-deskd` uses **that** host's hardware, `HERMES_DESK_HOME`, workspaces, and cloud keys. The browser talks to **one origin** (typically teela-brain at `http://10.0.0.10:8742/`). That origin merges peer rosters, reverse-proxies bot HTTP, fans SSE, and forwards teammate DMs.

## Three different secrets

Do not mix these up.

| Secret | Where | What you do with it |
| --- | --- | --- |
| **Cluster token** | `~/.hermes/desk.json` → Cluster tab | Shared mesh password. Generate **once**. Paste into **Cluster token** on the other hosts. |
| UI Bearer | `$XDG_RUNTIME_DIR/hermes-desk/token` | Automatic. The browser already has it. Ignore it. |
| xAI / Hermes cloud key | that host's `~/.hermes/auth.json` (`hermes login`); bots **share** this file (symlinked into each bot's `HERMES_HOME`), they must not get a second copy | How body/jetson *think*. Not the cluster token. |

“Confirm current token” is **not** where you paste the new secret for teela-body. Leave it blank on first setup.

## First-time setup (do this in order)

Settings → **LAN cluster** is a four-step wizard. Each step has its own button. Open **`http://127.0.0.1:8742/` on that machine** (a phone or the LAN IP cannot Save token or peers).

Footer **Save settings** is for listen address / profile. For the mesh, use the step buttons: **Save name**, **Generate** or **Save token**, **Save peers**, **Test connection**.

### 1. teela-brain — create the shared token

1. Open `http://127.0.0.1:8742/` on teela-brain.
2. Settings → **LAN cluster**.
3. Node name: `teela-brain`.
4. Click **Generate**. Copy the token that appears.
5. **Do not** paste it into **Confirm current token**. Leave Confirm blank.
6. First Generate also saves the token on brain. You should see “Token: saved on this host”.
7. Keep that copied token for the other machines.

### 2. teela-body — paste the same token

1. On teela-body: install Hermes Agent, `git clone` this repo, `./start.sh`, `hermes login`.
2. Open **`http://127.0.0.1:8742/`** on teela-body (localhost on body, not brain’s IP).
3. Settings → LAN cluster.
4. Node name: `teela-body`.
5. Paste the token into **Cluster token** (the first box).
6. Leave **Confirm current token** blank (it is hidden until this host already has a saved token).
7. **Save settings.** Status should say `token saved`.

Repeat the same paste+Save on teela-jetson when you get there.

### 3. Point the hosts at each other (peers)

A token alone is not a mesh. Each host must list the others by **LAN IPv4**.

On **teela-body**, Add peer:

- name: `teela-brain`
- URL: `http://10.0.0.10:8742`

On **teela-brain**, Add peer:

- name: `teela-body`
- URL: `http://<body-LAN-IP>:8742` (the IPv4 of body, not a hostname)

Then **Save peers** on each host. Saving an RFC1918 peer URL automatically promotes this desk off loopback: `listen_host` becomes this machine’s LAN IP and deskd binds `0.0.0.0:8742` so the other host can fetch `/v1/cluster/bots`. You do not have to type the LAN IP into Profile first. `HERMES_DESK_LOOPBACK=1` keeps the old loopback-only bind for tests.

After both hosts are saved and **deskd is running on each LAN IP**, the hallway on either origin lists local bots plus the other host’s bots (and a stub row for a peer that is configured but offline). If a peer is down, Search shows `Peers: teela-body offline…` — you will not see that computer’s agents until its `./start.sh` is listening on `http://<its-LAN-IP>:8742/`.

### 4. Test connection

Click **Test connection** only after Save. It uses the last Save, not unsaved boxes.

You want `forward ok` toward the other host. `Token saved: NO` or `Saved peers: 0` means step 2 or 3 was skipped. `auth` means the two hosts do not have the same saved token. `offline` means the URL/IP/port is wrong or deskd is not listening there.

## Reset and do it again

From `http://127.0.0.1:8742/` on teela-brain, Cluster step 2 → **Generate / reset token**. That overwrites the secret on this computer (no Confirm needed on localhost). Copy the new token. On teela-body, paste → **Save token**. Fingerprints must match, then Test.

| You are doing | What to click |
| --- | --- |
| First generate on brain | Generate / reset token, then Copy |
| First paste on body/jetson | Paste → Save token |
| Tokens do not match (`auth`) | Reset on brain, paste the new one on body |
| Clear this host’s token | Clear cluster token (localhost only) |

## Peer URLs

- `http` only, IPv4 literals in RFC1918 or `127.0.0.0/8`
- No hostnames, no userinfo, no `169.254.0.0/16`
- Git examples use placeholders `10.0.0.xx` / `10.0.0.yy` — fill real IPs

## Cognition stays per-node

Do not share vLLM. Do not bounce brain's local model or shrink 262k. `/v1/llm*` is loopback-only and is never proxied.

Each host's model picker is **that** host's `~/.hermes/config.toml`. Bots on brain list brain's local models (grayed from brain's vLLM occupancy). Bots on body list body's catalog (grayed from body's vLLM if it has one). Catalogs are not mixed. Cloud Hermes only appears if that host configured it.

| Host | Typical model | Mesh |
| --- | --- | --- |
| teela-brain | local vLLM `qwen38` | same `cluster_token` |
| teela-body | Hermes 4.6 cloud | same `cluster_token` |
| teela-jetson | Hermes 4.6 cloud | same `cluster_token` |

## Names vs ids

Pass **bot id** for DMs. Names may collide across nodes and resolve local-first, then `peers[]` order.

## Rollback

From localhost: Cluster tab → empty peer list → Save. Or set `HERMES_DESK_CLUSTER=0` to ignore peers while debugging. Local bots are untouched.

SSE fan-in has no replay. A reconnect can drop in-flight tokens (same class of gap as a local EventSource reconnect).
