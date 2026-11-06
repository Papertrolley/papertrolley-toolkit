import bpy
import json
import os
from mathutils import Vector

PBA_VERTEX_ID_ATTR = "pba_vertex_id"
PBA_INSTANCE_ID_PROP = "pba_instance_id"
PBA_ORIGINAL_NAME_PROP = "pba_original_name"
PBA_COMPONENT_ID_PROP = "pba_component_id"

MESH_FILE_NAME = "mesh.blend"


# ====================== Property groups ======================

class PBA_AM_Instance(bpy.types.PropertyGroup):
    instance_id: bpy.props.StringProperty()
    component_id: bpy.props.StringProperty()
    component_path: bpy.props.StringProperty()
    collection_name: bpy.props.StringProperty()
    position: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    rotation: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))


class PBA_AM_Props(bpy.types.PropertyGroup):
    component_id: bpy.props.StringProperty(name="Component ID")
    components_dir: bpy.props.StringProperty(
        name="Components Dir", subtype='DIR_PATH')
    new_instance_id: bpy.props.StringProperty(
        name="Instance ID (пусто = авто)")
    instances: bpy.props.CollectionProperty(type=PBA_AM_Instance)
    active_instance_index: bpy.props.IntProperty(default=-1)
    status_msg: bpy.props.StringProperty(default="")


def _set_status(scene, msg):
    scene.pba_am.status_msg = msg
    print(f"[PBA AM] {msg}")


# ====================== Утилиты ======================

def _ensure_object_mode(context):
    if context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass


def _check_view3d_context(context):
    return (context.area is not None
            and context.area.type == 'VIEW_3D')


def _current_blend_path():
    return bpy.data.filepath or ""


def _current_assembly_id():
    bp = _current_blend_path()
    if not bp:
        return ""
    return os.path.splitext(os.path.basename(bp))[0]


def _assembly_path():
    bp = _current_blend_path()
    if not bp:
        return None
    base = os.path.splitext(os.path.basename(bp))[0]
    return os.path.join(os.path.dirname(bp), f"{base}.assembly.json")


def _ensure_local_uuids(obj):
    me = obj.data
    attr = me.attributes.get(PBA_VERTEX_ID_ATTR)
    if attr is None:
        attr = me.attributes.new(
            name=PBA_VERTEX_ID_ATTR, type='INT', domain='POINT')
        for i in range(len(me.vertices)):
            attr.data[i].value = i + 1
        return
    max_id = 0
    for i in range(len(attr.data)):
        v = int(attr.data[i].value)
        if v > max_id:
            max_id = v
    next_id = max_id + 1
    seen = set()
    for i in range(len(attr.data)):
        v = int(attr.data[i].value)
        if v > 0 and v not in seen:
            seen.add(v)
            continue
        attr.data[i].value = next_id
        seen.add(next_id)
        next_id += 1


def get_vertex_uuid(obj, vi):
    attr = obj.data.attributes.get(PBA_VERTEX_ID_ATTR)
    if attr is None or vi < 0 or vi >= len(attr.data):
        return 0
    return int(attr.data[vi].value)


def get_vertex_key(obj, vi):
    iid = obj.get(PBA_INSTANCE_ID_PROP, "")
    orig = obj.get(PBA_ORIGINAL_NAME_PROP, obj.name)
    uuid = get_vertex_uuid(obj, vi)
    return (iid, orig, uuid)


def find_vertex_by_key(instance_id, orig_name, local_uuid):
    if instance_id:
        coll = bpy.data.collections.get(f"Instance_{instance_id}")
        if coll is None:
            return (None, -1)
        obj = next((o for o in coll.objects
                    if o.get(PBA_ORIGINAL_NAME_PROP) == orig_name), None)
    else:
        obj = bpy.data.objects.get(orig_name)
    if obj is None or obj.type != 'MESH':
        return (None, -1)
    attr = obj.data.attributes.get(PBA_VERTEX_ID_ATTR)
    if attr is None or len(attr.data) == 0:
        return (None, -1)
    for i in range(len(attr.data)):
        if int(attr.data[i].value) == local_uuid:
            return (obj, i)
    return (None, -1)


def _save_objects_to_blend(objects, filepath):
    datablocks = set()
    for obj in objects:
        if obj.type != 'MESH':
            continue
        datablocks.add(obj)
        try:
            obj.use_fake_user = True
        except Exception:
            pass
        if obj.data is not None:
            datablocks.add(obj.data)
            try:
                obj.data.use_fake_user = True
                obj.data.update()
            except Exception:
                pass
            for slot in obj.material_slots:
                if slot.material is not None:
                    datablocks.add(slot.material)
                    try:
                        slot.material.use_fake_user = True
                    except Exception:
                        pass
    if not datablocks:
        raise ValueError("Нет mesh-объектов для сохранения")
    bpy.data.libraries.write(filepath, datablocks, fake_user=True)
    size = os.path.getsize(filepath)
    print(f"[PBA AM] Written: {filepath} ({size} bytes)")


def _load_objects_from_blend(filepath):
    with bpy.data.libraries.load(filepath, link=False) as (data_from, data_to):
        names = list(data_from.objects)
        data_to.objects = names
    return [o for o in data_to.objects if o is not None]


def _clear_pba_props(obj):
    snapshot = {}
    for key in (PBA_INSTANCE_ID_PROP, PBA_ORIGINAL_NAME_PROP,
                PBA_COMPONENT_ID_PROP):
        snapshot[key] = obj.get(key, None)
        if key in obj:
            del obj[key]
    return snapshot


def _restore_pba_props(obj, snapshot):
    for key, val in snapshot.items():
        if val is not None:
            obj[key] = val


def _delete_collection_and_objects(coll_name):
    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        return
    for obj in list(coll.objects):
        mesh = obj.data if obj.type == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    bpy.data.collections.remove(coll)


def _instantiate_component(scene, iid, cid, comp_path,
                            target_loc, target_rot):
    mesh_path = os.path.join(comp_path, MESH_FILE_NAME)
    if not os.path.exists(mesh_path):
        return None, f"нет {MESH_FILE_NAME} в {comp_path}"

    try:
        new_objects = _load_objects_from_blend(mesh_path)
    except Exception as e:
        return None, f"ошибка загрузки: {e}"

    mesh_objects = [o for o in new_objects if o.type == 'MESH']
    if not mesh_objects:
        for o in new_objects:
            bpy.data.objects.remove(o, do_unlink=True)
        return None, "нет mesh-объектов в файле"

    coll_name = f"Instance_{iid}"
    coll = bpy.data.collections.new(coll_name)
    scene.collection.children.link(coll)

    target_loc = Vector(target_loc)
    target_rot = tuple(target_rot)

    top_objs = [o for o in mesh_objects if o.parent is None]
    orig_anchor = top_objs[0].location.copy() if top_objs else Vector((0, 0, 0))
    offset = target_loc - orig_anchor

    for obj in mesh_objects:
        orig_name = obj.name
        new_name = f"{iid}__{orig_name}"
        obj.name = new_name
        if obj.data is not None:
            obj.data.name = new_name

        for c in list(obj.users_collection):
            try:
                c.objects.unlink(obj)
            except Exception:
                pass
        coll.objects.link(obj)

        obj[PBA_INSTANCE_ID_PROP] = iid
        obj[PBA_ORIGINAL_NAME_PROP] = orig_name
        obj[PBA_COMPONENT_ID_PROP] = cid

        if obj.parent is None:
            obj.location = obj.location + offset
            obj.rotation_euler = target_rot

    for obj in new_objects:
        if obj.type != 'MESH':
            bpy.data.objects.remove(obj, do_unlink=True)

    return coll_name, None


def _try_autosave(scene):
    """Автосохранение — только если .blend уже сохранён."""
    if not _current_blend_path():
        print("[PBA AM] .blend не сохранён — assembly.json "
              "не будет создан автоматически")
        return
    try:
        bpy.ops.pba_am.save_assembly()
    except Exception as e:
        print(f"[PBA AM] autosave failed: {e}")


# ====================== Create Component ======================

class PBA_AM_OT_create_component(bpy.types.Operator):
    bl_idname = "pba_am.create_component"
    bl_label = "Create Component from Selection"
    bl_description = "Сохранить выделенные объекты в components/<id>/mesh.blend"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am
        _ensure_object_mode(context)
        if not _check_view3d_context(context):
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        cid = pba.component_id.strip()
        if not cid:
            self.report({'ERROR'}, "Укажите Component ID")
            return {'CANCELLED'}
        cdir = bpy.path.abspath(pba.components_dir)
        if not cdir:
            self.report({'ERROR'}, "Укажите Components Dir")
            return {'CANCELLED'}

        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not objs:
            self.report({'ERROR'}, "Выделите mesh-объекты")
            return {'CANCELLED'}

        for obj in objs:
            if obj.modifiers:
                mods = [m.type for m in obj.modifiers]
                print(f"[PBA AM] ВНИМАНИЕ: {obj.name} имеет "
                      f"модификаторы {mods}")

        comp_path = os.path.join(cdir, cid)
        os.makedirs(comp_path, exist_ok=True)

        for obj in objs:
            _ensure_local_uuids(obj)

        snapshots = [(obj, _clear_pba_props(obj)) for obj in objs]
        try:
            mesh_path = os.path.join(comp_path, MESH_FILE_NAME)
            _save_objects_to_blend(objs, mesh_path)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка: {e}")
            return {'CANCELLED'}
        finally:
            for obj, snap in snapshots:
                _restore_pba_props(obj, snap)

        _set_status(scene, f"Компонент создан: {cid} ({len(objs)} объектов)")
        return {'FINISHED'}


# ====================== Import Component ======================

class PBA_AM_OT_import_component(bpy.types.Operator):
    bl_idname = "pba_am.import_component"
    bl_label = "Import Component"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(
        default="*.blend", options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am
        _ensure_object_mode(context)
        if not _check_view3d_context(context):
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        blend_path = bpy.path.abspath(self.filepath)
        if not blend_path or not os.path.exists(blend_path):
            self.report({'ERROR'}, "mesh.blend не найден")
            return {'CANCELLED'}

        comp_path = os.path.dirname(blend_path)
        cid = os.path.basename(os.path.normpath(comp_path))
        if not cid:
            self.report({'ERROR'}, "Не удалось определить Component ID")
            return {'CANCELLED'}

        existing = {i.instance_id for i in pba.instances}
        iid = pba.new_instance_id.strip()
        if not iid:
            iid = cid
            if iid in existing:
                n = 2
                while f"{cid}_{n}" in existing:
                    n += 1
                iid = f"{cid}_{n}"
        elif iid in existing:
            self.report({'ERROR'}, f"Instance ID '{iid}' уже используется")
            return {'CANCELLED'}

        target_loc = scene.cursor.location.copy()
        coll_name, err = _instantiate_component(
            scene, iid, cid, comp_path, target_loc, (0, 0, 0))
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}

        inst = pba.instances.add()
        inst.instance_id = iid
        inst.component_id = cid
        inst.component_path = comp_path
        inst.collection_name = coll_name
        inst.position = tuple(target_loc)
        inst.rotation = (0.0, 0.0, 0.0)
        pba.active_instance_index = len(pba.instances) - 1

        _try_autosave(scene)
        _set_status(scene, f"Импортирован: {iid}")
        return {'FINISHED'}


# ====================== Remove / Select ======================

class PBA_AM_OT_remove_instance(bpy.types.Operator):
    bl_idname = "pba_am.remove_instance"
    bl_label = "Remove Instance"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am
        if not (0 <= self.index < len(pba.instances)):
            return {'CANCELLED'}
        inst = pba.instances[self.index]
        _delete_collection_and_objects(inst.collection_name)
        iid = inst.instance_id
        pba.instances.remove(self.index)
        pba.active_instance_index = min(self.index,
                                        len(pba.instances) - 1)
        _try_autosave(scene)
        _set_status(scene, f"Удалён: {iid}")
        return {'FINISHED'}


class PBA_AM_OT_select_instance(bpy.types.Operator):
    bl_idname = "pba_am.select_instance"
    bl_label = "Select Instance in Scene"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am
        if not (0 <= self.index < len(pba.instances)):
            return {'CANCELLED'}
        inst = pba.instances[self.index]
        pba.active_instance_index = self.index

        coll = bpy.data.collections.get(inst.collection_name)
        if coll is None:
            self.report({'WARNING'},
                        f"Коллекция '{inst.collection_name}' не в сцене")
            return {'CANCELLED'}

        for obj in context.view_layer.objects:
            obj.select_set(False)

        first = None
        for obj in coll.objects:
            try:
                obj.select_set(True)
                if first is None:
                    first = obj
            except Exception:
                pass

        if first is not None:
            context.view_layer.objects.active = first

        try:
            bpy.ops.view3d.view_selected()
        except Exception:
            pass

        _set_status(scene, f"Выделен: {inst.instance_id}")
        return {'FINISHED'}


# ====================== Save / Load Assembly ======================

class PBA_AM_OT_save_assembly(bpy.types.Operator):
    bl_idname = "pba_am.save_assembly"
    bl_label = "Save Assembly"
    bl_description = "Сохранить текущую сцену в <blend>.assembly.json"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am

        if not _current_blend_path():
            self.report({'ERROR'},
                        "Сохраните .blend файл сначала "
                        "(File → Save As). assembly.json будет "
                        "создан рядом с ним.")
            return {'CANCELLED'}

        path = _assembly_path()

        components_data = []
        instance_objects = set()

        for inst in pba.instances:
            coll = bpy.data.collections.get(inst.collection_name)
            position = list(inst.position)
            rotation = list(inst.rotation)

            if coll is not None:
                instance_objects.update(coll.objects)
                first = next(
                    (o for o in coll.objects if o.parent is None), None)
                if first is not None:
                    position = list(first.location)
                    rotation = list(first.rotation_euler)
                    inst.position = tuple(position)
                    inst.rotation = tuple(rotation)

            components_data.append({
                "instance_id": inst.instance_id,
                "component_id": inst.component_id,
                "component_path": inst.component_path,
                "collection_name": inst.collection_name,
                "position": position,
                "rotation": rotation,
            })

        local_objects = [
            o.name for o in scene.objects
            if o.type == 'MESH' and o not in instance_objects
        ]

        data = {
            "assembly_id": _current_assembly_id(),
            "blend_file": os.path.basename(_current_blend_path()),
            "components": components_data,
            "local_objects": local_objects,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        _set_status(scene,
                    f"Сохранено: {os.path.basename(path)} "
                    f"({len(components_data)} компонентов)")
        return {'FINISHED'}


class PBA_AM_OT_load_assembly(bpy.types.Operator):
    bl_idname = "pba_am.load_assembly"
    bl_label = "Load Assembly"
    bl_description = ("Загрузить <blend>.assembly.json и восстановить сцену")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am
        _ensure_object_mode(context)
        if not _check_view3d_context(context):
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        if not _current_blend_path():
            self.report({'ERROR'},
                        "Сохраните .blend файл сначала (File → Save As). "
                        "assembly.json ищется рядом с ним.")
            return {'CANCELLED'}

        path = _assembly_path()
        if not path or not os.path.exists(path):
            self.report({'ERROR'},
                        f"assembly.json не найден: {os.path.basename(path)}")
            return {'CANCELLED'}

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка чтения: {e}")
            return {'CANCELLED'}

        for inst in list(pba.instances):
            _delete_collection_and_objects(inst.collection_name)

        pba.instances.clear()
        pba.active_instance_index = -1

        succeeded = 0
        failed = []

        for cd in data.get("components", []):
            iid = cd.get("instance_id", "")
            cid = cd.get("component_id", "")
            comp_path = cd.get("component_path", "")
            pos = cd.get("position", [0.0, 0.0, 0.0])
            rot = cd.get("rotation", [0.0, 0.0, 0.0])

            inst = pba.instances.add()
            inst.instance_id = iid
            inst.component_id = cid
            inst.component_path = comp_path
            inst.collection_name = f"Instance_{iid}"
            inst.position = tuple(float(x) for x in pos[:3])
            inst.rotation = tuple(float(x) for x in rot[:3])

            if not comp_path or not os.path.isdir(comp_path):
                failed.append(f"{iid}: папка не найдена: {comp_path}")
                continue

            coll_name, err = _instantiate_component(
                scene, iid, cid, comp_path, pos, rot)
            if err:
                failed.append(f"{iid}: {err}")
                continue

            inst.collection_name = coll_name
            succeeded += 1

        if pba.instances:
            pba.active_instance_index = 0

        msg = f"Восстановлено: {succeeded} из {len(pba.instances)}"
        if failed:
            msg += f", ошибок: {len(failed)}"
            for e in failed:
                print(f"[PBA AM] ОШИБКА: {e}")
        _set_status(scene, msg)
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ====================== Validate ======================

class PBA_AM_OT_validate(bpy.types.Operator):
    bl_idname = "pba_am.validate"
    bl_label = "Validate Assembly"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am
        problems = []

        seen_iids = set()
        for inst in pba.instances:
            if not inst.instance_id:
                problems.append("Пустой instance_id")
                continue
            if inst.instance_id in seen_iids:
                problems.append(f"Дубликат instance_id: {inst.instance_id}")
            seen_iids.add(inst.instance_id)

            coll = bpy.data.collections.get(inst.collection_name)
            if coll is None:
                problems.append(f"{inst.instance_id}: коллекция не в сцене")
                continue
            if len(coll.objects) == 0:
                problems.append(f"{inst.instance_id}: коллекция пуста")

            if not inst.component_path:
                problems.append(f"{inst.instance_id}: component_path пуст")
                continue
            mesh_path = os.path.join(inst.component_path, MESH_FILE_NAME)
            if not os.path.exists(mesh_path):
                problems.append(
                    f"{inst.instance_id}: нет {MESH_FILE_NAME}")

            for obj in coll.objects:
                if obj.type != 'MESH':
                    continue
                attr = obj.data.attributes.get(PBA_VERTEX_ID_ATTR)
                if attr is None:
                    problems.append(
                        f"{inst.instance_id}/{obj.name}: нет UUID")
                    continue
                seen = set()
                for i in range(len(attr.data)):
                    v = int(attr.data[i].value)
                    if v <= 0:
                        problems.append(
                            f"{inst.instance_id}/{obj.name}: UUID={v}")
                        break
                    if v in seen:
                        problems.append(
                            f"{inst.instance_id}/{obj.name}: "
                            f"дубликат UUID={v}")
                        break
                    seen.add(v)

        if problems:
            for p in problems:
                print(f"[PBA AM] {p}")
            _set_status(scene, f"Проблем: {len(problems)} (см. консоль)")
            self.report({'WARNING'}, f"Проблем: {len(problems)}")
        else:
            _set_status(scene, f"OK: {len(pba.instances)} компонентов")
            self.report({'INFO'}, "Валидация пройдена")
        return {'FINISHED'}


classes = (
    PBA_AM_Instance,
    PBA_AM_Props,
    PBA_AM_OT_create_component,
    PBA_AM_OT_import_component,
    PBA_AM_OT_remove_instance,
    PBA_AM_OT_select_instance,
    PBA_AM_OT_save_assembly,
    PBA_AM_OT_load_assembly,
    PBA_AM_OT_validate,
)
