"""Tranchage automatique STL -> G-code via CuraEngine (Ender 3 / Marlin).

Utilise les definitions Ultimaker fdmprinter/fdmextruder fournies dans
pi_console/cura_defs/, et les params issus du questionnaire (couche,
remplissage, supports, adherence, temperatures materiau).
"""
import chemins
import os
import re
import shutil
import subprocess
import tempfile

CURA = "/usr/bin/CuraEngine"
# Relatif au package deploie
_DEF_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "cura_defs"),
    chemins.base("pi_console/cura_defs"),
    chemins.depot("lib/cura_defs"),
]

# Volume Ender 3 (mm) — aligne sur le profil OctoPrint typique
MACHINE = {
    "machine_width": "220",
    "machine_depth": "220",
    "machine_height": "250",
    "machine_heated_bed": "true",
    "machine_extruder_count": "1",
    "machine_center_is_zero": "false",
    "machine_gcode_flavor": "Marlin",
}

ADHESION = {
    "rien": "none",
    "skirt": "skirt",
    "brim": "brim",
    "raft": "raft",
}


def disponible():
    if not (os.path.isfile(CURA) and os.access(CURA, os.X_OK)):
        return False
    return bool(_defs_dir())


def _defs_dir():
    for d in _DEF_CANDIDATES:
        if os.path.isfile(os.path.join(d, "fdmprinter.def.json")) and \
           os.path.isfile(os.path.join(d, "fdmextruder.def.json")):
            return d
    return None


def _stl_of(item):
    for f in item.get("fichiers") or []:
        if f["nom"].lower().endswith(".stl"):
            return f
    return None


def _out_path(stl_path, params):
    base, _ = os.path.splitext(stl_path)
    tag = "{couche}_{remplissage}".format(
        couche=params.get("couche") or "normale",
        remplissage=params.get("remplissage") or "normal",
    )
    return f"{base}_{tag}.gcode"


def trancher(item, params, timeout_sec=600):
    """Tranche le premier STL du pret. Rend (gcode_path, message_erreur).

    Ecrit le G-code a cote du STL (dossier pret NAS).
    """
    if not disponible():
        return None, "cura_absent"
    stl = _stl_of(item)
    if not stl:
        return None, "pas_stl"
    stl_path = stl["chemin"]
    if not os.path.isfile(stl_path):
        return None, "stl_manquant"
    out = _out_path(stl_path, params)
    # Deja tranche avec les memes params ?
    if os.path.isfile(out) and os.path.getsize(out) > 1000:
        if os.path.getmtime(out) >= os.path.getmtime(stl_path):
            return out, None

    defs = _defs_dir()
    couche = float(params.get("couche_mm") or 0.2)
    remp = int(params.get("remplissage_pct") or 20)
    supports = "true" if params.get("supports") else "false"
    adhesion = ADHESION.get(params.get("adherence") or "skirt", "skirt")
    buse = int(params.get("buse") or 200)
    plateau = int(params.get("plateau") or 60)
    buse_mm = float(params.get("buse_mm") or 0.4)

    cmd = [
        CURA, "slice",
        "-j", os.path.join(defs, "fdmprinter.def.json"),
        "-s", f"machine_width={MACHINE['machine_width']}",
        "-s", f"machine_depth={MACHINE['machine_depth']}",
        "-s", f"machine_height={MACHINE['machine_height']}",
        "-s", f"machine_heated_bed={MACHINE['machine_heated_bed']}",
        "-s", f"machine_extruder_count={MACHINE['machine_extruder_count']}",
        "-s", f"machine_center_is_zero={MACHINE['machine_center_is_zero']}",
        "-s", f"machine_gcode_flavor={MACHINE['machine_gcode_flavor']}",
        "-s", f"layer_height={couche}",
        "-s", f"layer_height_0={couche}",
        "-s", "wall_line_count=3",
        "-s", "top_layers=4",
        "-s", "bottom_layers=4",
        "-s", f"infill_sparse_density={remp}",
        "-s", f"support_enable={supports}",
        "-s", f"adhesion_type={adhesion}",
        "-s", "skirt_line_count=3",
        "-s", f"material_print_temperature={buse}",
        "-s", f"material_bed_temperature={plateau}",
        "-s", f"material_print_temperature_layer_0={buse}",
        "-s", f"material_bed_temperature_layer_0={plateau}",
        "-s", "speed_print=50",
        "-s", "speed_travel=120",
        "-s", "retraction_enable=true",
        "-s", "retraction_amount=5",
        "-s", "center_object=true",
        "-e0", "-j", os.path.join(defs, "fdmextruder.def.json"),
        "-s", f"machine_nozzle_size={buse_mm}",
        "-s", "material_diameter=1.75",
        "-g", "-e0",
        "-l", stl_path,
        "-o", out,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "exec"
    if proc.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) < 500:
        # stderr Cura est souvent enorme ; garder la fin
        err = (proc.stderr or proc.stdout or "")[-400:]
        print(f"[tranche] echec code={proc.returncode} : {err!r}")
        return None, "echec"
    # Verifie qu'il y a des couches utiles
    try:
        with open(out, encoding="utf-8", errors="replace") as f:
            head = f.read(200000)
        if ";LAYER:" not in head and head.count("\nG1") < 20:
            # parfois ;LAYER: plus loin
            with open(out, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if ";LAYER:" not in text:
                return None, "vide"
    except OSError:
        return None, "lecture"
    # Miroir SD si le STL etait sur le NAS
    try:
        if "/nearfield-anchor-pieces/" in out:
            mirror_root = chemins.base("pieces_cache/prets")
            name = os.path.basename(os.path.dirname(out))
            dest_dir = os.path.join(mirror_root, name)
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(out, os.path.join(dest_dir, os.path.basename(out)))
    except OSError as exc:
        print(f"[tranche] miroir : {exc!r}")
    return out, None
