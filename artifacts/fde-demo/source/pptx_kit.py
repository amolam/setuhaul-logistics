"""Small drawing kit on top of python-pptx: native, editable shapes, connectors, tables and charts.

Everything is drawn in inches on a 13.333 x 7.5 in (16:9) slide. No raster images are generated here; the PNGs in
diagrams/ and charts/ are exported from these native slides by PowerPoint (render.ps1).
"""
from __future__ import annotations

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml import parse_xml
from pptx.util import Emu, Inches, Pt

# ---- palette (SetuHaul "road" palette, carried over from presentation/build_deck.js) -------------------------------
NAVY, AMBER, INK, MUTED = "14213D", "F2A007", "1B2333", "5B6B82"
LINE, LIGHT, WHITE = "D5DBE4", "EEF1F5", "FFFFFF"
GREEN, RED, BLUE = "2E7D5B", "C0392B", "3A6EA5"
# role colours used in every diagram: AI, deterministic code / database, people, external / data
AI_FILL, AI_LINE = "FDF0D5", "D98E04"
DET_FILL, DET_LINE = "E3ECF6", "3A6EA5"
HUM_FILL, HUM_LINE = "E2F0EA", "2E7D5B"
EXT_FILL, EXT_LINE = "F1F2F4", "8A94A6"
RISK_FILL, RISK_LINE = "FBE9E7", "C0392B"

HEAD_FONT, BODY_FONT = "Arial", "Calibri"
SLIDE_W, SLIDE_H = 13.333, 7.5


def rgb(h: str) -> RGBColor:
    return RGBColor.from_string(h)


def new_deck() -> Presentation:
    p = Presentation()
    p.slide_width, p.slide_height = Inches(SLIDE_W), Inches(SLIDE_H)
    return p


def blank(p: Presentation, bg: str | None = None):
    s = p.slides.add_slide(p.slide_layouts[6])
    if bg:
        f = s.background.fill
        f.solid()
        f.fore_color.rgb = rgb(bg)
    return s


def _apply_runs(par, content, size, color, bold, font, italic=False):
    """content: str, or a list of (text, {bold, color, italic, size}) runs."""
    runs = [(content, {})] if isinstance(content, str) else content
    for text, o in runs:
        r = par.add_run()
        r.text = text
        f = r.font
        f.name = o.get("font", font)
        f.size = Pt(o.get("size", size))
        f.bold = o.get("bold", bold)
        f.italic = o.get("italic", italic)
        f.color.rgb = rgb(o.get("color", color))


def _bullet(par, level=0, char="•", color=None):
    pPr = par._p.get_or_add_pPr()
    indent = 0.18 + 0.22 * level
    pPr.set("marL", str(int(Emu(Inches(indent)))))
    pPr.set("indent", str(int(-Emu(Inches(0.18)))))
    for tag in ("a:buNone", "a:buChar", "a:buAutoNum", "a:buClr"):
        for el in pPr.findall(qn(tag)):
            pPr.remove(el)
    if color:
        pPr.append(parse_xml(f'<a:buClr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                             f'<a:srgbClr val="{color}"/></a:buClr>'))
    pPr.append(parse_xml(f'<a:buChar xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" char="{char}"/>'))


def text(slide, x, y, w, h, paras, size=14, color=INK, bold=False, font=BODY_FONT, align="l", anchor="t",
         bullets=False, space_after=4, italic=False, margin=0.04, name=None, line_spacing=None, bullet_color=None):
    """paras: a str, or a list of paragraphs; a paragraph is a str, a run list, or a dict
    {"t": str|runs, "bullet": bool, "level": int, "size": n, "color": hex, "bold": b, "align": a}."""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        tb.name = name
    tf = tb.text_frame
    tf.word_wrap = True
    for side in ("left", "right", "top", "bottom"):
        setattr(tf, f"margin_{side}", Inches(margin))
    tf.vertical_anchor = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}[anchor]
    if isinstance(paras, (str, tuple)) or (isinstance(paras, list) and paras and isinstance(paras[0], tuple)):
        paras = [paras]
    for i, p in enumerate(paras):
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        d = p if isinstance(p, dict) else {"t": p}
        par.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}[d.get("align", align)]
        par.space_after = Pt(d.get("space_after", space_after))
        if line_spacing:
            par.line_spacing = line_spacing
        _apply_runs(par, d["t"], d.get("size", size), d.get("color", color), d.get("bold", bold), font,
                    d.get("italic", italic))
        if d.get("bullet", bullets):
            _bullet(par, d.get("level", 0), color=bullet_color)
    return tb


def box(slide, x, y, w, h, label="", fill=DET_FILL, line=DET_LINE, size=12, color=INK, bold=False, align="c",
        anchor="m", shape="round", radius=0.08, line_w=1.0, font=BODY_FONT, name=None, margin=0.06, shadow=False,
        dash=False):
    kind = {"round": MSO_SHAPE.ROUNDED_RECTANGLE, "rect": MSO_SHAPE.RECTANGLE, "chevron": MSO_SHAPE.CHEVRON,
            "pent": MSO_SHAPE.PENTAGON, "oval": MSO_SHAPE.OVAL, "hex": MSO_SHAPE.HEXAGON}[shape]
    s = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        s.name = name
    if shape == "round":
        s.adjustments[0] = radius
    if fill:
        s.fill.solid()
        s.fill.fore_color.rgb = rgb(fill)
    else:
        s.fill.background()
    if line:
        s.line.color.rgb = rgb(line)
        s.line.width = Pt(line_w)
        if dash:
            from pptx.enum.dml import MSO_LINE_DASH_STYLE
            s.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    else:
        s.line.fill.background()
    if not shadow:
        sp = s._element.spPr
        sp.append(parse_xml('<a:effectLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"/>'))
    tf = s.text_frame
    tf.word_wrap = True
    for side in ("left", "right", "top", "bottom"):
        setattr(tf, f"margin_{side}", Inches(margin))
    tf.vertical_anchor = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}[anchor]
    if label != "":
        paras = label if isinstance(label, list) and label and not isinstance(label[0], tuple) else [label]
        for i, p in enumerate(paras):
            par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            d = p if isinstance(p, dict) else {"t": p}
            par.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}[d.get("align", align)]
            par.space_after = Pt(d.get("space_after", 2))
            _apply_runs(par, d["t"], d.get("size", size), d.get("color", color), d.get("bold", bold), font)
            if d.get("bullet"):
                _bullet(par, d.get("level", 0))
    return s


def arrow(slide, x1, y1, x2, y2, color=MUTED, width=1.5, dash=False, head=True, both=False, name=None):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    if name:
        c.name = name
    c.line.color.rgb = rgb(color)
    c.line.width = Pt(width)
    ln = c.line._get_or_add_ln()
    if dash:
        ln.append(parse_xml('<a:prstDash xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" val="dash"/>'))
    if both:
        ln.append(parse_xml('<a:headEnd xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" type="triangle" w="med" len="med"/>'))
    if head:
        ln.append(parse_xml('<a:tailEnd xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" type="triangle" w="med" len="med"/>'))
    return c


def elbow(slide, pts, color=MUTED, width=1.5, dash=False):
    """A poly-line made of straight connectors; arrow head on the last segment only."""
    for i in range(len(pts) - 1):
        (a, b), (c, d) = pts[i], pts[i + 1]
        arrow(slide, a, b, c, d, color=color, width=width, dash=dash, head=(i == len(pts) - 2))


def table(slide, x, y, w, rows, col_w, size=11, header_fill=NAVY, header_color=WHITE, row_h=0.32, zebra=LIGHT,
          first_bold=False, fills=None, colors=None):
    nrows, ncols = len(rows), len(rows[0])
    gt = slide.shapes.add_table(nrows, ncols, Inches(x), Inches(y), Inches(w), Inches(row_h * nrows))
    t = gt.table
    tblPr = t._tbl.tblPr
    for el in list(tblPr):
        if el.tag == qn("a:tableStyleId"):
            tblPr.remove(el)
    for j, cw in enumerate(col_w):
        t.columns[j].width = Inches(cw)
    for i, row in enumerate(rows):
        t.rows[i].height = Inches(row_h)
        for j, val in enumerate(row):
            cell = t.cell(i, j)
            cell.margin_left = cell.margin_right = Inches(0.06)
            cell.margin_top = cell.margin_bottom = Inches(0.03)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            par = tf.paragraphs[0]
            hdr = i == 0
            c = header_color if hdr else (colors[i][j] if colors and colors[i] and colors[i][j] else INK)
            b = hdr or (first_bold and j == 0)
            _apply_runs(par, val, size, c, b, BODY_FONT)
            cell.fill.solid()
            if hdr:
                cell.fill.fore_color.rgb = rgb(header_fill)
            elif fills and fills[i] and fills[i][j]:
                cell.fill.fore_color.rgb = rgb(fills[i][j])
            else:
                cell.fill.fore_color.rgb = rgb(zebra if (i % 2 == 0 and zebra) else WHITE)
    return gt


def bar_chart(slide, x, y, w, h, categories, series, colors, title=None, stacked=False, horizontal=False,
              number_format='0.0', legend=True, font_size=11, label_size=10, gap=60, overlap=None, max_v=None,
              labels=True, axis_title=None):
    cd = CategoryChartData()
    cd.categories = categories
    for name, vals in series:
        cd.add_series(name, vals)
    if horizontal:
        kind = XL_CHART_TYPE.BAR_STACKED if stacked else XL_CHART_TYPE.BAR_CLUSTERED
    else:
        kind = XL_CHART_TYPE.COLUMN_STACKED if stacked else XL_CHART_TYPE.COLUMN_CLUSTERED
    gf = slide.shapes.add_chart(kind, Inches(x), Inches(y), Inches(w), Inches(h), cd)
    ch = gf.chart
    ch.font.size = Pt(font_size)
    ch.font.name = BODY_FONT
    ch.font.color.rgb = rgb(INK)
    if title:
        ch.has_title = True
        ch.chart_title.text_frame.text = title
        p = ch.chart_title.text_frame.paragraphs[0]
        p.runs[0].font.size = Pt(font_size + 1)
        p.runs[0].font.bold = True
        p.runs[0].font.color.rgb = rgb(INK)
    else:
        ch.has_title = False
    ch.has_legend = legend
    if legend:
        ch.legend.position = XL_LEGEND_POSITION.BOTTOM
        ch.legend.include_in_layout = False
        ch.legend.font.size = Pt(font_size)
    plot = ch.plots[0]
    plot.gap_width = gap
    if overlap is not None:
        plot.overlap = overlap
    for i, s in enumerate(plot.series):
        s.format.fill.solid()
        s.format.fill.fore_color.rgb = rgb(colors[i % len(colors)])
        s.invert_if_negative = False
    if labels:
        plot.has_data_labels = True
        dl = plot.data_labels
        dl.number_format = number_format
        dl.number_format_is_linked = False
        dl.font.size = Pt(label_size)
        dl.font.color.rgb = rgb(WHITE if stacked else INK)
        dl.position = XL_LABEL_POSITION.CENTER if stacked else XL_LABEL_POSITION.OUTSIDE_END
    va = ch.value_axis
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = rgb(LINE)
    va.format.line.fill.background()
    va.tick_labels.font.size = Pt(font_size - 1)
    va.tick_labels.font.color.rgb = rgb(MUTED)
    va.tick_labels.number_format = number_format
    va.tick_labels.number_format_is_linked = False
    if max_v is not None:
        va.maximum_scale = max_v
        va.minimum_scale = 0
    if axis_title:
        va.has_title = True
        va.axis_title.text_frame.text = axis_title
        r = va.axis_title.text_frame.paragraphs[0].runs[0]
        r.font.size = Pt(font_size - 1)
        r.font.bold = False
        r.font.color.rgb = rgb(MUTED)
    ca = ch.category_axis
    ca.tick_labels.font.size = Pt(font_size)
    ca.tick_labels.font.color.rgb = rgb(INK)
    ca.format.line.color.rgb = rgb(LINE)
    ca.has_major_gridlines = False
    return ch


def legend_row(slide, x, y, items, size=11, sw=0.22, gap=0.18):
    """items: [(label, fill, line)] drawn left to right."""
    cx = x
    for label, fill, line in items:
        box(slide, cx, y + 0.02, sw, sw, "", fill=fill, line=line, radius=0.2)
        tw = 0.12 + 0.075 * len(label)
        text(slide, cx + sw + 0.06, y - 0.02, tw, 0.3, label, size=size, color=MUTED, margin=0)
        cx += sw + 0.06 + tw + gap
    return cx


def notes(slide, txt: str):
    slide.notes_slide.notes_text_frame.text = txt
