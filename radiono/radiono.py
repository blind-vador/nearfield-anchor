import os, random, magicsound

from sound_paths import RADIONO_DIR

# Chemin corrige (2026-08-31, bug trouve en preparant le depot git) :
# pointait auparavant sur /media/nas/nd2/server/radiono, un dossier qui
# n'existe pas (/media/nas/nd2/server/ est introuvable) -- le vrai
# contenu vit sous nd1. Centralise desormais dans sound_paths.py.
main_path = RADIONO_DIR

folder_list=os.listdir(main_path)

selected_dir=random.choice(folder_list)

sound_list=os.listdir(main_path+"/"+selected_dir)

selected_sound=random.choice(sound_list)

target=main_path+"/"+selected_dir+"/"+selected_sound


makenoise=magicsound.magicsound(target,block=True)
