import math
import os
import bmesh
import numpy as np
from mathutils import Vector

import bpy as bpy
from bpy_extras.object_utils import world_to_camera_view
from bpy_extras.view3d_utils import region_2d_to_vector_3d

from .exif import camera_params
from .solver import solve_pnp


DEFAULT_LENS_MM = 50.0
DEFAULT_SENSOR_MM = 36.0

CAMERA_DISTANCE_M = 5.0
CAMERA_NAME = "PBA_VM_Camera"

# Относительный допуск при сравнении расстояний. 0.1% от дистанции
# до вершины. Для камеры в 5 м это ~5 мм.
VISIBILITY_EPS_REL = 1e-3

_POSITIONS = {
    'FRONT': (( CAMERA_DISTANCE_M, 0.0, 1.5),
              (math.pi / 2, 0.0,  math.pi / 2)),
    'BACK':  ((-CAMERA_DISTANCE_M, 0.0, 1.5),
              (math.pi / 2, 0.0, -math.pi / 2)),
    'LEFT': ((0.0,  CAMERA_DISTANCE_M, 1.5),
              (math.pi / 2, 0.0,  math.pi)),
    'RIGHT':  ((0.0, -CAMERA_DISTANCE_M, 1.5),
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

        corrs = scene.pba_vm.correspondences
        if len(corrs) < 6:
            self.report({'WARNING'}, f"Нужно минимум 6 пар, у вас {len(corrs)}")
            return {'CANCELLED'}

        W, H = _get_photo_size(scene)
        if W == 0 or H == 0:
            self.report({'ERROR'}, "Размер фото неизвестен")
            return {'CANCELLED'}


        lens = cam.data.lens
        sensor = cam.data.sensor_width
        if cam.data.sensor_fit == 'VERTICAL':
            f_px_init = lens / sensor * H
        elif cam.data.sensor_fit == 'HORIZONTAL':
            f_px_init = lens / sensor * W
        else:  # AUTO
            f_px_init = lens / sensor * max(W, H)

        world_pts = [np.array(c.x3d) for c in corrs]
        pixel_pts = [np.array(c.x2d) for c in corrs]

        from .solver import (solve_pnp, refine_focal_and_pose,
                              geometric_refine)

        # Шаг 1: DLT для начального приближения
        K_init = np.array([
            [f_px_init, 0.0, W / 2.0],
            [0.0, f_px_init, H / 2.0],
            [0.0, 0.0, 1.0],
        ])
        M_dlt = solve_pnp(world_pts, pixel_pts, K_init)

        # Шаг 2: grid search по фокусному
        M_grid, f_px_grid, rmse_grid = refine_focal_and_pose(
            world_pts, pixel_pts, W, H, f_px_init)

        if M_grid is None:
            self.report({'ERROR'}, "PnP не сошёлся")
            return {'CANCELLED'}

        # Шаг 3: geometric refinement (LM)
        M_ref, f_px_ref, rmse_ref = geometric_refine(
            world_pts, pixel_pts, W, H, M_grid, f_px_grid)

        if M_ref is None:
            M_ref, f_px_ref = M_grid, f_px_grid
            rmse_ref = rmse_grid

        print(f"[PBA refine] DLT rmse: {rmse_grid:.2f} px, "
              f"f_px = {f_px_grid:.1f}")
        print(f"[PBA refine] LM  rmse: {rmse_ref:.2f} px, "
              f"f_px = {f_px_ref:.1f}")

        M = M_ref
        f_px_best = f_px_ref

        # Пересчитываем lens для Blender-камеры
        if cam.data.sensor_fit == 'VERTICAL':
            lens_new = f_px_best * sensor / H
        elif cam.data.sensor_fit == 'HORIZONTAL':
            lens_new = f_px_best * sensor / W
        else:
            lens_new = f_px_best * sensor / max(W, H)

        lens_before = cam.data.lens
        cam.data.lens = lens_new
        cam.matrix_world = Matrix(M.tolist())
        
        # Диагностика ориентации
        R = cam.matrix_world.to_3x3()
        forward = -R.col[2]  # куда смотрит камера, в мире
        up = R.col[1]        # верх камеры
        right = R.col[0]
        print(f"[PBA pose] forward: ({forward.x:.3f}, {forward.y:.3f}, {forward.z:.3f})")
        print(f"[PBA pose] up:      ({up.x:.3f}, {up.y:.3f}, {up.z:.3f})")
        

        # Оценка ошибки после уточнения
        errors = []
        for c in corrs:
            co = world_to_camera_view(
                scene, cam, Vector((c.x3d[0], c.x3d[1], c.x3d[2])))
            px = co.x * W
            py = (1.0 - co.y) * H
            err = ((px - c.x2d[0]) ** 2 + (py - c.x2d[1]) ** 2) ** 0.5
            errors.append(err)

        mean_err = sum(errors) / len(errors)
        max_err = max(errors)
        
        for idx, c in enumerate(corrs):
            co = world_to_camera_view(
                scene, cam, Vector((c.x3d[0], c.x3d[1], c.x3d[2])))
            px = co.x * W
            py = (1.0 - co.y) * H
            err = ((px - c.x2d[0]) ** 2 + (py - c.x2d[1]) ** 2) ** 0.5
            c.last_error = err
            errors.append(err)
            
        print("[PBA per-pair] ошибки по парам:")
        for idx, err in enumerate(errors):
            flag = "  <-- выброс" if err > 2.5 * (sum(errors)/len(errors)) else ""
            print(f"  pair {idx}: {err:7.2f} px{flag}")

        _set_status(
            scene,
            f"Камера обновлена. Ошибка: средняя {mean_err:.2f}px, "
            f"макс {max_err:.2f}px. "
            f"Фокус: {lens_before:.1f} → {lens_new:.1f}мм")
        self.report({'INFO'},
                    f"PnP: RMSE {rmse_ref:.2f}px, lens {lens_new:.1f}мм")
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

        corrs = scene.pba_vm.correspondences
        if not corrs:
            self.report({'WARNING'}, "Нет пар")
            return {'CANCELLED'}

        W, H = _get_photo_size(scene)
        if W == 0 or H == 0:
            self.report({'ERROR'}, "Размер фото неизвестен")
            return {'CANCELLED'}

        errors = []
        for c in corrs:
            co = world_to_camera_view(
                scene, cam, Vector((c.x3d[0], c.x3d[1], c.x3d[2])))
            px = co.x * W
            py = (1.0 - co.y) * H
            err = ((px - c.x2d[0]) ** 2 + (py - c.x2d[1]) ** 2) ** 0.5
            c.last_error = err
            errors.append(err)

        mean_err = sum(errors) / len(errors)
        max_err = max(errors)
        outliers = sum(1 for e in errors if e > 2.5 * mean_err)

        _set_status(
            scene,
            f"Диагностика: средняя {mean_err:.2f}px, макс {max_err:.2f}px, "
            f"выбросов: {outliers}")
        self.report({'INFO'}, f"Средняя ошибка {mean_err:.2f}px")
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

        # Сначала обновим ошибки
        bpy.ops.pba_vm.diagnose_pairs()

        errs = [c.last_error for c in pba.correspondences
                if c.last_error >= 0.0]
        if not errs:
            return {'CANCELLED'}

        errs_sorted = sorted(errs)
        median = errs_sorted[len(errs_sorted) // 2]
        threshold = max(median * 2.5, 5.0)

        to_remove = []
        for i, c in enumerate(pba.correspondences):
            if c.last_error > threshold:
                to_remove.append(i)

        # Удаляем с конца, чтобы индексы не сбились
        for i in reversed(to_remove):
            pba.correspondences.remove(i)

        _set_status(
            scene,
            f"Удалено выбросов: {len(to_remove)} "
            f"(порог {threshold:.1f}px, медиана {median:.1f}px)")
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
