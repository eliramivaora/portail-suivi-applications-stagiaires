# Portail de suivi des applications des stagiaires — phases 1 à 4

La phase 1 installe la pile locale de télémétrie : OpenTelemetry Collector,
Prometheus, Tempo, Loki et Grafana derrière Caddy. Le portail FastAPI et Grafana
partagent le même domaine local.

La phase 2 ajoute le tableau de bord Grafana « Applications stagiaires » et
des règles d'alerte Prometheus pour les applications silencieuses, les erreurs,
la latence et l'indisponibilité des composants de supervision.

La phase 3 ajoute le registre des applications, les fiches, l'historique des
modifications, l'authentification et les rôles administrateur, stagiaire et
lecteur. La base SQLite est conservée dans un volume Docker dédié.

## Prérequis

- Docker Desktop avec le plugin Docker Compose v2
- 4 processeurs, 8 Go de mémoire et au moins 10 Go d'espace disque libre

Les images et dépendances sont épinglées à des versions précises. Le premier
lancement télécharge les images ; ensuite, l'exploitation peut se faire sans
connexion externe tant que les images sont présentes localement.

## Démarrage (PowerShell)

```powershell
Copy-Item .env.example .env
notepad .env
docker compose config
docker compose up -d --build
docker compose ps
```

Remplacez les valeurs de démonstration de `GRAFANA_ADMIN_PASSWORD`,
`PORTAL_SESSION_SECRET_KEY` et `PORTAL_ADMIN_PASSWORD` avant un déploiement
partagé. Utilisez des secrets aléatoires différents pour chaque service.
Ne versionnez jamais `.env`. Le port web est `8080` par défaut et le Collector
est publié uniquement sur la boucle locale de la machine.

- Portail des applications : http://localhost:8080/
- Grafana : http://localhost:8080/grafana/
- Identifiants Grafana : valeurs `GRAFANA_ADMIN_USER` et
  `GRAFANA_ADMIN_PASSWORD` du fichier `.env`
- Première connexion au portail : valeurs `PORTAL_ADMIN_EMAIL` et
  `PORTAL_ADMIN_PASSWORD` du fichier `.env`

Le compte administrateur initial est créé automatiquement au premier démarrage
de la base; les redémarrages suivants ne réinitialisent pas son mot de passe.
Connectez-vous puis créez les comptes stagiaires et lecteurs dans **Comptes**.
Un utilisateur peut modifier son propre mot de passe depuis **Mon compte**.

## Vérification rapide

```powershell
Invoke-RestMethod http://localhost:8080/health
docker compose logs --since 5m otel-collector
```

Dans Grafana Explore, vérifiez :

- **Prometheus** : `stagiaires_app_requests_total`
- **Loki** : `{service_name="demo-stagiaire"}`
- **Tempo** : recherchez les traces par service `demo-stagiaire`

Les métriques applicatives sont exportées périodiquement ; attendez jusqu'à
15 secondes après l'ouverture du portail.

## Phase 3 : portail, comptes et applications

Le portail redirige les visiteurs non connectés vers la page de connexion.
Dans **Applications**, les comptes autorisés peuvent rechercher et filtrer les
fiches par nom, service, responsable, état d'archivage et période de stage.
Une fiche contient le nom du service OpenTelemetry, la description, le langage,
le dépôt Git, la version, le responsable et les dates de stage; elle conserve
également un historique des modifications. Les fiches sont archivées, et non
supprimées.

Les administrateurs peuvent créer les comptes et gérer toutes les fiches. Les
stagiaires consultent et modifient uniquement leurs propres fiches. Les lecteurs
disposent d'un accès en consultation. Les mots de passe sont hachés avec
Argon2id; les formulaires sont protégés contre les requêtes CSRF, et les sessions
sont stockées dans un cookie signé `HttpOnly` avec `SameSite=Lax`.

La base du portail est stockée dans le volume Docker `portal_data`. La
notification par e-mail n'est pas encore configurée : elle nécessite un serveur
SMTP et des destinataires.

## Phase 4 : santé en direct et tableaux Grafana intégrés

Le registre affiche l'état de santé observé à l'ouverture. La fiche actualise
son état toutes les 30 secondes en interrogeant Prometheus : un battement reçu
depuis 60 secondes au plus est **Opérationnel**, de plus de 60 à 300 secondes
**À surveiller**, puis **Indisponible** au-delà de 300 secondes. Sans métrique, le portail indique
**Aucun signal**; si Prometheus ne répond pas, l'état reste explicitement
**inconnu**. Ces seuils sont distincts des règles d'alerte de la phase 2.

La fiche intègre le tableau de bord Grafana filtré sur le service. Une session
Grafana reste requise pour afficher ses panneaux. L'application interroge
Prometheus par une liaison Docker interne dédiée, sans publier son API sur
l'hôte.

## Fonctions complémentaires du registre

Le registre peut désormais être filtré selon l'état de supervision et exporté
en CSV avec les critères de recherche, de période, d'archivage, de responsable
et de santé sélectionnés. Chaque rôle ne reçoit que les fiches qu'il est déjà
autorisé à consulter; l'export neutralise également les cellules susceptibles
d'être interprétées comme des formules par un tableur.

### Tests d'intégration du portail

```powershell
docker build -f demo-app\Dockerfile.test -t stagiaires-portal-tests demo-app
docker run --rm stagiaires-portal-tests
```

Les tests utilisent une base SQLite temporaire et ne touchent pas aux données du
volume `portal_data`.

## Phase 2 : dashboard et alertes

Ouvrez le dashboard réutilisable **Applications stagiaires** dans le dossier
**Applications** de Grafana. Le filtre **Application** permet de sélectionner
un ou plusieurs services OpenTelemetry. Il présente le dernier battement de
coeur, le taux de réponses HTTP 5xx, la latence p95, le débit et les journaux
Loki de l'application sélectionnée.

Les règles sont provisionnées dans `prometheus-alerts.yml` et évaluées par
Prometheus :

- **ApplicationWithoutHeartbeat** : aucun battement de coeur depuis 5 minutes,
  puis confirmé pendant 1 minute.
- **ApplicationErrorRateHigh** : plus de 5 % de réponses 5xx sur 5 minutes,
  maintenu pendant 2 minutes.
- **ApplicationLatencyHigh** : latence p95 supérieure à 1 seconde, maintenue
  pendant 2 minutes.
- **ObservabilityComponentDown** : Collector, Tempo, Loki ou Grafana inaccessible
  à Prometheus pendant 1 minute.

Le seuil de latence de 1 seconde est une valeur initiale à ajuster pour
l'entreprise. Les règles sont visibles dans Prometheus et dans la page
**Alerting** de Grafana. Elles n'envoient pas encore d'e-mail : aucun serveur
SMTP ni destinataire n'a été défini.

Les routes suivantes simulent des cas de test sur l'application de démonstration
uniquement :

- `http://localhost:8080/demo/error` produit une réponse HTTP 500.
- `http://localhost:8080/demo/slow?seconds=1.2` simule une requête lente
  (durée plafonnée à 5 secondes).

Après toute modification de configuration, appliquez la phase 2 avec :

```powershell
docker compose up -d --build
docker compose ps
```

Les règles devraient apparaître dans Prometheus après le redémarrage. Dans
Grafana, ouvrez **Dashboards > Applications > Applications stagiaires**.

## Arrêt et données

```powershell
docker compose down
```

Les données restent dans les volumes Docker nommés. La suppression des volumes
efface les traces, métriques, journaux et données Grafana ; ne l'effectuez que
si vous souhaitez réellement réinitialiser cette installation.

## Accès réseau local

Par défaut, le site et les ports OTLP sont liés à `127.0.0.1` pour éviter une
exposition involontaire. Pour rendre le site accessible aux autres machines du
réseau, modifiez la publication du port Caddy dans `docker-compose.yml` puis
limitez l'accès au réseau interne avec le pare-feu. N'exposez jamais Grafana ni
les ports OTLP directement à Internet. Les ports du Collector restent liés à
la boucle locale dans cette première configuration.

## Réinitialisation de la pile

La configuration est montée en lecture seule depuis les fichiers du projet.
Après une modification :

```powershell
docker compose up -d
docker compose ps
```

Pour examiner un service : `docker compose logs --tail 100 <service>`.
