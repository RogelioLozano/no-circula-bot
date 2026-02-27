"""
contingencia_service.py
-----------------------
Consulta el portal oficial de la Comisión Ambiental de la Megalópolis (CAMe)
y detecta si existe una contingencia ambiental atmosférica activa (Fase 1 o Fase 2).

La detección se basa en búsqueda de texto sobre el HTML completo parseado,
sin depender de selectores CSS frágiles como clases o IDs específicos.
"""

import logging
import re
from enum import Enum
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── URLs oficiales ────────────────────────────────────────────────────────────
# Se consultan en orden; se retorna al primer resultado válido.
CAME_URLS: list[str] = [
    "https://www.gob.mx/comisionambiental/acciones-y-programas/contingencias-ambientales-atmosfericas",
    "https://www.gob.mx/comisionambiental",
]

HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-MX,es;q=0.9",
}

TIMEOUT: int = 15  # segundos

# ── Enum de nivel (usado por reglas_service) ──────────────────────────────────


class NivelContingencia(str, Enum):
    NINGUNA = "ninguna"
    FASE_1 = "fase_1"
    FASE_2 = "fase_2"


# ── Patrones de detección (case-insensitive) ──────────────────────────────────
# Fase 2 se evalúa ANTES que Fase 1 para evitar falsos negativos si el texto
# menciona ambas fases o usa "Fase II" junto con "Fase I".

_RE_FASE_2 = re.compile(
    r"fase\s*2"
    r"|contingencia\s+(?:ambiental\s+)?fase\s+(?:2|ii)\b",
    re.IGNORECASE,
)

_RE_FASE_1 = re.compile(
    r"fase\s*1"
    r"|contingencia\s+(?:ambiental\s+)?fase\s+(?:1|i)\b",
    re.IGNORECASE,
)

# Cuántos caracteres mostrar alrededor del match en el campo "detalle"
_SNIPPET_RADIO: int = 250


# ── Helpers privados ──────────────────────────────────────────────────────────


def _fetch_html(url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Descarga el HTML de la URL con manejo explícito de errores.

    Returns:
        (html, None)  — éxito.
        (None, mensaje_de_error)  — fallo.
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.Timeout:
        msg = f"Timeout ({TIMEOUT}s) al conectar con {url}."
        logger.warning(msg)
        return None, msg
    except requests.ConnectionError as exc:
        msg = f"Error de conexión con {url}: {exc}"
        logger.warning(msg)
        return None, msg
    except requests.RequestException as exc:
        msg = f"Error inesperado al consultar {url}: {exc}"
        logger.warning(msg)
        return None, msg

    if response.status_code != 200:
        msg = (
            f"El servidor respondió con HTTP {response.status_code} "
            f"(esperado 200) para {url}."
        )
        logger.warning(msg)
        return None, msg

    return response.text, None


def _extraer_texto_limpio(html: str) -> str:
    """
    Convierte el HTML a texto plano quitando ruido (scripts, estilos,
    navegación, meta-datos) sin depender de clases CSS específicas.
    Normaliza espacios múltiples y saltos de línea.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "head",
                     "nav", "footer", "aside", "template"]):
        tag.decompose()

    texto = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s{2,}", " ", texto).strip()


def _extraer_snippet(texto: str, match: re.Match) -> str:
    """
    Devuelve el fragmento de texto alrededor de la coincidencia
    para usarlo como contexto en el campo 'detalle'.
    """
    inicio = max(0, match.start() - _SNIPPET_RADIO)
    fin = min(len(texto), match.end() + _SNIPPET_RADIO)
    fragmento = texto[inicio:fin].strip()
    return re.sub(r"\s{2,}", " ", fragmento)


def _detectar_fase(texto: str) -> tuple[Optional[str], str]:
    """
    Busca patrones de contingencia en el texto y devuelve el primer nivel
    encontrado junto con su contexto.

    Returns:
        ("Fase 2", snippet) | ("Fase 1", snippet) | (None, mensaje_sin_resultado)
    """
    # Evaluar Fase 2 primero (más grave)
    match = _RE_FASE_2.search(texto)
    if match:
        return "Fase 2", _extraer_snippet(texto, match)

    match = _RE_FASE_1.search(texto)
    if match:
        return "Fase 1", _extraer_snippet(texto, match)

    return (
        None,
        "Sin contingencia activa: no se encontraron menciones de Fase 1 o Fase 2 en la página.",
    )


# ── API pública ───────────────────────────────────────────────────────────────


def fase_a_nivel(fase: Optional[str]) -> NivelContingencia:
    """
    Convierte el string de fase devuelto por ``verificar_contingencia``
    al enum ``NivelContingencia`` que consume ``reglas_service``.

    Args:
        fase: "Fase 1", "Fase 2" o None.

    Returns:
        NivelContingencia correspondiente.
    """
    _mapa: dict[str, NivelContingencia] = {
        "Fase 1": NivelContingencia.FASE_1,
        "Fase 2": NivelContingencia.FASE_2,
    }
    return _mapa.get(fase, NivelContingencia.NINGUNA)  # type: ignore[arg-type]


def verificar_contingencia() -> dict:
    """
    Consulta el portal oficial de la CAMe y determina si hay contingencia
    ambiental atmosférica activa.

    Intenta cada URL de ``CAME_URLS`` en orden y retorna al primer resultado
    válido. Si ninguna URL responde correctamente, retorna un dict de error
    con ``hay_contingencia=False``.

    Returns:
        {
            "hay_contingencia": bool,
            "fase": "Fase 1" | "Fase 2" | None,
            "detalle": str  — fragmento de texto cercano a la coincidencia,
                             o descripción del error/ausencia de datos.
        }

    Errores manejados:
        - Timeout de conexión
        - Código de respuesta HTTP distinto de 200
        - Página obtenida sin texto parseable
    """
    errores: list[str] = []

    for url in CAME_URLS:
        logger.info("Consultando CAMe en: %s", url)

        html, error = _fetch_html(url)

        if error or html is None:
            errores.append(error or f"Respuesta vacía de {url}")
            continue

        texto = _extraer_texto_limpio(html)

        # Fallback: si BS4 devuelve vacío (sitio JS-rendered), buscar en el
        # HTML crudo — el contenido suele estar embebido en JSON/script inline.
        if not texto:
            logger.warning(
                "Texto vacío tras parseo BS4, buscando en HTML crudo: %s", url
            )
            texto = html

        fase, detalle = _detectar_fase(texto)
        hay_contingencia = fase is not None

        logger.info(
            "Resultado: hay_contingencia=%s | fase=%s | fuente=%s",
            hay_contingencia,
            fase,
            url,
        )

        return {
            "hay_contingencia": hay_contingencia,
            "fase": fase,
            "detalle": detalle,
        }

    # Ninguna URL respondió de forma utilizable
    detalle_error = (
        "No se pudo consultar el portal de la CAMe. "
        "Errores encontrados: " + " | ".join(errores)
    )
    logger.error(detalle_error)

    return {
        "hay_contingencia": False,
        "fase": None,
        "detalle": detalle_error,
    }
