# Run a JoinBlaket directory

*[Versión en español](README-ES.md)*

This folder lets **any volunteer run a second JoinBlaket directory** at home, on a Linux computer or a Raspberry Pi, in about 15 minutes and without opening any port on the router.

---

## What is a directory, and why another one?

JoinBlaket is JoinMarket for BTC (the BLAKE2b chain). To make a CoinJoin, the people who offer liquidity (*makers*) and the people who use it (*takers*) have to find each other. The **directory** is the meeting point: a notice board at a Tor `.onion` address where makers post their offers and takers read them and message them.

At first there was only **one** directory, Kilombino's: if it went down, nobody found anybody and there were no CoinJoins. Since 0.43.0-xbt9 there are **three** (Kilombino's and two run by trusted operators), and the market keeps working when one of them fails. The more the better: nobody is indispensable.

## What a directory can and CANNOT do

**It cannot steal anything.** The directory:

- **holds no funds** and nobody's keys;
- **signs nothing** and never sees a seed;
- **cannot change a transaction**: every participant checks the whole transaction before signing it;
- **needs no Bitcoin node**, no wallet, no access to the chain.

**What its operator does see** (the same as any market participant):

- the *nicks* (random nicknames) connected, and when they come and go;
- the makers' public offers (amount ranges, fees and the proof of their fidelity bond, which includes the bond's coin);
- the `.onion` address of makers that announce one;
- which nick messages which nick and when, and a taker's opening request (the amount). The rest of the negotiation (coins, destination addresses, signatures) is **end-to-end encrypted** between taker and maker; often it does not even go through the directory, because the taker connects straight to the maker's `.onion`.

**It sees no IP addresses**: everything arrives over Tor.

**The worst a malicious or broken directory can do:** go down, or hide offers or messages (censor). That is why several are needed: clients connect to all of them at once, and one working is enough.

**Logs:** with this folder's settings the logs keep no nicks and no `.onion` addresses (non-sensitive mode). The directory keeps everything in memory and forgets it on restart.

## Requirements

- A computer that can stay **always on**, with **64-bit** Linux:
  - Debian 12 or 13, Ubuntu 22.04 or later, or
  - Raspberry Pi 4 or 5 (or 3B+) with **64-bit Raspberry Pi OS** (the 32-bit one will not work).
- Resources (measured on the current directory with 11 connections): **about 50 MB of RAM** for the directory and as much again for Tor, almost no CPU, **~1 GB of disk** for images and logs, and very little network traffic.
- A normal Internet connection. **No port has to be opened** on the router: Tor connects outwards.
- Being able to open a terminal and copy and paste commands.

About **uptime**: ideally 24 hours a day. Short outages (reboots, updates, a power cut) are fine, because clients use the other directory meanwhile. If you are going to switch it off for days, or stop for good, **tell us** so we take it off the list.

---

## Step 1. Install Docker

Open a terminal on that machine (or log in over SSH) and run:

```sh
sudo apt update
sudo apt install -y curl git
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

That is Docker's official installer; it works on Debian, Ubuntu and Raspberry Pi OS. (If you want to read it first, open `get-docker.sh` in an editor.)

Now let your user run Docker without `sudo`:

```sh
sudo usermod -aG docker $USER
```

**Log out and log back in** (or reboot). Check it works:

```sh
docker compose version
```

It should print something like `Docker Compose version v2...`.

## Step 2. Download JoinBlaket

```sh
cd ~
git clone --depth 1 -b xbt https://github.com/Kilombino/joinblaket.git
cd joinblaket/directory-xbt
```

From here on, every command runs **inside `~/joinblaket/directory-xbt`**.

## Step 3. Start it (first time only)

```sh
./init.sh
```

The script:

1. downloads the images (the directory, `ghcr.io/kilombino/joinblaket-directory`, and Tor);
2. starts Tor, which **creates your directory's `.onion` address**;
3. saves that address in the `.env` file;
4. starts the directory.

At the end it shows your address, something like:

```
    abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222
```

Running it again is harmless: it sees everything is set up and only makes sure it is running.

> **Important:** use only this folder and the `ghcr.io/kilombino/joinblaket-directory` image. The original JoinMarket-NG image (`ghcr.io/joinmarket-ng/...`) is for the Spamchain market and would reject every JoinBlaket user.

## Step 4. Check it works

Wait **a couple of minutes** (Tor takes a moment to announce a new address) and run:

```sh
./health.sh
```

It must end with `HEALTHY`. It checks that both containers are running, that the directory answers, how many users are connected, and it also **connects to your own `.onion` going round through the Tor network**, as an outside user would (takes up to a minute). For the quick part only: `./health.sh --quick`.

If the first time says `onion not reachable`, wait five minutes and try again: a brand-new `.onion` takes a little while to become visible.

At first you will see `Connected peers: 0`: normal, nobody knows your directory yet.

## Step 5. Send us the address

Copy the address `init.sh` gave you (also shown by `./health.sh`, or `cat tor/data/hidden_service/hostname`) and send it to us in the community Telegram group, [t.me/bip110_es](https://t.me/bip110_es), or through whatever channel you used to talk to us.

The `.onion` address **is not secret**: it gets published so every client can use it. What is secret are the keys in the next step.

We will test it and, if all is well, add it to JoinBlaket's list of directories in the next release.

## Step 6. Back up the keys (do not skip this!)

**The `.onion` address IS the directory's identity.** It comes from keys Tor stored in `tor/data/hidden_service/`. If you lose them (broken disk, dead SD card, reinstall), your directory gets a new address and every client has to be changed. If someone steals them, they could impersonate your directory.

Make the backup:

```sh
./backup.sh
```

It creates `joinblaket-directory-onion-YYYYMMDD.tar.gz` in your home folder. **Get it off the machine**: a USB stick, your password manager, another computer of yours. Once is enough (the keys never change). Do not share it with anyone, us included.

**To restore it on another machine:** do steps 1 and 2, copy the file into `~/joinblaket/directory-xbt`, then:

```sh
mkdir -p tor/data
tar -xzf joinblaket-directory-onion-YYYYMMDD.tar.gz -C tor/data
chmod 700 tor/data tor/data/hidden_service
./init.sh
```

`init.sh` finds the keys and keeps the same address. **Never** run two machines with the same keys at the same time.

---

## Day to day

Everything runs from `~/joinblaket/directory-xbt`.

| I want to... | Command |
|---|---|
| Check it is fine | `./health.sh` |
| See the directory's log | `docker compose logs -f directory` (Ctrl+C to leave) |
| See Tor's log | `docker compose logs -f tor` |
| See how many people are connected | `docker compose exec directory jm-directory-ctl status` |
| See RAM and CPU use | `docker stats --no-stream` |
| Stop it | `docker compose stop` |
| Start it again | `docker compose up -d` |
| Restart it | `docker compose restart` |

**After a reboot or a power cut** it starts on its own (Docker starts with the machine and the containers have `restart: always`). To confirm: `./health.sh`.

In the directory's log it is normal to see `Handshake timeout` or `Invalid handshake` now and then: connections that never finish introducing themselves (including `./health.sh`'s own test).

### Updating

When we announce a new version:

```sh
cd ~/joinblaket/directory-xbt
git pull
docker compose pull
docker compose up -d
./health.sh
```

The `.onion` address does not change on update. Update soon: if makers speak a protocol version the directory does not understand, they disconnect.

To keep the machine itself patched, now and then: `sudo apt update && sudo apt upgrade`.

### What is stored on the machine

- `tor/data/`: the `.onion` keys and Tor's state. **The only thing that matters.**
- `.env`: the `.onion` address and your user number. Not secret.
- The Docker images and the logs (capped: about 130 MB at most).

The directory writes nothing else to disk: offers and connections live in memory.

### Uninstalling

Tell us first so we take it off the list. Then:

```sh
cd ~/joinblaket/directory-xbt
docker compose down --rmi all
```

The keys stay in `tor/data/` in case you come back. To delete everything: `cd ~ && rm -rf joinblaket` (without a backup, the address is gone for good).

---

## Common problems

- **`permission denied ... docker.sock`**: you did not log out after `usermod` (Step 1). Log out and back in.
- **`DIRECTORY_SERVER__NICK_AUTH_DIRECTORY_ID is not set`**: `./init.sh` was not run. Run it.
- **`exec format error`**: the system is 32-bit. Install 64-bit Raspberry Pi OS.
- **`./health.sh` says `onion not reachable`** for more than 15 minutes: look at `docker compose logs tor`. If you see clock or certificate errors, check the machine's time is right (`timedatectl`). Tor does not work with a wrong clock.
- **Tor says `Permission denied` on `/var/lib/tor`**: the files in `tor/data` do not belong to your user (for example, copied with `sudo`). Fix with `sudo chown -R $USER: tor/data && chmod 700 tor/data tor/data/hidden_service`, then `docker compose up -d`.
- **`Pool overlaps with other one on this address space`**: you already use the `10.52.22.0/24` network. Pick another (say `10.53.22.0/24`) and change it in three places: `subnet` and `ipv4_address` in `docker-compose.yml`, and `HiddenServicePort` in `tor/torrc`.

## Technical details

- `docker-compose.yml`: two containers. `directory` (image built from this repository's `xbt` branch, amd64 and arm64) sits only on an internal network **with no Internet access**; only Tor reaches it, at a fixed address. `tor` uses `osminogin/tor-simple:0.4.9.17` pinned by digest (amd64, arm64, armv7; built with the proof-of-work module). No port is published on the host.
- `tor/torrc`: the same denial-of-service defenses as upstream JoinMarket-NG's directory (introduction rate limit, maximum streams per circuit) plus Tor's **proof of work** turned on.
- The directory runs as an unprivileged user, with a read-only filesystem and no Linux capabilities.
- Same settings as Kilombino's directory: network `mainnet` (on the wire, `xbt`), port 5222, up to 2000 connections (set `MAX_PEERS` in `.env` to change it) and `NICK_AUTH_DIRECTORY_ID` = its own `.onion:5222` (clients use it to verify that nicks belong to whoever claims them).
- The image is built from the release tag with upstream's pinned `directory_server/Dockerfile` (target `production`, `SOURCE_DATE_EPOCH` = the tag's commit time).
