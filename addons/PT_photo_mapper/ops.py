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
import math
import os
import bmesh
import numpy as np
import json
import logging
from datetime import datetime
from mathutils import Vector, Matrix

import bpy as bpy
from bpy_extras.object_utils import world_to_camera_view
from bpy_extras.view3d_utils import region_2d_to_vector_3d

from .exif import camera_params, read_exif, to_float, detect_native_resolution

PBA_VERTEX_ID_ATTR = "pba_vertex_id"


def get_uuid_attribute(mesh, create=False):
    attr = mesh.attributes.get(PBA_VERTEX_ID_ATTR)
    if attr is None and create:
        attr = mesh.attributes.new(
            name=PBA_VERTEX_ID_ATTR, type='INT', domain='POINT')
    return attr


def _get_uuid_layer_from_bmesh(obj):
    import bmesh
    bm = bmesh.from_edit_mesh(obj.data)
    return bm, bm.verts.layers.int.get(PBA_VERTEX_ID_ATTR)


def get_vertex_uuid(obj, vertex_index):
    # Edit Mode: читаем через bmesh
    if (bpy.context.mode == 'EDIT_MESH'
            and bpy.context.active_object == obj):
        bm, layer = _get_uuid_layer_from_bmesh(obj)
        if layer is None:
            return 0
        bm.verts.ensure_lookup_table()
        if vertex_index < 0 or vertex_index >= len(bm.verts):
            return 0
        return int(bm.verts[vertex_index][layer])

    # Object Mode: обычный путь
    attr = get_uuid_attribute(obj.data, create=False)
    if attr is None or len(attr.data) == 0:
        return 0
    n = len(obj.data.vertices)
    if vertex_index < 0 or vertex_index >= n:
        return 0
    try:
        return int(attr.data[vertex_index].value)
    except Exception:
        return 0


def find_vertex_by_uuid(obj, uuid_val, hint_world=None):
    if uuid_val <= 0:
        return -1, 0

    # Собираем UUID всех вершин
    if (bpy.context.mode == 'EDIT_MESH'
            and bpy.context.active_object == obj):
        bm, layer = _get_uuid_layer_from_bmesh(obj)
        if layer is None:
            return -1, 0
        bm.verts.ensure_lookup_table()
        candidates = [i for i, v in enumerate(bm.verts)
                      if int(v[layer]) == uuid_val]
    else:
        attr = get_uuid_attribute(obj.data, create=False)
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


DEFAULT_LENS_MM = 50.0
DEFAULT_SENSOR_MM = 36.0

CAMERA_DISTANCE_M = 20.0
CAMERA_NAME = "PBA_VM_Camera"

# Относительный допуск при сравнении расстояний. 0.1% от дистанции
# до вершины. Для камеры в 5 м это ~5 мм.
VISIBILITY_EPS_REL = 1e-3

_POSITIONS = {
    'FRONT': (( CAMERA_DISTANCE_M * 2, 0.0, 0.0),
              (math.pi / 2, 0.0,  math.pi / 2)),
    'BACK':  ((-CAMERA_DISTANCE_M * 2, 0.0, 0.0),
              (math.pi / 2, 0.0, -math.pi / 2)),
    'LEFT': ((0.0,  CAMERA_DISTANCE_M, 0.0),
              (math.pi / 2, 0.0,  math.pi)),
    'RIGHT':  ((0.0, -CAMERA_DISTANCE_M, 0.0),
              (math.pi / 2, 0.0, 0.0)),
    'TOP':   ((0.0, 0.0,  CAMERA_DISTANCE_M),
              (0.0, 0.0, 0.0)),
}

_POSITION_LABEL = {
    'FRONT': "спереди (+X)",
    'BACK':  "сзади (-X)",
    'RIGHT': "справа (-Y)",
    'LEFT':  "слева (+Y)",
    'TOP':   "сверху (+Z)",
}

_logger = None

def get_logger():
    global _logger
    if _logger is None:
        _logger = logging.getLogger("pba_vm")
        _logger.setLevel(logging.DEBUG)
        if not _logger.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("[PBA VM] %(message)s"))
            _logger.addHandler(h)
    return _logger

def _build_json_data(scene, path, W, H, cam_obj):
    pba = scene.pba_vm

    try:
        exif = read_exif(path)
    except Exception:
        exif = {}
    focal_exif = to_float(exif.get("FocalLength"))
    focal35_exif = to_float(exif.get("FocalLengthIn35mmFilm"))

    m = cam_obj.matrix_world
    matrix_flat = [float(m[i][j]) for i in range(4) for j in range(4)]

    corrs_data = []
    for i, c in enumerate(pba.correspondences):
        item = {
            "index": i,
            "obj": c.obj_name,
            "vertex": int(c.vertex_index),
            "x3d": [float(c.x3d[0]), float(c.x3d[1]), float(c.x3d[2])],
            "x2d": [float(c.x2d[0]), float(c.x2d[1])],
            "enabled": bool(c.enabled),
            "last_error_px": float(c.last_error),
        }
        if c.vertex_uuid > 0:
            item["vertex_uuid"] = int(c.vertex_uuid)
        corrs_data.append(item)

    active = [c for c in pba.correspondences if c.enabled]
    errors = [c.last_error for c in active if c.last_error >= 0.0]
    if errors:
        errors_sorted = sorted(errors)
        median_err = errors_sorted[len(errors_sorted) // 2]
        max_err = max(errors)
        rmse = float(np.sqrt(sum(e * e for e in errors) / len(errors)))
    else:
        median_err = 0.0
        max_err = 0.0
        rmse = 0.0

    return {
        "format_version": "1.1",
        "generator": "PBA Vertex Mapper 0.4.0",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "photo": {
            "path": path,
            "filename": os.path.basename(path),
            "width_px": int(W),
            "height_px": int(H),
            "native_width_px": int(pba.native_width_px or W),
            "native_height_px": int(pba.native_height_px or H),
            "native_source": pba.native_source or "same",
            "crop_offset_x": int(pba.crop_offset_x),
            "crop_offset_y": int(pba.crop_offset_y),
        },
        "camera": {
            "name": cam_obj.name,
            "lens_mm": float(pba.physical_lens_mm),
            "render_lens_mm": float(cam_obj.data.lens),
            "sensor_width_mm": float(cam_obj.data.sensor_width),
            "crop_factor": float(pba.crop_factor),
            "exif_focal_length_mm":
                float(focal_exif) if focal_exif else 0.0,
            "exif_focal_length_35mm":
                float(focal35_exif) if focal35_exif else 0.0,
            "matrix_world": matrix_flat,
        },
        "solve": {
            "f_px": float(pba.last_f_px),
            "k1": float(pba.last_k1),
            "k2": float(pba.last_k2),
            "use_distortion_k1": bool(pba.use_distortion_k1),
            "use_distortion_k2": bool(pba.use_distortion_k2),
            "huber_delta": float(pba.huber_delta),
            "rmse_px": rmse,
            "median_err_px": float(median_err),
            "max_err_px": float(max_err),
            "n_active": len(active),
            "n_total": len(pba.correspondences),
        },
        "correspondences": corrs_data,
    }


def _apply_json_data(scene, data, path, W, H, cam_obj):
    logger = get_logger()
    pba = scene.pba_vm

    photo_info = data.get("photo", {})
    json_W = int(photo_info.get("width_px", 0))
    json_H = int(photo_info.get("height_px", 0))
    if (json_W, json_H) != (W, H):
        msg = (f"Размер фото в JSON {json_W}×{json_H}, "
               f"загружено {W}×{H}")
        logger.warning(msg)
        _set_status(scene, msg + " — пропуск восстановления")
        return False

    cam_info = data.get("camera", {})
    physical_lens = float(cam_info.get("lens_mm", 0.0))
    render_lens = float(cam_info.get("render_lens_mm", 0.0))
    sensor = float(cam_info.get("sensor_width_mm", 0.0))

    # Если render_lens сохранён — используем его, иначе пересчитываем
    nw = int(photo_info.get("native_width_px", W))
    nh = int(photo_info.get("native_height_px", H))
    max_native = max(nw, nh)
    max_loaded = max(W, H)
    pba.crop_offset_x = int(photo_info.get("crop_offset_x", 0))
    pba.crop_offset_y = int(photo_info.get("crop_offset_y", 0))

    if render_lens > 0:
        cam_obj.data.lens = render_lens
    elif physical_lens > 0:
        cam_obj.data.lens = physical_lens * max_native / max_loaded

    if physical_lens > 0:
        pba.physical_lens_mm = physical_lens
    if sensor > 0:
        cam_obj.data.sensor_width = sensor

    m_flat = cam_info.get("matrix_world", [])
    if len(m_flat) == 16:
        rows = [m_flat[0:4], m_flat[4:8],
                m_flat[8:12], m_flat[12:16]]
        try:
            cam_obj.matrix_world = Matrix(rows)
        except Exception as e:
            logger.warning(f"Не удалось восстановить matrix_world: {e}")

    solve = data.get("solve", {})
    pba.last_f_px = float(solve.get("f_px", 0.0))
    pba.last_k1 = float(solve.get("k1", 0.0))
    pba.last_k2 = float(solve.get("k2", 0.0))
    pba.use_distortion = bool(solve.get("use_distortion", False))
    pba.use_k2 = bool(solve.get("use_k2", False))
    pba.huber_delta = float(solve.get("huber_delta", 0.0))

    pba.correspondences.clear()
    restored = 0
    skipped = 0
    skipped_uuid = 0
    dupes_resolved = 0
    for cd in data.get("correspondences", []):
        obj_name = cd.get("obj", "")
        vertex_uuid = int(cd.get("vertex_uuid", 0))
        vertex_idx = int(cd.get("vertex", -1))

        obj = scene.objects.get(obj_name)
        if obj is None or obj.type != 'MESH':
            print(f"[PBA VM] пропуск: объект '{obj_name}' не найден")
            skipped += 1
            continue

        # Приоритет — UUID
        resolved_idx = -1
        if vertex_uuid > 0:
            hint = None
            x3d_raw = cd.get("x3d")
            if x3d_raw and len(x3d_raw) == 3:
                hint = (float(x3d_raw[0]),
                        float(x3d_raw[1]),
                        float(x3d_raw[2]))
            resolved_idx, n_cand = find_vertex_by_uuid(
                obj, vertex_uuid, hint_world=hint)
            if resolved_idx < 0:
                print(f"[PBA VM] пропуск: {obj_name} "
                      f"uuid={vertex_uuid} не найден")
                skipped_uuid += 1
                continue
            if n_cand > 1:
                dupes_resolved += 1
                print(f"[PBA VM] {obj_name} uuid={vertex_uuid}: "
                      f"{n_cand} вершин, выбрана ближайшая к x3d")
        else:
            if 0 <= vertex_idx < len(obj.data.vertices):
                resolved_idx = vertex_idx
            else:
                print(f"[PBA VM] пропуск: {obj_name} "
                      f"v{vertex_idx} вне диапазона "
                      f"({len(obj.data.vertices)} вершин)")
                skipped += 1
                continue

        c = pba.correspondences.add()
        c.obj_name = obj_name
        c.vertex_index = resolved_idx
        c.vertex_uuid = vertex_uuid
        x3d = cd.get("x3d", [0.0, 0.0, 0.0])
        c.x3d[0] = float(x3d[0])
        c.x3d[1] = float(x3d[1])
        c.x3d[2] = float(x3d[2])
        x2d = cd.get("x2d", [0.0, 0.0])
        c.x2d[0] = float(x2d[0])
        c.x2d[1] = float(x2d[1])
        c.enabled = bool(cd.get("enabled", True))
        c.last_error = float(cd.get("last_error_px", -1.0))
        restored += 1

    rmse = float(solve.get("rmse_px", 0.0))
    parts = [f"Восстановлено: {restored} пар"]
    if skipped:
        parts.append(f"пропущено {skipped}")
    if skipped_uuid:
        parts.append(f"потеряно по UUID {skipped_uuid}")
    if dupes_resolved:
        parts.append(f"разрешено дубликатов {dupes_resolved}")
    if rmse > 0:
        parts.append(f"RMSE {rmse:.2f}px")
    msg = ", ".join(parts)
    _set_status(scene, msg)
    logger.info(msg)
    return True


def _set_status(scene, msg):
    scene.pba_vm.status_msg = msg
    print(f"[PBA VM] {msg}")


def _delete_old_camera(scene):
    old = scene.camera
    if old is not None and old.type == 'CAMERA':
        data = old.data
        bpy.data.objects.remove(old, do_unlink=True)
        if data.users == 0:
            bpy.data.cameras.remove(data)
    leftover = bpy.data.objects.get(CAMERA_NAME)
    if leftover is not None and leftover.type == 'CAMERA':
        data = leftover.data
        bpy.data.objects.remove(leftover, do_unlink=True)
        if data.users == 0:
            bpy.data.cameras.remove(data)

def _sync_x3d_from_mesh(scene):
    """Обновляет x3d по актуальному положению вершин. Работает
    по UUID (если есть) или по индексу. Пары с большим расхождением
    не перезаписываются, а помечаются."""
    import bmesh
    pba = scene.pba_vm
    updated = 0
    suspicious = 0

    edit_obj = None
    bm = None
    if bpy.context.mode == 'EDIT_MESH':
        edit_obj = bpy.context.active_object
        if edit_obj is not None and edit_obj.type == 'MESH':
            bm = bmesh.from_edit_mesh(edit_obj.data)
            bm.verts.ensure_lookup_table()

    DRIFT_THRESHOLD = 0.20

    for c in pba.correspondences:
        obj = scene.objects.get(c.obj_name)
        if obj is None or obj.type != 'MESH':
            continue

        if c.vertex_uuid > 0:
            hint = (c.x3d[0], c.x3d[1], c.x3d[2])
            idx, _ = find_vertex_by_uuid(obj, c.vertex_uuid,
                                          hint_world=hint)
            if idx < 0:
                continue
            if idx != c.vertex_index:
                c.vertex_index = idx
        else:
            idx = c.vertex_index

        if idx < 0 or idx >= len(obj.data.vertices):
            continue

        if (edit_obj is not None and obj == edit_obj
                and bm is not None and idx < len(bm.verts)):
            co_local = bm.verts[idx].co
        else:
            co_local = obj.data.vertices[idx].co

        p_world = obj.matrix_world @ co_local
        old = Vector((c.x3d[0], c.x3d[1], c.x3d[2]))
        drift = (p_world - old).length

        if drift > DRIFT_THRESHOLD:
            suspicious += 1
            print(f"[PBA VM] ПОДОЗРИТЕЛЬНО: {c.obj_name} "
                  f"uuid={c.vertex_uuid} v{idx} — сдвиг {drift:.3f} м")
            continue

        if (abs(c.x3d[0] - p_world.x) > 1e-9
                or abs(c.x3d[1] - p_world.y) > 1e-9
                or abs(c.x3d[2] - p_world.z) > 1e-9):
            c.x3d[0] = p_world.x
            c.x3d[1] = p_world.y
            c.x3d[2] = p_world.z
            updated += 1

    if suspicious > 0:
        print(f"[PBA VM] ВНИМАНИЕ: {suspicious} пар с большим сдвигом.")
    return updated


class PBA_VM_OT_load_photo(bpy.types.Operator):
    bl_idname = "pba_vm.load_photo"
    bl_label = "Load Photo & Setup Camera"
    bl_description = "Загрузить фото, создать камеру по EXIF"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        path = bpy.path.abspath(scene.pba_vm.photo_path)

        if not path:
            _set_status(scene, "Не задан путь к фото")
            self.report({'ERROR'}, "Не задан путь к фото")
            return {'CANCELLED'}
        if not os.path.exists(path):
            _set_status(scene, f"Файл не найден: {path}")
            self.report({'ERROR'}, "Файл не найден")
            return {'CANCELLED'}

        try:
            img = bpy.data.images.load(path, check_existing=True)
        except Exception as e:
            _set_status(scene, f"Ошибка загрузки: {e}")
            self.report({'ERROR'}, f"Ошибка загрузки: {e}")
            return {'CANCELLED'}

        if img.size[0] == 0 or img.size[1] == 0:
            _set_status(scene, "Изображение пустое")
            self.report({'ERROR'}, "Изображение пустое")
            return {'CANCELLED'}

        W_actual, H_actual = img.size
        pba = scene.pba_vm

        # --- 1. Нативное разрешение ---
        if (pba.use_manual_camera
                and pba.manual_native_width > 0
                and pba.manual_native_height > 0):
            nw = pba.manual_native_width
            nh = pba.manual_native_height
            src = "manual"
        elif pba.use_native_resolution:
            nw, nh, src = detect_native_resolution(
                path, W_actual, H_actual)
        else:
            nw, nh, src = W_actual, H_actual, "same"

        pba.native_width_px = nw
        pba.native_height_px = nh
        pba.native_source = src

        # --- 2. Фокусное и сенсор ---
        focal_exif, sensor_exif = camera_params(path)

        if pba.use_manual_camera and pba.manual_focal_length > 0:
            focal = pba.manual_focal_length
            focal_src = "manual"
        elif focal_exif:
            focal = focal_exif
            focal_src = "exif"
        else:
            focal = DEFAULT_LENS_MM
            focal_src = "default"

        if pba.use_manual_camera and pba.manual_sensor_width > 0:
            sensor = pba.manual_sensor_width
            sensor_src = "manual"
        elif sensor_exif:
            sensor = sensor_exif
            sensor_src = "model"
        else:
            sensor = DEFAULT_SENSOR_MM
            sensor_src = "default"

        # --- 3. Лог и предупреждения ---
        if src != "same":
            print(f"[PBA VM] Кроп: реальное {W_actual}×{H_actual}, "
                  f"нативное {nw}×{nh} (источник: {src})")
        else:
            print(f"[PBA VM] Размер {W_actual}×{H_actual} — нативный")

        print(f"[PBA VM] Камера: focal={focal:.1f}мм ({focal_src}), "
              f"sensor={sensor:.2f}мм ({sensor_src})")

        if focal_src == "default" or sensor_src == "default":
            print(f"[PBA VM] ВНИМАНИЕ: параметры камеры не определены "
                  f"из EXIF. Если фото кропнутое — включите "
                  f"'Use Manual Camera' и укажите правильные значения. "
                  f"Иначе f_px будет неверным и Solve развалится.")

        # Синхронизируем рендер с фото — иначе world_to_camera_view
        # работает в другом аспекте, и координаты уезжают.
        scene.render.resolution_x = img.size[0]
        scene.render.resolution_y = img.size[1]
        scene.render.resolution_percentage = 100

        focal, sensor = camera_params(path)
        
        focal = focal if focal else DEFAULT_LENS_MM
        sensor = sensor if sensor else DEFAULT_SENSOR_MM

        pos_key = scene.pba_vm.camera_position
        if pos_key not in _POSITIONS:
            pos_key = 'FRONT'
        location, rotation = _POSITIONS[pos_key]
        
        nw = scene.pba_vm.native_width_px or W_actual
        nh = scene.pba_vm.native_height_px or H_actual
        max_native = max(nw, nh)
        max_loaded = max(W_actual, H_actual)

        physical_lens = focal if focal else DEFAULT_LENS_MM
        scene.pba_vm.physical_lens_mm = physical_lens

        # Рендерное фокусное: такое, чтобы Blender с текущим
        # разрешением (loaded) давал то же f_px, что и физическая
        # камера на нативном сенсоре.
        render_lens = physical_lens * max_native / max_loaded

        _delete_old_camera(scene)

        cam_data = bpy.data.cameras.new(name=CAMERA_NAME)
        cam_data.lens = render_lens
        cam_data.sensor_width = sensor

        cam_obj = bpy.data.objects.new(name=CAMERA_NAME, object_data=cam_data)
        scene.collection.objects.link(cam_obj)

        cam_obj.location = location
        cam_obj.rotation_euler = rotation
        # ВАЖНО: shift_x и shift_y имеют разные знаки относительно
        # crop_offset_*, потому что:
        #   - crop_offset_* в пиксельной системе (Y растёт вниз);
        #   - shift_y в системе co.y (Y растёт вверх);
        #   - shift_x в экранной системе (X совпадает с пиксельной).
        # Двойная инверсия по Y даёт один знак, одинарная по X — другой.
        if pba.crop_offset_x != 0 or pba.crop_offset_y != 0:
            cam_obj.data.sensor_fit = 'HORIZONTAL'
            cam_obj.data.shift_x = -pba.crop_offset_x / W_actual
            cam_obj.data.shift_y = +pba.crop_offset_y / W_actual
        else:
            cam_obj.data.shift_x = 0.0
            cam_obj.data.shift_y = 0.0

        try:
            bg = cam_data.background_images.new()
            bg.source = 'IMAGE'
            bg.image = img
            bg.alpha = 0.5
            bg.display_depth = 'FRONT'
            bg.show_background_image = True
            cam_data.show_background_images = True
        except Exception as e:
            _set_status(scene, f"Фото загружено, но background не создан: {e}")
            self.report({'WARNING'}, f"Background: {e}")

        scene.camera = cam_obj
        for o in context.view_layer.objects:
            o.select_set(False)
        cam_obj.select_set(True)
        context.view_layer.objects.active = cam_obj
        
        # Восстановление из JSON (если есть и разрешено)
        if scene.pba_vm.restore_from_json:
            json_path = os.path.splitext(path)[0] + ".vertices.json"
            if os.path.exists(json_path):
                try:
                    with open(json_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    _apply_json_data(scene, data, path,
                                     img.size[0], img.size[1], cam_obj)
                except Exception as e:
                    self.report({'WARNING'}, f"Ошибка чтения JSON {json_path}: {e}")
                    _set_status(scene, f"Ошибка JSON: {e}")

        crop_info = ""
        if scene.pba_vm.native_source != "same":
            crop_info = (f" [кроп из "
                          f"{scene.pba_vm.native_width_px}×"
                          f"{scene.pba_vm.native_height_px}]")

        _set_status(
            scene,
            f"Фото: {W_actual}×{H_actual}{crop_info}, "
            f"focal={physical_lens:.1f}мм "
            f"(render={render_lens:.1f}мм), "
            f"sensor={sensor:.2f}мм, "
            f"старт: {_POSITION_LABEL[pos_key]}. "
            f"Numpad 0 — вид из камеры.")
        self.report({'INFO'}, "Камера настроена")
        return {'FINISHED'}


class PBA_VM_OT_save_vertices(bpy.types.Operator):
    bl_idname = "pba_vm.save_vertices"
    bl_label = "Save Vertex Positions"
    bl_description = "Сохранить координаты вершин в CSV в пикселях фото"
    bl_options = {'REGISTER'}

    check_visibility: bpy.props.BoolProperty(
        name="Проверять видимость",
        description="Raycast: исключать вершины, закрытые другими гранями",
        default=True)

    def execute(self, context):
        scene = context.scene

        cam_obj = scene.camera
        if cam_obj is None or cam_obj.type != 'CAMERA':
            _set_status(scene, "Нет активной камеры")
            self.report({'ERROR'}, "Нет активной камеры")
            return {'CANCELLED'}

        path = bpy.path.abspath(scene.pba_vm.photo_path)
        if not path or not os.path.exists(path):
            _set_status(scene, "Не задан путь к фото")
            self.report({'ERROR'}, "Не задан путь к фото")
            return {'CANCELLED'}

        base = os.path.splitext(os.path.basename(path))[0]
        out_dir = os.path.dirname(path)
        out_path = os.path.join(out_dir, f"{base}.vertices.csv")

        W = H = 0
        if cam_obj.data.background_images:
            bg = cam_obj.data.background_images[0]
            if bg.image is not None:
                W, H = bg.image.size
        if W == 0 or H == 0:
            try:
                img = bpy.data.images.load(path, check_existing=True)
                W, H = img.size
            except Exception as e:
                _set_status(scene, f"Не удалось получить размер фото: {e}")
                self.report({'ERROR'}, "Размер фото недоступен")
                return {'CANCELLED'}

        if W == 0 or H == 0:
            _set_status(scene, "Размер фото нулевой")
            self.report({'ERROR'}, "Размер фото нулевой")
            return {'CANCELLED'}

        # Обновляем сцену перед raycast
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        cam_loc = cam_obj.matrix_world.translation.copy()

        total = 0
        in_frame_count = 0
        visible_count = 0
        
        n = _sync_x3d_from_mesh(scene)
        if n > 0:
            print(f"[PBA VM] синхронизировано {n} 3D-координат")

        # JSON (новый формат)
        json_path = os.path.join(out_dir, f"{base}.vertices.json")
        try:
            json_data = _build_json_data(scene, path, W, H, cam_obj)
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _set_status(scene, f"Ошибка записи JSON: {e}")
            self.report({'ERROR'}, f"JSON: {e}")
            return {'CANCELLED'}
        _set_status(
            scene,
            f"Сохранено: {json_path} (вершин: {total})")
        self.report({'INFO'}, f"JSON: {base}")
        return {'FINISHED'}


classes = (
    PBA_VM_OT_load_photo,
    PBA_VM_OT_save_vertices,
)


def _get_photo_size(scene):
    cam = scene.camera
    if cam is None:
        return (0, 0)
    if cam.data.background_images:
        bg = cam.data.background_images[0]
        if bg.image is not None:
            return tuple(bg.image.size)
    return (0, 0)


def _commit_pair(scene):
    pba = scene.pba_vm
    if not (pba.pending_3d_set and pba.pending_2d_set):
        return

    # Дубликат 3D-вершины
    for existing in pba.correspondences:
        same_uuid = (pba.pending_3d_uuid > 0
                     and existing.vertex_uuid == pba.pending_3d_uuid)
        same_idx = (pba.pending_3d_uuid == 0
                    and existing.obj_name == pba.pending_3d_obj
                    and existing.vertex_index == pba.pending_3d_vertex)
        if existing.obj_name == pba.pending_3d_obj and (same_uuid or same_idx):
            pba.pending_3d_set = False
            pba.pending_2d_set = False
            pba.pending_3d_obj = ""
            pba.pending_3d_vertex = -1
            pba.pending_3d_uuid = 0
            print(f"[PBA VM] пропущен дубликат 3D: "
                  f"{existing.obj_name} v{existing.vertex_index}")
            return

    # Близкий 2D-клик
    px = pba.pending_x2d[0]
    py = pba.pending_x2d[1]
    for existing in pba.correspondences:
        dx = existing.x2d[0] - px
        dy = existing.x2d[1] - py
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < 5.0:
            print(f"[PBA VM] ВНИМАНИЕ: 2D-клик ({px:.1f},{py:.1f}) "
                  f"в {dist:.1f}px от пары "
                  f"{existing.obj_name} v{existing.vertex_index}")

    c = pba.correspondences.add()
    c.obj_name = pba.pending_3d_obj
    c.vertex_index = pba.pending_3d_vertex
    c.vertex_uuid = pba.pending_3d_uuid
    c.x3d = pba.pending_x3d
    c.x2d = pba.pending_x2d

    pba.pending_3d_set = False
    pba.pending_2d_set = False
    pba.pending_3d_obj = ""
    pba.pending_3d_vertex = -1
    pba.pending_3d_uuid = 0


class PBA_VM_OT_pick_3d(bpy.types.Operator):
    bl_idname = "pba_vm.pick_3d"
    bl_label = "Pick 3D Vertex"
    bl_description = "Запомнить выделенную вершину (Edit Mode, ровно одна)"

    def execute(self, context):
        scene = context.scene
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
        if len(selected) != 1:
            self.report({'ERROR'}, f"Выделено {len(selected)} вершин, нужна 1")
            return {'CANCELLED'}

        v = selected[0]
        p_world = obj.matrix_world @ v.co
        pba = scene.pba_vm
        uuid_val = get_vertex_uuid(obj, v.index)
        pba.pending_x3d = (p_world.x, p_world.y, p_world.z)
        pba.pending_3d_obj = obj.name
        pba.pending_3d_vertex = v.index
        pba.pending_3d_uuid = uuid_val
        pba.pending_3d_set = True

        if uuid_val == 0:
            print(f"[PBA VM] ВНИМАНИЕ: у вершины {obj.name} "
                  f"v{v.index} нет UUID. Запустите Assign UUIDs "
                  f"в PBA UV Exporter.")

        _commit_pair(scene)
        _set_status(scene, f"3D: {obj.name} v{v.index} = "
                           f"({p_world.x:.2f}, {p_world.y:.2f}, {p_world.z:.2f})")
        return {'FINISHED'}


class PBA_VM_OT_pick_2d(bpy.types.Operator):
    bl_idname = "pba_vm.pick_2d"
    bl_label = "Pick 2D on Photo"
    bl_description = "Кликните точку на фотографии (Numpad 0)"

    def invoke(self, context, event):
        scene = context.scene
        cam = scene.camera
        if cam is None or cam.type != 'CAMERA':
            self.report({'ERROR'}, "Нет активной камеры")
            return {'CANCELLED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Кликните точку на фото. ESC — отмена.")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            context.workspace.status_text_set(None)
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            scene = context.scene
            cam = scene.camera
            region = context.region
            region_3d = context.space_data.region_3d
            coord = (event.mouse_region_x, event.mouse_region_y)

            view_vec = region_2d_to_vector_3d(region, region_3d, coord)
            cam_loc = cam.matrix_world.translation
            p_world = cam_loc + view_vec

            co = world_to_camera_view(scene, cam, p_world)
            W, H = _get_photo_size(scene)
            if W == 0 or H == 0:
                self.report({'ERROR'}, "Размер фото неизвестен")
                context.workspace.status_text_set(None)
                return {'CANCELLED'}

            px = co.x * W
            py = (1.0 - co.y) * H

            pba = scene.pba_vm
            pba.pending_x2d = (px, py)
            pba.pending_2d_set = True
            _commit_pair(scene)

            _set_status(scene, f"2D: ({px:.1f}, {py:.1f})")
            context.workspace.status_text_set(None)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


class PBA_VM_OT_remove_pair(bpy.types.Operator):
    bl_idname = "pba_vm.remove_pair"
    bl_label = "Remove Pair"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        pba = context.scene.pba_vm
        if 0 <= self.index < len(pba.correspondences):
            pba.correspondences.remove(self.index)
        return {'FINISHED'}


class PBA_VM_OT_clear_pairs(bpy.types.Operator):
    bl_idname = "pba_vm.clear_pairs"
    bl_label = "Clear All Pairs"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        pba = context.scene.pba_vm
        pba.correspondences.clear()
        pba.pending_3d_set = False
        pba.pending_2d_set = False
        _set_status(context.scene, "Список соответствий очищен")
        return {'FINISHED'}


class PBA_VM_OT_solve_camera(bpy.types.Operator):
    bl_idname = "pba_vm.solve_camera"
    bl_label = "Solve Camera"
    bl_description = "Вычислить положение камеры по точкам соответствия"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from mathutils import Matrix

        scene = context.scene
        cam = scene.camera
        if cam is None or cam.type != 'CAMERA':
            self.report({'ERROR'}, "Нет активной камеры")
            return {'CANCELLED'}

        corrs = [c for c in scene.pba_vm.correspondences if c.enabled]
        if len(corrs) < 6:
            self.report({'WARNING'},
                        f"Активных пар {len(corrs)}, нужно ≥6")
            return {'CANCELLED'}
            
        pba = scene.pba_vm
        W, H = _get_photo_size(scene)
        if W == 0 or H == 0:
            self.report({'ERROR'}, "Размер фото неизвестен")
            return {'CANCELLED'}

        lens = cam.data.lens
        sensor = cam.data.sensor_width

        if cam.data.sensor_fit == 'VERTICAL':
            base_f_px = lens / sensor * H
        elif cam.data.sensor_fit == 'HORIZONTAL':
            base_f_px = lens / sensor * W
        else:
            base_f_px = lens / sensor * max(W, H)

        print(f"[PBA solve] lens={lens:.2f}мм (render), "
              f"sensor={sensor:.2f}мм, "
              f"loaded {W}×{H}, f_px={base_f_px:.1f}")
        print(f"[PBA solve] base_f_px = {base_f_px:.1f}")

        crop = float(pba.crop_factor)

        n = _sync_x3d_from_mesh(scene)
        if n > 0:
            print(f"[PBA VM] синхронизировано {n} 3D-координат")

        world_pts = [np.array(c.x3d) for c in corrs]
        off_x = pba.crop_offset_x
        off_y = pba.crop_offset_y
        if off_x != 0 or off_y != 0:
            print(f"[PBA solve] Применяю crop offset: "
                  f"({off_x}, {off_y}) px")
        pixel_pts = [np.array(c.x2d) - np.array([off_x, off_y]) for c in corrs]

        from mathutils import Matrix
        from .solver import pose_refine_multistart

        M_init = np.array(cam.matrix_world)

        print(f"[PBA solve] focal={lens:.2f}mm, sensor={sensor:.2f}mm, "
              f"точек={len(world_pts)}, "
              f"дисторсия={'K1+K2' if pba.use_distortion_k1 and pba.use_distortion_k2 else 'K1' if pba.use_distortion_k1 else 'K2' if pba.use_distortion_k2 else 'выкл'}, "
              f"crop={crop:.3f}")
        print(f"[PBA solve] base_f_px = {base_f_px:.1f}")

        best = None

        M_best, rmse, k1, k2 = pose_refine_multistart(
                world_pts, pixel_pts, W, H, base_f_px, M_init,
                use_distortion_k1=pba.use_distortion_k1,
                use_distortion_k2=pba.use_distortion_k2,
                huber_delta=pba.huber_delta,
                verbose=False)

        if best is None or rmse < best[1]:
                best = (M_best, rmse, k1, k2)

        if best is None:
            self.report({'ERROR'}, "LM не сошёлся")
            return {'CANCELLED'}

        M_best, rmse, k1, k2 = best
        print(f"[PBA solve] ПОЛУЧЕНО:"
              f"rmse={rmse:.3f}, k1={k1:+.4f}, k2={k2:+.4f}")

        cam.matrix_world = Matrix(M_best.tolist())
        # Синхронизировать shift камеры с текущим offset
        # См. комментарий в PBA_VM_OT_load_photo про разные знаки X/Y.
        if off_x != 0 or off_y != 0:
            cam.data.sensor_fit = 'HORIZONTAL'
            cam.data.shift_x = -off_x / W
            cam.data.shift_y = +off_y / W
        else:
            cam.data.shift_x = 0.0
            cam.data.shift_y = 0.0
        
        pba.last_k1 = k1
        pba.last_k2 = k2
        pba.last_f_px = base_f_px

        # Оценка ошибки через P-матрицу (согласовано с LM)
        # LM минимизирует |P(X) - (x2d - off)|, поэтому используем
        # ту же формулу.
        D = np.diag([1.0, -1.0, -1.0, 1.0])
        M_cv = D @ np.linalg.inv(np.array(M_best))
        K = np.array([
            [base_f_px, 0.0, W / 2.0],
            [0.0, base_f_px, H / 2.0],
            [0.0, 0.0, 1.0],
        ])
        P = K @ M_cv[:3, :]

        errors = []
        for c in corrs:
            X = np.array([c.x3d[0], c.x3d[1], c.x3d[2], 1.0])
            proj = P @ X
            if proj[2] <= 1e-9:
                err = 1e6
            else:
                u = proj[0] / proj[2]
                v = proj[1] / proj[2]
                u_adj = c.x2d[0] - off_x
                v_adj = c.x2d[1] - off_y
                err = ((u - u_adj) ** 2 + (v - v_adj) ** 2) ** 0.5
            c.last_error = err
            errors.append(err)

        mean_err = sum(errors) / len(errors)
        max_err = max(errors)

        print("[PBA per-pair] ошибки:")
        for idx, err in enumerate(errors):
            flag = "  <-- выброс" if err > 2.5 * mean_err else ""
            print(f"  pair {idx}: {err:7.2f} px{flag}")

        dist_str = f" k1={k1:+.4f}, k2={k2:+.4f}" if pba.use_distortion_k1 or pba.use_distortion_k2 else ""
        _set_status(
            scene,
            f"Ошибка: средняя {mean_err:.2f}px, макс {max_err:.2f}px. ")
        self.report({'INFO'},
                    f"Ошибка: средняя {mean_err:.2f}px, макс {max_err:.2f}px. RMSE {rmse:.2f}px")
        return {'FINISHED'}


classes += (
    PBA_VM_OT_pick_3d,
    PBA_VM_OT_pick_2d,
    PBA_VM_OT_remove_pair,
    PBA_VM_OT_clear_pairs,
    PBA_VM_OT_solve_camera,
)


class PBA_VM_OT_diagnose_pairs(bpy.types.Operator):
    bl_idname = "pba_vm.diagnose_pairs"
    bl_label = "Diagnose Pairs"
    bl_description = "Пересчитать ошибки по текущему положению камеры"

    def execute(self, context):
        scene = context.scene
        cam = scene.camera
        if cam is None or cam.type != 'CAMERA':
            self.report({'ERROR'}, "Нет активной камеры")
            return {'CANCELLED'}

        n = _sync_x3d_from_mesh(scene)
        if n > 0:
            print(f"[PBA VM] синхронизировано {n} 3D-координат")
        pba = scene.pba_vm
        
        corrs = [c for c in pba.correspondences if c.enabled]
        # Отключённые — сброс ошибки
        for c in pba.correspondences:
            if not c.enabled:
                c.last_error = -1.0

        if not corrs:
            self.report({'WARNING'}, "Нет активных пар")
            return {'CANCELLED'}

        W, H = _get_photo_size(scene)
        if W == 0 or H == 0:
            self.report({'ERROR'}, "Размер фото неизвестен")
            return {'CANCELLED'}

        f_px = pba.last_f_px if pba.last_f_px > 0 else 1.0
        k1 = pba.last_k1
        k2 = pba.last_k2
        off_x = pba.crop_offset_x
        off_y = pba.crop_offset_y

        # Оценка ошибки через P-матрицу (согласовано с LM)
        # LM минимизирует |P(X) - (x2d - off)|, поэтому используем
        # ту же формулу.
        D = np.diag([1.0, -1.0, -1.0, 1.0])
        M_cv = D @ np.linalg.inv(np.array(np.array(cam.matrix_world)))
        K = np.array([
            [f_px, 0.0, W / 2.0],
            [0.0, f_px, H / 2.0],
            [0.0, 0.0, 1.0],
        ])
        P = K @ M_cv[:3, :]

        errors = []
        for c in corrs:
            X = np.array([c.x3d[0], c.x3d[1], c.x3d[2], 1.0])
            proj = P @ X
            if proj[2] <= 1e-9:
                err = 1e6
            else:
                u = proj[0] / proj[2]
                v = proj[1] / proj[2]
                u_adj = c.x2d[0] - off_x
                v_adj = c.x2d[1] - off_y
                err = ((u - u_adj) ** 2 + (v - v_adj) ** 2) ** 0.5
            c.last_error = err
            errors.append(err)

        errs_sorted = sorted(errors)
        median = errs_sorted[len(errs_sorted) // 2]
        max_err = max(errors)
        outliers = sum(1 for e in errors if e > 2.5 * median)

        _set_status(
            scene,
            f"Диагностика ({len(corrs)} активных): медиана {median:.2f}px, "
            f"макс {max_err:.2f}px, выбросов: {outliers}")
        self.report({'INFO'}, f"Медиана {median:.2f}px")
        return {'FINISHED'}


class PBA_VM_OT_remove_outliers(bpy.types.Operator):
    bl_idname = "pba_vm.remove_outliers"
    bl_label = "Remove Outliers"
    bl_description = "Удалить пары с ошибкой > 2.5 × медианы"

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_vm

        if not pba.correspondences:
            self.report({'WARNING'}, "Нет пар")
            return {'CANCELLED'}

        bpy.ops.pba_vm.diagnose_pairs()

        errs = [c.last_error for c in pba.correspondences
                if c.last_error >= 0.0]
        if not errs:
            return {'CANCELLED'}

        errs_sorted = sorted(errs)
        median = errs_sorted[len(errs_sorted) // 2]
        threshold = max(median * 2.5, 5.0)

        to_remove = [i for i, c in enumerate(pba.correspondences)
                     if c.last_error > threshold]

        for i in reversed(to_remove):
            pba.correspondences.remove(i)

        _set_status(
            scene,
            f"Удалено: {len(to_remove)} (порог {threshold:.1f}px, "
            f"медиана {median:.1f}px). Осталось {len(pba.correspondences)} пар.")

        # Автоматически пересчитать решение, если хватает пар
        if len(pba.correspondences) >= 6:
            bpy.ops.pba_vm.solve_camera()
        else:
            _set_status(
                scene,
                f"Удалено: {len(to_remove)}. Осталось "
                f"{len(pba.correspondences)} пар — нужно ≥6 для решения.")

        return {'FINISHED'}


classes += (
    PBA_VM_OT_diagnose_pairs,
    PBA_VM_OT_remove_outliers,
)


class PBA_VM_OT_plot_residuals(bpy.types.Operator):
    bl_idname = "pba_vm.plot_residuals"
    bl_label = "Plot Residuals"
    bl_description = ("Показать на фото векторы ошибок: измеренная "
                      "точка → проекция из текущей камеры")

    def execute(self, context):
        scene = context.scene
        cam = scene.camera
        if cam is None or cam.type != 'CAMERA':
            self.report({'ERROR'}, "Нет активной камеры")
            return {'CANCELLED'}

        W, H = _get_photo_size(scene)
        if W == 0 or H == 0:
            self.report({'ERROR'}, "Размер фото неизвестен")
            return {'CANCELLED'}

        from PIL import Image, ImageDraw
        path = bpy.path.abspath(scene.pba_vm.photo_path)
        img = Image.open(path).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Начало координат фото, отмеченное крестиком — для контроля
        # совпадения системы координат.
        cx, cy = W / 2.0, H / 2.0
        draw.line([(cx - 20, cy), (cx + 20, cy)], fill=(0, 255, 0), width=2)
        draw.line([(cx, cy - 20), (cx, cy + 20)], fill=(0, 255, 0), width=2)

        for i, c in enumerate(scene.pba_vm.correspondences):
            co = world_to_camera_view(
                scene, cam, Vector((c.x3d[0], c.x3d[1], c.x3d[2])))
            px = co.x * W
            py = (1.0 - co.y) * H
            mx, my = c.x2d[0], c.x2d[1]

            # измеренная точка — синий крестик
            draw.line([(mx - 6, my), (mx + 6, my)], fill=(0, 128, 255), width=3)
            draw.line([(mx, my - 6), (mx, my + 6)], fill=(0, 128, 255), width=3)

            # вектор ошибки — красная стрелка от измеренной к проекции
            draw.line([(mx, my), (px, py)], fill=(255, 0, 0), width=2)
            # проекция — красный кружок
            draw.ellipse((px - 4, py - 4, px + 4, py + 4), fill=(255, 0, 0))

            draw.text((mx + 8, my + 8), f"{i}:{c.last_error:.0f}",
                      fill=(255, 255, 0))

        out = os.path.splitext(path)[0] + ".residuals.png"
        img.save(out)
        _set_status(scene, f"Остатки сохранены: {out}")
        self.report({'INFO'}, f"Residuals: {out}")
        return {'FINISHED'}


classes += (PBA_VM_OT_plot_residuals,)

def _get_selected_vertex_indices(context):
    """Возвращает (obj_name, [индексы выделенных вершин]) или (None, [])."""
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None, []
    if context.mode != 'EDIT_MESH':
        return None, []
    import bmesh
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    selected = [v.index for v in bm.verts if v.select]
    return obj.name, selected


class PBA_VM_OT_set_enabled(bpy.types.Operator):
    """Общий оператор для включения/отключения выделенных пар."""
    bl_idname = "pba_vm.set_enabled"
    bl_label = "Set Enabled for Selected"
    bl_options = {'REGISTER', 'UNDO'}

    enable: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_vm

        obj_name, selected = _get_selected_vertex_indices(context)
        if obj_name is None:
            self.report({'ERROR'},
                        "Требуется Edit Mode с активным mesh-объектом")
            return {'CANCELLED'}
        if not selected:
            self.report({'WARNING'}, "Не выделено ни одной вершины")
            return {'CANCELLED'}

        sel_set = set(selected)
        changed = 0
        for c in pba.correspondences:
            if c.obj_name == obj_name and c.vertex_index in sel_set:
                if c.enabled != self.enable:
                    c.enabled = self.enable
                    changed += 1

        action = "Включено" if self.enable else "Отключено"
        active = sum(1 for c in pba.correspondences if c.enabled)
        _set_status(
            scene,
            f"{action}: {changed} пар. Активных: {active} "
            f"из {len(pba.correspondences)}")

        # Автопересчёт, если достаточно активных пар
        if active >= 6 and scene.camera is not None:
            bpy.ops.pba_vm.solve_camera()
        elif active < 6:
            _set_status(
                scene,
                f"{action}: {changed}. Активных {active} — "
                f"мало для пересчёта.")

        return {'FINISHED'}


class PBA_VM_OT_disable_selected(bpy.types.Operator):
    bl_idname = "pba_vm.disable_selected"
    bl_label = "Disable Selected"
    bl_description = ("Отключить из решения все пары, чьи 3D-вершины "
                      "выделены в Edit Mode")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return bpy.ops.pba_vm.set_enabled(enable=False)


class PBA_VM_OT_enable_selected(bpy.types.Operator):
    bl_idname = "pba_vm.enable_selected"
    bl_label = "Enable Selected"
    bl_description = ("Включить в решение все пары, чьи 3D-вершины "
                      "выделены в Edit Mode")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return bpy.ops.pba_vm.set_enabled(enable=True)


class PBA_VM_OT_enable_all(bpy.types.Operator):
    bl_idname = "pba_vm.enable_all"
    bl_label = "Enable All"
    bl_description = "Включить все пары"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_vm
        changed = 0
        for c in pba.correspondences:
            if not c.enabled:
                c.enabled = True
                changed += 1
        _set_status(scene, f"Включено всех: {changed}")
        if len(pba.correspondences) >= 6 and scene.camera is not None:
            bpy.ops.pba_vm.solve_camera()
        return {'FINISHED'}

class PBA_VM_OT_select_correspondence(bpy.types.Operator):
    bl_idname = "pba_vm.select_correspondence"
    bl_label = "Select Correspondence Vertex"
    bl_description = "Выделить соответствующую вершину на 3D-модели"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        import bmesh
        scene = context.scene
        pba = scene.pba_vm

        if not (0 <= self.index < len(pba.correspondences)):
            return {'CANCELLED'}
        c = pba.correspondences[self.index]

        obj = scene.objects.get(c.obj_name)
        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'},
                        f"Объект '{c.obj_name}' не найден в сцене")
            return {'CANCELLED'}

        if c.vertex_index < 0 or c.vertex_index >= len(obj.data.vertices):
            self.report({'WARNING'},
                        f"Вершина {c.vertex_index} вне диапазона")
            return {'CANCELLED'}

        # Выйти из Edit Mode, если там
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        # Снять выделение со всех объектов
        for o in context.view_layer.objects:
            o.select_set(False)

        obj.select_set(True)
        context.view_layer.objects.active = obj

        # Войти в Edit Mode и выделить только нужную вершину
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        if c.vertex_index < len(bm.verts):
            bm.verts[c.vertex_index].select = True
            bm.verts.index_update()
        bmesh.update_edit_mesh(obj.data)

        # 3D-курсор — в позицию вершины
        v = obj.data.vertices[c.vertex_index]
        p_world = obj.matrix_world @ v.co
        scene.cursor.location = p_world

        _set_status(
            scene,
            f"Выделена: {c.obj_name} v{c.vertex_index} "
            f"({p_world.x:.2f}, {p_world.y:.2f}, {p_world.z:.2f})")
        return {'FINISHED'}

        
classes += (
    PBA_VM_OT_set_enabled,
    PBA_VM_OT_disable_selected,
    PBA_VM_OT_enable_selected,
    PBA_VM_OT_enable_all,
    PBA_VM_OT_select_correspondence
)

def _set_vertex_world(obj, vertex_index, new_world):
    """Устанавливает мировую позицию вершины, работая через bmesh
    в Edit Mode и через mesh data в Object Mode."""
    import bmesh
    co_local = obj.matrix_world.inverted() @ new_world
    ctx = bpy.context
    if ctx.mode == 'EDIT_MESH' and ctx.active_object == obj:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        if vertex_index < len(bm.verts):
            bm.verts[vertex_index].co = co_local
            bmesh.update_edit_mesh(obj.data)
            return True
        return False
    else:
        if 0 <= vertex_index < len(obj.data.vertices):
            obj.data.vertices[vertex_index].co = co_local
            return True
        return False


def _undistort(xn, yn, k1, k2, iters=20):
    """Обратная задача дисторсии: дано дистортированное (xn, yn),
    найти недистортированное (xu, yu) методом неподвижной точки."""
    xu, yu = xn, yn
    for _ in range(iters):
        r2 = xu * xu + yu * yu
        fac = 1.0 + k1 * r2 + k2 * r2 * r2
        if abs(fac) < 1e-9:
            break
        new_xu = xn / fac
        new_yu = yn / fac
        if (abs(new_xu - xu) < 1e-9 and abs(new_yu - yu) < 1e-9):
            xu, yu = new_xu, new_yu
            break
        xu, yu = new_xu, new_yu
    return xu, yu
    
class PBA_VM_OT_snap_to_click(bpy.types.Operator):
    bl_idname = "pba_vm.snap_to_click"
    bl_label = "Snap to Click"
    bl_description = ("Сдвинуть 3D-вершину так, чтобы её проекция "
                      "совпала с 2D-кликом. Глубина сохраняется.")
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        from mathutils import Vector
        scene = context.scene
        pba = scene.pba_vm

        if not (0 <= self.index < len(pba.correspondences)):
            return {'CANCELLED'}
        c = pba.correspondences[self.index]

        cam = scene.camera
        if cam is None or cam.type != 'CAMERA':
            self.report({'ERROR'}, "Нет активной камеры")
            return {'CANCELLED'}

        obj = scene.objects.get(c.obj_name)
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, f"Объект '{c.obj_name}' не найден")
            return {'CANCELLED'}
        if c.vertex_index < 0 or c.vertex_index >= len(obj.data.vertices):
            self.report({'ERROR'}, f"Вершина {c.vertex_index} вне диапазона")
            return {'CANCELLED'}

        # Размер фото и f_px
        W, H = _get_photo_size(scene)
        if W == 0 or H == 0:
            self.report({'ERROR'}, "Размер фото неизвестен")
            return {'CANCELLED'}

        lens = cam.data.lens
        sensor = cam.data.sensor_width
        if cam.data.sensor_fit == 'VERTICAL':
            f_px = lens / sensor * H
        elif cam.data.sensor_fit == 'HORIZONTAL':
            f_px = lens / sensor * W
        else:
            f_px = lens / sensor * max(W, H)

        k1 = pba.last_k1 if pba.use_distortion_k1 else 0.0
        k2 = pba.last_k2 if pba.use_distortion_k2 else 0.0

        u_px = c.x2d[0]
        v_px = c.x2d[1]
        cx_px = W / 2.0
        cy_px = H / 2.0

        xn = (u_px - cx_px) / f_px
        yn = (cy_px - v_px) / f_px   # вверх положительно

        xu, yu = _undistort(xn, yn, k1, k2)

        # Луч в системе камеры (Blender: X вправо, Y вверх, Z назад)
        direction_cam = Vector((xu, yu, -1.0))
        R_cw = cam.matrix_world.to_3x3()
        cam_loc = cam.matrix_world.translation.copy()
        direction_world = R_cw @ direction_cam
        forward_world = R_cw @ Vector((0.0, 0.0, -1.0))

        # Текущая позиция — берём из актуального меша
        v = obj.data.vertices[c.vertex_index]
        P_current = obj.matrix_world @ v.co

        # Глубина по оси взгляда камеры
        t = (P_current - cam_loc).dot(forward_world)
        if t <= 0.0:
            self.report({'WARNING'},
                        "Вершина за камерой — snap невозможен")
            return {'CANCELLED'}

        P_new = cam_loc + direction_world * t

        if not _set_vertex_world(obj, c.vertex_index, P_new):
            self.report({'ERROR'}, "Не удалось изменить вершину")
            return {'CANCELLED'}

        _sync_x3d_from_mesh(scene)

        shift = (P_new - P_current).length
        _set_status(
            scene,
            f"Snap: {c.obj_name} v{c.vertex_index} смещена "
            f"на {shift * 1000:.1f} мм "
            f"→ ({P_new.x:.3f}, {P_new.y:.3f}, {P_new.z:.3f})")
        return {'FINISHED'}


class PBA_VM_OT_nudge_vertex(bpy.types.Operator):
    bl_idname = "pba_vm.nudge_vertex"
    bl_label = "Nudge Vertex"
    bl_description = ("Сдвинуть выделенные в Edit Mode вершины "
                      "на шаг по одной оси в мировых координатах")
    bl_options = {'REGISTER', 'UNDO'}

    axis: bpy.props.EnumProperty(items=[
        ('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")])
    direction: bpy.props.IntProperty(default=1)

    def execute(self, context):
        import bmesh
        from mathutils import Vector

        scene = context.scene
        pba = scene.pba_vm
        step = pba.nudge_step

        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Нет активного mesh-объекта")
            return {'CANCELLED'}
        if context.mode != 'EDIT_MESH':
            self.report({'ERROR'}, "Нужен Edit Mode")
            return {'CANCELLED'}

        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        selected = [v for v in bm.verts if v.select]
        if not selected:
            self.report({'WARNING'}, "Не выделено вершин")
            return {'CANCELLED'}

        axis_idx = {'X': 0, 'Y': 1, 'Z': 2}[self.axis]
        world_offset = Vector((0.0, 0.0, 0.0))
        world_offset[axis_idx] = self.direction * step

        # Мировое смещение → локальное (только поворот, без трансляции)
        R_inv = obj.matrix_world.to_3x3().inverted()
        local_offset = R_inv @ world_offset

        for v in selected:
            v.co += local_offset

        bmesh.update_edit_mesh(obj.data)

        n = _sync_x3d_from_mesh(scene)
        sign = "+" if self.direction > 0 else "-"
        _set_status(
            scene,
            f"Nudge {self.axis}{sign}{step * 1000:.0f}мм: "
            f"{len(selected)} вершин, синхронизировано {n}")
        return {'FINISHED'}
        

class PBA_VM_OT_sort_pairs(bpy.types.Operator):
    bl_idname = "pba_vm.sort_pairs"
    bl_label = "Sort Pairs"
    bl_description = ("Сортировка: сначала по объекту, затем по "
                      "позиции клика сверху вниз, слева направо")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_vm
        n = len(pba.correspondences)
        if n < 2:
            _set_status(scene, f"Пар {n}, сортировка не нужна")
            return {'FINISHED'}

        data = []
        for c in pba.correspondences:
            data.append({
                "obj_name": c.obj_name,
                "vertex_index": c.vertex_index,
                "vertex_uuid": c.vertex_uuid,
                "x3d": (c.x3d[0], c.x3d[1], c.x3d[2]),
                "x2d": (c.x2d[0], c.x2d[1]),
                "enabled": c.enabled,
                "last_error": c.last_error,
                "sort_key": (c.obj_name, c.x2d[1], c.x2d[0]),
            })

        data.sort(key=lambda d: d["sort_key"])

        pba.correspondences.clear()
        for d in data:
            c = pba.correspondences.add()
            c.obj_name = d["obj_name"]
            c.vertex_index = d["vertex_index"]
            c.vertex_uuid = d["vertex_uuid"]
            c.x3d = d["x3d"]
            c.x2d = d["x2d"]
            c.enabled = d["enabled"]
            c.last_error = d["last_error"]

        _set_status(scene, f"Пары отсортированы: {n}")
        return {'FINISHED'}
        
classes += (
    PBA_VM_OT_snap_to_click,
    PBA_VM_OT_nudge_vertex,
    PBA_VM_OT_sort_pairs,
)
        
