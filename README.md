# netmount

Monte automatiquement des partages réseau (SMB/Windows ou NFS) quand leur
serveur est joignable, et les démonte quand il ne l'est plus.

Pensé pour un portable qui change de réseau : au bureau ou à la maison, les
partages apparaissent tout seuls ; une fois parti, un partage dont le serveur a
disparu est démonté de force avant qu'il ne fige les applications (explorateur
de fichiers, boîtes de dialogue « Ouvrir »…).

## Fonctions

- Surveillance toutes les 10 secondes : montage dès que le serveur répond,
  démontage dès qu'il disparaît.
- Recherche des partages proposés par un serveur (SMB et NFS), avec ou sans
  identifiants.
- **Mode départ** : démonte proprement tout avant de partir, sans remonter tant
  que les serveurs sont encore visibles.
- Icône dans la zone de notification et lancement à l'ouverture de session.
- **Scan DHCP** : liste les serveurs DHCP qui répondent sur un réseau, pour
  repérer un serveur en double.
- **Mise à jour** en un clic depuis GitHub.

## Prérequis

Linux avec un bureau graphique. Sous Debian / Ubuntu :

```bash
sudo apt install git python3-gi gir1.2-gtk-3.0 cifs-utils nfs-common smbclient pkexec sudo
```

`cifs-utils` et `smbclient` ne servent qu'aux partages SMB, `nfs-common` qu'aux
partages NFS.

## Installation

Installer avec `git clone` : c'est ce qui permet au bouton « Mettre à jour… »
de fonctionner (une copie téléchargée en ZIP ne peut pas se mettre à jour).

```bash
git clone https://github.com/myzrix/netmount.git ~/netmount
python3 ~/netmount/netmount.py
```

Au premier lancement, un bandeau indique que l'autorisation de montage n'est
pas installée : cliquer sur **Installer** et saisir le mot de passe
administrateur. À défaut, dans un terminal :

```bash
sudo ~/netmount/install.sh
```

Le programme s'ajoute ensuite au menu des applications. Cocher « Lancer
automatiquement à l'ouverture de session » pour qu'il démarre caché dans la
zone de notification.

## Utilisation

1. **Ajouter** : saisir l'adresse IP du serveur, choisir SMB ou NFS, et pour
   SMB l'utilisateur et le mot de passe (vide = accès invité).
2. **Rechercher les partages**, en choisir un, puis le dossier où le monter
   (par défaut `~/Reseau/<partage>`). Le dossier doit être dans votre dossier
   personnel.
3. Enregistrer : le partage est monté dès que le serveur répond.

Fermer la fenêtre la cache dans la zone de notification ; **Quitter** démonte
tous les partages et arrête le programme.

## Mise à jour

Bouton **Mettre à jour…** en bas de la fenêtre : il affiche les modifications
disponibles, les applique puis relance le programme. Les partages montés le
restent. Si l'autorisation de montage a changé, le mot de passe administrateur
est demandé pour la réinstaller.

En ligne de commande : `git -C ~/netmount pull`, puis relancer le programme
(et `sudo ~/netmount/install.sh` si `netmount-helper` a changé).

## Sécurité : ce que fait l'autorisation

Monter un partage réseau demande les droits root. `install.sh` installe donc :

- `/usr/local/sbin/netmount-helper`, un petit programme root ;
- `/etc/sudoers.d/netmount-<utilisateur>`, une règle qui permet à **cet
  utilisateur seulement** de lancer **ce programme seulement** sans mot de
  passe.

Le helper ne fait confiance à aucun argument :

- il ne monte que des partages SMB ou NFS, avec des options qu'il fixe lui-même
  (`nosuid`, `nodev`, fichiers appartenant à l'utilisateur) ;
- le dossier de montage doit appartenir à l'utilisateur et se trouver dans son
  dossier personnel ; les liens symboliques sont refusés et le dossier est
  vérifié après ouverture, pour qu'un dossier échangé en cours de route ne
  puisse pas faire monter un partage ailleurs ;
- il ne démonte que des montages réseau situés dans le dossier personnel ;
- le scan DHCP envoie seulement des demandes DHCPDISCOVER et n'accepte aucune
  offre : la configuration réseau ne change pas.

Les mots de passe SMB sont enregistrés dans `~/.config/netmount/creds/`,
lisibles par vous seul (mode 600).

## Fichiers

| Emplacement | Contenu |
|---|---|
| `~/.config/netmount/config.json` | liste des partages |
| `~/.config/netmount/creds/` | identifiants SMB |
| `~/.local/share/applications/netmount.desktop` | entrée du menu |
| `~/.config/autostart/netmount.desktop` | lancement à l'ouverture de session |

## Désinstallation

```bash
sudo ~/netmount/install.sh --uninstall
rm -rf ~/netmount ~/.config/netmount \
       ~/.local/share/applications/netmount.desktop ~/.config/autostart/netmount.desktop
```

## Licence

MIT — voir [LICENSE](LICENSE).
