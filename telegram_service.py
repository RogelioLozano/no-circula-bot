"""
telegram_service.py
-------------------
Envío de mensajes a Telegram usando la Bot API directamente con requests.

Variables de entorno requeridas:
  TELEGRAM_BOT_TOKEN — Token del bot (obtenido desde @BotFather).
  TELEGRAM_CHAT_ID   — ID del chat/grupo/canal destino.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT: int = 15  # segundos


def enviar_mensaje(mensaje: str) -> None:
    """
    Envía un mensaje a Telegram vía Bot API.

    Args:
        mensaje: Texto del mensaje. Acepta formato Markdown.

    Raises:
        KeyError: Si TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no están definidas.
        requests.HTTPError: Si la API de Telegram responde con error HTTP.
        requests.RequestException: Si hay un problema de conexión o timeout.
    """
    variables_requeridas = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    faltantes = [v for v in variables_requeridas if not os.getenv(v)]
    if faltantes:
        raise KeyError(
            f"Variables de entorno faltantes para Telegram: {', '.join(faltantes)}"
        )

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    url = _TELEGRAM_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": mensaje,
        "parse_mode": "Markdown",
    }

    logger.info("Enviando mensaje a Telegram | chat_id: %s", chat_id)

    try:
        response = requests.post(url, json=payload, timeout=TIMEOUT)
    except requests.Timeout:
        logger.error("Timeout (%ss) al conectar con la API de Telegram.", TIMEOUT)
        raise
    except requests.ConnectionError as exc:
        logger.error("Error de conexión con la API de Telegram: %s", exc)
        raise
    except requests.RequestException as exc:
        logger.error("Error inesperado al llamar a la API de Telegram: %s", exc)
        raise

    if not response.ok:
        logger.error(
            "La API de Telegram respondió con error HTTP %s:\n%s",
            response.status_code,
            response.text,
        )
        response.raise_for_status()

    logger.info("Mensaje enviado. message_id: %s", response.json().get("result", {}).get("message_id"))
