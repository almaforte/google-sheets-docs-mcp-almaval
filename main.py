"""
Serveur MCP Google Sheets — Almaval / Claude
=============================================

Authentification : compte de service avec délégation au niveau du domaine.
Le serveur agit sous l'identité de l'utilisateur défini par IMPERSONATE_USER,
avec exactement ses droits, ni plus ni moins.

Variables d'environnement attendues :
    GOOGLE_SERVICE_ACCOUNT_JSON   contenu intégral du fichier JSON de la clé
    IMPERSONATE_USER              ex. am.forte@almaval.ch
    MCP_PATH                      chemin d'écoute, ex. /mcp-sheets-8f3a91  (défaut /mcp)
    PORT                          fourni automatiquement par Railway
"""

import functools
import hmac
import json
import os
from typing import Any, Optional

import requests
import uvicorn
from fastmcp import FastMCP
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
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


# ---------------------------------------------------------------- démarrage

# ================================================================
#  APPS SCRIPT
#  Identité distincte : jeton utilisateur, car l'API Apps Script
#  ne fonctionne pas avec les comptes de service.
# ================================================================

SCRIPT_SCOPES = [
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


DEFAULT_MANIFEST = {
    "timeZone": "Europe/Zurich",
    "dependencies": {},
    "exceptionLogging": "STACKDRIVER",
    "runtimeVersion": "V8",
}

WEBAPP_MANIFEST = dict(
    DEFAULT_MANIFEST,
    webapp={"access": "ANYONE_ANONYMOUS", "executeAs": "USER_DEPLOYING"},
)


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
def create_script_project(title: str, parent_id: str = "") -> Any:
    """Crée un projet Apps Script.

    parent_id : identifiant d'un classeur ou document pour créer un script
                lié à ce fichier. Vide pour un script autonome.
    """
    body: dict = {"title": title}
    if parent_id:
        body["parentId"] = parent_id
    created = _script().projects().create(body=body).execute()
    return {
        "script_id": created["scriptId"],
        "titre": created.get("title"),
        "url": "https://script.google.com/d/{}/edit".format(created["scriptId"]),
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
) -> Any:
    """Écrit ou remplace un fichier dans un projet Apps Script.

    Les autres fichiers du projet sont conservés tels quels.

    filename  : nom sans extension, par exemple 'Code'
    file_type : 'SERVER_JS' pour du code, 'HTML' pour une page
    webapp    : si vrai, le manifeste est configuré pour une publication
                en application web exécutée sous ton identité
    """
    current = _script().projects().getContent(scriptId=script_id).execute()
    files = current.get("files", [])

    manifest_present = any(f.get("name") == "appsscript" for f in files)
    kept = [f for f in files if f.get("name") != filename]

    if webapp or not manifest_present:
        manifest = WEBAPP_MANIFEST if webapp else DEFAULT_MANIFEST
        kept = [f for f in kept if f.get("name") != "appsscript"]
        kept.append(
            {
                "name": "appsscript",
                "type": "JSON",
                "source": json.dumps(manifest, indent=2),
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

    Le secret partagé est ajouté automatiquement depuis la variable
    SCRIPT_SHARED_SECRET, le script doit le vérifier avant d'agir.

    url     : l'URL retournée par deploy_web_app
    payload : dictionnaire transmis au script
    """
    body = dict(payload or {})
    secret = os.environ.get("SCRIPT_SHARED_SECRET", "")
    if secret:
        body["secret"] = secret
    response = requests.post(url, json=body, timeout=timeout)
    try:
        return {"statut": response.status_code, "reponse": response.json()}
    except ValueError:
        return {"statut": response.status_code, "reponse": response.text[:4000]}


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
