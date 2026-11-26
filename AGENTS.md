# Horéveil Home Assistant

- Travailler dans ce dépôt et consulter le contrat de référence dans `docs/CALENDAR_SNAPSHOT_SCHEMA_V1.md`.
- Le projet Android voisin est une source en lecture seule. Toute évolution du contrat requiert une décision distincte.
- Reprendre au dernier jalon attesté et conserver les décisions et contraintes validées. Consigner l’avancement dans les documents existants.
- Limiter les investigations au lot. Adapter les vérifications aux risques et réutiliser les preuves applicables. Corriger les échecs liés au lot et relire le diff avant livraison.
- Relancer uniquement les campagnes de tests pertinentes pour le lot.
- Conserver un audit indépendant des changements sensibles touchant l’association, les autorisations ou la cryptographie.
- Garder les secrets, les URL de webhook complètes et les données privées hors du code, des tests réels et des journaux.
- Exécuter les tests Python ciblés et annoncer leur résultat réel. Les commits restent locaux. Publication et déploiement exigent une demande explicite.
