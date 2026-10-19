#!/usr/bin/env python3
"""Veille pont compagnon : travaux Mac (decrire_images, meteor, lora...).

Separee de meteo-watch : la recherche de pieces ne depend plus du process meteo.
"""
import compagnon_serveur

if __name__ == "__main__":
    print("[compagnon_watch] demarrage du pont compagnon", flush=True)
    compagnon_serveur.run()
