import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

DPI = 300
MM_PX = DPI / 25.4      # пикселей в 1 мм
CM_PX = DPI / 2.54      # пикселей в 1 см
PX_PT = 72.0 / DPI      # пиксель -> пункт ReportLab
PAGE_W_MM, PAGE_H_MM = 210, 297
MARGIN_MM = 10          # поля сверху и по бокам
MARGIN_BOTTOM_MM = 18   # «мёртвая зона» принтера снизу: туда элементы не ставим
GAP_MM = 4              # зазор между элементами для вырезания
OUTLINE_MM = 2.5        # белая обводка под вырезание ножницами

# Helvetica в ReportLab не содержит кириллицы (вместо букв — чёрные квадраты), поэтому ищем TTF.
# Свой шрифт: положите DejaVuSans.ttf в папку fonts/ рядом с кодом или задайте PDF_FONT_PATH.
_HERE = os.path.dirname(os.path.abspath(__file__))
_FONTS = [
    os.getenv("PDF_FONT_PATH", ""),
    os.path.join(_HERE, "fonts", "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial.ttf",
]


def _load_font():
    for path in _FONTS:
        if path and os.path.isfile(path):
            try:
                pdfmetrics.registerFont(TTFont("LayoutFont", path))
                return "LayoutFont", True
            except Exception:
                continue
    return "Helvetica", False   # без кириллицы: служебные надписи будут на английском


FONT, HAS_CYRILLIC = _load_font()


def pack_rects(rects, bin_w, bin_h):
    """Раскладка прямоугольников по листам (алгоритм максимальных свободных областей).
    rects: [(ширина, высота, id)] -> [(номер листа, x, y, ширина, высота, id)].
    Любой элемент гарантированно лежит внутри листа: координаты берутся из свободной области,
    которая целиком помещается на лист. Повороты на 90° разрешены."""
    placed, bins = [], []                       # bins: список свободных областей каждого листа

    def fit(free, w, h):
        return w <= free[2] and h <= free[3]

    def split(free, x, y, w, h):
        """Разрезает свободную область вокруг занятого места."""
        fx, fy, fw, fh = free
        if x >= fx + fw or x + w <= fx or y >= fy + fh or y + h <= fy:
            return [free]                       # не пересекаются
        out = []
        if y > fy:
            out.append((fx, fy, fw, y - fy))                       # сверху
        if y + h < fy + fh:
            out.append((fx, y + h, fw, fy + fh - y - h))           # снизу
        if x > fx:
            out.append((fx, fy, x - fx, fh))                       # слева
        if x + w < fx + fw:
            out.append((x + w, fy, fx + fw - x - w, fh))           # справа
        return out

    def prune(free_list):
        """Убирает области, целиком лежащие внутри других."""
        keep = []
        for i, a in enumerate(free_list):
            if a[2] <= 0 or a[3] <= 0:
                continue
            inside = any(
                i != j and a[0] >= b[0] and a[1] >= b[1]
                and a[0] + a[2] <= b[0] + b[2] and a[1] + a[3] <= b[1] + b[3]
                and (a[2] * a[3] < b[2] * b[3] or i > j)
                for j, b in enumerate(free_list)
            )
            if not inside:
                keep.append(a)
        return keep

    order = sorted(rects, key=lambda r: (-max(r[0], r[1]), -r[0] * r[1]))
    for w, h, rid in order:
        best = None                              # (лишний край, номер листа, x, y, w, h)
        for b, free_list in enumerate(bins):
            for fx, fy, fw, fh in free_list:
                for cw, ch in ((w, h), (h, w)) if w != h else ((w, h),):
                    if fit((fx, fy, fw, fh), cw, ch):
                        score = (min(fw - cw, fh - ch), max(fw - cw, fh - ch))
                        if best is None or score < best[0]:
                            best = (score, b, fx, fy, cw, ch)
        if best is None:                         # не влезло никуда — заводим новый лист
            if w > bin_w or h > bin_h:
                if h > bin_w or w > bin_h:
                    raise ValueError("элемент не помещается на лист")
                w, h = h, w
            bins.append([(0, 0, bin_w, bin_h)])
            best = (None, len(bins) - 1, 0, 0, w, h)
        _, b, x, y, cw, ch = best
        assert x >= 0 and y >= 0 and x + cw <= bin_w and y + ch <= bin_h, "элемент вышел за лист"
        placed.append((b, x, y, cw, ch, rid))
        new_free = []
        for free in bins[b]:
            new_free.extend(split(free, x, y, cw, ch))
        bins[b] = prune(new_free)
    return placed


def _uniform_border(rgb, tol=10, need=0.97):
    """Проверяет, что рамка картинки почти одного цвета (ровный фон)."""
    w, h = rgb.size
    px = rgb.load()
    step = max(1, min(w, h) // 60)
    pts = [(x, 0) for x in range(0, w, step)] + [(x, h - 1) for x in range(0, w, step)]
    pts += [(0, y) for y in range(0, h, step)] + [(w - 1, y) for y in range(0, h, step)]
    cols = [px[x, y] for x, y in pts]
    bg = tuple(sorted(c[i] for c in cols)[len(cols) // 2] for i in range(3))
    same = sum(1 for c in cols if max(abs(c[i] - bg[i]) for i in range(3)) <= tol)
    return same / len(cols) >= need


class A4LayoutEngine:
    def __init__(self):
        self.gap_px = round(GAP_MM * MM_PX)
        self.margin_px = round(MARGIN_MM * MM_PX)
        self.work_w_px = round((PAGE_W_MM - 2 * MARGIN_MM) * MM_PX)
        self.work_h_px = round((PAGE_H_MM - MARGIN_MM - MARGIN_BOTTOM_MM) * MM_PX)

    @staticmethod
    def _trim(img, tol=18):
        """Обрезает пустые поля. Есть прозрачность — режем по альфа-каналу;
        фон сплошной (так отдаёт Gemini: без альфы) — по цвету фона в углу."""
        alpha = img.getchannel("A")
        if alpha.getextrema()[0] < 255:
            box = alpha.point(lambda v: 255 if v > 8 else 0).getbbox()
        else:
            rgb = img.convert("RGB")
            diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, rgb.getpixel((0, 0))))
            box = diff.convert("L").point(lambda v: 255 if v > tol else 0).getbbox()
        return img.crop(box) if box else img

    @staticmethod
    def drop_background(img, tol=26):
        """Убирает ровный фон (белый у картинок Gemini) — иначе кондитер печатает лишнюю бумагу.
        Берётся только фон, связанный с краями листа: белые участки внутри рисунка остаются.
        Сделано на чистом Pillow, без дополнительных библиотек."""
        if img.getchannel("A").getextrema()[0] < 250:
            return img                                    # прозрачность уже есть, не трогаем
        rgb = img.convert("RGB")
        # Фон убираем только у картинок с ровным фоном (рисунки, клипарт).
        # У фотографии фон неровный: раньше на ней выедались белые дыры — теперь она не трогается.
        if not _uniform_border(rgb):
            return img
        w, h = rgb.size
        px = rgb.load()
        corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
        bg = tuple(sorted(c[i] for c in corners)[len(corners) // 2] for i in range(3))

        # маска "похоже на фон": берём максимальное расхождение по каналам
        diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg)).split()
        dmax = ImageChops.lighter(ImageChops.lighter(diff[0], diff[1]), diff[2])
        mask = dmax.point(lambda v: 255 if v <= tol else 0)

        # рамка в 1 пиксель + заливка из угла: фоном считаем только область, достающую до краёв
        framed = Image.new("L", (w + 2, h + 2), 255)
        framed.paste(mask, (1, 1))
        ImageDraw.floodfill(framed, (0, 0), 128, thresh=0)
        outside = framed.crop((1, 1, w + 1, h + 1)).point(lambda v: 0 if v == 128 else 255)

        out = img.copy()
        out.putalpha(ImageChops.darker(out.getchannel("A"), outside))
        return out

    @staticmethod
    def add_outline(img, width_px: int, color=(255, 255, 255, 255)):
        """Белый кант вокруг картинки: по нему кондитер режет ножницами, не ломая мелкие детали."""
        if width_px < 1:
            return img
        pad = width_px + 2
        base = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
        base.paste(img, (pad, pad))
        mask = base.getchannel("A").point(lambda v: 255 if v > 40 else 0)
        grown, left = mask, width_px
        while left > 0:                                   # расширяем силуэт шагами по 3 px: это быстро
            step = min(3, left)
            grown = grown.filter(ImageFilter.MaxFilter(2 * step + 1))
            left -= step
        halo = Image.new("RGBA", base.size, color)
        halo.putalpha(grown)
        halo.alpha_composite(base)                        # картинка поверх канта
        return halo.crop(halo.getchannel("A").getbbox())

    @staticmethod
    def to_circle(img, diameter_px: int, fill: bool = None):
        """Круглый топпер заданного диаметра.
        fill=True  — фото заполняет круг целиком (обрезается по центру), без белых углов;
        fill=False — фигурка вписывается внутрь круга на белом поле (запас под ножницы)."""
        side = max(1, diameter_px)
        if fill is None:                                          # фото заполняем, вырезанную фигурку вписываем
            fill = img.getchannel("A").getextrema()[0] >= 250
        disc = Image.new("RGBA", (side, side), (255, 255, 255, 255))
        if fill:
            k = max(side / img.width, side / img.height)          # покрываем круг целиком
            big = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.Resampling.LANCZOS)
            left, top = (big.width - side) // 2, (big.height - side) // 2
            disc.alpha_composite(big.crop((left, top, left + side, top + side)))
        else:
            k = min(side / img.width, side / img.height) * 0.88
            fitted = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.Resampling.LANCZOS)
            disc.alpha_composite(fitted, ((side - fitted.width) // 2, (side - fitted.height) // 2))
        big = Image.new("L", (side * 4, side * 4), 0)            # сглаженный край круга
        ImageDraw.Draw(big).ellipse((0, 0, side * 4 - 1, side * 4 - 1), fill=255)
        disc.putalpha(big.resize((side, side), Image.Resampling.LANCZOS))
        # тонкая серая линия реза по краю круга: видно, где резать, и что это именно круг
        line = Image.new("RGBA", (side * 4, side * 4), (0, 0, 0, 0))
        ImageDraw.Draw(line).ellipse((6, 6, side * 4 - 7, side * 4 - 7),
                                     outline=(120, 120, 120, 255), width=max(4, round(side * 4 * 0.004)))
        disc.alpha_composite(line.resize((side, side), Image.Resampling.LANCZOS))
        return disc

    def prepare_image(self, img_input, size_type: str, target_size_cm: float, outline_mm: float = 0.0,
                      circle: bool = False, raw: bool = False):
        """Trim + пропорциональный ресайз под сантиметры (300 DPI).
        circle=True — фигурка вписывается в круглый топпер заданного диаметра.
        raw=True — готовый макет из песочницы: фон не убираем и поля не режем, иначе собьётся размер."""
        img = Image.open(img_input) if isinstance(img_input, str) else img_input
        img = img.convert("RGBA")
        if not raw:
            img = self._trim(self.drop_background(img))
        if circle:
            # у круглого топпера линия реза — сам круг, поэтому внешний кант не добавляем:
            # иначе диаметр вырастал (6 -> 6.5 см) и на лист влезало меньше штук
            target = max(1, round(target_size_cm * CM_PX))
            disc = self.to_circle(img, target)
            return disc, target, target
        if outline_mm > 0:
            img = self.add_outline(img, round(outline_mm * MM_PX))
        w, h = img.size
        target = max(1, round(target_size_cm * CM_PX))
        if size_type == "height":
            scale = target / h
        elif size_type == "width":
            scale = target / w
        elif size_type == "diameter":
            scale = target / max(w, h)
        else:
            raise ValueError(f"Неизвестный тип размера: {size_type}")
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        return img.resize((nw, nh), Image.Resampling.LANCZOS), nw, nh

    def _layout(self, items_config: list, sheet_h_mm: float):
        """Раскладывает элементы по листам. Возвращает (страницы, группы, владельцы)."""
        rects, groups, owners = [], [], []   # уникальные картинки; owners[rid] -> индекс картинки
        # лист для укладки берём на один зазор больше: крайним элементам зазор по внешней
        # стороне не нужен, иначе в ряд не влезает лишняя колонка (12 кругов по 6 см уходили на 2 листа)
        work_h_px = round((sheet_h_mm - MARGIN_MM - MARGIN_BOTTOM_MM) * MM_PX)
        bin_w, bin_h = self.work_w_px + self.gap_px, work_h_px + self.gap_px
        max_w = (PAGE_W_MM - 2 * MARGIN_MM) / 10
        max_h = (sheet_h_mm - MARGIN_MM - MARGIN_BOTTOM_MM) / 10

        for n, cfg in enumerate(items_config, 1):
            img, w, h = self.prepare_image(cfg["image"], cfg["type"], cfg["size"],
                                           float(cfg.get("outline", 0) or 0), bool(cfg.get("circle")),
                                           bool(cfg.get("raw")))
            bw, bh = w + self.gap_px, h + self.gap_px
            fits = (bw <= bin_w and bh <= bin_h) or (bh <= bin_w and bw <= bin_h)
            if not fits:
                raise ValueError(
                    f"Картинка №{n} ({w / CM_PX:.1f}×{h / CM_PX:.1f} см) не помещается на A4: "
                    f"максимум {max_w:.1f}×{max_h:.1f} см. Уменьшите размер."
                )
            groups.append({"img": img, "w": w, "h": h, "bw": bw, "bh": bh})
            for _ in range(max(1, int(cfg.get("copies", 1)))):
                rects.append((bw, bh, len(owners)))
                owners.append(len(groups) - 1)

        result = pack_rects(rects, bin_w, bin_h)
        if len(result) != len(owners):
            raise ValueError("Не удалось разместить все элементы на листах A4. Уменьшите размеры.")

        pages = {}
        for b, x, y, w, h, rid in result:
            pages.setdefault(b, []).append((x, y, w, h, rid))
        return pages, groups, owners

    def generate_pdf(self, items_config: list, output_pdf_path: str, sheet_h_mm: float = PAGE_H_MM) -> int:
        """items_config: [{"image": PIL.Image, "type": "height|width|diameter", "size": см, "copies": n}]
        Возвращает число листов."""
        pages, groups, owners = self._layout(items_config, sheet_h_mm)
        c = canvas.Canvas(output_pdf_path, pagesize=(PAGE_W_MM * mm, sheet_h_mm * mm))
        c.setTitle("Sugar print layout")
        page_h = sheet_h_mm * mm
        for i, b in enumerate(sorted(pages)):
            if i:
                c.showPage()
            self._draw_service_marks(c)
            for x, y, w, h, rid in pages[b]:
                g = groups[owners[rid]]
                rotated = (w, h) != (g["bw"], g["bh"])   # ячейку положили боком
                iw, ih = (g["h"], g["w"]) if rotated else (g["w"], g["h"])
                key = "rot" if rotated else "std"
                if key not in g:                          # одна картинка = один объект на все копии
                    g[key] = ImageReader(g["img"].transpose(Image.Transpose.ROTATE_90) if rotated else g["img"])
                x_pt = (self.margin_px + x + self.gap_px / 2) * PX_PT
                y_pt = page_h - (self.margin_px + y + h - self.gap_px / 2) * PX_PT
                c.drawImage(g[key], x_pt, y_pt, iw * PX_PT, ih * PX_PT, mask="auto")
        c.save()
        return len(pages)

    def generate_images(self, items_config: list, out_dir, stem: str, sheet_h_mm: float = PAGE_H_MM) -> list:
        """Та же раскладка, но листы сохраняются в JPG 300 DPI. Возвращает пути к файлам."""
        pages, groups, owners = self._layout(items_config, sheet_h_mm)
        sheet_w_px, sheet_h_px = round(PAGE_W_MM * MM_PX), round(sheet_h_mm * MM_PX)
        paths = []
        for i, b in enumerate(sorted(pages), 1):
            sheet = Image.new("RGB", (sheet_w_px, sheet_h_px), (255, 255, 255))
            for x, y, w, h, rid in pages[b]:
                g = groups[owners[rid]]
                rotated = (w, h) != (g["bw"], g["bh"])
                img = g["img"].transpose(Image.Transpose.ROTATE_90) if rotated else g["img"]
                px = round(self.margin_px + x + self.gap_px / 2)
                py = round(self.margin_px + y + self.gap_px / 2)
                sheet.paste(img, (px, py), img.getchannel("A"))
            self._draw_marks_image(sheet)
            path = f"{out_dir}/{stem}_{i}.jpg" if len(pages) > 1 else f"{out_dir}/{stem}.jpg"
            sheet.save(path, "JPEG", quality=95, dpi=(DPI, DPI))
            paths.append(path)
        return paths

    @staticmethod
    def fit_exact(img, w_mm, h_mm, circle):
        """Картинка точного размера: круг обрезается по центру, прямоугольник вписывается."""
        img = img.convert("RGBA")
        tw, th = round(w_mm * MM_PX), round(h_mm * MM_PX)
        if circle:
            side = min(img.size)
            l, t = (img.width - side) // 2, (img.height - side) // 2
            fitted = img.crop((l, t, l + side, t + side)).resize((tw, th), Image.Resampling.LANCZOS)
            mask = Image.new("L", (tw * 4, th * 4), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, tw * 4 - 1, th * 4 - 1), fill=255)
            fitted.putalpha(mask.resize((tw, th), Image.Resampling.LANCZOS))
            return fitted
        k = min(tw / img.width, th / img.height)
        nw, nh = max(1, round(img.width * k)), max(1, round(img.height * k))
        out = Image.new("RGBA", (tw, th), (255, 255, 255, 255))
        out.alpha_composite(img.resize((nw, nh), Image.Resampling.LANCZOS), ((tw - nw) // 2, (th - nh) // 2))
        return out

    def export_jpg(self, img, w_mm, h_mm, circle, output_path, sheet_h_mm=PAGE_H_MM, dpi=DPI):
        """Лист для печати в JPG 300 DPI: макет точного размера по центру листа —
        то же, что и PDF, только картинкой. Круг остаётся кругом, лишнего не добавляем."""
        if w_mm > PAGE_W_MM or h_mm > sheet_h_mm:
            raise ValueError(f"Макет {w_mm:g}×{h_mm:g} мм не помещается на лист "
                             f"{PAGE_W_MM:g}×{sheet_h_mm:g} мм")
        fitted = self.fit_exact(img, w_mm, h_mm, circle)
        sheet = Image.new("RGB", (round(PAGE_W_MM * MM_PX), round(sheet_h_mm * MM_PX)), (255, 255, 255))
        sheet.paste(fitted, ((sheet.width - fitted.width) // 2, (sheet.height - fitted.height) // 2),
                    fitted.getchannel("A"))
        self._draw_marks_image(sheet)
        sheet.save(output_path, "JPEG", quality=95, dpi=(dpi, dpi))
        return 1

    @staticmethod
    def _draw_marks_image(sheet):
        """Линейка 100 мм внизу листа — чтобы проверить масштаб печати, как в PDF."""
        d = ImageDraw.Draw(sheet)
        y = sheet.height - round(8 * MM_PX)
        x0 = round(MARGIN_MM * MM_PX)
        d.line([(x0, y), (x0 + round(100 * MM_PX), y)], fill=(120, 120, 120), width=3)
        for i in range(11):
            x = x0 + round(i * 10 * MM_PX)
            d.line([(x, y), (x, y - round((2 if i % 5 == 0 else 1.2) * MM_PX))], fill=(120, 120, 120), width=3)

    def export_exact(self, img, w_mm, h_mm, circle, output_pdf_path, sheet_h_mm=PAGE_H_MM):
        """Один макет точного размера (в мм) по центру A4. Круг — с круглой обрезкой, остальное — вписывается без искажений."""
        if w_mm > PAGE_W_MM or h_mm > sheet_h_mm:
            raise ValueError(f"Макет {w_mm:g}×{h_mm:g} мм не помещается на лист "
                             f"{PAGE_W_MM:g}×{sheet_h_mm:g} мм")
        fitted = self.fit_exact(img, w_mm, h_mm, circle)
        page = (PAGE_W_MM * mm, sheet_h_mm * mm)
        c = canvas.Canvas(output_pdf_path, pagesize=page)
        c.setTitle("Sugar print exact size")
        x, y = (page[0] - w_mm * mm) / 2, (page[1] - h_mm * mm) / 2
        c.drawImage(ImageReader(fitted), x, y, w_mm * mm, h_mm * mm, mask="auto")
        if y >= MARGIN_BOTTOM_MM * mm:        # служебная линейка — только если есть место в поле листа
            self._draw_service_marks(c)
        c.save()

    def _draw_service_marks(self, c):
        """Памятка и линейка 100 мм — целиком в нижнем поле, чтобы не задевать картинки."""
        c.saveState()
        c.setFillColorRGB(0.3, 0.3, 0.3)
        c.setStrokeColorRGB(0.3, 0.3, 0.3)
        c.setLineWidth(0.4)
        c.setFont(FONT, 7)
        c.drawString(
            MARGIN_MM * mm, 6.5 * mm,
            "Печатать в масштабе 100% («Фактический размер»), НЕ «по размеру страницы»" if HAS_CYRILLIC
            else "Print at 100% / Actual size. Do NOT use 'Fit to page'.",
        )
        y = 3.5 * mm
        c.line(MARGIN_MM * mm, y, (MARGIN_MM + 100) * mm, y)
        for i in range(11):
            x = (MARGIN_MM + i * 10) * mm
            c.line(x, y, x, y + (2 if i % 5 == 0 else 1.2) * mm)
        c.drawString(
            (MARGIN_MM + 102) * mm, 3 * mm,
            "100 мм: проверьте линейкой" if HAS_CYRILLIC else "100 mm: check with a ruler",
        )
        c.restoreState()
