import math
import os
import bmesh
import numpy as np
from mathutils import Vector

import bpy as bpy
from bpy_extras.object_utils import world_to_camera_view
from bpy_extras.view3d_utils import region_2d_to_vector_3d

from .exif import camera_params


DEFAULT_LENS_MM = 50.0
DEFAULT_SENSOR_MM = 36.0

CAMERA_DISTANCE_M = 10.0
CAMERA_NAME = "PBA_VM_Camera"

# Относительный допуск при сравнении расстояний. 0.1% от дистанции
# до вершины. Для камеры в 5 м это ~5 мм.
VISIBILITY_EPS_REL = 1e-3

_POSITIONS = {
    'FRONT': (( CAMERA_DISTANCE_M, 0.0, 0.0),
              (math.pi / 2, 0.0,  math.pi / 2)),
    'BACK':  ((-CAMERA_DISTANCE_M, 0.0, 0.0),
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

        if img.size[0] == 0 or img.size[1] == 0:
            _set_status(scene, "Изображение пустое")
            self.report({'ERROR'}, "Изображение пустое")
            return {'CANCELLED'}

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

        _delete_old_camera(scene)

        cam_data = bpy.data.cameras.new(name=CAMERA_NAME)
        cam_data.lens = focal
        cam_data.sensor_width = sensor

        cam_obj = bpy.data.objects.new(name=CAMERA_NAME, object_data=cam_data)
        scene.collection.objects.link(cam_obj)

        cam_obj.location = location
        cam_obj.rotation_euler = rotation

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

        _set_status(
            scene,
            f"Фото: {img.size[0]}×{img.size[1]}, "
            f"focal={focal:.1f}мм, sensor={sensor:.2f}мм, "
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

        try:
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write("object,vertex,x_px,y_px,z_depth,"
                        "in_frame,visible\n")

                for obj in scene.objects:
                    if obj.type != 'MESH':
                        continue
                    M = obj.matrix_world
                    for v in obj.data.vertices:
                        p_world = M @ v.co
                        co = world_to_camera_view(scene, cam_obj, p_world)
                        x_px = co.x * W
                        y_px = (1.0 - co.y) * H
                        z = co.z

                        in_frame = 1 if (0.0 <= co.x <= 1.0
                                         and 0.0 <= co.y <= 1.0
                                         and z > 0.0) else 0
                        if in_frame:
                            in_frame_count += 1

                        visible = 0
                        if in_frame:
                            if not self.check_visibility:
                                visible = 1
                            else:
                                direction = p_world - cam_loc
                                dist_to_vertex = direction.length
                                if dist_to_vertex > 1e-9:
                                    dir_norm = direction / dist_to_vertex
                                    (hit, hit_loc, hit_n, hit_idx,
                                     hit_obj, hit_mat) = scene.ray_cast(
                                        depsgraph, cam_loc, dir_norm)
                                    if not hit:
                                        # Луч ни во что не попал —
                                        # считаем вершину видимой.
                                        visible = 1
                                    else:
                                        hit_dist = (hit_loc
                                                    - cam_loc).length
                                        eps = (VISIBILITY_EPS_REL
                                               * dist_to_vertex)
                                        if hit_dist >= dist_to_vertex - eps:
                                            visible = 1
                        if visible:
                            visible_count += 1

                        f.write(
                            f"{obj.name},{v.index},"
                            f"{x_px:.3f},{y_px:.3f},{z:.4f},"
                            f"{in_frame},{visible}\n")
                        total += 1
        except Exception as e:
            _set_status(scene, f"Ошибка записи: {e}")
            self.report({'ERROR'}, f"Ошибка записи: {e}")
            return {'CANCELLED'}

        _set_status(
            scene,
            f"Сохранено: {out_path} | вершин {total}, "
            f"в кадре {in_frame_count}, видимых {visible_count}")
        self.report({'INFO'},
                    f"CSV: {out_path} (видимых: {visible_count})")
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
        if (existing.obj_name == pba.pending_3d_obj
                and existing.vertex_index == pba.pending_3d_vertex):
            pba.pending_3d_set = False
            pba.pending_2d_set = False
            pba.pending_3d_obj = ""
            pba.pending_3d_vertex = -1
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
                  f"{existing.obj_name} v{existing.vertex_index} "
                  f"({existing.x2d[0]:.1f},{existing.x2d[1]:.1f}). "
                  f"Возможно, дубликат.")

    c = pba.correspondences.add()
    c.obj_name = pba.pending_3d_obj
    c.vertex_index = pba.pending_3d_vertex
    c.x3d = pba.pending_x3d
    c.x2d = pba.pending_x2d

    pba.pending_3d_set = False
    pba.pending_2d_set = False
    pba.pending_3d_obj = ""
    pba.pending_3d_vertex = -1


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
        pba.pending_x3d = (p_world.x, p_world.y, p_world.z)
        pba.pending_3d_obj = obj.name
        pba.pending_3d_vertex = v.index
        pba.pending_3d_set = True

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

        W, H = _get_photo_size(scene)
        if W == 0 or H == 0:
            self.report({'ERROR'}, "Размер фото неизвестен")
            return {'CANCELLED'}


        pba = scene.pba_vm
        lens = cam.data.lens
        sensor = cam.data.sensor_width
        if cam.data.sensor_fit == 'VERTICAL':
            base_f_px = lens / sensor * H
        elif cam.data.sensor_fit == 'HORIZONTAL':
            base_f_px = lens / sensor * W
        else:
            base_f_px = lens / sensor * max(W, H)

        crop = float(pba.crop_factor)
        candidates = [
            ("EXIF как есть", base_f_px),
            (f"×crop={crop:.2f}", base_f_px * crop),
            (f"÷crop={crop:.2f}", base_f_px / crop),
        ]

        world_pts = [np.array(c.x3d) for c in corrs]
        pixel_pts = [np.array(c.x2d) for c in corrs]

        from mathutils import Matrix
        from .solver import pose_refine_multistart

        M_init = np.array(cam.matrix_world)

        print(f"[PBA solve] focal={lens:.2f}mm, sensor={sensor:.2f}mm, "
              f"точек={len(world_pts)}, "
              f"дисторсия={'K1+K2' if pba.use_distortion_k1 and pba.use_distortion_k2 else 'K1' if pba.use_distortion_k1 else 'K2' if pba.use_distortion_k2 else 'выкл'}, "
              f"crop={crop:.3f}")
        print(f"[PBA solve] base_f_px = {base_f_px:.1f}")

        best = None
        for name, f_px_try in candidates:
            M_best, rmse, k1, k2 = pose_refine_multistart(
                world_pts, pixel_pts, W, H, f_px_try, M_init,
                use_distortion_k1=pba.use_distortion_k1,
                use_distortion_k2=pba.use_distortion_k2,
                huber_delta=pba.huber_delta,
                verbose=False)
            if M_best is None:
                print(f"[PBA focal] {name}: LM не сошёлся")
                continue
            print(f"[PBA focal] {name:16s} f_px={f_px_try:8.1f}  "
                  f"rmse={rmse:7.3f} px,  k1={k1:+.4f}, k2={k2:+.4f}")
            if best is None or rmse < best[1]:
                best = (M_best, rmse, k1, k2, f_px_try, name)

        if best is None:
            self.report({'ERROR'}, "LM не сошёлся ни для одного фокусного")
            return {'CANCELLED'}

        M_best, rmse, k1, k2, f_px_used, name_used = best
        print(f"[PBA focal] ВЫБРАНО: {name_used}, "
              f"f_px={f_px_used:.1f}, rmse={rmse:.3f}, k1={k1:+.4f}, k2={k2:+.4f}")

        cam.matrix_world = Matrix(M_best.tolist())
        pba.last_k1 = k1
        pba.last_k2 = k2
        pba.last_f_px = f_px_used

        cx, cy = W / 2.0, H / 2.0
        errors = []
        for c in corrs:
            co = world_to_camera_view(
                scene, cam, Vector((c.x3d[0], c.x3d[1], c.x3d[2])))
            u_px = co.x * W
            v_px = (1.0 - co.y) * H
            xn = (u_px - cx) / f_px_used
            yn = (v_px - cy) / f_px_used
            r2 = xn * xn + yn * yn
            fac = 1.0 + k1 * r2 + k2 * r2 * r2
            u_d = f_px_used * xn * fac + cx
            v_d = f_px_used * yn * fac + cy
            err = ((u_d - c.x2d[0]) ** 2 + (v_d - c.x2d[1]) ** 2) ** 0.5
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
            f"Ошибка: средняя {mean_err:.2f}px, макс {max_err:.2f}px. "
            f"Фокус: {name_used}.{dist_str}")
        self.report({'INFO'},
                    f"RMSE {rmse:.2f}px, фокус {name_used}")
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
        cx, cy = W / 2.0, H / 2.0

        errors = []
        for c in corrs:
            co = world_to_camera_view(
                scene, cam, Vector((c.x3d[0], c.x3d[1], c.x3d[2])))
            u_px = co.x * W
            v_px = (1.0 - co.y) * H
            xn = (u_px - cx) / f_px
            yn = (v_px - cy) / f_px
            r2 = xn * xn + yn * yn
            fac = 1.0 + k1 * r2 + k2 * r2 * r2
            u_d = f_px * xn * fac + cx
            v_d = f_px * yn * fac + cy
            err = ((u_d - c.x2d[0]) ** 2 + (v_d - c.x2d[1]) ** 2) ** 0.5
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
        
classes += (
    PBA_VM_OT_set_enabled,
    PBA_VM_OT_disable_selected,
    PBA_VM_OT_enable_selected,
    PBA_VM_OT_enable_all,
)
