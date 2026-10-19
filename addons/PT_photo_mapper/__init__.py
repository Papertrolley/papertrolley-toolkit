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
bl_info = {
    "name": "PT Photo Mapper",
    "author": "PaperTrolley",
    "version": (0, 4, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > PT Model",
    "description": "Загружает фото, настраивает камеру, сохраняет "
                   "координаты вершин в CSV и JSON",
    "category": "Modeling",
}

import bpy

from . import ops, panel


class PBA_VM_Correspondence(bpy.types.PropertyGroup):
    obj_name: bpy.props.StringProperty(default="")
    vertex_uuid: bpy.props.IntProperty(default=0)
    vertex_index: bpy.props.IntProperty(default=-1)
    x3d: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    x2d: bpy.props.FloatVectorProperty(size=2, default=(0.0, 0.0))
    last_error: bpy.props.FloatProperty(default=-1.0)
    enabled: bpy.props.BoolProperty(default=True)


class PBA_VM_Props(bpy.types.PropertyGroup):
    photo_path: bpy.props.StringProperty(subtype='FILE_PATH')
    camera_position: bpy.props.EnumProperty(
        items=[
            ('FRONT', "Front (+X)", ""),
            ('BACK',  "Back (-X)",  ""),
            ('RIGHT', "Right (+Y)", ""),
            ('LEFT',  "Left (-Y)",  ""),
            ('TOP',   "Top (+Z)",   ""),
        ],
        default='FRONT')
    visibility_check: bpy.props.BoolProperty(default=True)
    restore_from_json: bpy.props.BoolProperty(
        name="Восстанавливать из JSON",
        description="Если рядом с фото есть .vertices.json — "
                    "восстановить камеру и пары",
        default=True)
    status_msg: bpy.props.StringProperty(default="")

    # Solve-параметры
    crop_factor: bpy.props.FloatProperty(default=1.53, min=1.0, max=8.0)
    use_distortion_k1: bpy.props.BoolProperty(default=False)
    use_distortion_k2: bpy.props.BoolProperty(default=False)
    huber_delta: bpy.props.FloatProperty(default=0.0, min=0.0, max=100.0)
    last_f_px: bpy.props.FloatProperty(default=0.0)
    last_k1: bpy.props.FloatProperty(default=0.0)
    last_k2: bpy.props.FloatProperty(default=0.0)

    # Соответствия
    correspondences: bpy.props.CollectionProperty(
        type=PBA_VM_Correspondence)
    pending_x3d: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    pending_x2d: bpy.props.FloatVectorProperty(size=2, default=(0.0, 0.0))
    pending_3d_obj: bpy.props.StringProperty(default="")
    pending_3d_vertex: bpy.props.IntProperty(default=-1)
    pending_3d_uuid: bpy.props.IntProperty(default=0)
    pending_3d_set: bpy.props.BoolProperty(default=False)
    pending_2d_set: bpy.props.BoolProperty(default=False)
    
    nudge_step: bpy.props.FloatProperty(
    name="Шаг, м",
    description="На сколько метров двигать вершины по одной оси",
    default=0.01, min=0.001, max=1.0)
    use_native_resolution: bpy.props.BoolProperty(
        name="Кроп: использовать нативное разрешение",
        description="Если фото обрезано равномерно, учитывать "
                    "нативное разрешение камеры при решении",
        default=True)
    native_width_px: bpy.props.IntProperty(default=0)
    native_height_px: bpy.props.IntProperty(default=0)
    native_source: bpy.props.StringProperty(default="")
    physical_lens_mm: bpy.props.FloatProperty(
        name="Physical lens (mm)", default=50.0)    
    use_manual_camera: bpy.props.BoolProperty(
        name="Use Manual Camera",
        description="Использовать ручные параметры вместо EXIF. "
                    "Нужно для кропнутых фото без EXIF",
        default=False)
    manual_native_width: bpy.props.IntProperty(
        name="Native Width, px", default=0, min=0,
        description="Полное нативное разрешение по ширине (0 = авто)")
    manual_native_height: bpy.props.IntProperty(
        name="Native Height, px", default=0, min=0,
        description="Полное нативное разрешение по высоте (0 = авто)")
    manual_sensor_width: bpy.props.FloatProperty(
        name="Sensor Width, mm", default=0.0, min=0.0,
        description="Физическая ширина сенсора в мм (0 = авто). "
                    "Nikon Z50: 23.5. Полный кадр: 36.0")
    manual_focal_length: bpy.props.FloatProperty(
        name="Focal Length, mm", default=0.0, min=0.0,
        description="Физическое фокусное расстояние в мм (0 = авто)")
    crop_offset_x: bpy.props.IntProperty(
        name="Crop Offset X, px", default=0,
        description="Сдвиг главной точки по X. Для нецентрального "
                    "кропа. Положительный = главная точка правее центра")
    crop_offset_y: bpy.props.IntProperty(
        name="Crop Offset Y, px", default=0,
        description="Сдвиг главной точки по Y. Для нецентрального "
                    "кропа. Положительный = главная точка ниже центра")


classes = (PBA_VM_Correspondence, PBA_VM_Props) + ops.classes + panel.classes


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pba_vm = bpy.props.PointerProperty(type=PBA_VM_Props)


def unregister():
    del bpy.types.Scene.pba_vm
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
