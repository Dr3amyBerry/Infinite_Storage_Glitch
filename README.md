# ♾️ Infinite Storage Glitch (ISG) - Edición GPU 🚀

¡Bienvenido a **Infinite Storage Glitch**! Este proyecto es una herramienta loca y genial que te permite **guardar archivos de cualquier tipo dentro de videos** 📹. Sí, leíste bien. Convertimos tus archivos en "ruido" visual (píxeles blancos y negros) que puedes subir a YouTube (o cualquier sitio de video) y obtener **almacenamiento ilimitado y gratuito**. 🤯

## 🧠 ¿Cómo funciona la Magia? (Lógica Detallada)

Aquí te explico el "detrás de cámaras" de por qué el código es como es. 👇

### 1. ⬛⬜ ¿Por qué Grises (Grayscale)?
En lugar de usar colores (RGB), usamos **Escala de Grises** (blanco y negro puro).
*   **Razón**: La compresión de video de YouTube (y otros) es brutal con el color (submuestreo de croma). Sin embargo, la **luminancia** (el brillo, o blanco/negro) se conserva con mucha más fidelidad.
*   **En el código**: Convertimos tus bits (0s y 1s) directamente a píxeles: `0` -> Blanco (255), `1` -> Negro (0). Esto maximiza el contraste y facilita que el programa recupere los datos incluso si el video se ve un poco "borroso".

### 2. 🧱 Píxeles de 4x4 (Macro-Píxeles)
Si miras el código, verás una constante `pixel_size = 4`. Esto significa que cada "bit" de tu archivo no es 1 píxel de pantalla, sino un bloque de **4x4 píxeles**.
*   **¿Por qué?**: Si usáramos 1 píxel por bit, la compresión de video (H.264/VP9) destruiría la información al intentar "suavizar" la imagen.
*   **La Solución**: Al hacer los "píxeles de datos" más grandes (bloques de 4x4), creamos una redundancia masiva. Incluso si YouTube comprime los bordes del bloque, el centro del bloque (que es lo que leemos) se mantiene intacto. ¡Es como un escudo contra la compresión! 🛡️

### 3. 🏷️ La Cabecera Inteligente (Smart Header)
No solo guardamos "ruido". Al principio de cada video, inyectamos una **Cabecera Oculta** (Metadata).
*   **Estructura**: `[MAGIC "ISG2"] + [Largo del Header] + [JSON con Datos]`
*   **¿Qué guarda?**:
    *   📄 **Nombre original del archivo**: Para que al recuperarlo no se llame "video_recuperado.bin".
    *   💾 **Tamaño exacto**: Para cortar los bytes de relleno al final.
    *   ⚙️ **Versión**: Para saber con qué versión se creó.
*   **Magia**: Cuando cargas un video para decodificar, el programa lee estos primeros bytes y te dice: *"¡Hey! Encontré un archivo llamado 'foto_secreta.jpg' dentro de este video. ¿Quieres recuperarlo?"*. 😎

### 4. ⚡ Aceleración por GPU
El código detecta si tienes **NVIDIA**, **AMD** o **Intel** y usa comandos especiales de `ffmpeg` (`h264_nvenc`, `h264_amf`, etc.) para que la conversión sea **ultra rápida**. ¡Nada de esperar horas!

---

## 🛠️ Requisitos e Instalación

Necesitas tener **Python** y **FFmpeg** instalados.

1.  **Instala las librerías de Python**:
    ```bash
    pip install -r requirements.txt
    ```
    *(Esto instalará `customtkinter`, `numpy` y `yt-dlp`)*

2.  **Instala FFmpeg**:
    *   Es el motor que hace todo el trabajo duro de video. Asegúrate de que `ffmpeg` esté en tu variable de entorno PATH.

## 🚀 Cómo Usar

### 📤 Codificar (Subir Archivo)
1.  Abre la app.
2.  Ve a la pestaña **"Codificar"**.
3.  Selecciona tu archivo.
4.  Elige tu GPU (o CPU si eres humilde).
5.  Dale a **"Generar Video"**.
6.  ¡Sube ese video a YouTube!

### 📥 Decodificar (Recuperar Archivo)
1.  Ve a la pestaña **"YouTube"** y pega el link del video (o descarga el video manualmente).
2.  Ve a la pestaña **"Decodificar"**.
3.  Selecciona el video descargado.
4.  Dale a **"Analizar y Recuperar"**.
5.  ¡Magia! Tu archivo original aparecerá en la carpeta de salida. ✨

---

## 🤓 Estructura del Proyecto

*   `main.py`: El cerebro de la operación. Contiene la interfaz gráfica (CustomTkinter) y la lógica de codificación/decodificación.
*   `requirements.txt`: Lista de ingredientes necesarios.
*   `README.md`: Este hermoso manual que estás leyendo.

---
*Creado con ❤️ y un poco de locura por el equipo de Infinite Storage Glitch.*
