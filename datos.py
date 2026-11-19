"""Base de datos (SQLite): mensajes y tickets. La usan el bot de Telegram y, luego, las llamadas."""

import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "asistente.db"))
HISTORIAL_MAX = 16
ZONA = ZoneInfo(os.getenv("ZONA_HORARIA", "America/Bogota"))


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def ahora() -> str:
    return datetime.now(ZONA).strftime("%Y-%m-%d %H:%M")


def crear_tablas() -> None:
    with _db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS mensajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT NOT NULL,        -- 'telegram' o 'llamada'
                conversacion TEXT NOT NULL, -- chat_id de Telegram o número de teléfono
                quien TEXT,
                rol TEXT NOT NULL,          -- 'persona' o 'asistente'
                tipo TEXT NOT NULL,         -- 'texto' o 'voz'
                contenido TEXT NOT NULL,
                fecha TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canal TEXT NOT NULL,
                conversacion TEXT NOT NULL,
                quien TEXT,
                tipo TEXT,
                nombre TEXT,
                contacto TEXT,
                asunto TEXT,
                descripcion TEXT,
                prioridad TEXT,
                campos TEXT,                -- JSON [{dato, valor}]
                estado TEXT NOT NULL DEFAULT 'abierto',
                avance TEXT,
                fecha TEXT NOT NULL,
                actualizado TEXT
            );
            """
        )


def guardar_mensaje(canal, conversacion, quien, rol, tipo, contenido) -> None:
    with _db() as con:
        con.execute(
            "INSERT INTO mensajes (canal, conversacion, quien, rol, tipo, contenido, fecha) "
            "VALUES (?,?,?,?,?,?,?)",
            (canal, str(conversacion), quien, rol, tipo, contenido, ahora()),
        )


def historial(canal, conversacion) -> list[dict]:
    with _db() as con:
        filas = con.execute(
            "SELECT rol, contenido FROM mensajes WHERE canal=? AND conversacion=? ORDER BY id DESC LIMIT ?",
            (canal, str(conversacion), HISTORIAL_MAX),
        ).fetchall()
    return [
        {"role": "user" if f["rol"] == "persona" else "assistant", "content": f["contenido"]}
        for f in reversed(filas)
    ]


def crear_ticket(canal, conversacion, quien, t: dict, config: dict) -> int:
    tipo = t.get("tipo") or "general"
    prioridad = t.get("prioridad") or config.get("tipos", {}).get(tipo, {}).get("prioridad", "normal")
    with _db() as con:
        cur = con.execute(
            """INSERT INTO tickets (canal, conversacion, quien, tipo, nombre, contacto, asunto,
                                    descripcion, prioridad, campos, fecha)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                canal,
                str(conversacion),
                quien,
                tipo,
                t.get("nombre", ""),
                t.get("contacto", ""),
                t.get("asunto") or "Sin asunto",
                t.get("descripcion", ""),
                prioridad,
                json.dumps(t.get("campos") or [], ensure_ascii=False),
                ahora(),
            ),
        )
        return cur.lastrowid


def tickets_de(canal, conversacion, limite=5) -> list[dict]:
    with _db() as con:
        filas = con.execute(
            "SELECT id, estado, asunto, avance FROM tickets WHERE canal=? AND conversacion=? "
            "ORDER BY id DESC LIMIT ?",
            (canal, str(conversacion), limite),
        ).fetchall()
    return [dict(f) for f in filas]


def tickets_abiertos(limite=30) -> list[dict]:
    with _db() as con:
        filas = con.execute(
            "SELECT * FROM tickets WHERE estado != 'cerrado' ORDER BY "
            "CASE prioridad WHEN 'critica' THEN 0 WHEN 'alta' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, id DESC "
            "LIMIT ?",
            (limite,),
        ).fetchall()
    return [dict(f) for f in filas]


def ticket(numero: int) -> dict | None:
    with _db() as con:
        f = con.execute("SELECT * FROM tickets WHERE id=?", (numero,)).fetchone()
    return dict(f) if f else None


def actualizar_ticket(numero: int, estado: str | None = None, avance: str | None = None) -> bool:
    cambios, valores = ["actualizado=?"], [ahora()]
    if estado:
        cambios.append("estado=?")
        valores.append(estado)
    if avance:
        cambios.append("avance=?")
        valores.append(avance)
    with _db() as con:
        cur = con.execute(f"UPDATE tickets SET {', '.join(cambios)} WHERE id=?", (*valores, numero))
    return cur.rowcount > 0


def ultimos_mensajes(canal, conversacion, limite=10) -> list[dict]:
    with _db() as con:
        filas = con.execute(
            "SELECT rol, contenido FROM mensajes WHERE canal=? AND conversacion=? ORDER BY id DESC LIMIT ?",
            (canal, str(conversacion), limite),
        ).fetchall()
    return [dict(f) for f in reversed(filas)]
