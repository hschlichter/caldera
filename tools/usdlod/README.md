# Caldera in the UsdLod schema

Caldera expresses level of detail with two variant sets of its own: `districtLod` (`full`, `proxy`) on the districts and terrain tiles, and
`modelLod` (`lod0` … `lod3`) on the model assets. This branch rewrites both into the [UsdLod schema](https://openusd.org/release/api/usd_lod_page_front.html)
(OpenUSD 26.08), so a consumer that knows UsdLod needs nothing Caldera-specific. `convert.py` does the rewrite; `verify.py` checks that the
result composes to the same geometry, level for level.

## What changes

Each variant held one arc (a reference or a payload to that level's file) and an `extentsHint`. A root with more than one level becomes:

```usda
def Xform "map_capital"
{
    float3[] extentsHint = [...]

    def "lod" (prepend apiSchemas = ["LODRootAPI"])
    {
        int lod:default:index = 1                        # the coarsest level
        rel lod:heuristics = <../lodHeuristic>

        def "lod0" (customData = {string calderaVariant = "full"}  prepend payload = @...map_capital.usd@</world/map_capital>) {}
        def "lod1" (customData = {string calderaVariant = "proxy"} prepend payload = @...map_capital_proxy.gdt.usd@) {}
    }

    def LODScreenSizeHeuristic "lodHeuristic"
    {
        float3[] extent = [...]                          # from the extentsHint
        uniform token lod:domain = "imaging"
        uniform float[] thresholds = [0.25]              # [0.25, 0.125, 0.0625], one fewer than the levels
    }
}
```

- **Levels**, finest first: `full`, `proxy`; `lod0` … `lod3`. Consecutive variants pointing at the same file merge into one level (`calderaVariant`
  lists them); gaps (`lod0`, `lod3`) just give fewer levels.
- **Every level is a payload**, including `full`, which was a reference. All children of a UsdLod root are composed, so a reference would
  compose every district in full on open; as payloads, only the levels a consumer loads are composed.
- **The root is a child, `lod`**, because every child of a UsdLod root is a level, and the prims have other children (`mtl`). The heuristic is a
  sibling of `lod` for the same reason.
- **Instanceable models keep their instancing.** A prim is only an instance if it has a composition arc of its own, and the variant set was
  that arc. For an instanceable model the structure above is authored on `</_usdlod/<model>>`, a `class` prim in the same layer, and the model
  references it. The composed paths are the same: `<model>/lod/lod<i>`, `<model>/lodHeuristic`.
- **One level, no LOD root.** A model whose variants merge into one level gets that level's payload on the prim itself, as it composed before.
- The variant selections authored outside the roots (`caldera.usda`'s `districtLod = proxy` on every district) are removed, and with them the
  `over`s that held nothing else. `caldera.usda` opens the same way as before: every district at its coarsest level.

The thresholds are a starting point, not tuned against what a renderer draws.

## Numbers

| | |
|---|---|
| Layers rewritten | 8,076 of 63,668 |
| District and terrain-tile roots | 45, two levels each |
| Model roots | 2,563 (2,315 instanceable, through `_usdlod`): 1,729 with two levels, 458 with three, 376 with four |
| Models that became a single payload | 5,510 |
| Duplicate levels merged | 263 |
| Run time | about 8 s on 24 cores |

## Running it

```sh
python -m venv tools/usdlod/.venv
tools/usdlod/.venv/bin/pip install usd-core==26.8

tools/usdlod/.venv/bin/python tools/usdlod/convert.py scan      # report what would change (report-scan.json); changes nothing
tools/usdlod/.venv/bin/python tools/usdlod/verify.py before     # fingerprint the original (fingerprints/, ignored by git)
tools/usdlod/.venv/bin/python tools/usdlod/convert.py run       # rewrite the layers in place (report-run.json)
tools/usdlod/.venv/bin/python tools/usdlod/verify.py after      # compare against the fingerprints; exit 0 on PASS
```

`verify.py before` must run on the unconverted data (`main`), `after` on the converted. The converter is idempotent: on converted data it finds
nothing to do. Composing `map_capital` in full is the heavy part of `verify.py`: it ran within a 24 GB address-space limit (`ulimit -v`).

## What `verify.py` checks

- **The default map** (`caldera.usda`, every district at its coarsest level, models at their finest): the same prims, meshes and points, path
  for path, and the same instances and prototypes (85, 57).
- **`map_capital` in full**, every model at its finest: the same 1,935,073 prims, 596,867 meshes and 141,852,999 points (instance proxies
  included), and the same 139,137 instances. Prototypes drop from 2,815 to 2,675: different model assets that point at the same geometry files
  (a light's `off`, `on_cool` and `on_warm` assets) now share a prototype, since their instances compose the same payload; before, each asset's
  variant bodies made its prototype distinct.
- **200 sampled multi-level models**, every original variant against the converted level it was merged into: the same prims and points.
- **The roots**: no `districtLod` or `modelLod` variant set or selection is left; every `lod` prim has `LODRootAPI`, a `LODScreenSizeHeuristic`
  reached through `lod:heuristics`, and one threshold fewer than its levels.

Fingerprints are each prim's path (with the `lod/lod<i>` segments removed), type and point count; the conversion's own prims are left out.
