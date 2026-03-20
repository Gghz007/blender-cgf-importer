"""
cgf_exporter.py — Exports Blender scene objects to CryEngine 1 CGF/CGA/CAF files.
"""

import bpy
import os
import math
import mathutils

from .cgf_writer import (
    CGFWriter,
    ctrl_id_from_name,
    build_source_info_chunk,
    build_timing_chunk,
    build_bone_name_list_chunk,
    build_bone_anim_chunk,
    build_bone_initial_pos_chunk,
    build_mesh_chunk,
    build_node_chunk,
    build_material_chunk,
    build_controller_chunk_v827,
    pack_u32,
    CHUNK_TYPE_MESH, CHUNK_TYPE_NODE, CHUNK_TYPE_MATERIAL,
    CHUNK_TYPE_BONE_ANIM, CHUNK_TYPE_BONE_NAME_LIST,
    CHUNK_TYPE_BONE_INITIAL_POS, CHUNK_TYPE_TIMING,
    CHUNK_TYPE_SOURCE_INFO, CHUNK_TYPE_CONTROLLER,
)

from .cgf_reader import (
    CHUNK_TYPE_MESH       as CT_MESH,
    CHUNK_TYPE_NODE       as CT_NODE,
    CHUNK_TYPE_MATERIAL   as CT_MAT,
    CHUNK_TYPE_BONE_ANIM  as CT_BANIM,
    CHUNK_TYPE_BONE_NAME_LIST as CT_BNAMES,
    CHUNK_TYPE_BONE_INITIAL_POS as CT_BIPOS,
    CHUNK_TYPE_TIMING     as CT_TIMING,
    CHUNK_TYPE_SOURCE_INFO as CT_SRCINFO,
    CHUNK_TYPE_CONTROLLER as CT_CTRL,
)

INCHES_TO_METERS = 0.0254
METERS_TO_INCHES = 1.0 / INCHES_TO_METERS


# ── Coordinate conversion (Blender → CryEngine/Max) ──────────────────────────

def blender_vec_to_cry(v):
    """Blender meters → CryEngine inches."""
    return (v[0] * METERS_TO_INCHES,
            v[1] * METERS_TO_INCHES,
            v[2] * METERS_TO_INCHES)


def blender_matrix_to_cry(mat):
    """
    Blender Matrix4x4 → CGF flat 16-float row-major matrix.
    Blender columns = basis vectors → transpose → rows = basis vectors (Max convention).
    Scale translation from meters to inches.
    """
    m = mat.transposed()
    result = []
    for row_i in range(4):
        for col_i in range(4):
            v = m[row_i][col_i]
            if row_i == 3 and col_i < 3:
                v *= METERS_TO_INCHES  # scale translation
            result.append(v)
    return result


def blender_matrix_to_cry43(mat):
    """
    Blender Matrix4x4 → CGF flat 12-float 4x3 matrix (bone initial pos).
    """
    m = mat.transposed()
    result = []
    for row_i in range(3):
        for col_i in range(3):
            result.append(m[row_i][col_i])
    # Translation row
    result.append(mat.translation.x * METERS_TO_INCHES)
    result.append(mat.translation.y * METERS_TO_INCHES)
    result.append(mat.translation.z * METERS_TO_INCHES)
    return result


def blender_quat_to_cry(q):
    """Blender Quaternion (w,x,y,z) → CryEngine (x,y,z,w)."""
    return (q.x, q.y, q.z, q.w)


def quat_log(q):
    """
    Quaternion logarithm — inverse of quat_exp used in reader.
    Used for v827 controller keys.
    """
    bq = mathutils.Quaternion((q[3], q[0], q[1], q[2]))  # w,x,y,z
    # log(q) = (v/|v|) * acos(w) where v = (x,y,z)
    vec = mathutils.Vector((bq.x, bq.y, bq.z))
    half_angle = math.acos(max(-1.0, min(1.0, bq.w)))
    sin_half = math.sin(half_angle)
    if abs(sin_half) < 1e-10:
        return (0.0, 0.0, 0.0)
    scale = half_angle / sin_half
    return (vec.x * scale, vec.y * scale, vec.z * scale)


# ── Mesh extraction ───────────────────────────────────────────────────────────

def triangulate_mesh(obj):
    """Get a triangulated copy of the mesh."""
    import bmesh as bm_mod
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval  = obj.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(obj_eval)

    bm = bm_mod.new()
    bm.from_mesh(mesh)
    bm_mod.ops.triangulate(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()

    return mesh


def extract_mesh_data(obj, arm_obj=None):
    """
    Extract vertices, faces, UVs, normals, and bone weights from a Blender mesh.
    Returns dict with all data ready for build_mesh_chunk.
    """
    mesh = triangulate_mesh(obj)
    # calc_normals_split removed in Blender 4.1 — normals are automatic
    if hasattr(mesh, 'calc_normals_split'):
        mesh.calc_normals_split()

    # World matrix relative to armature (or world)
    if arm_obj:
        world_mat = arm_obj.matrix_world.inverted() @ obj.matrix_world
    else:
        world_mat = obj.matrix_world

    normal_mat = world_mat.to_3x3().inverted().transposed()

    vertices   = []  # (pos_cry, normal_cry)
    faces      = []  # (v0, v1, v2, mat_id, smooth_group)
    tex_verts  = []  # (u, v)
    tex_faces  = []  # (t0, t1, t2)

    # We need to split vertices by UV — same position can have different UVs
    uv_layer = mesh.uv_layers.active

    # Build vertex/UV table
    vert_map = {}    # (vert_idx, uv_tuple) → new_idx
    new_verts = []   # (pos, normal)
    new_uvs   = []   # (u, v)

    for poly in mesh.polygons:
        face_tv = []
        face_vv = []

        for li in poly.loop_indices:
            loop = mesh.loops[li]
            vi   = loop.vertex_index

            # Position
            pos_bl = world_mat @ mesh.vertices[vi].co
            pos_cry = (pos_bl.x * METERS_TO_INCHES,
                       pos_bl.y * METERS_TO_INCHES,
                       pos_bl.z * METERS_TO_INCHES)

            # Normal (split normal)
            n_bl = normal_mat @ loop.normal
            if n_bl.length > 1e-6:
                n_bl.normalize()
            nor_cry = (n_bl.x, n_bl.y, n_bl.z)

            # UV
            if uv_layer:
                uv = uv_layer.data[li].uv
                uv_key = (round(uv[0], 6), round(uv[1], 6))
            else:
                uv_key = (0.0, 0.0)

            key = (vi, uv_key)
            if key not in vert_map:
                vert_map[key] = len(new_verts)
                new_verts.append((pos_cry, nor_cry))
                new_uvs.append(uv_key)

            new_vi = vert_map[key]
            face_vv.append(new_vi)
            face_tv.append(new_vi)  # tex face uses same index

        mat_id = poly.material_index
        # CryEngine uses same winding as Blender
        faces.append((face_vv[0], face_vv[1], face_vv[2], mat_id, poly.use_smooth))
        tex_faces.append((face_tv[0], face_tv[1], face_tv[2]))

    vertices  = new_verts
    tex_verts = new_uvs

    # Bone weights
    physique = None
    has_bone_info = False
    if arm_obj and obj.vertex_groups:
        # Build bone name → index map from armature
        bone_names = [b.name for b in arm_obj.data.bones]
        bone_idx   = {name: i for i, name in enumerate(bone_names)}

        physique = []
        has_bone_info = True

        for key, new_vi in sorted(vert_map.items(), key=lambda x: x[1]):
            vi = key[0]
            vert = mesh.vertices[vi]
            links = []
            total_w = 0.0
            for g in vert.groups:
                vg = obj.vertex_groups[g.group]
                if vg.name in bone_idx and g.weight > 0.0:
                    bid = bone_idx[vg.name]
                    # offset = vertex position in bone local space
                    bone = arm_obj.pose.bones[vg.name]
                    bone_mat_inv = bone.matrix.inverted()
                    pos_world = obj.matrix_world @ vert.co
                    offset_bl = bone_mat_inv @ pos_world
                    offset_cry = (offset_bl.x * METERS_TO_INCHES,
                                  offset_bl.y * METERS_TO_INCHES,
                                  offset_bl.z * METERS_TO_INCHES)
                    links.append((bid, offset_cry, g.weight))
                    total_w += g.weight
            # Normalize weights
            if total_w > 0 and links:
                links = [(b, o, w/total_w) for b, o, w in links]
            physique.append(links)

    bpy.data.meshes.remove(mesh)

    return {
        'vertices':  vertices,
        'faces':     faces,
        'tex_verts': tex_verts,
        'tex_faces': tex_faces,
        'physique':  physique,
        'has_bone_info': has_bone_info,
    }


# ── Armature extraction ───────────────────────────────────────────────────────

def extract_armature_data(arm_obj):
    """
    Extract bone data from a Blender armature.
    Returns list of bone dicts in topological order (parents before children).
    """
    arm = arm_obj.data
    bones = arm.bones

    # Topological sort: parents before children
    sorted_bones = []
    visited = set()

    def visit(bone):
        if bone.name in visited:
            return
        if bone.parent:
            visit(bone.parent)
        visited.add(bone.name)
        sorted_bones.append(bone)

    for bone in bones:
        visit(bone)

    bone_idx = {b.name: i for i, b in enumerate(sorted_bones)}

    result = []
    for i, bone in enumerate(sorted_bones):
        parent_id = bone_idx[bone.parent.name] if bone.parent else -1
        num_children = len(bone.children)
        ctrl_id = ctrl_id_from_name(bone.name)

        # Check if original ctrl_id was stored
        if arm_obj.pose and bone.name in arm_obj.pose.bones:
            pbone = arm_obj.pose.bones[bone.name]
            stored = pbone.get('cry_ctrl_id')
            if stored:
                try:
                    ctrl_id = int(stored, 16)
                except Exception:
                    pass

        result.append({
            'bone_id':        i,
            'name':           bone.name,
            'parent_id':      parent_id,
            'num_children':   num_children,
            'ctrl_id':        ctrl_id,
            'custom_property': '',
            'bone_physics':   None,  # zeros
            'bone':           bone,  # keep reference for matrix
        })

    return result, bone_idx


def extract_bone_matrices(arm_obj, bone_data_list):
    """
    Extract 4x3 rest pose matrices for each bone (BoneInitialPos).
    Uses bone.matrix_local (local to armature).
    """
    matrices = []
    for bd in bone_data_list:
        bone = arm_obj.data.bones[bd['name']]
        # matrix_local is relative to armature — this is the world space in Cry
        m = blender_matrix_to_cry43(bone.matrix_local)
        matrices.append(m)
    return matrices


# ── Material extraction ───────────────────────────────────────────────────────

def extract_materials(obj):
    """
    Extract material data from a Blender object.
    Returns list of material dicts.
    """
    result = []
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            result.append({'name': 'default', 'diffuse': (0.8, 0.8, 0.8),
                           'specular': (0, 0, 0), 'opacity': 1.0,
                           'tex_diffuse': '', 'tex_bump': ''})
            continue

        diffuse  = (0.8, 0.8, 0.8)
        specular = (0, 0, 0)
        opacity  = 1.0
        tex_diff = ''
        tex_bump = ''

        if mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    bc = node.inputs.get('Base Color')
                    if bc:
                        diffuse = (bc.default_value[0],
                                   bc.default_value[1],
                                   bc.default_value[2])
                    al = node.inputs.get('Alpha')
                    if al:
                        opacity = al.default_value
                    # Find connected image texture
                    if bc and bc.links:
                        tex_node = bc.links[0].from_node
                        if tex_node.type == 'TEX_IMAGE' and tex_node.image:
                            tex_diff = tex_node.image.filepath_raw or tex_node.image.name

        result.append({
            # Use original CGF full name (with shader/surface) if stored
            'name':        mat.get('cgf_full_name', mat.name),
            'diffuse':     diffuse,
            'specular':    specular,
            'opacity':     opacity,
            'tex_diffuse': tex_diff,
            'tex_bump':    tex_bump,
        })

    return result


# ── CGF export ────────────────────────────────────────────────────────────────

def export_cgf(operator, context, filepath,
               export_materials=True, export_skeleton=True,
               export_weights=True, selected_only=False):
    """Export selected/active mesh(es) to CGF."""

    if selected_only:
        objects = [o for o in context.selected_objects
                   if o.type == 'MESH' and not o.hide_get()]
    else:
        # Only visible objects in the current view layer
        objects = [o for o in context.view_layer.objects
                   if o.type == 'MESH' and not o.hide_get() and o.visible_get()]

    if not objects:
        operator.report({'ERROR'}, "No visible mesh objects found")
        return {'CANCELLED'}

    # Find armature — only visible ones
    arm_obj = None
    if export_skeleton:
        for obj in context.view_layer.objects:
            if obj.type == 'ARMATURE' and not obj.hide_get():
                arm_obj = obj
                break
        if arm_obj is None:
            for obj in objects:
                for mod in obj.modifiers:
                    if mod.type == 'ARMATURE' and mod.object:
                        arm_obj = mod.object
                        break

    writer = CGFWriter(is_anim=False)
    chunk_id = 0

    def next_id():
        nonlocal chunk_id
        chunk_id += 1
        return chunk_id

    # Source info
    import getpass, datetime
    data, ver, cid = build_source_info_chunk(
        next_id(),
        source_file=filepath,
        date=datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y"),
        user=getpass.getuser()
    )
    writer.add_chunk(CT_SRCINFO, ver, cid, data)

    # Timing
    scene = context.scene
    fps = scene.render.fps
    ticks_per_frame = 160
    secs_per_tick = 1.0 / (fps * ticks_per_frame)
    data, ver, cid = build_timing_chunk(
        next_id(), ticks_per_frame, secs_per_tick,
        scene.frame_start, scene.frame_end
    )
    writer.add_chunk(CT_TIMING, ver, cid, data)

    # Armature (bones)
    bone_data_list = []
    bone_idx = {}
    bone_name_list = []

    if arm_obj and export_skeleton:
        print(f"[CGF Export] Extracting armature: {arm_obj.name}")
        bone_data_list, bone_idx = extract_armature_data(arm_obj)
        bone_name_list = [b['name'] for b in bone_data_list]

        # BoneAnim chunk
        data, ver, cid = build_bone_anim_chunk(next_id(), bone_data_list)
        writer.add_chunk(CT_BANIM, ver, cid, data)

        # BoneNameList chunk
        data, ver, cid = build_bone_name_list_chunk(next_id(), bone_name_list)
        writer.add_chunk(CT_BNAMES, ver, cid, data)

    # Materials
    mat_chunk_ids = {}  # mat_name → chunk_id
    all_standard_mats = []

    if export_materials:
        for obj in objects:
            mats = extract_materials(obj)
            for mat in mats:
                if mat['name'] not in mat_chunk_ids:
                    cid = next_id()
                    mat_chunk_ids[mat['name']] = cid
                    all_standard_mats.append((cid, mat))

        for cid, mat in all_standard_mats:
            data, ver, _ = build_material_chunk(
                cid, mat['name'],
                mat_type=1,
                diffuse=mat['diffuse'],
                specular=mat['specular'],
                opacity=mat['opacity'],
                tex_diffuse=mat.get('tex_diffuse', ''),
                tex_bump=mat.get('tex_bump', ''),
            )
            writer.add_chunk(CT_MAT, ver, cid, data)

    # Build multi-material if multiple mats per object
    multi_mat_ids = {}  # obj.name → multi_mat_chunk_id
    for obj in objects:
        if len(obj.material_slots) > 1:
            children = []
            for slot in obj.material_slots:
                if slot.material and slot.material.name in mat_chunk_ids:
                    children.append(mat_chunk_ids[slot.material.name])
            if children:
                cid = next_id()
                multi_mat_ids[obj.name] = cid
                data, ver, _ = build_material_chunk(
                    cid, obj.name + "_multi",
                    mat_type=2, children=children
                )
                writer.add_chunk(CT_MAT, ver, cid, data)

    # Meshes + Nodes
    mesh_chunk_ids = {}  # obj.name → mesh_chunk_id

    for obj in objects:
        print(f"[CGF Export] Extracting mesh: {obj.name}")
        md = extract_mesh_data(obj, arm_obj if export_weights else None)

        mesh_cid = next_id()
        mesh_chunk_ids[obj.name] = mesh_cid

        data, ver, _ = build_mesh_chunk(
            mesh_cid,
            vertices   = md['vertices'],
            faces      = md['faces'],
            tex_vertices = md['tex_verts'],
            tex_faces  = md['tex_faces'],
            physique   = md['physique'],
            has_bone_info = md['has_bone_info'],
        )
        writer.add_chunk(CT_MESH, ver, mesh_cid, data)

        # BoneInitialPos — once, for first skinned mesh
        if md['has_bone_info'] and arm_obj and bone_data_list:
            matrices = extract_bone_matrices(arm_obj, bone_data_list)
            data, ver, cid = build_bone_initial_pos_chunk(
                next_id(), mesh_cid, matrices
            )
            writer.add_chunk(CT_BIPOS, ver, cid, data)

        # Node chunk
        if export_materials:
            if obj.name in multi_mat_ids:
                mat_id = multi_mat_ids[obj.name]
            elif obj.material_slots and obj.material_slots[0].material:
                mat_id = mat_chunk_ids.get(obj.material_slots[0].material.name, -1)
            else:
                mat_id = -1
        else:
            mat_id = -1

        # Node transform — use identity matrix.
        # Vertex positions are already baked into world space in extract_mesh_data,
        # so the node transform should be identity (like Max exports static meshes).
        identity_m44 = [
            1,0,0,0,
            0,1,0,0,
            0,0,1,0,
            0,0,0,0,
        ]
        identity_pos = (0.0, 0.0, 0.0)
        identity_rot = (0.0, 0.0, 0.0, 1.0)  # x,y,z,w
        identity_scl = (1.0, 1.0, 1.0)

        node_cid = next_id()
        ctrl_id  = ctrl_id_from_name(obj.name)
        data, ver, _ = build_node_chunk(
            node_cid, obj.name,
            object_id   = mesh_cid,
            parent_id   = -1,
            material_id = mat_id,
            trans_matrix = identity_m44,
            position = identity_pos,
            rotation = identity_rot,
            scale    = identity_scl,
            pos_ctrl_id   = ctrl_id,
            rot_ctrl_id   = ctrl_id,
            scale_ctrl_id = ctrl_id,
        )
        writer.add_chunk(CT_NODE, ver, node_cid, data)

    writer.write(filepath)
    print(f"[CGF Export] Written: {filepath}")
    operator.report({'INFO'}, f"Exported {len(objects)} mesh(es) to {os.path.basename(filepath)}")
    return {'FINISHED'}


# ── CAF export ────────────────────────────────────────────────────────────────

def export_caf(operator, context, filepath, action=None):
    """Export an animation Action to CAF."""

    arm_obj = context.active_object
    if arm_obj is None or arm_obj.type != 'ARMATURE':
        operator.report({'ERROR'}, "Select an armature first")
        return {'CANCELLED'}

    if action is None:
        if arm_obj.animation_data and arm_obj.animation_data.action:
            action = arm_obj.animation_data.action
        else:
            operator.report({'ERROR'}, "No action found on armature")
            return {'CANCELLED'}

    scene = context.scene
    fps = scene.render.fps
    ticks_per_frame = 160
    secs_per_tick = 1.0 / (fps * ticks_per_frame)

    writer = CGFWriter(is_anim=True)
    chunk_id = 0

    def next_id():
        nonlocal chunk_id
        chunk_id += 1
        return chunk_id

    # Source info
    import getpass, datetime
    data, ver, cid = build_source_info_chunk(
        next_id(),
        date=datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y"),
        user=getpass.getuser()
    )
    writer.add_chunk(CT_SRCINFO, ver, cid, data)

    # Timing
    frame_start = int(action.frame_range[0])
    frame_end   = int(action.frame_range[1])
    data, ver, cid = build_timing_chunk(
        next_id(), ticks_per_frame, secs_per_tick,
        frame_start * ticks_per_frame,
        frame_end   * ticks_per_frame
    )
    writer.add_chunk(CT_TIMING, ver, cid, data)

    # One controller chunk per bone that has animation
    bone_data_list, _ = extract_armature_data(arm_obj)

    for bd in bone_data_list:
        bone_name = bd['name']
        ctrl_id   = bd['ctrl_id']

        # Find F-Curves for this bone
        path_loc = f'pose.bones["{bone_name}"].location'
        path_rot = f'pose.bones["{bone_name}"].rotation_quaternion'

        fc_loc = [action.fcurves.find(path_loc, index=i) for i in range(3)]
        fc_rot = [action.fcurves.find(path_rot, index=i) for i in range(4)]

        # Collect all keyframe times for this bone
        frame_set = set()
        for fc in fc_loc + fc_rot:
            if fc:
                for kp in fc.keyframe_points:
                    frame_set.add(int(kp.co[0]))

        if not frame_set:
            continue

        # Sample pose at each keyframe
        keys = []
        orig_frame = scene.frame_current

        for frame in sorted(frame_set):
            scene.frame_set(frame)
            bpy.context.view_layer.update()

            pbone = arm_obj.pose.bones.get(bone_name)
            if pbone is None:
                continue

            # Position in armature local space → inches
            pos_bl = pbone.location
            pos_cry = (pos_bl.x * METERS_TO_INCHES,
                       pos_bl.y * METERS_TO_INCHES,
                       pos_bl.z * METERS_TO_INCHES)

            # Rotation as quaternion log
            rot_bl = pbone.rotation_quaternion
            rot_xyzw = (rot_bl.x, rot_bl.y, rot_bl.z, rot_bl.w)
            rot_log = quat_log(rot_xyzw)

            time_ticks = frame * ticks_per_frame
            keys.append((time_ticks, pos_cry, rot_log))

        scene.frame_set(orig_frame)

        if keys:
            data, ver, cid = build_controller_chunk_v827(next_id(), ctrl_id, keys)
            writer.add_chunk(CT_CTRL, ver, cid, data)

    writer.write(filepath)
    print(f"[CGF Export] CAF written: {filepath}")
    operator.report({'INFO'}, f"Exported action '{action.name}' to {os.path.basename(filepath)}")
    return {'FINISHED'}


# ── CAL export ────────────────────────────────────────────────────────────────

def export_cal(operator, context, filepath):
    """
    Export all actions on the active armature as CAF files
    and write a CAL list file.
    """
    arm_obj = context.active_object
    if arm_obj is None or arm_obj.type != 'ARMATURE':
        operator.report({'ERROR'}, "Select an armature first")
        return {'CANCELLED'}

    cal_dir  = os.path.dirname(filepath)
    cal_name = os.path.splitext(os.path.basename(filepath))[0]

    cal_lines = []
    exported  = 0

    for action in bpy.data.actions:
        # Check if this action has curves for bones of this armature
        has_curves = any(
            fc.data_path.startswith('pose.bones[')
            for fc in action.fcurves
        )
        if not has_curves:
            continue

        caf_name = action.name + ".caf"
        caf_path = os.path.join(cal_dir, caf_name)

        # Temporarily assign action
        if arm_obj.animation_data is None:
            arm_obj.animation_data_create()
        arm_obj.animation_data.action = action

        result = export_caf(operator, context, caf_path, action=action)
        if result == {'FINISHED'}:
            cal_lines.append(f"{action.name} {caf_name}")
            exported += 1

    # Write CAL file
    with open(filepath, 'w') as f:
        f.write('\n'.join(cal_lines))

    operator.report({'INFO'}, f"Exported {exported} animations to CAL: {os.path.basename(filepath)}")
    return {'FINISHED'}
