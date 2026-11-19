# calls-hold: asistente con IA que atiende y arma tickets

Te escriben o te mandan notas de voz cuando no puedes contestar. **Claude** atiende, entiende qué necesita la persona, le pide los datos que faltan y te deja un **ticket completo** para que actúes sin tener que volver a llamar a preguntar.

- **Fase 1 (lista):** Telegram, con texto y notas de voz.
- **Fase 2 (pendiente):** llamadas telefónicas con el mismo cerebro, los mismos tickets y la misma configuración.

## Cómo está armado

| Archivo | Qué hace |
|---|---|
| `cerebro.py` | Claude: conversa y arma el ticket con salida estructurada (JSON validado) |
| `tickets_config.json` | **Tú decides** los tipos de ticket, qué datos pedir en cada uno y quién es quién |
| `datos.py` | Base de datos SQLite: mensajes y tickets |
| `voz.py` | Pasa audio a texto y texto a audio (OpenAI, opcional, porque Claude no procesa audio) |
| `bot_telegram.py` | Conexión con Telegram y comandos para ti |

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
