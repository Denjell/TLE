# Running TLE under Docker

How the bot starts, stops, restarts and gets rebuilt. Everything here was
verified against a running deployment; where behaviour is surprising, the
reason is given rather than just the command.

`README.md` covers first-time setup (cloning, `.env`, `;help`). This file is
about operating the container afterwards.

---

## The moving parts

| Piece | What it is |
|---|---|
| `Dockerfile` | Two-stage build. Compiles the native cairo/pango stack, then ships a slim Python 3.11 runtime. |
| `docker-compose.yaml` | One service, `tle`. Defines the image, env file, port, volume and restart policy. |
| `tle-bot:latest` | The built image. **Contains a copy of the source code.** |
| `tle-tle-1` | The running container. |
| `./data` | Bind-mounted to `/bot/data`. Holds `db/user.db`, `db/cache.db` and downloaded fonts. |

The source is baked into the image at build time, not mounted. That single
fact explains most of the rebuild behaviour below.

---

## Starting

```bash
docker compose up -d          # build if no image exists, then start detached
docker compose ps             # is it up?
docker compose logs -f        # follow the log; Ctrl-C detaches, does not stop the bot
```

The bot is ready when the log reaches:

```
Cogs loaded: Contests, CacheControl, Handles, …
Slash commands synced
Bot running
```

`Slash commands synced` matters: the bot runs without the privileged
Message Content intent, so slash commands are the primary interface. If that
line is missing, the command tree was rejected and most commands will not
appear in Discord.

To start a container that is merely stopped, without touching the image:

```bash
docker compose start
```

---

## Stopping

There are two kinds of stop, and they differ in whether the bot comes back.

### From inside Discord

```
;meta restart    # shuts down cleanly, exits 42 → Docker starts it again
;meta kill       # shuts down cleanly, exits 0  → stays down
```

Both close the database, cache and OAuth server first, so no writes are lost.
The difference is only the exit code; `restart: on-failure` in
`docker-compose.yaml` is what acts on it. After `;meta kill`, bring the bot
back with `docker compose start`.

### From the shell

```bash
docker compose stop           # stop the container, keep it
docker compose down           # stop and remove the container and network
```

`./data` is a bind mount, so `down` does **not** touch the database. The
image is not removed either — a later `up -d` starts again without
rebuilding.

> **Gotcha:** a container stopped by hand is not brought back by the restart
> policy. Docker marks it as manually stopped, and that flag outranks
> `on-failure`. The same applies to `docker stop` and `docker kill`. Only an
> exit that the bot itself (or a crash) produced triggers a restart. Start it
> again with `docker compose start` or `up -d`.

---

## Restart behaviour

The bot signals intent through its exit code; the policy decides what happens.

| Exit code | Source | Result |
|---|---|---|
| `42` | `;meta restart` | restarted |
| `0` | `;meta kill` | stays down |
| non-zero | crash | restarted, with Docker backing off between attempts |
| — | `docker compose stop` / `down` / `kill` | stays down (manual stop) |

The two codes live in `tle/cogs/meta.py` as `_RESTART_EXIT_CODE` and
`_SHUTDOWN_EXIT_CODE`. They only work in combination with the policy, so
`tests/unit/test_meta_exit_codes.py` checks both halves: that the codes
differ, and that `docker-compose.yaml` still carries a policy that restarts
on failure but not after a clean exit. Changing `restart:` to `no` or
`always` fails that test rather than silently breaking `;meta restart` or
`;meta kill`.

Unlike the `run.sh` loop this replaced, a crash now restarts the bot too. If
you would rather cap that, `restart: "on-failure:5"` gives up after five
consecutive failures.

### Host and daemon restarts

Verified behaviour, not extrapolation:

* `systemctl restart docker` — the container is stopped with the daemon and
  started again afterwards.
* `wsl --shutdown` followed by starting the distro — the Docker service comes
  up (it is `systemctl enable`d) and the container with it.

The caveat is WSL itself: Windows does not boot a WSL distro on login. After
a Windows restart nothing runs until something starts the distro — opening a
WSL terminal is enough.

---

## Rebuilding after a code change

**`docker compose up -d` alone will not pick up code changes.** The source
lives inside the image, and compose only builds when no image exists. With an
image already present it reports `Container tle-tle-1 Running` and leaves the
old code in place. This is the single easiest mistake to make here.

```bash
docker compose up -d --build      # rebuild the image, recreate the container
```

For a dependency change in `pyproject.toml`, or to pick up security updates
in the base image:

```bash
docker compose build --pull       # also re-pull python:3.11-slim
docker compose up -d
```

Layer caching keeps the common case fast: only the `COPY . .` layer and
everything after it is redone when just the bot source changed. A dependency
change invalidates the builder stage and takes considerably longer.

There is a short gap while the container is recreated — the bot goes offline
and comes back. It is not a zero-downtime operation.

To rebuild from scratch, ignoring the cache:

```bash
docker compose build --no-cache
docker compose up -d
```

---

## Inspecting a running bot

```bash
docker compose logs -f --tail=100          # follow
docker compose logs | grep -i error        # search
docker compose exec tle bash               # shell inside the container
docker compose exec tle python -c "..."    # run something against the live env
docker stats --no-stream tle-tle-1         # CPU and memory
docker inspect -f '{{.RestartCount}}' tle-tle-1
```

Logs are capped by compose at 3 files of 10 MB, so they cannot fill the disk.

---

## Data

Everything that must survive lives in `./data`, mounted into the container.
It is not part of the image and is untouched by builds, `down`, or removing
the image.

The container runs as `botuser` with uid 1000. On a host whose user is also
uid 1000 (the default for the first account on Ubuntu, including under WSL)
the permissions line up. If the bot cannot write the database, compare
`id -u` on the host with `docker run --rm tle-bot:latest id botuser`.

Back the database up before an upgrade that changes the schema:

```bash
cp data/db/user.db data/db/user.db.backup
```

---

## Quick reference

| Goal | Command |
|---|---|
| Start | `docker compose up -d` |
| Start a stopped container | `docker compose start` |
| Stop, keep the container | `docker compose stop` |
| Stop and remove | `docker compose down` |
| Restart, no rebuild | `docker compose restart` |
| **Apply code changes** | `docker compose up -d --build` |
| Apply dependency changes | `docker compose build --pull && docker compose up -d` |
| Follow logs | `docker compose logs -f` |
| Shell inside | `docker compose exec tle bash` |
| Restart from Discord | `;meta restart` |
| Shut down from Discord | `;meta kill` |
