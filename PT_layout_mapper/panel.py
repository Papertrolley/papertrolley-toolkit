import bpy


class PBA_LM_PT_main(bpy.types.Panel):
    bl_label = "PT Layout"
    bl_idname = "PT_LM_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PT Mapper"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        pba = scene.pba_lm

        # Layout
        box = layout.box()
        box.label(text="Layout", icon='IMAGE_DATA')
        box.prop(pba, "layout_path", text="")
        row = box.row(align=True)
        row.prop(pba, "plane_width_m", text="Ширина, м")
        row.prop(pba, "plane_offset_m", text="Отступ, м")
        row = box.row(align=True)
        row.operator("pba_lm.load_layout", icon='FILE_NEW')
        row.operator("pba_lm.frame_layout", icon='VIEW_CAMERA')

        # Current quad
        box = layout.box()
        box.label(text="Current Quad", icon='MESH_GRID')
        n3d = len(pba.pending_corners_3d)
        npx = pba.pending_px_count
        box.label(text=f"3D: {n3d}/4,  Image: {npx}/4")
        box.prop(pba, "pending_part", text="Part")

        col = box.column(align=True)
        col.operator("pba_lm.capture_3d", icon='VERTEXSEL')
        col.operator("pba_lm.pick_image_points", icon='MOUSE_LMB')
        box.operator("pba_lm.cancel_quad", icon='X')

        # Quads list
        box = layout.box()
        box.label(text=f"Quads ({len(pba.quads)})", icon='MESH_GRID')
        if pba.quads:
            for i, q in enumerate(pba.quads):
                row = box.row(align=True)
                label = f"{q.id}: {q.part or '(no part)'}"
                row.label(text=label)
                op = row.operator("pba_lm.remove_quad", text="", icon='X')
                op.index = i

        row = box.row(align=True)
        row.operator("pba_lm.clear_quads", text="Clear All", icon='TRASH')
        row.operator("pba_lm.export_json", text="Export", icon='EXPORT')

        # Status
        box = layout.box()
        box.label(text="Status", icon='INFO')
        box.label(text=pba.status_msg or "—")


classes = (PBA_LM_PT_main,)
