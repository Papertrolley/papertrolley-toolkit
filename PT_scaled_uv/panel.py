import bpy


class PBA_UVE_PT_main(bpy.types.Panel):
    bl_label = "PT Scaled UV"
    bl_idname = "PT_UVE_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PT Mapper"

    def draw(self, context):
        layout = self.layout
        pba = context.scene.pba_uv_exporter

        box = layout.box()
        box.label(text="Scale", icon='DRIVER_DISTANCE')
        row = box.row(align=True)
        row.prop(pba, "scale_num", text="1 :")
        row.prop(pba, "scale_den", text="")

        box = layout.box()
        box.label(text="Output", icon='IMAGE_DATA')
        box.prop(pba, "dpi")
        box.prop(pba, "margin_mm", text="Margin, mm")
        box.prop(pba, "gap_mm", text="Gap, mm")
        box.prop(pba, "wrap_width_px", text="Wrap width, px")
        box.prop(pba, "min_object_px", text="Min object, px")
        box.separator()
        box.prop(pba, "debug_labels")
        box.prop(pba, "per_object_pngs")

        box = layout.box()
        box.label(text="Lines", icon='GREASEPENCIL')
        box.prop(pba, "line_width_px")
        box.prop(pba, "draw_line_color", text="")

        box = layout.box()
        box.label(text="Vertex Markers", icon='TRACKER')
        box.prop(pba, "marker_color", text="")
        box.prop(pba, "marker_size_px")

        box = layout.box()
        box.label(text="Save", icon='FILE')
        box.prop(pba, "output_path", text="")
        box.operator("pba_uv_export.export", icon='EXPORT')

        box = layout.box()
        box.label(text="Status", icon='INFO')
        box.label(text=pba.status_msg or "—")


classes = (PBA_UVE_PT_main,)
