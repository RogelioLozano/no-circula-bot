"""
contingencia_service.py
-----------------------
Determina si hoy aplica "Doble No Circula" (contingencia ambiental atmosférica)
en la CDMX consultando noticias actuales y analizándolas con un LLM (Groq).

Estrategia:
  1. Busca "doble no circula hoy" en DuckDuckGo (con fallback a Google scraping).
  2. Valida que la fecha del artículo corresponda al día actual en México
     (zona horaria America/Mexico_City), tanto en el snippet del buscador
     como en los metadatos de la página descargada.
  3. Descarga hasta MAX_PAGINAS páginas que pasen la validación de fecha.
  4. Envía el texto de cada página al LLM (Groq) para que determine si
     el "Doble No Circula" está ACTIVO o CANCELADO hoy, y en qué fase.
  5. Aplica consenso por mayoría sobre las respuestas del LLM.

Requiere GROQ_API_KEY configurada en .env (gratuita en console.groq.com).
La API pública (verificar_contingencia / fase_a_nivel / NivelContingencia)
permanece idéntica para mantener compatibilidad con reglas_service y main.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, date
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Zona horaria ──────────────────────────────────────────────────────────────
TZ_MEXICO = ZoneInfo("America/Mexico_City")

# ── Parámetros de búsqueda ────────────────────────────────────────────────────
SEARCH_QUERY = "doble no circula hoy"

# Cuántos resultados pedir al buscador
SEARCH_MAX_RESULTS: int = 12

# Cuántas páginas con fecha válida analizar en detalle
MAX_PAGINAS: int = 5

# ── Configuración Groq LLM ────────────────────────────────────────────────────
# GROQ_API_KEY debe estar definida en .env (gratuita en console.groq.com).
GROQ_MODEL_DEFAULT: str = "llama-3.3-70b-versatile"

# Máximo de caracteres del artículo que se envían al LLM
LLM_MAX_CHARS: int = 4000

# Timeout HTTP en segundos
TIMEOUT: int = 15

HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-MX,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── Enum de nivel (consumido por reglas_service) ──────────────────────────────
class NivelContingencia(str, Enum):
    NINGUNA = "ninguna"
    FASE_1  = "fase_1"
    FASE_2  = "fase_2"


# ── Meses en español ──────────────────────────────────────────────────────────
_MESES_ES: dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

_PATRON_FECHA_ES = re.compile(
    r"\b(?P<dia>\d{1,2})\s+de\s+"
    r"(?P<mes>" + "|".join(_MESES_ES.keys()) + r")"
    r"(?:\s+de\s+(?P<anio>\d{4}))?",
    re.IGNORECASE,
)


# ── Análisis LLM (Groq) ───────────────────────────────────────────────────────

_PROMPT_SISTEMA = """\
Eres un asistente que analiza noticias mexicanas sobre el programa de circulación
vehicular en la Ciudad de México y el Estado de México.
Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional, sin markdown.
"""

_PROMPT_USUARIO_TMPL = """\
Hoy es {fecha}.

Analiza el siguiente artículo periodístico y determina si el programa
"Doble Hoy No Circula" (también llamado "Doble No Circula" o "restricción doble")
está activo HOY, {fecha}.

Reglas para tu análisis:
- Solo importa si aplica HOY ({fecha}), no días anteriores ni futuros.
- "Doble No Circula" es una restricción adicional que aplica en días de
  contingencia ambiental atmosférica declarada por la CAMe. Es distinto al
  programa regular "Hoy No Circula".
- Si el artículo pregunta "¿Habrá Doble No Circula?" y la respuesta del propio
  artículo es afirmativa, marca hay_doble_no_circula_hoy como true.
- Si el artículo indica explícitamente que NO hay contingencia o que se levantó,
  marca hay_doble_no_circula_hoy como false.
- Si no puedes determinarlo con certeza, usa null.
- Para "fase": usa "Fase 1", "Fase 2" o null. Si hay Doble No Circula pero no
  se menciona la fase, usa "Fase 1" por defecto.

Responde SOLO con este JSON (sin texto extra, sin ```json):
{{
  "hay_doble_no_circula_hoy": true | false | null,
  "fase": "Fase 1" | "Fase 2" | null,
  "razon": "explicación breve en una oración"
}}

Artículo:
{texto}
"""


def _analizar_con_llm(
    titular: str,
    texto_cuerpo: str,
    hoy: date,
) -> dict:
    """
    Envía el texto del artículo a Groq y devuelve el análisis estructurado.

    Returns:
        {
            "hay_doble": True | False | None,
            "fase": "Fase 1" | "Fase 2" | None,
            "razon": str,
        }

    Raises:
        RuntimeError: Si GROQ_API_KEY no está configurada o la librería no está instalada.
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY no está configurada. "
            "Obten una API key gratuita en https://console.groq.com y agrégala al .env."
        )

    try:
        from groq import Groq  # importación demorada
    except ImportError:
        raise RuntimeError(
            "La librería 'groq' no está instalada. Ejecuta: pip install groq"
        )

    modelo = os.environ.get("GROQ_MODEL", GROQ_MODEL_DEFAULT).strip()
    fecha_str = hoy.strftime("%d de %B de %Y").lower()
    # Reemplazar nombre del mes en inglés por español
    _meses_en_es = {
        "january": "enero", "february": "febrero", "march": "marzo",
        "april": "abril", "may": "mayo", "june": "junio",
        "july": "julio", "august": "agosto", "september": "septiembre",
        "october": "octubre", "november": "noviembre", "december": "diciembre",
    }
    for en, es in _meses_en_es.items():
        fecha_str = fecha_str.replace(en, es)

    texto_recortado = f"Titular: {titular}\n\n{texto_cuerpo[:LLM_MAX_CHARS]}"
    prompt_usuario = _PROMPT_USUARIO_TMPL.format(
        fecha=fecha_str,
        texto=texto_recortado,
    )

    try:
        client = Groq(api_key=api_key)
        completion = client.chat.completions.create(
            model=modelo,
            messages=[
                {"role": "system", "content": _PROMPT_SISTEMA},
                {"role": "user",   "content": prompt_usuario},
            ],
            temperature=0,          # determinista
            max_tokens=256,
            response_format={"type": "json_object"},
        )
        respuesta_raw = (completion.choices[0].message.content or "").strip()
        logger.debug("Respuesta LLM: %s", respuesta_raw)

        data = json.loads(respuesta_raw)
        hay = data.get("hay_doble_no_circula_hoy")  # True | False | None
        fase = data.get("fase")                      # "Fase 1" | "Fase 2" | None
        razon = data.get("razon", "")

        # Normalizar fase
        if isinstance(fase, str) and "2" in fase:
            fase = "Fase 2"
        elif isinstance(fase, str) and ("1" in fase or fase):
            fase = "Fase 1"
        else:
            fase = None

        logger.info(
            "  LLM (%s): hay_doble=%s | fase=%s | razón: %s",
            modelo, hay, fase, razon,
        )
        return {"hay_doble": hay, "fase": fase, "razon": razon}

    except json.JSONDecodeError as exc:
        raise RuntimeError(f"El LLM devolvió JSON inválido: {exc}") from exc
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Error al llamar a Groq: {exc}") from exc


# ── Helpers: fecha ────────────────────────────────────────────────────────────

def _hoy_mexico() -> date:
    """Retorna la fecha actual en la zona horaria de la Ciudad de México."""
    return datetime.now(TZ_MEXICO).date()


def _extraer_fecha_de_texto(texto: str) -> Optional[date]:
    """
    Busca la primera mención de una fecha en español dentro de 'texto'
    y la convierte a objeto date.  Acepta formatos como:
        "28 de febrero"  /  "28 de febrero de 2026"
    Si el año está ausente, asume el año actual.
    """
    match = _PATRON_FECHA_ES.search(texto)
    if not match:
        return None
    try:
        dia  = int(match.group("dia"))
        mes  = _MESES_ES[match.group("mes").lower()]
        anio = int(match.group("anio")) if match.group("anio") else _hoy_mexico().year
        return date(anio, mes, dia)
    except (ValueError, KeyError):
        return None


def _extraer_fecha_meta(html: str) -> Optional[date]:
    """
    Extrae la fecha de publicación de metadatos Open Graph / article tags.
    Soporta 'article:published_time', 'og:updated_time', 'datePublished'.
    """
    soup = BeautifulSoup(html, "html.parser")

    for prop in ("article:published_time", "og:updated_time",
                 "article:modified_time", "datePublished"):
        tag = (
            soup.find("meta", attrs={"property": prop})
            or soup.find("meta", attrs={"name": prop})
            or soup.find("meta", attrs={"itemprop": prop})
        )
        if tag and tag.get("content"):
            raw = tag["content"]
            # Intentar parsear solo la parte de fecha (primeros 10 chars ISO)
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                pass

    # Buscar <time datetime="...">
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        try:
            return date.fromisoformat(str(time_tag["datetime"])[:10])
        except ValueError:
            pass

    return None


def _fecha_es_hoy(fecha: Optional[date], hoy: date) -> bool:
    return fecha is not None and fecha == hoy


# ── Helpers: red ──────────────────────────────────────────────────────────────

def _fetch_html(url: str) -> tuple[Optional[str], Optional[str]]:
    """Descarga el HTML de una URL con manejo de errores."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
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

    if resp.status_code != 200:
        msg = f"HTTP {resp.status_code} en {url}."
        logger.warning(msg)
        return None, msg

    # Forzar encoding correcto para evitar mojibake en sitios latinos
    if resp.encoding and resp.encoding.upper() in ("ISO-8859-1", "LATIN-1", "WINDOWS-1252"):
        resp.encoding = resp.apparent_encoding or "utf-8"

    return resp.text, None


def _extraer_texto_limpio(html: str) -> str:
    """Convierte HTML a texto plano eliminando scripts, estilos y navegación."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "head",
                     "nav", "footer", "aside", "template"]):
        tag.decompose()
    texto = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s{2,}", " ", texto).strip()


def _extraer_titular(html: str) -> str:
    """Extrae el titular principal (H1 o <title>) de la página."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if h1:
        return re.sub(r"\s{2,}", " ", h1.get_text(strip=True))
    title_tag = soup.find("title")
    if title_tag:
        return re.sub(r"\s{2,}", " ", title_tag.get_text(strip=True))
    return ""


# ── Helpers: búsqueda ─────────────────────────────────────────────────────────

def _buscar_ddg(query: str, max_results: int) -> list[dict]:
    """
    Busca con DuckDuckGo usando la librería duckduckgo-search.
    Retorna lista de {title, href, body}.
    """
    try:
        try:
            from ddgs import DDGS  # librería renombrada ≥ v1.0
        except ImportError:
            from duckduckgo_search import DDGS  # nombre anterior como fallback
        with DDGS() as ddgs:
            resultados = list(ddgs.text(
                query,
                max_results=max_results,
                region="mx-es",
                safesearch="off",
                timelimit="d",   # últimas 24 horas
            ))
        logger.info("DDG devolvió %d resultados para '%s'.", len(resultados), query)
        return resultados
    except Exception as exc:
        logger.warning("DuckDuckGo falló: %s", exc)
        return []


def _buscar_google_scrape(query: str) -> list[dict]:
    """
    Fallback: scraping de resultados de Google (últimas 24 h).
    Retorna lista de {title, href, body}.
    """
    url = (
        "https://www.google.com/search"
        f"?q={urllib.parse.quote(query)}"
        "&hl=es-419&gl=mx&num=10&tbs=qdr:d"
    )
    html, error = _fetch_html(url)
    if error or not html:
        logger.warning("Google scraping falló: %s", error)
        return []

    soup = BeautifulSoup(html, "html.parser")
    resultados: list[dict] = []

    for a in soup.select("div.g a[href]"):
        href = str(a.get("href", "") or "")
        if not href.startswith("http"):
            continue
        h3 = a.find("h3")
        titulo = h3.get_text(strip=True) if h3 else ""
        if not titulo:
            continue
        snippet_tag = a.find_parent("div")
        snippet = ""
        if snippet_tag:
            span = snippet_tag.find("span", class_="MUxGbd")
            if not span:
                span = snippet_tag.find("span", attrs={"class": lambda c: isinstance(c, list) and "MUxGbd" in c})
            if span:
                snippet = span.get_text(strip=True)
        resultados.append({"title": titulo, "href": href, "body": snippet})

    logger.info("Google scraping devolvió %d resultados.", len(resultados))
    return resultados


# ── API pública ───────────────────────────────────────────────────────────────

def fase_a_nivel(fase: Optional[str]) -> NivelContingencia:
    """
    Convierte el string de fase al enum NivelContingencia.

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
    Determina si hoy aplica "Doble No Circula" consultando noticias actuales.

    Flujo:
      1. Busca "doble no circula hoy" en DuckDuckGo (fallback a Google scraping).
      2. Valida que la fecha del artículo sea hoy (hora México).
      3. Descarga hasta MAX_PAGINAS páginas que pasen la validación de fecha.
      4. Envía cada página al LLM (Groq) para análisis semántico.
      5. Consenso por mayoría: activo/no-activo, y fase (1 ó 2).

    Returns:
        {
            "hay_contingencia": bool,
            "fase": "Fase 1" | "Fase 2" | None,
            "detalle": str,
        }
    """
    hoy = _hoy_mexico()
    logger.info("Fecha en México: %s", hoy.isoformat())

    # ── 1. Obtener resultados del buscador ────────────────────────────────
    resultados = _buscar_ddg(SEARCH_QUERY, SEARCH_MAX_RESULTS)
    if not resultados:
        logger.warning("DuckDuckGo sin resultados; intentando Google scraping.")
        resultados = _buscar_google_scrape(SEARCH_QUERY)

    if not resultados:
        msg = "No se pudieron obtener resultados de búsqueda (DuckDuckGo y Google fallaron)."
        logger.error(msg)
        return {"hay_contingencia": False, "fase": None, "detalle": msg}

    logger.info("%d resultados obtenidos del buscador.", len(resultados))

    # ── 2. Ordenar: primero los que mencionan la fecha de hoy ─────────────
    con_fecha_hoy: list[dict] = []
    sin_fecha_clara: list[dict] = []

    for r in resultados:
        texto_snippet = f"{r.get('title', '')} {r.get('body', '')}"
        fecha = _extraer_fecha_de_texto(texto_snippet)
        if _fecha_es_hoy(fecha, hoy):
            logger.debug("Fecha OK en snippet: %s", r.get("title"))
            con_fecha_hoy.append(r)
        else:
            logger.debug("Fecha ausente o distinta (%s≠%s): %s", fecha, hoy, r.get("title"))
            sin_fecha_clara.append(r)

    candidatos = con_fecha_hoy + sin_fecha_clara
    logger.info(
        "%d candidatos con fecha=hoy, %d sin fecha clara.",
        len(con_fecha_hoy), len(sin_fecha_clara),
    )

    if not con_fecha_hoy:
        logger.warning(
            "Ningún resultado tiene la fecha de hoy (%s) en el snippet. "
            "Se analizarán hasta %d páginas de todas formas.",
            hoy, MAX_PAGINAS,
        )

    # ── 4. Descargar y analizar páginas ───────────────────────────────────
    votos_activo:     int = 0
    votos_no_activo:  int = 0
    fases_detectadas: list[str] = []
    fuentes:          list[str] = []
    detalles:         list[str] = []
    paginas_analizadas: int = 0

    for resultado in candidatos:
        if paginas_analizadas >= MAX_PAGINAS:
            break

        url    = resultado.get("href", "")
        titulo_buscador = resultado.get("title", "")
        if not url:
            continue

        logger.info(
            "Descargando página %d/%d: %s",
            paginas_analizadas + 1, MAX_PAGINAS, url,
        )

        html, error = _fetch_html(url)
        if error or not html:
            logger.warning("No se pudo descargar '%s': %s", url, error)
            continue

        # Validar fecha de publicación desde metadatos de la página
        fecha_meta   = _extraer_fecha_meta(html)
        texto_limpio = _extraer_texto_limpio(html)
        fecha_texto  = _extraer_fecha_de_texto(texto_limpio[:2000])
        fecha_pagina = fecha_meta or fecha_texto

        # Verificar si la URL o el título del buscador mencionan la fecha de hoy
        # (los medios publican la nota la noche anterior con fecha del día siguiente)
        fecha_en_url_title = _extraer_fecha_de_texto(url + " " + titulo_buscador)
        fecha_referida_hoy = _fecha_es_hoy(fecha_en_url_title, hoy)

        if fecha_pagina and fecha_pagina != hoy and not fecha_referida_hoy:
            logger.warning(
                "Página DESCARTADA (fecha meta=%s, url/título no menciona hoy %s): %s",
                fecha_pagina, hoy, url,
            )
            continue

        if fecha_pagina and fecha_pagina != hoy and fecha_referida_hoy:
            logger.info(
                "Página aceptada: meta indica %s pero URL/título confirman fecha de hoy %s.",
                fecha_pagina, hoy,
            )

        paginas_analizadas += 1
        titular = _extraer_titular(html)

        # ── Análisis con LLM (Groq) ───────────────────────────────────────
        try:
            analisis = _analizar_con_llm(titular, texto_limpio, hoy)
        except RuntimeError as exc:
            logger.error("Error LLM en '%s': %s", url, exc)
            # Si el LLM falla para UNA página, la saltamos pero no abortamos
            detalles.append(f"⚠ ERROR LLM [{url}] {exc}")
            fuentes.append(url)
            continue

        hay_doble = analisis["hay_doble"]
        fase      = analisis["fase"]
        razon_llm = analisis["razon"]

        logger.info(
            "  Titular: %s | resultado=%s | fase=%s | razón: %s",
            titular[:100], hay_doble, fase, razon_llm,
        )

        if hay_doble is True:
            votos_activo += 1
            if fase:
                fases_detectadas.append(fase)
            detalles.append(f"✅ ACTIVO [{url}] {razon_llm}")

        elif hay_doble is False:
            votos_no_activo += 1
            detalles.append(f"🚫 NO ACTIVO [{url}] {razon_llm}")

        else:
            # Inconclusivo (None): el LLM no pudo determinar con certeza
            detalles.append(f"? INCONCLUSIVO [{url}] {razon_llm}")

        fuentes.append(url)

    logger.info(
        "Resumen: %d páginas analizadas | activo=%d | no_activo=%d",
        paginas_analizadas, votos_activo, votos_no_activo,
    )

    # ── 5. Consenso ───────────────────────────────────────────────────────
    if paginas_analizadas == 0:
        msg = (
            "No se encontraron páginas con la fecha de hoy "
            f"({hoy.isoformat()}) para validar la contingencia."
        )
        logger.warning(msg)
        return {"hay_contingencia": False, "fase": None, "detalle": msg}

    hay_contingencia = votos_activo > votos_no_activo

    # Fase más frecuente entre páginas que votan "activo"
    fase_resultado: Optional[str] = None
    if hay_contingencia:
        if fases_detectadas:
            fase_resultado = max(set(fases_detectadas), key=fases_detectadas.count)
        else:
            # "Doble No Circula" sin fase explícita → Fase 1 por convención CAMe
            fase_resultado = "Fase 1"

    detalle_str = (
        f"Votación: {votos_activo} activo vs {votos_no_activo} no-activo "
        f"({paginas_analizadas} páginas analizadas). "
        "Fragmentos: " + " | ".join(detalles[:3])
    )

    logger.info(
        "Resultado final: hay_contingencia=%s | fase=%s",
        hay_contingencia, fase_resultado,
    )

    return {
        "hay_contingencia": hay_contingencia,
        "fase": fase_resultado,
        "detalle": detalle_str,
    }
