# RoboCoffe – Medición de repetibilidad por visión (scripts 1 y 2)

## Instalación (laptop)
```
pip install -r requirements.txt
```

## Paso 1 – Imprimir marcadores
```
python generar_marcadores.py
```
Genera en `imprimir/`:
- `charuco_tabloide.pdf`: tablero de calibración, doble carta.
- `marcadores_piso.pdf`: 6 hojas carta, IDs 30–35.
- `marcador_robot.pdf`: ID 40.

Reglas de impresión:
- Imprimir en **papel mate** y a **tamaño real (100 %)**, nunca con "ajustar a la página".
- Medir con regla la barra de 100 mm. Si no mide 100 mm, corregir `lado_cuadro_mm`, `lado_marcador_mm` y los lados en `config.yaml`. No hace falta reimprimir.
- Pegar el tablero ChArUco sobre algo rígido y plano.

## Paso 2 – Calibrar el lente
Con la cámara ya montada en su soporte y el foco fijo:
```
python calibrar_camara.py capturar    # 25–40 fotos, mover el tablero por TODA la imagen
python calibrar_camara.py calibrar    # crea datos/camara.yaml
python calibrar_camara.py verificar   # las líneas de la loseta deben verse rectas
```
Criterio para avanzar: **RMS ≤ 0.5 px** y el mensaje "área 1800x1200: CABE".

## Montaje del área (2 × 3 losetas = 1200 × 1800 mm)
```
 y=1200 ┌──────────────┬──────────────┬──────────────┐
        │ [33]         │     [34]     │         [35] │
        │              │              │              │
  y=600 │     A ●──────┼──────────────┼───● B        │   A=(400,600)  B=(1400,600)
        │              │              │              │
        │ [30]         │     [31]     │         [32] │
   y=0  └──────────────┴──────────────┴──────────────┘
       x=0           600            1200          1800
```
- Cada marcador va con su centro a 100 mm hacia adentro de las líneas y con la flecha "+X" apuntando hacia x creciente.
- Medir los centros reales con cinta y anotarlos en `config.yaml` → `marcadores_piso.posiciones`.
