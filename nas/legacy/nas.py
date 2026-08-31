# -- coding: utf-8 --

#on commence par importer le module os qui va nous permettre de
#faire d'une pierre deux coups, passer des comandes bash au pi et aussi
#lister les fichiers présents aux points de montage.
import os
#import du module permettant de démarer des sous-process python
import subprocess

#On prépare une variable qu'on appelera confirmed avec une valeur de 0
#a chaque fois qu'on confirmera qu'un  disque est  monté, on incrémentera sa valeur pour dire combiens de disque sont
#présent sur le système actuellement;
confirmed=0

#ajout d'un système de pointage qui permettra de démarer le server sound rts selon le point de montage de son disque
pointor=None

#La variable suivante détermine les paramètres de la voix que nous allons utilisé pour faire parler notre pi
#libre a chaqu'un de les ajuster, changer la langue, (il faudra réécrire les messages)
#débit, hauteur... autant de paramètres qu'on peu avoir envie de modifier en une seul fois au lieu de parcourir tout le script.
#consultez l'aide de espeak pour trouver les différents paramètres utilisable.
#veuillez noter que a la fin entre le dernier paramètre et le guillemet il y a un espace
#ceci pour éviter toute anomalie lors de l'interprétation de la comande, veuillez conservé un espace entre
#le paramètre et le guillemet.
speaking_cmd="espeak -v fr "

#décriptons un peu la ligne suivante.
#os.system() on fait appelle a la method system du module os.
#system atant de nous comme paramètre une chaine de caractère représentant une comande a envoyer a l'interpréteur.
#juste après la ( on trouve speaking_cmd+"\"
#cette syntaxe un peu déconsertante viens du fait que on passe en paramètre une chaine de caractère qui est en fait le résultat de la concaténation de deux chaines
#speaking_cmd qui contiens les paramètres et la comande espeak, + l'oppérateur de concaténation,
#" qui marque l'entré dans la nouvelle chaine de caractère comprenant elle le message envoyé a espeak.
#vous noterez que la chaine commence par: \" et se termine comme ça aussi.
#tout simplement parce que pour éviter toute incoérence lors du traitement de la comande espeak, il est préférable d'encadrer son texte avec des guillemets.
#hors, quelqu'un d'autre ici interprette les guillemets... python, pour éviter que python crois que les guillemets de la chaine de caractère sont pour lui et les traite, on les échappe;
os.system(speaking_cmd+"\"Vérification de la présence des disque pour le système nas.\"")


#on commence par monter un disque. adaptez le script selon combiens vous en avez.
#on retrouve os.system, la chaine de caractère mount... qui représente la ligne de comande pour monter un disque sur un point de notre arboressance.
#adaptez le chemin de celle-ci selon votre propre besoin.
os.system("mount /dev/sda1 /media/nas/nd1")

#ici on va tester une condition très simple pour vérifier si le disque qu'on viens de monté a bien été trouvé.
#on utilise la method listdir de os en lui passant en paramètre un chemin vers le point de montage pour remonter la liste des fichiers présent dans le dossier.
#mais avant tout on récupaire la longueur de cette dite liste, c'est pour cela qu'on obtiens len(listdir(chemin))
#ou traduit en bon français longueur de la liste(liste du contenu de(chemin vers le point de montage))
#on obtiens donc en sortie un integer qu'on va tester de se pas pour vérifier s'il est égal a 0.
#si c'est 0, alors la liste est vide, et de fait il n'y a rien dans le dossier indiqué comme point de montage
#deux cas de figure alors, soit votre disque n'est pas monté, soit votre disque est vide. C'est une des limites de se système on présupose que le disque n'est pas vide.
#pour détecter qu'il est bien là. Je n'ai pas trouvé après mes recherches d'autres métodes pour détecter et confirmer la présence d'un disque

if(len(os.listdir("/media/nas/nd1"))==0):
	disca_status="Oups! le disque de sauvegarde semble être déconnecté... veuillez vérifier votre installation, puis réessayer. "
else:
	#si le disque a bien été détecté comme étant en ligne, alors on incrémente confirmed qui passe de 0 à 1,
	#ceci pouvant par exemple permettre d'adapter le message a la fin du script pour savoir combiens de disque tourne actuellement.
	#notez que je nomme explicitement les disque, parce que c'est la fonction que je leur ai donné.
	#adaptez ça a votre propre installation.
	confirmed=confirmed+1
disca_status="Disque de sauvegarde en ligne et paré! "

#on reprend comme tout a l'heure, référez vous au commentairs explicatif précédent le bloque d'instruction similaire.
#vous pouvez copier ses bloques selon combiens de disque vous avez chez vous.
os.system("mount /dev/sdb1 /media/nas/nd2")
if(len(os.listdir("/media/nas/nd2"))==0):
	discb_status="aille! le disque principale de stocage semble être déconnecté. Veuillez vérifier votre installation, puis essayer a nouveau ."
else:
	discb_status="Le disque principale de stocage fonctionne selon des paramètres nomminaux."
	confirmed=confirmed+1

#on teste si aumoins un disque  a bien été monté.
if(confirmed>0):
	
	#lors d'un redémarage du NAS il peut arriver que le disque 1 se monte avant le disque 2.
	#Ceci est due au fait que lorsqu'on branche le disque il n'a pas de nommage stable.
	#si par exemple le disque abituellement prévue en dev/sda se retrouve en /dev/sdb et le sdb en sda
	#alors nos configurations samba serons a la ramace et lorsqu'on tentera d'ouvrir par eexemple /nas/disck1/storage depuis une machine distante, on obtiendra une erreure réseau... puisque le chemin pointe vers un emplacement qui n'existe pas.
	#Pour palier a se problème je n'ai pas trouvé de meilleur solussion que d'enregistrer a la racine de chaque disque un fichier vide appelé avec le nom du disque
	#ou en tout cas celui que par convention je voudrais qu'il ai.
	#il me suffit alors après le montage faire un simpple test pour voir quel disque c'est monté et où.
	#une fois que je sais quel disque est sur quel point de montage, je remplacerai le fichier de configuration de samba smb.conf
	#par une version tenant compte de l'ordre de montage.
	
	#NB: dans mon dossier de session, j'ai un dossier bin contenant quelques script python et leurs fichiers liés.
	#adaptez selon votre configuration et vos besoins.
	#pour rédiger votre fichier de configuration smb.conf référez vous à l'aticle de françois sur comment créer un nas avec un raspberry et samba
	
	#on commence par supprimer le fichier de configuration smb
	#puisqu'on ne saient pas a se stade si il est correctement rédigé
	os.system("sudo rm /etc/samba/smb.conf")
	
	#on test voir si le disque 1 c'est monté à l'emplacement 1
	if(os.path.isfile("/media/nas/nd1/nd1.txt")):
		#alors on fait une copie du fichier smb.conf tenant compte de cette configuration
		#on le prend dans son point de stocage (ou je conserve les deux version du fichier)
		#et on le renomme a la volé avec le nom que samba atant smb;conf
		os.system("cp /home/vador/autopilot/nas_manager/smbv1.conf /etc/samba/smb.conf")
		
		#on préparent un simple message m'indiquant lors du démarage du NAS comment samba est configuré.
		#bien entendu vous pouvez adapter le texte comme vous voulez, dans mon cas comme j'ai deux possiblité, et une mauvaise mémoire des chiffres je voulais le faire avec des lettres
		#mais "a" ou "b" prononcé à l'orale par une voix synthètique dans un environement bruillant peuvent être mal-entendu.
		#c'est pourquoi j'ai obté pour l'alphabet radio, ce qui nous donne alfa ou bravo.
		conftype=" Samba en configuration alfa. "
		#le pointeur reçois le chemin vers le server soundrts
		pointor="/media/nas/nd1/server/soundrtsv2"
	
	#si le bloque 1 ne c'était pas exécuté, cela signifi que
	#les disques se sont inversé.
	#on fait donc le même traitement que précédamant, en tenant compte de cette possibilité.
	if(os.path.isfile("/media/nas/nd1/nd2.txt")):
		os.system("cp /home/vador/autopilot/nas_manager/smbv2.conf /etc/samba/smb.conf")
		conftype=" Samba en configuration bravo. "
		pointor="/media/nas/nd2/server/soundrtsv2"
	
	#Comme on a commencé par supprimer la configuration de samba, on test juste qu'une des deux configuration est bien appliqué.
	#si le dossier /etc/samba contiens aumoins 1 fichier de configuration alors on peu démarer le server.
	if(os.path.isfile("/etc/samba/smb.conf")):
		os.system("samba start")
		
		#on en profite pour faire dire a espeak un petit message qui récap combiens de disque sont en ligne et ce qu'il se passe.
		#Vous noterez cette fois que j'utilise str(confirmed) qui permet de convertir a la voler
		#confirmed qui est un entier et donc pas concaténable avec une chaine
		#en une chaine qui elle peut être utilisée.
		os.system(speaking_cmd+"\""+str(confirmed)+"disques trouvé. "+disca_status+" "+discb_status+" Démarage"+conftype+" terminé.\"")






				
			






					
				
			
		
		
	#instant je ne dois pas me perdre dans les indentations...
	#ici se premier sinon correspond a sinon, si aucun fichier de configuration samba n'a été trouvé.
	else:
		os.system(speaking_cmd+"\"Erreur lors de la configuration de samba. Aucun fichier de configuration trouvé dans le répertoir de stocage indiqué. Veuillez vérifier le chemin d'accès vers le stocage des configurations possible, et réessayer.\"")
	#on a finit avec se niveau d'indentation.
#nous cloturons donc le script en tenant compte d'une derrnière possiblité,
#aucun de nos disque n'a été trouvé connecté au nas, et donc nous n'avons rien fait du tout.
else:
	os.system(speaking_cmd+"\"Aucun disque trouvé en ligne. Server nas en atante, veuillez vérifier votre installation, puis réessayer.\"")
	
