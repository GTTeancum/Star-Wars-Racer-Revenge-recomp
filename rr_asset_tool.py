#!/usr/bin/env python3
"""
Star Wars: Racer Revenge — Asset Tool  v1.2

All-in-one GUI tool for extracting, converting, and assembling Racer Revenge assets.

Features:
  - Extract .RES archives (zlib compressed, virtual sectors)
  - Convert .PSG geometry to .OBJ (world-space, per-bone transforms)
  - Export .PSG characters to .FBX (skeleton + rigid skinning)
  - Convert .PST textures to .TGA (32-bit with separate alpha):
      Format 0/1: 8-bit palettized (CLUT at offset 12, CSM1 deswizzle)
      Format 2: IPU compressed (pure Python MPEG2 intra decoder — no external deps)
  - Batch convert all assets in a folder
  - Assemble full tracks from SCB placements + geometry

Requirements:
  - Python 3.8+
  - Pillow (pip install Pillow) — for texture conversion
  - NumPy (pip install numpy) — OPTIONAL, speeds up IPU texture YCbCr→RGB conversion ~50x
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import os, sys, collections, struct, zlib, threading, math, glob, shutil


# ═════════════════════════════════════════════════════════════════════════════
#  PSG/PST/OBJ library (from rb_convert.py v3.3)
# ═════════════════════════════════════════════════════════════════════════════

# ── Vertex-type flags ─────────────────────────────────────────────────────────
HAS_XYZ     = 0x0001
HAS_NORMAL  = 0x0002
HAS_UV1     = 0x0004
HAS_RGBA    = 0x0008
HAS_UV2     = 0x0010
HAS_WEIGHTS = 0x1000

# ── VIF UNPACK element sizes ──────────────────────────────────────────────────
_ELEM_SZ = {
    0x0:4, 0x4:8, 0x5:4, 0x6:2,
    0x8:12, 0x9:6, 0xA:3,
    0xC:16, 0xD:8, 0xE:4,
}

# ─────────────────────────────────────────────────────────────────────────────
#  Matrix helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mat_mul(A, B):
    """Row-major 4×4 matrix multiply: C = A * B"""
    C = [0.0]*16
    for i in range(4):
        for j in range(4):
            C[i*4+j] = sum(A[i*4+k]*B[k*4+j] for k in range(4))
    return tuple(C)

def _world_mat(obj_idx, objects):
    """Concatenate matrices up the parent chain to produce bone world matrix.
    Neutralizes root-bone coordinate-system mirrors (e.g. -1,1,-1 diagonal)
    since OBJ export should stay in the authoring tool's right-hand coords."""
    _IDENTITY = (1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1)

    def _is_root_mirror(mat):
        tx, ty, tz = mat[12], mat[13], mat[14]
        if abs(tx) < 0.001 and abs(ty) < 0.001 and abs(tz) < 0.001:
            diag = (mat[0], mat[5], mat[10])
            if any(abs(d - 1.0) > 0.01 for d in diag):
                return True
        return False

    m = objects[obj_idx]['mat']
    # If this bone IS the root and has a mirror, neutralize it
    if objects[obj_idx]['parent'] == -1 and _is_root_mirror(m):
        m = _IDENTITY
    p = objects[obj_idx]['parent']
    while p != -1:
        parent_mat = objects[p]['mat']
        # If the parent is the root and has a mirror, neutralize it
        if objects[p]['parent'] == -1 and _is_root_mirror(parent_mat):
            parent_mat = _IDENTITY
        m = _mat_mul(parent_mat, m)
        p = objects[p]['parent']
    return m

def _xform_pt(x, y, z, mat):
    """Transform point by D3D row-major matrix: v_world = v_local * mat"""
    return (
        x*mat[0] + y*mat[4] + z*mat[8]  + mat[12],
        x*mat[1] + y*mat[5] + z*mat[9]  + mat[13],
        x*mat[2] + y*mat[6] + z*mat[10] + mat[14],
    )

def _xform_nrm(nx, ny, nz, mat):
    """Transform normal (rotation only, no translation)."""
    return (
        nx*mat[0] + ny*mat[4] + nz*mat[8],
        nx*mat[1] + ny*mat[5] + nz*mat[9],
        nx*mat[2] + ny*mat[6] + nz*mat[10],
    )

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rd(buf, pos, fmt):
    v = struct.unpack_from(fmt, buf, pos)
    return (v[0] if len(v)==1 else v), pos + struct.calcsize(fmt)

def _gcd(a, b):
    while b: a, b = b, a % b
    return a

# ─────────────────────────────────────────────────────────────────────────────
#  VIF / DMA packet decoder
# ─────────────────────────────────────────────────────────────────────────────

def _decode_packets(dma, xyz_d, bbcx, bbcy, bbcz, nrm_d, st_d):
    """
    Decode a PSG DMA packet stream.
    Returns (positions, normals, uvs, adc_flags, raw_w_values).

    Each DMA packet contains VIF UNPACK commands at specific VU offsets:
      vu=0          : GIF tag (V4_32 × 1) — skip
      vu=1          : XYZ+W   (V4_16 × N) — positions + bone/ADC in W
      vu=1+N        : Normals (V3_8  × N)
      vu=1+2N       : UVs     (V2_16 × N)
      [optional]    : RGBA    (V4_8  × N, unsigned)
      VIF MSCNT     : end of packet

    The W component of XYZ encodes: W = (bonePaletteIndex + 1) * boneSize
    with sign bit as ADC (strip restart): negative W = ADC set.
    Every vertex's W is a valid per-vertex bone reference — there is no
    distinction between "single-bone" and "multi-bone" packets.
    """
    positions  = []
    normals    = []
    uvs        = []
    adc_flags  = []
    raw_w_vals = []

    dp, dlen = 0, len(dma)
    while dp < dlen - 3:
        dp = (dp + 3) & ~3
        if dp + 4 > dlen: break
        w, = struct.unpack_from('<I', dma, dp)
        hi  = (w >> 24) & 0xFF
        cnt = (w >> 16) & 0xFF
        vu  = w & 0x3FF

        if hi in (0x30, 0x60, 0x70, 0x50): dp += 16; continue
        if hi in (0x00, 0x01, 0x17):        dp += 4;  continue

        if (hi & 0xE0) == 0x60:
            fmt  = hi & 0x1F
            esz  = _ELEM_SZ.get(fmt, 0)
            if esz == 0 or cnt == 0: dp += 4; continue
            raw  = dma[dp+4 : dp+4+cnt*esz]
            dp  += 4 + cnt*esz

            # GIF tag slot: V4_32 at vu=0 with cnt=1 — skip
            if vu == 0 and fmt == 0xC and cnt == 1:
                continue

            # XYZ+W — V4_16 at vu=1 (the standard quantized vertex format)
            if fmt == 0xD and vu == 1 and len(raw) >= cnt*8:
                for i in range(cnt):
                    x16, y16, z16, w16 = struct.unpack_from('<4h', raw, i*8)
                    positions.append((x16*xyz_d+bbcx, y16*xyz_d+bbcy, z16*xyz_d+bbcz))
                    adc_flags.append(w16 < 0)
                    raw_w_vals.append(abs(w16))
                continue

            # XYZ+W — V4_32 at vu=1 (uncompressed 24-bit xyzBits fallback)
            if fmt == 0xC and vu == 1 and cnt > 1:
                for i in range(cnt):
                    xi, yi, zi, wi = struct.unpack_from('<4i', raw, i*16)
                    positions.append((xi*xyz_d+bbcx, yi*xyz_d+bbcy, zi*xyz_d+bbcz))
                    adc_flags.append(wi < 0)
                    raw_w_vals.append(abs(wi))
                continue

            # XYZ+W — V4_8 at vu=1 (8-bit quantized, rare)
            if fmt == 0xE and vu == 1:
                for i in range(cnt):
                    x8, y8, z8, w8 = struct.unpack_from('<4b', raw, i*4)
                    positions.append((x8*xyz_d+bbcx, y8*xyz_d+bbcy, z8*xyz_d+bbcz))
                    adc_flags.append(w8 < 0)
                    raw_w_vals.append(abs(w8))
                continue

            # Normals — V3_8 (standard 8-bit quantized)
            if fmt == 0xA:
                for i in range(cnt):
                    nx, ny, nz = struct.unpack_from('<3b', raw, i*3)
                    normals.append((nx*nrm_d, ny*nrm_d, nz*nrm_d))
                continue

            # Normals — V3_16
            if fmt == 0x9:
                for i in range(cnt):
                    nx, ny, nz = struct.unpack_from('<3h', raw, i*6)
                    normals.append((nx*nrm_d, ny*nrm_d, nz*nrm_d))
                continue

            # Normals — V3_32 float
            if fmt == 0x8:
                for i in range(cnt):
                    nx, ny, nz = struct.unpack_from('<3f', raw, i*12)
                    normals.append((nx, ny, nz))
                continue

            # UVs — V2_16 (standard 16-bit quantized)
            if fmt == 0x5:
                for i in range(cnt):
                    u, v = struct.unpack_from('<2h', raw, i*4)
                    uvs.append((u*st_d, v*st_d))
                continue

            # UVs — V2_8
            if fmt == 0x6:
                for i in range(cnt):
                    u, v = struct.unpack_from('<2b', raw, i*2)
                    uvs.append((u*st_d, v*st_d))
                continue

            # RGBA / bone indices / weights — skip (handled by VU1 microprogram)
            continue
        dp += 4

    return positions, normals, uvs, adc_flags, raw_w_vals


def _build_triangles(positions, adc_flags, normals=None):
    """Convert PS2 triangle-strip with ADC restart bits to triangle list.
    If normals are provided, auto-corrects winding by comparing face normals
    against vertex normals. This fixes ~8% of faces that are inverted due to
    double-ADC strip restart sequences in the VU1→GS pipeline."""
    triangles = []
    n = len(positions)
    if n < 3: return triangles
    i = 0
    while i < n:
        j = i
        while j < n:
            if j > i+1 and adc_flags[j]: break
            j += 1
        for k in range(j - i - 2):
            v0, v1, v2 = i+k, i+k+1, i+k+2
            if adc_flags[v2]: continue
            if k % 2 == 0: triangles.append((v0, v1, v2))
            else:           triangles.append((v0, v2, v1))
        i = j

    # Auto-correct winding using vertex normals
    if normals and len(normals) == len(positions):
        corrected = []
        for a, b, c in triangles:
            pa, pb, pc = positions[a], positions[b], positions[c]
            # Face normal via cross product: (pb-pa) × (pc-pa)
            e1 = (pb[0]-pa[0], pb[1]-pa[1], pb[2]-pa[2])
            e2 = (pc[0]-pa[0], pc[1]-pa[1], pc[2]-pa[2])
            fn = (e1[1]*e2[2] - e1[2]*e2[1],
                  e1[2]*e2[0] - e1[0]*e2[2],
                  e1[0]*e2[1] - e1[1]*e2[0])
            fn_sq = fn[0]*fn[0] + fn[1]*fn[1] + fn[2]*fn[2]
            if fn_sq < 1e-20:
                corrected.append((a, b, c))
                continue
            # Average vertex normal
            na, nb, nc = normals[a], normals[b], normals[c]
            vn = (na[0]+nb[0]+nc[0], na[1]+nb[1]+nc[1], na[2]+nb[2]+nc[2])
            dot = fn[0]*vn[0] + fn[1]*vn[1] + fn[2]*vn[2]
            if dot < 0:
                corrected.append((b, a, c))  # flip winding
            else:
                corrected.append((a, b, c))
        return corrected

    return triangles


def _infer_bone_size(all_w_vals):
    """
    Derive boneSize from W values.
    W = (bonePaletteIndex + 1) * boneSize, so boneSize = GCD of all W values.
    Returns 16 if no usable values found (safe default for Racer Revenge / Cars PS2).
    """
    nonzero = [v for v in all_w_vals if 0 < v < 32768]
    if not nonzero: return 16
    g = nonzero[0]
    for v in nonzero[1:]:
        g = _gcd(g, v)
        if g == 1: break
    # boneSize must be a reasonable power-of-2-ish value
    if g in (4, 8, 12, 16, 24, 32, 48, 64):
        return g
    # If GCD is small (e.g. due to noise), find smallest W and derive from that
    mn = min(nonzero)
    for bs in (16, 12, 8, 4, 24, 32):
        if mn % bs == 0: return bs
    return 16


# ─────────────────────────────────────────────────────────────────────────────
#  PSG parser
# ─────────────────────────────────────────────────────────────────────────────

Mesh = collections.namedtuple('Mesh', [
    'name', 'material', 'positions', 'normals', 'uvs', 'triangles'
])


def parse_psg(filepath):
    """Parse a .psg file.  Returns a list of Mesh namedtuples in world space."""
    with open(filepath, 'rb') as f:
        data = f.read()
    pos = 0

    magic = data[0:4]
    if magic[:3] not in (b'psg', b'pss', b'pgi'):
        raise ValueError(f"Not a PSG file (magic={magic!r})")
    pos = 4
    version, pos = _rd(data, pos, '<I')

    # Object hierarchy
    num_objects, pos = _rd(data, pos, '<I')
    objects = []
    for _ in range(num_objects):
        name = data[pos:pos+128].split(b'\x00')[0].decode('ascii','replace'); pos += 128
        mat  = struct.unpack_from('<16f', data, pos); pos += 64
        pos += 12 + 12 + 4  # bbox_c, bbox_h, radius (patched later, skip)
        parent, pos = _rd(data, pos, '<i')
        objects.append({'name': name, 'parent': parent, 'mat': mat})

    # Pre-compute world matrices for all bones
    world_mats = [_world_mat(i, objects) for i in range(num_objects)]

    # ProcessMaterials
    total_surfs, pos = _rd(data, pos, '<I')
    mat_names = []
    for _ in range(total_surfs):
        mn = data[pos:pos+64].split(b'\x00')[0].decode('ascii','replace'); pos += 64
        mat_names.append(mn)

    # Quantization
    xyz_d, nrm_d, st_d, _ = struct.unpack_from('<4f', data, pos); pos += 16
    bbcx, bbcy, bbcz = struct.unpack_from('<3f', data, pos); pos += 12
    fixed, pos = _rd(data, pos, '<i')

    # Flags
    num_lod    = data[pos]; anim = data[pos+1]; ci = data[pos+2]; pos += 4

    # Matrix palettes (animated only)
    if anim & 1:
        mp_flag, pos = _rd(data, pos, '<I')
        n_pals = mp_flag & 0x3FFFFFFF
        if mp_flag & 0x40000000:    # SIMPLE
            n_ids, pos = _rd(data, pos, '<I'); pos += n_ids
        elif mp_flag & 0x80000000:  # COMPRESSED
            for _ in range(n_pals):
                n_ids, pos = _rd(data, pos, '<I'); pos += n_ids
            pos += total_surfs
        else:
            n_ids, pos = _rd(data, pos, '<I'); pos += n_ids

    # ── First pass: collect all W values to determine boneSize ──────────────
    # W = (bonePaletteIndex + 1) * boneSize, so boneSize = GCD of all abs(W).
    # Only collect from V4_16 UNPACKs at vu=1 (the XYZ+W slot).
    boneSize = 0
    if not fixed:
        save_pos = pos
        all_w = []
        for lod_idx in range(num_lod):
            _alod, pos = _rd(data, pos, '<f')
            nsurf, pos = _rd(data, pos, '<I')
            for _ in range(nsurf):
                _mi, pos = _rd(data, pos, '<I')
                vt, pos  = _rd(data, pos, '<I')
                n_uv = (1 if vt&HAS_UV1 else 0) + (1 if vt&HAS_UV2 else 0)
                pos += n_uv*4 + 4
                dma_sz, pos = _rd(data, pos, '<I')
                dma = data[pos:pos+dma_sz]; pos += dma_sz
                dp = 0
                while dp < len(dma)-4:
                    dp = (dp + 3) & ~3
                    if dp + 4 > len(dma): break
                    wv, = struct.unpack_from('<I', dma, dp)
                    hi = (wv>>24)&0xFF
                    cnt = (wv>>16)&0xFF
                    vu_off = wv & 0x3FF
                    if hi in (0x30,0x60,0x70,0x50): dp+=16; continue
                    if hi in (0x00,0x01,0x17): dp+=4; continue
                    if (hi&0xE0)==0x60:
                        fmt=hi&0x1F
                        esz=_ELEM_SZ.get(fmt,0)
                        if fmt==0xD and vu_off==1 and cnt>0 and dp+4+cnt*8 <= len(dma):
                            for i in range(cnt):
                                av=abs(struct.unpack_from('<h',dma,dp+4+i*8+6)[0])
                                if 0<av<32768: all_w.append(av)
                        if esz>0: dp+=4+cnt*esz
                        else: dp+=4
                        continue
                    dp+=4
        boneSize = _infer_bone_size(all_w)
        pos = save_pos

    # ── Second pass: decode geometry ─────────────────────────────────────────
    meshes = []
    for lod_idx in range(num_lod):
        _alod, pos = _rd(data, pos, '<f')
        nsurf, pos = _rd(data, pos, '<I')

        for surf_idx in range(nsurf):
            mat_idx,    pos = _rd(data, pos, '<I')
            vertex_type,pos = _rd(data, pos, '<I')
            n_uv = (1 if vertex_type&HAS_UV1 else 0) + (1 if vertex_type&HAS_UV2 else 0)
            uv_scales = []
            for _ui in range(n_uv):
                _uvs, pos = _rd(data, pos, '<f')
                uv_scales.append(_uvs)
            _dbgv, pos = _rd(data, pos, '<H')
            _dbgs, pos = _rd(data, pos, '<H')
            dma_sz, pos = _rd(data, pos, '<I')
            dma_start = pos; pos += dma_sz

            if dma_sz == 0 or dma_start + dma_sz > len(data):
                continue

            dma = data[dma_start:dma_start+dma_sz]
            verts, nrms, uvs, adc, raw_w = _decode_packets(
                dma, xyz_d, bbcx, bbcy, bbcz, nrm_d, st_d
            )
            if not verts: continue

            # Note: per-surface uv_scale is the engine's runtime texture tiling
            # factor, NOT a vertex UV correction. st_d scaling in _decode_packets
            # already produces correct 0-1 UV coordinates for export.

            # Vertices are in BONE-LOCAL space. Each vertex's W value encodes
            # which bone it belongs to: bone_idx = abs(W) / boneSize - 1.
            # To produce a T-pose OBJ, transform each vertex by its bone's
            # world matrix (concatenated up the parent chain).
            bone_idx = 0
            if not fixed and boneSize > 0:
                wm_cache = {}
                new_verts = []
                new_nrms  = []
                has_n = len(nrms) == len(verts)
                bone_votes = {}
                for vi, (v, w_abs) in enumerate(zip(verts, raw_w)):
                    bi = 0
                    if w_abs > 0 and w_abs % boneSize == 0:
                        bi = w_abs // boneSize - 1
                        if bi < 0 or bi >= num_objects:
                            bi = 0
                    bone_votes[bi] = bone_votes.get(bi, 0) + 1
                    if bi not in wm_cache:
                        wm_cache[bi] = world_mats[bi]
                    wm = wm_cache[bi]
                    new_verts.append(_xform_pt(v[0], v[1], v[2], wm))
                    if has_n:
                        n = nrms[vi]
                        new_nrms.append(_xform_nrm(n[0], n[1], n[2], wm))
                verts = new_verts
                if has_n: nrms = new_nrms
                if bone_votes:
                    bone_idx = max(bone_votes, key=bone_votes.get)
            elif not fixed and num_objects > 0:
                wm = world_mats[0]
                verts = [_xform_pt(v[0], v[1], v[2], wm) for v in verts]
                if len(nrms) == len(verts):
                    nrms = [_xform_nrm(n[0], n[1], n[2], wm) for n in nrms]

            tris = _build_triangles(verts, adc, nrms if len(nrms) == len(verts) else None)
            if not tris: continue

            mat_name  = mat_names[mat_idx] if mat_idx < len(mat_names) else ''
            bone_name = objects[bone_idx]['name'] if bone_idx < num_objects else f'bone{bone_idx}'
            mesh_name = f"{bone_name}_lod{lod_idx}_surf{surf_idx}"

            meshes.append(Mesh(
                name=mesh_name, material=mat_name,
                positions=verts, normals=nrms, uvs=uvs, triangles=tris,
            ))

    return meshes


# ─────────────────────────────────────────────────────────────────────────────
#  OBJ / MTL writer
# ─────────────────────────────────────────────────────────────────────────────

def write_obj(meshes, out_path, write_mtl=True):
    base    = os.path.splitext(os.path.basename(out_path))[0]
    out_dir = os.path.dirname(out_path) or '.'
    mtl_fn  = base + '.mtl'
    obj_lines, mtl_lines, seen_mats = [], [], set()

    if write_mtl and any(m.material for m in meshes):
        obj_lines.append(f"mtllib {mtl_fn}")

    v_off = vn_off = vt_off = 0
    for mesh in meshes:
        obj_lines.append(f"\no {mesh.name}")
        safe_mat = mesh.material.replace('\\','/').replace(' ','_') if mesh.material else ''
        if safe_mat and write_mtl:
            obj_lines.append(f"usemtl {safe_mat}")
            if safe_mat not in seen_mats:
                seen_mats.add(safe_mat)
                tb = os.path.splitext(os.path.basename(safe_mat))[0]
                mtl_lines += [f"\nnewmtl {safe_mat}","Ka 1 1 1","Kd 1 1 1","Ks 0 0 0",f"map_Kd {tb}.tga"]

        for x,y,z in mesh.positions:   obj_lines.append(f"v  {x:.6f} {y:.6f} {z:.6f}")
        for nx,ny,nz in mesh.normals:  obj_lines.append(f"vn {nx:.6f} {ny:.6f} {nz:.6f}")
        for u,v in mesh.uvs:           obj_lines.append(f"vt {u:.6f} {1.0-v:.6f}")

        has_n  = len(mesh.normals) == len(mesh.positions)
        has_uv = len(mesh.uvs)    == len(mesh.positions)
        for tri in mesh.triangles:
            parts = []
            for vi in tri:
                a = vi+v_off+1
                b = vi+vt_off+1 if has_uv else ''
                c = vi+vn_off+1 if has_n  else ''
                if has_uv and has_n: parts.append(f"{a}/{b}/{c}")
                elif has_uv:         parts.append(f"{a}/{b}")
                elif has_n:          parts.append(f"{a}//{c}")
                else:                parts.append(str(a))
            obj_lines.append("f " + " ".join(parts))

        v_off  += len(mesh.positions)
        vn_off += len(mesh.normals)
        vt_off += len(mesh.uvs)

    with open(out_path,'w') as f: f.write('\n'.join(obj_lines)+'\n')
    if write_mtl and mtl_lines:
        with open(os.path.join(out_dir, mtl_fn),'w') as f: f.write('\n'.join(mtl_lines)+'\n')


# ─────────────────────────────────────────────────────────────────────────────
#  PST texture → PNG
# ─────────────────────────────────────────────────────────────────────────────
#
#  Two old-format variants exist in Racer Revenge RES files:
#
#  Format A  — RGBA32 or palette textures (pod, prop, track assets)
#    [u32 version=1][u32 flags][u32 numMips]
#    RGBA32  : pixel data directly follows header (largest mip first)
#    Palette : pixel indices before CLUT; CLUT at end of file
#              4-bit (PSXPal4): last 64B = CLUT (16 entries), nibble-packed pixels
#              8-bit (PSXPal8): last 1024B = CLUT (256 entries), 1B-per-pixel indices
#    Width/height inferred from file size.
#
#  Format B  — IPU-compressed textures (DiffuseCompressed characters/vehicles)
#    [u32 version=1][u32 flags][u32 numMips]
#    Per mip (smallest first): [20B outer header] ["ipum" block with compressed data]
#    → Pure Python MPEG-2 intra decoder matching PS2 IPU hardware (IDP=0, QST=0,
#      IVF=0, AS=0, DC prediction reset to 0). No mb_addr_inc — sequential MBs
#      with mb_type VLC only. PyAV/ffmpeg no longer required.
#
#  The streaming-format (PSXTextureFileHeader with u16/u16/u32 + per-mip SizeBytes)
#  is only produced by the StreamingTexture tool for .pts files; not seen in .pst.

def _decode_ipu_pst(raw, num_mips, out_path):
    """
    Decode an IPU-compressed PST (format=2) via pure Python MPEG-2 intra decoder.

    The PS2 IPU processes raw sequential DCT blocks without standard MPEG-2
    mb_addr_inc VLCs. Each macroblock contains:
      - mb_type VLC (1="intra", 01="intra+quant")
      - optional 5-bit quantiser_scale_code (if mb_type = intra+quant)
      - 6 DCT blocks: Y0, Y1, Y2, Y3, Cb, Cr (4:2:0)

    PS2 IPU hardware settings after RST (all confirmed from ELF):
      IDP=0 (8-bit DC precision), QST=1 (non-linear quantiser scale),
      IVF=0 (Table B.14), AS=0 (zigzag scan)

    DC prediction resets to 128 per MPEG-2 §7.2.1 (IDP=0 → 1<<7 = 128).

    Returns (width, height) on success, None on failure.
    """
    try:
        from PIL import Image
    except ImportError:
        print("  ⚠ Pillow not installed", file=sys.stderr)
        return None

    # ── Parse ipum blocks ─────────────────────────────────────────────────
    pos = 12
    mips = []
    for _ in range(max(1, num_mips)):
        if pos + 16 > len(raw):
            break
        sh_h, sh_w, sh_sz, _ = struct.unpack_from('<4I', raw, pos)
        pos += 16

        if pos + 8 > len(raw):
            break
        block_size = struct.unpack_from('<I', raw, pos)[0]
        magic = raw[pos+4:pos+8]
        if magic != b'ipum':
            break

        if pos + 21 > len(raw):
            break
        width  = struct.unpack_from('<H', raw, pos+12)[0]
        height = struct.unpack_from('<H', raw, pos+14)[0]
        # Bitstream starts at ipum+21 (the 0x00 byte at +20 is the high
        # byte of the flag field; the 0x47 byte at +21 IS the slice body
        # containing QSC=8 in its top 5 bits)
        bitstream = raw[pos+21 : pos+4+block_size]
        mips.append((width, height, bitstream))
        pos += 4 + block_size

    if not mips:
        print("  ⚠ no valid ipum blocks found", file=sys.stderr)
        return None

    # Take largest mip (last, stored smallest-first)
    width, height, bitstream = mips[-1]
    if not bitstream:
        print("  ⚠ empty bitstream", file=sys.stderr)
        return None

    # ── Decode with pure Python IPU decoder ───────────────────────────────
    try:
        pixels = _ipu_decode_frame(bitstream, width, height)
        if pixels is None:
            print("  ⚠ IPU decode failed", file=sys.stderr)
            return None

        img = Image.frombytes('RGB', (width, height), bytes(pixels))
        img.save(out_path)
        return (width, height)

    except Exception as e:
        print(f"  ⚠ IPU decode error: {e}", file=sys.stderr)
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Pure Python MPEG-2 Intra Decoder for PS2 IPU bitstreams
# ═════════════════════════════════════════════════════════════════════════════

# ── Zigzag scan order (AS=0, standard MPEG-2) ────────────────────────────
_ZIGZAG = [
     0,  1,  8, 16,  9,  2,  3, 10,
    17, 24, 32, 25, 18, 11,  4,  5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13,  6,  7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
]

# ── Default intra quantization matrix (row-major 8×8) ────────────────────
_DEFAULT_INTRA_QM = [
     8, 16, 19, 22, 26, 27, 29, 34,
    16, 16, 22, 24, 27, 29, 34, 37,
    19, 22, 26, 27, 29, 34, 34, 38,
    22, 22, 26, 27, 29, 34, 37, 40,
    22, 26, 27, 29, 32, 35, 40, 48,
    26, 27, 29, 32, 35, 40, 48, 58,
    26, 27, 29, 34, 38, 46, 56, 69,
    27, 29, 35, 38, 46, 56, 69, 83,
]

# Pre-compute quantization matrix in zigzag order
_INTRA_QM_ZZ = [_DEFAULT_INTRA_QM[_ZIGZAG[i]] for i in range(64)]

# ── Non-linear quantiser scale table (QST=1, MPEG-2 Table 7-6) ──────────
_NL_QSCALE = [
     0,  1,  2,  3,  4,  5,  6,  7,
     8, 10, 12, 14, 16, 18, 20, 22,
    24, 28, 32, 36, 40, 44, 48, 52,
    56, 64, 72, 80, 88, 96,104,112,
]


class _IPUBitReader:
    """MSB-first bitstream reader for MPEG-2 / PS2 IPU data."""
    __slots__ = ('_data', '_pos', '_len')

    def __init__(self, data):
        self._data = data
        self._pos = 0
        self._len = len(data) * 8

    def read(self, n):
        """Read n bits MSB-first, return as unsigned integer."""
        val = 0
        for _ in range(n):
            if self._pos >= self._len:
                return val  # pad with zeros at end
            byte_idx = self._pos >> 3
            bit_idx = 7 - (self._pos & 7)
            val = (val << 1) | ((self._data[byte_idx] >> bit_idx) & 1)
            self._pos += 1
        return val

    def peek(self, n):
        """Peek at next n bits without consuming."""
        save = self._pos
        val = self.read(n)
        self._pos = save
        return val

    def skip(self, n):
        self._pos += n

    @property
    def bits_left(self):
        return self._len - self._pos


# ── DC VLC Tables (B.12 luma, B.13 chroma) ──────────────────────────────

def _read_dc_size_luma(br):
    """Read dct_dc_size_luminance from Table B.12."""
    # 1→00, 2→01, 0→100, 3→101, 4→110, 5→1110, 6→11110, ...
    b2 = br.peek(2)
    if b2 == 0:    # 00
        br.skip(2); return 1
    if b2 == 1:    # 01
        br.skip(2); return 2
    b3 = br.peek(3)
    if b3 == 4:    # 100
        br.skip(3); return 0
    if b3 == 5:    # 101
        br.skip(3); return 3
    if b3 == 6:    # 110
        br.skip(3); return 4
    # 111... : count 1s after the initial "111"
    br.skip(3)
    for size in range(5, 12):
        if br.read(1) == 0:
            return size
    return 11


def _read_dc_size_chroma(br):
    """Read dct_dc_size_chrominance from Table B.13."""
    # 0→00, 1→01, 2→10, 3→110, 4→1110, 5→11110, ...
    b2 = br.peek(2)
    if b2 == 0:    # 00
        br.skip(2); return 0
    if b2 == 1:    # 01
        br.skip(2); return 1
    if b2 == 2:    # 10
        br.skip(2); return 2
    # 11... : count 1s after "11"
    br.skip(2)
    for size in range(3, 12):
        if br.read(1) == 0:
            return size
    return 11


def _read_dc_diff(br, size):
    """Read DC differential value given size (number of additional bits)."""
    if size == 0:
        return 0
    val = br.read(size)
    # If MSB is 0, value is negative: diff = val - (1 << size) + 1
    if val < (1 << (size - 1)):
        val = val - (1 << size) + 1
    return val


# ── AC VLC Table B.14 (IVF=0) ───────────────────────────────────────────
# Built as a binary trie for fast lookup.  Each leaf is (run, level) or
# special marker _EOB / _ESC.

_EOB = (-1, -1)
_ESC = (-2, -2)

def _build_ac_trie():
    """Build binary trie for MPEG-2 AC coefficient Table B.14.

    For intra blocks (our case), all positions use the table where
    '10' = EOB and '11' = (run=0, level=1) base code.
    Sign bit is read separately after each entry.
    """
    # (vlc_string_without_sign, run, level)
    # Special entries (no sign bit):
    entries = [
        ('10',                   -1, -1),  # EOB
        ('000001',               -2, -2),  # ESCAPE
    ]
    # Regular entries (sign bit follows):
    regular = [
        # 2-bit base (3 with sign)
        ('11',                    0,  1),
        # 3-bit base
        ('011',                   1,  1),
        # 4-bit base
        ('0100',                  0,  2),
        ('0101',                  2,  1),
        # 5-bit base
        ('00101',                 0,  3),
        ('00110',                 4,  1),
        ('00111',                 3,  1),
        # 6-bit base
        ('000100',                7,  1),
        ('000101',                6,  1),
        ('000110',                1,  2),
        ('000111',                5,  1),
        # 7-bit base
        ('0000100',               2,  2),
        ('0000101',               9,  1),
        ('0000110',               0,  4),
        ('0000111',               8,  1),
        # 8-bit base
        ('00100000',             13,  1),
        ('00100001',              0,  6),
        ('00100010',             12,  1),
        ('00100011',             11,  1),
        ('00100100',              3,  2),
        ('00100101',              1,  3),
        ('00100110',              0,  5),
        ('00100111',             10,  1),
        # 10-bit base
        ('0000001000',           16,  1),
        ('0000001001',            5,  2),
        ('0000001010',            0,  7),
        ('0000001011',            2,  3),
        ('0000001100',            1,  4),
        ('0000001101',           15,  1),
        ('0000001110',           14,  1),
        ('0000001111',            4,  2),
        # 12-bit base
        ('000000010000',          0, 11),
        ('000000010001',          8,  2),
        ('000000010010',          4,  3),
        ('000000010011',          0, 10),
        ('000000010100',          2,  4),
        ('000000010101',          7,  2),
        ('000000010110',         21,  1),
        ('000000010111',         20,  1),
        ('000000011000',          0,  9),
        ('000000011001',         19,  1),
        ('000000011010',         18,  1),
        ('000000011011',          1,  5),
        ('000000011100',          3,  3),
        ('000000011101',          0,  8),
        ('000000011110',          6,  2),
        ('000000011111',         17,  1),
        # 13-bit base
        ('0000000010000',        10,  2),
        ('0000000010001',         9,  2),
        ('0000000010010',         5,  3),
        ('0000000010011',         3,  4),
        ('0000000010100',         2,  5),
        ('0000000010101',         1,  7),
        ('0000000010110',         1,  6),
        ('0000000010111',         0, 15),
        ('0000000011000',         0, 14),
        ('0000000011001',         0, 13),
        ('0000000011010',         0, 12),
        ('0000000011011',        26,  1),
        ('0000000011100',        25,  1),
        ('0000000011101',        24,  1),
        ('0000000011110',        23,  1),
        ('0000000011111',        22,  1),
        # 14-bit base
        ('00000000010000',        0, 31),
        ('00000000010001',        0, 30),
        ('00000000010010',        0, 29),
        ('00000000010011',        0, 28),
        ('00000000010100',        0, 27),
        ('00000000010101',        0, 26),
        ('00000000010110',        0, 25),
        ('00000000010111',        0, 24),
        ('00000000011000',        0, 23),
        ('00000000011001',        0, 22),
        ('00000000011010',        0, 21),
        ('00000000011011',        0, 20),
        ('00000000011100',        0, 19),
        ('00000000011101',        0, 18),
        ('00000000011110',        0, 17),
        ('00000000011111',        0, 16),
        # 15-bit base
        ('000000000010000',       0, 40),
        ('000000000010001',       0, 39),
        ('000000000010010',       0, 38),
        ('000000000010011',       0, 37),
        ('000000000010100',       0, 36),
        ('000000000010101',       0, 35),
        ('000000000010110',       0, 34),
        ('000000000010111',       0, 33),
        ('000000000011000',       0, 32),
        ('000000000011001',       1, 14),
        ('000000000011010',       1, 13),
        ('000000000011011',       1, 12),
        ('000000000011100',       1, 11),
        ('000000000011101',       1, 10),
        ('000000000011110',       1,  9),
        ('000000000011111',       1,  8),
        # 16-bit base
        ('0000000000010000',      1, 18),
        ('0000000000010001',      1, 17),
        ('0000000000010010',      1, 16),
        ('0000000000010011',      1, 15),
        ('0000000000010100',      6,  3),
        ('0000000000010101',     11,  2),
        ('0000000000010110',     12,  2),
        ('0000000000010111',     13,  2),
        ('0000000000011000',     14,  2),
        ('0000000000011001',     15,  2),
        ('0000000000011010',     16,  2),
        ('0000000000011011',     27,  1),
        ('0000000000011100',     28,  1),
        ('0000000000011101',     29,  1),
        ('0000000000011110',     30,  1),
        ('0000000000011111',     31,  1),
    ]

    # Build trie: dict of dicts, leaves are tuples
    root = {}
    for vlc, r, l in entries:
        node = root
        for bit_ch in vlc[:-1]:
            b = int(bit_ch)
            if b not in node:
                node[b] = {}
            node = node[b]
        node[int(vlc[-1])] = (r, l, False)  # no sign bit

    for vlc, r, l in regular:
        node = root
        for bit_ch in vlc[:-1]:
            b = int(bit_ch)
            if b not in node:
                node[b] = {}
            node = node[b]
        node[int(vlc[-1])] = (r, l, True)  # sign bit follows

    return root


_AC_TRIE = _build_ac_trie()


def _read_ac_coeff(br):
    """Read one AC coefficient from bitstream using Table B.14 trie.

    Returns (run, level) where:
      run=-1, level=-1 → End of Block
      run=-2, level=-2 → should not happen (escape handled internally)
      run>=0, level!=0 → normal coefficient
    """
    node = _AC_TRIE
    while isinstance(node, dict):
        if br.bits_left < 1:
            return _EOB  # treat exhausted stream as EOB
        b = br.read(1)
        if b not in node:
            return _EOB  # invalid code → treat as EOB (error recovery)
        node = node[b]

    run, level, has_sign = node

    # EOB
    if run == -1:
        return _EOB

    # ESCAPE: 6-bit run + 12-bit signed level
    if run == -2:
        run = br.read(6)
        level_raw = br.read(12)
        # 12-bit two's complement
        if level_raw >= 2048:
            level_raw -= 4096
        return (run, level_raw)

    # Regular: read sign bit
    if has_sign:
        sign = br.read(1)
        if sign:
            level = -level

    return (run, level)


# ── MB address increment VLC (MPEG-2 Table B.1) ──────────────────────────

def _build_addr_inc_trie():
    entries = [
        ('1',             1),
        ('011',           2),
        ('010',           3),
        ('0011',          4),
        ('0010',          5),
        ('00011',         6),
        ('00010',         7),
        ('0000111',       8),
        ('0000110',       9),
        ('00001011',     10),
        ('00001010',     11),
        ('00001001',     12),
        ('00001000',     13),
        ('00000111',     14),
        ('00000110',     15),
        ('0000010111',   16),
        ('0000010110',   17),
        ('0000010101',   18),
        ('0000010100',   19),
        ('0000010011',   20),
        ('0000010010',   21),
        ('00000100011',  22),
        ('00000100010',  23),
        ('00000100001',  24),
        ('00000100000',  25),
        ('00000011111',  26),
        ('00000011110',  27),
        ('00000011101',  28),
        ('00000011100',  29),
        ('00000011011',  30),
        ('00000011010',  31),
        ('00000011001',  32),
        ('00000011000',  33),
        ('00000001000',   0),  # macroblock_escape: accumulate 33, read again
    ]
    root = {}
    for vlc, val in entries:
        node = root
        for bit_ch in vlc[:-1]:
            b = int(bit_ch)
            if b not in node:
                node[b] = {}
            node = node[b]
        node[int(vlc[-1])] = val
    return root

_ADDR_INC_TRIE = _build_addr_inc_trie()


def _read_addr_inc(br):
    """Read macroblock_address_increment (Table B.1).
    addr_inc=1: decode current MB (no skip).
    addr_inc=N: skip N-1 MBs, decode the Nth.
    Escape code (0x00000001000) adds 33 and loops.
    """
    acc = 0
    while True:
        node = _ADDR_INC_TRIE
        while isinstance(node, dict):
            if br.bits_left < 1:
                return max(1, acc + 1)
            b = br.read(1)
            if b not in node:
                return max(1, acc + 1)
            node = node[b]
        val = node
        if val == 0:   # escape: add 33, read next increment
            acc += 33
        else:
            return acc + val


# ── IDCT (direct matrix multiply, reference quality) ─────────────────────

def _build_idct_matrix():
    """Pre-compute 8×8 IDCT basis matrix C[k][n] = cos(pi*(2n+1)*k/16).
    Scaled by: 1/sqrt(8) for k=0, sqrt(2/8) for k>0 (i.e. 0.5 for k>0)."""
    import math
    C = [[0.0]*8 for _ in range(8)]
    for k in range(8):
        for n in range(8):
            if k == 0:
                C[k][n] = 1.0 / math.sqrt(8.0)
            else:
                C[k][n] = math.sqrt(2.0 / 8.0) * math.cos(math.pi * (2*n + 1) * k / 16.0)
    return C


_IDCT_C = _build_idct_matrix()


def _idct_8x8(block):
    """Perform 2D IDCT on an 8×8 block (list of 64 values in row-major order).
    Returns list of 64 values."""
    C = _IDCT_C
    # Row transform
    temp = [0.0] * 64
    for i in range(8):
        for j in range(8):
            s = 0.0
            for k in range(8):
                s += C[k][j] * block[i*8 + k]
            temp[i*8 + j] = s
    # Column transform
    result = [0.0] * 64
    for j in range(8):
        for i in range(8):
            s = 0.0
            for k in range(8):
                s += C[k][i] * temp[k*8 + j]
            result[i*8 + j] = s
    return result


# ── Block decoder ────────────────────────────────────────────────────────

def _decode_block(br, dc_pred, is_luma, qscale):
    """Decode one 8×8 DCT block from the IPU bitstream.

    Args:
        br: _IPUBitReader
        dc_pred: current DC prediction value for this component
        is_luma: True for Y blocks, False for Cb/Cr
        qscale: current quantiser scale (linear, QST=0: qscale = QSC * 2)

    Returns:
        (pixels_8x8, new_dc_pred) where pixels_8x8 is a list of 64 ints (clamped 0-255)
    """
    # ── DC coefficient ────────────────────────────────────────────────
    if is_luma:
        dc_size = _read_dc_size_luma(br)
    else:
        dc_size = _read_dc_size_chroma(br)

    dc_diff = _read_dc_diff(br, dc_size)
    dc_pred = dc_pred + dc_diff

    # IDP=0: intra_dc_mult = 8
    qf = [0] * 64
    qf[0] = dc_pred * 8  # DC dequantized value

    # ── AC coefficients (zigzag positions 1..63) ──────────────────────
    idx = 1  # next zigzag position
    while idx < 64:
        run, level = _read_ac_coeff(br)
        if run == -1:  # EOB
            break
        idx += run
        if idx >= 64:
            break
        # Inverse quantization for AC: F = ((2*level+sign) * qscale * W) / 32
        # where W is the quantization matrix value at this zigzag position
        # For intra blocks with QST=0 (linear): qscale = quantiser_scale_code * 2
        w = _INTRA_QM_ZZ[idx]
        if level > 0:
            f = (2 * level + 1) * qscale * w // 32
        elif level < 0:
            f = (2 * level - 1) * qscale * w // 32
        else:
            f = 0
        # Mismatch control: ensure sum of all coefficients is odd
        qf[_ZIGZAG[idx]] = f
        idx += 1

    # Mismatch control (MPEG-2): toggle LSB of last coefficient
    s = sum(qf) & 1
    if s == 0:
        if qf[63] & 1:
            qf[63] -= 1
        else:
            qf[63] += 1

    # ── IDCT ──────────────────────────────────────────────────────────
    pixels = _idct_8x8(qf)

    # Clamp to 0-255 (output is unsigned 8-bit for IPU)
    clamped = [max(0, min(255, int(round(p)))) for p in pixels]

    return clamped, dc_pred


# ── Frame decoder ────────────────────────────────────────────────────────

def _ipu_decode_frame(bitstream, width, height):
    """Decode a full IPU frame from raw PS2 IPU bitstream.

    Verified parameters (all from ELF SLUS_202.68 + ffmpeg reference):
      QST=1 (non-linear quantiser scale), IDP=0 (8-bit DC precision),
      IVF=0 (Table B.14 for AC), AS=0 (zigzag scan).
      DC initial value = 128 (MPEG-2 §7.2.1: 1 << (7+IDP) = 128).
      DC not reset for skipped MBs (confirmed from alternate MB handler).
      Y+72 correction applied in CSC (PS2 DC offset vs ffmpeg reference).

    Returns RGB pixel data as bytearray (width*height*3), or None on failure.
    """
    br = _IPUBitReader(bitstream)

    # ── Slice header: QSC (5 bits) + extra_bit_slice loop ─────────────
    qsc = br.read(5)
    if qsc == 0:
        qsc = 1
    # QST=1 (non-linear): look up actual scale, ×2 for the /32 AC formula
    qscale = _NL_QSCALE[qsc]

    while br.bits_left > 9 and br.read(1) == 1:
        br.skip(8)

    # ── Allocate output planes (pre-filled with 128 for skipped MBs) ──
    mb_w = (width  + 15) // 16
    mb_h = (height + 15) // 16
    total_mbs = mb_w * mb_h

    y_plane  = bytearray(b'\x80' * (mb_h * 16 * mb_w * 16))
    cb_plane = bytearray(b'\x80' * (mb_h * 8  * mb_w * 8))
    cr_plane = bytearray(b'\x80' * (mb_h * 8  * mb_w * 8))

    y_stride = mb_w * 16
    c_stride = mb_w * 8

    # DC predictors: 128 per MPEG-2 §7.2.1 (IDP=0)
    dc_y  = 128
    dc_cb = 128
    dc_cr = 128

    # ── Decode macroblocks ────────────────────────────────────────────
    mb_addr = 0  # 0-based macroblock address

    while mb_addr < total_mbs:
        if br.bits_left < 2:
            break

        # addr_inc: 1 = current MB, N = skip N-1 then decode Nth
        addr_inc = _read_addr_inc(br)

        # Skip addr_inc-1 positions (ELF checks bounds per individual MB)
        for _ in range(addr_inc - 1):
            if mb_addr >= total_mbs:
                break
            # Skipped MB: plane already 128, DC predictors carry forward unchanged
            mb_addr += 1

        if mb_addr >= total_mbs:
            break

        # Decode coded MB at mb_addr
        mb_row = mb_addr // mb_w
        mb_col = mb_addr % mb_w
        mb_addr += 1

        # mb_type VLC (PCSX2-correct): peek 2 bits like UBITS(2), then DUMPBITS(len).
        # '1x' -> INTRA (consume 1 bit). '01' -> INTRA+QUANT (consume 2 + 5-bit QSC).
        # '00' -> invalid per MPEG-2; consume 0 bits, leave them for the DC size reader.
        mb_peek = br.peek(2)
        if mb_peek >= 2:        # '1x': INTRA, no quant update
            br.read(1)
        elif mb_peek == 1:      # '01': INTRA+QUANT
            br.read(2)
            new_qsc = br.read(5)
            if new_qsc > 0:
                qscale = _NL_QSCALE[new_qsc]
        # else mb_peek == 0: '00' — consume 0 bits; block decoder reads them as DC data

        # 6 DCT blocks: Y0 Y1 Y2 Y3 Cb Cr
        y0, dc_y  = _decode_block(br, dc_y,  True,  qscale)
        y1, dc_y  = _decode_block(br, dc_y,  True,  qscale)
        y2, dc_y  = _decode_block(br, dc_y,  True,  qscale)
        y3, dc_y  = _decode_block(br, dc_y,  True,  qscale)
        cb_blk, dc_cb = _decode_block(br, dc_cb, False, qscale)
        cr_blk, dc_cr = _decode_block(br, dc_cr, False, qscale)

        # Write to planes
        yb = mb_row * 16 * y_stride + mb_col * 16
        for row in range(8):
            off = yb + row * y_stride
            for col in range(8):
                y_plane[off + col]     = y0[row*8 + col]
                y_plane[off + col + 8] = y1[row*8 + col]
            off2 = yb + (row + 8) * y_stride
            for col in range(8):
                y_plane[off2 + col]     = y2[row*8 + col]
                y_plane[off2 + col + 8] = y3[row*8 + col]

        cb_base = mb_row * 8 * c_stride + mb_col * 8
        for row in range(8):
            for col in range(8):
                cb_plane[cb_base + row * c_stride + col] = cb_blk[row*8 + col]
                cr_plane[cb_base + row * c_stride + col] = cr_blk[row*8 + col]

    # ── YCbCr → RGB (BT.601, Y+72 PS2 DC offset correction) ─────────
    pixels = bytearray(width * height * 3)

    try:
        import numpy as np
        Y  = np.frombuffer(y_plane,  dtype=np.uint8).reshape(mb_h*16, mb_w*16)[:height, :width].astype(np.float32)
        Cb_s = np.frombuffer(cb_plane, dtype=np.uint8).reshape(mb_h*8, mb_w*8)[:((height+1)//2), :((width+1)//2)].astype(np.float32)
        Cr_s = np.frombuffer(cr_plane, dtype=np.uint8).reshape(mb_h*8, mb_w*8)[:((height+1)//2), :((width+1)//2)].astype(np.float32)
        Cb = np.repeat(np.repeat(Cb_s, 2, axis=0), 2, axis=1)[:height, :width]
        Cr = np.repeat(np.repeat(Cr_s, 2, axis=0), 2, axis=1)[:height, :width]
        Y  = Y - 72.0
        R = np.clip(Y + 1.402   * (Cr - 128), 0, 255).astype(np.uint8)
        G = np.clip(Y - 0.34414 * (Cb - 128) - 0.71414 * (Cr - 128), 0, 255).astype(np.uint8)
        B = np.clip(Y + 1.772   * (Cb - 128), 0, 255).astype(np.uint8)
        pixels = bytearray(np.stack([R, G, B], axis=2).tobytes())
    except ImportError:
        _clamp = [max(0, min(255, i)) for i in range(-512, 768)]
        def clamp(v):
            iv = int(v + 0.5)
            return _clamp[iv + 512] if -512 <= iv < 768 else max(0, min(255, iv))
        for py in range(height):
            cy = py >> 1
            row_y = py * y_stride
            row_c = cy * c_stride
            out_row = py * width * 3
            for px in range(width):
                y_val  = y_plane[row_y + px] - 72
                cb_val = cb_plane[row_c + (px >> 1)]
                cr_val = cr_plane[row_c + (px >> 1)]
                cb_off = cb_val - 128
                cr_off = cr_val - 128
                off = out_row + px * 3
                pixels[off]     = clamp(y_val + 1.402   * cr_off)
                pixels[off + 1] = clamp(y_val - 0.34414 * cb_off - 0.71414 * cr_off)
                pixels[off + 2] = clamp(y_val + 1.772   * cb_off)

    return pixels


def _csm1_deswizzle_clut(clut_raw):
    """PS2 CLUT CSM1 deswizzle: swap entries 8-15 ↔ 16-23 within each 32-entry block.
    Also scales PS2 alpha (0-128 range) to standard 0-255."""
    clut_ds = bytearray(1024)
    for i in range(256):
        blk = (i // 32) * 32
        ent = i % 32
        if   8 <= ent < 16: src = blk + ent + 8
        elif 16 <= ent < 24: src = blk + ent - 8
        else:                src = i
        clut_ds[i*4:i*4+4] = clut_raw[src*4:src*4+4]
    # Alpha scale: PS2 uses 0-128 → output 0-255
    for i in range(256):
        a = clut_ds[i*4+3]
        clut_ds[i*4+3] = min(a * 2, 255)
    return clut_ds


def convert_pst_to_png(pst_path, out_path, forced_w=None, forced_h=None):
    """
    Convert PST texture to PNG.  Returns (w, h, fmt) or None.
    fmt==2 means IPU-compressed; colors may be approximate (CSC not fully verified).

    PST header (12 bytes):
      u16 version (always 1)
      u16 flags   (NOPALETTE=0x01, NOSCALERGB=0x02, NOSCALEALPHA=0x04, STREAM=0x08)
      u32 format  (0=uncompressed single mip, 1=uncompressed multi-mip, 2=IPU compressed)
      u32 num_mips

    Uncompressed (format 0/1):
      [1024 bytes] 256-entry RGBA CLUT at offset 12
      Per mip (smallest → largest):
        [16 bytes] PSXTextureSurfaceHeader { H(u32), W(u32), SizeBytes(u32), Unused(u32) }
        [W×H bytes] 8-bit indexed pixel data (linear scan order, no deswizzle)

    IPU compressed (format 2): MPEG2 bitstream decoded via PyAV.
    """
    try:
        from PIL import Image
    except ImportError:
        print("  Pillow not installed — pip install Pillow", file=sys.stderr)
        return None

    with open(pst_path, 'rb') as f:
        raw = f.read()
    if len(raw) < 12:
        return None

    # ── Parse header: u16 version, u16 flags, u32 format, u32 num_mips ────
    version = struct.unpack_from('<H', raw, 0)[0]
    flags   = struct.unpack_from('<H', raw, 2)[0]
    fmt     = struct.unpack_from('<I', raw, 4)[0]
    num_mips= struct.unpack_from('<I', raw, 8)[0]

    if version != 1:
        print(f"  ⚠ unsupported PST version {version}", file=sys.stderr)
        return None

    # ── Format 2: IPU-compressed ──────────────────────────────────────────────
    if fmt == 2:
        res = _decode_ipu_pst(raw, num_mips, out_path)
        if res is None:
            return None
        return (res[0], res[1], 2)

    # ── Format 0/1: uncompressed ──────────────────────────────────────────
    has_palette = not bool(flags & 0x01)

    try:
        if has_palette:
            # ── 8-bit palettized: CLUT at offset 12, then per-mip data ────
            if len(raw) < 12 + 1024:
                print(f"  ⚠ file too small for CLUT ({len(raw)} bytes)",
                      file=sys.stderr)
                return None

            clut_raw = raw[12:12+1024]
            clut_ds = _csm1_deswizzle_clut(clut_raw)

            # Build RGBA palette for PIL
            palette_rgba = []
            for i in range(256):
                r, g, b, a = clut_ds[i*4], clut_ds[i*4+1], clut_ds[i*4+2], clut_ds[i*4+3]
                palette_rgba += [r, g, b, a]

            # Parse per-mip surface headers (smallest → largest)
            pos = 12 + 1024
            mips = []
            for mip_idx in range(max(1, num_mips)):
                if pos + 16 > len(raw):
                    break
                sh_h, sh_w, sh_sz, sh_unused = struct.unpack_from('<4I', raw, pos)
                pos += 16

                # SizeBytes in header may be 0; compute from W×H
                data_sz = sh_sz if sh_sz > 0 else sh_w * sh_h
                if data_sz == 0 or pos + data_sz > len(raw):
                    break

                mips.append((sh_w, sh_h, raw[pos:pos+data_sz]))
                pos += data_sz

            if not mips:
                print(f"  ⚠ no valid mips found", file=sys.stderr)
                return None

            # Take largest mip (last, since stored smallest-first)
            w, h, indices = mips[-1]
            if len(indices) < w * h:
                print(f"  ⚠ pixel data too short ({len(indices)} < {w*h})",
                      file=sys.stderr)
                return None

            img_p = Image.frombytes('P', (w, h), bytes(indices[:w*h]))
            img_p.putpalette(palette_rgba, rawmode='RGBA')
            img = img_p.convert('RGBA')

        else:
            # ── RGBA32 (NOPALETTE): per-mip data directly after header ────
            pos = 12
            mips = []
            for mip_idx in range(max(1, num_mips)):
                if pos + 16 > len(raw):
                    break
                sh_h, sh_w, sh_sz, sh_unused = struct.unpack_from('<4I', raw, pos)
                pos += 16

                data_sz = sh_sz if sh_sz > 0 else sh_w * sh_h * 4
                if data_sz == 0:
                    # Fallback: infer from forced dimensions or file remainder
                    if forced_w and forced_h:
                        sh_w, sh_h = forced_w, forced_h
                        data_sz = sh_w * sh_h * 4
                    else:
                        break
                if pos + data_sz > len(raw):
                    break

                mips.append((sh_w, sh_h, raw[pos:pos+data_sz]))
                pos += data_sz

            if not mips:
                # Legacy fallback: no surface headers, raw pixel data after header
                pixel_data = raw[12:]
                if forced_w and forced_h:
                    w, h = forced_w, forced_h
                else:
                    factor = sum(0.25**k for k in range(max(1, num_mips)))
                    base   = (len(pixel_data) / 4.0) / factor
                    side   = math.sqrt(base)
                    w = h  = None
                    for exp in range(1, 12):
                        c = 1 << exp
                        if abs(side - c) < c * 0.06:
                            w = h = c; break
                    if w is None:
                        print(f"  ⚠ cannot infer dims "
                              f"(size={len(raw)}, numMips={num_mips}). "
                              "Use --width/--height.", file=sys.stderr)
                        return None
                if len(pixel_data) < w * h * 4: return None
                img = Image.frombytes('RGBA', (w, h), pixel_data[:w*h*4])
            else:
                w, h, pixel_data = mips[-1]
                if len(pixel_data) < w * h * 4: return None
                img = Image.frombytes('RGBA', (w, h), pixel_data[:w*h*4])

        img.save(out_path)
        return (w, h, fmt)

    except Exception as e:
        print(f"  ⚠ PIL error: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────



# ═════════════════════════════════════════════════════════════════════════════
#  RES / SCB / Track / FBX / Batch / GUI
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
#  RES Extractor (inline)
# ─────────────────────────────────────────────────────────────────────────────

VSECTOR = 0x6000

def extract_res(res_path, out_dir, log_fn=None):
    """Extract a .RES archive. Returns (extracted_count, total_count)."""
    def log(msg):
        if log_fn: log_fn(msg)

    with open(res_path, 'rb') as f:
        data = f.read()

    pos = 0
    version = struct.unpack_from('<I', data, pos)[0]; pos += 4
    hdr_bytes = struct.unpack_from('<I', data, pos)[0]; pos += 4
    flags = 0
    if version >= 3:
        flags, _ = struct.unpack_from('<2I', data, pos); pos += 8
        hdr_bytes -= 8

    sectors = []
    if flags & 0x100:
        sl_sz = struct.unpack_from('<I', data, pos)[0]; pos += 4
        hdr_bytes -= 4
        n_sec = sl_sz // 2
        sectors = [struct.unpack_from('<H', data, pos + i * 2)[0] for i in range(n_sec)]
        pos += sl_sz; hdr_bytes -= sl_sz
    if hdr_bytes > 0:
        pos += hdr_bytes

    num_items, off_table_sz = struct.unpack_from('<2I', data, pos); pos += 8
    primary = pos + off_table_sz

    entries = []
    epos = pos
    for _ in range(num_items):
        nlen = struct.unpack_from('<I', data, epos)[0]; epos += 4
        name = data[epos:epos + nlen].decode('ascii', 'replace').replace('\x00', '')
        epos += nlen
        soff = struct.unpack_from('<I', data, epos)[0]; epos += 4
        fsz = struct.unpack_from('<I', data, epos)[0]; epos += 4
        entries.append((name, soff, fsz))

    has_markers = bool(flags & 0x200)

    if sectors:
        log(f"Decompressing {len(sectors)} sectors...")
        decompressed = bytearray()
        rpos = primary
        for csz in sectors:
            chunk = data[rpos:rpos + csz]; rpos += csz
            if has_markers: rpos += 1
            try:
                dec = zlib.decompress(chunk)
            except:
                dec = chunk
            if len(dec) < VSECTOR:
                dec = dec + b'\x00' * (VSECTOR - len(dec))
            decompressed.extend(dec[:VSECTOR])
        source = bytes(decompressed)
    else:
        source = data[primary:]

    os.makedirs(out_dir, exist_ok=True)
    extracted = 0
    for name, soff, fsz in entries:
        if fsz == 0: continue
        if soff + fsz > len(source): continue
        fdata = source[soff:soff + fsz]
        clean = name.replace('\\', '/')
        out_path = os.path.join(out_dir, clean)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'wb') as f:
            f.write(fdata)
        extracted += 1

    log(f"Extracted {extracted}/{num_items} files")
    return extracted, num_items


# ─────────────────────────────────────────────────────────────────────────────
#  SCB Parser + Track Assembler (inline)
# ─────────────────────────────────────────────────────────────────────────────

def parse_scb_placements(scb_path):
    """Parse SCB and return list of placement dicts."""
    with open(scb_path, 'rb') as f:
        data = f.read()
    if data[:7] != b'BINARY\x00':
        return []

    pos = 7
    version = struct.unpack_from('<I', data, pos)[0]; pos += 4
    bc = struct.unpack_from('<I', data, pos)[0]; pos += 4

    strings = []
    for _ in range(bc):
        sl = struct.unpack_from('<I', data, pos)[0]; pos += 4
        s = data[pos:pos + sl].split(b'\x00')[0].decode('ascii', 'replace')
        strings.append(s); pos += sl

    offsets = []
    for _ in range(bc):
        o = struct.unpack_from('<I', data, pos)[0]; pos += 4
        s = struct.unpack_from('<I', data, pos)[0]; pos += 4
        offsets.append((o, s))

    placements = []
    for i, name in enumerate(strings):
        off, sz = offsets[i]
        if sz == 0: continue
        sec = data[off:off + sz]
        fc = struct.unpack_from('<I', sec, 0)[0]
        fp = 4; fields = []
        for _ in range(fc):
            if fp + 43 > sz: break
            fn = sec[fp:fp + 35].split(b'\x00')[0].decode('ascii', 'replace')
            vo = struct.unpack_from('<I', sec, fp + 35)[0]
            vs = struct.unpack_from('<I', sec, fp + 39)[0]
            fields.append((fn, vo, vs)); fp += 43
        vals = {}
        for fn, vo, vs in fields:
            ao = vo + 4
            if ao + vs <= sz:
                vals[fn] = sec[ao:ao + vs].split(b'\x00')[0].decode('ascii', 'replace')
        if 'slt' in vals and vals['slt']:
            position = (0, 0, 0)
            lookvector = (0, 0, 1)
            try:
                p = [float(x) for x in vals.get('position', '').split(',')]
                if len(p) >= 3: position = tuple(p[:3])
            except: pass
            try:
                l = [float(x) for x in vals.get('lookvector', '').split(',')]
                if len(l) >= 3: lookvector = tuple(l[:3])
            except: pass
            placements.append({
                'name': name, 'slt': vals['slt'],
                'position': position, 'lookvector': lookvector
            })
    return placements


def _look_to_mat(lx, ly, lz):
    ln = math.sqrt(lx * lx + ly * ly + lz * lz)
    if ln < 1e-9: return [1, 0, 0, 0, 1, 0, 0, 0, 1]
    f = (lx / ln, ly / ln, lz / ln)
    up = (0, 1, 0)
    rx = up[1] * f[2] - up[2] * f[1]
    ry = up[2] * f[0] - up[0] * f[2]
    rz = up[0] * f[1] - up[1] * f[0]
    rl = math.sqrt(rx * rx + ry * ry + rz * rz)
    if rl < 1e-9: return [1, 0, 0, 0, 1, 0, 0, 0, 1]
    r = (rx / rl, ry / rl, rz / rl)
    ux = f[1] * r[2] - f[2] * r[1]
    uy = f[2] * r[0] - f[0] * r[2]
    uz = f[0] * r[1] - f[1] * r[0]
    return [r[0], r[1], r[2], ux, uy, uz, f[0], f[1], f[2]]


def assemble_track(scb_path, geom_dir, out_path, log_fn=None):
    """Assemble a track OBJ from SCB placements + PSG geometry."""
    def log(msg):
        if log_fn: log_fn(msg)

    placements = parse_scb_placements(scb_path)
    log(f"SCB: {len(placements)} placements")

    # Convert + cache
    obj_cache = {}
    for p in placements:
        base = os.path.splitext(p['slt'])[0].lower()
        if base in obj_cache: continue
        psg = None
        for fn in os.listdir(geom_dir):
            if os.path.splitext(fn)[0].lower() == base and fn.lower().endswith('.psg'):
                psg = os.path.join(geom_dir, fn); break
        if not psg: continue
        try:
            meshes = parse_psg(psg)
            if meshes:
                obj_cache[base] = meshes
        except Exception as e:
            log(f"  ✗ {base}: {e}")

    log(f"Converted {len(obj_cache)} unique models")

    # Assemble
    lines = [f"# Racer Revenge assembled track"]
    vo = vno = vto = 0
    placed = 0

    for p in placements:
        base = os.path.splitext(p['slt'])[0].lower()
        if base not in obj_cache: continue
        meshes = obj_cache[base]
        px, py, pz = p['position']
        rot = _look_to_mat(*p['lookvector'])

        for mesh in meshes:
            lines.append(f"\no {p['name']}_{mesh.name}")
            if mesh.material:
                lines.append(f"usemtl {mesh.material.replace(' ', '_')}")
            for x, y, z in mesh.positions:
                lines.append(f"v  {x*rot[0]+y*rot[3]+z*rot[6]+px:.6f} "
                             f"{x*rot[1]+y*rot[4]+z*rot[7]+py:.6f} "
                             f"{x*rot[2]+y*rot[5]+z*rot[8]+pz:.6f}")
            for nx, ny, nz in mesh.normals:
                lines.append(f"vn {nx*rot[0]+ny*rot[3]+nz*rot[6]:.6f} "
                             f"{nx*rot[1]+ny*rot[4]+nz*rot[7]:.6f} "
                             f"{nx*rot[2]+ny*rot[5]+nz*rot[8]:.6f}")
            for u, v in mesh.uvs:
                lines.append(f"vt {u:.6f} {1.0 - v:.6f}")

            has_n = len(mesh.normals) == len(mesh.positions)
            has_uv = len(mesh.uvs) == len(mesh.positions)
            for tri in mesh.triangles:
                parts = []
                for vi in tri:
                    a = vi + vo + 1
                    b = vi + vto + 1 if has_uv else ''
                    c = vi + vno + 1 if has_n else ''
                    if has_uv and has_n: parts.append(f"{a}/{b}/{c}")
                    elif has_uv: parts.append(f"{a}/{b}")
                    elif has_n: parts.append(f"{a}//{c}")
                    else: parts.append(str(a))
                lines.append("f " + " ".join(parts))

            vo += len(mesh.positions)
            vno += len(mesh.normals)
            vto += len(mesh.uvs)
        placed += 1

    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    with open(out_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    log(f"Placed {placed}/{len(placements)} models, {vo} verts")
    log(f"→ {out_path}")
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Batch Texture/PSG conversion
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
#  FBX 7.4 Binary Writer (skeleton + rigid skinning)
#  Compatible with Blender, Noesis, Autodesk FBX SDK, Softimage XSI
# ─────────────────────────────────────────────────────────────────────────────

# ─── FBX Property encoders ───────────────────────────────────────────────────

def _S(s):
    b = s.encode('utf-8') if isinstance(s, str) else s
    return b'S' + struct.pack('<I', len(b)) + b

def _I(v):
    return b'I' + struct.pack('<i', int(v))

def _L(v):
    return b'L' + struct.pack('<q', int(v))

def _D(v):
    return b'D' + struct.pack('<d', float(v))

def _F(v):
    return b'F' + struct.pack('<f', float(v))

def _C(v):
    return b'C' + struct.pack('<B', 1 if v else 0)

def _R(data):
    return b'R' + struct.pack('<I', len(data)) + data

def _ai(arr):
    raw = struct.pack(f'<{len(arr)}i', *arr)
    return b'i' + struct.pack('<III', len(arr), 0, len(raw)) + raw

def _ad(arr):
    raw = struct.pack(f'<{len(arr)}d', *arr)
    return b'd' + struct.pack('<III', len(arr), 0, len(raw)) + raw

def _af(arr):
    raw = struct.pack(f'<{len(arr)}f', *arr)
    return b'f' + struct.pack('<III', len(arr), 0, len(raw)) + raw

def _al(arr):
    raw = struct.pack(f'<{len(arr)}q', *arr)
    return b'l' + struct.pack('<III', len(arr), 0, len(raw)) + raw

# ─── Node ────────────────────────────────────────────────────────────────────

NULL_RECORD = b'\x00' * 13  # FBX 7.4 null record

class _FbxNode:
    """FBX node builder. Usage: _FbxNode('Name', prop1, prop2, ...).add(child1, child2, ...)"""
    __slots__ = ('name', 'props', 'children', 'num_props')

    def __init__(self, name, *props):
        self.name = name.encode('utf-8') if isinstance(name, str) else name
        self.props = b''.join(props)
        self.children = []
        self.num_props = len(props)

    def add(self, *children):
        self.children.extend(children)
        return self


def _fbx_serialize(nodes):
    """Serialize a list of top-level FBX nodes into a complete binary FBX 7.4 file."""
    # Header
    magic = b'Kaydara FBX Binary  \x00\x1a\x00'
    version = struct.pack('<I', 7400)
    header = magic + version

    # We need absolute offsets, so we do a two-pass approach:
    # First encode all nodes to get sizes, then fix offsets.
    # Simpler approach: encode with offset tracking.

    body = bytearray()
    offset = len(header)

    for node in nodes:
        chunk = _fbx_encode_node(node, offset)
        body.extend(chunk)
        offset += len(chunk)

    # Top-level null record
    body.extend(NULL_RECORD)
    offset += len(NULL_RECORD)

    # Footer padding to align to 16 bytes
    pad_len = (16 - (offset % 16)) % 16
    body.extend(b'\x00' * pad_len)
    offset += pad_len

    # Footer (version-dependent)
    footer = bytearray()
    footer.extend(b'\x00' * 4)  # unknown
    footer.extend(struct.pack('<I', 7400))  # version repeat
    footer.extend(b'\x00' * 120)  # padding
    # FBX footer magic
    footer.extend(bytes([
        0xf8, 0x5a, 0x8c, 0x6a, 0xde, 0xf5, 0xd9, 0x7e,
        0xec, 0xe9, 0x0c, 0xe3, 0x75, 0x8f, 0x29, 0x0b
    ]))

    return bytes(header) + bytes(body) + bytes(footer)


def _fbx_encode_node(node, base_offset):
    """Encode a single node with correct absolute offsets."""
    name_data = struct.pack('<B', len(node.name)) + node.name
    header_size = 4 + 4 + 4 + len(name_data)

    # Encode properties
    props_data = node.props

    # Encode children recursively
    children_data = bytearray()
    child_offset = base_offset + header_size + len(props_data)

    for child in node.children:
        chunk = _fbx_encode_node(child, child_offset)
        children_data.extend(chunk)
        child_offset += len(chunk)

    if node.children:
        children_data.extend(NULL_RECORD)
        child_offset += len(NULL_RECORD)

    end_offset = base_offset + header_size + len(props_data) + len(children_data)
    header = struct.pack('<III', end_offset, node.num_props, len(props_data)) + name_data

    return header + props_data + bytes(children_data)


# ─── High-level FBX scene builder ────────────────────────────────────────────

def _fbx_build(bones, world_mats, all_pos, all_nrm, all_uv, all_tri, all_bi, mesh_name="Mesh"):
    """
    Build a complete FBX 7.4 binary file from skeleton + skinned mesh data.

    Args:
        bones: list of {'name': str, 'parent': int, 'mat': 16-float tuple}
        world_mats: list of 16-float world matrices per bone
        all_pos: list of (x,y,z) world-space vertex positions
        all_nrm: list of (nx,ny,nz) world-space normals
        all_uv: list of (u,v) texture coordinates
        all_tri: list of (a,b,c) triangle indices
        all_bi: list of bone_index per vertex
        mesh_name: name for the mesh object

    Returns: bytes (complete FBX 7.4 binary file)
    """
    nv = len(all_pos)
    nt = len(all_tri)

    # ── Generate unique IDs ───────────────────────────────────────────────
    _next_id = [1000000000]
    def uid():
        _next_id[0] += 1
        return _next_id[0]

    bone_ids = [uid() for _ in bones]
    mesh_model_id = uid()
    geom_id = uid()
    skin_id = uid()
    pose_id = uid()
    mat_id = uid()

    # Per-bone vertex assignments
    b2v = {}
    for vi, bi in enumerate(all_bi):
        b2v.setdefault(bi, []).append(vi)
    cluster_ids = {bi: uid() for bi in b2v}

    # ── FBXHeaderExtension ────────────────────────────────────────────────
    header_ext = _FbxNode('FBXHeaderExtension').add(
        _FbxNode('FBXHeaderVersion', _I(1003)),
        _FbxNode('FBXVersion', _I(7400)),
        _FbxNode('Creator', _S('rr_asset_tool')),
    )

    # ── GlobalSettings ────────────────────────────────────────────────────
    gs_props = _FbxNode('Properties70').add(
        _FbxNode('P', _S('UpAxis'), _S('int'), _S('Integer'), _S(''), _I(1)),
        _FbxNode('P', _S('UpAxisSign'), _S('int'), _S('Integer'), _S(''), _I(1)),
        _FbxNode('P', _S('FrontAxis'), _S('int'), _S('Integer'), _S(''), _I(2)),
        _FbxNode('P', _S('FrontAxisSign'), _S('int'), _S('Integer'), _S(''), _I(1)),
        _FbxNode('P', _S('CoordAxis'), _S('int'), _S('Integer'), _S(''), _I(0)),
        _FbxNode('P', _S('CoordAxisSign'), _S('int'), _S('Integer'), _S(''), _I(1)),
        _FbxNode('P', _S('UnitScaleFactor'), _S('double'), _S('Number'), _S(''), _D(1.0)),
    )
    global_settings = _FbxNode('GlobalSettings').add(
        _FbxNode('Version', _I(1000)),
        gs_props,
    )

    # ── Documents ─────────────────────────────────────────────────────────
    doc_node = _FbxNode('Document', _L(1000000000), _S('Scene'), _S('Scene')).add(
        _FbxNode('RootNode', _L(0)),
    )
    documents = _FbxNode('Documents').add(
        _FbxNode('Count', _I(1)),
        doc_node,
    )

    # ── References ────────────────────────────────────────────────────────
    references = _FbxNode('References')

    # ── Definitions ───────────────────────────────────────────────────────
    n_models = len(bones) + 1  # bones + mesh
    n_deformers = 1 + len(cluster_ids)  # skin + clusters
    definitions = _FbxNode('Definitions').add(
        _FbxNode('Version', _I(100)),
        _FbxNode('Count', _I(n_models + 1 + n_deformers + 1 + 1)),  # models + geom + deformers + pose + material
        _FbxNode('ObjectType', _S('GlobalSettings')).add(_FbxNode('Count', _I(1))),
        _FbxNode('ObjectType', _S('Model')).add(_FbxNode('Count', _I(n_models))),
        _FbxNode('ObjectType', _S('Geometry')).add(_FbxNode('Count', _I(1))),
        _FbxNode('ObjectType', _S('Deformer')).add(_FbxNode('Count', _I(n_deformers))),
        _FbxNode('ObjectType', _S('Pose')).add(_FbxNode('Count', _I(1))),
        _FbxNode('ObjectType', _S('Material')).add(_FbxNode('Count', _I(1))),
    )

    # ── Objects ───────────────────────────────────────────────────────────
    objects = _FbxNode('Objects')

    # Bone models
    for i, b in enumerate(bones):
        bone_type = 'Null' if b['parent'] == -1 else 'LimbNode'
        m = b['mat']
        tx, ty, tz = m[12], m[13], m[14]

        props = _FbxNode('Properties70').add(
            _FbxNode('P', _S('Lcl Translation'), _S('Lcl Translation'), _S(''), _S('A'), _D(tx), _D(ty), _D(tz)),
        )
        bone_node = _FbxNode('Model', _L(bone_ids[i]), _S(f'Model::{b["name"]}\x00\x01Model'), _S(bone_type)).add(
            _FbxNode('Version', _I(232)),
            props,
        )
        objects.add(bone_node)

    # Mesh model
    mesh_model = _FbxNode('Model', _L(mesh_model_id), _S(f'Model::{mesh_name}\x00\x01Model'), _S('Mesh')).add(
        _FbxNode('Version', _I(232)),
        _FbxNode('Properties70'),
    )
    objects.add(mesh_model)

    # Geometry
    vertices_flat = []
    for x, y, z in all_pos:
        vertices_flat.extend([x, y, z])

    poly_indices = []
    for a, b, c in all_tri:
        poly_indices.extend([a, b, -(c + 1)])  # FBX: last index negated and -1

    geom = _FbxNode('Geometry', _L(geom_id), _S('Geometry::\x00\x01Geometry'), _S('Mesh')).add(
        _FbxNode('GeometryVersion', _I(124)),
        _FbxNode('Vertices', _ad(vertices_flat)),
        _FbxNode('PolygonVertexIndex', _ai(poly_indices)),
    )

    # Normals layer
    if all_nrm:
        normals_flat = []
        for a, b, c in all_tri:
            for vi in (a, b, c):
                if vi < len(all_nrm):
                    nx, ny, nz = all_nrm[vi]
                else:
                    nx, ny, nz = 0, 1, 0
                normals_flat.extend([nx, ny, nz])

        norm_layer = _FbxNode('LayerElementNormal', _I(0)).add(
            _FbxNode('Version', _I(102)),
            _FbxNode('Name', _S('')),
            _FbxNode('MappingInformationType', _S('ByPolygonVertex')),
            _FbxNode('ReferenceInformationType', _S('Direct')),
            _FbxNode('Normals', _ad(normals_flat)),
        )
        geom.add(norm_layer)

    # UV layer
    if all_uv:
        uv_flat = []
        uv_indices = []
        for a, b, c in all_tri:
            for vi in (a, b, c):
                if vi < len(all_uv):
                    u, v = all_uv[vi]
                else:
                    u, v = 0, 0
                uv_flat.extend([u, 1.0 - v])
                uv_indices.append(len(uv_flat) // 2 - 1)

        uv_layer = _FbxNode('LayerElementUV', _I(0)).add(
            _FbxNode('Version', _I(101)),
            _FbxNode('Name', _S('UVMap')),
            _FbxNode('MappingInformationType', _S('ByPolygonVertex')),
            _FbxNode('ReferenceInformationType', _S('Direct')),
            _FbxNode('UV', _ad(uv_flat)),
        )
        geom.add(uv_layer)

    # Layer definition
    layer = _FbxNode('Layer', _I(0)).add(
        _FbxNode('Version', _I(100)),
    )
    if all_nrm:
        layer.add(_FbxNode('LayerElement').add(
            _FbxNode('Type', _S('LayerElementNormal')),
            _FbxNode('TypedIndex', _I(0)),
        ))
    if all_uv:
        layer.add(_FbxNode('LayerElement').add(
            _FbxNode('Type', _S('LayerElementUV')),
            _FbxNode('TypedIndex', _I(0)),
        ))
    geom.add(layer)
    objects.add(geom)

    # Material (basic)
    mat_props = _FbxNode('Properties70').add(
        _FbxNode('P', _S('DiffuseColor'), _S('Color'), _S(''), _S('A'), _D(0.8), _D(0.8), _D(0.8)),
    )
    material = _FbxNode('Material', _L(mat_id), _S('Material::Default\x00\x01Material'), _S('')).add(
        _FbxNode('Version', _I(102)),
        _FbxNode('ShadingModel', _S('phong')),
        mat_props,
    )
    objects.add(material)

    # Skin deformer
    skin = _FbxNode('Deformer', _L(skin_id), _S('Deformer::Skin\x00\x01Deformer'), _S('Skin')).add(
        _FbxNode('Version', _I(101)),
        _FbxNode('Link_DeformAcuracy', _D(50.0)),
    )
    objects.add(skin)

    # Clusters (bone → vertex assignments)
    for bi, cid in cluster_ids.items():
        vis = b2v[bi]
        weights = [1.0] * len(vis)

        # Transform and TransformLink matrices
        # TransformLink = world matrix of the bone (bind pose)
        wm = world_mats[bi]
        transform_link = [wm[r * 4 + c] for r in range(4) for c in range(4)]

        # Transform = inverse of TransformLink (mesh-to-bone)
        # For rigid skinning with identity mesh transform, Transform = inverse(world_mat)
        inv = _fbx_mat4_inverse(wm)
        transform = [inv[r * 4 + c] for r in range(4) for c in range(4)]

        cluster = _FbxNode('Deformer', _L(cid), _S(f'SubDeformer::{bones[bi]["name"]}\x00\x01SubDeformer'), _S('Cluster')).add(
            _FbxNode('Version', _I(100)),
            _FbxNode('UserData', _S(''), _S('')),
            _FbxNode('Indexes', _ai(vis)),
            _FbxNode('Weights', _ad(weights)),
            _FbxNode('Transform', _ad(transform)),
            _FbxNode('TransformLink', _ad(transform_link)),
        )
        objects.add(cluster)

    # BindPose
    pose_nodes = []
    for i in range(len(bones)):
        wm = world_mats[i]
        mat_flat = [wm[r * 4 + c] for r in range(4) for c in range(4)]
        pose_nodes.append(_FbxNode('PoseNode').add(
            _FbxNode('Node', _L(bone_ids[i])),
            _FbxNode('Matrix', _ad(mat_flat)),
        ))
    # Mesh pose node (identity)
    pose_nodes.append(_FbxNode('PoseNode').add(
        _FbxNode('Node', _L(mesh_model_id)),
        _FbxNode('Matrix', _ad([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1])),
    ))

    bind_pose = _FbxNode('Pose', _L(pose_id), _S('Pose::BindPose\x00\x01Pose'), _S('BindPose')).add(
        _FbxNode('Type', _S('BindPose')),
        _FbxNode('Version', _I(100)),
        _FbxNode('NbPoseNodes', _I(len(pose_nodes))),
        *pose_nodes,
    )
    objects.add(bind_pose)

    # ── Connections ───────────────────────────────────────────────────────
    connections = _FbxNode('Connections')

    # Bones → parent (or root → scene)
    for i, b in enumerate(bones):
        if b['parent'] == -1:
            connections.add(_FbxNode('C', _S('OO'), _L(bone_ids[i]), _L(0)))
        else:
            connections.add(_FbxNode('C', _S('OO'), _L(bone_ids[i]), _L(bone_ids[b['parent']])))

    # Mesh → scene
    connections.add(_FbxNode('C', _S('OO'), _L(mesh_model_id), _L(0)))
    # Geometry → mesh
    connections.add(_FbxNode('C', _S('OO'), _L(geom_id), _L(mesh_model_id)))
    # Material → mesh
    connections.add(_FbxNode('C', _S('OO'), _L(mat_id), _L(mesh_model_id)))
    # Skin → geometry
    connections.add(_FbxNode('C', _S('OO'), _L(skin_id), _L(geom_id)))
    # Clusters → skin, bones → clusters
    for bi, cid in cluster_ids.items():
        connections.add(_FbxNode('C', _S('OO'), _L(cid), _L(skin_id)))
        connections.add(_FbxNode('C', _S('OO'), _L(bone_ids[bi]), _L(cid)))

    # ── Serialize ─────────────────────────────────────────────────────────
    return _fbx_serialize([
        header_ext, global_settings, documents, references,
        definitions, objects, connections,
    ])


def _fbx_mat4_inverse(m):
    """Invert a 4x4 row-major matrix. Returns 16-element list."""
    # Expand to readable vars
    m00,m01,m02,m03 = m[0],m[1],m[2],m[3]
    m10,m11,m12,m13 = m[4],m[5],m[6],m[7]
    m20,m21,m22,m23 = m[8],m[9],m[10],m[11]
    m30,m31,m32,m33 = m[12],m[13],m[14],m[15]

    a2323 = m22*m33 - m23*m32; a1323 = m21*m33 - m23*m31
    a1223 = m21*m32 - m22*m31; a0323 = m20*m33 - m23*m30
    a0223 = m20*m32 - m22*m30; a0123 = m20*m31 - m21*m30
    a2313 = m12*m33 - m13*m32; a1313 = m11*m33 - m13*m31
    a1213 = m11*m32 - m12*m31; a2312 = m12*m23 - m13*m22
    a1312 = m11*m23 - m13*m21; a1212 = m11*m22 - m12*m21
    a0313 = m10*m33 - m13*m30; a0213 = m10*m32 - m12*m30
    a0312 = m10*m23 - m13*m20; a0212 = m10*m22 - m12*m20
    a0113 = m10*m31 - m11*m30; a0112 = m10*m21 - m11*m20

    det = (m00*(m11*a2323 - m12*a1323 + m13*a1223)
         - m01*(m10*a2323 - m12*a0323 + m13*a0223)
         + m02*(m10*a1323 - m11*a0323 + m13*a0123)
         - m03*(m10*a1223 - m11*a0223 + m12*a0123))

    if abs(det) < 1e-30:
        return [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]

    inv_det = 1.0 / det

    return [
        inv_det *  (m11*a2323 - m12*a1323 + m13*a1223),
        inv_det * -(m01*a2323 - m02*a1323 + m03*a1223),
        inv_det *  (m01*a2313 - m02*a1313 + m03*a1213),
        inv_det * -(m01*a2312 - m02*a1312 + m03*a1212),
        inv_det * -(m10*a2323 - m12*a0323 + m13*a0223),
        inv_det *  (m00*a2323 - m02*a0323 + m03*a0223),
        inv_det * -(m00*a2313 - m02*a0313 + m03*a0213),
        inv_det *  (m00*a2312 - m02*a0312 + m03*a0212),
        inv_det *  (m10*a1323 - m11*a0323 + m13*a0123),
        inv_det * -(m00*a1323 - m01*a0323 + m03*a0123),
        inv_det *  (m00*a1313 - m01*a0313 + m03*a0113),
        inv_det * -(m00*a1312 - m01*a0312 + m03*a0112),
        inv_det * -(m10*a1223 - m11*a0223 + m12*a0123),
        inv_det *  (m00*a1223 - m01*a0223 + m02*a0123),
        inv_det * -(m00*a1213 - m01*a0213 + m02*a0113),
        inv_det *  (m00*a1212 - m01*a0212 + m02*a0112),
    ]




def export_psg_to_fbx(psg_path, fbx_path, log_fn=None):
    """Export a .psg file to FBX 7.4 binary with skeleton + rigid skinning.
    Returns (num_verts, num_tris, num_bones) on success, None on failure."""

    with open(psg_path, 'rb') as f:
        data = f.read()

    magic = data[0:4]
    if magic[:3] not in (b'psg', b'pss', b'pgi'):
        return None

    pos = 4
    version, pos = _rd(data, pos, '<I')
    num_objects, pos = _rd(data, pos, '<I')
    if num_objects < 2:
        return None  # No skeleton worth exporting

    bones = []
    for _ in range(num_objects):
        name = data[pos:pos+128].split(b'\x00')[0].decode('ascii', 'replace'); pos += 128
        mat = struct.unpack_from('<16f', data, pos); pos += 64
        pos += 28  # bbox_c(12) + bbox_h(12) + radius(4)
        parent = struct.unpack_from('<i', data, pos)[0]; pos += 4
        bones.append({'name': name, 'parent': parent, 'mat': mat})

    world_mats = [_world_mat(i, bones) for i in range(num_objects)]

    total_surfs, pos = _rd(data, pos, '<I')
    mat_names = []
    for _ in range(total_surfs):
        mat_names.append(data[pos:pos+64].split(b'\x00')[0].decode('ascii', 'replace')); pos += 64

    xyz_d, nrm_d, st_d, _ = struct.unpack_from('<4f', data, pos); pos += 16
    bbcx, bbcy, bbcz = struct.unpack_from('<3f', data, pos); pos += 12
    fixed = struct.unpack_from('<i', data, pos)[0]; pos += 4
    num_lod = data[pos]; anim = data[pos+1]; pos += 4

    if anim & 1:
        mp_flag = struct.unpack_from('<I', data, pos)[0]; pos += 4
        n_pals = mp_flag & 0x3FFFFFFF
        if mp_flag & 0x40000000:
            n_ids = struct.unpack_from('<I', data, pos)[0]; pos += 4; pos += n_ids
        elif mp_flag & 0x80000000:
            for _ in range(n_pals):
                n_ids = struct.unpack_from('<I', data, pos)[0]; pos += 4; pos += n_ids
            pos += total_surfs
        else:
            n_ids = struct.unpack_from('<I', data, pos)[0]; pos += 4; pos += n_ids

    HAS_UV1 = 0x0004; HAS_UV2 = 0x0010

    # First pass: determine boneSize
    save_pos = pos; all_w = []
    for _ in range(num_lod):
        pos += 4  # skip lod distance
        nsurf = struct.unpack_from('<I', data, pos)[0]; pos += 4
        for _ in range(nsurf):
            pos += 4  # mat_idx
            vt = struct.unpack_from('<I', data, pos)[0]; pos += 4
            n_uv = (1 if vt & HAS_UV1 else 0) + (1 if vt & HAS_UV2 else 0)
            pos += n_uv * 4 + 4
            dma_sz = struct.unpack_from('<I', data, pos)[0]; pos += 4
            dma = data[pos:pos+dma_sz]; pos += dma_sz
            _, _, _, _, rw = _decode_packets(dma, xyz_d, bbcx, bbcy, bbcz, nrm_d, st_d)
            all_w.extend(w for w in rw if 0 < w < 32768)
    boneSize = _infer_bone_size(all_w)
    if boneSize <= 0:
        return None
    pos = save_pos

    # Second pass: extract skinned mesh
    all_pos = []; all_nrm = []; all_uv = []; all_tri = []; all_bi = []
    vo = 0
    for _ in range(num_lod):
        pos += 4
        nsurf = struct.unpack_from('<I', data, pos)[0]; pos += 4
        for si in range(nsurf):
            pos += 4  # mat_idx
            vt = struct.unpack_from('<I', data, pos)[0]; pos += 4
            n_uv = (1 if vt & HAS_UV1 else 0) + (1 if vt & HAS_UV2 else 0)
            uv_scales = []
            for _ in range(n_uv):
                s = struct.unpack_from('<f', data, pos)[0]; pos += 4; uv_scales.append(s)
            pos += 4
            dma_sz = struct.unpack_from('<I', data, pos)[0]; pos += 4
            dma = data[pos:pos+dma_sz]; pos += dma_sz
            verts, nrms, uvs, adc, raw_w = _decode_packets(dma, xyz_d, bbcx, bbcy, bbcz, nrm_d, st_d)
            if not verts: continue

            bi_list = []
            for wa in raw_w:
                bi = 0
                if not fixed and boneSize > 0 and wa > 0 and wa % boneSize == 0:
                    bi = wa // boneSize - 1
                    if bi < 0 or bi >= num_objects: bi = 0
                bi_list.append(bi)

            wv = []; wn = []
            for vi, v in enumerate(verts):
                bi = bi_list[vi]; wm = world_mats[bi]
                wv.append((v[0]*wm[0]+v[1]*wm[4]+v[2]*wm[8]+wm[12],
                           v[0]*wm[1]+v[1]*wm[5]+v[2]*wm[9]+wm[13],
                           v[0]*wm[2]+v[1]*wm[6]+v[2]*wm[10]+wm[14]))
                if vi < len(nrms):
                    n = nrms[vi]
                    wn.append((n[0]*wm[0]+n[1]*wm[4]+n[2]*wm[8],
                               n[0]*wm[1]+n[1]*wm[5]+n[2]*wm[9],
                               n[0]*wm[2]+n[1]*wm[6]+n[2]*wm[10]))

            tris = _build_triangles(verts, adc, nrms if len(nrms) == len(verts) else None)
            if not tris: continue

            all_pos.extend(wv); all_nrm.extend(wn); all_uv.extend(uvs)
            all_bi.extend(bi_list)
            all_tri.extend([(a+vo, b+vo, c+vo) for a, b, c in tris])
            vo += len(wv)

    if not all_pos or not all_tri:
        return None

    nv = len(all_pos); nt = len(all_tri)
    mesh_name = os.path.splitext(os.path.basename(psg_path))[0]

    fbx_data = _fbx_build(bones, world_mats, all_pos, all_nrm, all_uv, all_tri, all_bi, mesh_name)
    with open(fbx_path, 'wb') as f:
        f.write(fbx_data)
    return (nv, nt, len(bones))


def batch_convert(input_dir, output_dir, log_fn=None, export_fbx=False):
    """Convert all PSGs to OBJ and PSTs to PNG in a directory tree."""
    def log(msg):
        if log_fn: log_fn(msg)

    os.makedirs(output_dir, exist_ok=True)
    n_psg = ok_psg = n_pst = ok_pst = 0

    for root, _, files in os.walk(input_dir):
        for fn in sorted(files):
            ext = os.path.splitext(fn)[1].lower()
            src = os.path.join(root, fn)
            rel = os.path.relpath(root, input_dir)
            dd = os.path.join(output_dir, rel)
            os.makedirs(dd, exist_ok=True)

            if ext == '.psg':
                n_psg += 1
                out = os.path.join(dd, os.path.splitext(fn)[0] + '.obj')
                try:
                    meshes = parse_psg(src)
                    if meshes:
                        write_obj(meshes, out)
                        tv = sum(len(m.positions) for m in meshes)
                        tt = sum(len(m.triangles) for m in meshes)
                        log(f"  ✓ {fn}  {tv}v {tt}t")
                        ok_psg += 1
                    else:
                        log(f"  ⚠ {fn}  (no geometry)")
                except Exception as e:
                    log(f"  ✗ {fn}  {e}")

                if export_fbx:
                    fbx_out = os.path.join(dd, os.path.splitext(fn)[0] + '.fbx')
                    try:
                        fbx_res = export_psg_to_fbx(src, fbx_out)
                        if fbx_res:
                            log(f"  ✓ {fn} → FBX  {fbx_res[0]}v {fbx_res[1]}t {fbx_res[2]} bones")
                        # None = no skeleton, silently skip
                    except Exception as e:
                        log(f"  ✗ {fn} FBX  {e}")

            elif ext == '.pst':
                n_pst += 1
                out = os.path.join(dd, os.path.splitext(fn)[0] + '.tga')
                try:
                    res = convert_pst_to_png(src, out)
                    if res:
                        note = ("  ⚠ IPU: colors may be approximate — "
                                "capture from PCSX2 for accuracy"
                                if res[2] == 2 else "")
                        log(f"  ✓ {fn}  {res[0]}×{res[1]}{note}")
                        ok_pst += 1
                    else:
                        log(f"  ⚠ {fn}  (failed)")
                except Exception as e:
                    log(f"  ✗ {fn}  {e}")

            elif ext == '.tga':
                shutil.copy2(src, os.path.join(dd, fn))

    log(f"\nDone. PSG: {ok_psg}/{n_psg}  PST: {ok_pst}/{n_pst}")


# ─────────────────────────────────────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────────────────────────────────────

class RacerRevengeToolApp:
    def __init__(self, root):
        self.root = root
        root.title("Star Wars: Racer Revenge — Asset Tool v1.3")
        root.geometry("750x600")
        root.minsize(650, 500)

        # Style
        BG = '#B2B2B2'
        root.configure(bg=BG)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', background=BG)
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG)
        style.configure('TCheckbutton', background=BG)
        style.configure('TLabelframe', background=BG)
        style.configure('TLabelframe.Label', background=BG)
        style.configure('TNotebook', background=BG)
        style.configure('TNotebook.Tab', background='#A0A0A0', padding=[8, 4])
        style.map('TNotebook.Tab', background=[('selected', BG)])
        style.configure('Header.TLabel', font=('Segoe UI', 11, 'bold'), background=BG)
        style.configure('Status.TLabel', font=('Segoe UI', 9), background=BG)

        # Main frame
        main = ttk.Frame(root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # ── Header ──
        ttk.Label(main, text="Star Wars: Racer Revenge — Asset Tool",
                  style='Header.TLabel').pack(anchor=tk.W)
        ttk.Separator(main, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(5, 10))

        # ── Tab notebook ──
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tab 1: Extract RES
        self._build_extract_tab()

        # Tab 2: Convert Assets
        self._build_convert_tab()

        # Tab 3: Assemble Track
        self._build_assemble_tab()

        # ── Log area ──
        log_frame = ttk.LabelFrame(main, text="Log", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10,
                                                   font=('Consolas', 9),
                                                   state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main, textvariable=self.status_var,
                  style='Status.TLabel').pack(anchor=tk.W, pady=(5, 0))

    def _build_extract_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  Extract RES  ")

        # Input
        row = ttk.Frame(tab); row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="RES File:", width=12).pack(side=tk.LEFT)
        self.extract_input = tk.StringVar()
        ttk.Entry(row, textvariable=self.extract_input).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(row, text="Browse...", command=self._browse_res).pack(side=tk.RIGHT)

        # Output
        row2 = ttk.Frame(tab); row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Output Dir:", width=12).pack(side=tk.LEFT)
        self.extract_output = tk.StringVar()
        ttk.Entry(row2, textvariable=self.extract_output).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(row2, text="Browse...", command=self._browse_extract_out).pack(side=tk.RIGHT)

        # Options
        opt_frame = ttk.Frame(tab); opt_frame.pack(fill=tk.X, pady=(10, 5))
        self.extract_convert = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="Also convert PSG→OBJ and PST→PNG after extraction",
                        variable=self.extract_convert).pack(anchor=tk.W)
        self.extract_assemble = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="Also assemble track (if SCB found)",
                        variable=self.extract_assemble).pack(anchor=tk.W)

        # Go button
        ttk.Button(tab, text="Extract", command=self._do_extract).pack(pady=10)

    def _build_convert_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  Convert Assets  ")

        row = ttk.Frame(tab); row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="Input Dir:", width=12).pack(side=tk.LEFT)
        self.convert_input = tk.StringVar()
        ttk.Entry(row, textvariable=self.convert_input).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(row, text="Browse...", command=self._browse_convert_in).pack(side=tk.RIGHT)

        row2 = ttk.Frame(tab); row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Output Dir:", width=12).pack(side=tk.LEFT)
        self.convert_output = tk.StringVar()
        ttk.Entry(row2, textvariable=self.convert_output).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(row2, text="Browse...", command=self._browse_convert_out).pack(side=tk.RIGHT)

        opt_frame = ttk.Frame(tab); opt_frame.pack(fill=tk.X, pady=(10, 5))
        self.convert_fbx = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="Also export PSG → FBX (skeleton + skinning, for files with bones)",
                        variable=self.convert_fbx).pack(anchor=tk.W)

        ttk.Button(tab, text="Convert All", command=self._do_convert).pack(pady=10)

        ttk.Label(tab,
                  text="Note: IPU-compressed textures (character/vehicle .pst) may have"
                       " incorrect colors.\nFor accurate results, capture them from PCSX2"
                       " using its texture dump feature (Settings → Advanced → Texture Dumping).",
                  foreground='#555555', justify=tk.LEFT,
                  wraplength=620).pack(anchor=tk.W, pady=(0, 4))

    def _build_assemble_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  Assemble Track  ")

        row = ttk.Frame(tab); row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="SCB File:", width=12).pack(side=tk.LEFT)
        self.assemble_scb = tk.StringVar()
        ttk.Entry(row, textvariable=self.assemble_scb).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(row, text="Browse...", command=self._browse_scb).pack(side=tk.RIGHT)

        row2 = ttk.Frame(tab); row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Geometry Dir:", width=12).pack(side=tk.LEFT)
        self.assemble_geom = tk.StringVar()
        ttk.Entry(row2, textvariable=self.assemble_geom).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(row2, text="Browse...", command=self._browse_geom).pack(side=tk.RIGHT)

        row3 = ttk.Frame(tab); row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="Output OBJ:", width=12).pack(side=tk.LEFT)
        self.assemble_output = tk.StringVar()
        ttk.Entry(row3, textvariable=self.assemble_output).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(row3, text="Browse...", command=self._browse_assemble_out).pack(side=tk.RIGHT)

        ttk.Button(tab, text="Assemble Track", command=self._do_assemble).pack(pady=10)

    # ── Browse helpers ──
    def _browse_res(self):
        f = filedialog.askopenfilename(filetypes=[("RES Files", "*.RES *.res"), ("All", "*.*")])
        if f:
            self.extract_input.set(f)
            base = os.path.splitext(f)[0]
            self.extract_output.set(base)

    def _browse_extract_out(self):
        d = filedialog.askdirectory()
        if d: self.extract_output.set(d)

    def _browse_convert_in(self):
        d = filedialog.askdirectory()
        if d:
            self.convert_input.set(d)
            self.convert_output.set(d + '_converted')

    def _browse_convert_out(self):
        d = filedialog.askdirectory()
        if d: self.convert_output.set(d)

    def _browse_scb(self):
        f = filedialog.askopenfilename(filetypes=[("SCB Files", "*.scb"), ("All", "*.*")])
        if f:
            self.assemble_scb.set(f)
            # Auto-fill geometry dir
            parent = os.path.dirname(os.path.dirname(f))
            geom = os.path.join(parent, 'geometry')
            if os.path.isdir(geom):
                self.assemble_geom.set(geom)
            # Auto-fill output
            name = os.path.splitext(os.path.basename(f))[0]
            self.assemble_output.set(os.path.join(parent, f'{name}_track.obj'))

    def _browse_geom(self):
        d = filedialog.askdirectory()
        if d: self.assemble_geom.set(d)

    def _browse_assemble_out(self):
        f = filedialog.asksaveasfilename(defaultextension='.obj',
                                          filetypes=[("OBJ Files", "*.obj")])
        if f: self.assemble_output.set(f)

    # ── Logging ──
    def log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + '\n')
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self.root.update_idletasks()

    def clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete('1.0', tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ── Actions ──
    def _run_in_thread(self, func):
        """Run a function in a background thread."""
        def wrapper():
            try:
                func()
            except Exception as e:
                self.log(f"\nERROR: {e}")
            finally:
                self.status_var.set("Ready")

        self.status_var.set("Working...")
        threading.Thread(target=wrapper, daemon=True).start()

    def _do_extract(self):
        res = self.extract_input.get()
        out = self.extract_output.get()
        if not res or not out:
            messagebox.showwarning("Missing Input", "Select a RES file and output directory.")
            return
        if not os.path.isfile(res):
            messagebox.showerror("Not Found", f"File not found: {res}")
            return

        def work():
            self.clear_log()
            self.log(f"Extracting: {os.path.basename(res)}")
            n, total = extract_res(res, out, log_fn=self.log)
            self.log(f"\nExtraction complete: {n}/{total} files → {out}")

            if self.extract_convert.get():
                self.log(f"\nConverting assets...")
                batch_convert(out, out + '_converted', log_fn=self.log)

            if self.extract_assemble.get():
                # Find SCB
                scbs = glob.glob(os.path.join(out, 'tracks', '*.scb'))
                geom = os.path.join(out, 'geometry')
                if scbs and os.path.isdir(geom):
                    name = os.path.splitext(os.path.basename(scbs[0]))[0]
                    obj_out = os.path.join(out, f'{name}_track.obj')
                    self.log(f"\nAssembling track: {name}")
                    assemble_track(scbs[0], geom, obj_out, log_fn=self.log)
                else:
                    self.log("\nNo SCB/geometry found for assembly.")

        self._run_in_thread(work)

    def _do_convert(self):
        inp = self.convert_input.get()
        out = self.convert_output.get()
        if not inp or not out:
            messagebox.showwarning("Missing Input", "Select input and output directories.")
            return
        do_fbx = self.convert_fbx.get()

        def work():
            self.clear_log()
            self.log(f"Converting: {inp}")
            if do_fbx:
                self.log("FBX export enabled for files with skeletons")
            batch_convert(inp, out, log_fn=self.log, export_fbx=do_fbx)

        self._run_in_thread(work)

    def _do_assemble(self):
        scb = self.assemble_scb.get()
        geom = self.assemble_geom.get()
        out = self.assemble_output.get()
        if not scb or not geom or not out:
            messagebox.showwarning("Missing Input", "Fill in all fields.")
            return

        def work():
            self.clear_log()
            self.log(f"Assembling track...")
            assemble_track(scb, geom, out, log_fn=self.log)

        self._run_in_thread(work)


def main():
    root = tk.Tk()
    app = RacerRevengeToolApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
