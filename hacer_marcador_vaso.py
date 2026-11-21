import cv2
import numpy as np
from common import ROOT

DPI = 300
px_mm = DPI / 25.4
LADO_MM = 45.0       # cuadro negro exacto
MARGEN_MM = 10.0     # margen blanco obligatorio alrededor (quiet zone)
ID_MARCADOR = 40

lado_px = int(round(LADO_MM * px_mm))
margen_px = int(round(MARGEN_MM * px_mm))
box_px = lado_px + 2 * margen_px

dic = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
marker = cv2.aruco.generateImageMarker(dic, ID_MARCADOR, lado_px)

# Hoja A4 a 300 DPI (210 x 297 mm)
a4_w, a4_h = int(round(210 * px_mm)), int(round(297 * px_mm))
page = np.full((a4_h, a4_w), 255, dtype=np.uint8)

for cx in [int(a4_w * 0.30), int(a4_w * 0.70)]:
    cy = int(a4_h * 0.28)
    x0, y0 = cx - box_px // 2, cy - box_px // 2
    cv2.rectangle(page, (x0, y0), (x0 + box_px, y0 + box_px), 160, 2)  # guía de corte (65x65 mm)
    mx0, my0 = x0 + margen_px, y0 + margen_px
    page[my0:my0 + lado_px, mx0:mx0 + lado_px] = marker

    cv2.putText(page, f"DICT_4X4_50 | ID {ID_MARCADOR} | {LADO_MM:.0f}x{LADO_MM:.0f} mm",
                (x0, y0 - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.72, 40, 2, cv2.LINE_AA)
    cv2.arrowedLine(page, (cx, my0 - 14), (cx, my0 - 82), 40, 3, tipLength=0.35)
    cv2.putText(page, "+X FRENTE", (cx + 14, my0 - 38),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, 40, 2, cv2.LINE_AA)

# Regla de verificación de 50 mm abajo
rx0, ry0 = int(a4_w * 0.30) - box_px // 2, int(a4_h * 0.55)
r_len = int(round(50.0 * px_mm))
cv2.line(page, (rx0, ry0), (rx0 + r_len, ry0), 0, 4)
cv2.line(page, (rx0, ry0 - 14), (rx0, ry0 + 14), 0, 3)
cv2.line(page, (rx0 + r_len, ry0 - 14), (rx0 + r_len, ry0 + 14), 0, 3)
cv2.putText(page, "Verificacion de escala: esta barra debe medir 50.0 mm exactos",
            (rx0, ry0 + 42), cv2.FONT_HERSHEY_SIMPLEX, 0.72, 40, 2, cv2.LINE_AA)

out_dir = ROOT / "imprimir"
out_dir.mkdir(parents=True, exist_ok=True)
png_path = out_dir / "aruco_40_vaso_45mm.png"
cv2.imwrite(str(png_path), page)
print(f"PNG guardado en: {png_path}")

try:
    from PIL import Image
    pdf_path = out_dir / "aruco_40_vaso_45mm.pdf"
    Image.fromarray(page).save(str(pdf_path), "PDF", resolution=float(DPI))
    print(f"PDF guardado en: {pdf_path}")
except ImportError:
    print("Imprime el PNG a 300 DPI / Escala 100% (o instala pillow para generar también el PDF).")