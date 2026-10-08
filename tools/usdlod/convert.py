#!/usr/bin/env python3
"""Rewrite Caldera's LOD variant sets into the UsdLod schema.

Caldera expresses level of detail as two variant sets: `districtLod` (full, proxy) on districts and terrain tiles, and `modelLod` (lod0 ...
lod3) on model assets. Each variant holds one arc (a reference or a payload to the level's file) and an `extentsHint`. This tool rewrites
every such variant set into a UsdLod root, so a consumer needs to know only UsdLod:

    <prim>/lod              LODRootAPI; lod:default:index = the coarsest level; lod:heuristics -> <prim>/lodHeuristic
    <prim>/lod/lod0 ...     one child per level, finest first, each a payload to the level's file
    <prim>/lodHeuristic     LODScreenSizeHeuristic, imaging domain, extent from the extentsHint

Every level becomes a payload, so only the levels a consumer loads are composed. An instanceable root (a model asset) keeps its instancing: its
structure is authored on </_usdlod/<prim>>, a class prim in the same layer, which the root references; the composed paths are the same. A
root with a single level gets no LOD root: the level's payload moves onto the prim.

    convert.py scan [--root DIR] [--report FILE]    report what would change; changes nothing
    convert.py run  [--root DIR] [--report FILE]    rewrite the layers in place

See README.md next to this file.
"""

import argparse
import json
import multiprocessing
import os
import sys
import time

from pxr import Gf, Sdf, Usd, Vt

LOD_SETS = ("districtLod", "modelLod")

# Finest first, per set. Variants not listed sort after the listed ones, by name.
LEVEL_ORDER = {
    "districtLod": ["full", "proxy"],
    "modelLod": ["lod0", "lod1", "lod2", "lod3"],
}

LAYER_EXTENSIONS = (".usd", ".usda", ".usdc")

# Screen-size thresholds: level k is wanted while the root's extent covers more than THRESHOLDS[k] of the view. A root with n levels uses the
# first n - 1. A starting point, to be tuned against what the engine draws.
THRESHOLDS = [0.25, 0.125, 0.0625]

# The prims the conversion adds under a root.
LOD_CHILD = "lod"
HEURISTIC_CHILD = "lodHeuristic"

# An instanceable root keeps its LOD structure on a prim under this class, in the same layer, and references it: a prim is only an instance
# if it has a composition arc of its own, and the variant set was that arc.
CLASS_ROOT = "_usdlod"


def layer_files(root):
    """Every USD layer under root, sorted, skipping the tool's own directory and hidden directories."""
    files = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if not d.startswith(".") and d != "tools")
        for name in sorted(names):
            if name.endswith(LAYER_EXTENSIONS):
                files.append(os.path.join(directory, name))
    return files


def ordered_variants(set_name, names):
    order = LEVEL_ORDER[set_name]
    return sorted(names, key=lambda name: (order.index(name) if name in order else len(order), name))


def arcs_of(spec):
    """The references and payloads a spec authors, as (kind, asset path, prim path) tuples."""
    arcs = []
    for reference in spec.referenceList.GetAddedOrExplicitItems():
        arcs.append(("reference", reference.assetPath, str(reference.primPath)))
    for payload in spec.payloadList.GetAddedOrExplicitItems():
        arcs.append(("payload", payload.assetPath, str(payload.primPath)))
    return arcs


def describe_body(spec):
    """A variant body: its arcs and extentsHint, and anything else it holds (which this tool does not convert)."""
    extents = None
    others = []
    for prop in spec.properties:
        if prop.name == "extentsHint" and isinstance(prop, Sdf.AttributeSpec) and prop.HasDefaultValue():
            extents = [list(map(float, corner)) for corner in prop.default]
        else:
            others.append("property " + prop.name)
    for child in spec.nameChildren:
        others.append("child " + child.name)
    for key in spec.ListInfoKeys():
        if key not in ("references", "payload", "specifier", "typeName"):
            others.append("metadata " + key)
    if spec.inheritPathList.GetAddedOrExplicitItems() or spec.specializesList.GetAddedOrExplicitItems():
        others.append("inherits or specializes")
    return {"arcs": arcs_of(spec), "extentsHint": extents, "others": others}


def walk_specs(spec, visit):
    """Every prim spec below spec, including those inside variants."""
    for child in spec.nameChildren:
        visit(child)
        walk_specs(child, visit)
    for variant_set in spec.variantSets:
        for variant in variant_set.variants:
            if variant.primSpec:
                walk_specs(variant.primSpec, visit)


def scan_layer(path):
    """What one layer authors that the conversion touches."""
    try:
        layer = Sdf.Layer.FindOrOpen(path)
    except Exception as error:  # A layer USD can't read is reported, not fatal.
        return {"path": path, "error": str(error)}
    if layer is None:
        return {"path": path, "error": "cannot open"}
    roots = []
    selections = []

    def visit(spec):
        defined_here = [name for name in spec.variantSets.keys() if name in LOD_SETS]
        for name in defined_here:
            variant_set = spec.variantSets[name]
            variants = {variant.name: variant for variant in variant_set.variants}
            levels = []
            for variant_name in ordered_variants(name, list(variants.keys())):
                body = describe_body(variants[variant_name].primSpec)
                body["variant"] = variant_name
                levels.append(body)
            roots.append({
                "path": str(spec.path),
                "set": name,
                "levels": levels,
                "selection": spec.variantSelections.get(name),
                "children": [child.name for child in spec.nameChildren],
            })
        for name, value in spec.variantSelections.items():
            if name in LOD_SETS and name not in defined_here:
                selections.append({"path": str(spec.path), "set": name, "value": value})

    walk_specs(layer.pseudoRoot, visit)
    if not roots and not selections:
        return None
    return {"path": path, "format": layer.GetFileFormat().formatId, "roots": roots, "selections": selections}


def merged_levels(levels):
    """The levels after merging consecutive levels whose arcs are the same."""
    merged = []
    for level in levels:
        if merged and merged[-1]["arcs"] == level["arcs"]:
            merged[-1]["variants"].append(level["variant"])
            continue
        merged.append({"arcs": level["arcs"], "variants": [level["variant"]], "extentsHint": level["extentsHint"]})
    return merged


def as_payload(arc):
    kind, asset_path, prim_path = arc
    return Sdf.Payload(asset_path, Sdf.Path(prim_path) if prim_path else Sdf.Path())


def vec3f_array(corners):
    return Vt.Vec3fArray([Gf.Vec3f(*corner) for corner in corners])


def set_attribute(spec, name, type_name, value, variability=Sdf.VariabilityVarying):
    attribute = spec.attributes[name] if name in spec.attributes else Sdf.AttributeSpec(spec, name, type_name, variability)
    attribute.default = value


def remove_variant_set(spec, set_name):
    """Removes a variant set, its name from the prim's list of variant sets, and the prim's own selection for it."""
    names = [name for name in spec.variantSetNameList.GetAddedOrExplicitItems() if name != set_name]
    spec.variantSetNameList.ClearEdits()
    if names:
        spec.variantSetNameList.prependedItems = names
    del spec.variantSets[set_name]
    if set_name in spec.variantSelections:
        del spec.variantSelections[set_name]


def convert_root(spec, set_name):
    """Rewrites one variant set into a UsdLod root (or, for one level, a payload on the prim). Returns what was done, or why not."""
    variants = {variant.name: variant for variant in spec.variantSets[set_name].variants}
    levels = []
    for variant_name in ordered_variants(set_name, list(variants.keys())):
        body = describe_body(variants[variant_name].primSpec)
        if len(body["arcs"]) != 1 or body["others"]:
            return {"skipped": f"variant {variant_name} holds {body['arcs']} {body['others']}"}
        if body["arcs"][0][0] == "reference" and not variants[variant_name].primSpec.referenceList.GetAddedOrExplicitItems()[0].layerOffset.IsIdentity():
            return {"skipped": f"variant {variant_name}'s reference has a layer offset"}
        body["variant"] = variant_name
        levels.append(body)
    if LOD_CHILD in spec.nameChildren or HEURISTIC_CHILD in spec.nameChildren:
        return {"skipped": f"the prim already has a child named {LOD_CHILD} or {HEURISTIC_CHILD}"}
    levels = merged_levels(levels)
    # The finest level that has an extentsHint: a district's full level has none, its proxy level's covers the district.
    extents = next((level["extentsHint"] for level in levels if level["extentsHint"] is not None), None)

    remove_variant_set(spec, set_name)
    if extents is not None:
        set_attribute(spec, "extentsHint", Sdf.ValueTypeNames.Float3Array, vec3f_array(extents))

    if len(levels) == 1:
        spec.payloadList.Prepend(as_payload(levels[0]["arcs"][0]))
        return {"levels": 1}

    holder = spec
    if spec.HasInfo("instanceable") and spec.instanceable:
        holder = class_holder(spec)
        spec.referenceList.Prepend(Sdf.Reference("", holder.path))

    lod = Sdf.PrimSpec(holder, LOD_CHILD, Sdf.SpecifierDef, "")
    lod.SetInfo("apiSchemas", Sdf.TokenListOp.Create(prependedItems=["LODRootAPI"]))
    set_attribute(lod, "lod:default:index", Sdf.ValueTypeNames.Int, len(levels) - 1)
    heuristics = Sdf.RelationshipSpec(lod, "lod:heuristics", False)
    heuristics.targetPathList.explicitItems = [holder.path.AppendChild(HEURISTIC_CHILD)]
    for index, level in enumerate(levels):
        child = Sdf.PrimSpec(lod, f"lod{index}", Sdf.SpecifierDef, "")
        child.payloadList.Prepend(as_payload(level["arcs"][0]))
        child.SetInfo("customData", {"calderaVariant": ",".join(level["variants"])})

    heuristic = Sdf.PrimSpec(holder, HEURISTIC_CHILD, Sdf.SpecifierDef, "LODScreenSizeHeuristic")
    set_attribute(heuristic, "lod:domain", Sdf.ValueTypeNames.Token, "imaging", Sdf.VariabilityUniform)
    if extents is not None:
        set_attribute(heuristic, "extent", Sdf.ValueTypeNames.Float3Array, vec3f_array(extents))
    set_attribute(heuristic, "thresholds", Sdf.ValueTypeNames.FloatArray, Vt.FloatArray(THRESHOLDS[: len(levels) - 1]), Sdf.VariabilityUniform)
    return {"levels": len(levels), "instanced": holder is not spec}


def class_holder(spec):
    """A new prim under the layer's class root to hold an instanceable root's LOD structure; named after the root, made unique."""
    layer = spec.layer
    class_root = layer.GetPrimAtPath(Sdf.Path.absoluteRootPath.AppendChild(CLASS_ROOT))
    if class_root is None:
        class_root = Sdf.PrimSpec(layer, CLASS_ROOT, Sdf.SpecifierClass)
    name = spec.name
    suffix = 1
    while name in class_root.nameChildren:
        suffix += 1
        name = f"{spec.name}_{suffix}"
    return Sdf.PrimSpec(class_root, name, Sdf.SpecifierDef, "")


def remove_selection(layer, spec, set_name):
    """Removes a selection authored outside its root, and the over that held only it (and its parents, if they held nothing else)."""
    del spec.variantSelections[set_name]
    while spec and spec.path != Sdf.Path.absoluteRootPath and spec.IsInert():
        parent = spec.nameParent
        if parent:
            del parent.nameChildren[spec.name]
        else:
            del layer.rootPrims[spec.name]
        spec = parent


def convert_layer(path):
    """Converts every LOD variant set one layer authors and removes the selections it authors for them; saves the layer if it changed."""
    layer = Sdf.Layer.FindOrOpen(path)
    roots = []
    selections = []

    def visit(spec):
        defined_here = [name for name in spec.variantSets.keys() if name in LOD_SETS]
        for name in defined_here:
            roots.append((spec, name))
        for name in spec.variantSelections.keys():
            if name in LOD_SETS and name not in defined_here:
                selections.append((spec, name))

    walk_specs(layer.pseudoRoot, visit)
    if not roots and not selections:
        return None
    result = {"path": path, "roots": [], "selections_removed": 0}
    for spec, name in roots:
        outcome = convert_root(spec, name)
        outcome.update({"path": str(spec.path), "set": name})
        result["roots"].append(outcome)
    for spec, name in selections:
        remove_selection(layer, spec, name)
        result["selections_removed"] += 1
    layer.Save()
    return result


def district_outside_opinions(root):
    """Opinions on prims inside a district's content that come from outside its variants.

    Every district is composed with its variant selection set to a variant that doesn't exist, so nothing from the variant set composes;
    whatever children the district still has come from elsewhere: a sibling child authored next to the variant set, or another layer's
    opinion on a prim the content defines. After conversion the content moves under <district>/lod/lod<i>, and such an opinion would no
    longer reach it.
    """
    stage = Usd.Stage.Open(os.path.join(root, "caldera.usda"), Usd.Stage.LoadNone)
    found = []
    for prim in Usd.PrimRange.AllPrims(stage.GetPseudoRoot()):
        if not prim.HasVariantSets() or "districtLod" not in prim.GetVariantSets().GetNames():
            continue
        prim.GetVariantSet("districtLod").SetVariantSelection("__none__")
        children = [child.GetName() for child in prim.GetAllChildren()]
        if children:
            found.append({"path": str(prim.GetPath()), "children": children})
    return found


def summarize(results, outside, seconds):
    roots = [dict(root, layer=result["path"]) for result in results for root in result["roots"]]
    selections = [dict(selection, layer=result["path"]) for result in results for selection in result["selections"]]
    errors = [result for result in results if "error" in result]
    by_set = {}
    for root in roots:
        entry = by_set.setdefault(root["set"], {"roots": 0, "levels": {}, "merged": 0, "skipped": [], "children": 0, "selections": {}})
        entry["roots"] += 1
        unconvertible = [level for level in root["levels"] if len(level["arcs"]) != 1 or level["others"]]
        if unconvertible:
            entry["skipped"].append({"layer": root["layer"], "path": root["path"], "why": [
                {"variant": level["variant"], "arcs": level["arcs"], "others": level["others"]} for level in unconvertible]})
            continue
        levels = merged_levels(root["levels"])
        key = str(len(levels))
        entry["levels"][key] = entry["levels"].get(key, 0) + 1
        entry["merged"] += len(root["levels"]) - len(levels)
        if root["children"]:
            entry["children"] += 1
        selection = root["selection"] or "(none)"
        entry["selections"][selection] = entry["selections"].get(selection, 0) + 1
    outside_selections = {}
    for selection in selections:
        key = selection["set"] + "=" + selection["value"]
        outside_selections[key] = outside_selections.get(key, 0) + 1
    return {
        "seconds": round(seconds, 1),
        "layers_with_changes": len([result for result in results if "roots" in result]),
        "unreadable_layers": [{"path": error["path"], "error": error["error"]} for error in errors],
        "sets": {
            name: dict(entry, skipped_count=len(entry["skipped"]), skipped=entry["skipped"][:50]) for name, entry in by_set.items()
        },
        "selections_outside_their_root": {"count": len(selections), "by_value": outside_selections, "first": selections[:50]},
        "district_outside_opinions": outside,
    }


def run(args, report_path):
    start = time.monotonic()
    files = layer_files(args.root)
    print(f"{len(files)} layers under {args.root}", flush=True)
    with multiprocessing.Pool(args.jobs) as pool:
        results = [result for result in pool.imap_unordered(convert_layer, files, chunksize=64) if result is not None]
    roots = [dict(root, layer=os.path.relpath(result["path"], args.root)) for result in results for root in result["roots"]]
    by_set = {}
    for root in roots:
        entry = by_set.setdefault(root["set"], {"converted": 0, "instanced_through_class": 0, "single_level_payloads": 0, "levels": {}, "skipped": []})
        if "skipped" in root:
            entry["skipped"].append(root)
            continue
        if root["levels"] == 1:
            entry["single_level_payloads"] += 1
        else:
            entry["converted"] += 1
            if root.get("instanced"):
                entry["instanced_through_class"] += 1
        key = str(root["levels"])
        entry["levels"][key] = entry["levels"].get(key, 0) + 1
    report = {
        "seconds": round(time.monotonic() - start, 1),
        "layers_rewritten": len(results),
        "selections_removed": sum(result["selections_removed"] for result in results),
        "sets": by_set,
    }
    with open(report_path, "w") as out:
        json.dump(report, out, indent=2)
    for name, entry in by_set.items():
        print(f"{name}: {entry['converted']} LOD roots ({entry['instanced_through_class']} instanced, through {CLASS_ROOT}), "
              f"{entry['single_level_payloads']} single-level payloads, levels {entry['levels']}, "
              f"skipped {len(entry['skipped'])}")
    print(f"{report['layers_rewritten']} layers rewritten, {report['selections_removed']} selections removed, in {report['seconds']} s")
    print(f"report: {report_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["scan", "run"])
    parser.add_argument("--root", default=os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))
    parser.add_argument("--report", default=None, help="where the JSON report goes (default: report-<command>.json next to this file)")
    parser.add_argument("--jobs", type=int, default=os.cpu_count())
    args = parser.parse_args()
    report_path = args.report or os.path.join(os.path.dirname(os.path.abspath(__file__)), "report-" + args.command + ".json")

    if args.command == "run":
        run(args, report_path)
        return

    start = time.monotonic()
    files = layer_files(args.root)
    print(f"{len(files)} layers under {args.root}", flush=True)
    with multiprocessing.Pool(args.jobs) as pool:
        results = [result for result in pool.imap_unordered(scan_layer, files, chunksize=64) if result is not None]
    print(f"scanned in {time.monotonic() - start:.0f} s; checking the districts for outside opinions", flush=True)
    outside = district_outside_opinions(args.root)
    for result in results:
        result["path"] = os.path.relpath(result["path"], args.root)
    report = summarize(results, outside, time.monotonic() - start)
    with open(report_path, "w") as out:
        json.dump(report, out, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != "sets"}, indent=2)[:4000])
    for name, entry in report["sets"].items():
        print(f"{name}: {entry['roots']} roots, levels {entry['levels']}, merged {entry['merged']}, skipped {entry['skipped_count']}, "
              f"with other children {entry['children']}, own selections {entry['selections']}")
    print(f"report: {report_path}")


if __name__ == "__main__":
    main()
