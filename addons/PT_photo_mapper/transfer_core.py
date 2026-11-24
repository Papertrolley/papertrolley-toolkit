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
"""Ядро transfer: проекция фото на layout и растеризация."""

import os
import numpy as np
import bpy
from PIL import Image

PBA_VERTEX_ID_ATTR = "pba_vertex_id"
PBA_ORIGINAL_NAME_PROP = "pba_original_name"


def _project_points(P, pts):
    n = pts.shape[0]
    homo = np.concatenate([pts, np.ones((n, 1))], axis=1)
    proj = homo @ P.T
    w = proj[:, 2:3]
    w_safe = np.where(np.abs(w) < 1e-9, 1e-9, w)
    uv = proj[:, :2] / w_safe
    return uv[:, 0], uv[:, 1], w[:, 0]


def _obj_world_vertices(obj):
    me = obj.data
    M = np.array(obj.matrix_world)
    co_local = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co_local)
    co_local = co_local.reshape(-1, 3)
    return (co_local @ M[:3, :3].T) + M[:3, 3]


def _obj_world_vertices_evaluated(obj, depsgraph):
    """Возвращает (verts_world Nx3, tris Kx3) с учётом модификаторов.
    None, None — если объект пуст или не удалось вычислить."""
    try:
        obj_eval = obj.evaluated_get(depsgraph)
    except Exception:
        return None, None
    me = obj_eval.to_mesh()
    if me is None or len(me.vertices) == 0:
        if me is not None:
            obj_eval.to_mesh_clear()
        return None, None
    try:
        M = np.array(obj_eval.matrix_world)
        co_local = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co_local)
        co_local = co_local.reshape(-1, 3)
        verts_world = (co_local @ M[:3, :3].T) + M[:3, 3]
        me.calc_loop_triangles()
        tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
        me.loop_triangles.foreach_get("vertices", tris)
        tris = tris.reshape(-1, 3)
    finally:
        obj_eval.to_mesh_clear()
    return verts_world, tris


def _build_depth_buffer(scene, P, W, H, depsgraph=None, sub=2):
    """Depth buffer с sub×sub субсэмплингом. Возвращает
    (depth_min, depth_max) размера (H, W): минимальная и максимальная
    z по субсэмплам пикселя. Для точки z:
        z <  depth_min·(1+eps)  → видима
        z >  depth_max·(1−eps)  → перекрыта
        иначе                   → граничная зона
    """
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    W_i = int(W)
    H_i = int(H)
    sub_W = W_i * sub
    sub_H = H_i * sub
    depth = np.full((sub_H, sub_W), np.inf, dtype=np.float32)

    for obj in scene.objects:
        if obj.type != 'MESH':
            continue
        verts_world, tris = _obj_world_vertices_evaluated(obj, depsgraph)
        if verts_world is None or len(tris) == 0:
            continue

        u, v, w = _project_points(P, verts_world)
        u_s = u * sub
        v_s = v * sub

        for tri in tris:
            v0, v1, v2 = int(tri[0]), int(tri[1]), int(tri[2])
            if w[v0] <= 0 or w[v1] <= 0 or w[v2] <= 0:
                continue
            _rasterize_depth(
                u_s[v0], v_s[v0], w[v0],
                u_s[v1], v_s[v1], w[v1],
                u_s[v2], v_s[v2], w[v2],
                sub_W, sub_H, depth)

    # Down-sample: min и max по sub×sub блоку
    d_blocks = depth.reshape(H_i, sub, W_i, sub)
    depth_min = d_blocks.min(axis=(1, 3))
    depth_max = d_blocks.max(axis=(1, 3))
    return depth_min, depth_max


def _find_object(instance_id, obj_name):
    if instance_id:
        coll = bpy.data.collections.get(f"Instance_{instance_id}")
        if coll is None:
            return None
        for obj in coll.objects:
            if obj.type != 'MESH':
                continue
            if obj.get(PBA_ORIGINAL_NAME_PROP) == obj_name:
                return obj
        return None
    obj = bpy.data.objects.get(obj_name)
    if obj is not None and obj.type == 'MESH':
        return obj
    return None


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


def _rasterize_depth(u0, v0, z0, u1, v1, z1, u2, v2, z2, W, H, depth):
    xs = (u0, u1, u2)
    ys = (v0, v1, v2)
    x_min = max(0, int(np.floor(min(xs))))
    x_max = min(W - 1, int(np.ceil(max(xs))))
    y_min = max(0, int(np.floor(min(ys))))
    y_max = min(H - 1, int(np.ceil(max(ys))))
    if x_max < x_min or y_max < y_min:
        return

    det = (v1 - v2) * (u0 - u2) + (u2 - u1) * (v0 - v2)
    if abs(det) < 1e-12:
        return

    xx, yy = np.meshgrid(
        np.arange(x_min, x_max + 1) + 0.5,
        np.arange(y_min, y_max + 1) + 0.5)

    a = ((v1 - v2) * (xx - u2) + (u2 - u1) * (yy - v2)) / det
    b = ((v2 - v0) * (xx - u2) + (u0 - u2) * (yy - v2)) / det
    c = 1.0 - a - b

    inside = (a >= 0) & (b >= 0) & (c >= 0)
    if not inside.any():
        return

    zi = a * z0 + b * z1 + c * z2
    sub = depth[y_min:y_max + 1, x_min:x_max + 1]
    update = inside & (zi < sub)
    sub[update] = zi[update]


def _rasterize_colored(p0, p1, p2, xyz, P, W_photo, H_photo,
                        pixels, depth_min, depth_max, output, W_out, H_out):
    xs = (p0[0], p1[0], p2[0])
    ys = (p0[1], p1[1], p2[1])
    x_min = max(0, int(np.floor(min(xs))))
    x_max = min(W_out - 1, int(np.ceil(max(xs))))
    y_min = max(0, int(np.floor(min(ys))))
    y_max = min(H_out - 1, int(np.ceil(max(ys))))
    if x_max < x_min or y_max < y_min:
        return

    det = ((p1[1] - p2[1]) * (p0[0] - p2[0])
           + (p2[0] - p1[0]) * (p0[1] - p2[1]))
    if abs(det) < 1e-12:
        return

    xx, yy = np.meshgrid(
        np.arange(x_min, x_max + 1) + 0.5,
        np.arange(y_min, y_max + 1) + 0.5)

    a = ((p1[1] - p2[1]) * (xx - p2[0]) + (p2[0] - p1[0]) * (yy - p2[1])) / det
    b = ((p2[1] - p0[1]) * (xx - p2[0]) + (p0[0] - p2[0]) * (yy - p2[1])) / det
    c = 1.0 - a - b

    inside = (a >= 0) & (b >= 0) & (c >= 0)
    if not inside.any():
        return

    ai = a[inside][:, None]
    bi = b[inside][:, None]
    ci = c[inside][:, None]
    pts_3d = ai * xyz[0] + bi * xyz[1] + ci * xyz[2]

    u_px, v_px, wv = _project_points(P, pts_3d)

    valid = ((wv > 0)
             & (u_px >= 0) & (u_px < W_photo - 1)
             & (v_px >= 0) & (v_px < H_photo - 1))
    if not valid.any():
        return

    us = np.clip(u_px.astype(np.int64), 0, int(W_photo) - 1)
    vs = np.clip(v_px.astype(np.int64), 0, int(H_photo) - 1)
    z_min = depth_min[vs, us]
    z_max = depth_max[vs, us]

    finite_min = np.isfinite(z_min)
    finite_max = np.isfinite(z_max)

    # Три зоны:
    #   z < min*1.02  — точно видима (ближе любой поверхности)
    #   z > max*0.98  — точно перекрыта (дальше любой поверхности)
    #   между         — граница: считаем видимой (мягкая проверка),
    #                   позднее можно заменить на ray cast (Вариант C)
    definitely_visible = ~finite_min | (wv < z_min * 1.02)
    definitely_hidden = finite_max & (wv > z_max * 0.98)
    uncertain_visible = ~(definitely_visible | definitely_hidden)

    visible = definitely_visible | uncertain_visible
    valid = valid & visible

    if not valid.any():
        return

    ux = u_px[valid]
    vy = v_px[valid]
    x_i = np.clip(np.floor(ux).astype(np.int64), 0, int(W_photo) - 2)
    y_i = np.clip(np.floor(vy).astype(np.int64), 0, int(H_photo) - 2)
    dx = (ux - x_i)[:, None]
    dy = (vy - y_i)[:, None]

    p00 = pixels[y_i, x_i]
    p10 = pixels[y_i, x_i + 1]
    p01 = pixels[y_i + 1, x_i]
    p11 = pixels[y_i + 1, x_i + 1]
    top = p00 * (1 - dx) + p10 * dx
    bot = p01 * (1 - dx) + p11 * dx
    col = top * (1 - dy) + bot * dy

    y_final = yy[inside][valid].astype(np.int64)
    x_final = xx[inside][valid].astype(np.int64)

    in_bounds = ((y_final >= 0) & (y_final < H_out)
                 & (x_final >= 0) & (x_final < W_out))
    if not in_bounds.any():
        return

    y_final = y_final[in_bounds]
    x_final = x_final[in_bounds]
    col = col[in_bounds]

    output[y_final, x_final, 0:3] = np.clip(col, 0, 255).astype(np.uint8)
    output[y_final, x_final, 3] = 255


def process_photo(scene, layout, image, camera_obj, out_dir,
                   f_px, off_x, off_y, verbose=False):
    W_photo = float(image.size[0])
    H_photo = float(image.size[1])
    if W_photo == 0 or H_photo == 0:
        raise RuntimeError("Изображение пустое")

    # Читаем фото через PIL — sRGB-значения, как в файле.
    photo_path = bpy.path.abspath(image.filepath)
    try:
        pil_img = Image.open(photo_path).convert("RGB")
        pixels = np.array(pil_img, dtype=np.float32)
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать фото: {e}")

    # P-матрица (world → пиксели фото)
    M_cv = np.diag([1.0, -1.0, -1.0, 1.0]) @ np.linalg.inv(
        np.array(camera_obj.matrix_world))
    K = np.array([
        [f_px, 0.0, W_photo / 2.0 + off_x],
        [0.0, f_px, H_photo / 2.0 + off_y],
        [0.0, 0.0, 1.0],
    ])
    P = K @ M_cv[:3, :]

    page = layout.get("page", {})
    W_out = int(page.get("width_px", 4960))
    H_out = int(page.get("height_px", 7016))

    if verbose:
        print(f"  depth buffer {int(W_photo)}×{int(H_photo)}...")
    depth_min, depth_max = _build_depth_buffer(scene, P, W_photo, H_photo)

    output = np.zeros((H_out, W_out, 4), dtype=np.uint8)
    stats = {
        "ok": 0, "bad_corners": 0, "no_object": 0,
        "no_uv_layer": 0, "no_attr": 0, "no_vertices": 0,
        "bad_homography": 0, "no_triangles": 0,
    }

    for isl in layout.get("islands", []):
        instance_id = isl.get("instance_id", "")
        obj_name = isl.get("obj_name", "")
        uv_map = isl.get("uv_map", "")
        corners_uv = isl.get("corners_uv", [])
        corners_layout = isl.get("corners_layout_px", [])
        vertices_data = isl.get("vertices", [])

        if len(corners_uv) != 4 or len(corners_layout) != 4:
            stats["bad_corners"] += 1
            continue

        obj = _find_object(instance_id, obj_name)
        if obj is None:
            stats["no_object"] += 1
            continue

        uv_layer = obj.data.uv_layers.get(uv_map) if uv_map else None
        if uv_layer is None:
            stats["no_uv_layer"] += 1
            continue

        attr = obj.data.attributes.get(PBA_VERTEX_ID_ATTR)
        if attr is None:
            stats["no_attr"] += 1
            continue

        uuid_to_idx = {}
        for i in range(len(attr.data)):
            uuid_to_idx[int(attr.data[i].value)] = i

        vert_indices = set()
        for vd in vertices_data:
            uuid_val = int(vd.get("local_uuid", 0))
            if uuid_val in uuid_to_idx:
                vert_indices.add(uuid_to_idx[uuid_val])
        if not vert_indices:
            stats["no_vertices"] += 1
            continue

        H_hom = _compute_homography(corners_uv, corners_layout)
        if H_hom is None:
            stats["bad_homography"] += 1
            continue

        uv_min_u = min(c[0] for c in corners_uv)
        uv_max_u = max(c[0] for c in corners_uv)
        uv_min_v = min(c[1] for c in corners_uv)
        uv_max_v = max(c[1] for c in corners_uv)

        verts_world = _obj_world_vertices(obj)
        me = obj.data
        me.calc_loop_triangles()

        touched = False
        for lt in me.loop_triangles:
            v0, v1, v2 = lt.vertices
            if (v0 not in vert_indices
                    or v1 not in vert_indices
                    or v2 not in vert_indices):
                continue

            layout_pts = []
            uv_in_bbox = True
            for li in lt.loops:
                uv = uv_layer.data[li].uv
                if not (uv_min_u - 1e-6 <= uv.x <= uv_max_u + 1e-6
                        and uv_min_v - 1e-6 <= uv.y <= uv_max_v + 1e-6):
                    uv_in_bbox = False
                    break
                h = H_hom @ np.array([uv.x, uv.y, 1.0])
                if abs(h[2]) < 1e-12:
                    uv_in_bbox = False
                    break
                layout_pts.append((h[0]/h[2], h[1]/h[2]))

            if not uv_in_bbox:
                continue

            xyz = verts_world[[v0, v1, v2]]
            _rasterize_colored(
                    layout_pts[0], layout_pts[1], layout_pts[2],
                    xyz, P, W_photo, H_photo, pixels, depth_min, depth_max,
                    output, W_out, H_out)
            touched = True

        if touched:
            stats["ok"] += 1
        else:
            stats["no_triangles"] += 1

    os.makedirs(out_dir, exist_ok=True)
    basename = os.path.splitext(os.path.basename(photo_path))[0]
    out_path = os.path.join(out_dir, basename + ".png")
    Image.fromarray(output, "RGBA").save(out_path, "PNG")
    return out_path, stats


def _process_photo_core(layout, pixels, W_photo, H_photo,
                        matrix_world_flat, f_px, off_x, off_y,
                        scene, out_path, verbose=False):
    """Общее ядро: принимает всё примитивами, не зависит от
    bpy.types.Image или bpy.types.Object. Используется и интерактивным
    transfer (Photo Mapper), и CLI (transfer_blender.py).
    """
    M = np.array(matrix_world_flat, dtype=np.float64).reshape(4, 4)
    M_cv = np.diag([1.0, -1.0, -1.0, 1.0]) @ np.linalg.inv(M)
    K = np.array([
        [f_px, 0.0, W_photo / 2.0 + off_x],
        [0.0, f_px, H_photo / 2.0 + off_y],
        [0.0, 0.0, 1.0],
    ])
    P = K @ M_cv[:3, :]

    page = layout.get("page", {})
    W_out = int(page.get("width_px", 4960))
    H_out = int(page.get("height_px", 7016))

    if pixels.dtype != np.float32:
        pixels = pixels.astype(np.float32)

    if verbose:
        print(f"  depth buffer {int(W_photo)}×{int(H_photo)}...")
    depth_min, depth_max = _build_depth_buffer(scene, P, W_photo, H_photo)

    output = np.zeros((H_out, W_out, 4), dtype=np.uint8)
    stats = {
        "ok": 0, "bad_corners": 0, "no_object": 0,
        "no_uv_layer": 0, "no_attr": 0, "no_vertices": 0,
        "bad_homography": 0, "no_triangles": 0,
    }

    for isl in layout.get("islands", []):
        instance_id = isl.get("instance_id", "")
        obj_name = isl.get("obj_name", "")
        uv_map = isl.get("uv_map", "")
        corners_uv = isl.get("corners_uv", [])
        corners_layout = isl.get("corners_layout_px", [])
        vertices_data = isl.get("vertices", [])

        if len(corners_uv) != 4 or len(corners_layout) != 4:
            stats["bad_corners"] += 1
            continue

        obj = _find_object(instance_id, obj_name)
        if obj is None:
            stats["no_object"] += 1
            if verbose and stats["no_object"] == 1:
                print(f"  [DEBUG] island '{isl.get('island_id')}': "
                      f"объект не найден "
                      f"(instance='{instance_id}', obj='{obj_name}')")
            continue

        uv_layer = obj.data.uv_layers.get(uv_map) if uv_map else None
        if uv_layer is None:
            stats["no_uv_layer"] += 1
            if verbose and stats["no_uv_layer"] == 1:
                print(f"  [DEBUG] island '{isl.get('island_id')}': "
                      f"нет UV '{uv_map}' у {obj.name}. "
                      f"Есть: {[l.name for l in obj.data.uv_layers]}")
            continue

        attr = obj.data.attributes.get(PBA_VERTEX_ID_ATTR)
        if attr is None:
            stats["no_attr"] += 1
            continue

        uuid_to_idx = {}
        for i in range(len(attr.data)):
            uuid_to_idx[int(attr.data[i].value)] = i

        vert_indices = set()
        for vd in vertices_data:
            uuid_val = int(vd.get("local_uuid", 0))
            if uuid_val in uuid_to_idx:
                vert_indices.add(uuid_to_idx[uuid_val])
        if not vert_indices:
            stats["no_vertices"] += 1
            continue

        H_hom = _compute_homography(corners_uv, corners_layout)
        if H_hom is None:
            stats["bad_homography"] += 1
            continue

        uv_min_u = min(c[0] for c in corners_uv)
        uv_max_u = max(c[0] for c in corners_uv)
        uv_min_v = min(c[1] for c in corners_uv)
        uv_max_v = max(c[1] for c in corners_uv)

        verts_world = _obj_world_vertices(obj)
        me = obj.data
        me.calc_loop_triangles()

        touched = False
        for lt in me.loop_triangles:
            v0, v1, v2 = lt.vertices
            if (v0 not in vert_indices
                    or v1 not in vert_indices
                    or v2 not in vert_indices):
                continue

            layout_pts = []
            uv_in_bbox = True
            for li in lt.loops:
                uv = uv_layer.data[li].uv
                if not (uv_min_u - 1e-6 <= uv.x <= uv_max_u + 1e-6
                        and uv_min_v - 1e-6 <= uv.y <= uv_max_v + 1e-6):
                    uv_in_bbox = False
                    break
                h = H_hom @ np.array([uv.x, uv.y, 1.0])
                if abs(h[2]) < 1e-12:
                    uv_in_bbox = False
                    break
                layout_pts.append((h[0] / h[2], h[1] / h[2]))

            if not uv_in_bbox:
                continue

            xyz = verts_world[[v0, v1, v2]]
            _rasterize_colored(
                layout_pts[0], layout_pts[1], layout_pts[2],
                xyz, P, W_photo, H_photo, pixels, depth_min, depth_max,
                output, W_out, H_out)
            touched = True

        if touched:
            stats["ok"] += 1
        else:
            stats["no_triangles"] += 1

    Image.fromarray(output, "RGBA").save(out_path, "PNG")
    return stats


def process_photo(scene, layout, image, camera_obj, out_dir,
                  f_px, off_x, off_y, verbose=False):
    """Интерактивный transfer: Image + Object из сцены."""
    W_photo = float(image.size[0])
    H_photo = float(image.size[1])
    if W_photo == 0 or H_photo == 0:
        raise RuntimeError("Изображение пустое")

    photo_path = bpy.path.abspath(image.filepath)
    try:
        pil_img = Image.open(photo_path).convert("RGB")
        pixels = np.array(pil_img, dtype=np.float32)
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать фото: {e}")

    os.makedirs(out_dir, exist_ok=True)
    basename = os.path.splitext(os.path.basename(photo_path))[0]
    out_path = os.path.join(out_dir, basename + ".png")

    matrix_flat = [float(camera_obj.matrix_world[i][j])
                   for i in range(4) for j in range(4)]
    stats = _process_photo_core(
        layout, pixels, W_photo, H_photo, matrix_flat,
        f_px, off_x, off_y, scene, out_path, verbose=verbose)
    return out_path, stats


def process_photo_from_meta(scene, layout, photo_path, meta_path,
                             out_dir, verbose=False):
    """CLI-обёртка: всё читается из файлов (фото + *.vertices.json).
    Используется в transfer_blender.py для batch-обработки."""
    import json as _json

    W_photo, H_photo, matrix_flat, f_px, off_x, off_y = \
        _parse_meta(meta_path)

    try:
        pil_img = Image.open(photo_path).convert("RGB")
        pixels = np.array(pil_img, dtype=np.float32)
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать фото: {e}")

    # Если размеры в meta не совпадают с фактическими — доверяем meta
    # (meta содержит правильные W/H, в т.ч. с учётом кропа).
    if pixels.shape[0] != int(H_photo) or pixels.shape[1] != int(W_photo):
        # Часто бывает, что meta снята с другого разрешения — предупреждаем
        print(f"  ВНИМАНИЕ: meta W×H={int(W_photo)}×{int(H_photo)}, "
              f"файл {pixels.shape[1]}×{pixels.shape[0]}. "
              f"Использую meta.")

    os.makedirs(out_dir, exist_ok=True)
    basename = os.path.splitext(os.path.basename(photo_path))[0]
    out_path = os.path.join(out_dir, basename + ".png")

    stats = _process_photo_core(
        layout, pixels, W_photo, H_photo, matrix_flat,
        f_px, off_x, off_y, scene, out_path, verbose=verbose)
    return out_path, stats


def _parse_meta(meta_path):
    """Читает *.vertices.json, возвращает примитивы для ядра."""
    import json as _json
    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = _json.load(f)

    cam = meta["camera"]
    matrix_flat = cam["matrix_world"]
    if len(matrix_flat) != 16:
        raise ValueError(
            f"matrix_world в {meta_path}: {len(matrix_flat)} чисел, "
            f"нужно 16")

    photo = meta["photo"]
    W = float(photo["width_px"])
    H = float(photo["height_px"])
    nw = float(photo.get("native_width_px", W))
    nh = float(photo.get("native_height_px", H))
    off_x = float(photo.get("crop_offset_x", 0))
    off_y = float(photo.get("crop_offset_y", 0))

    solve = meta.get("solve", {})
    f_px = float(solve.get("f_px", 0.0))
    if f_px <= 0:
        lens = float(cam.get("lens_mm", 50.0))
        sensor = float(cam.get("sensor_width_mm", 36.0))
        # sensor_width_mm соответствует длинной стороне сенсора
        # (sensor_fit='AUTO' в Blender). Для portrait max(nw, nh) = nh.
        f_px = lens / sensor * max(nw, nh)
        print(f"  f_px=0 в meta, fallback: {f_px:.1f} "
              f"(lens={lens}, sensor={sensor}, "
              f"native={int(nw)}×{int(nh)})")

    return W, H, matrix_flat, f_px, off_x, off_y
