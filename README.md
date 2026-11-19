# calls-hold: asistente con IA que atiende y arma tickets

Te escriben o te mandan notas de voz cuando no puedes contestar. **Claude** atiende, entiende qué necesita la persona, le pide los datos que faltan y te deja un **ticket completo** para que actúes sin tener que volver a llamar a preguntar.

- **Fase 1 (lista):** Telegram, con texto y notas de voz.
- **Fase 2 (lista):** llamadas telefónicas. Contesta con voz en tiempo real y, al colgar, Claude arma el ticket.

## Cómo está armado

| Archivo | Qué hace |
|---|---|
| `cerebro.py` | Claude: conversa y arma el ticket con salida estructurada (JSON validado) |
| `tickets_config.json` | **Tú decides** los tipos de ticket, qué datos pedir en cada uno y quién es quién |
| `datos.py` | Base de datos SQLite: mensajes y tickets |
| `voz.py` | Pasa audio a texto y texto a audio (OpenAI, opcional, porque Claude no procesa audio) |
| `bot_telegram.py` | Conexión con Telegram y comandos para ti |
| `llamadas.py` | Contestadora telefónica: Twilio ↔ voz en tiempo real de OpenAI, ticket al colgar |
| `servidor.py` | Corre Telegram + llamadas juntos (el que se usa en Railway) |

## Puesta en marcha

1. **Bot de Telegram:** escríbele a **@BotFather**, usa `/newbot` y copia el token.
2. **Clave de Claude:** créala en https://console.anthropic.com/settings/keys y carga saldo.
3. *(Opcional, para notas de voz)* Crea una clave de OpenAI en https://platform.openai.com/api-keys.
4. Instala y configura:
   ```bash
   python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env                                 # Windows: copy .env.example .env
   ```
   Llena `.env` con tus claves y tu contexto de trabajo.
5. Arranca el bot: `python bot_telegram.py`
6. Escríbele `/miid` a tu bot, pon ese número en `OWNER_CHAT_ID` y reinícialo. Desde ese momento los tickets te llegan a ti.

## Tus comandos (solo funcionan desde tu chat)

| Comando | Qué hace |
|---|---|
| `/tickets` | Tickets abiertos, los más urgentes primero (🔴 crítica, 🟠 alta, 🟡 normal, ⚪ baja) |
| `/ticket 12` | Detalle del ticket y la conversación completa |
| `/avance 12 Ya lo estoy revisando` | Guarda el avance y **se lo envía a quien lo pidió** |
| `/cerrar 12 Quedó desplegado` | Cierra el ticket y le avisa a la persona |
| `/disponible` | Contestas tú: el bot no responde en tu Telegram personal ni en grupos |
| `/ocupado` | El bot atiende y te deja los tickets |
| `/estado` | Muestra si estás disponible u ocupado |

Además, si **tú** escribes en un chat, el bot se calla en ese chat durante 30 minutos (`PAUSA_MINUTOS` en `.env`).

Si tu jefe vuelve a escribir preguntando "¿cómo va lo del login?", Claude le contesta con el último avance que registraste.

## Configurar los tickets (`tickets_config.json`)

Vienen listos estos tipos para un desarrollador:
- **incidente:** sistema, ambiente, qué pasa, desde cuándo, a quién afecta, error y pasos para reproducirlo
- **requerimiento:** qué se necesita, para qué sirve, fecha límite, quién lo aprueba y referencias
- **consulta**, **reunion**, **acceso** (despliegues y permisos; nunca pide contraseñas) y **otro**

Cada tipo tiene:
- `descripcion`: para que Claude lo reconozca
- `campos`: los datos que debe conseguir
- `prioridad`: la prioridad por defecto
- `instrucciones`: cómo atender ese caso (opcional)
- `avisar`: si te notifica o no

En `contactos` puedes fijar personas conocidas:
```json
"contactos": {
  "carlos_jefe": "requerimiento",
  "+573001234567": "incidente"
}
```
Puedes agregar, quitar o renombrar tipos cuando quieras. El archivo se vuelve a leer con cada mensaje, así que no hay que reiniciar el bot.

## Costos aproximados

- **Claude** (`claude-opus-5-5`, esfuerzo `low`): unos pocos centavos de dólar por conversación. Si quieres gastar menos, cambia `MODELO_CLAUDE` a `claude-sonnet-5-5` o `claude-haiku-5-5`.
- **Voz** (OpenAI): alrededor de USD 0,01 por audio.

## Privacidad
El archivo `.env` (tus claves) y la base de datos `*.db` (las conversaciones) están en `.gitignore` y **nunca se suben** al repositorio.


## Desplegar en Railway (24/7)

1. Crea una cuenta en https://railway.com e inicia sesión con GitHub.
2. **New Project → Deploy from GitHub repo →** `calls-hold-`.
3. En el servicio, pestaña **Variables**, agrega las mismas del `.env`:
   `TELEGRAM_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OWNER_CHAT_ID`, `NOMBRE_DUENO`, `CONTEXTO`,
   `TWILIO_AUTH_TOKEN` y `DB_PATH=/data/asistente.db`.
4. Clic derecho en el servicio → **Attach Volume** → ruta de montaje `/data` (para que los tickets no se borren).
5. **Settings → Networking → Generate Domain**. Te da una dirección como `calls-hold-production.up.railway.app`.
6. Abre esa dirección en el navegador: debe decir `calls-hold OK`.

Solo debe haber **una** copia del bot encendida: si corre en Railway, apaga cualquier otra.

## Llamadas con Twilio

1. Crea una cuenta en https://www.twilio.com y compra un número con capacidad de **Voice**.
2. En el número: **Voice Configuration → A call comes in → Webhook** →
   `https://TU-DOMINIO.up.railway.app/llamada` (método **HTTP POST**).
3. Copia el **Auth Token** (Console → Account Info) a la variable `TWILIO_AUTH_TOKEN` de Railway.
4. En tu celular, desvía las llamadas no contestadas a ese número (códigos GSM, funcionan en la mayoría de operadores):
   - Si no contestas: `**61*NUMERO#`
   - Si estás ocupado: `**67*NUMERO#`
   - Si no hay señal: `**62*NUMERO#`
   - Quitar todos los desvíos: `##002#`
   Consulta con tu operador si el desvío tiene costo, sobre todo hacia números de otro país.
