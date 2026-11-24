# Montar un directorio de JoinBlaket

*[English version](README.md)*

Esta carpeta sirve para que **cualquier voluntario monte un segundo directorio de JoinBlaket** en su casa, con un ordenador Linux o una Raspberry Pi, en unos 15 minutos y sin abrir puertos en el router.

---

## ¿Qué es un directorio y por qué hace falta otro?

JoinBlaket es la versión de JoinMarket para BTC (la cadena BLAKE2b). Para hacer un CoinJoin, quienes ofrecen liquidez (*makers*) y quienes la usan (*takers*) tienen que encontrarse. El **directorio** es el punto de encuentro: un tablón de anuncios en una dirección `.onion` de Tor donde los makers publican sus ofertas y los takers las leen y les escriben.

Al principio solo había **un** directorio, el de Kilombino: si se caía, nadie encontraba a nadie y no había CoinJoins. Desde la 0.43.0-xbt9 hay **tres** (el de Kilombino y dos de operadores de confianza), y el mercado sigue funcionando aunque uno falle. Cuantos más, mejor: nadie es imprescindible.

## Qué puede y qué NO puede hacer un directorio

**No puede robar nada.** El directorio:

- **no tiene fondos** ni claves de nadie;
- **no firma** nada ni ve semillas;
- **no puede cambiar una transacción**: cada participante comprueba la transacción completa antes de firmarla;
- **no necesita nodo de Bitcoin**, ni cartera, ni conexión a la cadena.

**Lo que sí ve** quien lo lleva (es lo mismo que ve cualquier participante del mercado):

- los *nicks* (apodos aleatorios) conectados y cuándo entran y salen;
- las ofertas públicas de los makers (rangos de importe, comisiones y la prueba de su bono de fidelidad, que incluye la moneda del bono);
- la dirección `.onion` de los makers que la anuncian;
- qué nick escribe a qué nick y cuándo, y la petición inicial de un taker (el importe). El resto de la negociación (monedas, direcciones de destino, firmas) va **cifrada de extremo a extremo** entre taker y maker; muchas veces ni siquiera pasa por el directorio, porque el taker se conecta directamente a la `.onion` del maker.

**No ve direcciones IP**: todo llega por Tor.

**Lo peor que puede hacer un directorio malicioso o caído:** dejar de funcionar, o esconder ofertas o mensajes (censurar). Por eso conviene que haya varios: los clientes se conectan a todos a la vez y con que uno funcione basta.

**Registros:** con la configuración de esta carpeta los registros no guardan nicks ni direcciones `.onion` (están en modo no sensible). El directorio lo guarda todo en memoria y lo olvida al reiniciarse.

## Requisitos

- Un ordenador que pueda estar **encendido siempre**, con Linux de **64 bits**:
  - Debian 12 o 13, Ubuntu 22.04 o posterior, o
  - Raspberry Pi 4 o 5 (o 3B+) con **Raspberry Pi OS de 64 bits** (el de 32 bits no sirve).
- Recursos (medidos en el directorio actual con 11 conexiones): **unos 50 MB de RAM** para el directorio y otros tantos para Tor, CPU casi nula, **~1 GB de disco** para las imágenes y registros, y muy poco tráfico de red.
- Conexión a Internet normal. **No hay que abrir ningún puerto** en el router: Tor sale hacia fuera.
- Saber abrir una terminal y copiar y pegar órdenes.

Sobre **cuánto tiempo encendido**: lo ideal es 24 horas al día. Unos cortes cortos (reinicios, actualizaciones, un apagón) no pasan nada, porque los clientes usan el otro directorio. Si vas a apagarlo durante días o dejarlo del todo, **avísanos** para quitarlo de la lista.

---

## Paso 1. Instalar Docker

Abre una terminal en esa máquina (o entra por SSH) y ejecuta:

```sh
sudo apt update
sudo apt install -y curl git
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

Esto es el instalador oficial de Docker; vale para Debian, Ubuntu y Raspberry Pi OS. (Si prefieres leerlo antes, abre `get-docker.sh` con un editor.)

Ahora deja que tu usuario use Docker sin `sudo`:

```sh
sudo usermod -aG docker $USER
```

**Cierra la sesión y vuelve a entrar** (o reinicia). Comprueba que funciona:

```sh
docker compose version
```

Tiene que salir algo como `Docker Compose version v2...`.

## Paso 2. Descargar JoinBlaket

```sh
cd ~
git clone --depth 1 -b xbt https://github.com/Kilombino/joinblaket.git
cd joinblaket/directory-xbt
```

A partir de aquí todas las órdenes se ejecutan **dentro de la carpeta `~/joinblaket/directory-xbt`**.

## Paso 3. Ponerlo en marcha (solo la primera vez)

```sh
./init.sh
```

El script:

1. descarga las imágenes (el directorio `ghcr.io/kilombino/joinblaket-directory` y Tor);
2. arranca Tor, que **crea la dirección `.onion`** de tu directorio;
3. guarda esa dirección en el archivo `.env`;
4. arranca el directorio.

Al terminar te enseña tu dirección, algo así:

```
    abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222
```

Si lo vuelves a ejecutar no pasa nada: ve que ya está creado y solo se asegura de que esté en marcha.

> **Importante:** usa solo esta carpeta y la imagen `ghcr.io/kilombino/joinblaket-directory`. La imagen original de JoinMarket-NG (`ghcr.io/joinmarket-ng/...`) es para el mercado de la Spamchain y rechazaría a todos los usuarios de JoinBlaket.

## Paso 4. Comprobar que funciona

Espera **un par de minutos** (Tor tarda en anunciar la dirección nueva) y ejecuta:

```sh
./health.sh
```

Tiene que acabar en `HEALTHY`. Comprueba que los dos contenedores están en marcha, que el directorio responde, cuántos usuarios hay conectados, y además **se conecta a tu propia `.onion` dando la vuelta por la red Tor**, igual que haría un usuario de fuera (tarda hasta un minuto). Si solo quieres lo rápido: `./health.sh --quick`.

Si la primera vez sale `onion not reachable`, espera cinco minutos y repite: es normal que una `.onion` recién creada tarde un poco en ser visible.

Al principio verás `Connected peers: 0`: es normal, nadie conoce aún tu directorio.

## Paso 5. Mandarnos la dirección

Copia la dirección que te dio `init.sh` (también la ves con `./health.sh` o con `cat tor/data/hidden_service/hostname`) y mándanosla por el grupo de Telegram de la comunidad, [t.me/bip110_es](https://t.me/bip110_es), o por el canal por el que hayas hablado con nosotros.

La dirección `.onion` **no es secreta**: se publica para que todos los clientes la usen. Lo secreto son las claves del paso siguiente.

Nosotros la probaremos y, si todo va bien, la añadiremos a la lista de directorios de JoinBlaket en la siguiente versión.

## Paso 6. Copia de seguridad de las claves (¡no te lo saltes!)

**La dirección `.onion` ES la identidad del directorio.** Sale de unas claves que Tor guardó en `tor/data/hidden_service/`. Si pierdes esas claves (disco roto, tarjeta SD estropeada, reinstalación) tu directorio tendrá una dirección nueva y habrá que cambiarla en todos los clientes. Si alguien te las roba, podría hacerse pasar por tu directorio.

Haz la copia:

```sh
./backup.sh
```

Crea en tu carpeta personal un archivo `joinblaket-directory-onion-AAAAMMDD.tar.gz`. **Sácalo de la máquina**: a un pendrive, a tu gestor de contraseñas, a otro ordenador tuyo. Basta con hacerlo una vez (las claves no cambian). No lo compartas con nadie, tampoco con nosotros.

**Para recuperarlo en otra máquina:** haz los pasos 1 y 2, copia el archivo a `~/joinblaket/directory-xbt` y luego:

```sh
mkdir -p tor/data
tar -xzf joinblaket-directory-onion-AAAAMMDD.tar.gz -C tor/data
chmod 700 tor/data tor/data/hidden_service
./init.sh
```

`init.sh` encuentra las claves y conserva la misma dirección. No tengas **nunca** dos máquinas a la vez con las mismas claves.

---

## Uso diario

Todo se hace desde `~/joinblaket/directory-xbt`.

| Quiero... | Orden |
|---|---|
| Ver si va bien | `./health.sh` |
| Ver el registro del directorio | `docker compose logs -f directory` (salir con Ctrl+C) |
| Ver el registro de Tor | `docker compose logs -f tor` |
| Ver cuánta gente hay conectada | `docker compose exec directory jm-directory-ctl status` |
| Ver cuánta RAM y CPU usa | `docker stats --no-stream` |
| Pararlo | `docker compose stop` |
| Arrancarlo otra vez | `docker compose up -d` |
| Reiniciarlo | `docker compose restart` |

**Después de un reinicio o un apagón** arranca solo (Docker se inicia con la máquina y los contenedores tienen `restart: always`). Para confirmarlo: `./health.sh`.

En el registro del directorio es normal ver de vez en cuando avisos como `Handshake timeout` o `Invalid handshake`: son conexiones que no terminan de presentarse (entre ellas la prueba de `./health.sh`).

### Actualizar

Cuando avisemos de una versión nueva:

```sh
cd ~/joinblaket/directory-xbt
git pull
docker compose pull
docker compose up -d
./health.sh
```

La dirección `.onion` no cambia al actualizar. Conviene actualizar pronto: si los makers usan una versión del protocolo que el directorio no entiende, se desconectan.

Para que la máquina se mantenga al día de seguridad, de vez en cuando: `sudo apt update && sudo apt upgrade`.

### Qué hay guardado en la máquina

- `tor/data/`: las claves de la `.onion` y el estado de Tor. **Lo único importante.**
- `.env`: la dirección `.onion` y tu número de usuario. No es secreto.
- Las imágenes de Docker y los registros (con tope: unos 130 MB como mucho).

El directorio no escribe nada más en disco: ofertas y conexiones viven en memoria.

### Desinstalar

Avísanos antes para quitarlo de la lista. Luego:

```sh
cd ~/joinblaket/directory-xbt
docker compose down --rmi all
```

Las claves siguen en `tor/data/` por si quieres volver. Para borrarlo todo: `cd ~ && rm -rf joinblaket` (sin copia, la dirección se pierde para siempre).

---

## Problemas frecuentes

- **`permission denied ... docker.sock`**: no cerraste la sesión después de `usermod` (Paso 1). Sal y vuelve a entrar.
- **`DIRECTORY_SERVER__NICK_AUTH_DIRECTORY_ID is not set`**: no se ejecutó `./init.sh`. Ejecútalo.
- **`exec format error`**: el sistema es de 32 bits. Instala Raspberry Pi OS de 64 bits.
- **`./health.sh` dice `onion not reachable`** durante más de 15 minutos: mira `docker compose logs tor`. Si ves errores de hora o certificados, comprueba que la hora de la máquina es correcta (`timedatectl`). Tor no funciona con el reloj desajustado.
- **Tor dice `Permission denied` en `/var/lib/tor`**: los archivos de `tor/data` no son de tu usuario (por ejemplo si los copiaste con `sudo`). Arréglalo con `sudo chown -R $USER: tor/data && chmod 700 tor/data tor/data/hidden_service` y `docker compose up -d`.
- **`Pool overlaps with other one on this address space`**: ya usas la red `10.52.22.0/24`. Elige otra (por ejemplo `10.53.22.0/24`) y cámbiala en tres sitios: `subnet` e `ipv4_address` en `docker-compose.yml`, y `HiddenServicePort` en `tor/torrc`.

## Detalles técnicos

- `docker-compose.yml`: dos contenedores. `directory` (imagen construida desde la rama `xbt` de este repositorio, amd64 y arm64) solo está conectado a una red interna **sin salida a Internet**; únicamente Tor llega a él, en una dirección fija. `tor` usa `osminogin/tor-simple:0.4.9.17` fijada por su huella (amd64, arm64, armv7; incluye el módulo de prueba de trabajo). No se publica ningún puerto en la máquina.
- `tor/torrc`: las mismas defensas contra ataques de denegación de servicio que el directorio original de JoinMarket-NG (límite de presentaciones, máximo de conexiones por circuito) y además la **prueba de trabajo** de Tor activada.
- El directorio corre como usuario sin privilegios, con el sistema de archivos en solo lectura y sin capacidades de Linux.
- Configuración equivalente a la del directorio de Kilombino: red `mainnet` (en el protocolo, `xbt`), puerto 5222, hasta 2000 conexiones (`MAX_PEERS` en `.env` para cambiarlo) y `NICK_AUTH_DIRECTORY_ID` = la propia `.onion:5222` (lo usan los clientes para verificar que los nicks son de quien dicen ser).
- La imagen se construye desde la etiqueta de la versión con el `directory_server/Dockerfile` fijado del proyecto original (objetivo `production`, `SOURCE_DATE_EPOCH` = hora del commit de la etiqueta).
