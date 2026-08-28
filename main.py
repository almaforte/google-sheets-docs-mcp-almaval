"""
Serveur MCP Google Sheets et Docs — Almaval / Claude
=====================================================

Authentification : compte de service avec délégation au niveau du domaine.
Le serveur agit sous l'identité de l'utilisateur défini par IMPERSONATE_USER,
avec exactement ses droits, ni plus ni moins.

Les outils Apps Script font exception et utilisent un jeton utilisateur,
l'API Apps Script ne fonctionnant pas avec les comptes de service.

Variables d'environnement attendues :
    GOOGLE_SERVICE_ACCOUNT_JSON   contenu intégral du fichier JSON de la clé
    IMPERSONATE_USER              ex. am.forte@almaval.ch
    MCP_PATH                      chemin d'écoute, ex. /mcp-sheets-8f3a91  (défaut /mcp)
    MCP_API_KEY                   clé attendue en en-tête, facultative
    GOOGLE_OAUTH_CLIENT_ID        outils Apps Script
    GOOGLE_OAUTH_CLIENT_SECRET    outils Apps Script
    GOOGLE_OAUTH_REFRESH_TOKEN    outils Apps Script
    SCRIPT_SHARED_SECRET          secret partagé transmis aux applications web
    PORT                          fourni automatiquement par Railway

Le scope https://www.googleapis.com/auth/documents doit être ajouté à la
délégation au niveau du domaine du compte de service, dans la console
d'administration, sans quoi les outils Docs répondront par une erreur de
permission.
"""

import functools
import hmac
import io
import json
import os
import re
from typing import Any, Optional

import requests
import uvicorn
from fastmcp import FastMCP
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

mcp = FastMCP("Google Sheets Almaval")

_services: dict = {}


# ---------------------------------------------------------------- auth

def _credentials():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("Variable GOOGLE_SERVICE_ACCOUNT_JSON absente.")
    subject = os.environ.get("IMPERSONATE_USER")
    if not subject:
        raise RuntimeError("Variable IMPERSONATE_USER absente.")
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return creds.with_subject(subject)


def _sheets():
    if "sheets" not in _services:
        _services["sheets"] = build(
            "sheets", "v4", credentials=_credentials(), cache_discovery=False
        )
    return _services["sheets"]


def _docs():
    if "docs" not in _services:
        _services["docs"] = build(
            "docs", "v1", credentials=_credentials(), cache_discovery=False
        )
    return _services["docs"]


def _drive():
    if "drive" not in _services:
        _services["drive"] = build(
            "drive", "v3", credentials=_credentials(), cache_discovery=False
        )
    return _services["drive"]


def tolerant(fn):
    """Renvoie une erreur lisible plutôt qu'une trace brute."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HttpError as exc:
            return {
                "erreur": f"HTTP {exc.resp.status}",
                "detail": exc._get_reason(),
            }
        except Exception as exc:  # noqa: BLE001
            return {"erreur": type(exc).__name__, "detail": str(exc)}

    return wrapper


def _hex_to_rgb(value: str) -> dict:
    value = value.lstrip("#")
    return {
        "red": int(value[0:2], 16) / 255,
        "green": int(value[2:4], 16) / 255,
        "blue": int(value[4:6], 16) / 255,
    }


def _grid_range(
    sheet_id: int,
    start_row: int = 0,
    end_row: Optional[int] = None,
    start_col: int = 0,
    end_col: Optional[int] = None,
) -> dict:
    grid = {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "startColumnIndex": start_col,
    }
    if end_row is not None:
        grid["endRowIndex"] = end_row
    if end_col is not None:
        grid["endColumnIndex"] = end_col
    return grid


# ---------------------------------------------------------------- lecture

@mcp.tool
@tolerant
def search_spreadsheets(name: str = "", limit: int = 20) -> Any:
    """Recherche des classeurs Google Sheets par nom.

    name  : fragment du nom recherché, vide pour les plus récents
    limit : nombre maximum de résultats
    """
    query = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    if name:
        query += " and name contains '{}'".format(name.replace("'", "\\'"))
    result = (
        _drive()
        .files()
        .list(
            q=query,
            pageSize=limit,
            orderBy="modifiedTime desc",
            fields="files(id,name,webViewLink,modifiedTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    return result.get("files", [])


@mcp.tool
@tolerant
def get_spreadsheet(spreadsheet_id: str) -> Any:
    """Retourne le titre du classeur et la liste de ses onglets.

    Pour chaque onglet : identifiant numérique, titre, nombre de lignes et de colonnes.
    L'identifiant numérique est celui à utiliser pour la mise en forme.
    """
    data = (
        _sheets()
        .spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="properties.title,sheets.properties(sheetId,title,index,gridProperties)",
        )
        .execute()
    )
    return {
        "titre": data.get("properties", {}).get("title"),
        "onglets": [
            {
                "sheet_id": s["properties"]["sheetId"],
                "titre": s["properties"]["title"],
                "index": s["properties"].get("index"),
                "lignes": s["properties"].get("gridProperties", {}).get("rowCount"),
                "colonnes": s["properties"].get("gridProperties", {}).get("columnCount"),
            }
            for s in data.get("sheets", [])
        ],
    }


@mcp.tool
@tolerant
def get_values(spreadsheet_id: str, range_a1: str, formulas: bool = False) -> Any:
    """Lit une plage en notation A1, par exemple 'Feuille 1!A1:D20'.

    formulas : si vrai, retourne les formules au lieu des valeurs calculées
    """
    result = (
        _sheets()
        .spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueRenderOption="FORMULA" if formulas else "UNFORMATTED_VALUE",
        )
        .execute()
    )
    return {"plage": result.get("range"), "valeurs": result.get("values", [])}


# ---------------------------------------------------------------- écriture

def _update_values(
    spreadsheet_id: str,
    range_a1: str,
    values: list,
    raw: bool = False,
) -> Any:
    """Implémentation partagée par update_values et update_cell."""
    result = (
        _sheets()
        .spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption="RAW" if raw else "USER_ENTERED",
            body={"values": values},
        )
        .execute()
    )
    return {
        "plage": result.get("updatedRange"),
        "lignes": result.get("updatedRows"),
        "colonnes": result.get("updatedColumns"),
        "cellules": result.get("updatedCells"),
    }


@mcp.tool
@tolerant
def update_values(
    spreadsheet_id: str,
    range_a1: str,
    values: list,
    raw: bool = False,
) -> Any:
    """Écrit une plage de cellules.

    values : liste de listes, une liste par ligne
    raw    : si vrai, le contenu est écrit littéralement sans interprétation.
             Par défaut les formules et les dates sont interprétées comme
             si elles étaient saisies au clavier.

    Les noms de fonctions doivent être écrits en anglais avec la virgule
    comme séparateur, par exemple =SUM(A1:A10). Le classeur les affichera
    en français selon sa langue.
    """
    return _update_values(spreadsheet_id, range_a1, values, raw)


@mcp.tool
@tolerant
def update_cell(spreadsheet_id: str, cell_a1: str, value: Any) -> Any:
    """Écrit une seule cellule, par exemple 'Feuille 1!C7'."""
    return _update_values(spreadsheet_id, cell_a1, [[value]])


@mcp.tool
@tolerant
def append_rows(spreadsheet_id: str, range_a1: str, values: list) -> Any:
    """Ajoute des lignes à la suite des données existantes de la plage."""
    result = (
        _sheets()
        .spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )
    return result.get("updates", {})


@mcp.tool
@tolerant
def clear_values(spreadsheet_id: str, range_a1: str) -> Any:
    """Efface le contenu d'une plage sans toucher à la mise en forme."""
    result = (
        _sheets()
        .spreadsheets()
        .values()
        .clear(spreadsheetId=spreadsheet_id, range=range_a1, body={})
        .execute()
    )
    return {"plage_effacee": result.get("clearedRange")}


# ---------------------------------------------------------------- structure

@mcp.tool
@tolerant
def create_spreadsheet(
    title: str,
    folder_id: str = "",
    sheet_titles: Optional[list] = None,
) -> Any:
    """Crée un nouveau classeur et retourne son identifiant et son lien.

    folder_id    : dossier Drive de destination, facultatif
    sheet_titles : titres des onglets à créer, facultatif
    """
    body: dict = {"properties": {"title": title, "locale": "fr_CH"}}
    if sheet_titles:
        body["sheets"] = [{"properties": {"title": t}} for t in sheet_titles]
    created = (
        _sheets()
        .spreadsheets()
        .create(body=body, fields="spreadsheetId,spreadsheetUrl")
        .execute()
    )
    file_id = created["spreadsheetId"]
    if folder_id:
        current = (
            _drive()
            .files()
            .get(fileId=file_id, fields="parents", supportsAllDrives=True)
            .execute()
        )
        _drive().files().update(
            fileId=file_id,
            addParents=folder_id,
            removeParents=",".join(current.get("parents", [])),
            fields="id,parents",
            supportsAllDrives=True,
        ).execute()
    return {"spreadsheet_id": file_id, "url": created["spreadsheetUrl"]}


@mcp.tool
@tolerant
def add_sheet(
    spreadsheet_id: str,
    title: str,
    rows: int = 1000,
    columns: int = 26,
) -> Any:
    """Ajoute un onglet au classeur et retourne son identifiant numérique."""
    response = (
        _sheets()
        .spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": title,
                                "gridProperties": {
                                    "rowCount": rows,
                                    "columnCount": columns,
                                },
                            }
                        }
                    }
                ]
            },
        )
        .execute()
    )
    props = response["replies"][0]["addSheet"]["properties"]
    return {"sheet_id": props["sheetId"], "titre": props["title"]}


@mcp.tool
@tolerant
def insert_dimension(
    spreadsheet_id: str,
    sheet_id: int,
    dimension: str = "ROWS",
    start_index: int = 0,
    count: int = 1,
) -> Any:
    """Insère des lignes ou des colonnes.

    dimension   : 'ROWS' ou 'COLUMNS'
    start_index : position d'insertion, comptée à partir de zéro
    count       : nombre de lignes ou colonnes à insérer
    """
    _sheets().spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": dimension,
                            "startIndex": start_index,
                            "endIndex": start_index + count,
                        },
                        "inheritFromBefore": start_index > 0,
                    }
                }
            ]
        },
    ).execute()
    return {"insere": count, "dimension": dimension, "a_partir_de": start_index}


@mcp.tool
@tolerant
def delete_dimension(
    spreadsheet_id: str,
    sheet_id: int,
    dimension: str = "ROWS",
    start_index: int = 0,
    count: int = 1,
) -> Any:
    """Supprime des lignes ou des colonnes. Opération irréversible."""
    _sheets().spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": dimension,
                            "startIndex": start_index,
                            "endIndex": start_index + count,
                        }
                    }
                }
            ]
        },
    ).execute()
    return {"supprime": count, "dimension": dimension, "a_partir_de": start_index}


# ---------------------------------------------------------------- mise en forme

@mcp.tool
@tolerant
def format_range(
    spreadsheet_id: str,
    sheet_id: int,
    start_row: int = 0,
    end_row: Optional[int] = None,
    start_col: int = 0,
    end_col: Optional[int] = None,
    horizontal: str = "",
    vertical: str = "",
    wrap: bool = False,
    bold: Optional[bool] = None,
    font_size: int = 0,
    background_hex: str = "",
    number_format: str = "",
    row_height: int = 0,
) -> Any:
    """Applique une mise en forme à une plage.

    Les index sont comptés à partir de zéro, la borne de fin est exclue.
    Laisser end_row ou end_col vide applique la mise en forme jusqu'au bout.

    horizontal    : 'LEFT', 'CENTER' ou 'RIGHT'
    vertical      : 'TOP', 'MIDDLE' ou 'BOTTOM'
    number_format : motif, par exemple '#,##0.00' ou 'dd.MM.yyyy'
    row_height    : hauteur des lignes en pixels
    """
    fmt: dict = {}
    fields: list = []

    if horizontal:
        fmt["horizontalAlignment"] = horizontal
        fields.append("horizontalAlignment")
    if vertical:
        fmt["verticalAlignment"] = vertical
        fields.append("verticalAlignment")
    if wrap:
        fmt["wrapStrategy"] = "WRAP"
        fields.append("wrapStrategy")
    if background_hex:
        fmt["backgroundColor"] = _hex_to_rgb(background_hex)
        fields.append("backgroundColor")
    if number_format:
        fmt["numberFormat"] = {"type": "NUMBER", "pattern": number_format}
        fields.append("numberFormat")

    text_format: dict = {}
    if bold is not None:
        text_format["bold"] = bold
        fields.append("textFormat.bold")
    if font_size:
        text_format["fontSize"] = font_size
        fields.append("textFormat.fontSize")
    if text_format:
        fmt["textFormat"] = text_format

    requests: list = []
    if fields:
        requests.append(
            {
                "repeatCell": {
                    "range": _grid_range(sheet_id, start_row, end_row, start_col, end_col),
                    "cell": {"userEnteredFormat": fmt},
                    "fields": "userEnteredFormat({})".format(",".join(fields)),
                }
            }
        )

    if row_height:
        dim_range = {
            "sheetId": sheet_id,
            "dimension": "ROWS",
            "startIndex": start_row,
        }
        if end_row is not None:
            dim_range["endIndex"] = end_row
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": dim_range,
                    "properties": {"pixelSize": row_height},
                    "fields": "pixelSize",
                }
            }
        )

    if not requests:
        return {"erreur": "Aucune propriété de mise en forme fournie."}

    _sheets().spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()
    return {"mise_en_forme_appliquee": len(requests)}


@mcp.tool
@tolerant
def apply_house_style(
    spreadsheet_id: str,
    sheet_id: int,
    header_row: bool = True,
    row_height: int = 30,
    freeze_header: bool = True,
) -> Any:
    """Applique la convention de présentation d'Alberto à tout un onglet.

    Cellules centrées horizontalement et verticalement, texte renvoyé à la
    ligne, lignes hautes de 30 pixels, première ligne en gras et figée.
    """
    info = (
        _sheets()
        .spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(sheetId,gridProperties)",
        )
        .execute()
    )
    row_count = 1000
    for sheet in info.get("sheets", []):
        if sheet["properties"]["sheetId"] == sheet_id:
            row_count = sheet["properties"].get("gridProperties", {}).get("rowCount", 1000)

    requests: list = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id},
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "WRAP",
                    }
                },
                "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,wrapStrategy)",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": 0,
                    "endIndex": row_count,
                },
                "properties": {"pixelSize": row_height},
                "fields": "pixelSize",
            }
        },
    ]

    if header_row:
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                    },
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }
        )

    if freeze_header:
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            }
        )

    _sheets().spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()
    return {"style_applique": True, "lignes_traitees": row_count}


@mcp.tool
@tolerant
def auto_resize_columns(
    spreadsheet_id: str,
    sheet_id: int,
    start_col: int = 0,
    end_col: Optional[int] = None,
) -> Any:
    """Ajuste automatiquement la largeur des colonnes à leur contenu."""
    dim_range = {
        "sheetId": sheet_id,
        "dimension": "COLUMNS",
        "startIndex": start_col,
    }
    if end_col is not None:
        dim_range["endIndex"] = end_col
    _sheets().spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"autoResizeDimensions": {"dimensions": dim_range}}]},
    ).execute()
    return {"colonnes_ajustees": True}


# ---------------------------------------------------------------- échappatoire

@mcp.tool
@tolerant
def batch_update(spreadsheet_id: str, requests: list) -> Any:
    """Exécute des requêtes batchUpdate brutes de l'API Sheets.

    À n'utiliser que pour les opérations non couvertes par les autres outils :
    fusion de cellules, mise en forme conditionnelle, filtres, graphiques,
    validation de données, protection de plages.
    """
    response = (
        _sheets()
        .spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
        .execute()
    )
    return {"requetes_executees": len(requests), "reponses": response.get("replies", [])}


# ================================================================
#  GOOGLE DOCS
#  Charte de mise en forme des documents Almaval, version juin 2026.
#  Tous les réglages typographiques sont regroupés ci-dessous, de façon
#  qu'un changement de charte se traduise par un changement de constante.
# ================================================================

# Gabarit institutionnel. Toute production part d'une copie de ce document,
# dont elle hérite l'en-tête, le pied de page et les marges. Le gabarit
# lui-même n'est jamais modifié.
DOC_TEMPLATE_ID = "1024m5Jtwqgdk6r9T4fEUVFg8mVbW9BlPrguqasrE37U"

DOC_FONT = "Manjari"
DOC_FONT_SIZE = 11  # taille unique, titres compris

COLOR_TEAL = "#128DA0"    # titres de chapitre, en-têtes de tableau, liens
COLOR_GOLD = "#E9B85F"    # titres de sous chapitre et de niveau trois
COLOR_GREY = "#666666"    # texte courant, jamais de noir
COLOR_ROW_ALT = "#F2F4F5"  # fond d'une ligne de tableau sur deux
COLOR_CALLOUT = "#E7F3F6"  # fond des encadrés
COLOR_BORDER = "#E2E2E2"   # bordures fines
COLOR_WHITE = "#FFFFFF"    # texte sur l'en-tête teal des tableaux

DOC_LINE_SPACING = 150      # une fois et demie, en pourcentage
DOC_SPACE_BEFORE = 6        # points, avant chaque paragraphe
DOC_SPACE_AFTER = 6         # points, après chaque paragraphe
DOC_HEADING_SPACE_BEFORE = 12  # rupture un peu plus marquée avant un titre

DOC_BODY_ALIGNMENT = "JUSTIFIED"
DOC_HEADING_ALIGNMENT = "START"
DOC_LIST_ALIGNMENT = "JUSTIFIED"

# Couleur d'un titre selon son niveau.
DOC_HEADING_COLORS = {1: COLOR_TEAL, 2: COLOR_GOLD, 3: COLOR_GOLD}

DOC_TABLE_BORDER_WIDTH = 0.5  # points

# Nombre maximal de requêtes envoyées dans un même batchUpdate.
DOC_BATCH_SIZE = 150


DOC_ZWSP = "\u200b"  # caractere invisible portant la couleur de la puce


def _rgb_from_color(color: dict) -> tuple:
    """Extrait un triplet 0..1 d'un objet OptionalColor de l'API Docs."""
    if not color:
        return ()
    rgb = color.get("color", {}).get("rgbColor", {})
    if not rgb and "rgbColor" in color:
        rgb = color["rgbColor"]
    if not rgb:
        return ()
    return (
        float(rgb.get("red", 0.0)),
        float(rgb.get("green", 0.0)),
        float(rgb.get("blue", 0.0)),
    )


def _luminance(rgb: tuple) -> float:
    """Luminance relative approchee, 0 pour le noir, 1 pour le blanc."""
    if not rgb:
        return 1.0
    r, v, b = rgb
    return 0.2126 * r + 0.7152 * v + 0.0722 * b


def _fond_soutenu(cellule: dict) -> bool:
    """Vrai si le fond de la cellule est assez sombre pour exiger du texte blanc.

    Le critere est le fond reel de la cellule, jamais la position de la ligne.
    Une ligne de total mise en couleur plus bas dans le tableau est traitee
    comme une ligne d'en-tete, ce qui est le comportement voulu.
    """
    if not cellule:
        return False
    fond = cellule.get("tableCellStyle", {}).get("backgroundColor")
    rgb = _rgb_from_color(fond)
    if not rgb:
        return False
    return _luminance(rgb) < 0.6


def _doc_color(hex_value: str) -> dict:
    return {"color": {"rgbColor": _hex_to_rgb(hex_value)}}


def _pt(value: float) -> dict:
    return {"magnitude": value, "unit": "PT"}


def _doc_text_style(
    bold: bool = False,
    italic: bool = False,
    color: str = COLOR_GREY,
    size: float = DOC_FONT_SIZE,
    link: str = "",
) -> tuple:
    """Retourne le couple style, champs attendu par updateTextStyle."""
    style: dict = {
        "weightedFontFamily": {"fontFamily": DOC_FONT, "weight": 700 if bold else 400},
        "fontSize": _pt(size),
        "bold": bold,
        "italic": italic,
        "foregroundColor": _doc_color(color),
    }
    fields = "weightedFontFamily,fontSize,bold,italic,foregroundColor"
    if link:
        style["link"] = {"url": link}
        fields += ",link"
    return style, fields


def _doc_paragraph_style(
    alignment: str = DOC_BODY_ALIGNMENT,
    named: str = "NORMAL_TEXT",
    space_before: float = DOC_SPACE_BEFORE,
    space_after: float = DOC_SPACE_AFTER,
) -> tuple:
    style = {
        "namedStyleType": named,
        "alignment": alignment,
        "lineSpacing": DOC_LINE_SPACING,
        "spaceAbove": _pt(space_before),
        "spaceBelow": _pt(space_after),
    }
    fields = "namedStyleType,alignment,lineSpacing,spaceAbove,spaceBelow"
    return style, fields


def _doc_batch(document_id: str, requests: list) -> int:
    """Envoie les requêtes par paquets, l'API limitant la taille d'un appel."""
    total = 0
    for start in range(0, len(requests), DOC_BATCH_SIZE):
        paquet = requests[start : start + DOC_BATCH_SIZE]
        if not paquet:
            continue
        _docs().documents().batchUpdate(
            documentId=document_id, body={"requests": paquet}
        ).execute()
        total += len(paquet)
    return total


def _doc_get(document_id: str) -> dict:
    return _docs().documents().get(documentId=document_id).execute()


def _doc_end_index(document: dict) -> int:
    contenu = document.get("body", {}).get("content", [])
    if not contenu:
        return 1
    return contenu[-1].get("endIndex", 1)


def _iter_paragraphs(document: dict):
    """Parcourt tous les paragraphes, cellules de tableau comprises.

    Rend un couple, le paragraphe et la cellule qui le contient, cette
    derniere valant None hors tableau. Sans cette information l'appelant
    ne peut pas distinguer un paragraphe de corps d'une cellule, et repeint
    les en-tetes de tableau en gris.
    """

    def parcourir(elements, cellule_courante):
        for element in elements:
            if "paragraph" in element:
                yield element, cellule_courante
            elif "table" in element:
                for ligne in element["table"].get("tableRows", []):
                    for cellule in ligne.get("tableCells", []):
                        yield from parcourir(cellule.get("content", []), cellule)

    yield from parcourir(document.get("body", {}).get("content", []), None)


# ------------------------------------------------ analyse du markdown

_INLINE_PATTERN = re.compile(
    r"\*\*(?P<gras>.+?)\*\*"
    r"|(?<!\*)\*(?P<italique>[^*\n]+?)\*(?!\*)"
    r"|`(?P<code>[^`\n]+?)`"
    r"|\[(?P<libelle>[^\]\n]+?)\]\((?P<url>[^)\s]+?)\)"
)

_TABLE_SEPARATOR = re.compile(r"^\|?[\s:\-|]+\|[\s:\-|]*$")


def _parse_inline(texte: str) -> tuple:
    """Sépare le texte brut des passages en gras, italique, code et liens.

    Retourne le texte débarrassé des marqueurs, et la liste des passages
    formatés avec leur position relative au début du texte.
    """
    morceaux: list = []
    passages: list = []
    position = 0
    longueur = 0

    for trouve in _INLINE_PATTERN.finditer(texte):
        if trouve.start() < position:
            continue
        litteral = texte[position : trouve.start()]
        morceaux.append(litteral)
        longueur += len(litteral)

        if trouve.group("gras") is not None:
            contenu = trouve.group("gras")
            style = {"bold": True}
        elif trouve.group("italique") is not None:
            contenu = trouve.group("italique")
            style = {"italic": True}
        elif trouve.group("code") is not None:
            contenu = trouve.group("code")
            style = {"code": True}
        else:
            contenu = trouve.group("libelle")
            style = {"link": trouve.group("url")}

        morceaux.append(contenu)
        passage = {"debut": longueur, "fin": longueur + len(contenu)}
        passage.update(style)
        passages.append(passage)
        longueur += len(contenu)
        position = trouve.end()

    morceaux.append(texte[position:])
    return "".join(morceaux), passages


def _parse_markdown(markdown: str) -> list:
    """Découpe le markdown en segments de texte et en tableaux.

    Les tableaux sont isolés parce qu'ils s'insèrent par une requête
    distincte, dont l'arithmétique d'index est différente.
    """
    segments: list = []
    courant: list = []
    lignes = markdown.replace("\r\n", "\n").split("\n")
    i = 0

    while i < len(lignes):
        brute = lignes[i]
        ligne = brute.strip()

        if not ligne:
            i += 1
            continue

        # Tableau markdown, reconnu à sa ligne de séparation.
        if (
            ligne.startswith("|")
            and i + 1 < len(lignes)
            and _TABLE_SEPARATOR.match(lignes[i + 1].strip())
        ):
            rangees = [[c.strip() for c in ligne.strip("|").split("|")]]
            i += 2
            while i < len(lignes) and lignes[i].strip().startswith("|"):
                rangees.append(
                    [c.strip() for c in lignes[i].strip().strip("|").split("|")]
                )
                i += 1
            if courant:
                segments.append({"type": "texte", "blocs": courant})
                courant = []
            segments.append({"type": "tableau", "rangees": rangees})
            continue

        titre = re.match(r"^(#{1,3})\s+(.*)$", ligne)
        if titre:
            courant.append(
                {
                    "genre": "titre",
                    "niveau": len(titre.group(1)),
                    "texte": titre.group(2).strip(),
                }
            )
            i += 1
            continue

        puce = re.match(r"^[-*+]\s+(.*)$", ligne)
        if puce:
            courant.append({"genre": "puce", "niveau": 1, "texte": puce.group(1).strip()})
            i += 1
            continue

        numero = re.match(r"^\d+[.)]\s+(.*)$", ligne)
        if numero:
            courant.append(
                {"genre": "numero", "niveau": 1, "texte": numero.group(1).strip()}
            )
            i += 1
            continue

        courant.append({"genre": "paragraphe", "niveau": 0, "texte": ligne})
        i += 1

    if courant:
        segments.append({"type": "texte", "blocs": courant})
    return segments


def _requetes_segment_texte(blocs: list, index_depart: int) -> tuple:
    """Construit les requêtes d'un segment de texte inséré d'un seul bloc.

    Tout le texte part en une seule insertion, puis les styles sont posés
    sur des plages calculées à partir des longueurs. Cette façon de faire
    évite le décalage d'index qui rend fragile une série d'insertions.
    """
    texte = ""
    metadonnees: list = []

    for bloc in blocs:
        clair, passages = _parse_inline(bloc["texte"])
        debut = len(texte)
        texte += clair + "\n"
        metadonnees.append(
            {
                "bloc": bloc,
                "debut": index_depart + debut,
                "fin": index_depart + len(texte),
                "fin_texte": index_depart + debut + len(clair),
                "passages": passages,
                "origine": index_depart + debut,
            }
        )

    requetes: list = [
        {"insertText": {"location": {"index": index_depart}, "text": texte}}
    ]

    for meta in metadonnees:
        bloc = meta["bloc"]
        genre = bloc["genre"]
        plage = {"startIndex": meta["debut"], "endIndex": meta["fin"]}

        if genre == "titre":
            niveau = bloc["niveau"]
            style_p, champs_p = _doc_paragraph_style(
                alignment=DOC_HEADING_ALIGNMENT,
                named="HEADING_{}".format(niveau),
                space_before=DOC_HEADING_SPACE_BEFORE,
            )
            style_t, champs_t = _doc_text_style(
                bold=True, color=DOC_HEADING_COLORS.get(niveau, COLOR_TEAL)
            )
        elif genre in ("puce", "numero"):
            style_p, champs_p = _doc_paragraph_style(alignment=DOC_LIST_ALIGNMENT)
            style_t, champs_t = _doc_text_style()
        else:
            style_p, champs_p = _doc_paragraph_style()
            style_t, champs_t = _doc_text_style()

        requetes.append(
            {
                "updateParagraphStyle": {
                    "range": plage,
                    "paragraphStyle": style_p,
                    "fields": champs_p,
                }
            }
        )

        if meta["fin_texte"] > meta["debut"]:
            requetes.append(
                {
                    "updateTextStyle": {
                        "range": {
                            "startIndex": meta["debut"],
                            "endIndex": meta["fin_texte"],
                        },
                        "textStyle": style_t,
                        "fields": champs_t,
                    }
                }
            )

        # Passages en gras, en italique, en code ou en lien.
        for passage in meta["passages"]:
            debut = meta["origine"] + passage["debut"]
            fin = meta["origine"] + passage["fin"]
            if fin <= debut:
                continue
            gras = passage.get("bold", False) or genre == "titre"
            couleur = COLOR_TEAL if passage.get("link") else (
                DOC_HEADING_COLORS.get(bloc["niveau"], COLOR_TEAL)
                if genre == "titre"
                else COLOR_GREY
            )
            style_passage, champs_passage = _doc_text_style(
                bold=gras,
                italic=passage.get("italic", False),
                color=couleur,
                link=passage.get("link", ""),
            )
            requetes.append(
                {
                    "updateTextStyle": {
                        "range": {"startIndex": debut, "endIndex": fin},
                        "textStyle": style_passage,
                        "fields": champs_passage,
                    }
                }
            )

    # Les puces et les numeros sont tous deux des listes natives, une
    # requete par groupe contigu, car createParagraphBullets ajoute un
    # retrait que updateParagraphStyle effacerait s'il passait ensuite.
    groupe: list = []
    genre_groupe = ""

    def vider_groupe():
        if not groupe:
            return
        preset = (
            "BULLET_DISC_CIRCLE_SQUARE"
            if genre_groupe == "puce"
            else "NUMBERED_DECIMAL_ALPHA_ROMAN"
        )
        requetes.append(
            {
                "createParagraphBullets": {
                    "range": {
                        "startIndex": groupe[0]["debut"],
                        "endIndex": groupe[-1]["fin"],
                    },
                    "bulletPreset": preset,
                }
            }
        )

    for meta in metadonnees:
        genre = meta["bloc"]["genre"]
        if genre in ("puce", "numero"):
            if genre != genre_groupe:
                vider_groupe()
                groupe = []
                genre_groupe = genre
            groupe.append(meta)
        else:
            vider_groupe()
            groupe = []
            genre_groupe = ""
    vider_groupe()

    return requetes, len(texte)


def _listes_numerotees(document: dict) -> set:
    """Identifiants des listes dont le premier niveau est numerote."""
    numerotees = set()
    for list_id, liste in (document.get("lists") or {}).items():
        niveaux = liste.get("listProperties", {}).get("nestingLevels", [])
        if not niveaux:
            continue
        if niveaux[0].get("glyphType"):
            numerotees.add(list_id)
    return numerotees


def _trouver_tableau(document: dict, index_minimal: int):
    """Retrouve le premier tableau situé à partir d'un index donné."""
    for element in document.get("body", {}).get("content", []):
        if "table" in element and element.get("startIndex", 0) >= index_minimal:
            return element
    return None


def _styler_tableau(element_tableau: dict, avec_entete: bool) -> list:
    """Construit les requêtes de mise en forme d'un tableau déjà rempli."""
    debut = element_tableau["startIndex"]
    tableau = element_tableau["table"]
    nb_lignes = len(tableau.get("tableRows", []))
    nb_colonnes = tableau.get("columns", 0)
    requetes: list = []

    bordure = {
        "color": _doc_color(COLOR_BORDER),
        "width": _pt(DOC_TABLE_BORDER_WIDTH),
        "dashStyle": "SOLID",
    }
    requetes.append(
        {
            "updateTableCellStyle": {
                "tableStartLocation": {"index": debut},
                "tableCellStyle": {
                    "borderTop": bordure,
                    "borderBottom": bordure,
                    "borderLeft": bordure,
                    "borderRight": bordure,
                    "contentAlignment": "MIDDLE",
                },
                "fields": "borderTop,borderBottom,borderLeft,borderRight,contentAlignment",
            }
        }
    )

    for indice_ligne, ligne in enumerate(tableau.get("tableRows", [])):
        entete = avec_entete and indice_ligne == 0
        if entete:
            fond = COLOR_TEAL
        elif indice_ligne % 2 == (1 if avec_entete else 0):
            fond = COLOR_ROW_ALT
        else:
            fond = ""

        if fond:
            requetes.append(
                {
                    "updateTableCellStyle": {
                        "tableRange": {
                            "tableCellLocation": {
                                "tableStartLocation": {"index": debut},
                                "rowIndex": indice_ligne,
                                "columnIndex": 0,
                            },
                            "rowSpan": 1,
                            "columnSpan": nb_colonnes,
                        },
                        "tableCellStyle": {"backgroundColor": _doc_color(fond)},
                        "fields": "backgroundColor",
                    }
                }
            )

        for cellule in ligne.get("tableCells", []):
            for contenu in cellule.get("content", []):
                if "paragraph" not in contenu:
                    continue
                plage = {
                    "startIndex": contenu["startIndex"],
                    "endIndex": contenu["endIndex"],
                }
                style_p, champs_p = _doc_paragraph_style(alignment="START")
                requetes.append(
                    {
                        "updateParagraphStyle": {
                            "range": plage,
                            "paragraphStyle": style_p,
                            "fields": champs_p,
                        }
                    }
                )
                if contenu["endIndex"] - 1 > contenu["startIndex"]:
                    style_t, champs_t = _doc_text_style(
                        bold=entete,
                        color=COLOR_WHITE if entete else COLOR_GREY,
                    )
                    requetes.append(
                        {
                            "updateTextStyle": {
                                "range": {
                                    "startIndex": contenu["startIndex"],
                                    "endIndex": contenu["endIndex"] - 1,
                                },
                                "textStyle": style_t,
                                "fields": champs_t,
                            }
                        }
                    )

    return requetes


def _supprimer_paragraphe_vide_avant(document_id: str, index: int) -> bool:
    """Retire le paragraphe vide que l'API cree au dessus d'un tableau.

    insertTable et l'insertion d'un encadre laissent systematiquement un
    paragraphe vide juste avant la structure inseree, visible comme un blanc
    supplementaire dans le PDF. Il n'est supprime que s'il est reellement vide
    et qu'il n'ouvre pas le corps du document, l'API refusant un corps qui ne
    commencerait pas par un paragraphe.
    """
    document = _doc_get(document_id)
    contenu = document.get("body", {}).get("content", [])
    for rang, element in enumerate(contenu):
        if "table" not in element:
            continue
        if element.get("startIndex", 0) < index:
            continue
        if rang == 0:
            return False
        precedent = contenu[rang - 1]
        if "paragraph" not in precedent:
            return False
        debut = precedent.get("startIndex")
        fin = precedent.get("endIndex")
        if debut is None or fin is None or fin - debut != 1:
            return False
        if debut <= 1:
            return False
        try:
            _doc_batch(
                document_id,
                [
                    {
                        "deleteContentRange": {
                            "range": {"startIndex": debut, "endIndex": fin}
                        }
                    }
                ],
            )
        except Exception:
            return False
        return True
    return False


def _inserer_tableau(
    document_id: str,
    index: int,
    rangees: list,
    avec_entete: bool = True,
) -> int:
    """Insère un tableau rempli et mis en forme, et retourne son index de fin.

    Le remplissage se fait à rebours, de la dernière cellule vers la
    première, pour que les index calculés restent valides à mesure que le
    texte s'insère.
    """
    nb_lignes = len(rangees)
    nb_colonnes = max(len(r) for r in rangees) if rangees else 0
    if nb_lignes == 0 or nb_colonnes == 0:
        return index

    _docs().documents().batchUpdate(
        documentId=document_id,
        body={
            "requests": [
                {
                    "insertTable": {
                        "location": {"index": index},
                        "rows": nb_lignes,
                        "columns": nb_colonnes,
                    }
                }
            ]
        },
    ).execute()

    document = _doc_get(document_id)
    element = _trouver_tableau(document, index)
    if element is None:
        raise RuntimeError("Tableau introuvable après insertion.")

    remplissage: list = []
    for indice_ligne in range(nb_lignes - 1, -1, -1):
        ligne_source = rangees[indice_ligne]
        cellules = element["table"]["tableRows"][indice_ligne].get("tableCells", [])
        for indice_colonne in range(nb_colonnes - 1, -1, -1):
            if indice_colonne >= len(cellules):
                continue
            valeur = (
                ligne_source[indice_colonne]
                if indice_colonne < len(ligne_source)
                else ""
            )
            if not valeur:
                continue
            clair, _ = _parse_inline(valeur)
            position = cellules[indice_colonne]["content"][0]["startIndex"]
            remplissage.append(
                {"insertText": {"location": {"index": position}, "text": clair}}
            )

    if remplissage:
        _doc_batch(document_id, remplissage)

    document = _doc_get(document_id)
    element = _trouver_tableau(document, index)
    if element is None:
        raise RuntimeError("Tableau introuvable après remplissage.")

    _doc_batch(document_id, _styler_tableau(element, avec_entete))

    _supprimer_paragraphe_vide_avant(document_id, index)

    document = _doc_get(document_id)
    element = _trouver_tableau(document, index)
    return element["endIndex"] if element else index


def _ecrire_segments(document_id: str, segments: list, index_depart: int) -> int:
    """Écrit une suite de segments à partir d'un index, et retourne l'index final."""
    curseur = index_depart
    for segment in segments:
        if segment["type"] == "texte":
            requetes, longueur = _requetes_segment_texte(segment["blocs"], curseur)
            _doc_batch(document_id, requetes)
            curseur += longueur
        else:
            curseur = _inserer_tableau(document_id, curseur, segment["rangees"])
    return curseur


# ------------------------------------------------ outils Docs, lecture

@mcp.tool
@tolerant
def search_documents(name: str = "", limit: int = 20) -> Any:
    """Recherche des documents Google Docs par nom.

    name  : fragment du nom recherché, vide pour les plus récents
    limit : nombre maximum de résultats
    """
    query = "mimeType='application/vnd.google-apps.document' and trashed=false"
    if name:
        query += " and name contains '{}'".format(name.replace("'", "\\'"))
    result = (
        _drive()
        .files()
        .list(
            q=query,
            pageSize=limit,
            orderBy="modifiedTime desc",
            fields="files(id,name,webViewLink,modifiedTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    return result.get("files", [])


@mcp.tool
@tolerant
def read_document_text(document_id: str) -> Any:
    """Lit le texte d'un document, sans les index ni les styles.

    À préférer quand il s'agit seulement de savoir ce que dit le document.
    Pour le modifier finement, utiliser get_document, qui donne les index.
    """
    document = _doc_get(document_id)
    lignes: list = []

    def texte_paragraphe(paragraphe: dict) -> str:
        morceaux = []
        for element in paragraphe.get("elements", []):
            morceaux.append(element.get("textRun", {}).get("content", ""))
        return "".join(morceaux).replace(DOC_ZWSP, "").rstrip("\n")

    def parcourir(elements, dans_tableau=False):
        for element in elements:
            if "paragraph" in element:
                contenu = texte_paragraphe(element["paragraph"])
                if contenu.strip():
                    prefixe = "    " if dans_tableau else ""
                    lignes.append(prefixe + contenu)
            elif "table" in element:
                lignes.append("[tableau]")
                for ligne in element["table"].get("tableRows", []):
                    cellules = []
                    for cellule in ligne.get("tableCells", []):
                        textes = []
                        for sous in cellule.get("content", []):
                            if "paragraph" in sous:
                                textes.append(texte_paragraphe(sous["paragraph"]))
                        cellules.append(" ".join(t for t in textes if t).strip())
                    lignes.append(" | ".join(cellules))

    parcourir(document.get("body", {}).get("content", []))
    return {
        "titre": document.get("title"),
        "document_id": document_id,
        "texte": "\n".join(lignes),
    }


@mcp.tool
@tolerant
def get_document(document_id: str, with_text: bool = True) -> Any:
    """Retourne la structure du document avec les index de chaque paragraphe.

    Les index sont indispensables pour insert_text, delete_range et
    format_range_doc. Ils changent à chaque modification, donc il faut
    relire le document avant toute série d'opérations chirurgicales.

    with_text : si faux, ne retourne que les index et les styles
    """
    document = _doc_get(document_id)
    paragraphes: list = []

    listes_numerotees = _listes_numerotees(document)

    for element, cellule in _iter_paragraphs(document):
        paragraphe = element["paragraph"]
        contenu = "".join(
            sous.get("textRun", {}).get("content", "")
            for sous in paragraphe.get("elements", [])
        )
        puce = paragraphe.get("bullet")
        if puce is None:
            genre_liste = ""
        elif puce.get("listId") in listes_numerotees:
            genre_liste = "numero"
        else:
            genre_liste = "puce"
        entree = {
            "debut": element.get("startIndex"),
            "fin": element.get("endIndex"),
            "style": paragraphe.get("paragraphStyle", {}).get(
                "namedStyleType", "NORMAL_TEXT"
            ),
            "liste": puce is not None,
            "genre_liste": genre_liste,
            "dans_tableau": cellule is not None,
        }
        if with_text:
            entree["texte"] = contenu.replace(DOC_ZWSP, "").rstrip("\n")
        paragraphes.append(entree)

    tableaux = []
    for element in document.get("body", {}).get("content", []):
        if "table" in element:
            tableaux.append(
                {
                    "debut": element.get("startIndex"),
                    "fin": element.get("endIndex"),
                    "lignes": len(element["table"].get("tableRows", [])),
                    "colonnes": element["table"].get("columns"),
                }
            )

    return {
        "titre": document.get("title"),
        "document_id": document_id,
        "index_fin": _doc_end_index(document),
        "paragraphes": paragraphes,
        "tableaux": tableaux,
    }


# ------------------------------------------------ outils Docs, création

@mcp.tool
@tolerant
def create_document(
    title: str,
    folder_id: str = "",
    from_template: bool = True,
    template_id: str = "",
) -> Any:
    """Crée un document Google Docs.

    Par défaut, le document est une copie du gabarit institutionnel Modèle
    vierge, dont il hérite l'en-tête, le pied de page et les marges. C'est
    le comportement voulu par la charte, qui interdit de reconstruire le
    bandeau et le pied à la main.

    from_template : mettre à faux pour un document réellement vierge
    template_id   : pour partir d'un autre gabarit que celui par défaut
    """
    if from_template:
        source = template_id or DOC_TEMPLATE_ID
        corps: dict = {"name": title}
        if folder_id:
            corps["parents"] = [folder_id]
        # supportsAllDrives est indispensable : le gabarit est dans un Drive
        # partage. Sans ce parametre, files.copy renvoie 404 sur un fichier
        # que l'API Docs lit pourtant sans difficulte.
        copie = (
            _drive()
            .files()
            .copy(
                fileId=source,
                body=corps,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        return {
            "document_id": copie["id"],
            "titre": copie.get("name"),
            "url": copie.get("webViewLink"),
            "gabarit": source,
        }

    cree = _docs().documents().create(body={"title": title}).execute()
    file_id = cree["documentId"]
    if folder_id:
        actuel = (
            _drive()
            .files()
            .get(fileId=file_id, fields="parents", supportsAllDrives=True)
            .execute()
        )
        _drive().files().update(
            fileId=file_id,
            addParents=folder_id,
            removeParents=",".join(actuel.get("parents", [])),
            fields="id,parents",
            supportsAllDrives=True,
        ).execute()
    return {
        "document_id": file_id,
        "titre": title,
        "url": "https://docs.google.com/document/d/{}/edit".format(file_id),
        "gabarit": None,
    }


@mcp.tool
@tolerant
def clear_document(document_id: str) -> Any:
    """Vide le corps du document sans toucher à l'en-tête ni au pied de page.

    Utile juste après create_document, pour retirer le contenu de
    démonstration du gabarit avant d'écrire le vrai contenu.
    """
    document = _doc_get(document_id)
    fin = _doc_end_index(document)
    if fin <= 2:
        return {"vide": True, "caracteres_supprimes": 0}
    _doc_batch(
        document_id,
        [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": fin - 1}}}],
    )
    return {"vide": True, "caracteres_supprimes": fin - 2}


# ------------------------------------------------ outils Docs, écriture

@mcp.tool
@tolerant
def write_markdown(document_id: str, markdown: str, clear_first: bool = True) -> Any:
    """Écrit un document entier à partir de markdown, à la charte Almaval.

    Reconnaît les titres de niveau un à trois, les paragraphes, les listes
    à puces et numérotées, les tableaux, ainsi que le gras, l'italique, le
    code et les liens.

    La mise en forme suit la charte, Manjari partout, taille unique, titres
    distingués par la graisse et la couleur, teal pour les chapitres, or
    pour les sous chapitres, gris pour le texte, justifié, interligne une
    fois et demie, espace avant et après chaque paragraphe.

    clear_first : vide le corps avant d'écrire. À laisser vrai sur une
                  copie fraîche du gabarit, à mettre à faux pour préserver
                  un contenu existant.
    """
    if clear_first:
        document = _doc_get(document_id)
        fin = _doc_end_index(document)
        if fin > 2:
            _doc_batch(
                document_id,
                [
                    {
                        "deleteContentRange": {
                            "range": {"startIndex": 1, "endIndex": fin - 1}
                        }
                    }
                ],
            )

    segments = _parse_markdown(markdown)
    if not segments:
        return {"erreur": "Aucun contenu à écrire."}

    fin = _ecrire_segments(document_id, segments, 1)
    return {
        "document_id": document_id,
        "segments_ecrits": len(segments),
        "index_fin": fin,
        "url": "https://docs.google.com/document/d/{}/edit".format(document_id),
    }


@mcp.tool
@tolerant
def append_markdown(document_id: str, markdown: str) -> Any:
    """Ajoute du markdown à la suite du contenu existant, à la charte Almaval."""
    segments = _parse_markdown(markdown)
    if not segments:
        return {"erreur": "Aucun contenu à ajouter."}
    document = _doc_get(document_id)
    depart = max(1, _doc_end_index(document) - 1)
    fin = _ecrire_segments(document_id, segments, depart)
    return {
        "document_id": document_id,
        "segments_ajoutes": len(segments),
        "index_depart": depart,
        "index_fin": fin,
    }


@mcp.tool
@tolerant
def replace_text(
    document_id: str,
    find: str,
    replace: str,
    match_case: bool = True,
) -> Any:
    """Remplace toutes les occurrences d'un texte dans tout le document.

    Fonctionne aussi dans les en-têtes, les pieds de page et les tableaux,
    ce qui en fait l'outil de choix pour remplir un gabarit à champs.

    Une limite à connaître. Remplacer par une chaîne vide vide le
    paragraphe sans le supprimer, il reste un paragraphe blanc.
    """
    reponse = (
        _docs()
        .documents()
        .batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": find, "matchCase": match_case},
                            "replaceText": replace,
                        }
                    }
                ]
            },
        )
        .execute()
    )
    occurrences = 0
    for reponse_unitaire in reponse.get("replies", []):
        occurrences += reponse_unitaire.get("replaceAllText", {}).get(
            "occurrencesChanged", 0
        )
    return {"occurrences_remplacees": occurrences}


@mcp.tool
@tolerant
def insert_text(document_id: str, index: int, text: str, styled: bool = True) -> Any:
    """Insère du texte brut à un index précis.

    Les index se lisent avec get_document et changent à chaque modification.
    Pour écrire du contenu structuré, préférer write_markdown ou
    append_markdown, qui calculent les index eux-mêmes.

    styled : applique la charte au texte inséré
    """
    requetes: list = [
        {"insertText": {"location": {"index": index}, "text": text}}
    ]
    if styled and text:
        style_p, champs_p = _doc_paragraph_style()
        style_t, champs_t = _doc_text_style()
        plage = {"startIndex": index, "endIndex": index + len(text)}
        requetes.append(
            {
                "updateParagraphStyle": {
                    "range": plage,
                    "paragraphStyle": style_p,
                    "fields": champs_p,
                }
            }
        )
        requetes.append(
            {"updateTextStyle": {"range": plage, "textStyle": style_t, "fields": champs_t}}
        )
    _doc_batch(document_id, requetes)
    return {"insere": len(text), "index": index}


@mcp.tool
@tolerant
def delete_range(document_id: str, start_index: int, end_index: int) -> Any:
    """Supprime un intervalle de contenu. Opération irréversible.

    Les index se lisent avec get_document. La borne de fin est exclue.
    """
    _doc_batch(
        document_id,
        [
            {
                "deleteContentRange": {
                    "range": {"startIndex": start_index, "endIndex": end_index}
                }
            }
        ],
    )
    return {"supprime": end_index - start_index}


@mcp.tool
@tolerant
def format_range_doc(
    document_id: str,
    start_index: int,
    end_index: int,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    color_hex: str = "",
    font_size: float = 0,
    alignment: str = "",
    heading: str = "",
    link: str = "",
) -> Any:
    """Applique une mise en forme à un intervalle précis.

    alignment : 'START', 'CENTER', 'END' ou 'JUSTIFIED'
    heading   : 'NORMAL_TEXT', 'HEADING_1', 'HEADING_2', 'HEADING_3'
    color_hex : couleur du texte, par exemple '#128DA0'
    """
    plage = {"startIndex": start_index, "endIndex": end_index}
    requetes: list = []

    style_p: dict = {}
    champs_p: list = []
    if heading:
        style_p["namedStyleType"] = heading
        champs_p.append("namedStyleType")
    if alignment:
        style_p["alignment"] = alignment
        champs_p.append("alignment")
    if champs_p:
        style_p["lineSpacing"] = DOC_LINE_SPACING
        style_p["spaceAbove"] = _pt(DOC_SPACE_BEFORE)
        style_p["spaceBelow"] = _pt(DOC_SPACE_AFTER)
        champs_p += ["lineSpacing", "spaceAbove", "spaceBelow"]
        requetes.append(
            {
                "updateParagraphStyle": {
                    "range": plage,
                    "paragraphStyle": style_p,
                    "fields": ",".join(champs_p),
                }
            }
        )

    style_t: dict = {}
    champs_t: list = []
    if bold is not None:
        style_t["bold"] = bold
        style_t["weightedFontFamily"] = {
            "fontFamily": DOC_FONT,
            "weight": 700 if bold else 400,
        }
        champs_t += ["bold", "weightedFontFamily"]
    if italic is not None:
        style_t["italic"] = italic
        champs_t.append("italic")
    if color_hex:
        style_t["foregroundColor"] = _doc_color(color_hex)
        champs_t.append("foregroundColor")
    if font_size:
        style_t["fontSize"] = _pt(font_size)
        champs_t.append("fontSize")
    if link:
        style_t["link"] = {"url": link}
        champs_t.append("link")
    if champs_t:
        requetes.append(
            {
                "updateTextStyle": {
                    "range": plage,
                    "textStyle": style_t,
                    "fields": ",".join(champs_t),
                }
            }
        )

    if not requetes:
        return {"erreur": "Aucune propriété de mise en forme fournie."}

    _doc_batch(document_id, requetes)
    return {"mise_en_forme_appliquee": len(requetes)}


@mcp.tool
@tolerant
def insert_table_doc(
    document_id: str,
    index: int,
    rows: list,
    header: bool = True,
) -> Any:
    """Insère un tableau à la charte Almaval à un index précis.

    rows   : liste de listes, la première étant l'en-tête si header est vrai
    header : en-tête à fond teal, texte blanc en gras

    Fond gris très clair une ligne sur deux, bordures fines, contenu en
    Manjari gris à la taille unique.
    """
    fin = _inserer_tableau(document_id, index, rows, header)
    return {
        "document_id": document_id,
        "lignes": len(rows),
        "colonnes": max(len(r) for r in rows) if rows else 0,
        "index_fin": fin,
    }


@mcp.tool
@tolerant
def insert_callout(document_id: str, index: int, text: str) -> Any:
    """Insère un encadré, cellule unique à fond bleu pâle sur toute la largeur.

    L'encadré met en évidence un point important ou un garde-fou.
    """
    fin = _inserer_tableau(document_id, index, [[text]], avec_entete=False)
    document = _doc_get(document_id)
    element = _trouver_tableau(document, index)
    if element is not None:
        _doc_batch(
            document_id,
            [
                {
                    "updateTableCellStyle": {
                        "tableStartLocation": {"index": element["startIndex"]},
                        "tableCellStyle": {
                            "backgroundColor": _doc_color(COLOR_CALLOUT)
                        },
                        "fields": "backgroundColor",
                    }
                }
            ],
        )
    return {"document_id": document_id, "index_fin": fin}


@mcp.tool
@tolerant
def insert_page_break(document_id: str, index: int) -> Any:
    """Insère un saut de page à un index précis."""
    _doc_batch(
        document_id, [{"insertPageBreak": {"location": {"index": index}}}]
    )
    return {"saut_de_page": True, "index": index}


# ------------------------------------------------ outils Docs, charte

@mcp.tool
@tolerant
def apply_house_style_doc(document_id: str, alignment: str = DOC_BODY_ALIGNMENT) -> Any:
    """Applique la charte de mise en forme Almaval à tout un document existant.

    Manjari partout, taille unique, gris pour le texte, teal pour les titres
    de chapitre, or pour les sous chapitres et le niveau trois, justifié,
    interligne une fois et demie, espace avant et après chaque paragraphe,
    y compris dans les cellules de tableau et les listes.

    L'en-tête et le pied de page, hérités du gabarit, ne sont pas touchés.
    """
    document = _doc_get(document_id)
    requetes: list = []
    titres = 0
    paragraphes = 0

    cellules_soutenues = 0

    for element, cellule in _iter_paragraphs(document):
        debut = element.get("startIndex")
        fin = element.get("endIndex")
        if debut is None or fin is None or fin <= debut:
            continue
        style_nomme = (
            element["paragraph"].get("paragraphStyle", {}).get("namedStyleType", "")
        )
        plage = {"startIndex": debut, "endIndex": fin}

        if cellule is not None:
            # Dans un tableau, c'est le fond de la cellule qui decide, jamais
            # la position de la ligne. Un fond soutenu impose du texte blanc,
            # sinon l'en-tete teal devient gris sur teal et ne se lit plus.
            soutenu = _fond_soutenu(cellule)
            if soutenu:
                cellules_soutenues += 1
            style_p, champs_p = _doc_paragraph_style(alignment="START")
            style_t, champs_t = _doc_text_style(
                bold=soutenu,
                color=COLOR_WHITE if soutenu else COLOR_GREY,
            )
            paragraphes += 1
        elif style_nomme.startswith("HEADING_"):
            try:
                niveau = int(style_nomme.split("_")[1])
            except (IndexError, ValueError):
                niveau = 1
            style_p, champs_p = _doc_paragraph_style(
                alignment=DOC_HEADING_ALIGNMENT,
                named=style_nomme,
                space_before=DOC_HEADING_SPACE_BEFORE,
            )
            style_t, champs_t = _doc_text_style(
                bold=True, color=DOC_HEADING_COLORS.get(niveau, COLOR_TEAL)
            )
            titres += 1
        else:
            style_p, champs_p = _doc_paragraph_style(alignment=alignment)
            style_t, champs_t = _doc_text_style()
            paragraphes += 1

        requetes.append(
            {
                "updateParagraphStyle": {
                    "range": plage,
                    "paragraphStyle": style_p,
                    "fields": champs_p,
                }
            }
        )

        if fin - 1 > debut:
            requetes.append(
                {
                    "updateTextStyle": {
                        "range": {"startIndex": debut, "endIndex": fin - 1},
                        "textStyle": style_t,
                        "fields": champs_t,
                    }
                }
            )

    if not requetes:
        return {"erreur": "Document vide, rien à mettre en forme."}

    envoyees = _doc_batch(document_id, requetes)

    return {
        "requetes_envoyees": envoyees,
        "titres": titres,
        "paragraphes": paragraphes,
        "cellules_sur_fond_soutenu": cellules_soutenues,
    }


@mcp.tool
@tolerant
def export_pdf(document_id: str, name: str = "", folder_id: str = "") -> Any:
    """Exporte un document en PDF et dépose le fichier sur le Drive.

    name      : nom du PDF, sans extension. Par défaut, le titre du document
    folder_id : dossier de destination, par défaut la racine du Drive
    """
    meta = (
        _drive()
        .files()
        .get(fileId=document_id, fields="name", supportsAllDrives=True)
        .execute()
    )
    titre = name or meta.get("name", "document")
    donnees = (
        _drive()
        .files()
        .export(fileId=document_id, mimeType="application/pdf")
        .execute()
    )
    media = MediaIoBaseUpload(
        io.BytesIO(donnees), mimetype="application/pdf", resumable=False
    )
    corps: dict = {"name": "{}.pdf".format(titre), "mimeType": "application/pdf"}
    if folder_id:
        corps["parents"] = [folder_id]
    cree = (
        _drive()
        .files()
        .create(
            body=corps,
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    return {
        "pdf_id": cree["id"],
        "nom": cree.get("name"),
        "url": cree.get("webViewLink"),
    }


@mcp.tool
@tolerant
def batch_update_doc(document_id: str, requests: list) -> Any:
    """Exécute des requêtes batchUpdate brutes de l'API Docs.

    À n'utiliser que pour les opérations non couvertes par les autres
    outils : images, notes de bas de page, colonnes, sections, en-têtes
    et pieds de page personnalisés, signets.
    """
    reponse = (
        _docs()
        .documents()
        .batchUpdate(documentId=document_id, body={"requests": requests})
        .execute()
    )
    return {
        "requetes_executees": len(requests),
        "reponses": reponse.get("replies", []),
    }


# ================================================================
#  APPS SCRIPT
#  Identité distincte : jeton utilisateur, car l'API Apps Script
#  ne fonctionne pas avec les comptes de service.
# ================================================================

SCRIPT_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/script.triggers",
]


def _user_credentials():
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    missing = [
        name
        for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Variables absentes : " + ", ".join(missing))
    return UserCredentials(
        None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCRIPT_SCOPES,
    )


def _script():
    if "script" not in _services:
        _services["script"] = build(
            "script", "v1", credentials=_user_credentials(), cache_discovery=False
        )
    return _services["script"]


# Ces scopes sont déclarés dans le manifeste de chaque projet créé par le
# serveur. Ils ne suppriment pas l'autorisation manuelle, qui reste à donner
# une fois par projet depuis l'éditeur, mais ils la rendent complète du
# premier coup : inutile de la redonner quand une action nouvelle touche un
# service qui n'avait pas encore été consenti.
PROJECT_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/script.external_request",
    "https://www.googleapis.com/auth/script.scriptapp",
    "https://www.googleapis.com/auth/userinfo.email",
]

DEFAULT_MANIFEST = {
    "timeZone": "Europe/Zurich",
    "dependencies": {},
    "exceptionLogging": "STACKDRIVER",
    "runtimeVersion": "V8",
    "oauthScopes": PROJECT_OAUTH_SCOPES,
}

# Accès par défaut des applications web publiées par le serveur.
# MYSELF, et non ANYONE_ANONYMOUS : run_web_app présente un jeton Google du
# compte propriétaire, l'accès réservé suffit donc et l'endpoint reste
# inatteignable depuis l'extérieur. N'ouvrir en anonyme qu'au cas par cas,
# via le paramètre access de write_script_file, pour un webhook public.
DEFAULT_WEBAPP_ACCESS = "MYSELF"


def _merge_manifest(existing_source: str, webapp: bool, access: str) -> str:
    """Fusionne le manifeste existant au lieu de l'écraser.

    Sans cette fusion, chaque écriture de fichier remettait le manifeste à
    zéro et effaçait notamment les oauthScopes déclarés.
    """
    manifest: dict = {}
    if existing_source:
        try:
            manifest = json.loads(existing_source)
        except ValueError:
            manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}

    for key, value in DEFAULT_MANIFEST.items():
        manifest.setdefault(key, value)

    if webapp:
        manifest["webapp"] = {"access": access, "executeAs": "USER_DEPLOYING"}

    return json.dumps(manifest, indent=2, ensure_ascii=False)


@mcp.tool
@tolerant
def list_script_projects(name: str = "", limit: int = 20) -> Any:
    """Liste les projets Apps Script du Drive.

    name  : fragment du nom recherché, vide pour les plus récents
    limit : nombre maximum de résultats
    """
    query = "mimeType='application/vnd.google-apps.script' and trashed=false"
    if name:
        query += " and name contains '{}'".format(name.replace("'", "\\'"))
    result = (
        _drive()
        .files()
        .list(
            q=query,
            pageSize=limit,
            orderBy="modifiedTime desc",
            fields="files(id,name,modifiedTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    return result.get("files", [])


@mcp.tool
@tolerant
def find_bound_script(file_id: str) -> Any:
    """Cherche un script Apps Script lié (container-bound) à un fichier Drive.

    file_id : ID du Google Sheets/Docs/Forms dont on cherche le script associé.

    LIMITATION CONFIRMÉE (test du 28.08.2026) : Google n'expose pas la relation
    container -> script bound via Drive.files.list, quel que soit l'âge du
    script. Vérifié empiriquement sur "Almaval - Patients" / script "Almaval -
    Gestion des Admissions" (créé en 2023, donc pas un cas "ancien") : le
    fichier script Drive n'a AUCUN champ parents renseigné, alors que le
    fichier conteneur en a un. Ce n'est pas un bug de ce connecteur ni un
    problème de Drive partagé / paramètre corpora : Google ne matérialise
    tout simplement pas ce lien dans le graphe parent/enfant de Drive, pour
    aucune API publique (Drive, Sheets, Apps Script).

    Cette fonction reste disponible à titre indicatif (elle peut
    ponctuellement trouver un résultat), mais une liste vide n'est PAS un
    indice fiable d'absence de script bound — ne pas la considérer comme
    un test d'exhaustivité.

    Méthode fiable à 100% : récupérer l'ID du projet manuellement depuis
    l'éditeur Apps Script (Extensions > Apps Script > icône engrenage >
    Paramètres du projet > ID du projet Apps Script), puis utiliser
    get_script_content(script_id).
    """
    query = (
        "'{}' in parents and mimeType='application/vnd.google-apps.script' "
        "and trashed=false"
    ).format(file_id.replace("'", "\\'"))
    result = (
        _drive()
        .files()
        .list(
            q=query,
            pageSize=10,
            fields="files(id,name,modifiedTime,parents)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    fichiers = result.get("files", [])
    return {
        "file_id_recherche": file_id,
        "scripts_trouves": fichiers,
        "note": (
            "Limitation confirmée de l'API Drive (pas un bug de ce "
            "connecteur) : la relation container -> script bound n'est pas "
            "exposée par Drive.files.list, indépendamment de l'âge du "
            "script (vérifié sur un script créé en 2023, le 28.08.2026). "
            "Liste vide = aucun indice fiable, ni dans un sens ni dans "
            "l'autre. Seule la récupération manuelle de l'ID depuis "
            "l'éditeur Apps Script (Paramètres du projet) fonctionne de "
            "façon fiable."
        ),
    }


@mcp.tool
@tolerant
def create_script_project(title: str, parent_id: str = "") -> Any:
    """Crée un projet Apps Script.

    parent_id : identifiant d'un classeur ou document pour créer un script
                lié à ce fichier. Vide pour un script autonome.

    Rappel : un projet neuf devra être autorisé une fois manuellement depuis
    l'éditeur, en exécutant n'importe quelle fonction et en acceptant les
    permissions. Sans cela l'application web répondra « Authorization needed ».
    """
    body: dict = {"title": title}
    if parent_id:
        body["parentId"] = parent_id
    created = _script().projects().create(body=body).execute()
    return {
        "script_id": created["scriptId"],
        "titre": created.get("title"),
        "url": "https://script.google.com/d/{}/edit".format(created["scriptId"]),
        "rappel": (
            "Autoriser une fois le projet depuis l'éditeur avant d'appeler "
            "run_web_app."
        ),
    }


@mcp.tool
@tolerant
def get_script_content(script_id: str) -> Any:
    """Lit le contenu complet d'un projet Apps Script.

    Retourne chaque fichier avec son nom, son type et son code source.
    """
    content = _script().projects().getContent(scriptId=script_id).execute()
    return {
        "script_id": content.get("scriptId"),
        "fichiers": [
            {
                "nom": f.get("name"),
                "type": f.get("type"),
                "source": f.get("source", ""),
            }
            for f in content.get("files", [])
        ],
    }


@mcp.tool
@tolerant
def write_script_file(
    script_id: str,
    filename: str,
    source: str,
    file_type: str = "SERVER_JS",
    webapp: bool = False,
    access: str = DEFAULT_WEBAPP_ACCESS,
) -> Any:
    """Écrit ou remplace un fichier dans un projet Apps Script.

    Les autres fichiers du projet sont conservés tels quels, et le manifeste
    est fusionné plutôt qu'écrasé : les clés déjà présentes survivent.

    filename  : nom sans extension, par exemple 'Code'
    file_type : 'SERVER_JS' pour du code, 'HTML' pour une page
    webapp    : si vrai, le manifeste est configuré pour une publication
                en application web exécutée sous ton identité
    access    : 'MYSELF' par défaut, l'appel de run_web_app étant authentifié.
                'ANYONE_ANONYMOUS' n'est à utiliser que pour un webhook
                réellement public, appelé par un tiers sans jeton Google.
    """
    current = _script().projects().getContent(scriptId=script_id).execute()
    files = current.get("files", [])

    # Cas particulier : on écrit directement le manifeste, il passe tel quel.
    if filename == "appsscript":
        kept = [f for f in files if f.get("name") != "appsscript"]
        kept.append({"name": "appsscript", "type": "JSON", "source": source})
        _script().projects().updateContent(
            scriptId=script_id, body={"files": kept}
        ).execute()
        return {
            "script_id": script_id,
            "fichier_ecrit": "appsscript",
            "fichiers_totaux": len(kept),
        }

    existing_manifest = ""
    for f in files:
        if f.get("name") == "appsscript":
            existing_manifest = f.get("source", "")

    kept = [f for f in files if f.get("name") not in (filename, "appsscript")]
    kept.append(
        {
            "name": "appsscript",
            "type": "JSON",
            "source": _merge_manifest(existing_manifest, webapp, access),
        }
    )
    kept.append({"name": filename, "type": file_type, "source": source})

    _script().projects().updateContent(
        scriptId=script_id, body={"files": kept}
    ).execute()
    return {
        "script_id": script_id,
        "fichier_ecrit": filename,
        "fichiers_totaux": len(kept),
        "acces_webapp": access if webapp else None,
    }


@mcp.tool
@tolerant
def deploy_web_app(script_id: str, description: str = "Déploiement Claude") -> Any:
    """Crée une version et la publie en application web.

    Retourne l'URL d'exécution, à utiliser ensuite avec run_web_app.
    Le manifeste doit avoir été configuré avec webapp=True.
    """
    version = (
        _script()
        .projects()
        .versions()
        .create(scriptId=script_id, body={"description": description})
        .execute()
    )
    deployment = (
        _script()
        .projects()
        .deployments()
        .create(
            scriptId=script_id,
            body={
                "versionNumber": version["versionNumber"],
                "manifestFileName": "appsscript",
                "description": description,
            },
        )
        .execute()
    )
    url = ""
    for entry in deployment.get("entryPoints", []):
        if entry.get("entryPointType") == "WEB_APP":
            url = entry.get("webApp", {}).get("url", "")
    return {
        "deployment_id": deployment.get("deploymentId"),
        "version": version["versionNumber"],
        "url": url,
    }


@mcp.tool
@tolerant
def list_deployments(script_id: str) -> Any:
    """Liste les déploiements existants d'un projet Apps Script."""
    result = _script().projects().deployments().list(scriptId=script_id).execute()
    sorties = []
    for dep in result.get("deployments", []):
        url = ""
        for entry in dep.get("entryPoints", []):
            if entry.get("entryPointType") == "WEB_APP":
                url = entry.get("webApp", {}).get("url", "")
        sorties.append(
            {
                "deployment_id": dep.get("deploymentId"),
                "version": dep.get("deploymentConfig", {}).get("versionNumber"),
                "description": dep.get("deploymentConfig", {}).get("description"),
                "url": url,
            }
        )
    return sorties


@mcp.tool
@tolerant
def run_web_app(url: str, payload: Optional[dict] = None, timeout: int = 120) -> Any:
    """Exécute une application web Apps Script déjà déployée.

    L'appel est authentifié avec ton identité Google, ce qui permet à
    l'application de rester publiée en accès réservé. Le secret partagé est
    ajouté en plus, depuis la variable SCRIPT_SHARED_SECRET.

    Si la réponse contient « Authorization needed », c'est que le projet n'a
    jamais été autorisé depuis l'éditeur. Cette autorisation est manuelle,
    vaut pour un projet donné et ne peut pas être donnée par l'API.

    url     : l'URL retournée par deploy_web_app
    payload : dictionnaire transmis au script
    """
    body = dict(payload or {})
    secret = os.environ.get("SCRIPT_SHARED_SECRET", "")
    if secret:
        body["secret"] = secret

    creds = _user_credentials()
    creds.refresh(GoogleAuthRequest())
    headers = {"Authorization": "Bearer " + creds.token}

    response = requests.post(
        url, json=body, headers=headers, timeout=timeout, allow_redirects=False
    )

    # Une application web redirige vers googleusercontent.com. L'en-tête
    # d'autorisation ne doit pas suivre la redirection, l'URL cible portant
    # déjà son propre jeton.
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("location", "")
        if location:
            response = requests.get(location, timeout=timeout)

    try:
        return {"statut": response.status_code, "reponse": response.json()}
    except ValueError:
        texte = response.text[:2000]
        if "Authorization needed" in response.text:
            return {
                "statut": response.status_code,
                "erreur": "Projet non autorisé.",
                "detail": (
                    "Ouvrir le projet dans l'éditeur Apps Script, exécuter une "
                    "fonction et accepter les permissions, puis relancer."
                ),
            }
        return {"statut": response.status_code, "reponse": texte}


# ---------------------------------------------------------------- démarrage

class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Refuse toute requête ne portant pas la clé attendue.

    La clé est acceptée sous deux formes, au choix du client :
        Authorization: Bearer <cle>
        X-API-Key: <cle>

    Si MCP_API_KEY n'est pas définie, le contrôle est désactivé et le
    serveur reste ouvert. À n'utiliser que le temps d'un test.
    """

    async def dispatch(self, request, call_next):
        expected = os.environ.get("MCP_API_KEY", "")
        if expected:
            header = request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                presented = header[7:].strip()
            else:
                presented = request.headers.get("x-api-key", "").strip()
            if not hmac.compare_digest(presented, expected):
                return JSONResponse(
                    {"error": "unauthorized", "detail": "Clé API absente ou invalide."},
                    status_code=401,
                )
        return await call_next(request)


app = mcp.http_app(
    path=os.environ.get("MCP_PATH", "/mcp"),
    middleware=[Middleware(ApiKeyMiddleware)],
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
