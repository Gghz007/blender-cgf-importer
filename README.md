# CryEngine 1 CGF Importer/Exporter for Blender

A Blender addon for importing and exporting **CryEngine 1 / Far Cry (2004)** geometry and animation files.

Ported from the original [CryImporter for 3ds Max 8](https://www.takaro.net) by Takaro Pty. Ltd.

---

## Features

### Import
- `.cgf` and `.cga` geometry files (mesh, UV, normals, materials, skeleton, weights)
- `.caf` animation files onto existing armatures
- `.cal` animation list files (multiple animations at once)
- **Automatic texture loading** — set your Far Cry folder once in Addon Preferences
- Supports `.dds`, `.tga`, `.tif`, `.tiff`, `.png`, `.jpg`, `.bmp` texture formats
- DDN normal maps → Normal Map node; `_bump` heightmaps → Bump node
- Gloss packed in diffuse alpha (`GlossAlpha` shaders) → Specular IOR Level

### Export
- `.cgf` geometry files (mesh + materials + skeleton + weights)
- `.caf` animation files from Blender Actions
- `.cal` animation list (exports all Actions as CAF files)
- Correct material name format with shader: `name(ShaderName)/surface`
- Bone initial pos matrices preserved from original file (perfect round-trip)
- Vertex colors (white by default — required by engine)

### N-Panel (View3D → N → CryEngine)
- Per-material **Shader** selector with all Far Cry shader presets
- Per-material **Surface / Physics** selector with all Far Cry surface types
- Shows current CGF material name in real-time

### General
- Correct scale: Max inches ↔ Blender meters (`× 0.0254`)
- Supports Blender 4.0, 4.1, 4.2, 5.0+

---

## Installation

1. Download `io_import_cgf.zip` from [Releases](../../releases)
2. In Blender: **Edit → Preferences → Add-ons → Install...**
3. Select the downloaded `.zip` file
4. Enable **"CryEngine 1 CGF Importer/Exporter (Far Cry)"**
5. Click the arrow next to the addon name and set **Game Root Path** to your Far Cry folder (e.g. `C:\FarCry`)

---

## Setup — Game Root Path

**Edit → Preferences → Add-ons → CryEngine 1 CGF → Game Root Path**

Set this to the root of your Far Cry installation — the folder that contains `Objects\`, `Textures\`, `Levels\` etc.

```
C:\FarCry\             ← set this
├── Objects\
│   └── Characters\
│       └── model.cgf
└── Textures\
    └── texture.tif
```

Textures are stored in CGF as relative paths (e.g. `Objects\Characters\...\texture.dds`). The addon tries all supported formats automatically — so `.dds` references will also find `.tif` files.

---

## Import Usage

### CGF / CGA — Geometry

**File → Import → CryEngine Geometry (.cgf, .cga)**

| Option | Default | Description |
|---|---|---|
| Import UVs | ✓ | Import texture coordinates |
| Import Normals | ✓ | Use normals from file |
| Import Materials | ✓ | Create Principled BSDF materials and load textures |
| Import Skeleton | ✓ | Build armature from bone chunks |
| Import Vertex Weights | ✓ | Assign bone weights |
| Override Game Root | — | Override the global path for this import only |

**Workflow for characters:**
1. Import CGF with skeleton enabled
2. Then import CAF or CAL animations on the existing armature

---

### CAF — Animation

**File → Import → CryEngine Animation (.caf)**

Imports one animation onto the active/existing armature in the scene.
If no armature exists, automatically imports the matching CGF from the same folder.

---

### CAL — Animation List

**File → Import → CryEngine Animation List (.cal)**

Imports all animations listed in the CAL file as separate Blender Actions.
Switch between them in the **Action Editor**.

---

## Export Usage

### CGF — Geometry

**File → Export → CryEngine Geometry (.cgf)**

| Option | Default | Description |
|---|---|---|
| Selected Only | — | Export only selected mesh objects |
| Export Materials | ✓ | Write material chunks |
| Export Skeleton | ✓ | Write bone chunks from visible armature |
| Export Vertex Weights | ✓ | Write physique (bone weights) |

**Round-trip workflow (import → modify → export):**
1. Import CGF — bone matrices and material shader names are stored automatically
2. Modify mesh/materials in Blender
3. Export — all original data is preserved correctly

---

### CAF — Animation

**File → Export → CryEngine Animation (.caf)**

Select the armature, set the active Action in the Action Editor, then export.

---

### CAL — Animation List

**File → Export → CryEngine Animation List (.cal)**

Exports all Actions with bone curves as individual CAF files + a CAL list file.

---

## Material Setup (N-Panel)

Open the **N panel → CryEngine** tab in the 3D viewport. Select a mesh object to see and edit its active material's CryEngine properties.

### Shaders

| Shader | Use for |
|---|---|
| `TemplModelCommon` | Standard props, weapons, vehicles — no bump map |
| `TemplBumpDiffuse` | Props with normal map, no specular |
| `TemplBumpSpec` | Props with normal map + specular |
| `TemplBumpSpec_GlossAlpha` | Characters, props with gloss in diffuse alpha |
| `TemplBumpSpec_HP_GlossAlpha` | Hi-poly characters with gloss in alpha |
| `Phong` | Simple shading, legacy objects |
| `NoDraw` | Invisible — collision/physics geometry only |
| `Glass` | Windows, transparent surfaces |
| `Vegetation` | Trees, bushes, foliage |
| `Terrain` | Terrain blend layers |

### Surface / Physics

| Surface | Use for |
|---|---|
| `mat_default` | General purpose — most props |
| `mat_metal` | Generic metal |
| `mat_metal_plate` | Metal panels, armor plates |
| `mat_metal_pipe` | Pipes, rails |
| `mat_concrete` | Concrete walls, floors |
| `mat_rock` | Rock, stone |
| `mat_wood` | Wood planks, crates |
| `mat_grass` | Grass, ground |
| `mat_sand` | Sand, dirt |
| `mat_water` | Water surfaces |
| `mat_glass` | Glass |
| `mat_flesh` | Character body (organic) |
| `mat_head` | Character head |
| `mat_helmet` | Hard hat / helmet |
| `mat_armor` | Armor, hard protection |
| `mat_arm` | Character arm |
| `mat_leg` | Character leg |
| `mat_cloth` | Fabric, clothing |

**Material name format in CGF:**
```
materialname(ShaderName)/surfaceName
```
Example: `s_mut_abrr(TemplBumpSpec_GlossAlpha)/mat_default`

The N-panel builds this automatically. Without a valid shader name the engine will not render the mesh.

---

## Texture Slots

| CGF Slot | Name | Usage |
|---|---|---|
| 1 | Diffuse | Main color texture (`.dds`) |
| 4 | Bump | Normal map — suffix `_ddn` |
| 9 | Detail | Height/bump map — suffix `_bump` |

---

## Coordinate System & Scale

- **Scale:** `1 Max inch = 0.0254 Blender meters` — applied automatically
- **Axes:** Z-up in both CryEngine and Blender — compatible
- **Bone matrices:** Original world-space matrices preserved for correct round-trip export

---

## Supported Chunk Types

| Chunk | Description |
|---|---|
| `0x0000` Mesh | Geometry, UVs, normals, vertex colors, bone weights, BoneInitialPos |
| `0x000B` Node | Scene hierarchy and transform |
| `0x000C` Material | Colors, shader name, texture paths (v745/v746) |
| `0x0003` BoneAnim | Skeleton definition (152 bytes/bone) |
| `0x0005` BoneNameList | Bone names |
| `0x000D` Controller | Animation keys (v826/v827) |
| `0x000F` BoneMesh | Bone physics collision meshes |
| `0x0011` MeshMorphTarget | Shape keys / facial expressions |
| `0x0012` BoneInitialPos | Bone rest pose matrices (embedded in Mesh chunk) |
| `0x000E` Timing | Animation timing / FPS |
| `0x0013` SourceInfo | Source file metadata |

---

## Known Limitations

- BSpline controller types not supported (rare in Far Cry assets)
- VertAnim (vertex animation) not exported
- Bone physics constraints exported as zeros (engine uses defaults)
- New rigs from scratch may need bone matrix adjustment for perfect skinning

---

## Credits

- Original **CryImporter for 3ds Max** by [Takaro Pty. Ltd.](https://www.takaro.net)
- Binary format verified against original Far Cry game files
- Blender addon port — this project

---

## License

Based on CryImporter by Takaro Pty. Ltd. — original license terms apply.
Free for non-commercial use. See `LICENSE` for details.
