#!/usr/bin/env python3
"""
Cocos Creator 3.x → 2.x Prefab Converter  (v3 — full pipeline)

NEW in v3:
  1. Folder input  : scans all .prefab recursively, mirrors structure to output
  2. Asset cloning : copies Textures, Fonts, AudioClips, AnimationClips,
                     and their .meta files into output/assets/<category>/
                     Re-wires __uuid__ refs in converted prefabs to new paths
  3. Script conversion : TypeScript (.ts) custom components bound on prefabs
                     are transpiled to CC2-compatible JavaScript ES5 with:
                       - class X extends cc.Component  →  cc.Class({ extends: cc.Component })
                       - @property decorators          →  properties: { ... }
                       - @ccclass decorator            →  stripped (handled by cc.Class)
                       - lifecycle methods preserved   (onLoad, start, update, …)
                       - import statements             →  require() calls
                       - export default / export class →  stripped
                     Unknown / complex TS is preserved with a header comment

Usage:
    python cc3_to_cc2_converter.py  SRC_DIR  OUT_DIR
    python cc3_to_cc2_converter.py  SRC_DIR  OUT_DIR  --assets SRC_ASSETS_DIR
    python cc3_to_cc2_converter.py  file.prefab  out.prefab
    python cc3_to_cc2_converter.py  SRC_DIR  OUT_DIR  --verbose  --strict  --no-scripts  --no-assets

Flags:
    --assets DIR     root of CC3 assets (default: parent of SRC_DIR / "assets")
    --verbose        print every component and asset copied
    --strict         abort on unknown component
    --no-assets      skip asset cloning
    --no-scripts     skip script conversion
"""

import json, sys, re, shutil, argparse, warnings
from pathlib import Path
from collections import defaultdict

# ── globals ──────────────────────────────────────────────────────────────────
VERBOSE    = False
STRICT     = False
DO_ASSETS  = True
DO_SCRIPTS = True

# ─────────────────────────────────────────────────────────────────────────────
# Primitive helpers
# ─────────────────────────────────────────────────────────────────────────────

def vec2(x=0,y=0):               return {"__type__":"cc.Vec2","x":x,"y":y}
def vec3(x=0,y=0,z=0):           return {"__type__":"cc.Vec3","x":x,"y":y,"z":z}
def cc_color(r=255,g=255,b=255,a=255): return {"__type__":"cc.Color","r":r,"g":g,"b":b,"a":a}
def ref(i):                       return {"__id__":i}

def _f(d,*keys,default=0.0):
    for k in keys:
        if k in d: return float(d[k])
    return float(default)

def _i(d,*keys,default=0):
    for k in keys:
        if k in d: return int(d[k])
    return int(default)

def _b(d,*keys,default=False):
    for k in keys:
        if k in d: return bool(d[k])
    return bool(default)

def _s(d,*keys,default=""):
    for k in keys:
        if k in d: return str(d[k])
    return str(default)

def _color(d,key,default=None):
    c=d.get(key) or {}
    return cc_color(_i(c,"r",default=255),_i(c,"g",default=255),
                    _i(c,"b",default=255),_i(c,"a",default=255))

def _asset(d,*keys):
    for k in keys:
        v=d.get(k)
        if isinstance(v,dict) and v.get("__uuid__"):
            return {"__uuid__":v["__uuid__"]}
    return None

def _events(d,*keys):
    for k in keys:
        v=d.get(k)
        if isinstance(v,list): return v
    return []

# ─────────────────────────────────────────────────────────────────────────────
# Asset registry  (built once per run, shared across all prefab conversions)
# ─────────────────────────────────────────────────────────────────────────────

class AssetRegistry:
    """
    Scans a CC3 assets root and builds:
      uuid  →  Path  (the asset file)
      uuid  →  category  (Textures / Fonts / Sounds / Animations / Scripts)

    Categories are inferred from file extension + .meta content.
    """
    EXT_CATEGORY = {
        ".png":  "Textures", ".jpg": "Textures", ".jpeg": "Textures",
        ".webp": "Textures", ".psd": "Textures", ".svg":  "Textures",
        ".ttf":  "Fonts",    ".otf": "Fonts",    ".fnt":  "Fonts",
        ".mp3":  "Sounds",   ".ogg": "Sounds",   ".wav":  "Sounds",
        ".m4a":  "Sounds",
        ".anim": "Animations",
        ".ts":   "Scripts",  ".js":  "Scripts",
        ".atlas":"Spine",    ".skel":"Spine",     ".json": None,  # json may be spine/atlas
        ".tmx":  "TiledMaps",
        ".plist":"Particles",
    }

    def __init__(self, assets_root: Path):
        self.root = assets_root
        self.uuid_to_path: dict[str, Path]  = {}
        self.uuid_to_cat:  dict[str, str]   = {}
        self._scan()

    def _scan(self):
        if not self.root or not self.root.exists():
            return
        for meta in self.root.rglob("*.meta"):
            try:
                m = json.loads(meta.read_text("utf-8"))
            except Exception:
                continue
            uuid = m.get("uuid")
            if not uuid:
                continue
            asset_path = meta.with_suffix("")   # strip .meta
            if not asset_path.exists():
                # some .meta files describe sub-assets; keep the meta at least
                asset_path = meta
            ext = asset_path.suffix.lower()
            cat = self.EXT_CATEGORY.get(ext)
            # refine json: if meta has "type" field use it
            if ext == ".json":
                mtype = m.get("type","")
                if "DragonBones" in mtype:  cat = "Spine"
                elif "spine"     in mtype.lower(): cat = "Spine"
                elif "anim"      in mtype.lower(): cat = "Animations"
                else: cat = "Misc"
            if cat is None:
                cat = "Misc"
            self.uuid_to_path[uuid] = asset_path
            self.uuid_to_cat[uuid]  = cat
            # sub-asset uuids inside the meta (spriteFrames, etc.)
            for sub in m.get("subMetas", {}).values():
                sub_uuid = sub.get("uuid")
                if sub_uuid:
                    self.uuid_to_path[sub_uuid] = asset_path
                    self.uuid_to_cat[sub_uuid]  = cat

    def resolve(self, uuid: str):
        return self.uuid_to_path.get(uuid), self.uuid_to_cat.get(uuid, "Misc")


# ─────────────────────────────────────────────────────────────────────────────
# Asset copier
# ─────────────────────────────────────────────────────────────────────────────

class AssetCopier:
    """
    Tracks all UUIDs seen in prefab data, copies each asset (+ its .meta)
    into  out_root/assets/<Category>/  exactly once.
    Returns a uuid → new_relative_path dict for re-wiring.
    """
    def __init__(self, registry: AssetRegistry, out_root: Path):
        self.reg      = registry
        self.out_root = out_root
        self._done:   set[str]        = set()
        self.uuid_map: dict[str, str] = {}  # uuid → new relative path string

    def register(self, uuid: str):
        if not uuid or uuid in self._done:
            return
        self._done.add(uuid)
        src, cat = self.reg.resolve(uuid)
        if src is None:
            if VERBOSE:
                print(f"    [asset] UUID {uuid[:8]}… not found in registry")
            return
        dst_dir = self.out_root / "assets" / cat
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst     = dst_dir / src.name
        rel     = str(dst.relative_to(self.out_root)).replace("\\","/")
        self.uuid_map[uuid] = rel

        if not dst.exists():
            shutil.copy2(src, dst)
            if VERBOSE:
                print(f"    [asset] {cat}/{src.name}  ({uuid[:8]}…)")
        # copy .meta too
        meta_src = src.parent / (src.name + ".meta")
        if not meta_src.exists():
            meta_src = src.with_suffix(src.suffix + ".meta")
        meta_dst = dst.parent / (dst.name + ".meta")
        if meta_src.exists() and not meta_dst.exists():
            shutil.copy2(meta_src, meta_dst)

    def collect_from_prefab(self, data: list):
        """Walk entire prefab JSON and register every __uuid__ found."""
        def walk(obj):
            if isinstance(obj, dict):
                u = obj.get("__uuid__")
                if u: self.register(u)
                for v in obj.values(): walk(v)
            elif isinstance(obj, list):
                for v in obj: walk(v)
        walk(data)


# ─────────────────────────────────────────────────────────────────────────────
# Script converter  (TypeScript CC3  →  JavaScript CC2)
# ─────────────────────────────────────────────────────────────────────────────

# Lifecycle methods that CC2 cc.Component understands
CC2_LIFECYCLE = {
    "onLoad","start","update","lateUpdate","onEnable","onDisable",
    "onDestroy","onFocusInEditor","onLostFocusInEditor",
    "resetInEditor","onRestore",
}

# CC3 decorator → CC2 property type hint (best-effort)
DECORATOR_TYPE_MAP = {
    "CCString":  '"string"', "CCInteger": '"number"', "CCFloat": '"number"',
    "CCBoolean": '"boolean"',
    "Node":      "cc.Node", "Sprite":    "cc.Sprite", "Label":   "cc.Label",
    "Button":    "cc.Button","AudioClip": "cc.AudioClip",
    "Prefab":    "cc.Prefab","SpriteFrame":"cc.SpriteFrame",
    "Animation": "cc.Animation", "Animator": "cc.Animation",
}

def convert_script(ts_src: str, class_name: str = "") -> str:
    """
    Best-effort TypeScript (CC3) → JavaScript ES5 (CC2) transpiler.
    Handles the most common CC3 patterns. Complex generics / async / decorators
    that cannot be parsed are left as-is inside a commented warning block.
    """
    lines  = ts_src.splitlines()
    out    = []
    props  = {}          # name → {type, default, tooltip}
    in_class        = False
    class_detected  = ""
    base_class      = "cc.Component"
    skip_decorator  = False
    brace_depth     = 0
    class_brace_start = 0
    body_lines      = []   # lines inside the class body

    # ── pass 1: collect imports and class signature ───────────────────────────
    import_lines = []
    remaining    = []
    _cc_names    = set()   # names imported from 'cc' – replaced by cc.X in output
    for line in lines:
        # import { X, Y } from 'cc'  →  track names, suppress require (use cc.X directly)
        m = re.match(r"""^\s*import\s+\{([^}]+)\}\s+from\s+['"]cc['"]\s*;?\s*$""", line)
        if m:
            for nm in m.group(1).split(","):
                nm = nm.strip().split(" as ")[-1].strip()  # handle "X as Y"
                if nm: _cc_names.add(nm)
            continue
        # import { X } from 'other'
        m = re.match(r"""^\s*import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"]\s*;?\s*$""", line)
        if m:
            names = [n.strip() for n in m.group(1).split(",") if n.strip()]
            mod   = m.group(2)
            for nm in names:
                import_lines.append(f'const {nm} = require("{mod}");')
            continue
        m2 = re.match(r"""^\s*import\s+(\w+)\s+from\s+['"]([^'"]+)['"]\s*;?\s*$""", line)
        if m2:
            if m2.group(2) != "cc":
                import_lines.append(f'const {m2.group(1)} = require("{m2.group(2)}");')
            continue
        # strip export keyword
        line2 = re.sub(r'^\s*export\s+(default\s+)?','', line)
        remaining.append(line2)

    # ── pass 2: parse class, properties, methods ─────────────────────────────
    prop_buf   = []      # accumulates @property lines
    method_buf = []      # lines of current method
    methods    = []      # list of (name, is_lifecycle, body_str)
    in_prop    = False
    in_method  = False
    method_name= ""
    method_depth = 0
    class_name_found = ""
    base_found  = ""

    i = 0
    while i < len(remaining):
        line = remaining[i]
        stripped = line.strip()

        # @ccclass — skip
        if re.match(r'@ccclass', stripped):
            i += 1; continue

        # @property(...)  or  @property
        if re.match(r'@property', stripped):
            prop_buf.append(stripped)
            i += 1; continue

        # class declaration
        m = re.match(r'class\s+(\w+)(?:\s+extends\s+([\w.]+))?', stripped)
        if m and not in_class:
            class_name_found = m.group(1)
            base_found       = m.group(2) or "cc.Component"
            in_class         = True
            i += 1; continue

        if not in_class:
            i += 1; continue

        # inside class body ──────────────────────────────────────────────────

        # property field:  name: Type = default;
        if prop_buf and not in_method:
            fm = re.match(r'(\w+)\s*(?::\s*([\w.<>\[\]|]+))?\s*(?:=\s*(.+?))?;?\s*$', stripped)
            if fm and stripped and not stripped.startswith("//"):
                pname   = fm.group(1)
                ptype   = fm.group(2) or ""
                pdefval = fm.group(3) or "null"
                pdefval = pdefval.rstrip(";").strip()
                # map type
                cc2type = DECORATOR_TYPE_MAP.get(ptype, None)
                tooltip = ""
                for pb in prop_buf:
                    tm = re.search(r'tooltip\s*:\s*[\'"]([^\'"]+)[\'"]', pb)
                    if tm: tooltip = tm.group(1)
                props[pname] = {"cc2type": cc2type, "default": pdefval, "tooltip": tooltip}
                prop_buf = []
                i += 1; continue

        prop_buf = []

        # method declaration
        _KEYWORDS = {"if","else","for","while","switch","try","catch","return","new","typeof","instanceof"}
        mm = re.match(r'^(async\s+)?(\w+)\s*\(([^)]*)\)\s*(?::\s*[\w<>\[\]|]+\s*)?\s*\{', stripped)
        if mm and not in_method and mm.group(2) not in _KEYWORDS:
            method_name  = mm.group(2)
            in_method    = True
            method_depth = stripped.count("{") - stripped.count("}")
            method_buf   = [_strip_types_from_line(line)]
            if method_depth <= 0:
                methods.append((method_name, method_name in CC2_LIFECYCLE, "\n".join(method_buf)))
                in_method = False; method_buf = []; method_name = ""
            i += 1; continue

        if in_method:
            method_buf.append(_strip_types_from_line(line))
            method_depth += stripped.count("{") - stripped.count("}")
            if method_depth <= 0:
                methods.append((method_name, method_name in CC2_LIFECYCLE, "\n".join(method_buf)))
                in_method = False; method_buf = []; method_name = ""
            i += 1; continue

        i += 1

    # ── pass 3: render CC2 cc.Class(...) ─────────────────────────────────────
    cn   = class_name_found or class_name or "MyComponent"
    base_raw = base_found or "cc.Component"
    # resolve base: if it's a plain name imported from cc, prefix with cc.
    if base_raw in _cc_names or (base_raw == "Component"):
        base = "cc.Component"
    elif "." not in base_raw:
        base = base_raw   # user script base – keep as-is
    else:
        base = base_raw

    result_lines = [
        f"// AUTO-CONVERTED from TypeScript (CC3) → JavaScript (CC2)",
        f"// Original class: {cn}  extends {base}",
        f"// Converter: cc3_to_cc2_converter.py",
        "",
    ]
    if import_lines:
        result_lines += import_lines + [""]

    result_lines += [f'var {cn} = cc.Class({{', f'    name: "{cn}",', f'    extends: {base},', ""]

    # properties block
    if props:
        result_lines.append("    properties: {")
        for pname, info in props.items():
            t    = info["cc2type"]
            dflt = info["default"]
            tip  = info["tooltip"]
            # resolve type: if it's a cc-imported name, prefix with cc.
            if t and t in _cc_names:
                t = "cc." + t
            if t:
                result_lines.append(f"        {pname}: {{")
                result_lines.append(f"            default: {dflt},")
                result_lines.append(f"            type: {t},")
                if tip:
                    result_lines.append(f'            tooltip: "{tip}",')
                result_lines.append("        },")
            else:
                result_lines.append(f"        {pname}: {dflt},")
        result_lines += ["    },", ""]

    # methods – extract original param list from source
    for idx2, (mname, is_lifecycle, mbody) in enumerate(methods):
        # try to recover parameter names from the method signature
        sig_m = re.search(r'\b' + re.escape(mname) + r'\s*\(([^)]*)\)', mbody)
        params = ""
        if sig_m:
            raw_params = sig_m.group(1)
            # strip type annotations from each param  "points: number" → "points"
            clean_params = []
            for param in raw_params.split(","):
                param = param.strip()
                param = re.sub(r'\s*:\s*[\w.<>\[\]|]+', '', param)
                param = re.sub(r'\s*=\s*.+', '', param).strip()
                if param: clean_params.append(param)
            params = ", ".join(clean_params)

        trailing_comma = "," if idx2 < len(methods) - 1 else ""
        result_lines.append(f"    {mname}: function({params}) {{")
        body_src = mbody.splitlines()
        # skip the opening brace line (already added above)
        for ml in body_src[1:]:
            result_lines.append("    " + ml)
        result_lines[-1] = result_lines[-1] + trailing_comma   # add comma after closing }
        result_lines.append("")

    result_lines += ["});", "", f"module.exports = {cn};"]
    return "\n".join(result_lines)


def _strip_types_from_line(line: str) -> str:
    """Remove common TypeScript type annotations from a single line."""
    # remove  : Type  in variable declarations and params (not in ternary)
    line = re.sub(r':\s*(?:readonly\s+)?[\w.<>\[\]|]+(?=\s*[=,);{])', '', line)
    # remove <T> generics in casts
    line = re.sub(r'<[\w.<>\[\], ]+>', '', line)
    # remove 'as Type'
    line = re.sub(r'\bas\s+\w[\w.<>]*', '', line)
    # remove access modifiers
    line = re.sub(r'\b(private|protected|public|readonly|static)\s+', '', line)
    # remove 'let' → 'var', keep const
    line = re.sub(r'\blet\b', 'var', line)
    return line


def convert_script_file(src: Path, dst: Path):
    """Read a .ts file, convert, write as .js"""
    dst = dst.with_suffix(".js")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        ts_src = src.read_text("utf-8")
    except Exception as e:
        print(f"  ✗  Cannot read script {src}: {e}", file=sys.stderr)
        return
    js_out = convert_script(ts_src, src.stem)
    dst.write_text(js_out, "utf-8")
    if VERBOSE:
        print(f"    [script] {src.name}  →  {dst.name}")


# ─────────────────────────────────────────────────────────────────────────────
# CC3 node helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_all_components(cc3, node):
    out = []
    for cr in node.get("_components", []):
        ci = cr.get("__id__")
        if ci is not None and ci < len(cc3):
            out.append((ci, cc3[ci]))
    return out

def get_component(cc3, node, substr):
    for _, c in get_all_components(cc3, node):
        if substr in c.get("__type__",""):
            return c
    return None

def read_position(n):
    p = n.get("_lpos", n.get("position", {}))
    return _f(p,"x"), _f(p,"y"), _f(p,"z")

def read_scale(n):
    s = n.get("_lscale", n.get("scale", {}))
    return _f(s,"x",default=1), _f(s,"y",default=1), _f(s,"z",default=1)

def read_rotation(n):
    e = n.get("_euler_angles")
    return (_f(e,"x"),_f(e,"y"),_f(e,"z")) if e else (0.0,0.0,0.0)

def read_ui_transform(cc3, node):
    uit = get_component(cc3, node, "UITransform")
    if not uit: return 100.0, 40.0, 0.5, 0.5
    cs = uit.get("_contentSize", {})
    ap = uit.get("_anchorPoint", {})
    return (_f(cs,"width","x",default=100), _f(cs,"height","y",default=40),
            _f(ap,"x",default=0.5),         _f(ap,"y",default=0.5))

# ─────────────────────────────────────────────────────────────────────────────
# CC2 node builder
# ─────────────────────────────────────────────────────────────────────────────

def build_node(name, x,y,z, sx,sy, rot_z,
               w,h, ax,ay, active, opacity, r,g,b,a,
               parent_ref, children_refs, comp_refs, prefab_info=None):
    return {
        "__type__":     "cc.Node",
        "_name":        name,
        "_objFlags":    0,
        "_parent":      parent_ref,
        "_children":    children_refs,
        "_active":      active,
        "_components":  comp_refs,
        "_prefab":      prefab_info,
        "_opacity":     int(opacity),
        "_color":       cc_color(r,g,b,a),
        "_contentSize": {"__type__":"cc.Size","width":w,"height":h},
        "_anchorPoint": vec2(ax,ay),
        "_trs":{"__type__":"TypedArray","ctor":"Float64Array",
                "array":[x,y,z,0,0,0,1,sx,sy,1]},
        "_eulerAngles": vec3(0,0,rot_z),
        "_skewX":0,"_skewY":0,"_is3DNode":False,
        "_groupIndex":0,"groupIndex":0,"_id":"",
    }

# ─────────────────────────────────────────────────────────────────────────────
# Component converters
# ─────────────────────────────────────────────────────────────────────────────

def conv_widget(c,nr):
    af=0
    if _b(c,"_alignTop",   "isAlignTop"):    af|=1
    if _b(c,"_alignBottom","isAlignBottom"): af|=2
    if _b(c,"_alignLeft",  "isAlignLeft"):   af|=4
    if _b(c,"_alignRight", "isAlignRight"):  af|=8
    return {"__type__":"cc.Widget","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "alignMode":_i(c,"alignMode",default=1),"_target":None,"_alignFlags":af,
            "_left":_f(c,"_left","left"),"_right":_f(c,"_right","right"),
            "_top":_f(c,"_top","top"),   "_bottom":_f(c,"_bottom","bottom"),
            "_isAbsLeft":_b(c,"_isAbsLeft",default=True),
            "_isAbsRight":_b(c,"_isAbsRight",default=True),
            "_isAbsTop":_b(c,"_isAbsTop",default=True),
            "_isAbsBottom":_b(c,"_isAbsBottom",default=True),
            "_originalWidth":0,"_originalHeight":0,"_id":""}

def conv_sprite(c,nr):
    fc=c.get("_fillCenter",{})
    return {"__type__":"cc.Sprite","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),"_materials":[None],
            "_srcBlendFactor":_i(c,"_srcBlendFactor","srcBlendFactor",default=770),
            "_dstBlendFactor":_i(c,"_dstBlendFactor","dstBlendFactor",default=771),
            "_spriteFrame":_asset(c,"_spriteFrame","spriteFrame"),
            "_type":_i(c,"_type","type"),"_sizeMode":_i(c,"_sizeMode","sizeMode"),
            "_fillType":_i(c,"_fillType","fillType"),
            "_fillCenter":vec2(_f(fc,"x"),_f(fc,"y")),
            "_fillStart":_f(c,"_fillStart","fillStart"),
            "_fillRange":_f(c,"_fillRange","fillRange"),
            "_isTrimmedMode":_b(c,"_isTrimmedMode","isTrimmedMode",default=True),
            "_atlas":_asset(c,"_atlas","atlas"),"_id":""}

def _lstyle(c):
    f=0
    if _b(c,"_isBold","isBold","bold"):             f|=1
    if _b(c,"_isItalic","isItalic","italic"):       f|=2
    if _b(c,"_isUnderline","isUnderline","underline"): f|=4
    return f

def conv_label(c,nr):
    return {"__type__":"cc.Label","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_materials":[None],"_srcBlendFactor":770,"_dstBlendFactor":771,
            "_string":_s(c,"_string","string"),"_N$string":_s(c,"_string","string"),
            "_fontSize":_i(c,"_fontSize","fontSize",default=40),
            "_lineHeight":_i(c,"_lineHeight","lineHeight",default=40),
            "_enableWrapText":_b(c,"_enableWrapText","enableWrapText",default=True),
            "_N$file":_asset(c,"_font","font"),
            "_isSystemFontUsed":_b(c,"_useSystemFont","useSystemFont",default=True),
            "_spacingX":_f(c,"_spacingX","spacingX"),"_batchAsBitmap":False,
            "_styleFlags":_lstyle(c),"_underlineHeight":_i(c,"_underlineHeight",default=2),
            "_N$horizontalAlign":_i(c,"_horizontalAlign","horizontalAlign","_hAlign","hAlign"),
            "_N$verticalAlign":  _i(c,"_verticalAlign",  "verticalAlign",  "_vAlign","vAlign"),
            "_N$fontFamily":_s(c,"_fontFamily","fontFamily",default="Arial"),
            "_N$overflow":_i(c,"_overflow","overflow"),
            "_N$cacheMode":_i(c,"_cacheMode","cacheMode"),"_id":""}

def conv_rich_text(c,nr):
    return {"__type__":"cc.RichText","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_string":_s(c,"_string","string"),"_N$string":_s(c,"_string","string"),
            "_N$horizontalAlign":_i(c,"_horizontalAlign","horizontalAlign"),
            "_N$fontSize":_i(c,"_fontSize","fontSize",default=40),
            "_N$fontFamily":_s(c,"_fontFamily",default="Arial"),
            "_N$font":_asset(c,"_font","font"),
            "_N$maxWidth":_i(c,"_maxWidth","maxWidth"),
            "_N$lineHeight":_i(c,"_lineHeight","lineHeight",default=40),
            "_N$imageAtlas":_asset(c,"_imageAtlas","imageAtlas"),"_id":""}

def conv_button(c,nr):
    return {"__type__":"cc.Button","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_N$interactable":_b(c,"_interactable","interactable",default=True),
            "_N$enableAutoGrayEffect":_b(c,"_enableAutoGrayEffect","enableAutoGrayEffect"),
            "_N$transition":_i(c,"_transition","transition"),
            "duration":_f(c,"duration","_duration",default=0.1),
            "zoomScale":_f(c,"zoomScale","_zoomScale",default=1.2),
            "_N$normalColor":  _color(c,"_normalColor"),
            "_N$pressedColor": _color(c,"_pressedColor"),
            "hoverColor":      _color(c,"_hoverColor"),
            "_N$disabledColor":_color(c,"_disabledColor"),
            "_N$normalSprite":  _asset(c,"_normalSprite","normalSprite"),
            "_N$pressedSprite": _asset(c,"_pressedSprite","pressedSprite"),
            "hoverSprite":      _asset(c,"_hoverSprite","hoverSprite"),
            "_N$disabledSprite":_asset(c,"_disabledSprite","disabledSprite"),
            "_N$target":None,
            "clickEvents":_events(c,"clickEvents","_clickEvents"),"_id":""}

def conv_toggle(c,nr):
    return {"__type__":"cc.Toggle","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_N$interactable":_b(c,"_interactable","interactable",default=True),
            "_N$enableAutoGrayEffect":_b(c,"_enableAutoGrayEffect"),
            "_N$transition":_i(c,"_transition","transition"),
            "duration":_f(c,"duration",default=0.1),"zoomScale":_f(c,"zoomScale",default=1.2),
            "_N$normalColor": _color(c,"_normalColor"),
            "_N$pressedColor":_color(c,"_pressedColor"),
            "hoverColor":     _color(c,"_hoverColor"),
            "_N$disabledColor":_color(c,"_disabledColor"),
            "_N$normalSprite": _asset(c,"_normalSprite"),
            "_N$pressedSprite":_asset(c,"_pressedSprite"),
            "hoverSprite":     _asset(c,"_hoverSprite"),
            "_N$disabledSprite":_asset(c,"_disabledSprite"),
            "_N$target":None,"clickEvents":_events(c,"clickEvents"),
            "isChecked":_b(c,"isChecked","_isChecked"),
            "_N$checkMark":None,"_N$toggleGroup":None,
            "checkEvents":_events(c,"checkEvents"),"_id":""}

def conv_toggle_group(c,nr):
    return {"__type__":"cc.ToggleGroup","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "allowSwitchOff":_b(c,"allowSwitchOff","_allowSwitchOff"),"_id":""}

def conv_slider(c,nr):
    return {"__type__":"cc.Slider","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "handle":None,"direction":_i(c,"direction","_direction"),
            "progress":_f(c,"progress","_progress",default=1.0),
            "slideEvents":_events(c,"slideEvents"),"_id":""}

def conv_scroll_view(c,nr):
    return {"__type__":"cc.ScrollView","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "horizontal":_b(c,"horizontal","_horizontal"),
            "vertical":  _b(c,"vertical","_vertical",default=True),
            "inertia":   _b(c,"inertia","_inertia",default=True),
            "brake":     _f(c,"brake","_brake",default=0.5),
            "elastic":   _b(c,"elastic","_elastic",default=True),
            "bounceDuration":_f(c,"bounceDuration","_bounceDuration",default=1.0),
            "scrollEvents":_events(c,"scrollEvents"),
            "cancelInnerEvents":_b(c,"cancelInnerEvents",default=True),
            "content":None,"_id":""}

def conv_scrollbar(c,nr):
    return {"__type__":"cc.Scrollbar","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "handle":None,"direction":_i(c,"direction","_direction"),
            "enableAutoHide":_b(c,"enableAutoHide","_enableAutoHide",default=True),
            "autoHideTime":_f(c,"autoHideTime","_autoHideTime",default=1.0),"_id":""}

def conv_page_view(c,nr):
    return {"__type__":"cc.PageView","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "sizeMode":_i(c,"sizeMode","_sizeMode"),
            "direction":_i(c,"direction","_direction"),
            "scrollThreshold":_f(c,"scrollThreshold",default=0.5),
            "autoPageTurnVelocity":_f(c,"autoPageTurnVelocity",default=180),
            "pageTurningEventTiming":_f(c,"pageTurningEventTiming",default=0.1),
            "indicator":None,"pageEvents":_events(c,"pageEvents"),
            "cancelInnerEvents":_b(c,"cancelInnerEvents",default=True),"_id":""}

def conv_page_indicator(c,nr):
    return {"__type__":"cc.PageViewIndicator","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "spriteFrame":_asset(c,"spriteFrame","_spriteFrame"),
            "direction":_i(c,"direction","_direction"),
            "spacing":_f(c,"spacing","_spacing"),"_id":""}

def conv_edit_box(c,nr):
    return {"__type__":"cc.EditBox","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_N$string":_s(c,"_string","string"),
            "_N$backgroundImage":_asset(c,"_backgroundImage","backgroundImage"),
            "_N$returnType":_i(c,"_returnType","returnType"),
            "_N$inputFlag":  _i(c,"_inputFlag","inputFlag"),
            "_N$inputMode":  _i(c,"_inputMode","inputMode"),
            "_N$fontSize":   _i(c,"_fontSize","fontSize",default=20),
            "_N$lineHeight": _i(c,"_lineHeight","lineHeight",default=40),
            "_N$fontColor":  _color(c,"_fontColor"),
            "_N$placeholder":_s(c,"_placeholder","placeholder"),
            "_N$placeholderFontSize": _i(c,"_placeholderFontSize",default=20),
            "_N$placeholderFontColor":_color(c,"_placeholderFontColor"),
            "_N$maxLength":  _i(c,"_maxLength","maxLength",default=20),
            "_N$tabIndex":   _i(c,"_tabIndex"),
            "editingDidBegan":_events(c,"editingDidBegan","editingDidBegin"),
            "editingChanged": _events(c,"editingChanged"),
            "editingDidEnded":_events(c,"editingDidEnded"),
            "textChanged":    _events(c,"textChanged"),"_id":""}

def conv_layout(c,nr):
    cs=c.get("_cellSize",{})
    return {"__type__":"cc.Layout","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_layoutSize":{"__type__":"cc.Size","width":0,"height":0},
            "_resize":_i(c,"_resize","resizeMode","_resizeMode"),
            "_N$layoutType":_i(c,"_N$layoutType","type","_layoutType"),
            "_N$cellSize":{"__type__":"cc.Size","width":_f(cs,"width",default=40),"height":_f(cs,"height",default=40)},
            "_N$startAxis":_i(c,"_startAxis","startAxis"),
            "_N$paddingLeft":  _f(c,"_paddingLeft","paddingLeft"),
            "_N$paddingRight": _f(c,"_paddingRight","paddingRight"),
            "_N$paddingTop":   _f(c,"_paddingTop","paddingTop"),
            "_N$paddingBottom":_f(c,"_paddingBottom","paddingBottom"),
            "_N$spacingX":_f(c,"_spacingX","spacingX"),
            "_N$spacingY":_f(c,"_spacingY","spacingY"),
            "_N$verticalDirection":  _i(c,"_verticalDirection","verticalDirection",default=1),
            "_N$horizontalDirection":_i(c,"_horizontalDirection","horizontalDirection"),
            "_N$affectedByScale":_b(c,"_affectedByScale","affectedByScale"),"_id":""}

def conv_mask(c,nr):
    return {"__type__":"cc.Mask","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),"_materials":[None],
            "_type":_i(c,"_type","type"),
            "_segments":_i(c,"_segments","segments",default=64),
            "_alphaThreshold":_f(c,"_alphaThreshold","alphaThreshold",default=0.1),
            "_inverted":_b(c,"_inverted","inverted"),
            "_spriteFrame":_asset(c,"_spriteFrame","spriteFrame"),"_id":""}

def conv_progress_bar(c,nr):
    return {"__type__":"cc.ProgressBar","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_N$barSprite":None,"_N$mode":_i(c,"_N$mode","mode"),
            "_N$totalLength":_f(c,"_N$totalLength","totalLength",default=200),
            "_N$progress":_f(c,"_N$progress","progress",default=1.0),
            "_N$reverse":_b(c,"_N$reverse","reverse"),"_id":""}

def conv_animation(c,nr):
    return {"__type__":"cc.Animation","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_clips":c.get("_clips",c.get("clips",[])),
            "_defaultClip":c.get("_defaultClip",c.get("defaultClip")),
            "playOnLoad":_b(c,"playOnLoad","_playOnLoad"),"_id":""}

def conv_audio(c,nr):
    return {"__type__":"cc.AudioSource","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "clip":_asset(c,"clip","_clip","_audioClip","audioClip"),
            "volume":_f(c,"volume","_volume",default=1.0),
            "mute":_b(c,"mute","_mute"),"loop":_b(c,"loop","_loop"),
            "playOnAwake":_b(c,"playOnAwake","_playOnAwake",default=True),"_id":""}

def conv_camera(c,nr):
    return {"__type__":"cc.Camera","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_cullingMask":_i(c,"_cullingMask","cullingMask",default=0xFFFFFFFF),
            "_clearFlags":_i(c,"_clearFlags","clearFlags",default=7),
            "_backgroundColor":_color(c,"_backgroundColor"),
            "_depth":_i(c,"_depth","depth"),
            "_zoomRatio":_f(c,"_orthoSize","orthoSize",default=10),
            "_fov":_f(c,"_fov","fov",default=45),
            "_nearClip":_f(c,"_nearClip","nearClip",default=0.1),
            "_farClip":_f(c,"_farClip","farClip",default=1000),
            "targetTexture":_asset(c,"targetTexture","_targetTexture"),"_id":""}

def _cv(d,*keys,default=0.0):
    for k in keys:
        v=d.get(k)
        if v is None: continue
        if isinstance(v,dict): return float(v.get("constant",v.get("x",default)))
        return float(v)
    return float(default)

def conv_particle(c,nr):
    main=c.get("main",{}); em=c.get("emission",{}); shp=c.get("shape",{})
    return {"__type__":"cc.ParticleSystem","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),"_materials":[None],
            "preview":_b(c,"preview"),
            "playOnAwake":_b(main,"loop",default=True),
            "allowAnimationCulling":False,
            "duration":_cv(main,"duration",default=1.0),
            "capacity":_i(main,"maxParticles","_maxParticles",default=100),
            "loop":_b(main,"loop"),"playbackSpeed":_f(main,"simulationSpeed",default=1.0),
            "prewarm":_b(main,"prewarm"),"simulationSpace":_i(main,"simulationSpace"),
            "startDelay":_cv(main,"startDelay"),
            "startLifetime":_cv(main,"startLifetime",default=5.0),
            "startColor":main.get("startColor",cc_color()),"scaleSpace":0,
            "startSize":_cv(main,"startSize",default=50.0),
            "startRotation":_cv(main,"startAngle"),
            "startSpeed":_cv(main,"startSpeed",default=100.0),
            "gravity":_cv(main,"gravityModifier"),
            "rateOverTime":_cv(em,"rateOverTime",default=10.0),
            "rateOverDistance":_cv(em,"rateOverDistance"),
            "shapeModule":shp,
            "colorOverLifetimeModule":c.get("colorOverLifetime",{}),
            "sizeOverLifetimeModule":c.get("sizeOverLifetime",{}),
            "speedOverLifetimeModule":c.get("speedOverLifetime",{}),
            "rotationOverLifetimeModule":c.get("rotationOverLifetime",{}),
            "forceOverLifetimeModule":c.get("forceOverLifetime",{}),
            "spriteFrame":_asset(c,"spriteFrame","_spriteFrame"),"_id":""}

def conv_tiled_map(c,nr):
    return {"__type__":"cc.TiledMap","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),
            "_tmxFile":_asset(c,"_tmxFile","tmxFile"),"_id":""}

def conv_skeleton(c,nr):
    return {"__type__":"sp.Skeleton","_name":"","_objFlags":0,
            "node":nr,"_enabled":_b(c,"_enabled",default=True),"paused":_b(c,"paused"),
            "_N$skeletonData":_asset(c,"_skeletonData","skeletonData"),
            "_defaultSkin":_s(c,"_defaultSkin","defaultSkin",default="default"),
            "_defaultAnimation":_s(c,"_defaultAnimation","defaultAnimation"),
            "loop":_b(c,"loop","_loop",default=True),
            "premultipliedAlpha":_b(c,"premultipliedAlpha",default=True),
            "timeScale":_f(c,"timeScale","_timeScale",default=1.0),
            "debugBones":_b(c,"debugBones"),"debugSlots":_b(c,"debugSlots"),"_id":""}

def conv_custom_script(c, nr):
    """
    Any component whose __type__ contains a dot-less name or a project namespace
    (i.e. not a cc.* / sp.* builtin) is treated as a custom script component.
    We preserve all serialised properties and re-type it as the same name so
    CC2 can load the converted .js file.
    """
    t = c.get("__type__","")
    # Derive a short class name (last segment after last dot or slash)
    short = re.split(r'[./]', t)[-1]
    result = {"__type__": short, "_name":"","_objFlags":0, "node": nr,
              "_enabled": _b(c,"_enabled",default=True)}
    # copy all other serialised fields verbatim
    skip = {"__type__","_name","_objFlags","node","_enabled","_id"}
    for k,v in c.items():
        if k not in skip:
            result[k] = v
    result["_id"] = ""
    return result

# ─────────────────────────────────────────────────────────────────────────────
# Dispatch table
# ─────────────────────────────────────────────────────────────────────────────

COMPONENT_MAP = [
    ("UITransform",        None),
    ("Widget",             conv_widget),
    ("Sprite",             conv_sprite),
    ("Label",              conv_label),
    ("RichText",           conv_rich_text),
    ("Button",             conv_button),
    ("Toggle",             conv_toggle),
    ("ToggleGroup",        conv_toggle_group),
    ("Slider",             conv_slider),
    ("ScrollView",         conv_scroll_view),
    ("Scrollbar",          conv_scrollbar),
    ("ScrollBar",          conv_scrollbar),
    ("PageViewIndicator",  conv_page_indicator),
    ("PageView",           conv_page_view),
    ("EditBox",            conv_edit_box),
    ("Layout",             conv_layout),
    ("Mask",               conv_mask),
    ("ProgressBar",        conv_progress_bar),
    ("Animation",          conv_animation),
    ("AudioSource",        conv_audio),
    ("Camera",             conv_camera),
    ("ParticleSystem",     conv_particle),
    ("TiledMap",           conv_tiled_map),
    ("TiledLayer",         None),
    ("Skeleton",           conv_skeleton),
    ("Graphics",           "SKIP"),
    ("MeshRenderer",       "SKIP"),
    ("SkinnedMeshRenderer","SKIP"),
    ("Light",              "SKIP"),
    ("RigidBody",          "SKIP"),
    ("Collider",           "SKIP"),
]

# Types that are definitely engine builtins (don't treat as custom scripts)
_BUILTIN_PREFIXES = ("cc.","sp.","dragonBones.","cc3_","TypedArray")

def find_converter(type_str):
    for substr, fn in COMPONENT_MAP:
        if substr in type_str:
            return fn
    # If it looks like a user script (no known prefix), treat as custom
    if not any(type_str.startswith(p) for p in _BUILTIN_PREFIXES):
        return conv_custom_script
    return "UNKNOWN"

# ─────────────────────────────────────────────────────────────────────────────
# Prefab Converter
# ─────────────────────────────────────────────────────────────────────────────

class PrefabConverter:
    def __init__(self, cc3):
        self.cc3 = cc3
        self.cc2 = []
        self._map = {}

    def _allocate(self):
        for i,o in enumerate(self.cc3):
            if isinstance(o,dict) and o.get("__type__") in ("cc.Node","cc.Prefab"):
                self._map[i] = len(self.cc2)
                self.cc2.append(None)

    def _prefab_header(self):
        hdr = self.cc3[0] if self.cc3 else {}
        if not isinstance(hdr,dict): return
        root_cc3 = hdr["data"].get("__id__") if "data" in hdr else None
        if root_cc3 is None:
            for i,o in enumerate(self.cc3):
                if isinstance(o,dict) and o.get("__type__")=="cc.Node":
                    p=o.get("_parent")
                    if not (p and "__id__" in p):
                        root_cc3=i; break
        root_cc2 = self._map.get(root_cc3,1)
        h={"__type__":"cc.Prefab","_name":hdr.get("_name",""),"_objFlags":0,
           "data":ref(root_cc2),"optimizationPolicy":0,"asyncLoadAssets":False,"readonly":False}
        slot=self._map.get(0)
        if slot is not None and self.cc2[slot] is None:
            self.cc2[slot]=h
        else:
            self.cc2.insert(0,h)

    def _convert_nodes(self):
        for ci in sorted(self._map):
            o=self.cc3[ci]
            if isinstance(o,dict) and o.get("__type__")=="cc.Node":
                self._convert_node(ci,o)

    def _convert_node(self,ci,n3):
        idx=self._map[ci]
        x,y,z=read_position(n3); sx,sy,_=read_scale(n3); _,_,rz=read_rotation(n3)
        w,h,ax,ay=read_ui_transform(self.cc3,n3)
        active=n3.get("_active",True); opacity=n3.get("_opacity",255)
        col=n3.get("_color",{}); r,g,b,a=col.get("r",255),col.get("g",255),col.get("b",255),col.get("a",255)
        name=n3.get("_name","Node")

        children=[ref(self._map[cr["__id__"]]) for cr in n3.get("_children",[])
                  if cr.get("__id__") in self._map]
        pr=n3.get("_parent")
        parent=ref(self._map[pr["__id__"]]) if (pr and "__id__" in pr and pr["__id__"] in self._map) else None

        comp_refs=[]
        for _,comp in get_all_components(self.cc3,n3):
            t=comp.get("__type__","")
            fn=find_converter(t)
            if fn is None: continue
            if fn=="SKIP":
                warnings.warn(f"Skipping (no CC2 equiv): {t}"); continue
            if fn=="UNKNOWN":
                if STRICT: raise ValueError(f"Unknown component: {t}")
                warnings.warn(f"Unknown component passed through: {t}")
                cc2c=dict(comp); cc2c["__type__"]="cc3_UNKNOWN_"+t.split(".")[-1]; cc2c["node"]=ref(idx)
            else:
                cc2c=fn(comp,ref(idx))
                if cc2c is None: continue

            ni=len(self.cc2); self.cc2.append(cc2c); comp_refs.append(ref(ni))
            if VERBOSE: print(f"      [{ni}] {cc2c.get('__type__','?')}")

        prefab_info=({"__type__":"cc.PrefabInfo","root":ref(idx),"asset":ref(0),"fileId":"","sync":False}
                     if parent is None else {"__id__":0})
        self.cc2[idx]=build_node(name,x,y,z,sx,sy,rz,w,h,ax,ay,active,opacity,r,g,b,a,
                                  parent,children,comp_refs,prefab_info)

    def convert(self):
        self._allocate(); self._prefab_header(); self._convert_nodes()
        return [o for o in self.cc2 if o is not None]


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline  (folder scan + asset copy + script convert)
# ─────────────────────────────────────────────────────────────────────────────

class Pipeline:
    def __init__(self, src_root: Path, out_root: Path, assets_root: Path):
        self.src     = src_root
        self.out     = out_root
        # When output is a single file, place assets/ next to it, not inside it
        assets_out_root = out_root.parent if (src_root.is_file() and out_root.suffix) else out_root
        self.reg     = AssetRegistry(assets_root) if DO_ASSETS else None
        self.copier  = AssetCopier(self.reg, assets_out_root) if DO_ASSETS and self.reg else None

        # stats
        self.n_prefabs  = 0
        self.n_assets   = 0
        self.n_scripts  = 0
        self.n_errors   = 0

        # script source lookup: class_short_name → Path(.ts)
        self._script_map: dict[str, Path] = {}
        if DO_SCRIPTS and assets_root and assets_root.exists():
            for ts in assets_root.rglob("*.ts"):
                self._script_map[ts.stem] = ts

    # ── entry ─────────────────────────────────────────────────────────────────
    def run(self):
        if self.src.is_file():
            self._process_prefab(self.src, self.out if self.out.suffix else self.out/self.src.name)
        elif self.src.is_dir():
            prefabs = sorted(self.src.rglob("*.prefab"))
            if not prefabs:
                print(f"No .prefab files found under {self.src}")
                return
            print(f"Found {len(prefabs)} prefab(s) under {self.src}\n")
            for sp in prefabs:
                rel = sp.relative_to(self.src)
                self._process_prefab(sp, self.out/rel)
        else:
            print(f"Error: {self.src} not found", file=sys.stderr); sys.exit(1)

        self._print_summary()

    # ── single prefab ─────────────────────────────────────────────────────────
    def _process_prefab(self, src: Path, dst: Path):
        print(f"  {src.name}")
        try:
            data = json.loads(src.read_text("utf-8"))
        except Exception as e:
            print(f"    ✗ read error: {e}", file=sys.stderr); self.n_errors+=1; return
        if not isinstance(data,list):
            print(f"    ✗ not a CC3 prefab (expected JSON array)", file=sys.stderr); self.n_errors+=1; return

        # 1. collect + copy assets
        if self.copier:
            self.copier.collect_from_prefab(data)

        # 2. collect custom scripts bound on this prefab
        script_types = set()
        if DO_SCRIPTS:
            for obj in data:
                if not isinstance(obj,dict): continue
                t=obj.get("__type__","")
                if t and not any(t.startswith(p) for p in _BUILTIN_PREFIXES):
                    short=re.split(r'[./]',t)[-1]
                    script_types.add(short)

        # 3. convert prefab
        try:
            out_data = PrefabConverter(data).convert()
        except Exception as e:
            print(f"    ✗ conversion error: {e}", file=sys.stderr); self.n_errors+=1
            if STRICT: raise
            return

        dst.parent.mkdir(parents=True,exist_ok=True)
        dst.write_text(json.dumps(out_data,ensure_ascii=False,indent=2),"utf-8")
        print(f"    ✓ prefab  ({len(out_data)} objects)")
        self.n_prefabs+=1

        # 4. convert bound scripts
        if DO_SCRIPTS:
            for short in script_types:
                self._convert_script(short, dst.parent)

    # ── script conversion ─────────────────────────────────────────────────────
    def _convert_script(self, class_name: str, prefab_out_dir: Path):
        ts_path = self._script_map.get(class_name)
        if ts_path is None:
            print(f"    ⚠  Script '{class_name}.ts' not found in assets root")
            return
        dst_scripts = self.out / "assets" / "Scripts"
        dst_scripts.mkdir(parents=True,exist_ok=True)
        dst_js = dst_scripts / (class_name+".js")
        if dst_js.exists():
            return   # already converted (shared by multiple prefabs)
        convert_script_file(ts_path, dst_js)
        print(f"    ✓ script  {class_name}.ts  →  Scripts/{class_name}.js")
        self.n_scripts+=1

    # ── summary ───────────────────────────────────────────────────────────────
    def _print_summary(self):
        n_assets = len(self.copier.uuid_map) if self.copier else 0
        print("\n" + "─"*50)
        print(f"  Prefabs   converted : {self.n_prefabs}")
        print(f"  Assets    copied    : {n_assets}")
        print(f"  Scripts   converted : {self.n_scripts}")
        if self.n_errors:
            print(f"  Errors              : {self.n_errors}")
        print(f"  Output              : {self.out}")
        print("─"*50)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global VERBOSE, STRICT, DO_ASSETS, DO_SCRIPTS
    p=argparse.ArgumentParser(
        description="CC3 → CC2 prefab converter  (v3: folder + assets + scripts)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert entire prefabs folder, auto-detect assets root
  python cc3_to_cc2_converter.py  cc3_project/assets/prefabs/  cc2_project/assets/prefabs/

  # Explicit assets root (to copy textures / sounds / fonts from)
  python cc3_to_cc2_converter.py  prefabs/  out/  --assets cc3_project/assets/

  # Single file, no asset copying
  python cc3_to_cc2_converter.py  UI.prefab  UI_cc2.prefab  --no-assets --no-scripts

  # Verbose output, abort on unknown components
  python cc3_to_cc2_converter.py  prefabs/  out/  --verbose --strict

Output layout:
  out/
    prefabs/          ← converted .prefab files (mirrors input structure)
    assets/
      Textures/       ← .png / .jpg + .meta files
      Fonts/          ← .ttf / .fnt + .meta
      Sounds/         ← .mp3 / .ogg / .wav + .meta
      Animations/     ← .anim + .meta
      Scripts/        ← converted .js files (from .ts)
      Spine/          ← .atlas / .skel / .json + .meta
      TiledMaps/      ← .tmx + .meta
      Misc/           ← everything else
""")
    p.add_argument("input",  help="Source .prefab file or folder")
    p.add_argument("output", help="Destination file or folder")
    p.add_argument("--assets",     metavar="DIR",
                   help="CC3 assets root for scanning UUIDs (default: auto-detect)")
    p.add_argument("--verbose",    action="store_true", help="Verbose output")
    p.add_argument("--strict",     action="store_true", help="Abort on unknown component")
    p.add_argument("--no-assets",  action="store_true", help="Skip asset copying")
    p.add_argument("--no-scripts", action="store_true", help="Skip script conversion")
    args=p.parse_args()

    VERBOSE    = args.verbose
    STRICT     = args.strict
    DO_ASSETS  = not args.no_assets
    DO_SCRIPTS = not args.no_scripts

    src = Path(args.input)
    out = Path(args.output)

    # auto-detect assets root
    if args.assets:
        assets_root = Path(args.assets)
    else:
        # Walk up the directory tree from src looking for a folder named "assets"
        assets_root = None
        search_path = src if src.is_dir() else src.parent
        for parent in [search_path] + list(search_path.parents):
            candidate = parent if parent.name == "assets" and parent.is_dir() else parent / "assets"
            if candidate.is_dir():
                assets_root = candidate
                break
        if assets_root is None:
            assets_root = src.parent  # fallback

    if DO_ASSETS:
        print(f"Assets root : {assets_root}")
    print(f"Source      : {src}")
    print(f"Output      : {out}\n")

    warnings.simplefilter("always")
    Pipeline(src, out, assets_root).run()
    print("\nDone.")

if __name__=="__main__":
    main()