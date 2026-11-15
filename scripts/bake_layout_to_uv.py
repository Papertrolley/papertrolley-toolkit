# Copyright (C) 2026 Mungojerrie
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#!/usr/bin/env python3
"""
Обратный bake: выкройка → UV-текстура.

Читает PNG (результат transfer + composite), «разворачивает» его
обратно в UV-пространство модели через гомографии островов,
описанных в layout.quads.json формата PaperBus Toolkit.

Группировка по композитному ключу (instance_id, obj_name, uv_map).
Объект в сцене ищется как "<instance_id>__<obj_name>".
Материал назначается через slot.link = 'OBJECT' — не задевает
другие instance, разделяющие mesh data.

Запуск (CLI):
    blender --background model.blend --python bake_layout_to_uv.py -- \\
        --layout layout.quads.json \\
        --layout-png result.png \\
        --out uv_baked/ \\
        [--size 4096] [--no-apply] [--verbose] [--save-blend out.blend]

Запуск (интерактивно, из Text Editor / Python Console):
    from bake_layout_to_uv import run_bake_interactive
    run_bake_interactive(
        layout_path=".../layout.quads.json",
        layout_png=".../sheet_composite.png",
        output_dir=".../uv_baked/",
        size=4096,
        verbose=True,
    )
"""

import argparse
import json
import os
import sys
from collections import defaultdict

import bpy
import numpy as np
from PIL import Image


# ====================== JSON ======================

def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


# ====================== Гомография ======================

def compute_homography(src_pts, dst_pts):
    """H: src → dst. Использует SVD (DLT, без нормализации — ок для
    4 точек bbox острова). Возвращает 3×3 float64 или None при вырождении."""
    A = []
    for (x, y), (u, v) in zip(src_pts, dst_pts):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    A = np.array(A, dtype=np.float64)
    _, _, Vt = np.linalg.svd(A)
    H = Vt[-1].reshape(3, 3)
    n = np.linalg.norm(H)
    return H / n if n > 1e-12 else None


# ====================== Загрузка layout ======================

def _validate_island(isl):
    """Проверить, что запись острова соответствует новому формату
    PaperBus Toolkit (layout.quads.json). Возвращает (ok, reason)."""
    if "corners_uv" in isl and "corners_layout_px" in isl \
            and "obj_name" in isl and "instance_id" in isl:
        return True, None
    if "corners" in isl and "id" in isl:
        return False, ("старый формат layout — обновите Composer "
                       "(нужен layout.quads.json нового формата)")
    return False, "отсутствуют обязательные поля"


def _group_islands(layout):
    """Сгруппировать острова по (instance_id, obj_name, uv_map).
    Возвращает dict и список ошибок валидации."""
    by_key = defaultdict(list)
    errors = []
    for idx, isl in enumerate(layout.get("islands", [])):
        ok, reason = _validate_island(isl)
        if not ok:
            errors.append(f"islands[{idx}]: {reason}")
            continue
        iid = isl.get("instance_id", "")
        obj_name = isl.get("obj_name", "")
        uv_map = isl.get("uv_map", "")
        if not (iid and obj_name and uv_map):
            errors.append(
                f"islands[{idx}]: пустые instance_id/obj_name/uv_map "
                f"({iid!r}, {obj_name!r}, {uv_map!r})")
            continue
        if len(isl.get("corners_uv", [])) < 4 \
                or len(isl.get("corners_layout_px", [])) < 4:
            errors.append(
                f"islands[{idx}]: менее 4 углов "
                f"(corners_uv={len(isl.get('corners_uv', []))}, "
                f"corners_layout_px={len(isl.get('corners_layout_px', []))})")
            continue
        by_key[(iid, obj_name, uv_map)].append(isl)
    return by_key, errors


# ====================== Bake ======================

def bake_islands_to_uv(layout, layout_arr, texture_size, verbose=False):
    """Возвращает ({ (iid, obj_name, uv_map): np.array(H,W,4) }, total_px,
    errors)."""
    src_h, src_w = layout_arr.shape[:2]
    by_key, errors = _group_islands(layout)

    print(f"Островов сгруппировано: "
          f"{sum(len(v) for v in by_key.values())} "
          f"в {len(by_key)} групп (instance, obj, uv_map)")
    if errors:
        for e in errors:
            print(f"  WARN: {e}")

    results = {}
    total_written_all = 0

    for (iid, obj_name, uv_map), islands in by_key.items():
        uv_tex = np.zeros((texture_size, texture_size, 4), dtype=np.uint8)
        total_written = 0

        for isl in islands:
            isl_id = isl.get("island_id", "?")

            uv_pts = np.array(isl["corners_uv"][:4], dtype=np.float64)
            layout_pts = np.array(
                isl["corners_layout_px"][:4], dtype=np.float64)

            H = compute_homography(layout_pts, uv_pts)
            if H is None:
                if verbose:
                    print(f"  [{iid}/{obj_name}] {isl_id}: "
                          f"гомография вырождена")
                continue

            x0 = max(0, int(np.floor(layout_pts[:, 0].min())))
            x1 = min(src_w - 1, int(np.ceil(layout_pts[:, 0].max())))
            y0 = max(0, int(np.floor(layout_pts[:, 1].min())))
            y1 = min(src_h - 1, int(np.ceil(layout_pts[:, 1].max())))
            if x1 < x0 or y1 < y0:
                continue

            yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
            px = xx.astype(np.float64).ravel() + 0.5
            py = yy.astype(np.float64).ravel() + 0.5
            n = len(px)

            region = layout_arr[y0:y1 + 1, x0:x1 + 1, :]
            colors = region.reshape(-1, 4)
            has_content = colors[:, 3] > 0
            if not has_content.any():
                continue

            v_h = np.stack([px, py, np.ones(n)], axis=0)
            h = H @ v_h
            w = h[2]
            ok = (np.abs(w) > 1e-12) & has_content
            if not ok.any():
                continue

            u = h[0, ok] / w[ok]
            v = h[1, ok] / w[ok]

            # Blender UV: (0,0) — левый-нижний; sheet PNG: (0,0) — левый-верхний
            tx = np.clip((u * texture_size).astype(np.int64),
                         0, texture_size - 1)
            ty = np.clip(((1.0 - v) * texture_size).astype(np.int64),
                         0, texture_size - 1)

            idx = np.where(ok)[0]
            uv_tex[ty, tx] = colors[idx]
            total_written += len(idx)

            if verbose:
                print(f"  [{iid}/{obj_name} @ {uv_map}] {isl_id}: "
                      f"{len(idx)} px")

        if total_written > 0:
            results[(iid, obj_name, uv_map)] = uv_tex
            total_written_all += total_written
            if verbose:
                print(f"[{iid}/{obj_name} @ {uv_map}] итого: "
                      f"{total_written} px")

    print(f"Всего пикселей: {total_written_all}")
    return results, total_written_all, errors


# ====================== Применение ======================

def _apply_one(bpy_obj, iid, obj_name, uv_map, uv_tex,
               output_dir, verbose=False):
    """Сохранить PNG, создать материал с UV Map node, назначить
    через slot.link='OBJECT' (безопасно для шаренного mesh data)."""
    # 1. Сохраняем PNG
    png_name = f"uv_baked_{iid}__{obj_name}__{uv_map}.png"
    tex_path = os.path.join(output_dir, png_name)
    Image.fromarray(uv_tex, "RGBA").save(tex_path, "PNG")

    # 2. Загружаем в Blender
    img_name = f"UVBaked_{iid}__{obj_name}__{uv_map}"
    if img_name in bpy.data.images:
        bpy.data.images.remove(bpy.data.images[img_name])
    img = bpy.data.images.load(tex_path, check_existing=False)
    img.name = img_name

    # 3. Материал
    mat_name = f"UVBaked_{iid}__{obj_name}__{uv_map}"
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nt = mat.node_tree

    tex_node = None
    uv_node = None
    bsdf = None
    for node in nt.nodes:
        if node.bl_idname == "ShaderNodeTexImage" and tex_node is None:
            tex_node = node
        elif node.bl_idname == "ShaderNodeUVMap" and uv_node is None:
            uv_node = node
        elif node.bl_idname == "ShaderNodeBsdfPrincipled" and bsdf is None:
            bsdf = node

    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)
        out = next((n for n in nt.nodes
                    if n.bl_idname == "ShaderNodeOutputMaterial"), None)
        if out is None:
            out = nt.nodes.new("ShaderNodeOutputMaterial")
            out.location = (300, 0)
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    if uv_node is None:
        uv_node = nt.nodes.new("ShaderNodeUVMap")
        uv_node.location = (-600, 0)
    uv_node.uv_map = uv_map

    if tex_node is None:
        tex_node = nt.nodes.new("ShaderNodeTexImage")
        tex_node.location = (-300, 0)
    tex_node.image = img

    # связи: UVMap.UV → Image.Vector, Image.Color → BSDF.BaseColor
    for l in list(tex_node.inputs["Vector"].links):
        nt.links.remove(l)
    nt.links.new(uv_node.outputs["UV"], tex_node.inputs["Vector"])
    for l in list(bsdf.inputs["Base Color"].links):
        nt.links.remove(l)
    nt.links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])

    # 4. Назначение. Используем slot.link='OBJECT' — материал живёт на
    #    объекте, а не на mesh data. Критично, когда два instance
    #    (hatch_standard, hatch_standard_2) делят один obj.data.
    if len(bpy_obj.data.materials) == 0:
        bpy_obj.data.materials.append(None)
    slot = bpy_obj.material_slots[0]
    slot.link = 'OBJECT'
    slot.material = mat


def apply_to_model(textures, output_dir, verbose=False):
    """Раскладывает textures по объектам сцены. Возвращает (applied, errors)."""
    # Группируем по (iid, obj_name), чтобы поймать случай нескольких uv_map
    by_obj = defaultdict(dict)
    for (iid, obj_name, uv_map), uv_tex in textures.items():
        by_obj[(iid, obj_name)][uv_map] = uv_tex

    applied = 0
    errors = []
    for (iid, obj_name), uv_map_textures in by_obj.items():
        scene_name = f"{iid}__{obj_name}"
        bpy_obj = bpy.data.objects.get(scene_name)
        if bpy_obj is None or bpy_obj.type != 'MESH':
            errors.append(f"объект '{scene_name}' не найден в сцене")
            continue

        if len(uv_map_textures) > 1:
            uvs = ", ".join(uv_map_textures.keys())
            errors.append(
                f"'{scene_name}': несколько uv_map ({uvs}) — "
                f"используется первая, остальные сохранены на диск")
        uv_map, uv_tex = next(iter(uv_map_textures.items()))

        # Проверим, что UV-слой реально существует на меше
        if uv_map not in [l.name for l in bpy_obj.data.uv_layers]:
            errors.append(
                f"'{scene_name}': UV-слой '{uv_map}' не найден на меше "
                f"(есть: {[l.name for l in bpy_obj.data.uv_layers]})")
            # всё равно применяем — материал валиден, пользователь разберётся

        _apply_one(bpy_obj, iid, obj_name, uv_map, uv_tex,
                   output_dir, verbose=verbose)

        # Если у объекта было несколько uv_map — сохраним их PNG на диск
        for other_uv, other_tex in uv_map_textures.items():
            if other_uv == uv_map:
                continue
            png_name = f"uv_baked_{iid}__{obj_name}__{other_uv}.png"
            Image.fromarray(other_tex, "RGBA").save(
                os.path.join(output_dir, png_name), "PNG")

        applied += 1
        if verbose:
            print(f"  {scene_name}: назначен материал (UV: {uv_map})")

    return applied, errors


# ====================== Точки входа ======================

def _run(layout_path, layout_png, output_dir, size,
         apply=True, verbose=False, save_blend="", ):
    print(f"Layout JSON: {layout_path}")
    print(f"Layout PNG:  {layout_png}")
    print(f"Output dir:  {output_dir}")
    print(f"UV size:     {size}×{size}")

    layout = load_json(layout_path)
    layout_img = Image.open(layout_png).convert("RGBA")
    layout_arr = np.array(layout_img)
    print(f"Sheet: {layout_arr.shape[1]}×{layout_arr.shape[0]}")

    os.makedirs(output_dir, exist_ok=True)

    textures, total, load_errors = bake_islands_to_uv(
        layout, layout_arr, size, verbose=verbose)

    if total == 0:
        print("Ничего не записано", file=sys.stderr if not bpy.app.background else sys.stdout)
        return False

    print(f"Сохранено текстур: {len(textures)}")

    if apply:
        applied, apply_errors = apply_to_model(
            textures, output_dir, verbose=verbose)
        print(f"Текстур применено к объектам: {applied}")
        for e in apply_errors:
            print(f"  WARN: {e}")

    if apply and save_blend:
        save_path = os.path.abspath(save_blend)
        bpy.ops.wm.save_as_mainfile(filepath=save_path)
        print(f"Blend сохранён: {save_path}")

    return True


def run_bake_interactive(layout_path, layout_png, output_dir,
                          size=4096, verbose=True):
    ok = _run(layout_path, layout_png, output_dir, size,
              apply=True, verbose=verbose)
    if not ok:
        return
    # Force redraw всех 3D-вьюпортов
    try:
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    except Exception:
        pass
    print("Готово. Переключись в Material Preview (Z)")


def main():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []

    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", required=True)
    ap.add_argument("--layout-png", required=True)
    ap.add_argument("--out", required=True,
                    help="Папка для выходных PNG")
    ap.add_argument("--size", type=int, default=4096)
    ap.add_argument("--no-apply", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--save-blend", default="",
                    help="Путь для сохранения изменённого .blend")
    args = ap.parse_args(argv)

    for path in (args.layout, args.layout_png):
        if not os.path.exists(path):
            print(f"Файл не найден: {path}", file=sys.stderr)
            sys.exit(1)

    ok = _run(args.layout, args.layout_png, args.out, args.size,
              apply=not args.no_apply, verbose=args.verbose,
              save_blend=args.save_blend)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if "--" in sys.argv and bpy.app.background:
        main()
    else:
        # Интерактивный режим — подставьте свои пути и Run Script
        run_bake_interactive(
            layout_path="/path/to/layout.quads.json",
            layout_png="/path/to/sheet_composite.png",
            output_dir="/path/to/uv_baked/",
            size=4096,
            verbose=True,
        )
