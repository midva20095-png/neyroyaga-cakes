import os

from PIL import Image, ImageChops
from rectpack import newPacker
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
MARGIN_MM = 10          # поля листа
GAP_MM = 4              # зазор между элементами для вырезания

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


class A4LayoutEngine:
    def __init__(self):
        self.gap_px = round(GAP_MM * MM_PX)
        self.margin_px = round(MARGIN_MM * MM_PX)
        self.work_w_px = round((PAGE_W_MM - 2 * MARGIN_MM) * MM_PX)
        self.work_h_px = round((PAGE_H_MM - 2 * MARGIN_MM) * MM_PX)

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

    def prepare_image(self, img_input, size_type: str, target_size_cm: float):
        """Trim + пропорциональный ресайз под сантиметры (300 DPI)."""
        img = Image.open(img_input) if isinstance(img_input, str) else img_input
        img = self._trim(img.convert("RGBA"))
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

    def generate_pdf(self, items_config: list, output_pdf_path: str) -> int:
        """items_config: [{"image": PIL.Image, "type": "height|width|diameter", "size": см, "copies": n}]
        Возвращает число листов A4."""
        packer = newPacker(rotation=True)
        groups, owners = [], []          # уникальные картинки; owners[rid] -> индекс картинки
        max_w = (PAGE_W_MM - 2 * MARGIN_MM - GAP_MM) / 10
        max_h = (PAGE_H_MM - 2 * MARGIN_MM - GAP_MM) / 10

        for n, cfg in enumerate(items_config, 1):
            img, w, h = self.prepare_image(cfg["image"], cfg["type"], cfg["size"])
            bw, bh = w + self.gap_px, h + self.gap_px
            fits = (bw <= self.work_w_px and bh <= self.work_h_px) or (bh <= self.work_w_px and bw <= self.work_h_px)
            if not fits:
                raise ValueError(
                    f"Картинка №{n} ({w / CM_PX:.1f}×{h / CM_PX:.1f} см) не помещается на A4: "
                    f"максимум {max_w:.1f}×{max_h:.1f} см. Уменьшите размер."
                )
            groups.append({"img": img, "w": w, "h": h, "bw": bw, "bh": bh})
            for _ in range(max(1, int(cfg.get("copies", 1)))):
                packer.add_rect(bw, bh, rid=len(owners))
                owners.append(len(groups) - 1)

        # add_bin(count=1) по умолчанию даёт ОДИН лист — всё, что не влезло, rectpack молча выбрасывал.
        packer.add_bin(self.work_w_px, self.work_h_px, count=len(owners))
        packer.pack()
        rects = packer.rect_list()
        if len(rects) != len(owners):
            raise ValueError("Не удалось разместить все элементы на листах A4. Уменьшите размеры.")

        pages = {}
        for b, x, y, w, h, rid in rects:
            pages.setdefault(b, []).append((x, y, w, h, rid))

        c = canvas.Canvas(output_pdf_path, pagesize=A4)
        c.setTitle("Sugar print layout A4")
        page_h = A4[1]
        for i, b in enumerate(sorted(pages)):
            if i:
                c.showPage()
            self._draw_service_marks(c)
            for x, y, w, h, rid in pages[b]:
                g = groups[owners[rid]]
                rotated = (w, h) != (g["bw"], g["bh"])   # rectpack повернул ячейку на 90°
                iw, ih = (g["h"], g["w"]) if rotated else (g["w"], g["h"])
                key = "rot" if rotated else "std"
                if key not in g:                          # одна картинка = один объект на все копии
                    g[key] = ImageReader(g["img"].transpose(Image.Transpose.ROTATE_90) if rotated else g["img"])
                x_pt = (self.margin_px + x + self.gap_px / 2) * PX_PT
                y_pt = page_h - (self.margin_px + y + h - self.gap_px / 2) * PX_PT
                c.drawImage(g[key], x_pt, y_pt, iw * PX_PT, ih * PX_PT, mask="auto")
        c.save()
        return len(pages)

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