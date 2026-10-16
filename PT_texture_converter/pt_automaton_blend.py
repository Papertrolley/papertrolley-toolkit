#!/usr/bin/env python3
"""
PBA Texture Transfer (Blender edition).

Читает модель из открытого .blend напрямую через bpy.
Не требует OBJ или иных промежуточных форматов.

Запуск:
    blender --background model.blend --python transfer_blender.py -- \\
        --layout <layout.quads.json> \\
        --uv-json <layout_uv.uv.json> \\
        --photo <photo.jpg> --photo-meta <photo.vertices.json> \\
        [--photo ... --photo-meta ...] \\
        --out <result.png> \\
        [--bg layout|transparent] [--verbose]
"""

import argparse
import json
import os
import sys

import bpy
import numpy as np
from PIL import Image


PBA_VERTEX_ID_ATTR = "pba_vertex_id"


# =============================================================
# Чтение модели из bpy
# =============================================================

def get_mesh_data(obj_name, verbose=False):
    """Возвращает dict с vertices, uvs, triangles для объекта Blender.

    - vertices: (N, 3) мировые координаты
    - uvs:      (M, 2) UV-координаты всех петель
    - triangles: list of (v0, v1, v2, t0, t1, t2)
    - uuid_by_index: (N,) int массив UUID каждой вершины (0 если нет)
    """
    obj = bpy.data.objects.get(obj_name)
    if obj is None or obj.type != 'MESH':
        if verbose:
            print(f"[read] объект '{obj_name}' не найден")
        return None

    me = obj.data
    if len(me.vertices) == 0:
        return None

    # Вершины в мировых координатах
    M = np.array(obj.matrix_world)
    co_local = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co_local)
    co_local = co_local.reshape(-1, 3)
    verts_world = (co_local @ M[:3, :3].T) + M[:3, 3]

    # UV-слой
    uv_layer = me.uv_layers.active
    if uv_layer is None:
        if verbose:
            print(f"[read] '{obj_name}': нет активного UV-слоя")
        return None
    uvs = np.empty(len(uv_layer.data) * 2, dtype=np.float64)
    uv_layer.data.foreach_get("uv", uvs)
    uvs = uvs.reshape(-1, 2)

    # Триангулированные грани
    me.calc_loop_triangles()
    tris = []
    for lt in me.loop_triangles:
        v0, v1, v2 = lt.vertices
        l0, l1, l2 = lt.loops
        tris.append((v0, v1, v2, l0, l1, l2))

    # UUID по индексу вершины
    uuid_by_index = np.zeros(len(me.vertices), dtype=np.int64)
    attr = me.attributes.get(PBA_VERTEX_ID_ATTR)
    if attr is not None and len(attr.data) > 0:
        vals = np.empty(len(attr.data), dtype=np.int32)
        attr.data.foreach_get("value", vals)
        uuid_by_index = vals.astype(np.int64)

    return {
        "obj": obj,
        "verts_world": verts_world,
        "uvs": uvs,
        "triangles": tris,
        "uuid_by_index": uuid_by_index,
    }


def find_vertex_index_by_uuid(mesh_data, uuid_val):
    """Индекс вершины по UUID или -1."""
    if uuid_val <= 0:
        return -1
    idxs = np.where(mesh_data["uuid_by_index"] == uuid_val)[0]
    if len(idxs) == 0:
        return -1
    return int(idxs[0])


# =============================================================
# Чтение JSON-ов
# =============================================================

def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_photo_pixels(path):
    img = Image.open(path).convert("RGB")
    return np.array(img, dtype=np.float32), img.size


def make_camera_matrix(photo_meta):
    """P (3x4) — world → пиксели фото."""
    cam = photo_meta["camera"]
    m = np.array(cam["matrix_world"], dtype=np.float64).reshape(4, 4)
    m_inv = np.linalg.inv(m)
    D = np.diag([1.0, -1.0, -1.0, 1.0])
    m_cv = D @ m_inv

    W = float(photo_meta["photo"]["width_px"])
    H = float(photo_meta["photo"]["height_px"])
    f_px = float(photo_meta["solve"]["f_px"])
    if f_px <= 0:
        lens = float(cam["lens_mm"])
        sensor = float(cam["sensor_width_mm"])
        nw = float(photo_meta["photo"].get("native_width_px", W))
        nh = float(photo_meta["photo"].get("native_height_px", H))
        f_px = lens / sensor * max(nw, nh)

    K = np.array([
        [f_px, 0.0, W / 2.0],
        [0.0, f_px, H / 2.0],
        [0.0, 0.0, 1.0],
    ])
    return K @ m_cv[:3, :], W, H


# =============================================================
# Гомография
# =============================================================

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


def point_in_quad(px, py, corners):
    signs = []
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        signs.append((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1))
    signs = np.stack(signs, axis=0)
    return np.all(signs >= 0, axis=0) | np.all(signs <= 0, axis=0)


# =============================================================
# Проекция
# =============================================================

def project_points(P, pts_xyz):
    n = pts_xyz.shape[0]
    homo = np.concatenate([pts_xyz, np.ones((n, 1))], axis=1)
    proj = homo @ P.T
    w = proj[:, 2:3]
    w_safe = np.where(np.abs(w) < 1e-9, 1e-9, w)
    uv = proj[:, :2] / w_safe
    return uv[:, 0], uv[:, 1], w[:, 0]


# =============================================================
# Depth buffer
# =============================================================

def build_depth_buffer(mesh_data, P, W, H):
    """Растеризует все треугольники модели из камеры. Возвращает
    (depth, W, H) с float32, scale=1."""
    W_i = int(W)
    H_i = int(H)
    depth = np.full((H_i, W_i), np.inf, dtype=np.float32)

    verts = mesh_data["verts_world"]
    u, v, w = project_points(P, verts)

    for (v0, v1, v2, _t0, _t1, _t2) in mesh_data["triangles"]:
        if w[v0] <= 0 or w[v1] <= 0 or w[v2] <= 0:
            continue
        _rasterize_depth_tri(
            u[v0], v[v0], w[v0],
            u[v1], v[v1], w[v1],
            u[v2], v[v2], w[v2],
            W_i, H_i, depth)

    return depth, W_i, H_i


def _rasterize_depth_tri(u0, v0, z0, u1, v1, z1, u2, v2, z2,
                          W, H, depth):
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


# =============================================================
# Поиск UV-острова
# =============================================================

def find_island_triangles(mesh_data, seed_uvs, tol=1e-4):
    """BFS по UV-связности.

    Соседство определяется одинаковой (вершина, UV) парой петель —
    так же, как в Layout Mapper. Это корректно работает для
    произвольных развёрток."""
    from collections import defaultdict

    uvs = mesh_data["uvs"]
    tris = mesh_data["triangles"]

    # Ключ: (индекс вершины, округлённая UV). Треугольники,
    # разделяющие такую петлю, считаются соседями.
    def key(v, t):
        return (int(v), round(float(uvs[t, 0]), 6),
                round(float(uvs[t, 1]), 6))

    # Ключ UV-координаты → треугольники (для seed)
    uv_only_to_tris = defaultdict(list)
    # Ключ (vertex, UV) → треугольники (для BFS)
    loopkey_to_tris = defaultdict(list)

    for ti, (v0, v1, v2, t0, t1, t2) in enumerate(tris):
        for v, t in ((v0, t0), (v1, t1), (v2, t2)):
            k = key(v, t)
            loopkey_to_tris[k].append(ti)
            uv_key = (round(float(uvs[t, 0]), 6),
                      round(float(uvs[t, 1]), 6))
            uv_only_to_tris[uv_key].append(ti)

    # Seed-треугольники
    seeds = set()
    for (u, v) in seed_uvs:
        u_r = round(float(u), 6)
        v_r = round(float(v), 6)
        best_tris = uv_only_to_tris.get((u_r, v_r))
        if best_tris:
            seeds.update(best_tris)
            continue
        # Fallback: ближайшая UV с допуском
        d = np.linalg.norm(uvs - np.array([u, v]), axis=1)
        idx = int(np.argmin(d))
        if d[idx] < tol:
            for k, tlist in uv_only_to_tris.items():
                if abs(k[0] - u_r) < 1e-6 and abs(k[1] - v_r) < 1e-6:
                    seeds.update(tlist)
                    break

    if not seeds:
        return set()

    visited = set(seeds)
    queue = list(seeds)
    head = 0
    while head < len(queue):
        ti = queue[head]
        head += 1
        v0, v1, v2, t0, t1, t2 = tris[ti]
        for v, t in ((v0, t0), (v1, t1), (v2, t2)):
            k = key(v, t)
            for other in loopkey_to_tris[k]:
                if other not in visited:
                    visited.add(other)
                    queue.append(other)

    return visited


# =============================================================
# Обработка островов
# =============================================================

def _expand_island_region(output, y0, y1, x0, x1,
                           ys, xs, colors,
                           iterations=2, alpha_value=255):
    """Расширяет пиксели острова на N пикселей в 8 направлениях.

    Расширение записывается только в те пиксели, где ещё нет
    никакого контента (output.alpha == 0). Основные пиксели
    уже записаны и не перезаписываются."""
    region_h = y1 - y0 + 1
    region_w = x1 - x0 + 1

    buf_color = np.zeros((region_h, region_w, 3), dtype=np.uint8)
    buf_alpha = np.zeros((region_h, region_w), dtype=np.uint8)

    ys_local = ys - y0
    xs_local = xs - x0
    valid = ((ys_local >= 0) & (ys_local < region_h)
             & (xs_local >= 0) & (xs_local < region_w))
    ys_local = ys_local[valid]
    xs_local = xs_local[valid]
    cols_l = colors[valid]

    buf_color[ys_local, xs_local] = cols_l
    buf_alpha[ys_local, xs_local] = 255

    shifts = [(-1, -1), (-1, 0), (-1, 1),
              (0, -1),           (0, 1),
              (1, -1),  (1, 0),  (1, 1)]

    for _ in range(iterations):
        prev_color = buf_color.copy()
        prev_alpha = buf_alpha.copy()

        for dy, dx in shifts:
            # Диапазоны src и dst
            if dy > 0:
                src_y = slice(0, region_h - dy)
                dst_y = slice(dy, region_h)
            elif dy < 0:
                src_y = slice(-dy, region_h)
                dst_y = slice(0, region_h + dy)
            else:
                src_y = slice(0, region_h)
                dst_y = slice(0, region_h)
            if dx > 0:
                src_x = slice(0, region_w - dx)
                dst_x = slice(dx, region_w)
            elif dx < 0:
                src_x = slice(-dx, region_w)
                dst_x = slice(0, region_w + dx)
            else:
                src_x = slice(0, region_w)
                dst_x = slice(0, region_w)

            sub_c = buf_color[dst_y, dst_x]
            sub_a = buf_alpha[dst_y, dst_x]
            src_c = prev_color[src_y, src_x]
            src_a = prev_alpha[src_y, src_x]

            mask = (sub_a == 0) & (src_a > 0)
            if not mask.any():
                continue

            new_c = np.where(mask[..., None], src_c, sub_c)
            new_a = np.where(mask, alpha_value, sub_a)

            buf_color[dst_y, dst_x] = new_c
            buf_alpha[dst_y, dst_x] = new_a

    # Записываем в output только там, где ещё пусто
    out_region = output[y0:y1 + 1, x0:x1 + 1]
    out_a = out_region[..., 3]

    write_mask = (buf_alpha > 0) & (out_a == 0)
    if not write_mask.any():
        return

    out_region[write_mask, 0:3] = buf_color[write_mask]
    out_region[write_mask, 3] = buf_alpha[write_mask]

def process_islands(layout, uv_meta, mesh_cache, photos, output,
                     W_out, H_out, verbose=False, expand_n=0, expand_alpha=255):
    objects_meta = {o["name"]: o for o in uv_meta["objects"]}
    
    # Нормали всех треугольников меша (для выбора ракурса)
    tri_normal_cache = {}

    def _get_tri_normal(md, tri_id):
        key = (md["obj"].name, tri_id)
        if key in tri_normal_cache:
            return tri_normal_cache[key]
        v0, v1, v2, _, _, _ = md["triangles"][tri_id]
        p0 = md["verts_world"][v0]
        p1 = md["verts_world"][v1]
        p2 = md["verts_world"][v2]
        n = np.cross(p1 - p0, p2 - p0)
        ln = np.linalg.norm(n)
        if ln > 1e-12:
            n = n / ln
        tri_normal_cache[key] = n
        return n

    for isl in layout.get("islands", []):
        isl_id = isl.get("id", "?")
        corners = isl["corners"]
        layout_px = np.array(isl["corners_layout_px"], dtype=np.float64)
        obj_name = corners[0]["obj"]

        md = mesh_cache.get(obj_name)
        if md is None:
            print(f"  {isl_id}: объект '{obj_name}' не найден")
            continue

        # Гомография layout_px → UV
        uv_src = np.array([c["uv"] for c in corners], dtype=np.float64)
        H_l2uv = compute_homography(layout_px, uv_src)
        if H_l2uv is None:
            print(f"  {isl_id}: гомография не сошлась")
            continue

        # Треугольники острова по BFS
        island_tris = find_island_triangles(mesh_data=md, seed_uvs=uv_src)
        if not island_tris:
            print(f"  {isl_id}: остров не найден в BFS")
            continue

        # Собираем UV и XYZ треугольников
        tris = md["triangles"]
        uvs = md["uvs"]
        verts = md["verts_world"]

        tri_uv = []
        tri_xyz = []
        for ti in island_tris:
            v0, v1, v2, t0, t1, t2 = tris[ti]
            tri_uv.append([uvs[t0], uvs[t1], uvs[t2]])
            tri_xyz.append([verts[v0], verts[v1], verts[v2]])

        tri_uv = np.array(tri_uv, dtype=np.float64)
        tri_xyz = np.array(tri_xyz, dtype=np.float64)

        # UV bbox острова → bbox в layout
        all_uv = tri_uv.reshape(-1, 2)
        uv_min = all_uv.min(axis=0)
        uv_max = all_uv.max(axis=0)

        try:
            H_uv2l = np.linalg.inv(H_l2uv)
        except np.linalg.LinAlgError:
            print(f"  {isl_id}: гомография вырождена")
            continue

        corners_uv = np.array([
            [uv_min[0], uv_min[1]],
            [uv_max[0], uv_min[1]],
            [uv_max[0], uv_max[1]],
            [uv_min[0], uv_max[1]],
        ])
        corners_layout = []
        for (u, v) in corners_uv:
            vh = H_uv2l @ np.array([u, v, 1.0])
            if abs(vh[2]) < 1e-12:
                break
            corners_layout.append((vh[0] / vh[2], vh[1] / vh[2]))
        if len(corners_layout) != 4:
            continue
        corners_layout = np.array(corners_layout)

        x0 = max(0, int(np.floor(corners_layout[:, 0].min())) - 1)
        x1 = min(W_out - 1, int(np.ceil(corners_layout[:, 0].max())) + 1)
        y0 = max(0, int(np.floor(corners_layout[:, 1].min())) - 1)
        y1 = min(H_out - 1, int(np.ceil(corners_layout[:, 1].max())) + 1)
        if x1 < x0 or y1 < y0:
            continue

        yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
        fx = xx.astype(np.float64).ravel() + 0.5
        fy = yy.astype(np.float64).ravel() + 0.5
        n = fx.size

        v_hom = np.stack([fx, fy, np.ones(n)], axis=0)
        h = H_l2uv @ v_hom
        w = h[2]
        ok_w = np.abs(w) > 1e-12
        u_arr = np.full(n, np.nan)
        v_arr = np.full(n, np.nan)
        u_arr[ok_w] = h[0, ok_w] / w[ok_w]
        v_arr[ok_w] = h[1, ok_w] / w[ok_w]

        # Триангуляция
        xyz_result = np.full((n, 3), np.nan, dtype=np.float64)
        filled = np.zeros(n, dtype=bool)
        tri_id_result = np.full(n, -1, dtype=np.int64)
        island_tri_list = list(island_tris)

        T = tri_uv.shape[0]
        for t in range(T):
            tuv = tri_uv[t]
            txyz = tri_xyz[t]
            p0, p1, p2 = tuv[0], tuv[1], tuv[2]
            det = ((p1[1] - p2[1]) * (p0[0] - p2[0])
                   + (p2[0] - p1[0]) * (p0[1] - p2[1]))
            if abs(det) < 1e-14:
                continue

            a = ((p1[1] - p2[1]) * (u_arr - p2[0])
                 + (p2[0] - p1[0]) * (v_arr - p2[1])) / det
            b = ((p2[1] - p0[1]) * (u_arr - p2[0])
                 + (p0[0] - p2[0]) * (v_arr - p2[1])) / det
            c = 1.0 - a - b

            inside = (a >= 0) & (b >= 0) & (c >= 0) & ok_w & (~filled)
            if not inside.any():
                continue

            idx_in = np.where(inside)[0]
            ai = a[idx_in][:, None]
            bi = b[idx_in][:, None]
            ci = c[idx_in][:, None]
            xyz_result[idx_in] = (ai * txyz[0] + bi * txyz[1]
                                   + ci * txyz[2])
            filled[idx_in] = True
            tri_id_result[idx_in] = island_tri_list[t]

        if not filled.any():
            if verbose:
                print(f"  {isl_id}: пиксели не попали в треугольники")
            continue

        idx_filled = np.where(filled)[0]
        xyz = xyz_result[idx_filled]

        # Проекция на фото
        # Проекция на фото с выбором лучшего ракурса
        idx_filled = np.where(filled)[0]
        xyz = xyz_result[idx_filled]
        tri_ids = tri_id_result[idx_filled]

        # Нормаль каждого пикселя
        normals = np.zeros((len(tri_ids), 3), dtype=np.float64)
        for k, ti in enumerate(tri_ids):
            if ti < 0:
                continue
            normals[k] = _get_tri_normal(md, ti)

        # best_cos[k] = cos лучшего ракурса для пикселя k
        best_cos = np.full(len(xyz), -2.0, dtype=np.float64)
        best_photo = np.full(len(xyz), -1, dtype=np.int64)

        # ==================================================
        # ФАЗА 1: cos и UV-координаты для каждого фото
        # ==================================================
        n_photos = len(photos)
        SINGLE_PHOTO_LAX = (n_photos == 1)

        if SINGLE_PHOTO_LAX:
            # Мягкий режим: не отсекаем по углу, только по
            # «в кадре» и по окклюзии.
            COS_MIN = -1.0
        else:
            COS_MIN = 0.15

        n_pix = len(xyz)

        cos_buf = np.full((n_photos, n_pix), -1.0, dtype=np.float32)
        u_buf = np.zeros((n_photos, n_pix), dtype=np.float32)
        v_buf = np.zeros((n_photos, n_pix), dtype=np.float32)

        for pi, ph in enumerate(photos):
            P = ph["P"]
            Wp = ph["W"]
            Hp = ph["H"]
            u_px, v_px, wv = project_points(P, xyz)

            vis = ((wv > 0)
                   & (u_px >= 0) & (u_px < Wp - 1)
                   & (v_px >= 0) & (v_px < Hp - 1))

            if vis.any():
                us = np.clip(u_px.astype(np.int64), 0, ph["depth_w"] - 1)
                vs = np.clip(v_px.astype(np.int64), 0, ph["depth_h"] - 1)
                scene_z = ph["depth"][vs, us]
                occ_tol = 1.15 if SINGLE_PHOTO_LAX else 1.05
                if SINGLE_PHOTO_LAX:
                    # inf → доверяем (край силуэта)
                    not_occ = np.where(
                        np.isfinite(scene_z),
                        wv < scene_z * occ_tol,
                        True)
                else:
                    # inf → не видно
                    not_occ = (np.isfinite(scene_z)
                               & (wv < scene_z * occ_tol))
                vis = vis & not_occ

            if not vis.any():
                continue

            cam_pos = ph["cam_pos"]
            dirs = cam_pos[None, :] - xyz
            dists = np.linalg.norm(dirs, axis=1)
            dists_safe = np.where(dists > 1e-9, dists, 1.0)
            dirs = dirs / dists_safe[:, None]
            cos_vals = (normals * dirs).sum(axis=1)

            valid = vis & (cos_vals > COS_MIN)
            cos_buf[pi, valid] = cos_vals[valid]
            u_buf[pi, valid] = u_px[valid]
            v_buf[pi, valid] = v_px[valid]

        # ==================================================
        # ФАЗА 2: per-triangle cos и назначение
        # ==================================================
        T_total = len(island_tris)
        tri_list = list(island_tris)

        # Мэп: global tri id → local idx
        tri_to_local = {gt: li for li, gt in enumerate(tri_list)}

        # Для каждого треугольника: нормаль (уже посчитана) и cos к каждому фото
        tri_cos = np.full((T_total, n_photos), -1.0, dtype=np.float32)

        # Группируем filled-пиксели по треугольникам.
        # k — регионный индекс (0..n-1), local_i — финальный (0..n_pix-1).
        tri_pixels = {li: [] for li in range(T_total)}
        for local_i, k in enumerate(idx_filled):
            gt = int(tri_id_result[k])
            if gt < 0:
                continue
            li = tri_to_local.get(gt, -1)
            if li < 0:
                continue
            tri_pixels[li].append(local_i)

        for li in range(T_total):
            pxs = tri_pixels[li]
            if not pxs:
                continue
            # центроид и нормаль
            pts = xyz[pxs]
            c = pts.mean(axis=0)
            gt = tri_list[li]
            n_t = _get_tri_normal(md, gt)

            for pi, ph in enumerate(photos):
                P = ph["P"]
                u_c, v_c, w_c = project_points(P, c.reshape(1, 3))
                if w_c[0] <= 0:
                    continue
                uc = float(u_c[0])
                vc = float(v_c[0])
                if not (0 <= uc < ph["W"] - 1 and 0 <= vc < ph["H"] - 1):
                    continue
                sz = ph["depth"][int(vc), int(uc)]
                if not np.isfinite(sz) or not (w_c[0] < sz * 1.05):
                    continue
                cam_pos = ph["cam_pos"]
                d = cam_pos - c
                dn = np.linalg.norm(d)
                if dn < 1e-9:
                    continue
                d = d / dn
                cv = float(np.dot(n_t, d))
                if cv > 0:
                    tri_cos[li, pi] = cv

        if SINGLE_PHOTO_LAX:
            # Одно фото — все треугольники назначаем на него,
            # никаких seed/BFS/фильтров.
            assign = np.zeros(T_total, dtype=np.int32)
            conf = np.ones(T_total, dtype=np.float32)
            weights = np.zeros((n_photos, T_total), dtype=np.float32)
            weights[0, :] = 1.0
            if verbose:
                print(f"    режим одного фото: все треугольники "
                      f"на фото 0")
        else:
            # ---------- Seed + propagation ----------
            SEED_COS = 0.5        # было 0.85
            SEED_CONF = 0.1       # было 0.15
            MEDIUM_COS = 0.15     # было 0.35
            HARD_COS = 0.7        # было 0.90
            HARD_CONF = 0.2       # было 0.25

            # Соседство треугольников внутри острова
            from collections import defaultdict, deque
            vert_to_local = defaultdict(list)
            for li, gt in enumerate(tri_list):
                v0, v1, v2, _, _, _ = tris[gt]
                vert_to_local[v0].append(li)
                vert_to_local[v1].append(li)
                vert_to_local[v2].append(li)

            adj = defaultdict(set)
            for v, tlist in vert_to_local.items():
                for a in tlist:
                    for b in tlist:
                        if a != b:
                            adj[a].add(b)

            # Начальное назначение (seed)
            assign = np.full(T_total, -1, dtype=np.int32)
            conf = np.zeros(T_total, dtype=np.float32)
            for li in range(T_total):
                row = tri_cos[li]
                valid = row > 0
                if not valid.any():
                    continue
                sorted_vals = np.sort(row)[::-1]
                best = float(sorted_vals[0])
                second = float(sorted_vals[1]) if len(sorted_vals) > 1 else 0.0
                if best >= SEED_COS and (best - second) >= SEED_CONF:
                    pi = int(np.argmax(row))
                    assign[li] = pi
                    conf[li] = best

            # BFS propagation
            q = deque(np.where(assign >= 0)[0].tolist())
            while q:
                li = q.popleft()
                pi = assign[li]
                for nb in adj[li]:
                    if assign[nb] >= 0:
                        continue
                    cv = float(tri_cos[nb, pi])
                    if cv >= MEDIUM_COS:
                        assign[nb] = pi
                        conf[nb] = cv
                        q.append(nb)

            # Fallback: всё ещё не назначено — берём argmax
            for li in range(T_total):
                if assign[li] >= 0:
                    continue
                row = tri_cos[li]
                valid = row > 0
                if valid.any():
                    pi = int(np.argmax(row))
                    assign[li] = pi
                    conf[li] = float(row[pi])

            # ---------- Фильтр мелких регионов ----------
            # Считаем площадь треугольника в 3D для оценки «веса».
            tri_area = np.zeros(T_total, dtype=np.float64)
            for li, gt in enumerate(tri_list):
                v0, v1, v2, _, _, _ = tris[gt]
                p0 = verts[v0]
                p1 = verts[v1]
                p2 = verts[v2]
                a = 0.5 * np.linalg.norm(np.cross(p1 - p0, p2 - p0))
                tri_area[li] = a

            total_area = float(tri_area.sum())

            # Абсолютный минимум площади (в м²). Регионы меньше —
            # схлопываются, независимо от размера острова.
            MIN_REGION_AREA_ABS = 0.05   # 0.05 м² ≈ 22×22 см

            # Относительный минимум — 20% площади острова.
            MIN_REGION_AREA_FRAC = 0.2

            min_region_area = max(
                MIN_REGION_AREA_ABS,
                MIN_REGION_AREA_FRAC * total_area)

            # Пропускаем фильтр для мелких островов целиком.
            # Если общая площадь острова меньше MIN_ISLAND_AREA_SKIP,
            # не схлопываем ничего.
            MIN_ISLAND_AREA_SKIP = 0.2   # 0.2 м²

            skip_filter = (total_area < MIN_ISLAND_AREA_SKIP)
            if skip_filter and verbose:
                print(f"    фильтр мелких регионов: пропущен "
                      f"(остров всего {total_area:.3f} м²)")

            # Разбиваем на связные компоненты по assign.
            # Итерируем, пока есть мелкие регионы.
            MAX_ITER = 10 if not skip_filter else 0
            for iteration in range(MAX_ITER):
                # Находим компоненты
                seen = np.zeros(T_total, dtype=bool)
                components = []  # list of (list_of_tris, area, assign_pi)

                for start in range(T_total):
                    if seen[start] or assign[start] < 0:
                        continue
                    pi = assign[start]
                    comp = []
                    q2 = deque([start])
                    seen[start] = True
                    while q2:
                        li = q2.popleft()
                        comp.append(li)
                        for nb in adj[li]:
                            if seen[nb]:
                                continue
                            if assign[nb] == pi:
                                seen[nb] = True
                                q2.append(nb)
                    area = float(tri_area[comp].sum())
                    components.append((comp, area, pi))

                # Есть ли мелкие?
                small = [(c, a, pi) for (c, a, pi) in components
                         if a < min_region_area]
                if not small:
                    break

                # Присоединяем каждый мелкий регион к соседнему
                # самому крупному
                for comp, area, pi in small:
                    # Соседи региона: треугольники из других регионов,
                    # граничащие с нашим
                    neighbor_votes = {}
                    for li in comp:
                        for nb in adj[li]:
                            if assign[nb] < 0 or assign[nb] == pi:
                                continue
                            nb_pi = assign[nb]
                            neighbor_votes[nb_pi] = (
                                neighbor_votes.get(nb_pi, 0) + 1)

                    if not neighbor_votes:
                        continue

                    # Сосед с максимальной суммарной площадью
                    best_pi = None
                    best_area = -1
                    for nb_pi in neighbor_votes:
                        nb_area = sum(
                            tri_area[li] for li in range(T_total)
                            if assign[li] == nb_pi)
                        if nb_area > best_area:
                            best_area = nb_area
                            best_pi = nb_pi

                    if best_pi is not None:
                        for li in comp:
                            assign[li] = best_pi

            # Убираем возможные неоднозначности: у всех треугольников
            # должен быть валидный assign
            for li in range(T_total):
                if assign[li] < 0:
                    # Fallback на глобальный argmax
                    row = tri_cos[li]
                    valid = row > 0
                    if valid.any():
                        assign[li] = int(np.argmax(row))

            # ---------- Веса на треугольник ----------
            # weights[pi, li] = доля фото pi для треугольника li
            weights = np.zeros((n_photos, T_total), dtype=np.float32)
            for li in range(T_total):
                pi = int(assign[li])
                if pi < 0:
                    continue
                row = tri_cos[li].copy()
                row[row < 0] = 0.0
                best = float(row[pi])
                # Top-2 среди остальных
                row_no_best = row.copy()
                row_no_best[pi] = 0.0
                pi2 = int(np.argmax(row_no_best))
                second = float(row_no_best[pi2])

                if best >= HARD_COS and (best - second) >= HARD_CONF:
                    # Жёстко одно фото
                    weights[pi, li] = 1.0
                else:
                    # Soft: два фото с весами cos^2
                    w1 = best ** 2
                    w2 = second ** 2 if second > COS_MIN else 0.0
                    s = w1 + w2
                    if s < 1e-9:
                        weights[pi, li] = 1.0
                    else:
                        weights[pi, li] = w1 / s
                        if w2 > 0:
                            weights[pi2, li] = w2 / s

        # ==================================================
        # ФАЗА 3: пиксельная сборка по весам треугольников
        # ==================================================
        # Разворачиваем per-triangle weights в per-pixel.
        # local_i — финальный индекс (0..n_pix-1), k — регионный.
        pix_weights = np.zeros((n_photos, n_pix), dtype=np.float32)
        for local_i, k in enumerate(idx_filled):
            gt = int(tri_id_result[k])
            if gt < 0:
                continue
            li = tri_to_local.get(gt, -1)
            if li < 0:
                continue
            pix_weights[:, local_i] = weights[:, li]

        # Отключаем фото, для которых cos < 0 (пиксель не виден)
        # В мягком режиме разрешаем cos = 0 и чуть ниже.
        if SINGLE_PHOTO_LAX:
            pix_weights = np.where(cos_buf > -1e-6, pix_weights, 0.0)
        else:
            pix_weights = np.where(cos_buf > 0, pix_weights, 0.0)

        total_w = pix_weights.sum(axis=0)
        ok_px = total_w > 1e-9

        colors = np.zeros((n_pix, 3), dtype=np.float64)

        for pi in range(n_photos):
            w_row = pix_weights[pi]
            valid = w_row > 1e-6
            if not valid.any():
                continue
            ph = photos[pi]
            pix = ph["pixels"]
            us = u_buf[pi, valid]
            vs = v_buf[pi, valid]
            w = w_row[valid].astype(np.float64)

            x_i = np.clip(np.floor(us).astype(np.int64),
                          0, pix.shape[1] - 2)
            y_i = np.clip(np.floor(vs).astype(np.int64),
                          0, pix.shape[0] - 2)
            dx = us - x_i
            dy = vs - y_i
            p00 = pix[y_i, x_i]
            p10 = pix[y_i, x_i + 1]
            p01 = pix[y_i + 1, x_i]
            p11 = pix[y_i + 1, x_i + 1]
            top_c = p00 * (1 - dx[:, None]) + p10 * dx[:, None]
            bot_c = p01 * (1 - dx[:, None]) + p11 * dx[:, None]
            col = top_c * (1 - dy[:, None]) + bot_c * dy[:, None]

            idx_valid = np.where(valid)[0]
            colors[idx_valid] += w[:, None] * col

        colors[ok_px] /= total_w[ok_px][:, None]

        n_covered = int(ok_px.sum())
        n_total = n_pix
        if verbose:
            # Распределение по фото
            best_pi = np.argmax(pix_weights, axis=0)
            votes = np.zeros(n_photos, dtype=np.int64)
            for pi in range(n_photos):
                votes[pi] = int((ok_px & (best_pi == pi)).sum())
            votes_str = ", ".join(
                f"{os.path.basename(p['path'])}: {votes[i]}"
                for i, p in enumerate(photos))
            print(f"  {isl_id} ({obj_name}): остров {T} треугольников, "
                  f"покрыто {n_covered} / {n_total} "
                  f"({100.0 * n_covered / max(n_total, 1):.1f}%)")
            print(f"    распределение: {votes_str}")

        if not ok_px.any():
            continue

        ys = yy.ravel()[idx_filled]
        xs = xx.ravel()[idx_filled]

        # 1) Записываем основные пиксели (с перезаписью)
        ys_v = ys[ok_px]
        xs_v = xs[ok_px]
        cols_v = np.clip(colors[ok_px], 0, 255).astype(np.uint8)
        output[ys_v, xs_v, 0:3] = cols_v
        output[ys_v, xs_v, 3] = 255

        # 2) Расширение на N пикселей
        if expand_n > 0:
            _expand_island_region(
                output, y0, y1, x0, x1,
                ys_v, xs_v, cols_v,
                iterations=expand_n,
                alpha_value=expand_alpha)


# =============================================================
# Обработка квадов
# =============================================================

def _check_visible_with_cos(ph, u_px, v_px, wv, point_xyz, normal):
    """Возвращает cos угла, если точка видна, или -1."""
    if wv <= 0:
        return -1.0
    if u_px < 0 or u_px >= ph["W"] - 1:
        return -1.0
    if v_px < 0 or v_px >= ph["H"] - 1:
        return -1.0

    us = int(u_px)
    vs = int(v_px)
    us = max(0, min(us, ph["depth_w"] - 1))
    vs = max(0, min(vs, ph["depth_h"] - 1))
    scene_z = ph["depth"][vs, us]

    if not np.isfinite(scene_z):
        return -1.0
    if not (wv < scene_z * 1.05):
        return -1.0

    cam_pos = ph["cam_pos"]
    d = cam_pos - point_xyz
    n = np.linalg.norm(d)
    if n < 1e-9:
        return -1.0
    d = d / n
    return float(np.dot(normal, d))


def process_quads(layout, mesh_cache, photos, output, W_out, H_out,
                   verbose=False):
    for q in layout.get("quads", []):
        qid = q.get("id", "?")
        corners = q.get("corners_3d", [])
        layout_px = q.get("corners_layout_px", [])
        if len(corners) != 4 or len(layout_px) != 4:
            continue

        # 3D-точки: по UUID (приоритет), иначе по индексу
        pts3d_list = []
        missing = []
        for c in corners:
            obj_name = c.get("obj", "")
            vuuid = int(c.get("vertex_uuid", 0))
            vi = int(c.get("vertex", -1))
            md = mesh_cache.get(obj_name)
            if md is None:
                missing.append(f"{obj_name}")
                pts3d_list.append(c.get("x_world", [0.0, 0.0, 0.0]))
                continue

            idx = -1
            if vuuid > 0:
                idx = find_vertex_index_by_uuid(md, vuuid)
            if idx < 0 and 0 <= vi < len(md["verts_world"]):
                idx = vi

            if idx < 0:
                missing.append(f"{obj_name} v{vi}")
                pts3d_list.append(c.get("x_world", [0.0, 0.0, 0.0]))
            else:
                pts3d_list.append(md["verts_world"][idx].tolist())

        if missing:
            print(f"  {qid}: не найдены в модели: {', '.join(missing)}")
        pts3d = np.array(pts3d_list, dtype=np.float64)

        # Выбор фото
        # Нормаль плоскости квада (по 3 углам)
        n_plane = np.cross(pts3d[1] - pts3d[0], pts3d[2] - pts3d[0])
        ln = np.linalg.norm(n_plane)
        if ln < 1e-9:
            if verbose:
                print(f"  {qid}: вырожденная плоскость")
            continue
        n_plane = n_plane / ln

        COS_MIN = 0.15

        best_cos = -2.0
        chosen = None
        chosen_uv = None

        for ph in photos:
            P = ph["P"]
            u_px, v_px, wv = project_points(P, pts3d)

            # Минимальный cos среди 4 углов — определяет
            # насколько «плоско» видна вся панель
            min_cos = 2.0
            ok = True
            for k in range(4):
                c = _check_visible_with_cos(
                    ph, u_px[k], v_px[k], wv[k], pts3d[k], n_plane)
                if c < COS_MIN:
                    ok = False
                    break
                if c < min_cos:
                    min_cos = c
            if not ok:
                continue

            if min_cos > best_cos:
                best_cos = min_cos
                chosen = ph
                chosen_uv = np.stack([u_px, v_px], axis=1)

        if chosen is None:
            if verbose:
                print(f"  {qid}: не выбрано фото")
            continue

        # Гомография фото → layout
        src_pts = chosen_uv
        dst_pts = np.array(layout_px, dtype=np.float64)
        H = compute_homography(src_pts, dst_pts)
        if H is None:
            continue
        try:
            H_l2p = np.linalg.inv(H)
        except np.linalg.LinAlgError:
            continue

        xs = [p[0] for p in layout_px]
        ys = [p[1] for p in layout_px]
        x0 = max(0, int(np.floor(min(xs))))
        x1 = min(W_out - 1, int(np.ceil(max(xs))))
        y0 = max(0, int(np.floor(min(ys))))
        y1 = min(H_out - 1, int(np.ceil(max(ys))))

        yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
        px = xx.astype(np.float64) + 0.5
        py = yy.astype(np.float64) + 0.5
        inside = point_in_quad(px, py, dst_pts)
        if not inside.any():
            continue

        fx = px[inside]
        fy = py[inside]
        n = fx.size
        h = H_l2p @ np.stack([fx, fy, np.ones(n)], axis=0)
        w = h[2]
        ok = np.abs(w) > 1e-9
        if not ok.any():
            continue
        ux = np.where(ok, h[0] / np.where(ok, w, 1.0), 0.0)
        vy = np.where(ok, h[1] / np.where(ok, w, 1.0), 0.0)

        Hp, Wp, _ = chosen["pixels"].shape
        x_i = np.clip(np.floor(ux).astype(np.int64), 0, Wp - 2)
        y_i = np.clip(np.floor(vy).astype(np.int64), 0, Hp - 2)
        dx = (ux - x_i)[:, None]
        dy = (vy - y_i)[:, None]

        pix = chosen["pixels"]
        p00 = pix[y_i, x_i]
        p10 = pix[y_i, x_i + 1]
        p01 = pix[y_i + 1, x_i]
        p11 = pix[y_i + 1, x_i + 1]
        top = p00 * (1 - dx) + p10 * dx
        bot = p01 * (1 - dx) + p11 * dx
        col = top * (1 - dy) + bot * dy

        output[yy[inside], xx[inside], 0:3] = np.clip(
            col, 0, 255).astype(np.uint8)
        output[yy[inside], xx[inside], 3] = 255

        if verbose:
            print(f"  {qid}: {n} px")

# =============================================================
# main
# =============================================================

def main():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []

    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", required=True)
    ap.add_argument("--uv-json", required=True)
    ap.add_argument("--photo", action="append", required=True)
    ap.add_argument("--photo-meta", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bg", choices=["transparent", "layout"],
                    default="transparent")
    ap.add_argument("--expand", type=int, default=2,
                    help="Расширение острова на N пикселей "
                         "(0 — выключено)")
    ap.add_argument("--expand-alpha", type=int, default=255,
                    help="Альфа для расширенных пикселей")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    if len(args.photo) != len(args.photo_meta):
        print("Ошибка: --photo и --photo-meta не совпадают по числу",
              file=sys.stderr)
        sys.exit(1)

    for path in [args.layout, args.uv_json] + args.photo + args.photo_meta:
        if not os.path.exists(path):
            print(f"Файл не найден: {path}", file=sys.stderr)
            sys.exit(1)

    # Force update сцены
    bpy.context.view_layer.update()

    layout = load_json(args.layout)
    uv_meta = load_json(args.uv_json)

    # Собираем mesh_data только для объектов, упомянутых в layout
    needed_objs = set()
    for isl in layout.get("islands", []):
        if isl.get("corners"):
            needed_objs.add(isl["corners"][0]["obj"])
    for q in layout.get("quads", []):
        for c in q.get("corners_3d", []):
            needed_objs.add(c.get("obj", ""))

    mesh_cache = {}
    for name in needed_objs:
        md = get_mesh_data(name, verbose=args.verbose)
        if md is not None:
            mesh_cache[name] = md
            if args.verbose:
                print(f"[read] {name}: {len(md['verts_world'])} вершин, "
                      f"{len(md['triangles'])} треугольников")

    # Резолюция выходного PNG
    W_out, H_out = 4960, 7016
    if args.bg == "layout":
        layout_png = os.path.splitext(args.layout)[0] + ".png"
        if os.path.exists(layout_png):
            base = Image.open(layout_png).convert("RGBA")
            W_out, H_out = base.size
            output = np.array(base, dtype=np.uint8)
        else:
            output = np.full((H_out, W_out, 4), 255, dtype=np.uint8)
    else:
        output = np.zeros((H_out, W_out, 4), dtype=np.uint8)

    # Фото + depth buffer
    photos = []
    for photo_path, meta_path in zip(args.photo, args.photo_meta):
        meta = load_json(meta_path)
        P, W, H = make_camera_matrix(meta)
        pixels, _ = load_photo_pixels(photo_path)

        # Depth buffer считаем на всех объектах, не только тех,
        # что в layout — иначе окклюзия не учтёт соседние детали
        all_md = {}
        for obj in bpy.data.objects:
            if obj.type != 'MESH':
                continue
            md = get_mesh_data(obj.name)
            if md is not None:
                all_md[obj.name] = md

        depth = _build_depth_all(all_md, P, W, H)

        cam = meta["camera"]
        m = np.array(cam["matrix_world"], dtype=np.float64).reshape(4, 4)
        cam_pos = m[:3, 3].copy()

        f_px_val = float(meta["solve"]["f_px"])
        if f_px_val <= 0:
            # fallback — по физическим параметрам
            lens = float(cam["lens_mm"])
            sensor = float(cam["sensor_width_mm"])
            nW = float(photo_meta["photo"].get("native_width_px", W))
            nH = float(photo_meta["photo"].get("native_height_px", H))
            f_px_val = lens / sensor * max(nW, nH)

        photos.append({
            "P": P, "W": W, "H": H, "pixels": pixels,
            "depth": depth, "depth_w": int(W), "depth_h": int(H),
            "cam_pos": cam_pos,
            "f_px": f_px_val,
            "path": photo_path,
        })
        
        print(f"Фото: {os.path.basename(photo_path)} "
              f"{int(W)}×{int(H)}, depth buffer готов")

    print(f"Острова: {len(layout.get('islands', []))}, "
          f"квады: {len(layout.get('quads', []))}")

    print("Обработка островов...")
    process_islands(layout, uv_meta, mesh_cache, photos, output,
                     W_out, H_out, verbose=args.verbose, expand_n=args.expand, expand_alpha=args.expand_alpha)

    print("Обработка квадов...")
    process_quads(layout, mesh_cache, photos, output, W_out, H_out,
                   verbose=args.verbose)

    Image.fromarray(output, "RGBA").save(args.out, "PNG")
    print(f"Сохранено: {args.out}")


def _build_depth_all(all_md, P, W, H):
    """Depth buffer по всем объектам сцены."""
    W_i = int(W)
    H_i = int(H)
    depth = np.full((H_i, W_i), np.inf, dtype=np.float32)

    for md in all_md.values():
        verts = md["verts_world"]
        u, v, w = project_points(P, verts)
        for (v0, v1, v2, _t0, _t1, _t2) in md["triangles"]:
            if w[v0] <= 0 or w[v1] <= 0 or w[v2] <= 0:
                continue
            _rasterize_depth_tri(
                u[v0], v[v0], w[v0],
                u[v1], v[v1], w[v1],
                u[v2], v[v2], w[v2],
                W_i, H_i, depth)

    return depth


if __name__ == "__main__":
    main()
