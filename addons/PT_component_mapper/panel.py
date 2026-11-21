import bpy
import os
from .ops import _list_uv_maps, _parse_uv_name


class PBA_CM_PT_main(bpy.types.Panel):
    bl_label = "PT Component Mapper"
    bl_idname = "PT_CM_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PT Component"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        pba = scene.pba_cm

        # Component
        box = layout.box()
        box.label(text="Component", icon='FILE_BLEND')
        if pba.loaded_component_path:
            name = os.path.basename(pba.loaded_component_path)
            comp_id = os.path.basename(
                os.path.dirname(pba.loaded_component_path))
            box.label(text=name, icon='CHECKMARK')
            box.label(text=f"ID: {comp_id}", icon='INFO')
        else:
            box.label(text="Не загружен", icon='INFO')
        box.operator("pba_cm.load_component",
                     text="Load mesh.blend...", icon='FILE_FOLDER')

        # Save Component
        box = layout.box()
        box.label(text="Save", icon='FILE_BLEND')
        box.operator("pba_cm.save_component",
                     text="Save .blend only", icon='FILE_TICK')
        box.label(text="Save Piece = .blend + PNG + JSON", icon='INFO')
        if bpy.data.filepath:
            box.label(text=os.path.basename(bpy.data.filepath),
                      icon='CHECKMARK')

        # UV Maps
        uv_maps = _list_uv_maps(scene)
        if not uv_maps:
            box = layout.box()
            box.label(text="Нет UV-слоёв в сцене", icon='INFO')
        else:
            box = layout.box()
            box.label(text=f"UV Maps ({len(uv_maps)})", icon='UV')
            for name in uv_maps:
                active = (name == pba.active_uv_map)
                icon = 'RADIOBUT_ON' if active else 'RADIOBUT_OFF'
                parsed = _parse_uv_name(name)
                if parsed:
                    piece, scale, dpi = parsed
                    label = f"{name}  →  {piece}, 1:{scale}, {dpi}dpi"
                else:
                    label = f"{name}  (нет шаблона)"
                row = box.row(align=True)
                op = row.operator("pba_cm.set_uv_map",
                                  text=label, icon=icon, emboss=active)
                op.uv_map_name = name
                op_del = row.operator("pba_cm.delete_uv_layer",
                                       text="", icon='X')
                op_del.uv_layer_name = name

        # Управление UV-слоями
        box = layout.box()
        box.label(text="UV Layer Management", icon='TOOL_SETTINGS')
        row = box.row(align=True)
        row.prop(pba, "new_uv_layer_name", text="")
        row.operator("pba_cm.create_uv_layer", text="", icon='ADD')
        box.operator("pba_cm.validate_uv_layers",
                     text="Validate UV Layers", icon='CHECKMARK')

        # Sidecar info
        if pba.loaded_piece_json:
            box = layout.box()
            box.label(text="Loaded Sidecar", icon='FILE_SCRIPT')
            box.label(text=pba.loaded_piece_summary)

        # Save Piece
        box = layout.box()
        box.label(text="Save Piece", icon='EXPORT')
        box.prop(pba, "piece_name", text="Name")
        row = box.row(align=True)
        row.prop(pba, "scale_str", text="Scale")
        row.prop(pba, "dpi", text="DPI")
        box.prop(pba, "margin_px", text="Margin (px)")
        box.operator("pba_cm.save_piece", icon='FILE_TICK',
                     text="Save Piece...")

        # Status
        box = layout.box()
        box.label(text="Status", icon='INFO')
        for line in (pba.status_msg or "—").split("\n")[-3:]:
            box.label(text=line)


classes = (PBA_CM_PT_main,)
