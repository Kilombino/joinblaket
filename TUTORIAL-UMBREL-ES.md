# Jam XBT en Umbrel

Jam es una cartera con interfaz web para hacer CoinJoin en Bitcoin BLAKE2b
(XBT). En Umbrel no hay nada que configurar. Necesitas umbrelOS 1.3 o
posterior.

## Instalar

1. Añade las tiendas: **App Store → ⋯ → Community App Stores** y pega
   `https://github.com/paulscode/umbrel-store` (el nodo) y
   `https://github.com/Kilombino/umbrel-store` (Jam).
2. Instala **Knots (BLAKE2b) Companion** (o Bitcoin Knots de la tienda Misc
   en la cadena BLAKE2b) y espera a que esté sincronizado.
3. Instala **JoinMarket XBT (Jam)** de la tienda Kilombino. Jam busca solo
   el nodo BLAKE2b y se niega a arrancar con uno de la cadena SHA-256.

## Usar

1. Abre Jam desde el escritorio de Umbrel.
2. **Crear cartera nueva** → nombre y contraseña → apunta en papel las
   **12 palabras**.
3. **Recibir**, espera 5 confirmaciones, y **Enviar** con "transacción
   colaborativa" para mezclar. Solo se mezcla con quien tiene bono de
   fidelidad.
4. Para **Ganar** (ofrecer tus monedas a otros) necesitas un **bono de
   fidelidad**: **Ganar → Crear bono de fidelidad**. Bloquea las monedas
   que metas **hasta la fecha que elijas** (no se pueden mover antes). Sin
   bono nadie acepta tu oferta. Bonos desde cartera hardware: por ahora no.

Tus carteras están en los datos de la app. Lo importante son las 12
palabras en papel.

Más detalles (botes, mezclar, ganar): `TUTORIAL-LINUX-ES.md`, desde el
paso 5 (bonos: paso 8).
