#!/usr/bin/env python3
"""
PT Automaton v2.

Использование:
    python pt_automaton.py \\
        --layout layout.quads.json \\
        --uv-json layout_uv.uv.json \\
        --obj model.obj \\
        --photo DSC_6166.jpg --photo-meta DSC_6166.vertices.json \\
        --photo DSC_6170.jpg --photo-meta DSC_6170.vertices.json \\
        --out result.png \\
        [--bg transparent|layout] [--verbose]
"""

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image


# ===== Гомография =====

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


def apply_homography(H, x, y):
    v = H @ np.array([x, y, 1.0])
    if abs(v[2]) < 1e-12:
        return None
    return v[0] / v[2], v[1] / v[2]


def point_in_quad(px, py, corners):
    signs = []
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        signs.append((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1))
    signs = np.stack(signs, axis=0)
    return np.all(signs >= 0, axis=0) | np.all(signs <= 0, axis=0)


# ===== Проекция камеры =====

def make_camera_matrix(photo_meta):
    """P (3x4) world → пиксели фото."""
    cam = photo_meta["camera"]
    m = np.array(cam["matrix_world"], dtype=np.float64).reshape(4, 4)
    m_inv = np.linalg.inv(m)
    D = np.diag([1.0, -1.0, -1.0, 1.0])
    m_cv = D @ m_inv

    W = float(photo_meta["photo"]["width_px"])
    H = float(photo_meta["photo"]["height_px"])
    nW = float(photo_meta["photo"].get("native_width_px", W))
    nH = float(photo_meta["photo"].get("native_height_px", H))

    f_px = float(photo_meta["solve"]["f_px"])
    if f_px <= 0:
        lens = float(cam["lens_mm"])
        sensor = float(cam["sensor_width_mm"])
        # Если solve не выполнен — считаем от нативных размеров
        f_px = lens / sensor * max(nW, nH)

    # cx, cy — центр кропнутого изображения
    K = np.array([
        [f_px, 0.0, W / 2.0],
        [0.0, f_px, H / 2.0],
        [0.0, 0.0, 1.0],
    ])
    return K @ m_cv[:3, :], W, H


def project_points(P, pts_xyz):
    """pts_xyz: (N, 3) → (u_px, v_px, w) (N, 3)."""
    n = pts_xyz.shape[0]
    homo = np.concatenate([pts_xyz, np.ones((n, 1))], axis=1)
    proj = homo @ P.T
    w = proj[:, 2:3]
    w_safe = np.where(np.abs(w) < 1e-9, 1e-9, w)
    uv = proj[:, :2] / w_safe
    return uv[:, 0], uv[:, 1], w[:, 0]


# ===== UV-карта =====

def load_obj(path):
    """Читает OBJ. Для каждой группы запоминает отсортированный
    список глобальных индексов вершин, чтобы корректно переводить
    локальный индекс в глобальный."""
    vertices, uvs, faces = [], [], []
    groups = []
    current = {"name": "__default__", "face_indices": []}
    groups.append(current)

    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('v '):
                p = line.split()
                vertices.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith('vt '):
                p = line.split()
                uvs.append((float(p[1]), float(p[2])))
            elif line.startswith('f '):
                tokens = line.split()[1:]
                face = []
                for tk in tokens:
                    if '/' in tk:
                        parts = tk.split('/')
                        vi = int(parts[0]) - 1
                        ti = int(parts[1]) - 1 if len(parts) > 1 and parts[1] else -1
                        face.append((vi, ti))
                    else:
                        face.append((int(tk) - 1, -1))
                faces.append(face)
                current["face_indices"].append(len(faces) - 1)
            elif line.startswith('o ') or line.startswith('g '):
                current = {"name": line.split(maxsplit=1)[1].strip(),
                           "face_indices": []}
                groups.append(current)

    # Точный список индексов вершин для каждой группы
    for g in groups:
        used = set()
        for fi in g["face_indices"]:
            for (vi, _) in faces[fi]:
                used.add(vi)
        g["vert_indices"] = sorted(used)
        g["vert_set"] = used
        g["n_vertices"] = len(used)
        if used:
            g["vert_min"] = min(used)
            g["vert_max"] = max(used)
        else:
            g["vert_min"] = 0
            g["vert_max"] = -1

    return {
        "vertices": np.array(vertices, dtype=np.float64),
        "uvs": np.array(uvs, dtype=np.float64) if uvs else np.zeros((0, 2)),
        "faces": faces,
        "groups": groups,
    }


def get_vertex_xyz(obj_data, obj_name, local_index):
    """Ищет XYZ вершины по имени группы и индексу в Blender-меше."""
    for g in obj_data["groups"]:
        if g["name"] != obj_name:
            continue
        if local_index < 0:
            return None
        obj_idx = g["vert_min"] + local_index
        if obj_idx not in g["vert_set"]:
            return None
        if 0 <= obj_idx < len(obj_data["vertices"]):
            return obj_data["vertices"][obj_idx]
        return None
    return None


# ===== Глубина точек =====

def build_scene_depth_buffer(obj_data, P, W, H, scale=4):
    """Растеризует все треугольники модели с точки зрения камеры.
    Возвращает (depth, W_small, H_small, scale).

    depth[y, x] — глубина (z_c) ближайшей поверхности.
    inf там, где ничего не попало."""
    W_small = (int(W) + scale - 1) // scale
    H_small = (int(H) + scale - 1) // scale
    depth = np.full((H_small, W_small), np.inf, dtype=np.float64)

    verts = obj_data["vertices"]
    faces = obj_data["faces"]

    u, v, w = project_points(P, verts)

    for face in faces:
        if len(face) < 3:
            continue
        for k in range(1, len(face) - 1):
            v0 = face[0][0]
            v1 = face[k][0]
            v2 = face[k + 1][0]
            if w[v0] <= 0 or w[v1] <= 0 or w[v2] <= 0:
                continue
            _rasterize_depth_tri(
                u[v0], v[v0], w[v0],
                u[v1], v[v1], w[v1],
                u[v2], v[v2], w[v2],
                W_small, H_small, scale, depth)
    return depth, W_small, H_small, scale


def _rasterize_depth_tri(u0, v0, z0, u1, v1, z1, u2, v2, z2,
                          Ws, Hs, scale, depth):
    p0 = (u0 / scale, v0 / scale)
    p1 = (u1 / scale, v1 / scale)
    p2 = (u2 / scale, v2 / scale)

    xs = (p0[0], p1[0], p2[0])
    ys = (p0[1], p1[1], p2[1])
    x_min = max(0, int(np.floor(min(xs))))
    x_max = min(Ws - 1, int(np.ceil(max(xs))))
    y_min = max(0, int(np.floor(min(ys))))
    y_max = min(Hs - 1, int(np.ceil(max(ys))))
    if x_max < x_min or y_max < y_min:
        return

    det = ((p1[1] - p2[1]) * (p0[0] - p2[0])
           + (p2[0] - p1[0]) * (p0[1] - p2[1]))
    if abs(det) < 1e-12:
        return

    xx, yy = np.meshgrid(
        np.arange(x_min, x_max + 1) + 0.5,
        np.arange(y_min, y_max + 1) + 0.5)

    a = ((p1[1] - p2[1]) * (xx - p2[0])
         + (p2[0] - p1[0]) * (yy - p2[1])) / det
    b = ((p2[1] - p0[1]) * (xx - p2[0])
         + (p0[0] - p2[0]) * (yy - p2[1])) / det
    c = 1.0 - a - b

    inside = (a >= 0) & (b >= 0) & (c >= 0)
    if not inside.any():
        return

    zi = a * z0 + b * z1 + c * z2
    sub = depth[y_min:y_max + 1, x_min:x_max + 1]
    update = inside & (zi < sub)
    sub[update] = zi[update]


def _check_visible(ph, u_px, v_px, wv):
    """Проверяет, что точка видна на фото: в кадре и не окклюзирована.

    Окклюзия проверяется минимумом в окне 3×3 depth buffer.
    Если depth buffer содержит inf в окрестности — доверяем точке.
    """
    if wv <= 0:
        return False
    if u_px < 0 or u_px >= ph["W"] - 1:
        return False
    if v_px < 0 or v_px >= ph["H"] - 1:
        return False

    scale = ph["depth_scale"]
    us = int(u_px / scale)
    vs = int(v_px / scale)
    us = max(0, min(us, ph["depth_w"] - 1))
    vs = max(0, min(vs, ph["depth_h"] - 1))

    u0 = max(0, us - 1)
    u1 = min(ph["depth_w"], us + 2)
    v0 = max(0, vs - 1)
    v1 = min(ph["depth_h"], vs + 2)
    patch = ph["depth"][v0:v1, u0:u1]

    finite = patch[np.isfinite(patch)]
    if finite.size == 0:
        # Полная "дырка" на границе силуэта — доверяем точке.
        return True

    scene_z = float(finite.min())
    return wv < scene_z * 1.05
    
    
# ===== Загрузка фото =====

def load_photo_pixels(path):
    img = Image.open(path).convert("RGB")
    return np.array(img, dtype=np.float32), img.size  # (H, W, 3), (W, H)


# ===== Основная логика =====

from collections import defaultdict


def find_island_faces(obj_data, seed_uvs):
    """Находит связную компоненту граней (UV-остров) по seed-координатам."""
    uvs = obj_data["uvs"]
    faces = obj_data["faces"]

    uv_to_faces = defaultdict(list)
    for fi, face in enumerate(faces):
        for (vi, ti) in face:
            if ti >= 0:
                uv_to_faces[ti].append(fi)

    seed_indices = set()
    for (u, v) in seed_uvs:
        d = np.linalg.norm(uvs - np.array([u, v]), axis=1)
        idx = int(np.argmin(d))
        if d[idx] < 1e-4:
            seed_indices.add(idx)
    if not seed_indices:
        return set()

    visited = set()
    queue = []
    for ui in seed_indices:
        for fi in uv_to_faces[ui]:
            if fi not in visited:
                visited.add(fi)
                queue.append(fi)

    head = 0
    while head < len(queue):
        fi = queue[head]
        head += 1
        for (vi, ti) in faces[fi]:
            if ti < 0:
                continue
            for other in uv_to_faces[ti]:
                if other not in visited:
                    visited.add(other)
                    queue.append(other)
    return visited


def process_islands(layout, uv_meta, obj_data, photos, output,
                     W_out, H_out, verbose=False):
    objects_meta = {o["name"]: o for o in uv_meta["objects"]}
    verts = obj_data["vertices"]
    uvs_all = obj_data["uvs"]
    faces_all = obj_data["faces"]

    for isl in layout.get("islands", []):
        isl_id = isl.get("id", "?")
        corners = isl["corners"]
        layout_px = np.array(isl["corners_layout_px"], dtype=np.float64)

        obj_name = corners[0]["obj"]

        # 1) Гомография layout_px -> UV
        uv_src = np.array([c["uv"] for c in corners], dtype=np.float64)
        H_l2uv = compute_homography(layout_px, uv_src)
        if H_l2uv is None:
            if verbose:
                print(f"  {isl_id}: гомография не сошлась")
            continue

        # 2) Ищем треугольники острова в OBJ
        island_faces = find_island_faces(
            obj_data, [tuple(c["uv"]) for c in corners])
        if not island_faces:
            if verbose:
                print(f"  {isl_id}: остров не найден в OBJ")
            continue

        tri_uv_list = []
        tri_xyz_list = []
        for fi in island_faces:
            face = faces_all[fi]
            if len(face) < 3:
                continue
            for k in range(1, len(face) - 1):
                v0, t0 = face[0]
                v1, t1 = face[k]
                v2, t2 = face[k + 1]
                if t0 < 0 or t1 < 0 or t2 < 0:
                    continue
                tri_uv_list.append([uvs_all[t0], uvs_all[t1], uvs_all[t2]])
                tri_xyz_list.append([verts[v0], verts[v1], verts[v2]])

        if not tri_uv_list:
            continue

        tri_uv = np.array(tri_uv_list, dtype=np.float64)   # (T, 3, 2)
        tri_xyz = np.array(tri_xyz_list, dtype=np.float64) # (T, 3, 3)

        # 3) UV-bbox острова -> в пространство выкройки
        all_uv = tri_uv.reshape(-1, 2)
        uv_min = all_uv.min(axis=0)
        uv_max = all_uv.max(axis=0)

        try:
            H_uv2l = np.linalg.inv(H_l2uv)
        except np.linalg.LinAlgError:
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

        # 4) Сетка пикселей и UV для каждого
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

        # 5) Для каждого пикселя — найти треугольник, вычислить 3D
        xyz_result = np.full((n, 3), np.nan, dtype=np.float64)
        filled = np.zeros(n, dtype=bool)

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

        if not filled.any():
            if verbose:
                print(f"  {isl_id}: ни один пиксель не попал в остров")
            continue

        # 6) Проекция на фото
        idx_filled = np.where(filled)[0]
        xyz = xyz_result[idx_filled]

        colors = np.zeros((xyz.shape[0], 3), dtype=np.float64)
        still = np.ones(xyz.shape[0], dtype=bool)

        for ph in photos:
            if not still.any():
                break
            P = ph["P"]
            Wp = ph["W"]
            Hp = ph["H"]
            pts = xyz[still]
            u_px, v_px, wv = project_points(P, pts)

            vis = ((wv > 0)
                   & (u_px >= 0) & (u_px < Wp - 1)
                   & (v_px >= 0) & (v_px < Hp - 1))

            # Проверка окклюзии через depth buffer
            if vis.any():
                us = (u_px / ph["depth_scale"]).astype(np.int64)
                vs = (v_px / ph["depth_scale"]).astype(np.int64)
                us = np.clip(us, 0, ph["depth_w"] - 1)
                vs = np.clip(vs, 0, ph["depth_h"] - 1)
                scene_z = ph["depth"][vs, us]
                # Точка видима, если она не глубже, чем поверхность
                # в этом пикселе (допуск 2% на численные погрешности)
                not_occluded = wv < scene_z * 1.02
                vis = vis & not_occluded

            if not vis.any():
                continue

            ux = u_px[vis]
            vy = v_px[vis]
            x_i = np.floor(ux).astype(np.int64)
            y_i = np.floor(vy).astype(np.int64)
            dx = (ux - x_i)[:, None]
            dy = (vy - y_i)[:, None]

            pix = ph["pixels"]
            p00 = pix[y_i, x_i]
            p10 = pix[y_i, x_i + 1]
            p01 = pix[y_i + 1, x_i]
            p11 = pix[y_i + 1, x_i + 1]

            top = p00 * (1 - dx) + p10 * dx
            bot = p01 * (1 - dx) + p11 * dx
            col = top * (1 - dy) + bot * dy

            idx_still = np.where(still)[0]
            idx_vis = idx_still[vis]
            colors[idx_vis] = col
            still[idx_vis] = False

        # 7) Запись в output
        ys = yy.ravel()[idx_filled]
        xs = xx.ravel()[idx_filled]
        ok_px = ~still
        if ok_px.any():
            ys_f = ys[ok_px]
            xs_f = xs[ok_px]
            output[ys_f, xs_f, 0:3] = np.clip(
                colors[ok_px], 0, 255).astype(np.uint8)
            output[ys_f, xs_f, 3] = 255

        if verbose:
            print(f"  {isl_id} ({obj_name}): остров {T} треугольников, "
                  f"{filled.sum()} пикселей, "
                  f"покрыто фото: {ok_px.sum()}")
           

def process_quads(layout, obj_data,  photos, output, W_out, H_out, verbose=False):
    for q in layout.get("quads", []):
        qid = q.get("id", "?")
        corners = q.get("corners_3d", [])
        layout_px = q.get("corners_layout_px", [])
        if len(corners) != 4 or len(layout_px) != 4:
            continue

        # Собираем 3D-точки из OBJ
        pts3d_list = []
        missing = []
        for c in corners:
            obj_name = c.get("obj", "")
            vi = int(c.get("vertex", -1))
            xyz = get_vertex_xyz(obj_data, obj_name, vi)
            if xyz is None:
                missing.append(f"{obj_name} v{vi}")
                pts3d_list.append(c.get("x_world", [0.0, 0.0, 0.0]))
            else:
                pts3d_list.append(xyz.tolist())
        if missing:
            print(f"  {qid}: не найдены в OBJ: {', '.join(missing)}")
        pts3d = np.array(pts3d_list, dtype=np.float64)

        # Ищем фото, где все 4 угла видны
        # (упрощённо: где все точки попадают в кадр)
        chosen_photo = None
        chosen_uv = None
        reject_reasons = []
        for ph in photos:
            P = ph["P"]
            u_px, v_px, wv = project_points(P, pts3d)

            # Проверяем все 4 угла через depth buffer
            ok = True
            reason = ""
            for k in range(4):
                if not _check_visible(ph, u_px[k], v_px[k], wv[k]):
                    ok = False
                    if wv[k] <= 0:
                        reason = f"v{k} за камерой"
                    elif (u_px[k] < 0 or u_px[k] >= ph["W"] - 1
                          or v_px[k] < 0 or v_px[k] >= ph["H"] - 1):
                        reason = f"v{k} вне кадра"
                    else:
                        scale = ph["depth_scale"]
                        us = int(u_px[k] / scale)
                        vs = int(v_px[k] / scale)
                        us = max(0, min(us, ph["depth_w"] - 1))
                        vs = max(0, min(vs, ph["depth_h"] - 1))
                        sz = float(ph["depth"][vs, us])
                        reason = (f"v{k} окклюзирован "
                                  f"(wv={wv[k]:.2f}, scene={sz:.2f})")
                    break
            if ok:
                chosen_photo = ph
                chosen_uv = np.stack([u_px, v_px], axis=1)
                break
            else:
                scale = ph["depth_scale"]
                us = int(u_px[k] / scale)
                vs = int(v_px[k] / scale)
                us = max(0, min(us, ph["depth_w"] - 1))
                vs = max(0, min(vs, ph["depth_h"] - 1))
                sz = float(ph["depth"][vs, us])
                reason = (f"v{k} окклюзирован "
                          f"(wv={wv[k]:.2f}, scene={sz:.2f})")

        if chosen_photo is None:
            if verbose:
                print(f"  {qid}: не выбрано фото")
                for r in reject_reasons:
                    print(f"    {r}")
            continue

        src_pts = chosen_uv
        dst_pts = np.array(layout_px, dtype=np.float64)
        H_hom = compute_homography(src_pts, dst_pts)
        if H_hom is None:
            continue
        try:
            H_l2p = np.linalg.inv(H_hom)
        except np.linalg.LinAlgError:
            continue

        # Bbox квада
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
        uy = np.where(ok, h[1] / np.where(ok, w, 1.0), 0.0)

        # Билинейно семплируем из выбранного фото
        Hp, Wp, _ = chosen_photo["pixels"].shape
        ix = np.clip(np.round(ux).astype(np.int64), 0, Wp - 1)
        iy = np.clip(np.round(uy).astype(np.int64), 0, Hp - 1)
        colors = chosen_photo["pixels"][iy, ix]
        output[yy[inside], xx[inside], 0:3] = np.clip(
            colors, 0, 255).astype(np.uint8)
        output[yy[inside], xx[inside], 3] = 255

        if verbose:
            print(f"  {qid}: {n} px")


def transfer(layout_json, uv_json, obj_path, photo_pairs,
             output_png, background='transparent', verbose=False):
    layout = json.load(open(layout_json, encoding='utf-8'))
    uv_meta = json.load(open(uv_json, encoding='utf-8'))
    obj_data = load_obj(obj_path)

    if background == 'layout':
        base = Image.open(layout_json.replace('.json', '.png')).convert('RGBA') \
            if os.path.exists(layout_json.replace('.json', '.png')) else None
        if base is None:
            base = Image.new('RGBA', (4960, 7016), (255, 255, 255, 255))
        W_out, H_out = base.size
        output = np.array(base, dtype=np.uint8)
    else:
        W_out, H_out = 4960, 7016
        output = np.zeros((H_out, W_out, 4), dtype=np.uint8)

    photos = []
    for photo_path, meta_path in photo_pairs:
        meta = json.load(open(meta_path, encoding='utf-8'))
        P, W, H = make_camera_matrix(meta)
        pixels, _ = load_photo_pixels(photo_path)
        print(f"Фото: {os.path.basename(photo_path)} {int(W)}×{int(H)}, "
              f"строю depth buffer...")
        depth, dw, dh, dscale = build_scene_depth_buffer(
            obj_data, P, W, H, scale=1)
        photos.append({
            "P": P, "W": W, "H": H, "pixels": pixels,
            "depth": depth,
            "depth_w": dw, "depth_h": dh, "depth_scale": dscale,
            "path": photo_path,
        })
        print(f"  depth buffer: {dw}×{dh} (scale 1/{dscale})")

    print(f"Острова: {len(layout.get('islands', []))}, "
          f"квады: {len(layout.get('quads', []))}")

    print("Обработка островов...")
    process_islands(layout, uv_meta, obj_data, photos, output,
                    W_out, H_out, verbose=verbose)

    print("Обработка квадов...")
    process_quads(layout, obj_data, photos, output, W_out, H_out, verbose=verbose)

    Image.fromarray(output, 'RGBA').save(output_png, 'PNG')
    print(f"Сохранено: {output_png}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", required=True)
    ap.add_argument("--uv-json", required=True)
    ap.add_argument("--obj", required=True)
    ap.add_argument("--photo", action="append", required=True)
    ap.add_argument("--photo-meta", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bg", choices=["transparent", "layout"],
                    default="transparent")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if len(args.photo) != len(args.photo_meta):
        print("Ошибка: --photo и --photo-meta не совпадают по числу",
              file=sys.stderr)
        sys.exit(1)

    for path in [args.layout, args.uv_json, args.obj] \
            + args.photo + args.photo_meta:
        if not os.path.exists(path):
            print(f"Файл не найден: {path}", file=sys.stderr)
            sys.exit(1)

    pairs = list(zip(args.photo, args.photo_meta))
    transfer(args.layout, args.uv_json, args.obj, pairs,
             args.out, background=args.bg, verbose=args.verbose)


if __name__ == "__main__":
    main()
