# Copyright (C) 2026 Mungojerrie
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-photo visualization: layout PNG → UV-текстура на 3D-модели.

Только для валидации. Не пишет в компоненты, не трогает mesh.data.materials.
Per-object material override, auto-cleanup через PT_Dbg_Viz_ префикс.
"""

import json
import numpy as np
import bpy


VIZ_UV_SIZE = 2048
VIZ_MATERIAL_PREFIX = "PT_Dbg_Viz_"
VIZ_IMAGE_PREFIX = "PT_Dbg_Viz_"


# ====================== Bake: layout → UV ======================

def _compute_homography(src, dst):
    A = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    A = np.array(A, dtype=np.float64)
    _, _, Vt = np.linalg.svd(A)
    H = Vt[-1].reshape(3, 3)
    n = np.linalg.norm(H)
    return H / n if n > 1e-12 else None


def _group_islands(layout):
    from collections import defaultdict
    by_key = defaultdict(list)
    for isl in layout.get("islands", []):
        if ("corners_uv" not in isl
                or "corners_layout_px" not in isl):
            continue
        iid = isl.get("instance_id", "")
        obj_name = isl.get("obj_name", "")
        uv_map = isl.get("uv_map", "")
        if not (iid and obj_name and uv_map):
            continue
        if (len(isl["corners_uv"]) < 4
                or len(isl["corners_layout_px"]) < 4):
            continue
        by_key[(iid, obj_name, uv_map)].append(isl)
    return by_key


def _sample_bilinear_premult(arr, lx, ly):
    """Bilinear-сэмплирование layout RGBA в позициях (lx, ly).
    Премультиплицированная альфа — не даёт тёмных ореолов на границах
    островов. За пределами [0, W-1]×[0, H-1] возвращает alpha=0.

    Возвращает (rgb [N,3] float32 0..255, alpha [N] float32 0..255)."""
    H, W = arr.shape[:2]

    # Пиксельные центры в (x+0.5, y+0.5). Для точки (lx, ly):
    x0f = np.floor(lx - 0.5)
    y0f = np.floor(ly - 0.5)
    fx = lx - 0.5 - x0f
    fy = ly - 0.5 - y0f

    x0 = x0f.astype(np.int64)
    y0 = y0f.astype(np.int64)
    x1 = x0 + 1
    y1 = y0 + 1

    inside = (x0 >= 0) & (x1 < W) & (y0 >= 0) & (y1 < H)

    x0c = np.clip(x0, 0, W - 1)
    x1c = np.clip(x1, 0, W - 1)
    y0c = np.clip(y0, 0, H - 1)
    y1c = np.clip(y1, 0, H - 1)

    p00 = arr[y0c, x0c].astype(np.float32)
    p10 = arr[y0c, x1c].astype(np.float32)
    p01 = arr[y1c, x0c].astype(np.float32)
    p11 = arr[y1c, x1c].astype(np.float32)

    p00_rgb = p00[:, :3] * (p00[:, 3:4] / 255.0)
    p10_rgb = p10[:, :3] * (p10[:, 3:4] / 255.0)
    p01_rgb = p01[:, :3] * (p01[:, 3:4] / 255.0)
    p11_rgb = p11[:, :3] * (p11[:, 3:4] / 255.0)

    fx_ = fx[:, None]
    fy_ = fy[:, None]

    rgb_top = p00_rgb * (1 - fx_) + p10_rgb * fx_
    rgb_bot = p01_rgb * (1 - fx_) + p11_rgb * fx_
    rgb_prem = rgb_top * (1 - fy_) + rgb_bot * fy_

    a_top = p00[:, 3:4] * (1 - fx_) + p10[:, 3:4] * fx_
    a_bot = p01[:, 3:4] * (1 - fx_) + p11[:, 3:4] * fx_
    a = a_top * (1 - fy_) + a_bot * fy_

    a_norm = a / 255.0
    eps = 1e-6
    rgb = np.clip(rgb_prem / np.maximum(a_norm, eps), 0, 255)

    rgb[~inside] = 0
    a[~inside] = 0

    return rgb, a[:, 0]


def bake_to_uv(layout, layout_arr, size):
    """layout PNG → dict[(iid, obj_name, uv_map)] → (size, size, 4) uint8.

    Backward mapping: для каждого UV-текселя вычисляется позиция в
    layout через H⁻¹ и сэмплируется bilinear. Дырок нет — каждый
    тексель внутри UV-острова получает цвет."""
    src_h, src_w = layout_arr.shape[:2]
    by_key = _group_islands(layout)
    results = {}

    for (iid, obj_name, uv_map), islands in by_key.items():
        uv_tex = np.zeros((size, size, 4), dtype=np.uint8)
        total_written = 0

        for isl in islands:
            corners_uv = np.array(isl["corners_uv"][:4],
                                   dtype=np.float64)
            corners_layout = np.array(isl["corners_layout_px"][:4],
                                       dtype=np.float64)

            # layout px → UV (в [0,1]) — та же H, что и раньше
            H = _compute_homography(corners_layout, corners_uv)
            if H is None:
                continue
            try:
                H_inv = np.linalg.inv(H)
            except np.linalg.LinAlgError:
                continue

            uv_min = corners_uv.min(axis=0)
            uv_max = corners_uv.max(axis=0)

            # UV bbox → диапазон текселей (с Y-flip: UV v=1 → ty=0)
            tx0 = max(0, int(np.floor(uv_min[0] * size)))
            tx1 = min(size - 1, int(np.ceil(uv_max[0] * size)))
            ty0 = max(0, int(np.floor((1.0 - uv_max[1]) * size)))
            ty1 = min(size - 1, int(np.ceil((1.0 - uv_min[1]) * size)))
            if tx1 < tx0 or ty1 < ty0:
                continue

            yy, xx = np.mgrid[ty0:ty1 + 1, tx0:tx1 + 1]

            # Центр текселя → UV-координата
            u = (xx.astype(np.float64) + 0.5) / size
            v = 1.0 - (yy.astype(np.float64) + 0.5) / size

            n = u.size
            u_flat = u.ravel()
            v_flat = v.ravel()

            v_h = np.stack(
                [u_flat, v_flat, np.ones(n)], axis=0)
            h = H_inv @ v_h
            w = h[2]
            ok_w = np.abs(w) > 1e-12
            w_safe = np.where(ok_w, w, 1.0)
            lx = h[0] / w_safe
            ly = h[1] / w_safe

            rgb, alpha = _sample_bilinear_premult(layout_arr, lx, ly)

            ok = ok_w & (alpha > 0.5)
            if not ok.any():
                continue

            ty_flat = yy.ravel()[ok]
            tx_flat = xx.ravel()[ok]
            uv_tex[ty_flat, tx_flat, 0:3] = rgb[ok].astype(np.uint8)
            uv_tex[ty_flat, tx_flat, 3] = np.clip(
                alpha[ok], 0, 255).astype(np.uint8)
            total_written += int(ok.sum())

        if total_written > 0:
            results[(iid, obj_name, uv_map)] = uv_tex

    return results


# ====================== Материал ======================

def _create_viz_material(mat_name, img, uv_map):
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (400, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (100, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    tex = nodes.new("ShaderNodeTexImage")
    tex.location = (-300, 0)
    tex.image = img

    uvn = nodes.new("ShaderNodeUVMap")
    uvn.location = (-550, 0)
    uvn.uv_map = uv_map

    links.new(uvn.outputs["UV"], tex.inputs["Vector"])
    links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

    # Мягкий фиолетовый оттенок в solid-режиме — визуальный маркер
    mat.diffuse_color = (0.7, 0.4, 0.7, 1.0)
    return mat


# ====================== Слоты: save / restore ======================

def _save_and_replace_slots(obj, mat):
    """Сохранить исходные слоты в custom prop, заменить на viz."""
    orig = []
    for slot in obj.material_slots:
        orig.append({
            "link": slot.link,
            "material_name": slot.material.name if slot.material else "",
        })
    obj["_pba_viz_orig_slots"] = json.dumps(orig)

    for slot in obj.material_slots:
        slot.link = 'OBJECT'
        slot.material = mat


def _restore_slots(obj):
    if "_pba_viz_orig_slots" not in obj:
        return
    try:
        orig = json.loads(obj["_pba_viz_orig_slots"])
    except Exception:
        orig = []

    for i, entry in enumerate(orig):
        if i < len(obj.material_slots):
            slot = obj.material_slots[i]
            slot.link = entry["link"]
            mat_name = entry["material_name"]
            slot.material = (bpy.data.materials.get(mat_name)
                             if mat_name else None)

    try:
        del obj["_pba_viz_orig_slots"]
    except Exception:
        pass


# ====================== Главные операции ======================

def clear_all_viz():
    """Удалить все viz-материалы/images, восстановить исходные слоты.
    Возвращает (removed_materials, removed_images)."""
    removed_mats = 0
    removed_imgs = 0

    for obj in bpy.data.objects:
        if "_pba_viz_orig_slots" in obj:
            _restore_slots(obj)

    for mat in list(bpy.data.materials):
        if mat.name.startswith(VIZ_MATERIAL_PREFIX):
            try:
                bpy.data.materials.remove(mat)
                removed_mats += 1
            except Exception:
                pass

    for img in list(bpy.data.images):
        if img.name.startswith(VIZ_IMAGE_PREFIX):
            try:
                bpy.data.images.remove(img)
                removed_imgs += 1
            except Exception:
                pass

    return removed_mats, removed_imgs


def create_viz(layout, layout_arr, scene, verbose=True):
    """Создать viz-материалы для всех видимых объектов. Возвращает
    (created, skipped)."""
    clear_all_viz()

    if layout_arr.ndim == 3 and layout_arr.shape[2] >= 4:
        if not (layout_arr[:, :, 3] < 255).any():
            print("[PBA VM] viz WARNING: layout PNG без прозрачности "
                  "(alpha=255 везде), viz может быть неточной")

    textures = bake_to_uv(layout, layout_arr, VIZ_UV_SIZE)
    if verbose:
        print(f"[PBA VM] viz: baked {len(textures)} textures "
              f"at {VIZ_UV_SIZE}×{VIZ_UV_SIZE}")

    created = 0
    skipped = 0

    for (iid, obj_name, uv_map), uv_tex in textures.items():
        if not (uv_tex[:, :, 3] > 0).any():
            if verbose:
                print(f"[PBA VM] viz: skip {iid}/{obj_name}/{uv_map} "
                      f"(empty texture)")
            skipped += 1
            continue

        scene_name = f"{iid}__{obj_name}"
        obj = bpy.data.objects.get(scene_name)
        if obj is None or obj.type != 'MESH':
            if verbose:
                print(f"[PBA VM] viz: skip {scene_name} "
                      f"(object not found)")
            skipped += 1
            continue

        if len(obj.material_slots) == 0:
            if verbose:
                print(f"[PBA VM] viz: skip {scene_name} "
                      f"(no material slots)")
            skipped += 1
            continue

        uv_layer_names = [l.name for l in obj.data.uv_layers]
        if uv_map not in uv_layer_names:
            if verbose:
                print(f"[PBA VM] viz: skip {scene_name} "
                      f"(UV '{uv_map}' not found, have: {uv_layer_names})")
            skipped += 1
            continue

        img_name = f"{VIZ_IMAGE_PREFIX}{iid}_{obj_name}_{uv_map}"
        existing = bpy.data.images.get(img_name)
        if existing is not None:
            bpy.data.images.remove(existing)
        img = bpy.data.images.new(
            img_name, width=VIZ_UV_SIZE, height=VIZ_UV_SIZE, alpha=True)

        # uint8 [0..255] → float [0..1], flip vertically (Blender image
        # начинается с нижнего левого угла, numpy-массив — с верхнего)
        arr_f = uv_tex.astype(np.float32) / 255.0
        img.pixels.foreach_set(arr_f[::-1].ravel())

        mat_name = f"{VIZ_MATERIAL_PREFIX}{iid}_{obj_name}_{uv_map}"
        existing_mat = bpy.data.materials.get(mat_name)
        if existing_mat is not None:
            bpy.data.materials.remove(existing_mat)
        mat = _create_viz_material(mat_name, img, uv_map)

        _save_and_replace_slots(obj, mat)
        created += 1

    return created, skipped
