# MNQ Signal Bot — Instrucciones de instalación

Bot que vigila NQ=F (proxy de MNQ) cada 15 minutos y avisa por Telegram cuando
aparece la señal de entrada (RSI(13) ≤ 20, distancia a banda inferior de
Bollinger ≤ 40%, posición vs EMA(20) ≤ 0%), y luego avisa cuando esa operación
se cierra por Take-Profit (2%), Stop-Loss (0.7%), o tiempo máximo (8 velas de 1h).

Esta es la configuración que validamos en el backtester con 2 años de
historia: profit factor 2.00, drawdown máximo histórico $2524 (1 contrato).
Recuerda que esto es información generada automáticamente y el pasado no
garantiza resultados futuros — no es asesoría financiera.

## Paso 1 — Crear el repositorio

1. Ve a github.com → “New repository”.
1. Nómbralo como quieras, por ejemplo `mnq-signal-bot`.
1. Puede ser público o privado, no importa para que funcione.

## Paso 2 — Subir los 3 archivos

Sube estos archivos a la raíz del repo nuevo:

- `signal_bot.py`
- `state.json`
- `.github/workflows/mnq-signal-bot.yml` (el archivo `mnq-signal-bot.yml` que
  te entregué debe ir DENTRO de una carpeta `.github/workflows/` — créala al
  subir el archivo, GitHub te deja escribir esa ruta en el nombre al subir
  desde la web).

Puedes hacerlo directamente desde la web de GitHub con “Add file” → “Upload
files”, sin necesidad de terminal.

## Paso 3 — Configurar los secrets de Telegram

1. En el repo nuevo, ve a Settings → Secrets and variables → Actions.
1. Click “New repository secret”.
1. Crea uno llamado `TELEGRAM_BOT_TOKEN` con el token de tu bot (el mismo que
   ya usas en BOT4H).
1. Crea otro llamado `TELEGRAM_CHAT_ID` con tu chat ID (el mismo de siempre).

Si tu workflow actual de BOT4H usa nombres distintos a estos dos, dímelo y
ajusto el código para que coincidan exactamente.

## Paso 4 — Activar y probar

1. Ve a la pestaña “Actions” del repo nuevo.
1. Si GitHub te pregunta si quieres habilitar Actions, acepta.
1. Busca el workflow “MNQ Signal Bot” en la lista de la izquierda.
1. Click “Run workflow” (botón manual) para probarlo de inmediato sin esperar
   los 15 minutos.
1. Revisa los logs de la ejecución — debe decir “Revisión completada.” al
   final si todo salió bien. Si hay señal activa en ese momento, deberías
   recibir el mensaje en Telegram en segundos.

A partir de ahí corre solo cada 15 minutos, sin que tengas que hacer nada.

## Cómo funciona el seguimiento

El bot guarda en `state.json` si hay una operación “abierta” actualmente.
Cada vez que corre:

- Si ya hay una operación abierta, revisa las velas nuevas desde la entrada
  para ver si tocó TP, SL, o se acabaron las 8 velas. Si se cerró, te avisa
  y borra la operación abierta del estado.
- Si NO hay operación abierta, busca si la última vela cerrada cumple la
  señal de entrada. Si sí, te avisa y guarda la operación como abierta.

Mientras haya una operación abierta, el bot no busca nuevas señales — espera
a que esa se cierre primero (igual que harías tú operando manualmente con un
solo contrato a la vez).

## Limitaciones a tener en cuenta

- Yahoo Finance puede bloquear la descarga ocasionalmente. El script intenta
  varios proxies de respaldo automáticamente, pero si todos fallan, esa
  corrida en particular no hace nada y lo vuelve a intentar en 15 minutos.
- Esto es una señal informativa, no ejecuta órdenes reales en ningún broker.
  La entrada/salida que reporta es teórica, basada en el precio de cierre de
  la vela o en el nivel de TP/SL tocado según los datos de Yahoo Finance, que
  pueden diferir levemente del precio real de tu broker.