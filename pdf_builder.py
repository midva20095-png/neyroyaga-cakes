import os
from PIL import Image
from rectpack import newPacker
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

# Константы печати (300 DPI)
DPI = 300
CM_TO_PX = DPI / 2.54  # ~118.11 px на 1 см

# Параметры листа A4 и отступов (в миллиметрах)
PAGE_WIDTH_MM = 210
PAGE_HEIGHT_MM = 297
MARGIN_MM = 10         # Поля со всех сторон (10 мм)
GAP_MM = 4             # Зазор между элементами для вырезания (4 мм)

def pt(mm_val):
    """Конвертация миллиметров в пункты ReportLab (1 pt = 1/72 дюйма)"""
    return mm_val * mm

def px_to_pt(px_val):
    """Конвертация пикселей (при 300 DPI) в пункты ReportLab"""
    return px_val * (72.0 / DPI)

class A4LayoutEngine:
    def __init__(self):
        self.page_w_px = int(PAGE_WIDTH_MM * (DPI / 25.4))
        self.page_h_px = int(PAGE_HEIGHT_MM * (DPI / 25.4))
        self.margin_px = int(MARGIN_MM * (DPI / 25.4))
        self.gap_px = int(GAP_MM * (DPI / 25.4))
        
        self.work_w_px = self.page_w_px - (self.margin_px * 2)
        self.work_h_px = self.page_h_px - (self.margin_px * 2)

    def prepare_image(self, img_input, size_type: str, target_size_cm: float):
        """Обрезка прозрачных краев (trim) и пропорциональный ресайз под сантиметры"""
        if isinstance(img_input, str):
            img = Image.open(img_input).convert("RGBA")
        else:
            img = img_input.convert("RGBA")
        
        # 1. Trim (удаление пустого прозрачного фона)
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
            
        orig_w, orig_h = img.size
        target_px = int(target_size_cm * CM_TO_PX)
        
        # 2. Масштабирование по типу размера
        if size_type == 'height':
            new_h = target_px
            new_w = int(orig_w * (target_px / orig_h))
        elif size_type == 'width':
            new_w = target_px
            new_h = int(orig_h * (target_px / orig_w))
        elif size_type == 'diameter':
            max_side = max(orig_w, orig_h)
            scale = target_px / max_side
            new_w = int(orig_w * scale)
            new_h = int(orig_h * scale)
        else:
            raise ValueError(f"Неизвестный тип размера: {size_type}")
            
        img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        return img_resized, new_w, new_h

    def generate_pdf(self, items_config: list, output_pdf_path: str):
        """
        items_config: список словарей вида:
        [
            {"image": PIL.Image, "type": "height", "size": 10.0, "copies": 2},
            ...
        ]
        """
        packer = newPacker(rotation=True)
        processed_items = []
        item_id_counter = 0
        
        for cfg in items_config:
            img_obj, w_px, h_px = self.prepare_image(cfg['image'], cfg['type'], cfg['size'])
            
            box_w = w_px + self.gap_px
            box_h = h_px + self.gap_px
            
            for _ in range(cfg.get('copies', 1)):
                packer.add_rect(box_w, box_h, item_id_counter)
                processed_items.append({
                    "id": item_id_counter,
                    "img": img_obj,
                    "w_px": w_px,
                    "h_px": h_px,
                    "box_w": box_w,
                    "box_h": box_h
                })
                item_id_counter += 1

        packer.add_bin(self.work_w_px, self.work_h_px)
        packer.pack()
        
        bins = packer.bin_list()
        if not bins:
            raise ValueError("Элементы не помещаются ни на один лист A4 с учетом полей и отступов!")

        c = canvas.Canvas(output_pdf_path, pagesize=A4)
        rects = packer.rect_list()
        
        sheets_data = {}
        for r in rects:
            b_idx, rx, ry, rw, rh, rid = r
            if b_idx not in sheets_data:
                sheets_data[b_idx] = []
            sheets_data[b_idx].append((rx, ry, rid))

        for b_idx, placed_rects in sheets_data.items():
            if b_idx > 0:
                c.showPage()
                
            # Служебные метки на каждом листе
            self._draw_service_marks(c)

            # Отрисовка картинок
            for rx, ry, rid, in placed_rects:
                item = next(it for it in processed_items if it["id"] == rid)
                
                pos_x_pt = px_to_pt(self.margin_px + rx + (self.gap_px / 2))
                pos_y_pt = pt(PAGE_HEIGHT_MM) - px_to_pt(self.margin_px + ry + rh) + px_to_pt(self.gap_px / 2)
                
                w_pt = px_to_pt(item["w_px"])
                h_pt = px_to_pt(item["h_pt"])
                
                temp_img_path = f"_temp_{rid}.png"
                item["img"].save(temp_img_path, "PNG")
                
                c.drawImage(temp_img_path, pos_x_pt, pos_y_pt, width=w_pt, height=h_pt, mask='auto')
                
                if os.path.exists(temp_img_path):
                    os.remove(temp_img_path)

        c.save()
        return len(sheets_data)

    def _draw_service_marks(self, c):
        """Контрольная линейка 10 мм и памятка для печати в масштабе 100%"""
        c.saveState()
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.3, 0.3, 0.3)
        
        # Инструкция для кассира / мастера
        c.drawString(pt(MARGIN_MM), pt(5), "Печатать в масштабе 100% / Фактический размер. НЕ 'по размеру страницы'.")
        
        # Контрольная линейка 10 мм
        ruler_x = pt(PAGE_WIDTH_MM - MARGIN_MM - 20)
        ruler_y = pt(8)
        c.rect(ruler_x, ruler_y, pt(10), pt(3), stroke=1, fill=1)
        c.drawString(ruler_x, ruler_y + pt(4), "10 мм")
        
        c.restoreState()
