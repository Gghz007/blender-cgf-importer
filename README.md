# CryEngine 1 CGF Importer for Blender

A Blender addon for importing **CryEngine 1 / Far Cry (2004)** geometry and animation files into Blender 4.0+.

Ported from the original [CryImporter for 3ds Max 8](https://www.takaro.net) by Takaro Pty. Ltd.

---

## Features

- Import `.cgf` and `.cga` geometry files
- Import `.caf` animation files onto armatures
- Import `.cal` animation list files (multiple animations at once)
- Mesh with correct vertex positions, normals, and UV coordinates
- Multi-material support (single and multi-sub materials)
- Texture auto-detection from file path (`.dds`, `.tga`, `.png`)
- Skeleton / armature import from bone chunks
- Vertex weights (skinning) for character models
- Shape keys (morph targets / facial expressions)
- Full animation support: CryBone, Linear, Bezier, TCB controller types (v826 and v827)
- Correct scale conversion: Max inches → Blender meters (`× 0.0254`) applied to geometry AND animation
- Supports Blender 4.0, 4.1, 4.2, 5.0+

---

## Installation

1. Download `io_import_cgf.zip` from [Releases](../../releases)
2. In Blender: **Edit → Preferences → Add-ons → Install...**
3. Select the downloaded `.zip` file
4. Enable **"CryEngine 1 CGF Importer (Far Cry)"**

---

## Usage

### Geometry

**File → Import → CryEngine Geometry (.cgf, .cga)**

| Option | Description |
|---|---|
| Import UVs | Import texture coordinates |
| Import Normals | Use normals from file |
| Import Materials | Create Principled BSDF materials from chunk data |
| Import Skeleton | Build armature from bone chunks |
| Import Vertex Weights | Assign bone weights for skinned meshes |

### Animation

**Important:** Import the CGF geometry file first, then import animation with the armature selected.

**File → Import → CryEngine Animation (.caf)** — single animation file

**File → Import → CryEngine Animation List (.cal)** — imports all animations listed in the CAL file as separate Actions

### Workflow example

```
1. File → Import → CryEngine Geometry (.cgf)     ← imports mesh + skeleton
2. Select the armature in the viewport
3. File → Import → CryEngine Animation (.caf)     ← imports animation onto armature
   or
   File → Import → CryEngine Animation List (.cal) ← imports all animations at once
```

### Tips

- Place textures in the same folder as the `.cgf` file for auto-detection
- Far Cry textures are `.dds` — Blender can load them natively
- After CAF import, switch to the **Action Editor** to see and switch between imported animations
- Scale is applied to geometry and animation data automatically — object scale stays `(1, 1, 1)`

---

## Supported Chunk Types

| Chunk | Description |
|---|---|
| `0x0000` Mesh | Geometry, UVs, normals, vertex colors |
| `0x000B` Node | Scene hierarchy and world transform |
| `0x000C` Material | Colors and texture references |
| `0x0003` BoneAnim | Skeleton definition |
| `0x0005` BoneNameList | Bone names |
| `0x000D` Controller | Animation keys (v826 and v827) |
| `0x000F` BoneMesh | Bone physics meshes |
| `0x0011` MeshMorphTarget | Shape keys / facial expressions |
| `0x0012` BoneInitialPos | Bone rest pose matrices |
| `0x000E` Timing | Animation timing / FPS info |

### Supported controller types (v826)

| Type | Description |
|---|---|
| CryBone | Position + quaternion per bone |
| Linear3 / LinearQ | Linear interpolation (position / rotation) |
| Bezier3 / BezierQ | Bezier interpolation (position / rotation) |
| TCB3 / TCBQ | Tension-Continuity-Bias (position / rotation) |

---

## Coordinate System & Scale

CryEngine 1 / Far Cry was authored in **3ds Max with inches** as the unit system.

- **Scale:** `1 Max inch = 0.0254 Blender meters` — applied automatically to all geometry and animation data
- **Axes:** Max Z-up → Blender Z-up (compatible, no rotation needed — node transforms handle orientation)
- **Object scale:** always `(1, 1, 1)` — scale is baked into coordinates, not the object transform

---

## Known Limitations

- BSpline controller types not supported (rare in Far Cry assets, complex to implement)
- VertAnim (vertex animation) chunks not yet applied
- Physics-only meshes are skipped
- Some very early CGF format versions may not parse correctly

---

## File Format

The CGF format is a chunk-based binary format developed by Crytek.
File versions supported: `0x0744`, `0x0745`, `0x0746`, `0x0826`, `0x0827`

Format reference based on reverse engineering by the modding community and the original CryImporter source by Takaro Pty. Ltd.

---

## Credits

- Original **CryImporter for 3ds Max** by [Takaro Pty. Ltd.](https://www.takaro.net) — binary format parsing and animation logic ported directly from their MaxScript
- Blender addon port and Python rewrite — this project

---

## License

This software is provided **as-is**, without any express or implied warranty.
Based on CryImporter by Takaro Pty. Ltd. — original license terms apply to the format knowledge derived from that work.

Free for non-commercial use. See `LICENSE` for details.
