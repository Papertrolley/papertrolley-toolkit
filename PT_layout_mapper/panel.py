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
        row.operator("pba_lm.import_json", icon='IMPORT')
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

        row = col.row(align=True)
        op = row.operator("pba_lm.pick_image_points",
                          text="Pick 4 Points", icon='MOUSE_LMB')
        row.operator("pba_lm.undo_last_click", text="", icon='LOOP_BACK')

        row = box.row(align=True)
        sub = row.row(align=True)
        sub.enabled = (n3d == 4 and npx == 4)
        sub.operator("pba_lm.commit_quad", text="Commit",
                     icon='CHECKMARK')
        row.operator("pba_lm.cancel_quad", text="Cancel", icon='X')

        # Snap options
        col = box.column(align=True)
        col.prop(pba, "snap_to_lines")
        sub = col.row(align=True)
        sub.enabled = pba.snap_to_lines
        sub.prop(pba, "snap_radius_px")
        sub.prop(pba, "snap_threshold")
        sub2 = col.row()
        sub2.enabled = pba.snap_to_lines
        sub2.prop(pba, "snap_marker_tolerance")

        # Quads list
        box = layout.box()
        box.label(text=f"Quads ({len(pba.quads)})", icon='MESH_GRID')
        box.prop(pba, "highlight_size", text="Размер маркера, м")

        if pba.quads:
            for i, q in enumerate(pba.quads):
                row = box.row(align=True)
                active = (i == pba.active_quad_index)
                icon = 'RADIOBUT_ON' if active else 'RADIOBUT_OFF'
                label = f"{q.id}: {q.part or '(no part)'}"

                op_act = row.operator("pba_lm.set_active_quad",
                                       text=label, icon=icon,
                                       emboss=active)
                op_act.index = i

                op = row.operator("pba_lm.rotate_quad", text="↺",
                                  icon='LOOP_BACK')
                op.index = i
                op.direction = -1

                op = row.operator("pba_lm.rotate_quad", text="↻",
                                  icon='LOOP_FORWARDS')
                op.index = i
                op.direction = 1

                op = row.operator("pba_lm.flip_quad", text="⇄",
                                  icon='ARROW_LEFTRIGHT')
                op.index = i

                op = row.operator("pba_lm.remove_quad", text="", icon='X')
                op.index = i

        row = box.row(align=True)
        row.operator("pba_lm.refresh_highlight", text="Refresh",
                     icon='FILE_REFRESH')
        row.operator("pba_lm.validate_quads", text="Validate",
                     icon='CHECKMARK')

        row = box.row(align=True)
        row.operator("pba_lm.clear_quads", text="Clear All", icon='TRASH')
        row.operator("pba_lm.export_json", text="Export", icon='EXPORT')

        # Status
        box = layout.box()
        box.label(text="Status", icon='INFO')
        box.label(text=pba.status_msg or "—")


classes = (PBA_LM_PT_main,)
