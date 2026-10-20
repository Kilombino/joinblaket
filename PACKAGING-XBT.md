# Empaquetar JoinMarket-NG XBT + Jam (StartOS, Umbrel, Linux)

Estado: probado en regtest el 20 oct 2026 (Jam creó la cartera, recibió,
hizo un CoinJoin como taker contra 3 makers y arrancó/paró un maker). Este
documento recoge lo necesario para empaquetarlo y un borrador del tutorial.

## Piezas y versiones

| Pieza | Origen | Versión |
|---|---|---|
| Backend | este repo, rama `xbt` (sobre `0.41.0` = `66d9eea`) | jmcore, jmwallet, maker, taker, tumbler, orderbook_watcher, jmwalletd |
| Interfaz | `/mnt/datos2/jam-xbt`, rama `xbt` sobre Jam `v2.0.0-beta.4` (`7dbd7010`, la que fija el Flatpak de NG 0.41.0; etiqueta firmada por theborakompanioni, `3550 2225 7551 EAB1 26D7 5616 E807 0AF0 053A AC0D`) | parche: etiqueta "Bitcoin BLAKE2b" en el pie, enlaces de txid a `https://mempool.kilombino.com/tx/…` (solo mainnet), título |
| Node.js para compilar Jam | tarball oficial `node-v26.7.0-linux-x64` (el `.nvmrc` de Jam; SHASUMS256 firmado comprobado) | Jam exige node ≥ 26.3, npm ≥ 11.16 |
| Nodo | Bitcoin Knots BLAKE2b (el backend lo comprueba: `difficulty_blake2b` + `header_version >= 2`) | — |

Jam no pide nada a servicios externos: su CSP es `default-src 'self'`; no hay
precio ni comisiones de terceros. Los únicos enlaces externos son los que
pulsa el usuario (documentación de Jam, GitHub, Telegram/Matrix de Jam y,
ahora, mempool.kilombino.com).

Compilar Jam: `npm ci --ignore-scripts && npm run build` (el lockfile tiene
`integrity` en los 738 paquetes; con `--ignore-scripts` compila igual).
Salida: `dist/` (≈5 MB).

## Procesos (un solo contenedor)

| Proceso | Escucha | Notas |
|---|---|---|
| `tor` | 127.0.0.1:28185 (SOCKS), 127.0.0.1:28186 (control, cookie) | taker y watcher salen por SOCKS al directorio .onion; el maker crea su servicio onion efímero por el puerto de control |
| `jm-orderbook-watcher` (o `python -m orderbook_watcher.main`) | 127.0.0.1:28184 HTTP | Jam muestra el libro de órdenes a través de jmwalletd (`/obwatch/*`); `OBWATCH_URL=http://127.0.0.1:28184` |
| `jmwalletd` | 28183 | API `/api/v1`, websocket `/jmws`, y sirve Jam si existe `/usr/share/jmwalletd/jam/index.html`. Lanza dentro de sí el taker, el maker y el tumbler cuando Jam los pide |

Puertos internos fuera de los habituales (9050/9051/8000) para no chocar con
nada. Todo escucha en 127.0.0.1 dentro del contenedor salvo jmwalletd
(28183, `JMWALLETD_HOST=0.0.0.0` en la imagen): es lo único que se publica
(`-p 127.0.0.1:28183:28183` en Linux; el proxy en StartOS/Umbrel). En Linux
el contenedor va en red puente, **no** `--network host`; llega al nodo por
`host.docker.internal` (el nodo con `rpcbind=172.17.0.1` y
`rpcallowip=172.16.0.0/12`). Nick-auth: `require_verified` por defecto.

Supervisor: el `jmwalletd/rootfs/etc/s6-overlay` de upstream ya define `tor`,
`tor-ready`, `obwatcher` y `jmwalletd-svc`; se reutiliza tal cual.

TLS: jmwalletd genera un certificado propio en `<datos>/ssl/`. Detrás del
proxy de StartOS/Umbrel va `JMWALLETD_NO_TLS=true` (el proxy de la
plataforma pone TLS y, en Umbrel, la contraseña de Umbrel). jmwalletd avisa
en el log de que es HTTP plano: es lo esperado ahí. En Linux suelto se deja
TLS.

## Configuración de jmwalletd

Todo va en `<datos>/config.toml` o en variables `SECCION__CAMPO`:

```toml
[bitcoin]
backend_type = "descriptor_wallet"      # único admitido en XBT
rpc_url = "http://NODO:8332"
rpc_user = "…"
rpc_password = "…"

[network_config]
network = "mainnet"                     # en el cable anuncia "xbt"
directory_servers = ["<onion-del-directorio-xbt>.onion:5222"]

[tor]
socks_host = "127.0.0.1"
socks_port = 28185
control_host = "127.0.0.1"
control_port = 28186
cookie_path = "/data/tor/control_auth_cookie"

[orderbook_watcher]
http_host = "127.0.0.1"
http_port = 28184

[taker]
# ver "Decisiones pendientes"
```

Equivalente por entorno: `BITCOIN__RPC_URL`, `BITCOIN__RPC_USER`,
`BITCOIN__RPC_PASSWORD`, `NETWORK_CONFIG__NETWORK=mainnet`,
`NETWORK_CONFIG__DIRECTORY_SERVERS=<onion>:5222`, `TOR__…`,
`OBWATCH_URL`, `DIRECTORY_NODES` (watcher), `JOINMARKET_DATA_DIR=/data`.

Cuidado (arreglado en el fork, commit `65696c8d`): en upstream
`jmwalletd --data-dir X` NO leía `X/config.toml`, sino
`~/.joinmarket-ng/config.toml`, y arrancaba con los valores por defecto
(RPC `127.0.0.1:8332`). En el paquete usar además `JOINMARKET_DATA_DIR`.

En el nodo: jmwalletd crea una cartera watch-only `jm_<huella>_<red>` en el
nodo (`createwallet` con claves privadas desactivadas). El usuario RPC
necesita permisos de cartera y el nodo no puede tener `disablewallet=1`.

## Qué hay que conservar (volumen `/data`)

| Ruta | Qué es | Pérdida |
|---|---|---|
| `wallets/*.jmdat` | carteras cifradas de Jam | **crítico** (sin la semilla, fondos perdidos) |
| `config.toml` | configuración | se regenera desde el paquete |
| `cmtdata/commitments.json`, `cmtdata/commitmentlist` | compromisos PoDLE usados / lista negra | el taker podría reutilizar compromisos ya quemados (fallos de CoinJoin) |
| `wallet_metadata_*.jsonl`, `history.csv`, `state/` | etiquetas, historial de CoinJoins, reconstrucción | se pierde el historial/etiquetas |
| `ssl/` | certificado de jmwalletd | se regenera |
| `tor/` | estado de Tor (guardas, cookie) | se regenera |
| `jam/` | no: Jam va en la imagen (`/usr/share/jmwalletd/jam`) | — |

Copia de seguridad = `/data` completo excepto `tor/` y `ssl/`. En la
interfaz, recordar la semilla de 12 palabras y la fecha de nacimiento.

## Imagen (una sola, construida por nosotros)

Dockerfile nuevo `docker/jam-xbt/Dockerfile` (multi-etapa):

1. `node:26.7.0-bookworm-slim@sha256:…` → `git clone` de jam-xbt en el commit
   fijado, `npm ci --ignore-scripts`, `npm run build`.
2. Etapa `builder` del `jmwalletd/Dockerfile` de upstream sin cambios
   (requirements con hashes, snapshot de Debian, `SOURCE_DATE_EPOCH`).
3. Imagen final: python slim fijado + `tor` (del snapshot de Debian) +
   s6-overlay (versión y sha256 fijados) + `rootfs/` de upstream +
   `COPY --from=jam /src/dist /usr/share/jmwalletd/jam`.
   `ENV JOINMARKET_DATA_DIR=/data`, `VOLUME /data`, `EXPOSE 28183`,
   usuario no root (uid 1000), `ENTRYPOINT ["/init"]`.
4. torrc: `SocksPort 127.0.0.1:28185`, `ControlPort 127.0.0.1:28186`,
   `CookieAuthentication 1`, `DataDirectory /data/tor`.
5. Publicar en `ghcr.io/kilombino/jam-xbt:<versión>` con digest, firmar
   (cosign o firma GPG del digest) y anotar en las notas el commit de
   ambos repos.
6. amd64 y arm64 (Umbrel en Pi). Jam es estático; el python tiene ruedas
   para ambas.

Comprobaciones antes de publicar: abrir la interfaz, `/api/v1/session`
responde, el log dice `XBT chain check passed` y el watcher ve el directorio.

## StartOS (s9pk, mismo esquema que kilowallet-startos / kilojoin-startos)

- Imagen: la de arriba.
- Dependencia: el paquete del nodo Knots BLAKE2b (como Kilowallet). Del
  `actions`/`dependencies` del nodo se sacan host, puerto, usuario y
  contraseña RPC; se escriben en `/data/config.toml` en cada arranque.
- Interfaz `ui`: puerto 28183, tipo `ui`, con `JMWALLETD_NO_TLS=true`
  (StartOS pone TLS en `.local` y `.onion`).
- Volumen `main` → `/data`. Copia de seguridad: `main` excluyendo `tor/`, `ssl/`.
- Configuración (acciones): directorio(s) XBT (por defecto el nuestro),
  `minimum_makers`, comisiones máximas; nada más.
- Salud: `GET /api/v1/session` (200) y que exista el SOCKS de Tor.
- Tor: el del contenedor (no hace falta el del sistema). El maker publica su
  onion efímera solo mientras "Earn" está activo.
- Publicar en registry.kilombino.com como los demás.

## Umbrel (tienda kilombino-umbrel-store)

- `umbrel-app.yml`: `id: kilombino-jam-xbt`, `dependencies: [bitcoin]`, igual
  que `kilombino-kilowallet` (que el usuario elija Knots BLAKE2b Companion o
  Bitcoin Knots).
- `docker-compose.yml`: `app_proxy` → `APP_HOST: kilombino-jam-xbt_web_1`,
  `APP_PORT: 28183`; servicio `web` con la imagen por digest,
  `user: "1000:1000"`, `stop_grace_period: 1m` (CoinJoin a medias),
  volumen `${APP_DATA_DIR}/data:/data`, y el mismo bucle de
  `NODE_KNOTS_BLAKE2B_* / NODE_BITCOIN_KNOTS_* / NODE_BITCOIN_*` que usa
  Kilowallet para exportar `BITCOIN__RPC_URL/USER/PASSWORD`.
- Entorno fijo: `NETWORK_CONFIG__NETWORK=mainnet`,
  `NETWORK_CONFIG__DIRECTORY_SERVERS=<onion>:5222`, `JMWALLETD_NO_TLS=true`,
  `JMWALLETD_HOST=0.0.0.0`.
- `app_proxy` de Umbrel ya pide la contraseña de Umbrel; Jam pide además la
  de la cartera.

## Imagen construida (20 oct 2026)

`xbt-docker/` (Dockerfile, `entrypoint.sh`, `preflight.py`, `torrc`,
`build.sh`, `docker-compose.yml`). Sin s6: `tini -g` + un `entrypoint.sh`
que arranca Tor, comprueba el nodo (`preflight.py`: BLAKE2b y cadena igual a
la red; si no, no arranca), y lanza el watcher y jmwalletd; si uno muere, el
contenedor sale y Docker lo reinicia. `build.sh` construye desde `git
archive` (solo lo commiteado) de los dos repos; Jam entra como contexto
`jam`. Probado en regtest de punta a punta (CoinJoin desde Jam dentro del
contenedor, todas las entradas 0x21) y el Tor del contenedor llega al
directorio real con nick-auth verificado.

## Decisiones tomadas

1. Directorio XBT: `f3tfzgk5ilwfapx427yx5ckeh7ptuvskfxnfnicfo747jyq5mgzdtvad.onion:5222`
   (servicio `jm-xbt-directory` en start10). Va por defecto en la imagen,
   con `nick_auth_directory_ids` = el mismo `host:port`.
2. Solo makers **con bono de fidelidad**: `bondless_makers_allowance = 0`
   en la imagen (el código XBT descarta toda oferta sin bono y el CoinJoin
   falla con "Not enough makers with a fidelity bond" en vez de rellenar
   con makers sin bono, como hacía upstream) y `bondless_require_zero_fee
   = true` como segunda barrera. Jam avisa en Ganar de que sin bono no te
   eligen. Bonos desde cartera hardware / en frío: fuera de alcance.
3. `minimum_makers` = 4 (por defecto).
4. Jam en español (opcional, según el navegador o el selector) y el enlace
   de fallos a `https://github.com/Kilombino/joinmarket-ng/issues`.
5. Tests de jmwalletd adaptados; la suite pasa.

## Problemas abiertos

1. Jam guarda la configuración leída de jmwalletd en `sessionStorage`
   (`jm-config-store`): tras cambiar `config.toml` hay que bloquear y volver
   a entrar (o cerrar la pestaña) para que la vea.
2. Si el CoinJoin falla, Jam solo dice "Collaborative flow stopped"; el
   motivo está en Logs (pie de página) o en el log de jmwalletd.
3. Historial: el depósito aparece dos veces con importe 0 y el CoinJoin se
   queda en "0 confs" tras minarse (vista del historial de jmwalletd/Jam).
4. Las variables de entorno mandan sobre `/data/config.toml`: red,
   directorio y puertos de Tor se cambian con `-e`, no editando el fichero.
5. Falta: publicar (ghcr, repos, registry.kilombino.com, umbrel-store) y
   firmar el digest. Paquetes preparados: `~/Projects/startos/jam-xbt-startos`
   (s9pk x86_64/aarch64) y `kilombino-jam-xbt` en la tienda Umbrel.

---

Tutoriales: `TUTORIAL-LINUX-ES.md` (capturas en `tutorial-shots/`, regtest,
interfaz en español, palabras de la frase tapadas), `TUTORIAL-STARTOS-ES.md`
y `TUTORIAL-UMBREL-ES.md`.
