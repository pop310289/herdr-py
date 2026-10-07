"""Drawing tools of the layout team (components.py): the optional settings, the svg component and its sanitiser.

Every sanitiser rule has its own test here; each was checked by removing the rule and watching its test fail.
"""
import hashlib
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "examples", "slide_team"))
import components as C  # noqa: E402

SIZE = (1206, 1441)
LAYOUT = os.path.join(ROOT, "examples", "slide_team", "layout")
NEW = {"text": ("italic",), "tokens": ("border_width", "outline"), "grid": ("cell_w", "cell_h"),
       "box": ("border_width", "title_color", "dashed"), "arrow": ("curved", "head_size")}
# sha256 of C.svg(base + reference) drawn by components.py before the optional settings existed (main at 06f9396)
BEFORE = "bbe18b57c313590f695e43a0a1a9efc50d689228c45a6806b2f97f2ef1c7fac2"


def load(name):
    with open(os.path.join(LAYOUT, name), encoding="utf-8") as handle:
        return json.load(handle)


def one(comp, prefix="r1."):
    """(errors, warnings, normalized component or None) for a single component."""
    errors, warnings, out = C.check([comp], prefix, SIZE)
    return errors, warnings, (out[0] if out else None)


def svg_comp(markup, **extra):
    comp = {"id": "r1.pic", "type": "svg", "x": 100, "y": 200, "w": 60, "h": 40, "markup": markup}
    comp.update(extra)
    return comp


def problem(markup):
    """The first error check() gives for an svg component with this markup, without the id ('' when it passes)."""
    errors = one(svg_comp(markup))[0]
    return errors[0].split(": ", 1)[1] if errors else ""


class NewSettingsTest(unittest.TestCase):
    def test_settings_drawers_wrote_are_now_known_and_unknown_ones_still_warn(self):
        _, warnings, box = one({"id": "r1.b", "type": "box", "x": 10, "y": 10, "w": 100, "h": 50, "border_width": 2,
                                "title_color": "prefill", "dashed": True, "stroke_width": 2})
        self.assertEqual(warnings, ['r1.b: "stroke_width" is not a box setting and was ignored'])
        self.assertEqual((box["border_width"], box["title_color"], box["dashed"]), (2, "prefill", True))
        _, warnings, tok = one({"id": "r1.t", "type": "tokens", "x": 10, "y": 10, "labels": ["A"], "border_width": 1.5, "outline": True})
        self.assertEqual((warnings, tok["border_width"], tok["outline"]), ([], 1.5, True))

    def test_defaults_are_filled_and_follow_settings_stay_out(self):
        box = one({"id": "r1.b", "type": "box", "x": 10, "y": 10, "w": 100, "h": 50})[2]
        self.assertEqual((box["border_width"], box["dashed"]), (3, False))
        self.assertNotIn("title_color", box)  # left out: the title keeps the colour of the box kind
        grid = one({"id": "r1.g", "type": "grid", "x": 10, "y": 10, "cell": 30})[2]
        self.assertFalse({"cell_w", "cell_h"} & set(grid))  # left out, so a later change of "cell" still shows
        arrow = one({"id": "r1.a", "type": "arrow", "points": [[1, 1], [9, 9]]})[2]
        self.assertEqual((arrow["curved"], "head_size" in arrow), (False, False))
        self.assertEqual(one({"id": "r1.x", "type": "text", "x": 1, "y": 1, "text": "hi"})[2]["italic"], False)
        tok = one({"id": "r1.t", "type": "tokens", "x": 10, "y": 10, "labels": ["A"]})[2]
        self.assertEqual((tok["border_width"], tok["outline"]), (2, False))

    def test_wrong_values_name_the_component_and_the_fix(self):
        cases = [
            ({"type": "box", "x": 1, "y": 1, "w": 9, "h": 9, "title_color": "orange"},
             'r1.c: title_color "orange" is not allowed; use one of text, muted, red, white, prefill, kv, decode'),
            ({"type": "box", "x": 1, "y": 1, "w": 9, "h": 9, "border_width": "2"}, 'r1.c: "border_width" must be a number'),
            ({"type": "box", "x": 1, "y": 1, "w": 9, "h": 9, "border_width": 11}, "r1.c: border_width must be 0 to 10 (pixels)"),
            ({"type": "box", "x": 1, "y": 1, "w": 9, "h": 9, "dashed": "yes"}, 'r1.c: "dashed" must be bool'),
            ({"type": "tokens", "x": 1, "y": 1, "labels": ["A"], "border_width": -1}, "r1.c: border_width must be 0 to 10 (pixels)"),
            ({"type": "tokens", "x": 1, "y": 1, "labels": ["A"], "outline": 1}, 'r1.c: "outline" must be bool'),
            ({"type": "grid", "x": 1, "y": 1, "cell_h": 2}, "r1.c: cell_h must be 4 to 120 (pixels)"),
            ({"type": "grid", "x": 1, "y": 1, "cell_w": 121}, "r1.c: cell_w must be 4 to 120 (pixels)"),
            ({"type": "arrow", "points": [[1, 1], [9, 9]], "head_size": 50}, "r1.c: head_size must be 4 to 40 (pixels)"),
            ({"type": "arrow", "points": [[1, 1], [9, 9]], "curved": "true"}, 'r1.c: "curved" must be bool'),
            ({"type": "text", "x": 1, "y": 1, "text": "hi", "italic": 1}, 'r1.c: "italic" must be bool'),
        ]
        for comp, expected in cases:
            comp = dict(comp, id="r1.c")
            self.assertEqual(one(comp)[0], [expected], comp)

    def test_text_italic(self):
        italic = C.draw_text(one({"id": "r1.x", "type": "text", "x": 1, "y": 1, "text": "TheAiEdge.io", "italic": True})[2])
        upright = C.draw_text(one({"id": "r1.x", "type": "text", "x": 1, "y": 1, "text": "TheAiEdge.io"})[2])
        self.assertIn(' font-style="italic"', italic)
        self.assertNotIn("font-style", upright)

    def test_tokens_outline_and_border_width(self):
        tok = one({"id": "r1.t", "type": "tokens", "x": 10, "y": 10, "labels": ["A"], "kind": "decode", "outline": True,
                   "border_width": 1.5})[2]
        drawn = C.draw_tokens(tok)
        fill, border, ink = C.TOKENS["decode"]
        self.assertIn(f'fill="none" stroke="{border}" stroke-width="1.5"', drawn)
        self.assertIn(f'fill="{border}" text-anchor="middle">A<', drawn)  # a white letter would vanish without the fill
        prefill = C.draw_tokens(one({"id": "r1.t", "type": "tokens", "x": 10, "y": 10, "labels": ["P"], "kind": "prefill",
                                     "outline": True})[2])
        self.assertIn(f'fill="{C.TOKENS["prefill"][2]}" text-anchor="middle">P<', prefill)  # a dark letter stays
        self.assertNotIn(C.TOKENS["prefill"][0], prefill)
        filled = C.draw_tokens(one({"id": "r1.t", "type": "tokens", "x": 10, "y": 10, "labels": ["A"], "kind": "decode"})[2])
        self.assertIn(f'fill="{fill}" stroke="{border}" stroke-width="2"', filled)

    def test_grid_cells_taller_than_wide(self):
        grid = one({"id": "r1.g", "type": "grid", "x": 612, "y": 340, "cell_w": 22, "cell_h": 31, "gap": 3,
                    "row_labels": ["P", "A", "B"]})[2]
        drawn = C.draw_grid(grid)
        cells = re.findall(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', drawn)
        self.assertEqual(len(cells), 9)
        self.assertEqual(cells[:2], [("612", "340", "22", "31"), ("637", "340", "22", "31")])
        self.assertEqual(cells[3][:2], ("612", "374"))  # next row: 31 + 3 lower
        self.assertEqual(C.bbox(grid), (612, 340, 612 + 3 * 25, 340 + 3 * 34))
        self.assertIn('font-size="19"', drawn)  # row labels follow the cell height
        square = C.draw_grid(one({"id": "r1.g", "type": "grid", "x": 612, "y": 340, "cell": 24, "cell_h": 31})[2])
        self.assertIn('width="24" height="31"', square)  # cell_w falls back to cell

    def test_box_border_width_title_color_and_dashed(self):
        box = one({"id": "r1.b", "type": "box", "x": 10, "y": 20, "w": 100, "h": 50, "kind": "chunk", "title": "Chunk 3",
                   "stack": 1, "border_width": 2, "title_color": "prefill", "dashed": True})[2]
        drawn = C.draw_box(box)
        self.assertIn('stroke-width="2" stroke-dasharray="6 4"/>', drawn)  # the box
        self.assertIn('stroke-width="1.33" stroke-dasharray="6 4"/>', drawn)  # the machine behind: 2/3 of the border
        self.assertIn(f'fill="{C.INK["prefill"]}" text-anchor="middle">Chunk 3<', drawn)
        plain = C.draw_box(one({"id": "r1.b", "type": "box", "x": 10, "y": 20, "w": 100, "h": 50, "kind": "chunk",
                                "title": "Chunk 3", "stack": 1})[2])
        self.assertIn(f'fill="{C.BOXES["chunk"][2]}" text-anchor="middle">Chunk 3<', plain)
        self.assertNotIn("dasharray", plain)

    def test_curved_arrow_rounds_every_bend(self):
        arrow = one({"id": "r1.a", "type": "arrow", "points": [[262, 287], [335, 287], [335, 378], [412, 378]], "curved": True})[2]
        drawn = C.draw_arrow(arrow)
        self.assertTrue(drawn.startswith('<path d="M262,287 L320.0,287.0 Q335,287 335.0,302.0 L335.0,363.0 Q335,378 350.0,378.0 '
                                         'L412,378"'), drawn)
        self.assertIn('marker-end="url(#head-line)"', drawn)
        short = C.rounded([[0, 0], [10, 0], [10, 40]])  # 10 px segment: the corner radius shrinks to half of it
        self.assertEqual(short, "M0,0 L5.0,0.0 Q10,0 10.0,5.0 L10,40")
        self.assertEqual(C.rounded([[0, 0], [0, 0], [5, 0]]), "M0,0 L0,0 L5,0")  # a repeated point is not divided by

    def test_head_size_makes_one_marker_per_colour_and_size(self):
        comps = C.check([{"id": "r1.a", "type": "arrow", "points": [[1, 1], [90, 1]], "color": "chunk", "head_size": 10},
                         {"id": "r1.b", "type": "arrow", "points": [[1, 9], [90, 9]], "color": "chunk", "head_size": 10},
                         {"id": "r1.c", "type": "arrow", "points": [[1, 19], [90, 19]], "color": "chunk", "head_size": 12.5},
                         {"id": "r1.d", "type": "arrow", "points": [[1, 29], [90, 29]], "color": "chunk"},
                         {"id": "r1.e", "type": "arrow", "points": [[1, 39], [90, 39]], "color": "kv", "head": False,
                          "head_size": 20}], "r1.", SIZE)[2]
        drawing = C.svg(comps, size=SIZE)
        self.assertEqual(drawing.count('<marker id="head-chunk-10" '), 1)
        self.assertIn('<marker id="head-chunk-12_5" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" '
                      'markerWidth="12.5" markerHeight="12.5"', drawing)
        self.assertNotIn("head-kv-20", drawing)  # no head, no marker
        self.assertEqual(re.findall(r'marker-end="url\(#([\w-]+)\)"', drawing), ["head-chunk-10", "head-chunk-10", "head-chunk-12_5",
                                                                             "head-chunk"])

    def test_lists_without_new_settings_draw_exactly_as_before(self):
        """The reference with every new setting taken out must draw byte for byte what main drew: ids, positions and
        every old default are unchanged, and the new code adds nothing when a setting is not used."""
        old = [{k: v for k, v in c.items() if k not in NEW.get(c["type"], ())}
               for c in load("base_portrait.json") + load("reference_portrait.json") if c["type"] != "svg"]
        errors, warnings, comps = C.check(old, size=SIZE)
        self.assertEqual((errors, warnings), ([], []))
        self.assertEqual(hashlib.sha256(C.svg(comps, size=SIZE).encode("utf-8")).hexdigest(), BEFORE)

    def test_the_reference_uses_the_new_settings_cleanly(self):
        errors, warnings, comps = C.check(load("base_portrait.json") + load("reference_portrait.json"), size=SIZE)
        self.assertEqual((errors, warnings), ([], []))
        used = {(c["type"], k) for c in load("base_portrait.json") + load("reference_portrait.json") for k in NEW.get(c["type"], ())
                if k in c}
        self.assertTrue({("grid", "cell_w"), ("grid", "cell_h"), ("tokens", "outline"), ("arrow", "curved")} <= used, used)
        self.assertNotEqual(hashlib.sha256(C.svg(comps, size=SIZE).encode("utf-8")).hexdigest(), BEFORE)
        for row in (1, 2, 3):  # every row still passes the row check a drawer's list gets
            self.assertEqual(C.check([c for c in load("reference_portrait.json") if c["id"].startswith(f"r{row}.")],
                                     f"r{row}.", SIZE)[:2], ([], []))

    def test_guide_teaches_every_new_setting_and_its_examples_pass_the_check(self):
        for kind, keys in NEW.items():
            for key in keys:
                self.assertIn(f'"{key}"', C.GUIDE, (kind, key))
        decoder = json.JSONDecoder()  # an example line may end with a note in brackets after the JSON
        examples = [decoder.raw_decode(line.split(":", 1)[1].strip())[0] for line in C.guide(SIZE).splitlines()
                    if re.match(r"- \w+: +\{", line)]
        self.assertEqual([e["type"] for e in examples], list(C.AGENT_TYPES))
        self.assertEqual(C.check(examples, "r1.", (1920, 1080))[0], [])


class SvgComponentTest(unittest.TestCase):
    def test_drawn_inside_its_box_scaled_from_its_viewbox(self):
        comp = one(svg_comp("<svg viewBox='0 0 70 8' fill='none' width='700'><path d='M1 0 V7 H69 V0' stroke='#C77C02'/></svg>"))[2]
        self.assertEqual(C.draw_svg(comp), '<svg x="100" y="200" width="60" height="40" viewBox="0 0 70 8" fill="none">'
                                           '<path d="M1 0 V7 H69 V0" stroke="#C77C02"></path></svg>')
        self.assertEqual(C.bbox(comp), (100, 200, 160, 240))
        self.assertIn(C.draw_svg(comp), C.svg([comp], size=SIZE))

    def test_shapes_without_an_svg_element_use_the_box_size(self):
        comp = one(svg_comp("<rect width='60' height='40' fill='#4CAF50'/><circle cx='30' cy='20' r='5'/>"))[2]
        self.assertEqual(C.draw_svg(comp), '<svg x="100" y="200" width="60" height="40" viewBox="0 0 60 40">'
                                           '<rect width="60" height="40" fill="#4CAF50"></rect><circle cx="30" cy="20" r="5">'
                                           '</circle></svg>')

    def test_width_and_height_stand_in_for_a_missing_viewbox(self):
        comp = one(svg_comp('<svg xmlns="http://www.w3.org/2000/svg" width="24px" height="12"><rect width="24" height="12"/></svg>'))[2]
        self.assertTrue(C.draw_svg(comp).startswith('<svg x="100" y="200" width="60" height="40" viewBox="0 0 24 12">'))
        zero = one(svg_comp("<svg width='0' height='12'><rect width='9' height='9'/></svg>"))[2]  # a 0 wide viewBox hides all
        self.assertTrue(C.draw_svg(zero).startswith('<svg x="100" y="200" width="60" height="40" viewBox="0 0 60 40">'))

    def test_written_back_from_the_parsed_tree_only(self):
        comp = one(svg_comp("<?xml version='1.0'?><svg xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink'"
                            " xmlns:ink='urn:x' viewBox='0 0 9 9'><!-- note --><?pi x?><defs><path id='p' d='M0 0'/></defs>"
                            "<use xlink:href='#p' ink:label='x'/><text xml:space='preserve'>a &lt; b &amp; \"c\"</text></svg>"))[2]
        drawn = C.draw_svg(comp)
        self.assertIn('<use href="#p"></use>', drawn)  # xlink:href written as href, foreign attributes dropped
        self.assertIn('<text xml:space="preserve">a &lt; b &amp; &quot;c&quot;</text>', drawn)
        self.assertNotIn("<!--", drawn)
        self.assertNotIn("<?", drawn)
        for markup in ("<use href='#a' xlink:href='#b'/>", "<use xlink:href='#b' href='#a'/>"):  # either order: href wins (SVG 2)
            self.assertIn('<use href="#a"></use>', C.draw_svg(one(svg_comp(markup))[2]), markup)

    def test_text_inside_counts_as_a_label(self):
        comps = C.check([svg_comp("<svg viewBox='0 0 9 9'><text>WAIT</text></svg>")], "r1.", SIZE)[2]
        self.assertEqual(C.missing(comps, ["WAIT", "New"]), ["New"])

    def test_bad_markup_is_never_written(self):
        """svg() on a list that skipped check() draws a red frame instead of the markup."""
        drawn = C.svg([dict(svg_comp("<script>alert(1)</script>"))], size=SIZE)
        self.assertNotIn("script", drawn)
        self.assertIn('stroke-dasharray="6 4"', drawn)

    def test_safe_markup_passes(self):
        for markup in ("<svg viewBox='0 0 9 9'><rect width='9' height='9' fill='url(#g)' style='stroke: url( \"#g\" )'/></svg>",
                       "<image href='data:image/png;base64,iVBORw0KGgo=' width='9' height='9'/>",
                       "<style>/* note */ rect { fill: #F5A623 }</style><rect width='9' height='9'/>",
                       "<a href='#r'><rect id='r' width='9' height='9' opacity='0.5'/></a>"):
            self.assertEqual(problem(markup), "", markup)


class SanitiserRuleTest(unittest.TestCase):
    """One test per rule; each markup here is refused by that rule alone (the others would let it through)."""

    def test_size_is_capped(self):
        markup = "<svg viewBox='0 0 9 9'><title>" + "x" * C.MAX_MARKUP + "</title></svg>"
        self.assertEqual(problem(markup), f"markup is {len(markup)} characters; keep it under {C.MAX_MARKUP}")

    def test_element_count_is_capped(self):
        self.assertEqual(problem("<g/>" * C.MAX_ELEMENTS), f"the markup has more than {C.MAX_ELEMENTS} elements; draw fewer shapes")
        self.assertEqual(problem("<g/>" * (C.MAX_ELEMENTS - 1)), "")  # the wrapping <svg> is the last one allowed

    def test_nesting_depth_is_capped(self):
        self.assertEqual(problem("<g>" * 1500 + "</g>" * 1500), f"the markup nests elements more than {C.MAX_DEPTH} deep")
        self.assertEqual(problem("<g>" * (C.MAX_DEPTH - 1) + "</g>" * (C.MAX_DEPTH - 1)), "")

    def test_doctype_is_refused(self):
        self.assertTrue(problem("<!DOCTYPE svg><svg viewBox='0 0 9 9'/>").startswith("remove the DOCTYPE or ENTITY declaration"))

    def test_entity_declarations_are_refused(self):
        markup = "<!DOCTYPE svg [<!ENTITY a \"aaaaaaaaaa\"><!ENTITY b \"&a;&a;&a;&a;\">]><svg viewBox='0 0 9 9'><text>&b;</text></svg>"
        self.assertTrue(problem(markup).startswith("remove the DOCTYPE or ENTITY declaration"))

    def test_javascript_is_refused_anywhere(self):
        for markup in ("<svg viewBox='0 0 9 9'><text>javascript:alert(1)</text></svg>",
                       "<svg viewBox='0 0 9 9'><!-- JavaScript:alert(1) --></svg>",
                       "<svg viewBox='0 0 9 9'><text>java&#x9;script&#58;alert(1)</text></svg>"):
            self.assertEqual(problem(markup), "javascript: is not allowed anywhere in the markup", markup)

    def test_markup_that_is_not_xml_is_refused(self):
        self.assertTrue(problem("<svg viewBox='0 0 9 9'><rect></svg>").startswith("the markup is not well-formed XML (mismatched tag"))
        self.assertTrue(problem("<rect width='\ud800'/>").startswith("the markup is not well-formed XML"))  # JSON allows lone surrogates

    def test_one_svg_root(self):
        self.assertEqual(problem("<?xml version='1.0'?><g/>"),
                         "the markup must be one <svg viewBox='...'> element, or shapes without an <svg> around them")

    def test_script_is_refused(self):
        self.assertEqual(problem("<svg viewBox='0 0 9 9'><script>alert(1)</script></svg>"), "<script> is not allowed: a slide runs no code")
        self.assertEqual(problem("<SCRIPT xmlns='http://www.w3.org/1999/xhtml'>x()</SCRIPT>"),
                         "<SCRIPT> is not allowed: a slide runs no code")

    def test_foreign_object_is_refused(self):
        self.assertEqual(problem("<foreignObject width='9' height='9'><div xmlns='http://www.w3.org/1999/xhtml'>hi</div></foreignObject>"),
                         "<foreignObject> is not allowed: draw with SVG shapes, not HTML")

    def test_iframe_embed_and_object_are_refused(self):
        for tag in ("iframe", "embed", "object"):
            self.assertEqual(problem(f"<{tag}/>"), f"<{tag}> is not allowed: a slide embeds no pages or plugins")

    def test_animation_is_refused(self):  # <set> could turn a checked #id link into an outside one after loading
        for markup in ("<a href='#r'><set attributeName='href' to='https://example.com/'/></a>",
                       "<rect width='9' height='9'><animate attributeName='width' to='1' dur='1s'/></rect>"):
            self.assertTrue(problem(markup).endswith("is not allowed: the slide is a still picture"), markup)

    def test_elements_that_are_not_svg_drawing_are_refused(self):
        self.assertEqual(problem("<meta http-equiv='refresh' content='0;url=https://example.com/'/>"),
                         "<meta> is not an SVG drawing element; use rect, circle, ellipse, line, polyline, polygon, path, text or g")

    def test_event_attributes_are_refused(self):
        self.assertEqual(problem("<svg viewBox='0 0 9 9' onload='alert(1)'/>"), "the event attribute onload is not allowed")
        self.assertEqual(problem("<rect width='9' height='9' OnClick='x()'/>"), "the event attribute OnClick is not allowed")

    def test_links_only_to_ids_and_data_images(self):
        for markup in ("<image href='https://example.com/a.png' width='9' height='9'/>",
                       "<use xmlns:xlink='http://www.w3.org/1999/xlink' xlink:href='other.svg#a'/>",
                       "<image href='file:///etc/passwd' width='9' height='9'/>",
                       "<image href='data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=' width='9' height='9'/>",
                       "<g xml:base='https://example.com/'><use href='#a'/></g>",
                       "<use href=''/>"):
            self.assertIn(f"is not allowed; {C.LINK_RULE}", problem(markup), markup)

    def test_url_only_to_ids_and_data_images(self):
        for markup in ("<rect width='9' height='9' fill='url(https://example.com/p.svg#x)'/>",
                       "<rect width='9' height='9' style='fill: URL( \"//example.com/p.svg#x\" )'/>",
                       "<style>rect { mask: url(file:///etc/x) }</style>",
                       "<rect width='9' height='9' filter='url(#f'/>"):
            self.assertTrue(problem(markup).startswith("url("), markup)
            self.assertTrue(problem(markup).endswith("is not allowed; use url(#id) or a data:image/png, jpeg or gif;base64 picture"))

    def test_css_import_is_refused(self):
        self.assertEqual(problem("<style>@import 'https://example.com/a.css';</style>"),
                         "CSS @import is not allowed; write the styles in the markup itself")

    def test_css_escapes_are_refused(self):  # u\72l( is url( to a browser, but not to the url() rule
        self.assertEqual(problem("<rect width='9' height='9' style='fill: u\\72l(https://example.com/p)'/>"),
                         "CSS escapes (backslashes) are not allowed in styles")

    def test_css_image_loaders_are_refused(self):  # image-set() fetches a plain string, without url()
        self.assertEqual(problem("<style>svg { background-image: image-set('https://example.com/a.png' 1x) }</style>"),
                         "image-set(), image() and src() are not allowed in styles; use url(#id) or a data:image")

    def test_viewbox_must_be_four_numbers(self):
        for view in ("0 0 24", "0 0 -1 9", "a b c d", "0 0 nan 9"):
            self.assertEqual(problem(f"<svg viewBox='{view}'/>"),
                             "viewBox must be four numbers like '0 0 24 24' (width and height more than 0)", view)

    def test_empty_markup(self):
        self.assertTrue(problem("  ").startswith("markup is empty"))

    def test_svg_box_must_fit_the_canvas(self):
        self.assertEqual(one(svg_comp("<rect width='9' height='9'/>", x=1200))[0],
                         ["r1.pic: it goes past the right or bottom edge of the canvas"])
        self.assertEqual(one(svg_comp("<rect width='9' height='9'/>", w=0))[0], ["r1.pic: w and h must be more than 0"])
        self.assertEqual(one(dict(svg_comp(""), markup=None))[0], ['r1.pic: "markup" must be str'])


if __name__ == "__main__":
    unittest.main()
