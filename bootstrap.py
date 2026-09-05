"""Almaval - point d'entrée du serveur MCP, avec les outils ajoutés après coup.

Pourquoi ce fichier plutôt qu'une modification de main.py

main.py fait plus de cent kilo-octets. L'outil d'écriture dont dispose
Claude ne remplace que des fichiers entiers : le réémettre pour ajouter
trente lignes frôle la limite de sortie et risque une troncature
silencieuse. Ce fichier contourne l'obstacle sans rien réécrire : il
importe main, qui enregistre ses outils au passage, ajoute les siens sur
le même serveur, puis reconstruit l'application ASGI avec le même chemin
et le même contrôle de clé.

main.py n'est pas modifié d'une ligne.

Attention : la commande de démarrage vit dans les réglages Railway, pas
dans le Procfile, et elle l'emporte sur lui. Elle doit valoir
« python bootstrap.py », sans quoi ce fichier n'est jamais exécuté et
rien ne le signale.
"""

import os
import threading

import uvicorn
from googleapiclient.discovery import build
from starlette.middleware import Middleware

import main
from main import ApiKeyMiddleware, mcp, tolerant


# ================================================================
#  CLIENTS GOOGLE PAR THREAD
#  Correctif du 05.09.2026, après deux arrêts brutaux du serveur.
# ================================================================
#
# Symptôme : « free(): corrupted unsorted chunks » puis « Fatal Python
# error: Aborted », dans la fermeture d'une socket SSL sous httplib2,
# appelée par googleapiclient. Le processus entier meurt d'un coup, sans
# exception Python, donc le décorateur tolerant ne peut rien intercepter,
# et Railway finit par cesser de le relancer.
#
# Cause : main.py met en cache un client par API dans le dictionnaire
# global _services. Chaque client construit par build() embarque un objet
# httplib2.Http unique, et httplib2 n'est pas conçu pour être partagé
# entre threads. Or FastMCP exécute chaque appel d'outil dans un thread
# de travail distinct : dès que deux conversations sollicitent le
# connecteur en même temps, deux threads manipulent et referment la même
# connexion, d'où la double libération mémoire et l'abandon du processus.
#
# Correctif : un client par thread, donc un objet Http par thread, plus
# aucun partage. Le cache reste, mais il vit dans un threading.local()
# au lieu d'un dictionnaire global. Les identifiants sont eux aussi
# reconstruits par thread, un jeton se rafraîchissant sans coordination
# entre threads.
#
# À noter, pour éviter un aller-retour inutile plus tard : le transport
# requests (AuthorizedSession) ne peut pas remplacer httplib2 ici.
# google-api-python-client n'accepte, en paramètre http, qu'un objet
# exposant l'interface httplib2, et une session requests ne la respecte
# pas. La façon soutenue par Google d'écarter le problème est exactement
# celle appliquée ci-dessous, un Http par thread.
#
# build() n'appelle pas le réseau : depuis la version 2, la découverte
# des API est servie par les documents embarqués dans le paquet, donc
# créer un client dans un nouveau thread ne coûte quasiment rien.

_par_thread = threading.local()


def _client(cle: str, api: str, version: str, fabrique_identifiants):
    clients = getattr(_par_thread, "clients", None)
    if clients is None:
        clients = {}
        _par_thread.clients = clients
    if cle not in clients:
        clients[cle] = build(
            api,
            version,
            credentials=fabrique_identifiants(),
            cache_discovery=False,
        )
    return clients[cle]


def _sheets():
    return _client("sheets", "sheets", "v4", main._credentials)


def _docs():
    return _client("docs", "docs", "v1", main._credentials)


def _drive():
    return _client("drive", "drive", "v3", main._credentials)


def _script():
    return _client("script", "script", "v1", main._user_credentials)


# Les outils de main.py appellent _sheets(), _docs(), _drive() et
# _script() par leur nom global, résolu à chaque appel : remplacer ces
# quatre noms dans le module main suffit à ce que tous ses outils
# passent par les clients par thread, sans toucher une ligne de main.py.
main._sheets = _sheets
main._docs = _docs
main._drive = _drive
main._script = _script


@mcp.tool()
@tolerant
def update_web_app_deployment(
    script_id: str,
    deployment_id: str,
    description: str = "Mise à jour Claude",
):
    """Publie une nouvelle version SUR un déploiement existant.

    À préférer systématiquement à deploy_web_app dès qu'une adresse est
    en service : l'URL du déploiement ne change pas, donc les favoris,
    les liens de site et la tuile du lanceur Workspace continuent de
    servir le nouveau code sans être retouchés.

    deploy_web_app, lui, crée un déploiement de plus à chaque appel :
    nouvelle URL, ancienne adresse figée sur du code périmé, et le
    plafond de vingt déploiements atteint tôt ou tard. C'est exactement
    ce qui est arrivé au portail de documents cliniques le 01.09.2026,
    dont l'adresse a changé sans que personne ne s'en aperçoive.

    deployment_id se relève avec list_deployments : prendre celui qui
    porte un numéro de version, jamais celui dont la version est nulle,
    qui est le déploiement de tête réservé aux essais.
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
        .update(
            scriptId=script_id,
            deploymentId=deployment_id,
            body={
                "deploymentConfig": {
                    "scriptId": script_id,
                    "versionNumber": version["versionNumber"],
                    "manifestFileName": "appsscript",
                    "description": description,
                }
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
        "note": "Adresse inchangée : les liens existants restent valides.",
    }


@mcp.tool()
@tolerant
def identite_du_serveur():
    """Dit sous quel compte Google ce serveur travaille réellement.

    Deux identités cohabitent et ne se recouvrent pas : le compte de
    service impersonne IMPERSONATE_USER pour Sheets, Docs et Drive, et un
    jeton utilisateur porte les appels Apps Script, l'API Apps Script
    n'acceptant pas les comptes de service.

    À interroger au moindre doute sur « qui a écrit ce fichier ».
    """
    rapport = {"impersonate_user": os.environ.get("IMPERSONATE_USER", "(absent)")}

    try:
        _script()
        rapport["apps_script"] = "jeton utilisateur configuré"
    except Exception as exc:  # noqa: BLE001
        rapport["apps_script"] = "indisponible : " + str(exc)

    try:
        about = _drive().about().get(fields="user(emailAddress)").execute()
        rapport["drive"] = about.get("user", {}).get("emailAddress", "")
    except Exception as exc:  # noqa: BLE001
        rapport["drive"] = "indisponible : " + str(exc)

    return rapport


@mcp.tool()
@tolerant
def move_file_to_folder(file_id: str, target_folder_id: str):
    """Déplace un fichier OU un dossier Drive vers un autre dossier.

    file_id          : identifiant Drive de l'élément à déplacer
    target_folder_id : identifiant Drive du dossier de destination

    Ajouté le 03.09.2026. Root cause identifiée ce jour-là : le
    connecteur Drive générique de Cowork échouait systématiquement en
    erreur de permission sur tout déplacement traversant la frontière
    d'un Drive partagé (ici « 1a Employés & collaborateurs - Almaval »),
    alors même que le compte impersonné (IMPERSONATE_USER) est confirmé
    organisateur sur les dossiers source ET cible. Ce serveur pose déjà
    supportsAllDrives=True sur tous ses appels Drive (voir
    create_spreadsheet, rename_drive_file, get_file_metadata...) ; ce
    même paramètre, vraisemblablement absent côté connecteur générique,
    suffit à lever le blocage ici.

    Retire TOUS les parents actuels avant d'ajouter le nouveau, pas
    seulement le premier : un fichier avec plusieurs parents (rare mais
    possible dans un Drive partagé) se retrouve donc avec exactement un
    seul parent après l'appel, le dossier cible. Idempotent dans son
    effet si rappelé avec la même cible (le fichier y est déjà, l'appel
    ne fait que confirmer son état).
    """
    actuel = (
        _drive()
        .files()
        .get(fileId=file_id, fields="parents,name", supportsAllDrives=True)
        .execute()
    )
    anciens_parents = actuel.get("parents", [])
    deplace = (
        _drive()
        .files()
        .update(
            fileId=file_id,
            addParents=target_folder_id,
            removeParents=",".join(anciens_parents),
            fields="id,name,parents,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    return {
        "id": deplace.get("id"),
        "nom": deplace.get("name"),
        "parents": deplace.get("parents", []),
        "anciens_parents": anciens_parents,
        "url": deplace.get("webViewLink"),
    }


@mcp.tool()
@tolerant
def copy_file_to_folder(file_id: str, target_folder_id: str, new_name: str = ""):
    """Copie un fichier Drive dans un dossier, avec son contenu réel.

    file_id          : identifiant Drive du fichier à copier (un dossier
                        ne peut pas être copié récursivement par l'API
                        Drive ; ne fonctionne que sur un fichier)
    target_folder_id : dossier de destination pour la copie
    new_name         : nom de la copie ; nom d'origine conservé si omis

    Ajouté le 03.09.2026, même cause que move_file_to_folder : un essai
    via le connecteur Drive générique de Cowork avait produit un fichier
    corrompu de 1 octet au lieu d'une vraie copie, en traversant la même
    frontière de Drive partagé (supportsAllDrives vraisemblablement
    absent côté connecteur). Ce paramètre est posé ici, comme partout
    ailleurs dans ce serveur.
    """
    corps: dict = {"parents": [target_folder_id]}
    if new_name:
        corps["name"] = new_name
    copie = (
        _drive()
        .files()
        .copy(
            fileId=file_id,
            body=corps,
            fields="id,name,parents,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    return {
        "id": copie.get("id"),
        "nom": copie.get("name"),
        "parents": copie.get("parents", []),
        "url": copie.get("webViewLink"),
    }


@mcp.tool()
@tolerant
def trash_drive_file(file_id: str):
    """Met un fichier ou dossier Drive à la corbeille (récupérable ~30 jours).

    file_id : identifiant Drive de l'élément à mettre à la corbeille

    Idempotent : si l'élément est déjà à la corbeille, l'appel renvoie
    son état actuel sans erreur plutôt que d'échouer. Ajouté le
    03.09.2026 en même temps que move_file_to_folder et
    copy_file_to_folder, pour couvrir le cas d'un doublon orphelin qu'il
    vaut mieux supprimer que déplacer (ex. Pannatier Virginie, migration
    « 1a », 03.09.2026 : deux copies vierges identiques, l'orpheline
    mise à la corbeille plutôt que déplacée à côté de la bonne).
    """
    mis_a_jour = (
        _drive()
        .files()
        .update(
            fileId=file_id,
            body={"trashed": True},
            fields="id,name,trashed",
            supportsAllDrives=True,
        )
        .execute()
    )
    return {
        "id": mis_a_jour.get("id"),
        "nom": mis_a_jour.get("name"),
        "corbeille": mis_a_jour.get("trashed", False),
    }


app = mcp.http_app(
    path=os.environ.get("MCP_PATH", "/mcp"),
    middleware=[Middleware(ApiKeyMiddleware)],
)


# Trace de démarrage. main.py et bootstrap.py produisent les mêmes
# journaux uvicorn : sans cette ligne, rien ne dit lequel tourne.
try:
    import fastmcp as _fastmcp

    _version = getattr(_fastmcp, "__version__", "inconnue")
except Exception:  # noqa: BLE001
    _version = "inconnue"

print(
    "[bootstrap] point d'entrée actif, fastmcp " + str(_version) +
    ", clients Google par thread actifs"
    ", outils supplémentaires : update_web_app_deployment, "
    "identite_du_serveur, move_file_to_folder, copy_file_to_folder, "
    "trash_drive_file",
    flush=True,
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
