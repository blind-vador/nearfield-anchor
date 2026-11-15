# nearfield-pont — protocole de confiance v2

Crate Rust qui implémente la v2 du pont entre l'ordonnateur et les machines du parc :
clés d'identité, objets signés, appairage vocal, certificats de rôle, transport chiffré.
Il remplacera `bridge/pont_protocole.py` (v1).

Licence : **MIT** (voir `LICENCE`), par exception au reste du dépôt (GPL v3), pour que
d'autres logiciels puissent l'utiliser.

Spécification complète : `specs/protocole-v2.md` dans le dossier de conception Prismaforge.

## Modules

| Module | Rôle |
|---|---|
| `jcs` | Forme canonique JSON (RFC 8785, entiers seulement) : ce qui est signé |
| `cles` | Clés Ed25519 et P-256, coffres (fichier aujourd'hui, puces ensuite), identifiants de machine. Relit la `cle.pem` de la v1 |
| `objet` | Objets signés pour le NAS : vérification, empreintes des fichiers joints, noms sûrs, registre anti-rejeu de 7 jours |
| `appairage` | Engagement préalable et code vocal à 6 chiffres, dit en 3 paires |
| `certificat` | Certificats de 12 h (rôles, capacités, niveau), liste de révocation qui ne recule jamais |
| `messages` | Liste fermée des messages du canal, limites de taille |
| `transport` | Session Noise XX (`Noise_XX_25519_ChaChaPoly_SHA256`) authentifiée par certificat, port 47812 |

## Tests

```
cargo test
```

33 tests : vecteurs fixes (`vecteurs.json`, recalculés indépendamment en Python),
et les attaques que la v2 doit repousser (charge modifiée, rejeu, objet trop vieux, autre
parc, faux ordonnateur, clé Noise différente du certificat, intermédiaire pendant
l'appairage, type de message inconnu, chemin `../`, liste de révocation plus ancienne…).

Pour ne pas remplir le NAS, compiler avec un dossier de sortie local :
`CARGO_TARGET_DIR=~/.cache/cible-pont cargo test`.

## Reste à faire

- Coffres matériels : Secure Enclave (macOS), TPM 2.0 (Linux, Windows).
- Liaison Python (PyO3 / maturin) pour nearfield.
- Enveloppe chiffrée pour les objets sensibles.
- Migration depuis la v1 (message `migration` signé par l'ancienne clé).
