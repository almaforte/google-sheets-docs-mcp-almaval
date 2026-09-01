"""Almaval - point d'entrée du serveur MCP, avec les outils ajoutés après coup.

Pourquoi ce fichier plutôt qu'une modification de main.py

main.py fait plus de cent kilo-octets. Le connecteur GitHub n'écrit que
des fichiers entiers : le réémettre pour ajouter trente lignes frôle la
limite de sortie du modèle, et une troncature silencieuse corromprait le
serveur. Ce fichier contourne l'obstacle sans rien réécrire : il importe
main, qui enregistre ses quarante-neuf outils au passage, ajoute les
siens sur le même serveur, puis reconstruit l'application ASGI avec le
même chemin et le même contrôle de clé.

main.py n'est pas modifié d'une ligne. Seul le Procfile change, pour
démarrer ici plutôt que là.
"""

import os

import uvicorn
from starlette.middleware import Middleware

import main
from main import ApiKeyMiddleware, mcp, tolerant, _script


@mcp.tool
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


@mcp.tool
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
        about = main._drive().about().get(fields="user(emailAddress)").execute()
        rapport["drive"] = about.get("user", {}).get("emailAddress", "")
    except Exception as exc:  # noqa: BLE001
        rapport["drive"] = "indisponible : " + str(exc)

    return rapport


app = mcp.http_app(
    path=os.environ.get("MCP_PATH", "/mcp"),
    middleware=[Middleware(ApiKeyMiddleware)],
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
