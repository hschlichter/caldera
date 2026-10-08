#!/usr/bin/env python3
"""Check that the UsdLod conversion composes to the same geometry as the original, level for level.

    verify.py before    fingerprint the original data (run on the unconverted branch, before convert.py run)
    verify.py after     fingerprint the converted data and compare; check that no LOD variant set is left and the roots are valid

A fingerprint is a composed stage's prims, each as (path, type, point count), with the conversion's own prims (lod, lod<i>, lodHeuristic)
left out and their path segments removed, so the original and the converted name the same prim the same way. Three cases:

    default     caldera.usda as it opens: every district at its coarsest level, everything else loaded
    capital     map_capital at its finest level, every model in it at its finest level
    models      a sample of multi-level model assets, each at every level

Fingerprints are written to fingerprints/ next to this file (ignored by git); a mismatch prints the first differing lines.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time

from pxr import Sdf, Usd, UsdGeom, UsdLod

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
FINGERPRINTS = os.path.join(HERE, "fingerprints")

CAPITAL = "/world/mp_wz_island/mp_wz_island_paths/mp_wz_island_geo/map_capital"
GEO = "/world/mp_wz_island/mp_wz_island_paths/mp_wz_island_geo"
MODEL_SAMPLE = 200

STRUCTURAL = {"lod", "lodHeuristic"}
LEVEL_SEGMENT = re.compile(r"/lod/lod(\d+)(?=/|$)")


def normalize(path):
    """The original's name for a converted prim: the lod/lod<i> segments removed."""
    return LEVEL_SEGMENT.sub("", path)


def deeper_level(path):
    """True when the path is inside a level other than lod0 of some root: not part of a finest-level fingerprint."""
    return any(int(level) > 0 for level in LEVEL_SEGMENT.findall(path))


def fingerprint(stage, root_path, keep):
    """(path, type, points) per prim under root_path, instance proxies included, for the prims keep() accepts."""
    lines = []
    root = stage.GetPrimAtPath(root_path)
    iterator = iter(Usd.PrimRange(root, Usd.TraverseInstanceProxies(Usd.PrimAllPrimsPredicate)))
    for prim in iterator:
        path = str(prim.GetPath())
        if not keep(path):
            iterator.PruneChildren()
            continue
        if prim.GetName() in STRUCTURAL or re.fullmatch(r"lod\d+", prim.GetName()) and prim.GetParent().GetName() == "lod":
            continue
        points = 0
        if prim.IsA(UsdGeom.PointBased):
            value = UsdGeom.PointBased(prim).GetPointsAttr().Get()
            points = len(value) if value is not None else 0
        lines.append(f"{normalize(path)}\t{prim.GetTypeName()}\t{points}")
    lines.sort()
    return lines


def instancing(stage, root_path):
    """Instances under root_path and the stage's prototypes: what the conversion must keep."""
    instances = sum(1 for prim in Usd.PrimRange(stage.GetPrimAtPath(root_path)) if prim.IsInstance())
    return {"instances": instances, "prototypes": len(stage.GetPrototypes())}


def summary(lines):
    meshes = sum(1 for line in lines if line.split("\t")[1] == "Mesh")
    points = sum(int(line.split("\t")[2]) for line in lines)
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
    return {"prims": len(lines), "meshes": meshes, "points": points, "sha256": digest}


def save(name, lines):
    os.makedirs(FINGERPRINTS, exist_ok=True)
    with gzip.open(os.path.join(FINGERPRINTS, name + ".txt.gz"), "wt") as out:
        out.write("\n".join(lines))


def load(name):
    with gzip.open(os.path.join(FINGERPRINTS, name + ".txt.gz"), "rt") as source:
        text = source.read()
    return text.split("\n") if text else []


def session_stage(path, load_set):
    """A stage whose edits (variant selections) go to the session layer, never to a file."""
    stage = Usd.Stage.Open(path, load_set)
    stage.SetEditTarget(stage.GetSessionLayer())
    return stage


def model_sample():
    """Every Nth multi-level model asset, by path, so before and after pick the same ones."""
    candidates = []
    for directory, subdirectories, names in os.walk(os.path.join(ROOT, "assets", "xmodel")):
        subdirectories.sort()
        for name in sorted(names):
            if name.endswith(".gdt.usd"):
                candidates.append(os.path.join(directory, name))
    return candidates


def original_model_levels(path):
    """For an unconverted model asset: its root's path and its modelLod variants, finest first; None if it has fewer than two."""
    layer = Sdf.Layer.FindOrOpen(path)
    pending = list(layer.rootPrims)
    while pending:
        spec = pending.pop(0)
        if "modelLod" in spec.variantSets.keys():
            names = sorted(variant.name for variant in spec.variantSets["modelLod"].variants)
            return (str(spec.path), names) if len(names) >= 2 else None
        pending.extend(spec.nameChildren)
    return None


# ---- before


def before():
    started = time.monotonic()
    results = {}

    stage = Usd.Stage.Open(os.path.join(ROOT, "caldera.usda"), Usd.Stage.LoadAll)
    lines = fingerprint(stage, "/", lambda path: True)
    save("default", lines)
    results["default"] = dict(summary(lines), **instancing(stage, "/"))
    print("default", results["default"], flush=True)

    stage = session_stage(os.path.join(ROOT, "caldera.usda"), Usd.Stage.LoadNone)
    stage.GetPrimAtPath(CAPITAL).GetVariantSet("districtLod").SetVariantSelection("full")
    stage.Load(CAPITAL)
    lines = fingerprint(stage, CAPITAL, lambda path: True)
    save("capital", lines)
    results["capital"] = dict(summary(lines), **instancing(stage, CAPITAL))
    print("capital", results["capital"], flush=True)

    models = []
    for path in model_sample():
        levels = original_model_levels(path)
        if levels:
            models.append((path, levels))
    step = max(1, len(models) // MODEL_SAMPLE)
    models = models[::step][:MODEL_SAMPLE]
    results["models"] = []
    for path, (root_path, variants) in models:
        entry = {"path": os.path.relpath(path, ROOT), "root": root_path, "variants": {}}
        for variant in variants:
            stage = session_stage(path, Usd.Stage.LoadAll)
            stage.GetPrimAtPath(root_path).GetVariantSet("modelLod").SetVariantSelection(variant)
            lines = fingerprint(stage, root_path, lambda p: True)
            entry["variants"][variant] = {"summary": summary(lines), "lines": lines}
        results["models"].append(entry)
    print(f"models: {len(results['models'])} sampled", flush=True)

    with open(os.path.join(FINGERPRINTS, "before.json"), "w") as out:
        json.dump(results, out, indent=1)
    print(f"done in {time.monotonic() - started:.0f} s")


# ---- after


def lod_roots(stage, root_path):
    """Paths of the converted roots (prims whose lod child has LODRootAPI) under root_path, instance proxies included."""
    roots = []
    for prim in Usd.PrimRange(stage.GetPrimAtPath(root_path), Usd.TraverseInstanceProxies(Usd.PrimAllPrimsPredicate)):
        if prim.GetName() == "lod" and prim.HasAPI(UsdLod.RootAPI):
            roots.append(prim.GetParent().GetPath())
    return roots


def compare_instancing(name, before_result, actual):
    """Instances must match exactly; prototypes may only grow by the levels now loaded side by side (each level is its own payload)."""
    same = actual["instances"] == before_result["instances"]
    print(f"{'same' if same else 'DIFFERENT'}  {name} instancing: before {before_result['instances']} instances, "
          f"{before_result['prototypes']} prototypes; after {actual['instances']} instances, {actual['prototypes']} prototypes", flush=True)
    return same


def compare(name, expected_lines, actual_lines):
    expected = summary(expected_lines)
    actual = summary(actual_lines)
    same = expected["sha256"] == actual["sha256"]
    print(f"{'same' if same else 'DIFFERENT'}  {name}: before {expected}  after {actual}", flush=True)
    if not same:
        missing = sorted(set(expected_lines) - set(actual_lines))[:10]
        extra = sorted(set(actual_lines) - set(expected_lines))[:10]
        print("  only before:", *missing, sep="\n    ")
        print("  only after:", *extra, sep="\n    ")
    return same


def check_roots():
    """No LOD variant set or selection left; every root valid: LODRootAPI, a heuristic found through lod:heuristics, thresholds = levels - 1."""
    problems = []
    roots = 0
    for directory, subdirectories, names in os.walk(ROOT):
        subdirectories[:] = sorted(d for d in subdirectories if not d.startswith(".") and d != "tools")
        for name in sorted(names):
            if not name.endswith((".usd", ".usda", ".usdc")):
                continue
            layer = Sdf.Layer.FindOrOpen(os.path.join(directory, name))
            specs = []
            layer.Traverse(layer.pseudoRoot.path, lambda path: specs.append(path) if path.IsPrimPath() or path.IsPrimVariantSelectionPath() else None)
            for path in specs:
                spec = layer.GetPrimAtPath(path)
                if spec is None:
                    continue
                for set_name in ("districtLod", "modelLod"):
                    if set_name in spec.variantSets.keys() or set_name in spec.variantSelections.keys():
                        problems.append(f"{layer.identifier}: {path} still has {set_name}")
                if spec.name != "lod" or "LODRootAPI" not in str(spec.GetInfo("apiSchemas")):
                    continue
                roots += 1
                levels = [child for child in spec.nameChildren]
                heuristic = layer.GetPrimAtPath(spec.path.GetParentPath().AppendChild("lodHeuristic"))
                targets = spec.relationships["lod:heuristics"].targetPathList.GetAddedOrExplicitItems() if "lod:heuristics" in spec.relationships else []
                if heuristic is None or heuristic.typeName != "LODScreenSizeHeuristic" or [str(t) for t in targets] != [str(heuristic.path)]:
                    problems.append(f"{layer.identifier}: {path} has no heuristic through lod:heuristics")
                    continue
                thresholds = heuristic.attributes["thresholds"].default if "thresholds" in heuristic.attributes else []
                if len(thresholds) != len(levels) - 1:
                    problems.append(f"{layer.identifier}: {path} has {len(levels)} levels and {len(thresholds)} thresholds")
    return roots, problems


def after():
    started = time.monotonic()
    with open(os.path.join(FINGERPRINTS, "before.json")) as source:
        before_results = json.load(source)
    ok = True

    # Default: every district at its coarsest level, the rest loaded.
    stage = Usd.Stage.Open(os.path.join(ROOT, "caldera.usda"), Usd.Stage.LoadNone)
    rules = Usd.StageLoadRules.LoadNone()
    rules.AddRule("/", Usd.StageLoadRules.AllRule)
    probe = Usd.Stage.Open(os.path.join(ROOT, "caldera.usda"), Usd.Stage.LoadNone)
    for district in lod_roots(probe, GEO):
        levels = probe.GetPrimAtPath(district.AppendChild("lod")).GetAllChildren()
        for level in levels[:-1]:
            rules.AddRule(level.GetPath(), Usd.StageLoadRules.NoneRule)
    stage.SetLoadRules(rules)
    coarsest = {str(district): len(probe.GetPrimAtPath(district.AppendChild("lod")).GetAllChildren()) - 1 for district in lod_roots(probe, GEO)}

    def as_opened(path):
        """True for the levels the original opened at: a district's coarsest, any other root's finest."""
        for match in LEVEL_SEGMENT.finditer(path):
            root = path[: match.start()]
            wanted = coarsest.get(root, 0)
            if int(match.group(1)) != wanted:
                return False
        return True

    lines = fingerprint(stage, "/", as_opened)
    ok &= compare("default", load("default"), lines)
    ok &= compare_instancing("default", before_results["default"], instancing(stage, "/"))

    # Capital at its finest level, models at their finest: levels other than lod0 are pruned.
    stage = Usd.Stage.Open(os.path.join(ROOT, "caldera.usda"), Usd.Stage.LoadNone)
    stage.Load(CAPITAL + "/lod/lod0")
    lines = fingerprint(stage, CAPITAL, lambda path: not deeper_level(path))
    ok &= compare("capital", load("capital"), lines)
    ok &= compare_instancing("capital", before_results["capital"], instancing(stage, CAPITAL))

    # Models: each original variant against the converted level it was merged into.
    mismatches = 0
    for entry in before_results["models"]:
        path = os.path.join(ROOT, entry["path"])
        root_path = entry["root"]
        probe = Usd.Stage.Open(path, Usd.Stage.LoadNone)
        levels = probe.GetPrimAtPath(root_path + "/lod").GetAllChildren() if probe.GetPrimAtPath(root_path + "/lod") else []
        for variant, expected in entry["variants"].items():
            if not levels:
                # Every variant named the same file: merged into one level, the payload is on the root itself (no LOD root).
                stage = Usd.Stage.Open(path, Usd.Stage.LoadAll)
                lines = fingerprint(stage, root_path, lambda p: True)
                if summary(lines)["sha256"] != expected["summary"]["sha256"]:
                    mismatches += 1
                    compare(f"{entry['path']} {variant}", expected["lines"], lines)
                continue
            level = next((l for l in levels if variant in l.GetCustomDataByKey("calderaVariant").split(",")), None)
            if level is None:
                print(f"DIFFERENT  {entry['path']} {variant}: no converted level names it")
                mismatches += 1
                continue
            stage = Usd.Stage.Open(path, Usd.Stage.LoadNone)
            stage.Load(level.GetPath())
            lines = fingerprint(stage, root_path, lambda p, keep=str(level.GetPath()): not LEVEL_SEGMENT.search(p) or p.startswith(keep))
            if summary(lines)["sha256"] != expected["summary"]["sha256"]:
                mismatches += 1
                compare(f"{entry['path']} {variant}", expected["lines"], lines)
    print(f"{'same' if mismatches == 0 else 'DIFFERENT'}  models: {len(before_results['models'])} sampled, {mismatches} levels differ", flush=True)
    ok &= mismatches == 0

    roots, problems = check_roots()
    print(f"{'ok' if not problems else 'PROBLEMS'}  {roots} LOD roots; {len(problems)} problems", flush=True)
    for problem in problems[:20]:
        print("  " + problem)
    ok &= not problems

    print(f"done in {time.monotonic() - started:.0f} s: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["before", "after"])
    args = parser.parse_args()
    if args.command == "before":
        before()
        return 0
    return after()


if __name__ == "__main__":
    sys.exit(main())
