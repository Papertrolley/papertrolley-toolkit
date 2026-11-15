bl_info = {
    "name": "PT Component Mapper",
    "author": "PaperTrolley",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > PBA Component Mapper",
    "description": "Экспорт UV-раскладок компонента в PNG",
    "category": "Modeling",
}

import bpy
from . import ops, panel

classes = ops.classes + panel.classes


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pba_cm = bpy.props.PointerProperty(type=ops.PBA_CM_Props)


def unregister():
    del bpy.types.Scene.pba_cm
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
