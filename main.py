"""
Serveur MCP Google Sheets — Almaval / Claude
=============================================

Authentification Google : compte de service avec délégation au niveau du domaine.
Le serveur agit sous l'identité de l'utilisateur défini par IMPERSONATE_USER,
avec exactement ses droits, ni plus ni moins.

Authentification du connecteur : clé API portée par un en-tête de requête.

Variables d'environnement attendues :
    GOOGLE_SERVICE_ACCOUNT_JSON   contenu intégral du fichier JSON de la clé
    IMPERSONATE_USER              ex. am.forte@almaval.ch
    MCP_API_KEY                   clé attendue dans l'en-tête des requêtes
    MCP_PATH                      chemin d'écoute, ex. /mcp-sheets-a7f3d91c4e2b
    PORT                          fourni automatiquement par Railway
"""

import functools
import hmac
import json
import os
from typing import Any, Optional

import uvicorn
from fastmcp import FastMCP
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
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
def update_cell(spreadsheet_id: str, cell_a1: str, value: Any) -> Any:
    """Écrit une seule cellule, par exemple 'Feuille 1!C7'."""
    return update_values(spreadsheet_id, cell_a1, [[value]])


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
        current = _drive().files().get(fileId=file_id, fields="parents").execute()
        _drive().files().update(
            fileId=file_id,
            addParents=folder_id,
            removeParents=",".join(current.get("parents", [])),
            fields="id,parents",
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
