from collections import deque
from dataclasses import dataclass
import io
import math
from typing import List, Tuple
import zipfile

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

MAX_SOURCE_SIDE = 4096
MAX_WEBP_BYTES = 500 * 1024  # Safe margin below Telegram's 512 KB static sticker limit


@dataclass
class SliceConfig:
    slice_mode: str = "grid"      # 'grid' | 'objects'
    cols: int = 0                 # 0 = auto
    rows: int = 0                 # 0 = auto
    fit_mode: str = "crop"        # 'crop' | 'pad' | 'stretch'
    tile_style: str = "seamless"  # 'seamless' | 'rounded' | 'border'
    pack_type: str = "regular"    # 'regular' (512x512) | 'custom_emoji' (100x100)


@dataclass
class SliceResult:
    tiles_webp: List[bytes]
    preview_png: bytes
    cols: int
    rows: int
    count: int
    slice_mode: str
    tile_px: int


def load_clean_rgba(raw_bytes: bytes) -> Image.Image:
    with Image.open(io.BytesIO(raw_bytes)) as im:
        im = ImageOps.exif_transpose(im)
        rgba = im.convert("RGBA")
    w, h = rgba.size
    if max(w, h) > MAX_SOURCE_SIDE:
        scale = MAX_SOURCE_SIDE / float(max(w, h))
        rgba = rgba.resize(
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            Image.Resampling.LANCZOS,
        )
    return rgba


def max_tiles_for_pack(pack_type: str) -> int:
    return 200 if pack_type == "custom_emoji" else 120


def tile_size_for_pack(pack_type: str) -> int:
    return 100 if pack_type == "custom_emoji" else 512


def resolve_grid_dimensions(
    width: int, height: int, cols: int, rows: int, pack_type: str
) -> Tuple[int, int]:
    limit = max_tiles_for_pack(pack_type)
    if cols <= 0 or rows <= 0:
        default_cols = 7 if pack_type == "custom_emoji" else 4
        max_auto_rows = limit // default_cols
        ratio = height / float(max(1, width))
        calc_rows = int(round(default_cols * ratio))
        calc_rows = max(1, min(max_auto_rows, calc_rows))
        return default_cols, calc_rows

    cols = max(1, min(20, cols))
    rows = max(1, min(20, rows))
    while cols * rows > limit:
        if rows >= cols and rows > 1:
            rows -= 1
        elif cols > 1:
            cols -= 1
        else:
            break
    return cols, rows


def _ensure_non_empty_alpha(tile: Image.Image) -> Image.Image:
    """Keep at least one imperceptible pixel so empty padded corner tiles preserve grid alignment."""
    alpha_extrema = tile.getchannel("A").getextrema()
    if alpha_extrema and alpha_extrema[1] == 0:
        out = tile.copy()
        out.putpixel((0, 0), (0, 0, 0, 1))
        return out
    return tile


def _apply_grid_tile_style(tile: Image.Image, tile_style: str) -> Image.Image:
    size = tile.width
    if tile_style == "seamless":
        return _ensure_non_empty_alpha(tile)

    alpha_max = tile.getchannel("A").getextrema()[1]
    if alpha_max == 0:
        return _ensure_non_empty_alpha(tile)

    margin = max(2, int(round(size * 0.045)))
    inner = max(4, size - margin * 2)
    radius = max(4, int(round(size * 0.12)))

    resized = tile.resize((inner, inner), Image.Resampling.LANCZOS)

    ss = 2
    mask_hi = Image.new("L", (inner * ss, inner * ss), 0)
    draw_hi = ImageDraw.Draw(mask_hi)
    draw_hi.rounded_rectangle(
        (0, 0, inner * ss - 1, inner * ss - 1),
        radius=radius * ss,
        fill=255,
    )
    rounded_mask = mask_hi.resize((inner, inner), Image.Resampling.LANCZOS)
    combined_alpha = ImageChops.multiply(resized.getchannel("A"), rounded_mask)
    resized.putalpha(combined_alpha)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    if tile_style == "border":
        stroke = max(2, int(round(size * 0.03)))
        bg_hi = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
        bg_draw = ImageDraw.Draw(bg_hi)
        b0 = max(0, (margin - stroke) * ss)
        b1 = min(size * ss - 1, (margin + inner + stroke) * ss - 1)
        bg_draw.rounded_rectangle(
            (b0, b0, b1, b1),
            radius=(radius + stroke) * ss,
            fill=(255, 255, 255, 255),
        )
        canvas = bg_hi.resize((size, size), Image.Resampling.LANCZOS)

    canvas.alpha_composite(resized, (margin, margin))
    return _ensure_non_empty_alpha(canvas)


def _prepare_grid_canvas(
    src: Image.Image, cols: int, rows: int, cell_px: int, fit_mode: str
) -> Image.Image:
    target_w = cols * cell_px
    target_h = rows * cell_px

    if fit_mode == "stretch":
        return src.resize((target_w, target_h), Image.Resampling.LANCZOS)

    if fit_mode == "pad":
        scale = min(target_w / float(src.width), target_h / float(src.height))
        new_w = max(1, int(round(src.width * scale)))
        new_h = max(1, int(round(src.height * scale)))
        resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        off_x = (target_w - new_w) // 2
        off_y = (target_h - new_h) // 2
        canvas.alpha_composite(resized, (off_x, off_y))
        return canvas

    return ImageOps.fit(
        src,
        (target_w, target_h),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _make_checkerboard(width: int, height: int, cell: int = 16) -> Image.Image:
    bg = Image.new("RGBA", (width, height), (238, 240, 244, 255))
    draw = ImageDraw.Draw(bg)
    dark = (214, 218, 226, 255)
    for y in range(0, height, cell):
        row_idx = y // cell
        for x in range((row_idx % 2) * cell, width, cell * 2):
            draw.rectangle(
                (x, y, min(width, x + cell) - 1, min(height, y + cell) - 1),
                fill=dark,
            )
    return bg


def _draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    p1: Tuple[int, int],
    p2: Tuple[int, int],
    dash: int = 10,
    gap: int = 6,
) -> None:
    x1, y1 = p1
    x2, y2 = p2
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 0:
        return
    dx = (x2 - x1) / length
    dy = (y2 - y1) / length
    pos = 0.0
    draw.line((p1, p2), fill=(0, 0, 0, 150), width=3)
    while pos < length:
        end = min(length, pos + dash)
        sx = int(round(x1 + dx * pos))
        sy = int(round(y1 + dy * pos))
        ex = int(round(x1 + dx * end))
        ey = int(round(y1 + dy * end))
        draw.line(((sx, sy), (ex, ey)), fill=(255, 255, 255, 235), width=2)
        pos += dash + gap


def encode_webp_under_limit(tile: Image.Image) -> bytes:
    """Encode an RGBA tile into static WEBP below 500 KB while preserving sharpness."""
    buf = io.BytesIO()
    tile.save(buf, format="WEBP", lossless=True, method=4)
    data = buf.getvalue()
    if len(data) <= MAX_WEBP_BYTES:
        return data

    for quality in (94, 88, 80, 72, 62, 50):
        buf = io.BytesIO()
        tile.save(buf, format="WEBP", lossless=False, quality=quality, method=4)
        data = buf.getvalue()
        if len(data) <= MAX_WEBP_BYTES:
            return data
    return data


def _detect_border_background_rgb(src: Image.Image) -> Tuple[int, int, int]:
    w, h = src.size
    px = src.load()
    samples_r: List[int] = []
    samples_g: List[int] = []
    samples_b: List[int] = []
    step_x = max(1, w // 40)
    step_y = max(1, h // 40)

    for x in range(0, w, step_x):
        for y in (0, h - 1):
            r, g, b, _ = px[x, y]
            samples_r.append(r)
            samples_g.append(g)
            samples_b.append(b)
    for y in range(0, h, step_y):
        for x in (0, w - 1):
            r, g, b, _ = px[x, y]
            samples_r.append(r)
            samples_g.append(g)
            samples_b.append(b)

    samples_r.sort()
    samples_g.sort()
    samples_b.sort()
    mid = len(samples_r) // 2
    return samples_r[mid], samples_g[mid], samples_b[mid]


def _extract_object_boxes(
    src: Image.Image, max_items: int
) -> Tuple[List[Tuple[int, int, int, int]], bool, Tuple[int, int, int]]:
    """Detect separate objects on a transparent or solid-color sheet."""
    orig_w, orig_h = src.size
    work_max = 512
    scale = min(1.0, work_max / float(max(orig_w, orig_h)))
    work_w = max(8, int(round(orig_w * scale)))
    work_h = max(8, int(round(orig_h * scale)))
    small = src.resize((work_w, work_h), Image.Resampling.BILINEAR)

    alpha = small.getchannel("A")
    alpha_bytes = alpha.tobytes()
    transparent_count = sum(1 for a in alpha_bytes if a < 180)
    has_alpha_bg = transparent_count >= int(work_w * work_h * 0.04)

    bg_rgb = (255, 255, 255)
    if has_alpha_bg:
        fg_mask = alpha.point(lambda a: 255 if a > 28 else 0)
    else:
        bg_rgb = _detect_border_background_rgb(small)
        bg_img = Image.new("RGB", (work_w, work_h), bg_rgb)
        diff = ImageChops.difference(small.convert("RGB"), bg_img)
        r_ch, g_ch, b_ch = diff.split()
        max_diff = ImageChops.lighter(ImageChops.lighter(r_ch, g_ch), b_ch)
        fg_mask = max_diff.point(lambda d: 255 if d > 28 else 0)

    fg_mask = fg_mask.filter(ImageFilter.MaxFilter(5))
    mask_data = fg_mask.tobytes()

    visited = bytearray(work_w * work_h)
    min_area = max(20, int(work_w * work_h * 0.0025))
    raw_boxes: List[Tuple[int, int, int, int]] = []

    for idx in range(work_w * work_h):
        if mask_data[idx] == 0 or visited[idx]:
            continue
        visited[idx] = 1
        q = deque([idx])
        min_x = max_x = idx % work_w
        min_y = max_y = idx // work_w
        area = 0

        while q:
            cur = q.popleft()
            area += 1
            cx = cur % work_w
            cy = cur // work_w
            if cx < min_x:
                min_x = cx
            if cx > max_x:
                max_x = cx
            if cy < min_y:
                min_y = cy
            if cy > max_y:
                max_y = cy

            for ny in range(max(0, cy - 1), min(work_h, cy + 2)):
                row_off = ny * work_w
                for nx in range(max(0, cx - 1), min(work_w, cx + 2)):
                    n_idx = row_off + nx
                    if not visited[n_idx] and mask_data[n_idx] > 0:
                        visited[n_idx] = 1
                        q.append(n_idx)

        if area >= min_area:
            raw_boxes.append((min_x, min_y, max_x + 1, max_y + 1))

    if not raw_boxes:
        return [(0, 0, orig_w, orig_h)], has_alpha_bg, bg_rgb

    merged = True
    while merged:
        merged = False
        out: List[Tuple[int, int, int, int]] = []
        used = [False] * len(raw_boxes)
        for i in range(len(raw_boxes)):
            if used[i]:
                continue
            ax0, ay0, ax1, ay1 = raw_boxes[i]
            for j in range(i + 1, len(raw_boxes)):
                if used[j]:
                    continue
                bx0, by0, bx1, by1 = raw_boxes[j]
                ix0, iy0 = max(ax0, bx0), max(ay0, by0)
                ix1, iy1 = min(ax1, bx1), min(ay1, by1)
                if ix1 > ix0 and iy1 > iy0:
                    inter = (ix1 - ix0) * (iy1 - iy0)
                    smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
                    if inter >= smaller * 0.45:
                        ax0, ay0 = min(ax0, bx0), min(ay0, by0)
                        ax1, ay1 = max(ax1, bx1), max(ay1, by1)
                        used[j] = True
                        merged = True
            out.append((ax0, ay0, ax1, ay1))
        raw_boxes = out

    avg_h = sum(b[3] - b[1] for b in raw_boxes) / float(len(raw_boxes))
    band_h = max(12.0, avg_h * 0.55)
    raw_boxes.sort(key=lambda b: (int(((b[1] + b[3]) * 0.5) // band_h), b[0]))
    raw_boxes = raw_boxes[:max_items]

    inv_scale_x = orig_w / float(work_w)
    inv_scale_y = orig_h / float(work_h)
    pad_px = max(2, int(round(max(orig_w, orig_h) * 0.008)))

    full_boxes: List[Tuple[int, int, int, int]] = []
    for x0, y0, x1, y1 in raw_boxes:
        fx0 = max(0, int(math.floor(x0 * inv_scale_x)) - pad_px)
        fy0 = max(0, int(math.floor(y0 * inv_scale_y)) - pad_px)
        fx1 = min(orig_w, int(math.ceil(x1 * inv_scale_x)) + pad_px)
        fy1 = min(orig_h, int(math.ceil(y1 * inv_scale_y)) + pad_px)
        if fx1 > fx0 and fy1 > fy0:
            full_boxes.append((fx0, fy0, fx1, fy1))

    return full_boxes or [(0, 0, orig_w, orig_h)], has_alpha_bg, bg_rgb


def _remove_solid_bg(crop: Image.Image, bg_rgb: Tuple[int, int, int]) -> Image.Image:
    """Softly key out a uniform background color around an extracted object."""
    bg_img = Image.new("RGB", crop.size, bg_rgb)
    diff = ImageChops.difference(crop.convert("RGB"), bg_img)
    r_ch, g_ch, b_ch = diff.split()
    max_diff = ImageChops.lighter(ImageChops.lighter(r_ch, g_ch), b_ch)

    def soft_ramp(d: int) -> int:
        if d <= 18:
            return 0
        if d >= 44:
            return 255
        return int(round((d - 18) * 255.0 / 26.0))

    alpha = max_diff.point(soft_ramp)
    out = crop.copy()
    out.putalpha(ImageChops.multiply(crop.getchannel("A"), alpha))
    return out


def _render_object_tile(
    piece: Image.Image, tile_px: int, tile_style: str
) -> Image.Image:
    margin = max(3, int(round(tile_px * (0.09 if tile_style == "border" else 0.05))))
    avail = max(8, tile_px - margin * 2)
    scale = min(avail / float(max(1, piece.width)), avail / float(max(1, piece.height)))
    nw = max(1, int(round(piece.width * scale)))
    nh = max(1, int(round(piece.height * scale)))
    resized = piece.resize((nw, nh), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (tile_px, tile_px), (0, 0, 0, 0))
    ox = (tile_px - nw) // 2
    oy = (tile_px - nh) // 2
    canvas.alpha_composite(resized, (ox, oy))

    if tile_style == "border":
        stroke = max(3, int(round(tile_px * 0.025)))
        ksize = stroke * 2 + 1
        expanded = canvas.getchannel("A").filter(ImageFilter.MaxFilter(ksize))
        expanded = expanded.filter(ImageFilter.GaussianBlur(radius=1.1))
        expanded = expanded.point(lambda a: min(255, int(a * 1.6)))
        outline = Image.new("RGBA", (tile_px, tile_px), (255, 255, 255, 0))
        outline.putalpha(expanded)
        outline.alpha_composite(canvas)
        canvas = outline
    elif tile_style == "rounded":
        canvas = _apply_grid_tile_style(canvas, "rounded")

    return _ensure_non_empty_alpha(canvas)


def slice_image(raw_bytes: bytes, cfg: SliceConfig) -> SliceResult:
    src = load_clean_rgba(raw_bytes)
    tile_px = tile_size_for_pack(cfg.pack_type)
    limit = max_tiles_for_pack(cfg.pack_type)

    if cfg.slice_mode == "objects":
        boxes, has_alpha_bg, bg_rgb = _extract_object_boxes(src, limit)
        tiles_webp: List[bytes] = []
        for x0, y0, x1, y1 in boxes:
            crop = src.crop((x0, y0, x1, y1))
            if not has_alpha_bg:
                crop = _remove_solid_bg(crop, bg_rgb)
            bbox_alpha = crop.getchannel("A").getbbox()
            if bbox_alpha:
                crop = crop.crop(bbox_alpha)
            tile_img = _render_object_tile(crop, tile_px, cfg.tile_style)
            tiles_webp.append(encode_webp_under_limit(tile_img))

        prev_max = 960
        pscale = min(1.0, prev_max / float(max(src.width, src.height)))
        pw = max(64, int(round(src.width * pscale)))
        ph = max(64, int(round(src.height * pscale)))
        preview = _make_checkerboard(pw, ph)
        preview.alpha_composite(src.resize((pw, ph), Image.Resampling.LANCZOS))
        draw = ImageDraw.Draw(preview)

        for idx, (x0, y0, x1, y1) in enumerate(boxes, start=1):
            rx0 = int(round(x0 * pscale))
            ry0 = int(round(y0 * pscale))
            rx1 = min(pw - 1, int(round(x1 * pscale)))
            ry1 = min(ph - 1, int(round(y1 * pscale)))
            draw.rectangle((rx0, ry0, rx1, ry1), outline=(16, 185, 129, 245), width=3)
            badge_text = str(idx)
            bw = max(20, 10 + len(badge_text) * 8)
            bh = 20
            draw.rounded_rectangle(
                (rx0, ry0, min(pw - 1, rx0 + bw), min(ph - 1, ry0 + bh)),
                radius=5,
                fill=(17, 24, 39, 225),
            )
            draw.text((rx0 + 5, ry0 + 3), badge_text, fill=(255, 255, 255, 255))

        pbuf = io.BytesIO()
        preview.convert("RGB").save(pbuf, format="PNG", optimize=True)
        return SliceResult(
            tiles_webp=tiles_webp,
            preview_png=pbuf.getvalue(),
            cols=0,
            rows=0,
            count=len(tiles_webp),
            slice_mode="objects",
            tile_px=tile_px,
        )

    cols, rows = resolve_grid_dimensions(
        src.width, src.height, cfg.cols, cfg.rows, cfg.pack_type
    )
    full_canvas = _prepare_grid_canvas(src, cols, rows, tile_px, cfg.fit_mode)

    tiles_webp = []
    for r in range(rows):
        y0 = r * tile_px
        for c in range(cols):
            x0 = c * tile_px
            raw_tile = full_canvas.crop((x0, y0, x0 + tile_px, y0 + tile_px))
            styled = _apply_grid_tile_style(raw_tile, cfg.tile_style)
            tiles_webp.append(encode_webp_under_limit(styled))

    prev_max = 960
    pscale = min(1.0, prev_max / float(max(full_canvas.width, full_canvas.height)))
    pw = max(64, int(round(full_canvas.width * pscale)))
    ph = max(64, int(round(full_canvas.height * pscale)))
    preview = _make_checkerboard(pw, ph)
    preview.alpha_composite(full_canvas.resize((pw, ph), Image.Resampling.LANCZOS))
    draw = ImageDraw.Draw(preview)

    cell_w = pw / float(cols)
    cell_h = ph / float(rows)
    for c in range(1, cols):
        x = int(round(c * cell_w))
        _draw_dashed_line(draw, (x, 0), (x, ph - 1))
    for r in range(1, rows):
        y = int(round(r * cell_h))
        _draw_dashed_line(draw, (0, y), (pw - 1, y))

    if cols * rows <= 64 and cell_w >= 34 and cell_h >= 28:
        num = 1
        for r in range(rows):
            for c in range(cols):
                bx = int(round(c * cell_w)) + 5
                by = int(round(r * cell_h)) + 5
                txt = str(num)
                tw = 8 + len(txt) * 7
                draw.rounded_rectangle(
                    (bx, by, bx + tw, by + 17),
                    radius=4,
                    fill=(17, 24, 39, 190),
                )
                draw.text((bx + 4, by + 2), txt, fill=(255, 255, 255, 245))
                num += 1

    pbuf = io.BytesIO()
    preview.convert("RGB").save(pbuf, format="PNG", optimize=True)
    return SliceResult(
        tiles_webp=tiles_webp,
        preview_png=pbuf.getvalue(),
        cols=cols,
        rows=rows,
        count=len(tiles_webp),
        slice_mode="grid",
        tile_px=tile_px,
    )


def build_tiles_zip(tiles_webp: List[bytes], cols: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, data in enumerate(tiles_webp, start=1):
            if cols > 0:
                r = (idx - 1) // cols + 1
                c = (idx - 1) % cols + 1
                fname = f"sticker_{idx:03d}_r{r}_c{c}.webp"
            else:
                fname = f"sticker_{idx:03d}.webp"
            zf.writestr(fname, data)
    return buf.getvalue()
