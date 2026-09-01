# Serveur MCP Google Sheets, Docs et Apps Script — Almaval

Serveur FastMCP exposant Google Sheets, Docs, Drive et Apps Script à
Claude. Hébergé sur Railway, projet « Google Sheets & Apps Script ».

## Deux identités Google, séparées par API

Ce n'est pas un repli de l'une sur l'autre, c'est une séparation câblée.

**Sheets, Docs, Drive** passent par le compte de service avec délégation
à l'échelle du domaine : `GOOGLE_SERVICE_ACCOUNT_JSON` et
`IMPERSONATE_USER`, via `creds.with_subject(subject)`.

**Apps Script** passe par un jeton utilisateur :
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
`GOOGLE_OAUTH_REFRESH_TOKEN`. Google l'impose, l'API Apps Script
n'acceptant pas les comptes de service.

Conséquence pratique : changer `IMPERSONATE_USER` sans refaire le jeton
ne déplace que la moitié du serveur.

## Deux services sur ce même dépôt

`web` travaille sous am.forte. `web-contact` travaille sous
contact@almaval.ch, le compte qui exécute le système de documents
cliniques. Les secrets partagés sont posés en variables de référence
`${{web.…}}` : une seule source de vérité.

Un commit reconstruit **les deux**. Ne pas pousser en fin de journée.

## bootstrap.py

`main.py` fait plus de cent kilo-octets, et l'outil d'écriture dont
dispose Claude ne remplace que des fichiers entiers : le réémettre pour
ajouter quelques lignes risquait une troncature silencieuse.

`bootstrap.py` contourne l'obstacle sans rien réécrire. Il importe
`main`, qui enregistre ses outils au passage, ajoute les siens sur le
même serveur FastMCP, puis reconstruit l'application ASGI avec le même
chemin et le même contrôle de clé. Le `Procfile` démarre donc
`bootstrap.py`, et `main.py` n'est pas modifié d'une ligne.

Outils ajoutés là :

`update_web_app_deployment` publie une nouvelle version **sur un
déploiement existant**, donc sans changer son adresse. `deploy_web_app`,
lui, crée un déploiement de plus à chaque appel : nouvelle URL, et
l'ancienne adresse continue de servir du code périmé sans erreur
visible. C'est arrivé au portail de documents cliniques le 01.09.2026.
Dès qu'une adresse est en service, utiliser la mise à jour, jamais la
création.

`identite_du_serveur` répond sous quel compte le serveur travaille
réellement, des deux côtés. À interroger au moindre doute sur « qui a
écrit ce fichier ».

## Sécurité

`MCP_PATH` est un chemin non devinable, `MCP_API_KEY` la clé attendue en
en-tête. **Si `MCP_API_KEY` est vide, le contrôle est entièrement
désactivé** et le serveur devient ouvert à qui connaît l'URL, avec
l'identité d'un compte du domaine. Elle doit toujours être posée.

## À faire

Figer la version de Python (`.python-version`) et épingler les versions
dans `requirements.txt` : aujourd'hui, une reconstruction peut changer
d'environnement sans que personne ne l'ait demandé.
