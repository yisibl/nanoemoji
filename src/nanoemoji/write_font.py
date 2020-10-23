# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Writes UFO and/or font files."""


from absl import app
from absl import flags
from absl import logging
from collections import defaultdict
import csv
from fontTools import ttLib
from itertools import chain
from lxml import etree  # pytype: disable=import-error
from nanoemoji import codepoints
from nanoemoji.colors import Color
from nanoemoji.color_glyph import ColorGlyph, PaintedLayer
from nanoemoji.glyph import glyph_name
from nanoemoji.paint import (
    CompositeMode, Paint, PaintComposite, PaintColrGlyph, PaintGlyph, PaintSolid
)
from nanoemoji.svg import make_svg_table
from nanoemoji.svg_path import draw_svg_path
from nanoemoji import util
import os
import ufoLib2
from picosvg.svg import SVG
from picosvg.svg_transform import Affine2D
from picosvg.svg_types import SVGPath
import regex
import sys
from typing import (
    Any,
    Callable,
    Generator,
    Iterable,
    Mapping,
    NamedTuple,
    Sequence,
    Tuple,
)
from ufoLib2.objects import Component, Glyph
import ufo2ft


FLAGS = flags.FLAGS


class ColorFontConfig(NamedTuple):
    upem: int
    family: str
    color_format: str
    fea_file: str
    output_format: str
    keep_glyph_names: bool = False


class InputGlyph(NamedTuple):
    filename: str
    codepoints: Tuple[int, ...]
    picosvg: SVG


class ColorGlyphSubset(NamedTuple):
    color_glyph_idx: int
    layer_start: int
    layer_end: int


# A color font generator.
#   apply_ufo(ufo, color_glyphs) is called first, to update a generated UFO
#   apply_ttfont(ufo, color_glyphs, ttfont) is called second, to allow fixups after ufo2ft
# Ideally we delete the ttfont stp in future. Blocking issues:
#   https://github.com/unified-font-object/ufo-spec/issues/104
# If the output file is .ufo then apply_ttfont is not called.
# Where possible code to the ufo and let apply_ttfont be a nop.
class ColorGenerator(NamedTuple):
    apply_ufo: Callable[[ufoLib2.Font, Sequence[ColorGlyph]], None]
    apply_ttfont: Callable[[ufoLib2.Font, Sequence[ColorGlyph], ttLib.TTFont], None]
    font_ext: str  # extension for font binary, .ttf or .otf


_COLOR_FORMAT_GENERATORS = {
    "glyf": ColorGenerator(lambda *args: _glyf_ufo(*args), lambda *_: None, ".ttf"),
    "glyf_colr_0": ColorGenerator(
        lambda *args: _colr_ufo(0, *args), lambda *_: None, ".ttf"
    ),
    "glyf_colr_1": ColorGenerator(
        lambda *args: _colr_ufo(1, *args), lambda *_: None, ".ttf"
    ),
    "cff_colr_0": ColorGenerator(
        lambda *args: _colr_ufo(0, *args), lambda *_: None, ".otf"
    ),
    "cff_colr_1": ColorGenerator(
        lambda *args: _colr_ufo(1, *args), lambda *_: None, ".otf"
    ),
    "cff2_colr_0": ColorGenerator(
        lambda *args: _colr_ufo(0, *args), lambda *_: None, ".otf"
    ),
    "cff2_colr_1": ColorGenerator(
        lambda *args: _colr_ufo(1, *args), lambda *_: None, ".otf"
    ),
    "picosvg": ColorGenerator(
        lambda *_: None,
        lambda *args: _svg_ttfont(*args, picosvg=True, compressed=False),
        ".ttf",
    ),
    "picosvgz": ColorGenerator(
        lambda *_: None,
        lambda *args: _svg_ttfont(*args, picosvg=True, compressed=True),
        ".ttf",
    ),
    "untouchedsvg": ColorGenerator(
        lambda *_: None,
        lambda *args: _svg_ttfont(*args, picosvg=False, compressed=False),
        ".ttf",
    ),
    "untouchedsvgz": ColorGenerator(
        lambda *_: None,
        lambda *args: _svg_ttfont(*args, picosvg=False, compressed=True),
        ".ttf",
    ),
    "cbdt": ColorGenerator(
        lambda *args: _not_impl("ufo", *args),
        lambda *args: _not_impl("TTFont", *args),
        ".ttf",
    ),
    "sbix": ColorGenerator(
        lambda *args: _not_impl("ufo", *args),
        lambda *args: _not_impl("TTFont", *args),
        ".ttf",
    ),
}


# TODO move to config file?
# TODO flag on/off shape reuse
flags.DEFINE_integer("upem", 1024, "Units per em.")
flags.DEFINE_string("family", "An Emoji Family", "Family name.")
flags.DEFINE_string("output_file", None, "Output filename.")
flags.DEFINE_enum(
    "color_format",
    "glyf_colr_1",
    sorted(_COLOR_FORMAT_GENERATORS.keys()),
    "Type of font to generate.",
)
flags.DEFINE_enum(
    "output", "font", ["ufo", "font"], "Whether to output a font binary or a UFO"
)
flags.DEFINE_bool(
    "keep_glyph_names", False, "Whether or not to store glyph names in the font."
)


def _ufo(family: str, upem: int, keep_glyph_names: bool = False) -> ufoLib2.Font:
    ufo = ufoLib2.Font()
    ufo.info.familyName = family
    # set various font metadata; see the full list of fontinfo attributes at
    # http://unifiedfontobject.org/versions/ufo3/fontinfo.plist/
    ufo.info.unitsPerEm = upem

    # Must have .notdef and Win 10 Chrome likes a blank gid1 so make gid1 space
    ufo.newGlyph(".notdef")
    space = ufo.newGlyph(".space")
    space.unicodes = [0x0020]
    space.width = upem
    ufo.glyphOrder = [".notdef", ".space"]

    # use 'post' format 3.0 for TTFs, shaving a kew KBs of unneeded glyph names
    ufo.lib[ufo2ft.constants.KEEP_GLYPH_NAMES] = keep_glyph_names

    return ufo


def _make_ttfont(config, ufo, color_glyphs):
    if config.output_format == ".ufo":
        return None

    # Use skia-pathops to remove overlaps (i.e. simplify self-overlapping
    # paths) because the default ("booleanOperations") does not support
    # quadratic bezier curves (qcurve), which may appear
    # when we pass through picosvg (e.g. arcs or stroked paths).
    ttfont = None
    if config.output_format == ".ttf":
        ttfont = ufo2ft.compileTTF(ufo, overlapsBackend="pathops")
    if config.output_format == ".otf":
        cff_version = 1
        if config.color_format.startswith("cff2_"):
            cff_version = 2
        ttfont = ufo2ft.compileOTF(
            ufo, cffVersion=cff_version, overlapsBackend="pathops"
        )

    if not ttfont:
        raise ValueError(
            f"Unable to generate {config.color_format} {config.output_format}"
        )

    # Permit fixups where we can't express something adequately in UFO
    _COLOR_FORMAT_GENERATORS[config.color_format].apply_ttfont(
        ufo, color_glyphs, ttfont
    )

    return ttfont


def _write(ufo, ttfont, output_file):
    logging.info("Writing %s", output_file)

    if os.path.splitext(output_file)[1] == ".ufo":
        ufo.save(output_file, overwrite=True)
    else:
        ttfont.save(output_file)


def _not_impl(*_):
    raise NotImplementedError("%s not implemented" % FLAGS.color_format)


def _next_name(ufo: ufoLib2.Font, name_fn) -> str:
    i = 0
    while name_fn(i) in ufo:
        i += 1
    return name_fn(i)


def _create_glyph(color_glyph: ColorGlyph, painted_layer: PaintedLayer) -> Glyph:
    ufo = color_glyph.ufo

    glyph = ufo.newGlyph(_next_name(ufo, lambda i: f"{color_glyph.glyph_name}.{i}"))
    glyph_names = [glyph.name]
    glyph.width = ufo.info.unitsPerEm

    svg_units_to_font_units = color_glyph.transform_for_font_space()

    if painted_layer.reuses:
        # Shape repeats, form a composite
        base_glyph = ufo.newGlyph(
            _next_name(ufo, lambda i: f"{glyph.name}.component.{i}")
        )
        glyph_names.append(base_glyph.name)

        draw_svg_path(SVGPath(d=painted_layer.path), base_glyph.getPen(), svg_units_to_font_units)

        glyph.components.append(
            Component(baseGlyph=base_glyph.name, transformation=Affine2D.identity())
        )

        for transform in painted_layer.reuses:
            # We already redrew the component into font space, don't redo it
            # scale x/y translation and flip y movement to match font space
            transform = transform._replace(
                e=transform.e * svg_units_to_font_units.a,
                f=transform.f * svg_units_to_font_units.d,
            )
            glyph.components.append(
                Component(baseGlyph=base_glyph.name, transformation=transform)
            )
    else:
        # Not a composite, just draw directly on the glyph
        draw_svg_path(SVGPath(d=painted_layer.path), glyph.getPen(), svg_units_to_font_units)

    ufo.glyphOrder += glyph_names

    return glyph


def _draw_glyph_extents(ufo: ufoLib2.Font, glyph: Glyph):
    # apparently on Mac (but not Linux) Chrome and Firefox end up relying on the
    # extents of the base layer to determine where the glyph might paint. If you
    # leave the base blank the COLR glyph never renders.

    # TODO we could narrow this to bbox to cover all layers

    pen = glyph.getPen()
    pen.moveTo((0, 0))
    pen.lineTo((ufo.info.unitsPerEm, ufo.info.unitsPerEm))
    pen.endPath()

    return glyph


def _glyf_ufo(ufo, color_glyphs):
    # glyphs by reuse_key
    glyphs = {}
    reused = set()
    for color_glyph in color_glyphs:
        logging.debug(
            "%s %s %s",
            ufo.info.familyName,
            color_glyph.glyph_name,
            color_glyph.transform_for_font_space(),
        )
        parent_glyph = ufo[color_glyph.glyph_name]
        for painted_layer in color_glyph.as_painted_layers():
            # if we've seen this shape before reuse it
            reuse_key = _inter_glyph_reuse_key(painted_layer)
            if reuse_key not in glyphs:
                glyph = _create_glyph(color_glyph, painted_layer)
                glyphs[reuse_key] = glyph
            else:
                glyph = glyphs[reuse_key]
                reused.add(glyph.name)
            parent_glyph.components.append(Component(baseGlyph=glyph.name))

    for color_glyph in color_glyphs:
        parent_glyph = ufo[color_glyph.glyph_name]
        # No great reason to keep single-component glyphs around (unless reused)
        if (
            len(parent_glyph.components) == 1
            and parent_glyph.components[0].baseGlyph not in reused
        ):
            component = ufo[parent_glyph.components[0].baseGlyph]
            del ufo[component.name]
            component.unicode = parent_glyph.unicode
            ufo[color_glyph.glyph_name] = component
            assert component.name == color_glyph.glyph_name


def _colr_layer(colr_version: int, layer_glyph_name: str, paint: Paint, palette: Sequence[Color]):
    # For COLRv0, paint is just the palette index
    # For COLRv1, it's a data structure describing paint
    if colr_version == 0:
        # COLRv0: draw using the first available color on the glyph_layer
        # Results for gradients will be suboptimal :)
        color = next(paint.colors())
        return (layer_glyph_name, palette.index(color))

    elif colr_version == 1:
        # COLRv1: layer is graph of (solid, gradients, etc.) paints.
        # Root node is always a PaintGlyph (format 4) for now.
        # TODO Support more paints (PaintTransform, PaintColorGlyph, PaintComposite)
        return PaintGlyph(layer_glyph_name, paint).to_ufo_paint(palette)

    else:
        raise ValueError(f"Unsupported COLR version: {colr_version}")


def _inter_glyph_reuse_key(painted_layer: PaintedLayer) -> PaintedLayer:
    """Individual glyf entries, including composites, can be reused.

    COLR lets us reuse the shape regardless of paint so paint is not part of key."""
    return painted_layer._replace(paint=PaintSolid())


def _extract_colr_glyphs(color_glyphs: Sequence[ColorGlyph]) -> Iterable[Tuple[ColorGlyphSubset]]:
    layers = tuple(c.painted_layers for c in color_glyphs)
    def _key(glyph_idx, lbound, ubound): 
        return tuple(layers[glyph_idx][lbound:ubound])

    # count # of instances for every sequence of layers
    layer_seqs = defaultdict(set)
    for glyph_idx, painted_layers in enumerate(layers):
        num_layers = len(layers[glyph_idx])
        for lbound in range(num_layers):
            for ubound in range(lbound + 1, num_layers):
                layer_seqs[_key(glyph_idx, lbound, ubound)].add((glyph_idx, (lbound, ubound)))

    # drop single-instance layers.
    # Sort by #layers then #occurences, higher counts [higher value reuse] first
    layer_seqs = sorted(((k, v) for k, v in layer_seqs.items() if len(v) > 1),
                        key=lambda t: tuple(len(v) for v in t),
                        reverse=True)

    # find reused sequences of layers
    reuse_groups = defaultdict(list)
    consumed = defaultdict(set)
    for _, instances in layer_seqs:
        for glyph_idx, (lbound, ubound) in instances:
            # if we already reuse layer in a higher priority sequence
            # drop it out of this one
            while lbound in consumed[glyph_idx] and lbound < ubound:
                lbound += 1
            while ubound in consumed[glyph_idx] and lbound < ubound:
                ubound -= 1
            if lbound >= ubound:
                continue
            consumed[glyph_idx].update(range(lbound, ubound))
            reuse_groups[_key(glyph_idx, lbound, ubound)].append(ColorGlyphSubset(glyph_idx, lbound, ubound))

    return tuple(tuple(s) for s in reuse_groups.values() if len(s) > 1)


def _ufo_colr_layers(colr_version, colors, color_glyph, colr_insertions, glyph_cache):
    # The value for a COLOR_LAYERS_KEY entry per
    # https://github.com/googlefonts/ufo2ft/pull/359
    colr_layers = []

    # accumulate layers in z-order
    layer_idx = 0
    while layer_idx < len(color_glyph.painted_layers):
        end_idx, colr_glyph_name = colr_insertions.get((color_glyph.glyph_name, layer_idx), (-1, -1))
        if end_idx != -1:
            logging.debug("Insert %s for %s layers %d..%d",
                colr_glyph_name, color_glyph.glyph_name, layer_idx, end_idx)
            layer_idx = end_idx
            layer = PaintColrGlyph(glyph=colr_glyph_name).to_ufo_paint(colors)
        else:
            painted_layer = color_glyph.painted_layers[layer_idx]
            layer_idx += 1

            # if we've seen this shape before reuse it
            glyph_cache_key = _inter_glyph_reuse_key(painted_layer)
            if glyph_cache_key not in glyph_cache:
                glyph = _create_glyph(color_glyph, painted_layer)
                glyph_cache[glyph_cache_key] = glyph
            else:
                glyph = glyph_cache[glyph_cache_key]

            layer = _colr_layer(colr_version, glyph.name, painted_layer.paint, colors)

        colr_layers.append(layer)

    if colr_version > 0 and len(colr_layers) > MAX_LAYER_V1_COUNT:
        logging.info(
            "%s contains > {MAX_LAYER_V1_COUNT} layers (%s); "
            "merging last layers in PaintComposite tree",
            color_glyph.glyph_name,
            len(colr_layers),
        )

        i = MAX_LAYER_V1_COUNT - 1
        colr_layers[i:] = [_build_composite_layer(colr_layers[i:])]

    return colr_layers


def _build_composite_layer(layers: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Construct PaintComposite binary tree from list of UFO color layers.

    Layers are in z-order, from bottom to top layer. We use SRC_OVER compositing
    mode to paint "source" over "backdrop". The binary tree is balanced to keep
    its depth short and limit the risk of RecursionError.
    """
    assert layers

    if len(layers) == 1:
        return layers[0]

    mid = len(layers) // 2
    return dict(
        format=PaintComposite.format,
        mode=CompositeMode.SRC_OVER.name.lower(),
        backdrop=_build_composite_layer(layers[:mid]),
        source=_build_composite_layer(layers[mid:]),
    )


MAX_LAYER_V1_COUNT = 255


def _colr_ufo(colr_version, ufo, color_glyphs):
    # Sort colors so the index into colors == index into CPAL palette.
    # We only store opaque colors in CPAL for CORLv1, as 'alpha' is
    # encoded separately.
    colors = sorted(
        set(
            c if colr_version == 0 else c.opaque()
            for c in chain.from_iterable(g.colors() for g in color_glyphs)
        )
    )
    logging.debug("colors %s", colors)

    # KISS; use a single global palette
    ufo.lib[ufo2ft.constants.COLOR_PALETTES_KEY] = [[c.to_ufo_color() for c in colors]]

    # each base glyph maps to a list of (glyph name, paint info) in z-order
    ufo_color_layers = {}

    # potentially reusable glyphs
    glyph_cache = {}

    # examine layers to hoist out repeated layers to COLR glyphs
    colr_insertions = {}
    if colr_version > 0:
        for idx, instances in enumerate(_extract_colr_glyphs(color_glyphs)):
            color_glyph = color_glyphs[instances[0].color_glyph_idx]
            new_id = f"colr.{idx}"
            logging.debug("Extracting %s for %d instances of %s", new_id, len(instances), instances[0])

            new_color_glyph = color_glyph._replace(
                filename=f"{color_glyph.filename}.{new_id}",
                glyph_name = new_id,
                glyph_id = len(ufo.glyphOrder),
                codepoints = (),
                painted_layers = color_glyph.painted_layers[instances[0].layer_start:instances[0].layer_end]
            )
            ufo_color_layers[new_id] = _ufo_colr_layers(colr_version, colors, new_color_glyph, colr_insertions, glyph_cache)
            ufo.newGlyph(new_id)
            ufo.glyphOrder += [new_id]
            for color_glyph_idx, layer_start, layer_end in instances:
                colr_insertions[(color_glyphs[color_glyph_idx].glyph_name, layer_start)] = (layer_end, new_id)

    # write glyphs using the hoisted COLR glyphs
    for color_glyph in color_glyphs:
        logging.debug(
            "%s %s %s",
            ufo.info.familyName,
            color_glyph.glyph_name,
            color_glyph.transform_for_font_space(),
        )

        ufo_color_layers[color_glyph.glyph_name] = _ufo_colr_layers(colr_version, colors, color_glyph, colr_insertions, glyph_cache)

        colr_glyph = ufo.get(color_glyph.glyph_name)
        _draw_glyph_extents(ufo, colr_glyph)

    ufo.lib[ufo2ft.constants.COLOR_LAYERS_KEY] = ufo_color_layers

    # TEMPORARY
    for k, v in ufo_color_layers.items():
        print(k, v)


def _svg_ttfont(_, color_glyphs, ttfont, picosvg=True, compressed=False):
    make_svg_table(ttfont, color_glyphs, picosvg, compressed)


def _ensure_codepoints_will_have_glyphs(ufo, glyph_inputs):
    """Ensure all codepoints we use will have a glyph.

    Single codepoint sequences will directly mapped to their glyphs.
    We need to add a glyph for any codepoint that is only used in a multi-codepoint sequence.

    """
    all_codepoints = set()
    direct_mapped_codepoints = set()
    for _, codepoints, _ in glyph_inputs:
        if len(codepoints) == 1:
            direct_mapped_codepoints.update(codepoints)
        all_codepoints.update(codepoints)

    need_blanks = all_codepoints - direct_mapped_codepoints
    logging.debug("%d codepoints require blanks", len(need_blanks))
    glyph_names = []
    for codepoint in need_blanks:
        # Any layer is fine; we aren't going to draw
        glyph = ufo.newGlyph(glyph_name(codepoint))
        glyph.unicode = codepoint
        glyph_names.append(glyph.name)

    ufo.glyphOrder = ufo.glyphOrder + sorted(glyph_names)


def _generate_color_font(config: ColorFontConfig, inputs: Iterable[InputGlyph]):
    """Make a UFO and optionally a TTFont from svgs."""
    ufo = _ufo(config.family, config.upem, config.keep_glyph_names)
    _ensure_codepoints_will_have_glyphs(ufo, inputs)

    base_gid = len(ufo.glyphOrder)
    color_glyphs = [
        ColorGlyph.create(ufo, filename, base_gid + idx, codepoints, psvg)
        for idx, (filename, codepoints, psvg) in enumerate(inputs)
    ]
    ufo.glyphOrder = ufo.glyphOrder + [g.glyph_name for g in color_glyphs]
    for g in color_glyphs:
        assert g.glyph_id == ufo.glyphOrder.index(g.glyph_name)

    _COLOR_FORMAT_GENERATORS[config.color_format].apply_ufo(ufo, color_glyphs)

    with open(config.fea_file) as f:
        ufo.features.text = f.read()
    logging.debug("fea:\n%s\n" % ufo.features.text)

    ttfont = _make_ttfont(config, ufo, color_glyphs)

    # TODO may wish to nuke 'post' glyph names

    return ufo, ttfont


def _inputs(
    codepoints: Mapping[str, Tuple[int, ...]], svg_files: Iterable[str]
) -> Generator[InputGlyph, None, None]:
    for svg_file in svg_files:
        rgi = codepoints.get(os.path.basename(svg_file), None)
        if not rgi:
            raise ValueError(f"No codepoint sequence for {svg_file}")
        try:
            picosvg = SVG.parse(svg_file)
        except etree.ParseError as e:
            raise IOError(f"Unable to parse {svg_file}") from e
        yield InputGlyph(svg_file, rgi, picosvg)


def _codepoint_map(codepoint_csv):
    return {name: rgi for name, rgi in codepoints.parse_csv(codepoint_csv)}


def output_file(family, output, color_format):
    if FLAGS.output == "ufo":
        output_format = ".ufo"
    else:
        output_format = _COLOR_FORMAT_GENERATORS[color_format].font_ext
    return f"{family.replace(' ', '')}{output_format}"


def main(argv):
    argv = util.expand_ninja_response_files(argv)

    config = ColorFontConfig(
        upem=FLAGS.upem,
        family=FLAGS.family,
        color_format=FLAGS.color_format,
        fea_file=util.only(lambda a: os.path.splitext(a)[1] == ".fea", argv),
        output_format=os.path.splitext(FLAGS.output_file)[1],
        keep_glyph_names=FLAGS.keep_glyph_names,
    )

    codepoints = _codepoint_map(
        util.only(lambda a: os.path.splitext(a)[1] == ".csv", argv)
    )
    svg_files = filter(lambda a: os.path.splitext(a)[1] == ".svg", argv)
    inputs = list(_inputs(codepoints, svg_files))
    if not inputs:
        sys.exit("Please provide at least one svg filename")
    ufo, ttfont = _generate_color_font(config, inputs)
    _write(ufo, ttfont, FLAGS.output_file)
    logging.info("Wrote %s" % FLAGS.output_file)


if __name__ == "__main__":
    app.run(main)
