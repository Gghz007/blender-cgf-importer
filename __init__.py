bl_info = {
    "name": "CryEngine 1 CGF Importer/Exporter (Far Cry)",
    "author": "Ported from Takaro CryImporter for 3ds Max",
    "version": (1, 2, 0),
    "blender": (4, 0, 0),
    "location": "File > Import/Export > CryEngine",
    "description": "Import/Export CryEngine 1 / Far Cry geometry and animation files",
    "category": "Import-Export",
}

import bpy
import os
from bpy.props import StringProperty, BoolProperty, EnumProperty
from bpy.types import AddonPreferences
from bpy_extras.io_utils import ImportHelper, ExportHelper
from . import cgf_reader
from . import cgf_builder
from . import cgf_exporter


# ── Addon Preferences ─────────────────────────────────────────────────────────

class CGFAddonPreferences(AddonPreferences):
    bl_idname = __name__

    game_root_path: StringProperty(
        name="Game Root Path",
        description="Root folder of Far Cry installation (where Objects/, Textures/ etc. are). "
                    "Used globally for all CGF imports to find textures automatically.",
        default="",
        subtype='DIR_PATH',
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Far Cry / CryEngine 1 Settings:", icon='SETTINGS')
        layout.prop(self, "game_root_path")
        if not self.game_root_path:
            layout.label(text="⚠  Set this to your Far Cry install folder (e.g. C:\\FarCry)",
                         icon='ERROR')


def get_game_root_path():
    """Get the globally configured game root path from addon preferences."""
    prefs = bpy.context.preferences.addons.get(__name__)
    if prefs:
        return prefs.preferences.game_root_path
    return ""


# ── CGF / CGA geometry importer ───────────────────────────────────────────────

class ImportCGF(bpy.types.Operator, ImportHelper):
    """Import CryEngine 1 CGF/CGA geometry file (Far Cry)"""
    bl_idname  = "import_scene.cgf"
    bl_label   = "Import CGF/CGA"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".cgf"
    filter_glob: StringProperty(default="*.cgf;*.cga", options={'HIDDEN'})

    import_materials: BoolProperty(name="Import Materials",
        description="Create Principled BSDF materials", default=True)
    import_normals: BoolProperty(name="Import Normals",
        description="Use normals from file", default=True)
    import_uvs: BoolProperty(name="Import UVs",
        description="Import texture coordinates", default=True)
    import_skeleton: BoolProperty(name="Import Skeleton",
        description="Build armature from bone chunks", default=False)
    import_weights: BoolProperty(name="Import Vertex Weights",
        description="Assign bone weights for skinned meshes", default=False)
    game_root_override: StringProperty(
        name="Override Game Root",
        description="Override the global Game Root Path for this import only. "
                    "Leave empty to use the path from Addon Preferences.",
        default="",
        subtype='DIR_PATH',
    )

    def execute(self, context):
        # Use per-import override if set, otherwise fall back to global prefs
        game_root = self.game_root_override.strip() or get_game_root_path()
        result = cgf_builder.load(
            self, context,
            filepath         = self.filepath,
            import_materials = self.import_materials,
            import_normals   = self.import_normals,
            import_uvs       = self.import_uvs,
            import_skeleton  = self.import_skeleton,
            import_weights   = self.import_weights,
            game_root_path   = game_root,
        )
        if result == {'FINISHED'}:
            for obj in context.scene.objects:
                if obj.type == 'ARMATURE' and not obj.get('cgf_source_path'):
                    obj['cgf_source_path'] = self.filepath
                    _store_ctrl_ids(obj)
        return result

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Geometry", icon='MESH_DATA')
        box.prop(self, "import_uvs")
        box.prop(self, "import_normals")
        box.prop(self, "import_materials")
        box = layout.box()
        box.label(text="Skinning", icon='ARMATURE_DATA')
        box.prop(self, "import_skeleton")
        box.prop(self, "import_weights")
        box = layout.box()
        box.label(text="Textures", icon='TEXTURE')
        global_root = get_game_root_path()
        if global_root:
            box.label(text=f"Global root: {global_root}", icon='CHECKMARK')
        else:
            box.label(text="No global root set (see Addon Preferences)", icon='ERROR')
        box.prop(self, "game_root_override")


def _store_ctrl_ids(arm_obj):
    source_path = arm_obj.get('cgf_source_path', '')
    if not source_path:
        return
    try:
        reader = cgf_reader.ChunkReader()
        archive = reader.read_file(source_path)
        if archive.bone_anim_chunks:
            name_list = archive.bone_name_list_chunks[0].name_list \
                        if archive.bone_name_list_chunks else []
            for bone in archive.bone_anim_chunks[0].bones:
                bid   = bone.bone_id
                bname = name_list[bid] if bid < len(name_list) else f"Bone_{bid}"
                if bname in arm_obj.pose.bones:
                    arm_obj.pose.bones[bname]['cry_ctrl_id'] = bone.ctrl_id
    except Exception as e:
        print(f"[CGF] Could not store ctrl_ids: {e}")


# ── CAF animation importer ────────────────────────────────────────────────────

class ImportCAF(bpy.types.Operator, ImportHelper):
    """Import CryEngine 1 CAF animation file onto the active armature"""
    bl_idname  = "import_scene.caf"
    bl_label   = "Import CAF Animation"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".caf"
    filter_glob: StringProperty(default="*.caf", options={'HIDDEN'})

    append: BoolProperty(name="Append to Timeline",
        description="Add after existing animation range", default=True)

    def execute(self, context):
        return cgf_builder.load_caf(self, context, self.filepath, self.append)

    def draw(self, context):
        self.layout.prop(self, "append")


# ── CAL animation list importer ───────────────────────────────────────────────

class ImportCAL(bpy.types.Operator, ImportHelper):
    """Import all animations from a CryEngine 1 CAL file"""
    bl_idname  = "import_scene.cal"
    bl_label   = "Import CAL Animation List"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".cal"
    filter_glob: StringProperty(default="*.cal", options={'HIDDEN'})

    def execute(self, context):
        return cgf_builder.load_cal(self, context, self.filepath)


# ── CGF exporter ──────────────────────────────────────────────────────────────

class ExportCGF(bpy.types.Operator, ExportHelper):
    """Export to CryEngine 1 CGF geometry file (Far Cry)"""
    bl_idname  = "export_scene.cgf"
    bl_label   = "Export CGF"
    bl_options = {'PRESET'}

    filename_ext = ".cgf"
    filter_glob: StringProperty(default="*.cgf", options={'HIDDEN'})

    export_materials: BoolProperty(name="Export Materials",
        description="Write material chunks", default=True)
    export_skeleton: BoolProperty(name="Export Skeleton",
        description="Write bone chunks from active armature", default=True)
    export_weights: BoolProperty(name="Export Vertex Weights",
        description="Write physique (bone weights)", default=True)
    selected_only: BoolProperty(name="Selected Only",
        description="Export only selected mesh objects", default=False)

    def execute(self, context):
        return cgf_exporter.export_cgf(
            self, context, self.filepath,
            export_materials = self.export_materials,
            export_skeleton  = self.export_skeleton,
            export_weights   = self.export_weights,
            selected_only    = self.selected_only,
        )

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Geometry", icon='MESH_DATA')
        box.prop(self, "selected_only")
        box.prop(self, "export_materials")
        box = layout.box()
        box.label(text="Skinning", icon='ARMATURE_DATA')
        box.prop(self, "export_skeleton")
        box.prop(self, "export_weights")


# ── CAF exporter ──────────────────────────────────────────────────────────────

class ExportCAF(bpy.types.Operator, ExportHelper):
    """Export active action to CryEngine 1 CAF animation file"""
    bl_idname  = "export_scene.caf"
    bl_label   = "Export CAF Animation"
    bl_options = {'PRESET'}

    filename_ext = ".caf"
    filter_glob: StringProperty(default="*.caf", options={'HIDDEN'})

    def execute(self, context):
        return cgf_exporter.export_caf(self, context, self.filepath)


# ── CAL exporter ──────────────────────────────────────────────────────────────

class ExportCAL(bpy.types.Operator, ExportHelper):
    """Export all actions to CAF files and write a CAL list"""
    bl_idname  = "export_scene.cal"
    bl_label   = "Export CAL Animation List"
    bl_options = {'PRESET'}

    filename_ext = ".cal"
    filter_glob: StringProperty(default="*.cal", options={'HIDDEN'})

    def execute(self, context):
        return cgf_exporter.export_cal(self, context, self.filepath)


# ── Menu entries ──────────────────────────────────────────────────────────────

def menu_import(self, context):
    self.layout.operator(ImportCGF.bl_idname, text="CryEngine Geometry (.cgf, .cga)")
    self.layout.operator(ImportCAF.bl_idname, text="CryEngine Animation (.caf)")
    self.layout.operator(ImportCAL.bl_idname, text="CryEngine Animation List (.cal)")


def menu_export(self, context):
    self.layout.operator(ExportCGF.bl_idname, text="CryEngine Geometry (.cgf)")
    self.layout.operator(ExportCAF.bl_idname, text="CryEngine Animation (.caf)")
    self.layout.operator(ExportCAL.bl_idname, text="CryEngine Animation List (.cal)")


def register():
    bpy.utils.register_class(CGFAddonPreferences)
    bpy.utils.register_class(ImportCGF)
    bpy.utils.register_class(ImportCAF)
    bpy.utils.register_class(ImportCAL)
    bpy.utils.register_class(ExportCGF)
    bpy.utils.register_class(ExportCAF)
    bpy.utils.register_class(ExportCAL)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    bpy.utils.unregister_class(CGFAddonPreferences)
    bpy.utils.unregister_class(ImportCGF)
    bpy.utils.unregister_class(ImportCAF)
    bpy.utils.unregister_class(ImportCAL)
    bpy.utils.unregister_class(ExportCGF)
    bpy.utils.unregister_class(ExportCAF)
    bpy.utils.unregister_class(ExportCAL)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)


if __name__ == "__main__":
    register()
