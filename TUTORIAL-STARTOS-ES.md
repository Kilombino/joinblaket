# Jam XBT en StartOS

Jam es una cartera con interfaz web para hacer CoinJoin en Bitcoin BLAKE2b
(XBT). En StartOS no hay nada que configurar.

## Instalar

1. Instala un nodo BLAKE2b: **Bitcoin Knots (BLAKE2b) Companion** o
   **Bitcoin** en la cadena BLAKE2b. Espera a que esté sincronizado.
2. **Marketplace → ⋯ → Añadir registro** y pega
   `https://registry.kilombino.com`.
3. Busca **JoinMarket XBT (Jam)** e instálalo. Arráncalo.

Si tienes los dos nodos, la acción **Elegir nodo BLAKE2b** escoge uno. Jam
comprueba que el nodo sigue la cadena BLAKE2b; si no, no arranca.

## Usar

1. Abre la **interfaz web** de Jam (por `.local` o `.onion`).
2. **Crear cartera nueva** → nombre y contraseña → apunta en papel las
   **12 palabras**.
3. **Recibir**, espera 5 confirmaciones, y **Enviar** con "transacción
   colaborativa" para mezclar. Solo se mezcla con quien tiene bono de
   fidelidad.
4. Para **Ganar** (ofrecer tus monedas a otros) necesitas un **bono de
   fidelidad**: **Ganar → Crear bono de fidelidad**. Bloquea las monedas
   que metas **hasta la fecha que elijas** (no se pueden mover antes). Sin
   bono nadie acepta tu oferta. Bonos desde cartera hardware: por ahora no.

Las copias de seguridad de StartOS guardan tus carteras. Lo importante
siguen siendo las 12 palabras en papel.

Más detalles (botes, mezclar, ganar): `TUTORIAL-LINUX-ES.md`, desde el
paso 5 (bonos: paso 8).
