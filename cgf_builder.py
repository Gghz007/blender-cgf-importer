"""
cgf_builder.py — Converts parsed CGF/CAF chunks into Blender scene objects.

Coordinate system:
  CryEngine 1 / 3ds Max: Z-up, right-handed, units = inches
  Blender: Z-up, right-handed, units = meters
  Scale: INCHES_TO_METERS = 0.0254

Matrix convention:
  Max Matrix3 stores BASIS VECTORS AS ROWS → need .transposed() for Blender
  (Blender matrix_world stores basis vectors as COLUMNS)
"""

import bpy
import bmesh
import math
import os
import mathutils

from . import cgf_reader
from .cgf_reader import (CTRL_CRY_BONE, CTRL_LINEAR1, CTRL_LINEAR3, CTRL_LINEAR_Q,
                         CTRL_BEZIER1, CTRL_BEZIER3, CTRL_BEZIER_Q,
                         CTRL_TCB1, CTRL_TCB3, CTRL_TCBQ)

# ── Scale ─────────────────────────────────────────────────────────────────────
# 3ds Max default units = inches. 1 inch = 0.0254 meters.
INCHES_TO_METERS = 0.0254


# ── Coordinate helpers ────────────────────────────────────────────────────────

def cry_vec(v):
    """Scale a CryEngine/Max vector (inches) to Blender (meters)."""
    s = INCHES_TO_METERS
    return mathutils.Vector((v[0]*s, v[1]*s, v[2]*s))


def cry_matrix_to_blender(m44):
    """
    Convert CGF 4x4 row-major matrix (flat list of 16 floats) to Blender Matrix.
    Max stores basis vectors as ROWS → .transposed() makes them COLUMNS for Blender.
    Only translation is scaled (inches→meters); rotation/scale are dimensionless.
    """
    m = mathutils.Matrix((m44[0:4], m44[4:8], m44[8:12], m44[12:16])).transposed()
    m.translation *= INCHES_TO_METERS
    return m


def cry_matrix43_to_blender(m43):
    """Convert CGF 4x3 bone matrix (flat 12 floats) to Blender Matrix4x4."""
    rot = mathutils.Matrix((
        (m43[0], m43[1], m43[2]),
        (m43[3], m43[4], m43[5]),
        (m43[6], m43[7], m43[8]),
    )).transposed()
    m = rot.to_4x4()
    m.translation = mathutils.Vector((m43[9]*INCHES_TO_METERS,
                                       m43[10]*INCHES_TO_METERS,
                                       m43[11]*INCHES_TO_METERS))
    return m


def cry_quat(xyzw):
    """CryEngine quat (x,y,z,w) → Blender Quaternion (w,x,y,z)."""
    return mathutils.Quaternion((xyzw[3], xyzw[0], xyzw[1], xyzw[2]))


def quat_exp(rot_log):
    """
    Reconstruct quaternion from logarithm (x,y,z).
    Max: exp(quat rx ry rz 0)  ←  standard quaternion exponential map.
    """
    rx, ry, rz = rot_log
    theta = math.sqrt(rx*rx + ry*ry + rz*rz)
    if theta < 1e-10:
        return mathutils.Quaternion((1, 0, 0, 0))
    s = math.sin(theta) / theta
    return mathutils.Quaternion((math.cos(theta), rx*s, ry*s, rz*s))


# ── Material ──────────────────────────────────────────────────────────────────

def _build_cgf_mat_name(name, shader_name, surface_name):
    """Reconstruct full CGF material name: 'name(shader)/surface'"""
    result = name
    if shader_name:
        result += f"({shader_name})"
    if surface_name:
        result += f"/{surface_name}"
    return result


def _round_tuple(values, digits=6):
    return tuple(round(float(v), digits) for v in values)


def _normalize_material_texture_key(tex_data, filepath, game_root_path=""):
    if not tex_data or not tex_data.name:
        return ""

    resolved = _find_texture(tex_data.name, filepath, game_root_path)
    if resolved:
        return os.path.normcase(os.path.abspath(resolved))

    raw = tex_data.name.replace('\\', os.sep).replace('/', os.sep)
    return os.path.normcase(os.path.splitext(raw)[0])


def _material_signature(mat_chunk, filepath, game_root_path=""):
    return (
        (mat_chunk.shader_name or '').strip().lower(),
        (mat_chunk.surface_name or '').strip().lower(),
        _normalize_material_texture_key(mat_chunk.tex_diffuse, filepath, game_root_path),
        _normalize_material_texture_key(mat_chunk.tex_bump, filepath, game_root_path),
        _normalize_material_texture_key(mat_chunk.tex_detail, filepath, game_root_path),
        _normalize_material_texture_key(mat_chunk.tex_specular, filepath, game_root_path),
        _normalize_material_texture_key(mat_chunk.tex_reflection, filepath, game_root_path),
        _round_tuple(mat_chunk.diffuse),
        _round_tuple(mat_chunk.specular),
        _round_tuple(mat_chunk.ambient),
        round(float(mat_chunk.specular_level), 6),
        round(float(mat_chunk.specular_shininess), 6),
        round(float(mat_chunk.self_illumination), 6),
        round(float(mat_chunk.opacity), 6),
        round(float(mat_chunk.alpha_test), 6),
        int(mat_chunk.type),
        int(mat_chunk.flags),
    )


def _is_nodraw_material(mat_chunk):
    shader = (mat_chunk.shader_name or '').strip().lower()
    surface = (mat_chunk.surface_name or '').strip().lower()
    diffuse = ((getattr(mat_chunk.tex_diffuse, 'name', '') or '')
               .replace('\\', '/').strip().lower())

    if shader in {'nodraw', 'no_draw'}:
        return True
    if surface in {'mat_obstruct', 'mat_nodraw'}:
        return True
    if diffuse.endswith('/nodraw.dds') or diffuse.endswith('common/nodraw.dds'):
        return True
    return False


def _global_standard_material_chunks(archive):
    result = []
    for mc in archive.material_chunks:
        if mc.type == 2:
            continue
        result.append(mc)
    return result


def _mesh_is_collision_like(mesh_chunk, archive):
    global_chunks = _global_standard_material_chunks(archive)
    face_mat_ids = sorted({face.mat_id for face in mesh_chunk.faces if face.mat_id >= 0})
    if not face_mat_ids:
        return False

    resolved = []
    for face_mat_id in face_mat_ids:
        if face_mat_id >= len(global_chunks):
            return False
        resolved.append(global_chunks[face_mat_id])

    return bool(resolved) and all(_is_nodraw_material(mat) for mat in resolved)


def _global_collision_material_ids(archive):
    result = set()
    for idx, mat in enumerate(_global_standard_material_chunks(archive)):
        if _is_nodraw_material(mat):
            result.add(idx)
    return result


def _uses_diffuse_alpha_as_opacity(mat_chunk):
    shader = (mat_chunk.shader_name or '').strip().lower()
    diffuse = ((getattr(mat_chunk.tex_diffuse, 'name', '') or '')
               .replace('\\', '/').strip().lower())

    if 'glossalpha' in shader:
        return False
    if shader in {'glass', 'vegetation'}:
        return True
    if mat_chunk.alpha_test > 0.0:
        return True
    if any(token in diffuse for token in ('chainlink', 'fence', 'grate', 'wire', 'mesh', 'net')):
        return True
    return False


def _configure_diffuse_image_alpha(img, mat_chunk):
    if img is None or not hasattr(img, 'alpha_mode'):
        return
    try:
        if _uses_diffuse_alpha_as_opacity(mat_chunk):
            if img.alpha_mode == 'NONE':
                img.alpha_mode = 'STRAIGHT'
        else:
            img.alpha_mode = 'NONE'
    except Exception:
        pass


def _set_input(node, *names, value):
    for name in names:
        if name in node.inputs:
            try: node.inputs[name].default_value = value
            except Exception: pass
            return


def build_material(mat_chunk, filepath, import_materials, game_root_path=""):
    if not import_materials:
        return None
    full_name = _build_cgf_mat_name(mat_chunk.name,
                                    mat_chunk.shader_name,
                                    mat_chunk.surface_name)

    mat = bpy.data.materials.new(name=full_name)
    # Store original CGF material info for round-trip export
    mat['cgf_chunk_id']     = int(mat_chunk.header.chunk_id)
    mat['cgf_shader_name']  = mat_chunk.shader_name
    mat['cgf_surface_name'] = mat_chunk.surface_name
    mat['cgf_full_name']    = full_name
    mat['cgf_source_name']  = mat_chunk.name
    # Populate CryEngine panel properties
    if hasattr(mat, 'cry'):
        # Set shader — check if it matches a preset
        shader = mat_chunk.shader_name or ''
        preset_values = [item[0] for item in [
            ('Phong',''),('TemplModelCommon',''),('TemplBumpDiffuse',''),
            ('TemplBumpSpec',''),('TemplBumpSpec_GlossAlpha',''),
            ('NoDraw',''),('Glass',''),('Vegetation',''),('Terrain',''),
        ]]
        if shader in preset_values:
            mat.cry.shader_preset = shader
        else:
            mat.cry.shader_preset = 'custom'
            mat.cry.shader_custom = shader
        # Set surface
        surface = mat_chunk.surface_name or 'mat_default'
        try:
            mat.cry.surface = surface
        except Exception:
            mat.cry.surface = 'mat_default'
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    out  = nodes.new('ShaderNodeOutputMaterial'); out.location  = (400, 0)
    bsdf = nodes.new('ShaderNodeBsdfPrincipled'); bsdf.location = (0, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    if _is_nodraw_material(mat_chunk):
        _set_input(bsdf, 'Base Color', value=(0.0, 0.0, 0.0, 1.0))
        _set_input(bsdf, 'Alpha', value=0.0)
        if hasattr(mat, 'blend_method'):
            mat.blend_method = 'CLIP'
        if hasattr(mat, 'shadow_method'):
            try:
                mat.shadow_method = 'NONE'
            except Exception:
                pass
        return mat

    d = mat_chunk.diffuse
    _set_input(bsdf, 'Base Color', value=(d[0], d[1], d[2], 1.0))

    s = mat_chunk.specular
    spec = ((s[0]+s[1]+s[2])/3.0) * mat_chunk.specular_level
    _set_input(bsdf, 'Specular IOR Level', 'Specular', value=min(spec, 1.0))

    if mat_chunk.specular_shininess > 0:
        _set_input(bsdf, 'Roughness',
                   value=1.0 - min(mat_chunk.specular_shininess/100.0, 1.0))

    # opacity: 0.0 in CGF often means "unused", not "fully transparent"
    # Only apply if it's a meaningful value between 0 and 1 (exclusive)
    if 0.0 < mat_chunk.opacity < 1.0 and _uses_diffuse_alpha_as_opacity(mat_chunk):
        _set_input(bsdf, 'Alpha', value=mat_chunk.opacity)
        if hasattr(mat, 'blend_method'):
            mat.blend_method = 'BLEND'

    def add_tex(tex_data, x, y, color_space='sRGB'):
        if not tex_data or not tex_data.name:
            return None
        print(f"[CGF] Searching: '{tex_data.name}' | root='{game_root_path}'")
        path = _find_texture(tex_data.name, filepath, game_root_path)
        if not path:
            print(f"[CGF] NOT found: {tex_data.name}")
            return None
        print(f"[CGF] Found: {path}")
        node = nodes.new('ShaderNodeTexImage')
        node.location = (x, y)
        try:
            img = bpy.data.images.load(path, check_existing=True)
            img.colorspace_settings.name = color_space
            # Force absolute filepath so FBX export picks up the correct path
            img.filepath = os.path.abspath(path)
            img.filepath_raw = os.path.abspath(path)
            node.image = img
        except Exception as e:
            print(f"[CGF] Load error {path}: {e}")
        return node

    tex_diff = add_tex(mat_chunk.tex_diffuse, -400, 0)
    if tex_diff:
        mat['tex_diffuse'] = bpy.path.abspath(tex_diff.image.filepath) if tex_diff.image else ""
        _configure_diffuse_image_alpha(tex_diff.image, mat_chunk)
        links.new(tex_diff.outputs['Color'], bsdf.inputs['Base Color'])
        alpha_input = bsdf.inputs.get('Alpha')
        if alpha_input and _uses_diffuse_alpha_as_opacity(mat_chunk):
            links.new(tex_diff.outputs['Alpha'], alpha_input)
            if hasattr(mat, 'blend_method'):
                if mat_chunk.alpha_test > 0.0:
                    mat.blend_method = 'CLIP'
                elif mat_chunk.opacity < 1.0:
                    mat.blend_method = 'CLIP'
                elif tex_diff.image and getattr(tex_diff.image, 'depth', 0) in (32, 64):
                    mat.blend_method = 'CLIP'
        # Gloss packed in diffuse alpha → connect to Specular
        shader_name = mat_chunk.shader_name or ''
        if 'GlossAlpha' in shader_name or 'glossalpha' in shader_name.lower():
            spec_input = (bsdf.inputs.get('Specular IOR Level') or
                          bsdf.inputs.get('Specular'))
            if spec_input:
                links.new(tex_diff.outputs['Alpha'], spec_input)

    tex_bump = add_tex(mat_chunk.tex_bump, -600, -300, 'Non-Color')
    if tex_bump:
        mat['tex_bump'] = bpy.path.abspath(tex_bump.image.filepath) if tex_bump.image else ""
        tex_name = (mat_chunk.tex_bump.name or '').lower()
        if '_ddn' in tex_name:
            # DDN = normal map
            normal_map = nodes.new('ShaderNodeNormalMap')
            normal_map.location = (-200, -300)
            links.new(tex_bump.outputs['Color'], normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
        else:
            # _bump or other = heightmap → Bump node
            bump = nodes.new('ShaderNodeBump')
            bump.location = (-200, -300)
            links.new(tex_bump.outputs['Color'], bump.inputs['Height'])
            links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])

    tex_detail = add_tex(mat_chunk.tex_detail, -600, -520, 'Non-Color')
    if tex_detail:
        mat['tex_detail'] = bpy.path.abspath(tex_detail.image.filepath) if tex_detail.image else ""
        detail_bump = nodes.new('ShaderNodeBump')
        detail_bump.location = (-200, -520)
        links.new(tex_detail.outputs['Color'], detail_bump.inputs['Height'])
        if not bsdf.inputs['Normal'].links:
            links.new(detail_bump.outputs['Normal'], bsdf.inputs['Normal'])

    return mat


def _find_texture(name, cgf_path, game_root_path=""):
    """
    Search for a texture. Mirrors getFullFilename from original Max script:
    1. game_root_path + name  (texture name is relative to game root)
    2. CGF folder + name
    3. CGF folder + basename only
    """
    if not name:
        return None

    name = name.replace('\\', os.sep).replace('/', os.sep)
    basename = os.path.basename(name)
    cgf_dir  = os.path.dirname(cgf_path)
    exts     = ['', '.dds', '.tga', '.png', '.jpg', '.bmp', '.tif', '.tiff']

    def try_path(p):
        if os.path.isfile(p): return p
        base_no_ext = os.path.splitext(p)[0]
        for ext in exts:
            if os.path.isfile(base_no_ext + ext):
                return base_no_ext + ext
        return None

    if game_root_path:
        r = try_path(os.path.join(game_root_path, name))
        if r: return r
        print(f"[CGF] Texture not found in game root: {os.path.join(game_root_path, name)}")

    r = try_path(os.path.join(cgf_dir, name))
    if r: return r

    r = try_path(os.path.join(cgf_dir, basename))
    if r: return r

    print(f"[CGF] Texture not found anywhere: '{name}'")
    return None


# ── Mesh ──────────────────────────────────────────────────────────────────────

def build_mesh(mesh_chunk, node_chunk, archive, collection,
               import_materials, import_normals, import_uvs,
               import_weights, blender_materials, filepath,
               skip_collision_geometry=False):

    mc = mesh_chunk
    if not mc.vertices or not mc.faces:
        return None

    name = node_chunk.name if node_chunk else f"Mesh_{mc.header.chunk_id}"
    mesh = bpy.data.meshes.new(name)
    obj  = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    # Build the mesh from face corners, not from the raw shared vertex table.
    # Some CryEngine meshes intentionally reuse the same geometric triangle more than once
    # with different UV/material data; bmesh collapses those faces, which is what caused the
    # visible UV breakage on coa_storage.cgf.
    verts = []
    faces = []
    vertex_source_ids = []
    face_texcoords = []
    face_normals = []
    face_material_ids = []
    face_smooth_flags = []
    collision_material_ids = _global_collision_material_ids(archive) if skip_collision_geometry else set()

    for fi, cf in enumerate(mc.faces):
        if collision_material_ids and cf.mat_id in collision_material_ids:
            continue
        src_vis = (cf.v0, cf.v1, cf.v2)
        if any(vi >= len(mc.vertices) for vi in src_vis):
            continue

        if mc.tex_faces and fi < len(mc.tex_faces):
            tf = mc.tex_faces[fi]
            src_tvis = (tf.t0, tf.t1, tf.t2)
        else:
            src_tvis = src_vis

        face_indices = []
        corner_uvs = []
        corner_normals = []

        for corner_idx, src_vi in enumerate(src_vis):
            verts.append(cry_vec(mc.vertices[src_vi].pos))
            vertex_source_ids.append(src_vi)
            face_indices.append(len(verts) - 1)

            tvi = src_tvis[corner_idx] if corner_idx < len(src_tvis) else src_vi
            if import_uvs and tvi is not None and tvi < len(mc.tex_vertices):
                corner_uvs.append(mc.tex_vertices[tvi])
            else:
                corner_uvs.append((0.0, 0.0))

            n = mc.vertices[src_vi].normal
            bn = mathutils.Vector(n)
            if bn.length > 1e-6:
                bn.normalize()
            else:
                bn = mathutils.Vector((0, 0, 1))
            corner_normals.append((bn.x, bn.y, bn.z))

        faces.append(face_indices)
        face_texcoords.append(corner_uvs)
        face_normals.append(corner_normals)
        face_material_ids.append(cf.mat_id)
        face_smooth_flags.append(cf.smooth_group != 0)

    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj["_cgf_source_vert_ids"] = vertex_source_ids

    for poly_index, poly in enumerate(mesh.polygons):
        if poly_index < len(face_smooth_flags):
            poly.use_smooth = face_smooth_flags[poly_index]

    # Custom normals
    if import_normals and face_normals:
        normals = [n for face in face_normals for n in face]
        try:
            if hasattr(mesh, 'use_auto_smooth'):
                mesh.use_auto_smooth = True
            for poly in mesh.polygons:
                poly.use_smooth = True
            if len(normals) == len(mesh.loops):
                mesh.normals_split_custom_set(normals)
        except Exception:
            pass

    # UVs
    if import_uvs and face_texcoords:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        loop_index = 0
        for poly_index, poly in enumerate(mesh.polygons):
            corner_uvs = face_texcoords[poly_index]
            for corner_idx in range(poly.loop_total):
                if corner_idx < len(corner_uvs):
                    u, v = corner_uvs[corner_idx]
                    uv_layer.data[loop_index].uv = (u, v)
                loop_index += 1

    # Materials
    # face.mat_id in CGF = global index among ALL standard materials in file,
    # excluding Multi materials. This matches how Max buildMatMappings() works.
    # We build the same mapping: standard_mat_index → (blender_material, slot_index)
    if import_materials and blender_materials:
        global_material_map = _build_global_standard_material_map(archive, blender_materials)
        slot_map = {}  # face.mat_id → mesh material slot index
        for pi, poly in enumerate(mesh.polygons):
            if pi >= len(face_material_ids):
                continue
            face_mat_id = face_material_ids[pi]
            bmat = global_material_map.get(face_mat_id)
            if bmat is None:
                continue
            if bmat.name not in [m.name for m in mesh.materials]:
                mesh.materials.append(bmat)
            if face_mat_id not in slot_map:
                slot_map[face_mat_id] = list(mesh.materials).index(bmat)
            poly.material_index = slot_map[face_mat_id]

    # Transform
    if node_chunk and node_chunk.trans_matrix:
        obj.matrix_world = cry_matrix_to_blender(node_chunk.trans_matrix)
    elif node_chunk:
        obj.location = cry_vec(node_chunk.position)

    # Vertex weights
    if import_weights and mc.physique and archive.bone_anim_chunks:
        _assign_weights(obj, mc, archive, vertex_source_ids)

    return obj


def _assign_weights(obj, mc, archive, vertex_source_ids=None):
    print(f"[CGF] Assigning weights: {len(mc.physique)} source vertices...")
    names = {}
    if archive.bone_name_list_chunks:
        for i, n in enumerate(archive.bone_name_list_chunks[0].name_list):
            names[i] = n

    links_by_source_vid = {bl.vertex_id: bl.links for bl in mc.physique}

    if vertex_source_ids is None:
        vertex_source_ids = list(range(len(mc.vertices)))

    for vid, src_vid in enumerate(vertex_source_ids):
        for lnk in links_by_source_vid.get(src_vid, []):
            bname = names.get(lnk.bone_id, f"Bone_{lnk.bone_id}")
            if bname not in obj.vertex_groups:
                obj.vertex_groups.new(name=bname)
            obj.vertex_groups[bname].add([vid], lnk.blending, 'REPLACE')
    print(f"[CGF] Weights done")


def _collect_standard_chunks(mat_chunk, archive, result):
    """
    Recursively collect all STANDARD material chunks in order, skipping Multi.
    This mirrors Max's tempStandardMatArray — face.mat_id is an index into this list.

    materialType_Standard = 1
    materialType_Multi    = 2
    """
    if mat_chunk.type == 1:  # Standard
        result.append(mat_chunk)
    elif mat_chunk.type == 2:  # Multi — recurse into children
        for cid in mat_chunk.children:
            child = archive.get_material_chunk(cid)
            if child:
                _collect_standard_chunks(child, archive, result)
    else:
        # Unknown type — treat as standard
        result.append(mat_chunk)


# ── Armature ──────────────────────────────────────────────────────────────────

def _build_material_cache(archive, filepath, import_materials, game_root_path="", skip_collision_geometry=False):
    by_name = {}
    by_signature = {}

    if not import_materials:
        return by_name, by_signature

    for mc in archive.material_chunks:
        standard_chunks = []
        _collect_standard_chunks(mc, archive, standard_chunks)
        print(f"[CGF]   material chunk: {mc.name} type={mc.type} -> {len(standard_chunks)} standard")
        for std in standard_chunks:
            if skip_collision_geometry and _is_nodraw_material(std):
                continue
            std_key = _build_cgf_mat_name(std.name, std.shader_name, std.surface_name)
            signature = _material_signature(std, filepath, game_root_path)
            bmat = by_signature.get(signature)
            if bmat is None:
                bmat = build_material(std, filepath, import_materials, game_root_path)
                if bmat:
                    by_signature[signature] = bmat
            if bmat:
                by_name[std_key] = bmat

    return by_name, by_signature


def _build_global_standard_material_map(archive, blender_materials):
    slot_map = {}
    standard_index = 0

    for mc in _global_standard_material_chunks(archive):
        std_key = _build_cgf_mat_name(mc.name, mc.shader_name, mc.surface_name)
        bmat = blender_materials.get(std_key)
        if bmat is not None:
            slot_map[standard_index] = bmat
        standard_index += 1

    return slot_map


def _source_vert_map_from_object(obj):
    values = obj.get("_cgf_source_vert_ids")
    if not values:
        return None
    source_map = {}
    for mesh_vid, src_vid in enumerate(values):
        source_map.setdefault(int(src_vid), []).append(mesh_vid)
    return source_map


def build_armature(archive, collection):
    if not archive.bone_anim_chunks or not archive.bone_anim_chunks[0].bones:
        return None, None

    names = archive.bone_name_list_chunks[0].name_list if archive.bone_name_list_chunks else []

    arm_data = bpy.data.armatures.new("Armature")
    arm_obj  = bpy.data.objects.new("Armature", arm_data)
    collection.objects.link(arm_obj)

    # Make active and enter edit mode
    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)

    # Find window/area/region for temp_override
    win    = bpy.context.window_manager.windows[0]
    screen = win.screen
    area   = next((a for a in screen.areas if a.type == 'VIEW_3D'), None)

    if area is None:
        # No 3D viewport — try any area
        area = screen.areas[0]

    region = next((r for r in area.regions if r.type == 'WINDOW'), area.regions[0])

    ctx = {
        'window': win, 'screen': screen, 'area': area, 'region': region,
        'active_object': arm_obj, 'object': arm_obj,
        'selected_objects': [arm_obj], 'selected_editable_objects': [arm_obj],
    }

    with bpy.context.temp_override(**ctx):
        bpy.ops.object.mode_set(mode='EDIT')

    eb_map = {}
    for bone in archive.bone_anim_chunks[0].bones:
        bid   = bone.bone_id
        bname = names[bid] if bid < len(names) else (bone.name or f"Bone_{bid}")
        eb = arm_data.edit_bones.new(bname)
        eb.head = (0, 0, 0)
        eb.tail = (0, 0.05 * INCHES_TO_METERS, 0)

        init = archive.get_bone_initial_pos(bid)
        if init:
            try:
                mx = cry_matrix43_to_blender(init)
                head = mx.translation
                local_y = mx.col[1].xyz
                local_z = mx.col[2].xyz
                if local_y.length <= 1e-8:
                    local_y = mathutils.Vector((0, 1, 0))
                if local_z.length <= 1e-8:
                    local_z = mathutils.Vector((0, 0, 1))
                eb.head = head
                eb.tail = head + local_y.normalized() * (0.05 * INCHES_TO_METERS)
                eb.align_roll(local_z.normalized())
            except Exception as e:
                print(f"[CGF] Bone matrix error {bname}: {e}")
        eb_map[bid] = eb

    for bone in archive.bone_anim_chunks[0].bones:
        if bone.parent_id >= 0 and bone.parent_id in eb_map:
            child  = eb_map[bone.bone_id]
            parent = eb_map[bone.parent_id]
            child.parent = parent
            if (child.head - parent.tail).length < 0.0001:
                child.use_connect = True

    with bpy.context.temp_override(**ctx):
        bpy.ops.object.mode_set(mode='OBJECT')

    # Preserve original bone metadata for round-trip export.
    for bone in archive.bone_anim_chunks[0].bones:
        bid   = bone.bone_id
        bname = names[bid] if bid < len(names) else (bone.name or f"Bone_{bid}")
        if not arm_obj.pose or bname not in arm_obj.pose.bones:
            continue
        pbone = arm_obj.pose.bones[bname]
        pbone['cry_ctrl_id'] = bone.ctrl_id
        pbone['cry_bone_id'] = int(bone.bone_id)
        pbone['cry_parent_id'] = int(bone.parent_id)
        pbone['cry_custom_property'] = bone.custom_property or ""
        if bone.bone_physics:
            pbone['cry_bone_mesh_id'] = int(bone.bone_physics.mesh_id)
            pbone['cry_bone_flags'] = f"{int(bone.bone_physics.flags) & 0xFFFFFFFF:08X}"

    # Store original CGF bone matrices on armature for round-trip export
    # Must be done AFTER exit from edit mode (data bones are accessible now)
    cgf_matrices = {}
    for bone in archive.bone_anim_chunks[0].bones:
        bid   = bone.bone_id
        bname = names[bid] if bid < len(names) else (bone.name or f"Bone_{bid}")
        init  = archive.get_bone_initial_pos(bid)
        if init:
            cgf_matrices[bname] = list(init)

    if cgf_matrices:
        import json
        arm_obj['cgf_bone_matrices'] = json.dumps(cgf_matrices)

    return arm_obj, arm_data


def apply_armature_to_meshes(arm_obj, mesh_objects):
    if not arm_obj:
        return
    for obj in mesh_objects:
        if obj and obj.vertex_groups:
            obj.parent = arm_obj
            mod = obj.modifiers.new("Armature", 'ARMATURE')
            mod.object = arm_obj
            mod.use_vertex_groups = True


# ── Shape keys ────────────────────────────────────────────────────────────────

def build_shape_keys(obj, mesh_chunk, archive):
    morphs = archive.get_morphs_for_mesh(mesh_chunk.header.chunk_id)
    if not morphs:
        return
    source_map = _source_vert_map_from_object(obj)
    obj.shape_key_add(name="Basis", from_mix=False)
    for morph in morphs:
        sk = obj.shape_key_add(name=morph.name, from_mix=False)
        for mv in morph.target_vertices:
            target_ids = source_map.get(mv.vertex_id, []) if source_map else [mv.vertex_id]
            for target_id in target_ids:
                if target_id < len(sk.data):
                    sk.data[target_id].co = cry_vec(mv.target_point)


# ── Animation ─────────────────────────────────────────────────────────────────

def apply_animation(arm_obj, geom_archive, anim_archive, action_name="Action"):
    """
    Apply controller chunks from anim_archive to the armature.
    Ported from CryImporter-scenebuilder.ms createController826/827 + addAnim.

    The controller chunk's ctrl_id matches the bone's ctrl_id.
    Keys are in ticks; divide by ticks_per_frame to get frame number.
    """
    if not arm_obj:
        return

    tpf = anim_archive.get_ticks_per_frame()
    fps = round(1.0 / (anim_archive.get_secs_per_tick() * tpf))
    if fps <= 0:
        fps = 25

    # Set scene FPS
    bpy.context.scene.render.fps = fps

    # Build ctrl_id → bone name map from geom archive
    # Bone ctrl_id is stored as 8-char hex string in CryBone
    ctrl_to_bone = {}
    if geom_archive.bone_anim_chunks:
        name_list = geom_archive.bone_name_list_chunks[0].name_list \
                    if geom_archive.bone_name_list_chunks else []
        for bone in geom_archive.bone_anim_chunks[0].bones:
            bid = bone.bone_id
            bname = name_list[bid] if bid < len(name_list) else f"Bone_{bid}"
            if bone.ctrl_id and bone.ctrl_id != "FFFFFFFF":
                ctrl_to_bone[bone.ctrl_id] = bname

    if not anim_archive.controller_chunks:
        print("[CGF] No controller chunks found in animation file")
        return

    # Determine total frame range from timing chunk
    frame_start = 0
    frame_end   = 0
    if anim_archive.timing_chunks:
        gr = anim_archive.timing_chunks[0].global_range
        if gr:
            frame_start = gr[1]
            frame_end   = gr[2]

    # Create or get action
    action = bpy.data.actions.get(action_name)
    if action is None:
        action = bpy.data.actions.new(name=action_name)

    if arm_obj.animation_data is None:
        arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    bpy.context.scene.frame_start = frame_start
    bpy.context.scene.frame_end   = max(frame_end, frame_start + 1)

    for ctrl_chunk in anim_archive.controller_chunks:
        if not ctrl_chunk.keys:
            continue

        bone_name = ctrl_to_bone.get(ctrl_chunk.ctrl_id)
        if not bone_name:
            continue

        if bone_name not in arm_obj.pose.bones:
            continue

        pbone = arm_obj.pose.bones[bone_name]
        _apply_controller_to_bone(pbone, ctrl_chunk, action, tpf, bone_name)

    print(f"[CGF] Animation '{action_name}' applied: {len(anim_archive.controller_chunks)} controllers, fps={fps}")


def _apply_controller_to_bone(pbone, ctrl_chunk, action, ticks_per_frame, bone_name):
    """Apply a single controller chunk to a pose bone as F-Curves."""

    bone_path_loc  = f'pose.bones["{bone_name}"].location'
    bone_path_rot  = f'pose.bones["{bone_name}"].rotation_quaternion'
    bone_path_scl  = f'pose.bones["{bone_name}"].scale'
    pbone.rotation_mode = 'QUATERNION'

    def get_or_make_fcurve(data_path, index):
        fc = action.fcurves.find(data_path, index=index)
        if fc is None:
            fc = action.fcurves.new(data_path, index=index)
        return fc

    from .cgf_reader import (CTRL_CRY_BONE, CTRL_LINEAR3, CTRL_LINEAR_Q,
                              CTRL_BEZIER3, CTRL_BEZIER_Q,
                              CTRL_TCB3, CTRL_TCBQ)

    ct = ctrl_chunk.ctrl_type

    # v827 or v826 CryBone: pos + rotation (as quat or rotLog)
    if ct == CTRL_CRY_BONE:
        fc_loc = [get_or_make_fcurve(bone_path_loc, i) for i in range(3)]
        fc_rot = [get_or_make_fcurve(bone_path_rot, i) for i in range(4)]

        for key in ctrl_chunk.keys:
            frame = key.time / ticks_per_frame

            # Position
            s = INCHES_TO_METERS
            if hasattr(key, 'rel_pos'):
                # CryBoneKey (v826)
                pos = key.rel_pos
                q   = cry_quat(key.rel_quat)
            else:
                # CryKey (v827): rot_log is logarithm of quat
                pos = key.pos
                q   = quat_exp(key.rot_log)

            for i, v in enumerate((pos[0]*s, pos[1]*s, pos[2]*s)):
                fc_loc[i].keyframe_points.insert(frame, v, options={'FAST'})

            # Rotation (w, x, y, z)
            for i, v in enumerate((q.w, q.x, q.y, q.z)):
                fc_rot[i].keyframe_points.insert(frame, v, options={'FAST'})

    # Linear position
    elif ct == CTRL_LINEAR3:
        fc = [get_or_make_fcurve(bone_path_loc, i) for i in range(3)]
        s = INCHES_TO_METERS
        for key in ctrl_chunk.keys:
            frame = key.time / ticks_per_frame
            for i, v in enumerate((key.val[0]*s, key.val[1]*s, key.val[2]*s)):
                fc[i].keyframe_points.insert(frame, v, options={'FAST'})

    # Linear rotation (quat)
    elif ct == CTRL_LINEAR_Q:
        fc = [get_or_make_fcurve(bone_path_rot, i) for i in range(4)]
        for key in ctrl_chunk.keys:
            frame = key.time / ticks_per_frame
            q = cry_quat(key.val)
            for i, v in enumerate((q.w, q.x, q.y, q.z)):
                fc[i].keyframe_points.insert(frame, v, options={'FAST'})

    # Bezier position
    elif ct == CTRL_BEZIER3:
        fc = [get_or_make_fcurve(bone_path_loc, i) for i in range(3)]
        s = INCHES_TO_METERS
        for key in ctrl_chunk.keys:
            frame = key.time / ticks_per_frame
            for i, v in enumerate((key.val[0]*s, key.val[1]*s, key.val[2]*s)):
                fc[i].keyframe_points.insert(frame, v, options={'FAST'})

    # Bezier rotation (quat, no tangents for rotation)
    elif ct == CTRL_BEZIER_Q:
        fc = [get_or_make_fcurve(bone_path_rot, i) for i in range(4)]
        for key in ctrl_chunk.keys:
            frame = key.time / ticks_per_frame
            q = cry_quat(key.val)
            for i, v in enumerate((q.w, q.x, q.y, q.z)):
                fc[i].keyframe_points.insert(frame, v, options={'FAST'})

    # TCB position
    elif ct == CTRL_TCB3:
        fc = [get_or_make_fcurve(bone_path_loc, i) for i in range(3)]
        s = INCHES_TO_METERS
        for key in ctrl_chunk.keys:
            frame = key.time / ticks_per_frame
            for i, v in enumerate((key.val[0]*s, key.val[1]*s, key.val[2]*s)):
                fc[i].keyframe_points.insert(frame, v, options={'FAST'})

    # TCB rotation
    elif ct == CTRL_TCBQ:
        fc = [get_or_make_fcurve(bone_path_rot, i) for i in range(4)]
        for key in ctrl_chunk.keys:
            frame = key.time / ticks_per_frame
            q = cry_quat(key.val)
            for i, v in enumerate((q.w, q.x, q.y, q.z)):
                fc[i].keyframe_points.insert(frame, v, options={'FAST'})

    # Update F-Curve handles
    for fc in action.fcurves:
        fc.update()


# ── CAF file search (mirrors getCAFFilename from Max script) ──────────────────

def find_caf_file(caf_name, cal_filepath, geom_filepath):
    cal_dir  = os.path.dirname(cal_filepath)
    geom_dir = os.path.dirname(geom_filepath) if geom_filepath else ""
    candidates = [
        os.path.join(cal_dir,  caf_name),
        os.path.join(geom_dir, caf_name),
    ]
    for path in candidates:
        if os.path.isfile(path): return path
    return None


# ── Main load functions ───────────────────────────────────────────────────────

def load(operator, context, filepath,
         import_materials=True, import_normals=True, import_uvs=True,
         import_skeleton=True, import_weights=True, game_root_path="",
         skip_collision_geometry=False):
    """Import a CGF/CGA geometry file."""

    print(f"[CGF] Loading: {filepath}")
    print(f"[CGF] Game root: '{game_root_path}'")
    reader = cgf_reader.ChunkReader()
    try:
        print(f"[CGF] Reading file...")
        archive = reader.read_file(filepath)
    except ValueError as e:
        operator.report({'ERROR'}, str(e)); return {'CANCELLED'}

    print(f"[CGF] {archive.num_chunks} chunks — "
          f"meshes:{len(archive.mesh_chunks)} nodes:{len(archive.node_chunks)} "
          f"mats:{len(archive.material_chunks)} bones:{len(archive.bone_anim_chunks)}")

    file_name  = os.path.splitext(os.path.basename(filepath))[0]
    collection = bpy.data.collections.new(file_name)
    context.scene.collection.children.link(collection)

    # Materials
    print(f"[CGF] Building materials...")
    blender_materials, _ = _build_material_cache(
        archive, filepath, import_materials, game_root_path, skip_collision_geometry
    )
    print(f"[CGF] Materials done: {len(blender_materials)}")

    # Armature
    arm_obj = None
    if import_skeleton and archive.bone_anim_chunks:
        print(f"[CGF] Building armature...")
        arm_obj, _ = build_armature(archive, collection)
        print(f"[CGF] Armature done: {arm_obj}")

    # Meshes
    print(f"[CGF] Building {len(archive.mesh_chunks)} mesh(es)...")
    mesh_objects = []
    for i, mc in enumerate(archive.mesh_chunks):
        print(f"[CGF]   mesh {i}: verts={len(mc.vertices)} faces={len(mc.faces)} bone_info={mc.has_bone_info} physique={len(mc.physique)}")
        if skip_collision_geometry and _mesh_is_collision_like(mc, archive):
            print(f"[CGF]   mesh {i} skipped as collision-like geometry")
            continue
        node = archive.get_node(mc.header.chunk_id)
        obj  = build_mesh(mc, node, archive, collection,
                          import_materials, import_normals, import_uvs,
                          import_weights, blender_materials, filepath,
                          skip_collision_geometry=skip_collision_geometry)
        if obj:
            obj['cgf_chunk_id'] = int(mc.header.chunk_id)
            obj['cgf_source_name'] = node.name if node and node.name else obj.name
            mesh_objects.append(obj)
            print(f"[CGF]   mesh {i} done: {obj.name}")
            if archive.mesh_morph_target_chunks:
                build_shape_keys(obj, mc, archive)

    print(f"[CGF] All meshes done")
    if arm_obj and import_skeleton and import_weights:
        apply_armature_to_meshes(arm_obj, mesh_objects)
    if arm_obj and import_skeleton and archive.controller_chunks:
        action_name = f"{file_name}_Embedded"
        apply_animation(arm_obj, archive, archive, action_name)
        try:
            context.scene.frame_set(context.scene.frame_start)
        except Exception:
            pass

    bpy.ops.object.select_all(action='DESELECT')
    for obj in collection.objects: obj.select_set(True)
    if mesh_objects: context.view_layer.objects.active = mesh_objects[0]

    operator.report({'INFO'},
        f"Imported {len(mesh_objects)} mesh(es) from {os.path.basename(filepath)}")
    return {'FINISHED'}


def _find_cgf_near(filepath):
    """
    Find a CGF or CGA file with the SAME base name as the given CAF/CAL file.
    Returns the path if found, or None.
    Does NOT fall back to random CGF files in the folder.
    """
    folder = os.path.dirname(filepath)
    base = os.path.splitext(os.path.basename(filepath))[0]
    for ext in ('.cgf', '.cga'):
        p = os.path.join(folder, base + ext)
        if os.path.isfile(p):
            return p
    return None


def _ensure_armature(operator, context, anim_filepath):
    # Check active object first
    arm_obj = context.active_object
    print(f"[CGF] active_object: {arm_obj} type: {arm_obj.type if arm_obj else None}")
    if arm_obj and arm_obj.type == 'ARMATURE':
        return arm_obj, None

    # Search the whole scene
    print(f"[CGF] Scene objects: {[o.name+':'+o.type for o in context.scene.objects]}")
    for obj in context.scene.objects:
        if obj.type == 'ARMATURE':
            return obj, None

    # No armature — try to auto-import CGF from same folder
    cgf_path = _find_cgf_near(anim_filepath)
    print(f"[CGF] CGF found near anim: {cgf_path}")
    if not cgf_path:
        base = os.path.splitext(os.path.basename(anim_filepath))[0]
        operator.report({'ERROR'},
            f"No CGF/CGA found with name '{base}' in the same folder. "
            f"Expected: {base}.cgf or {base}.cga")
        return None, None

    print(f"[CGF] Auto-importing geometry: {cgf_path}")
    reader = cgf_reader.ChunkReader()
    try:
        archive = reader.read_file(cgf_path)
    except ValueError as e:
        operator.report({'ERROR'}, f"Failed to read CGF: {e}")
        return None, None

    print(f"[CGF] Archive: bone_anim_chunks={len(archive.bone_anim_chunks)} mesh={len(archive.mesh_chunks)}")

    file_name  = os.path.splitext(os.path.basename(cgf_path))[0]
    collection = bpy.data.collections.new(file_name)
    context.scene.collection.children.link(collection)

    arm_obj = None
    if archive.bone_anim_chunks:
        try:
            arm_obj, _ = build_armature(archive, collection)
            print(f"[CGF] build_armature result: {arm_obj}")
        except Exception as e:
            print(f"[CGF] build_armature FAILED: {e}")
            import traceback; traceback.print_exc()

        if arm_obj:
            arm_obj['cgf_source_path'] = cgf_path
            if archive.bone_name_list_chunks:
                name_list = archive.bone_name_list_chunks[0].name_list
                for bone in archive.bone_anim_chunks[0].bones:
                    bid   = bone.bone_id
                    bname = name_list[bid] if bid < len(name_list) else f"Bone_{bid}"
                    if arm_obj.pose and bname in arm_obj.pose.bones:
                        arm_obj.pose.bones[bname]['cry_ctrl_id'] = bone.ctrl_id

    # Get game root from addon preferences
    game_root_path = ""
    skip_collision_geometry = False
    try:
        prefs = bpy.context.preferences.addons.get('io_import_cgf')
        if prefs:
            game_root_path = prefs.preferences.game_root_path
            skip_collision_geometry = bool(getattr(prefs.preferences, "skip_collision_geometry", False))
    except Exception:
        pass

    blender_materials, _ = _build_material_cache(
        archive, cgf_path, True, game_root_path, skip_collision_geometry
    )

    for mc in archive.mesh_chunks:
        if skip_collision_geometry and _mesh_is_collision_like(mc, archive):
            continue
        node = archive.get_node(mc.header.chunk_id)
        build_mesh(mc, node, archive, collection,
                   import_materials=True, import_normals=True,
                   import_uvs=True, import_weights=True,
                   blender_materials=blender_materials, filepath=cgf_path,
                   skip_collision_geometry=skip_collision_geometry)

    if arm_obj is None:
        operator.report({'ERROR'},
            "CGF imported but no armature was created (file has no skeleton).")
        return None, None

    context.view_layer.objects.active = arm_obj
    return arm_obj, archive


def load_caf(operator, context, filepath, append=True):
    """Import a CAF animation file. Auto-imports CGF if no armature in scene."""

    arm_obj, auto_archive = _ensure_armature(operator, context, filepath)
    if arm_obj is None:
        return {'CANCELLED'}

    # Use the auto-imported archive directly if available (avoids re-reading CGF)
    if auto_archive is not None:
        geom_archive = auto_archive
    else:
        geom_archive = _build_geom_archive_from_armature(arm_obj)

    print(f"[CGF] Loading animation: {filepath}")
    reader = cgf_reader.ChunkReader()
    try:
        anim_archive = reader.read_file(filepath)
    except ValueError as e:
        operator.report({'ERROR'}, str(e)); return {'CANCELLED'}

    print(f"[CGF] Controllers: {len(anim_archive.controller_chunks)}")

    action_name = os.path.splitext(os.path.basename(filepath))[0]
    apply_animation(arm_obj, geom_archive, anim_archive, action_name)

    operator.report({'INFO'}, f"Animation '{action_name}' imported")
    return {'FINISHED'}


def load_cal(operator, context, filepath):
    """Import all animations from a CAL file. Auto-imports CGF if needed."""

    arm_obj, auto_archive = _ensure_armature(operator, context, filepath)
    if arm_obj is None:
        return {'CANCELLED'}

    if auto_archive is not None:
        geom_archive = auto_archive
    else:
        geom_archive = _build_geom_archive_from_armature(arm_obj)

    records = cgf_reader.read_cal_file(filepath)
    if not records:
        operator.report({'WARNING'}, "CAL file is empty or could not be parsed")
        return {'CANCELLED'}

    imported = 0
    for rec in records:
        caf_path = find_caf_file(rec.path, filepath,
                                  arm_obj.get('cgf_source_path', ''))
        if not caf_path:
            print(f"[CGF] CAF not found: {rec.path}"); continue
        reader = cgf_reader.ChunkReader()
        try:
            anim_archive = reader.read_file(caf_path)
        except Exception as e:
            print(f"[CGF] Failed {caf_path}: {e}"); continue

        apply_animation(arm_obj, geom_archive, anim_archive, rec.name)
        imported += 1

    operator.report({'INFO'}, f"Imported {imported}/{len(records)} animations from CAL")
    return {'FINISHED'}


def _build_geom_archive_from_armature(arm_obj):
    """
    Reconstruct a minimal CryChunkArchive from an imported armature
    so we can match controller IDs to bone names during CAF import.
    Bone ctrl_ids are stored as custom properties on pose bones.
    Falls back to re-reading the source CGF if pose data is unavailable.
    """
    archive = cgf_reader.CryChunkArchive()
    archive.geom_file_name = arm_obj.get('cgf_source_path', '')

    # Try to reload from source CGF first — most reliable
    source_path = arm_obj.get('cgf_source_path', '')
    if source_path and os.path.isfile(source_path):
        try:
            reader = cgf_reader.ChunkReader()
            src = reader.read_file(source_path)
            archive.bone_anim_chunks      = src.bone_anim_chunks
            archive.bone_name_list_chunks = src.bone_name_list_chunks
            return archive
        except Exception as e:
            print(f"[CGF] Could not reload source CGF: {e}")

    # Fallback: build from pose bones + stored ctrl_ids
    bac  = cgf_reader.CryBoneAnimChunk()
    bac.header  = cgf_reader.ChunkHeader()
    bnlc = cgf_reader.CryBoneNameListChunk()
    bnlc.header = cgf_reader.ChunkHeader()

    pose_bones = arm_obj.pose.bones if arm_obj.pose else []
    for i, pbone in enumerate(pose_bones):
        bone = cgf_reader.CryBone()
        bone.bone_id = i
        bone.name    = pbone.name
        bone.ctrl_id = pbone.get('cry_ctrl_id', 'FFFFFFFF')
        bac.bones.append(bone)
        bnlc.name_list.append(pbone.name)

    archive.bone_anim_chunks.append(bac)
    archive.bone_name_list_chunks.append(bnlc)
    return archive
