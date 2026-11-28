# Copyright (C) 2026 Mungojerrie
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-photo visualization: layout PNG → UV-текстура на 3D-модели.

Только для валидации. Не пишет в компоненты, не трогает mesh.data.materials.
Per-object material override, auto-cleanup через PT_Dbg_Viz_ префикс.
"""

import json
import numpy as np
import bpy


VIZ_UV_SIZE = 1024
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


def bake_to_uv(layout, layout_arr, size):
    """layout PNG → dict[(iid, obj_name, uv_map)] → (size, size, 4) uint8."""
    src_h, src_w = layout_arr.shape[:2]
    by_key = _group_islands(layout)
    results = {}

    for (iid, obj_name, uv_map), islands in by_key.items():
        uv_tex = np.zeros((size, size, 4), dtype=np.uint8)
        total_written = 0

        for isl in islands:
            uv_pts = np.array(isl["corners_uv"][:4], dtype=np.float64)
            layout_pts = np.array(isl["corners_layout_px"][:4],
                                   dtype=np.float64)

            H = _compute_homography(layout_pts, uv_pts)
            if H is None:
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

            tx = np.clip((u * size).astype(np.int64), 0, size - 1)
            ty = np.clip(((1.0 - v) * size).astype(np.int64),
                          0, size - 1)

            idx = np.where(ok)[0]
            uv_tex[ty, tx] = colors[idx]
            total_written += len(idx)

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
