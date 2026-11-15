import bpy
import json
import os
from mathutils import Vector
import time as _pba_time

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
    component_mtime: bpy.props.FloatProperty(
        name="Component mtime", default=0.0,
        description="mtime mesh.blend на момент импорта. 0.0 = не задан (старые сборки)")
    has_local_changes: bpy.props.BoolProperty(
        name="Has Local Changes", default=False,
        description="Меш instance изменён после последнего Import/Reload/Publish")
    mesh_fingerprint: bpy.props.StringProperty(
        default="", description="Хэш геометрии на момент Import/Reload/Publish")


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


def _collect_uuid_sets(coll):
    """Собрать {orig_name: frozenset(uuids)} для mesh-объектов коллекции.
    Читает obj.data.attributes — вызывать только в Object Mode."""
    result = {}
    if coll is None:
        return result
    for obj in coll.objects:
        if obj.type != 'MESH' or obj.data is None:
            continue
        orig = obj.get(PBA_ORIGINAL_NAME_PROP)
        if not orig:
            continue
        attr = obj.data.attributes.get(PBA_VERTEX_ID_ATTR)
        if attr is None:
            result[orig] = frozenset()
            continue
        result[orig] = frozenset(
            int(attr.data[i].value) for i in range(len(attr.data)))
    return result


def _compare_uuid_sets(old, new):
    """Список предупреждений о несовпадении UUID-наборов."""
    warnings = []
    for orig, old_set in old.items():
        if orig not in new:
            warnings.append(f"{orig}: отсутствует в новой библиотеке")
            continue
        new_set = new[orig]
        if old_set != new_set:
            added = len(new_set - old_set)
            removed = len(old_set - new_set)
            warnings.append(
                f"{orig}: UUID изменились (+{added}/-{removed})")
    for orig in new:
        if orig not in old:
            warnings.append(f"{orig}: новый объект (не было раньше)")
    return warnings


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


def _find_collection_parent_and_index(scene, coll):
    """Найти (parent_collection, index) для coll в иерархии.
    Master Collection сцены не входит в bpy.data.collections, проверяем отдельно."""
    if coll is None:
        return None, -1
    if scene is not None and scene.collection is not None:
        for i, c in enumerate(scene.collection.children):
            if c == coll:
                return scene.collection, i
    for parent in bpy.data.collections:
        if parent == coll:
            continue
        for i, c in enumerate(parent.children):
            if c == coll:
                return parent, i
    return None, -1


def _move_collection_to(scene, coll, target_parent, target_index):
    """Переместить coll под target_parent на позицию target_index.
    CollectionChildren не поддерживает insert — перестраиваем порядок
    через unlink/link. Объекты коллекций при этом не теряются:
    у каждой child сохраняются её objects, пересобирается только
    список children у родителя."""
    if target_parent is None or coll is None:
        return

    current_parent, _ = _find_collection_parent_and_index(scene, coll)
    if current_parent is None:
        target_parent.children.link(coll)
    elif current_parent != target_parent:
        current_parent.children.unlink(coll)
        target_parent.children.link(coll)
    # после этого coll гарантированно в target_parent

    children = list(target_parent.children)
    if coll not in children:
        return
    current = children.index(coll)
    if current == target_index:
        return

    children.pop(current)
    if target_index < 0:
        target_index = 0
    target_index = min(target_index, len(children))
    children.insert(target_index, coll)

    for c in children:
        target_parent.children.unlink(c)
    for c in children:
        target_parent.children.link(c)


def _reload_instance(scene, inst):
    """Перезагрузить один instance из его mesh.blend.
    Возвращает (ok: bool, error: str|None, warnings: list[str])."""
    iid = inst.instance_id
    cid = inst.component_id
    comp_path = inst.component_path
    coll_name = inst.collection_name

    if not comp_path:
        return False, "нет component_path", []

    mesh_path = os.path.join(comp_path, MESH_FILE_NAME)
    if not os.path.exists(mesh_path):
        return False, f"mesh.blend не найден: {mesh_path}", []

    # Запоминаем позицию, UUID и место в иерархии до удаления
    position = list(inst.position)
    rotation = list(inst.rotation)
    coll = bpy.data.collections.get(coll_name)
    old_uuids = _collect_uuid_sets(coll)
    saved_parent, saved_index = _find_collection_parent_and_index(scene, coll)
    if coll is not None:
        first = next((o for o in coll.objects if o.parent is None), None)
        if first is not None:
            position = list(first.location)
            rotation = list(first.rotation_euler)

    siblings = [
        x.instance_id for x in scene.pba_am.instances
        if x.instance_id != iid and x.component_id == cid
    ]
    if siblings:
        print(f"[PBA AM] ВНИМАНИЕ: reload '{iid}' — остальные instance "
              f"этого компонента ({', '.join(siblings)}) останутся "
              f"на старой версии. Используйте Reload All для синхронизации.")

    _delete_collection_and_objects(coll_name)

    new_coll_name, err = _instantiate_component(
        scene, iid, cid, comp_path, position, rotation)
    if err:
        return False, err, []

    # Восстанавливаем позицию в иерархии коллекций
    new_coll = bpy.data.collections.get(new_coll_name)
    if new_coll is not None and saved_parent is not None:
        _move_collection_to(scene, new_coll, saved_parent, saved_index)

    new_coll = bpy.data.collections.get(new_coll_name)
    new_uuids = _collect_uuid_sets(new_coll)
    warnings = _compare_uuid_sets(old_uuids, new_uuids)

    inst.collection_name = new_coll_name
    inst.position = tuple(position)
    inst.rotation = tuple(rotation)
    try:
        inst.component_mtime = float(os.path.getmtime(mesh_path))
    except Exception:
        inst.component_mtime = 0.0

    new_coll_for_fp = bpy.data.collections.get(new_coll_name)
    inst.mesh_fingerprint = _instance_fingerprint(new_coll_for_fp)
    _pba_last_fp_check = 0.0
    return True, None, warnings


def _cleanup_publish_temp(temp_coll):
    """Удалить временную коллекцию Publish и её объекты/mesh-data."""
    if temp_coll is None:
        return
    clones = list(temp_coll.objects)
    bpy.data.collections.remove(temp_coll)
    for obj in clones:
        mesh = obj.data if obj.type == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)


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


_PBA_SUPPRESS_DIRTY = False


def _suppress_dirty_mark():
    global _PBA_SUPPRESS_DIRTY
    _PBA_SUPPRESS_DIRTY = True


def _resume_dirty_mark():
    global _PBA_SUPPRESS_DIRTY
    _PBA_SUPPRESS_DIRTY = False


_pba_last_fp_check = 0.0
_PBA_FP_THROTTLE = 0.4  # сек


def _on_depsgraph_update(scene, depsgraph):
    """Fingerprint-сравнение в depsgraph_update_post.

    Не полагаемся на is_updated_geometry — сравниваем хэш вершин
    с сохранённым. Throttle 0.4s, чтобы не тормозить viewport.
    Пустой fingerprint у instance = «не инициализирован» — при
    первом визите инициализируем без маркировки (апгрейд со старой
    версии / открытие файла с новым кодом).
    """
    global _pba_last_fp_check

    print(f"[PBA AM DEBUG] handler fired, suppress={_PBA_SUPPRESS_DIRTY}, "
          f"instances={len(getattr(getattr(scene, 'pba_am', None), 'instances', [])) if scene else 'no-scene'}")

    if _PBA_SUPPRESS_DIRTY:
        return
    if scene is None:
        scene = bpy.context.scene
    if scene is None:
        return
    pba = getattr(scene, "pba_am", None)
    if pba is None or len(pba.instances) == 0:
        return

    now = _pba_time.time()
    if now - _pba_last_fp_check < _PBA_FP_THROTTLE:
        return
    _pba_last_fp_check = now

    marked = False
    for inst in pba.instances:
        if inst.has_local_changes:
            continue
        coll = bpy.data.collections.get(inst.collection_name)
        if coll is None:
            continue
        fp = _instance_fingerprint(coll)
        if not fp:
            continue
        if not inst.mesh_fingerprint:
            inst.mesh_fingerprint = fp
            continue
        if fp != inst.mesh_fingerprint:
            inst.has_local_changes = True
            marked = True
            print(f"[PBA AM] Помечен как изменённый: {inst.instance_id}")

    if marked:
        _tag_redraw_viewports()


def _tag_redraw_viewports():
    """Принудительный redraw всех 3D Viewport'ов (панели в сайдбаре)."""
    try:
        wm = bpy.context.window_manager
        if wm is None:
            return
        for window in wm.windows:
            if window.screen is None:
                continue
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _instance_fingerprint(coll):
    """Хэш от позиций вершин всех mesh-объектов коллекции."""
    import hashlib
    h = hashlib.sha1()
    if coll is None:
        return ""
    for obj in sorted(coll.objects, key=lambda o: o.name):
        if obj.type != 'MESH' or obj.data is None:
            continue
        h.update(obj.name.encode('utf-8'))
        me = obj.data
        h.update(str(len(me.vertices)).encode('utf-8'))
        for v in me.vertices:
            co = v.co
            h.update(f"|{co.x:.5f},{co.y:.5f},{co.z:.5f}".encode('utf-8'))
    return h.hexdigest()[:16]


def _on_save_pre(*args):
    scene = bpy.context.scene
    pba = getattr(scene, "pba_am", None)
    if pba is None:
        return
    for inst in pba.instances:
        if inst.has_local_changes:
            continue
        coll = bpy.data.collections.get(inst.collection_name)
        if coll is None:
            continue
        fp = _instance_fingerprint(coll)
        if not fp:
            continue
        if not inst.mesh_fingerprint:
            inst.mesh_fingerprint = fp
            continue
        if fp != inst.mesh_fingerprint:
            inst.has_local_changes = True
            print(f"[PBA AM] Помечен при save_pre: {inst.instance_id}")


def _on_load_post(*args):
    scene = bpy.context.scene
    pba = getattr(scene, "pba_am", None)
    if pba is None:
        return
    for inst in pba.instances:
        coll = bpy.data.collections.get(inst.collection_name)
        if coll is None:
            continue
        fp = _instance_fingerprint(coll)
        if fp and not inst.mesh_fingerprint:
            inst.mesh_fingerprint = fp


# Маркеры для cleanup в __init__.py
_on_depsgraph_update._pba_am_handler = True
_on_save_pre._pba_am_handler = True
_on_load_post._pba_am_handler = True


def _handlers_watchdog():
    """Переживает холодный старт extensions: Blender 5.0 сбрасывает
    bpy.app.handlers между первым register() и синком extension-репозиториев.
    Таймер периодически проверяет хендлеры и возвращает пропавшие.
    Останавливается при выключении аддона.
    Возвращает интервал до следующего тика или None для остановки.
    """
    try:
        pkg = __package__
        if pkg and bpy.context.preferences:
            if pkg not in bpy.context.preferences.addons:
                return None  # аддон выключен — стоим
    except Exception:
        pass

    needed = [
        (bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
        (bpy.app.handlers.save_pre, _on_save_pre),
        (bpy.app.handlers.load_post, _on_load_post),
    ]
    for lst, fn in needed:
        if not any(getattr(h, "_pba_am_handler", False) for h in lst):
            lst.append(fn)
            print(f"[PBA AM] watchdog: re-added {fn.__name__}")
    return 2.0

# ====================== Create Component ======================

class PBA_AM_OT_create_component(bpy.types.Operator):
    bl_idname = "pba_am.create_component"
    bl_label = "Create Component from Selection"
    bl_description = ("Сохранить выделенные объекты в components/<id>/mesh.blend "
                      "и сразу импортировать как instance, заменив выделение")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am

        _ensure_object_mode(context)
        if context.mode != 'OBJECT':
            self.report({'ERROR'},
                        "Не удалось выйти из Edit Mode — выйдите вручную")
            return {'CANCELLED'}
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

        # instance_id: ручной (если задан) или авто = cid с суффиксом
        existing = {i.instance_id for i in pba.instances}
        iid = pba.new_instance_id.strip() or cid
        if iid in existing:
            if iid == cid:
                n = 2
                while f"{cid}_{n}" in existing:
                    n += 1
                iid = f"{cid}_{n}"
            else:
                self.report({'ERROR'},
                            f"Instance ID '{iid}' уже используется")
                return {'CANCELLED'}

        # Позиция/поворот = у первого top-level объекта выделения.
        # Top-level: parent is None или parent вне выделения.
        top_objs = [o for o in objs
                    if o.parent is None or o.parent not in objs]
        if not top_objs:
            self.report({'ERROR'},
                        "В выделении нет top-level объектов "
                        "(все с parent'ами внутри выделения — невозможно)")
            return {'CANCELLED'}
        target_loc = top_objs[0].location.copy()
        target_rot = tuple(top_objs[0].rotation_euler)

        for obj in objs:
            if obj.modifiers:
                mods = [m.type for m in obj.modifiers]
                print(f"[PBA AM] ВНИМАНИЕ: {obj.name} имеет "
                      f"модификаторы {mods}")

        comp_path = os.path.join(cdir, cid)
        os.makedirs(comp_path, exist_ok=True)

        for obj in objs:
            _ensure_local_uuids(obj)

        # 1. Сохранить в библиотеку (с временно снятыми PBA-props,
        #    чтобы instance_id не утёк в mesh.blend)
        snapshots = [(obj, _clear_pba_props(obj)) for obj in objs]
        try:
            mesh_path = os.path.join(comp_path, MESH_FILE_NAME)
            _save_objects_to_blend(objs, mesh_path)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка записи библиотеки: {e}")
            return {'CANCELLED'}
        finally:
            for obj, snap in snapshots:
                _restore_pba_props(obj, snap)

        # 2. Удалить исходное выделение.
        #    Порядок важен: если исходные объекты останутся в сцене,
        #    libraries.load вернёт загружаемые с суффиксом .001,
        #    что сломает pba_original_name и композитный ключ.
        for obj in objs:
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)

        # 3. Импорт из библиотеки на месте исходного выделения
        coll_name, err = _instantiate_component(
            scene, iid, cid, comp_path, target_loc, target_rot)
        if err:
            self.report(
                {'ERROR'},
                f"Компонент сохранён в {mesh_path}, но импорт упал: "
                f"{err}. Используйте Import Component вручную.")
            _set_status(scene, f"Ошибка импорта: {err}")
            return {'CANCELLED'}

        # 4. Запись instance
        inst = pba.instances.add()
        inst.instance_id = iid
        inst.component_id = cid
        inst.component_path = comp_path
        inst.collection_name = coll_name
        inst.position = tuple(target_loc)
        inst.rotation = tuple(target_rot)
        try:
            inst.component_mtime = float(os.path.getmtime(mesh_path))
        except Exception:
            inst.component_mtime = 0.0
        inst.has_local_changes = False
        inst.mesh_fingerprint = _instance_fingerprint(
            bpy.data.collections.get(coll_name))
        pba.active_instance_index = len(pba.instances) - 1

        _try_autosave(scene)
        _set_status(scene, f"Компонент создан и импортирован: {iid}")
        self.report({'INFO'}, f"Компонент создан и импортирован: {iid}")
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
        _suppress_dirty_mark()
        try:
            coll_name, err = _instantiate_component(
                scene, iid, cid, comp_path, target_loc, (0, 0, 0))
        finally:
            _resume_dirty_mark()
            _pba_last_fp_check = 0.0
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
        inst.has_local_changes = False
        try:
            inst.component_mtime = float(os.path.getmtime(blend_path))
        except Exception:
            inst.component_mtime = 0.0
        pba.active_instance_index = len(pba.instances) - 1

        inst.mesh_fingerprint = _instance_fingerprint(
            bpy.data.collections.get(coll_name))

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
                "component_mtime": float(inst.component_mtime),
                "has_local_changes": bool(inst.has_local_changes),
                "mesh_fingerprint": inst.mesh_fingerprint,
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

        _suppress_dirty_mark()
        try:
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
                inst.position = tuple(float(x) for x in pos[:3])
                inst.rotation = tuple(float(x) for x in rot[:3])
                inst.component_mtime = float(cd.get("component_mtime", 0.0))
                inst.has_local_changes = bool(cd.get("has_local_changes", False))
                inst.mesh_fingerprint = cd.get("mesh_fingerprint", "")

                if not comp_path or not os.path.isdir(comp_path):
                    failed.append(f"{iid}: папка не найдена: {comp_path}")
                    continue

                coll_name, err = _instantiate_component(
                    scene, iid, cid, comp_path, pos, rot)
                if err:
                    failed.append(f"{iid}: {err}")
                    continue

                inst.collection_name = coll_name
                if coll_name:
                    inst.mesh_fingerprint = _instance_fingerprint(
                        bpy.data.collections.get(coll_name))
                succeeded += 1
        finally:
            _resume_dirty_mark()

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

            # Проверка файла компонента и mtime
            if not inst.component_path:
                problems.append(f"{inst.instance_id}: component_path пуст")
            else:
                mesh_path = os.path.join(
                    inst.component_path, MESH_FILE_NAME)
                if not os.path.exists(mesh_path):
                    problems.append(
                        f"{inst.instance_id}: mesh.blend не найден")
                elif inst.component_mtime <= 0.0:
                    problems.append(
                        f"{inst.instance_id}: mtime не задан "
                        f"(выполните Reload Component)")
                else:
                    try:
                        current = os.path.getmtime(mesh_path)
                        if current > inst.component_mtime + 1.0:
                            problems.append(
                                f"{inst.instance_id}: библиотека новее "
                                f"сборки (+{current - inst.component_mtime:.0f}s). "
                                f"Используйте Reload Component")
                    except Exception as e:
                        problems.append(
                            f"{inst.instance_id}: ошибка чтения mtime: {e}")

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


class PBA_AM_OT_reload_component(bpy.types.Operator):
    bl_idname = "pba_am.reload_component"
    bl_label = "Reload Component"
    bl_description = ("Перезагрузить объекты instance из mesh.blend "
                      "библиотеки. Сохраняет instance_id, obj_name и "
                      "позицию. UUID берутся из библиотеки.")
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am

        if not (0 <= self.index < len(pba.instances)):
            return {'CANCELLED'}

        # Авто-выход из Edit Mode + проверка VIEW_3D (для libraries.load)
        _ensure_object_mode(context)
        if context.mode != 'OBJECT':
            self.report({'ERROR'},
                        "Не удалось выйти из Edit Mode — выйдите вручную")
            return {'CANCELLED'}
        if not _check_view3d_context(context):
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        inst = pba.instances[self.index]
        iid = inst.instance_id

        _suppress_dirty_mark()
        try:
            ok, err, warnings = _reload_instance(scene, inst)
        finally:
            _resume_dirty_mark()
            inst.has_local_changes = False
        if not ok:
            _set_status(scene, f"Reload '{iid}': {err}")
            self.report({'ERROR'}, f"{iid}: {err}")
            return {'CANCELLED'}

        # Снять выделение
        for obj in context.view_layer.objects:
            obj.select_set(False)
        context.view_layer.objects.active = None

        _try_autosave(scene)

        if warnings:
            for w in warnings:
                print(f"[PBA AM] {iid}: {w}")
            short = "; ".join(warnings[:3])
            if len(warnings) > 3:
                short += f" (и ещё {len(warnings) - 3})"
            _set_status(scene,
                        f"Перезагружен: {iid} (топология изменилась: {short})")
            self.report({'WARNING'},
                        f"{iid}: топология изменилась — pieces/transfers "
                        f"могут не совпасть. {short}")
        else:
            _set_status(scene, f"Перезагружен: {iid}")
            self.report({'INFO'}, f"Reload: {iid}")
        return {'FINISHED'}


class PBA_AM_OT_reload_all(bpy.types.Operator):
    bl_idname = "pba_am.reload_all"
    bl_label = "Reload All Components"
    bl_description = "Перезагрузить все instance из их mesh.blend"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am

        if not pba.instances:
            self.report({'WARNING'}, "Нет компонентов")
            return {'CANCELLED'}

        _ensure_object_mode(context)
        if context.mode != 'OBJECT':
            self.report({'ERROR'},
                        "Не удалось выйти из Edit Mode — выйдите вручную")
            return {'CANCELLED'}
        if not _check_view3d_context(context):
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        total = len(pba.instances)
        succeeded = 0
        failed = []
        warned = []  # [(iid, [warnings...]), ...]

        target_iids = [inst.instance_id for inst in pba.instances]

        _suppress_dirty_mark()

        for iid in target_iids:
            idx = next(
                (i for i, inst in enumerate(pba.instances)
                 if inst.instance_id == iid), None)
            if idx is None:
                continue
            try:
                ok, err, warnings = _reload_instance(
                    scene, pba.instances[idx])
                if ok:
                    pba.instances[idx].has_local_changes = False
                    succeeded += 1
                    if warnings:
                        warned.append((iid, warnings))
                else:
                    failed.append(f"{iid}: {err}")
            except Exception as e:
                failed.append(f"{iid}: {e}")

        # Снять выделение после пакета
        for obj in context.view_layer.objects:
            obj.select_set(False)
        context.view_layer.objects.active = None

        _resume_dirty_mark()

        _try_autosave(scene)

        msg = f"Перезагружено: {succeeded}/{total}"
        if failed:
            msg += f", ошибок: {len(failed)}"
        if warned:
            msg += f", с изменением топологии: {len(warned)}"
            for iid, ws in warned:
                for w in ws:
                    print(f"[PBA AM] {iid}: {w}")

        _set_status(scene, msg)
        if failed:
            for e in failed:
                print(f"[PBA AM] {e}")
            self.report({'ERROR'}, msg + " (см. консоль)")
        elif warned:
            self.report({'WARNING'},
                        msg + f". У {len(warned)} instance изменились UUID "
                        f"— pieces/transfers могут не совпасть.")
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


class PBA_AM_OT_publish_instance(bpy.types.Operator):
    bl_idname = "pba_am.publish_instance"
    bl_label = "Publish Instance to Library"
    bl_description = ("Записать текущую геометрию instance обратно в "
                      "components/<cid>/mesh.blend. Остальные instance "
                      "этого компонента останутся на старой версии.")
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        scene = context.scene
        pba = scene.pba_am

        # 1. Проверки контекста
        _ensure_object_mode(context)
        if context.mode != 'OBJECT':
            self.report({'ERROR'},
                        "Не удалось выйти из Edit Mode — выйдите вручную")
            return {'CANCELLED'}
        if not _check_view3d_context(context):
            self.report({'ERROR'}, "Запустите из 3D Viewport")
            return {'CANCELLED'}

        # 2. Найти instance: приоритет — выделенный объект, fallback — index
        idx = -1
        active = context.active_object
        if active is not None:
            iid_from_sel = active.get(PBA_INSTANCE_ID_PROP, "")
            if iid_from_sel:
                idx = next(
                    (i for i, inst in enumerate(pba.instances)
                     if inst.instance_id == iid_from_sel), -1)
        if idx < 0 and 0 <= self.index < len(pba.instances):
            idx = self.index
        if idx < 0:
            idx = pba.active_instance_index
        if idx < 0 or idx >= len(pba.instances):
            self.report({'ERROR'},
                        "Не удалось определить instance для Publish "
                        "(выделите объект или активный instance)")
            return {'CANCELLED'}

        inst = pba.instances[idx]
        iid = inst.instance_id
        cid = inst.component_id
        comp_path = inst.component_path
        coll_name = inst.collection_name

        if not comp_path:
            self.report({'ERROR'}, f"{iid}: component_path пуст")
            return {'CANCELLED'}

        mesh_path = os.path.join(comp_path, MESH_FILE_NAME)
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            self.report({'ERROR'}, f"{iid}: коллекция не в сцене")
            return {'CANCELLED'}

        mesh_objects = [o for o in coll.objects if o.type == 'MESH']
        if not mesh_objects:
            self.report({'ERROR'}, f"{iid}: нет mesh-объектов")
            return {'CANCELLED'}

        _suppress_dirty_mark()
        try:
            # 3. Клоны с оригинальными именами (без iid-префикса)
            clone_map = {}
            for obj in mesh_objects:
                orig = obj.get(PBA_ORIGINAL_NAME_PROP, obj.name)
                clone = obj.copy()
                if obj.data is not None:
                    clone.data = obj.data.copy()
                    clone.data.name = orig
                clone.name = orig
                clone.parent = None
                clone_map[obj] = clone

            # 4. Восстановить parent-связи между клонами (внутри компонента)
            for orig, clone in clone_map.items():
                if orig.parent is not None and orig.parent in clone_map:
                    clone.parent = clone_map[orig.parent]

            # 5. Временная коллекция + линк клонов
            temp_coll = bpy.data.collections.new("_PBA_Publish_Temp")
            scene.collection.children.link(temp_coll)
            for clone in clone_map.values():
                temp_coll.objects.link(clone)

            # 6. Собрать datablocks для write
            datablocks = set()
            for clone in clone_map.values():
                datablocks.add(clone)
                try:
                    clone.use_fake_user = True
                except Exception:
                    pass
                if clone.data is not None:
                    datablocks.add(clone.data)
                    try:
                        clone.data.use_fake_user = True
                        clone.data.update()
                    except Exception:
                        pass
                    for slot in clone.material_slots:
                        if slot.material is not None:
                            datablocks.add(slot.material)
                            try:
                                slot.material.use_fake_user = True
                            except Exception:
                                pass

            # 7. Сравнить наборы имён с существующей библиотекой (без загрузки)
            library_names = set()
            if os.path.exists(mesh_path):
                try:
                    with bpy.data.libraries.load(mesh_path, link=False) as (src, _dst):
                        library_names = set(src.objects)
                except Exception as e:
                    print(f"[PBA AM] Не удалось прочитать имена из {mesh_path}: {e}")

            published_names = set(clone_map[o].name for o in mesh_objects)
            extra_in_instance = published_names - library_names
            extra_in_library = library_names - published_names
            name_warnings = []
            if extra_in_instance:
                name_warnings.append(
                    f"новые в instance → добавятся в библиотеку: "
                    f"{', '.join(sorted(extra_in_instance))}")
            if extra_in_library:
                name_warnings.append(
                    f"есть в библиотеке, но не в instance → будут удалены: "
                    f"{', '.join(sorted(extra_in_library))}")

            # 8. Записать в библиотеку
            try:
                bpy.data.libraries.write(mesh_path, datablocks, fake_user=True)
            except Exception as e:
                self.report({'ERROR'}, f"libraries.write failed: {e}")
                _cleanup_publish_temp(temp_coll)
                return {'CANCELLED'}

            # 9. Cleanup временной коллекции
            _cleanup_publish_temp(temp_coll)
        finally:
            _resume_dirty_mark()
            _pba_last_fp_check = 0.0
        # после write, до отчёта:
        inst.has_local_changes = False
        inst.mesh_fingerprint = _instance_fingerprint(coll)

        # 10. Обновить mtime у всех instance этого cid
        try:
            new_mtime = float(os.path.getmtime(mesh_path))
        except Exception:
            new_mtime = 0.0
        affected = []
        for other in pba.instances:
            if other.component_id == cid and other.instance_id != iid:
                other.component_mtime = new_mtime
                affected.append(other.instance_id)
        inst.component_mtime = new_mtime

        # 11. Автосохранение
        _try_autosave(scene)

        # 12. Отчёт
        msg = f"Опубликован: {iid} → {os.path.basename(mesh_path)}"
        if affected:
            msg += (f". ВНИМАНИЕ: остальные instance '{cid}' устарели "
                    f"({', '.join(affected)}) — сделайте Reload All.")
        if name_warnings:
            msg += " " + "; ".join(name_warnings)
        _set_status(scene, msg)

        level = 'WARNING' if (affected or name_warnings) else 'INFO'
        self.report({level}, msg)
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
    PBA_AM_OT_reload_component,
    PBA_AM_OT_reload_all,
    PBA_AM_OT_publish_instance,
)
