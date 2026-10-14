import json
import os

import bpy
import bmesh
import numpy as np
from mathutils import Matrix, Vector
from bpy_extras.view3d_utils import (
    region_2d_to_origin_3d, region_2d_to_vector_3d)
from bpy_extras.object_utils import world_to_camera_view

from .core import make_P, triangulate


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

    attr = _get_uuid_attr(obj.data, create=False)
    if attr is None or len(attr.data) == 0:
        return 0
    if vertex_index >= len(obj.data.vertices):
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


OUTPUT_OBJ_NAME = "Triangulated_Points"
CAM_NAME_TPL = "PBA_TRI_Cam_{}"


def _set_status(scene, msg):
    scene.pba_tri.status_msg = msg
    print(f"[PBA TRI] {msg}")


def _ensure_slots(scene):
    p = scene.pba_tri
    while len(p.photos) < 3:
        p.photos.add()


def _remove_camera_by_name(scene, name):
    obj = scene.objects.get(name) if name else None
    if obj is not None and obj.type == 'CAMERA':
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data.users == 0:
            bpy.data.cameras.remove(data)


def _parse_json(json_path):
    with open(json_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _get_or_create_output(scene):
    obj = scene.objects.get(OUTPUT_OBJ_NAME)
    if obj is None or obj.type != 'MESH':
        mesh = bpy.data.meshes.new(OUTPUT_OBJ_NAME + "_mesh")
        obj = bpy.data.objects.new(OUTPUT_OBJ_NAME, mesh)
        scene.collection.objects.link(obj)
    return obj


def _clear_output(scene):
    obj = scene.objects.get(OUTPUT_OBJ_NAME)
    if obj is None or obj.type != 'MESH':
        return
    try:
        obj.data.clear_geometry()
    except Exception:
        obj.data = bpy.data.meshes.new(OUTPUT_OBJ_NAME + "_mesh")


def _append_point_marker(obj, P, size):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    offsets = [(1, 0, 0), (-1, 0, 0), (0, 1, 0),
               (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    vs = []
    for dx, dy, dz in offsets:
        vs.append(bm.verts.new((P[0] + dx * size,
                                 P[1] + dy * size,
                                 P[2] + dz * size)))
    faces = [
        (0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4),
        (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5),
    ]
    for f in faces:
        try:
            bm.faces.new([vs[i] for i in f])
        except ValueError:
            pass
    bm.to_mesh(obj.data)
    bm.free()


def _cam_kwargs_from_slot(slot):
    f_px = slot.f_px
    if f_px <= 0:
        # Fallback: посчитать из lens и sensor
        if slot.sensor_width_mm > 0 and slot.lens_mm > 0:
            f_px = slot.lens_mm / slot.sensor_width_mm * max(
                slot.width_px, slot.height_px)
        else:
            f_px = 1.0
    return {
        'P': make_P(slot.matrix_world, f_px,
                    slot.width_px, slot.height_px),
        'f_px': f_px,
        'W': slot.width_px,
        'H': slot.height_px,
        'k1': slot.k1 if slot.use_distortion else 0.0,
        'k2': (slot.k2 if (slot.use_distortion and slot.use_k2) else 0.0),
    }


# ---------- Operators ----------

class PBA_TRI_OT_load_slot(bpy.types.Operator):
    bl_idname = "pba_tri.load_slot"
    bl_label = "Load Slot"
    bl_description = "Загрузить JSON для слота и создать камеру"

    slot_index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        _ensure_slots(scene)
        p = scene.pba_tri
        if not (0 <= self.slot_index < len(p.photos)):
            return {'CANCELLED'}
        slot = p.photos[self.slot_index]

        path = bpy.path.abspath(slot.json_path)
        if not path or not os.path.exists(path):
            self.report({'ERROR'}, "JSON не найден")
            return {'CANCELLED'}

        try:
            data = _parse_json(path)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка чтения: {e}")
            return {'CANCELLED'}

        photo = data.get("photo", {})
        cam_info = data.get("camera", {})
        solve = data.get("solve", {})

        photo_path = photo.get("path", "")
        if photo_path and not os.path.exists(photo_path):
            photo_path = os.path.join(os.path.dirname(path),
                                       photo.get("filename", ""))
        if not photo_path or not os.path.exists(photo_path):
            self.report({'ERROR'}, "Фото не найдено рядом с JSON")
            return {'CANCELLED'}

        # Загружаем изображение
        try:
            img = bpy.data.images.load(photo_path, check_existing=True)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка фото: {e}")
            return {'CANCELLED'}

        W = int(photo.get("width_px", img.size[0]))
        H = int(photo.get("height_px", img.size[1]))

        slot.width_px = W
        slot.height_px = H
        slot.image_name = img.name
        slot.lens_mm = float(cam_info.get("lens_mm", 50.0))
        slot.sensor_width_mm = float(cam_info.get("sensor_width_mm", 36.0))
        slot.f_px = float(solve.get("f_px", 0.0))
        slot.k1 = float(solve.get("k1", 0.0))
        slot.k2 = float(solve.get("k2", 0.0))
        slot.use_distortion = bool(solve.get("use_distortion", False))
        slot.use_k2 = bool(solve.get("use_k2", False))

        m_flat = cam_info.get("matrix_world", [])
        if len(m_flat) == 16:
            for i in range(16):
                slot.matrix_world[i] = float(m_flat[i])

        # Создаём камеру
        cam_name = CAM_NAME_TPL.format(self.slot_index + 1)
        _remove_camera_by_name(scene, cam_name)
        cam_data = bpy.data.cameras.new(name=cam_name)
        cam_data.lens = slot.lens_mm
        cam_data.sensor_width = slot.sensor_width_mm
        cam_obj = bpy.data.objects.new(name=cam_name, object_data=cam_data)
        scene.collection.objects.link(cam_obj)
        rows = [m_flat[0:4], m_flat[4:8], m_flat[8:12], m_flat[12:16]]
        cam_obj.matrix_world = Matrix(rows)

        try:
            bg = cam_data.background_images.new()
            bg.source = 'IMAGE'
            bg.image = img
            bg.alpha = 0.5
            bg.display_depth = 'FRONT'
            bg.show_background_image = True
            cam_data.show_background_images = True
        except Exception:
            pass

        slot.camera_name = cam_obj.name

        # Соответствия
        slot.correspondences.clear()
        missing_uuid = 0
        for cd in data.get("correspondences", []):
            if not cd.get("enabled", True):
                continue
            obj_name = cd.get("obj", "")
            v_uuid = int(cd.get("vertex_uuid", 0))
            v_idx = int(cd.get("vertex", -1))
            xw_raw = cd.get("x3d", [0.0, 0.0, 0.0])

            # Восстановление индекса по UUID
            resolved_idx = v_idx
            if v_uuid > 0:
                obj = scene.objects.get(obj_name)
                if obj is not None and obj.type == 'MESH':
                    hint = (float(xw_raw[0]), float(xw_raw[1]),
                            float(xw_raw[2]))
                    idx, n_cand = find_vertex_by_uuid(
                        obj, v_uuid, hint_world=hint)
                    if idx >= 0:
                        resolved_idx = idx
                    else:
                        missing_uuid += 1
                        print(f"[PBA TRI] {obj_name} uuid={v_uuid} "
                              f"не найден в меше")

            c = slot.correspondences.add()
            c.obj_name = obj_name
            c.vertex_index = resolved_idx
            c.vertex_uuid = v_uuid
            c.x_world = (float(xw_raw[0]), float(xw_raw[1]),
                         float(xw_raw[2]))
            x2d = cd.get("x2d", [0.0, 0.0])
            c.x2d[0] = float(x2d[0])
            c.x2d[1] = float(x2d[1])
            c.enabled = True

        slot.loaded = True
        tail = f", потеряно по UUID {missing_uuid}" if missing_uuid else ""
        _set_status(scene,
                    f"Слот {self.slot_index + 1}: загружено "
                    f"{len(slot.correspondences)} пар, "
                    f"{W}×{H}{tail}")
        return {'FINISHED'}


class PBA_TRI_OT_activate_slot(bpy.types.Operator):
    bl_idname = "pba_tri.activate_slot"
    bl_label = "Activate Slot"

    slot_index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        _ensure_slots(scene)
        p = scene.pba_tri
        if not (0 <= self.slot_index < len(p.photos)):
            return {'CANCELLED'}
        slot = p.photos[self.slot_index]
        if not slot.loaded:
            self.report({'WARNING'}, "Слот не загружен")
            return {'CANCELLED'}

        cam = scene.objects.get(slot.camera_name)
        if cam is None or cam.type != 'CAMERA':
            self.report({'ERROR'}, "Камера не найдена")
            return {'CANCELLED'}

        scene.camera = cam
        p.active_slot = self.slot_index

        # Синхронизируем разрешение рендера с фото
        scene.render.resolution_x = slot.width_px
        scene.render.resolution_y = slot.height_px
        scene.render.resolution_percentage = 100

        _set_status(scene, f"Активен слот {self.slot_index + 1}")
        return {'FINISHED'}


class PBA_TRI_OT_auto_triangulate(bpy.types.Operator):
    bl_idname = "pba_tri.auto_triangulate"
    bl_label = "Auto-Triangulate Matches"
    bl_description = ("Найти вершины, встречающиеся в 2+ JSON, "
                      "и триангулировать их")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        _ensure_slots(scene)
        p = scene.pba_tri

        loaded = [s for s in p.photos if s.loaded]
        if len(loaded) < 2:
            self.report({'ERROR'}, "Нужно минимум 2 загруженных слота")
            return {'CANCELLED'}

        # Индексы слотов
        loaded_slots = [(i, s) for i, s in enumerate(p.photos) if s.loaded]

        # Группируем: приоритет по (obj_name, vertex_uuid),
        # fallback по (obj_name, vertex_index)
        groups = {}
        for i, slot in loaded_slots:
            for c in slot.correspondences:
                if c.vertex_uuid > 0:
                    key = (c.obj_name, "uuid", c.vertex_uuid)
                else:
                    key = (c.obj_name, "idx", c.vertex_index)
                groups.setdefault(key, []).append(
                    (i, (c.x2d[0], c.x2d[1])))

        # Оставляем только ключи, встречающиеся в 2+ слотах
        multi = {k: v for k, v in groups.items() if len(v) >= 2}
        print(f"[PBA TRI] групп всего: {len(groups)}, "
              f"с 2+ слотами: {len(multi)}")

        _clear_output(scene)
        out_obj = _get_or_create_output(scene)
        p.output_object_name = out_obj.name

        size = p.point_size
        added = 0
        for key, obs in multi.items():
            cameras = []
            points_2d = []
            for slot_idx, (u, v) in obs:
                slot = p.photos[slot_idx]
                cameras.append(_cam_kwargs_from_slot(slot))
                points_2d.append((u, v))

            P3 = triangulate(cameras, points_2d)
            if P3 is None:
                print(f"[PBA TRI] {key}: триангуляция не удалась")
                continue
            _append_point_marker(out_obj, P3, size)
            added += 1

        _set_status(scene,
                    f"Авто-триангуляция: {added} точек "
                    f"из {len(multi)} совпадений")
        self.report({'INFO'}, f"Точек: {added}")
        return {'FINISHED'}


class PBA_TRI_OT_start_new_point(bpy.types.Operator):
    bl_idname = "pba_tri.start_new_point"
    bl_label = "Start New Point"
    bl_description = "Начать создание новой точки"

    def execute(self, context):
        scene = context.scene
        p = scene.pba_tri
        p.building = True
        p.clicks.clear()
        _set_status(scene, "Новая точка: переключайте слоты и жмите Pick")
        return {'FINISHED'}


class PBA_TRI_OT_pick_click(bpy.types.Operator):
    bl_idname = "pba_tri.pick_click"
    bl_label = "Pick on Active Photo"
    bl_description = "Кликните точку на фото в текущем слоте"

    def invoke(self, context, event):
        scene = context.scene
        p = scene.pba_tri
        if not p.building:
            self.report({'ERROR'}, "Сначала Start New Point")
            return {'CANCELLED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}
        slot = p.photos[p.active_slot]
        if not slot.loaded:
            self.report({'ERROR'}, "Активный слот не загружен")
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
            p = scene.pba_tri
            slot = p.photos[p.active_slot]
            cam = scene.objects.get(slot.camera_name)
            if cam is None:
                context.workspace.status_text_set(None)
                return {'CANCELLED'}

            coord = (event.mouse_region_x, event.mouse_region_y)
            origin = region_2d_to_origin_3d(
                context.region, context.space_data.region_3d, coord)
            direction = region_2d_to_vector_3d(
                context.region, context.space_data.region_3d, coord)
            # Точка далеко по лучу
            P_far = origin + direction * 100.0

            # Нормализованные координаты в кадре камеры
            co = world_to_camera_view(scene, cam, P_far)
            W = slot.width_px
            H = slot.height_px
            u = co.x * W
            v = (1.0 - co.y) * H

            # Перезаписываем клик для этого слота, если был
            for i, cl in enumerate(p.clicks):
                if cl.slot == p.active_slot:
                    p.clicks.remove(i)
                    break

            c = p.clicks.add()
            c.slot = p.active_slot
            c.x2d = (u, v)

            _set_status(
                scene,
                f"Слот {p.active_slot + 1}: ({u:.1f}, {v:.1f})")
            context.workspace.status_text_set(None)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}


class PBA_TRI_OT_commit_point(bpy.types.Operator):
    bl_idname = "pba_tri.commit_point"
    bl_label = "Add Vertex"
    bl_description = "Триангулировать и добавить точку в новый объект"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        p = scene.pba_tri

        if not p.building:
            self.report({'ERROR'}, "Сначала Start New Point")
            return {'CANCELLED'}
        if len(p.clicks) < 2:
            self.report({'WARNING'},
                        f"Кликов {len(p.clicks)}, нужно ≥2")
            return {'CANCELLED'}

        cameras = []
        points_2d = []
        for cl in p.clicks:
            slot = p.photos[cl.slot]
            cameras.append(_cam_kwargs_from_slot(slot))
            points_2d.append((cl.x2d[0], cl.x2d[1]))

        P3 = triangulate(cameras, points_2d)
        if P3 is None:
            self.report({'ERROR'}, "Триангуляция не удалась")
            return {'CANCELLED'}

        out_obj = _get_or_create_output(scene)
        p.output_object_name = out_obj.name
        _append_point_marker(out_obj, P3, p.point_size)

        _set_status(
            scene,
            f"Точка добавлена: ({P3[0]:.3f}, {P3[1]:.3f}, "
            f"{P3[2]:.3f}) из {len(p.clicks)} наблюдений")
        p.building = False
        p.clicks.clear()
        return {'FINISHED'}


class PBA_TRI_OT_cancel_point(bpy.types.Operator):
    bl_idname = "pba_tri.cancel_point"
    bl_label = "Cancel"

    def execute(self, context):
        p = context.scene.pba_tri
        p.building = False
        p.clicks.clear()
        _set_status(context.scene, "Отменено")
        return {'FINISHED'}


class PBA_TRI_OT_clear_output(bpy.types.Operator):
    bl_idname = "pba_tri.clear_output"
    bl_label = "Clear Output"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        _clear_output(context.scene)
        _set_status(context.scene, "Выходной объект очищен")
        return {'FINISHED'}


class PBA_TRI_OT_open_output(bpy.types.Operator):
    bl_idname = "pba_tri.open_output"
    bl_label = "Select Output Object"

    def execute(self, context):
        scene = context.scene
        obj = scene.objects.get(OUTPUT_OBJ_NAME)
        if obj is None:
            self.report({'WARNING'}, "Объект ещё не создан")
            return {'CANCELLED'}
        for o in context.view_layer.objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {'FINISHED'}


classes = (
    PBA_TRI_OT_load_slot,
    PBA_TRI_OT_activate_slot,
    PBA_TRI_OT_auto_triangulate,
    PBA_TRI_OT_start_new_point,
    PBA_TRI_OT_pick_click,
    PBA_TRI_OT_commit_point,
    PBA_TRI_OT_cancel_point,
    PBA_TRI_OT_clear_output,
    PBA_TRI_OT_open_output,
)
