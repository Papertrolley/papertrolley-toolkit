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

Читает PNG, полученный из transfer_blender.py, и «разворачивает»
его обратно в UV-пространство модели через гомографии островов.

Запуск:
    blender --background model.blend --python bake_layout_to_uv.py -- \\
        --layout layout.quads.json \\
        --layout-png result.png \\
        --out uv_baked.png \\
        [--size 4096] [--no-apply] [--verbose]
"""

import argparse
import json
import os
import sys

import bpy
import numpy as np
from PIL import Image


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def compute_homography(src_pts, dst_pts):
    A = []
    for (x, y), (u, v) in zip(src_pts, dst_pts):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    A = np.array(A, dtype=np.float64)
    _, _, Vt = np.linalg.svd(A)
    H = Vt[-1].reshape(3, 3)
    n = np.linalg.norm(H)
    return H / n if n > 1e-12 else None


def bake_islands_to_uv_per_object(layout, layout_arr, texture_size,
                                    verbose=False):
    """Возвращает dict: {obj_name: np.array (H, W, 4)}."""
    from collections import defaultdict

    src_h, src_w = layout_arr.shape[:2]

    # Сгруппируем острова по объекту
    by_obj = defaultdict(list)
    for isl in layout.get("islands", []):
        corners = isl.get("corners", [])
        if len(corners) < 4:
            continue
        obj_name = corners[0].get("obj", "")
        if not obj_name:
            continue
        by_obj[obj_name].append(isl)

    print(f"Объектов с островами: {len(by_obj)}")

    results = {}
    total_written_all = 0

    for obj_name, islands in by_obj.items():
        uv_tex = np.zeros((texture_size, texture_size, 4), dtype=np.uint8)
        total_written = 0

        for isl in islands:
            isl_id = isl.get("id", "?")
            corners = isl["corners"]
            layout_px = isl.get("corners_layout_px", [])
            if len(layout_px) < 4:
                continue

            uv_pts = np.array([c["uv"][:2] for c in corners[:4]],
                               dtype=np.float64)
            layout_pts = np.array(layout_px[:4], dtype=np.float64)

            H = compute_homography(layout_pts, uv_pts)
            if H is None:
                if verbose:
                    print(f"  {isl_id}: гомография вырождена")
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

            tx = np.clip((u * texture_size).astype(np.int64),
                         0, texture_size - 1)
            ty = np.clip(((1.0 - v) * texture_size).astype(np.int64),
                         0, texture_size - 1)

            idx = np.where(ok)[0]
            uv_tex[ty, tx] = colors[idx]
            total_written += len(idx)

            if verbose:
                print(f"  [{obj_name}] {isl_id}: {len(idx)} px")

        if total_written > 0:
            results[obj_name] = uv_tex
            total_written_all += total_written
            if verbose:
                print(f"[{obj_name}] итого: {total_written} px")

    print(f"Всего пикселей: {total_written_all}")
    return results, total_written_all


def apply_to_model_per_object(textures, output_dir, verbose=False):
    """Создаёт материалы по одному на объект с правильной текстурой."""
    from collections import defaultdict

    applied = 0
    for obj_name, uv_tex in textures.items():
        obj = bpy.data.objects.get(obj_name)
        if obj is None or obj.type != 'MESH':
            if verbose:
                print(f"  объект '{obj_name}' не найден в сцене")
            continue

        # Сохраняем PNG
        tex_path = os.path.join(output_dir,
                                 f"uv_baked_{obj_name}.png")
        Image.fromarray(uv_tex, "RGBA").save(tex_path, "PNG")

        # Загружаем в Blender
        tex_name = f"UVBaked_{obj_name}"
        if tex_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[tex_name])
        img = bpy.data.images.load(tex_path, check_existing=False)
        img.name = tex_name

        # Материал
        mat_name = f"UVBaked_{obj_name}"
        if mat_name in bpy.data.materials:
            mat = bpy.data.materials[mat_name]
        else:
            mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        nt = mat.node_tree

        tex_node = None
        bsdf = None
        for node in nt.nodes:
            if node.bl_idname == "ShaderNodeTexImage" and tex_node is None:
                tex_node = node
            if node.bl_idname == "ShaderNodeBsdfPrincipled":
                bsdf = node

        if tex_node is None:
            tex_node = nt.nodes.new("ShaderNodeTexImage")
            tex_node.location = (-300, 0)
        tex_node.image = img

        if bsdf is not None:
            for link in list(bsdf.inputs["Base Color"].links):
                nt.links.remove(link)
            nt.links.new(tex_node.outputs["Color"],
                         bsdf.inputs["Base Color"])

        # Назначаем материал объекту
        obj.data.materials.clear()
        obj.data.materials.append(mat)

        applied += 1
        if verbose:
            print(f"  {obj_name}: текстура назначена")

    return applied


def run_bake_interactive(layout_path, layout_png, output_dir,
                          size=4096, verbose=True):
    """Интерактивный запуск: те же действия, но без argparse.

    Вызывается из Text Editor / Python Console в Blender.
    Изменения применяются сразу — Blender открыт, результат виден."""
    import os
    import json
    import numpy as np
    from PIL import Image

    print(f"Layout JSON: {layout_path}")
    print(f"Layout PNG:  {layout_png}")
    print(f"Output dir:  {output_dir}")
    print(f"UV size:     {size}×{size}")

    layout = load_json(layout_path)
    layout_img = Image.open(layout_png).convert("RGBA")
    layout_arr = np.array(layout_img)
    print(f"Sheet: {layout_arr.shape[1]}×{layout_arr.shape[0]}")

    os.makedirs(output_dir, exist_ok=True)

    textures, total = bake_islands_to_uv_per_object(
        layout, layout_arr, size, verbose=verbose)

    if total == 0:
        print("Ничего не записано")
        return

    print(f"Сохранено текстур: {len(textures)}")

    n = apply_to_model_per_object(textures, output_dir,
                                    verbose=verbose)
    print(f"Текстур применено к объектам: {n}")

    # Force redraw всех 3D-вьюпортов
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()
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

    print(f"Layout JSON: {args.layout}")
    print(f"Layout PNG:  {args.layout_png}")
    print(f"Output dir:  {args.out}")
    print(f"UV size:     {args.size}×{args.size}")

    layout = load_json(args.layout)
    layout_img = Image.open(args.layout_png).convert("RGBA")
    layout_arr = np.array(layout_img)
    print(f"Sheet: {layout_arr.shape[1]}×{layout_arr.shape[0]}")

    # Создаём папку
    os.makedirs(args.out, exist_ok=True)

    textures, total = bake_islands_to_uv_per_object(
        layout, layout_arr, args.size, verbose=args.verbose)

    if total == 0:
        print("Ничего не записано", file=sys.stderr)
        sys.exit(1)

    print(f"Сохранено текстур: {len(textures)}")

    if not args.no_apply:
        n = apply_to_model_per_object(textures, args.out,
                                        verbose=args.verbose)
        print(f"Текстур применено к объектам: {n}")
        
    if not args.no_apply and args.save_blend:
        save_path = os.path.abspath(args.save_blend)
        bpy.ops.wm.save_as_mainfile(filepath=save_path)
        print(f"Blend сохранён: {save_path}")


if __name__ == "__main__":
    import sys
    if "--" in sys.argv and bpy.app.background:
        # Режим CLI (blender --background)
        main()
    else:
        # Интерактивный режим — параметры зашиты в коде
        # Меняй эти строки под свои пути и запускай Run Script
        run_bake_interactive(
            layout_path="/path/to/layout.quads.json",
            layout_png="/path/to/result.png",
            output_dir="/path/to/out/",
            size=4096,
            verbose=True,
        )
