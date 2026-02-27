"""
whatsapp_service.py
-------------------
Integración con la API de Twilio para envío de mensajes por WhatsApp.

Requisitos en Twilio:
- Tener activo el Sandbox de WhatsApp o un número aprobado.
- El destinatario debe haber enviado el mensaje de activación al Sandbox.

Variables de entorno requeridas:
  TWILIO_SID    — Account SID de tu cuenta Twilio.
  TWILIO_TOKEN  — Auth Token de tu cuenta Twilio.
  TWILIO_FROM   — Número de WhatsApp remitente (ej. +14155238886).
  TWILIO_TO     — Número de WhatsApp destinatario (ej. +521234567890).
"""

import logging
import os

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

logger = logging.getLogger(__name__)


def _get_client() -> Client:
    """Construye el cliente de Twilio leyendo TWILIO_SID y TWILIO_TOKEN."""
    sid = os.environ["TWILIO_SID"]
    token = os.environ["TWILIO_TOKEN"]
    return Client(sid, token)


def _formatear_numero(numero: str) -> str:
    """
    Garantiza el prefijo 'whatsapp:' requerido por la API de Twilio.
    Acepta '+521234567890' o 'whatsapp:+521234567890'.
    """
    numero = numero.strip()
    if not numero.startswith("whatsapp:"):
        return f"whatsapp:{numero}"
    return numero


def enviar_mensaje(mensaje: str) -> str:
    """
    Envía un mensaje de WhatsApp usando Twilio.

    Lee las variables de entorno TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM
    y TWILIO_TO para autenticar y determinar los números de origen/destino.

    Args:
        mensaje: Texto del mensaje a enviar.

    Returns:
        SID del mensaje enviado por Twilio.

    Raises:
        KeyError: Si alguna variable de entorno requerida no está definida.
        TwilioRestException: Si la API de Twilio devuelve un error.
    """
    # Leer y validar variables de entorno
    variables_requeridas = ["TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_TO"]
    faltantes = [v for v in variables_requeridas if not os.getenv(v)]
    if faltantes:
        raise KeyError(
            f"Variables de entorno faltantes para Twilio: {', '.join(faltantes)}"
        )

    from_number = _formatear_numero(os.environ["TWILIO_FROM"])
    to_number = _formatear_numero(os.environ["TWILIO_TO"])

    logger.info("Enviando mensaje WhatsApp | de: %s → a: %s", from_number, to_number)

    client = _get_client()

    try:
        message = client.messages.create(
            body=mensaje,
            from_=from_number,
            to=to_number,
        )
        logger.info("Mensaje enviado. SID: %s", message.sid)
        return message.sid or ""

    except TwilioRestException as exc:
        logger.error(
            "Error de Twilio al enviar WhatsApp: código=%s mensaje=%s",
            exc.code,
            exc.msg,
        )
        raise


def construir_mensaje(
    puede_circular: bool,
    razon: str,
    nivel_contingencia: str,
    placa_digito: int,
    holograma: str,
    fecha_str: str,
) -> str:
    """
    Construye el cuerpo del mensaje de WhatsApp con formato legible.

    Args:
        puede_circular: True si el vehículo puede circular.
        razon: Explicación detallada de la regla aplicada.
        nivel_contingencia: Nivel de contingencia detectado.
        placa_digito: Último dígito de la placa.
        holograma: Tipo de holograma del vehículo.
        fecha_str: Fecha en formato legible (ej. "miércoles 26 de febrero de 2026").

    Returns:
        Texto formateado del mensaje.
    """
    estado_emoji = "✅" if puede_circular else "🚫"
    estado_texto = "PUEDE CIRCULAR" if puede_circular else "NO CIRCULA"

    contingencia_info = (
        "⚠️ *Contingencia activa*: "
        + nivel_contingencia.replace("_", " ").upper()
        if nivel_contingencia != "ninguna"
        else "✅ Sin contingencia ambiental activa"
    )

    mensaje = (
        f"🚗 *No Circula Bot* — {fecha_str}\n"
        f"{'─' * 30}\n"
        f"Placa termina en: *{placa_digito}*\n"
        f"Holograma: *{holograma}*\n\n"
        f"{contingencia_info}\n\n"
        f"{estado_emoji} *{estado_texto}*\n\n"
        f"📋 {razon}"
    )
    return mensaje
