import json
import math
import os

import bmesh
import bpy
import numpy as np
from mathutils import Vector
from bpy_extras.view3d_utils import (region_2d_to_origin_3d,
                                      region_2d_to_vector_3d)
PBA_VERTEX_ID_ATTR = "pba_vertex_id"


def _get_uuid_attr(mesh, create=False):
    attr = mesh.attributes.get(PBA_VERTEX_ID_ATTR)
    if attr is None and create:
        attr = mesh.attributes.new(
            name=PBA_VERTEX_ID_ATTR, type='INT', domain='POINT')
    return attr


def get_vertex_uuid(obj, vertex_index):
    if vertex_index < 0:
        return 0

    # Edit Mode — читаем через bmesh
    if (bpy.context.mode == 'EDIT_MESH'
            and bpy.context.active_object == obj):
        bm = bmesh.from_edit_mesh(obj.data)
        layer = bm.verts.layers.int.get(PBA_VERTEX_ID_ATTR)
        if layer is None:
            return 0
        bm.verts.ensure_lookup_table()
        if vertex_index >= len(bm.verts):
            return 0
        return int(bm.verts[vertex_index][layer])

    # Object Mode — обычный путь
    attr = _get_uuid_attr(obj.data, create=False)
    if attr is None or len(attr.data) == 0:
        return 0
    n = len(obj.data.vertices)
    if vertex_index >= n:
        return 0
    try:
        return int(attr.data[vertex_index].value)
    except Exception:
        return 0


def find_vertex_by_uuid(obj, uuid_val, hint_world=None):
    if uuid_val <= 0:
        return -1, 0

    if (bpy.context.mode == 'EDIT_MESH'
            and bpy.context.active_object == obj):
        bm = bmesh.from_edit_mesh(obj.data)
        layer = bm.verts.layers.int.get(PBA_VERTEX_ID_ATTR)
        if layer is None:
            return -1, 0
        bm.verts.ensure_lookup_table()
        candidates = [i for i, v in enumerate(bm.verts)
                      if int(v[layer]) == uuid_val]
    else:
        attr = _get_uuid_attr(obj.data, create=False)
        if attr is None or len(attr.data) == 0:
            return -1, 0
        n = min(len(obj.data.vertices), len(attr.data))
        candidates = []
        for i in range(n):
            try:
                if int(attr.data[i].value) == uuid_val:
                    candidates.append(i)
            except Exception:
                continue

    if not candidates:
        return -1, 0
    if len(candidates) == 1:
        return candidates[0], 1

    if hint_world is not None:
        M = obj.matrix_world
        hint = Vector(hint_world)
        best_i = candidates[0]
        best_d = float('inf')
        for i in candidates:
            p = M @ obj.data.vertices[i].co
            d = (p - hint).length
            if d < best_d:
                best_d = d
                best_i = i
        return best_i, len(candidates)

    return candidates[0], len(candidates)


PLANE_MESH_NAME = "PBA_LayoutMesh"
PLANE_MAT_NAME = "PBA_LayoutMat"
PENDING_OBJ_NAME = "PBA_LM_Pending"
COMMITTED_OBJ_NAME = "PBA_LM_Committed"


CORNER_COLORS = [
    (1.0, 0.0, 0.0, 1.0),   # 0 — красный
    (0.0, 1.0, 0.0, 1.0),   # 1 — зелёный
    (0.0, 0.0, 1.0, 1.0),   # 2 — синий
    (1.0, 1.0, 0.0, 1.0),   # 3 — жёлтый
]


HIGHLIGHT_LAYOUT_NAME = "PBA_LM_Highlight_Layout"
HIGHLIGHT_MODEL_NAME = "PBA_LM_Highlight_Model"


def _ensure_color_material(name):
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    attr = nt.nodes.new("ShaderNodeAttribute")
    attr.attribute_name = "pba_color"
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.inputs["Strength"].default_value = 1.0
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(attr.outputs["Color"], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return mat


def _apply_colors(mesh, colors_per_vertex):
    """Создаёт или обновляет цветовой атрибут на mesh."""
    if "pba_color" in mesh.color_attributes:
        attr = mesh.color_attributes["pba_color"]
    else:
        attr = mesh.color_attributes.new(
            name="pba_color", type='FLOAT_COLOR', domain='POINT')
    for i, col in enumerate(colors_per_vertex):
        if i < len(attr.data):
            attr.data[i].color = col


def _make_cross_geometry(cx, cy, cz, length, thickness):
    l, t = length, thickness
    verts = [
        (cx - l, cy, cz - t), (cx + l, cy, cz - t),
        (cx + l, cy, cz + t), (cx - l, cy, cz + t),
        (cx - t, cy, cz - l), (cx + t, cy, cz - l),
        (cx + t, cy, cz + l), (cx - t, cy, cz + l),
    ]
    faces = [(0, 1, 2, 3), (4, 5, 6, 7)]
    return verts, faces


def _make_octahedron_geometry(cx, cy, cz, size):
    s = size
    verts = [
        (cx + s, cy, cz), (cx - s, cy, cz),
        (cx, cy + s, cz), (cx, cy - s, cz),
        (cx, cy, cz + s), (cx, cy, cz - s),
    ]
    faces = [
        (0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4),
        (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5),
    ]
    return verts, faces


def _delete_highlight_obj(scene, name):
    obj = scene.objects.get(name)
    if obj is not None:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def _set_status(scene, msg):
    scene.pba_lm.status_msg = msg
    print(f"[PBA LM] {msg}")


def _get_plane(scene):
    name = scene.pba_lm.plane_obj_name
    if not name:
        return None
    obj = scene.objects.get(name)
    if obj is None or obj.type != 'MESH':
        return None
    return obj


def _get_layout_image(scene):
    name = scene.pba_lm.layout_image_name
    if not name:
        return None
    return bpy.data.images.get(name)
    
    
_IMAGE_CACHE = {}


def _get_image_cached(image):
    """Кэш пикселей: (gray_uint8, rgba_float32, W, H)."""
    key = (image.name, tuple(image.size))
    if key in _IMAGE_CACHE:
        return _IMAGE_CACHE[key]

    W, H = image.size
    if W == 0 or H == 0:
        return None

    import numpy as np
    n = W * H * 4
    buf = np.empty(n, dtype=np.float32)
    image.pixels.foreach_get(buf)
    rgba = buf.reshape((H, W, 4))[::-1].copy()  # flip: Blender снизу вверх

    gray = (rgba[..., 0] * 0.299
            + rgba[..., 1] * 0.587
            + rgba[..., 2] * 0.114) * 255.0
    gray = gray.astype(np.uint8)

    result = (gray, rgba, W, H)
    _IMAGE_CACHE[key] = result
    return result


def _snap_to_line(gray, px, py, radius, threshold):
    """Возвращает (x, y, snapped) — координаты ближайшей линии."""
    import numpy as np

    H, W = gray.shape
    x0 = max(0, int(px - radius))
    x1 = min(W, int(px + radius + 1))
    y0 = max(0, int(py - radius))
    y1 = min(H, int(py + radius + 1))
    if x1 <= x0 or y1 <= y0:
        return px, py, False

    window = gray[y0:y1, x0:x1].astype(np.float32)

    # Вес: темнее пиксель — больше вес, обрезаем по порогу
    weights = np.maximum(0.0, threshold - window)

    # Дополнительно взвешиваем по близости к клику
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d2 = (xx - px) ** 2 + (yy - py) ** 2
    proximity = np.exp(-d2 / (radius ** 2))

    total = (weights * proximity).sum()
    if total < 1e-6:
        return px, py, False

    sx = ((weights * proximity) * xx).sum() / total
    sy = ((weights * proximity) * yy).sum() / total
    return float(sx), float(sy), True


def _find_marker(rgba, px, py, radius, tolerance):
    """Находит кластер пикселей ~#FF00FF, ближайший к клику.
    Возвращает центр этого кластера или None."""
    import numpy as np

    H, W = rgba.shape[:2]
    x0 = max(0, int(px - radius))
    x1 = min(W, int(px + radius + 1))
    y0 = max(0, int(py - radius))
    y1 = min(H, int(py + radius + 1))
    if x1 <= x0 or y1 <= y0:
        return None

    window = rgba[y0:y1, x0:x1, :3].astype(np.float32)
    target = np.array([1.0, 0.0, 1.0], dtype=np.float32)
    dist = np.sqrt(((window - target) ** 2).sum(axis=-1))
    mask = dist < tolerance
    if not mask.any():
        return None

    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=bool)

    # Ищем кластеры через BFS (4-связность достаточно)
    cx_click = px - x0
    cy_click = py - y0
    best = None
    best_d2 = float('inf')

    for iy in range(h):
        for ix in range(w):
            if not mask[iy, ix] or visited[iy, ix]:
                continue
            stack = [(iy, ix)]
            visited[iy, ix] = True
            pixels = []
            while stack:
                cy, cx = stack.pop()
                pixels.append((cy, cx))
                if cy > 0 and mask[cy - 1, cx] and not visited[cy - 1, cx]:
                    visited[cy - 1, cx] = True
                    stack.append((cy - 1, cx))
                if cy < h - 1 and mask[cy + 1, cx] and not visited[cy + 1, cx]:
                    visited[cy + 1, cx] = True
                    stack.append((cy + 1, cx))
                if cx > 0 and mask[cy, cx - 1] and not visited[cy, cx - 1]:
                    visited[cy, cx - 1] = True
                    stack.append((cy, cx - 1))
                if cx < w - 1 and mask[cy, cx + 1] and not visited[cy, cx + 1]:
                    visited[cy, cx + 1] = True
                    stack.append((cy, cx + 1))

            # Центроид кластера
            if pixels:
                sum_y = sum(p[0] for p in pixels)
                sum_x = sum(p[1] for p in pixels)
                n = len(pixels)
                cyc = sum_y / n
                cxc = sum_x / n
                d2 = (cxc - cx_click) ** 2 + (cyc - cy_click) ** 2
                if d2 < best_d2:
                    best_d2 = d2
                    best = (x0 + cxc, y0 + cyc)

    return best


def _snap_to_features(gray, rgba, px, py, radius, threshold, tolerance):
    """Возвращает (x, y, mode) с mode ∈ {'marker','line','none'}."""
    marker = _find_marker(rgba, px, py, radius, tolerance)
    if marker is not None:
        return marker[0], marker[1], 'marker'

    lx, ly, snapped = _snap_to_line(gray, px, py, radius, threshold)
    if snapped:
        return lx, ly, 'line'

    return px, py, 'none'
    

def _pixel_to_world(plane, px, py, W_px, H_px):
    """Пиксель выкройки → мировая точка на плоскости."""
    from mathutils import Vector
    if W_px <= 0 or H_px <= 0:
        return None
    u = px / W_px
    v = 1.0 - py / H_px

    xs = [vtx.co.x for vtx in plane.data.vertices]
    zs = [vtx.co.z for vtx in plane.data.vertices]
    hw = (max(xs) - min(xs)) / 2.0
    hh = (max(zs) - min(zs)) / 2.0

    local = Vector((u * 2 * hw - hw, 0.0, v * 2 * hh - hh))
    return plane.matrix_world @ local


def _ensure_visual_obj(scene, name):
    obj = scene.objects.get(name)
    if obj is not None and obj.type == 'MESH':
        return obj
    mesh = bpy.data.meshes.new(name + "_mesh")
    obj = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(obj)
    obj.display_type = 'WIRE'
    obj.hide_render = True
    # Тонкая настройка, чтобы не мешать работе
    obj.show_in_front = True
    return obj


def _rebuild_pending_mesh(scene):
    """Перестраивает outline текущего набираемого квада."""
    plane = _get_plane(scene)
    img = _get_layout_image(scene)
    pba = scene.pba_lm
    if plane is None or img is None:
        return

    W, H = img.size
    obj = _ensure_visual_obj(scene, PENDING_OBJ_NAME)
    mesh = obj.data
    mesh.clear_geometry()

    n = pba.pending_px_count
    if n <= 0:
        return

    verts = []
    for i in range(n):
        px = pba.pending_corners_px[i * 2]
        py = pba.pending_corners_px[i * 2 + 1]
        w = _pixel_to_world(plane, px, py, W, H)
        if w is not None:
            verts.append((w.x, w.y, w.z))

    edges = [(i, i + 1) for i in range(len(verts) - 1)]
    if len(verts) == 4:
        edges.append((3, 0))

    mesh.from_pydata(verts, edges, [])
    mesh.update()


def _rebuild_committed_mesh(scene):
    """Перестраивает outline всех закоммиченных квадов и островов."""
    plane = _get_plane(scene)
    img = _get_layout_image(scene)
    pba = scene.pba_lm
    if plane is None or img is None:
        return

    W, H = img.size
    obj = _ensure_visual_obj(scene, COMMITTED_OBJ_NAME)
    mesh = obj.data
    mesh.clear_geometry()

    all_verts = []
    all_edges = []

    # Квады — сплошные рёбра
    for q in pba.quads:
        px = list(q.corners_px)
        base = len(all_verts)
        for i in range(4):
            w = _pixel_to_world(plane, px[i * 2], px[i * 2 + 1], W, H)
            if w is None:
                all_verts.append((0.0, 0.0, 0.0))
            else:
                all_verts.append((w.x, w.y, w.z))
        all_edges.append((base + 0, base + 1))
        all_edges.append((base + 1, base + 2))
        all_edges.append((base + 2, base + 3))
        all_edges.append((base + 3, base + 0))

    # Острова — тоже сплошные рёбра (для визуального отличия можно
    # сделать их другого цвета, но на этапе MVP — те же)
    for isl in pba.islands:
        px = list(isl.corners_layout_px)
        base = len(all_verts)
        for i in range(4):
            w = _pixel_to_world(plane, px[i * 2], px[i * 2 + 1], W, H)
            if w is None:
                all_verts.append((0.0, 0.0, 0.0))
            else:
                all_verts.append((w.x, w.y, w.z))
        all_edges.append((base + 0, base + 1))
        all_edges.append((base + 1, base + 2))
        all_edges.append((base + 2, base + 3))
        all_edges.append((base + 3, base + 0))

    mesh.from_pydata(all_verts, all_edges, [])
    mesh.update()


def _remove_visual_objs(scene):
    for name in (PENDING_OBJ_NAME, COMMITTED_OBJ_NAME):
        obj = scene.objects.get(name)
        if obj is not None:
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)


def _remove_plane(scene):
    obj = _get_plane(scene)
    if obj is not None:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    scene.pba_lm.plane_obj_name = ""


def _create_plane(scene, image, width_m, offset_m):
    W_px, H_px = image.size
    if W_px == 0 or H_px == 0:
        raise ValueError("Изображение пустое")

    aspect = H_px / W_px
    height_m = width_m * aspect
    hw = width_m / 2.0
    hh = height_m / 2.0

    # Плоскость в XZ (y=0), нормаль смотрит в -Y
    verts = [(-hw, 0.0, -hh), (hw, 0.0, -hh),
             (hw, 0.0, hh), (-hw, 0.0, hh)]
    faces = [(0, 1, 2, 3)]

    mesh = bpy.data.meshes.new(PLANE_MESH_NAME)
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    uv_layer = mesh.uv_layers.new(name="UVMap")
    for i, uv in enumerate([(0.0, 0.0), (1.0, 0.0),
                             (1.0, 1.0), (0.0, 1.0)]):
        uv_layer.data[i].uv = uv

    obj = bpy.data.objects.new(PLANE_MESH_NAME, mesh)
    scene.collection.objects.link(obj)
    obj.location = (0.0, -offset_m, 0.0)

    # Материал
    mat = bpy.data.materials.new(PLANE_MAT_NAME)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = image
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Alpha"].default_value = 0.5
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if hasattr(mat, "blend_method"):
        try:
            mat.blend_method = 'BLEND'
        except Exception:
            pass
    mat.diffuse_color = (1.0, 1.0, 1.0, 0.5)
    obj.data.materials.append(mat)

    scene.pba_lm.plane_obj_name = obj.name
    return obj


# --- Сортировка углов ---

def _canonical_order(points_2d):
    """Порядок обхода 4 точек: по часовой стрелке (в системе с y вниз),
    начиная с верхнего-левого."""
    if len(points_2d) != 4:
        return list(range(len(points_2d)))
    cx = sum(p[0] for p in points_2d) / 4.0
    cy = sum(p[1] for p in points_2d) / 4.0

    order = sorted(
        range(4),
        key=lambda i: math.atan2(points_2d[i][1] - cy,
                                  points_2d[i][0] - cx))
    sums = [points_2d[i][0] + points_2d[i][1] for i in range(4)]
    tl_idx = sums.index(min(sums))
    pos = order.index(tl_idx)
    return order[pos:] + order[:pos]


def _get_view_direction(context):
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        return Vector((0.0, 1.0, 0.0))
    rv3d = area.spaces[0].region_3d
    return rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))


def _project_for_sorting(points_world, view_dir):
    """Проецирует 3D точки на плоскость вида, возвращает 2D экранные."""
    vd = np.array(view_dir, dtype=np.float64)
    n = np.linalg.norm(vd)
    if n < 1e-9:
        vd = np.array([0.0, 1.0, 0.0])
    else:
        vd = vd / n

    up_hint = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(up_hint, vd))) > 0.95:
        up_hint = np.array([0.0, 1.0, 0.0])
    right = np.cross(vd, up_hint)
    right = right / np.linalg.norm(right)
    up = np.cross(right, vd)
    up = up / np.linalg.norm(up)

    pts_2d = []
    for p in points_world:
        parr = np.array([p[0], p[1], p[2]], dtype=np.float64)
        x_r = float(np.dot(parr, right))
        y_u = float(np.dot(parr, up))
        pts_2d.append((x_r, -y_u))
    return pts_2d


def _get_selected_face_indices(obj):
    if bpy.context.mode != 'EDIT_MESH':
        return []
    import bmesh
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    return [f.index for f in bm.faces if f.select]


def _find_island_faces(obj, uv_name, seed_indices):
    """BFS по UV-связности. Работает и в Edit Mode, и в Object Mode."""
    import bmesh
    from collections import defaultdict

    if (bpy.context.mode == 'EDIT_MESH'
            and bpy.context.active_object == obj):
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.get(uv_name)
        if uv_layer is None:
            return set()
        loop_map = defaultdict(list)
        for f in bm.faces:
            for loop in f.loops:
                uv = loop[uv_layer].uv
                key = (loop.vert.index, round(uv.x, 6), round(uv.y, 6))
                loop_map[key].append(f.index)
    else:
        me = obj.data
        uv_layer = me.uv_layers.get(uv_name)
        if uv_layer is None or len(uv_layer.data) == 0:
            return set()
        loop_map = defaultdict(list)
        for poly in me.polygons:
            for li in poly.loop_indices:
                vi = me.loops[li].vertex_index
                uv = uv_layer.data[li].uv
                key = (vi, round(uv.x, 6), round(uv.y, 6))
                loop_map[key].append(poly.index)

    visited = set(seed_indices)
    queue = list(seed_indices)
    head = 0
    while head < len(queue):
        fi = queue[head]
        head += 1
        if fi < 0:
            continue
        # Собрать вершины грани fi
        if (bpy.context.mode == 'EDIT_MESH'
                and bpy.context.active_object == obj):
            bm.faces.ensure_lookup_table()
            if fi >= len(bm.faces):
                continue
            face = bm.faces[fi]
            for loop in face.loops:
                uv = loop[uv_layer].uv
                key = (loop.vert.index, round(uv.x, 6), round(uv.y, 6))
                for other in loop_map[key]:
                    if other not in visited:
                        visited.add(other)
                        queue.append(other)
        else:
            poly = obj.data.polygons[fi]
            for li in poly.loop_indices:
                vi = obj.data.loops[li].vertex_index
                uv = uv_layer.data[li].uv
                key = (vi, round(uv.x, 6), round(uv.y, 6))
                for other in loop_map[key]:
                    if other not in visited:
                        visited.add(other)
                        queue.append(other)
    return visited


def _vertex_uv_candidates(obj, uv_name, vertex_index, island_faces):
    """Все уникальные UV данной вершины в гранях острова."""
    import bmesh
    if (bpy.context.mode == 'EDIT_MESH'
            and bpy.context.active_object == obj):
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.get(uv_name)
        if uv_layer is None:
            return []
        uvs = []
        seen = set()
        for fi in island_faces:
            if fi < 0 or fi >= len(bm.faces):
                continue
            for loop in bm.faces[fi].loops:
                if loop.vert.index == vertex_index:
                    uv = loop[uv_layer].uv
                    key = (round(uv.x, 6), round(uv.y, 6))
                    if key not in seen:
                        seen.add(key)
                        uvs.append((uv.x, uv.y))
        return uvs
    else:
        me = obj.data
        uv_layer = me.uv_layers.get(uv_name)
        if uv_layer is None:
            return []
        uvs = []
        seen = set()
        for fi in island_faces:
            if fi < 0 or fi >= len(me.polygons):
                continue
            poly = me.polygons[fi]
            for li in poly.loop_indices:
                if me.loops[li].vertex_index == vertex_index:
                    uv = uv_layer.data[li].uv
                    key = (round(uv.x, 6), round(uv.y, 6))
                    if key not in seen:
                        seen.add(key)
                        uvs.append((uv.x, uv.y))
        return uvs


def _find_nearest_vertex(context, event, obj, allowed_indices,
                          radius_px=20.0):
    """Ближайшая к курсору вершина из allowed_indices."""
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    region = context.region
    rv3d = context.space_data.region_3d
    if region is None or rv3d is None:
        return None
    mouse = Vector((event.mouse_region_x, event.mouse_region_y))
    best_idx = None
    best_dist = radius_px
    M = obj.matrix_world
    for vi in allowed_indices:
        p_world = M @ obj.data.vertices[vi].co
        p_2d = location_3d_to_region_2d(region, rv3d, p_world)
        if p_2d is None:
            continue
        d = (p_2d - mouse).length
        if d < best_dist:
            best_dist = d
            best_idx = vi
    return best_idx


def _similarity_fit(uv_pts, layout_pts, theta_deg):
    """Оптимальное подобие layout ≈ a·R(θ)·uv + t.
    Возвращает (a, tx, ty, rmse) или None."""
    import math
    th = math.radians(theta_deg)
    c, s = math.cos(th), math.sin(th)
    R = np.array([[c, -s], [s, c]])

    uv = np.asarray(uv_pts, dtype=np.float64)
    lp = np.asarray(layout_pts, dtype=np.float64)

    rot_pts = uv @ R.T  # (4, 2)

    A = np.zeros((8, 3))
    b = lp.reshape(-1)
    for i in range(4):
        A[2 * i, 0] = rot_pts[i, 0]
        A[2 * i, 1] = 1.0
        A[2 * i + 1, 0] = rot_pts[i, 1]
        A[2 * i + 1, 2] = 1.0

    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    a, tx, ty = sol
    if abs(a) < 1e-9:
        return None

    pred = np.zeros((4, 2))
    for i in range(4):
        pred[i, 0] = a * rot_pts[i, 0] + tx
        pred[i, 1] = a * rot_pts[i, 1] + ty
    res = lp - pred
    rmse = float(np.sqrt((res ** 2).sum() / 4))
    return (a, tx, ty, rmse)

def _fit_best_uv_combination(candidates_per_corner, layout_px, verbose=True):
    """Полный перебор кандидатов и углов 0..360° шагом 2°."""
    import itertools
    import math

    layout = np.asarray(layout_px, dtype=np.float64)
    n_lists = [len(c) for c in candidates_per_corner]
    if any(n == 0 for n in n_lists):
        return None, None, None

    if verbose:
        print(f"[PBA LM] Layout points: {layout.tolist()}")
        for i, c in enumerate(candidates_per_corner):
            cands_str = ", ".join(f"({u:.4f},{v:.4f})" for (u, v) in c)
            print(f"[PBA LM]   corner {i}: {len(c)} cand: {cands_str}")

    best_rmse = float('inf')
    best_combo = None
    best_params = None
    best_uv_pts = None

    thetas = list(range(0, 360, 2))

    for combo in itertools.product(*[range(n) for n in n_lists]):
        uv_pts = []
        for i, k in enumerate(combo):
            u, v = candidates_per_corner[i][k]
            uv_pts.append((u, v))
        uv_pts = np.asarray(uv_pts, dtype=np.float64)

        for theta in thetas:
            res = _similarity_fit(uv_pts, layout, theta)
            if res is None:
                continue
            a, tx, ty, rmse = res
            if rmse < best_rmse:
                best_rmse = rmse
                best_combo = combo
                best_params = (a, theta, tx, ty)
                best_uv_pts = uv_pts

    if best_combo is None:
        return None, None, None

    if verbose:
        print(f"[PBA LM] Best: rmse={best_rmse:.4f}, "
              f"scale={best_params[0]:.3f}, θ={best_params[1]}°, "
              f"combo={best_combo}")
        a, theta, tx, ty = best_params
        th = math.radians(theta)
        c, s = math.cos(th), math.sin(th)
        R = np.array([[c, -s], [s, c]])
        for i in range(4):
            rp = R @ best_uv_pts[i]
            px = a * rp[0] + tx
            py = a * rp[1] + ty
            print(f"[PBA LM]   corner {i}: pred=({px:.1f},{py:.1f}) "
                  f"act=({layout[i,0]:.1f},{layout[i,1]:.1f})")

    return best_combo, best_rmse, best_params

# --- Операторы ---

class PBA_LM_OT_load_layout(bpy.types.Operator):
    bl_idname = "pba_lm.load_layout"
    bl_label = "Load Layout"
    bl_description = "Загрузить PNG выкройки и создать плоскость"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        path = bpy.path.abspath(pba.layout_path)
        if not path or not os.path.exists(path):
            self.report({'ERROR'}, "Файл не найден")
            return {'CANCELLED'}

        try:
            img = bpy.data.images.load(path, check_existing=True)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка загрузки: {e}")
            return {'CANCELLED'}

        if img.size[0] == 0 or img.size[1] == 0:
            self.report({'ERROR'}, "Изображение пустое")
            return {'CANCELLED'}

        _remove_plane(scene)
        _delete_highlight_obj(scene, HIGHLIGHT_LAYOUT_NAME)
        _delete_highlight_obj(scene, HIGHLIGHT_MODEL_NAME)
        _remove_visual_objs(scene)
        _IMAGE_CACHE.clear()
        try:
            _create_plane(scene, img, pba.plane_width_m, pba.plane_offset_m)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка создания плоскости: {e}")
            return {'CANCELLED'}

        pba.layout_image_name = img.name
        _set_status(
            scene,
            f"Загружено {img.size[0]}×{img.size[1]} px. "
            f"Переключитесь в вид спереди (Numpad 1).")
        self.report({'INFO'}, "Выкройка загружена")
        return {'FINISHED'}


class PBA_LM_OT_capture_3d(bpy.types.Operator):
    bl_idname = "pba_lm.capture_3d"
    bl_label = "Capture 4 3D Vertices"
    bl_description = "Выделите 4 вершины в Edit Mode и нажмите"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Нужен активный mesh-объект")
            return {'CANCELLED'}
        if context.mode != 'EDIT_MESH':
            self.report({'ERROR'}, "Нужен Edit Mode")
            return {'CANCELLED'}

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        selected = [v for v in bm.verts if v.select]
        if len(selected) != 4:
            self.report({'ERROR'}, f"Выделено {len(selected)}, нужно 4")
            return {'CANCELLED'}

        M = obj.matrix_world
        world_pos = [M @ v.co for v in selected]
        view_dir = _get_view_direction(context)
        pts_2d = _project_for_sorting(world_pos, view_dir)
        order = _canonical_order(pts_2d)

        pba.pending_corners_3d.clear()
        for i in order:
            v = selected[i]
            wp = world_pos[i]
            c = pba.pending_corners_3d.add()
            c.obj_name = obj.name
            c.vertex_index = v.index
            c.vertex_uuid = get_vertex_uuid(obj, v.index)
            c.x_world = (wp.x, wp.y, wp.z)

        ids = [f"v{selected[i].index}" for i in order]
        _set_status(scene, f"3D захвачены: {', '.join(ids)}. "
                           f"Теперь кликните 4 точки на выкройке.")
        self.report({'INFO'}, "4 вершины захвачены")
        return {'FINISHED'}


class PBA_LM_OT_pick_image_points(bpy.types.Operator):
    bl_idname = "pba_lm.pick_image_points"
    bl_label = "Pick 4 Image Points"
    bl_description = "Кликните 4 точки на плоскости выкройки"
    bl_options = {'REGISTER'}

    _clicks = 0

    def invoke(self, context, event):
        scene = context.scene
        pba = scene.pba_lm
        if _get_plane(scene) is None:
            self.report({'ERROR'}, "Плоскость выкройки не создана")
            return {'CANCELLED'}

        # Проверка в зависимости от режима
        if pba.mode == 'ISLAND':
            if len(pba.pending_island_corners) != 4:
                self.report({'ERROR'},
                            "Сначала выделите 4 вершины острова")
                return {'CANCELLED'}
        else:
            if len(pba.pending_corners_3d) != 4:
                self.report({'ERROR'},
                            "Сначала захватите 4 3D-вершины")
                return {'CANCELLED'}

        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        self._clicks = 0
        pba.pending_px_count = 0
        pba.pending_corners_px = (0.0,) * 8

        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Кликните 4 точки на выкройке. ESC — отмена.")
        _set_status(scene, "Ожидание точки 1 из 4")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        scene = context.scene
        pba = scene.pba_lm

        if event.type in {'ESC', 'RIGHTMOUSE'}:
            context.workspace.status_text_set(None)
            _set_status(scene, "Отменено")
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            coord = (event.mouse_region_x, event.mouse_region_y)
            origin = region_2d_to_origin_3d(
                context.region, context.space_data.region_3d, coord)
            direction = region_2d_to_vector_3d(
                context.region, context.space_data.region_3d, coord)

            plane = _get_plane(scene)
            if plane is None:
                context.workspace.status_text_set(None)
                return {'CANCELLED'}

            inv = plane.matrix_world.inverted()
            o_loc = inv @ origin
            d_loc = inv.to_3x3() @ direction
            hit, loc, _, _ = plane.ray_cast(o_loc, d_loc)
            if not hit:
                return {'RUNNING_MODAL'}

            mesh = plane.data
            xs = [v.co.x for v in mesh.vertices]
            zs = [v.co.z for v in mesh.vertices]
            hw = (max(xs) - min(xs)) / 2.0
            hh = (max(zs) - min(zs)) / 2.0

            u = (loc.x + hw) / (2.0 * hw)
            v = (loc.z + hh) / (2.0 * hh)

            img = _get_layout_image(scene)
            if img is None:
                return {'RUNNING_MODAL'}
            W_px, H_px = img.size
            px = u * W_px
            py = (1.0 - v) * H_px

            # Snap: маркер → линия → клик
            mode = 'none'
            if pba.snap_to_lines:
                cached = _get_image_cached(img)
                if cached is not None:
                    gray, rgba, _, _ = cached
                    px, py, mode = _snap_to_features(
                        gray, rgba, px, py, pba.snap_radius_px,
                        pba.snap_threshold,
                        pba.snap_marker_tolerance)

            arr = list(pba.pending_corners_px)
            arr[self._clicks * 2] = px
            arr[self._clicks * 2 + 1] = py
            pba.pending_corners_px = tuple(arr)
            pba.pending_px_count = self._clicks + 1
            self._clicks += 1

            _rebuild_pending_mesh(scene)

            suffix = {
                'marker': " (маркер)",
                'line':   " (линия)",
                'none':   "",
            }[mode]
            _set_status(scene,
                        f"Точка {self._clicks}: "
                        f"({px:.1f}, {py:.1f}){suffix}")

            if self._clicks >= 4:
                context.workspace.status_text_set(None)
                _set_status(scene,
                            "4 точки заданы. Commit или Undo Last.")
                return {'FINISHED'}

        return {'RUNNING_MODAL'}


def _commit_quad(scene):
    pba = scene.pba_lm
    if len(pba.pending_corners_3d) != 4 or pba.pending_px_count != 4:
        return

    # Канонический порядок изображения
    px = list(pba.pending_corners_px)
    pts_2d = [(px[0], px[1]), (px[2], px[3]),
              (px[4], px[5]), (px[6], px[7])]
    order = _canonical_order(pts_2d)
    pts_sorted = [pts_2d[i] for i in order]

    q = pba.quads.add()
    pba.quad_counter += 1
    q.id = f"q_{pba.quad_counter:03d}"
    q.part = pba.pending_part

    for c in pba.pending_corners_3d:
        nc = q.corners_3d.add()
        nc.obj_name = c.obj_name
        nc.vertex_index = c.vertex_index
        nc.x_world = c.x_world
        nc.vertex_uuid = c.vertex_uuid

    flat = []
    for (x, y) in pts_sorted:
        flat.extend([x, y])
    q.corners_px = tuple(flat)

    pba.pending_corners_3d.clear()
    pba.pending_corners_px = (0.0,) * 8
    pba.pending_px_count = 0

    _set_status(scene, f"Квад {q.id} добавлен. "
                       f"Всего квадов: {len(pba.quads)}")


class PBA_LM_OT_cancel_quad(bpy.types.Operator):
    bl_idname = "pba_lm.cancel_quad"
    bl_label = "Cancel Quad"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        pba.pending_corners_3d.clear()
        pba.pending_corners_px = (0.0,) * 8
        pba.pending_px_count = 0
        _rebuild_pending_mesh(scene)
        _set_status(scene, "Текущий квад сброшен")
        return {'FINISHED'}


class PBA_LM_OT_remove_quad(bpy.types.Operator):
    bl_idname = "pba_lm.remove_quad"
    bl_label = "Remove Quad"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        pba = context.scene.pba_lm
        if 0 <= self.index < len(pba.quads):
            pba.quads.remove(self.index)
        pba.active_quad_index = min(
            pba.active_quad_index, len(pba.quads) - 1)
        bpy.ops.pba_lm.refresh_highlight()
        _rebuild_committed_mesh(context.scene)
        return {'FINISHED'}


class PBA_LM_OT_clear_quads(bpy.types.Operator):
    bl_idname = "pba_lm.clear_quads"
    bl_label = "Clear All Quads"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        pba = context.scene.pba_lm
        pba.quads.clear()
        pba.quad_counter = 0
        pba.active_quad_index = min(
            pba.active_quad_index, len(pba.quads) - 1)
        bpy.ops.pba_lm.refresh_highlight()
        _rebuild_committed_mesh(context.scene)
        _set_status(context.scene, "Все квады удалены")
        return {'FINISHED'}


class PBA_LM_OT_export_json(bpy.types.Operator):
    bl_idname = "pba_lm.export_json"
    bl_label = "Export JSON"
    bl_description = "Сохранить разметку в JSON"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        if not pba.quads and not pba.islands:
            self.report({'WARNING'},
                        "Нет размеченных квадов или островов")
            return {'CANCELLED'}

        layout_path = bpy.path.abspath(pba.layout_path)
        if not layout_path:
            self.report({'ERROR'}, "Не задан путь к выкройке")
            return {'CANCELLED'}

        img = _get_layout_image(scene)
        if img is None:
            self.report({'ERROR'}, "Изображение выкройки не загружено")
            return {'CANCELLED'}
        W_px, H_px = img.size
        orientation = "portrait" if H_px > W_px else "landscape"

        quads_data = []
        for q in pba.quads:
            corners_3d = []
            for c in q.corners_3d:
                item = {
                    "obj": c.obj_name,
                    "vertex": c.vertex_index,
                    "x_world": [float(c.x_world[0]),
                                 float(c.x_world[1]),
                                 float(c.x_world[2])],
                }
                if c.vertex_uuid > 0:
                    item["vertex_uuid"] = int(c.vertex_uuid)
                corners_3d.append(item)
            px = list(q.corners_px)
            corners_px = [[px[0], px[1]], [px[2], px[3]],
                          [px[4], px[5]], [px[6], px[7]]]
            quads_data.append({
                "id": q.id,
                "part": q.part,
                "corners_3d": corners_3d,
                "corners_layout_px": corners_px,
            })

        islands_data = []
        for isl in pba.islands:
            corners = []
            for c in isl.corners:
                item = {
                    "obj": c.obj_name,
                    "vertex": c.vertex_index,
                    "uv": [float(c.uv[0]), float(c.uv[1])],
                    "x_world": [float(c.x_world[0]),
                                 float(c.x_world[1]),
                                 float(c.x_world[2])],
                }
                if c.vertex_uuid > 0:
                    item["vertex_uuid"] = int(c.vertex_uuid)
                corners.append(item)
            px = list(isl.corners_layout_px)
            corners_px = [[px[0], px[1]], [px[2], px[3]],
                          [px[4], px[5]], [px[6], px[7]]]
            item = {
                "id": isl.id,
                "part": isl.part,
                "corners": corners,
                "corners_layout_px": corners_px,
            }
            res = [c.fit_residual for c in isl.corners if c.fit_residual >= 0]
            if res:
                item["fit_residual"] = float(res[0])
            if isl.duplicate_of:
                item["duplicate_of"] = isl.duplicate_of
            islands_data.append(item)

        data = {
            "sheet": os.path.basename(layout_path),
            "size": [W_px, H_px],
            "dpi": 600,
            "orientation": orientation,
            "quads": quads_data,
            "islands": islands_data
        }

        out = os.path.splitext(layout_path)[0] + ".quads.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        _set_status(scene, f"Сохранено: {out} ({len(pba.quads)} квадов)")
        self.report({'INFO'}, f"JSON: {out}")
        return {'FINISHED'}


class PBA_LM_OT_frame_layout(bpy.types.Operator):
    bl_idname = "pba_lm.frame_layout"
    bl_label = "Frame Layout View"
    bl_description = "Ортографический вид спереди + Material Preview"
    bl_options = {'REGISTER'}

    def execute(self, context):
        from mathutils import Quaternion, Vector

        scene = context.scene
        plane = _get_plane(scene)
        if plane is None:
            self.report({'ERROR'}, "Плоскость выкройки не создана")
            return {'CANCELLED'}

        # Найти 3D Viewport
        area = None
        if context.area is not None and context.area.type == 'VIEW_3D':
            area = context.area
        else:
            for a in context.screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    break

        if area is None:
            self.report({'ERROR'}, "Не найден 3D Viewport")
            return {'CANCELLED'}

        space = area.spaces.active
        rv3d = space.region_3d

        # Режим отображения: Material Preview
        try:
            space.shading.type = 'MATERIAL'
        except Exception as e:
            print(f"[PBA LM] не удалось переключить shading: {e}")

        # Ортографическая проекция
        rv3d.view_perspective = 'ORTHO'

        # Front view: поворот вокруг X на 90°
        rv3d.view_rotation = Quaternion((0.7071068, 0.7071068, 0.0, 0.0))

        # Центр плоскости в мире
        center = plane.matrix_world.translation.copy()
        rv3d.view_location = Vector((center.x, center.y, center.z))

        # Дистанция и масштаб
        pba = scene.pba_lm
        rv3d.view_distance = max(pba.plane_offset_m, 1.0)

        try:
            rv3d.ortho_scale = pba.plane_width_m * 1.1
        except Exception as e:
            print(f"[PBA LM] не удалось задать ortho_scale: {e}")

        _set_status(scene, "Вид переключён: Front, Ortho, Material Preview")
        return {'FINISHED'}

class PBA_LM_OT_undo_last_click(bpy.types.Operator):
    bl_idname = "pba_lm.undo_last_click"
    bl_label = "Undo Last Click"
    bl_description = "Убрать последний клик по выкройке"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        if pba.pending_px_count <= 0:
            self.report({'WARNING'}, "Нет кликов для отмены")
            return {'CANCELLED'}
        pba.pending_px_count -= 1
        arr = list(pba.pending_corners_px)
        arr[pba.pending_px_count * 2] = 0.0
        arr[pba.pending_px_count * 2 + 1] = 0.0
        pba.pending_corners_px = tuple(arr)
        _rebuild_pending_mesh(scene)
        _set_status(scene,
                    f"Отменён. Осталось {pba.pending_px_count} кликов")
        return {'FINISHED'}


class PBA_LM_OT_commit_quad(bpy.types.Operator):
    bl_idname = "pba_lm.commit_quad"
    bl_label = "Commit Quad"
    bl_description = "Сохранить набранный квад"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        if len(pba.pending_corners_3d) != 4:
            self.report({'ERROR'}, "Нужно 4 3D-вершины")
            return {'CANCELLED'}
        if pba.pending_px_count != 4:
            self.report({'ERROR'}, "Нужно 4 клика по выкройке")
            return {'CANCELLED'}

        _commit_quad(scene)
        _rebuild_pending_mesh(scene)
        _rebuild_committed_mesh(scene)
        return {'FINISHED'}


def _is_convex_quad_order(pts):
    """Проверяет, что 4 точки в порядке обхода без самопересечений."""
    n = len(pts)
    sign = None
    for i in range(n):
        p0 = pts[i]
        p1 = pts[(i + 1) % n]
        p2 = pts[(i + 2) % n]
        cross = ((p1[0] - p0[0]) * (p2[1] - p1[1])
                 - (p1[1] - p0[1]) * (p2[0] - p1[0]))
        if abs(cross) < 1e-9:
            continue
        s = 1 if cross > 0 else -1
        if sign is None:
            sign = s
        elif s != sign:
            return False
    return True


class PBA_LM_OT_validate_quads(bpy.types.Operator):
    bl_idname = "pba_lm.validate_quads"
    bl_label = "Validate Quads"
    bl_description = "Проверить порядок углов в размеченных квадах"
    bl_options = {'REGISTER'}

    def execute(self, context):
        pba = context.scene.pba_lm
        bad = []
        for i, q in enumerate(pba.quads):
            px = list(q.corners_px)
            pts = [(px[0], px[1]), (px[2], px[3]),
                   (px[4], px[5]), (px[6], px[7])]
            if not _is_convex_quad_order(pts):
                bad.append((i, q.id))

        if not bad:
            _set_status(context.scene,
                        f"Все {len(pba.quads)} квадов корректны")
            self.report({'INFO'}, "Все квады корректны")
            return {'FINISHED'}

        print("[PBA LM] Подозрительные квады (порядок углов):")
        for i, qid in bad:
            print(f"  [{i}] {qid}")
        _set_status(context.scene,
                    f"Подозрительных: {len(bad)} из {len(pba.quads)} "
                    f"(см. консоль)")
        self.report({'WARNING'}, f"Подозрительных: {len(bad)}")
        return {'FINISHED'}


class PBA_LM_OT_rotate_quad(bpy.types.Operator):
    bl_idname = "pba_lm.rotate_quad"
    bl_label = "Rotate Quad Order"
    bl_description = "Сдвинуть порядок 2D-кликов квада на одну позицию"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()
    direction: bpy.props.IntProperty(default=1)

    def execute(self, context):
        pba = context.scene.pba_lm
        if not (0 <= self.index < len(pba.quads)):
            return {'CANCELLED'}
        q = pba.quads[self.index]

        px = list(q.corners_px)
        pts = [[px[0], px[1]], [px[2], px[3]],
               [px[4], px[5]], [px[6], px[7]]]
        if self.direction > 0:
            pts = pts[1:] + pts[:1]
        else:
            pts = pts[-1:] + pts[:-1]
        flat = []
        for x, y in pts:
            flat.extend([x, y])
        q.corners_px = tuple(flat)

        _set_status(context.scene,
                    f"Квад {q.id}: поворот 2D-кликов")
        return {'FINISHED'}


class PBA_LM_OT_flip_quad(bpy.types.Operator):
    bl_idname = "pba_lm.flip_quad"
    bl_label = "Flip Quad Order"
    bl_description = "Обратить порядок 2D-кликов"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        pba = context.scene.pba_lm
        if not (0 <= self.index < len(pba.quads)):
            return {'CANCELLED'}
        q = pba.quads[self.index]

        px = list(q.corners_px)
        pts = [[px[0], px[1]], [px[2], px[3]],
               [px[4], px[5]], [px[6], px[7]]]
        pts = [pts[0], pts[3], pts[2], pts[1]]
        flat = []
        for x, y in pts:
            flat.extend([x, y])
        q.corners_px = tuple(flat)

        _set_status(context.scene,
                    f"Квад {q.id}: отражение 2D-кликов")
        return {'FINISHED'}
    bl_idname = "pba_lm.flip_quad"
    bl_label = "Flip Quad"
    bl_description = "Обратить порядок углов (исправить зеркало)"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        pba = context.scene.pba_lm
        if not (0 <= self.index < len(pba.quads)):
            return {'CANCELLED'}
        q = pba.quads[self.index]

        items = [(
            q.corners_3d[i].obj_name,
            q.corners_3d[i].vertex_index,
            tuple(q.corners_3d[i].x_world),
        ) for i in range(4)]
        items = [items[0], items[3], items[2], items[1]]
        for i, (obj, vi, xw) in enumerate(items):
            q.corners_3d[i].obj_name = obj
            q.corners_3d[i].vertex_index = vi
            q.corners_3d[i].x_world = xw

        px = list(q.corners_px)
        pts = [[px[0], px[1]], [px[2], px[3]],
               [px[4], px[5]], [px[6], px[7]]]
        pts = [pts[0], pts[3], pts[2], pts[1]]
        flat = []
        for x, y in pts:
            flat.extend([x, y])
        q.corners_px = tuple(flat)

        _set_status(context.scene, f"Квад {q.id} отражён")
        return {'FINISHED'}


class PBA_LM_OT_import_json(bpy.types.Operator):
    bl_idname = "pba_lm.import_json"
    bl_label = "Import JSON"
    bl_description = "Загрузить квады и острова из JSON"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(
        default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        path = bpy.path.abspath(self.filepath)
        if not path or not os.path.exists(path):
            self.report({'ERROR'}, "Файл не найден")
            return {'CANCELLED'}

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка чтения: {e}")
            return {'CANCELLED'}

        # --- Квады ---
        pba.quads.clear()
        quads_missing = 0
        for qd in data.get("quads", []):
            q = pba.quads.add()
            q.id = qd.get("id", "")
            q.part = qd.get("part", "")
            if "duplicate_of" in qd:
                q.duplicate_of = qd["duplicate_of"]

            for cd in qd.get("corners_3d", []):
                c = q.corners_3d.add()
                c.obj_name = cd.get("obj", "")
                c.vertex_index = int(cd.get("vertex", -1))
                xw = cd.get("x_world", [0.0, 0.0, 0.0])
                c.x_world = (float(xw[0]), float(xw[1]), float(xw[2]))

                vuuid = int(cd.get("vertex_uuid", 0))
                c.vertex_uuid = vuuid

                if vuuid > 0:
                    obj = scene.objects.get(c.obj_name)
                    if obj is not None and obj.type == 'MESH':
                        hint = (c.x_world[0],
                                c.x_world[1],
                                c.x_world[2])
                        idx, n_cand = find_vertex_by_uuid(
                            obj, vuuid, hint_world=hint)
                        if idx >= 0:
                            c.vertex_index = idx
                            if n_cand > 1:
                                print(f"[PBA LM] {q.id}: "
                                      f"{c.obj_name} uuid={vuuid} — "
                                      f"{n_cand} вершин, "
                                      f"выбрана ближайшая")
                        else:
                            quads_missing += 1
                            print(f"[PBA LM] {q.id}: "
                                  f"{c.obj_name} uuid={vuuid} "
                                  f"не найден")

            px_list = qd.get("corners_layout_px", [])
            flat = [0.0] * 8
            for i, pt in enumerate(px_list[:4]):
                flat[i * 2] = float(pt[0])
                flat[i * 2 + 1] = float(pt[1])
            q.corners_px = tuple(flat)

        # --- Острова ---
        pba.islands.clear()
        islands_missing = 0
        for idata in data.get("islands", []):
            isl = pba.islands.add()
            isl.id = idata.get("id", "")
            isl.part = idata.get("part", "")
            if "duplicate_of" in idata:
                isl.duplicate_of = idata["duplicate_of"]

            for cd in idata.get("corners", []):
                c = isl.corners.add()
                c.obj_name = cd.get("obj", "")
                c.vertex_index = int(cd.get("vertex", -1))
                uv = cd.get("uv", [0.0, 0.0])
                c.uv = (float(uv[0]), float(uv[1]))
                xw = cd.get("x_world", [0.0, 0.0, 0.0])
                c.x_world = (float(xw[0]), float(xw[1]), float(xw[2]))

                # uv_candidates может отсутствовать в старых файлах
                uvc = cd.get("uv_candidates", [])
                if uvc:
                    n = min(len(uvc) // 2, 8)
                    arr = [0.0] * 16
                    for k in range(n):
                        arr[k * 2] = float(uvc[k * 2])
                        arr[k * 2 + 1] = float(uvc[k * 2 + 1])
                    c.uv_candidates = tuple(arr)
                    c.n_uv_candidates = n
                else:
                    c.n_uv_candidates = 0

                vuuid = int(cd.get("vertex_uuid", 0))
                c.vertex_uuid = vuuid
                if "fit_residual" in cd:
                    c.fit_residual = float(cd["fit_residual"])

                if vuuid > 0:
                    obj = scene.objects.get(c.obj_name)
                    if obj is not None and obj.type == 'MESH':
                        hint = (c.x_world[0],
                                c.x_world[1],
                                c.x_world[2])
                        idx, n_cand = find_vertex_by_uuid(
                            obj, vuuid, hint_world=hint)
                        if idx >= 0:
                            c.vertex_index = idx
                            if n_cand > 1:
                                print(f"[PBA LM] {isl.id}: "
                                      f"{c.obj_name} uuid={vuuid} — "
                                      f"{n_cand} вершин, "
                                      f"выбрана ближайшая")
                        else:
                            islands_missing += 1
                            print(f"[PBA LM] {isl.id}: "
                                  f"{c.obj_name} uuid={vuuid} "
                                  f"не найден")

            px_list = idata.get("corners_layout_px", [])
            flat = [0.0] * 8
            for i, pt in enumerate(px_list[:4]):
                flat[i * 2] = float(pt[0])
                flat[i * 2 + 1] = float(pt[1])
            isl.corners_layout_px = tuple(flat)

        # Счётчики
        pba.quad_counter = max(
            (int(q.id.split("_")[-1]) for q in pba.quads
             if q.id.startswith("q_") and q.id.split("_")[-1].isdigit()),
            default=0)
        pba.island_counter = max(
            (int(i.id.split("_")[-1]) for i in pba.islands
             if i.id.startswith("i_") and i.id.split("_")[-1].isdigit()),
            default=0)

        pba.active_quad_index = 0 if pba.quads else -1

        # Визуализация
        _rebuild_committed_mesh(scene)
        _rebuild_pending_mesh(scene)

        # Статус
        parts = [f"Квадов: {len(pba.quads)}, "
                 f"островов: {len(pba.islands)}"]
        if quads_missing:
            parts.append(f"потеряно углов квадов: {quads_missing}")
        if islands_missing:
            parts.append(f"потеряно углов островов: {islands_missing}")
        msg = ", ".join(parts)
        _set_status(scene, msg)
        self.report({'INFO'}, msg)
        return {'FINISHED'}
        

class PBA_LM_OT_set_active_quad(bpy.types.Operator):
    bl_idname = "pba_lm.set_active_quad"
    bl_label = "Set Active Quad"
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        pba = context.scene.pba_lm
        if 0 <= self.index < len(pba.quads):
            pba.active_quad_index = self.index
            return bpy.ops.pba_lm.refresh_highlight()
        return {'CANCELLED'}


class PBA_LM_OT_refresh_highlight(bpy.types.Operator):
    bl_idname = "pba_lm.refresh_highlight"
    bl_label = "Refresh Highlight"
    bl_description = "Перестроить маркеры активного квада или острова"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm

        # Удаляем старые маркеры
        _delete_highlight_obj(scene, HIGHLIGHT_LAYOUT_NAME)
        _delete_highlight_obj(scene, HIGHLIGHT_MODEL_NAME)
        pba.highlight_layout_obj = ""
        pba.highlight_model_obj = ""

        # Определяем, что показывать: остров или квад
        corners_world = []
        corners_px = None
        label = ""

        if 0 <= pba.active_island_index < len(pba.islands):
            isl = pba.islands[pba.active_island_index]
            label = f"Остров {isl.id}"
            px = list(isl.corners_layout_px)
            corners_px = [(px[0], px[1]), (px[2], px[3]),
                          (px[4], px[5]), (px[6], px[7])]
            for c in isl.corners:
                obj = scene.objects.get(c.obj_name)
                if (obj is not None and obj.type == 'MESH'
                        and 0 <= c.vertex_index < len(obj.data.vertices)):
                    v = obj.data.vertices[c.vertex_index]
                    p = obj.matrix_world @ v.co
                    corners_world.append((p.x, p.y, p.z))
                else:
                    corners_world.append((c.x_world[0],
                                           c.x_world[1],
                                           c.x_world[2]))
        elif 0 <= pba.active_quad_index < len(pba.quads):
            q = pba.quads[pba.active_quad_index]
            label = f"Квад {q.id}"
            px = list(q.corners_px)
            corners_px = [(px[0], px[1]), (px[2], px[3]),
                          (px[4], px[5]), (px[6], px[7])]
            for c in q.corners_3d:
                obj = scene.objects.get(c.obj_name)
                if (obj is not None and obj.type == 'MESH'
                        and 0 <= c.vertex_index < len(obj.data.vertices)):
                    v = obj.data.vertices[c.vertex_index]
                    p = obj.matrix_world @ v.co
                    corners_world.append((p.x, p.y, p.z))
                else:
                    corners_world.append((c.x_world[0],
                                           c.x_world[1],
                                           c.x_world[2]))
        else:
            return {'FINISHED'}

        size = pba.highlight_size

        # --- Маркеры на выкройке ---
        plane = _get_plane(scene)
        img = _get_layout_image(scene)
        if plane is not None and img is not None and corners_px is not None:
            W_px, H_px = img.size
            plane_y = plane.matrix_world.translation.y
            cross_y = plane_y - 0.01

            all_verts = []
            all_faces = []
            colors = []
            for corner_i, (cx_px, cy_px) in enumerate(corners_px):
                w = _pixel_to_world(plane, cx_px, cy_px, W_px, H_px)
                if w is None:
                    continue
                v, f = _make_cross_geometry(
                    w.x, cross_y, w.z,
                    size * 1.5, size * 0.25)
                base = len(all_verts)
                all_verts.extend(v)
                for face in f:
                    all_faces.append(tuple(base + i for i in face))
                for _ in range(len(v)):
                    colors.append(CORNER_COLORS[corner_i])

            if all_verts:
                mesh = bpy.data.meshes.new(
                    HIGHLIGHT_LAYOUT_NAME + "_mesh")
                mesh.from_pydata(all_verts, [], all_faces)
                mesh.update()
                _apply_colors(mesh, colors)
                obj = bpy.data.objects.new(HIGHLIGHT_LAYOUT_NAME, mesh)
                scene.collection.objects.link(obj)
                obj.data.materials.append(
                    _ensure_color_material("PBA_LM_HighlightMat"))
                pba.highlight_layout_obj = obj.name

        # --- Маркеры на 3D-модели ---
        if len(corners_world) == 4:
            all_verts = []
            all_faces = []
            colors = []
            for corner_i, cw in enumerate(corners_world):
                v, f = _make_octahedron_geometry(
                    cw[0], cw[1], cw[2], size)
                base = len(all_verts)
                all_verts.extend(v)
                for face in f:
                    all_faces.append(tuple(base + i for i in face))
                for _ in range(len(v)):
                    colors.append(CORNER_COLORS[corner_i])

            if all_verts:
                mesh = bpy.data.meshes.new(
                    HIGHLIGHT_MODEL_NAME + "_mesh")
                mesh.from_pydata(all_verts, [], all_faces)
                mesh.update()
                _apply_colors(mesh, colors)
                obj = bpy.data.objects.new(HIGHLIGHT_MODEL_NAME, mesh)
                scene.collection.objects.link(obj)
                obj.data.materials.append(
                    _ensure_color_material("PBA_LM_HighlightMat"))
                pba.highlight_model_obj = obj.name

        _set_status(scene,
                    f"{label}: маркеры показаны (R=0, G=1, B=2, Y=3)")
        return {'FINISHED'}


class PBA_LM_OT_start_island(bpy.types.Operator):
    bl_idname = "pba_lm.start_island"
    bl_label = "Start Island"
    bl_description = ("Подготовить остров: выделите грани одной "
                      "UV-развёртки в Edit Mode")
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm

        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Нужен активный mesh-объект")
            return {'CANCELLED'}
        if context.mode != 'EDIT_MESH':
            self.report({'ERROR'}, "Нужен Edit Mode")
            return {'CANCELLED'}

        uv_name = None
        if obj.data.uv_layers.active is not None:
            uv_name = obj.data.uv_layers.active.name
        elif obj.data.uv_layers:
            uv_name = obj.data.uv_layers[0].name
        if uv_name is None:
            self.report({'ERROR'}, "У объекта нет UV-слоя")
            return {'CANCELLED'}

        seeds = _get_selected_face_indices(obj)
        if not seeds:
            self.report({'ERROR'}, "Выделите грани острова (L под курсором)")
            return {'CANCELLED'}

        island_faces = _find_island_faces(obj, uv_name, seeds)
        if not island_faces:
            self.report({'ERROR'}, "Остров не найден")
            return {'CANCELLED'}

        pba.pending_island_obj = obj.name
        pba.pending_island_uv_name = uv_name
        pba.pending_island_corners.clear()
        pba.pending_corners_px = (0.0,) * 8
        pba.pending_px_count = 0

        _set_status(
            scene,
            f"Остров: {obj.name}, {len(island_faces)} граней. "
            f"Кликните 4 вершины острова по порядку.")
        self.report({'INFO'},
                    f"Остров: {len(island_faces)} граней")
        return {'FINISHED'}


class PBA_LM_OT_pick_island_vertices(bpy.types.Operator):
    bl_idname = "pba_lm.pick_island_vertices"
    bl_label = "Pick 4 Island Vertices"
    bl_description = "Кликните 4 вершины острова по порядку"

    _clicks = 0
    _island_faces = None
    _allowed = None

    def invoke(self, context, event):
        scene = context.scene
        pba = scene.pba_lm
        obj = context.active_object
        if obj is None or obj.name != pba.pending_island_obj:
            self.report({'ERROR'},
                        "Активный объект не совпадает с pending островом")
            return {'CANCELLED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        seeds = _get_selected_face_indices(obj)
        if not seeds:
            self.report({'ERROR'}, "Выделение острова потеряно, "
                        "перезапустите Start Island")
            return {'CANCELLED'}

        self._island_faces = _find_island_faces(
            obj, pba.pending_island_uv_name, seeds)
        verts = set()
        for fi in self._island_faces:
            poly = obj.data.polygons[fi]
            for li in poly.loop_indices:
                verts.add(obj.data.loops[li].vertex_index)
        self._allowed = verts
        self._clicks = 0

        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Кликните вершину 1 из 4 (ESC — отмена)")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            context.workspace.status_text_set(None)
            _set_status(context.scene, "Отменено")
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            scene = context.scene
            pba = scene.pba_lm
            obj = context.active_object

            vi = _find_nearest_vertex(context, event, obj, self._allowed)
            if vi is None:
                return {'RUNNING_MODAL'}

            uvs = _vertex_uv_candidates(
                obj, pba.pending_island_uv_name, vi, self._island_faces)
            if not uvs:
                return {'RUNNING_MODAL'}

            p_world = obj.matrix_world @ obj.data.vertices[vi].co
            c = pba.pending_island_corners.add()
            c.obj_name = obj.name
            c.vertex_index = vi
            c.vertex_uuid = get_vertex_uuid(obj, vi)
            n = min(len(uvs), 8)
            c.n_uv_candidates = n
            arr = [0.0] * 16
            for k in range(n):
                arr[k * 2] = uvs[k][0]
                arr[k * 2 + 1] = uvs[k][1]
            c.uv_candidates = tuple(arr)
            c.uv = uvs[0]
            c.x_world = (p_world.x, p_world.y, p_world.z)
            self._clicks += 1

            _set_status(
                scene,
                f"Вершина {self._clicks}: v{vi}, "
                f"{n} UV-вариант(ов)")

            # Обновить панель
            if context.area is not None:
                context.area.tag_redraw()

            if self._clicks >= 4:
                context.workspace.status_text_set(None)
                _set_status(
                    scene,
                    "4 вершины заданы. Теперь Pick 4 Layout Points.")
                return {'FINISHED'}

            context.workspace.status_text_set(
                f"Кликните вершину {self._clicks + 1} из 4")

        return {'RUNNING_MODAL'}


class PBA_LM_OT_cancel_island(bpy.types.Operator):
    bl_idname = "pba_lm.cancel_island"
    bl_label = "Cancel Island"
    bl_options = {'REGISTER'}

    def execute(self, context):
        pba = context.scene.pba_lm
        pba.pending_island_obj = ""
        pba.pending_island_uv_name = ""
        pba.pending_island_corners.clear()
        pba.pending_corners_px = (0.0,) * 8
        pba.pending_px_count = 0
        _set_status(context.scene, "Остров сброшен")
        return {'FINISHED'}


def _commit_island(scene):
    pba = scene.pba_lm
    if len(pba.pending_island_corners) != 4:
        return
    if pba.pending_px_count != 4:
        return

    # Собираем кандидатов
    candidates_per_corner = []
    for c in pba.pending_island_corners:
        n = c.n_uv_candidates
        if n == 0:
            candidates_per_corner.append([(c.uv[0], c.uv[1])])
        else:
            lst = []
            for k in range(n):
                lst.append((c.uv_candidates[k * 2],
                            c.uv_candidates[k * 2 + 1]))
            candidates_per_corner.append(lst)

    px = list(pba.pending_corners_px)
    layout_px = [(px[i * 2], px[i * 2 + 1]) for i in range(4)]

    combo, rmse, params = _fit_best_uv_combination(
        candidates_per_corner, layout_px, verbose=True)

    if combo is not None:
        for i, c in enumerate(pba.pending_island_corners):
            u, v = candidates_per_corner[i][combo[i]]
            c.uv = (u, v)
            c.fit_residual = rmse

    # Создание записи
    isl = pba.islands.add()
    pba.island_counter += 1
    isl.id = f"i_{pba.island_counter:03d}"
    isl.part = pba.pending_part

    for c in pba.pending_island_corners:
        nc = isl.corners.add()
        nc.obj_name = c.obj_name
        nc.vertex_index = c.vertex_index
        nc.vertex_uuid = c.vertex_uuid
        nc.uv = (c.uv[0], c.uv[1])
        nc.x_world = (c.x_world[0], c.x_world[1], c.x_world[2])
        nc.uv_candidates = tuple(c.uv_candidates)
        nc.n_uv_candidates = c.n_uv_candidates
        nc.fit_residual = c.fit_residual

    isl.corners_layout_px = tuple(pba.pending_corners_px)

    pba.pending_island_obj = ""
    pba.pending_island_uv_name = ""
    pba.pending_island_corners.clear()
    pba.pending_corners_px = (0.0,) * 8
    pba.pending_px_count = 0

    if rmse is not None:
        _set_status(scene,
                    f"Остров {isl.id}: UV-fit rmse={rmse:.5f}")
    else:
        _set_status(scene,
                    f"Остров {isl.id}: UV-fit не сошёлся")


class PBA_LM_OT_commit_island(bpy.types.Operator):
    bl_idname = "pba_lm.commit_island"
    bl_label = "Commit Island"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        if len(pba.pending_island_corners) != 4:
            self.report({'ERROR'},
                        f"Вершин острова: {len(pba.pending_island_corners)}, "
                        f"нужно 4")
            return {'CANCELLED'}
        if pba.pending_px_count != 4:
            self.report({'ERROR'},
                        f"Точек на выкройке: {pba.pending_px_count}, "
                        f"нужно 4")
            return {'CANCELLED'}

        _commit_island(scene)
        _rebuild_pending_mesh(scene)
        _rebuild_committed_mesh(scene)
        return {'FINISHED'}


class PBA_LM_OT_rotate_island(bpy.types.Operator):
    bl_idname = "pba_lm.rotate_island"
    bl_label = "Rotate Island Order"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()
    direction: bpy.props.IntProperty(default=1)

    def execute(self, context):
        pba = context.scene.pba_lm
        if not (0 <= self.index < len(pba.islands)):
            return {'CANCELLED'}
        isl = pba.islands[self.index]

        px = list(isl.corners_layout_px)
        pts = [[px[0], px[1]], [px[2], px[3]],
               [px[4], px[5]], [px[6], px[7]]]
        if self.direction > 0:
            pts = pts[1:] + pts[:1]
        else:
            pts = pts[-1:] + pts[:-1]
        flat = []
        for x, y in pts:
            flat.extend([x, y])
        isl.corners_layout_px = tuple(flat)

        _set_status(context.scene,
                    f"Остров {isl.id}: поворот 2D-кликов")
        return {'FINISHED'}


class PBA_LM_OT_flip_island(bpy.types.Operator):
    bl_idname = "pba_lm.flip_island"
    bl_label = "Flip Island Order"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        pba = context.scene.pba_lm
        if not (0 <= self.index < len(pba.islands)):
            return {'CANCELLED'}
        isl = pba.islands[self.index]

        px = list(isl.corners_layout_px)
        pts = [[px[0], px[1]], [px[2], px[3]],
               [px[4], px[5]], [px[6], px[7]]]
        pts = [pts[0], pts[3], pts[2], pts[1]]
        flat = []
        for x, y in pts:
            flat.extend([x, y])
        isl.corners_layout_px = tuple(flat)

        _set_status(context.scene,
                    f"Остров {isl.id}: отражение  2D-кликов")
        return {'FINISHED'}

class PBA_LM_OT_duplicate_quad(bpy.types.Operator):
    bl_idname = "pba_lm.duplicate_quad"
    bl_label = "Duplicate Quad"
    bl_description = "Создать дубликат: те же 3D-вершины, новые точки"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        if not (0 <= self.index < len(pba.quads)):
            return {'CANCELLED'}
        src = pba.quads[self.index]

        new_q = pba.quads.add()
        pba.quad_counter += 1
        new_q.id = f"q_{pba.quad_counter:03d}"
        new_q.part = src.part
        new_q.duplicate_of = src.id
        for c in src.corners_3d:
            nc = new_q.corners_3d.add()
            nc.obj_name = c.obj_name
            nc.vertex_index = c.vertex_index
            nc.x_world = tuple(c.x_world)

        # Восстанавливаем pending из нового квада (без точек выкройки)
        pba.pending_corners_3d.clear()
        for c in new_q.corners_3d:
            pc = pba.pending_corners_3d.add()
            pc.obj_name = c.obj_name
            pc.vertex_index = c.vertex_index
            pc.x_world = tuple(c.x_world)
        pba.pending_corners_px = (0.0,) * 8
        pba.pending_px_count = 0
        pba.pending_part = src.part

        _set_status(
            scene,
            f"Дубликат {new_q.id} (источник {src.id}). "
            f"Кликните 4 новые точки на выкройке.")
        return {'FINISHED'}


class PBA_LM_OT_duplicate_island(bpy.types.Operator):
    bl_idname = "pba_lm.duplicate_island"
    bl_label = "Duplicate Island"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        if not (0 <= self.index < len(pba.islands)):
            return {'CANCELLED'}
        src = pba.islands[self.index]

        new_i = pba.islands.add()
        pba.island_counter += 1
        new_i.id = f"i_{pba.island_counter:03d}"
        new_i.part = src.part
        new_i.duplicate_of = src.id
        for c in src.corners:
            nc = new_i.corners.add()
            nc.obj_name = c.obj_name
            nc.vertex_index = c.vertex_index
            nc.uv = tuple(c.uv)
            nc.x_world = tuple(c.x_world)

        pba.pending_island_corners.clear()
        for c in new_i.corners:
            pc = pba.pending_island_corners.add()
            pc.obj_name = c.obj_name
            pc.vertex_index = c.vertex_index
            pc.uv = tuple(c.uv)
            pc.x_world = tuple(c.x_world)
        pba.pending_island_obj = src.corners[0].obj_name if src.corners else ""
        pba.pending_island_uv_name = ""
        pba.pending_corners_px = (0.0,) * 8
        pba.pending_px_count = 0
        pba.pending_part = src.part

        _set_status(
            scene,
            f"Дубликат {new_i.id} (источник {src.id}). "
            f"Кликните 4 новые точки на выкройке.")
        return {'FINISHED'}


class PBA_LM_OT_remove_island(bpy.types.Operator):
    bl_idname = "pba_lm.remove_island"
    bl_label = "Remove Island"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        pba = context.scene.pba_lm
        if 0 <= self.index < len(pba.islands):
            pba.islands.remove(self.index)
            _rebuild_committed_mesh(context.scene)
        return {'FINISHED'}


class PBA_LM_OT_clear_islands(bpy.types.Operator):
    bl_idname = "pba_lm.clear_islands"
    bl_label = "Clear All Islands"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        pba = context.scene.pba_lm
        pba.islands.clear()
        pba.island_counter = 0
        _rebuild_committed_mesh(context.scene)
        return {'FINISHED'}


class PBA_LM_OT_set_active_island(bpy.types.Operator):
    bl_idname = "pba_lm.set_active_island"
    bl_label = "Set Active Island"
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        pba = context.scene.pba_lm
        if 0 <= self.index < len(pba.islands):
            pba.active_island_index = self.index
            # Сбрасываем активный квад, чтобы не путать визуализацию
            pba.active_quad_index = -1
            return bpy.ops.pba_lm.refresh_highlight()
        return {'CANCELLED'}



def _sort_key_from_px(corners_px):
    """Ключ (min_y, min_x) — верхняя-левая точка по правилу C."""
    px = list(corners_px)
    pts = [(px[0], px[1]), (px[2], px[3]),
           (px[4], px[5]), (px[6], px[7])]
    min_pt = min(pts, key=lambda p: (p[1], p[0]))
    return min_pt[1], min_pt[0]


class PBA_LM_OT_sort_quads(bpy.types.Operator):
    bl_idname = "pba_lm.sort_quads"
    bl_label = "Sort Quads"
    bl_description = ("Сортировка: сначала по объекту первого угла, "
                      "затем сверху вниз, слева направо")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        n = len(pba.quads)
        if n < 2:
            _set_status(scene, f"Квадов {n}, сортировка не нужна")
            return {'FINISHED'}

        data = []
        for q in pba.quads:
            corners_3d = []
            for c in q.corners_3d:
                corners_3d.append({
                    "obj_name": c.obj_name,
                    "vertex_index": c.vertex_index,
                    "vertex_uuid": c.vertex_uuid,
                    "x_world": (c.x_world[0], c.x_world[1], c.x_world[2]),
                })
            obj_name = corners_3d[0]["obj_name"] if corners_3d else ""
            py, px_ = _sort_key_from_px(q.corners_px)
            data.append({
                "id": q.id,
                "part": q.part,
                "duplicate_of": q.duplicate_of,
                "corners_3d": corners_3d,
                "corners_px": tuple(q.corners_px),
                "sort_key": (obj_name, py, px_),
            })

        data.sort(key=lambda d: d["sort_key"])

        pba.quads.clear()
        for d in data:
            q = pba.quads.add()
            q.id = d["id"]
            q.part = d["part"]
            q.duplicate_of = d["duplicate_of"]
            for c3d in d["corners_3d"]:
                c = q.corners_3d.add()
                c.obj_name = c3d["obj_name"]
                c.vertex_index = c3d["vertex_index"]
                c.vertex_uuid = c3d["vertex_uuid"]
                c.x_world = c3d["x_world"]
            q.corners_px = d["corners_px"]

        pba.active_quad_index = -1
        _set_status(scene, f"Квады отсортированы: {n}")
        return {'FINISHED'}


class PBA_LM_OT_sort_islands(bpy.types.Operator):
    bl_idname = "pba_lm.sort_islands"
    bl_label = "Sort Islands"
    bl_description = ("Сортировка: сначала по объекту первого угла, "
                      "затем сверху вниз, слева направо")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_lm
        n = len(pba.islands)
        if n < 2:
            _set_status(scene, f"Островов {n}, сортировка не нужна")
            return {'FINISHED'}

        data = []
        for isl in pba.islands:
            corners = []
            for c in isl.corners:
                corners.append({
                    "obj_name": c.obj_name,
                    "vertex_index": c.vertex_index,
                    "vertex_uuid": c.vertex_uuid,
                    "uv": (c.uv[0], c.uv[1]),
                    "x_world": (c.x_world[0], c.x_world[1], c.x_world[2]),
                    "uv_candidates": tuple(c.uv_candidates),
                    "n_uv_candidates": c.n_uv_candidates,
                    "fit_residual": c.fit_residual,
                })
            obj_name = corners[0]["obj_name"] if corners else ""
            py, px_ = _sort_key_from_px(isl.corners_layout_px)
            data.append({
                "id": isl.id,
                "part": isl.part,
                "duplicate_of": isl.duplicate_of,
                "corners": corners,
                "corners_layout_px": tuple(isl.corners_layout_px),
                "sort_key": (obj_name, py, px_),
            })

        data.sort(key=lambda d: d["sort_key"])

        pba.islands.clear()
        for d in data:
            isl = pba.islands.add()
            isl.id = d["id"]
            isl.part = d["part"]
            isl.duplicate_of = d["duplicate_of"]
            for cdata in d["corners"]:
                c = isl.corners.add()
                c.obj_name = cdata["obj_name"]
                c.vertex_index = cdata["vertex_index"]
                c.vertex_uuid = cdata["vertex_uuid"]
                c.uv = cdata["uv"]
                c.x_world = cdata["x_world"]
                c.uv_candidates = cdata["uv_candidates"]
                c.n_uv_candidates = cdata["n_uv_candidates"]
                c.fit_residual = cdata["fit_residual"]
            isl.corners_layout_px = d["corners_layout_px"]

        pba.active_island_index = -1
        _set_status(scene, f"Острова отсортированы: {n}")
        return {'FINISHED'}


classes = (
    PBA_LM_OT_load_layout,
    PBA_LM_OT_capture_3d,
    PBA_LM_OT_pick_image_points,
    PBA_LM_OT_cancel_quad,
    PBA_LM_OT_remove_quad,
    PBA_LM_OT_clear_quads,
    PBA_LM_OT_export_json,
    PBA_LM_OT_frame_layout,
    PBA_LM_OT_undo_last_click,
    PBA_LM_OT_commit_quad,
    PBA_LM_OT_validate_quads,
    PBA_LM_OT_rotate_quad,
    PBA_LM_OT_flip_quad,
    PBA_LM_OT_import_json,
    PBA_LM_OT_set_active_quad,
    PBA_LM_OT_refresh_highlight,
    PBA_LM_OT_start_island,
    PBA_LM_OT_pick_island_vertices,
    PBA_LM_OT_cancel_island,
    PBA_LM_OT_commit_island,
    PBA_LM_OT_duplicate_quad,
    PBA_LM_OT_duplicate_island,
    PBA_LM_OT_remove_island,
    PBA_LM_OT_clear_islands,
    PBA_LM_OT_rotate_island,
    PBA_LM_OT_flip_island,
    PBA_LM_OT_set_active_island,
    PBA_LM_OT_sort_quads,
    PBA_LM_OT_sort_islands
)
