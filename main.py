"""
main.py
-------
Orquestador del bot de No Circula.

Flujo:
  1. Carga variables desde .env
  2. Obtiene la fecha actual
  3. Consulta contingencia ambiental (CAMe)
  4. Evalúa circulación según Hoy No Circula
  5. Construye mensaje formateado
  6. Envía mensaje por Telegram vía Bot API

Uso directo:
  python main.py

Cron (todos los días a las 6:00 AM, lunes–sábado):
  0 6 * * 1-6 /usr/bin/python3 /ruta/al/proyecto/main.py >> /var/log/no-circula-bot.log 2>&1
"""

import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

from contingencia_service import fase_a_nivel, verificar_contingencia
from reglas_service import evaluar_circulacion
from telegram_service import enviar_mensaje


def configurar_logging(nivel: str) -> None:
    """Configura el sistema de logging con el nivel especificado."""
    numeric_level = getattr(logging, nivel.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def cargar_configuracion() -> dict:
    """
    Carga y valida las variables de entorno requeridas.

    Returns:
        Diccionario con la configuración del bot.

    Raises:
        SystemExit: Si alguna variable obligatoria no está definida o es inválida.
    """
    load_dotenv()

    errores: list[str] = []

    variables_requeridas = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "PLACA_ULTIMO_DIGITO",
        "HOLOGRAMA",
    ]

    for var in variables_requeridas:
        if not os.getenv(var):
            errores.append(f"  • {var} no está definida en .env")

    digito_raw = os.getenv("PLACA_ULTIMO_DIGITO", "")
    ultimo_digito = None
    if digito_raw:
        try:
            ultimo_digito = int(digito_raw)
            if ultimo_digito not in range(10):
                errores.append("  • PLACA_ULTIMO_DIGITO debe ser un número entre 0 y 9")
        except ValueError:
            errores.append(f"  • PLACA_ULTIMO_DIGITO debe ser numérico, se recibió: '{digito_raw}'")

    holograma = os.getenv("HOLOGRAMA", "")
    if holograma and holograma not in ("00", "0", "1", "2"):
        errores.append(f"  • HOLOGRAMA debe ser 00, 0, 1 o 2. Se recibió: '{holograma}'")

    if errores:
        print("❌ Errores de configuración en .env:\n" + "\n".join(errores))
        print("\nCopia .env.example a .env y completa los valores requeridos.")
        sys.exit(1)

    notificar_solo_no_circula = os.getenv("NOTIFICAR_SOLO_SI_NO_CIRCULA", "true").lower() == "true"

    return {
        "ultimo_digito": ultimo_digito,
        "holograma": holograma,
        "notificar_solo_no_circula": notificar_solo_no_circula,
    }


def formatear_fecha(fecha: date) -> str:
    """Retorna la fecha en formato legible en español."""
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
    }
    dias = {
        1: "lunes", 2: "martes", 3: "miércoles", 4: "jueves",
        5: "viernes", 6: "sábado", 7: "domingo",
    }
    nombre_dia = dias[fecha.isoweekday()]
    nombre_mes = meses[fecha.month]
    return f"{nombre_dia} {fecha.day} de {nombre_mes} de {fecha.year}"


def _construir_mensaje(
    fecha_str: str,
    hay_contingencia: bool,
    fase: str | None,
    puede_circular: bool,
    razon: str,
) -> str:
    """
    Construye el mensaje final en el formato estándar del bot:

        📅 Fecha
        🌫 Contingencia: Sí (Fase X) / No
        🚗 Tu auto: Circula / No circula
        📌 Motivo: ...
    """
    contingencia_texto = (
        f"Sí ({fase})" if hay_contingencia and fase else "No"
    )
    circulacion_texto = "✅ Circula" if puede_circular else "🚫 No circula"

    return (
        f"📅 *{fecha_str}*\n"
        f"🌫 *Contingencia:* {contingencia_texto}\n"
        f"🚗 *Tu auto:* {circulacion_texto}\n"
        f"📌 *Motivo:* {razon}"
    )


def main() -> None:
    """Punto de entrada principal del bot."""
    # ── 1. Cargar variables desde .env ──────────────────────────────────────
    config = cargar_configuracion()
    configurar_logging(os.getenv("LOG_LEVEL", "INFO"))

    logger = logging.getLogger(__name__)

    # ── 2. Obtener fecha actual ──────────────────────────────────────────────
    hoy = date.today()
    fecha_str = formatear_fecha(hoy)

    logger.info("=== no-circula-bot iniciado (%s) ===", fecha_str)

    # Hoy No Circula no aplica los domingos
    if hoy.isoweekday() == 7:
        logger.info("Hoy es domingo — Hoy No Circula no aplica. Sin notificación.")
        return

    # ── 3. Consultar contingencia ────────────────────────────────────────────
    logger.info("Consultando contingencia ambiental en portal CAMe...")
    resultado_contingencia = verificar_contingencia()
    logger.info(
        "Contingencia: hay=%s | fase=%s | detalle: %s",
        resultado_contingencia["hay_contingencia"],
        resultado_contingencia["fase"],
        resultado_contingencia["detalle"],
    )

    # ── 4. Evaluar circulación ───────────────────────────────────────────────
    logger.info(
        "Evaluando circulación: placa ...%s, holograma %s",
        config["ultimo_digito"],
        config["holograma"],
    )
    resultado_circulacion = evaluar_circulacion(
        ultimo_digito=config["ultimo_digito"],
        holograma=config["holograma"],
        nivel_contingencia=fase_a_nivel(resultado_contingencia["fase"]),
        fecha=hoy,
    )
    logger.info("Resultado: %s", resultado_circulacion.razon)

    # Respetar preferencia de notificar solo cuando no circula
    debe_notificar = (
        not resultado_circulacion.puede_circular
        if config["notificar_solo_no_circula"]
        else True
    )
    if not debe_notificar:
        logger.info(
            "El vehículo puede circular hoy y NOTIFICAR_SOLO_SI_NO_CIRCULA=true. "
            "No se envía mensaje."
        )
        return

    # ── 5. Construir mensaje ─────────────────────────────────────────────────
    mensaje = _construir_mensaje(
        fecha_str=fecha_str,
        hay_contingencia=resultado_contingencia["hay_contingencia"],
        fase=resultado_contingencia["fase"],
        puede_circular=resultado_circulacion.puede_circular,
        razon=resultado_circulacion.razon,
    )
    logger.debug("Mensaje a enviar:\n%s", mensaje)

    # ── 6. Enviar por Telegram ───────────────────────────────────────────────
    logger.info("Enviando notificación por Telegram...")
    enviar_mensaje(mensaje)
    logger.info("✅ Notificación enviada.")
    logger.info("=== no-circula-bot finalizado ===")


if __name__ == "__main__":
    main()
