# Scripts 3 y 4: plano del piso y medición del robot

## Archivos nuevos (copiarlos a `robocoffe_vision\`)
- `geometria.py`: funciones compartidas (rayo–plano, pose del marcador, fuente de video).
- `calibrar_plano.py`: calibración extrínseca del piso. Genera `datos\plano.yaml`.
- `medir_robot.py`: localización en vivo y prueba de repetibilidad. Genera `datos\resultados_repetibilidad.csv`.

No reemplazan `common.py`, `config.yaml` ni `datos\camara.yaml`.

## Claves opcionales para `config.yaml`
Si no las agregas, se usan estos mismos valores por defecto.
```yaml
robot:
  # ... (lo que ya tienes)
  punto_evaluado: o1        # o1 = centro/eje de ruedas; o5 = punto de control 100 mm adelante
  adelanto_o5_mm: 100.0
prueba:
  # ... (lo que ya tienes)
  quieto_umbral_deg: 1.0    # durante una parada, theta tampoco debe variar más que esto
  radio_captura_mm: 300     # una parada más lejos que esto del destino = "intermedia"
  salida_mm: 20             # se considera que el robot salió de la parada al alejarse más que esto
```

## Comandos en PowerShell
```powershell
cd C:\ruta\a\robocoffe_vision          # tu carpeta

# 1) Plano del piso: área DESPEJADA, nadie pisando los marcadores
python calibrar_plano.py
#    Revisar en consola: RMS del plano <= 5 mm, altura estimada ~1940 mm,
#    ningún "AVISO ... girado". Una tecla cierra la ventana.

# 2) Medición: poner el robot en A con el marcador ID 40
python medir_robot.py
#    Con video crudo para poder re-procesar después:
python medir_robot.py --grabar

# 3) (opcional) Re-procesar un video grabado
python medir_robot.py --entrada datos\video_AAAAMMDD_HHMMSS.avi --sin_ventana --salida datos\reproceso.csv
```

## Teclas en `medir_robot.py`

| Tecla | Acción |
|---|---|
| ESPACIO | Medición manual: promedia 30 cuadros y la asigna al punto más cercano, A o B |
| R | Nueva sesión: reinicia los ciclos y abre una nueva trayectoria |
| E | Calibra la excentricidad. Pulsa E, gira el robot sobre su eje una vuelta completa y pulsa E otra vez. El script imprime la línea `offset_marcador_mm` que hay que copiar a `config.yaml` |
| Q | Salir |

Cada llegada se guarda en el CSV en el momento en que ocurre, así que no se pierde nada si la ventana se cierra de golpe.

## Orden recomendado en el laboratorio
1. Correr `calibrar_plano.py`.
2. Calibrar la excentricidad con la tecla E y copiar el resultado a `config.yaml`.
3. Validar el instrumento:
   - **Robot quieto:** con ESPACIO tomar 10 lecturas. La dispersión (`sd_x`, `sd_y`) debe ser menor a 1 mm.
   - **Exactitud:** colocar el robot en 5 puntos medidos con cinta y comparar.
4. Correr los ciclos A→B→A. El robot debe hacer una **pausa de al menos 1.5 s** en cada destino.
