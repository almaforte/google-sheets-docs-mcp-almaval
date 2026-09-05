"""Almaval - point d'entrée du serveur MCP, avec les outils ajoutés après coup.

Pourquoi ce fichier plutôt qu'une modification de main.py

main.py fait plus de cent kilo-octets. L'outil d'écriture dont dispose
Claude ne remplace que des fichiers entiers : le réémettre pour ajouter
trente lignes frôle la limite de sortie et risque une troncature
silencieuse. Ce fichier contourne l'obstacle sans rien réécrire : il
importe main, qui enregistre ses outils au passage, ajoute les siens sur
le même serveur, puis reconstruit l'application ASGI avec le même chemin
et le même contrôle de clé.

Attention : la commande de démarrage vit dans les réglages Railway, pas
dans le Procfile, et elle l'emporte sur lui. Elle doit valoir
« python bootstrap.py », sans quoi ce fichier n'est jamais exécuté et
rien ne le signale.
"""

import os

import uvicorn
from starlette.middleware import Middleware

import main
from main import ApiKeyMiddleware, mcp, tolerant, _script


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
        about = main._drive().about().get(fields="user(emailAddress)").execute()
        rapport["drive"] = about.get("user", {}).get("emailAddress", "")
    except Exception as exc:  # noqa: BLE001
        rapport["drive"] = "indisponible : " + str(exc)

    return rapport


def _contenu_verifie(script_id: str, fichiers_attendus: int = 0):
    """Lit le contenu d'un projet Apps Script en vérifiant que c'est le bon.

    POURQUOI CE GARDE-FOU EXISTE. Le 05.09.2026, une écriture a échoué en
    SSL. La relance a lu, sous l'identifiant du projet « Almaval - Base
    patients universelle et radars », le contenu d'un AUTRE projet, et
    l'écriture qui a suivi a effacé les 63 fichiers du premier. L'API
    Apps Script n'a pas d'écriture partielle : toute modification passe
    par un cycle lire, modifier, tout réécrire. Une lecture fausse
    détruit donc le projet entier.

    Trois vérifications avant toute réécriture :
      - l'identifiant du projet relu est bien celui demandé ;
      - le projet n'est pas revenu vide, ce qui effacerait tout ;
      - si l'appelant annonce un nombre de fichiers attendu, il correspond.

    En cas de doute on lève une exception et on n'écrit rien. Ne rien
    écrire est toujours réparable, écrire par-dessus ne l'est pas.
    """
    contenu = _script().projects().getContent(scriptId=script_id).execute()
    lu = contenu.get("scriptId")
    if lu and lu != script_id:
        raise RuntimeError(
            "Lecture incohérente : projet demandé {}, contenu renvoyé par le "
            "serveur {}. Aucune écriture n'a été faite.".format(script_id, lu)
        )
    fichiers = contenu.get("files", [])
    if not fichiers:
        raise RuntimeError(
            "Le projet {} est revenu sans aucun fichier. Aucune écriture n'a "
            "été faite : réécrire par-dessus une lecture vide effacerait "
            "tout.".format(script_id)
        )
    if fichiers_attendus and len(fichiers) != fichiers_attendus:
        raise RuntimeError(
            "Le projet {} contient {} fichiers, or {} étaient attendus. "
            "Aucune écriture n'a été faite.".format(
                script_id, len(fichiers), fichiers_attendus
            )
        )
    return fichiers


@mcp.tool()
@tolerant
def delete_script_files(
    script_id: str,
    filenames: str,
    fichiers_attendus: int = 0,
):
    """Supprime définitivement un ou plusieurs fichiers d'un projet Apps Script.

    L'API Apps Script n'a pas d'opération de suppression : elle ne sait
    que remplacer le contenu entier d'un projet. Supprimer un fichier
    consiste donc à relire tout le projet, à retirer les fichiers visés
    de la liste, puis à réécrire le reste. C'est exactement le cycle qui
    a détruit un projet le 05.09.2026, quand la lecture a renvoyé un
    autre projet que celui demandé. Les protections ci-dessous ne sont
    donc pas décoratives.

    script_id : le projet à modifier.
    filenames : les noms des fichiers à supprimer, sans extension, tels
                qu'ils apparaissent dans l'éditeur. Plusieurs noms se
                séparent par une virgule :
                « 00 Configuration, 01 Lecture, 02 Contrat ».
                Un nom contenant lui-même une virgule n'est pas prévu,
                Apps Script n'en accepte pas.
    fichiers_attendus : nombre de fichiers que le projet doit contenir
                AVANT la suppression. 0 pour ne pas contrôler. Le
                renseigner est la meilleure protection disponible : si le
                serveur relit un autre projet, le compte ne correspond
                pas et rien n'est supprimé.

    Refus explicites, jamais silencieux :
      - le manifeste « appsscript » ne se supprime pas, un projet sans
        manifeste est invalide ;
      - une suppression qui viderait le projet est refusée ;
      - un nom absent est signalé et n'empêche pas les autres.

    Après l'écriture, le projet est relu et la réponse dit ce qui reste :
    la vérification ne repose pas sur la confiance.
    """
    demandes = [n.strip() for n in str(filenames or "").split(",") if n.strip()]
    if not demandes:
        raise RuntimeError("aucun nom de fichier à supprimer n'a été donné")

    fichiers = _contenu_verifie(script_id, fichiers_attendus)
    presents = [f.get("name") for f in fichiers]

    if "appsscript" in demandes:
        raise RuntimeError(
            "le manifeste « appsscript » ne peut pas être supprimé : un projet "
            "Apps Script sans manifeste est invalide"
        )

    introuvables = [n for n in demandes if n not in presents]
    a_supprimer = [n for n in demandes if n in presents]
    if not a_supprimer:
        return {
            "script_id": script_id,
            "fichiers_supprimes": [],
            "introuvables": introuvables,
            "fichiers_presents": presents,
            "message": (
                "aucun des noms demandés n'existe dans ce projet, "
                "rien n'a été modifié"
            ),
        }

    restants = [f for f in fichiers if f.get("name") not in a_supprimer]
    if not restants:
        raise RuntimeError(
            "refus : cette suppression viderait entièrement le projet "
            + str(script_id)
        )
    if not any(f.get("name") == "appsscript" for f in restants):
        raise RuntimeError(
            "refus : le projet " + str(script_id)
            + " se retrouverait sans manifeste"
        )

    _script().projects().updateContent(
        scriptId=script_id, body={"files": restants}
    ).execute()

    relu = _script().projects().getContent(scriptId=script_id).execute()
    noms_apres = [f.get("name") for f in relu.get("files", [])]
    return {
        "script_id": script_id,
        "fichiers_supprimes": a_supprimer,
        "introuvables": introuvables,
        "fichiers_avant": len(fichiers),
        "fichiers_apres": len(noms_apres),
        "encore_presents_a_tort": [n for n in a_supprimer if n in noms_apres],
        "fichiers": noms_apres,
    }


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
        main._drive()
        .files()
        .get(fileId=file_id, fields="parents,name", supportsAllDrives=True)
        .execute()
    )
    anciens_parents = actuel.get("parents", [])
    deplace = (
        main._drive()
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
        main._drive()
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
        main._drive()
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
#
# Le nombre d'outils réellement enregistrés y figure depuis le
# 05.09.2026 : ce jour-là, un outil ajouté dans main.py n'est jamais
# apparu côté client alors que le déploiement était en succès, et rien
# ne permettait de savoir si le conteneur avait pris le nouveau code.
# Un compteur au démarrage tranche la question en une ligne de journal.
try:
    import fastmcp as _fastmcp

    _version = getattr(_fastmcp, "__version__", "inconnue")
except Exception:  # noqa: BLE001
    _version = "inconnue"

_noms_outils = []
try:
    import asyncio

    _exposes = asyncio.run(mcp.get_tools())
    if isinstance(_exposes, dict):
        _noms_outils = sorted(_exposes.keys())
    else:
        _noms_outils = sorted(getattr(o, "name", str(o)) for o in _exposes)
except Exception as exc:  # noqa: BLE001
    _noms_outils = ["(liste indisponible : " + type(exc).__name__ + " " + str(exc)[:120] + ")"]

print(
    "[bootstrap] point d'entrée actif, fastmcp " + str(_version) +
    ", outils exposés : " + str(len(_noms_outils)),
    flush=True,
)
print("[bootstrap] noms des outils : " + ", ".join(_noms_outils), flush=True)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
