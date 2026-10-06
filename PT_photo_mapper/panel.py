import bpy


class PBA_VM_PT_main(bpy.types.Panel):
    bl_label = "PT Photo"
    bl_idname = "PT_PM_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PT Mapper"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        pba = scene.pba_vm

        box = layout.box()
        box.label(text="Photo", icon='IMAGE_DATA')
        box.prop(pba, "photo_path", text="")

        box = layout.box()
        box.label(text="Camera Start", icon='CAMERA_DATA')
        box.prop(pba, "camera_position", text="")

        col = layout.column(align=True)
        col.operator("pba_vm.load_photo", icon='CAMERA_DATA')

        op = col.operator("pba_vm.save_vertices", icon='EXPORT')
        op.check_visibility = pba.visibility_check

        box = layout.box()
        box.label(text="Visibility", icon='HIDE_OFF')
        box.prop(pba, "visibility_check")

        # Correspondences
        box = layout.box()
        box.label(text=f"Correspondences ({len(pba.correspondences)})",
                  icon='TRACKER')

        row = box.row(align=True)
        row.label(text="3D: " + ("✓" if pba.pending_3d_set else "—"))
        row.label(text="2D: " + ("✓" if pba.pending_2d_set else "—"))

        col = box.column(align=True)
        col.operator("pba_vm.pick_3d", icon='VERTEXSEL')
        col.operator("pba_vm.pick_2d", icon='MOUSE_LMB')

        if pba.correspondences:
            box.separator()
            for i, c in enumerate(pba.correspondences):
                row = box.row(align=True)
                # Цвет маркера
                if c.last_error < 0:
                    icon = 'RADIOBUT_OFF'
                elif c.last_error < 5:
                    icon = 'CHECKMARK'  # зелёный
                elif c.last_error < 20:
                    icon = 'INFO'       # жёлтый
                else:
                    icon = 'ERROR'      # красный

                label = f"{i+1}. {c.obj_name} v{c.vertex_index} → " \
                        f"({c.x2d[0]:.0f}, {c.x2d[1]:.0f})"
                row.label(text=label, icon=icon)
                if c.last_error >= 0:
                    row.label(text=f"{c.last_error:.1f}px")
                op = row.operator("pba_vm.remove_pair", text="", icon='X')
                op.index = i

        row = box.row(align=True)
        row.operator("pba_vm.clear_pairs", text="Clear All", icon='TRASH')
        row.operator("pba_vm.solve_camera", text="Solve", icon='CAMERA_DATA')
        row = box.row(align=True)
        row.operator("pba_vm.diagnose_pairs", text="Diagnose", icon='VIEWZOOM')
        row.operator("pba_vm.remove_outliers", text="Remove Outliers",
                     icon='FILTER')
        row = box.row(align=True)
        row.operator("pba_vm.plot_residuals", text="Plot Residuals",
                     icon='IMAGE_ZDEPTH')


        box = layout.box()
        box.label(text="Status", icon='INFO')
        box.label(text=pba.status_msg or "—")


classes = (PBA_VM_PT_main,)
