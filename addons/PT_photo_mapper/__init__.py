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
    "version": (0, 5, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > PT Photo Mapper",
    "description": "Калибровка камеры по фото и разметка соответствий",
    "category": "Modeling",
}

import bpy
from . import ops, panel

classes = ops.classes + panel.classes


class PBA_VM_Correspondence(bpy.types.PropertyGroup):
    instance_id: bpy.props.StringProperty()
    obj_name: bpy.props.StringProperty()
    local_uuid: bpy.props.IntProperty(default=0)
    x3d: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    x2d: bpy.props.FloatVectorProperty(size=2, default=(0.0, 0.0))
    last_error: bpy.props.FloatProperty(default=-1.0)
    enabled: bpy.props.BoolProperty(default=True)


class PBA_VM_Photo(bpy.types.PropertyGroup):
    id: bpy.props.StringProperty()
    file: bpy.props.StringProperty(subtype='FILE_PATH')
    image_name: bpy.props.StringProperty()
    camera_object: bpy.props.StringProperty()
    lens: bpy.props.FloatProperty(default=50.0)
    sensor_width: bpy.props.FloatProperty(default=36.0)
    priority: bpy.props.IntProperty(default=1)
    matrix_world: bpy.props.FloatVectorProperty(size=16,
                                                 default=(0.0,) * 16)


class PBA_VM_Props(bpy.types.PropertyGroup):
    photo_path: bpy.props.StringProperty(subtype='FILE_PATH')
    layout_path: bpy.props.StringProperty(
        name="Layout JSON", subtype='FILE_PATH')
    camera_position: bpy.props.EnumProperty(
        items=[
            ('FRONT', "Front (+X)", ""),
            ('BACK',  "Back (-X)",  ""),
            ('RIGHT', "Right (+Y)", ""),
            ('LEFT',  "Left (-Y)",  ""),
            ('TOP',   "Top (+Z)",   ""),
        ],
        default='FRONT')
    status_msg: bpy.props.StringProperty(default="")

    # Solve-параметры
    crop_factor: bpy.props.FloatProperty(default=1.53, min=1.0, max=8.0)
    use_distortion_k1: bpy.props.BoolProperty(default=False)
    use_distortion_k2: bpy.props.BoolProperty(default=False)
    huber_delta: bpy.props.FloatProperty(default=0.0, min=0.0, max=100.0)
    last_f_px: bpy.props.FloatProperty(default=0.0)
    last_k1: bpy.props.FloatProperty(default=0.0)
    last_k2: bpy.props.FloatProperty(default=0.0)

    # Кроп и ручные настройки камеры
    manual_camera_show: bpy.props.BoolProperty(name="Показать Manual Camera", default=False)
    use_manual_camera: bpy.props.BoolProperty(default=False)
    manual_native_width: bpy.props.IntProperty(default=0, min=0)
    manual_native_height: bpy.props.IntProperty(default=0, min=0)
    manual_sensor_width: bpy.props.FloatProperty(default=0.0, min=0.0)
    manual_focal_length: bpy.props.FloatProperty(default=0.0, min=0.0)
    use_native_resolution: bpy.props.BoolProperty(default=True)
    native_width_px: bpy.props.IntProperty(default=0)
    native_height_px: bpy.props.IntProperty(default=0)
    native_source: bpy.props.StringProperty(default="")
    physical_lens_mm: bpy.props.FloatProperty(default=50.0)
    crop_offset_x: bpy.props.IntProperty(default=0)
    crop_offset_y: bpy.props.IntProperty(default=0)

    # Pending
    pending_x3d: bpy.props.FloatVectorProperty(size=3,
                                                default=(0.0, 0.0, 0.0))
    pending_x2d: bpy.props.FloatVectorProperty(size=2,
                                                default=(0.0, 0.0))
    pending_instance_id: bpy.props.StringProperty(default="")
    pending_obj_name: bpy.props.StringProperty(default="")
    pending_local_uuid: bpy.props.IntProperty(default=0)
    pending_3d_set: bpy.props.BoolProperty(default=False)
    pending_2d_set: bpy.props.BoolProperty(default=False)

    # Nudge
    nudge_show: bpy.props.BoolProperty(name="Показать секцию Nudge", default=False)
    nudge_step: bpy.props.FloatProperty(default=0.01, min=0.001, max=1.0)

    correspondences: bpy.props.CollectionProperty(
        type=PBA_VM_Correspondence)


classes = (PBA_VM_Correspondence, PBA_VM_Photo,
           PBA_VM_Props) + ops.classes + panel.classes


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pba_vm = bpy.props.PointerProperty(type=PBA_VM_Props)
    ops.register_overlay()


def unregister():
    ops.unregister_overlay()
    del bpy.types.Scene.pba_vm
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
