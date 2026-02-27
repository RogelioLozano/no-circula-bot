"""
reglas_service.py
-----------------
Lógica de circulación según el programa "Hoy No Circula" de la CDMX
y las restricciones adicionales por contingencia ambiental (Fase 1 / Fase 2).

Fuente oficial de las reglas:
https://www.sedema.cdmx.gob.mx/programas/programa/hoy-no-circula
"""

import logging
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional

from contingencia_service import NivelContingencia

logger = logging.getLogger(__name__)


class Holograma(str, Enum):
    CERO_CERO = "00"   # Exento
    CERO = "0"          # Exento
    UNO = "1"           # Restricción un día a la semana
    DOS = "2"           # Restricción un día a la semana + sábados rotativos


# ---------------------------------------------------------------------------
# Tablas de reglas — Hoy No Circula normal (lunes a viernes, 5:00–22:00 h)
# Clave: día ISO (1=lunes … 7=domingo), valor: dígitos finales restringidos
# ---------------------------------------------------------------------------
RESTRICCION_SEMANAL: dict[int, list[int]] = {
    1: [5, 6],   # Lunes
    2: [7, 8],   # Martes
    3: [3, 4],   # Miércoles
    4: [1, 2],   # Jueves
    5: [9, 0],   # Viernes
}

# Sábados: los hologramas 2 tienen restricción rotativa cada semana.
# Semana 1 y 3 del mes → dígitos 5, 6, 7, 8  /  Semana 2 y 4 → dígitos 0, 1, 2, 3, 4, 9
SABADO_SEMANAS_IMPARES: list[int] = [5, 6, 7, 8]
SABADO_SEMANAS_PARES: list[int] = [0, 1, 2, 3, 4, 9]

# ---------------------------------------------------------------------------
# Restricciones adicionales por contingencia
# ---------------------------------------------------------------------------

# Fase 1: Holograma 1 ya no circula ese día.  Holograma 2 tampoco.
# (los hologramas 0/00 siguen exentos en Fase 1)

# Fase 2: Holograma 1 y 2 todos los días no circulan.
# Autos con holograma 0 y 00 también tienen restricción en Fase 2.
FASE2_RESTRICCION_HOLOGRAMA_0_00: dict[int, list[int]] = {
    1: [0, 1, 2, 3, 4],   # Lunes
    2: [5, 6, 7, 8, 9],   # Martes
    3: [0, 1, 2, 3, 4],   # Miércoles
    4: [5, 6, 7, 8, 9],   # Jueves
    5: [0, 1, 2, 3, 4],   # Viernes
}


@dataclass
class ResultadoCirculacion:
    puede_circular: bool
    razon: str
    restriccion_normal: bool
    restriccion_contingencia: bool
    nivel_contingencia: NivelContingencia
    dia: str
    holograma: str
    ultimo_digito: int


def _semana_del_mes(fecha: date) -> int:
    """Retorna en qué semana del mes cae la fecha (1-indexed)."""
    return (fecha.day - 1) // 7 + 1


def _digitos_restringidos_sabado(fecha: date) -> list[int]:
    """Determina qué dígitos no circulan el sábado según la semana del mes."""
    semana = _semana_del_mes(fecha)
    return SABADO_SEMANAS_IMPARES if semana % 2 != 0 else SABADO_SEMANAS_PARES


def evaluar_circulacion(
    ultimo_digito: int,
    holograma: str,
    nivel_contingencia: NivelContingencia,
    fecha: Optional[date] = None,
) -> ResultadoCirculacion:
    """
    Evalúa si un vehículo puede circular en la fecha indicada.

    Args:
        ultimo_digito: Último dígito numérico de la placa (0-9).
        holograma: Tipo de holograma ("00", "0", "1", "2").
        nivel_contingencia: Resultado del servicio de contingencia.
        fecha: Fecha a evaluar. Si es None, usa la fecha actual.

    Returns:
        ResultadoCirculacion con el veredicto y explicación detallada.
    """
    if fecha is None:
        fecha = date.today()

    try:
        holograma_enum = Holograma(holograma)
    except ValueError:
        raise ValueError(
            f"Holograma inválido: '{holograma}'. "
            f"Valores aceptados: {[h.value for h in Holograma]}"
        )

    if ultimo_digito not in range(10):
        raise ValueError(f"Último dígito debe ser entre 0 y 9. Se recibió: {ultimo_digito}")

    dia_iso = fecha.isoweekday()  # 1=lunes … 7=domingo
    nombre_dia = fecha.strftime("%A")  # En el locale del sistema

    NOMBRES_DIA = {1: "lunes", 2: "martes", 3: "miércoles", 4: "jueves", 5: "viernes", 6: "sábado", 7: "domingo"}
    nombre_dia_es = NOMBRES_DIA.get(dia_iso, nombre_dia)

    restriccion_normal = False
    restriccion_contingencia = False

    # ------------------------------------------------------------------
    # 1. Hologramas 0 y 00 — exentos en condiciones normales y Fase 1
    # ------------------------------------------------------------------
    if holograma_enum in (Holograma.CERO_CERO, Holograma.CERO):
        if nivel_contingencia == NivelContingencia.FASE_2:
            digitos_restringidos = FASE2_RESTRICCION_HOLOGRAMA_0_00.get(dia_iso, [])
            if ultimo_digito in digitos_restringidos:
                restriccion_contingencia = True
                return ResultadoCirculacion(
                    puede_circular=False,
                    razon=(
                        f"Holograma {holograma}: normalmente exento, pero en CONTINGENCIA FASE 2 "
                        f"el {nombre_dia_es} no circulan placas terminadas en {ultimo_digito}."
                    ),
                    restriccion_normal=False,
                    restriccion_contingencia=True,
                    nivel_contingencia=nivel_contingencia,
                    dia=nombre_dia_es,
                    holograma=holograma,
                    ultimo_digito=ultimo_digito,
                )
        return ResultadoCirculacion(
            puede_circular=True,
            razon=f"Holograma {holograma} está exento del programa Hoy No Circula.",
            restriccion_normal=False,
            restriccion_contingencia=False,
            nivel_contingencia=nivel_contingencia,
            dia=nombre_dia_es,
            holograma=holograma,
            ultimo_digito=ultimo_digito,
        )

    # ------------------------------------------------------------------
    # 2. Hologramas 1 y 2 — Verificar restricción diaria (lunes–viernes)
    # ------------------------------------------------------------------
    if dia_iso in RESTRICCION_SEMANAL:
        digitos_hoy = RESTRICCION_SEMANAL[dia_iso]
        if ultimo_digito in digitos_hoy:
            restriccion_normal = True

    # Sábados — solo holograma 2
    if dia_iso == 6 and holograma_enum == Holograma.DOS:
        digitos_sabado = _digitos_restringidos_sabado(fecha)
        if ultimo_digito in digitos_sabado:
            restriccion_normal = True

    # ------------------------------------------------------------------
    # 3. Contingencia Fase 2 — todos los hologramas 1 y 2 no circulan
    #    de lunes a viernes
    # ------------------------------------------------------------------
    if nivel_contingencia == NivelContingencia.FASE_2 and dia_iso in range(1, 6):
        restriccion_contingencia = True

    # ------------------------------------------------------------------
    # 4. Contingencia Fase 1 — aplica la restricción normal del día
    #    (mismo efecto que el programa regular, no agrega días extra
    #     para hologramas 1 y 2 según reglas CAMe vigentes)
    # ------------------------------------------------------------------
    if nivel_contingencia == NivelContingencia.FASE_1:
        restriccion_contingencia = restriccion_normal  # refuerza la normal

    # ------------------------------------------------------------------
    # 5. Componer resultado
    # ------------------------------------------------------------------
    no_circula = restriccion_normal or restriccion_contingencia

    if no_circula:
        causas = []
        if restriccion_normal:
            causas.append("programa Hoy No Circula regular")
        if restriccion_contingencia and not restriccion_normal:
            causas.append(f"contingencia ambiental {nivel_contingencia.value.replace('_', ' ').upper()}")
        razon = (
            f"Placa terminada en {ultimo_digito} con holograma {holograma} "
            f"NO CIRCULA el {nombre_dia_es} por: {', '.join(causas)}."
        )
    else:
        razon = (
            f"Placa terminada en {ultimo_digito} con holograma {holograma} "
            f"puede circular el {nombre_dia_es} sin restricciones."
        )

    return ResultadoCirculacion(
        puede_circular=not no_circula,
        razon=razon,
        restriccion_normal=restriccion_normal,
        restriccion_contingencia=restriccion_contingencia,
        nivel_contingencia=nivel_contingencia,
        dia=nombre_dia_es,
        holograma=holograma,
        ultimo_digito=ultimo_digito,
    )
