bl_info = {
    "name": "PT Photo Mapper",
    "author": "PaperTrolley",
    "version": (0, 2, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > PT Photo",
    "description": "Загружает фото, настраивает камеру, сохраняет "
                   "координаты вершин в CSV",
    "category": "Modeling",
}

import bpy

from . import ops, panel


class PBA_VM_Correspondence(bpy.types.PropertyGroup):
    obj_name: bpy.props.StringProperty(default="")
    vertex_index: bpy.props.IntProperty(default=-1)
    x3d: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    x2d: bpy.props.FloatVectorProperty(size=2, default=(0.0, 0.0))
    last_error: bpy.props.FloatProperty(default=-1.0)


class PBA_VM_Props(bpy.types.PropertyGroup):
    photo_path: bpy.props.StringProperty(subtype='FILE_PATH')
    camera_position: bpy.props.EnumProperty(
        items=[
            ('FRONT', "Front (+X)", ""),
            ('BACK',  "Back (-X)",  ""),
            ('LEFT', "Left (+Y)", ""),
            ('RIGHT',  "Right (-Y)",  ""),
            ('TOP',   "Top (+Z)",   ""),
        ],
        default='FRONT')
    visibility_check: bpy.props.BoolProperty(default=True)
    status_msg: bpy.props.StringProperty(default="")

    # Соответствия
    correspondences: bpy.props.CollectionProperty(
        type=PBA_VM_Correspondence)
    pending_x3d: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    pending_x2d: bpy.props.FloatVectorProperty(size=2, default=(0.0, 0.0))
    pending_3d_obj: bpy.props.StringProperty(default="")
    pending_3d_vertex: bpy.props.IntProperty(default=-1)
    pending_3d_set: bpy.props.BoolProperty(default=False)
    pending_2d_set: bpy.props.BoolProperty(default=False)


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
